#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Operations acceptance gate.

This gate proves the operations loop is functional without fabricating publish
success: keyword ideas, content quality, publishing preparation, URL rejection,
scorecard creation, and cleanup consistency must all pass.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "geo_data.db"
from _geo_ports import BASE_URL
from _geo_auth import auth_headers

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


def db_rows(sql: str, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def artifact_ok(path: str | None) -> bool:
    if not path:
        return False
    value = str(path)
    if value.startswith("/artifacts/"):
        p = artifacts_dir() / value.replace("/artifacts/", "", 1)
    elif value.startswith("/screenshot/"):
        p = screenshots_dir() / value.replace("/screenshot/", "", 1)
    else:
        p = Path(value)
    return p.exists() and p.is_file() and p.stat().st_size > 128


def valid_url(url: str | None) -> bool:
    parsed = urlparse(url or "")
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def unique_name(prefix: str) -> str:
    return f"{prefix}-{int(time.time())}"


def generate_keyword_ideas_with_retry(brand_id: int, *, seed: str, count: int, attempts: int = 3) -> dict:
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return request("POST", "/api/operations/keywords/generate", {
                "brand_id": brand_id,
                "seed": seed,
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
    if brand_id:
        try:
            request("DELETE", f"/api/brands/{brand_id}")
        except Exception:
            pass


def assert_no_brand_orphans(brand_id: int):
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
    for name, sql in checks.items():
        count = db_rows(sql, (brand_id,))[0]["count"]
        check(count == 0, f"cleanup left {count} orphan rows in {name}")


def main() -> int:
    health = request("GET", "/health")
    check(health.get("status") == "healthy", "backend is not healthy")

    brand_id = None
    incomplete_brand_id = None
    original_workflows = {}
    account_ids = []
    passed = []
    try:
        incomplete = request("POST", "/api/brands", {
            "name": unique_name("ops-incomplete"),
            "industry": "酒店",
            "target_platforms": "doubao,qianwen,yuanbao",
            "status": "active",
        })
        incomplete_brand_id = incomplete["id"]
        expect_http(400, "POST", "/api/operations/keywords/generate", {
            "brand_id": incomplete_brand_id,
            "count": 5,
        })
        passed.append("incomplete brand blocked")

        brand = request("POST", "/api/brands", {
            "name": unique_name("ops-brand"),
            "industry": "酒店",
            "products": "高层景观房, 早餐, 会议服务",
            "target_audience": "商务差旅, 情侣出行, 亲子家庭",
            "selling_points": "江景, 近地铁, 服务稳定, 交通方便",
            "competitors": "附近连锁酒店, 同档商圈酒店",
            "owned_sites": "https://www.example-brand.com/,https://hotels.ctrip.com/",
            "target_platforms": "doubao,qianwen,yuanbao,web",
            "status": "active",
        })
        brand_id = brand["id"]
        keyword = request("POST", "/api/keywords", {
            "brand_id": brand_id,
            "word": unique_name("市中心酒店推荐"),
            "category": "longtail",
            "search_volume": 0,
        })
        passed.append("brand and keyword write")

        ideas = generate_keyword_ideas_with_retry(brand_id, seed="商圈酒店", count=8)
        check(ideas.get("count", 0) >= 8, "keyword idea generation failed")
        idea_ids = [item["id"] for item in ideas.get("items", []) if item.get("status") == "pending"][:3]
        adopted = request("POST", "/api/operations/keywords/adopt", {"idea_ids": idea_ids})
        check(adopted.get("count", 0) >= 1, "adopting keyword ideas failed")
        passed.append("keyword generation and adoption")

        workflows = request("GET", "/api/operations/publish/workflows")
        original_workflows = {item["platform"]: item for item in workflows if item.get("platform") in {"baijiahao", "xiaohongshu"}}
        check(not any(item.get("platform") == "wechat" for item in workflows), "wechat must not be an active publishing platform")
        workflow_platforms = {item.get("platform") for item in workflows}
        for platform in ["ctrip_travel", "douyin", "toutiao"]:
            check(platform in workflow_platforms, f"{platform} ecosystem workflow missing")
        check("xigua" not in workflow_platforms and "penguin" not in workflow_platforms, "xigua/penguin must not be active workflows")
        ecosystems = request("GET", f"/api/operations/publish/ecosystems?brand_id={brand_id}")
        ecosystem_ids = {item.get("ecosystem") for item in ecosystems}
        for ecosystem in ["byte", "alibaba", "tencent", "travel_local"]:
            check(ecosystem in ecosystem_ids, f"{ecosystem} source ecosystem missing")
        for item in ecosystems:
            check("evidence_count" in item and isinstance(item.get("recommendations"), list), "ecosystem analysis missing evidence or recommendations")
        publish_workflow = next((item for item in workflows if item.get("platform") == "baijiahao"), None)
        check(publish_workflow and publish_workflow.get("publish_url"), "baijiahao fixed publish URL missing")
        expect_http(400, "PUT", "/api/operations/publish/workflows/baijiahao", {"status": "tested"})
        account = request("POST", "/api/operations/accounts", {
            "platform": "baijiahao",
            "account_name": unique_name("gate-baijiahao"),
        })
        account_ids.append(account["id"])
        check(account.get("publish_url") == publish_workflow["publish_url"], "account did not inherit fixed publish URL")
        passed.append("publish workflow planning")

        content = request("POST", "/api/operations/content/generate", {
            "brand_id": brand_id,
            "keyword_id": keyword["id"],
            "content_type": "guide",
            "target_platform": "website",
        })
        content_id = content["id"]
        original_body = content.get("body") or ""
        original_title = content.get("title") or ""
        quality = request("GET", f"/api/operations/content/{content_id}/quality-check?ai=false")
        check(quality.get("passed") is True, "generated content failed quality gate")
        passed.append("content generation quality")

        request("PUT", f"/api/operations/content/{content_id}", {"body": "短内容"})
        expect_http(400, "POST", f"/api/operations/content/{content_id}/approve")
        request("PUT", f"/api/operations/content/{content_id}", {
            "title": original_title,
            "body": original_body,
        })
        approved = request("POST", f"/api/operations/content/{content_id}/approve")
        check(approved.get("status") == "approved", "content approval did not persist")
        passed.append("quality gate blocks weak content")

        expect_http(400, "POST", "/api/operations/publish/tasks", {
            "content_id": content_id,
            "platform": "baijiahao",
        })
        passed.append("external account gate blocked")

        task = request("POST", "/api/operations/publish/tasks", {
            "content_id": content_id,
            "platform": "website",
            "publish_mode": "manual_confirm",
        })
        task_id = task["id"]
        prepared = request("POST", f"/api/operations/publish/tasks/{task_id}/prepare")
        check(prepared.get("status") == "prepared", "publish prepare did not persist prepared status")
        check(artifact_ok(prepared.get("raw_artifact_path")), "publish prepare raw artifact is invalid")
        task_detail = request("GET", f"/api/operations/publish/tasks/{task_id}")
        check(task_detail.get("raw_artifact_verified") == 1, "publish raw artifact not verified")
        check(task_detail.get("evidence_status") in ("verified", "material_verified", "unverified"), "publish evidence status missing")
        passed.append("publish preparation evidence")

        expect_http(400, "POST", f"/api/operations/publish/tasks/{task_id}/confirm", {
            "published_url": "https://example.com/not-real",
            "trigger_monitor": True,
        })
        expect_http(400, "POST", f"/api/operations/publish/tasks/{task_id}/retest")
        passed.append("fake publish URL blocked")

        report = request("POST", f"/api/operations/reports/scorecard?brand_id={brand_id}")
        check(artifact_ok(report.get("artifact_path")), "scorecard artifact missing")
        stats = report.get("stats") or {}
        check("raw_artifacts" in stats, "scorecard missing raw artifact evidence stats")
        check((stats.get("sources") or 0) >= 0 and (stats.get("screenshots") or 0) >= 0, "scorecard stats invalid")
        passed.append("scorecard generated")

    finally:
        cleanup_brand(brand_id)
        cleanup_brand(incomplete_brand_id)
        for account_id in account_ids:
            try:
                request("DELETE", f"/api/operations/accounts/{account_id}")
            except Exception:
                pass
        for platform, workflow in original_workflows.items():
            try:
                request("PUT", f"/api/operations/publish/workflows/{platform}", {
                    "platform_name": workflow.get("platform_name"),
                    "login_url": workflow.get("login_url"),
                    "publish_url": workflow.get("publish_url"),
                    "workflow_json": workflow.get("workflow") or {},
                    "status": workflow.get("status"),
                    "notes": workflow.get("notes"),
                })
            except Exception:
                pass

    if brand_id:
        assert_no_brand_orphans(brand_id)
        passed.append("cleanup cascade")
    if incomplete_brand_id:
        assert_no_brand_orphans(incomplete_brand_id)

    print(json.dumps({"status": "passed", "checks": passed}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
