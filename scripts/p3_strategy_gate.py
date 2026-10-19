#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3 gate: AI strategy center must generate strategy and selectable actions only."""
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

ALLOWED_ACTION_TYPES = {
    "generate_keywords",
    "adopt_keywords",
    "run_monitor",
    "generate_content",
    "create_publish_task",
    "open_publish_browser",
    "prepare_rpa_draft",
    "run_recheck",
    "generate_report",
}


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


def db_one(sql: str, params=()):
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def db_all(sql: str, params=()):
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def cleanup_brand(brand_id: int | None):
    if brand_id:
        try:
            request("DELETE", f"/api/brands/{brand_id}", timeout=30)
        except Exception:
            pass


def active_llm_configured() -> bool:
    providers = request("GET", "/api/llm/providers", timeout=30)
    return any(item.get("enabled") and item.get("api_key_configured") for item in providers)


def table_count(table: str, brand_id: int) -> int:
    return db_one(f"SELECT COUNT(*) AS count FROM {table} WHERE brand_id = ?", (brand_id,))["count"]


def main() -> int:
    brand_id = None
    try:
        health = request("GET", "/health", timeout=30)
        if health.get("status") != "healthy":
            raise GateFailure("backend health failed")

        with sqlite3.connect(DB) as conn:
            for table in ["geo_strategies", "geo_strategy_actions"]:
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not exists:
                    raise GateFailure(f"missing table: {table}")

        stamp = int(time.time())
        brand = request("POST", "/api/brands", {
            "name": f"p3-strategy-gate-{stamp}",
            "industry": "酒店",
            "products": "高层景观房, 步行街住宿, 早餐, 地铁旁住宿",
            "target_audience": "城市旅游用户, 步行街游客, 商务差旅, 情侣出游",
            "selling_points": "江景, 近步行街商圈, 交通方便, 高空视野, 可步行到核心商圈",
            "competitors": "同商圈连锁酒店A, 同商圈连锁酒店B, 临江景观酒店",
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

        with sqlite3.connect(DB) as conn:
            conn.execute(
                """INSERT INTO monitor_results
                   (brand_id, keyword_id, platform, search_query, is_cited, cited_sources,
                    snippet, source_url, source_domain, answer_text, verification_status,
                    source_verified, screenshot_verified, raw_artifact_verified, monitored_at)
                   VALUES (?, ?, 'qianwen', ?, 1, 'P3真实门禁来源', 'P3门禁监测回答',
                           'https://www.ctrip.com', 'ctrip.com', 'P3门禁回答文本',
                           'verified_citation', 1, 1, 1, datetime('now','localtime'))""",
                (brand_id, keyword["id"], "市中心步行街酒店推荐"),
            )
            conn.commit()

        before = {
            "keywords": table_count("keywords", brand_id),
            "keyword_ideas": table_count("keyword_ideas", brand_id),
            "content_assets": table_count("content_assets", brand_id),
            "publishing_tasks": table_count("publishing_tasks", brand_id),
            "geo_reports": table_count("geo_reports", brand_id),
        }

        if not active_llm_configured():
            try:
                request("POST", "/api/operations/strategy/generate", {
                    "brand_id": brand_id,
                    "objective": "生成酒店 GEO 策略",
                })
            except HttpFailure as exc:
                detail = exc.data.get("detail") if isinstance(exc.data, dict) else str(exc.data)
                if exc.status != 400 or "未接入可用大模型" not in str(detail):
                    raise GateFailure(f"strategy should fail clearly without LLM key, got {exc.status}: {exc.data}")
            else:
                raise GateFailure("strategy generation succeeded without configured LLM key")
            print(json.dumps({
                "status": "passed",
                "mode": "no_key_guard",
                "checks": ["无可用大模型时策略生成明确失败", "失败不写入策略数据"],
            }, ensure_ascii=False, indent=2))
            return 0

        strategy = request("POST", "/api/operations/strategy/generate", {
            "brand_id": brand_id,
            "objective": "围绕市中心步行街酒店推荐场景，生成可执行 GEO 策略",
        }, timeout=150)
        if strategy.get("source") != "llm_generated_from_real_system_data":
            raise GateFailure(f"strategy source invalid: {strategy.get('source')}")
        if not strategy.get("llm_provider") or not strategy.get("llm_model") or not strategy.get("llm_usage_id"):
            raise GateFailure("strategy missing llm trace fields")
        if not strategy.get("strategy") or not strategy.get("actions"):
            raise GateFailure("strategy missing strategy/actions payload")
        if len(strategy["actions"]) < 3:
            raise GateFailure(f"expected at least 3 actions, got {len(strategy['actions'])}")

        usage = db_one("SELECT success, total_tokens FROM llm_usage WHERE id = ?", (strategy["llm_usage_id"],))
        if not usage or usage["success"] != 1 or usage["total_tokens"] <= 0:
            raise GateFailure(f"strategy usage is not a successful real call: {usage}")

        rows = db_all("SELECT * FROM geo_strategy_actions WHERE strategy_id = ?", (strategy["id"],))
        if len(rows) != len(strategy["actions"]):
            raise GateFailure("action rows do not match API response")
        invalid = [row["action_type"] for row in rows if row["action_type"] not in ALLOWED_ACTION_TYPES]
        if invalid:
            raise GateFailure(f"invalid action types: {invalid}")
        if any(row["requires_confirmation"] != 1 for row in rows):
            raise GateFailure("all strategy actions must require confirmation")
        if any(row["status"] != "pending" for row in rows):
            raise GateFailure("new strategy actions must stay pending before user confirmation")

        confirm_ids = [row["id"] for row in rows[:2]]
        confirmed = request("POST", f"/api/operations/strategy/{strategy['id']}/actions/confirm", {
            "action_ids": confirm_ids,
        }, timeout=30)
        if confirmed.get("side_effects_executed") is not False:
            raise GateFailure("P3 confirm must not execute side effects")
        if confirmed.get("confirmed_count") != len(confirm_ids):
            raise GateFailure("confirmed action count mismatch")
        statuses = db_all(
            "SELECT id, status FROM geo_strategy_actions WHERE strategy_id = ? ORDER BY id",
            (strategy["id"],),
        )
        confirmed_statuses = {row["id"]: row["status"] for row in statuses}
        if any(confirmed_statuses[action_id] != "confirmed" for action_id in confirm_ids):
            raise GateFailure("confirmed actions did not persist")

        after = {
            "keywords": table_count("keywords", brand_id),
            "keyword_ideas": table_count("keyword_ideas", brand_id),
            "content_assets": table_count("content_assets", brand_id),
            "publishing_tasks": table_count("publishing_tasks", brand_id),
            "geo_reports": table_count("geo_reports", brand_id),
        }
        if before != after:
            raise GateFailure(f"P3 strategy generated side-effect data: before={before}, after={after}")

        listed = request("GET", f"/api/operations/strategy?brand_id={brand_id}&limit=5", timeout=30)
        if not listed or listed[0].get("id") != strategy["id"]:
            raise GateFailure("strategy list endpoint did not return latest strategy")

        print(json.dumps({
            "status": "passed",
            "mode": "real_llm",
            "checks": [
                "AI策略由真实大模型生成并写库",
                "策略记录包含 llm_provider/llm_model/llm_usage_id",
                "执行项只包含系统白名单 action_type",
                "执行项默认 pending 且 requires_confirmation=1",
                "确认执行项只保存选择，不执行关键词/内容/发布/报告副作用",
                "策略列表接口返回真实库记录",
            ],
            "strategy_id": strategy["id"],
            "action_count": len(rows),
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    finally:
        cleanup_brand(brand_id)


if __name__ == "__main__":
    raise SystemExit(main())
