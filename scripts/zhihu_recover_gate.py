#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate: Zhihu recover/verify does not fabricate publish success."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "geo_data.db"
from _geo_ports import BASE_URL
from _geo_auth import auth_headers



class GateFailure(Exception):
    pass


class HttpFailure(Exception):
    def __init__(self, status: int, data):
        super().__init__(f"HTTP {status}: {data}")
        self.status = status
        self.data = data


def request(method: str, path: str, body=None, timeout=120):
    data = None
    headers = {"Content-Type": "application/json", **auth_headers()}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = raw
        raise HttpFailure(exc.code, payload)


def check(condition, message: str):
    if not condition:
        raise GateFailure(message)


def db_one(sql: str, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def cleanup_brand(brand_id: int | None):
    if brand_id:
        try:
            request("DELETE", f"/api/brands/{brand_id}", timeout=30)
        except Exception:
            pass


def main() -> int:
    health = request("GET", "/health")
    check(health.get("status") == "healthy", "backend is not healthy")
    brand_id = None
    account_id = None
    passed = []
    try:
        brand = request("POST", "/api/brands", {
            "name": f"zhihu-recover-{int(time.time())}",
            "industry": "酒店",
            "products": "高层景观房, 早餐",
            "target_audience": "商务差旅, 情侣出行",
            "selling_points": "市中心步行街, 高层景观, 近地铁",
            "owned_sites": "https://www.example-brand.com/",
            "target_platforms": "doubao,qianwen,yuanbao",
            "status": "active",
        })
        brand_id = brand["id"]
        keyword = request("POST", "/api/keywords", {
            "brand_id": brand_id,
            "word": f"市中心酒店知乎推荐{int(time.time())}",
            "category": "longtail",
        })
        content = request("POST", "/api/operations/content/generate", {
            "brand_id": brand_id,
            "keyword_id": keyword["id"],
            "content_type": "guide",
            "target_platform": "zhihu",
        })
        request("POST", f"/api/operations/content/{content['id']}/approve")
        account = request("POST", "/api/operations/accounts", {
            "platform": "zhihu",
            "account_name": f"zhihu-gate-{int(time.time())}",
        })
        account_id = account["id"]
        task = request("POST", "/api/operations/publish/tasks", {
            "content_id": content["id"],
            "platform": "zhihu",
            "account_id": account_id,
        })
        task_id = task["id"]
        prepared = request("POST", f"/api/operations/publish/tasks/{task_id}/prepare")
        check(prepared.get("status") in {"prepared", "manual_required", "login_required"}, "zhihu prepare returned invalid status")
        row = db_one(
            "SELECT status, published_url_verified, screenshot_verified, raw_artifact_verified, summary_artifact_verified FROM publishing_tasks WHERE id = ?",
            (task_id,),
        )
        check(row and row["status"] in {"prepared", "manual_required", "login_required"}, "zhihu task did not persist safe non-published state")
        check(int(row["published_url_verified"] or 0) == 0, "zhihu prepare must not mark URL verified before real published URL")
        if row["status"] != "login_required":
            check(int(row["screenshot_verified"] or 0) == 1, "zhihu prepare missing screenshot evidence")
            check(int(row["raw_artifact_verified"] or 0) == 1, "zhihu prepare missing raw artifact evidence")
            check(int(row["summary_artifact_verified"] or 0) == 1, "zhihu prepare missing summary evidence")
        passed.append("tested Zhihu workflow creates safe non-published task with evidence")

        data = request("POST", f"/api/operations/publish/tasks/{task_id}/recover-url", {
            "published_url": "https://www.zhihu.com/",
            "trigger_monitor": False,
        })
        check(data.get("status") == "manual_required", "invalid Zhihu URL must not verify")
        row = db_one("SELECT status, published_url_verified, evidence_status FROM publishing_tasks WHERE id = ?", (task_id,))
        check(row and row["status"] == "manual_required", "failed verification did not persist manual_required")
        check(int(row["published_url_verified"] or 0) == 0, "failed verification incorrectly marked URL verified")
        passed.append("invalid Zhihu URL does not fake success")
    finally:
        if account_id:
            try:
                request("DELETE", f"/api/operations/accounts/{account_id}", timeout=30)
            except Exception:
                pass
        cleanup_brand(brand_id)

    orphan = db_one("SELECT COUNT(*) AS count FROM brands WHERE id = ?", (brand_id or -1,))
    check((orphan or {}).get("count") == 0, "gate cleanup failed")
    print(json.dumps({"status": "passed", "checks": passed}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
