#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Post-reboot recovery check for GEO运营系统.

Read-only check: services, DB, AI sessions, publishing workflows, and ports.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "geo_data.db"
REPORT_PATH = ROOT / "RUNTIME_RECOVERY_STATUS.md"
from _geo_auth import auth_headers
from _geo_ports import (
    AI_PORTS,
    BASE_URL,
    FRONTEND_URL,
    PUBLISH_PORTS as PUBLISHING_PORTS,
)

AI_PLATFORMS = ("doubao", "qianwen", "yuanbao")


def http_json(path: str, timeout: float = 12.0) -> tuple[bool, object]:
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE_URL + path, headers=auth_headers()), timeout=timeout) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return False, str(exc)


def http_ok(url: str, timeout: float = 12.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 500, f"HTTP {resp.status}"
    except Exception as exc:
        return False, str(exc)


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.7):
            return True
    except OSError:
        return False


def table_count(cursor: sqlite3.Cursor, table: str) -> int | None:
    try:
        return int(cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except Exception:
        return None


def db_snapshot() -> dict:
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return {"ok": False, "path": str(DB_PATH), "error": "数据库不存在或为空"}

    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            counts = {
                table: table_count(cursor, table)
                for table in (
                    "brands",
                    "keywords",
                    "monitor_results",
                    "monitor_answers",
                    "monitor_sources",
                    "content_assets",
                    # 真实表名是 publishing_tasks；旧写法 publish_tasks 不存在，
                    # 导致该字段在报告里永远是 null（不可用信号）。
                    "publishing_tasks",
                    "publishing_workflows",
                )
            }
            workflows = [
                {
                    "platform": row[0],
                    "status": row[1],
                    "last_tested_at": row[2],
                    "notes": row[3],
                }
                for row in cursor.execute(
                    """SELECT platform, status, last_tested_at, notes
                       FROM publishing_workflows
                       ORDER BY platform"""
                ).fetchall()
            ]
        return {"ok": True, "path": str(DB_PATH), "counts": counts, "workflows": workflows}
    except Exception as exc:
        return {"ok": False, "path": str(DB_PATH), "error": str(exc)}


def required_workflow_ok(workflows: list[dict]) -> tuple[bool, list[str], list[str]]:
    required = {"website", "ctrip_travel", "baijiahao", "sohu", "zhihu", "xiaohongshu", "douyin", "toutiao"}
    disabled_ok = {"wechat", "xigua", "penguin"}
    problems: list[str] = []
    warnings: list[str] = []
    by_platform = {item.get("platform"): item for item in workflows}
    for platform in sorted(required):
        item = by_platform.get(platform) or {}
        status = item.get("status")
        if status not in ("tested", "active"):
            has_recovery_evidence = bool(item.get("last_tested_at")) and bool(item.get("notes"))
            if status == "template" and has_recovery_evidence:
                warnings.append(f"{platform} 工作流为 template：已有测试记录，但当前仍按人工确认/RPA辅助发布处理。")
            elif status == "pending_review" and has_recovery_evidence:
                warnings.append(f"{platform} 工作流处于平台审核中：需后续回捞公开 URL 并验真，当前不计自动发布闭环。")
            elif platform == "baijiahao":
                warnings.append(f"{platform} 已有真实发布闭环任务，但工作流测试仍为 {status}：需复测 RPA，不阻断重启恢复。")
            else:
                problems.append(f"{platform} 工作流缺少可恢复配置或测试记录: {status}")
    for platform in sorted(disabled_ok):
        status = (by_platform.get(platform) or {}).get("status")
        if status != "disabled":
            problems.append(f"{platform} 应保持禁用，当前: {status}")
    return not problems, problems, warnings


def render_report(result: dict) -> str:
    lines = [
        "# GEO运营系统 重启恢复状态",
        "",
        f"检查时间：{result['checked_at']}",
        f"总状态：{result['status']}",
        "",
        "## 当前结论",
        "",
        result["conclusion"],
        "",
        "## 重启后操作",
        "",
        "1. 进入项目目录并启动：`python start_services.py`",
        f"2. 打开系统：`{FRONTEND_URL}`，账号：`admin / admin123`",
        "3. 进入系统设置，豆包/千问/元宝如果不是“可用”，点“打开登录”并人工处理登录/验证码。",
        "4. 分别点三个平台的“运行自检”，必须产出回答、截图、原始证据后才算可用于监测。",
        "5. 运行：`python scripts/restart_recovery_check.py`，确认总状态为 `ready`。",
        "",
        "## 核心检查",
        "",
        f"- 后端：{result['services']['backend']}",
        f"- 前端：{result['services']['frontend']}",
        f"- 数据库：{result['database'].get('path')}，ok={result['database'].get('ok')}",
        f"- 发布工作流：{result['publishing']['status']}",
        "",
        "## AI 登录态",
        "",
    ]
    for platform, item in result["ai_sessions"].items():
        lines.append(
            f"- {platform}: status={item.get('status')}, port={item.get('port')}, "
            f"port_open={item.get('port_open')}, notes={item.get('notes')}"
        )
    lines.extend(["", "## 发布浏览器端口", ""])
    for platform, item in result["publishing_ports"].items():
        lines.append(f"- {platform}: port={item['port']}, {'占用' if item['open'] else '空闲'}")
    if result["actions"]:
        lines.extend(["", "## 必须处理", ""])
        lines.extend(f"- {item}" for item in result["actions"])
    if result.get("warnings"):
        lines.extend(["", "## 风险提示", ""])
        lines.extend(f"- {item}" for item in result["warnings"])
    lines.extend(["", "## 原始 JSON", "", "```json", json.dumps(result, ensure_ascii=False, indent=2), "```", ""])
    return "\n".join(lines)


def main() -> int:
    checked_at = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    actions: list[str] = []
    warnings: list[str] = []

    backend_ok, backend_data = http_json("/health")
    frontend_ok, frontend_status = http_ok(FRONTEND_URL)
    services = {
        "backend": "healthy" if backend_ok and isinstance(backend_data, dict) and backend_data.get("status") == "healthy" else f"failed: {backend_data}",
        "frontend": "reachable" if frontend_ok else f"failed: {frontend_status}",
    }
    if not services["backend"].startswith("healthy"):
        actions.append("后端未健康：先运行 `python start_services.py`，再复查。")
    if not services["frontend"].startswith("reachable"):
        actions.append("前端不可访问：先运行 `python start_services.py`，再复查。")

    database = db_snapshot()
    if not database.get("ok"):
        actions.append(f"数据库异常：{database.get('error')}")

    sessions_ok, sessions_data = http_json("/api/platform-sessions") if backend_ok else (False, {})
    if not sessions_ok:
        actions.append(f"平台登录态接口不可用：{sessions_data}")
    ai_sessions = {}
    for platform in AI_PLATFORMS:
        item = sessions_data.get(platform, {}) if isinstance(sessions_data, dict) else {}
        port = int(item.get("port") or AI_PORTS[platform])
        item["port"] = port
        item["port_open"] = port_open(port)
        ai_sessions[platform] = item
        profile_dir = Path(item.get("profile_dir") or "")
        if item.get("status") == "captcha_required":
            actions.append(f"{platform} 需要验证码/安全验证：到系统设置打开登录并人工处理。")
        elif not item.get("profile_dir") or not profile_dir.exists():
            actions.append(f"{platform} 固定登录态目录缺失：到系统设置打开登录并重新登录。")
        elif item.get("status") != "logged_in":
            warnings.append(f"{platform} 浏览器当前未保持运行；这是可恢复状态，使用前点系统设置里的打开登录/运行自检。")

    workflows = database.get("workflows") or []
    workflows_ok, workflow_problems, workflow_warnings = required_workflow_ok(workflows)
    warnings.extend(workflow_warnings)
    publishing = {
        "status": "ok" if workflows_ok and not workflow_warnings else ("warning" if workflows_ok else "failed"),
        "problems": workflow_problems,
        "warnings": workflow_warnings,
    }
    actions.extend(workflow_problems)

    publishing_ports = {
        platform: {"port": port, "open": port_open(port)}
        for platform, port in PUBLISHING_PORTS.items()
    }
    busy_publishing = [f"{platform}:{item['port']}" for platform, item in publishing_ports.items() if item["open"]]
    if busy_publishing:
        warnings.append(
            "发布浏览器端口已被占用："
            + ", ".join(busy_publishing)
            + "。若是GEO自己的发布浏览器，空闲时点各平台「关闭浏览器」以节省内存；"
            "若是其它程序占用，可在 backend/.env 改 GEO_PUBLISH_PORTS 换一段端口后重启。"
        )

    status = "ready" if not actions else "action_required"
    if services["backend"].startswith("failed") or services["frontend"].startswith("failed") or not database.get("ok"):
        status = "failed"
    conclusion = "系统已具备重启后恢复运行条件。" if status == "ready" else "系统未达到重启后 ready 状态，按“必须处理”项修复后复查。"

    result = {
        "checked_at": checked_at,
        "status": status,
        "conclusion": conclusion,
        "services": services,
        "database": database,
        "ai_sessions": ai_sessions,
        "publishing": publishing,
        "publishing_ports": publishing_ports,
        "actions": actions,
        "warnings": warnings,
    }
    REPORT_PATH.write_text(render_report(result), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n报告已写入: {REPORT_PATH}")
    return 0 if status == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
