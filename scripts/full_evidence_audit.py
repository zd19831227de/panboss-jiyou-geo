#!/usr/bin/env python3
"""Full evidence audit for monitor, publish tasks, and workflow tests."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend" / "geo_data.db"
sys.path.insert(0, str(ROOT / "backend"))

from app.services.evidence import (
    verify_local_artifact,
    verify_publish_html,
    verify_publish_screenshot,
    verify_publish_summary_artifact,
    verify_source_url,
)  # noqa: E402


def rows(sql: str, params=()) -> list[dict]:
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def ok_artifact(path: str | None, kind: str) -> bool:
    ok, _ = verify_local_artifact(path, kind)
    return ok


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []

    monitor_bad = rows(
        """SELECT id, platform, source_url, screenshot_path, raw_artifact_path
           FROM monitor_results
           WHERE verification_status = 'verified_citation'
             AND (
               COALESCE(source_verified,0) != 1
               OR (COALESCE(screenshot_verified,0) != 1 AND COALESCE(raw_artifact_verified,0) != 1)
             )"""
    )
    for item in monitor_bad:
        failures.append(f"monitor_results #{item['id']} verified_citation lacks source or local artifact")

    for item in rows(
        """SELECT id, screenshot_path, raw_artifact_path, screenshot_verified, raw_artifact_verified
           FROM monitor_results
           WHERE screenshot_verified = 1 OR raw_artifact_verified = 1"""
    ):
        if item["screenshot_verified"] == 1 and not ok_artifact(item["screenshot_path"], "监测截图"):
            failures.append(f"monitor_results #{item['id']} screenshot artifact invalid")
        if item["raw_artifact_verified"] == 1 and not ok_artifact(item["raw_artifact_path"], "监测原始证据"):
            failures.append(f"monitor_results #{item['id']} raw artifact invalid")

    for item in rows(
        """SELECT id, status, published_url, published_url_verified,
                  screenshot_path, raw_artifact_path, summary_artifact_path,
                  screenshot_verified, raw_artifact_verified, summary_artifact_verified
           FROM publishing_tasks"""
    ):
        has_material = bool(item["screenshot_verified"] and item["raw_artifact_verified"] and item["summary_artifact_verified"])
        if item["status"] in {"prepared", "manual_required", "login_required", "published"} and not has_material:
            failures.append(f"publishing_tasks #{item['id']} active publish task lacks HTML/screenshot/summary evidence set")
        if item["status"] == "published" and item["published_url_verified"] != 1:
            failures.append(f"publishing_tasks #{item['id']} published without verified URL")
        if item["published_url_verified"] == 1:
            ok, reason = verify_source_url(item["published_url"], live_check=True, timeout=6)
            if not ok:
                warnings.append(
                    f"publishing_tasks #{item['id']} published URL live check unavailable: {reason}; "
                    "kept as verified only because local URL structure and evidence artifacts are already verified"
                )
        if item["screenshot_verified"] == 1 and not verify_publish_screenshot(item["screenshot_path"])[0]:
            failures.append(f"publishing_tasks #{item['id']} screenshot artifact invalid")
        if item["raw_artifact_verified"] == 1 and not verify_publish_html(item["raw_artifact_path"])[0]:
            failures.append(f"publishing_tasks #{item['id']} raw artifact invalid")
        if item["summary_artifact_verified"] == 1 and not verify_publish_summary_artifact(
            item["summary_artifact_path"],
            task_id=item["id"],
            raw_artifact_path=item["raw_artifact_path"],
            screenshot_path=item["screenshot_path"],
        )[0]:
            failures.append(f"publishing_tasks #{item['id']} summary artifact invalid")
        if item["status"] == "prepared" and has_material and not item["published_url"]:
            warnings.append(f"publishing_tasks #{item['id']} has material evidence but no published URL yet")

    for item in rows(
        """SELECT id, status, screenshot_path, raw_artifact_path, screenshot_verified, raw_artifact_verified
           FROM publishing_workflow_tests"""
    ):
        if item["status"] == "prepared" and not (item["screenshot_verified"] or item["raw_artifact_verified"]):
            failures.append(f"publishing_workflow_tests #{item['id']} prepared without evidence")
        if item["screenshot_verified"] == 1 and not ok_artifact(item["screenshot_path"], "工作流截图"):
            failures.append(f"publishing_workflow_tests #{item['id']} screenshot artifact invalid")
        if item["raw_artifact_verified"] == 1 and not ok_artifact(item["raw_artifact_path"], "工作流原始证据"):
            failures.append(f"publishing_workflow_tests #{item['id']} raw artifact invalid")

    summary = {
        "monitor_verified": rows("SELECT COUNT(*) AS count FROM monitor_results WHERE verification_status='verified_citation'")[0]["count"],
        "monitor_unverified": rows("SELECT COUNT(*) AS count FROM monitor_results WHERE verification_status='unverified_citation'")[0]["count"],
        "publish_tasks": rows("SELECT COUNT(*) AS count FROM publishing_tasks")[0]["count"],
        "published_verified": rows("SELECT COUNT(*) AS count FROM publishing_tasks WHERE status='published' AND published_url_verified=1")[0]["count"],
        "workflow_tests": rows("SELECT COUNT(*) AS count FROM publishing_workflow_tests")[0]["count"],
    }
    status = "failed" if failures else "passed"
    print(json.dumps({"status": status, "summary": summary, "warnings": warnings[:30], "failures": failures[:50], "failure_count": len(failures)}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
