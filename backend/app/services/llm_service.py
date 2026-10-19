"""Local LLM provider configuration and OpenAI-compatible calls."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import httpx

from app.database import get_db, local_now


ALLOWED_PROVIDERS = {"deepseek", "minimax", "qianwen", "kimi"}


def mask_key(api_key: Optional[str]) -> Optional[str]:
    if not api_key:
        return None
    if len(api_key) <= 10:
        return api_key[:2] + "***"
    return f"{api_key[:6]}...{api_key[-4:]}"


def provider_row(provider: str) -> Optional[dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM llm_providers WHERE provider = ?", (provider,)).fetchone()
        return dict(row) if row else None


def list_providers() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM llm_providers ORDER BY priority ASC, id ASC").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["api_key_masked"] = mask_key(item.get("api_key"))
        item["api_key_configured"] = bool(item.get("api_key"))
        item.pop("api_key", None)
        result.append(item)
    return result


def update_provider(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    if provider not in ALLOWED_PROVIDERS:
        raise ValueError(f"不支持的大模型供应商: {provider}")
    current = provider_row(provider)
    if not current:
        raise ValueError(f"大模型供应商不存在: {provider}")

    allowed = {
        "display_name",
        "base_url",
        "default_model",
        "api_key",
        "enabled",
        "priority",
        "prompt_price_per_1k",
        "completion_price_per_1k",
    }
    updates = []
    params: list[Any] = []
    for key in allowed:
        if key not in payload:
            continue
        value = payload[key]
        if key == "api_key" and value == "":
            value = None
        updates.append(f"{key} = ?")
        params.append(value)
    if not updates:
        return sanitized_provider(provider_row(provider) or current)

    updates.append("updated_at = ?")
    params.append(local_now())
    params.append(provider)
    with get_db() as conn:
        conn.execute(f"UPDATE llm_providers SET {', '.join(updates)} WHERE provider = ?", params)
    return sanitized_provider(provider_row(provider) or current)


def sanitized_provider(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["api_key_masked"] = mask_key(item.get("api_key"))
    item["api_key_configured"] = bool(item.get("api_key"))
    item.pop("api_key", None)
    return item


def _chat_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def record_usage(
    provider: str,
    model: str,
    operation: str,
    usage: Optional[dict[str, Any]] = None,
    success: bool = False,
    error_message: Optional[str] = None,
) -> dict[str, Any]:
    usage = usage or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
    row = provider_row(provider) or {}
    prompt_price = float(row.get("prompt_price_per_1k") or 0)
    completion_price = float(row.get("completion_price_per_1k") or 0)
    cost = (prompt_tokens / 1000 * prompt_price) + (completion_tokens / 1000 * completion_price)
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO llm_usage
               (provider, model, operation, prompt_tokens, completion_tokens,
                total_tokens, cost, success, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                provider,
                model,
                operation,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                round(cost, 6),
                1 if success else 0,
                (error_message or "")[:500],
            ),
        )
        usage_id = cursor.lastrowid
    return {
        "id": usage_id,
        "provider": provider,
        "model": model,
        "operation": operation,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost": round(cost, 6),
        "success": success,
        "error_message": error_message,
    }


def chat_completion(
    provider: str,
    messages: list[dict[str, str]],
    operation: str,
    model: Optional[str] = None,
    temperature: float = 0.2,
    timeout: int = 45,
    response_format: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    row = provider_row(provider)
    if not row:
        raise ValueError(f"大模型供应商不存在: {provider}")
    if not row.get("enabled"):
        raise ValueError(f"{row.get('display_name') or provider} 未启用")
    if not row.get("api_key"):
        raise ValueError(f"{row.get('display_name') or provider} 未配置 API Key")

    selected_model = model or row["default_model"]
    payload = {
        "model": selected_model,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = response_format
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                _chat_endpoint(row["base_url"]),
                headers={
                    "Authorization": f"Bearer {row['api_key']}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        data = response.json() if response.content else {}
        if response.status_code >= 400:
            message = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else data.get("message")
            raise RuntimeError(message or f"HTTP {response.status_code}")
        usage_record = record_usage(provider, selected_model, operation, data.get("usage"), True)
        return {"provider": provider, "model": selected_model, "data": data, "usage_record": usage_record}
    except Exception as exc:
        record_usage(provider, selected_model, operation, None, False, str(exc))
        raise


def test_provider(provider: str) -> dict[str, Any]:
    row = provider_row(provider)
    if not row:
        raise ValueError(f"大模型供应商不存在: {provider}")
    if not row.get("api_key"):
        message = "未配置 API Key，不能测试连接"
        _update_test_status(provider, "missing_key", message)
        return {"success": False, "status": "missing_key", "message": message}
    if not row.get("enabled"):
        message = "供应商未启用，不能测试连接"
        _update_test_status(provider, "disabled", message)
        return {"success": False, "status": "disabled", "message": message}

    try:
        result = chat_completion(
            provider,
            [
                {"role": "system", "content": "你是连接测试助手，只返回 OK。"},
                {"role": "user", "content": "请回复 OK"},
            ],
            operation="connection_test",
            temperature=0,
            timeout=30,
        )
        content = (
            result["data"].get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        message = f"连接成功，返回长度 {len(content or '')}"
        _update_test_status(provider, "success", message)
        return {
            "success": True,
            "status": "success",
            "message": message,
            "usage": result["usage_record"],
        }
    except Exception as exc:
        message = str(exc)[:500]
        _update_test_status(provider, "failed", message)
        return {"success": False, "status": "failed", "message": message}


def _update_test_status(provider: str, status: str, message: str) -> None:
    with get_db() as conn:
        conn.execute(
            """UPDATE llm_providers
               SET last_test_status = ?, last_test_message = ?, last_tested_at = ?, updated_at = ?
               WHERE provider = ?""",
            (status, message[:500], local_now(), local_now(), provider),
        )


def usage_summary() -> dict[str, Any]:
    today = datetime.now().strftime("%Y-%m-%d")
    month = datetime.now().strftime("%Y-%m")
    with get_db() as conn:
        total = conn.execute(
            """SELECT COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens,
                      COALESCE(SUM(cost), 0) AS cost
               FROM llm_usage"""
        ).fetchone()
        today_row = conn.execute(
            """SELECT COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens,
                      COALESCE(SUM(cost), 0) AS cost
               FROM llm_usage WHERE substr(created_at, 1, 10) = ?""",
            (today,),
        ).fetchone()
        month_row = conn.execute(
            """SELECT COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens,
                      COALESCE(SUM(cost), 0) AS cost
               FROM llm_usage WHERE substr(created_at, 1, 7) = ?""",
            (month,),
        ).fetchone()
        by_provider = conn.execute(
            """SELECT provider, COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens,
                      COALESCE(SUM(cost), 0) AS cost,
                      SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_calls
               FROM llm_usage GROUP BY provider ORDER BY calls DESC"""
        ).fetchall()
    return {
        "total": dict(total),
        "today": dict(today_row),
        "month": dict(month_row),
        "by_provider": [dict(row) for row in by_provider],
    }


def active_provider() -> dict[str, Any]:
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM llm_providers
               WHERE enabled = 1 AND api_key IS NOT NULL AND api_key != ''
               ORDER BY priority ASC, id ASC LIMIT 1"""
        ).fetchone()
    if not row:
        raise ValueError("未接入可用大模型：请先在系统设置启用 DeepSeek/MiniMax/千问/Kimi 并填写 API Key")
    return dict(row)


def chat_with_active_provider(
    messages: list[dict[str, str]],
    operation: str,
    temperature: float = 0.3,
    timeout: int = 60,
    response_format: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    row = active_provider()
    return chat_completion(
        row["provider"],
        messages,
        operation=operation,
        model=row["default_model"],
        temperature=temperature,
        timeout=timeout,
        response_format=response_format,
    )
