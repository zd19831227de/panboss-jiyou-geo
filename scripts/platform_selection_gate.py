#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate for project-local GEO knowledge resources and platform selection."""
from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


class GateFailure(Exception):
    pass


def check(condition, message: str):
    if not condition:
        raise GateFailure(message)


def main() -> int:
    geo_dir = ROOT / "geo"
    knowledge_dir = geo_dir / "knowledge_base"
    source_map_path = geo_dir / "source_maps" / "industry_source_map.seed.json"
    manifest_path = knowledge_dir / "manifest.json"

    check(geo_dir.is_dir(), "missing project geo resource directory")
    check(knowledge_dir.is_dir(), "missing project-local knowledge_base directory")
    check(source_map_path.is_file(), "missing industry source map seed")
    check(manifest_path.is_file(), "missing knowledge base manifest")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    check(manifest.get("packaging_required") is True, "knowledge base must be packaging_required")
    check(manifest.get("resource_scope") == "project_embedded", "knowledge base must be project_embedded")

    from app.services.publish_platform_selection import recommend_publish_platforms

    real_estate = recommend_publish_platforms({
        "name": "示例江景住宅项目",
        "industry": "房产",
        "products": "住宅,江景房,改善型住房",
        "target_audience": "本市买房用户,改善家庭",
    })
    executable = {item["platform"] for item in real_estate["recommended_publish_platforms"] if item["execution_level"] in {"draft_ready", "workflow_tested", "verified_loop"}}
    excluded = {item["platform"] for item in real_estate["excluded_publish_platforms"]}
    pending = {item["platform"] for item in real_estate["publishing_workflow_todos"]}
    sources = {item["platform"] for item in real_estate["source_platforms"]}

    check("ctrip_travel" not in executable, "real estate must not recommend ctrip_travel as executable")
    check("ctrip_travel" in excluded, "real estate must explain ctrip_travel exclusion")
    for platform in {"anjuke", "fangtianxia", "beike", "leju"}:
        check(platform in pending, f"{platform} must stay pending integration")
        check(platform in sources, f"{platform} must be visible as source platform")
        check(platform not in executable, f"{platform} must not become executable")

    hotel = recommend_publish_platforms({
        "name": "测试商务酒店",
        "industry": "酒店",
        "products": "商圈住宿,早餐,亲子房",
        "target_audience": "城市旅游用户,商务差旅",
    })
    hotel_recommended = {item["platform"] for item in hotel["recommended_publish_platforms"]}
    check("ctrip_travel" in hotel_recommended, "hotel/local life should keep ctrip_travel as candidate")

    from app.routers.operations import _ensure_multiplatform_strategy_actions

    strategy, actions = _ensure_multiplatform_strategy_actions(
        {},
        [],
        {
            "name": "示例江景住宅项目",
            "industry": "房产",
            "products": "住宅,江景房,改善型住房",
            "target_audience": "本市买房用户,改善家庭",
        },
        {"website": "tested", "ctrip_travel": "tested", "zhihu": "draft", "baijiahao": "draft"},
    )
    action_platforms = {
        (item.get("payload") or {}).get("target_platform") or (item.get("payload") or {}).get("platform")
        for item in actions
    }
    check("ctrip_travel" not in action_platforms, "strategy actions must not include ctrip_travel for real estate")
    check(strategy.get("recommended_publish_platforms"), "strategy must expose recommended_publish_platforms")
    check(strategy.get("excluded_publish_platforms"), "strategy must expose excluded_publish_platforms")

    print(json.dumps({
        "passed": True,
        "geo_dir": str(geo_dir),
        "real_estate_excluded": sorted(excluded),
        "real_estate_pending": sorted(pending),
        "hotel_recommended": sorted(hotel_recommended),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateFailure as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
