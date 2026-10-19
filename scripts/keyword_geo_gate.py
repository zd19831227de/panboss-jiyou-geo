#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Keyword GEO classification acceptance gate."""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "geo_data.db"
# 品牌词提示（逗号分隔）：按当前部署的实际品牌名配置，命中则要求分类为 brand_included
BRAND_NAME_HINTS = [h.strip() for h in os.environ.get("GEO_GATE_BRAND_HINTS", "").split(",") if h.strip()]
from _geo_ports import BASE_URL
from _geo_auth import auth_headers

REQUIRED_COLUMNS = {
    "generation_batch",
    "batch_label",
    "seed_text",
    "is_duplicate",
    "brand_mention_type",
    "geo_value_type",
}


class GateFailure(Exception):
    pass


def request(path: str):
    with urllib.request.urlopen(urllib.request.Request(BASE_URL + path, headers=auth_headers()), timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


def db_rows(sql: str, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def fail(message: str):
    raise GateFailure(message)


def check(condition, message: str):
    if not condition:
        fail(message)


def main() -> int:
    evidence = {"checks": []}
    health = request("/health")
    check(health.get("status") == "healthy", "backend is not healthy")
    evidence["checks"].append("backend healthy")

    columns = {row["name"] for row in db_rows("PRAGMA table_info(keyword_ideas)")}
    missing = sorted(REQUIRED_COLUMNS - columns)
    check(not missing, f"keyword_ideas missing columns: {missing}")
    evidence["checks"].append("keyword batch/classification columns exist")

    batch = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    if not batch:
        batches = request("/api/operations/keywords/idea-batches?limit=5")
        check(isinstance(batches, list) and batches, "no keyword idea batches found")
        batch = batches[0]["generation_batch"]
    check(batch, "latest batch has empty generation_batch")
    evidence["latest_batch"] = batch
    evidence["checks"].append("latest batch available")

    ideas = request(f"/api/operations/keywords/ideas?batch={urllib.parse.quote(batch)}&limit=100")
    check(isinstance(ideas, list) and ideas, "latest batch has no ideas")
    for item in ideas:
        check(item.get("generation_batch") == batch, f"idea #{item.get('id')} has wrong batch")
        check(item.get("seed_text") is not None, f"idea #{item.get('id')} missing seed_text")
        check(item.get("brand_mention_type") in {"generic_demand", "brand_included"}, f"idea #{item.get('id')} invalid brand_mention_type")
        check(item.get("geo_value_type") in {"泛需求推荐词", "场景推荐词", "品牌认知词", "售前问答词"}, f"idea #{item.get('id')} invalid geo_value_type")
        keyword = item.get("keyword") or ""
        if any(hint in keyword for hint in BRAND_NAME_HINTS):
            check(item.get("brand_mention_type") == "brand_included", f"brand keyword misclassified: {keyword}")
        if any(term in keyword for term in ["早餐", "入住", "退房", "会议设施", "怎么样"]):
            check(item.get("geo_value_type") == "售前问答词", f"sales FAQ misclassified: {keyword}")
    evidence["idea_count"] = len(ideas)
    generic_count = sum(1 for item in ideas if item.get("brand_mention_type") == "generic_demand")
    faq_count = sum(1 for item in ideas if item.get("geo_value_type") == "售前问答词")
    min_generic = max(1, math.ceil(len(ideas) * 0.4))
    max_faq = max(1, math.floor(len(ideas) * 0.25))
    check(generic_count >= min_generic, f"generic demand ratio too low: {generic_count}/{len(ideas)}, need >= {min_generic}")
    check(faq_count <= max_faq, f"sales FAQ ratio too high: {faq_count}/{len(ideas)}, need <= {max_faq}")
    evidence["generic_demand_count"] = generic_count
    evidence["sales_faq_count"] = faq_count
    evidence["checks"].append("latest batch ideas classified")

    brand_items = request(
        f"/api/operations/keywords/ideas?batch={urllib.parse.quote(batch)}&brand_mention_type=brand_included&limit=100"
    )
    check(all(item.get("brand_mention_type") == "brand_included" for item in brand_items), "brand filter returned generic item")
    evidence["brand_included_count"] = len(brand_items)
    evidence["checks"].append("brand mention filter works")

    faq_param = urllib.parse.quote("售前问答词")
    faq_items = request(
        f"/api/operations/keywords/ideas?batch={urllib.parse.quote(batch)}&geo_value_type={faq_param}&limit=100"
    )
    check(all(item.get("geo_value_type") == "售前问答词" for item in faq_items), "FAQ filter returned non-FAQ item")
    evidence["sales_faq_filter_count"] = len(faq_items)
    evidence["checks"].append("GEO value filter works")

    db_bad = db_rows(
        """SELECT id, keyword, brand_mention_type, geo_value_type
           FROM keyword_ideas
           WHERE generation_batch = ?
             AND (
               brand_mention_type NOT IN ('generic_demand','brand_included')
               OR geo_value_type NOT IN ('泛需求推荐词','场景推荐词','品牌认知词','售前问答词')
               OR brand_mention_type IS NULL
               OR geo_value_type IS NULL
             )""",
        (batch,),
    )
    check(not db_bad, f"DB has invalid classification rows: {db_bad[:3]}")
    evidence["checks"].append("DB classification persisted")

    evidence["status"] = "passed"
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateFailure as exc:
        print(json.dumps({"status": "failed", "failure": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
