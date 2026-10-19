"""Platform session status and login browser controls."""
import json
import subprocess
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.database import get_db, local_now
from app.services.platform_adapters import (
    check_all_platform_sessions,
    check_platform_session,
    open_login_browser,
    run_platform_monitor,
)
from app.services.platform_adapters.sessions import deep_login_check

router = APIRouter(prefix="/api/platform-sessions", tags=["platform-sessions"])

# 平台级自检锁：防止同一平台被并发自检导致浏览器/CDP 资源冲突
_self_test_locks = {
    "doubao": threading.Lock(),
    "qianwen": threading.Lock(),
    "yuanbao": threading.Lock(),
}


def _friendly_failure(raw: str) -> str:
    """把底层报错转成可操作的友好提示。"""
    text = raw or ""
    if "10061" in text or "Failed to establish a new connection" in text or "Max retries exceeded" in text:
        return "监测浏览器未启动。请先到【系统设置】点击该平台【打开登录】，待浏览器打开后再自检。"
    if "未找到可输入的聊天框" in text:
        return "未找到聊天输入框，通常是未登录。请点击【打开登录】完成登录后再自检。"
    if "10054" in text or "connection reset" in text.lower():
        return "浏览器连接被重置，可能被意外关闭。请重新点击【打开登录】后再试。"
    return text[-300:] if text else "自检失败，请查看后端日志"


@router.get("")
def list_platform_sessions():
    return check_all_platform_sessions()


@router.get("/{platform}")
def get_platform_session(platform: str):
    result = check_platform_session(platform)
    if result.get("status") == "unknown" and "未知平台" in result.get("notes", ""):
        raise HTTPException(status_code=404, detail=result["notes"])
    return result


@router.post("/{platform}/open-login")
def open_platform_login(platform: str):
    result = open_login_browser(platform)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("notes", "打开登录窗口失败"))
    return result


@router.post("/{platform}/reset-risk")
def reset_platform_risk(platform: str):
    if platform not in ("doubao", "qianwen", "yuanbao"):
        raise HTTPException(status_code=404, detail=f"未知平台: {platform}")
    with get_db() as conn:
        conn.execute(
            """UPDATE platform_sessions
               SET risk_status = 'normal', risk_reason = NULL, run_after = NULL,
                   daily_run_date = NULL, daily_run_count = 0,
                   notes = ?, updated_at = ?
               WHERE platform = ?""",
            ("已人工确认账号恢复，风控熔断已重置；后续仍按低频安全策略执行", local_now(), platform),
        )
    return {"success": True, "platform": platform, "status": "normal", "notes": "风控状态已重置"}


@router.post("/{platform}/self-test")
def platform_self_test(platform: str, mode: str = Query(default="dom", description="dom=无侵入检测(默认，不发送消息)；chat=真实发消息深度检测")):
    if platform not in ("doubao", "qianwen", "yuanbao"):
        raise HTTPException(status_code=404, detail=f"未知平台: {platform}")

    lock = _self_test_locks[platform]
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=f"{platform} 正在自检中，请勿重复点击；等待当前自检完成")

    try:
        if mode == "chat":
            return _do_self_test_chat(platform)
        return _do_self_test_dom(platform)
    finally:
        lock.release()


def _do_self_test_dom(platform: str):
    """无侵入自检：只读页面 DOM 判断登录态，不发送任何消息。

    豆包有每周提问限额，旧的"发一条消息等回复"自检既耗额度又依赖
    回复时序（窗口不对就永远验证不过）。千问/元宝页面结构变化后
    URL 启发式也失效，这里统一改为 DOM 证据判定。
    """
    result = deep_login_check(platform)
    evidence = result.get("evidence") or {}
    return {
        "success": bool(result.get("success")),
        "platform": platform,
        "status": result.get("status"),
        "mode": "dom",
        "verification_status": "logged_in" if result.get("success") else (result.get("verdict") or "unknown"),
        "answer_len": 0,
        "page_url": evidence.get("url"),
        "page_title": evidence.get("title"),
        "has_input": evidence.get("hasInput"),
        "login_entries": evidence.get("loginEntryCount"),
        "avatar_count": evidence.get("avatarCount"),
        "body_head": evidence.get("bodyHead"),
        "captcha_status": "detected" if result.get("verdict") == "captcha_required" else "none",
        "failure_reason": None if result.get("success") else (result.get("notes") or "登录态验证未通过"),
        "notes": result.get("notes"),
    }


def _do_self_test_chat(platform: str):
    backend_dir = Path(__file__).resolve().parents[2]
    # run_mode=selftest：自检由人工主动触发，不受每日限额/最小间隔约束，
    # 但仍会记账（见 runner._mark_platform_run，自检确实消耗了一次平台提问），
    # 并且同样要抢跨进程平台锁，避免与正在跑的批量监测争抢同一个聊天标签。
    code = (
        "import json, sys; "
        "from app.services.platform_adapters.runner import run_platform_monitor; "
        "result = run_platform_monitor(sys.argv[1], 'GEO运营系统自检', '回复：登录态正常', "
        "timeout=25, run_mode='selftest'); "
        "print(json.dumps(result, ensure_ascii=False))"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code, platform],
            cwd=str(backend_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            # 必须大于 runner 内部单题等待上限（max(timeout,150)），否则父进程会在
            # 子进程仍在等待回答时把它杀掉，留下半发送状态的聊天标签。
            timeout=200,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="自检超时，请检查页面是否卡住或验证码未处理")
    if completed.returncode != 0:
        raw = (completed.stderr or completed.stdout or "")
        raise HTTPException(status_code=500, detail=_friendly_failure(raw))
    try:
        result = json.loads((completed.stdout or "").strip().splitlines()[-1])
    except Exception:
        raise HTTPException(status_code=500, detail="自检结果解析失败")
    ok = not result.get("failure_reason") and bool(result.get("answer_text"))
    if ok:
        with get_db() as conn:
            conn.execute(
                """UPDATE platform_sessions
                   SET status = 'logged_in', notes = ?, updated_at = ?
                   WHERE platform = ?""",
                ("自检通过；浏览器登录态可用；自检已计入当日提问次数（不再重置限额计数）", local_now(), platform),
            )
    return {
        "success": ok,
        "platform": platform,
        "status": "logged_in" if ok else "failed",
        "verification_status": result.get("verification_status"),
        "answer_len": len(result.get("answer_text") or ""),
        "screenshot_path": result.get("screenshot_path"),
        "raw_artifact_path": result.get("raw_artifact_path"),
        "captcha_status": result.get("captcha_status"),
        "failure_reason": result.get("failure_reason"),
        "notes": result.get("notes"),
    }
