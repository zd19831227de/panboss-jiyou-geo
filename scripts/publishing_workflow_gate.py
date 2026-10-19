#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Publishing workflow acceptance gate.

Proves the publishing center is not a shell: fixed platform URLs exist,
workflow edits persist, external publishing requires account binding, and
accounts inherit the platform publishing URL.
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
from _geo_auth import auth_headers
from _geo_ports import BASE_URL, PUBLISH_PORTS as CONFIGURED_PUBLISH_PORTS

sys.path.insert(0, str(ROOT / "backend"))
URL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

from app.services.publishing_rpa import test_publish_workflow
from app.services.publishing_rpa import PUBLISH_PORTS, RESERVED_PORTS, close_publish_browser


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
        with URL_OPENER.open(req, timeout=120) as resp:
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


def valid_url(url: str | None) -> bool:
    parsed = urlparse(url or "")
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def unique_name(prefix: str) -> str:
    return f"{prefix}-{int(time.time())}"


def db_rows(sql: str, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def cleanup_brand(brand_id: int | None):
    if brand_id:
        try:
            request("DELETE", f"/api/brands/{brand_id}")
        except Exception:
            pass


def assert_tested_workflows_have_evidence():
    rows = db_rows(
        """SELECT w.platform, w.status
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
    )
    check(not rows, f"tested/active workflow without real test evidence: {rows}")


def main() -> int:
    brand_id = None
    original_workflows: dict[str, dict] = {}
    account_ids: list[int] = []
    passed: list[str] = []
    try:
        workflows = request("GET", "/api/operations/publish/workflows")
        platforms = {item["platform"]: item for item in workflows}
        original_workflows = {
            platform: platforms[platform]
            for platform in ("baijiahao", "zhihu", "ctrip_travel", "website")
            if platform in platforms
        }
        expected = {"website", "ctrip_travel", "douyin", "toutiao", "baijiahao", "sohu", "zhihu", "xiaohongshu"}
        check(expected.issubset(platforms.keys()), "missing publishing workflow platforms")
        check("wechat" not in platforms, "wechat must not be an active publishing platform")
        check("xigua" not in platforms and "penguin" not in platforms, "xigua/penguin must not be active publishing platforms")
        check(platforms["ctrip_travel"]["login_url"] == "https://we.ctrip.com/account/login", "ctrip must use content center/travel note login URL")
        for platform, item in platforms.items():
            workflow = item.get("workflow") or {}
            check(workflow.get("requires_manual_publish") is True, f"{platform} must require manual final publish")
            check(workflow.get("steps"), f"{platform} workflow steps missing")
            if platform != "website":
                check(valid_url(item.get("login_url")), f"{platform} login URL invalid")
                check(valid_url(item.get("publish_url")), f"{platform} fixed publish URL invalid")
        passed.append("fixed platform workflows")

        expect_http(400, "PUT", "/api/operations/publish/workflows/wechat", {"publish_url": "https://mp.weixin.qq.com/"})
        expect_http(400, "PUT", "/api/operations/publish/workflows/baijiahao", {"publish_url": "not-a-url"})
        expect_http(400, "PUT", "/api/operations/publish/workflows/baijiahao", {
            "workflow_json": {
                "steps": ["open_publish_url", "fill_title", "publish"],
                "selectors": {},
                "requires_manual_publish": False,
            },
        })
        expect_http(400, "PUT", "/api/operations/publish/workflows/website", {
            "workflow_json": {
                "steps": ["generate_html", "auto_deploy", "verify_url"],
                "selectors": {},
                "requires_manual_publish": True,
            },
        })
        baijiahao_url = platforms["baijiahao"]["publish_url"]
        expect_http(400, "PUT", "/api/operations/publish/workflows/baijiahao", {"status": "tested"})
        expect_http(400, "PUT", "/api/operations/publish/workflows/baijiahao", {"status": "active"})
        updated = request("PUT", "/api/operations/publish/workflows/baijiahao", {
            "publish_url": baijiahao_url,
            "workflow_json": {
                "steps": ["open_publish_url", "fill_title", "fill_body", "manual_publish", "verify_url"],
                "selectors": {
                    "title": [".client_pages_edit_components_titleInput [contenteditable=\"true\"]"],
                    "body": ["frame:#ueditor_0 => body"],
                },
                "requires_manual_publish": True,
            },
        })
        check((updated.get("workflow") or {}).get("requires_manual_publish") is True, "workflow JSON did not persist")
        check((updated.get("workflow") or {}).get("selectors", {}).get("title"), "workflow selectors did not persist")
        passed.append("workflow update gate")

        website_updated = request("PUT", "/api/operations/publish/workflows/website", {
            "workflow_json": {
                "steps": ["generate_html", "manual_deploy", "verify_url", "post_publish_monitor"],
                "selectors": {},
                "requires_manual_publish": True,
            },
        })
        check((website_updated.get("workflow") or {}).get("requires_manual_publish") is True, "website workflow JSON did not persist")
        check("manual_deploy" in ((website_updated.get("workflow") or {}).get("steps") or []), "website workflow lost manual deploy gate")
        passed.append("website workflow manual deploy gate")

        account = request("POST", "/api/operations/accounts", {
            "platform": "baijiahao",
            "account_name": unique_name("workflow-account"),
        })
        account_ids.append(account["id"])
        check(account.get("publish_url") == baijiahao_url, "account did not inherit fixed publish URL")
        rows = db_rows("SELECT publish_url FROM publishing_accounts WHERE id = ?", (account["id"],))
        check(rows and rows[0]["publish_url"] == baijiahao_url, "account publish URL not persisted")
        passed.append("account inherits workflow")

        check(PUBLISH_PORTS == CONFIGURED_PUBLISH_PORTS, "publishing ports drift from config (.env GEO_PUBLISH_PORTS)")
        check(len(set(PUBLISH_PORTS.values())) == len(PUBLISH_PORTS), "publishing ports must be unique")
        check(not (set(PUBLISH_PORTS.values()) & RESERVED_PORTS), "publishing ports must not use reserved Hermes/AI/legacy ranges")
        check(all(1024 < p <= 65535 for p in PUBLISH_PORTS.values()), "publishing ports out of range")
        passed.append("workflow browser port isolation gate")
        close_publish_browser("baijiahao")

        fixture = ROOT / "tmp_publish_workflow_fixture.html"
        fixture.write_text(
            """<!doctype html><html><body>
            <input placeholder="标题" />
            <textarea placeholder="正文内容"></textarea>
            <button>保存草稿</button><button>发布</button>
            </body></html>""",
            encoding="utf-8",
        )
        try:
            missing_selector = test_publish_workflow("baijiahao", fixture.as_uri(), fixture.as_uri(), {
                "steps": ["open_publish_url", "fill_title", "fill_body", "manual_publish", "verify_url"],
                "selectors": {},
                "requires_manual_publish": True,
            }, account_name=account["account_name"])
            check(missing_selector.status != "prepared", "external workflow without selectors must not pass")
            selector_test = test_publish_workflow("baijiahao", fixture.as_uri(), fixture.as_uri(), {
                "steps": ["open_publish_url", "fill_title", "fill_body", "manual_publish", "verify_url"],
                "selectors": {"title": ["input[placeholder='标题']"], "body": ["textarea[placeholder='正文内容']"]},
                "requires_manual_publish": True,
            }, account_name=account["account_name"])
            check(selector_test.status == "prepared", f"external workflow selectors did not pass fixture test: {selector_test}")
            check(selector_test.screenshot_path or selector_test.raw_artifact_path, "selector workflow test did not save evidence")
        finally:
            try:
                fixture.unlink()
            except OSError:
                pass
        passed.append("external selector workflow gate")

        brand_name = unique_name("workflow-brand")
        brand = request("POST", "/api/brands", {
            "name": brand_name,
            "industry": "酒店",
            "products": "江景房, 早餐",
            "target_audience": "商务差旅, 情侣",
            "selling_points": "近地铁, 江景, 服务稳定",
            "competitors": "附近连锁酒店",
            "owned_sites": "https://www.example-brand.com/",
            "target_platforms": "doubao,qianwen,yuanbao,web",
            "status": "active",
        })
        brand_id = brand["id"]
        keyword = request("POST", "/api/keywords", {
            "brand_id": brand_id,
            "word": unique_name("百家号发布测试"),
            "category": "longtail",
            "search_volume": 0,
        })
        content = request("POST", "/api/operations/content/generate", {
            "brand_id": brand_id,
            "keyword_id": keyword["id"],
            "content_type": "guide",
            "target_platform": "baijiahao",
        })
        request("POST", f"/api/operations/content/{content['id']}/approve")
        expect_http(400, "POST", "/api/operations/publish/tasks", {
            "content_id": content["id"],
            "platform": "baijiahao",
        })
        publish_task_error = expect_http(400, "POST", "/api/operations/publish/tasks", {
            "content_id": content["id"],
            "platform": "baijiahao",
            "account_id": account["id"],
        })
        check("workflow" in str(publish_task_error).lower() or "tested" in str(publish_task_error).lower(), "unverified external workflow must block publish task creation")
        passed.append("external publish gate blocks untested workflow")

        website_test = request("POST", "/api/operations/publish/workflows/website/test")
        check(website_test.get("status") == "prepared", "website workflow test should be prepared")
        check(
            website_test.get("screenshot_verified") == 1 or website_test.get("raw_artifact_verified") == 1,
            "website workflow test missing verified artifact",
        )
        tests = request("GET", "/api/operations/publish/workflows/website/tests")
        check(any(item["id"] == website_test["id"] for item in tests), "workflow test was not persisted")
        passed.append("workflow test persistence")

        operations_source = (ROOT / "backend" / "app" / "routers" / "operations.py").read_text(encoding="utf-8")
        check(
            'workflow_status = "tested" if result.status == "prepared" else "draft"' in operations_source,
            "workflow test must only mark prepared editor tests as tested",
        )
        assert_tested_workflows_have_evidence()
        passed.append("workflow tested status gate")
    finally:
        for account_id in account_ids:
            try:
                request("DELETE", f"/api/operations/accounts/{account_id}")
            except Exception:
                pass
        cleanup_brand(brand_id)
        status_rank = {"disabled": 0, "template": 1, "draft": 2, "pending_review": 3, "tested": 4, "active": 5}
        for platform, workflow in original_workflows.items():
            try:
                current = request("GET", f"/api/operations/publish/workflows")
                current_item = next((item for item in current if item.get("platform") == platform), None)
                restore_status = workflow.get("status")
                restore_notes = workflow.get("notes")
                original_status = workflow.get("status")
                if current_item:
                    current_status = current_item.get("status")
                    if status_rank.get(current_status, -1) > status_rank.get(restore_status, -1):
                        restore_status = current_status
                        restore_notes = current_item.get("notes") or restore_notes
                restore_payload = {
                    "platform_name": workflow.get("platform_name"),
                    "login_url": workflow.get("login_url"),
                    "publish_url": workflow.get("publish_url"),
                    "workflow_json": workflow.get("workflow") or {},
                    "notes": restore_notes,
                }
                if restore_status not in {"tested", "active"}:
                    restore_payload["status"] = restore_status
                request("PUT", f"/api/operations/publish/workflows/{platform}", restore_payload)
                if original_status in {"tested", "active"}:
                    request("POST", f"/api/operations/publish/workflows/{platform}/test")
            except Exception:
                pass

    print(json.dumps({"status": "passed", "checks": passed}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
