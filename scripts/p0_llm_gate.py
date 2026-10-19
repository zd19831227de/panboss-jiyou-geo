#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 gate: LLM provider settings and token accounting."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend" / "geo_data.db"
from _geo_ports import BASE_URL
from _geo_auth import auth_headers

REQUIRED_PROVIDERS = {"deepseek", "minimax", "qianwen", "kimi"}


def fail(message: str) -> int:
    print(json.dumps({"status": "failed", "reason": message}, ensure_ascii=False, indent=2))
    return 1


def request(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = None
    headers = {**auth_headers()}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(payload)
        except Exception:
            return exc.code, {"detail": payload}


def main() -> int:
    if not DB.exists():
        return fail(f"数据库不存在: {DB}")

    with sqlite3.connect(DB) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing_tables = {"llm_providers", "llm_usage"} - tables
        if missing_tables:
            return fail(f"缺少 P0 表: {sorted(missing_tables)}")
        providers = {row[0] for row in conn.execute("SELECT provider FROM llm_providers")}
        missing_providers = REQUIRED_PROVIDERS - providers
        if missing_providers:
            return fail(f"缺少默认大模型供应商: {sorted(missing_providers)}")

    status, health = request("GET", "/health")
    if status != 200 or health.get("status") != "healthy":
        return fail(f"后端 health 异常: HTTP {status} {health}")

    status, providers_payload = request("GET", "/api/llm/providers")
    if status != 200 or not isinstance(providers_payload, list):
        return fail(f"providers 接口异常: HTTP {status} {providers_payload}")
    provider_names = {item.get("provider") for item in providers_payload}
    if REQUIRED_PROVIDERS - provider_names:
        return fail("providers 接口未返回完整四模型配置")
    if any("api_key" in item for item in providers_payload):
        return fail("providers 接口泄露 api_key 明文")

    target = next((item for item in providers_payload if item.get("provider") == "deepseek"), None)
    if not target:
        return fail("DeepSeek provider missing")
    original_payload = {
        "enabled": bool(target.get("enabled")),
        "base_url": target.get("base_url") or "https://api.deepseek.com",
        "default_model": target.get("default_model") or "deepseek-chat",
        "prompt_price_per_1k": target.get("prompt_price_per_1k") or 0,
        "completion_price_per_1k": target.get("completion_price_per_1k") or 0,
    }
    try:
        status, saved = request("PUT", "/api/llm/providers/deepseek", {
            "enabled": False,
            "base_url": "https://api.deepseek.com",
            "default_model": "deepseek-chat",
            "prompt_price_per_1k": 0,
            "completion_price_per_1k": 0,
        })
        if status != 200 or saved.get("provider") != "deepseek":
            return fail(f"provider 保存失败: HTTP {status} {saved}")

        status, tested = request("POST", "/api/llm/providers/deepseek/test")
        if status != 200 or tested.get("success") is not False or tested.get("status") not in {"missing_key", "disabled"}:
            return fail(f"无 Key/未启用测试必须明确失败: HTTP {status} {tested}")
    finally:
        request("PUT", "/api/llm/providers/deepseek", original_payload)

    status, usage = request("GET", "/api/llm/usage")
    if status != 200 or not {"total", "today", "month", "by_provider"}.issubset(usage.keys()):
        return fail(f"usage 接口异常: HTTP {status} {usage}")

    print(json.dumps({
        "status": "passed",
        "checks": [
            "llm_providers/llm_usage 表存在",
            "四个默认供应商存在",
            "providers 接口不泄露明文 API Key",
            "保存配置接口可用",
            "无 Key/未启用时测试明确失败",
            "Token 用量接口可用",
        ],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
