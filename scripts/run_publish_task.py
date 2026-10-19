#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run one publish task in a separate process and persist the result."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# Windows 控制台默认 GBK：结果 JSON 带中文标题/emoji 时 print 直接
# UnicodeEncodeError 崩溃（父进程按 UTF-8 解码，两边必须一致）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from app.database import DATABASE_PATH, local_now  # noqa: E402
from app.routers.operations import _finalize_publish_run, _workflow_payload  # noqa: E402
from app.services.publishing_rpa import PublishRpaResult, prepare_publish_browser  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", type=int, required=True)
    args = parser.parse_args()

    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        task_row = conn.execute(
            """SELECT pt.*, ca.title, ca.body, ca.target_keyword, ca.media_assets_json
               FROM publishing_tasks pt
               LEFT JOIN content_assets ca ON ca.id = pt.content_id
               WHERE pt.id = ?""",
            (args.task_id,),
        ).fetchone()
        if not task_row:
            raise RuntimeError(f"Publish task not found: {args.task_id}")
        task = dict(task_row)
        content = dict(conn.execute("SELECT * FROM content_assets WHERE id = ?", (task["content_id"],)).fetchone())
        account = None
        if task.get("account_id"):
            row = conn.execute("SELECT * FROM publishing_accounts WHERE id = ?", (task["account_id"],)).fetchone()
            account = dict(row) if row else None
        workflow = conn.execute("SELECT * FROM publishing_workflows WHERE platform = ?", (task["platform"],)).fetchone()
        workflow = dict(workflow) if workflow else {}
        if workflow.get("workflow_json"):
            try:
                workflow["workflow"] = json.loads(workflow["workflow_json"])
            except Exception:
                workflow["workflow"] = {}
        if account:
            account["login_url"] = account.get("login_url") or workflow.get("login_url") or ""
            account["publish_url"] = account.get("publish_url") or workflow.get("publish_url") or ""
    finally:
        conn.close()

    try:
        result = prepare_publish_browser(task, content, account, _workflow_payload(workflow))
    except Exception as exc:
        result = PublishRpaResult(
            status="failed",
            failure_reason=f"发布 RPA 子进程异常: {str(exc)[:240]}",
            notes="系统未伪装发布成功，已保留任务供重试",
        )
    _finalize_publish_run(args.task_id, task, result)
    print(json.dumps({"id": args.task_id, **result.__dict__}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
