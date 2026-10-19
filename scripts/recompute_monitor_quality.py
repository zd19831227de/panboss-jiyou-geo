#!/usr/bin/env python3
"""Recompute monitor run business quality from stored answers and sources."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "geo_data.db"
sys.path.insert(0, str(ROOT / "backend"))

from app.services.monitor.quality_gate import summarize_monitor_quality


def dict_rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def recompute(run_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    try:
        run_rows = dict_rows(conn, "SELECT * FROM monitor_runs WHERE id = ?", (run_id,))
        task_id = run_rows[0].get("task_id") if run_rows else None
        answers = dict_rows(conn, "SELECT * FROM monitor_answers WHERE run_id = ? ORDER BY id", (run_id,))
        answer_ids = [item["id"] for item in answers]
        sources_by_answer: dict[int, list[dict]] = {}
        if answer_ids:
            placeholders = ",".join("?" for _ in answer_ids)
            for source in dict_rows(
                conn,
                f"SELECT * FROM monitor_sources WHERE answer_id IN ({placeholders}) ORDER BY answer_id, source_order",
                answer_ids,
            ):
                sources_by_answer.setdefault(source["answer_id"], []).append(source)
        for answer in answers:
            answer["sources"] = sources_by_answer.get(answer["id"], [])
            answer["is_cited"] = bool(answer.get("brand_mentioned"))
        quality = summarize_monitor_quality(answers)
        conn.execute(
            """UPDATE monitor_runs
               SET business_status = ?, business_failure_reason = ?, effective_source_count = ?,
                   brand_mention_count = ?, brand_mention_rate = ?, error_msg = ?
               WHERE id = ?""",
            (
                quality["business_status"],
                quality["business_failure_reason"],
                quality["effective_source_count"],
                quality["brand_mention_count"],
                quality["brand_mention_rate"],
                quality["business_failure_reason"] or None,
                run_id,
            ),
        )
        if task_id:
            task_rows = dict_rows(conn, "SELECT result FROM task_queue WHERE id = ?", (task_id,))
            result_payload = {}
            if task_rows and task_rows[0].get("result"):
                try:
                    result_payload = json.loads(task_rows[0]["result"])
                except Exception:
                    result_payload = {}
            result_payload.update({
                "business_status": quality["business_status"],
                "business_failure_reason": quality["business_failure_reason"],
                "effective_source_count": quality["effective_source_count"],
                "brand_mention_count": quality["brand_mention_count"],
                "brand_mention_rate": quality["brand_mention_rate"],
            })
            progress_msg = "完成" if quality["business_status"] == "passed" else f"业务失败: {quality['business_failure_reason'] or '质量门禁未通过'}"
            conn.execute(
                """UPDATE task_queue
                   SET progress_msg = ?, result = ?
                   WHERE id = ?""",
                (progress_msg[:240], json.dumps(result_payload, ensure_ascii=False), task_id),
            )
        conn.commit()
        return quality
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()
    quality = recompute(args.run_id)
    print(quality)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
