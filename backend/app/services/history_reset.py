"""测试环境监测历史重置（开发记录 #9）。

旧监测历史/任务记录/日志影响开发阶段的数据观察，本模块提供一次性重置能力：
  - 预览（preview_reset）：只统计要清什么，不动任何数据。
  - 执行（execute_reset）：
      1. 先把 geo_data.db 备份到 backend/db_archive/（唯一保留的备份）
      2. 清空监测/任务/告警/报告/发布工作流测试等历史表并 VACUUM
      3. 删除监测平台证据目录（保留 publishing/，发布任务仍引用）
      4. 截断运行日志
      5. 删除 backend/ 下散落的 geo_data.before_*.db 旧快照

保留：品牌、关键词、关键词创意、内容资产、发布任务/账号/工作流配置、
LLM 供应商与用量、用户账号——这些是配置与工作产物，不是日志历史。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

from app.core.runtime_paths import artifacts_dir, logs_dir, screenshots_dir
from app.database import DATABASE_PATH

# 历史类数据表（清空）
HISTORY_TABLES = [
    "monitor_sources",
    "monitor_answers",
    "monitor_results",
    "monitor_runs",
    "task_queue",
    "alerts",
    "geo_reports",
    "publishing_workflow_tests",
]

# artifacts/ 下保留的目录（发布任务证据仍被 publishing_tasks 引用）
KEEP_ARTIFACT_DIRS = {"publishing"}

BACKEND_DIR = DATABASE_PATH.parent
ARCHIVE_DIR = BACKEND_DIR / "db_archive"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    return bool(row and row[0])


def _dir_stats(path: Path) -> dict:
    files = bytes_total = 0
    if path.exists():
        for item in path.rglob("*"):
            if item.is_file():
                files += 1
                try:
                    bytes_total += item.stat().st_size
                except OSError:
                    pass
    return {"files": files, "bytes": bytes_total}


def _monitor_artifact_dirs() -> list[Path]:
    root = artifacts_dir()
    if not root.exists():
        return []
    return [child for child in sorted(root.iterdir()) if child.is_dir() and child.name not in KEEP_ARTIFACT_DIRS]


def _log_files() -> list[Path]:
    root = logs_dir()
    if not root.exists():
        return []
    return sorted(root.glob("*.log"))


def _stale_db_backups() -> list[Path]:
    return sorted(BACKEND_DIR.glob("geo_data.before_*.db"))


def preview_reset() -> dict:
    """统计将要清理的内容；不修改任何数据。"""
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        conn.row_factory = sqlite3.Row
        table_rows = {
            table: (conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
                    if _table_exists(conn, table) else None)
            for table in HISTORY_TABLES
        }
        active_tasks = 0
        if _table_exists(conn, "task_queue"):
            row = conn.execute(
                "SELECT COUNT(*) c FROM task_queue WHERE status IN ('pending','running')"
            ).fetchone()
            active_tasks = row["c"]
    finally:
        conn.close()

    artifact_dirs = []
    artifact_files = artifact_bytes = 0
    for child in _monitor_artifact_dirs():
        stats = _dir_stats(child)
        artifact_dirs.append({"dir": str(child), **stats})
        artifact_files += stats["files"]
        artifact_bytes += stats["bytes"]
    shot_stats = _dir_stats(screenshots_dir())
    logs = [{"file": str(p), "bytes": p.stat().st_size if p.exists() else 0} for p in _log_files()]
    stale = _stale_db_backups()
    stale_bytes = sum(p.stat().st_size for p in stale if p.exists())

    return {
        "status": "dry_run",
        "database_path": str(DATABASE_PATH),
        "database_bytes": DATABASE_PATH.stat().st_size if DATABASE_PATH.exists() else 0,
        "history_table_rows": table_rows,
        "active_tasks": active_tasks,
        "artifact_dirs": artifact_dirs,
        "artifact_files": artifact_files,
        "artifact_bytes": artifact_bytes,
        "screenshot_files": shot_stats["files"],
        "screenshot_bytes": shot_stats["bytes"],
        "log_files": logs,
        "stale_db_backups": [str(p) for p in stale],
        "stale_db_backup_bytes": stale_bytes,
        "keep_rule": (
            "保留：品牌/关键词/内容资产/发布任务/账号/工作流配置/LLM配置；"
            "保留 artifacts/publishing（发布任务证据仍被引用）"
        ),
    }


def _backup_database() -> str:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = ARCHIVE_DIR / f"geo_data_before_reset_{stamp}.db"
    src = sqlite3.connect(DATABASE_PATH)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return str(target)


def _rmtree(path: Path, expected_root: Path) -> tuple[int, list[str]]:
    """删除目录，返回 (删除文件数, 错误列表)。路径必须在 expected_root 内。"""
    errors = []
    removed = 0
    if not path.exists():
        return 0, []
    # 路径边界校验：防止符号链接或路径注入逃逸到预期目录之外
    try:
        resolved = path.resolve()
        root = expected_root.resolve()
        if not str(resolved).startswith(str(root) + os.sep) and resolved != root:
            return 0, [f"路径越界拒绝删除: {path} 不在 {expected_root} 内"]
    except Exception as exc:
        return 0, [f"路径解析失败: {exc}"]
    for item in path.rglob("*"):
        if item.is_file():
            removed += 1
    try:
        shutil.rmtree(path)
    except Exception as exc:
        errors.append(f"删除目录失败 {path}: {str(exc)[:120]}")
    return removed, errors


def execute_reset(backup: bool = True) -> dict:
    """执行重置。有 pending/running 任务时拒绝执行（避免和 worker 抢行）。"""
    preview = preview_reset()
    if preview["active_tasks"]:
        return {
            "status": "refused",
            "reason": f"仍有 {preview['active_tasks']} 个等待中/运行中任务，请先取消后再重置",
        }

    report: dict = {"status": "done"}

    # 1. 备份当前库（重置前唯一快照，旧 .before_* 快照随后删除）
    if backup:
        report["backup_path"] = _backup_database()

    # 2. 清历史表 + VACUUM 回收空间
    cleared_rows = {}
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        for table in HISTORY_TABLES:
            if not _table_exists(conn, table):
                continue
            cur = conn.execute(f"DELETE FROM {table}")
            cleared_rows[table] = cur.rowcount
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    report["cleared_table_rows"] = cleared_rows

    # 3. 监测证据目录 + 截图目录（路径边界限制在 artifacts/screenshots 根下）
    artifact_root = artifacts_dir()
    removed_files = 0
    file_errors: list[str] = []
    for child in _monitor_artifact_dirs():
        n, errs = _rmtree(child, artifact_root)
        removed_files += n
        file_errors.extend(errs)
    shot_root = screenshots_dir()
    removed_files += sum(1 for p in shot_root.rglob("*") if p.is_file())
    n, errs = _rmtree(shot_root, shot_root)
    file_errors.extend(errs)
    shot_root.mkdir(parents=True, exist_ok=True)
    report["removed_artifact_files"] = removed_files
    if file_errors:
        report["file_errors"] = file_errors

    # 4. 截断日志（保留文件句柄兼容）
    truncated_logs = []
    log_errors: list[str] = []
    for log in _log_files():
        try:
            log.write_text("", encoding="utf-8")
            truncated_logs.append(str(log))
        except OSError as exc:
            log_errors.append(f"截断日志失败 {log}: {str(exc)[:80]}")
    report["truncated_logs"] = truncated_logs
    if log_errors:
        report["log_errors"] = log_errors

    # 5. 删除旧库快照（backend/geo_data.before_*.db）
    removed_backups = []
    backup_errors: list[str] = []
    for stale in _stale_db_backups():
        try:
            stale.unlink()
            for suffix in ("-shm", "-wal"):
                sidecar = Path(str(stale) + suffix)
                if sidecar.exists():
                    sidecar.unlink()
            removed_backups.append(str(stale))
        except OSError as exc:
            backup_errors.append(f"删除旧快照失败 {stale}: {str(exc)[:80]}")
    report["removed_stale_db_backups"] = removed_backups
    if backup_errors:
        report["backup_errors"] = backup_errors
    report["database_bytes_after"] = DATABASE_PATH.stat().st_size if DATABASE_PATH.exists() else 0
    return report


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="监测历史重置（测试环境）")
    parser.add_argument("--execute", action="store_true", help="真正执行（默认只预览）")
    parser.add_argument("--no-backup", action="store_true", help="执行时不做重置前备份（不建议）")
    args = parser.parse_args()
    result = execute_reset(backup=not args.no_backup) if args.execute else preview_reset()
    print(json.dumps(result, ensure_ascii=False, indent=2))
