#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate: AI strategy context must load project-local GEO knowledge base."""
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
    from app.services.geo_knowledge_base import append_knowledge_record, delete_knowledge_record
    from app.routers.operations import _attach_knowledge_to_strategy, _strategy_knowledge_context

    brand_id = 987654321
    marker = "strategy-knowledge-gate-20260708"
    records = []
    try:
        records.append(("items", append_knowledge_record("items", {
            "brand_id": brand_id,
            "title": marker,
            "category": "brand_fact",
            "content": "示例江景住宅项目位于城市江景改善居住带。",
            "status": "active",
        })["id"]))
        records.append(("items", append_knowledge_record("items", {
            "brand_id": brand_id,
            "title": f"{marker}-missing-evidence",
            "category": "brand_fact",
            "content": "这是缺少证据的策略事实。",
            "status": "active",
        })["id"]))
        records.append(("evidence", append_knowledge_record("evidence", {
            "brand_id": brand_id,
            "title": marker,
            "source_url": "https://example.com/strategy-knowledge-gate",
            "evidence_type": "official_site",
            "status": "verified",
        })["id"]))
        records.append(("questions", append_knowledge_record("questions", {
            "brand_id": brand_id,
            "title": marker,
            "question": "改善型住宅怎么选？",
            "intent_type": "comparison",
            "status": "active",
        })["id"]))
        records.append(("diagnostics", append_knowledge_record("diagnostics", {
            "brand_id": brand_id,
            "title": marker,
            "diagnosis_type": "knowledge_gap",
            "content": "近期未被引用时优先补官网事实页和地图信源。",
            "status": "active",
        })["id"]))

        context = _strategy_knowledge_context(brand_id)
        check(context["summary"]["root"] == "geo/knowledge_base", "strategy context must use project-local knowledge base")
        check(any(item["title"] == marker for item in context["items"]), "strategy context missing knowledge item")
        check(any(item["title"] == marker for item in context["evidence"]), "strategy context missing evidence source")
        check(any(item["title"] == marker for item in context["questions"]), "strategy context missing question matrix")
        check(any(item["title"] == marker for item in context["diagnostics"]), "strategy context missing citation diagnostic")
        check(any(marker in item for item in context["knowledge_references"]), "strategy context missing knowledge reference summary")
        check(any("missing-evidence" in item for item in context["evidence_gaps"]), "strategy context must expose evidence gaps")

        strategy = _attach_knowledge_to_strategy({"gap_analysis": []}, context)
        check(strategy.get("knowledge_references"), "strategy must expose knowledge_references")
        check(strategy.get("evidence_gaps"), "strategy must expose evidence_gaps")
        check(any("missing-evidence" in item for item in strategy["gap_analysis"]), "strategy gap_analysis must include evidence gaps")

        print(json.dumps({
            "passed": True,
            "knowledge_references": strategy["knowledge_references"],
            "evidence_gaps": strategy["evidence_gaps"],
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        for resource, record_id in records:
            delete_knowledge_record(resource, record_id)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateFailure as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
