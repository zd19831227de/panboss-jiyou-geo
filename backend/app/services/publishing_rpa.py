"""Publishing RPA framework.

This intentionally starts with a safe semi-automatic flow: open the publisher
account in a fixed browser profile, prepare the draft page, try to prefill
obvious editor fields, then save screenshot/HTML evidence. It never pretends a
publication succeeded without a published URL or explicit evidence.
"""
import json
import os
import sys
import html as html_lib
import re
import shutil
import socket
import subprocess
import time
import textwrap
import base64
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import websocket

from app.core.config import settings, publish_port_map, reserved_port_set
from app.core.process_utils import launch_detached

LOCAL_REQUESTS = requests.Session()
LOCAL_REQUESTS.trust_env = False


def _mod_key() -> str:
    """macOS uses Meta (Cmd); Windows/Linux use Control."""
    return "Meta" if sys.platform == "darwin" else "Control"


def _select_all_shortcut() -> str:
    return f"{_mod_key()}+A"


def _paste_shortcut() -> str:
    return f"{_mod_key()}+V"


def _allow_placeholder_media(task: Optional[dict] = None, workflow: Optional[dict] = None) -> bool:
    """Only workflow/self-tests may inject generated placeholder images."""
    if (workflow or {}).get("allow_test_media") or (task or {}).get("allow_test_media"):
        return True
    notes = str((task or {}).get("notes") or "")
    return "workflow_test" in notes or "工作流测试" in notes


def _resolve_media_paths(content: dict, task: Optional[dict] = None, workflow: Optional[dict] = None, min_count: int = 1) -> list[str]:
    paths = _content_media_file_paths(content, min_count=min_count)
    if paths:
        return paths
    if _allow_placeholder_media(task, workflow):
        return [_test_upload_image_path()]
    return []


PUBLISH_PORT_BASE = 9521
PUBLISH_PORTS = publish_port_map()
RESERVED_PORTS = reserved_port_set()


@dataclass
class PublishRpaResult:
    status: str
    notes: str = ""
    published_url: Optional[str] = None
    screenshot_path: Optional[str] = None
    raw_artifact_path: Optional[str] = None
    summary_artifact_path: Optional[str] = None
    failure_reason: Optional[str] = None


def publish_profile_dir(platform: str, account_name: str = "default") -> str:
    safe_account = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in account_name)[:48] or "default"
    return str(Path(os.path.expanduser(settings.PLATFORM_PROFILE_ROOT)) / "publish" / platform / safe_account)


def publish_port(platform: str) -> int:
    port = PUBLISH_PORTS.get(platform, PUBLISH_PORT_BASE)
    if port in RESERVED_PORTS:
        raise RuntimeError(f"发布端口 {port} 属于保留端口段，禁止用于GEO发布浏览器")
    return port


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


def _decode_console_output(data) -> str:
    """Windows 控制台输出使用 OEM 代码页（中文系统为 GBK），非 UTF-8；逐级尝试解码。"""
    if not data:
        return ""
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, AttributeError):
            continue
    return data.decode("utf-8", errors="replace")


def _win_listening_pids(port: int) -> list:
    """Windows：返回监听该端口的进程 PID 列表（netstat -ano）。"""
    try:
        output = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return []
    pids = []
    for line in _decode_console_output(output.stdout).splitlines():
        parts = line.split()
        # 形如: TCP  127.0.0.1:9522  0.0.0.0:0  LISTENING  1234
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].rsplit(":", 1)[-1] == str(port) and parts[4].isdigit():
                pids.append(parts[4])
    return pids


