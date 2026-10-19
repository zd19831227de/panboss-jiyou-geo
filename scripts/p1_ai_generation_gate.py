#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 gate: production generation must use a real configured LLM or fail clearly."""
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


def request(method: str, path: str, body: dict | None = None):
    data = None
    headers = {"Content-Type": "application/json", **auth_headers()}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
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


def cleanup_brand(brand_id: int | None):
    if brand_id:
        try:
            request("DELETE", f"/api/brands/{brand_id}")
        except Exception:
            pass


def active_llm_configured() -> bool:
    providers = request("GET", "/api/llm/providers")
    return any(item.get("enabled") and item.get("api_key_configured") for item in providers)


def usage_count() -> int:
    return db_one("SELECT COUNT(*) AS count FROM llm_usage")["count"]


def assert_trace_row(table: str, brand_id: int, provider: str = "deepseek"):
    row = db_one(
        f"""SELECT llm_provider, llm_model, llm_usage_id
            FROM {table}
            WHERE brand_id = ? ORDER BY id DESC LIMIT 1""",
        (brand_id,),
    )
    if not row:
        raise GateFailure(f"{table} did not write a row")
    if row.get("llm_provider") != provider:
        raise GateFailure(f"{table} did not record provider {provider}: {row}")
    if not row.get("llm_model") or not row.get("llm_usage_id"):
        raise GateFailure(f"{table} missing model or usage trace: {row}")
    usage = db_one(
        "SELECT success, total_tokens FROM llm_usage WHERE id = ?",
        (row["llm_usage_id"],),
    )
    if not usage or usage.get("success") != 1 or usage.get("total_tokens", 0) <= 0:
        raise GateFailure(f"{table} usage record is not a successful real call: {usage}")


def main() -> int:
    brand_id = None
    try:
        health = request("GET", "/health")
        if health.get("status") != "healthy":
            raise GateFailure("backend health failed")

        brand = request("POST", "/api/brands", {
            "name": f"p1-ai-gate-{int(time.time())}",
            "industry": "酒店",
            "products": "高层景观房, 早餐, 地铁旁住宿",
            "target_audience": "城市旅游用户, 步行街游客, 商务差旅",
            "selling_points": "江景, 近步行街商圈, 交通方便, 高空视野",
            "competitors": "同商圈连锁酒店A, 同商圈连锁酒店B",
            "owned_sites": "https://www.example-hotel-brand.invalid/manual-check-required",
            "target_platforms": "doubao,qianwen,yuanbao",
            "status": "active",
        })
        brand_id = brand["id"]
        keyword = request("POST", "/api/keywords", {
            "brand_id": brand_id,
            "word": "市中心步行街酒店推荐",
            "category": "longtail",
            "search_volume": 0,
        })

        schema_checks = {
            "keyword_ideas": ["llm_provider", "llm_model", "llm_usage_id"],
            "content_assets": ["llm_provider", "llm_model", "llm_usage_id"],
            "geo_reports": ["llm_provider", "llm_model", "llm_usage_id"],
        }
        with sqlite3.connect(DB) as conn:
            for table, cols in schema_checks.items():
                existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                missing = set(cols) - existing
                if missing:
                    raise GateFailure(f"{table} missing traceability columns: {sorted(missing)}")

        if active_llm_configured():
            before_usage = usage_count()
            ideas = request("POST", "/api/operations/keywords/generate", {
                "brand_id": brand_id,
                "seed": "市中心酒店",
                "count": 6,
            })
            if ideas.get("source") != "llm_generated" or ideas.get("count", 0) < 3:
                raise GateFailure(f"keyword generation did not return LLM result: {ideas}")
            assert_trace_row("keyword_ideas", brand_id)

            content = request("POST", "/api/operations/content/generate", {
                "brand_id": brand_id,
                "keyword_id": keyword["id"],
                "content_type": "guide",
                "target_platform": "website",
            })
            if content.get("source") != "llm_generated" or not content.get("body"):
                raise GateFailure(f"content generation did not return LLM result: {content}")
            assert_trace_row("content_assets", brand_id)

            report = request("POST", f"/api/operations/reports/scorecard?brand_id={brand_id}")
            if report.get("source") != "llm_generated_from_real_data" or not report.get("diagnosis"):
                raise GateFailure(f"report generation did not return LLM result: {report}")
            assert_trace_row("geo_reports", brand_id)

            after_usage = usage_count()
            if after_usage < before_usage + 3:
                raise GateFailure(f"expected at least 3 LLM usage rows, before={before_usage}, after={after_usage}")

            checks = [
                "有可用大模型时关键词真实生成并写库",
                "有可用大模型时内容真实生成并写库",
                "有可用大模型时运营报告真实生成并写库",
                "关键词/内容/报告均记录模型与用量追溯",
                "llm_usage 产生真实 Token 记录",
            ]
        else:
            before = {
                "ideas": db_one("SELECT COUNT(*) AS count FROM keyword_ideas")["count"],
                "content": db_one("SELECT COUNT(*) AS count FROM content_assets")["count"],
                "reports": db_one("SELECT COUNT(*) AS count FROM geo_reports")["count"],
            }
            for path, body, label in [
                ("/api/operations/keywords/generate", {"brand_id": brand_id, "seed": "市中心酒店", "count": 6}, "keyword"),
                ("/api/operations/content/generate", {"brand_id": brand_id, "keyword_id": keyword["id"], "content_type": "guide", "target_platform": "website"}, "content"),
                (f"/api/operations/reports/scorecard?brand_id={brand_id}", None, "report"),
            ]:
                try:
                    request("POST", path, body)
                except HttpFailure as exc:
                    detail = exc.data.get("detail") if isinstance(exc.data, dict) else str(exc.data)
                    if exc.status != 400 or "未接入可用大模型" not in str(detail):
                        raise GateFailure(f"{label} should fail clearly without LLM key, got {exc.status}: {exc.data}")
                else:
                    raise GateFailure(f"{label} generation succeeded without configured LLM key")
            after = {
                "ideas": db_one("SELECT COUNT(*) AS count FROM keyword_ideas")["count"],
                "content": db_one("SELECT COUNT(*) AS count FROM content_assets")["count"],
                "reports": db_one("SELECT COUNT(*) AS count FROM geo_reports")["count"],
            }
            if before != after:
                raise GateFailure(f"no-key generation wrote data: before={before}, after={after}")
            checks = [
                "无可用大模型时关键词生产明确失败",
                "无可用大模型时内容生产明确失败",
                "无可用大模型时运营报告明确失败",
                "失败状态不写入生产数据",
                "关键词/内容/报告具备模型与用量追溯字段",
            ]

        print(json.dumps({
            "status": "passed",
            "mode": "real_llm" if active_llm_configured() else "no_key_guard",
            "checks": checks,
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    finally:
        cleanup_brand(brand_id)


if __name__ == "__main__":
    raise SystemExit(main())
