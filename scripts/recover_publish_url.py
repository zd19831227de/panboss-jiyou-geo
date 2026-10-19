#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Recover external publish URL in a separate process."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.database import DATABASE_PATH  # noqa: E402
from app.services.publishing_rpa import (  # noqa: E402
    recover_baijiahao_urls,
    recover_ctrip_travel_urls,
    recover_toutiao_urls,
    recover_sohu_urls,
    recover_zhihu_urls,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", type=int, required=True)
    args = parser.parse_args()

    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT pt.*, ca.title, pa.account_name
               FROM publishing_tasks pt
               LEFT JOIN content_assets ca ON ca.id = pt.content_id
               LEFT JOIN publishing_accounts pa ON pa.id = pt.account_id
               WHERE pt.id = ?""",
            (args.task_id,),
        ).fetchone()
        if not row:
            raise RuntimeError(f"Publish task not found: {args.task_id}")
        task = dict(row)
    finally:
        conn.close()

    platform = task.get("platform") or ""
    account_name = task.get("account_name") or "default"
    title = task.get("title") or ""
    if platform == "ctrip_travel":
        result = recover_ctrip_travel_urls(account_name, title)
    elif platform == "zhihu":
        result = recover_zhihu_urls(account_name, title)
    elif platform == "baijiahao":
        result = recover_baijiahao_urls(account_name, title)
    elif platform == "toutiao":
        result = recover_toutiao_urls(account_name, title)
    elif platform == "sohu":
        result = recover_sohu_urls(account_name, title)
    else:
        result = {"status": "failed", "failure_reason": f"Unsupported platform: {platform}", "candidates": []}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
