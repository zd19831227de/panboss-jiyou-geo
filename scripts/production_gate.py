# -*- coding: utf-8 -*-
"""Production acceptance gate for GEO Ops.

This script checks runtime APIs, database evidence, business gates, and the
operations loop. It creates temporary records and deletes the temporary brand
at the end; it does not fabricate monitor results or publishing success.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "geo_data.db"
from _geo_auth import auth_headers
from _geo_ports import AI_PORTS, BASE_URL

sys.path.insert(0, str(ROOT / "backend"))
from app.core.runtime_paths import artifacts_dir, screenshots_dir  # noqa: E402


class GateFailure(Exception):
    pass


class HttpFailure(Exception):
    def __init__(self, status: int, data):
        super().__init__(f"HTTP {status}: {data}")
        self.status = status
        self.data = data


def request(method: str, path: str, body=None):
    data = None
    headers = {"Content-Type": "application/json", **auth_headers()}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = raw
        raise HttpFailure(exc.code, payload)


def multipart_request(path: str, fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]):
    boundary = "----GeoGate" + uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    for name, (filename, content, content_type) in files.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n".encode()
        )
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    data = b"".join(chunks)
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = raw
        raise HttpFailure(exc.code, payload)


def expect_http(status: int, method: str, path: str, body=None):
    try:
        data = request(method, path, body)
    except HttpFailure as exc:
        if exc.status == status:
            return exc.data
        raise
    raise GateFailure(f"{method} {path} expected HTTP {status}, got success: {data}")


def check(condition, message: str):
    if not condition:
        raise GateFailure(message)


def local_artifact_ok(path: str | None) -> bool:
    if not path:
        return False
    if str(path).startswith("/artifacts/"):
        p = artifacts_dir() / str(path).replace("/artifacts/", "", 1)
    elif str(path).startswith("/screenshot/"):
        p = screenshots_dir() / str(path).replace("/screenshot/", "", 1)
    else:
        p = Path(path)
    return p.exists() and p.is_file() and p.stat().st_size > 128


def valid_url(url: str | None) -> bool:
    parsed = urlparse(url or "")
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def effective_source(source: dict) -> bool:
    parsed = urlparse(source.get("url") or "")
    domain = (source.get("domain") or parsed.netloc or "").lower()
    if parsed.scheme not in ("http", "https") or not domain:
        return False
    if domain in {
        "www.qianwen.com",
        "qianwen.com",
        "chat-side.qianwen.com",
        "chat2-api.qianwen.com",
        "zd.qianwen.com",
        "www.doubao.com",
        "doubao.com",
        "yuanbao.tencent.com",
        "lf3-short.ibytedapm.com",
        "lf6-short.ibytedapm.com",
        "rumt-zh.com",
    }:
        return False
    if domain in {"www.baidu.com", "baidu.com", "m.baidu.com"} and parsed.path.startswith(("/link", "/s")):
        return False
    status = str(source.get("check_status") or "").lower()
    return not any(marker in status for marker in ["http 401", "http 403", "http 429", "需要权限", "限制访问", "不可访问", "访问失败"])


def db_rows(sql: str, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def unique_name(prefix: str) -> str:
    return f"{prefix}-{int(time.time())}"


def generate_keyword_ideas_with_retry(brand_id: int, *, count: int, attempts: int = 3) -> dict:
    last_error = None
    for _ in range(attempts):
        try:
            return request("POST", "/api/operations/keywords/generate", {
                "brand_id": brand_id,
                "seed": "步行街商圈酒店推荐",
                "count": count,
            })
        except HttpFailure as exc:
            last_error = exc
            detail = exc.data.get("detail") if isinstance(exc.data, dict) else str(exc.data)
            if exc.status != 502 or "关键词批次未通过 GEO 质量门禁" not in str(detail):
                raise
            time.sleep(1)
    raise GateFailure(f"keyword idea generation failed after {attempts} attempts: {last_error.data if last_error else ''}")


def cleanup_brand(brand_id: int | None):
    if not brand_id:
        return
    last_error = None
    for _ in range(6):
        try:
            request("DELETE", f"/api/brands/{brand_id}")
            return
        except HttpFailure as exc:
            if exc.status == 404:
                return
            last_error = exc
        except Exception as exc:
            last_error = exc
        time.sleep(1)
    if last_error:
        raise last_error


def assert_no_brand_orphans(brand_id: int | None):
    if not brand_id:
        return
    checks = {
        "brands": "SELECT COUNT(*) AS count FROM brands WHERE id = ?",
        "keywords": "SELECT COUNT(*) AS count FROM keywords WHERE brand_id = ?",
        "keyword_ideas": "SELECT COUNT(*) AS count FROM keyword_ideas WHERE brand_id = ?",
        "content_assets": "SELECT COUNT(*) AS count FROM content_assets WHERE brand_id = ?",
        "publishing_tasks": "SELECT COUNT(*) AS count FROM publishing_tasks WHERE brand_id = ?",
        "monitor_results": "SELECT COUNT(*) AS count FROM monitor_results WHERE brand_id = ?",
        "monitor_answers": "SELECT COUNT(*) AS count FROM monitor_answers WHERE brand_id = ?",
        "monitor_runs": "SELECT COUNT(*) AS count FROM monitor_runs WHERE brand_id = ?",
        "geo_reports": "SELECT COUNT(*) AS count FROM geo_reports WHERE brand_id = ?",
    }
    last_counts = {}
    for _ in range(6):
        last_counts = {}
        for table, sql in checks.items():
            rows = db_rows(sql, (brand_id,))
            last_counts[table] = rows[0]["count"]
        if all(count == 0 for count in last_counts.values()):
            return
        cleanup_brand(brand_id)
        time.sleep(1)
    for table, count in last_counts.items():
        check(count == 0, f"cleanup left orphan rows in {table} for brand {brand_id}")


def create_brand(payload: dict) -> dict:
    return request("POST", "/api/brands", payload)


def create_keyword(brand_id: int, word: str) -> dict:
    return request(
        "POST",
        "/api/keywords",
        {"brand_id": brand_id, "word": word, "category": "longtail", "search_volume": 0},
    )


def assert_core_runtime():
    health = request("GET", "/health")
    check(health.get("status") == "healthy", "backend health is not healthy")
    check("GEO" in health.get("service", ""), "service name is not GEO")

    status = request("GET", "/api/system/status")
    check(status["backend"]["status"] == "running", "backend status is not running")
    check(status["worker"]["status"] == "running", "worker status is not running")
    check(status["database_path"].replace("\\", "/").endswith("backend/geo_data.db"), "runtime DB is not backend/geo_data.db")
    check(DB_PATH.exists() and DB_PATH.stat().st_size > 0, "active DB is missing or empty")

    captcha = request("GET", "/api/captcha/status")
    check(captcha.get("fallback_mode") in ("auto+manual", "manual"), "captcha fallback mode missing")

    sessions = request("GET", "/api/platform-sessions")
    for platform, port in AI_PORTS.items():
        item = sessions.get(platform) or {}
        check(item.get("port") == port, f"{platform} port is not {port}")
        profile_dir = item.get("profile_dir")
        check(profile_dir, f"{platform} profile_dir missing")
        check(Path(profile_dir).exists(), f"{platform} profile_dir not recoverable: {profile_dir}")
        check(
            item.get("status") in {"logged_in", "browser_open", "captcha_required", "expired", "not_connected"},
            f"{platform} not recoverable: {item}",
        )
        if platform == "qianwen":
            check(item.get("login_url") == "https://www.qianwen.com/", f"qianwen login_url is wrong: {item.get('login_url')}")


def assert_dashboard_data():
    stats = request("GET", "/api/dashboard/all-stats")
    for key in [
        "totalMonitorCount", "withSourceUrl", "withScreenshot", "withAnswerText",
        "evidenceCompleteRate", "contentAssetCount", "publishingTaskCount",
        "verifiedPublishedCount",
    ]:
        check(key in stats, f"dashboard stat missing: {key}")
    check(stats["verifiedPublishedCount"] <= stats["publishingTaskCount"], "verified publish count exceeds publish tasks")

    command = request("GET", "/api/dashboard/command-center")
    overview = command.get("overview") or {}
    for key in ["total_monitor", "verified", "with_source", "with_screenshot", "with_answer", "failed"]:
        check(key in overview, f"command center overview missing: {key}")
    check(overview.get("content_assets") == stats["contentAssetCount"], "command center content assets mismatch dashboard")
    check(overview.get("publishing_tasks") == stats["publishingTaskCount"], "command center publishing tasks mismatch dashboard")
    allowed_platforms = {"web", "doubao", "qianwen", "yuanbao"}
    platform_stats = command.get("platform_stats") or []
    check(
        all((item.get("platform") in allowed_platforms) for item in platform_stats),
        f"command center contains non-P1 monitor platform: {platform_stats}",
    )
    recent = request("GET", "/api/dashboard/all-recent-results?limit=20")
    check(
        all((item.get("platform") in allowed_platforms) for item in recent),
        f"recent monitor results contain non-P1 platform: {recent}",
    )


def assert_latest_monitor_evidence():
    terminal_statuses = {"completed", "completed_with_errors"}
    tracked_failure_markers = [
        "风控",
        "封禁",
        "验证码",
        "未登录",
        "安全间隔",
        "超时",
        "页面结构",
        "登录",
        "captcha",
        "restricted",
        "blocked",
        "rate",
        "timeout",
    ]
    runs = db_rows(
        """SELECT r.*
           FROM monitor_runs r
           JOIN brands b ON b.id = r.brand_id
           WHERE r.status IN ('completed', 'completed_with_errors') AND r.total_operations >= 3
           ORDER BY r.id DESC LIMIT 10"""
    )
    check(runs, "no completed 3-platform monitor run")
    run = None
    answers = []
    for candidate in runs:
        candidate_answers = db_rows("SELECT * FROM monitor_answers WHERE run_id = ? ORDER BY platform", (candidate["id"],))
        platforms = {item.get("platform") for item in candidate_answers}
        successful_ai = [
            item for item in candidate_answers
            if item.get("platform") in {"doubao", "qianwen", "yuanbao"}
            and item.get("verification_status") != "failed"
            and not item.get("failure_reason")
        ]
        if {"doubao", "qianwen", "yuanbao"}.issubset(platforms) and successful_ai:
            run = candidate
            answers = candidate_answers
            break
    check(run is not None, "no terminal monitor run covers doubao/qianwen/yuanbao with successful evidence")
    check(run.get("status") in terminal_statuses, f"run {run['id']} is not terminal")
    check(run.get("business_status") == "passed", f"run {run['id']} business quality failed: {run.get('business_failure_reason')}")
    ai_answers = [a for a in answers if a.get("platform") in {"doubao", "qianwen", "yuanbao"}]
    check(len(ai_answers) >= 3, f"run {run['id']} does not have 3 AI platform answers")

    successful_answers = []
    for platform in {"doubao", "qianwen", "yuanbao"}:
        platform_answers = [a for a in ai_answers if a.get("platform") == platform]
        check(platform_answers, f"run {run['id']} missing {platform} answer")
        platform_successes = [
            a for a in platform_answers
            if a.get("verification_status") != "failed" and not a.get("failure_reason")
        ]
        platform_failures = [
            a for a in platform_answers
            if a.get("verification_status") == "failed" or a.get("failure_reason")
        ]
        for answer in platform_successes:
            check(answer.get("answer_text"), f"run {run['id']} {platform} has empty answer text")
            check(
                answer.get("screenshot_verified") == 1 and local_artifact_ok(answer.get("screenshot_path")),
                f"run {run['id']} {platform} has invalid screenshot evidence",
            )
            check(
                answer.get("raw_artifact_verified") == 1 and local_artifact_ok(answer.get("raw_artifact_path")),
                f"run {run['id']} {platform} has invalid raw artifact",
            )
            check(
                answer.get("verification_status") in {"not_cited", "unverified_citation", "verified_citation"},
                f"run {run['id']} {platform} has invalid verification status",
            )
        for answer in platform_failures:
            reason = answer.get("failure_reason") or answer.get("evidence_failure_reason") or ""
            check(reason, f"run {run['id']} {platform} failure has no reason")
            check(
                any(marker.lower() in reason.lower() for marker in tracked_failure_markers),
                f"run {run['id']} {platform} failure is not traceable: {reason}",
            )
        check(
            platform_successes or platform_failures,
            f"run {run['id']} {platform} has neither success evidence nor traceable failure",
        )
        successful_answers.extend(platform_successes)
    check(successful_answers, f"run {run['id']} has no successful AI answer evidence")

    answer_ids = [a["id"] for a in answers]
    placeholders = ",".join("?" for _ in answer_ids)
    sources = db_rows(f"SELECT * FROM monitor_sources WHERE answer_id IN ({placeholders})", answer_ids)
    for item in sources:
        check(valid_url(item.get("url")), "source URL is not http(s)")
        check(item.get("source_verified") == 1, "source URL is not verified")
    check(any(effective_source(item) for item in sources), f"run {run['id']} has no effective external source")
    cited_answers = [a for a in ai_answers if a.get("verification_status") in {"unverified_citation", "verified_citation"}]
    verified_answers = [a for a in cited_answers if a.get("verification_status") == "verified_citation"]
    check(verified_answers, f"run {run['id']} has no verified citation")
    for answer in verified_answers:
        check(answer.get("source_verified") == 1, f"verified citation answer {answer['id']} has no verified source")


def assert_monitor_progress_contract():
    monitor_code = (ROOT / "backend" / "app" / "services" / "monitor" / "ai_monitor.py").read_text(encoding="utf-8")
    check("def _update_task_progress" in monitor_code, "monitor task progress helper missing")
    check("执行中 {step_label}" in monitor_code, "monitor task does not write in-progress message")
    check("已完成 {completed_steps}/{total_steps}" in monitor_code, "monitor task does not write per-step completion message")
    check("task_queue" in monitor_code and "progress_pct" in monitor_code, "monitor progress is not persisted to task_queue")


def assert_database_integrity():
    checks = {
        "keywords": "SELECT COUNT(*) AS count FROM keywords k LEFT JOIN brands b ON b.id = k.brand_id WHERE b.id IS NULL",
        "keyword_ideas": "SELECT COUNT(*) AS count FROM keyword_ideas t LEFT JOIN brands b ON b.id = t.brand_id WHERE b.id IS NULL",
        "content_assets": "SELECT COUNT(*) AS count FROM content_assets t LEFT JOIN brands b ON b.id = t.brand_id WHERE b.id IS NULL",
        "publishing_tasks": "SELECT COUNT(*) AS count FROM publishing_tasks t LEFT JOIN brands b ON b.id = t.brand_id WHERE b.id IS NULL",
        "monitor_results": "SELECT COUNT(*) AS count FROM monitor_results t LEFT JOIN brands b ON b.id = t.brand_id WHERE b.id IS NULL",
        "monitor_answers": "SELECT COUNT(*) AS count FROM monitor_answers t LEFT JOIN brands b ON b.id = t.brand_id WHERE b.id IS NULL",
        "monitor_runs": "SELECT COUNT(*) AS count FROM monitor_runs t LEFT JOIN brands b ON b.id = t.brand_id WHERE b.id IS NULL",
        "geo_reports": "SELECT COUNT(*) AS count FROM geo_reports t LEFT JOIN brands b ON b.id = t.brand_id WHERE b.id IS NULL",
        "monitor_sources": "SELECT COUNT(*) AS count FROM monitor_sources s LEFT JOIN monitor_answers a ON a.id = s.answer_id WHERE a.id IS NULL",
        "active_publish_without_artifact": "SELECT COUNT(*) AS count FROM publishing_tasks WHERE status IN ('prepared','published') AND COALESCE(raw_artifact_verified,0) != 1",
        "published_without_verified_url": "SELECT COUNT(*) AS count FROM publishing_tasks WHERE status = 'published' AND COALESCE(published_url_verified,0) != 1",
        "workflow_test_missing_evidence": "SELECT COUNT(*) AS count FROM publishing_workflow_tests WHERE status = 'prepared' AND COALESCE(screenshot_verified,0) != 1 AND COALESCE(raw_artifact_verified,0) != 1",
        "workflow_tested_without_evidence": """
            SELECT COUNT(*) AS count
            FROM publishing_workflows w
            WHERE w.status IN ('tested','active')
              AND NOT EXISTS (
                SELECT 1
                FROM publishing_workflow_tests t
                WHERE t.platform = w.platform
                  AND t.status = 'prepared'
                  AND COALESCE(t.completed_at, '') != ''
                  AND (COALESCE(t.screenshot_verified,0) = 1 OR COALESCE(t.raw_artifact_verified,0) = 1)
              )
        """,
        "active_content_has_template_terms": "SELECT COUNT(*) AS count FROM content_assets WHERE status != 'rejected' AND (title LIKE '%GEO内容稿%' OR body LIKE '%GEO内容稿%' OR body LIKE '%用于提升%' OR body LIKE '%生成式 AI 回答中的可见性%' OR body LIKE '%品牌归因%' OR body LIKE '%待补充%' OR body LIKE '%模板生成%')",
        "active_content_has_body_url": "SELECT COUNT(*) AS count FROM content_assets WHERE status != 'rejected' AND (body LIKE '%http://%' OR body LIKE '%https://%' OR body LIKE '%www.%')",
        "active_content_missing_source": "SELECT COUNT(*) AS count FROM content_assets WHERE status != 'rejected' AND generation_notes NOT LIKE '%evidence_sources=%' AND generation_notes NOT LIKE '%来源%' AND generation_notes NOT LIKE '%链接%' AND generation_notes NOT LIKE '%owned_sites%' AND generation_notes NOT LIKE '%可核验%' AND generation_notes NOT LIKE '%核实%'",
    }
    for name, sql in checks.items():
        count = db_rows(sql)[0]["count"]
        check(count == 0, f"database integrity failed: {name} has {count} orphan rows")


def assert_api_gates_and_operations():
    temp_brand_id = None
    incomplete_brand_id = None
    try:
        invalid_brand = {
            "name": unique_name("gate-invalid"),
            "industry": "hotel",
            "target_platforms": "doubao,bad",
            "status": "active",
        }
        expect_http(400, "POST", "/api/brands", invalid_brand)

        incomplete = create_brand({
            "name": unique_name("gate-incomplete"),
            "industry": "hotel",
            "target_platforms": "doubao,qianwen,yuanbao",
            "status": "active",
        })
        incomplete_brand_id = incomplete["id"]
        expect_http(400, "POST", "/api/operations/keywords/generate", {"brand_id": incomplete_brand_id, "count": 5})

        brand = create_brand({
            "name": unique_name("市中心步行街高层景观测试酒店"),
            "industry": "酒店",
            "products": "高层景观房, 亲子房, 商务会议服务, 早餐",
            "target_audience": "城市旅游用户, 商务差旅用户, 亲子家庭, 情侣出行",
            "selling_points": "市中心步行街, 近地铁, 靠近江边景观带, 高层景观, 早餐便利",
            "competitors": "同商圈连锁酒店, 江边附近酒店",
            "owned_sites": "https://www.example-brand.com/,https://www.ctrip.com/",
            "target_platforms": "doubao,qianwen,yuanbao,web",
            "status": "active",
        })
        temp_brand_id = brand["id"]
        keyword = create_keyword(temp_brand_id, unique_name("市中心步行街酒店推荐"))
        updated_brand = request("PUT", f"/api/brands/{temp_brand_id}", {
            "selling_points": "市中心步行街, 近地铁, 靠近江边景观带, 高层景观, 早餐便利, 延迟退房",
        })
        check("延迟退房" in (updated_brand.get("selling_points") or ""), "brand update did not persist")
        updated_keyword = request("PUT", f"/api/keywords/{keyword['id']}", {
            "word": keyword["word"] + " near subway",
            "category": "longtail",
        })
        check(updated_keyword["word"].endswith("near subway"), "keyword update did not persist")

        brands = request("GET", "/api/brands")
        check(any(item["id"] == temp_brand_id for item in brands), "created brand not visible in brand list")
        keywords = request("GET", f"/api/brands/{temp_brand_id}/keywords")
        check(any(item["id"] == keyword["id"] for item in keywords), "created keyword not visible in brand keywords")

        csv_text = "word,category,search_volume\n" + unique_name("import hotel question") + ",longtail,12\n"
        imported = multipart_request(
            "/api/keywords/import",
            {"brand_id": str(temp_brand_id)},
            {"file": ("keywords.csv", csv_text.encode("utf-8"), "text/csv")},
        )
        check(imported.get("total") == 1, "keyword import did not write one keyword")
        exported_req = urllib.request.Request(BASE_URL + f"/api/keywords/export_csv?brand_id={temp_brand_id}", method="GET")
        with urllib.request.urlopen(exported_req, timeout=120) as resp:
            exported = resp.read().decode("utf-8-sig")
        check("word,category" in exported and "longtail" in exported, "keyword export CSV missing imported data")

        task_created = request("POST", "/api/monitor/run", {
            "brand_id": temp_brand_id,
            "keyword_ids": [keyword["id"]],
            "platform": "web",
        })
        task_id = task_created.get("task_id")
        check(task_id, "monitor task was not created")
        task_detail = request("GET", f"/api/tasks/{task_id}")
        check(task_detail.get("task_id") == task_id, "task detail did not return created task")
        cancel_result = request("POST", f"/api/tasks/{task_id}/cancel")
        check("cancelled" in cancel_result and "status" in cancel_result, "task cancel did not return honest status")
        expect_http(404, "GET", "/api/tasks/999999999")
        expect_http(404, "POST", "/api/tasks/999999999/cancel")
        expect_http(400, "POST", "/api/scheduler/run?mode=bad")
        expect_http(400, "POST", "/api/system/run-scheduler?mode=bad")

        expect_http(400, "POST", "/api/monitor/run", {
            "brand_id": temp_brand_id,
            "keyword_ids": [keyword["id"]],
            "platform": "bad_platform",
        })

        ideas = generate_keyword_ideas_with_retry(temp_brand_id, count=8)
        check(ideas.get("count", 0) >= 8, "keyword idea generation did not write ideas")

        content = request("POST", "/api/operations/content/generate", {
            "brand_id": temp_brand_id,
            "keyword_id": keyword["id"],
            "content_type": "guide",
            "target_platform": "website",
        })
        content_id = content["id"]
        original_title = content.get("title") or ""
        original_body = content.get("body") or ""
        quality = request("GET", f"/api/operations/content/{content_id}/quality-check?ai=false")
        check(quality.get("passed") is True, "generated content did not pass quality check")

        request("PUT", f"/api/operations/content/{content_id}", {"body": "too short"})
        expect_http(400, "POST", f"/api/operations/content/{content_id}/approve")

        request("PUT", f"/api/operations/content/{content_id}", {"title": original_title, "body": original_body})
        approved = request("POST", f"/api/operations/content/{content_id}/approve")
        check(approved.get("status") == "approved", "content approval did not write approved status")

        task = request("POST", "/api/operations/publish/tasks", {
            "content_id": content_id,
            "platform": "website",
            "publish_mode": "manual_confirm",
        })
        task_id = task["id"]
        prepared = request("POST", f"/api/operations/publish/tasks/{task_id}/prepare")
        check(prepared.get("status") == "prepared", "prepare publish did not produce prepared status")
        check(prepared.get("raw_artifact_path"), "prepare publish did not produce raw artifact")
        prepared_detail = request("GET", f"/api/operations/publish/tasks/{task_id}")
        check(prepared_detail.get("raw_artifact_verified") == 1, "prepare publish raw artifact was not verified")
        check(prepared_detail.get("published_url_verified") in (0, None), "unpublished task should not have verified published URL")
        expect_http(400, "POST", f"/api/operations/publish/tasks/{task_id}/confirm", {
            "published_url": "https://example.com/fake",
            "trigger_monitor": True,
        })
        expect_http(400, "POST", f"/api/operations/publish/tasks/{task_id}/retest")

        report = request("POST", f"/api/operations/reports/scorecard?brand_id={temp_brand_id}")
        check(report.get("artifact_path"), "scorecard did not produce report artifact")
        check("raw_artifacts" in (report.get("stats") or {}), "scorecard does not expose verified raw artifact stats")
    finally:
        # finally 中清理失败不应掩盖 try 中原始失败/异常。
        # 此处单独捕获，仅打印告警，避免 raise 覆盖原始 stack。
        cleanup_errors = []
        for bid in (temp_brand_id, incomplete_brand_id):
            try:
                cleanup_brand(bid)
            except Exception as exc:  # noqa: BLE001 - finally cleanup best-effort
                cleanup_errors.append(f"cleanup_brand({bid}): {exc}")
        for bid in (temp_brand_id, incomplete_brand_id):
            try:
                assert_no_brand_orphans(bid)
            except Exception as exc:  # noqa: BLE001 - finally cleanup best-effort
                cleanup_errors.append(f"assert_no_brand_orphans({bid}): {exc}")
        if cleanup_errors:
            print(f"[production_gate] cleanup warnings: {cleanup_errors}", file=sys.stderr)


def maybe_touch_browser():
    for platform in ["doubao", "qianwen", "yuanbao"]:
        data = request("POST", f"/api/platform-sessions/{platform}/open-login")
        check(data.get("success") is True, f"{platform} open-login failed")
        check(data.get("port"), f"{platform} open-login did not return port")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--touch-browser", action="store_true", help="activate/open the 3 platform login tabs")
    args = parser.parse_args()

    checks = [
        ("core runtime", assert_core_runtime),
        ("dashboard data", assert_dashboard_data),
        ("latest monitor evidence", assert_latest_monitor_evidence),
        ("monitor progress contract", assert_monitor_progress_contract),
        ("database integrity", assert_database_integrity),
        ("api gates and operations", assert_api_gates_and_operations),
    ]
    if args.touch_browser:
        checks.append(("platform login buttons", maybe_touch_browser))

    passed = []
    try:
        for name, fn in checks:
            fn()
            passed.append(name)
            print(f"PASS {name}")
    except Exception as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        print(json.dumps({"passed": passed, "failed": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps({"status": "passed", "checks": passed}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