def _win_process_command(pid: str) -> str:
    """Windows：读取进程完整命令行（wmic 优先，PowerShell 兜底）。"""
    for cmd in (
        ["wmic", "process", "where", f"processid={pid}", "get", "commandline", "/value"],
        ["powershell", "-NoProfile", "-Command",
         f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine"],
    ):
        try:
            output = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            text = _decode_console_output(output.stdout)
            if "CommandLine=" in text:  # wmic /value 输出格式
                text = text.split("CommandLine=", 1)[1]
            value = text.strip()
            if value:
                return value
        except Exception:
            continue
    return ""


def _port_owner_command(port: int) -> str:
    """返回监听该端口进程的完整命令行（用于归属校验）。Windows/POSIX 各自实现。"""
    if sys.platform.startswith("win"):
        commands = []
        for pid in _win_listening_pids(port):
            cmdline = _win_process_command(pid)
            if cmdline:
                commands.append(cmdline)
        return "\n".join(commands)
    try:
        output = subprocess.run(
            ["lsof", "-tiTCP:%s" % port, "-sTCP:LISTEN"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
        )
        commands = []
        for raw_pid in output.stdout.splitlines():
            if raw_pid.strip().isdigit():
                ps = subprocess.run(
                    ["ps", "-p", raw_pid.strip(), "-o", "command="],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=2,
                )
                if ps.stdout.strip():
                    commands.append(ps.stdout.strip())
        return "\n".join(commands)
    except Exception:
        return ""


def _port_uses_profile(port: int, profile_dir: str) -> bool:
    command = _port_owner_command(port)
    return bool(command and f"--user-data-dir={profile_dir}" in command)


def _port_uses_publish_root(port: int, platform: str) -> bool:
    command = _port_owner_command(port)
    root = str(Path(os.path.expanduser(settings.PLATFORM_PROFILE_ROOT)) / "publish" / platform)
    return bool(command and root in command)


def _artifact_dir() -> Path:
    path = Path(settings.GEO_ARTIFACT_DIR) / "publishing" / datetime.now().strftime("%Y%m%d")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_artifact(task_id: int, suffix: str, data, binary: bool = False) -> str:
    path = _artifact_dir() / f"publish_task_{task_id}_{datetime.now().strftime('%H%M%S_%f')}.{suffix}"
    if binary:
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    return str(path)


def open_publish_browser(platform: str, account_name: str, login_url: str = "") -> dict:
    try:
        port = publish_port(platform)
    except RuntimeError as exc:
        return {"success": False, "status": "port_reserved", "notes": str(exc)}
    profile_dir = publish_profile_dir(platform, account_name)
    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    if _is_port_open(port):
        if not _port_uses_profile(port, profile_dir):
            return {
                "success": False,
                "status": "port_conflict",
                "port": port,
                "profile_dir": profile_dir,
                "notes": f"端口 {port} 已被非当前GEO发布 profile 占用，已阻止误连",
            }
        result = {"success": True, "status": "browser_open", "port": port, "profile_dir": profile_dir, "url": login_url}
        return _apply_login_state(result, platform, port)

    chrome = _chrome_path()
    if not chrome:
        return {"success": False, "status": "not_connected", "notes": "未找到 Chrome/Chromium"}

    args = [
        chrome,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
    ]
    if login_url:
        args.append(login_url)
    try:
        launch_detached(args)
    except Exception as exc:
        return {"success": False, "status": "not_connected", "notes": f"启动浏览器失败: {exc}"}

    deadline = time.time() + int(os.getenv("GEO_PUBLISH_BROWSER_START_TIMEOUT", "20"))
    while time.time() < deadline:
        if _is_port_open(port):
            time.sleep(1.5)  # 等 CDP websocket endpoint 就绪再探测登录态
            result = {"success": True, "status": "browser_open", "port": port, "profile_dir": profile_dir, "url": login_url}
            return _apply_login_state(result, platform, port)
        time.sleep(0.5)
    return {"success": False, "status": "not_connected", "notes": f"浏览器端口 {port} 未就绪"}


PUBLISH_LOGIN_COOKIES = {
    # 平台 -> (登录凭证 cookie 所在域, 登录态 cookie 名列表)。
    # cookie 存在且未过期即判定已登录；不依赖当前打开的是哪个页面。
    # 凭证名 2026-10-12 经 CDP Storage.getCookies 实测确认：
    # sohu=ppinf/pprdig（搜狐通行证，13 天）；ctrip_travel=cticket/weticket
    # （.ctrip.com，29 天）；xiaohongshu 创作平台不用消费站 web_session，
    # 而是 access-token-creator.xiaohongshu.com / galaxy_creator_session_id。
    "zhihu": (".zhihu.com", ["z_c0"]),
    "baijiahao": (".baidu.com", ["BDUSS"]),
    "toutiao": (".toutiao.com", ["sessionid", "sid_guard"]),
    "douyin": (".douyin.com", ["sessionid", "sid_guard"]),
    "xiaohongshu": (".xiaohongshu.com", ["access-token-creator.xiaohongshu.com", "galaxy_creator_session_id"]),
    "sohu": (".sohu.com", ["ppinf", "pprdig"]),
    "ctrip_travel": (".ctrip.com", ["cticket", "weticket"]),
}


def _detect_login_cookies(platform: str, port: int) -> Optional[dict]:
    """CDP 读取浏览器 cookie store，按平台登录凭证判定登录态。

    返回 {"verdict": "logged_in"|"logged_out", "cookie": 名字}；
    平台未配置或 CDP 探测失败返回 None（调用方退回 browser_open 语义）。
    """
    spec = PUBLISH_LOGIN_COOKIES.get(platform)
    if not spec:
        return None
    domain, keys = spec
    try:
        tabs = LOCAL_REQUESTS.get(f"http://127.0.0.1:{port}/json", timeout=2).json()
    except Exception:
        return None
    ws_url = next(
        (t.get("webSocketDebuggerUrl") for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")),
        None,
    )
    if not ws_url:
        return None
    try:
        ws = websocket.create_connection(ws_url, timeout=5, http_proxy_host=None, http_proxy_port=None)
    except Exception:
        return None

    def call(method: str, params: Optional[dict] = None, timeout: float = 8) -> dict:
        call.seq += 1  # CDP 消息 id 必须是整数，字符串 id Chrome 不响应
        mid = call.seq
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            payload = json.loads(ws.recv())
            if payload.get("id") == mid:
                if payload.get("error"):
                    raise RuntimeError(str(payload.get("error")))
                return payload.get("result") or {}
        raise TimeoutError(f"CDP call timeout: {method}")

    call.seq = 0

    try:
        try:
            cookies = call("Storage.getCookies").get("cookies") or []
        except Exception:
            cookies = call("Network.getAllCookies").get("cookies") or []
        now = time.time()
        for cookie in cookies:
            if cookie.get("name") not in keys:
                continue
            if domain not in (cookie.get("domain") or ""):
                continue
            if not cookie.get("value"):
                continue
            expires = cookie.get("expires") or 0
            if expires > 0 and expires < now:
                continue
            return {"verdict": "logged_in", "cookie": cookie.get("name")}
        return {"verdict": "logged_out", "cookie": None}
    except Exception:
        return None
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _apply_login_state(result: dict, platform: str, port: int) -> dict:
    """在 browser_open 结果上叠加登录态：检测到凭证时状态升级为 logged_in。"""
    login = _detect_login_cookies(platform, port)
    if login and login.get("verdict") == "logged_in":
        result["status"] = "logged_in"
    result["login_state"] = (login or {}).get("verdict") or "unknown"
    result["login_cookie"] = (login or {}).get("cookie")
    return result


def check_publish_browser(platform: str, account_name: str) -> dict:
    try:
        port = publish_port(platform)
    except RuntimeError as exc:
        return {"status": "port_reserved", "is_connected": False, "notes": str(exc)}
    profile_dir = publish_profile_dir(platform, account_name)
    if not _is_port_open(port):
        return {"status": "not_connected", "is_connected": False, "port": port, "profile_dir": profile_dir}
    if not _port_uses_profile(port, profile_dir):
        return {
            "status": "port_conflict",
            "is_connected": False,
            "port": port,
            "profile_dir": profile_dir,
            "notes": f"端口 {port} 已被非当前GEO发布 profile 占用",
        }
    try:
        tabs = LOCAL_REQUESTS.get(f"http://127.0.0.1:{port}/json", timeout=2).json()
    except Exception:
        tabs = []
    result = {"status": "browser_open", "is_connected": True, "port": port, "profile_dir": profile_dir, "tabs": len(tabs)}
    return _apply_login_state(result, platform, port)


def close_publish_browser(platform: str) -> dict:
    try:
        port = publish_port(platform)
    except RuntimeError as exc:
        return {"success": False, "status": "port_reserved", "notes": str(exc)}
    uses_publish_root = _port_uses_publish_root(port, platform)
    if not _is_port_open(port):
        return {"success": True, "status": "not_connected", "port": port}
    if not uses_publish_root:
        return {
            "success": False,
            "status": "port_conflict_not_closed",
            "port": port,
            "notes": f"端口 {port} 不是GEO {platform} 发布浏览器，已拒绝关闭",
        }
    try:
        tabs = LOCAL_REQUESTS.get(f"http://127.0.0.1:{port}/json", timeout=2).json()
        for tab in tabs:
            target_id = tab.get("id")
            if target_id:
                try:
                    LOCAL_REQUESTS.get(f"http://127.0.0.1:{port}/json/close/{target_id}", timeout=1)
                except Exception:
                    pass
    except Exception:
        pass
    time.sleep(0.8)
    if not _is_port_open(port):
        return {"success": True, "status": "closed", "port": port}
    # 跨平台兜底：按端口找监听进程 PID 并终止（此前仅 POSIX lsof 生效）
    try:
        if sys.platform.startswith("win"):
            pids = _win_listening_pids(port)
        else:
            output = subprocess.run(
                ["lsof", "-tiTCP:%s" % port, "-sTCP:LISTEN"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2,
            )
            pids = [line.strip() for line in output.stdout.splitlines() if line.strip().isdigit()]
        for raw_pid in pids:
            try:
                os.kill(int(raw_pid), 15)
            except Exception:
                pass
    except Exception:
        pass
    return {"success": not _is_port_open(port), "status": "closed" if not _is_port_open(port) else "close_requested", "port": port}


def recover_baijiahao_urls(account_name: str, title: str = "", limit: int = 8) -> dict:
    opened = open_publish_browser("baijiahao", account_name or "default", "https://baijiahao.baidu.com/")
    if not opened.get("success"):
        return {"status": "failed", "failure_reason": opened.get("notes") or "百家号浏览器未连接", "candidates": []}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return {"status": "failed", "failure_reason": f"Playwright 不可用: {exc}", "candidates": []}

    candidates = []
    screenshot = None
    raw = None
    html = ""
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{opened['port']}", timeout=3000)
            context, page = _new_page_from_cdp_context(browser, opened["port"] )
            page.set_default_timeout(4000)
            page.set_default_navigation_timeout(12000)
            for target in [
                "https://baijiahao.baidu.com/builder/rc/content",
                "https://baijiahao.baidu.com/builder/rc/manage",
                "https://baijiahao.baidu.com/",
            ]:
                try:
                    page.goto(target, wait_until="domcontentloaded", timeout=12000)
                    page.wait_for_timeout(1200)
                    _dismiss_publish_popups(page)
                    html = page.content()
                    text = _visible_text(page)
                    found = page.evaluate(
                        """(needle) => {
                          const links = Array.from(document.querySelectorAll('a[href]'))
                            .map(a => ({ href: a.href, text: (a.innerText || a.textContent || '').trim() }))
                            .filter(item => /baidu\\.com|baijiahao\\.baidu\\.com|mbd\\.baidu\\.com/.test(item.href));
                          return links.map(item => ({
                            ...item,
                            score: (needle && (item.text.includes(needle) || item.href.includes(needle)) ? 20 : 0)
                              + (/article|news|item|detail|s\\?/.test(item.href) ? 5 : 0)
                              + (item.text ? 1 : 0)
                          })).sort((a,b) => b.score - a.score).slice(0, 12);
                        }""",
                        title or "",
                    )
                    seen = {item.get("href") for item in candidates}
                    for item in found:
                        if item.get("href") and item.get("href") not in seen and _is_public_content_url("baijiahao", item.get("href")):
                            candidates.append(item)
                            seen.add(item.get("href"))
                    if title and title in text:
                        break
                except Exception:
                    continue
            screenshot = _try_screenshot(0, page)
            raw = _save_artifact(0, "baijiahao_recover.html", html)
            _try_close_page(page)
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return {"status": "failed", "failure_reason": f"百家号URL回捞异常: {exc}", "candidates": candidates[:limit]}
    return {
        "status": "ready" if candidates else "not_found",
        "failure_reason": "" if candidates else "未从百家号内容管理页回捞到候选作品URL",
        "candidates": candidates[:limit],
        "screenshot_path": screenshot,
        "raw_artifact_path": raw,
    }


def recover_zhihu_urls(account_name: str, title: str = "", limit: int = 8) -> dict:
    opened = open_publish_browser("zhihu", account_name or "default", "https://www.zhihu.com/")
    if not opened.get("success"):
        return {"status": "failed", "failure_reason": opened.get("notes") or "知乎浏览器未连接", "candidates": []}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return {"status": "failed", "failure_reason": f"Playwright 不可用: {exc}", "candidates": []}

    candidates = []
    screenshot = None
    raw = None
    html = ""
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{opened['port']}", timeout=3000)
            context, page = _new_page_from_cdp_context(browser, opened["port"] )
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(15000)
            for target in [
                "https://www.zhihu.com/creator/contents",
                "https://www.zhihu.com/creator/manage/creation/all",
                "https://www.zhihu.com/people/me/posts",
                "https://www.zhihu.com/people/me/answers",
                "https://zhuanlan.zhihu.com/write",
                "https://www.zhihu.com/creator",
                "https://www.zhihu.com/people",
                "https://www.zhihu.com/",
            ]:
                try:
                    page.goto(target, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(1800)
                    _dismiss_publish_popups(page)
                    html = page.content()
                    text = _visible_text(page)
                    found = page.evaluate(
                        """(needle) => {
                          const links = Array.from(document.querySelectorAll('a[href]'))
                            .map(a => ({ href: a.href, text: (a.innerText || a.textContent || '').trim() }))
                            .filter(item => /zhihu\\.com/.test(item.href));
                          return links.map(item => ({
                            ...item,
                            score: (needle && (item.text.includes(needle) || item.href.includes(needle)) ? 25 : 0)
                              + (/zhuanlan\\.zhihu\\.com\\/p\\//.test(item.href) ? 10 : 0)
                              + (/question\\/\\d+\\/answer\\//.test(item.href) ? 8 : 0)
                              + (item.text ? 1 : 0)
                          })).sort((a,b) => b.score - a.score).slice(0, 20);
                        }""",
                        title or "",
                    )
                    seen = {item.get("href") for item in candidates}
                    for item in found:
                        href = item.get("href")
                        if href and href not in seen and _is_public_content_url("zhihu", href):
                            candidates.append(item)
                            seen.add(href)
                    url = page.url
                    if url and url not in seen and _is_public_content_url("zhihu", url):
                        candidates.append({"href": url, "text": page.title() or "", "score": 5})
                        seen.add(url)
                    if title and title in text and candidates:
                        break
                except Exception:
                    continue
            screenshot = _try_screenshot(0, page)
            raw = _save_artifact(0, "zhihu_recover.html", html)
            _try_close_page(page)
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return {"status": "failed", "failure_reason": f"知乎URL回捞异常: {exc}", "candidates": candidates[:limit]}
    return {
        "status": "ready" if candidates else "not_found",
        "failure_reason": "" if candidates else "未从知乎页面回捞到候选作品URL",
        "candidates": candidates[:limit],
        "screenshot_path": screenshot,
        "raw_artifact_path": raw,
    }


def recover_toutiao_urls(account_name: str, title: str = "", limit: int = 8) -> dict:
    opened = open_publish_browser("toutiao", account_name or "default", "https://mp.toutiao.com/")
    if not opened.get("success"):
        return {"status": "failed", "failure_reason": opened.get("notes") or "今日头条浏览器未连接", "candidates": []}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return {"status": "failed", "failure_reason": f"Playwright 不可用: {exc}", "candidates": []}

    def add_links(found: list[dict], candidates: list[dict]) -> None:
        seen = {item.get("href") for item in candidates}
        title_terms = [term for term in ["住宿", "酒店", "步行街", "早餐", "推荐"] if term in (title or "")]
        for item in found or []:
            href = item.get("href")
            text = item.get("text") or ""
            matched_title = not title_terms or len([term for term in title_terms if term in text]) >= min(2, len(title_terms))
            matched_score = int(item.get("score") or 0) >= 20
            if href and href not in seen and _is_public_content_url("toutiao", href) and (matched_title or matched_score):
                candidates.append(item)
                seen.add(href)

    candidates = []
    screenshot = None
    raw = None
    html = ""
    clean_title = re.sub(r"\s+", "", title or "")
    search_title = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", clean_title) or clean_title
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{opened['port']}", timeout=3000)
            context, page = _new_page_from_cdp_context(browser, opened["port"] )
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(15000)
            homepage_targets = []
            try:
                page.goto("https://www.toutiao.com/", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(1800)
                homepage_targets = page.evaluate(
                    """() => Array.from(document.querySelectorAll('a[href]'))
                      .map(a => ({href:a.href,text:(a.innerText||a.textContent||'').trim()}))
                      .filter(x => /www\\.toutiao\\.com\\/c\\/user\\//.test(x.href) && /个人主页|高级蛋糕|主页/.test(x.text))
                      .map(x => x.href).slice(0, 3)"""
                ) or []
            except Exception:
                homepage_targets = []
            targets = [
                "https://mp.toutiao.com/profile_v4/manage/content/all",
                "https://mp.toutiao.com/profile_v4/content/manage",
                "https://mp.toutiao.com/profile_v4/content/article",
                *homepage_targets,
                *[url.split("?", 1)[0] + "?tab=article" for url in homepage_targets],
                "https://www.toutiao.com/search/?keyword=" + requests.utils.quote(search_title),
            ]
            for target in targets:
                try:
                    page.goto(target, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(2200)
                    _dismiss_publish_popups(page)
                    html = page.content()
                    text = _visible_text(page)
                    found = page.evaluate(
                        """(needle) => {
                          const clean = (value) => String(value || '').replace(/\\s+/g, '');
                          const titleProbe = clean(needle).slice(0, 16);
                          const links = Array.from(document.querySelectorAll('a[href]'))
                            .map(a => ({ href: (a.href || '').replace('/item/', '/article/'), text: (a.innerText || a.textContent || '').trim() }))
                            .filter(item => /toutiao\\.com|toutiaocdn\\.com/.test(item.href));
                          return links.map(item => {
                            const href = item.href || '';
                            const text = clean(item.text || '');
                            return {
                              ...item,
                              score: (titleProbe && (text.includes(titleProbe) || clean(href).includes(titleProbe)) ? 30 : 0)
                                + (/article|group\\/|item|weitoutiao/.test(href) ? 8 : 0)
                                + (item.text ? 1 : 0)
                            };
                          }).sort((a,b) => b.score - a.score).slice(0, 20);
                        }""",
                        title or "",
                    )
                    add_links(found, candidates)
                    url = page.url
                    if url and _is_public_content_url("toutiao", url):
                        add_links([{"href": url, "text": page.title() or "", "score": 5}], candidates)
                    if clean_title and clean_title[:10] in re.sub(r"\s+", "", text) and candidates:
                        break
                except Exception:
                    continue
            screenshot = _try_screenshot(0, page)
            raw = _save_artifact(0, "toutiao_recover.html", html)
            _try_close_page(page)
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return {"status": "failed", "failure_reason": f"今日头条URL回捞异常: {exc}", "candidates": candidates[:limit]}
    return {
        "status": "ready" if candidates else "not_found",
        "failure_reason": "" if candidates else "未从今日头条C端搜索/内容管理页回捞到候选作品URL",
        "candidates": candidates[:limit],
        "screenshot_path": screenshot,
        "raw_artifact_path": raw,
    }


def recover_sohu_urls(account_name: str, title: str = "", limit: int = 8) -> dict:
    opened = open_publish_browser("sohu", account_name or "default", "https://mp.sohu.com/")
    if not opened.get("success"):
        return {"status": "failed", "failure_reason": opened.get("notes") or "搜狐号浏览器未连接", "candidates": []}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return {"status": "failed", "failure_reason": f"Playwright 不可用: {exc}", "candidates": []}

    candidates = []
    screenshot = None
    raw = None
    html = ""
    clean_title = re.sub(r"\s+", "", title or "")
    title_probe = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", clean_title)[:16]
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{opened['port']}", timeout=3000)
            context, page = _new_page_from_cdp_context(browser, opened["port"] )
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(15000)
            targets = [
                "https://mp.sohu.com/mpfe/v4/contentManagement/news/list",
                "https://mp.sohu.com/mpfe/v4/contentManagement/news/moment",
                "https://mp.sohu.com/mpfe/v4/main/news/list",
                "https://mp.sohu.com/",
            ]
            status_hint = ""
            for target in targets:
                try:
                    page.goto(target, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(2200)
                    _dismiss_publish_popups(page)
                    html = page.content()
                    text = _visible_text(page)
                    compact_text = re.sub(r"\s+", "", text)
                    if any(marker in compact_text for marker in ["审核中", "待审核", "审核状态"]):
                        status_hint = "pending_review"
                    if any(marker in compact_text for marker in ["审核不通过", "未通过", "违规", "驳回"]):
                        status_hint = "rejected"
                    found = page.evaluate(
                        """(needle) => {
                          const clean = (value) => String(value || '').replace(/\\s+/g, '');
                          const probe = clean(needle || '').slice(0, 16);
                          const rows = Array.from(document.querySelectorAll('a[href]'))
                            .map(a => ({ href: a.href, text: (a.innerText || a.textContent || '').trim() }))
                            .filter(item => /sohu\\.com|mp\\.sohu\\.com/.test(item.href));
                          return rows.map(item => {
                            const href = item.href || '';
                            const text = clean(item.text || '');
                            return {
                              ...item,
                              score: (probe && (text.includes(probe) || clean(href).includes(probe)) ? 30 : 0)
                                + (/a\\//.test(href) || /sohu\\.com\\/a\\//.test(href) ? 10 : 0)
                                + (item.text ? 1 : 0)
                            };
                          }).sort((a,b) => b.score - a.score).slice(0, 20);
                        }""",
                        title or "",
                    ) or []
                    seen = {item.get("href") for item in candidates}
                    for item in found:
                        href = item.get("href")
                        item_text = item.get("text") or ""
                        matched = not title_probe or title_probe in re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", item_text)
                        if href and href not in seen and _is_public_content_url("sohu", href) and (matched or int(item.get("score") or 0) >= 10):
                            candidates.append(item)
                            seen.add(href)
                    current = page.url
                    if current and _is_public_content_url("sohu", current):
                        candidates.append({"href": current, "text": page.title() or "", "score": 5})
                    if candidates:
                        break
                except Exception:
                    continue
            screenshot = _try_screenshot(0, page)
            raw = _save_artifact(0, "sohu_recover.html", html)
            _try_close_page(page)
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return {"status": "failed", "failure_reason": f"搜狐号URL回捞异常: {exc}", "candidates": candidates[:limit]}
    if candidates:
        return {
            "status": "ready",
            "failure_reason": "",
            "candidates": candidates[:limit],
            "screenshot_path": screenshot,
            "raw_artifact_path": raw,
        }
    if status_hint == "pending_review":
        return {
            "status": "pending_review",
            "failure_reason": "搜狐号页面显示作品仍在审核中，暂未暴露公开URL",
            "candidates": [],
            "screenshot_path": screenshot,
            "raw_artifact_path": raw,
        }
    if status_hint == "rejected":
        return {
            "status": "rejected",
            "failure_reason": "搜狐号页面显示作品审核未通过",
            "candidates": [],
            "screenshot_path": screenshot,
            "raw_artifact_path": raw,
        }
    return {
        "status": "not_found",
        "failure_reason": "未从搜狐号内容管理页回捞到候选作品URL",
        "candidates": [],
        "screenshot_path": screenshot,
        "raw_artifact_path": raw,
    }


def verify_toutiao_published_page_with_browser(
    account_name: str,
    url: str,
    title: str = "",
    body: str = "",
    task_id: int = 0,
) -> dict:
    if not _is_public_content_url("toutiao", url):
        return {"verified": False, "failure_reason": "今日头条URL不是公开作品页"}
    opened = open_publish_browser("toutiao", account_name or "default", "https://www.toutiao.com/")
    if not opened.get("success"):
        return {"verified": False, "failure_reason": opened.get("notes") or "今日头条浏览器未连接"}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return {"verified": False, "failure_reason": f"Playwright 不可用: {exc}"}

    title_clean = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", title or "")
    html = ""
    screenshot = None
    raw = None
    matched_url = False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{opened['port']}", timeout=3000)
            context, page = _new_page_from_cdp_context(browser, opened["port"] )
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(15000)
            article_id = (re.search(r"/(?:article|item)/(\d+)/", url or "") or re.search(r"/w/(\d+)/", url or ""))
            article_id = article_id.group(1) if article_id else ""
            targets = ["https://mp.toutiao.com/profile_v4/manage/content/all"]
            try:
                page.goto("https://www.toutiao.com/", wait_until="domcontentloaded", timeout=12000)
                page.wait_for_timeout(1500)
                home_links = page.evaluate(
                    """() => Array.from(document.querySelectorAll('a[href]'))
                      .map(a => ({href:a.href,text:(a.innerText||a.textContent||'').trim()}))
                      .filter(x => /www\\.toutiao\\.com\\/c\\/user\\//.test(x.href) && /个人主页|主页|高级蛋糕/.test(x.text))
                      .map(x => x.href).slice(0, 3)"""
                ) or []
                targets.extend(home_links)
                targets.extend([item.split("?", 1)[0] + "?tab=article" for item in home_links])
            except Exception:
                pass
            visible = ""
            for target in targets:
                try:
                    page.goto(target, wait_until="domcontentloaded", timeout=12000)
                    page.wait_for_timeout(2600)
                    _dismiss_publish_popups(page)
                    visible = _visible_text(page)
                    html = page.content()
                    compact = re.sub(r"\s+", "", visible)
                    title_hit = not title_clean or any(probe and probe in compact for probe in [title_clean, title_clean[:14], title_clean[:10], title_clean[:8]])
                    url_hit = bool(article_id and article_id in html)
                    if title_hit and url_hit:
                        matched_url = True
                        break
                except Exception:
                    continue
            screenshot = _try_screenshot(task_id, page)
            raw = _save_artifact(task_id, "toutiao_published.html", html)
            _try_close_page(page)
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return {"verified": False, "failure_reason": f"今日头条登录态页面验真异常: {exc}"}
    plain = re.sub(r"\s+", "", visible)
    title_ok = not title_clean or any(probe and probe in plain for probe in [title_clean, title_clean[:14], title_clean[:10], title_clean[:8]])
    if not title_ok:
        return {
            "verified": False,
            "failure_reason": "今日头条登录态页面未匹配标题",
            "screenshot_path": screenshot,
            "raw_artifact_path": raw,
        }
    if not matched_url:
        return {
            "verified": False,
            "failure_reason": "今日头条登录态页面未匹配作品URL",
            "screenshot_path": screenshot,
            "raw_artifact_path": raw,
        }
    return {
        "verified": True,
        "failure_reason": "",
        "http_status": 200,
        "screenshot_path": screenshot,
        "raw_artifact_path": raw,
    }


def verify_published_page_url(url: str, title: str = "", body: str = "", platform: str = "") -> dict:
    if not url or not url.startswith(("http://", "https://")):
        return {"verified": False, "failure_reason": "发布URL不是有效http/https链接"}
    if platform and not _is_public_content_url(platform, url):
        return {"verified": False, "failure_reason": f"{platform}发布URL不是公开内容页"}
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 GeoOps/1.0"},
            timeout=12,
            allow_redirects=True,
        )
        status = int(response.status_code)
        text = response.text or ""
    except Exception as exc:
        return {"verified": False, "failure_reason": f"发布URL访问失败: {str(exc)[:120]}"}
    if status >= 400:
        return {"verified": False, "failure_reason": f"发布URL不可访问 HTTP {status}"}
    plain = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", html_lib.unescape(text)))
    html_text = html_lib.unescape(text)
    title_plain = re.sub(r"\s+", "", title or "")
    title_clean = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", title_plain)
    title_probes = [title_plain, title_clean]
    if platform == "ctrip_travel" and title_clean:
        title_probes.extend([title_clean[:12], title_clean[:8]])
        for sep in ["，", ",", "。", "！", "!", "？", "?"]:
            if sep in title_plain:
                part = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", title_plain.split(sep, 1)[0])
                if part:
                    title_probes.extend([part, part[:8]])
                break
    if platform == "toutiao" and title_clean:
        title_probes.extend([title_clean[:14], title_clean[:10], title_clean[:8]])
    if platform == "sohu" and title_clean:
        title_probes.extend([title_clean[:16], title_clean[:12], title_clean[:8]])
    title_ok = bool(not title or any(probe and (probe in html_text or probe in plain) for probe in title_probes))
    body_plain = re.sub(r"\s+", "", body or "")
    body_probes = [body_plain[:60]]
    if len(body_plain) > 160:
        body_probes.append(body_plain[80:140])
    body_ok = bool(not body_plain or any(probe and probe in plain for probe in body_probes))
    if platform == "ctrip_travel" and body_plain and not body_ok:
        ctrip_terms = []
        for term in ["住宿", "步行街", "早餐", "酒店", "推荐"]:
            if term in body_plain:
                ctrip_terms.append(term)
        body_ok = len([term for term in ctrip_terms if term in plain]) >= min(3, len(ctrip_terms) or 3)
    if platform == "toutiao" and body_plain and not body_ok:
        toutiao_terms = []
        for term in ["住宿", "酒店", "步行街", "地铁", "早餐"]:
            if term in body_plain:
                toutiao_terms.append(term)
        body_ok = len([term for term in toutiao_terms if term in plain]) >= min(4, len(toutiao_terms) or 4)
    if platform == "sohu" and body_plain and not body_ok:
        sohu_terms = []
        for term in ["住宿", "酒店", "步行街", "早餐", "推荐"]:
            if term in body_plain:
                sohu_terms.append(term)
        body_ok = len([term for term in sohu_terms if term in plain]) >= min(4, len(sohu_terms) or 4)
    if any(marker in plain[:1200] for marker in ["登录", "扫码", "验证码", "安全验证"]) and not title_ok:
        return {"verified": False, "failure_reason": "发布URL疑似登录/验证页"}
    if not title_ok:
        return {"verified": False, "failure_reason": "发布URL页面未匹配标题"}
    if body_plain and not body_ok:
        return {"verified": False, "failure_reason": "发布URL页面未匹配正文关键片段"}
    raw = _save_artifact(0, f"{platform or 'published'}_verify.html", text)
    screenshot = _make_text_image(0, f"{platform or '发布'} URL验真", f"{url}\nHTTP {status}\n标题匹配: {title_ok}\n正文匹配: {body_ok}")
    return {
        "verified": True,
        "failure_reason": "",
        "http_status": status,
        "raw_artifact_path": raw,
        "screenshot_path": screenshot,
    }


def discover_publish_selectors(platform: str, publish_url: str, login_url: str = "") -> dict:
    if not publish_url:
        return {"status": "failed", "failure_reason": "固定发布URL为空，无法识别选择器"}
    opened = open_publish_browser(platform, "workflow_default", login_url or publish_url)
    if not opened.get("success"):
        return {"status": "failed", "failure_reason": opened.get("notes") or "发布浏览器启动失败"}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return {"status": "failed", "failure_reason": f"Playwright 不可用: {exc}"}

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{opened['port']}", timeout=3000)
            context, page = _new_page_from_cdp_context(browser, opened["port"] )
            page.set_default_timeout(4000)
            page.set_default_navigation_timeout(12000)
            if publish_url and publish_url.split("?", 1)[0] not in (page.url or ""):
                page.goto(publish_url, wait_until="domcontentloaded", timeout=12000)
            page.wait_for_timeout(800)
            _dismiss_publish_popups(page)
            html = page.content()
            visible_text = _visible_text(page)
            screenshot = _try_screenshot(0, page)
            raw = _save_artifact(0, "selector_discovery.html", html)
            if any(word in visible_text for word in ["登录", "扫码", "验证码", "安全验证"]):
                _try_close_page(page)
                return {
                    "status": "login_required",
                    "failure_reason": "发布页要求登录或验证",
                    "screenshot_path": screenshot,
                    "raw_artifact_path": raw,
                    "selectors": {"title": [], "body": []},
                    "candidates": {"title": [], "body": []},
                }
            candidates = page.evaluate(
                """() => {
                  const cssEscape = window.CSS && CSS.escape ? CSS.escape : (v) => String(v).replace(/[^a-zA-Z0-9_-]/g, '\\\\$&');
                  const labelText = (el) => {
                    const aria = el.getAttribute('aria-label') || '';
                    const ph = el.getAttribute('placeholder') || '';
                    const dataPh = el.getAttribute('data-placeholder') || '';
                    const cls = el.getAttribute('class') || '';
                    const id = el.getAttribute('id') || '';
                    const role = el.getAttribute('role') || '';
                    return `${aria} ${ph} ${dataPh} ${cls} ${id} ${role} ${(el.innerText || '').slice(0, 80)}`;
                  };
                  const selectorFor = (el) => {
                    if (el.id) return `#${cssEscape(el.id)}`;
                    const ph = el.getAttribute('placeholder');
                    if (ph) return `${el.tagName.toLowerCase()}[placeholder="${ph.replace(/"/g, '\\\\"')}"]`;
                    const dataPh = el.getAttribute('data-placeholder');
                    if (dataPh) return `[data-placeholder="${dataPh.replace(/"/g, '\\\\"')}"]`;
                    if (el.getAttribute('aria-label')) return `[aria-label="${el.getAttribute('aria-label').replace(/"/g, '\\\\"')}"]`;
                    if (el.getAttribute('contenteditable') === 'true') return '[contenteditable="true"]';
                    if (el.getAttribute('role') === 'textbox') return '[role="textbox"]';
                    return el.tagName.toLowerCase();
                  };
                  const elements = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"], [role="textbox"]'))
                    .filter(el => {
                      const rect = el.getBoundingClientRect();
                      return rect.width > 20 && rect.height > 10 && getComputedStyle(el).visibility !== 'hidden';
                    })
                    .map(el => ({ selector: selectorFor(el), text: labelText(el), tag: el.tagName.toLowerCase() }));
                  const scoreTitle = (item) => /标题|title|题目|名称/.test(item.text) ? 10 : (/input/.test(item.tag) ? 3 : 0);
                  const scoreBody = (item) => /正文|内容|描述|文章|editor|content|body/.test(item.text) ? 10 : (/textarea|contenteditable|textbox/.test(item.selector + item.tag) ? 4 : 0);
                  const uniq = (items) => Array.from(new Map(items.map(item => [item.selector, item])).values());
                  return {
                    title: uniq(elements.map(item => ({...item, score: scoreTitle(item)})).filter(item => item.score > 0).sort((a,b) => b.score - a.score)).slice(0, 5),
                    body: uniq(elements.map(item => ({...item, score: scoreBody(item)})).filter(item => item.score > 0).sort((a,b) => b.score - a.score)).slice(0, 5)
                  };
                }"""
            )
            _try_close_page(page)
            selectors = {
                "title": [item["selector"] for item in (candidates.get("title") or [])[:1]],
                "body": [item["selector"] for item in (candidates.get("body") or [])[:1]],
            }
            status = "ready" if selectors["title"] and selectors["body"] else "manual_required"
            return {
                "status": status,
                "selectors": selectors,
                "candidates": candidates,
                "screenshot_path": screenshot,
                "raw_artifact_path": raw,
                "failure_reason": None if status == "ready" else "未识别到完整 title/body 选择器，请人工补充",
            }
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return {"status": "failed", "failure_reason": f"选择器识别异常: {exc}"}


def prepare_publish_browser(task: dict, content: dict, account: Optional[dict], workflow: Optional[dict] = None) -> PublishRpaResult:
    platform = task["platform"]
    if platform == "website":
        return _prepare_website_artifact(task, content)
    if platform == "ctrip_travel":
        return _publish_ctrip_travel(task, content, account, workflow)
    if platform == "zhihu":
        return _publish_zhihu(task, content, account, workflow)
    if platform == "toutiao":
        return _publish_toutiao(task, content, account, workflow)
    if platform == "sohu":
        return _publish_sohu_moment(task, content, account, workflow)
    if platform == "xiaohongshu":
        return _publish_xiaohongshu(task, content, account, workflow)
    if platform == "douyin":
        return _publish_douyin_image_note(task, content, account, workflow)
    if platform == "baijiahao":
        return _publish_baijiahao(task, content, account, workflow)

    account_name = (account or {}).get("account_name") or "default"
    login_url = (account or {}).get("login_url") or ""
    publish_url = (account or {}).get("publish_url") or login_url
    opened = open_publish_browser(platform, account_name, login_url)
    if not opened.get("success"):
        return PublishRpaResult(status="failed", failure_reason=opened.get("notes") or "发布浏览器启动失败")

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return PublishRpaResult(status="failed", failure_reason=f"Playwright 不可用: {exc}")

    draft_html = _content_html(content["title"], content["body"])
    local_html = _save_artifact(task["id"], "html", draft_html)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{opened['port']}", timeout=3000)
            context, page = _new_page_from_cdp_context(browser, opened["port"] )
            page.set_default_timeout(4000)
            page.set_default_navigation_timeout(8000)
            # External publishing is RPA-assisted: open the fixed publishing
            # URL, try known editor selectors, then require a human final click.
            # Success is never counted until the real published URL is filled
            # back and verified by the API.
            target_url = publish_url or f"file://{local_html}"
            page.goto(target_url, wait_until="domcontentloaded", timeout=10000)
            page.wait_for_timeout(500)
            _dismiss_publish_popups(page)
            _apply_workflow_steps(page, workflow)
            html = page.content()
            visible_text = _visible_text(page)

            if any(word in visible_text for word in ["登录", "扫码", "验证码", "安全验证"]) and login_url:
                screenshot = _try_screenshot(task["id"], page)
                raw = _save_artifact(task["id"], "page.html", html)
                _try_close_page(page)
                summary = _make_publish_summary(task, content, raw, screenshot, "login_required")
                return PublishRpaResult(
                    status="login_required",
                    notes="发布账号需要先登录或处理验证",
                    screenshot_path=screenshot,
                    raw_artifact_path=raw,
                    summary_artifact_path=summary,
                    failure_reason="发布账号未确认登录态",
                )

            filled = False
            if publish_url:
                filled = _try_fill_editor(page, content["title"], content["body"], (workflow or {}).get("selectors") or {})
            page.wait_for_timeout(300)
            html = page.content()
            visible_text = _visible_text(page)
            screenshot = _try_screenshot(task["id"], page)
            raw = _save_artifact(task["id"], "page.html", html)
            preflight = _publish_preflight_issue(platform, visible_text)
            result_status = "manual_required" if (preflight or (not filled and publish_url)) else "prepared"
            summary = _make_publish_summary(task, content, raw, screenshot, result_status)
            _try_close_page(page)
            if preflight:
                return PublishRpaResult(
                    status="manual_required",
                    notes="已填充草稿，但发布前必填项未完成",
                    screenshot_path=screenshot,
                    raw_artifact_path=raw,
                    summary_artifact_path=summary,
                    failure_reason=preflight,
                )
            if not filled and publish_url:
                return PublishRpaResult(
                    status="manual_required",
                    notes="已打开固定发布URL，但未识别到可自动填充的编辑器；请人工复制草稿发布，系统已保存页面证据",
                    screenshot_path=screenshot,
                    raw_artifact_path=raw,
                    summary_artifact_path=summary,
                    failure_reason="发布页面选择器未固化或页面结构变化",
                )
            return PublishRpaResult(
                status="prepared",
                notes="已打开固定发布URL并保存RPA证据；请在发布平台账号中人工确认发布" + ("；已尝试填充编辑器" if filled else ""),
                screenshot_path=screenshot,
                raw_artifact_path=raw,
                summary_artifact_path=summary,
            )
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return PublishRpaResult(status="failed", failure_reason=f"发布 RPA 异常: {exc}", raw_artifact_path=local_html)


def _publish_preflight_issue(platform: str, visible_text: str) -> Optional[str]:
    text = visible_text or ""
    if platform == "baijiahao":
        if "请添加封面" in text or "选择封面" in text and "编辑\n更换" not in text:
            return "百家号发布前必填项未完成：请添加封面"
        if "请选择地点" in text or "请选择时间" in text:
            return "百家号来源说明已勾选但未完成时间/地点；请人工补全或取消来源说明"
    return None


def _publish_baijiahao(task: dict, content: dict, account: Optional[dict], workflow: Optional[dict] = None) -> PublishRpaResult:
    """百家号图文发布 RPA：打开文章编辑器，填充标题/正文/封面，声明 AI 生成，保留人工发布确认。"""
    account_name = (account or {}).get("account_name") or "workflow_default"
    login_url = (account or {}).get("login_url") or "https://baijiahao.baidu.com/"
    publish_url = (account or {}).get("publish_url") or "https://baijiahao.baidu.com/builder/rc/edit?type=news"
    opened = open_publish_browser("baijiahao", account_name, login_url)
    if not opened.get("success"):
        return PublishRpaResult(status="failed", failure_reason=opened.get("notes") or "百家号发布浏览器启动失败")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return PublishRpaResult(status="failed", failure_reason=f"Playwright 不可用: {exc}")

    draft_html = _content_html(content.get("title") or "", content.get("body") or "")
    local_html = _save_artifact(task["id"], "html", draft_html)
    media_paths = _resolve_media_paths(content, task, workflow, min_count=1)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{opened['port']}", timeout=3000)
            context, page = _cdp_context_and_page(browser, opened["port"], publish_url or login_url)
            page.on("dialog", lambda dialog: dialog.accept())
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(12000)
            try:
                page.goto(publish_url, wait_until="domcontentloaded", timeout=12000)
            except Exception:
                page.goto(login_url, wait_until="domcontentloaded", timeout=12000)
            page.wait_for_timeout(2200)
            _dismiss_publish_popups(page)
            _click_text_if_present(page, "知道了", exact=True)
            _click_text_if_present(page, "暂不", exact=False)
            visible_text = _visible_text(page)
            if _baijiahao_requires_login(visible_text):
                screenshot = _try_screenshot(task["id"], page)
                raw = _save_artifact(task["id"], "page.html", page.content())
                summary = _make_publish_summary(task, content, raw, screenshot, "login_required")
                return PublishRpaResult(
                    status="login_required",
                    notes="百家号账号需要先登录或处理验证",
                    screenshot_path=screenshot,
                    raw_artifact_path=raw,
                    summary_artifact_path=summary,
                    failure_reason="百家号发布页要求登录或验证",
                )
            _baijiahao_enter_article_editor(page)
            # 新版 Lexical 标题编辑器水合要 5 秒上下：填早了会被水合用服务端
            # 草稿整体覆盖（2026-10-12 实测任务填完 2 秒后被打回旧标题）。
            # 先等标题元素出现再沉淀，填完再校验，被覆盖就重填一次。
            try:
                page.wait_for_selector(
                    '[data-testid="news-title-input"] [contenteditable="true"]', timeout=8000
                )
            except Exception:
                pass
            page.wait_for_timeout(1500)
            title_ok = _baijiahao_fill_title(page, content.get("title") or "")
            body_ok = _baijiahao_fill_body(page, content.get("body") or "")
            if title_ok and (content.get("title") or "")[:12] not in _visible_text(page):
                page.wait_for_timeout(1200)
                title_ok = _baijiahao_fill_title(page, content.get("title") or "")
            if body_ok and (content.get("body") or "")[:16] not in _visible_text_with_frames(page):
                page.wait_for_timeout(800)
                body_ok = _baijiahao_fill_body(page, content.get("body") or "")
            cover_paths = _content_media_file_paths(content, min_count=1)
            cover_ok = _upload_baijiahao_cover(page, cover_paths[0] if cover_paths else None)
            ai_ok = _prepare_baijiahao_declarations(page)
            page.wait_for_timeout(1200)
            accepted_ok, accepted_missing = _baijiahao_validate_required_fields(
                page, content.get("title") or "", content.get("body") or ""
            )
            missing = []
            if not title_ok:
                missing.append("标题未填入")
            if not body_ok:
                missing.append("正文未填入")
            if not cover_ok:
                missing.append("封面未上传")
            if not ai_ok:
                missing.append("AI生成声明未勾选")
            missing.extend([item for item in accepted_missing if item not in missing])
            screenshot = _try_screenshot(task["id"], page)
            raw = _save_artifact(task["id"], "page.html", page.content())
            result_status = "manual_required" if missing or not accepted_ok else "prepared"
            summary = _make_publish_summary(task, content, raw, screenshot, result_status)
            if missing or not accepted_ok:
                return PublishRpaResult(
                    status="manual_required",
                    notes="百家号文章发布页已打开并保存证据，但必填项未全部完成",
                    screenshot_path=screenshot,
                    raw_artifact_path=raw,
                    summary_artifact_path=summary,
                    failure_reason="；".join(dict.fromkeys(missing)),
                )
            return PublishRpaResult(
                status="prepared",
                notes="百家号文章草稿已填入标题/正文/封面并声明AI生成；请人工点击发布，发布后回填URL验真",
                screenshot_path=screenshot,
                raw_artifact_path=raw,
                summary_artifact_path=summary,
            )
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return PublishRpaResult(
            status="failed",
            raw_artifact_path=local_html,
            summary_artifact_path=_make_publish_summary(task, content, local_html, None, "failed"),
            failure_reason=f"百家号发布 RPA 异常: {exc}",
        )


def _baijiahao_requires_login(visible_text: str) -> bool:
    text = visible_text or ""
    editor_indicators = ["发布文章", "图文", "标题", "正文", "文章管理"]
    if any(ind in text for ind in editor_indicators) and ("标题" in text or "正文" in text):
        return False
    login_signals = ["登录", "扫码", "验证码", "安全验证", "手机号登录", "账号登录", "短信登录"]
    return any(word in text for word in login_signals)


def _baijiahao_enter_article_editor(page) -> bool:
    try:
        if "builder/rc/content" in (page.url or ""):
            return True
        if _click_text_if_present(page, "发布文章", exact=True):
            page.wait_for_timeout(1500)
            return True
        if _click_role_button_if_present(page, "发布文章"):
            page.wait_for_timeout(1500)
            return True
        return True
    except Exception:
        return True


def _baijiahao_fill_title(page, title: str) -> bool:
    value = (title or "").strip()[:50] or "（无标题）"
    selectors = [
        "input#article-title",
        "textarea#article-title",
        # 2026-10 编辑器改版：标题是 Lexical contenteditable（无 id/placeholder），
        # 仅可靠锚点是 data-testid="news-title-input"。
        "[data-testid='news-title-input'] [contenteditable='true']",
        "[data-lexical-editor='true'][contenteditable='true']",
        "input[placeholder*='标题']",
        "textarea[placeholder*='标题']",
        "[contenteditable='true'][data-placeholder*='标题']",
        "[contenteditable='true'][placeholder*='标题']",
    ]
    for selector in selectors:
        try:
            target = _resolve_locator(page, selector)
            if target and target.is_visible(timeout=1200):
                _fill_editor_locator(target, value)
                return True
        except Exception:
            continue
    return False


def _baijiahao_fill_body(page, body: str) -> bool:
    value = (body or "").strip() or "（无正文）"
    selectors = [
        ("frame", "#ueditor_0", "body"),
        ("frame", "iframe[src*='ueditor']", "body"),
        ".editor-content [contenteditable='true']",
        ".ProseMirror[contenteditable='true']",
        ".ql-editor[contenteditable='true']",
        "textarea[placeholder*='正文']",
        "[contenteditable='true'][data-placeholder*='正文']",
        "[contenteditable='true'][placeholder*='正文']",
        "[contenteditable='true'][placeholder*='内容']",
        "div[role='textbox']",
        "[contenteditable='true']",
    ]
    for selector in selectors:
        try:
            if isinstance(selector, tuple) and selector[0] == "frame":
                _, frame_sel, inner_sel = selector
                target = page.frame_locator(frame_sel).first.locator(inner_sel)
            else:
                target = _resolve_locator(page, selector)
            if target and target.is_visible(timeout=1200):
                _fill_editor_locator(target, value)
                return True
        except Exception:
            continue
    return False


def _baijiahao_validate_required_fields(page, title: str, body: str) -> tuple[bool, list[str]]:
    missing = []
    visible = _visible_text_with_frames(page) or ""
    title_text = (title or "（无标题）").strip()[:12]
    body_text = (body or "（无正文）").strip()[:20]
    if title_text not in visible:
        missing.append("标题未在页面中识别到")
    if body_text not in visible:
        missing.append("正文未在页面中识别到")
    if "编辑" not in visible and "更换" not in visible and "封面" in visible:
        missing.append("封面未设置")
    ai_state = _get_checkbox_state_by_label(page, "采用AI生成内容")
    if ai_state is not True:
        missing.append("AI生成声明未勾选")
    return (not missing), list(dict.fromkeys(missing))


def _publish_sohu_moment(task: dict, content: dict, account: Optional[dict], workflow: Optional[dict] = None) -> PublishRpaResult:
    account_name = (account or {}).get("account_name") or "workflow_default"
    login_url = (account or {}).get("login_url") or "https://mp.sohu.com/"
    publish_url = (account or {}).get("publish_url") or "https://mp.sohu.com/mpfe/v4/contentManagement/news/addmoment"
    opened = open_publish_browser("sohu", account_name, login_url)
    if not opened.get("success"):
        return PublishRpaResult(status="failed", failure_reason=opened.get("notes") or "搜狐号发布浏览器启动失败")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return PublishRpaResult(status="failed", failure_reason=f"Playwright 不可用: {exc}")

    draft_html = _content_html(content.get("title") or "", content.get("body") or "")
    local_html = _save_artifact(task["id"], "html", draft_html)
    media_paths = _resolve_media_paths(content, task, workflow, min_count=1)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{opened['port']}", timeout=3000)
            context, page = _cdp_context_and_page(browser, opened["port"], publish_url or login_url)
            page.on("dialog", lambda dialog: dialog.accept())
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(12000)
            try:
                page.goto(publish_url, wait_until="domcontentloaded", timeout=12000)
            except Exception:
                page.goto(login_url, wait_until="domcontentloaded", timeout=12000)
            page.wait_for_timeout(1800)
            _dismiss_publish_popups(page)
            _click_text_if_present(page, "知道了", exact=True)
            _click_text_if_present(page, "暂不", exact=False)
            _click_text_if_present(page, "暂不认证", exact=True)
            # 实名认证引导弹窗（el-dialog + v-modal 遮罩）每次进页都会弹：
            # Playwright 常规点击被遮罩拦截，必须 JS 直点「暂不认证」。
            try:
                page.evaluate(
                    """() => {
                      const els = Array.from(document.querySelectorAll('button, span, div, a'))
                        .filter(el => (el.innerText || '').trim() === '暂不认证');
                      if (els.length) { els[els.length - 1].click(); return true; }
                      return false;
                    }"""
                )
                page.wait_for_timeout(900)
            except Exception:
                pass
            _click_text_if_present(page, "动态", exact=True)
            page.wait_for_timeout(800)
            visible_text = _visible_text(page)
            if _sohu_requires_login(visible_text):
                screenshot = _try_screenshot(task["id"], page)
                raw = _save_artifact(task["id"], "page.html", page.content())
                summary = _make_publish_summary(task, content, raw, screenshot, "login_required")
                return PublishRpaResult(
                    status="login_required",
                    notes="搜狐号账号需要先登录或处理验证",
                    screenshot_path=screenshot,
                    raw_artifact_path=raw,
                    summary_artifact_path=summary,
                    failure_reason="搜狐号发布页要求登录或验证",
                )
            title_ok = _sohu_fill_title(page, content.get("title") or "")
            body_ok = _sohu_fill_body(page, content.get("body") or "")
            image_ok = _sohu_upload_image(page, media_paths[0] if media_paths else None)
            # 声明选项文案：现行页面为「无需声明」，旧版为「无特别声明」。
            source_ok = (
                _click_label_near_text(page, "无需声明")
                or _click_label_near_text(page, "无特别声明")
                or "无需声明" in _visible_text(page)
                or "无特别声明" in _visible_text(page)
            )
            page.wait_for_timeout(1200)
            accepted_ok, missing = _sohu_validate_required_fields(page, content.get("title") or "", content.get("body") or "")
            if not title_ok:
                missing.append("标题未填入")
            if not body_ok:
                missing.append("正文未填入")
            if not image_ok:
                missing.append("图片未上传")
            if not source_ok:
                missing.append("信息来源未选择无特别声明")
            screenshot = _try_screenshot(task["id"], page)
            raw = _save_artifact(task["id"], "page.html", page.content())
            status = "manual_required" if missing or not accepted_ok else "prepared"
            summary = _make_publish_summary(task, content, raw, screenshot, status)
            if missing or not accepted_ok:
                return PublishRpaResult(
                    status="manual_required",
                    notes="搜狐号动态发布页已打开并保存证据，但必填项未全部完成",
                    screenshot_path=screenshot,
                    raw_artifact_path=raw,
                    summary_artifact_path=summary,
                    failure_reason="；".join(dict.fromkeys(missing)),
                )
            return PublishRpaResult(
                status="prepared",
                notes="搜狐号动态草稿已填入标题/正文/图片/信息来源；请人工点击发布，发布后回填URL验真",
                screenshot_path=screenshot,
                raw_artifact_path=raw,
                summary_artifact_path=summary,
            )
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return PublishRpaResult(
            status="failed",
            raw_artifact_path=local_html,
            summary_artifact_path=_make_publish_summary(task, content, local_html, None, "failed"),
            failure_reason=f"搜狐号发布 RPA 异常: {exc}",
            )


def _sohu_requires_login(visible_text: str) -> bool:
    text = visible_text or ""
    if "小狐狸" in text or "退出" in text or "发布\n存草稿\n预览" in text:
        return False
    return any(word in text for word in ["扫码登录", "账号登录", "手机登录", "验证码", "安全验证"])


def _publish_xiaohongshu(task: dict, content: dict, account: Optional[dict], workflow: Optional[dict] = None) -> PublishRpaResult:
    account_name = (account or {}).get("account_name") or "default"
    login_url = (account or {}).get("login_url") or "https://creator.xiaohongshu.com/"
    # Align with social-auto-upload: from=homepage&target=image
    publish_url = (
        (account or {}).get("publish_url")
        or "https://creator.xiaohongshu.com/publish/publish?from=homepage&target=image"
    )
    opened = open_publish_browser("xiaohongshu", account_name, login_url)
    if not opened.get("success"):
        return PublishRpaResult(status="failed", failure_reason=opened.get("notes") or "小红书发布浏览器启动失败")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return PublishRpaResult(status="failed", failure_reason=f"Playwright 不可用: {exc}")

    media_paths = _resolve_media_paths(content, task, workflow, min_count=1)
    local_html = _save_artifact(task["id"], "html", _content_html(content.get("title") or "", content.get("body") or ""))
    if not media_paths:
        return PublishRpaResult(
            status="manual_required",
            raw_artifact_path=local_html,
            summary_artifact_path=_make_publish_summary(task, content, local_html, None, "manual_required"),
            failure_reason="缺少可用图片素材；请先上传图片后再运行小红书 RPA（工作流测试除外）",
        )
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{opened['port']}", timeout=3000)
            context = browser.contexts[0] if browser.contexts else None
            page = _select_existing_page(context, publish_url) if context else None
            page = page or _new_page_from_cdp_context(browser, opened["port"], publish_url or login_url)[1]
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(18000)
            page.goto(publish_url, wait_until="domcontentloaded", timeout=18000)
            try:
                page.bring_to_front()
            except Exception:
                pass
            page.wait_for_timeout(2500)
            _dismiss_publish_popups(page)
            visible_text = _visible_text(page)
            if _xiaohongshu_requires_login(visible_text):
                screenshot = _try_screenshot(task["id"], page)
                raw = _save_artifact(task["id"], "page.html", page.content())
                summary = _make_publish_summary(task, content, raw, screenshot, "login_required")
                return PublishRpaResult(
                    status="login_required",
                    notes="小红书账号需要先登录或处理验证",
                    screenshot_path=screenshot,
                    raw_artifact_path=raw,
                    summary_artifact_path=summary,
                    failure_reason="小红书发布页要求登录或验证",
                )
            image_ok = _xiaohongshu_upload_images(page, media_paths)
            title_ok = _xiaohongshu_fill_title(page, content.get("title") or "")
            body_ok = _xiaohongshu_fill_body(page, content.get("body") or "")
            page.wait_for_timeout(1200)
            accepted_ok, missing = _xiaohongshu_validate_required_fields(page, content.get("title") or "", content.get("body") or "")
            if not image_ok and "图片未被页面真实识别" not in missing:
                missing.append("图片未被页面真实识别")
            if not title_ok and "标题未被页面真实识别" not in missing:
                missing.append("标题未被页面真实识别")
            if not body_ok and "正文未被页面真实识别" not in missing:
                missing.append("正文未被页面真实识别")
            status = "prepared" if accepted_ok and not missing else "manual_required"
            html = page.content()
            screenshot = _try_screenshot(task["id"], page)
            raw = _save_artifact(task["id"], "page.html", html)
            summary = _make_publish_summary(task, content, raw, screenshot, status)
            try:
                page.bring_to_front()
            except Exception:
                pass
            if missing:
                return PublishRpaResult(
                    status="manual_required",
                    notes="小红书图文草稿已尝试填充，但仍有必填项需人工确认",
                    screenshot_path=screenshot,
                    raw_artifact_path=raw,
                    summary_artifact_path=summary,
                    failure_reason="；".join(missing),
                )
            return PublishRpaResult(
                status="prepared",
                notes="小红书图文草稿已填入图片、标题和正文；系统未点击最终发布",
                screenshot_path=screenshot,
                raw_artifact_path=raw,
                summary_artifact_path=summary,
            )
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return PublishRpaResult(
            status="failed",
            raw_artifact_path=local_html,
            summary_artifact_path=_make_publish_summary(task, content, local_html, None, "failed"),
            failure_reason=f"小红书发布 RPA 异常: {exc}",
        )


def _xiaohongshu_requires_login(visible_text: str) -> bool:
    text = visible_text or ""
    if "发布笔记" in text and ("上传图文" in text or "上传图片" in text):
        return False
    return any(word in text for word in ["扫码登录", "验证码登录", "手机号登录", "安全验证", "登录后"])


def _xiaohongshu_upload_images(page, media_paths: list[str]) -> bool:
    paths = [path for path in media_paths if path and Path(path).exists()]
    if not paths:
        paths = [_test_upload_image_path()]
    try:
        upload_input = page.locator('input[type="file"][accept*="image"]').first
        if upload_input.count() <= 0:
            upload_input = page.locator("div[class^='upload-content'] input.upload-input").first
        if upload_input.count() > 0:
            upload_input.wait_for(state="attached", timeout=15000)
            upload_input.set_input_files(paths[:9])
        else:
            with page.expect_file_chooser(timeout=5000) as chooser:
                page.get_by_text("上传图片", exact=False).first.click(timeout=3000, force=True)
            chooser.value.set_files(paths[:9])
        # Wait for editor ready (OSS: 填写标题 visible) instead of fixed sleep only
        for _ in range(20):
            try:
                title_loc = page.locator('input[placeholder*="填写标题"]').first
                if title_loc.count() > 0 and title_loc.is_visible(timeout=800):
                    break
            except Exception:
                pass
            page.wait_for_timeout(1000)
        else:
            page.wait_for_timeout(2500)
    except Exception:
        if not _upload_first_file_input(page, paths[0], wait_ms=5500):
            return False
    try:
        return bool(page.evaluate(
            """() => {
              const text = document.body && document.body.innerText || '';
              const images = document.querySelectorAll('img').length;
              return images > 0 || /重新上传|删除|更换|编辑|封面|下一步|发布|填写标题/.test(text);
            }"""
        ))
    except Exception:
        return False


def _xiaohongshu_fill_title(page, title: str) -> bool:
    value = (title or "").strip()[:20] or "本地住宿推荐"
    selectors = [
        'input[placeholder*="填写标题"]',
        'input[placeholder*="标题"]',
        'textarea[placeholder*="标题"]',
        '[contenteditable="true"][data-placeholder*="标题"]',
    ]
    return _fill_first_matching(page, selectors, value, value[:8])


def _xiaohongshu_fill_body(page, body: str) -> bool:
    value = (body or "").strip()[:950]
    selectors = [
        'p[data-placeholder*="输入正文描述"]',
        'textarea[placeholder*="正文描述"]',
        'textarea[placeholder*="输入正文"]',
        '.tiptap.ProseMirror[contenteditable="true"]',
        '.ProseMirror[contenteditable="true"]',
        '[contenteditable="true"][data-placeholder*="正文"]',
        '[contenteditable="true"][data-placeholder*="内容"]',
        'textarea[placeholder*="正文"]',
        'textarea[placeholder*="描述"]',
    ]
    if _fill_first_matching(page, selectors, value, value[:16]):
        return True
    try:
        rect = page.evaluate(
            """() => {
              const nodes = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], p[data-placeholder], div'));
              const target = nodes.find(el => {
                const text = (el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || el.innerText || el.textContent || '').trim();
                const r = el.getBoundingClientRect();
                return /正文|描述|分享/.test(text) && r.width > 300 && r.height > 80;
              });
              if (!target) return null;
              const r = target.getBoundingClientRect();
              return {x: r.left + 24, y: r.top + 24};
            }"""
        )
        if rect:
            page.mouse.click(rect["x"], rect["y"])
            page.keyboard.press(_select_all_shortcut())
            page.keyboard.press("Backspace")
            page.keyboard.type(value, delay=6)
            page.wait_for_timeout(800)
            return bool(page.evaluate("(probe) => (document.body.innerText || '').replace(/\\s+/g,'').includes(probe)", re.sub(r"\s+", "", value)[:16]))
    except Exception:
        pass
    return False


def _fill_first_matching(page, selectors: list[str], value: str, probe: str) -> bool:
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() <= 0:
                continue
            try:
                loc.click(timeout=2500, force=True)
            except Exception:
                pass
            # force 点击被悬浮层吃掉时输入框不会聚焦，后续键盘输入全部落空
            # （2026-10-12 小红书标题实测）：显式 JS focus 兜底。
            loc.evaluate("(el) => el.focus()")
            page.keyboard.press(_select_all_shortcut())
            page.keyboard.press("Backspace")
            page.keyboard.type(value, delay=10)
            page.wait_for_timeout(600)
            current = loc.evaluate("(el) => (el.value || el.innerText || el.textContent || '').trim()")
            if (probe or value[:8]) in (current or ""):
                return True
        except Exception:
            continue
    return False


def _xiaohongshu_validate_required_fields(page, title: str, body: str) -> tuple[bool, list[str]]:
    title_probe = re.sub(r"\s+", "", (title or "").strip())[:8]
    body_probe = re.sub(r"\s+", "", (body or "").strip())[:16]
    try:
        state = page.evaluate(
            """([titleProbe, bodyProbe]) => {
              const clean = (value) => String(value || '').replace(/\\s+/g, '');
              const fields = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"]'))
                .map(el => el.value || el.innerText || el.textContent || '').join('\\n');
              const text = document.body && document.body.innerText || '';
              return {
                titleOk: !titleProbe || clean(fields).includes(titleProbe),
                bodyOk: !bodyProbe || clean(fields).includes(bodyProbe),
                imageOk: document.querySelectorAll('img').length > 0 || /重新上传|删除|更换|编辑|封面/.test(text),
                publishOk: /发布|存草稿|草稿/.test(text)
              };
            }""",
            [title_probe, body_probe],
        )
    except Exception:
        return False, ["小红书页面验收脚本执行失败"]
    missing = []
    if not state.get("imageOk"):
        missing.append("图片未被页面真实识别")
    if not state.get("titleOk"):
        missing.append("标题未被页面真实识别")
    if not state.get("bodyOk"):
        missing.append("正文未被页面真实识别")
    if not state.get("publishOk"):
        missing.append("发布区域未被页面真实识别")
    return not missing, missing


def _publish_douyin_image_note(task: dict, content: dict, account: Optional[dict], workflow: Optional[dict] = None) -> PublishRpaResult:
    account_name = (account or {}).get("account_name") or "workflow_default"
    login_url = (account or {}).get("login_url") or "https://creator.douyin.com/"
    # Align with social-auto-upload: land on /upload then click「发布图文」
    publish_url = (
        (account or {}).get("publish_url")
        or (workflow or {}).get("publish_url")
        or "https://creator.douyin.com/creator-micro/content/upload"
    )
    opened = open_publish_browser("douyin", account_name, login_url)
    if not opened.get("success"):
        return PublishRpaResult(status="failed", failure_reason=opened.get("notes") or "抖音发布浏览器启动失败")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return PublishRpaResult(status="failed", failure_reason=f"Playwright 不可用: {exc}")

    draft_html = _content_html(content.get("title") or "", content.get("body") or "")
    local_html = _save_artifact(task["id"], "html", draft_html)
    media_paths = _resolve_media_paths(content, task, workflow, min_count=1)
    if not media_paths:
        return PublishRpaResult(
            status="manual_required",
            raw_artifact_path=local_html,
            summary_artifact_path=_make_publish_summary(task, content, local_html, None, "manual_required"),
            failure_reason="缺少可用图片素材；请先上传图片后再运行抖音 RPA（工作流测试除外）",
        )
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{opened['port']}", timeout=3000)
            context = browser.contexts[0] if browser.contexts else None
            page = _select_existing_page(context, publish_url) if context else None
            page = page or _new_page_from_cdp_context(browser, opened["port"], publish_url or login_url)[1]
            page.on("dialog", lambda dialog: dialog.accept())
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(18000)
            try:
                page.goto(publish_url, wait_until="domcontentloaded", timeout=18000)
            except Exception:
                page.goto(login_url, wait_until="domcontentloaded", timeout=12000)
            try:
                page.bring_to_front()
            except Exception:
                pass
            page.wait_for_timeout(2500)
            _dismiss_publish_popups(page)
            visible_text = _visible_text(page)
            if _douyin_requires_login(visible_text):
                screenshot = _try_screenshot(task["id"], page)
                raw = _save_artifact(task["id"], "page.html", page.content())
                summary = _make_publish_summary(task, content, raw, screenshot, "login_required")
                return PublishRpaResult(
                    status="login_required",
                    notes="抖音账号需要先登录或处理验证",
                    screenshot_path=screenshot,
                    raw_artifact_path=raw,
                    summary_artifact_path=summary,
                    failure_reason="抖音创作者中心要求登录或验证",
                )

            _dismiss_native_file_dialog(page)
            image_ok = _douyin_upload_images(page, media_paths)
            _dismiss_native_file_dialog(page)
            title_ok = _douyin_fill_title(page, content.get("title") or "")
            _dismiss_native_file_dialog(page)
            body_ok = _douyin_fill_body(page, content.get("body") or "")
            _dismiss_native_file_dialog(page)
            cover_ok = _douyin_select_cover(page)
            _dismiss_native_file_dialog(page)
            declaration_ok = _douyin_select_ai_declaration(page)
            if not declaration_ok:
                declaration_ok = _douyin_force_select_ai_declaration(page)
            _dismiss_native_file_dialog(page)
            save_permission_ok = _douyin_select_save_permission(page, allow=False)
            _dismiss_native_file_dialog(page)
            publish_time_ok = _douyin_select_publish_time(page)
            page.wait_for_timeout(1200)
            if "请选择自主声明" in _visible_text(page):
                declaration_ok = _douyin_force_select_ai_declaration(page) or declaration_ok
                page.wait_for_timeout(700)
            accepted_ok, missing = _douyin_validate_required_fields(page, content.get("title") or "", content.get("body") or "")
            if not image_ok:
                missing.append("图片/图文素材未上传")
            if not title_ok:
                missing.append("作品标题未填入")
            if not body_ok:
                missing.append("作品简介/正文未填入")
            if not cover_ok:
                missing.append("封面未设置")
            if not declaration_ok:
                missing.append("自主声明未选择")
            if not save_permission_ok:
                missing.append("保存权限未选择不允许")
            if not publish_time_ok:
                missing.append("发布时间未确认")
            missing = list(dict.fromkeys(missing))
            status = "prepared" if accepted_ok and not missing else "manual_required"
            screenshot = _try_screenshot(task["id"], page)
            raw = _save_artifact(task["id"], "page.html", page.content())
            summary = _make_publish_summary(task, content, raw, screenshot, status)
            try:
                page.bring_to_front()
            except Exception:
                pass
            if missing:
                return PublishRpaResult(
                    status="manual_required",
                    notes="抖音图文草稿已尝试填充，但仍有必填项需人工确认",
                    screenshot_path=screenshot,
                    raw_artifact_path=raw,
                    summary_artifact_path=summary,
                    failure_reason="；".join(missing),
                )
            return PublishRpaResult(
                status="prepared",
                notes="抖音图文草稿已填入图片、标题、正文、封面、自主声明、保存权限和发布时间；系统未点击最终发布",
                screenshot_path=screenshot,
                raw_artifact_path=raw,
                summary_artifact_path=summary,
            )
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return PublishRpaResult(
            status="failed",
            raw_artifact_path=local_html,
            summary_artifact_path=_make_publish_summary(task, content, local_html, None, "failed"),
            failure_reason=f"抖音图文发布 RPA 异常: {exc}",
        )


def _douyin_requires_login(visible_text: str) -> bool:
    text = visible_text or ""
    if any(marker in text for marker in ["作品描述", "添加作品简介", "设置封面", "发布设置", "发布图文", "上传视频"]):
        return False
    # Avoid bare「登录」false positives on creator chrome UI text
    return any(word in text for word in ["扫码登录", "手机号登录", "验证码登录", "安全验证", "登录后继续"])


def _douyin_upload_images(page, media_paths: list[str]) -> bool:
    paths = [path for path in media_paths if path and Path(path).exists()]
    if not paths:
        paths = [_test_upload_image_path()]
    try:
        # OSS flow: click exact「发布图文」then set_input_files on image accept input
        try:
            page.get_by_text("发布图文", exact=True).first.click(timeout=4000, force=True)
            page.wait_for_timeout(1000)
        except Exception:
            for label in ["图文", "上传图文", "上传图片"]:
                if _click_text_if_present(page, label, exact=False):
                    page.wait_for_timeout(800)
                    break
        image_input = page.locator("div[class^='container'] input[accept*='image']").first
        if image_input.count() <= 0:
            image_input = page.locator('input[type="file"][accept*="image"]').first
        if image_input.count() > 0:
            image_input.set_input_files(paths[:18])
        else:
            file_inputs = page.locator('input[type="file"]')
            if file_inputs.count() > 0:
                file_inputs.first.set_input_files(paths[:18])
            else:
                with page.expect_file_chooser(timeout=5000) as chooser:
                    page.get_by_text("上传", exact=False).first.click(timeout=3000, force=True)
                chooser.value.set_files(paths[:18])
        _dismiss_native_file_dialog(page)
        # Wait until editor route appears (OSS: **/creator-micro/content/post/image?**)
        entered = False
        for _ in range(24):
            try:
                if "content/post/image" in (page.url or ""):
                    entered = True
                    break
                page.wait_for_url("**/creator-micro/content/post/image**", timeout=2500)
                entered = True
                break
            except Exception:
                page.wait_for_timeout(500)
        if not entered:
            page.wait_for_timeout(3500)
        _dismiss_native_file_dialog(page)
    except Exception:
        _dismiss_native_file_dialog(page)
        if not _upload_first_file_input(page, paths[0], wait_ms=6500):
            return False
        _dismiss_native_file_dialog(page)
    try:
        text = _visible_text(page)
        images = page.evaluate("() => document.querySelectorAll('img, video, canvas').length")
        on_editor = "content/post/image" in (page.url or "")
        return on_editor or bool(images and images > 0) or any(
            marker in text for marker in ["重新上传", "设置封面", "选择封面", "填写作品标题", "预览封面/标题"]
        )
    except Exception:
        return False


def _dismiss_native_file_dialog(page=None) -> None:
    if page is not None:
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
        except Exception:
            pass
    if sys.platform != "darwin":
        return
    scripts = [
        'tell application "System Events" to key code 53',
        (
            'tell application "System Events"\n'
            '  repeat with p in (processes whose visible is true)\n'
            '    try\n'
            '      repeat with w in windows of p\n'
            '        try\n'
            '          if exists button "取消" of w then click button "取消" of w\n'
            '        end try\n'
            '        try\n'
            '          if exists button "Cancel" of w then click button "Cancel" of w\n'
            '        end try\n'
            '      end repeat\n'
            '    end try\n'
            '  end repeat\n'
            'end tell'
        ),
    ]
    for script in scripts:
        try:
            subprocess.run(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        except Exception:
            pass
        time.sleep(0.15)


def _douyin_title(title: str) -> str:
    value = re.sub(r"\s+", "", (title or "").strip())
    value = re.sub(r"https?://\S+", "", value)
    # OSS 图文标题上限 20（视频可为 30）
    return (value or "本地住宿攻略")[:20]


def _douyin_fill_title(page, title: str) -> bool:
    value = _douyin_title(title)
    selectors = [
        'input[placeholder*="填写作品标题"]',
        'input[placeholder*="作品标题"]',
        'textarea[placeholder*="作品标题"]',
        'input[placeholder*="标题"]',
        'textarea[placeholder*="标题"]',
    ]
    return _fill_first_matching(page, selectors, value, value[:8])


def _douyin_fill_body(page, body: str) -> bool:
    value = re.sub(r"https?://[^\s，。；、)）]+", "", (body or "").strip())[:1000]
    if "\n#" in value:
        value = value.replace("\n#", "\n\n#")
    # Prefer visible zone-container only (hotel/OSS); avoid hidden contenteditables
    try:
        zone = page.locator('div.zone-container[contenteditable="true"]').first
        if zone.count() > 0 and zone.is_visible(timeout=1500):
            _fill_editor_locator(zone, value)
            current = zone.evaluate("(el) => (el.value || el.innerText || el.textContent || '').trim()")
            if value[:16] in (current or ""):
                return True
    except Exception:
        pass
    selectors = [
        'div.zone-container[contenteditable="true"]',
        '[data-slate-editor="true"][contenteditable="true"]',
        '.editor-comp-publish[contenteditable="true"]',
        'textarea[placeholder*="作品简介"]',
        'textarea[placeholder*="简介"]',
        'textarea[placeholder*="描述"]',
        '[contenteditable="true"][data-placeholder*="作品简介"]',
        '[contenteditable="true"][data-placeholder*="简介"]',
        '[contenteditable="true"][data-placeholder*="描述"]',
    ]
    if _fill_first_matching(page, selectors, value, value[:16]):
        return True
    try:
        page_text = page.evaluate("() => (document.body && document.body.innerText || '').replace(/\\s+/g, '')")
        if re.sub(r"\s+", "", value)[:16] in (page_text or ""):
            return True
    except Exception:
        pass
    return False


def _douyin_select_cover(page) -> bool:
    try:
        text = _visible_text(page)
        if any(marker in text for marker in ["封面检测通过", "编辑封面", "选择一张图片作为封面", "好封面会吸引更多人浏览作品"]):
            return True
        for label in ["选择封面", "设置封面"]:
            try:
                loc = page.get_by_text(label, exact=False).first
                if loc.count() <= 0:
                    continue
                loc.click(timeout=2500, force=True)
                page.wait_for_timeout(1200)
                for ok_label in ["确定", "完成", "保存"]:
                    try:
                        page.get_by_role("button", name=ok_label).last.click(timeout=1800, force=True)
                        page.wait_for_timeout(1800)
                        break
                    except Exception:
                        try:
                            page.get_by_text(ok_label, exact=True).last.click(timeout=1800, force=True)
                            page.wait_for_timeout(1800)
                            break
                        except Exception:
                            continue
                return True
            except Exception:
                continue
        return any(marker in text for marker in ["竖封面", "横封面", "好封面会吸引更多人浏览作品", "编辑封面"])
    except Exception:
        return False


def _douyin_select_ai_declaration(page) -> bool:
    try:
        text = _visible_text(page)
        if "自主声明" not in text:
            return True
        if "请选择自主声明" not in text and any(marker in text for marker in ["AI生成", "AI合成", "无特别声明", "原创"]):
            return True
        for _ in range(3):
            page.evaluate(
                """() => {
                  const clean = (value) => String(value || '').replace(/\\s+/g, '');
                  const target = Array.from(document.querySelectorAll('div, span, input, button, [role="button"]'))
                    .find(el => clean(el.innerText || el.textContent || el.value || '').includes('请选择自主声明'));
                  if (target) {
                    const y = target.getBoundingClientRect().top + window.scrollY - Math.max(180, window.innerHeight * 0.22);
                    window.scrollTo(0, Math.max(0, y));
                  } else {
                    window.scrollTo(0, document.body.scrollHeight * 0.62);
                  }
                }"""
            )
            page.wait_for_timeout(500)
            if _douyin_click_declaration_select(page):
                page.wait_for_timeout(1200)
            elif _click_text_if_present(page, "请选择自主声明", exact=False) or _click_text_if_present(page, "自主声明", exact=False):
                page.wait_for_timeout(1200)
            if _douyin_choose_ai_declaration_modal(page):
                return True
            if "服务器/网络开小差" in _visible_text(page):
                page.wait_for_timeout(1800)
        for label in ["AI生成", "AI合成", "含AI", "无特别声明", "原创", "不声明"]:
            if _click_text_if_present(page, label, exact=False) or _douyin_click_option_near_text(page, label):
                page.wait_for_timeout(600)
                return True
        return "请选择自主声明" not in _visible_text(page)
    except Exception:
        return False


def _douyin_click_declaration_select(page) -> bool:
    try:
        target = page.evaluate(
            """() => {
              const clean = (value) => String(value || '').replace(/\\s+/g, '');
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
              };
              const nodes = Array.from(document.querySelectorAll('div, span, input, button, [role="button"]'))
                .filter(el => visible(el) && clean(el.innerText || el.textContent || el.value || '').includes('请选择自主声明'));
              for (const node of nodes) {
                let current = node;
                const chain = [];
                while (current && current !== document.body && chain.length < 8) {
                  chain.push(current);
                  current = current.parentElement;
                }
                const candidates = chain
                  .filter(el => visible(el))
                  .map(el => ({el, rect: el.getBoundingClientRect()}))
                  .filter(item => item.rect.width >= 240 && item.rect.height >= 28 && item.rect.height <= 90)
                  .sort((a, b) => (a.rect.height - b.rect.height) || (a.rect.width - b.rect.width));
                const picked = candidates[0] || {el: node, rect: node.getBoundingClientRect()};
                const rect = picked.el.getBoundingClientRect();
                if (rect.top < 0 || rect.bottom > window.innerHeight) {
                  const y = window.scrollY + rect.top - Math.max(180, window.innerHeight * 0.22);
                  window.scrollTo(0, Math.max(0, y));
                  const next = picked.el.getBoundingClientRect();
                  return {
                    x: next.left + Math.min(next.width - 18, Math.max(24, next.width * 0.45)),
                    y: next.top + next.height / 2
                  };
                }
                return {
                  x: rect.left + Math.min(rect.width - 18, Math.max(24, rect.width * 0.45)),
                  y: rect.top + rect.height / 2
                };
              }
              return null;
            }"""
        )
        if not target:
            return False
        page.mouse.move(float(target["x"]), float(target["y"]))
        page.wait_for_timeout(120)
        page.mouse.down()
        page.wait_for_timeout(80)
        page.mouse.up()
        page.wait_for_timeout(900)
        return True
    except Exception:
        return False


def _douyin_select_save_permission(page, allow: bool = False) -> bool:
    label = "允许" if allow else "不允许"
    try:
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight * 0.72)")
        page.wait_for_timeout(300)
        return _douyin_click_option_in_section(page, "保存权限", label) or _set_radio_by_label(page, label) or _click_label_near_text(page, label)
    except Exception:
        return False


def _douyin_select_publish_time(page) -> bool:
    try:
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight * 0.8)")
        page.wait_for_timeout(300)
        if _douyin_click_option_in_section(page, "发布时间", "立即发布") or _set_radio_by_label(page, "立即发布") or _click_label_near_text(page, "立即发布"):
            return True
        return "立即发布" in _visible_text(page)
    except Exception:
        return False


def _douyin_choose_ai_declaration_modal(page) -> bool:
    try:
        opened = bool(page.evaluate(
            """() => {
              const text = document.body && document.body.innerText || '';
              return text.includes('对作品内容添加声明') && text.includes('内容由AI生成');
            }"""
        ))
        if not opened:
            return False
        option = page.evaluate(
            """() => {
              const clean = (value) => String(value || '').replace(/\\s+/g, '');
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < window.innerHeight && r.right > 0 && r.left < window.innerWidth && s.visibility !== 'hidden' && s.display !== 'none';
              };
              const nodes = Array.from(document.querySelectorAll('label, div, span'))
                .filter(el => visible(el) && clean(el.innerText || el.textContent || '') === '内容由AI生成');
              const labels = nodes.map(el => el.closest('label') || el).filter(visible);
              const target = labels
                .sort((a, b) => {
                  const ar = a.getBoundingClientRect();
                  const br = b.getBoundingClientRect();
                  return (ar.height - br.height) || (ar.width - br.width);
                })[0];
              if (!target) return null;
              const rect = target.getBoundingClientRect();
              return {x: rect.left + 18, y: rect.top + rect.height / 2};
            }"""
        )
        if not option:
            return False
        page.mouse.move(float(option["x"]), float(option["y"]))
        page.mouse.click(float(option["x"]), float(option["y"]))
        page.wait_for_timeout(500)
        confirm = page.evaluate(
            """() => {
              const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
              const target = buttons.reverse().find(el => (el.innerText || el.textContent || '').trim() === '确定');
              if (!target) return null;
              const rect = target.getBoundingClientRect();
              return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
            }"""
        )
        if not confirm:
            return False
        page.mouse.move(float(confirm["x"]), float(confirm["y"]))
        page.mouse.click(float(confirm["x"]), float(confirm["y"]))
        page.wait_for_timeout(900)
        text = _visible_text(page)
        return "请选择自主声明" not in text
    except Exception:
        return False


def _douyin_force_select_ai_declaration(page) -> bool:
    try:
        if "请选择自主声明" not in _visible_text(page):
            return True
        for _ in range(2):
            page.evaluate("() => window.scrollTo(0, 260)")
            page.wait_for_timeout(400)
            box = page.evaluate(
                """() => {
                  const clean = (value) => String(value || '').replace(/\\s+/g, '');
                  const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth && s.display !== 'none' && s.visibility !== 'hidden';
                  };
                  const nodes = Array.from(document.querySelectorAll('div, span, input, button, [role="button"]'))
                    .filter(el => visible(el) && clean(el.innerText || el.textContent || el.value || '').includes('请选择自主声明'));
                  const el = nodes.find(el => String(el.className || '').includes('selectBox')) || nodes[0];
                  if (!el) return null;
                  const r = el.getBoundingClientRect();
                  return {x: r.left + r.width * 0.45, y: r.top + r.height / 2};
                }"""
            )
            if not box:
                continue
            page.mouse.click(float(box["x"]), float(box["y"]))
            page.wait_for_timeout(1200)
            if _douyin_choose_ai_declaration_modal(page):
                return True
        return "请选择自主声明" not in _visible_text(page)
    except Exception:
        return False


def _douyin_click_option_near_text(page, option_text: str) -> bool:
    try:
        return bool(page.evaluate(
            """(optionText) => {
              const nodes = Array.from(document.querySelectorAll('label, button, div, span'));
              const target = nodes.find(el => (el.innerText || el.textContent || '').trim().includes(optionText));
              if (!target) return false;
              const rect = target.getBoundingClientRect();
              const x = rect.left + Math.min(Math.max(rect.width / 2, 8), Math.max(rect.width - 4, 8));
              const y = rect.top + rect.height / 2;
              const clickTarget = document.elementFromPoint(x, y) || target;
              clickTarget.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, clientX: x, clientY: y}));
              clickTarget.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, clientX: x, clientY: y}));
              clickTarget.dispatchEvent(new MouseEvent('click', {bubbles: true, clientX: x, clientY: y}));
              return true;
            }""",
            option_text,
        ))
    except Exception:
        return False


def _douyin_click_option_in_section(page, section_text: str, option_text: str) -> bool:
    try:
        clicked = page.evaluate(
            """([sectionText, optionText]) => {
              const clean = (value) => String(value || '').replace(/\\s+/g, '');
              const sections = Array.from(document.querySelectorAll('div, section'))
                .filter(el => clean(el.innerText || el.textContent || '').includes(sectionText) && clean(el.innerText || el.textContent || '').includes(optionText))
                .sort((a, b) => (a.getBoundingClientRect().height || 9999) - (b.getBoundingClientRect().height || 9999));
              const root = sections[0] || document.body;
              const options = Array.from(root.querySelectorAll('label, button, div, span'))
                .filter(el => clean(el.innerText || el.textContent || '').includes(optionText));
              const target = options[0];
              if (!target) return false;
              const rect = target.getBoundingClientRect();
              const x = rect.left + Math.min(24, Math.max(8, rect.width / 2));
              const y = rect.top + rect.height / 2;
              const clickTarget = document.elementFromPoint(x, y) || target;
              clickTarget.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, clientX: x, clientY: y}));
              clickTarget.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, clientX: x, clientY: y}));
              clickTarget.dispatchEvent(new MouseEvent('click', {bubbles: true, clientX: x, clientY: y}));
              return true;
            }""",
            [section_text, option_text],
        )
        page.wait_for_timeout(500)
        return bool(clicked)
    except Exception:
        return False


def _douyin_validate_required_fields(page, title: str, body: str) -> tuple[bool, list[str]]:
    title_probe = re.sub(r"\s+", "", _douyin_title(title))[:8]
    body_probe = re.sub(r"\s+", "", re.sub(r"https?://[^\s，。；、)）]+", "", (body or "").strip()))[:16]
    try:
        state = page.evaluate(
            """([titleProbe, bodyProbe]) => {
              const clean = (value) => String(value || '').replace(/\\s+/g, '');
              const text = document.body && document.body.innerText || '';
              const fieldText = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"], [role="textbox"], [data-slate-editor="true"]'))
                .map(el => el.value || el.innerText || el.textContent || '').join('\\n');
              const pageText = document.body && document.body.innerText || '';
              const mediaOk = document.querySelectorAll('img, video, canvas').length > 0 || /重新上传|选择封面|设置封面/.test(text);
              const publishButtons = Array.from(document.querySelectorAll('button, [role="button"]'))
                .filter(el => /发布/.test(el.innerText || el.textContent || ''))
                .map(el => ({
                  disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true' || /disabled/.test(String(el.className || '').toLowerCase()),
                  rect: (() => { const r = el.getBoundingClientRect(); return {w:r.width,h:r.height}; })()
                }))
                .filter(item => item.rect.w > 0 && item.rect.h > 0);
              return {
                mediaOk,
                titleOk: !titleProbe || clean(fieldText).includes(titleProbe),
                bodyOk: !bodyProbe || clean(fieldText).includes(bodyProbe) || clean(pageText).includes(bodyProbe),
                coverOk: /封面|编辑封面|选择一张图片作为封面/.test(text),
                declarationOk: !/请选择自主声明/.test(text),
                savePermissionOk: /保存权限[\\s\\S]{0,120}(不允许|允许)/.test(text),
                publishTimeOk: /立即发布|定时发布/.test(text),
                publishOk: publishButtons.length > 0 && publishButtons.some(item => !item.disabled)
              };
            }""",
            [title_probe, body_probe],
        )
    except Exception:
        return False, ["抖音页面验收脚本执行失败"]
    missing = []
    if not state.get("mediaOk"):
        missing.append("图文素材未被页面真实识别")
    if not state.get("titleOk"):
        missing.append("标题未被页面真实识别")
    if not state.get("bodyOk"):
        missing.append("正文未被页面真实识别")
    if not state.get("coverOk"):
        missing.append("封面区域未被页面真实识别")
    if not state.get("declarationOk"):
        missing.append("自主声明未完成")
    if not state.get("savePermissionOk"):
        missing.append("保存权限未完成")
    if not state.get("publishTimeOk"):
        missing.append("发布时间未完成")
    if not state.get("publishOk"):
        missing.append("发布按钮不可用")
    return not missing, missing


def _sohu_fill_title(page, title: str) -> bool:
    value = (title or "").strip()[:72] or "本地住宿推荐"
    selectors = [
        'input[placeholder*="标题"]',
        'textarea[placeholder*="标题"]',
        'input#title',
        'input[name="title"]',
        '[contenteditable="true"][placeholder*="标题"]',
        '[contenteditable="true"][data-placeholder*="标题"]',
    ]
    for selector in selectors:
        try:
            loc = _resolve_locator(page, selector)
            if not loc or not loc.is_visible(timeout=1200):
                continue
            _fill_editor_locator(loc, value)
            current = loc.evaluate("(el) => (el.value || el.innerText || el.textContent || '').trim()")
            if value[:10] in (current or ""):
                return True
        except Exception:
            continue
    return False


def _sohu_fill_body(page, body: str) -> bool:
    value = (body or "").strip()[:4500]
    selectors = [
        ".ql-editor[contenteditable='true']",
        ".ProseMirror[contenteditable='true']",
        ".tiptap.ProseMirror",
        "textarea[placeholder*='正文']",
        "textarea[placeholder*='内容']",
        "[contenteditable='true'][data-placeholder*='正文']",
        "[contenteditable='true'][data-placeholder*='内容']",
        "[contenteditable='true']",
    ]
    for selector in selectors:
        try:
            loc = _resolve_locator(page, selector)
            if not loc or not loc.is_visible(timeout=1200):
                continue
            _fill_editor_locator(loc, value)
            current = loc.evaluate("(el) => (el.innerText || el.textContent || '').trim()")
            if value[:20] in (current or ""):
                return True
        except Exception:
            continue
    return False


def _sohu_upload_image(page, image_path: str) -> bool:
    try:
        if page.evaluate("() => document.querySelectorAll('img').length") > 0 and "上传图片" not in _visible_text(page)[:500]:
            return True
    except Exception:
        pass
    # 实名认证引导弹窗随时会盖在编辑器上（el-dialog__wrapper + v-modal），
    # Playwright 点击会被拦截；先 JS 直点「暂不认证」关掉再操作。
    try:
        page.evaluate(
            """() => {
              const els = Array.from(document.querySelectorAll('button, span, div, a'))
                .filter(el => (el.innerText || '').trim() === '暂不认证');
              if (els.length) { els[els.length - 1].click(); return true; }
              return false;
            }"""
        )
        page.wait_for_timeout(700)
    except Exception:
        pass
    try:
        with page.expect_file_chooser(timeout=5000) as chooser:
            page.get_by_text("上传图片", exact=False).first.click(timeout=3000, force=True)
        chooser.value.set_files(image_path)
        page.wait_for_timeout(4500)
    except Exception:
        # 动态上传框：avatar-uploader 组件只在被点击后才创建 input[type=file]，
        # 且 JS 直点可绕过弹窗遮罩（2026-10-12 实测：JS click 后 input 出现）。
        injected = False
        try:
            injected = bool(page.evaluate(
                """() => {
                  const box = document.querySelector('.avatar-uploader .avatar-box')
                    || document.querySelector('.avatar-uploader');
                  if (!box) return false;
                  box.click();
                  return true;
                }"""
            ))
        except Exception:
            injected = False
        if injected:
            try:
                page.wait_for_selector("input[type='file']", timeout=4000)
                page.locator("input[type='file']").last.set_input_files(image_path)
                page.wait_for_timeout(4500)
            except Exception:
                pass
        if "上传成功" not in _visible_text(page)[:2000] and page.evaluate("() => document.querySelectorAll('img').length") == 0:
            if not _upload_first_file_input(page, image_path, wait_ms=4500):
                return False
    try:
        for label in ["确定", "完成", "保存"]:
            try:
                page.get_by_role("button", name=label).last.click(timeout=1800, force=True)
                page.wait_for_timeout(2200)
                break
            except Exception:
                try:
                    page.get_by_text(label, exact=True).last.click(timeout=1800, force=True)
                    page.wait_for_timeout(2200)
                    break
                except Exception:
                    continue
        text = _visible_text(page)
        return any(marker in text for marker in ["上传成功", "（1/1）", "(1/1)", "编辑\n删除", "编辑 删除"]) or bool(page.evaluate("() => document.querySelectorAll('img').length > 0"))
    except Exception:
        return False


def _sohu_validate_required_fields(page, title: str, body: str) -> tuple[bool, list[str]]:
    title_probe = re.sub(r"\s+", "", (title or "").strip())[:10]
    body_probe = re.sub(r"\s+", "", (body or "").strip())[:30]
    try:
        state = page.evaluate(
            """([titleProbe, bodyProbe]) => {
              const clean = (value) => String(value || '').replace(/\\s+/g, '');
              const titleValue = Array.from(document.querySelectorAll('input[placeholder*="标题"]')).map(el => el.value || '').join('\\n');
              const bodyValue = Array.from(document.querySelectorAll('.ql-editor,[contenteditable="true"]')).map(el => el.innerText || el.textContent || '').join('\\n');
              const text = document.body && document.body.innerText || '';
              return {
                titleOk: !titleProbe || clean(titleValue).includes(titleProbe),
                bodyOk: !bodyProbe || clean(bodyValue).includes(bodyProbe),
                imageOk: text.includes('上传成功') || text.includes('（1/1）') || text.includes('(1/1)') || (text.includes('编辑') && text.includes('删除')) || document.querySelectorAll('img').length > 0,
                sourceOk: text.includes('无需声明') || text.includes('无特别声明'),
                publishOk: text.includes('发布') && text.includes('存草稿') && text.includes('预览')
              };
            }""",
            [title_probe, body_probe],
        )
    except Exception:
        return False, ["搜狐号页面验收脚本执行失败"]
    missing = []
    if not state.get("titleOk"):
        missing.append("标题未被页面真实识别")
    if not state.get("bodyOk"):
        missing.append("正文未被页面真实识别")
    if not state.get("imageOk"):
        missing.append("图片未被页面真实识别")
    if not state.get("sourceOk"):
        missing.append("信息来源未确认")
    if not state.get("publishOk"):
        missing.append("发布区域未被页面真实识别")
    return not missing, missing


def _publish_toutiao(task: dict, content: dict, account: Optional[dict], workflow: Optional[dict] = None) -> PublishRpaResult:
    account_name = (account or {}).get("account_name") or "workflow_default"
    login_url = (account or {}).get("login_url") or "https://mp.toutiao.com/"
    publish_url = (account or {}).get("publish_url") or "https://mp.toutiao.com/profile_v4/graphic/publish"
    opened = open_publish_browser("toutiao", account_name, login_url)
    if not opened.get("success"):
        return PublishRpaResult(status="failed", failure_reason=opened.get("notes") or "今日头条发布浏览器启动失败")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return PublishRpaResult(status="failed", failure_reason=f"Playwright 不可用: {exc}")

    draft_html = _content_html(content.get("title") or "", content.get("body") or "")
    local_html = _save_artifact(task["id"], "html", draft_html)
    media_paths = _resolve_media_paths(content, task, workflow, min_count=1)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{opened['port']}", timeout=3000)
            context, page = _cdp_context_and_page(browser, opened["port"], publish_url or login_url)
            page.on("dialog", lambda dialog: dialog.accept())
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(12000)
            try:
                page.goto(publish_url, wait_until="domcontentloaded", timeout=12000)
            except Exception:
                page.goto(login_url, wait_until="domcontentloaded", timeout=12000)
            page.wait_for_timeout(1500)
            _dismiss_publish_popups(page)
            _click_text_if_present(page, "知道了", exact=True)
            _click_text_if_present(page, "暂不", exact=False)
            visible_text = _visible_text(page)
            if any(word in visible_text for word in ["登录", "扫码", "验证码", "安全验证"]) and "发布文章" not in visible_text:
                screenshot = _try_screenshot(task["id"], page)
                raw = _save_artifact(task["id"], "page.html", page.content())
                summary = _make_publish_summary(task, content, raw, screenshot, "login_required")
                return PublishRpaResult(
                    status="login_required",
                    notes="今日头条账号需要先登录或处理验证",
                    screenshot_path=screenshot,
                    raw_artifact_path=raw,
                    summary_artifact_path=summary,
                    failure_reason="今日头条发布页要求登录或验证",
                )
            _toutiao_enter_article_editor(page)
            filled_title = _toutiao_fill_title(page, content.get("title") or "")
            filled_body = _toutiao_fill_body(page, content.get("body") or "")
            cover_ok = _toutiao_upload_cover(page, media_paths[0] if media_paths else None)
            # 2026-10 发布页改版后单篇广告开关可能已移除：只有页面表单里
            # 确实存在广告选项而未选中时才算缺项（与工作流测试同口径）。
            ad_option_present = False
            try:
                ad_option_present = bool(page.evaluate(
                    """() => /广告/.test(Array.from(document.querySelectorAll(
                      'label, [class*="radio" i], [class*="checkbox" i], [class*="form-item" i]'
                    )).map(el => el.innerText || '').join(' '))"""
                ))
            except Exception:
                ad_option_present = False
            ad_ok = _toutiao_select_no_ad(page) if ad_option_present else True
            first_ok = _toutiao_check_first_publish(page)
            statement_ok = _toutiao_select_statement(page)
            page.wait_for_timeout(1200)
            accepted_ok, accepted_missing = _toutiao_validate_required_fields(
                page,
                content.get("title") or "",
                content.get("body") or "",
            )
            visible_text = _visible_text(page)
            missing = []
            if not filled_title:
                missing.append("标题未填入")
            if not filled_body:
                missing.append("正文未填入")
            if not cover_ok:
                missing.append("单图封面未上传")
            if not ad_ok:
                missing.append("投放广告未选择不投放")
            if not first_ok:
                missing.append("首发声明未勾选")
            if not statement_ok:
                missing.append("作品声明个人观点未选择")
            missing.extend([item for item in accepted_missing if item not in missing])
            screenshot = _try_screenshot(task["id"], page)
            raw = _save_artifact(task["id"], "page.html", page.content())
            result_status = "manual_required" if missing or not accepted_ok else "prepared"
            summary = _make_publish_summary(task, content, raw, screenshot, result_status)
            if missing or not accepted_ok:
                return PublishRpaResult(
                    status="manual_required",
                    notes="今日头条发布页已打开并保存证据，但必填项未全部完成",
                    screenshot_path=screenshot,
                    raw_artifact_path=raw,
                    summary_artifact_path=summary,
                    failure_reason="；".join(missing),
                )
            return PublishRpaResult(
                status="prepared",
                notes="今日头条草稿已填入标题/正文/单图封面/不投放广告/首发声明/个人观点；请人工点击预览并发布，发布后回填URL验真",
                screenshot_path=screenshot,
                raw_artifact_path=raw,
                summary_artifact_path=summary,
            )
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return PublishRpaResult(
            status="failed",
            raw_artifact_path=local_html,
            summary_artifact_path=_make_publish_summary(task, content, local_html, None, "failed"),
            failure_reason=f"今日头条发布 RPA 异常: {exc}",
        )


def _publish_zhihu(task: dict, content: dict, account: Optional[dict], workflow: Optional[dict] = None) -> PublishRpaResult:
    account_name = (account or {}).get("account_name") or "default"
    login_url = (account or {}).get("login_url") or "https://www.zhihu.com/"
    opened = open_publish_browser("zhihu", account_name, login_url)
    if not opened.get("success"):
        return PublishRpaResult(status="failed", failure_reason=opened.get("notes") or "知乎发布浏览器启动失败")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return PublishRpaResult(status="failed", failure_reason=f"Playwright 不可用: {exc}")

    draft_html = _content_html(content.get("title") or "", content.get("body") or "")
    local_html = _save_artifact(task["id"], "html", draft_html)
    media_paths = _content_media_file_paths(content, min_count=1)
    if not media_paths:
        return PublishRpaResult(
            status="manual_required",
            raw_artifact_path=local_html,
            summary_artifact_path=_make_publish_summary(task, content, local_html, None, "manual_required"),
            failure_reason="知乎图文发布需要封面图片；当前内容没有可用图片素材",
            notes="请先在内容生产页为该内容上传至少 1 张图片素材",
        )

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{opened['port']}", timeout=3000)
            context, page = _new_page_from_cdp_context(browser, opened["port"] )
            page.on("dialog", lambda dialog: dialog.accept())
            page.set_default_timeout(4500)
            page.set_default_navigation_timeout(10000)
            try:
                page.goto("https://zhuanlan.zhihu.com/write", wait_until="domcontentloaded", timeout=10000)
            except Exception:
                page.goto("https://www.zhihu.com/", wait_until="commit", timeout=6000)
            page.wait_for_timeout(1000)
            _dismiss_publish_popups(page)
            visible_text = _visible_text(page)
            if any(word in visible_text for word in ["登录", "扫码", "验证码", "安全验证"]) and "写文章" not in visible_text:
                screenshot = _try_screenshot(task["id"], page)
                raw = _save_artifact(task["id"], "page.html", page.content())
                summary = _make_publish_summary(task, content, raw, screenshot, "login_required")
                return PublishRpaResult(
                    status="login_required",
                    notes="知乎账号需要先登录或处理验证",
                    screenshot_path=screenshot,
                    raw_artifact_path=raw,
                    summary_artifact_path=summary,
                    failure_reason="知乎发布页要求登录或验证",
                )

            opened_editor = _zhihu_open_article_editor(page)
            page.wait_for_timeout(1000)
            filled_title = _zhihu_fill_title(page, content.get("title") or "")
            filled_body = _zhihu_fill_body(page, content.get("body") or "")
            uploaded_cover = _zhihu_upload_cover(page, media_paths[0] if media_paths else None)
            # 填充过程的按键可能误触"插入注释"等编辑器弹窗（快捷键N），收尾再清一次，
            # 别把打开的模态框留给人工确认环节
            _dismiss_publish_popups(page)
            publish_ready = _zhihu_ensure_publish_ready(page, content.get("title") or "")
            # ensure_publish_ready 的键盘操作也可能再触发弹窗，截图前最后清一次
            _dismiss_publish_popups(page)
            page.wait_for_timeout(800)
            html = page.content()
            screenshot = _try_screenshot(task["id"], page)
            raw = _save_artifact(task["id"], "page.html", html)
            missing = []
            if not opened_editor:
                missing.append("未进入写文章编辑页")
            if not filled_title:
                missing.append("标题未填入")
            if not filled_body:
                missing.append("正文未填入")
            if not uploaded_cover:
                missing.append("封面未上传")
            if not publish_ready:
                missing.append("发布按钮未变亮，请缩短标题或检查必填项")
            summary = _make_publish_summary(task, content, raw, screenshot, "manual_required" if missing else "prepared")
            if missing:
                return PublishRpaResult(
                    status="manual_required",
                    notes="知乎发布页已打开并保存证据，但必填项未全部完成",
                    screenshot_path=screenshot,
                    raw_artifact_path=raw,
                    summary_artifact_path=summary,
                    failure_reason="；".join(missing),
                )
            return PublishRpaResult(
                status="prepared",
                notes="知乎草稿已填入标题、正文和封面；请人工检查后点击发布，再回捞验真URL",
                screenshot_path=screenshot,
                raw_artifact_path=raw,
                summary_artifact_path=summary,
            )
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return PublishRpaResult(status="failed", failure_reason=f"知乎发布 RPA 异常: {exc}", raw_artifact_path=local_html)


def _zhihu_open_article_editor(page) -> bool:
    try:
        if page.locator('textarea[placeholder*="标题"], .public-DraftEditor-content, [contenteditable="true"]').count() >= 2:
            return True
    except Exception:
        pass
    for text in ["写文章", "文章"]:
        try:
            page.get_by_text(text, exact=True).first.click(timeout=2500, force=True)
            page.wait_for_timeout(2200)
            if page.locator('textarea[placeholder*="标题"], .public-DraftEditor-content, [contenteditable="true"]').count() >= 2:
                return True
        except Exception:
            continue
    try:
        page.goto("https://zhuanlan.zhihu.com/write", wait_until="domcontentloaded", timeout=10000)
        page.wait_for_timeout(1800)
        return page.locator('textarea[placeholder*="标题"], .public-DraftEditor-content, [contenteditable="true"]').count() >= 2
    except Exception:
        return False


def _zhihu_fill_title(page, title: str) -> bool:
    value = _zhihu_safe_title(title)
    try:
        target = page.locator('textarea[placeholder*="标题"]').first
        target.click(timeout=3000, force=True)
        target.fill(value, timeout=3000)
        _zhihu_humanize_title_edit(page, value)
        page.wait_for_timeout(500)
        current = page.evaluate("""() => {
          const el = document.querySelector('textarea[placeholder*="标题"]');
          return (el && el.value || '').trim();
        }""")
        return bool(current and len(current) <= 28 and len(current) >= 6)
    except Exception:
        return _fill_first_selector(page, ['textarea', '[contenteditable="true"]'], value)


def _zhihu_ensure_publish_ready(page, original_title: str) -> bool:
    _zhihu_humanize_required_fields(page, original_title)
    if _zhihu_publish_button_enabled(page):
        return True
    # 只用真实标题重试；此前这里硬编码了品牌测试标题，发布键没亮时会把
    # 错误标题覆盖进编辑器（2026-09-30 任务#405 实证）
    try:
        _zhihu_fill_title(page, original_title)
        page.wait_for_timeout(800)
    except Exception:
        pass
    return _zhihu_publish_button_enabled(page)


def _zhihu_humanize_required_fields(page, original_title: str) -> None:
    try:
        _zhihu_humanize_title_edit(page, _zhihu_safe_title(original_title))
    except Exception:
        pass
    try:
        body = page.locator(".public-DraftEditor-content").first
        if body.count() > 0:
            body.click(timeout=1500, force=True)
            page.keyboard.press("End")
            page.keyboard.type(" ")
            page.wait_for_timeout(120)
            page.keyboard.press("Backspace")
            page.wait_for_timeout(500)
    except Exception:
        pass


def _zhihu_humanize_title_edit(page, title: str) -> None:
    title = title or ""
    if not title:
        return
    target = page.locator('textarea[placeholder*="标题"]').first
    target.click(timeout=2000, force=True)
    page.keyboard.press("End")
    page.keyboard.press("Backspace")
    page.wait_for_timeout(160)
    page.keyboard.type(title[-1])
    page.wait_for_timeout(350)


def _zhihu_publish_button_enabled(page) -> bool:
    try:
        buttons = page.locator("button").evaluate_all(
            """els => els
              .filter(el => (el.innerText || '').trim() === '发布')
              .map(el => ({
                disabled: !!el.disabled,
                aria: el.getAttribute('aria-disabled'),
                cls: String(el.className || ''),
                rect: (() => { const r = el.getBoundingClientRect(); return {w:r.width,h:r.height}; })()
              }))"""
        )
        if not buttons:
            return False
        item = buttons[-1]
        if item.get("disabled") or item.get("aria") == "true":
            return False
        cls = item.get("cls") or ""
        if "disabled" in cls.lower():
            return False
        rect = item.get("rect") or {}
        return rect.get("w", 0) > 0 and rect.get("h", 0) > 0
    except Exception:
        return False


def _zhihu_safe_title(title: str) -> str:
    value = re.sub(r"\s+", "", (title or "").strip())
    if len(value) <= 28:
        return value
    for marker in ["？", "?", "：", ":", "，", ","]:
        if marker in value and value.index(marker) + 1 <= 28:
            candidate = value[: value.index(marker) + 1]
            if len(candidate) >= 8:
                return candidate
    compact = value.replace("实测解析", "实测").replace("商旅适配逻辑", "商旅怎么选")
    return compact[:28]


def _fill_first_selector(page, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        try:
            loc = _resolve_locator(page, selector)
            if not loc or not loc.is_visible(timeout=1200):
                continue
            _fill_editor_locator(loc, value)
            current = loc.evaluate("(el) => (el.innerText || el.textContent || el.value || '').trim()")
            if (value or "")[:20] in (current or ""):
                return True
        except Exception:
            continue
    return False


def _zhihu_fill_body(page, body: str) -> bool:
    value = (body or "").strip()
    if len(value) > 9000:
        value = value[:9000]
    try:
        target = page.locator(".public-DraftEditor-content").first
        if target.count() <= 0:
            return False
        target.click(timeout=3000, force=True)
        page.keyboard.press(_select_all_shortcut())
        page.keyboard.press("Backspace")
        page.wait_for_timeout(300)
        # Draft.js is more reliable with paste than direct DOM assignment.
        page.evaluate(
            """async (value) => {
              await navigator.clipboard.writeText(value);
            }""",
            value,
        )
        page.keyboard.press(_paste_shortcut())
        page.wait_for_timeout(1500)
        current = target.evaluate("(el) => (el.innerText || el.textContent || '').trim()")
        if value[:30] in (current or "") and len(current) > 100:
            return True
        target.evaluate(
            """(el, value) => {
              el.innerText = value;
              el.textContent = value;
              el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
              el.dispatchEvent(new Event('change', {bubbles: true}));
              el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
            }""",
            value,
        )
        page.wait_for_timeout(1000)
        current = target.evaluate("(el) => (el.innerText || el.textContent || '').trim()")
        return value[:30] in (current or "") and len(current) > 100
    except Exception:
        return False
    return False


def _zhihu_upload_cover(page, image_path: str) -> bool:
    try:
        text = _visible_text(page)
        if "更换封面" in text or "编辑封面" in text:
            return True
        try:
            cover_input = page.locator("input.UploadPicture-input[type='file']").first
            if cover_input.count() > 0:
                cover_input.set_input_files(image_path)
                page.wait_for_timeout(5000)
                text = _visible_text(page)
                selected = cover_input.evaluate("(el) => !!(el.files && el.files.length)")
                if selected or "添加文章封面" not in text:
                    return True
        except Exception:
            pass
        for label in ["添加封面", "设置封面", "上传封面", "封面"]:
            try:
                page.get_by_text(label, exact=False).first.click(timeout=2000, force=True)
                page.wait_for_timeout(800)
                break
            except Exception:
                continue
        try:
            with page.expect_file_chooser(timeout=3000) as chooser:
                for label in ["上传图片", "本地上传", "添加封面", "设置封面"]:
                    try:
                        page.get_by_text(label, exact=False).first.click(timeout=1500, force=True)
                        break
                    except Exception:
                        continue
            chooser.value.set_files(image_path)
        except Exception:
            if not _upload_first_file_input(page, image_path, wait_ms=1500):
                return False
        page.wait_for_timeout(3000)
        for label in ["确定", "完成", "保存"]:
            try:
                page.get_by_role("button", name=label).last.click(timeout=2000, force=True)
                page.wait_for_timeout(1000)
                break
            except Exception:
                try:
                    page.get_by_text(label, exact=True).last.click(timeout=2000, force=True)
                    page.wait_for_timeout(1000)
                    break
                except Exception:
                    continue
        text = _visible_text(page)
        return "添加封面" not in text or "更换封面" in text or "编辑封面" in text
    except Exception:
        return False


def _ctrip_clickthrough_login_choice(page) -> bool:
    """携程内容中心身份选择页点穿：登录 -> 个人登录。

    仅当页面存在「个人登录」菜单项时动作（说明停在选择页）；个人创作者
    账号 cookie 有效时点完即跳个人中心。返回是否发生了点穿。
    """
    try:
        text = _visible_text(page)
        if "个人登录" not in text:
            return False
        clicked = page.evaluate(
            """() => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
              };
              const loginBtn = Array.from(document.querySelectorAll('div, span, button, a'))
                .filter(el => visible(el) && (el.innerText || '').trim() === '登录')
                .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)[0];
              if (!loginBtn) return 'no-login-btn';
              loginBtn.click();
              return 'clicked-login';
            }"""
        )
        if clicked != "clicked-login":
            return False
        page.wait_for_timeout(900)
        personal = page.evaluate(
            """() => {
              const els = Array.from(document.querySelectorAll('div, span, a, li'))
                .filter(el => (el.innerText || '').trim() === '个人登录');
              if (!els.length) return 'no-personal';
              els[0].click();
              return 'clicked-personal';
            }"""
        )
        if personal != "clicked-personal":
            return False
        page.wait_for_timeout(2500)
        return True
    except Exception:
        return False


def _publish_ctrip_travel(task: dict, content: dict, account: Optional[dict], workflow: Optional[dict] = None) -> PublishRpaResult:
    account_name = (account or {}).get("account_name") or "default"
    login_url = (account or {}).get("login_url") or "https://we.ctrip.com/account/login"
    publish_url = (account or {}).get("publish_url") or (workflow or {}).get("publish_url") or "https://we.ctrip.com/publish/publishPictureText"
    opened = open_publish_browser("ctrip_travel", account_name, login_url)
    if not opened.get("success"):
        return PublishRpaResult(status="failed", failure_reason=opened.get("notes") or "携程发布浏览器启动失败")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return PublishRpaResult(status="failed", failure_reason=f"Playwright 不可用: {exc}")

    draft_html = _content_html(content.get("title") or "", content.get("body") or "")
    local_html = _save_artifact(task["id"], "html", draft_html)
    media_paths = _content_media_file_paths(content, min_count=1)
    if not media_paths:
        return PublishRpaResult(
            status="manual_required",
            raw_artifact_path=local_html,
            summary_artifact_path=_make_publish_summary(task, content, local_html, None, "manual_required"),
            failure_reason="携程图文发布至少需要上传图片素材；当前内容没有可用图片",
            notes="请先在内容生产页为该内容上传图片素材",
        )

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{opened['port']}", timeout=3000)
            context = browser.contexts[0] if browser.contexts else None
            page = _select_existing_page(context, publish_url) if context else None
            page = page or _new_page_from_cdp_context(browser, opened["port"], publish_url or login_url)[1]
            page.on("dialog", lambda dialog: dialog.accept())
            page.set_default_timeout(3500)
            page.set_default_navigation_timeout(8000)
            try:
                page.goto(publish_url, wait_until="domcontentloaded", timeout=8000)
            except Exception:
                page.goto(publish_url, wait_until="commit", timeout=5000)
            page.wait_for_timeout(800)
            _dismiss_publish_popups(page)
            # 2026-10 冷启动：固定浏览器首次打开 we.ctrip.com 会停在
            # /account/login 身份选择页（登录/注册 营销页），即使 cookie
            # 有效也不自动跳个人中心。检测到「个人登录」入口就点穿：
            # 点「登录」展开身份菜单 -> 点「个人登录」-> 回发布页。
            if _ctrip_clickthrough_login_choice(page):
                try:
                    page.goto(publish_url, wait_until="domcontentloaded", timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(800)
                _dismiss_publish_popups(page)
            visible_text = _visible_text(page)
            if (
                any(word in visible_text for word in ["登录", "扫码", "验证码", "安全验证"])
                and "个人登录" not in visible_text
            ):
                screenshot = _try_screenshot(task["id"], page)
                raw = _save_artifact(task["id"], "page.html", page.content())
                summary = _make_publish_summary(task, content, raw, screenshot, "login_required")
                return PublishRpaResult(
                    status="login_required",
                    notes="携程账号需要先登录或处理验证",
                    screenshot_path=screenshot,
                    raw_artifact_path=raw,
                    summary_artifact_path=summary,
                    failure_reason="携程发布页要求登录或验证",
                )

            uploaded = _ctrip_upload_images(page, media_paths)
            filled_title = _ctrip_fill_title(page, content.get("title") or "")
            filled_body = _ctrip_fill_body(page, content.get("body") or "")
            _ctrip_fill_shooting_date(page)
            location_ok = _ctrip_fill_location(page, content)
            declaration_ok = _ctrip_select_ai_declaration(page)
            page.wait_for_timeout(500)
            html = page.content()
            screenshot = _try_screenshot(task["id"], page)
            raw = _save_artifact(task["id"], "page.html", html)
            missing = []
            if not uploaded:
                missing.append("图片上传失败")
            if not filled_title:
                missing.append("标题未填入")
            if not filled_body:
                missing.append("描述未填入")
            if not location_ok:
                missing.append("地点未选择")
            if not declaration_ok:
                missing.append("AI内容声明未选择")
            if missing:
                summary = _make_publish_summary(task, content, raw, screenshot, "manual_required")
                return PublishRpaResult(
                    status="manual_required",
                    notes="携程发布页已打开并保存证据，但必填项未全部完成",
                    screenshot_path=screenshot,
                    raw_artifact_path=raw,
                    summary_artifact_path=summary,
                    failure_reason="；".join(missing),
                )

            summary = _make_publish_summary(task, content, raw, screenshot, "prepared")
            return PublishRpaResult(
                status="prepared",
                notes="携程表单已填完并保存证据；请人工检查封面/地点/声明后点击最终发布，再回捞验真URL",
                screenshot_path=screenshot,
                raw_artifact_path=raw,
                summary_artifact_path=summary,
            )
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return PublishRpaResult(status="failed", failure_reason=f"携程发布 RPA 异常: {exc}", raw_artifact_path=local_html)


def recover_ctrip_travel_urls(account_name: str, title: str = "", limit: int = 8) -> dict:
    opened = open_publish_browser("ctrip_travel", account_name or "default", "https://we.ctrip.com/")
    if not opened.get("success"):
        return {"status": "failed", "failure_reason": opened.get("notes") or "携程浏览器未连接", "candidates": []}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return {"status": "failed", "failure_reason": f"Playwright 不可用: {exc}", "candidates": []}
    candidates = []
    screenshot = None
    raw = None
    review_status = "unknown"
    internal_detail_url = ""
    article_id = ""
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{opened['port']}", timeout=3000)
            context = browser.contexts[0] if browser.contexts else None
            page = None
            if context:
                page = next((p for p in context.pages if "publish/detail" in (p.url or "")), None)
                page = page or next((p for p in context.pages if "contentManagement" in (p.url or "")), None)
            page = page or _new_page_from_cdp_context(browser, opened["port"], "https://we.ctrip.com/publish/contentManagement")[1]
            page.set_default_timeout(2500)
            page.set_default_navigation_timeout(8000)
            existing_detail = next((p for p in (context.pages if context else []) if "publish/detail" in (p.url or "")), None)
            if existing_detail:
                internal_detail_url = existing_detail.url or ""
                detail_text = _visible_text(existing_detail)
                if "已发布" in detail_text:
                    review_status = "published_in_platform"
                detail_match = re.search(r"作品ID[:：]\s*(\d+)", detail_text)
                if detail_match:
                    article_id = detail_match.group(1)
            if "contentManagement" not in (page.url or "") and "publish/detail" not in (page.url or ""):
                page.goto("https://we.ctrip.com/publish/contentManagement", wait_until="domcontentloaded", timeout=8000)
            else:
                try:
                    page.reload(wait_until="domcontentloaded", timeout=8000)
                except Exception:
                    pass
            page.wait_for_timeout(1200)
            _dismiss_publish_popups(page)
            visible = _visible_text(page)
            if "已发布" in visible:
                review_status = "published_in_platform"
            elif "待审核" in visible or re.search(r"审核中/未通过\([1-9]\d*\)", visible):
                review_status = "pending_review"
            elif "未通过" in visible:
                review_status = "rejected"
            detail_match = re.search(r"作品ID[:：]\s*(\d+)", visible)
            if detail_match:
                article_id = detail_match.group(1)
            found = page.evaluate(
                """(needle) => Array.from(document.querySelectorAll('a[href]'))
                  .map(a => ({href: a.href, text: (a.innerText || a.textContent || '').trim()}))
                  .filter(item => /ctrip\\.com|trip\\.com/.test(item.href))
                  .map(item => ({
                    ...item,
                    score: (needle && item.text.includes(needle) ? 20 : 0)
                      + (/you|travel|notes|article|detail|content|html/.test(item.href) ? 5 : 0)
                      + (item.text ? 1 : 0)
                  }))
                  .sort((a,b) => b.score - a.score)
                  .slice(0, 20)""",
                title or "",
            )
            seen = {item.get("href") for item in candidates}
            for item in found:
                href = item.get("href") or ""
                if href and href not in seen and _is_ctrip_public_content_url(href):
                    candidates.append(item)
                    seen.add(href)
            if not candidates and review_status == "published_in_platform":
                try:
                    card = page.locator(".top-title-bg, .right-container .title, .c-m-c-container, .content").first
                    if card.count() > 0:
                        box = card.bounding_box()
                        if box:
                            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + min(18, box["height"] / 2))
                            page.wait_for_timeout(1200)
                            internal_detail_url = page.url or ""
                            visible = _visible_text(page)
                            if "已发布" in visible:
                                review_status = "published_in_platform"
                            detail_match = re.search(r"作品ID[:：]\s*(\d+)", visible)
                            if detail_match:
                                article_id = detail_match.group(1)
                            candidate = _ctrip_extract_published_url(page, title)
                            if candidate:
                                candidates.append({"href": candidate, "text": page.title() or title or "", "score": 10})
                except Exception:
                    pass
            if not candidates and review_status == "published_in_platform":
                try:
                    page.goto("https://we.ctrip.com/home/home", wait_until="domcontentloaded", timeout=8000)
                    page.wait_for_timeout(1200)
                    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight * 0.45)")
                    page.wait_for_timeout(500)
                    page.get_by_text("查看详情", exact=True).first.click(timeout=2500, force=True)
                    page.wait_for_timeout(1200)
                    analyse_page = next((p for p in (context.pages if context else []) if "analyse/singleDetail" in (p.url or "")), page)
                    analyse_page.bring_to_front()
                    analyse_page.wait_for_timeout(600)
                    title_node = analyse_page.get_by_text(title[:10], exact=False).first if title else analyse_page.locator(".head-left-bg, body").first
                    box = title_node.bounding_box()
                    if box:
                        before = {p.url for p in (context.pages if context else [])}
                        analyse_page.mouse.click(box["x"] + box["width"] + 24, box["y"] + box["height"] / 2)
                        analyse_page.wait_for_timeout(1200)
                        after_pages = context.pages if context else [analyse_page]
                        detail_page = next((p for p in after_pages if "publish/detail" in (p.url or "") and p.url not in before), None)
                        detail_page = detail_page or next((p for p in after_pages if "publish/detail" in (p.url or "")), None)
                        if detail_page:
                            internal_detail_url = detail_page.url or internal_detail_url
                            detail_text = _visible_text(detail_page)
                            detail_match = re.search(r"作品ID[:：]\s*(\d+)", detail_text)
                            if detail_match:
                                article_id = detail_match.group(1)
                            candidate = _ctrip_extract_published_url(detail_page, title)
                            if candidate:
                                candidates.append({"href": candidate, "text": detail_page.title() or title or "", "score": 10})
                except Exception:
                    pass
            screenshot = _try_screenshot(0, page)
            raw = _save_artifact(0, "ctrip_recover.html", page.content())
            _try_close_page(page)
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return {"status": "failed", "failure_reason": f"携程URL回捞异常: {exc}", "candidates": candidates[:limit]}
    if candidates:
        status = "ready"
        failure_reason = ""
    elif review_status == "published_in_platform":
        status = "platform_published_no_public_url"
        failure_reason = "携程后台显示已发布，但PC内容中心未暴露可验真的公开URL；需继续接入公开分享URL回捞或人工回填"
    elif review_status in {"pending_review", "rejected"}:
        status = review_status
        failure_reason = "携程作品仍在审核中" if review_status == "pending_review" else "携程作品审核未通过"
    else:
        status = "not_found"
        failure_reason = "未从携程内容管理页回捞到候选作品URL"
    return {
        "status": status,
        "failure_reason": failure_reason,
        "candidates": candidates[:limit],
        "screenshot_path": screenshot,
        "raw_artifact_path": raw,
        "review_status": review_status,
        "internal_detail_url": internal_detail_url,
        "article_id": article_id,
    }


def test_publish_workflow(
    platform: str,
    publish_url: str,
    login_url: str = "",
    workflow: Optional[dict] = None,
    account_name: str = "workflow_default",
) -> PublishRpaResult:
    if not publish_url and platform != "website":
        return PublishRpaResult(status="failed", failure_reason="固定发布URL为空，无法测试外部平台工作流")
    if platform == "website":
        raw = _save_artifact(
            0,
            "workflow.html",
            """<!doctype html><html><head><meta charset="utf-8"><title>官网/落地页发布工作流测试</title></head>
<body><h1>官网/落地页发布工作流测试</h1><p>系统已生成本地发布草稿证据。官网类内容不走外部平台编辑器，但仍必须保留可验证 HTML 与截图证据。</p></body></html>""",
        )
        screenshot = _make_text_screenshot(
            0,
            "官网/落地页发布工作流测试",
            "官网类内容不走外部平台编辑器。系统生成本地 HTML 草稿与截图证据，后续必须由真实站点发布 URL 回填并验真后，才能计入已发布。",
        )
        return PublishRpaResult(
            status="prepared",
            notes="官网/落地页工作流已生成可验证 HTML 与截图证据；真实发布仍需回填已上线 URL 并验真",
            screenshot_path=screenshot,
            raw_artifact_path=raw,
        )
    if platform == "ctrip_travel":
        task = {"id": 0, "platform": "ctrip_travel", "brand_id": 0, "content_id": 0}
        content = {
            "id": 0,
            "title": "市中心步行街住宿攻略",
            "body": "📍这是一条仅用于验证携程旅拍RPA工作流的图文草稿内容。\n\n✨系统会验证图片上传、标题、描述、拍摄时间、地点和AI声明，不会点击最终发布。\n\n✅请人工确认页面状态后，再进行正式发布。",
            "media_assets_json": json.dumps([{"kind": "image", "path": _test_upload_image_path(), "filename": "ctrip_test.png"}], ensure_ascii=False),
        }
        account = {"account_name": account_name, "login_url": login_url or "https://we.ctrip.com/account/login", "publish_url": publish_url}
        return _publish_ctrip_travel(task, content, account, workflow)
    if platform == "toutiao":
        return _test_toutiao_workflow(publish_url, login_url, workflow)
    if platform == "douyin":
        task = {"id": 0, "platform": "douyin", "brand_id": 0, "content_id": 0}
        content = {
            "id": 0,
            "title": "市中心住宿攻略",
            "body": "📍步行街商圈附近住宿怎么选？\n\n✨看重交通、夜景和吃喝便利，可以优先选择地铁口附近、步行能到景点的酒店。\n\n✅这是一条抖音图文草稿测试内容，系统只验证上传图片、标题、正文、封面、自主声明和发布设置，不点击最终发布。\n\n#本地酒店 #步行街 #旅游攻略",
            "media_assets_json": json.dumps([{"kind": "image", "path": _test_upload_image_path(), "filename": "douyin_test.png"}], ensure_ascii=False),
        }
        account = {"account_name": account_name, "login_url": login_url or "https://creator.douyin.com/", "publish_url": publish_url}
        return _publish_douyin_image_note(task, content, account, workflow)
    if platform == "xiaohongshu":
        task = {"id": 0, "platform": "xiaohongshu", "brand_id": 0, "content_id": 0}
        content = {
            "id": 0,
            "title": "市中心住宿攻略🌃",
            "body": "📍步行街商圈附近住宿怎么选？\n\n✨看重交通、夜景和吃喝便利，可以优先选择地铁口附近、步行能到景点的酒店。\n\n✅这是一条小红书图文草稿测试内容，系统只验证上传图片、标题和正文填充，不点击最终发布。",
            "media_assets_json": json.dumps([{"kind": "image", "path": _test_upload_image_path(), "filename": "xhs_test.png"}], ensure_ascii=False),
        }
        account = {"account_name": account_name or "小红书号", "login_url": login_url or "https://creator.xiaohongshu.com/", "publish_url": publish_url}
        return _publish_xiaohongshu(task, content, account, workflow)

    selectors = (workflow or {}).get("selectors") or {}
    if not _configured_selectors_ready(selectors):
        raw = _save_artifact(
            0,
            "workflow.html",
            f"""<!doctype html><html><head><meta charset="utf-8"><title>{platform} 发布工作流未就绪</title></head>
<body><h1>{platform} 发布工作流未就绪</h1><p>外部平台工作流缺少已固化的标题/正文选择器，不能标记为已测试。</p></body></html>""",
        )
        screenshot = _make_text_screenshot(
            0,
            f"{platform} 发布工作流未就绪",
            "外部平台工作流缺少已固化的标题/正文选择器，不能标记为已测试。",
        )
        return PublishRpaResult(
            status="manual_required",
            failure_reason="外部平台工作流缺少已固化的标题/正文选择器，不能标记为已测试",
            notes="请先在真实发布页面固化 title/body 选择器，再执行工作流测试",
            screenshot_path=screenshot,
            raw_artifact_path=raw,
        )

    opened = open_publish_browser(platform, account_name or "workflow_default", login_url or publish_url)
    if not opened.get("success"):
        return PublishRpaResult(status="failed", failure_reason=opened.get("notes") or "发布浏览器启动失败")

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        return PublishRpaResult(status="failed", failure_reason=f"Playwright 不可用: {exc}")

    page = None  # 预占位，保证 except 分支可安全 close（避免 CDP websocket 泄漏）
    try:
        existing = _cdp_find_existing_page(opened["port"], publish_url, ["请输入正文", "设置封面", "基础信息"])
        if existing:
            page, _ = existing
        else:
            page, _ = _cdp_open_page(opened["port"], publish_url)
            page.call("Page.navigate", {"url": publish_url}, timeout=5)
            _cdp_wait_ready(page, timeout=12)
            _cdp_wait_for_text(page, ["设置封面", "基础信息", "图文", "标题"], timeout=25)
            time.sleep(1.0)
        html = _cdp_html(page)
        visible_text = _cdp_text(page)
        # 搜狐动态编辑器自带「必须登录才能查看全文」链接占位文案，会误命中
        # "登录"；页面已像编辑器（含 草稿/发布/上传 等标记）时不判为未登录。
        if any(word in visible_text for word in ["登录", "扫码", "验证码", "安全验证"]) and not _looks_like_publish_editor(visible_text, html):
            screenshot = _cdp_screenshot(0, page)
            raw = _save_artifact(0, "workflow.html", html)
            page.close()
            return PublishRpaResult(
                status="login_required",
                notes="固定发布URL可打开，但需要账号登录或验证码处理",
                screenshot_path=screenshot,
                raw_artifact_path=raw,
                failure_reason="发布页要求登录或验证",
            )
        probe_title = f"GEO运营系统工作流测试-{datetime.now().strftime('%H%M%S')}"
        probe_body = "这是一条仅用于验证RPA选择器的草稿内容，系统不会点击最终发布。"
        filled_state = page.evaluate(
            f"""(() => {{
              const titleSelectors = {json.dumps(_selector_list(selectors.get("title")), ensure_ascii=False)};
              const bodySelectors = {json.dumps(_selector_list(selectors.get("body")), ensure_ascii=False)};
              const title = {json.dumps(probe_title, ensure_ascii=False)};
              const body = {json.dumps(probe_body, ensure_ascii=False)};
              const setValue = (selector, value) => {{
                if (!selector || selector.startsWith('frame:')) return false;
                let el = null;
                try {{
                  el = document.querySelector(selector);
                }} catch (e) {{
                  return false;
                }}
                if (!el) return false;
                el.focus && el.focus();
                if ('value' in el) {{
                  const proto = Object.getPrototypeOf(el);
                  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                  if (setter) setter.call(el, value);
                  else el.value = value;
                }} else {{
                  let inserted = false;
                  try {{
                    el.focus && el.focus();
                    document.execCommand && document.execCommand('selectAll', false, null);
                    inserted = !!(document.execCommand && document.execCommand('insertText', false, value));
                  }} catch (e) {{}}
                  if (!inserted) {{
                    el.innerHTML = '<p dir="auto"><span data-lexical-text="true"></span></p>';
                    const span = el.querySelector('[data-lexical-text="true"]') || el;
                    span.textContent = value;
                    el.innerText = value;
                    el.textContent = value;
                  }}
                }}
                // InputEvent 不带 data：Lexical/ProseMirror 对带 data 的合成
                // insertText 会再插入一次文本，与 DOM 写入叠加导致标题重复
                // （2026-10-12 百家号新版编辑器实测）。
                el.dispatchEvent(new InputEvent('input', {{bubbles:true, composed:true, inputType:'insertText'}}));
                el.dispatchEvent(new Event('change', {{bubbles:true}}));
                return true;
              }};
              const setRichText = (el, value) => {{
                if (!el) return false;
                el.focus && el.focus();
                el.innerHTML = '<p dir="auto"><span data-lexical-text="true"></span></p>';
                const span = el.querySelector('[data-lexical-text="true"]') || el;
                span.textContent = value;
                el.dispatchEvent(new InputEvent('input', {{bubbles:true, composed:true, inputType:'insertText'}}));
                el.dispatchEvent(new Event('change', {{bubbles:true}}));
                return true;
              }};
              let titleOk = titleSelectors.some(sel => setValue(sel, title));
              if (!titleOk) {{
                const titleBox = document.querySelector('[data-testid="news-title-input"] [contenteditable="true"]')
                  || Array.from(document.querySelectorAll('[contenteditable="true"]')).find(el => {{
                    const r = el.getBoundingClientRect();
                    return r.width > 120 && r.height > 20;
                  }});
                titleOk = setRichText(titleBox, title);
              }}
              let bodyOk = bodySelectors.some(sel => setValue(sel, body));
              if (!bodyOk) {{
                const frame = document.querySelector('#ueditor_0');
                try {{
                  const editor = window.UE_V2 && window.UE_V2.instants && window.UE_V2.instants['ueditorInstant0'];
                  if (editor && typeof editor.setContent === 'function') {{
                    editor.setContent('<p>' + body.replace(/\\n/g, '</p><p>') + '</p>');
                    bodyOk = true;
                  }}
                }} catch (e) {{}}
              }}
              if (!bodyOk) {{
                const frame = document.querySelector('#ueditor_0');
                const doc = frame && (frame.contentDocument || frame.contentWindow?.document);
                const bodyEl = doc && doc.body;
                if (bodyEl) {{
                  bodyEl.focus && bodyEl.focus();
                  bodyEl.innerHTML = '<p>' + body.replace(/\\n/g, '</p><p>') + '</p>';
                  bodyEl.dispatchEvent(new InputEvent('input', {{bubbles:true, composed:true, inputType:'insertText', data:body}}));
                  bodyEl.dispatchEvent(new Event('change', {{bubbles:true}}));
                  bodyOk = true;
                }}
              }}
              if (!bodyOk) {{
                const editors = Array.from(document.querySelectorAll('#ueditor [contenteditable="true"], .ueditor [contenteditable="true"], [data-lexical-editor="true"]'))
                  .filter(el => {{
                    const r = el.getBoundingClientRect();
                    return r.width > 80 && r.height > 20 && !el.closest('[data-testid="news-title-input"]') && !String(el.innerText || '').includes(title);
                  }})
                  .sort((a,b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
                const bodyEditor = editors[0];
                bodyOk = setRichText(bodyEditor, body);
              }}
              const text = document.body ? document.body.innerText : '';
              const values = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"], [role="textbox"]'))
                .map(el => el.value || el.innerText || el.textContent || '').join('\\n');
              let frameValues = '';
              try {{
                const frame = document.querySelector('#ueditor_0');
                const doc = frame && (frame.contentDocument || frame.contentWindow?.document);
                frameValues = doc && doc.body ? (doc.body.innerText || doc.body.textContent || '') : '';
              }} catch (e) {{}}
              const bodyPieces = body.split(/\\n+/).map(item => item.trim()).filter(Boolean);
              const bodyVerified = bodyPieces.length
                ? bodyPieces.every(item => values.includes(item) || frameValues.includes(item))
                : (values.includes(body) || frameValues.includes(body));
              return {{titleOk, bodyOk, titleVerified: values.includes(title) || text.includes(title), bodyVerified, text}};
            }})()""",
            timeout=8,
        ) or {}
        if not isinstance(filled_state, dict):
            filled_state = {}
        if filled_state.get("titleOk") and not filled_state.get("bodyVerified"):
            try:
                body_point = page.evaluate(
                    """(() => {
                      const holder = document.querySelector('#edui1_iframeholder') || document.querySelector('#ueditor');
                      if (!holder) return null;
                      const r = holder.getBoundingClientRect();
                      return {x: Math.max(10, r.left + 80), y: Math.max(10, r.top + 80)};
                    })()""",
                    timeout=3,
                )
                if isinstance(body_point, dict):
                    page.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": body_point["x"], "y": body_point["y"], "button": "left", "clickCount": 1}, timeout=3)
                    page.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": body_point["x"], "y": body_point["y"], "button": "left", "clickCount": 1}, timeout=3)
                    time.sleep(0.2)
                    _cdp_type_text(page, probe_body)
                    time.sleep(0.5)
                    body_seen = page.evaluate(
                        f"""(() => {{
                          const body = {json.dumps(probe_body, ensure_ascii=False)};
                          const text = document.body ? document.body.innerText : '';
                          let frameText = '';
                          try {{
                            const editor = window.UE_V2 && window.UE_V2.instants && window.UE_V2.instants['ueditorInstant0'];
                            if (editor && typeof editor.getContentTxt === 'function') frameText += editor.getContentTxt();
                          }} catch (e) {{}}
                          try {{
                            const frame = document.querySelector('#ueditor_0');
                            const doc = frame && (frame.contentDocument || frame.contentWindow?.document);
                            frameText = doc && doc.body ? (doc.body.innerText || doc.body.textContent || '') : '';
                          }} catch (e) {{}}
                          const bodyPieces = body.split(/\\n+/).map(item => item.trim()).filter(Boolean);
                          return bodyPieces.length
                            ? bodyPieces.every(item => text.includes(item) || frameText.includes(item))
                            : (text.includes(body) || frameText.includes(body));
                        }})()""",
                        timeout=3,
                    )
                    filled_state["bodyOk"] = bool(body_seen)
                    filled_state["bodyVerified"] = bool(body_seen)
            except Exception:
                pass
        time.sleep(0.3)
        html = _cdp_html(page)
        screenshot = _cdp_screenshot(0, page)
        raw = _save_artifact(0, "workflow.html", html)
        page.close()
        if not (filled_state.get("titleOk") and filled_state.get("bodyOk") and filled_state.get("titleVerified") and filled_state.get("bodyVerified")):
            return PublishRpaResult(
                status="manual_required",
                notes="固定发布URL可打开，但固化选择器未能真实填入测试标题和正文",
                screenshot_path=screenshot,
                raw_artifact_path=raw,
                failure_reason="发布页选择器测试失败",
            )
        return PublishRpaResult(
            status="prepared",
            notes="固定发布URL可打开，固化选择器已真实填入测试标题和正文，已保存测试证据",
            screenshot_path=screenshot,
            raw_artifact_path=raw,
        )
    except Exception as exc:
        # 异常路径下也要关闭 CDP page，避免 websocket 连接泄漏。
        # page 在 try 外已预定义为 None，可安全判断。
        _try_close_page(locals().get("page"))  # 异常路径兜底：关闭本函数已创建的页签
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        return PublishRpaResult(status="failed", failure_reason=f"发布工作流测试异常: {exc}")


def _test_toutiao_workflow(publish_url: str, login_url: str = "", workflow: Optional[dict] = None) -> PublishRpaResult:
    try:
        return _test_toutiao_workflow_cdp(publish_url, login_url)
    except Exception as exc:
        return PublishRpaResult(status="failed", failure_reason=f"今日头条 CDP 工作流测试异常: {exc}")


def _test_toutiao_workflow_cdp(publish_url: str, login_url: str = "") -> PublishRpaResult:
    opened = open_publish_browser("toutiao", "今日头条号", login_url or "https://mp.toutiao.com/")
    if not opened.get("success"):
        return PublishRpaResult(status="failed", failure_reason=opened.get("notes") or "今日头条发布浏览器启动失败")
    page, target_id = _cdp_open_page(opened["port"], publish_url or "https://mp.toutiao.com/profile_v4/graphic/publish")
    task = {
        "id": 0,
        "platform": "toutiao",
        "brand_id": 0,
        "content_id": 0,
    }
    content = {
        "id": 0,
        "title": "📍商圈住宿怎么选",
        "body": (
            "这是一条仅用于验证今日头条 RPA 工作流的草稿内容，系统不会点击最终发布。\n\n"
            "📍测试目标：确认标题、正文、单图封面、不投放广告、声明首发、个人观点等必填项可以被自动填入。\n\n"
            "🚇测试说明：本内容只作为本地工作流验证证据，不代表真实发布，不计入商业发布成果。\n\n"
            "✅通过标准：页面保存真实截图、HTML 和摘要证据，最终发布按钮仍由人工确认。"
        ),
        "media_assets_json": json.dumps([{
            "kind": "image",
            "path": _test_upload_image_path(),
            "filename": "workflow_test.png",
        }], ensure_ascii=False),
    }
    title = content["title"]
    body = content["body"]
    try:
        page.call("Page.navigate", {"url": publish_url or "https://mp.toutiao.com/profile_v4/graphic/publish"}, timeout=5)
        _cdp_wait_ready(page, timeout=15)
        time.sleep(2)
        text = _cdp_text(page)
        if any(word in text for word in ["登录", "扫码", "验证码", "安全验证"]) and "发布" not in text:
            screenshot = _cdp_screenshot(0, page)
            raw = _save_artifact(0, "workflow.html", _cdp_html(page))
            summary = _make_publish_summary(task, content, raw, screenshot, "login_required")
            return PublishRpaResult(
                status="login_required",
                failure_reason="今日头条发布页要求登录或验证",
                notes="请先完成今日头条登录/验证，再重试工作流测试",
                screenshot_path=screenshot,
                raw_artifact_path=raw,
                summary_artifact_path=summary,
            )
        script = f"""
        (async () => {{
          const sleep = ms => new Promise(r => setTimeout(r, ms));
          const title = {json.dumps(title, ensure_ascii=False)};
          const body = {json.dumps(body, ensure_ascii=False)};
          const setNativeValue = (el, value) => {{
            if (!el) return false;
            el.focus && el.focus();
            if ('value' in el) {{
              const proto = Object.getPrototypeOf(el);
              const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
              if (setter) setter.call(el, value);
              else el.value = value;
            }}
            else {{
              el.innerText = value;
              el.textContent = value;
            }}
            el.dispatchEvent(new InputEvent('input', {{ bubbles: true, composed: true, inputType: 'insertText', data: value }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            el.dispatchEvent(new KeyboardEvent('keyup', {{ bubbles: true, key: 'a' }}));
            return true;
          }};
          const byText = (needle, exact = true) => Array.from(document.querySelectorAll('button, span, label, div, a'))
            .find(el => {{
              const text = (el.innerText || el.textContent || '').trim();
              return exact ? text === needle : text.includes(needle);
            }});
          ['我知道了','继续编辑','关闭','跳过'].forEach(t => {{ const el = byText(t); if (el) el.click(); }});
          await sleep(800);
          const titleEl = document.querySelector('textarea[placeholder*="文章标题"], textarea[placeholder*="标题"], input[placeholder*="标题"], [contenteditable="true"][data-placeholder*="标题"]')
            || Array.from(document.querySelectorAll('textarea,input,[contenteditable="true"], div, p')).find(el => {{
              const hint = String(el.placeholder || '') + ' ' + String(el.getAttribute('data-placeholder') || '') + ' ' + String(el.innerText || '');
              const rect = el.getBoundingClientRect();
              return /请输入文章标题|标题/.test(hint) && rect.width > 200 && rect.height > 20;
            }});
          const bodyEl = document.querySelector('.ProseMirror[contenteditable="true"], .tiptap.ProseMirror, [contenteditable="true"][data-placeholder*="正文"], [contenteditable="true"][data-placeholder*="内容"], div[role="textbox"]')
            || Array.from(document.querySelectorAll('[contenteditable="true"], textarea')).find(el => !/标题/.test(el.placeholder || el.getAttribute('data-placeholder') || ''));
          const titleOk = setNativeValue(titleEl, title);
          await sleep(400);
          const bodyOk = setNativeValue(bodyEl, body);
          await sleep(700);
          const clickText = (t, exact = true) => {{ const el = byText(t, exact); if (el) {{ el.click(); return true; }} return false; }};
          const singleCover = clickText('单图');
          const noAd = clickText('不投放广告');
          const firstPublish = clickText('声明首发');
          const personal = clickText('个人观点', false) || (clickText('作品声明') && (await sleep(300), clickText('个人观点', false)));
          const pageText = (document.body.innerText || '').slice(0, 3000);
          // 广告选项存在性只看表单控件标签：探针正文本身含"不投放广告"字样，
          // 用整页文本判断会被正文误命中（2026-10-12 实测）。
          const controlText = Array.from(document.querySelectorAll(
            'label, [class*="radio" i], [class*="checkbox" i], [class*="form-item" i]'
          )).map(el => el.innerText || '').join(' ');
          const hasAdOption = /投放广告|广告/.test(controlText);
          return {{
            titleOk,
            bodyOk,
            singleCover,
            noAd,
            hasAdOption,
            firstPublish,
            personal,
            titleFound: !!titleEl,
            bodyFound: !!bodyEl,
            titleText: titleEl ? (titleEl.value || titleEl.innerText || titleEl.textContent || '').slice(0,80) : '',
            bodyText: bodyEl ? (bodyEl.value || bodyEl.innerText || bodyEl.textContent || '').slice(0,120) : '',
            pageText
          }};
        }})()
        """
        result = page.evaluate(script, timeout=12) or {}
        if not result.get("titleOk") or title[:8] not in (result.get("titleText") or ""):
            try:
                rect = page.evaluate(
                    """(() => {
                      const nodes = Array.from(document.querySelectorAll('div, p, span'));
                      const el = nodes.find(item => /请输入文章标题/.test(item.innerText || item.textContent || ''));
                      if (!el) return null;
                      const r = el.getBoundingClientRect();
                      return {x: r.left + 20, y: r.top + Math.max(12, r.height / 2)};
                    })()""",
                    timeout=5,
                )
                if rect and rect.get("x") and rect.get("y"):
                    page.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": rect["x"], "y": rect["y"], "button": "left", "clickCount": 1}, timeout=5)
                    page.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": rect["x"], "y": rect["y"], "button": "left", "clickCount": 1}, timeout=5)
                    time.sleep(0.2)
                    _cdp_type_text(page, title)
                    time.sleep(0.5)
                    result["titleText"] = page.evaluate(
                        """(() => {
                          const text = document.body.innerText || '';
                          return text.includes('📍商圈住宿怎么选') ? '📍商圈住宿怎么选' : '';
                        })()""",
                        timeout=5,
                    )
                    result["titleOk"] = bool(result["titleText"])
            except Exception:
                pass
        time.sleep(1)
        screenshot = _cdp_screenshot(0, page)
        raw = _save_artifact(0, "workflow.html", _cdp_html(page))
        missing = []
        if not result.get("titleOk") or title[:8] not in (result.get("titleText") or ""):
            missing.append("标题未填入")
        if not result.get("bodyOk") or body[:12] not in (result.get("bodyText") or ""):
            missing.append("正文未填入")
        # 2026-10 头条发布页改版：单篇广告开关已移除，仅当页面表单里确实
        # 存在广告选项却未选中时才算缺项。
        if result.get("hasAdOption") and not result.get("noAd"):
            missing.append("未选择不投放广告")
        if not result.get("firstPublish"):
            missing.append("未勾选声明首发")
        if not result.get("personal"):
            missing.append("未选择个人观点")
        status = "manual_required" if missing else "prepared"
        summary = _make_publish_summary(task, content, raw, screenshot, status)
        if missing:
            return PublishRpaResult(
                status="manual_required",
                failure_reason="；".join(missing),
                notes=f"今日头条发布页已打开并保存证据，但部分必填项仍需人工确认；页面片段：{(result.get('pageText') or '')[:160]}",
                screenshot_path=screenshot,
                raw_artifact_path=raw,
                summary_artifact_path=summary,
            )
        return PublishRpaResult(
            status="prepared",
            notes="今日头条工作流测试通过：已填入标题/正文并处理基础必填项；系统未点击最终发布",
            screenshot_path=screenshot,
            raw_artifact_path=raw,
            summary_artifact_path=summary,
        )
    finally:
        page.close()
        if target_id:
            try:
                LOCAL_REQUESTS.get(f"http://127.0.0.1:{opened['port']}/json/close/{target_id}", timeout=1)
            except Exception:
                pass


def _prepare_website_artifact(task: dict, content: dict) -> PublishRpaResult:
    draft_html = _content_html(content["title"], content["body"])
    local_html = _save_artifact(task["id"], "html", draft_html)
    screenshot = _make_html_screenshot(task["id"], local_html)
    summary = _make_publish_summary(task, content, local_html, screenshot, "prepared")
    return PublishRpaResult(
        status="prepared",
        notes="官网/落地页草稿已生成，并保存发布 HTML/截图/摘要三件套",
        screenshot_path=screenshot,
        raw_artifact_path=local_html,
        summary_artifact_path=summary,
    )


def _visible_text(page) -> str:
    try:
        return page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 4000)")
    except Exception:
        return ""


def _visible_text_with_frames(page) -> str:
    """顶层文档 + ueditor 正文 iframe 的可见文本：百家号正文在 #ueditor_0
    iframe 里，只读顶层文档会把已填好的正文误判为缺失（2026-10-12 实测）。"""
    text = _visible_text(page)
    try:
        frame_text = page.evaluate(
            """() => {
              const f = document.querySelector('#ueditor_0');
              const d = f && (f.contentDocument || (f.contentWindow && f.contentWindow.document));
              return d && d.body ? (d.body.innerText || '') : '';
            }"""
        )
        return text + "\n" + (frame_text or "")
    except Exception:
        return text


def _try_screenshot(task_id: int, page) -> Optional[str]:
    try:
        return _save_artifact(task_id, "png", page.screenshot(full_page=True), binary=True)
    except Exception:
        return None


def _make_text_screenshot(task_id: int, title: str, body: str) -> Optional[str]:
    html_path = _save_artifact(task_id, "html", _content_html(title, body))
    return _make_html_screenshot(task_id, html_path)


def _make_html_screenshot(task_id: int, html_path: str) -> Optional[str]:
    try:
        html_source = Path(html_path).read_text(encoding="utf-8")
        title_match = re.search(r"<title>(.*?)</title>", html_source, re.S | re.I)
        body_match = re.search(r"<pre>(.*?)</pre>", html_source, re.S | re.I)
        title = html_lib.unescape(title_match.group(1).strip()) if title_match else "发布素材截图"
        body = html_lib.unescape(body_match.group(1).strip()) if body_match else re.sub(r"<[^>]+>", "\n", html_source)
        return _make_text_image(task_id, title, body)
    except Exception:
        return None


def _make_text_image(task_id: int, title: str, body: str) -> Optional[str]:
    try:
        from PIL import Image, ImageDraw, ImageFont

        # Windows 优先（服务器实际运行环境），macOS 路径保留以便跨平台复用
        windir = os.environ.get("WINDIR") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
        font_candidates = [
            os.path.join(windir, "Fonts", name)
            for name in ("msyh.ttc", "msyh.ttf", "simhei.ttf", "simsun.ttc")
        ] + [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        ]

        def font(size: int):
            for candidate in font_candidates:
                if os.path.exists(candidate):
                    try:
                        return ImageFont.truetype(candidate, size)
                    except Exception:
                        continue
            return ImageFont.load_default()

        title_font = font(34)
        body_font = font(22)
        meta_font = font(16)
        width = 1280
        pad = 72
        max_chars = 46
        raw_lines = []
        for para in (body or "").splitlines():
            para = para.strip()
            if not para:
                raw_lines.append("")
                continue
            raw_lines.extend(textwrap.wrap(para, width=max_chars, break_long_words=False, replace_whitespace=False) or [para])
        lines = raw_lines[:80]
        line_height = 34
        height = min(max(720, 190 + len(lines) * line_height), 3600)
        image = Image.new("RGB", (width, height), "#f8fafc")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((36, 36, width - 36, height - 36), radius=18, fill="#ffffff", outline="#dbe3ef", width=2)
        draw.text((pad, 72), title or "发布素材截图", fill="#111827", font=title_font)
        draw.text((pad, 128), f"task_id={task_id} | generated_at={datetime.now().isoformat(timespec='seconds')}", fill="#64748b", font=meta_font)
        y = 176
        for line in lines:
            if y > height - 80:
                draw.text((pad, y), "...", fill="#64748b", font=body_font)
                break
            draw.text((pad, y), line, fill="#1f2937", font=body_font)
            y += line_height if line else 22
        output = _artifact_dir() / f"publish_task_{task_id}_{datetime.now().strftime('%H%M%S_%f')}.png"
        image.save(output, format="PNG")
        return str(output)
    except Exception:
        return None


def _content_html(title: str, body: str) -> str:
    safe_title = html_lib.escape(title or "")
    safe_body = html_lib.escape(body or "")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{safe_title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; max-width: 920px; margin: 32px auto; line-height: 1.75; color: #1f2937; background: #f8fafc; }}
    main {{ background: #fff; border: 1px solid #dbe3ef; border-radius: 8px; padding: 28px 34px; }}
    h1 {{ font-size: 28px; margin: 0 0 20px; }}
    pre {{ white-space: pre-wrap; font-family: inherit; font-size: 16px; background: #f8fafc; border: 1px solid #e5e7eb; padding: 16px; border-radius: 8px; }}
  </style>
</head>
<body><main><h1>{safe_title}</h1><pre>{safe_body}</pre></main></body>
</html>"""


_PUBLISH_PLATFORM_NAMES = {
    "website": "官网/落地页",
    "ctrip_travel": "携程内容中心/旅拍",
    "douyin": "抖音创作者中心",
    "toutiao": "今日头条号",
    "baijiahao": "百家号",
    "sohu": "搜狐号",
    "zhihu": "知乎",
    "xiaohongshu": "小红书",
}


def _make_publish_summary(
    task: dict,
    content: dict,
    raw_artifact_path: Optional[str],
    screenshot_path: Optional[str],
    status: str,
) -> Optional[str]:
    try:
        import json

        body = content.get("body") or ""
        task_id = task.get("id")
        payload = {
            "task_id": task_id,
            "brand_id": task.get("brand_id"),
            "platform": task.get("platform"),
            "content_id": task.get("content_id"),
            "status": status,
            "title": content.get("title") or task.get("title"),
            "target_keyword": content.get("target_keyword") or task.get("target_keyword"),
            "raw_artifact_path": raw_artifact_path,
            "screenshot_path": screenshot_path,
            "content_excerpt": body[:800],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "evidence_set": ["html", "screenshot", "summary"],
        }
        # 浏览器直接打开可读：正文用 <pre> 真实换行；同一 payload 内嵌 JSON 供验真
        # （verify_publish_summary_artifact 兼容解析纯 JSON 与本 HTML 两种格式）
        payload_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        platform_name = _PUBLISH_PLATFORM_NAMES.get(payload.get("platform") or "", payload.get("platform") or "-")
        meta_rows = [
            ("任务编号", f"#{task_id}"),
            ("品牌 ID", payload.get("brand_id")),
            ("发布平台", platform_name),
            ("内容 ID", payload.get("content_id")),
            ("状态", payload.get("status")),
            ("目标关键词", payload.get("target_keyword") or "-"),
            ("生成时间", payload.get("generated_at")),
            ("网页原文", raw_artifact_path or "-"),
            ("截图", screenshot_path or "-"),
        ]
        meta_html = "\n".join(
            f"<tr><th>{html_lib.escape(str(label))}</th><td>{html_lib.escape(str(value))}</td></tr>"
            for label, value in meta_rows
        )
        doc = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>发布摘要 · 任务 #{task_id}</title>
  <style>
    body {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif; max-width: 860px; margin: 32px auto; line-height: 1.75; color: #1f2937; background: #f8fafc; }}
    main {{ background: #fff; border: 1px solid #dbe3ef; border-radius: 8px; padding: 28px 34px; }}
    h1 {{ font-size: 24px; margin: 0 0 6px; }}
    .sub {{ color: #64748b; font-size: 13px; margin: 0 0 18px; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 14px; margin-bottom: 20px; }}
    th, td {{ border: 1px solid #e5e7eb; padding: 6px 10px; text-align: left; vertical-align: top; word-break: break-all; }}
    th {{ width: 110px; background: #f8fafc; font-weight: 600; }}
    h2 {{ font-size: 17px; margin: 0 0 10px; }}
    pre {{ white-space: pre-wrap; font-family: inherit; font-size: 15px; background: #f8fafc; border: 1px solid #e5e7eb; padding: 16px; border-radius: 8px; }}
  </style>
</head>
<body><main>
  <h1>发布摘要 · 任务 #{task_id}</h1>
  <p class="sub">发布素材三件套（网页原文 / 截图 / 摘要）的机器可验摘要，字段与内嵌 JSON 一致</p>
  <table>
{meta_html}
  </table>
  <h2>内容节选</h2>
  <pre>{html_lib.escape(body[:800])}</pre>
</main>
<script type="application/json" id="summary-payload">{payload_json}</script>
</body>
</html>"""
        return _save_artifact(task_id or 0, "summary.html", doc)
    except Exception:
        return None


def _try_close_page(page):
    try:
        page.close()
    except Exception:
        pass


def _close_browser_connection(browser):
    try:
        browser.close()
    except Exception:
        pass


def _select_existing_page(context, target_url: str):
    if not context:
        return None
    target_base = (target_url or "").split("?", 1)[0]
    for page in context.pages:
        try:
            if target_base and target_base in (page.url or ""):
                return page
        except Exception:
            continue
    return None


def _new_page_from_cdp_context(browser, port: int, target_url: str = ""):
    before_ids = set()
    context = browser.contexts[0] if browser.contexts else None
    if context:
        for page in context.pages:
            try:
                before_ids.add(id(page))
            except Exception:
                pass
    if target_url:
        try:
            LOCAL_REQUESTS.put(f"http://127.0.0.1:{port}/json/new", params={"url": target_url}, timeout=2)
            time.sleep(0.8)
        except Exception:
            pass
    elif context and context.pages:
        try:
            LOCAL_REQUESTS.put(f"http://127.0.0.1:{port}/json/new", params={"url": "about:blank"}, timeout=2)
            time.sleep(0.8)
        except Exception:
            pass
    context = browser.contexts[0] if browser.contexts else None
    if context and context.pages:
        for page in reversed(context.pages):
            if id(page) not in before_ids:
                return context, page
        return context, _select_existing_page(context, target_url) or context.pages[-1]
    raise RuntimeError("无法从 Chrome 调试端口创建页面")


def _cdp_context_and_page(browser, port: int, target_url: str = ""):
    context = browser.contexts[0] if browser.contexts else None
    if context:
        page = _select_existing_page(context, target_url)
        if page:
            return context, page
        if context.pages:
            return context, context.pages[0]

    if target_url:
        try:
            LOCAL_REQUESTS.put(f"http://127.0.0.1:{port}/json/new", params={"url": target_url}, timeout=2)
            time.sleep(0.8)
        except Exception:
            pass

    context = browser.contexts[0] if browser.contexts else None
    if context and context.pages:
        page = _select_existing_page(context, target_url) or context.pages[-1]
        return context, page
    raise RuntimeError("无法从 Chrome 调试端口取得可操作页面")


class _CdpPage:
    def __init__(self, websocket_url: str, target_id: Optional[str] = None, owns_target: bool = False):
        self.ws = websocket.create_connection(websocket_url, timeout=5, http_proxy_host=None, http_proxy_port=None)
        self.seq = 0
        self.target_id = target_id
        # owns_target=True 表示该 tab 由本流程通过 /json/new 创建，close 时应连带关闭 tab；
        # 复用已有 tab 的连接（_cdp_find_existing_page）只关 websocket，不动用户页签。
        self.owns_target = owns_target

    def call(self, method: str, params: Optional[dict] = None, timeout: float = 8):
        self.seq += 1
        message_id = self.seq
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.ws.recv()
            payload = json.loads(raw)
            if payload.get("id") == message_id:
                if "error" in payload:
                    raise RuntimeError(payload["error"].get("message") or str(payload["error"]))
                return payload.get("result") or {}
        raise TimeoutError(f"CDP call timeout: {method}")

    def evaluate(self, expression: str, timeout: float = 8):
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout=timeout,
        )
        value = result.get("result", {})
        if "value" in value:
            return value.get("value")
        return value.get("description")

    def close(self, close_target: Optional[bool] = None):
        """关闭调试连接；owns_target 时默认先关闭 tab（只关 ws 会让 tab 永久残留）。"""
        should_close_target = self.owns_target if close_target is None else close_target
        if should_close_target and self.target_id:
            try:
                self.call("Target.closeTarget", {"targetId": self.target_id}, timeout=3)
            except Exception:
                pass  # target 已关闭/连接随 target 销毁，均属正常
        try:
            self.ws.close()
        except Exception:
            pass


def _cdp_open_page(port: int, target_url: str) -> tuple[_CdpPage, str]:
    response = LOCAL_REQUESTS.put(f"http://127.0.0.1:{port}/json/new", params={"url": target_url}, timeout=3)
    if response.status_code >= 400:
        raise RuntimeError(f"Chrome 调试端口创建页面失败 HTTP {response.status_code}")
    tab = response.json()
    websocket_url = tab.get("webSocketDebuggerUrl")
    if not websocket_url:
        tabs = LOCAL_REQUESTS.get(f"http://127.0.0.1:{port}/json", timeout=3).json()
        tab = tabs[-1] if tabs else {}
        websocket_url = tab.get("webSocketDebuggerUrl")
    if not websocket_url:
        raise RuntimeError("Chrome 调试端口未返回页面 websocket")
    page = _CdpPage(websocket_url, target_id=tab.get("id") or None, owns_target=True)
    page.call("Page.enable")
    page.call("Runtime.enable")
    return page, tab.get("id") or ""


def _cdp_find_existing_page(port: int, target_url: str, required_text: Optional[list[str]] = None) -> Optional[tuple[_CdpPage, str]]:
    target_base = (target_url or "").split("?", 1)[0]
    try:
        tabs = LOCAL_REQUESTS.get(f"http://127.0.0.1:{port}/json", timeout=3).json()
    except Exception:
        return None
    for tab in reversed(tabs):
        if tab.get("type") != "page":
            continue
        url = tab.get("url") or ""
        if target_base and target_base not in url:
            continue
        websocket_url = tab.get("webSocketDebuggerUrl")
        if not websocket_url:
            continue
        try:
            page = _CdpPage(websocket_url)
            page.call("Runtime.enable")
            text = _cdp_text(page)
            if required_text and not any(item in text for item in required_text):
                page.close()
                continue
            return page, tab.get("id") or ""
        except Exception:
            # 探测失败也必须关掉刚建立的 websocket，否则每次探测泄漏一条连接
            try:
                page.close()
            except Exception:
                pass
            continue
    return None


def _cdp_wait_ready(page: _CdpPage, timeout: float = 15) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            state = page.evaluate("document.readyState")
            text_len = page.evaluate("(document.body && document.body.innerText || '').length") or 0
            if state in {"interactive", "complete"} and int(text_len) > 10:
                return
        except Exception:
            pass
        time.sleep(0.4)


def _cdp_wait_for_text(page: _CdpPage, patterns: list[str], timeout: float = 20) -> str:
    deadline = time.time() + timeout
    last_text = ""
    while time.time() < deadline:
        try:
            last_text = _cdp_text(page) or ""
            if any(pattern in last_text for pattern in patterns):
                return last_text
        except Exception:
            pass
        time.sleep(0.8)
    return last_text


def _cdp_html(page: _CdpPage) -> str:
    return page.evaluate("document.documentElement.outerHTML") or ""


def _cdp_text(page: _CdpPage) -> str:
    return page.evaluate("(document.body && document.body.innerText || '').slice(0, 4000)") or ""


def _cdp_screenshot(task_id: int, page: _CdpPage) -> Optional[str]:
    try:
        data = page.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}, timeout=10).get("data")
        if not data:
            return None
        return _save_artifact(task_id, "png", base64.b64decode(data), binary=True)
    except Exception:
        return None


def _cdp_type_text(page: _CdpPage, text: str) -> None:
    for chunk in [text[index:index + 80] for index in range(0, len(text), 80)]:
        page.call("Input.insertText", {"text": chunk}, timeout=5)
        time.sleep(0.05)


def _dismiss_publish_popups(page):
    for _ in range(8):
        clicked = False
        # "取消"是插入注释等编辑器弹窗的关闭按钮；"确认/嵌入/绑定"绝不能点
        for text in ["我知道了", "继续编辑", "完成", "下一步", "关闭", "跳过", "收起", "取消"]:
            try:
                loc = page.get_by_text(text, exact=True)
                if loc.count() > 0:
                    try:
                        loc.first.click(timeout=1200)
                    except Exception:
                        loc.first.click(timeout=1200, force=True)
                    page.wait_for_timeout(250)
                    clicked = True
                    break
            except Exception:
                continue
        # 设置手机号/活动促销这类模态框没有上述文案按钮，只能点自带的 ×
        # （aria-label=关闭 或 zhihu 的 .Modal-closeButton）；绝不能点"确认/绑定"
        if not clicked:
            try:
                close_btn = page.locator('[aria-label="关闭"], .Modal-closeButton').first
                if close_btn.count() > 0:
                    close_btn.click(timeout=1200, force=True)
                    page.wait_for_timeout(300)
                    clicked = True
            except Exception:
                pass
        if not clicked:
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(250)
            except Exception:
                pass
            break


def _looks_like_publish_editor(visible_text: str, html: str) -> bool:
    text = (visible_text or "")[:4000]
    page = (html or "")[:12000]
    markers = ["标题", "正文", "内容", "发布", "草稿", "文章", "创作", "编辑器"]
    if sum(1 for marker in markers if marker in text) >= 2:
        return True
    selectors = ["contenteditable", "textarea", "input", "ProseMirror", "ql-editor", "editor"]
    return sum(1 for marker in selectors if marker in page) >= 2


def _page_requires_upload_before_editor(page, visible_text: str) -> bool:
    text = visible_text or ""
    if "上传图片" not in text and "上传图文" not in text:
        return False
    try:
        has_file = page.locator('input[type="file"]').count() > 0
    except Exception:
        has_file = False
    try:
        has_title = page.locator('input[placeholder*="标题"], textarea[placeholder*="标题"], .tiptap.ProseMirror').count() > 0
    except Exception:
        has_title = False
    return has_file and not has_title


def _selector_list(value) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _configured_selectors_ready(selectors: Optional[dict]) -> bool:
    configured = selectors or {}
    return bool(_selector_list(configured.get("title")) and _selector_list(configured.get("body")))


def _configured_editor_present(page, selectors: Optional[dict]) -> bool:
    configured = selectors or {}
    found = {"title": False, "body": False}
    for key in ["title", "body"]:
        for selector in _selector_list(configured.get(key)):
            try:
                if selector.startswith("frame:"):
                    parts = selector.split("=>", 1)
                    frame = _find_frame(page, parts[0].replace("frame:", "", 1).strip())
                    inner = parts[1].strip() if len(parts) > 1 else "body"
                    found[key] = bool(frame and frame.locator(inner).count() > 0)
                else:
                    target = _resolve_locator(page, selector)
                    found[key] = bool(target and target.is_visible(timeout=800))
                if found[key]:
                    break
            except Exception:
                continue
    return found["title"] and found["body"]


def _apply_workflow_steps(page, workflow: Optional[dict]):
    steps = (workflow or {}).get("steps") or []
    if not steps:
        return
    if "disable_auto_podcast" in steps:
        _set_checkbox_by_label(page, "自动生成播客", False)
    if "upload_test_image" in steps:
        _upload_test_image_if_needed(page)
    if "upload_cover" in steps:
        _upload_baijiahao_cover(page)
    if "mark_ai_generated" in steps:
        _prepare_baijiahao_declarations(page)
    if "click_article_tab" in steps:
        _click_text_if_present(page, "发布文章", exact=True)
    if "click_write_article" in steps:
        if _click_role_button_if_present(page, "我要发文"):
            return
        try:
            page.mouse.click(600, 420)
            page.wait_for_timeout(2500)
        except Exception:
            pass


def _upload_test_image_if_needed(page) -> bool:
    try:
        if page.locator('input[placeholder*="标题"], .tiptap.ProseMirror[contenteditable="true"]').count() > 0:
            return True
    except Exception:
        pass
    try:
        inputs = page.locator('input[type="file"]')
        if inputs.count() <= 0:
            return False
        inputs.first.set_input_files(_test_upload_image_path())
        page.wait_for_timeout(6000)
        return True
    except Exception:
        return False


def _upload_first_file_input(page, path: str, wait_ms: int = 2000) -> bool:
    try:
        inputs = page.locator('input[type="file"]')
        if inputs.count() <= 0:
            return False
        inputs.first.set_input_files(path)
        page.wait_for_timeout(wait_ms)
        return True
    except Exception:
        return False


def _upload_baijiahao_cover(page, cover_path: Optional[str] = None) -> bool:
    try:
        text = _visible_text(page)
        if "编辑\n更换" in text or ("编辑" in text and "更换" in text and "选择封面" not in text):
            return True
        page.get_by_text("选择封面", exact=True).first.click(timeout=3000, force=True)
        page.wait_for_timeout(1000)
        path = cover_path if cover_path and Path(cover_path).exists() else _test_upload_cover_3x2_path()
        try:
            with page.expect_file_chooser(timeout=5000) as chooser:
                page.get_by_text("点击本地上传", exact=True).first.click(timeout=3000, force=True)
            chooser.value.set_files(path)
        except Exception:
            if not _upload_first_file_input(page, path, wait_ms=1000):
                return False
        page.wait_for_timeout(2500)
        try:
            page.get_by_role("button", name="确定").last.click(timeout=3000, force=True)
        except Exception:
            page.get_by_text("确定", exact=True).last.click(timeout=3000, force=True)
        page.wait_for_timeout(1500)
        return "选择封面" not in _visible_text(page) or "编辑" in _visible_text(page)
    except Exception:
        return False


def _toutiao_enter_article_editor(page) -> bool:
    try:
        if "profile_v4/graphic/publish" not in (page.url or ""):
            try:
                page.get_by_text("文章", exact=True).first.click(timeout=2500, force=True)
                page.wait_for_timeout(1500)
            except Exception:
                page.goto("https://mp.toutiao.com/profile_v4/graphic/publish", wait_until="domcontentloaded", timeout=12000)
        page.wait_for_timeout(1000)
        return True
    except Exception:
        return False


def _toutiao_fill_title(page, title: str) -> bool:
    value = (title or "").strip()[:30] or "市中心步行街住宿推荐"
    selectors = [
        "textarea[placeholder*='标题']",
        "input[placeholder*='标题']",
        "[contenteditable='true'][data-placeholder*='标题']",
        "[contenteditable='true'][placeholder*='标题']",
    ]
    for selector in selectors:
        try:
            loc = _resolve_locator(page, selector)
            if not loc or not loc.is_visible(timeout=1200):
                continue
            loc.click(timeout=3000, force=True)
            page.keyboard.press(_select_all_shortcut())
            page.keyboard.press("Backspace")
            page.wait_for_timeout(250)
            page.keyboard.type(value, delay=35)
            page.wait_for_timeout(500)
            current = loc.evaluate("(el) => (el.value || el.innerText || el.textContent || '').trim()")
            if value[:10] in (current or ""):
                return True
            try:
                loc.evaluate(
                    """(el, value) => {
                      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set
                        || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                      if ('value' in el && setter) setter.call(el, value);
                      else if ('value' in el) el.value = value;
                      else el.textContent = value;
                      el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
                      el.dispatchEvent(new Event('change', {bubbles: true}));
                    }""",
                    value,
                )
                page.wait_for_timeout(500)
                current = loc.evaluate("(el) => (el.value || el.innerText || el.textContent || '').trim()")
                if value[:10] in (current or ""):
                    return True
            except Exception:
                pass
        except Exception:
            continue
    try:
        ok = page.evaluate(
            """(value) => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 120 && r.height > 20;
              };
              const candidates = Array.from(document.querySelectorAll('textarea, input, [contenteditable="true"], [role="textbox"]'))
                .filter(visible)
                .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
              const titleEl = candidates.find(el => {
                const placeholder = el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || '';
                const text = el.innerText || el.textContent || el.value || '';
                return placeholder.includes('标题') || text.includes('请输入文章标题') || el.getBoundingClientRect().height < 90;
              }) || candidates[0];
              if (!titleEl) return false;
              titleEl.focus();
              if ('value' in titleEl) titleEl.value = value;
              else {
                titleEl.innerText = value;
                titleEl.textContent = value;
              }
              titleEl.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
              titleEl.dispatchEvent(new Event('change', {bubbles: true}));
              titleEl.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, key: value.slice(-1) || 'a'}));
              titleEl.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: value.slice(-1) || 'a'}));
              const current = (titleEl.value || titleEl.innerText || titleEl.textContent || '').trim();
              return current.includes(value.slice(0, Math.min(12, value.length)));
            }""",
            value,
        )
        if ok:
            return True
    except Exception:
        pass
    try:
        editable = page.locator("[contenteditable='true']").first
        if editable.count() > 0:
            _fill_editor_locator(editable, value)
            return True
    except Exception:
        pass
    return False


def _toutiao_validate_required_fields(page, title: str, body: str) -> tuple[bool, list[str]]:
    expected_title = ((title or "").strip()[:30] or "市中心步行街住宿推荐")
    body_probe = re.sub(r"\s+", "", (body or "").strip())[:40]
    try:
        state = page.evaluate(
            """([expectedTitle, bodyProbe]) => {
              const clean = (value) => String(value || '').replace(/\\s+/g, '');
              const fields = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"], [role="textbox"]'))
                .map(el => {
                  const r = el.getBoundingClientRect();
                  return {
                    text: el.value || el.innerText || el.textContent || '',
                    placeholder: el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || '',
                    visible: r.width > 0 && r.height > 0,
                    top: r.top,
                    height: r.height
                  };
                })
                .filter(item => item.visible);
              const titleProbe = clean(expectedTitle).slice(0, Math.min(12, clean(expectedTitle).length));
              const titleField = fields.find(item => clean(item.text).includes(titleProbe));
              const bodyText = clean(fields.map(item => item.text).join('\\n'));
              const cover = document.querySelector('.article-cover');
              const coverOk = !!cover && !cover.querySelector('.article-cover-add') && !!cover.querySelector('img, [style*="background-image"]');
              const visibleText = document.body && document.body.innerText || '';
              // 2026-10 发布页改版移除了单篇广告开关：页面完全没有
              // 投放广告选项时视为满足"不投放广告"。
              const controlText = Array.from(document.querySelectorAll(
                'label, [class*="radio" i], [class*="checkbox" i], [class*="form-item" i]'
              )).map(el => el.innerText || '').join(' ');
              const noAdOk = visibleText.includes('不投放广告') || !/投放广告|广告/.test(controlText);
              const firstOk = visibleText.includes('声明首发');
              const statementOk = visibleText.includes('个人观点');
              const publishButtons = Array.from(document.querySelectorAll('button, [role="button"]'))
                .filter(el => /预览并发布|发布/.test(el.innerText || el.textContent || ''))
                .map(el => ({
                  disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true' || /disabled/.test(String(el.className || '').toLowerCase()),
                  text: (el.innerText || el.textContent || '').trim(),
                  rect: (() => { const r = el.getBoundingClientRect(); return {w:r.width,h:r.height}; })()
                }))
                .filter(item => item.rect.w > 0 && item.rect.h > 0);
              const publishOk = publishButtons.length > 0 && publishButtons.some(item => !item.disabled);
              return {
                titleOk: !!titleField,
                bodyOk: !bodyProbe || bodyText.includes(clean(bodyProbe)),
                coverOk,
                noAdOk,
                firstOk,
                statementOk,
                publishOk
              };
            }""",
            [expected_title, body_probe],
        )
    except Exception:
        return False, ["今日头条页面验收脚本执行失败"]
    missing = []
    if not state.get("titleOk"):
        missing.append("标题未被页面真实识别")
    if not state.get("bodyOk"):
        missing.append("正文未被页面真实识别")
    if not state.get("coverOk"):
        missing.append("单图封面未被页面真实识别")
    if not state.get("noAdOk"):
        missing.append("投放广告选项未确认")
    if not state.get("firstOk"):
        missing.append("首发声明未确认")
    if not state.get("statementOk"):
        missing.append("作品声明未确认")
    if not state.get("publishOk"):
        missing.append("预览并发布按钮不可用")
    return not missing, missing


def _toutiao_fill_body(page, body: str) -> bool:
    value = (body or "").strip()[:5000]
    selectors = [
        ".ProseMirror[contenteditable='true']",
        ".tiptap.ProseMirror",
        ".editor-content [contenteditable='true']",
        "[contenteditable='true'][data-placeholder*='正文']",
        "[contenteditable='true'][data-placeholder*='内容']",
        "div[role='textbox']",
        "textarea[placeholder*='正文']",
        "textarea[placeholder*='内容']",
    ]
    if _fill_first_selector(page, selectors, value):
        return True
    try:
        editables = page.locator("[contenteditable='true']")
        count = editables.count()
        target = editables.nth(1 if count > 1 else 0)
        _fill_editor_locator(target, value)
        return True
    except Exception:
        return False


def _toutiao_upload_cover(page, image_path: str) -> bool:
    def upload_visible_input() -> bool:
        upload_input = page.locator('input[type="file"][accept*="image"]').first
        if upload_input.count() <= 0:
            return False
        upload_input.set_input_files(image_path)
        page.wait_for_timeout(5500)
        return after_upload_ok()

    def after_upload_ok() -> bool:
        for label in ["确定", "完成", "保存"]:
            try:
                page.get_by_role("button", name=label).last.click(timeout=1500, force=True)
                page.wait_for_timeout(2500)
                break
            except Exception:
                try:
                    page.get_by_text(label, exact=True).last.click(timeout=1500, force=True)
                    page.wait_for_timeout(2500)
                    break
                except Exception:
                    continue
        try:
            return bool(page.evaluate(
                """() => {
                  const cover = document.querySelector('.article-cover');
                  if (!cover) return false;
                  const hasAdd = !!cover.querySelector('.article-cover-add');
                  const img = cover.querySelector('img, [style*="background-image"]');
                  const src = img ? (img.getAttribute('src') || img.style.backgroundImage || '') : '';
                  return !hasAdd && !!img && src.length > 20;
                }"""
            ))
        except Exception:
            return False

    try:
        text = _visible_text(page)
        if "更换" in text and "预览" in text:
            return True
        try:
            page.get_by_text("单图", exact=True).first.click(timeout=2000, force=True)
        except Exception:
            pass
        try:
            plus_box = page.locator(".article-cover-add").first
            if plus_box.count() <= 0:
                plus_box = page.locator(".article-cover-images, .article-cover").filter(has_text="+").first
            # 2026-10 新版封面流程（实测）：点 + 号(.article-cover-add)直接弹出
            # 「上传图片」抽屉，抽屉内自带 input[type=file]；set_input_files 传图
            # （显示 已上传 1 张图片）后点「确定」才落到封面。不走原生文件框。
            # + 号用 JS 直点：编辑器顶部「草稿恢复」悬浮条会拦坐标点击。
            drawer_seen = False
            try:
                drawer_seen = page.locator(".byte-drawer input[type='file']").count() > 0
            except Exception:
                drawer_seen = False
            for _ in range(2):
                if drawer_seen:
                    break
                try:
                    page.evaluate(
                        """() => {
                          const el = document.querySelector('.article-cover-add');
                          if (el) { el.click(); return true; }
                          return false;
                        }"""
                    )
                except Exception:
                    try:
                        plus_box.click(timeout=2500, force=True)
                    except Exception:
                        pass
                page.wait_for_timeout(1500)
                try:
                    page.wait_for_selector(".byte-drawer input[type='file']", timeout=4000)
                    drawer_seen = True
                except Exception:
                    drawer_seen = False
            try:
                if drawer_seen:
                    page.locator(".byte-drawer input[type='file']").first.set_input_files(image_path)
                    page.wait_for_timeout(5000)
                    if after_upload_ok():
                        return True
            except Exception:
                pass
            try:
                page.wait_for_selector(
                    ".upload-image-panel .upload-btn, [data-e2e='image-upload'] .upload-btn",
                    timeout=4000,
                )
                with page.expect_file_chooser(timeout=7000) as chooser:
                    page.locator(
                        ".upload-image-panel .upload-btn, [data-e2e='image-upload'] .upload-btn"
                    ).first.click(timeout=3000, force=True)
                chooser.value.set_files(image_path)
                page.wait_for_timeout(5000)
                if after_upload_ok():
                    return True
            except Exception:
                pass
                try:
                    with page.expect_file_chooser(timeout=5000) as chooser:
                        page.get_by_text("本地上传", exact=False).first.click(timeout=3000, force=True)
                    chooser.value.set_files(image_path)
                    page.wait_for_timeout(5500)
                    if after_upload_ok():
                        return True
                except Exception:
                    pass
                if upload_visible_input():
                    return True
        except Exception:
            pass
        try:
            box = page.evaluate(
                """() => {
                  const el = document.querySelector('.article-cover-add');
                  if (el) { const r = el.getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height/2}; }
                  const nodes = Array.from(document.querySelectorAll('div, span, label'));
                  const marker = nodes.find(el => (el.innerText || el.textContent || '').includes('展示封面'));
                  if (!marker) return null;
                  const candidates = Array.from(document.querySelectorAll('div')).map(el => {
                    const r = el.getBoundingClientRect();
                    const text = el.innerText || el.textContent || '';
                    return {x:r.left+r.width/2,y:r.top+r.height/2,w:r.width,h:r.height,text};
                  }).filter(item => item.w >= 100 && item.w <= 260 && item.h >= 90 && item.h <= 220 && item.y > marker.getBoundingClientRect().top && item.text.includes('+'));
                  return candidates[0] || null;
                }"""
            )
            if box:
                page.mouse.click(box["x"], box["y"])
                page.wait_for_timeout(1000)
                try:
                    with page.expect_file_chooser(timeout=5000) as chooser:
                        page.get_by_text("本地上传", exact=False).first.click(timeout=3000, force=True)
                    chooser.value.set_files(image_path)
                    page.wait_for_timeout(5500)
                    if after_upload_ok():
                        return True
                except Exception:
                    pass
                if upload_visible_input():
                    return True
        except Exception:
            pass
        try:
            page.mouse.click(432, 1138)
            page.wait_for_timeout(1000)
            try:
                with page.expect_file_chooser(timeout=5000) as chooser:
                    page.get_by_text("本地上传", exact=False).first.click(timeout=3000, force=True)
                chooser.value.set_files(image_path)
                page.wait_for_timeout(5500)
                if after_upload_ok():
                    return True
            except Exception:
                pass
            if upload_visible_input():
                return True
        except Exception:
            pass
        for text_value in ["本地上传", "上传封面", "选择封面", "+"]:
            try:
                with page.expect_file_chooser(timeout=3000) as chooser:
                    page.get_by_text(text_value, exact=False).first.click(timeout=2500, force=True)
                chooser.value.set_files(image_path)
                page.wait_for_timeout(4500)
                text = _visible_text(page)
                return "更换" in text or "重新上传" in text or "预览" in text
            except Exception:
                continue
        if _upload_first_file_input(page, image_path, wait_ms=4500):
            return after_upload_ok()
        return False
    except Exception:
        return False


def _toutiao_select_no_ad(page) -> bool:
    try:
        return _click_label_near_text(page, "不投放广告") or _set_radio_by_label(page, "不投放广告")
    except Exception:
        return False


def _toutiao_check_first_publish(page) -> bool:
    try:
        return _set_checkbox_by_label(page, "声明首发", True) or _click_label_near_text(page, "声明首发")
    except Exception:
        return False


def _toutiao_select_statement(page) -> bool:
    try:
        if _click_label_near_text(page, "个人观点，仅供参考") or _click_label_near_text(page, "个人观点") or _set_checkbox_by_label(page, "个人观点，仅供参考", True) or _set_radio_by_label(page, "个人观点"):
            return True
        if _click_text_if_present(page, "作品声明", exact=False):
            page.wait_for_timeout(500)
            return _click_label_near_text(page, "个人观点，仅供参考") or _click_label_near_text(page, "个人观点") or _set_checkbox_by_label(page, "个人观点，仅供参考", True) or _set_radio_by_label(page, "个人观点")
        try:
            page.get_by_text("个人观点，仅供参考", exact=False).first.click(timeout=2500, force=True)
            page.wait_for_timeout(500)
            return True
        except Exception:
            return False
    except Exception:
        pass
    return False


def _content_media_file_paths(content: dict, min_count: int = 1) -> list[str]:
    try:
        items = json.loads(content.get("media_assets_json") or "[]")
    except Exception:
        items = []
    paths = []
    for item in items if isinstance(items, list) else []:
        path = str((item or {}).get("path") or "").strip()
        if not path:
            continue
        suffix = Path(path).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue
        try:
            local_path = Path(path).expanduser().resolve()
        except Exception:
            continue
        if local_path.exists() and local_path.is_file() and local_path.stat().st_size > 1024:
            paths.append(str(local_path))
    if len(paths) >= min_count:
        return paths[:20]
    return paths


def _ctrip_upload_images(page, paths: list[str]) -> bool:
    try:
        if not paths:
            return False
        file_input = page.locator('input[type="file"]').first
        file_input.set_input_files(paths[:20])
        page.wait_for_timeout(3500)
        text = _visible_text(page)
        return any(str(index) in text for index in range(1, min(len(paths), 20) + 1))
    except Exception:
        return False


def _ctrip_fill_title(page, title: str) -> bool:
    return _ctrip_fill_draft_editor(page, 0, _ctrip_title(title))


def _ctrip_fill_body(page, body: str) -> bool:
    value = (body or "").strip()
    if len(value) > 2950:
        value = value[:2950]
    return _ctrip_fill_draft_editor(page, 1, value)


def _ctrip_fill_draft_editor(page, index: int, value: str) -> bool:
    try:
        editor = page.locator(".public-DraftEditor-content").nth(index)
        editor.click(timeout=4000, force=True)
        page.keyboard.press(_select_all_shortcut())
        page.keyboard.press("Backspace")
        page.keyboard.insert_text(value)
        page.wait_for_timeout(500)
        current = editor.evaluate("(el) => (el.innerText || el.textContent || '').trim()")
        if value[:20] in (current or ""):
            return True
        editor.evaluate(
            """(el, value) => {
              el.innerText = value;
              el.textContent = value;
              el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
              el.dispatchEvent(new Event('change', {bubbles: true}));
              el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
            }""",
            value,
        )
        page.wait_for_timeout(500)
        current = editor.evaluate("(el) => (el.innerText || el.textContent || '').trim()")
        return value[:20] in (current or "")
    except Exception:
        return False


def _ctrip_fill_shooting_date(page) -> bool:
    try:
        value = datetime.now().strftime("%Y-%m-%d")
        date_input = page.locator('input[placeholder="请选择日期"]').first
        if date_input.count() <= 0:
            return False
        current = ""
        try:
            current = date_input.input_value()
        except Exception:
            pass
        if current:
            return True
        date_input.fill(value)
        date_input.press("Enter")
        page.wait_for_timeout(500)
        return True
    except Exception:
        return False


def _ctrip_location_query(content: dict) -> str:
    title = content.get("title") or ""
    body = content.get("body") or ""
    text = f"{title}\n{body}"
    for pattern in [
        r"([^，。\n]{2,18}酒店)",
        r"([^，。\n]{2,12}(?:步行街|商圈|地标))",
    ]:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return (content.get("target_keyword") or title or "市中心步行街")[:24]


def _ctrip_fill_location(page, content: dict) -> bool:
    query = _ctrip_location_query(content)
    try:
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight * 0.55)")
        page.wait_for_timeout(300)
        selector = page.locator(".ant-select-selector").last
        selector.click(timeout=4000, force=True)
        page.wait_for_timeout(300)
        input_box = page.locator("input.ant-select-selection-search-input").first
        input_box.evaluate(
            """(el) => {
              el.removeAttribute('readonly');
              el.style.opacity = '1';
              el.style.width = '220px';
            }"""
        )
        input_box.click(timeout=2000, force=True)
        input_box.fill(query)
        input_box.evaluate(
            """(el) => {
              el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: el.value}));
              el.dispatchEvent(new Event('change', {bubbles: true}));
              el.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, key: 'ArrowDown'}));
              el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: 'ArrowDown'}));
            }"""
        )
        page.wait_for_timeout(1200)
        for locator in [
            ".search-input-detail-c .item-c",
            ".item-c",
            ".search-input-detail-c [class*='item']",
        ]:
            try:
                candidate = page.locator(locator).first
                if candidate.count() <= 0:
                    continue
                box = candidate.bounding_box()
                if box:
                    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                else:
                    candidate.click(timeout=1500, force=True)
                page.wait_for_timeout(900)
                if _ctrip_location_selected(page, selector, query):
                    return True
            except Exception:
                continue
        try:
            first_text = page.get_by_text(query, exact=True).last
            if first_text.count() > 0:
                box = first_text.bounding_box()
                if box:
                    page.mouse.click(box["x"] + min(40, box["width"] / 2), box["y"] + box["height"] / 2)
                    page.wait_for_timeout(800)
                    if _ctrip_location_selected(page, selector, query):
                        return True
        except Exception:
            pass
        for _ in range(4):
            option = page.locator(".ant-select-item-option").first
            if option.count() > 0:
                box = option.bounding_box()
                if box:
                    page.mouse.click(box["x"] + min(80, box["width"] / 2), box["y"] + box["height"] / 2)
                else:
                    option.click(timeout=1500, force=True)
                page.wait_for_timeout(500)
                return _ctrip_location_selected(page, selector, query)
            try:
                text_option = page.get_by_text(query, exact=True).last
                if text_option.count() > 0:
                    box = text_option.bounding_box()
                    if box:
                        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    else:
                        text_option.click(timeout=1500, force=True)
                    page.wait_for_timeout(500)
                    return _ctrip_location_selected(page, selector, query)
            except Exception:
                pass
            page.wait_for_timeout(400)
        input_box.press("ArrowDown")
        input_box.press("Enter")
        page.wait_for_timeout(500)
        return _ctrip_location_selected(page, selector, query)
    except Exception:
        return False


def _ctrip_location_selected(page, selector, query: str) -> bool:
    try:
        selected = selector.inner_text(timeout=800) or ""
    except Exception:
        selected = ""
    if selected and "请输入地理位置" not in selected and (query[:4] in selected or len(selected.strip()) >= 2):
        return True
    try:
        selected_html = selector.evaluate("(el) => el.outerHTML || ''")
    except Exception:
        selected_html = ""
    if query[:4] in selected_html and "placeholder" not in selected_html:
        return True
    try:
        placeholder = page.locator("text=请输入地理位置").count()
    except Exception:
        placeholder = 0
    page_text = _visible_text(page)
    return placeholder == 0 and query[:4] in page_text and ("添加地点" in page_text or "景点" in page_text)


def _ctrip_select_ai_declaration(page) -> bool:
    try:
        text = _visible_text(page)
        if "笔记含AI合成内容" not in text:
            return True
        page.evaluate(
            """() => {
              const nodes = Array.from(document.querySelectorAll('label, span, div'));
              const node = nodes.find(el => (el.innerText || el.textContent || '').includes('笔记含AI合成内容'));
              if (!node) return false;
              const label = node.closest('label') || node;
              const rect = label.getBoundingClientRect();
              const x = rect.left + Math.min(16, Math.max(4, rect.width / 4));
              const y = rect.top + rect.height / 2;
              const target = document.elementFromPoint(x, y) || label;
              target.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, clientX: x, clientY: y}));
              target.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, clientX: x, clientY: y}));
              target.dispatchEvent(new MouseEvent('click', {bubbles: true, clientX: x, clientY: y}));
              return true;
            }"""
        )
        page.wait_for_timeout(600)
        return True
    except Exception:
        return False


def _ctrip_title(title: str) -> str:
    value = re.sub(r"\s+", "", title or "").strip()
    if len(value) <= 20:
        return value
    for sep in ["？", "?", "！", "!", "，", ",", "：", ":"]:
        if sep in value:
            head = value.split(sep, 1)[0]
            if 5 <= len(head) <= 20:
                return head
    compact = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", value)
    return (compact or value)[:20]


def _ctrip_click_publish(page) -> bool:
    try:
        page.get_by_role("button", name=re.compile(r"发\s*布")).last.click(timeout=5000, force=True)
        return True
    except Exception:
        try:
            page.get_by_text("发 布", exact=True).last.click(timeout=5000, force=True)
            return True
        except Exception:
            return False


def _ctrip_extract_published_url(page, title: str = "") -> Optional[str]:
    try:
        text = _visible_text(page)
        match = re.search(r"作品ID[:：]\s*(\d+)", text)
        if match:
            return f"https://m.ctrip.com/webapp/you/community/detail?articleId={match.group(1)}"
    except Exception:
        pass
    try:
        current = page.url or ""
        blocked = ["publishPictureText", "account/login", "contentManagement", "activitysetupapp"]
        if current.startswith("http") and not any(part in current for part in blocked) and _is_ctrip_public_content_url(current):
            return current
    except Exception:
        pass
    try:
        links = page.evaluate(
            """(needle) => Array.from(document.querySelectorAll('a[href]'))
              .map(a => ({href: a.href, text: (a.innerText || a.textContent || '').trim()}))
              .filter(item => /ctrip\\.com|trip\\.com/.test(item.href))
              .map(item => ({
                ...item,
                score: (needle && item.text.includes(needle) ? 20 : 0)
                  + (/you|travel|notes|article|detail|content|html/.test(item.href) ? 5 : 0)
                  + (item.text ? 1 : 0)
              }))
              .sort((a,b) => b.score - a.score)""",
            title or "",
        )
        if links:
            for item in links:
                href = item.get("href") or ""
                if _is_ctrip_public_content_url(href):
                    return href
    except Exception:
        pass
    return None


def _is_ctrip_public_content_url(url: str) -> bool:
    value = (url or "").lower()
    if not value.startswith(("http://", "https://")):
        return False
    blocked = [
        "we.ctrip.com",
        "pages.ctrip.com/public",
        "activitysetupapp",
        "copyright",
        "abctrip",
        "school",
        "home/home",
        "publish",
    ]
    if any(part in value for part in blocked):
        return False
    return any(part in value for part in ["you.ctrip.com", "m.ctrip.com/webapp/you", "travel", "notes", "article", "detail"])


def _is_public_content_url(platform: str, url: str) -> bool:
    value = (url or "").lower()
    if not value.startswith(("http://", "https://")):
        return False
    if platform == "ctrip_travel":
        return _is_ctrip_public_content_url(url)
    if platform == "baijiahao":
        blocked = ["builder", "login", "setting", "manage", "edit", "publish"]
        return ("baidu.com" in value or "mbd.baidu.com" in value) and not any(part in value for part in blocked)
    if platform == "zhihu":
        blocked = ["/write", "/signin", "/settings", "/creator", "/people/"]
        return (
            ("zhuanlan.zhihu.com/p/" in value or "/question/" in value and "/answer/" in value)
            and not any(part in value for part in blocked)
        )
    if platform == "toutiao":
        blocked = ["mp.toutiao.com", "login", "profile_v4", "publish", "manage", "search"]
        return (
            "toutiao.com" in value
            and any(part in value for part in ["/article/", "/group/", "group/", "item_id=", "wid="])
            and not any(part in value for part in blocked)
        )
    if platform == "sohu":
        blocked = ["mp.sohu.com", "login", "passport", "publish", "manage", "contentmanagement", "addmoment"]
        return (
            "sohu.com" in value
            and any(part in value for part in ["sohu.com/a/", "/a/"])
            and not any(part in value for part in blocked)
        )
    return False


def _set_checkbox_by_label(page, label_text: str, checked: bool) -> bool:
    try:
        label = page.get_by_text(label_text, exact=True)
        if label.count() <= 0:
            return False
        wrapper = label.first.locator("xpath=ancestor::label[1]")
        box = wrapper.locator('input[type="checkbox"]').first if wrapper.count() > 0 else None
        if not box or box.count() <= 0:
            return False
        current = bool(box.is_checked())
        if current != checked:
            label.first.click(timeout=2500, force=True)
            page.wait_for_timeout(500)
        return True
    except Exception:
        return False


def _set_radio_by_label(page, label_text: str) -> bool:
    try:
        label = page.get_by_text(label_text, exact=True)
        if label.count() <= 0:
            return False
        wrapper = label.first.locator("xpath=ancestor::label[1]")
        radio = wrapper.locator('input[type="radio"]').first if wrapper.count() > 0 else None
        if radio and radio.count() > 0 and not radio.is_checked():
            label.first.click(timeout=2500, force=True)
            page.wait_for_timeout(500)
        elif radio and radio.count() > 0:
            return True
        else:
            label.first.click(timeout=2500, force=True)
            page.wait_for_timeout(500)
        return True
    except Exception:
        return False


def _click_label_near_text(page, label_text: str) -> bool:
    try:
        loc = page.get_by_text(label_text, exact=True)
        if loc.count() <= 0:
            return False
        loc.first.click(timeout=2500, force=True)
        page.wait_for_timeout(500)
        return True
    except Exception:
        return False


def _test_upload_image_path() -> str:
    path = Path(settings.GEO_ARTIFACT_DIR) / "publishing" / "test_upload_xiaohongshu.png"
    if path.exists() and path.stat().st_size > 128:
        return str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image, ImageDraw, ImageFont
        image = Image.new("RGB", (1080, 1440), "#f8fafc")
        draw = ImageDraw.Draw(image)
        draw.rectangle((80, 80, 1000, 1360), fill="#ffffff", outline="#d1d5db", width=4)
        try:
            title_font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 46)
            body_font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 30)
        except Exception:
            title_font = ImageFont.load_default()
            body_font = ImageFont.load_default()
        draw.text((140, 160), "GEO运营系统", fill="#111827", font=title_font)
        draw.text((140, 250), "小红书RPA上传测试图", fill="#334155", font=body_font)
        draw.text((140, 320), "仅用于草稿填充验证，不点击最终发布。", fill="#64748b", font=body_font)
        image.save(path)
    except Exception:
        path.write_bytes(b"geo upload test image placeholder")
    return str(path)


def _test_upload_cover_3x2_path() -> str:
    path = Path(settings.GEO_ARTIFACT_DIR) / "publishing" / "test_upload_cover_3x2.png"
    if path.exists() and path.stat().st_size > 128:
        return str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image, ImageDraw, ImageFont
        image = Image.new("RGB", (1200, 800), "#f8fafc")
        draw = ImageDraw.Draw(image)
        draw.rectangle((80, 80, 1120, 720), fill="#ffffff", outline="#d1d5db", width=4)
        try:
            title_font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 48)
            body_font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 30)
        except Exception:
            title_font = ImageFont.load_default()
            body_font = ImageFont.load_default()
        draw.text((140, 170), "GEO运营系统", fill="#111827", font=title_font)
        draw.text((140, 270), "百家号 3:2 封面测试图", fill="#334155", font=body_font)
        draw.text((140, 340), "用于真实封面上传与裁剪验证。", fill="#64748b", font=body_font)
        image.save(path)
    except Exception:
        path.write_bytes(b"geo 3x2 cover upload test image placeholder")
    return str(path)


def _click_text_if_present(page, text: str, exact: bool = True) -> bool:
    try:
        loc = page.get_by_text(text, exact=exact)
        if loc.count() > 0:
            loc.first.click(timeout=2500, force=True)
            page.wait_for_timeout(1500)
            return True
    except Exception:
        return False
    return False


def _get_checkbox_state_by_label(page, label_text: str) -> Optional[bool]:
    try:
        label = page.get_by_text(label_text, exact=True)
        if label.count() <= 0:
            return None
        wrapper = label.first.locator("xpath=ancestor::label[1]")
        box = wrapper.locator('input[type="checkbox"]').first if wrapper.count() > 0 else None
        if not box or box.count() <= 0:
            return None
        return bool(box.is_checked())
    except Exception:
        return None


def _prepare_baijiahao_declarations(page) -> bool:
    ok = _set_checkbox_by_label(page, "采用AI生成内容", True)
    # 百家号“来源说明”会展开时间/地点等额外必填项。普通 GEO 草稿没有
    # 明确原文转载来源时不默认勾选，否则会阻塞发布前检查。
    _set_checkbox_by_label(page, "来源说明", False)
    page.wait_for_timeout(400)
    return ok and _get_checkbox_state_by_label(page, "采用AI生成内容") is True


def _click_role_button_if_present(page, name: str) -> bool:
    try:
        loc = page.get_by_role("button", name=name)
        if loc.count() > 0:
            loc.first.click(timeout=3000, force=True)
            page.wait_for_timeout(2500)
            return True
    except Exception:
        return False
    return False


def _wait_for_configured_editors(page, selectors: Optional[dict]):
    configured = selectors or {}
    for selector in _selector_list(configured.get("title"))[:2]:
        try:
            wait_selector = _base_selector(selector)
            page.wait_for_selector(wait_selector, state="attached", timeout=12000)
            break
        except Exception:
            continue
    for selector in _selector_list(configured.get("body"))[:2]:
        if not selector.startswith("frame:"):
            continue
        frame_ref = selector.split("=>", 1)[0].replace("frame:", "", 1).strip()
        if frame_ref.startswith("#") or frame_ref.startswith(".") or "[" in frame_ref:
            try:
                page.wait_for_selector(
                    f"iframe{frame_ref}" if frame_ref.startswith("#") or frame_ref.startswith(".") else frame_ref,
                    state="attached",
                    timeout=12000,
                )
                break
            except Exception:
                continue


def _try_fill_editor(page, title: str, body: str, selectors: Optional[dict] = None, require_both: bool = False) -> bool:
    filled_title = False
    filled_body = False
    configured = selectors or {}
    title_selectors = _selector_list(configured.get("title")) + [
        "input[placeholder*='标题']",
        "textarea[placeholder*='标题']",
        "[contenteditable='true'][data-placeholder*='标题']",
    ]
    body_selectors = _selector_list(configured.get("body")) + [
        "textarea[placeholder*='正文']",
        "textarea[placeholder*='内容']",
        ".tiptap.ProseMirror",
        "[contenteditable='true']",
        "div[role='textbox']",
    ]
    for selector in title_selectors:
        try:
            target = _resolve_locator(page, selector)
            if target and target.is_visible(timeout=1000):
                _fill_editor_locator(target, title)
                filled_title = True
                break
        except Exception:
            continue
    for selector in body_selectors:
        try:
            if isinstance(selector, str) and selector.startswith("frame:"):
                parts = selector.split("=>", 1)
                frame_name = parts[0].replace("frame:", "", 1).strip()
                frame_selector = parts[1].strip() if len(parts) > 1 else "body"
                target_frame = _find_frame(page, frame_name)
                if not target_frame:
                    continue
                target = target_frame.locator(frame_selector).first
                _fill_editor_locator(target, body[:5000])
                filled_body = True
                break
            target = _resolve_locator(page, selector)
            if not target or not target.is_visible(timeout=1000):
                continue
            _fill_editor_locator(target, body[:5000])
            filled_body = True
            break
        except Exception:
            continue
    return (filled_title and filled_body) if require_both else (filled_title or filled_body)


def _base_selector(selector: str) -> str:
    if isinstance(selector, str) and selector.startswith("nth=") and "=>" in selector:
        return selector.split("=>", 1)[1].strip()
    return selector


def _resolve_locator(page, selector: str):
    if isinstance(selector, str) and selector.startswith("nth=") and "=>" in selector:
        prefix, css = selector.split("=>", 1)
        try:
            index = int(prefix.replace("nth=", "", 1).strip())
        except ValueError:
            index = 0
        loc = page.locator(css.strip())
        if loc.count() > index:
            return loc.nth(index)
        return None
    loc = page.locator(selector)
    if loc.count() > 0:
        return loc.first
    return None


def _fill_editor_locator(target, value: str):
    value = value or ""
    try:
        target.click(timeout=3000)
    except Exception:
        try:
            target.click(timeout=3000, force=True)
        except Exception:
            target.evaluate("(el) => el.focus && el.focus()")
    # 真实键盘输入优先：Lexical/ProseMirror 这类受控富文本只认自己的事件
    # 管线，直接改 DOM 会被框架用内部 state 回滚（2026-10-12 百家号实测：
    # DOM 写入后旧草稿被还原）。全选+退格+逐字键入对 input/textarea 同样适用。
    try:
        target.press(_select_all_shortcut(), timeout=2000)
        target.press("Backspace", timeout=2000)
        target.type(value[:4000], delay=0, timeout=45000)
        try:
            current = target.evaluate("(el) => (el.value || el.innerText || el.textContent || '').trim()")
        except Exception:
            current = ""
        if value[:20] in (current or ""):
            return True
    except Exception:
        pass
    before = ""
    try:
        before = target.evaluate("(el) => (el.value || el.innerText || el.textContent || '').trim()")
    except Exception:
        before = ""
    try:
        is_contenteditable = bool(target.evaluate("(el) => !!el && (el.isContentEditable || el.getAttribute('contenteditable') === 'true')"))
    except Exception:
        is_contenteditable = False
    try:
        if is_contenteditable:
            target.evaluate(
                """(el) => {
                  el.focus && el.focus();
                  const sel = window.getSelection && window.getSelection();
                  if (sel) {
                    const range = document.createRange();
                    range.selectNodeContents(el);
                    sel.removeAllRanges();
                    sel.addRange(range);
                  }
                  try { document.execCommand && document.execCommand('selectAll', false, null); } catch (e) {}
                  try { document.execCommand && document.execCommand('delete', false, null); } catch (e) {}
                  el.innerHTML = '<p><br></p>';
                }"""
            )
    except Exception:
        pass
    try:
        target.evaluate(
            """(el, value) => {
              if ('value' in el) el.value = value;
              else {
                try { document.execCommand && document.execCommand('insertText', false, value); } catch (e) {}
                el.innerHTML = '<p dir="auto"><span data-lexical-text="true"></span></p>';
                const span = el.querySelector('[data-lexical-text="true"]') || el;
                span.textContent = value;
                el.innerText = value;
                el.textContent = value;
              }
              el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
              el.dispatchEvent(new Event('change', {bubbles: true}));
              el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
            }""",
            value,
        )
    except Exception:
        pass
    try:
        current = target.evaluate("(el) => (el.value || el.innerText || el.textContent || '').trim()")
    except Exception:
        current = ""
    if value[:20] not in (current or ""):
        try:
            target.fill(value, timeout=5000)
        except Exception:
            try:
                target.press(_select_all_shortcut(), timeout=2000)
                target.press("Backspace", timeout=2000)
                target.type(value[:4000], delay=0, timeout=45000)
            except Exception:
                try:
                    target.evaluate(
                        """(el, value) => {
                          if ('value' in el) el.value = value;
                          else {
                            el.innerHTML = '<p dir="auto"><span data-lexical-text="true"></span></p>';
                            const span = el.querySelector('[data-lexical-text="true"]') || el;
                            span.textContent = value;
                            el.innerText = value;
                            el.textContent = value;
                          }
                          el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
                          el.dispatchEvent(new Event('change', {bubbles: true}));
                        }""",
                        value,
                    )
                except Exception:
                    pass
    try:
        after = target.evaluate("(el) => (el.value || el.innerText || el.textContent || '').trim()")
    except Exception:
        after = ""
    if before == after and value[:20] not in after:
        raise RuntimeError("editor fill did not change target content")


def _editor_contains_probe(page, title: str, body: str) -> bool:
    try:
        main_text = page.evaluate(
            """([title, body]) => {
              const values = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"], [role="textbox"]'))
                .map(el => (el.value || el.innerText || el.textContent || '').trim())
                .join('\\n') + '\\n' + (document.body && document.body.innerText || '');
              return values;
            }""",
            [title, body],
        )
        main_ok = title in (main_text or "")
        if not main_ok:
            try:
                main_ok = page.evaluate(
                    """(title) => Array.from(document.querySelectorAll('textarea,input'))
                      .some(el => String(el.value || '').includes(title))""",
                    title,
                )
            except Exception:
                main_ok = False
        main_body_ok = body[:20] in (main_text or "")
        frame_ok = any(body[:20] in _frame_body_text(frame) for frame in page.frames)
        return main_ok and (main_body_ok or frame_ok)
    except Exception:
        return False


def _find_frame(page, frame_ref: str):
    if not frame_ref:
        return None
    if frame_ref.startswith("#") or frame_ref.startswith(".") or "[" in frame_ref:
        try:
            element = page.locator(f"iframe{frame_ref}" if frame_ref.startswith("#") or frame_ref.startswith(".") else frame_ref).first
            handle = element.element_handle(timeout=3000)
            return handle.content_frame() if handle else None
        except Exception:
            return None
    return next((frame for frame in page.frames if frame.name == frame_ref), None)


def _frame_body_text(frame) -> str:
    try:
        return frame.locator("body").inner_text(timeout=1000)
    except Exception:
        return ""
