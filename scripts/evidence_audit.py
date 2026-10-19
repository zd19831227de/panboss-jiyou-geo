#!/usr/bin/env python3
"""Audit and backfill real evidence flags for monitor data.

This script verifies source URLs and local artifacts. It never invents sources
or screenshots; rows that fail verification are downgraded.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "geo_data.db"
sys.path.insert(0, str(ROOT / "backend"))

from app.services.evidence import validate_monitor_evidence, verify_source_url  # noqa: E402


def row_dict(row):
    return dict(zip(row.keys(), row))


def best_verified_source(cur: sqlite3.Cursor, answer_id: int | None) -> sqlite3.Row | None:
    if not answer_id:
        return None
    return cur.execute(
        """SELECT * FROM monitor_sources
           WHERE answer_id = ? AND source_verified = 1
           ORDER BY source_order ASC, id ASC
           LIMIT 1""",
        (answer_id,),
    ).fetchone()


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    url_checks: dict[str, tuple[bool, str]] = {}
    checked_results = 0
    verified_results = 0
    downgraded_results = 0
    checked_sources = 0
    verified_sources = 0
    try:
        cur = conn.cursor()

        for source in cur.execute("SELECT * FROM monitor_sources ORDER BY id DESC").fetchall():
            source = row_dict(source)
            url = source.get("url") or ""
            if url not in url_checks:
                url_checks[url] = verify_source_url(url, live_check=True, timeout=4)
            ok, reason = url_checks[url]
            cur.execute(
                "UPDATE monitor_sources SET source_verified = ?, check_status = ?, checked_at = ? WHERE id = ?",
                (1 if ok else 0, reason or "verified", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), source["id"]),
            )
            checked_sources += 1
            verified_sources += 1 if ok else 0

        rows = cur.execute(
            """SELECT mr.*, ma.brand_mentioned, ma.id AS answer_id
               FROM monitor_results mr
               LEFT JOIN monitor_answers ma ON ma.monitor_result_id = mr.id
               ORDER BY mr.id DESC"""
        ).fetchall()
        for row in rows:
            item = row_dict(row)
            source = best_verified_source(cur, item.get("answer_id"))
            verified_source_url = source["url"] if source else item.get("source_url")
            verified_source_domain = source["domain"] if source and source["domain"] else (
                urlparse(verified_source_url).netloc if verified_source_url else item.get("source_domain")
            )
            evidence = validate_monitor_evidence(
                brand_mentioned=bool(item.get("brand_mentioned")),
                is_cited=bool(item.get("is_cited")),
                source_url=verified_source_url,
                screenshot_path=item.get("screenshot_path"),
                raw_artifact_path=item.get("raw_artifact_path"),
                live_url_check=False,
            )
            if verified_source_url:
                if verified_source_url not in url_checks:
                    url_checks[verified_source_url] = verify_source_url(verified_source_url, live_check=True, timeout=4)
                source_ok, source_reason = url_checks[verified_source_url]
                evidence["source_verified"] = 1 if source_ok else 0
                evidence["has_verified_source"] = 1 if bool(item.get("brand_mentioned") or item.get("is_cited")) and source_ok else 0
                if not source_ok and bool(item.get("brand_mentioned") or item.get("is_cited")):
                    evidence["verification_status"] = "unverified_citation"
                    evidence["evidence_status"] = "unverified"
                    evidence["evidence_failure_reason"] = source_reason
            if item.get("failure_reason"):
                verification_status = "failed"
            else:
                verification_status = evidence["verification_status"]
            before = item.get("verification_status")
            cur.execute(
                """UPDATE monitor_results
                   SET verification_status = ?, has_verified_source = ?,
                       source_url = ?, source_domain = ?,
                       source_verified = ?, screenshot_verified = ?,
                       raw_artifact_verified = ?, evidence_status = ?,
                       evidence_failure_reason = ?
                   WHERE id = ?""",
                (
                    verification_status,
                    evidence["has_verified_source"] if verification_status == "verified_citation" else 0,
                    verified_source_url if evidence["source_verified"] else item.get("source_url"),
                    verified_source_domain if evidence["source_verified"] else item.get("source_domain"),
                    evidence["source_verified"],
                    evidence["screenshot_verified"],
                    evidence["raw_artifact_verified"],
                    evidence["evidence_status"],
                    evidence["evidence_failure_reason"],
                    item["id"],
                ),
            )
            if item.get("answer_id"):
                cur.execute(
                    """UPDATE monitor_answers
                       SET verification_status = ?, source_verified = ?,
                           screenshot_verified = ?, raw_artifact_verified = ?,
                           evidence_status = ?, evidence_failure_reason = ?
                       WHERE id = ?""",
                    (
                        verification_status,
                        evidence["source_verified"],
                        evidence["screenshot_verified"],
                        evidence["raw_artifact_verified"],
                        evidence["evidence_status"],
                        evidence["evidence_failure_reason"],
                        item["answer_id"],
                    ),
                )
            checked_results += 1
            verified_results += 1 if verification_status == "verified_citation" else 0
            downgraded_results += 1 if before == "verified_citation" and verification_status != "verified_citation" else 0

        cur.execute(
            """UPDATE keywords
               SET ai_status = COALESCE((
                   SELECT CASE
                       WHEN EXISTS (
                           SELECT 1 FROM monitor_results mr
                           WHERE mr.keyword_id = keywords.id
                             AND mr.verification_status = 'verified_citation'
                             AND mr.source_verified = 1
                       ) THEN 'cited'
                       WHEN EXISTS (
                           SELECT 1 FROM monitor_results mr
                           WHERE mr.keyword_id = keywords.id
                             AND mr.verification_status = 'unverified_citation'
                       ) THEN 'unverified_citation'
                       WHEN EXISTS (
                           SELECT 1 FROM monitor_results mr
                           WHERE mr.keyword_id = keywords.id
                       ) THEN 'not_found'
                       ELSE keywords.ai_status
                   END
               ), ai_status)"""
        )
        conn.commit()
    finally:
        conn.close()

    print({
        "checked_results": checked_results,
        "verified_results": verified_results,
        "downgraded_results": downgraded_results,
        "checked_sources": checked_sources,
        "verified_sources": verified_sources,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
