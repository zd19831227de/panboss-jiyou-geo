"""Business quality gate for monitor runs.

This separates "the task finished running" from "the monitor result is useful".
"""
from __future__ import annotations

from urllib.parse import urlparse


AI_INTERNAL_DOMAINS = {
    "www.qianwen.com",
    "qianwen.com",
    "chat-side.qianwen.com",
    "chat2-api.qianwen.com",
    "zd.qianwen.com",
    "www.doubao.com",
    "doubao.com",
    "yuanbao.tencent.com",
}

ASSET_OR_BLOCKED_DOMAINS = {
    "lf3-short.ibytedapm.com",
    "lf6-short.ibytedapm.com",
}

KNOWN_LOW_VALUE_DOMAINS = {
    "rumt-zh.com",
}

SEARCH_REDIRECT_DOMAINS = {
    "www.baidu.com",
    "baidu.com",
    "m.baidu.com",
}

BLOCKED_STATUS_MARKERS = (
    "http 401",
    "http 403",
    "http 429",
    "需要权限",
    "限制访问",
    "不可访问",
    "访问失败",
)


def _domain(value: str | None) -> str:
    if not value:
        return ""
    raw = str(value).strip().lower()
    if "://" in raw:
        return urlparse(raw).netloc.lower()
    return raw


def _is_search_redirect(url: str | None, domain: str) -> bool:
    if domain not in SEARCH_REDIRECT_DOMAINS:
        return False
    parsed = urlparse(url or "")
    return parsed.path.startswith("/link") or parsed.path.startswith("/s")


def is_effective_source(source: dict) -> bool:
    """Return whether a source is usable business evidence."""
    url = source.get("url")
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False

    domain = _domain(source.get("domain") or parsed.netloc)
    if domain in AI_INTERNAL_DOMAINS:
        return False
    if domain in ASSET_OR_BLOCKED_DOMAINS or domain in KNOWN_LOW_VALUE_DOMAINS:
        return False
    if _is_search_redirect(url, domain):
        return False

    status = str(source.get("check_status") or "").lower()
    if any(marker in status for marker in BLOCKED_STATUS_MARKERS):
        return False
    return bool(source.get("source_verified", 0))


def summarize_monitor_quality(results: list[dict]) -> dict:
    total = len(results)
    failed = sum(1 for item in results if item.get("failure_reason") or item.get("verification_status") == "failed")
    verified = sum(1 for item in results if item.get("verification_status") == "verified_citation")
    unverified = sum(1 for item in results if item.get("verification_status") == "unverified_citation")
    brand_mentions = sum(1 for item in results if item.get("brand_mentioned") or item.get("is_cited"))

    effective_source_count = 0
    for item in results:
        if not (item.get("brand_mentioned") or item.get("is_cited") or item.get("verification_status") in {"verified_citation", "unverified_citation"}):
            continue
        sources = item.get("sources") or []
        if not sources and item.get("source_url"):
            sources = [{
                "url": item.get("source_url"),
                "domain": item.get("source_domain"),
                "source_verified": item.get("source_verified", 0),
                "check_status": item.get("check_status") or "verified",
            }]
        effective_source_count += sum(1 for source in sources if is_effective_source(source))

    brand_mention_rate = round((brand_mentions / total) if total else 0.0, 4)

    # 区分"监测全部失败"和"品牌未露出"：
    # 如果所有结果都有 failure_reason（监测本身没跑通），那是监测故障，不是品牌未露出。
    # 只有当至少有 1 条成功结果但品牌没被提到，才是真正的"品牌未露出"。
    success_count = total - failed

    failure_reasons: list[str] = []
    if total > 0 and failed == total:
        # 全部失败 = 监测本身没跑通（消息没发出、页面异常等），不是品牌未露出
        business_status = "failed"
        business_failure_reason = "监测全部失败（消息未发出或页面异常），无法判定品牌露出"
        return {
            "business_status": business_status,
            "business_failure_reason": business_failure_reason,
            "effective_source_count": effective_source_count,
            "brand_mention_count": brand_mentions,
            "brand_mention_rate": brand_mention_rate,
            "verified_citation_count": verified,
            "unverified_citation_count": unverified,
            "failed_count": failed,
            "total": total,
        }
    elif total > 0 and brand_mentions <= 0 and success_count > 0:
        failure_reasons.append("品牌未露出（AI 回答未提及目标品牌）")
    elif total >= 20 and brand_mention_rate < 0.05:
        failure_reasons.append(f"品牌露出率过低 {brand_mentions}/{total}")

    if failure_reasons:
        business_status = "failed_quality_gate"
    elif failed > 0:
        business_status = "completed_with_errors"
    elif verified > 0 and effective_source_count > 0:
        business_status = "passed"
    else:
        business_status = "brand_mentioned_unverified"

    if business_status == "brand_mentioned_unverified":
        note = f"品牌已露出 {brand_mentions}/{total} 项"
        if unverified > 0:
            note += f"；{unverified} 项为未验证引用"
        note += "；未捕获可验证来源链接"
        business_failure_reason = note
    else:
        business_failure_reason = "；".join(failure_reasons)

    return {
        "business_status": business_status,
        "business_failure_reason": business_failure_reason,
        "effective_source_count": effective_source_count,
        "brand_mention_count": brand_mentions,
        "brand_mention_rate": brand_mention_rate,
        "verified_citation_count": verified,
        "unverified_citation_count": unverified,
        "failed_count": failed,
        "total": total,
    }
