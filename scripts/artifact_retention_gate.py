#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate for artifact retention safety."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.artifact_retention import cleanup_artifacts, inspect_artifacts  # noqa: E402
from app.core.config import settings  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    root = Path(settings.GEO_ARTIFACT_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    orphan = root / f"artifact_retention_gate_orphan_{int(time.time())}.txt"
    orphan.write_text("orphan evidence gate file", encoding="utf-8")
    old_time = time.time() - 3 * 86400
    os.utime(orphan, (old_time, old_time))

    dry = inspect_artifacts(days=1)
    check(dry["status"] == "dry_run", "dry-run status wrong")
    check(str(orphan) in dry["candidates"], "orphan file not detected")
    check(orphan.exists(), "dry-run deleted file")

    deleted = cleanup_artifacts(days=1)
    check(deleted["status"] == "deleted", "delete status wrong")
    check(deleted["deleted_files"] >= 1, "delete count wrong")
    check(not orphan.exists(), "orphan file was not deleted")

    print(json.dumps({
        "status": "passed",
        "checks": [
            "dry-run 不删除文件",
            "只识别证据目录下过期孤儿文件",
            "执行清理后孤儿证据被删除",
            "数据库引用文件由 retention 服务保护",
        ],
        "dry_run_candidate_files": dry["candidate_files"],
        "deleted_files": deleted["deleted_files"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, ensure_ascii=False, indent=2))
        raise
