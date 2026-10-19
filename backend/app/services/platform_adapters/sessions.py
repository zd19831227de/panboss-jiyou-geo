"""Persistent platform browser/API session management.

The session layer is intentionally honest: it reports whether a platform is
connected, logged in, captcha-blocked, or simply unknown. It never marks an AI
platform healthy unless a real browser/API check has succeeded.
"""
import json
import os
import socket
import subprocess
import sys
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import quote, urlparse

import requests

from app.core.config import settings, ai_port_map
from app.core.process_utils import launch_detached
from app.database import get_db, local_now

LOCAL_REQUESTS = requests.Session()
LOCAL_REQUESTS.trust_env = False

PLATFORMS = {
    "doubao": {
        "name": "豆包",
        "login_url": "https://www.doubao.com/chat/",
        "host": "doubao.com",
        "hosts": ["doubao.com", "www.doubao.com"],
        "logged_in_markers": ["textarea", "contenteditable", "chat"],
        "logged_out_markers": ["登录", "扫码", "验证码", "安全验证"],
    },
    "qianwen": {
        "name": "千问",
        # 门禁与公开官网口径保持一致；真正进入问答页由监测链路再跳转。
        "login_url": "https://www.qianwen.com/",
        "host": "qianwen.com",
        "hosts": ["qianwen.com", "www.qianwen.com", "qianwen.aliyun.com"],
        "logged_in_markers": ["textarea", "contenteditable", "chat"],
        "logged_out_markers": ["登录", "扫码", "验证码", "安全验证"],
    },
    "yuanbao": {
        "name": "元宝",
        "login_url": "https://yuanbao.tencent.com/chat",
        "host": "yuanbao.tencent.com",
        "hosts": ["yuanbao.tencent.com"],
        "logged_in_markers": ["textarea", "contenteditable", "chat"],
        "logged_out_markers": ["登录", "扫码", "验证码", "安全验证"],
    },
}


def platform_hosts(platform: str) -> list:
    meta = PLATFORMS.get(platform, {})
    return meta.get("hosts") or [meta.get("host", "")]


def url_matches_platform(platform: str, url: str) -> bool:
    host = (urlparse(url or "").netloc or url or "").lower()
    return any(host == item or host.endswith("." + item) for item in platform_hosts(platform) if item)


def _port_map() -> Dict[str, int]:
    """AI 监测端口表：统一来自 app.core.config。

    .env 里 GEO_PLATFORM_PORTS=doubao:9321,qianwen:9322,yuanbao:9323 可覆盖。
    """
    return ai_port_map()


def get_profile_dir(platform: str) -> str:
    return os.path.join(os.path.expanduser(settings.PLATFORM_PROFILE_ROOT), platform)


def get_port(platform: str) -> int:
    return _port_map().get(platform, 9321)


