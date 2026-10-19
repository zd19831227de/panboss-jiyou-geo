#!/usr/bin/env python3
"""Monitor business quality gate.

Fails when a monitor run finished technically but has no useful business
evidence such as verified citations, effective external sources, or meaningful
brand exposure.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "geo_data.db"
sys.path.insert(0, str(ROOT / "backend"))

import app.database  # noqa: F401


def rows(sql: str, params=()) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def evaluate_run(run_id: int) -> dict:
    run_rows = rows("SELECT * FROM monitor_runs WHERE id = ?", (run_id,))
    if not run_rows:
        raise SystemExit(f"monitor_run #{run_id} not found")
    run = run_rows[0]
    answer_rows = rows("SELECT * FROM monitor_answers WHERE run_id = ?", (run_id,))
    source_rows = rows(
        """SELECT ms.*
           FROM monitor_sources ms
           JOIN monitor_answers ma ON ma.id = ms.answer_id
           WHERE ma.run_id = ?""",
        (run_id,),
    )

    problems: list[str] = []
    if (run.get("verified_citation_count") or 0) <= 0:
        problems.append("0 verified_citation")
    if (run.get("effective_source_count") or 0) <= 0:
        problems.append("0 effective_source")
    if answer_rows and (run.get("brand_mention_count") or 0) / len(answer_rows) < 0.05:
        problems.append(f"low_brand_exposure {run.get('brand_mention_count') or 0}/{len(answer_rows)}")
    if not source_rows:
        problems.append("no source rows")

    expected_failed = bool(problems)
    actual_failed = run.get("business_status") == "failed_quality_gate"
    return {
        "run_id": run_id,
        "business_status": run.get("business_status"),
        "business_failure_reason": run.get("business_failure_reason"),
        "verified_citation_count": run.get("verified_citation_count") or 0,
        "effective_source_count": run.get("effective_source_count") or 0,
        "brand_mention_count": run.get("brand_mention_count") or 0,
        "total_answers": len(answer_rows),
        "source_rows": len(source_rows),
        "problems": problems,
        "passed": actual_failed if expected_failed else run.get("business_status") == "passed",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()
    result = evaluate_run(args.run_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
