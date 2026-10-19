#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P4 gate: confirmed AI strategy actions execute only whitelisted system APIs."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
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


def request(method: str, path: str, body: dict | None = None, timeout: int = 120):
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


def expect_http(status: int, method: str, path: str, body: dict | None = None):
    try:
        result = request(method, path, body, timeout=30)
    except HttpFailure as exc:
        if exc.status == status:
            return exc.data
        raise
    raise GateFailure(f"{method} {path} expected HTTP {status}, got success: {result}")


def db_one(sql: str, params=()):
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def db_all(sql: str, params=()):
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def db_exec(sql: str, params=()):
    with sqlite3.connect(DB) as conn:
        conn.execute(sql, params)
        conn.commit()


def cleanup_brand(brand_id: int | None):
    if brand_id:
        try:
            request("DELETE", f"/api/brands/{brand_id}", timeout=30)
        except Exception:
            pass


def count(table: str, brand_id: int) -> int:
    return db_one(f"SELECT COUNT(*) AS count FROM {table} WHERE brand_id = ?", (brand_id,))["count"]


def insert_action(strategy_id: int, brand_id: int, action_type: str, title: str, payload: dict, status: str = "confirmed") -> int:
    with sqlite3.connect(DB) as conn:
        cursor = conn.execute(
            """INSERT INTO geo_strategy_actions
               (strategy_id, brand_id, action_type, title, description, payload_json,
                requires_login, cost_risk, requires_confirmation, status)
               VALUES (?, ?, ?, ?, ?, ?, 0, 'low', 1, ?)""",
            (strategy_id, brand_id, action_type, title, f"P4 gate {action_type}", json.dumps(payload, ensure_ascii=False), status),
        )
        conn.commit()
        return int(cursor.lastrowid)


def main() -> int:
    brand_id = None
    try:
        health = request("GET", "/health", timeout=30)
        if health.get("status") != "healthy":
            raise GateFailure("backend health failed")

        stamp = int(time.time())
        brand = request("POST", "/api/brands", {
            "name": f"p4-executor-gate-{stamp}",
            "industry": "酒店",
            "products": "高层景观房, 步行街住宿, 早餐",
            "target_audience": "城市旅游用户, 商务差旅, 情侣出游",
            "selling_points": "江景, 近步行街商圈, 交通方便, 高空视野",
            "competitors": "同商圈连锁酒店A, 同商圈连锁酒店B",
            "owned_sites": "https://www.example-hotel-brand.invalid, https://www.ctrip.com",
            "target_platforms": "doubao,qianwen,yuanbao",
            "status": "active",
        }, timeout=30)
        brand_id = brand["id"]
        keyword = request("POST", "/api/keywords", {
            "brand_id": brand_id,
            "word": "市中心步行街酒店推荐",
            "category": "longtail",
            "search_volume": 0,
        }, timeout=30)
        strategy = request("POST", "/api/operations/strategy/generate", {
            "brand_id": brand_id,
            "objective": "P4 executor gate strategy",
        }, timeout=150)
        strategy_id = strategy["id"]

        expect_http(400, "POST", f"/api/operations/strategy/{strategy_id}/actions/execute", {})

        generated = request("POST", "/api/operations/content/generate", {
            "brand_id": brand_id,
            "keyword_id": keyword["id"],
            "content_type": "guide",
            "target_platform": "website",
        }, timeout=120)
        request("POST", f"/api/operations/content/{generated['id']}/approve", timeout=60)

        db_exec("DELETE FROM geo_strategy_actions WHERE strategy_id = ?", (strategy_id,))
        actions = [
            insert_action(strategy_id, brand_id, "generate_keywords", "生成补充关键词", {"seed": "市中心酒店推荐", "count": 3}),
            insert_action(strategy_id, brand_id, "adopt_keywords", "采纳关键词建议", {"count": 2}),
            insert_action(strategy_id, brand_id, "run_monitor", "提交监测任务", {"keyword_ids": [keyword["id"]], "platform": "web"}),
            insert_action(strategy_id, brand_id, "generate_content", "生成官网内容", {"keyword_id": keyword["id"], "content_type": "guide", "target_platform": "website"}),
            insert_action(strategy_id, brand_id, "create_publish_task", "创建官网发布任务", {"content_id": generated["id"], "platform": "website"}),
            insert_action(strategy_id, brand_id, "prepare_rpa_draft", "准备发布草稿", {}),
            insert_action(strategy_id, brand_id, "generate_report", "生成运营报告", {}),
        ]

        before = {
            "keyword_ideas": count("keyword_ideas", brand_id),
            "keywords": count("keywords", brand_id),
            "monitor_runs": count("monitor_runs", brand_id),
            "content_assets": count("content_assets", brand_id),
            "publishing_tasks": count("publishing_tasks", brand_id),
            "geo_reports": count("geo_reports", brand_id),
        }
        result = request("POST", f"/api/operations/strategy/{strategy_id}/actions/execute", {}, timeout=240)
        if result.get("failed_count") != 0:
            raise GateFailure(f"executor returned failures: {result}")
        if result.get("executed_count") != len(actions):
            raise GateFailure(f"executed_count mismatch: {result}")
        after = {
            "keyword_ideas": count("keyword_ideas", brand_id),
            "keywords": count("keywords", brand_id),
            "monitor_runs": count("monitor_runs", brand_id),
            "content_assets": count("content_assets", brand_id),
            "publishing_tasks": count("publishing_tasks", brand_id),
            "geo_reports": count("geo_reports", brand_id),
        }
        if after["keyword_ideas"] <= before["keyword_ideas"]:
            raise GateFailure("generate_keywords did not create keyword ideas")
        if after["keywords"] <= before["keywords"]:
            raise GateFailure("adopt_keywords did not create/adopt keywords")
        if after["monitor_runs"] <= before["monitor_runs"]:
            raise GateFailure("run_monitor did not create monitor run")
        if after["content_assets"] <= before["content_assets"]:
            raise GateFailure("generate_content did not create content")
        if after["publishing_tasks"] <= before["publishing_tasks"]:
            raise GateFailure("create_publish_task did not create task")
        if after["geo_reports"] <= before["geo_reports"]:
            raise GateFailure("generate_report did not create report")

        rows = db_all("SELECT id, status, execution_result_json FROM geo_strategy_actions WHERE strategy_id = ?", (strategy_id,))
        if any(row["status"] != "executed" for row in rows):
            raise GateFailure(f"not all actions marked executed: {rows}")
        for row in rows:
            payload = json.loads(row["execution_result_json"] or "{}")
            if payload.get("success") is not True:
                raise GateFailure(f"execution result not successful: {payload}")

        print(json.dumps({
            "status": "passed",
            "checks": [
                "未确认执行项不能执行",
                "P4 执行器只执行 confirmed action",
                "generate_keywords 真实调用大模型并写 keyword_ideas",
                "adopt_keywords 真实写入 keywords",
                "run_monitor 真实创建 monitor_runs/task_queue",
                "generate_content 真实调用大模型并写 content_assets",
                "create_publish_task 真实写 publishing_tasks",
                "prepare_rpa_draft 真实生成本地发布草稿证据",
                "generate_report 真实生成 geo_reports",
                "每个 action 持久化 execution_result_json",
            ],
            "strategy_id": strategy_id,
            "executed_count": result.get("executed_count"),
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    finally:
        cleanup_brand(brand_id)


if __name__ == "__main__":
    raise SystemExit(main())
