"""Monitor router — 异步任务队列模式
   POST /api/monitor/run → 创建任务并立即返回 task_id
   GET  /api/monitor/task/{task_id} → 轮询任务进度
   GET  /api/monitor/results → 获取历史结果
   GET  /api/monitor/history → 获取历史统计
"""
from fastapi import APIRouter, HTTPException, Query
import json
import subprocess
import sys
from typing import Optional
from pathlib import Path
from datetime import datetime

from app.database import get_db
from app.models import MonitorRunRequest
from app.services.task_queue import create_task, get_task, list_tasks, cancel_task, delete_task
from app.services.monitor.ai_monitor import check_all_login_status
from app.services.platform_adapters.runner import _extract_sources_from_text
from app.services.evidence import enrich_evidence_row, verify_source_url, validate_monitor_evidence

router = APIRouter(prefix="/api/monitor", tags=["monitor"])

EXECUTOR = Path(__file__).parent.parent / "services" / "monitor" / "executor.py"
BACKEND_DIR = str(Path(__file__).parent.parent.parent)

ALL_PLATFORMS = ["web", "doubao", "qianwen", "yuanbao"]


@router.post("/run")
def run_monitor(request: MonitorRunRequest):
    """
    创建监测任务 — 立即返回 task_id
    前端统一轮询 GET /api/monitor/task/{task_id} 获取进度
    """
    if not request.keyword_ids:
        raise HTTPException(status_code=400, detail="No keyword ids provided")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM brands WHERE id = ?", (request.brand_id,))
        brand = cursor.fetchone()
        if not brand:
            raise HTTPException(status_code=404, detail="Brand not found")
        brand = dict(brand)

        placeholders = ",".join(["?" for _ in request.keyword_ids])
        cursor.execute(
            f"SELECT * FROM keywords WHERE id IN ({placeholders}) AND brand_id = ?",
            [*request.keyword_ids, request.brand_id]
        )
        keywords = [{"id": row["id"], "word": row["word"], "brand_id": row["brand_id"]}
                    for row in cursor.fetchall()]

    if not keywords:
        raise HTTPException(status_code=400, detail="No valid keywords found")

    # 确定平台
    if request.platform == "all":
        platforms = ALL_PLATFORMS
    elif request.platform == "ai":
        platforms = ["doubao", "qianwen", "yuanbao"]
    elif request.platform in ("doubao", "yuanbao", "qianwen", "web"):
        platforms = [request.platform]
    else:
        raise HTTPException(status_code=400, detail=f"未知监测平台: {request.platform}")

    total_kws = len(keywords) * len(platforms)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO monitor_runs
               (brand_id, platforms, status, total_keywords, total_operations)
               VALUES (?, ?, 'pending', ?, ?)""",
            (request.brand_id, ",".join(platforms), len(keywords), total_kws),
        )
        run_id = cursor.lastrowid

    task_params = {
        "brand_name": brand["name"],
        "brand_id": request.brand_id,
        "keywords": keywords,
        "platforms": platforms,
        "run_id": run_id,
        "run_mode": request.run_mode or "manual",
        "timeout": int(getattr(request, "timeout", 0) or 90),
        "auto_rounds": int(request.auto_rounds or 1),
        "scheduled_start_at": request.scheduled_start_at,
        "interval_minutes": int(request.interval_minutes or 0),
    }

    task_id = create_task("monitor", task_params, priority=1)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE monitor_runs SET task_id = ? WHERE id = ?", (task_id, run_id))

    return {
        "task_id": task_id,
        "run_id": run_id,
        "brand_name": brand["name"],
        "total_operations": total_kws,
        "total_keywords": len(keywords),
        "platforms": platforms,
        "run_mode": request.run_mode or "manual",
        "auto_rounds": int(request.auto_rounds or 1),
        "scheduled_start_at": request.scheduled_start_at,
        "interval_minutes": int(request.interval_minutes or 0),
        "status": "pending",
        "message": f"任务已创建 (task_id={task_id})，共 {total_kws} 次检测操作",
    }


@router.get("/task/{task_id}")
def get_task_status(task_id: int):
    """轮询任务进度"""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # task.get("result", {}) 在列为 NULL 时返回 None（键存在、值为 None），
    # pending/running 任务 result 尚未写入，若不归一化会在下方 result.get 处 500
    result = task.get("result") or {}
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            result = {}

    run = None
    answers = []
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM monitor_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,))
        row = cursor.fetchone()
        if row:
            run = dict(row)
            cursor.execute(
                """SELECT ma.*, k.word as keyword_word, b.name as brand_name,
                          mr.is_cited, mr.source_url, mr.source_domain, mr.cited_sources,
                          mr.snippet, mr.notes, mr.total_results
                   FROM monitor_answers ma
                   LEFT JOIN monitor_results mr ON ma.monitor_result_id = mr.id
                   LEFT JOIN keywords k ON ma.keyword_id = k.id
                   LEFT JOIN brands b ON ma.brand_id = b.id
                   WHERE ma.run_id = ?
                   ORDER BY ma.monitored_at DESC, ma.id DESC""",
                (run["id"],),
            )
            answers = [dict(item) for item in cursor.fetchall()]
            answer_ids = [item["id"] for item in answers]
            sources_by_answer = {}
            if answer_ids:
                placeholders = ",".join("?" for _ in answer_ids)
                cursor.execute(
                    f"SELECT * FROM monitor_sources WHERE answer_id IN ({placeholders}) ORDER BY answer_id, source_order",
                    answer_ids,
                )
                for src in cursor.fetchall():
                    d = dict(src)
                    sources_by_answer.setdefault(d["answer_id"], []).append(d)
            for answer in answers:
                answer["sources"] = sources_by_answer.get(answer["id"], [])
                answer["is_cited"] = bool(answer.get("is_cited") or answer.get("brand_mentioned"))
                answer["keyword_word"] = answer.get("keyword_word") or ""
                answer["verification_status"] = answer.get("verification_status") or "not_cited"
                answer.update(enrich_evidence_row(answer))

    return {
        "task_id": task["id"],
        "run": run,
        "status": task.get("status"),
        "task_type": task.get("task_type"),
        "progress_pct": task.get("progress_pct", 0),
        "progress_msg": task.get("progress_msg", ""),
        "error_msg": task.get("error_msg"),
        "result": result if task.get("status") in ("completed", "failed") else None,
        "business_status": (run or {}).get("business_status") or result.get("business_status"),
        "business_failure_reason": (run or {}).get("business_failure_reason") or result.get("business_failure_reason"),
        "effective_source_count": (run or {}).get("effective_source_count") or result.get("effective_source_count", 0),
        "brand_mention_count": (run or {}).get("brand_mention_count") or result.get("brand_mention_count", 0),
        "brand_mention_rate": (run or {}).get("brand_mention_rate") or result.get("brand_mention_rate", 0),
        "answers": answers,
        "created_at": task.get("created_at"),
        "completed_at": task.get("completed_at"),
    }


@router.get("/tasks")
def list_monitor_tasks(limit: int = Query(default=20)):
    """列出最近的监测任务"""
    tasks = list_tasks(task_type="monitor", limit=limit)
    run_by_task = {}
    task_ids = [t["id"] for t in tasks]
    if task_ids:
        with get_db() as conn:
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in task_ids)
            cursor.execute(
                f"""SELECT task_id, business_status, business_failure_reason,
                           effective_source_count, brand_mention_count, brand_mention_rate
                    FROM monitor_runs
                    WHERE task_id IN ({placeholders})
                    ORDER BY id DESC""",
                task_ids,
            )
            for row in cursor.fetchall():
                d = dict(row)
                run_by_task.setdefault(d["task_id"], d)
    # 简化返回
    simplified = []
    for t in tasks:
        run = run_by_task.get(t["id"], {})
        simplified.append({
            "id": t["id"],
            "status": t.get("status"),
            "progress_pct": t.get("progress_pct", 0),
            "progress_msg": t.get("progress_msg", ""),
            "business_status": run.get("business_status"),
            "business_failure_reason": run.get("business_failure_reason"),
            "effective_source_count": run.get("effective_source_count", 0),
            "brand_mention_count": run.get("brand_mention_count", 0),
            "brand_mention_rate": run.get("brand_mention_rate", 0),
            "created_at": t.get("created_at"),
            "completed_at": t.get("completed_at"),
        })
    return simplified


@router.delete("/tasks/{task_id}")
def delete_monitor_task(task_id: int):
    task = get_task(task_id)
    if not task or task.get("task_type") != "monitor":
        raise HTTPException(status_code=404, detail="Monitor task not found")
    if task.get("status") in {"running", "pending"}:
        cancel_task(task_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE monitor_runs SET status = 'cancelled', completed_at = ?, error_msg = ? WHERE task_id = ?", (datetime.now().isoformat(), "任务已取消并从列表删除", task_id))
        task = get_task(task_id) or task
    ok = delete_task(task_id, task_type="monitor")
    if not ok:
        raise HTTPException(status_code=400, detail="任务删除失败")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE monitor_runs SET task_id = NULL WHERE task_id = ?", (task_id,))
    return {"deleted": True, "task_id": task_id}


@router.post("/tasks/{task_id}/cancel")
def cancel_monitor_task(task_id: int):
    task = get_task(task_id)
    if not task or task.get("task_type") != "monitor":
        raise HTTPException(status_code=404, detail="Monitor task not found")
    ok = cancel_task(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="只有等待中/运行中任务可取消")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE monitor_runs SET status = 'cancelled', completed_at = ? WHERE task_id = ?", (datetime.now().isoformat(), task_id))
    return {"cancelled": True, "task_id": task_id}


@router.get("/results")
def get_monitor_results(
    brand_id: Optional[int] = Query(None),
    limit: int = Query(default=50),
    platform: Optional[str] = Query(None),
):
    """获取监测结果"""
    with get_db() as conn:
        cursor = conn.cursor()
        where = []
        params = []
        if brand_id:
            where.append("mr.brand_id = ?")
            params.append(brand_id)
        if platform:
            where.append("mr.platform = ?")
            params.append(platform)
        where_sql = "WHERE " + " AND ".join(where) if where else ""

        cursor.execute(
            f"""SELECT mr.*, k.word as keyword_word, b.name as brand_name,
                       ma.id as answer_id, ma.answer_text as answer_text_full,
                       ma.verification_status as answer_verification_status,
                       ma.failure_reason as answer_failure_reason
                FROM monitor_results mr
                LEFT JOIN keywords k ON mr.keyword_id = k.id
                LEFT JOIN brands b ON mr.brand_id = b.id
                LEFT JOIN monitor_answers ma ON ma.monitor_result_id = mr.id
                {where_sql} ORDER BY mr.monitored_at DESC LIMIT ?""",
            params + [limit],
        )
        rows = [dict(row) for row in cursor.fetchall()]
        answer_ids = [row["answer_id"] for row in rows if row.get("answer_id")]
        sources_by_answer = {}
        if answer_ids:
            placeholders = ",".join("?" for _ in answer_ids)
            cursor.execute(
                f"SELECT * FROM monitor_sources WHERE answer_id IN ({placeholders}) ORDER BY answer_id, source_order",
                answer_ids,
            )
            for src in cursor.fetchall():
                d = dict(src)
                sources_by_answer.setdefault(d["answer_id"], []).append(d)
        for row in rows:
            row["sources"] = sources_by_answer.get(row.get("answer_id"), [])
            row["answer_text"] = row.get("answer_text") or row.get("answer_text_full") or ""
            row["verification_status"] = row.get("verification_status") or row.get("answer_verification_status") or "not_cited"
            row["failure_reason"] = row.get("failure_reason") or row.get("answer_failure_reason")
            row.update(enrich_evidence_row(row))
        return rows


@router.get("/runs/{run_id}")
def get_monitor_run(run_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM monitor_runs WHERE id = ?", (run_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Run not found")
        run = dict(row)
        cursor.execute(
            """SELECT ma.*, k.word as keyword_word
               FROM monitor_answers ma
               LEFT JOIN keywords k ON ma.keyword_id = k.id
               WHERE ma.run_id = ?
               ORDER BY ma.monitored_at DESC""",
            (run_id,),
        )
        answers = [dict(r) for r in cursor.fetchall()]
        answer_ids = [a["id"] for a in answers]
        sources_by_answer = {}
        if answer_ids:
            placeholders = ",".join("?" for _ in answer_ids)
            cursor.execute(
                f"SELECT * FROM monitor_sources WHERE answer_id IN ({placeholders}) ORDER BY answer_id, source_order",
                answer_ids,
            )
            for src in cursor.fetchall():
                d = dict(src)
                sources_by_answer.setdefault(d["answer_id"], []).append(d)
        for answer in answers:
            answer["sources"] = sources_by_answer.get(answer["id"], [])
            answer.update(enrich_evidence_row(answer))
        run["answers"] = answers
        return run


@router.get("/evidence-stats")
def get_evidence_stats(brand_id: Optional[int] = Query(None)):
    with get_db() as conn:
        cursor = conn.cursor()
        where = "WHERE brand_id = ?" if brand_id else ""
        params = [brand_id] if brand_id else []
        cursor.execute(
            f"""SELECT COUNT(*) as total,
                       SUM(CASE WHEN is_cited = 1 THEN 1 ELSE 0 END) as cited,
                       SUM(CASE WHEN verification_status = 'verified_citation' AND source_verified = 1 THEN 1 ELSE 0 END) as verified_cited,
                       SUM(CASE WHEN verification_status = 'unverified_citation' THEN 1 ELSE 0 END) as unverified_cited,
                       SUM(CASE WHEN source_verified = 1 THEN 1 ELSE 0 END) as with_source_url,
                       SUM(CASE WHEN screenshot_verified = 1 THEN 1 ELSE 0 END) as with_screenshot,
                       SUM(CASE WHEN answer_text IS NOT NULL AND answer_text != '' THEN 1 ELSE 0 END) as with_answer_text,
                       SUM(CASE WHEN failure_reason IS NOT NULL AND failure_reason != '' THEN 1 ELSE 0 END) as failed
                FROM monitor_results {where}""",
            params,
        )
        row = dict(cursor.fetchone())
        total = row.get("total") or 0
        row["evidence_complete_rate"] = round((row.get("verified_cited") or 0) / total * 100, 2) if total else 0
        return row


@router.post("/backfill-sources")
def backfill_sources_from_answers(
    brand_id: Optional[int] = Query(None),
    limit: int = Query(default=200),
):
    """从已保存回答原文中回填来源链接。

    只提取回答文本里已经存在的 URL，不生成或猜测任何来源；用于修复早期
    RPA 抓到答案但未把明文链接写入 monitor_sources 的记录。
    """
    updated_answers = 0
    inserted_sources = 0
    updated_results = 0
    with get_db() as conn:
        cursor = conn.cursor()
        where = ["(ma.answer_text IS NOT NULL AND ma.answer_text != '')"]
        params = []
        if brand_id:
            where.append("ma.brand_id = ?")
            params.append(brand_id)
        cursor.execute(
            f"""SELECT ma.*, mr.id as result_id, mr.source_url as result_source_url,
                       mr.is_cited as result_is_cited
                FROM monitor_answers ma
                LEFT JOIN monitor_results mr ON ma.monitor_result_id = mr.id
                WHERE {' AND '.join(where)}
                ORDER BY ma.monitored_at DESC, ma.id DESC
                LIMIT ?""",
            params + [limit],
        )
        answers = [dict(row) for row in cursor.fetchall()]
        for answer in answers:
            sources = [source.__dict__ for source in _extract_sources_from_text(answer.get("answer_text") or "")]
            verified_sources = []
            for source in sources:
                ok, reason = verify_source_url(source.get("url"), live_check=True)
                if ok:
                    source["source_verified"] = True
                    source["check_status"] = reason or "verified"
                    verified_sources.append(source)
            sources = verified_sources
            if not sources:
                continue
            inserted_for_answer = 0
            cursor.execute("SELECT url FROM monitor_sources WHERE answer_id = ?", (answer["id"],))
            existing = {row["url"] for row in cursor.fetchall()}
            for idx, source in enumerate(sources, start=1):
                if source["url"] in existing:
                    continue
                cursor.execute(
                    """INSERT INTO monitor_sources
                       (answer_id, title, url, domain, source_order, is_brand_owned, is_third_party,
                        source_verified, check_status, checked_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        answer["id"],
                        source.get("title"),
                        source.get("url"),
                        source.get("domain"),
                        source.get("source_order") or idx,
                        1 if source.get("is_brand_owned") else 0,
                        0 if source.get("is_brand_owned") else 1,
                        1,
                        source.get("check_status") or "verified",
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                inserted_sources += 1
                inserted_for_answer += 1
            if inserted_for_answer:
                updated_answers += 1
            first = sources[0]
            evidence = validate_monitor_evidence(
                brand_mentioned=bool(answer.get("brand_mentioned")),
                is_cited=bool(answer.get("result_is_cited")),
                source_url=first.get("url"),
                screenshot_path=answer.get("screenshot_path"),
                raw_artifact_path=answer.get("raw_artifact_path"),
                live_url_check=False,
            )
            verification_status = evidence["verification_status"]
            cursor.execute(
                """UPDATE monitor_answers
                   SET verification_status = ?,
                       source_verified = ?, screenshot_verified = ?,
                       raw_artifact_verified = ?, evidence_status = ?,
                       evidence_failure_reason = ?
                   WHERE id = ?""",
                (
                    verification_status,
                    evidence["source_verified"],
                    evidence["screenshot_verified"],
                    evidence["raw_artifact_verified"],
                    evidence["evidence_status"],
                    evidence["evidence_failure_reason"],
                    answer["id"],
                ),
            )
            if answer.get("result_id") and not answer.get("result_source_url"):
                cursor.execute(
                    """UPDATE monitor_results
                       SET source_url = ?, source_domain = ?, cited_sources = COALESCE(cited_sources, ?),
                           verification_status = ?, has_verified_source = ?,
                           total_results = ?, source_verified = ?, screenshot_verified = ?,
                           raw_artifact_verified = ?, evidence_status = ?, evidence_failure_reason = ?
                       WHERE id = ?""",
                    (
                        first.get("url"),
                        first.get("domain"),
                        " | ".join((s.get("title") or s.get("domain") or s.get("url") or "")[:120] for s in sources[:5]),
                        verification_status,
                        evidence["has_verified_source"],
                        len(sources),
                        evidence["source_verified"],
                        evidence["screenshot_verified"],
                        evidence["raw_artifact_verified"],
                        evidence["evidence_status"],
                        evidence["evidence_failure_reason"],
                        answer["result_id"],
                    ),
                )
                updated_results += 1

    return {
        "updated_answers": updated_answers,
        "inserted_sources": inserted_sources,
        "updated_results": updated_results,
    }


@router.get("/history")
def get_monitor_history(brand_id: Optional[int] = Query(None)):
    """获取历史统计"""
    with get_db() as conn:
        cursor = conn.cursor()
        query = """SELECT DATE(monitored_at) as date, COUNT(*) as total,
                          SUM(is_cited) as cited, platform
                   FROM monitor_results"""
        params = []
        if brand_id:
            query += " WHERE brand_id = ?"
            params.append(brand_id)
        query += " GROUP BY DATE(monitored_at), platform ORDER BY date DESC"
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


@router.post("/run-now/{brand_id}")
def trigger_scan(brand_id: int):
    """触发品牌即时扫描 — 同步模式（供后台定时任务调用）"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM keywords WHERE brand_id = ?", (brand_id,))
        keyword_ids = [row["id"] for row in cursor.fetchall()]
    if not keyword_ids:
        raise HTTPException(status_code=400, detail="该品牌暂无关键词")
    return run_monitor(MonitorRunRequest(
        brand_id=brand_id,
        keyword_ids=keyword_ids,
        platform="all",
    ))


@router.get("/login-status")
def login_status():
    """检查所有平台的登录状态"""
    return check_all_login_status()


@router.post("/login/{platform}")
def open_login(platform: str):
    """打开平台登录页"""
    from app.services.monitor.ai_monitor import login_platform as do_login
    result = do_login(platform)
    return result


# ── 告警 ──────────────────────────────────────────────────

@router.get("/alerts")
def get_alerts(
    brand_id: Optional[int] = Query(None),
    unread_only: bool = Query(default=False),
):
    with get_db() as conn:
        cursor = conn.cursor()
        where = []
        params = []
        if brand_id:
            where.append("brand_id = ?")
            params.append(brand_id)
        if unread_only:
            where.append("is_read = 0")
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        cursor.execute(
            f"SELECT a.*, k.word as keyword_word FROM alerts a "
            f"LEFT JOIN keywords k ON a.keyword_id = k.id "
            f"{where_sql} ORDER BY a.created_at DESC LIMIT 50",
            params,
        )
        return [dict(row) for row in cursor.fetchall()]


@router.post("/alerts/{alert_id}/read")
def mark_alert_read(alert_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE alerts SET is_read = 1 WHERE id = ?", (alert_id,))
        return {"ok": True}


@router.post("/alerts/read-all")
def mark_all_alerts_read(brand_id: Optional[int] = Query(None)):
    with get_db() as conn:
        cursor = conn.cursor()
        if brand_id:
            cursor.execute("UPDATE alerts SET is_read = 1 WHERE brand_id = ?", (brand_id,))
        else:
            cursor.execute("UPDATE alerts SET is_read = 1")
        return {"ok": True}
