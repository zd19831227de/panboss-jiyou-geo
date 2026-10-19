#!/usr/bin/env python3
"""Downgrade monitor citations that lack checkable local evidence.

Rule: verified_citation requires a verified source URL and either a verified
screenshot or verified raw artifact. Source-only rows remain visible, but are
unverified_citation and must not count as delivered evidence.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend" / "geo_data.db"


def main() -> int:
    with sqlite3.connect(DB) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE monitor_results
               SET verification_status = 'unverified_citation',
                   has_verified_source = 0,
                   evidence_status = 'unverified',
                   evidence_failure_reason = TRIM(
                     COALESCE(NULLIF(evidence_failure_reason, ''), '') ||
                     CASE WHEN COALESCE(NULLIF(evidence_failure_reason, ''), '') = ''
                          THEN '' ELSE '；' END ||
                     '缺少截图或原始证据，来源链接不单独计入已验证引用'
                   )
               WHERE verification_status = 'verified_citation'
                 AND COALESCE(source_verified,0) = 1
                 AND COALESCE(screenshot_verified,0) != 1
                 AND COALESCE(raw_artifact_verified,0) != 1"""
        )
        result_rows = cursor.rowcount
        cursor.execute(
            """UPDATE monitor_answers
               SET verification_status = 'unverified_citation',
                   evidence_status = 'unverified',
                   evidence_failure_reason = TRIM(
                     COALESCE(NULLIF(evidence_failure_reason, ''), '') ||
                     CASE WHEN COALESCE(NULLIF(evidence_failure_reason, ''), '') = ''
                          THEN '' ELSE '；' END ||
                     '缺少截图或原始证据，来源链接不单独计入已验证引用'
                   )
               WHERE verification_status = 'verified_citation'
                 AND COALESCE(source_verified,0) = 1
                 AND COALESCE(screenshot_verified,0) != 1
                 AND COALESCE(raw_artifact_verified,0) != 1"""
        )
        answer_rows = cursor.rowcount
        conn.commit()
    print(json.dumps({"status": "passed", "monitor_results_updated": result_rows, "monitor_answers_updated": answer_rows}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
