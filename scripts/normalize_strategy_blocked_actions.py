#!/usr/bin/env python3
"""Mark business-gate strategy actions as blocked instead of failed."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend" / "geo_data.db"
BLOCKED_REASONS = {
    "没有可采纳的 pending 关键词建议",
    "没有可监测关键词",
    "没有审核通过且可发布的内容",
    "没有可准备的发布任务",
    "没有已发布且带真实 URL 的发布任务可复测",
}


def main() -> int:
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, execution_result_json, failure_reason
               FROM geo_strategy_actions
               WHERE status = 'failed'"""
        ).fetchall()
        updated = 0
        for row in rows:
            reason = (row["failure_reason"] or "").strip()
            if reason not in BLOCKED_REASONS:
                continue
            payload = {}
            try:
                payload = json.loads(row["execution_result_json"] or "{}")
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                payload["blocked"] = True
                payload.setdefault("success", False)
                if "error" not in payload:
                    payload["error"] = reason
            conn.execute(
                """UPDATE geo_strategy_actions
                   SET status = 'blocked', execution_result_json = ?, updated_at = datetime('now', 'localtime')
                   WHERE id = ?""",
                (json.dumps(payload, ensure_ascii=False), row["id"]),
            )
            updated += 1
        conn.commit()
    print(json.dumps({"status": "passed", "updated": updated}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
