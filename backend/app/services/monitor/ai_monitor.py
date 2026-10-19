#!/usr/bin/env python3
"""AI platform brand citation monitoring engine.

Commercial P1 policy:
  - web keeps using the web search monitor.
  - doubao/qianwen/yuanbao use real platform adapters.
  - no AI platform falls back to Web search pretending to be that platform.
"""
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional
from urllib.parse import urlparse

from app.database import get_db, local_now
from app.services.evidence import validate_monitor_evidence, verify_source_url
from app.services.monitor.quality_gate import summarize_monitor_quality
from app.services.monitor.base import CitationResult
from app.services.platform_adapters import (
    check_all_platform_sessions,
    check_platform_session,
    open_login_browser,
    run_platform_monitor,
)
from app.services.platform_adapters.runner import _extract_sources_from_text
from app.services.task_queue import TASK_TYPE_AI_DETECT, TASK_TYPE_MONITOR, is_task_cancelled


AI_PLATFORMS = ["doubao", "qianwen", "yuanbao"]
ALL_PLATFORMS = ["web", *AI_PLATFORMS]

# 同一个平台两道题之间的随机冷却（秒）。批量监测即使平台内已串行，短时间连续提问
# 仍然容易触发平台风控；这里在题目之间加一段随机停顿，把"连续轰炸"摊开。
# web（搜索引擎请求、不涉及账号）不需要冷却；设 0 可关闭。
# 可用环境变量调整，例如 GEO_MONITOR_KEYWORD_COOLDOWN_LOW=0 / _HIGH=0。
_KEYWORD_COOLDOWN_LOW = float(os.getenv("GEO_MONITOR_KEYWORD_COOLDOWN_LOW", "20") or 0)
_KEYWORD_COOLDOWN_HIGH = float(os.getenv("GEO_MONITOR_KEYWORD_COOLDOWN_HIGH", "60") or 0)


def _keyword_cooldown(platform: str, task_id: Optional[int] = None) -> None:
    """平台内两道题之间的可取消随机冷却（元宝等敏感平台放大 1.8 倍）。"""
    if platform == "web":
        return
    low = min(_KEYWORD_COOLDOWN_LOW, _KEYWORD_COOLDOWN_HIGH)
    high = max(_KEYWORD_COOLDOWN_LOW, _KEYWORD_COOLDOWN_HIGH)
    if high <= 0:
        return
    factor = 1.8 if platform == "yuanbao" else 1.0
    deadline = time.time() + random.uniform(max(0.0, low), high) * factor
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        if is_task_cancelled(task_id):
            return
        time.sleep(min(1.0, remaining))


def monitor_platform(platform: str, brand_name: str, keyword: str, timeout: int = 90, run_mode: str = "manual", cancel_checker: Optional[Callable[[], bool]] = None) -> dict:
    start = time.time()
    if platform == "web":
        result = _web_search(brand_name, keyword)
        elapsed = round(time.time() - start, 2)
        source_ok, source_reason = verify_source_url(result.source_url, live_check=True) if result.source_url else (False, "无来源链接")
        evidence = validate_monitor_evidence(
            brand_mentioned=bool(result.is_cited),
            is_cited=bool(result.is_cited),
            source_url=result.source_url if source_ok else None,
            screenshot_path=result.screenshot_path,
            raw_artifact_path=None,
            live_url_check=False,
        )
        verification_status = evidence["verification_status"]
        return {
            "platform": "web",
            "prompt": f"{keyword} {brand_name}",
            "answer_text": result.snippet or result.cited_sources or "",
            "brand_mentioned": bool(result.is_cited),
            "mention_count": 1 if result.is_cited else 0,
            "is_cited": bool(result.is_cited),
            "verification_status": verification_status,
            "cited_sources": result.cited_sources,
            "snippet": result.snippet,
            "source_url": result.source_url if source_ok else None,
            "source_domain": result.source_domain if source_ok else None,
            "sources": ([{
                "title": result.cited_sources or result.source_domain or result.source_url,
                "url": result.source_url,
                "domain": result.source_domain,
                "source_order": 1,
                "is_brand_owned": False,
                "is_third_party": True,
                "source_verified": True,
                "check_status": "verified",
            }] if result.source_url and source_ok else []),
            "total_results": result.total_results,
            "screenshot_path": result.screenshot_path,
            "raw_artifact_path": None,
            "html_excerpt": None,
            "elapsed_seconds": elapsed,
            "failure_reason": None,
            "captcha_status": "detected" if "验证码" in (result.notes or "") else "none",
            "notes": f"网页搜索 | {result.notes or ''} | {source_reason if result.source_url and not source_ok else ''}".strip(),
            "has_verified_source": bool(evidence["has_verified_source"]),
            "source_verified": bool(evidence["source_verified"]),
            "screenshot_verified": bool(evidence["screenshot_verified"]),
            "raw_artifact_verified": bool(evidence["raw_artifact_verified"]),
            "evidence_status": evidence["evidence_status"],
            "evidence_failure_reason": evidence["evidence_failure_reason"],
        }
    if platform in AI_PLATFORMS:
        return run_platform_monitor(platform, brand_name, keyword, timeout, run_mode=run_mode, cancel_checker=cancel_checker)
    return {
        "platform": platform,
        "prompt": f"{keyword} {brand_name}",
        "is_cited": False,
        "verification_status": "failed",
        "failure_reason": f"未知平台: {platform}",
        "notes": f"未知平台: {platform}",
    }


