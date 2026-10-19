#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 gate: dashboard all-brand and single-brand filtering."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend" / "geo_data.db"
from _geo_ports import BASE_URL
from _geo_auth import auth_headers



class GateFailure(Exception):
    pass


class HttpFailure(Exception):
    def __init__(self, status: int, data):
        super().__init__(f"HTTP {status}: {data}")
        self.status = status
        self.data = data


def request(method: str, path: str, body: dict | None = None):
    data = None
    headers = {"Content-Type": "application/json", **auth_headers()}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = raw
        raise HttpFailure(exc.code, payload)


def db_exec(sql: str, params=()):
    with sqlite3.connect(DB) as conn:
        conn.execute(sql, params)
        conn.commit()


def db_one(sql: str, params=()):
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def cleanup(brand_ids: list[int]):
    for brand_id in brand_ids:
        try:
            request("DELETE", f"/api/brands/{brand_id}")
        except Exception:
            pass


def add_result(brand_id: int, keyword_id: int, platform: str, query: str, verified: bool):
    db_exec(
        """INSERT INTO monitor_results
           (brand_id, keyword_id, platform, search_query, is_cited, cited_sources,
            snippet, source_url, source_domain, answer_text, verification_status,
            source_verified, screenshot_verified, raw_artifact_verified, monitored_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
        (
            brand_id,
            keyword_id,
            platform,
            query,
            1 if verified else 0,
            "P2 gate source",
            "P2 gate answer",
            "https://www.example.com/p2-gate",
            "example.com",
            "P2 gate answer text",
            "verified_citation" if verified else "not_cited",
            1 if verified else 0,
            1,
            1,
        ),
    )


def assert_equal(left, right, message: str):
    if left != right:
        raise GateFailure(f"{message}: expected {right}, got {left}")


def main() -> int:
    brand_ids: list[int] = []
    try:
        health = request("GET", "/health")
        if health.get("status") != "healthy":
            raise GateFailure("backend health failed")

        stamp = int(time.time())
        brand_a = request("POST", "/api/brands", {
            "name": f"p2-dashboard-a-{stamp}",
            "industry": "酒店",
            "products": "江景房",
            "target_audience": "游客",
            "selling_points": "江景",
            "competitors": "竞品A",
            "owned_sites": "https://www.example.com/a",
            "target_platforms": "doubao,qianwen,yuanbao",
            "status": "active",
        })
        brand_b = request("POST", "/api/brands", {
            "name": f"p2-dashboard-b-{stamp}",
            "industry": "酒店",
            "products": "亲子房",
            "target_audience": "亲子",
            "selling_points": "亲子便利",
            "competitors": "竞品B",
            "owned_sites": "https://www.example.com/b",
            "target_platforms": "doubao,qianwen,yuanbao",
            "status": "active",
        })
        brand_ids = [brand_a["id"], brand_b["id"]]
        kw_a = request("POST", "/api/keywords", {"brand_id": brand_a["id"], "word": "市中心酒店推荐A", "category": "longtail", "search_volume": 0})
        kw_b = request("POST", "/api/keywords", {"brand_id": brand_b["id"], "word": "市中心酒店推荐B", "category": "longtail", "search_volume": 0})
        add_result(brand_a["id"], kw_a["id"], "doubao", "市中心酒店推荐A", True)
        add_result(brand_a["id"], kw_a["id"], "qianwen", "市中心酒店推荐A", True)
        add_result(brand_b["id"], kw_b["id"], "yuanbao", "市中心酒店推荐B", False)

        stats_a = request("GET", f"/api/dashboard/stats?brand_id={brand_a['id']}")
        stats_b = request("GET", f"/api/dashboard/stats?brand_id={brand_b['id']}")
        assert_equal(stats_a["totalMonitorCount"], 2, "brand A monitor count")
        assert_equal(stats_a["verifiedCitationCount"], 2, "brand A verified count")
        assert_equal(stats_b["totalMonitorCount"], 1, "brand B monitor count")
        assert_equal(stats_b["verifiedCitationCount"], 0, "brand B verified count")

        for mode in ["monthly", "weekly", "daily"]:
            trend_a = request("GET", f"/api/dashboard/{mode}-trend?brand_id={brand_a['id']}")
            trend_b = request("GET", f"/api/dashboard/{mode}-trend?brand_id={brand_b['id']}")
            if sum(item.get("count", 0) for item in trend_a) < 2:
                raise GateFailure(f"brand A {mode} trend did not include its rows: {trend_a}")
            if sum(item.get("count", 0) for item in trend_b) < 1:
                raise GateFailure(f"brand B {mode} trend did not include its rows: {trend_b}")

        recent_a = request("GET", f"/api/monitor/results?brand_id={brand_a['id']}&limit=20")
        recent_b = request("GET", f"/api/monitor/results?brand_id={brand_b['id']}&limit=20")
        if not recent_a or any(item.get("brand_id") != brand_a["id"] for item in recent_a):
            raise GateFailure("recent results for brand A are not scoped")
        if not recent_b or any(item.get("brand_id") != brand_b["id"] for item in recent_b):
            raise GateFailure("recent results for brand B are not scoped")

        all_stats = request("GET", "/api/dashboard/all-stats")
        if all_stats.get("totalMonitorCount", 0) < stats_a["totalMonitorCount"] + stats_b["totalMonitorCount"]:
            raise GateFailure("all stats did not include both test brands")

        command_a = request("GET", f"/api/dashboard/command-center?brand_id={brand_a['id']}")
        command_b = request("GET", f"/api/dashboard/command-center?brand_id={brand_b['id']}")
        assert_equal(command_a["scope"]["brand_id"], brand_a["id"], "command center brand A scope")
        assert_equal(command_b["scope"]["brand_id"], brand_b["id"], "command center brand B scope")
        assert_equal(command_a["overview"]["total_monitor"], 2, "command center brand A monitor count")
        assert_equal(command_a["overview"]["verified"], 2, "command center brand A verified count")
        assert_equal(command_b["overview"]["total_monitor"], 1, "command center brand B monitor count")
        assert_equal(command_b["overview"]["verified"], 0, "command center brand B verified count")
        if any(item.get("brand_name") != brand_a["name"] for item in command_a.get("latest_results", [])):
            raise GateFailure("command center latest results for brand A are not scoped")
        if any(item.get("brand_name") != brand_b["name"] for item in command_b.get("latest_results", [])):
            raise GateFailure("command center latest results for brand B are not scoped")

        print(json.dumps({
            "status": "passed",
            "checks": [
                "单品牌统计按 brand_id 过滤",
                "全量统计包含全部品牌",
                "日/周/月趋势按 brand_id 过滤",
                "最近监测结果按 brand_id 过滤",
                "运营大屏 command-center 按 brand_id 过滤",
                "刷新数据来自真实 monitor_results 写库记录",
            ],
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    finally:
        cleanup(brand_ids)


if __name__ == "__main__":
    raise SystemExit(main())
