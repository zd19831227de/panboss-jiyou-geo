#!/usr/bin/env python3
"""Clear verified flags for local artifacts that no longer exist or are invalid."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend" / "geo_data.db"
sys.path.insert(0, str(ROOT / "backend"))

from app.services.evidence import verify_local_artifact  # noqa: E402


TABLES = [
    ("monitor_results", "id", "截图", "原始证据", None),
    ("monitor_answers", "id", "回答截图", "回答原始证据", None),
    ("publishing_tasks", "id", "发布截图", "发布原始证据", "发布摘要"),
    ("publishing_workflow_tests", "id", "工作流测试截图", "工作流测试原始证据", None),
]


def main() -> int:
    updates = {}
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        for table, pk, screenshot_kind, raw_kind, summary_kind in TABLES:
            changed = 0
            select_summary = ", summary_artifact_path, summary_artifact_verified" if summary_kind else ""
            rows = conn.execute(
                f"""SELECT {pk}, screenshot_path, screenshot_verified, raw_artifact_path, raw_artifact_verified{select_summary}
                    FROM {table}
                    WHERE COALESCE(screenshot_verified,0)=1 OR COALESCE(raw_artifact_verified,0)=1{(' OR COALESCE(summary_artifact_verified,0)=1' if summary_kind else '')}"""
            ).fetchall()
            for row in rows:
                sets = []
                params = []
                if row["screenshot_verified"] == 1:
                    ok, _ = verify_local_artifact(row["screenshot_path"], screenshot_kind)
                    if not ok:
                        sets.append("screenshot_verified = 0")
                if row["raw_artifact_verified"] == 1:
                    ok, _ = verify_local_artifact(row["raw_artifact_path"], raw_kind)
                    if not ok:
                        sets.append("raw_artifact_verified = 0")
                if summary_kind and row["summary_artifact_verified"] == 1:
                    ok, _ = verify_local_artifact(row["summary_artifact_path"], summary_kind)
                    if not ok:
                        sets.append("summary_artifact_verified = 0")
                if sets:
                    if table in {"monitor_results", "monitor_answers"}:
                        sets.append("verification_status = CASE WHEN verification_status = 'verified_citation' THEN 'unverified_citation' ELSE verification_status END")
                    sets.append("evidence_status = 'unverified'")
                    if table in {"monitor_results", "monitor_answers", "publishing_tasks"}:
                        sets.append("evidence_failure_reason = COALESCE(NULLIF(evidence_failure_reason, ''), '本地证据文件缺失或无效，已取消验真')")
                    params.append(row[pk])
                    conn.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE {pk} = ?", params)
                    changed += 1
            updates[table] = changed
        cursor = conn.execute(
            """UPDATE publishing_workflow_tests
               SET status = 'failed',
                   evidence_status = 'unverified',
                   failure_reason = COALESCE(NULLIF(failure_reason, ''), '本地证据文件缺失或无效，已取消工作流测试通过状态')
               WHERE status = 'prepared'
                 AND COALESCE(screenshot_verified,0) != 1
                 AND COALESCE(raw_artifact_verified,0) != 1"""
        )
        updates["publishing_workflow_tests_status"] = cursor.rowcount
        cursor = conn.execute(
            """UPDATE publishing_workflows
               SET status = 'template',
                   notes = COALESCE(NULLIF(notes, ''), '真实测试证据缺失，已取消已测试状态'),
                   updated_at = datetime('now','localtime')
               WHERE status IN ('tested','active')
                 AND NOT EXISTS (
                   SELECT 1 FROM publishing_workflow_tests t
                   WHERE t.platform = publishing_workflows.platform
                     AND t.status = 'prepared'
                     AND (COALESCE(t.screenshot_verified,0)=1 OR COALESCE(t.raw_artifact_verified,0)=1)
                 )"""
        )
        updates["publishing_workflows_status"] = cursor.rowcount
        conn.commit()
    print(json.dumps({"status": "passed", "updates": updates}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
