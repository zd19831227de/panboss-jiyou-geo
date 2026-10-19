#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate for project-local GEO knowledge base resources."""
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
    from app.services.geo_knowledge_base import (
        append_knowledge_record,
        delete_knowledge_record,
        knowledge_base_summary,
        list_knowledge_records,
    )

    summary = knowledge_base_summary()
    check(summary["resource_scope"] == "project_embedded", "knowledge base must be project embedded")
    check(summary["packaging_required"] is True, "knowledge base must be required for packaging")
    for name in ["items", "evidence", "questions", "diagnostics"]:
        check(summary["resources"][name]["exists"], f"missing knowledge resource file: {name}")

    marker = "knowledge-gate-20260708"
    item = append_knowledge_record("items", {
        "brand_id": 0,
        "title": marker,
        "category": "brand_fact",
        "content": "知识库门禁事实：项目内 geo/knowledge_base 可写可读。",
        "status": "active",
    })
    evidence = append_knowledge_record("evidence", {
        "brand_id": 0,
        "title": marker,
        "source_url": "https://example.com/geo-knowledge-gate",
        "evidence_type": "gate_fixture",
        "status": "verified",
    })

    items = list_knowledge_records("items", {"title": marker})
    evidences = list_knowledge_records("evidence", {"title": marker})
    check(any(row["id"] == item["id"] for row in items), "written knowledge item was not readable")
    check(any(row["id"] == evidence["id"] for row in evidences), "written evidence source was not readable")

    try:
        append_knowledge_record("items", {"title": "bad", "api_token": "secret"})
    except ValueError:
        pass
    else:
        raise GateFailure("knowledge base must reject secret-like fields")

    from app.routers.operations import (
        KnowledgeRecordRequest,
        create_knowledge_base_record,
        get_knowledge_base_summary,
        list_knowledge_base_records,
    )

    route_summary = get_knowledge_base_summary()
    check(route_summary["root"] == "geo/knowledge_base", "knowledge route must use project-local geo directory")
    route_record = create_knowledge_base_record("questions", KnowledgeRecordRequest(
        brand_id=0,
        title=marker,
        question="项目内知识库 API 是否可写可读？",
        intent_type="gate_fixture",
        status="active",
    ))
    route_rows = list_knowledge_base_records("questions", brand_id=None, title=marker, status=None)
    check(any(row["id"] == route_record["id"] for row in route_rows), "knowledge route record was not readable")

    delete_knowledge_record("items", item["id"])
    delete_knowledge_record("evidence", evidence["id"])
    delete_knowledge_record("questions", route_record["id"])

    print(json.dumps({
        "passed": True,
        "summary": summary,
        "written": {"item_id": item["id"], "evidence_id": evidence["id"], "route_id": route_record["id"]},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateFailure as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
