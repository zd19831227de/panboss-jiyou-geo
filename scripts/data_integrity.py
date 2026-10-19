#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check and optionally clean orphaned production data."""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "geo_data.db"


CHECKS = {
    "keywords": "SELECT COUNT(*) AS count FROM keywords k LEFT JOIN brands b ON b.id = k.brand_id WHERE b.id IS NULL",
    "keyword_ideas": "SELECT COUNT(*) AS count FROM keyword_ideas t LEFT JOIN brands b ON b.id = t.brand_id WHERE b.id IS NULL",
    "content_assets": "SELECT COUNT(*) AS count FROM content_assets t LEFT JOIN brands b ON b.id = t.brand_id WHERE b.id IS NULL",
    "publishing_tasks": "SELECT COUNT(*) AS count FROM publishing_tasks t LEFT JOIN brands b ON b.id = t.brand_id WHERE b.id IS NULL",
    "monitor_results": "SELECT COUNT(*) AS count FROM monitor_results t LEFT JOIN brands b ON b.id = t.brand_id WHERE b.id IS NULL",
    "monitor_answers": "SELECT COUNT(*) AS count FROM monitor_answers t LEFT JOIN brands b ON b.id = t.brand_id WHERE b.id IS NULL",
    "monitor_runs": "SELECT COUNT(*) AS count FROM monitor_runs t LEFT JOIN brands b ON b.id = t.brand_id WHERE b.id IS NULL",
    "geo_reports": "SELECT COUNT(*) AS count FROM geo_reports t LEFT JOIN brands b ON b.id = t.brand_id WHERE b.id IS NULL",
    "monitor_sources": "SELECT COUNT(*) AS count FROM monitor_sources s LEFT JOIN monitor_answers a ON a.id = s.answer_id WHERE a.id IS NULL",
    "active_publish_without_artifact": "SELECT COUNT(*) AS count FROM publishing_tasks WHERE status IN ('prepared','published') AND (COALESCE(raw_artifact_verified,0) != 1 OR COALESCE(screenshot_verified,0) != 1 OR COALESCE(summary_artifact_verified,0) != 1)",
    "published_without_verified_url": "SELECT COUNT(*) AS count FROM publishing_tasks WHERE status = 'published' AND COALESCE(published_url_verified,0) != 1",
    "workflow_test_bad_artifact": "SELECT COUNT(*) AS count FROM publishing_workflow_tests WHERE (screenshot_verified = 1 AND (screenshot_path IS NULL OR screenshot_path = '')) OR (raw_artifact_verified = 1 AND (raw_artifact_path IS NULL OR raw_artifact_path = ''))",
    "workflow_test_missing_evidence": "SELECT COUNT(*) AS count FROM publishing_workflow_tests WHERE status = 'prepared' AND COALESCE(screenshot_verified,0) != 1 AND COALESCE(raw_artifact_verified,0) != 1",
    "active_content_has_template_terms": "SELECT COUNT(*) AS count FROM content_assets WHERE status != 'rejected' AND (title LIKE '%GEO内容稿%' OR body LIKE '%GEO内容稿%' OR body LIKE '%用于提升%' OR body LIKE '%生成式 AI 回答中的可见性%' OR body LIKE '%品牌归因%' OR body LIKE '%待补充%' OR body LIKE '%模板生成%')",
    "active_content_has_body_url": "SELECT COUNT(*) AS count FROM content_assets WHERE status != 'rejected' AND (body LIKE '%http://%' OR body LIKE '%https://%' OR body LIKE '%www.%')",
    "active_content_missing_source": "SELECT COUNT(*) AS count FROM content_assets WHERE status != 'rejected' AND generation_notes NOT LIKE '%evidence_sources=%' AND generation_notes NOT LIKE '%来源%' AND generation_notes NOT LIKE '%链接%' AND generation_notes NOT LIKE '%owned_sites%' AND generation_notes NOT LIKE '%可核验%' AND generation_notes NOT LIKE '%核实%'",
}


def backup_db() -> Path:
    backup = DB_PATH.with_name(f"geo_data.before_integrity_cleanup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
    shutil.copy2(DB_PATH, backup)
    return backup


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {name: conn.execute(sql).fetchone()[0] for name, sql in CHECKS.items()}


def cleanup(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM monitor_sources WHERE answer_id NOT IN (SELECT id FROM monitor_answers)")
    for table in [
        "keywords",
        "keyword_ideas",
        "content_assets",
        "publishing_tasks",
        "monitor_results",
        "monitor_answers",
        "monitor_runs",
        "geo_reports",
        "monitor_configs",
        "alerts",
        "competitors",
    ]:
        conn.execute(f"DELETE FROM {table} WHERE brand_id NOT IN (SELECT id FROM brands)")
    conn.execute("""
        UPDATE publishing_tasks
        SET status = 'failed',
            failure_reason = COALESCE(NULLIF(failure_reason, ''), '系统门禁：发布任务缺少已验真的HTML/截图证据'),
            evidence_status = 'unverified',
            updated_at = datetime('now','localtime')
        WHERE status IN ('prepared','published')
          AND (
            COALESCE(raw_artifact_verified,0) != 1 OR
            COALESCE(screenshot_verified,0) != 1 OR
            COALESCE(summary_artifact_verified,0) != 1
          )
    """)
    conn.execute("""
        UPDATE publishing_tasks
        SET status = 'failed',
            failure_reason = COALESCE(NULLIF(failure_reason, ''), '系统门禁：已发布任务缺少已验真的真实发布URL'),
            evidence_status = 'unverified',
            updated_at = datetime('now','localtime')
        WHERE status = 'published' AND COALESCE(published_url_verified,0) != 1
    """)
    conn.execute("""
        UPDATE publishing_workflow_tests
        SET status = 'failed',
            failure_reason = COALESCE(NULLIF(failure_reason, ''), '系统门禁：发布工作流测试缺少已验真的HTML/截图证据'),
            evidence_status = 'unverified'
        WHERE status = 'prepared'
          AND COALESCE(screenshot_verified,0) != 1
          AND COALESCE(raw_artifact_verified,0) != 1
    """)
    conn.execute("""
        UPDATE content_assets
        SET status = 'rejected',
            generation_notes = COALESCE(NULLIF(generation_notes, ''), '系统门禁：内容含模板词或缺少可核验来源，已拒绝'),
            updated_at = datetime('now','localtime')
        WHERE status != 'rejected'
          AND (
            title LIKE '%GEO内容稿%' OR body LIKE '%GEO内容稿%' OR body LIKE '%用于提升%' OR
            body LIKE '%生成式 AI 回答中的可见性%' OR body LIKE '%品牌归因%' OR
            body LIKE '%待补充%' OR body LIKE '%模板生成%' OR
            body LIKE '%http://%' OR body LIKE '%https://%' OR body LIKE '%www.%' OR
            (generation_notes NOT LIKE '%evidence_sources=%' AND generation_notes NOT LIKE '%来源%' AND generation_notes NOT LIKE '%链接%' AND generation_notes NOT LIKE '%owned_sites%' AND generation_notes NOT LIKE '%可核验%' AND generation_notes NOT LIKE '%核实%')
          )
    """)
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    try:
        before = counts(conn)
        backup = None
        if args.cleanup and any(before.values()):
            backup = backup_db()
            cleanup(conn)
        after = counts(conn)
    finally:
        conn.close()

    print({
        "status": "passed" if not any(after.values()) else "failed",
        "before": before,
        "after": after,
        "backup": str(backup) if backup else None,
    })
    return 0 if not any(after.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
