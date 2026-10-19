#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Evidence acceptance gate.

Fails when any verified monitor/publishing evidence is only a string instead
of a checkable URL/file.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "geo_data.db"
sys.path.insert(0, str(ROOT / "backend"))

from app.services.evidence import (
    verify_local_artifact,
    verify_publish_html,
    verify_publish_screenshot,
    verify_publish_summary_artifact,
    verify_source_url,
)  # noqa: E402


class GateFailure(Exception):
    pass


def check(condition, message: str):
    if not condition:
        raise GateFailure(message)


def rows(sql: str, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def verify_urls_concurrently(items: list[dict], *, id_key: str, url_key: str, label: str, timeout: int = 15) -> list[str]:
    failures: list[str] = []
    if not items:
        return failures

    def verify_one(item: dict) -> tuple[dict, bool, str]:
        ok, reason = verify_source_url(item.get(url_key), live_check=True, timeout=timeout)
        return item, ok, reason

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(verify_one, item) for item in items]
        for future in as_completed(futures):
            item, ok, reason = future.result()
            if not ok:
                failures.append(f"{label} #{item[id_key]} source URL invalid: {reason}")
    return failures


def verify_url_shape(items: list[dict], *, id_key: str, url_key: str, label: str) -> list[str]:
    failures: list[str] = []
    for item in items:
        ok, reason = verify_source_url(item.get(url_key), live_check=False)
        if not ok:
            failures.append(f"{label} #{item[id_key]} source URL invalid: {reason}")
    return failures


def main() -> int:
    failures: list[str] = []

    verified_results = rows(
        """SELECT id, source_url, screenshot_path, raw_artifact_path,
                  source_verified, screenshot_verified, raw_artifact_verified
           FROM monitor_results
           WHERE verification_status = 'verified_citation'"""
    )
    for row in verified_results:
        if row.get("source_verified") != 1:
            failures.append(f"monitor_results #{row['id']} verified citation without source_verified")
        if row.get("screenshot_verified") != 1 and row.get("raw_artifact_verified") != 1:
            failures.append(f"monitor_results #{row['id']} verified citation without screenshot/raw artifact")
        if row.get("screenshot_verified") == 1:
            ok, reason = verify_local_artifact(row.get("screenshot_path"), "监测截图")
            if not ok:
                failures.append(f"monitor_results #{row['id']} screenshot invalid: {reason}")
        if row.get("raw_artifact_verified") == 1:
            ok, reason = verify_local_artifact(row.get("raw_artifact_path"), "监测原始证据")
            if not ok:
                failures.append(f"monitor_results #{row['id']} raw artifact invalid: {reason}")
    failures.extend(
        verify_url_shape(
            verified_results,
            id_key="id",
            url_key="source_url",
            label="monitor_results",
        )
    )

    verified_sources = rows(
        """SELECT id, answer_id, url
           FROM monitor_sources
           WHERE source_verified = 1"""
    )
    seen_urls: dict[str, int] = {}
    for row in verified_sources:
        url = row.get("url")
        if url in seen_urls:
            continue
        seen_urls[url] = row["id"]
    failures.extend(
        verify_url_shape(
            [{"id": item_id, "url": url} for url, item_id in seen_urls.items()],
            id_key="id",
            url_key="url",
            label="monitor_sources",
        )
    )

    verified_answers = rows(
        """SELECT id, screenshot_path, raw_artifact_path, screenshot_verified, raw_artifact_verified
           FROM monitor_answers
           WHERE screenshot_verified = 1 OR raw_artifact_verified = 1"""
    )
    for row in verified_answers:
        if row.get("screenshot_verified") == 1:
            ok, reason = verify_local_artifact(row.get("screenshot_path"), "回答截图")
            if not ok:
                failures.append(f"monitor_answers #{row['id']} screenshot invalid: {reason}")
        if row.get("raw_artifact_verified") == 1:
            ok, reason = verify_local_artifact(row.get("raw_artifact_path"), "回答原始证据")
            if not ok:
                failures.append(f"monitor_answers #{row['id']} raw artifact invalid: {reason}")

    published_tasks = rows(
        """SELECT id, status, published_url, published_url_verified, screenshot_path, raw_artifact_path,
                  summary_artifact_path, screenshot_verified, raw_artifact_verified,
                  summary_artifact_verified
           FROM publishing_tasks
           WHERE status = 'published' OR published_url_verified = 1
              OR screenshot_verified = 1 OR raw_artifact_verified = 1 OR summary_artifact_verified = 1"""
    )
    for row in published_tasks:
        if row.get("published_url_verified") == 1:
            failures.extend(
                verify_urls_concurrently(
                    [row],
                    id_key="id",
                    url_key="published_url",
                    label="publishing_tasks",
                )
            )
        if row.get("screenshot_verified") == 1:
            ok, reason = verify_publish_screenshot(row.get("screenshot_path"))
            if not ok:
                failures.append(f"publishing_tasks #{row['id']} screenshot invalid: {reason}")
        if row.get("raw_artifact_verified") == 1:
            ok, reason = verify_publish_html(row.get("raw_artifact_path"))
            if not ok:
                failures.append(f"publishing_tasks #{row['id']} raw artifact invalid: {reason}")
        if row.get("summary_artifact_verified") == 1:
            ok, reason = verify_publish_summary_artifact(
                row.get("summary_artifact_path"),
                task_id=row.get("id"),
                raw_artifact_path=row.get("raw_artifact_path"),
                screenshot_path=row.get("screenshot_path"),
            )
            if not ok:
                failures.append(f"publishing_tasks #{row['id']} summary artifact invalid: {reason}")
        if row.get("status") in {"prepared", "manual_required", "login_required", "published"}:
            if not (row.get("screenshot_verified") == 1 and row.get("raw_artifact_verified") == 1 and row.get("summary_artifact_verified") == 1):
                failures.append(f"publishing_tasks #{row['id']} active task lacks HTML/screenshot/summary evidence set")

    workflow_tests = rows(
        """SELECT id, screenshot_path, raw_artifact_path, screenshot_verified, raw_artifact_verified
           FROM publishing_workflow_tests
           WHERE screenshot_verified = 1 OR raw_artifact_verified = 1"""
    )
    for row in workflow_tests:
        if row.get("screenshot_verified") == 1:
            ok, reason = verify_local_artifact(row.get("screenshot_path"), "工作流测试截图")
            if not ok:
                failures.append(f"publishing_workflow_tests #{row['id']} screenshot invalid: {reason}")
        if row.get("raw_artifact_verified") == 1:
            ok, reason = verify_local_artifact(row.get("raw_artifact_path"), "工作流测试原始证据")
            if not ok:
                failures.append(f"publishing_workflow_tests #{row['id']} raw artifact invalid: {reason}")

    if failures:
        print(json.dumps({"status": "failed", "failures": failures[:50], "failure_count": len(failures)}, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps({
        "status": "passed",
        "verified_monitor_results": len(verified_results),
        "verified_source_urls": len(seen_urls),
        "verified_answer_artifacts": len(verified_answers),
        "checked_publishing_tasks": len(published_tasks),
        "checked_workflow_tests": len(workflow_tests),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
