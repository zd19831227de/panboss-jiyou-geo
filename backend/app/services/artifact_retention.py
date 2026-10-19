"""Artifact retention utilities.

Only unreferenced files under the configured evidence roots can be deleted.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from app.core.config import settings
from app.database import DATABASE_PATH


ARTIFACT_ROOT = Path(settings.GEO_ARTIFACT_DIR).resolve()
SCREENSHOT_ROOT = Path(settings.SCREENSHOT_DIR).resolve()


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _add_path(refs: set[Path], value: object) -> None:
    if not value or not isinstance(value, str):
        return
    text = value.strip()
    if not text:
        return
    if text.startswith("/artifacts/"):
        text = text.replace("/artifacts", str(ARTIFACT_ROOT), 1)
    elif text.startswith("/screenshot/"):
        text = text.replace("/screenshot", str(SCREENSHOT_ROOT), 1)
    refs.add(Path(text).resolve())


def _add_media_json(refs: set[Path], value: object) -> None:
    if not value or not isinstance(value, str):
        return
    try:
        payload = json.loads(value)
    except Exception:
        return
    if not isinstance(payload, list):
        return
    for item in payload:
        if isinstance(item, dict):
            _add_path(refs, item.get("path"))


def collect_references() -> set[Path]:
    refs: set[Path] = set()
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        table_columns = {
            table: _existing_columns(conn, table)
            for table in [
                "monitor_results",
                "monitor_answers",
                "publishing_workflow_tests",
                "publishing_tasks",
                "geo_reports",
                "content_assets",
            ]
        }

        queries: list[tuple[str, bool]] = []
        if table_columns["monitor_results"]:
            queries.append(("SELECT screenshot_path, raw_artifact_path FROM monitor_results", False))
        if table_columns["monitor_answers"]:
            queries.append(("SELECT screenshot_path, raw_artifact_path FROM monitor_answers", False))
        if table_columns["publishing_workflow_tests"]:
            queries.append(("SELECT screenshot_path, raw_artifact_path FROM publishing_workflow_tests", False))
        if table_columns["publishing_tasks"]:
            queries.append(("SELECT screenshot_path, raw_artifact_path, summary_artifact_path FROM publishing_tasks", False))
        if table_columns["geo_reports"]:
            queries.append(("SELECT artifact_path FROM geo_reports", False))
        if "media_assets_json" in table_columns["content_assets"]:
            queries.append(("SELECT media_assets_json FROM content_assets", True))

        for sql, is_media_json in queries:
            for row in conn.execute(sql):
                for value in row:
                    if is_media_json:
                        _add_media_json(refs, value)
                    else:
                        _add_path(refs, value)
    finally:
        conn.close()
    return refs


def _is_under_root(path: Path, roots: Iterable[Path]) -> bool:
    """判断 path 是否位于任一 root 之下。

    使用 Path.is_relative_to / parent 比对，避免硬编码路径分隔符
    导致 Windows 下永远判 False（原实现用 ``/`` 拼接，与 Windows ``\\`` 不一致）。
    """
    try:
        for root in roots:
            if path == root:
                return True
            # Python 3.9+: is_relative_to 原生支持
            if path.is_relative_to(root):
                return True
        return False
    except AttributeError:
        # 兜底：使用 os.sep 避免硬编码 /
        import os
        text = str(path)
        return any(
            text == str(root) or text.startswith(str(root) + os.sep)
            for root in roots
        )


def inspect_artifacts(days: int = 30) -> dict:
    days = max(int(days or 1), 1)
    roots = [ARTIFACT_ROOT, SCREENSHOT_ROOT]
    refs = collect_references()
    cutoff = time.time() - days * 86400
    candidates: list[Path] = []
    total_files = 0
    total_bytes = 0
    referenced_existing = 0

    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not _is_under_root(resolved, roots):
                continue
            total_files += 1
            total_bytes += path.stat().st_size
            if resolved in refs:
                referenced_existing += 1
                continue
            if path.stat().st_mtime <= cutoff:
                candidates.append(path)

    candidate_bytes = sum(path.stat().st_size for path in candidates)
    return {
        "status": "dry_run",
        "days": days,
        "roots": [str(root) for root in roots],
        "total_files": total_files,
        "total_bytes": total_bytes,
        "referenced_files": len(refs),
        "referenced_existing_files": referenced_existing,
        "candidate_files": len(candidates),
        "candidate_bytes": candidate_bytes,
        "candidates": [str(path) for path in candidates[:50]],
        "safety_rule": "只清理证据目录下、超过保留天数、且数据库未引用的文件",
    }


def cleanup_artifacts(days: int = 30) -> dict:
    report = inspect_artifacts(days)
    deleted_files = 0
    deleted_bytes = 0
    for raw_path in report["candidates"]:
        path = Path(raw_path)
        try:
            size = path.stat().st_size
            path.unlink()
            deleted_files += 1
            deleted_bytes += size
        except FileNotFoundError:
            continue
        except Exception:
            continue
    report.update({
        "status": "deleted",
        "deleted_files": deleted_files,
        "deleted_bytes": deleted_bytes,
    })
    return report
