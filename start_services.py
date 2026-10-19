"""GEO运营系统 — 进程管理器
启动后端API + Worker + 前端Vite，使用 start_new_session 确保不随shell退出

用法:
  python3 start_services.py          # 启动所有服务
  python3 start_services.py stop     # 停止所有服务
  python3 start_services.py status   # 查看状态
  python3 start_services.py recovery # 重启恢复检查
"""
import os
import sys
import time
import json
import signal
import shutil
import subprocess
import platform
from pathlib import Path

IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"

# Windows 控制台默认是 GBK；直接 print emoji（✅🌐📡 等）会抛 UnicodeEncodeError，
# 导致 start()/stop() 中途崩溃、服务起不来。这里把本进程 stdout/stderr 统一改成
# UTF-8（无法编码的字符用 replace 兜底），不再依赖调用方先跑 chcp 65001。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str((Path(__file__).parent / "backend").resolve()))
from app.core.runtime_paths import logs_dir, pid_file  # noqa: E402

BASE_DIR = Path(__file__).parent.resolve()
BACKEND_DIR = BASE_DIR / "backend"
FRONTEND_DIR = BASE_DIR / "frontend"
PID_FILE = str(pid_file())
LOG_DIR = logs_dir()

ENV = os.environ.copy()
ENV["PYTHONUNBUFFERED"] = "1"
# 强制 Python 子进程及本进程 stdout/stderr 用 UTF-8 输出，避免在 GBK 控制台下
# 输出 emoji（🌐 📡 ✅ 等）时 UnicodeEncodeError 中断 start() 流程。
# 配合 bat 里的 `chcp 65001` 使 cmd 控制台也按 UTF-8 显示。
ENV["PYTHONIOENCODING"] = "utf-8"
if IS_MACOS:
    ENV["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"


def _load_env_file(path: Path):
    """Load simple KEY=VALUE lines into the service environment."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in ENV:
            ENV[key] = value


_load_env_file(BASE_DIR / ".env")
_load_env_file(BACKEND_DIR / ".env")

# ---------------------------------------------------------------------------
# 端口配置（可用 .env 或环境变量覆盖；改完重启服务即生效）
#   GEO_BACKEND_PORT   后端 API 端口，默认 18770
#   GEO_FRONTEND_PORT  前端页面端口，默认 15173
# 同时把 PORT / VITE_PORT / VITE_API_TARGET 透传给子进程，保证前后端互相找得到。
# ---------------------------------------------------------------------------
def _read_port(*keys: str, default: int) -> int:
    for key in keys:
        raw = (ENV.get(key) or "").strip()
        if raw.isdigit() and 1 <= int(raw) <= 65535:
            return int(raw)
    return default


BACKEND_PORT = _read_port("GEO_BACKEND_PORT", "PORT", default=18770)
FRONTEND_PORT = _read_port("GEO_FRONTEND_PORT", "VITE_PORT", default=15173)

ENV["PORT"] = str(BACKEND_PORT)
ENV["GEO_BACKEND_PORT"] = str(BACKEND_PORT)
ENV["GEO_FRONTEND_PORT"] = str(FRONTEND_PORT)
ENV["VITE_PORT"] = str(FRONTEND_PORT)
ENV["VITE_API_TARGET"] = f"http://127.0.0.1:{BACKEND_PORT}"

# 优先使用项目自带的完整 Python（runtime/python，整文件夹拷贝迁移即用）；
# 其次项目 venv（本机开发）；最后退回当前解释器。
_runtime_python = (BASE_DIR / "runtime" / "python" / "python.exe")
_venv_python = (BASE_DIR / "backend" / ".venv" / "Scripts" / "python.exe")
if IS_WINDOWS and _runtime_python.exists():
    _python_exe = str(_runtime_python)
elif IS_WINDOWS and _venv_python.exists():
    _python_exe = str(_venv_python)
else:
    _python_exe = sys.executable

# 查找 node 可执行文件（跨平台）
_node_exe = shutil.which("node") or "node"
if not shutil.which("node") and IS_MACOS:
    for candidate in ["/usr/local/bin/node", "/opt/homebrew/bin/node"]:
        if os.path.isfile(candidate):
            _node_exe = candidate
            break

# 前端运行模式：
#   有 Node.js + vite  → vite dev server（热更新开发体验，与原先一致）
#   没有 Node.js       → 不起前端进程，由后端直接伺服 frontend/dist
#                        （自包含迁移模式，见 backend/app/main.py 的 SPA 挂载）
_vite_entry = FRONTEND_DIR / "node_modules" / "vite" / "bin" / "vite.js"
_frontend_dist_index = FRONTEND_DIR / "dist" / "index.html"
_use_vite = bool(shutil.which("node")) and _vite_entry.is_file()
if not _use_vite and not _frontend_dist_index.is_file():
    print("[WARN] 未找到 Node.js 且 frontend/dist 不存在：界面将无法访问（仅 API 可用）")
    print("       解决：安装 Node.js，或在一台有 Node 的机器上执行 cd frontend && npm run build")

# UI 地址：vite 模式 = 前端端口；静态模式 = 后端端口
UI_URL = f"http://localhost:{FRONTEND_PORT}" if _use_vite else f"http://localhost:{BACKEND_PORT}"

SERVICES = [
    {
        "name": "backend",
        "cmd": [
            _python_exe, "-m", "uvicorn", "app.main:app",
            "--port", str(BACKEND_PORT), "--host", "127.0.0.1",
        ],
        "cwd": str(BACKEND_DIR),
        "log": str(LOG_DIR / "geo_backend.log"),
        "check_url": f"http://127.0.0.1:{BACKEND_PORT}/health",
    },
    {
        "name": "worker",
        "cmd": [_python_exe, str(BACKEND_DIR / "run_worker.py")],
        "cwd": str(BACKEND_DIR),
        "log": str(LOG_DIR / "geo_worker.log"),
        "check_url": None,
    },
]

if _use_vite:
    SERVICES.append({
        "name": "frontend",
        "cmd": [
            _node_exe,
            str(_vite_entry),
            "--port", str(FRONTEND_PORT), "--host", "127.0.0.1",
        ],
        "cwd": str(FRONTEND_DIR),
        "log": str(LOG_DIR / "geo_frontend.log"),
        "check_url": f"http://127.0.0.1:{FRONTEND_PORT}",
    })


def _find_pid_by_port(port: int) -> int:
    """Find PID listening on port (cross-platform via psutil)"""
    try:
        import psutil
        for conn in psutil.net_connections(kind='inet'):
            if conn.laddr and conn.laddr.port == port and conn.status == 'LISTEN':
                return conn.pid or 0
    except Exception:
        pass
    return 0


def _pids_by_pattern(pattern: str) -> list[int]:
    """Find PIDs with matching command line (cross-platform via psutil)"""
    try:
        import psutil
        my_pid = os.getpid()
        result = []
        for proc in psutil.process_iter(['pid', 'cmdline']):
            try:
                cmdline = ' '.join(proc.info.get('cmdline') or [])
                pid = proc.info['pid']
                if pid == my_pid:
                    continue
                # Match as substring in the combined command line
                if pattern in cmdline:
                    result.append(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return result
    except Exception:
        return []


def _pid_tree_alive(pid: int) -> bool:
    """进程本身或任一子进程还活着就算活着（Popen 记录的可能是壳进程）。"""
    try:
        import psutil
    except Exception:
        return False
    if not psutil.pid_exists(pid):
        return False
    try:
        proc = psutil.Process(pid)
        if proc.status() != psutil.STATUS_ZOMBIE:
            return True
        return bool(proc.children(recursive=True))
    except Exception:
        return True


def _project_process(pid: int) -> bool:
    """判断进程是否属于本项目（命令行或工作目录落在项目根目录内）。

    避免误杀同一台机器上跑着的其它项目进程（它们同样可能用到 uvicorn / vite）。
    """
    try:
        import psutil
        proc = psutil.Process(pid)
        cmdline = " ".join(proc.cmdline() or [])
        if str(BASE_DIR).lower() in cmdline.lower():
            return True
        try:
            cwd = str(proc.cwd())
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            cwd = ""
        return bool(cwd) and str(BASE_DIR).lower() in cwd.lower()
    except Exception:
        return False


BROWSER_PROCESS_NAMES = {"chrome.exe", "msedge.exe", "chromium.exe", "brave.exe", "firefox.exe"}


def _is_browser_process(proc) -> bool:
    try:
        return (proc.name() or "").lower() in BROWSER_PROCESS_NAMES
    except Exception:
        return False


def _terminate_pid(pid: int, label: str):
    """结束进程及其子进程（跨平台）。

    项目用 venv 的 python.exe 启动服务时，Popen 记录的可能是"壳进程"，真正干活的
    是它的子进程；因此这里连同子进程一起结束，避免留下孤儿进程占着端口。

    注意：**不结束浏览器子进程**（Chrome/Edge）。AI 监测和发布用的固定浏览器是
    长期保持运行的，登录态存在 profile 里；服务重启不应该把它一起关掉，否则用户
    会看到"刚打开的浏览器一会儿就没了"。
    """
    try:
        import psutil
    except Exception:
        psutil = None

    if psutil is not None:
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        except Exception:
            proc = None
        if proc is not None:
            try:
                children = proc.children(recursive=True)
            except Exception:
                children = []
            targets = [c for c in children if not _is_browser_process(c)]
            if not _is_browser_process(proc):
                targets.append(proc)
            if not targets:
                return
            for target in targets:
                try:
                    target.terminate()
                except Exception:
                    pass
            try:
                _, alive = psutil.wait_procs(targets, timeout=3)
                for target in alive:
                    try:
                        target.kill()
                    except Exception:
                        pass
            except Exception:
                pass
            print(f"  {label} PID {pid} 已释放")
            return

    # psutil 不可用时的兜底
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=8)
        else:
            os.kill(pid, signal.SIGKILL)
        print(f"  {label} PID {pid} 已释放")
    except Exception:
        pass


# 只清理这些"本项目特征"命令；再用 _project_process 做项目归属校验。
PROCESS_PATTERNS = [
    "uvicorn app.main:app",
    "run_worker.py",
    "vite",
]


def _cleanup_project_processes():
    """清理本项目自己的残留进程。

    先按特征命令找，再确认进程命令行/工作目录确实在本项目根目录内；
    同名子串的其它项目进程（例如另一个项目的 vite/uvicorn）不会被误杀。
    """
    seen = set()
    for pattern in PROCESS_PATTERNS:
        for pid in _pids_by_pattern(pattern):
            if pid in seen:
                continue
            seen.add(pid)
            if _project_process(pid):
                _terminate_pid(pid, "清理残留进程")


def start():
    print("=" * 50)
    print("  GEO运营系统 v2.0 — 进程管理器")
    print("=" * 50)

    _cleanup_project_processes()

    # Kill existing on same ports
    for port in [BACKEND_PORT, FRONTEND_PORT]:
        pid = _find_pid_by_port(port)
        if pid:
            _terminate_pid(pid, f"释放端口 {port}")

    pids = {}
    for svc in SERVICES:
        name = svc["name"]
        # 显式 encoding="utf-8"：Windows 默认 cp1252/mbcs，含中文日志会乱码。
        log_file = open(svc["log"], "w", encoding="utf-8")
        try:
            popen_kwargs = dict(
                args=svc["cmd"],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=svc["cwd"],
                env=ENV,
            )
            if IS_WINDOWS:
                # Break away from Cursor/IDE job objects so services survive the launcher exit.
                CREATE_NEW_PROCESS_GROUP = 0x00000200
                DETACHED_PROCESS = 0x00000008
                CREATE_BREAKAWAY_FROM_JOB = 0x01000000
                CREATE_NO_WINDOW = 0x08000000
                popen_kwargs["creationflags"] = (
                    CREATE_NEW_PROCESS_GROUP
                    | DETACHED_PROCESS
                    | CREATE_BREAKAWAY_FROM_JOB
                    | CREATE_NO_WINDOW
                )
                popen_kwargs["close_fds"] = True
            else:
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(**popen_kwargs)
            pids[name] = proc.pid
            print(f"  [{name}] PID {proc.pid} → {svc['log']}")
        except Exception as e:
            print(f"  [{name}] ❌ 启动失败: {e}")

    # Save PIDs
    with open(PID_FILE, "w") as f:
        json.dump(pids, f)

    # Wait and check
    print("\n等待服务就绪...")
    time.sleep(4)

    all_ok = True
    for svc in SERVICES:
        name = svc["name"]
        url = svc.get("check_url")
        if url:
            try:
                import urllib.request
                r = urllib.request.urlopen(url, timeout=5)
                if r.status == 200:
                    print(f"  [{name}] ✅ {r.status}")
                else:
                    print(f"  [{name}] ⚠️  HTTP {r.status}")
                    all_ok = False
            except Exception as e:
                print(f"  [{name}] ❌ {e}")
                all_ok = False
        else:
            # worker 没有健康检查 URL，用进程树判断存活（避免用 os.kill 在 Windows 上的坑）
            pid = pids.get(name)
            if pid and _pid_tree_alive(pid):
                print(f"  [{name}] ✅ PID {pid} 运行中")
            else:
                print(f"  [{name}] ❌ 进程已退出")
                all_ok = False

    print(f"\n  🌐 UI:   {UI_URL}" + ("" if _use_vite else "  （无 Node.js，后端伺服 frontend/dist 静态构建）"))
    print(f"  📡 API:  http://localhost:{BACKEND_PORT}")
    print(f"  🔑 账号: admin / admin123")
    # Windows 上提示用项目自带 venv python，避免依赖系统 PATH
    print(f"\n  📋 停止: {_python_exe} {__file__} stop")
    print(f"  📋 状态: {_python_exe} {__file__} status")
    print(f"  📋 重启恢复检查: {_python_exe} scripts/restart_recovery_check.py")
    print("=" * 50)

    return 0 if all_ok else 1


def stop():
    print("停止GEO运营系统服务...")
    if os.path.exists(PID_FILE):
        with open(PID_FILE, encoding="utf-8") as f:
            pids = json.load(f)
        for name, pid in pids.items():
            # PID 文件跨重启持久：机器重启后 PID 可能已被别的进程复用，
            # 这里先确认该 PID 确实属于本项目，否则宁可不动（避免误杀无关进程树）。
            if not _project_process(int(pid)):
                print(f"  [{name}] PID {pid} 不属于本项目（PID 可能已被复用），跳过")
                continue
            _terminate_pid(pid, f"[{name}]")
        try:
            # Overwrite PID file instead of deleting (safer across sandboxes)
            with open(PID_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f)
        except (PermissionError, OSError):
            pass
    else:
        print("  未找到PID文件，通过端口清理...")
        for port in [BACKEND_PORT, FRONTEND_PORT]:
            pid = _find_pid_by_port(port)
            if pid:
                if not _project_process(int(pid)):
                    print(f"  端口 {port} 被非本项目进程占用（PID {pid}），跳过不杀")
                    continue
                _terminate_pid(pid, f"端口 {port}")
    _cleanup_project_processes()
    print("✅ 已停止")


def status():
    print("[status] GEO Ops")
    print("-" * 40)

    if os.path.exists(PID_FILE):
        with open(PID_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        for name, pid in saved.items():
            alive = _pid_tree_alive(pid)
            print(f"  [{name}] PID {pid} {'RUNNING' if alive else 'EXITED'}")

    print()

    for port, name in [(BACKEND_PORT, "backend"), (FRONTEND_PORT, "frontend")]:
        pid = _find_pid_by_port(port)
        if pid:
            print(f"  {name}: port {port} UP (PID {pid})")
            if name == "backend":
                try:
                    import urllib.request
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
                        body = resp.read().decode("utf-8", errors="replace")
                    print(f"    health: {body[:80]}")
                except Exception as e:
                    print(f"    health: FAIL {e}")
        else:
            print(f"  {name}: port {port} DOWN")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "stop":
            stop()
        elif cmd == "status":
            status()
        elif cmd == "start":
            # 与无参数等价，符合用户直觉（避免 "python start_services.py start" 报未知命令）
            sys.exit(start())
        elif cmd == "restart":
            stop()
            time.sleep(2)
            start()
        elif cmd == "recovery":
            raise SystemExit(subprocess.call([_python_exe, str(BASE_DIR / "scripts" / "restart_recovery_check.py")], cwd=str(BASE_DIR)))
        elif cmd == "uiurl":
            # 供启动 bat 调用：输出当前应打开的界面地址（跟随端口配置与前端模式）
            print(UI_URL)
        else:
            print(f"未知命令: {cmd}")
            print(f"用法: python {__file__} [start|stop|status|restart|recovery]")
            sys.exit(1)
    else:
        sys.exit(start())
