#!/usr/bin/env python3
"""Clear source_verified for source URLs that fail live verification now."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend" / "geo_data.db"
sys.path.insert(0, str(ROOT / "backend"))

from app.services.evidence import verify_source_url  # noqa: E402


def main() -> int:
    changed_sources = 0
    downgraded_results = 0
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        sources = conn.execute(
            "SELECT id, answer_id, url FROM monitor_sources WHERE source_verified = 1"
        ).fetchall()
        bad_answer_ids = set()
        for source in sources:
            ok, reason = verify_source_url(source["url"], live_check=True, timeout=6)
            if ok:
                continue
            conn.execute(
                "UPDATE monitor_sources SET source_verified = 0, check_status = ? WHERE id = ?",
                (reason, source["id"]),
            )
            if source["answer_id"]:
                bad_answer_ids.add(source["answer_id"])
            changed_sources += 1
        for answer_id in bad_answer_ids:
            row = conn.execute(
                "SELECT monitor_result_id FROM monitor_answers WHERE id = ?",
                (answer_id,),
            ).fetchone()
            if not row:
                continue
            conn.execute(
                """UPDATE monitor_answers
                   SET source_verified = 0,
                       verification_status = CASE WHEN verification_status='verified_citation' THEN 'unverified_citation' ELSE verification_status END,
                       evidence_status = 'unverified',
                       evidence_failure_reason = COALESCE(NULLIF(evidence_failure_reason,''), '来源链接当前不可访问，已取消验真')
                   WHERE id = ?""",
                (answer_id,),
            )
            conn.execute(
                """UPDATE monitor_results
                   SET source_verified = 0,
                       has_verified_source = 0,
                       verification_status = CASE WHEN verification_status='verified_citation' THEN 'unverified_citation' ELSE verification_status END,
                       evidence_status = 'unverified',
                       evidence_failure_reason = COALESCE(NULLIF(evidence_failure_reason,''), '来源链接当前不可访问，已取消验真')
                   WHERE id = ?""",
                (row["monitor_result_id"],),
            )
            downgraded_results += 1
        conn.commit()
    print(json.dumps({"status": "passed", "changed_sources": changed_sources, "downgraded_results": downgraded_results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