def _chrome_path() -> Optional[str]:
    candidates = []
    if sys.platform == "darwin":
        candidates.extend([
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ])
    elif sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        candidates.extend([
            os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(local, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(program_files, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(program_files_x86, "Microsoft", "Edge", "Application", "msedge.exe"),
        ])
    candidates.extend([
        "google-chrome",
        "chromium",
        "chromium-browser",
        "chrome",
        "msedge",
    ])
    for candidate in candidates:
        if candidate.startswith("/") and os.path.exists(candidate):
            return candidate
        if not candidate.startswith("/"):
            found = shutil.which(candidate)
            if found:
                return found
    return None


def _is_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _cdp_tabs(port: int) -> list:
    try:
        return LOCAL_REQUESTS.get(f"http://127.0.0.1:{port}/json", timeout=2).json()
    except Exception:
        return []


def _activate_browser_app() -> None:
    if sys.platform != "darwin":
        return
    try:
        subprocess.Popen(
            ["osascript", "-e", 'tell application "Google Chrome" to activate'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def _open_or_activate_platform_tab(platform: str, port: int, url: str) -> dict:
    """Ensure the already-running persistent browser shows this platform.

    A port being open is not enough for users: the settings button must visibly
    open or focus the platform tab so login/captcha can be handled.
    """
    tabs = _cdp_tabs(port)
    for tab in tabs:
        tab_url = tab.get("url", "")
        if tab.get("type") == "page" and url_matches_platform(platform, tab_url):
            target_id = tab.get("id")
            if target_id:
                for method in (LOCAL_REQUESTS.post, LOCAL_REQUESTS.get):
                    try:
                        method(f"http://127.0.0.1:{port}/json/activate/{target_id}", timeout=2)
                        break
                    except Exception:
                        continue
            _activate_browser_app()
            return {"opened": False, "activated": True, "tab_url": tab_url}

    encoded = quote(url, safe="")
    created = None
    for method in (LOCAL_REQUESTS.put, LOCAL_REQUESTS.get):
        try:
            resp = method(f"http://127.0.0.1:{port}/json/new?{encoded}", timeout=3)
            if resp.ok:
                created = resp.json()
                break
        except Exception:
            continue
    _activate_browser_app()
    return {
        "opened": bool(created),
        "activated": False,
        "tab_url": (created or {}).get("url") or url,
        "target_id": (created or {}).get("id"),
    }


def _inspect_browser_page(platform: str, port: int) -> dict:
    """Read the live page enough to avoid false login-state guesses."""
    result = {
        "has_platform_tab": False,
        "has_input": False,
        "captcha_required": False,
        "logged_out": False,
        "visible_text": "",
    }
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return result

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=2500)
            for context in browser.contexts:
                for page in context.pages:
                    if not url_matches_platform(platform, page.url):
                        continue
                    result["has_platform_tab"] = True
                    try:
                        text = page.evaluate(
                            """() => (document.body && document.body.innerText || '')
                                .replace(/\n{3,}/g, '\n\n').slice(0, 3000)"""
                        ) or ""
                    except Exception:
                        text = ""
                    result["visible_text"] = text
                    result["captcha_required"] = any(word in text for word in ["验证码", "安全验证", "人机验证", "滑块"])
                    # A real chat page should expose an input surface. We only
                    # treat login as logged-out when explicit login UI appears
                    # without a chat input.
                    try:
                        result["has_input"] = page.locator(
                            "textarea, [contenteditable='true'], div[role='textbox'], input[type='text']"
                        ).count() > 0
                    except Exception:
                        result["has_input"] = False
                    result["logged_out"] = (not result["has_input"]) and any(
                        word in text for word in ["登录", "扫码登录", "手机号登录", "微信登录", "请登录"]
                    )
                    browser.close()
                    return result
            browser.close()
    except Exception:
        return result
    return result


# ─────────────────────────────────────────────────────────
# 无侵入登录态检测（DOM probe）
#
# 旧自检靠"真实发一条消息再等回复"验证登录：豆包有每周提问限额，
# 且窗口不对/收不到回复时无法判断；千问、元宝则因页面结构变化导致
# URL/标题启发式误判。改为直接读页面 DOM：有聊天输入框且没有
# "登录"入口就视为已登录，全程不发送任何消息。
# ─────────────────────────────────────────────────────────

_LOGIN_PROBE_JS = r"""(() => {
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 8 && r.height > 8 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const bodyText = (document.body && document.body.innerText) || '';
  const inputs = Array.from(document.querySelectorAll(
    "textarea, [contenteditable='true'], div[role='textbox'], input[type='text']"
  )).filter(visible);
  // 登录入口：可见的按钮/链接，文本恰为"登录"，或为明确的登录引导文案
  const loginNodes = Array.from(document.querySelectorAll(
    'button, a, [role="button"]'
  )).filter(visible).filter(el => {
    const t = (el.innerText || el.textContent || '').trim();
    return t === '登录' || t === '登陆' || t === 'Log in'
      || /^扫码登录$/.test(t) || /^手机号登录$/.test(t) || /^请登录后/.test(t);
  });
  // 账号头像/用户信息组件（登录成功后才会出现）
  const avatarNodes = Array.from(document.querySelectorAll(
    'img[class*="avatar" i], [class*="avatar" i], [class*="user-info" i], [class*="userInfo" i], [class*="account-entry" i]'
  )).filter(visible).filter(el => {
    const r = el.getBoundingClientRect();
    return r.width < 220 && r.height < 220;
  });
  return {
    url: location.href,
    title: document.title || '',
    hasInput: inputs.length > 0,
    inputCount: inputs.length,
    loginEntryCount: loginNodes.length,
    loginEntryText: loginNodes[0] ? (loginNodes[0].innerText || '').trim().slice(0, 24) : '',
    avatarCount: avatarNodes.length,
    captcha: /验证码|安全验证|人机验证|滑块/.test(bodyText.slice(0, 4000)),
    bodyHead: bodyText.replace(/\s+/g, ' ').trim().slice(0, 200),
  };
})()"""


def _probe_verdict(info: dict) -> str:
    """根据 DOM 证据判定登录态：logged_in / logged_out / captcha_required / unknown。"""
    if not info:
        return "unknown"
    if info.get("captcha"):
        return "captcha_required"
    if int(info.get("loginEntryCount") or 0) > 0:
        return "logged_out"
    if info.get("hasInput"):
        return "logged_in"
    if int(info.get("avatarCount") or 0) > 0:
        return "logged_in"
    return "unknown"


_DOM_PROBE_CACHE: Dict[str, tuple] = {}
_DOM_PROBE_TTL_SECONDS = 20.0


def _probe_platform_dom(platform: str, port: int, navigate_to_chat: bool = False, force: bool = False) -> Optional[dict]:
    """附着平台 tab 读取登录态证据。返回 {verdict, ...证据}；无法探测返回 None。

    navigate_to_chat=True 时先把 tab 导航到聊天入口页（用于自检前的深度确认），
    平时轮询只读不跳转，避免干扰用户操作。
    """
    now = time.time()
    if not force and not navigate_to_chat:
        cached = _DOM_PROBE_CACHE.get(platform)
        if cached and now - cached[0] < _DOM_PROBE_TTL_SECONDS:
            return cached[1]

    tabs = _cdp_tabs(port)
    tab = next((t for t in tabs if t.get("type") == "page" and url_matches_platform(platform, t.get("url", ""))), None)
    if tab is None:
        return None
    ws_url = tab.get("webSocketDebuggerUrl")
    if not ws_url:
        return None

    import websocket

    chat_url = PLATFORMS[platform]["login_url"] if platform != "qianwen" else "https://www.qianwen.com/chat"
    try:
        ws = websocket.create_connection(ws_url, timeout=5, http_proxy_host=None, http_proxy_port=None)
    except Exception:
        return None
    try:
        seq = 0

        def call(method: str, params: Optional[dict] = None, timeout: float = 8) -> dict:
            nonlocal seq
            seq += 1
            mid = seq
            ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                payload = json.loads(ws.recv())
                if payload.get("id") == mid:
                    if payload.get("error"):
                        raise RuntimeError(payload["error"].get("message") or str(payload["error"]))
                    return payload.get("result") or {}
            raise TimeoutError(f"CDP call timeout: {method}")

        call("Page.enable")
        call("Runtime.enable")
        if navigate_to_chat:
            try:
                call("Page.navigate", {"url": chat_url}, timeout=8)
            except Exception:
                pass
            # 等页面渲染出可判定的内容
            deadline = time.time() + 12
            while time.time() < deadline:
                time.sleep(0.8)
                try:
                    state = call("Runtime.evaluate", {"expression": "document.readyState", "returnByValue": True})
                    if (state.get("result") or {}).get("value") in ("interactive", "complete"):
                        break
                except Exception:
                    pass
        payload = call(
            "Runtime.evaluate",
            {"expression": _LOGIN_PROBE_JS, "returnByValue": True},
            timeout=10,
        )
        value = (payload.get("result") or {}).get("value")
        if not isinstance(value, dict):
            return None
        info = dict(value)
        info["verdict"] = _probe_verdict(info)
        if not navigate_to_chat:
            _DOM_PROBE_CACHE[platform] = (time.time(), info)
        return info
    except Exception:
        return None
    finally:
        try:
            ws.close()
        except Exception:
            pass


def deep_login_check(platform: str) -> dict:
    """自检用深度登录检查：确保浏览器和平台页都在，导航到聊天页后读 DOM。

    全程不发送任何消息，不消耗豆包每周提问限额，也不依赖"发消息收回复"
    的时序（旧逻辑窗口不对就永远验证不过）。
    """
    if platform not in PLATFORMS:
        return {"success": False, "platform": platform, "verdict": "unknown", "notes": f"未知平台: {platform}"}
    port = get_port(platform)
    if not _is_port_open(port):
        opened = open_login_browser(platform)
        if not opened.get("success"):
            return {"success": False, "platform": platform, "verdict": "not_connected", "notes": opened.get("notes") or "浏览器未能启动"}
        deadline = time.time() + 10
        while time.time() < deadline and not _is_port_open(port):
            time.sleep(1)
        if not _is_port_open(port):
            return {"success": False, "platform": platform, "verdict": "not_connected", "notes": "浏览器启动超时，请重试打开登录"}

    info = _probe_platform_dom(platform, port, navigate_to_chat=True, force=True)
    if info is None:
        # 没有平台tab时先打开一个再探测
        _open_or_activate_platform_tab(platform, port, PLATFORMS[platform]["login_url"])
        time.sleep(2)
        info = _probe_platform_dom(platform, port, navigate_to_chat=True, force=True)
    if info is None:
        _update_session(platform, status="unknown", last_checked_at=local_now(),
                        notes="DOM登录检测失败：无法读取平台页面，请检查浏览器是否正常")
        return {"success": False, "platform": platform, "verdict": "unknown", "notes": "无法读取平台页面（CDP探测失败），请确认浏览器窗口正常显示"}

    verdict = info.get("verdict")
    if verdict == "logged_in":
        status, notes = "logged_in", "无侵入自检通过：检测到聊天输入且无登录入口，未发送消息（不消耗提问额度）"
    elif verdict == "logged_out":
        status, notes = "expired", "页面存在登录入口（未登录），请在打开的浏览器完成登录后重新自检"
    elif verdict == "captcha_required":
        status, notes = "captcha_required", "页面处于验证码/安全验证状态，请在浏览器中人工处理后重新自检"
    else:
        status, notes = "unknown", "无法从页面确认登录态（无输入框也无登录入口），请打开浏览器人工确认"
    _update_session(platform, status=status, last_checked_at=local_now(),
                    captcha_required=1 if verdict == "captcha_required" else 0, notes=notes)
    return {"success": verdict == "logged_in", "platform": platform, "verdict": verdict, "status": status, "notes": notes, "evidence": info}


def _update_session(platform: str, **fields) -> None:
    fields["updated_at"] = local_now()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT OR IGNORE INTO platform_sessions
               (platform, profile_dir, port, status, login_url)
               VALUES (?, ?, ?, 'unknown', ?)""",
            (
                platform,
                get_profile_dir(platform),
                get_port(platform),
                PLATFORMS.get(platform, {}).get("login_url"),
            ),
        )
        sets = ", ".join(f"{key} = ?" for key in fields)
        cursor.execute(
            f"UPDATE platform_sessions SET {sets} WHERE platform = ?",
            [*fields.values(), platform],
        )


def ensure_platform_sessions() -> None:
    Path(os.path.expanduser(settings.PLATFORM_PROFILE_ROOT)).mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        cursor = conn.cursor()
        for platform, meta in PLATFORMS.items():
            Path(get_profile_dir(platform)).mkdir(parents=True, exist_ok=True)
            cursor.execute(
                """INSERT OR IGNORE INTO platform_sessions
                   (platform, profile_dir, port, status, login_url, notes)
                   VALUES (?, ?, ?, 'unknown', ?, '尚未检查登录态')""",
                (platform, get_profile_dir(platform), get_port(platform), meta["login_url"]),
            )
            cursor.execute(
                """UPDATE platform_sessions
                   SET profile_dir = ?, port = ?, login_url = ?
                   WHERE platform = ?""",
                (get_profile_dir(platform), get_port(platform), meta["login_url"], platform),
            )


def open_login_browser(platform: str) -> dict:
    if platform not in PLATFORMS:
        return {"success": False, "status": "unknown", "notes": f"未知平台: {platform}"}

    ensure_platform_sessions()
    port = get_port(platform)
    profile_dir = get_profile_dir(platform)
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    url = PLATFORMS[platform]["login_url"]

    if _is_port_open(port):
        tab_result = _open_or_activate_platform_tab(platform, port, url)
        _update_session(
            platform,
            status="browser_open",
            last_checked_at=local_now(),
            captcha_required=0,
            notes=f"已{'打开' if tab_result.get('opened') else '激活'}{PLATFORMS[platform]['name']}登录页，端口 {port}",
        )
        return {
            "success": True,
            "platform": platform,
            "status": "browser_open",
            "port": port,
            "profile_dir": profile_dir,
            "url": url,
            "opened": tab_result.get("opened", False),
            "activated": tab_result.get("activated", False),
            "tab_url": tab_result.get("tab_url"),
            "notes": f"已{'打开' if tab_result.get('opened') else '激活'}{PLATFORMS[platform]['name']}登录页，端口 {port}",
        }

    chrome = _chrome_path()
    if not chrome:
        _update_session(platform, status="not_connected", notes="未找到 Chrome/Chromium")
        return {"success": False, "status": "not_connected", "notes": "未找到 Chrome/Chromium"}

    args = [
        chrome,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        url,
    ]
    try:
        launch_detached(args)
    except Exception as exc:
        _update_session(platform, status="not_connected", notes=f"启动浏览器失败: {exc}")
        return {"success": False, "status": "not_connected", "notes": f"启动浏览器失败: {exc}"}

    _update_session(
        platform,
        status="browser_open",
        last_checked_at=local_now(),
        captcha_required=0,
        notes=f"已打开固定登录浏览器，端口 {port}",
    )
    _activate_browser_app()
    return {
        "success": True,
        "platform": platform,
        "status": "browser_open",
        "port": port,
        "profile_dir": profile_dir,
        "url": url,
        "notes": "请在打开的浏览器完成登录；登录态会保存在固定 profile 中",
    }


def check_platform_session(platform: str) -> dict:
    if platform not in PLATFORMS:
        return {"platform": platform, "status": "unknown", "is_logged_in": False, "notes": "未知平台"}

    ensure_platform_sessions()

    # Qianwen API key means API path may work; browser CDP monitor still needs a real session.
    if platform == "qianwen" and settings.DASHSCOPE_API_KEY:
        result = {
            "platform": platform,
            "status": "api_ready",
            "is_logged_in": False,
            "port": get_port(platform),
            "profile_dir": get_profile_dir(platform),
            "login_url": PLATFORMS[platform]["login_url"],
            "captcha_required": False,
            "notes": "DashScope API 已配置（api_ready）；浏览器监测仍需打开登录并自检，不能视为已登录",
        }
        _update_session(platform, status="api_ready", last_checked_at=local_now(), captcha_required=0, notes=result["notes"])
        return result

    port = get_port(platform)
    profile_dir = get_profile_dir(platform)
    if not _is_port_open(port):
        result = {
            "platform": platform,
            "status": "not_connected",
            "is_logged_in": False,
            "port": port,
            "profile_dir": profile_dir,
            "login_url": PLATFORMS[platform]["login_url"],
            "captcha_required": False,
            "notes": f"固定浏览器未运行，需打开登录窗口，端口 {port}",
        }
        _update_session(platform, status="not_connected", last_checked_at=local_now(), captcha_required=0, notes=result["notes"])
        return result

    tabs = _cdp_tabs(port)
    text = " ".join(f"{t.get('title','')} {t.get('url','')}" for t in tabs)
    captcha_required = any(word in text for word in ["验证码", "安全验证", "人机验证", "滑块"])
    has_platform_tab = any(url_matches_platform(platform, t.get("url", "")) for t in tabs)
    explicit_login_page = False
    for tab in tabs:
        if not url_matches_platform(platform, tab.get("url", "")):
            continue
        parsed = urlparse(tab.get("url", ""))
        title = tab.get("title", "")
        path = (parsed.path or "").lower()
        if path in ("/login", "/passport/login") or "扫码登录" in title or "手机号登录" in title:
            explicit_login_page = True
            break

    if captcha_required:
        status = "captcha_required"
        notes = "检测到验证码/安全验证，需要自动或人工处理"
    elif has_platform_tab:
        # 优先用 DOM 探测结果判定登录态（千问/元宝页面结构改版后
        # URL/标题启发式经常误判，这里直接读页面元素）。
        probe = _probe_platform_dom(platform, port)
        if probe:
            verdict = probe.get("verdict")
            if verdict == "logged_out":
                status = "expired"
                notes = f"页面检测到登录入口（{probe.get('loginEntryText') or '登录'}），登录态未生效"
            elif verdict == "captcha_required":
                status = "captcha_required"
                notes = "页面处于验证码/安全验证状态，需人工处理"
            elif verdict == "logged_in":
                status = "logged_in"
                notes = "页面检测到聊天输入且无登录入口，登录态可用"
            else:
                stored_preview = _stored_session(platform)
                if (stored_preview or {}).get("status") == "logged_in":
                    status = "logged_in"
                    notes = "沿用自检通过后的登录态；若监测失败请重新自检"
                else:
                    status = "browser_open"
                    notes = "固定浏览器已连接但页面无法确认登录态；请点「运行自检」"
        else:
            # DOM 探测不可用时退回旧的 URL/标题启发式
            if explicit_login_page:
                status = "expired"
                notes = "检测到登录入口，登录态可能已失效"
            else:
                stored_preview = _stored_session(platform)
                if (stored_preview or {}).get("status") == "logged_in":
                    status = "logged_in"
                    notes = "沿用自检通过后的登录态；若监测失败请重新自检"
                else:
                    status = "browser_open"
                    notes = "固定浏览器已连接且未见登录页；请点「运行自检」后才标为可用"
    else:
        status = "unknown"
        notes = "浏览器已连接，但无法确认登录态；请打开平台页面确认"

    stored = _stored_session(platform)
    result = {
        "platform": platform,
        "status": status,
        "is_logged_in": status == "logged_in",
        "port": port,
        "profile_dir": profile_dir,
        "login_url": PLATFORMS[platform]["login_url"],
        "captcha_required": captcha_required,
        "risk_status": (stored or {}).get("risk_status") or "normal",
        "risk_reason": (stored or {}).get("risk_reason"),
        "run_after": (stored or {}).get("run_after"),
        "daily_run_count": (stored or {}).get("daily_run_count") or 0,
        "notes": notes,
    }
    _update_session(
        platform,
        status=status,
        last_checked_at=local_now(),
        captcha_required=1 if captcha_required else 0,
        notes=notes,
    )
    return result


def check_all_platform_sessions() -> dict:
    return {platform: check_platform_session(platform) for platform in PLATFORMS}


def _stored_session(platform: str) -> dict:
    try:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM platform_sessions WHERE platform = ?", (platform,)).fetchone()
            return dict(row) if row else {}
    except Exception:
        return {}