def _web_search(brand_name: str, keyword: str) -> CitationResult:
    from app.services.monitor.search_engine import WebSearchMonitor
    monitor = WebSearchMonitor()
    try:
        return monitor.search(brand_name, keyword)
    finally:
        monitor.close()


def check_login_status(platform: str) -> dict:
    status = check_platform_session(platform)
    return {
        "is_logged_in": status.get("is_logged_in", False),
        "status": status.get("status", "unknown"),
        "notes": status.get("notes", ""),
        "port": status.get("port"),
        "profile_dir": status.get("profile_dir"),
        "captcha_required": status.get("captcha_required", False),
        "login_url": status.get("login_url"),
    }


def check_all_login_status() -> dict:
    statuses = check_all_platform_sessions()
    return {
        key: {
            "is_logged_in": value.get("is_logged_in", False),
            "status": value.get("status", "unknown"),
            "notes": value.get("notes", ""),
            "port": value.get("port"),
            "profile_dir": value.get("profile_dir"),
            "captcha_required": value.get("captcha_required", False),
            "login_url": value.get("login_url"),
        }
        for key, value in statuses.items()
    }


def login_platform(platform: str) -> dict:
    return open_login_browser(platform)


def ensure_browser_running() -> bool:
    """True if any AI monitor CDP port is already open (does not auto-launch Chrome)."""
    from app.services.platform_adapters.sessions import PLATFORMS, get_port, _is_port_open

    return any(_is_port_open(get_port(name)) for name in PLATFORMS)


def _check_cdp_alive() -> bool:
    return any(check_platform_session(p).get("status") in ("logged_in", "browser_open") for p in AI_PLATFORMS)


def _create_monitor_run(
    brand_id: Optional[int],
    platforms: list,
    keyword_count: int,
    task_id: Optional[int] = None,
    run_id: Optional[int] = None,
) -> Optional[int]:
    if not brand_id:
        return None
    with get_db() as conn:
        cursor = conn.cursor()
        if run_id:
            cursor.execute(
                """UPDATE monitor_runs SET status = 'running', started_at = ?,
                   platforms = ?, total_keywords = ?, total_operations = ?, task_id = COALESCE(?, task_id)
                   WHERE id = ?""",
                (local_now(), ",".join(platforms), keyword_count, keyword_count * len(platforms), task_id, run_id),
            )
            return run_id
        cursor.execute(
            """INSERT INTO monitor_runs
               (brand_id, task_id, platforms, status, total_keywords, total_operations, started_at)
               VALUES (?, ?, ?, 'running', ?, ?, ?)""",
            (brand_id, task_id, ",".join(platforms), keyword_count, keyword_count * len(platforms), local_now()),
        )
        return cursor.lastrowid


