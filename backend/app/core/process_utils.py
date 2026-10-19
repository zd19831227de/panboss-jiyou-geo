"""跨平台进程工具：以"脱离父进程"的方式启动浏览器。

背景：Windows 上 ``subprocess.Popen(start_new_session=True)`` 不生效，
浏览器会变成后端 / Worker 的子进程。服务 stop/restart 按进程树清理时会把浏览器
一起杀掉，表现为「刚打开的固定浏览器一会儿就没了 / 老是拉不起来」。

这里在 Windows 上显式使用 DETACHED_PROCESS + CREATE_BREAKAWAY_FROM_JOB，
让浏览器独立于服务进程存活；登录态本来就在固定 profile 里，也不会丢。
"""
from __future__ import annotations

import subprocess
import sys

_WIN_DETACHED_PROCESS = 0x00000008
_WIN_CREATE_NEW_PROCESS_GROUP = 0x00000200
_WIN_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def launch_detached(args, **kwargs) -> subprocess.Popen:
    """启动一个脱离父进程的子进程（固定浏览器用）。"""
    opts = {
        "args": list(args),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    opts.update(kwargs)
    if sys.platform.startswith("win"):
        base = int(opts.get("creationflags") or 0)
        flags = _WIN_DETACHED_PROCESS | _WIN_CREATE_NEW_PROCESS_GROUP | _WIN_CREATE_BREAKAWAY_FROM_JOB
        try:
            return subprocess.Popen(**{**opts, "creationflags": base | flags})
        except OSError:
            # 某些启动方式（服务包装器 / CI / 容器）会把进程放进未设置
            # JOB_OBJECT_LIMIT_BREAKAWAY_OK 的 Job Object，此时
            # CREATE_BREAKAWAY_FROM_JOB 直接失败（ERROR_ACCESS_DENIED），浏览器会
            # 完全起不来。退化为不带 breakaway 的方式重试：虽然脱离不了 Job，
            # 但至少浏览器能启动；服务 stop/restart 另有「按进程名跳过浏览器」
            # 的保护（start_services._terminate_pid），不会误关。
            return subprocess.Popen(
                **{**opts, "creationflags": base | _WIN_DETACHED_PROCESS | _WIN_CREATE_NEW_PROCESS_GROUP}
            )
    opts.setdefault("start_new_session", True)
    return subprocess.Popen(**opts)
