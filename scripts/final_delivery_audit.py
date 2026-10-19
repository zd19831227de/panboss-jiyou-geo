#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Final delivery audit for GEO Ops.

This gate maps the user-facing delivery requirements to concrete runtime,
database, and source-code evidence. It does not create fake data or mark
external publishing as complete.
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "geo_data.db"
FRONTEND = ROOT / "frontend" / "src"
BACKEND = ROOT / "backend" / "app"
from _geo_auth import auth_headers
from _geo_ports import AI_PORTS, BASE_URL



class GateFailure(Exception):
    pass


def check(condition, message: str):
    if not condition:
        raise GateFailure(message)


def request(path: str):
    with urllib.request.urlopen(urllib.request.Request(BASE_URL + path, headers=auth_headers()), timeout=25) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


def db_rows(sql: str, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def assert_pages_and_buttons():
    routes = read(FRONTEND / "router" / "index.js")
    app = read(FRONTEND / "App.vue")
    required_routes = ["/brands", "/keywords", "/monitor", "/dashboard", "/command-center", "/operations", "/settings"]
    for route in required_routes:
        check(route in routes, f"route missing: {route}")
        check(route in app or route == "/command-center", f"sidebar route missing: {route}")
    views = {
        "brands": "BrandList.vue",
        "keywords": "KeywordList.vue",
        "monitor": "MonitorCenter.vue",
        "dashboard": "Dashboard.vue",
        "command": "CommandCenter.vue",
        "operations": "Operations.vue",
        "settings": "Settings.vue",
    }
    for name, filename in views.items():
        text = read(FRONTEND / "views" / filename)
        check("@click" in text or "onMounted" in text, f"{name} view has no interactive/data behavior")
    combined = "\n".join(read(path) for path in (FRONTEND / "views").glob("*.vue"))
    check("catch { /*" not in combined and "catch {}" not in combined, "frontend has silent catch blocks")
    check("value=\"wechat\"" not in combined and "公众号草稿" not in combined, "wechat exposed as active frontend option")
    check("activeWorkflows" in read(FRONTEND / "views" / "Operations.vue"), "operations platform choices are not workflow-driven")


def assert_runtime_and_sessions():
    health = request("/health")
    check(health.get("status") == "healthy", "backend health is not healthy")
    status = request("/api/system/status")
    check(status["backend"]["status"] == "running", "backend offline")
    check(status["worker"]["status"] == "running", "worker offline")
    check(status["database_path"].replace("\\", "/").endswith("backend/geo_data.db"), "runtime DB path mismatch")
    sessions = request("/api/platform-sessions")
    for platform, port in AI_PORTS.items():
        item = sessions.get(platform) or {}
        check(item.get("port") == port, f"{platform} fixed port missing")
        check(item.get("profile_dir"), f"{platform} persistent profile missing")
        check(item.get("status") in {"logged_in", "browser_open", "captcha_required", "expired", "not_connected"}, f"{platform} unusable status: {item}")
    captcha = request("/api/captcha/status")
    check(captcha.get("fallback_mode") in {"auto+manual", "manual"}, "captcha fallback missing")


def assert_evidence_truth():
    stats = request("/api/dashboard/all-stats")
    for key in ["totalMonitorCount", "verifiedCitationCount", "withSourceUrl", "withScreenshot", "withAnswerText", "evidenceCompleteRate"]:
        check(key in stats, f"dashboard evidence stat missing: {key}")
    invalid_verified = db_rows(
        """SELECT COUNT(*) AS count FROM monitor_results
           WHERE verification_status = 'verified_citation'
             AND (source_verified != 1 OR source_url IS NULL OR source_url = '')"""
    )[0]["count"]
    check(invalid_verified == 0, "verified citations without verified source exist")
    invalid_publish = db_rows(
        """SELECT COUNT(*) AS count FROM publishing_tasks
           WHERE status = 'published'
             AND (published_url_verified != 1 OR published_url IS NULL OR published_url = '')"""
    )[0]["count"]
    check(invalid_publish == 0, "published tasks without verified URL exist")
    fake_sources = db_rows(
        """SELECT COUNT(*) AS count FROM monitor_results
           WHERE source_url LIKE '%example.com%' OR source_url LIKE '%localhost%'"""
    )[0]["count"]
    check(fake_sources == 0, "fake/local monitor source URLs exist")


def assert_business_gates_in_source():
    operations = read(BACKEND / "routers" / "operations.py")
    monitor = read(BACKEND / "routers" / "monitor.py")
    evidence = read(BACKEND / "services" / "evidence.py")
    frontend_ops = read(FRONTEND / "views" / "Operations.vue")
    checks = [
        ("资料不全禁生成", "品牌资料不完整" in operations),
        ("未审核禁发布", "Content must be approved before creating a publish task" in operations),
        ("无真实链接禁复测", "真实发布链接" in operations or "published_url_verified" in operations),
        ("无来源不算验证引用", "verified_citation" in evidence and "source_ok" in evidence),
        ("失败原因可追踪", "failure_reason" in operations and "failure_reason" in monitor),
        ("人工发布确认", "requires_manual_publish" in operations and "manual_publish" in operations),
        ("未测试工作流禁发布", "Publish workflow must be tested or active" in operations),
        ("未通过测试状态可见", "未通过测试" in frontend_ops),
        ("证据校验不吃系统代理", "trust_env = False" in evidence),
    ]
    for label, ok in checks:
        check(ok, f"business gate missing: {label}")


def assert_operations_loop():
    workflows = request("/api/operations/publish/workflows")
    platforms = {item["platform"]: item for item in workflows}
    for platform in ["website", "ctrip_travel", "douyin", "toutiao", "baijiahao", "sohu", "zhihu", "xiaohongshu"]:
        check(platform in platforms, f"publish workflow missing: {platform}")
    check("wechat" not in platforms, "wechat returned as active workflow")
    check("xigua" not in platforms and "penguin" not in platforms, "xigua/penguin returned as active workflow")
    check(platforms["ctrip_travel"].get("login_url") == "https://we.ctrip.com/account/login", "ctrip must be content center/travel note, not EBK/C-end")
    for platform, item in platforms.items():
        workflow = item.get("workflow") or {}
        check(workflow.get("requires_manual_publish") is True, f"{platform} missing manual publish requirement")
        if platform != "website":
            check(item.get("publish_url", "").startswith(("http://", "https://")), f"{platform} fixed publish URL invalid")
    active_untested = db_rows(
        """SELECT COUNT(*) AS count FROM publishing_tasks
           WHERE platform IN ('douyin','toutiao','ctrip_travel')
             AND status IN ('pending','prepared','running','manual_required','login_required')"""
    )[0]["count"]
    check(active_untested == 0, "untested ecosystem platform has active publish tasks")
    tested_without_evidence = db_rows(
        """SELECT COUNT(*) AS count
           FROM publishing_workflows w
           WHERE w.status IN ('tested','active')
             AND NOT EXISTS (
               SELECT 1
               FROM publishing_workflow_tests t
               WHERE t.platform = w.platform
                 AND t.status = 'prepared'
                 AND COALESCE(t.completed_at, '') != ''
                 AND (COALESCE(t.screenshot_verified,0) = 1 OR COALESCE(t.raw_artifact_verified,0) = 1)
             )"""
    )[0]["count"]
    check(tested_without_evidence == 0, "tested/active workflow without real test evidence")
    ecosystems = request("/api/operations/publish/ecosystems")
    ids = {item.get("ecosystem") for item in ecosystems}
    for ecosystem in ["byte", "alibaba", "tencent", "travel_local", "general_web"]:
        check(ecosystem in ids, f"source ecosystem missing: {ecosystem}")


def assert_data_and_cleanup():
    orphan_checks = {
        "monitor_sources": "SELECT COUNT(*) AS count FROM monitor_sources s LEFT JOIN monitor_answers a ON a.id = s.answer_id WHERE a.id IS NULL",
        "monitor_answers": "SELECT COUNT(*) AS count FROM monitor_answers a LEFT JOIN brands b ON b.id = a.brand_id WHERE b.id IS NULL",
        "keyword_ideas": "SELECT COUNT(*) AS count FROM keyword_ideas ki LEFT JOIN brands b ON b.id = ki.brand_id WHERE b.id IS NULL",
        "content_assets": "SELECT COUNT(*) AS count FROM content_assets ca LEFT JOIN brands b ON b.id = ca.brand_id WHERE b.id IS NULL",
        "publishing_tasks": "SELECT COUNT(*) AS count FROM publishing_tasks pt LEFT JOIN brands b ON b.id = pt.brand_id WHERE b.id IS NULL",
    }
    for name, sql in orphan_checks.items():
        count = db_rows(sql)[0]["count"]
        check(count == 0, f"orphan rows in {name}: {count}")
    check(DB_PATH.exists() and DB_PATH.stat().st_size > 0, "active DB missing")


def main() -> int:
    checks = [
        ("pages and buttons", assert_pages_and_buttons),
        ("runtime and sessions", assert_runtime_and_sessions),
        ("evidence truth", assert_evidence_truth),
        ("business gates", assert_business_gates_in_source),
        ("operations loop", assert_operations_loop),
        ("data integrity", assert_data_and_cleanup),
    ]
    passed = []
    try:
        for name, fn in checks:
            fn()
            passed.append(name)
    except Exception as exc:
        print(json.dumps({"status": "failed", "passed": passed, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"status": "passed", "checks": passed}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