def _update_monitor_run(run_id: Optional[int], results: list) -> None:
    if not run_id:
        return
    success = sum(1 for r in results if not r.get("failure_reason"))
    failed = sum(1 for r in results if r.get("failure_reason"))
    verified = sum(1 for r in results if r.get("verification_status") == "verified_citation")
    unverified = sum(1 for r in results if r.get("verification_status") == "unverified_citation")
    quality = summarize_monitor_quality(results)
    status = "completed" if failed == 0 else ("failed" if success == 0 else "completed_with_errors")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE monitor_runs SET status = ?, success_count = ?, failed_count = ?,
               verified_citation_count = ?, unverified_citation_count = ?,
               business_status = ?, business_failure_reason = ?, effective_source_count = ?,
               brand_mention_count = ?, brand_mention_rate = ?,
               completed_at = ?, error_msg = ?
               WHERE id = ?""",
            (
                status,
                success,
                failed,
                verified,
                unverified,
                quality["business_status"],
                quality["business_failure_reason"],
                quality["effective_source_count"],
                quality["brand_mention_count"],
                quality["brand_mention_rate"],
                local_now(),
                None if failed == 0 and quality["business_status"] == "passed" else (
                    quality["business_failure_reason"] or (f"{failed} 项监测失败或待处理" if failed else "业务质量门禁未通过")
                ),
                run_id,
            ),
        )


def _mark_monitor_run_cancelled(run_id: Optional[int]) -> None:
    if not run_id:
        return
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE monitor_runs SET status = 'cancelled', completed_at = ?, error_msg = ? WHERE id = ?",
            (local_now(), "任务已取消", run_id),
        )


def _save_monitor_evidence(run_id: Optional[int], brand_id: int, keyword_id: int, platform: str, keyword_word: str, result: dict) -> int:
    cited_sources = result.get("cited_sources")
    if isinstance(cited_sources, (list, dict)):
        cited_sources = json.dumps(cited_sources, ensure_ascii=False)

    sources = result.get("sources") or []
    if not sources and result.get("answer_text"):
        sources = [source.__dict__ for source in _extract_sources_from_text(result.get("answer_text") or "")]

    verified_sources = []
    for source in sources:
        url = source.get("url")
        if not url:
            continue
        ok, reason = verify_source_url(url, live_check=True)
        if not ok:
            continue
        source["source_verified"] = True
        source["check_status"] = reason or "verified"
        verified_sources.append(source)
    sources = verified_sources

    first_url = result.get("source_url") or (sources[0].get("url") if sources else None)
    if first_url:
        first_ok, _ = verify_source_url(first_url, live_check=True)
        if not first_ok:
            first_url = None
    first_domain = result.get("source_domain") or (sources[0].get("domain") if sources else None)
    if first_url and not first_domain:
        first_domain = urlparse(first_url).netloc
    evidence = validate_monitor_evidence(
        brand_mentioned=bool(result.get("brand_mentioned") or result.get("is_cited")),
        is_cited=bool(result.get("is_cited")),
        source_url=first_url,
        screenshot_path=result.get("screenshot_path"),
        raw_artifact_path=result.get("raw_artifact_path"),
        live_url_check=False,
    )
    verification_status = "failed" if result.get("failure_reason") else evidence["verification_status"]
    has_verified_source = evidence["has_verified_source"] if verification_status == "verified_citation" else 0
    prompt = result.get("prompt") or f"{keyword_word} {result.get('brand_name','')}".strip()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO monitor_results
               (brand_id, keyword_id, platform, search_query, is_cited,
                cited_sources, snippet, source_url, source_domain,
                screenshot_path, scan_mode, notes, monitored_at,
                answer_text, verification_status, has_verified_source,
                raw_artifact_path, failure_reason, elapsed_seconds, captcha_status,
                total_results, source_verified, screenshot_verified, raw_artifact_verified,
                evidence_status, evidence_failure_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                brand_id,
                keyword_id,
                platform,
                prompt,
                1 if result.get("is_cited") else 0,
                cited_sources,
                result.get("snippet") or (result.get("answer_text") or "")[:500],
                first_url,
                first_domain,
                result.get("screenshot_path"),
                "auto",
                result.get("notes"),
                local_now(),
                result.get("answer_text"),
                verification_status,
                has_verified_source,
                result.get("raw_artifact_path"),
                result.get("failure_reason"),
                result.get("elapsed_seconds"),
                result.get("captcha_status") or "none",
                len(sources),
                evidence["source_verified"],
                evidence["screenshot_verified"],
                evidence["raw_artifact_verified"],
                evidence["evidence_status"],
                evidence["evidence_failure_reason"],
            ),
        )
        monitor_result_id = cursor.lastrowid
        cursor.execute(
            """INSERT INTO monitor_answers
               (run_id, monitor_result_id, brand_id, keyword_id, platform, prompt,
                answer_text, brand_mentioned, mention_count, rank_position,
                verification_status, screenshot_path, raw_artifact_path, html_excerpt,
                elapsed_seconds, failure_reason, captcha_status, monitored_at,
                source_verified, screenshot_verified, raw_artifact_verified,
                evidence_status, evidence_failure_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                monitor_result_id,
                brand_id,
                keyword_id,
                platform,
                prompt,
                result.get("answer_text"),
                1 if (result.get("brand_mentioned") or result.get("is_cited")) else 0,
                result.get("mention_count") or 0,
                result.get("rank_position"),
                verification_status,
                result.get("screenshot_path"),
                result.get("raw_artifact_path"),
                result.get("html_excerpt"),
                result.get("elapsed_seconds"),
                result.get("failure_reason"),
                result.get("captcha_status") or "none",
                local_now(),
                evidence["source_verified"],
                evidence["screenshot_verified"],
                evidence["raw_artifact_verified"],
                evidence["evidence_status"],
                evidence["evidence_failure_reason"],
            ),
        )
        answer_id = cursor.lastrowid
        for idx, source in enumerate(sources, start=1):
            url = source.get("url")
            if not url:
                continue
            cursor.execute(
                """INSERT INTO monitor_sources
                   (answer_id, title, url, domain, source_order, is_brand_owned, is_third_party,
                    source_verified, check_status, checked_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    answer_id,
                    source.get("title"),
                    url,
                    source.get("domain"),
                    source.get("source_order") or idx,
                    1 if source.get("is_brand_owned") else 0,
                    0 if source.get("is_brand_owned") else 1,
                    1,
                    source.get("check_status") or "verified",
                    local_now(),
                ),
            )

        keyword_status = "cited" if verification_status == "verified_citation" else (
            "unverified_citation" if verification_status == "unverified_citation" else "not_found"
        )
        cursor.execute(
            "UPDATE keywords SET last_tested = ?, ai_status = ?, ai_platform = ? WHERE id = ?",
            (local_now(), keyword_status, platform, keyword_id),
        )
        return monitor_result_id


def _update_task_progress(task_id: Optional[int], progress_pct: int, progress_msg: str) -> None:
    if not task_id:
        return
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE task_queue
               SET progress_pct = ?, progress_msg = ?
               WHERE id = ?""",
            (max(0, min(100, int(progress_pct))), progress_msg[:240], task_id),
        )
        conn.commit()


