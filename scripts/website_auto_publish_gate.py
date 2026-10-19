#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate: website/landing-page auto publish URL and verification loop."""
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


def request(method: str, path: str, body=None):
    data = None
    headers = {"Content-Type": "application/json", **auth_headers()}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
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
            request("DELETE", f"/api/brands/{brand_id}")
        except Exception:
            pass


def main() -> int:
    health = request("GET", "/health")
    check(health.get("status") == "healthy", "backend is not healthy")
    brand_id = None
    passed = []
    try:
        brand = request("POST", "/api/brands", {
            "name": f"website-auto-publish-{int(time.time())}",
            "industry": "酒店",
            "products": "高空江景房, 早餐",
            "target_audience": "商务差旅, 周末旅行",
            "selling_points": "市中心地段, 高层景观, 交通便利",
            "owned_sites": "https://www.example-brand.com/",
            "target_platforms": "doubao,qianwen,yuanbao",
            "status": "active",
        })
        brand_id = brand["id"]
        keyword = request("POST", "/api/keywords", {
            "brand_id": brand_id,
            "word": f"市中心高层景观酒店推荐{int(time.time())}",
            "category": "longtail",
        })
        content = request("POST", "/api/operations/content/generate", {
            "brand_id": brand_id,
            "keyword_id": keyword["id"],
            "content_type": "guide",
            "target_platform": "website",
        })
        approved = request("POST", f"/api/operations/content/{content['id']}/approve")
        check(approved.get("status") == "approved", "content approval failed")
        task = request("POST", "/api/operations/publish/tasks", {
            "content_id": content["id"],
            "platform": "website",
            "publish_mode": "manual_confirm",
        })
        prepared = request("POST", f"/api/operations/publish/tasks/{task['id']}/prepare")
        # prepare 落库状态为 assets_ready（历史实现曾返回 prepared），两者都代表三件套已生成。
        check(prepared.get("status") in {"assets_ready", "prepared"}, "website prepare failed")
        verified = request("POST", f"/api/operations/publish/tasks/{task['id']}/auto-verify", {
            "trigger_monitor": False,
        })
        task_data = verified.get("task") or {}
        check(task_data.get("status") == "published", "auto verify did not publish")
        check(task_data.get("published_url_verified") == 1, "published URL was not verified")
        check(task_data.get("evidence_status") == "verified", "evidence status is not verified")
        check((task_data.get("published_url") or "").startswith("/artifacts/"), "website published URL must be an artifact URL")
        with urllib.request.urlopen(BASE_URL + task_data["published_url"], timeout=10) as resp:
            html = resp.read().decode("utf-8")
        check(resp.status == 200, "published artifact URL is not reachable")
        check((task_data.get("raw_artifact_path") or "").endswith(".html"), "raw artifact is not HTML")
        row = db_one(
            """SELECT published_url_verified, screenshot_verified, raw_artifact_verified, summary_artifact_verified
               FROM publishing_tasks WHERE id = ?""",
            (task["id"],),
        )
        check(row and all(int(row[key] or 0) == 1 for key in row), "DB evidence flags are incomplete")
        check(content["title"] in html, "published HTML does not contain generated title")
        passed.append("website auto publish URL verified")
    finally:
        cleanup_brand(brand_id)

    orphan = db_one("SELECT COUNT(*) AS count FROM brands WHERE id = ?", (brand_id or -1,))
    check((orphan or {}).get("count") == 0, "gate cleanup failed")
    print(json.dumps({"status": "passed", "checks": passed}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
