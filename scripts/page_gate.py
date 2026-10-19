#!/usr/bin/env python3
"""Lightweight page/API acceptance gate.

This checks that production pages consume the real APIs and expose verified
evidence fields. It avoids synthetic data and fails on stale fake-proof fields.
"""
from __future__ import annotations

import json
import os
import urllib.request


from _geo_auth import auth_headers
from _geo_ports import AI_PORTS, BASE_URL



class GateFailure(Exception):
    pass


def request(path: str):
    with urllib.request.urlopen(urllib.request.Request(BASE_URL + path, headers=auth_headers()), timeout=20) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


def check(condition, message: str):
    if not condition:
        raise GateFailure(message)


def main() -> int:
    status = request("/api/system/status")
    check(status["backend"]["status"] == "running", "backend offline")
    check(status["worker"]["status"] == "running", "worker offline")

    sessions = request("/api/platform-sessions")
    for platform in ["doubao", "qianwen", "yuanbao"]:
        item = sessions.get(platform) or {}
        check(item.get("port") == AI_PORTS.get(platform), f"{platform} bad port")
        check(item.get("status") in ("logged_in", "browser_open", "captcha_required"), f"{platform} bad status")

    stats = request("/api/dashboard/all-stats")
    check(stats["verifiedCitationCount"] <= stats["citationCount"], "verified count exceeds citation count")
    check(stats["verifiedCitationCount"] <= stats["withSourceUrl"], "verified citations cannot exceed verified sources")
    check(stats["evidenceCompleteRate"] == stats["citationRate"], "evidence and citation rate mismatch")
    for key in ["contentAssetCount", "publishingTaskCount", "verifiedPublishedCount"]:
        check(key in stats, f"dashboard stats missing operation key: {key}")
    check(stats["verifiedPublishedCount"] <= stats["publishingTaskCount"], "verified published exceeds publishing tasks")

    results = request("/api/monitor/results?limit=20")
    check(results, "no monitor results")
    for row in results:
        if row.get("verification_status") == "verified_citation":
            check(row.get("source_verified") == 1, "verified citation without verified source")
            check(row.get("source_url", "").startswith(("http://", "https://")), "verified citation without URL")
        if row.get("screenshot_path"):
            check("screenshot_verified" in row, "screenshot field without verification flag")
        if row.get("raw_artifact_path"):
            check("raw_artifact_verified" in row, "raw artifact field without verification flag")

    command = request("/api/dashboard/command-center")
    overview = command.get("overview") or {}
    check(overview.get("content_assets") == stats["contentAssetCount"], "dashboard and command center content asset counts differ")
    check(overview.get("publishing_tasks") == stats["publishingTaskCount"], "dashboard and command center publishing task counts differ")
    latest = command.get("latest_results") or []
    for row in latest:
        if row.get("source_url"):
            check("source_verified" in row, "command center source missing verification flag")

    print(json.dumps({
        "status": "passed",
        "checks": ["system", "platform_sessions", "dashboard_stats", "monitor_results", "command_center"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        raise