def _monitor_one_keyword(
    run_id: Optional[int],
    brand_name: str,
    brand_id: Optional[int],
    platform: str,
    keyword: dict,
    timeout: int,
    run_mode: str = "manual",
    task_id: Optional[int] = None,
) -> dict:
    try:
        result = monitor_platform(
            platform,
            brand_name,
            keyword["word"],
            timeout=timeout,
            run_mode=run_mode,
            cancel_checker=(lambda: is_task_cancelled(task_id)) if task_id else None,
        )
        result["platform"] = platform
        result["brand_name"] = brand_name
        result["brand_id"] = brand_id
        result["keyword_word"] = keyword["word"]
        result["keyword_id"] = keyword["id"]
        result["run_id"] = run_id
        if brand_id:
            result["monitor_result_id"] = _save_monitor_evidence(
                run_id, brand_id, keyword["id"], platform, keyword["word"], result
            )
        return result
    except Exception as exc:
        failed = {
            "platform": platform,
            "brand_name": brand_name,
            "brand_id": brand_id,
            "keyword_word": keyword.get("word"),
            "keyword_id": keyword.get("id"),
            "run_id": run_id,
            "is_cited": False,
            "verification_status": "failed",
            "failure_reason": f"异常: {str(exc)[:200]}",
            "notes": f"异常: {str(exc)[:200]}",
        }
        if brand_id and keyword.get("id"):
            try:
                failed["monitor_result_id"] = _save_monitor_evidence(
                    run_id, brand_id, keyword["id"], platform, keyword["word"], failed
                )
            except Exception:
                pass
        return failed


