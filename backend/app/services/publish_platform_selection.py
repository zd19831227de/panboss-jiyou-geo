"""Project-local GEO source map and publish platform recommendation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
SOURCE_MAP_PATH = ROOT / "geo" / "source_maps" / "industry_source_map.seed.json"

PLATFORM_NAMES = {
    "website": "官网/落地页",
    "ctrip_travel": "携程内容中心/旅拍",
    "douyin": "抖音创作者中心",
    "toutiao": "今日头条号",
    "baijiahao": "百家号",
    "sohu": "搜狐号",
    "zhihu": "知乎",
    "xiaohongshu": "小红书",
    "anjuke": "安居客",
    "fangtianxia": "房天下",
    "beike": "贝壳",
    "leju": "乐居",
    "official_site": "官网",
    "map": "地图",
    "government_public_info": "政府公开信息",
    "meituan_dianping": "美团/大众点评",
    "trip": "Trip",
    "booking": "Booking",
    "agoda": "Agoda",
    "ctrip": "携程",
}

DEFAULT_CAPABILITIES = {
    "website": {"level": "verified_loop"},
    "ctrip_travel": {"level": "rpa_testing"},
    "douyin": {"level": "rpa_testing"},
    "xiaohongshu": {"level": "rpa_testing"},
    "baijiahao": {"level": "rpa_draft"},
    "zhihu": {"level": "rpa_draft"},
    "toutiao": {"level": "rpa_draft"},
    "sohu": {"level": "rpa_draft"},
}

REAL_ESTATE_TERMS = ("房产", "房地产", "楼盘", "住宅", "置业", "买房", "新房", "二手房", "公寓")
HOTEL_TERMS = ("酒店", "住宿", "民宿", "旅店", "本地生活", "旅游", "酒旅")


def load_source_map(path: Path = SOURCE_MAP_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"version": "missing", "industries": {}}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def infer_industry_key(brand: dict[str, Any]) -> str:
    text = " ".join(str(brand.get(key) or "") for key in ("industry", "products", "target_audience", "name"))
    if any(term in text for term in REAL_ESTATE_TERMS):
        return "real_estate"
    if any(term in text for term in HOTEL_TERMS):
        return "hotel_local_life"
    return "general"


def platform_name(platform: str) -> str:
    return PLATFORM_NAMES.get(platform, platform)


def _execution_level(platform: str, workflow_statuses: dict[str, str] | None) -> str:
    if platform not in DEFAULT_CAPABILITIES:
        return "candidate"
    if platform == "website":
        return "verified_loop"
    status = (workflow_statuses or {}).get(platform)
    if status in {"tested", "active"}:
        return "workflow_tested"
    level = DEFAULT_CAPABILITIES.get(platform, {}).get("level")
    if level in {"rpa_draft", "rpa_testing"}:
        return "draft_ready"
    return "candidate"


def recommend_publish_platforms(
    brand: dict[str, Any],
    *,
    workflow_statuses: dict[str, str] | None = None,
    source_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = source_map or load_source_map()
    industry_key = infer_industry_key(brand)
    industry = (data.get("industries") or {}).get(industry_key) or {}
    if not industry:
        industry = {
            "source_platforms": ["official_site", "map"],
            "distribution_candidates": ["website", "zhihu", "baijiahao", "toutiao", "sohu"],
            "pending_integrations": [],
            "excluded_executable_platforms": [],
        }

    excluded_set = set(industry.get("excluded_executable_platforms") or [])
    pending_set = set(industry.get("pending_integrations") or [])

    source_platforms = [
        {
            "platform": platform,
            "platform_name": platform_name(platform),
            "reason": "行业信源图谱命中，可作为 AI 引用和事实校验来源",
            "execution_level": "source_only",
        }
        for platform in industry.get("source_platforms") or []
    ]

    recommendations = []
    excluded = []
    todos = []

    for platform in industry.get("distribution_candidates") or []:
        reason = "行业内容分发候选平台"
        if platform in excluded_set:
            excluded.append({
                "platform": platform,
                "platform_name": platform_name(platform),
                "reason": "行业语境不匹配，不能作为可执行发布项",
            })
            continue
        execution_level = _execution_level(platform, workflow_statuses)
        recommendations.append({
            "platform": platform,
            "platform_name": platform_name(platform),
            "reason": reason,
            "execution_level": execution_level,
            "workflow_status": (workflow_statuses or {}).get(platform),
            "can_create_publish_task": execution_level in {"workflow_tested", "verified_loop"},
            "can_prepare_draft": execution_level in {"draft_ready", "workflow_tested", "verified_loop"} and platform != "website",
        })

    for platform in sorted(pending_set):
        todos.append({
            "platform": platform,
            "platform_name": platform_name(platform),
            "reason": "行业相关，但未完成账号、工作流测试、URL 回捞和验真，暂不可创建发布任务",
            "execution_level": "candidate",
        })

    for platform in sorted(excluded_set):
        if platform not in {item["platform"] for item in excluded}:
            excluded.append({
                "platform": platform,
                "platform_name": platform_name(platform),
                "reason": "行业信源图谱排除该平台作为可执行发布项",
            })

    return {
        "industry_key": industry_key,
        "source_map_version": data.get("version"),
        "source_platforms": source_platforms,
        "recommended_publish_platforms": recommendations,
        "excluded_publish_platforms": excluded,
        "publishing_workflow_todos": todos,
    }