def _run_platform_serial(
    run_id: Optional[int],
    brand_name: str,
    brand_id: Optional[int],
    platform: str,
    keywords: list,
    timeout: int,
    task_id: Optional[int],
    progress: dict,
    progress_lock: threading.Lock,
    total_steps: int,
    run_mode: str = "manual",
) -> list:
    platform_results = []
    total_keywords = max(1, len(keywords))
    for index, keyword in enumerate(keywords, start=1):
        with progress_lock:
            if is_task_cancelled(task_id):
                return platform_results
            completed_steps = progress["completed"]
            step_label = f"{platform} {index}/{total_keywords} {keyword.get('word', '')[:18]}"
            progress["current"][platform] = f"{index}/{total_keywords} {keyword.get('word', '')[:18]}"
            msg = f"执行中 {step_label}；已完成 {completed_steps}/{total_steps}；" + "；".join(
                f"{plat}:{state}" for plat, state in progress["current"].items()
            )
            _update_task_progress(task_id, int((progress["completed"] / total_steps) * 100), msg)

        result = _monitor_one_keyword(run_id, brand_name, brand_id, platform, keyword, timeout, run_mode=run_mode, task_id=task_id)
        if is_task_cancelled(task_id):
            return platform_results
        platform_results.append(result)

        with progress_lock:
            progress["completed"] += 1
            if is_task_cancelled(task_id):
                return platform_results
            completed_steps = progress["completed"]
            progress["current"][platform] = f"{index}/{total_keywords} 完成"
            step_label = f"{platform} {index}/{total_keywords} 完成"
            msg = f"执行中 {step_label}；已完成 {completed_steps}/{total_steps}；" + "；".join(
                f"{plat}:{state}" for plat, state in progress["current"].items()
            )
            _update_task_progress(task_id, int((progress["completed"] / total_steps) * 100), msg)

        # 同平台两题之间冷却（最后一题不等）：把连续提问摊开，降低平台风控/封号概率。
        if index < total_keywords:
            with progress_lock:
                progress["current"][platform] = f"{index}/{total_keywords} 冷却中"
                _update_task_progress(
                    task_id,
                    int((progress["completed"] / total_steps) * 100),
                    f"冷却中 {platform} {index}/{total_keywords}；已完成 {progress['completed']}/{total_steps}",
                )
            _keyword_cooldown(platform, task_id)
            if is_task_cancelled(task_id):
                return platform_results
    return platform_results


def run_batch_monitor(params: dict) -> dict:
    brand_name = params["brand_name"]
    brand_id = params.get("brand_id")
    keywords = params.get("keywords", [])
    platforms = params.get("platforms", ["web"])
    task_id = params.get("task_id")
    run_id = params.get("run_id")
    timeout = int(params.get("timeout", 90))
    run_mode = str(params.get("run_mode") or "manual").strip().lower()

    if not keywords:
        return {"error": "No keywords", "results": []}

    run_id = _create_monitor_run(brand_id, platforms, len(keywords), task_id, run_id)
    results = []
    start_time = time.time()
    total_steps = max(1, len(platforms) * len(keywords))
    completed_steps = 0

    progress_lock = threading.Lock()
    progress = {
        "completed": completed_steps,
        "current": {platform: "等待" for platform in platforms},
    }
    _update_task_progress(task_id, 0, f"平台并行启动：{','.join(platforms)}；每个平台内关键词串行")
    if is_task_cancelled(task_id):
        _mark_monitor_run_cancelled(run_id)
        return {
            "cancelled": True,
            "run_id": run_id,
            "total": 0,
            "cited": 0,
            "verified_cited": 0,
            "unverified_cited": 0,
            "failed": 0,
            "results": [],
            "elapsed_seconds": 0,
        }

    max_workers = max(1, min(len(platforms), 4))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _run_platform_serial,
                run_id,
                brand_name,
                brand_id,
                platform,
                keywords,
                timeout,
                task_id,
                progress,
                progress_lock,
                total_steps,
                run_mode,
            ): platform
            for platform in platforms
        }
        for future in as_completed(future_map):
            platform = future_map[future]
            if is_task_cancelled(task_id):
                _mark_monitor_run_cancelled(run_id)
                return {
                    "cancelled": True,
                    "run_id": run_id,
                    "total": len(results),
                    "cited": sum(1 for r in results if r.get("is_cited")),
                    "verified_cited": sum(1 for r in results if r.get("verification_status") == "verified_citation"),
                    "unverified_cited": sum(1 for r in results if r.get("verification_status") == "unverified_citation"),
                    "failed": sum(1 for r in results if r.get("failure_reason")),
                    "results": results,
                    "elapsed_seconds": round(time.time() - start_time, 1),
                }
            try:
                results.extend(future.result())
            except Exception as exc:
                if is_task_cancelled(task_id):
                    _mark_monitor_run_cancelled(run_id)
                    return {"cancelled": True, "run_id": run_id, "total": len(results), "cited": 0, "verified_cited": 0, "unverified_cited": 0, "failed": len(results), "results": results, "elapsed_seconds": round(time.time() - start_time, 1)}
                for keyword in keywords:
                    failed = {
                        "platform": platform,
                        "brand_name": brand_name,
                        "brand_id": brand_id,
                        "keyword_word": keyword.get("word"),
                        "keyword_id": keyword.get("id"),
                        "run_id": run_id,
                        "is_cited": False,
                        "verification_status": "failed",
                        "failure_reason": f"平台并行任务异常: {str(exc)[:200]}",
                        "notes": f"平台并行任务异常: {str(exc)[:200]}",
                    }
                    if brand_id and keyword.get("id"):
                        try:
                            failed["monitor_result_id"] = _save_monitor_evidence(
                                run_id, brand_id, keyword["id"], platform, keyword["word"], failed
                            )
                        except Exception:
                            pass
                    results.append(failed)

    if is_task_cancelled(task_id):
        _mark_monitor_run_cancelled(run_id)
        return {
            "cancelled": True,
            "run_id": run_id,
            "total": len(results),
            "cited": sum(1 for r in results if r.get("is_cited")),
            "verified_cited": sum(1 for r in results if r.get("verification_status") == "verified_citation"),
            "unverified_cited": sum(1 for r in results if r.get("verification_status") == "unverified_citation"),
            "failed": sum(1 for r in results if r.get("failure_reason")),
            "results": results,
            "elapsed_seconds": round(time.time() - start_time, 1),
        }
    _update_monitor_run(run_id, results)
    elapsed = round(time.time() - start_time, 1)
    final_quality = summarize_monitor_quality(results)
    _status = final_quality["business_status"]
    if _status == "passed":
        task_msg = "完成"
    elif _status == "brand_mentioned_unverified":
        task_msg = f"完成(品牌已露出,来源未验证): {final_quality['business_failure_reason'] or ''}"
    else:
        task_msg = f"业务失败: {final_quality['business_failure_reason'] or '质量门禁未通过'}"
    # 监测缺口自动补内容（开发记录 #10）：未引用关键词 → 自动生成品牌×关键词文章草稿
    auto_content_info = None
    try:
        from app.services.auto_content import maybe_queue_auto_content
        auto_content_info = maybe_queue_auto_content(run_id, results, brand_id, brand_name, task_id)
    except Exception as exc:
        print(f"[MONITOR] 自动补内容入队失败（不影响监测结果）: {str(exc)[:160]}")
    if auto_content_info:
        task_msg += f"；{auto_content_info['message']}"
    _update_task_progress(task_id, 100, task_msg)
    return {
        "run_id": run_id,
        "total": len(results),
        "cited": sum(1 for r in results if r.get("is_cited")),
        "verified_cited": sum(1 for r in results if r.get("verification_status") == "verified_citation"),
        "unverified_cited": sum(1 for r in results if r.get("verification_status") == "unverified_citation"),
        "failed": sum(1 for r in results if r.get("failure_reason")),
        "business_status": final_quality["business_status"],
        "business_failure_reason": final_quality["business_failure_reason"],
        "effective_source_count": final_quality["effective_source_count"],
        "brand_mention_count": final_quality["brand_mention_count"],
        "brand_mention_rate": final_quality["brand_mention_rate"],
        "results": results,
        "auto_content": auto_content_info,
        "elapsed_seconds": elapsed,
    }


from app.services.task_queue import register_handler
register_handler(TASK_TYPE_MONITOR, run_batch_monitor)
register_handler(TASK_TYPE_AI_DETECT, run_batch_monitor)
