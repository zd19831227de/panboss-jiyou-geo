from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


APP_DIR_NAME = "GeoOpsGEO"


def _user_home() -> Path:
    return Path.home()


def _portable_home() -> Path | None:
    """项目根内存在 runtime-data/ 时进入自包含模式（整文件夹拷贝迁移）。

    logs/artifacts/screenshots/pid 全部落在项目内，拷贝文件夹即带走。
    注意：浏览器登录态 profile 不随 app_home 走（体积大且属登录态），
    见 browser_profile_root 的固定逻辑。
    """
    marker = Path(__file__).resolve().parents[3] / "runtime-data"
    return marker.resolve() if marker.is_dir() else None


def app_home() -> Path:
    override = os.environ.get("GEO_HOME", "").strip()
    if override:
        return Path(os.path.expanduser(override)).resolve()

    portable = _portable_home()
    if portable is not None:
        return portable

    if sys.platform == "darwin":
        return (_user_home() / "Library" / "Application Support" / APP_DIR_NAME).resolve()
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            return (Path(base) / APP_DIR_NAME).resolve()
        return (_user_home() / "AppData" / "Roaming" / APP_DIR_NAME).resolve()
    return (_user_home() / f".{APP_DIR_NAME.lower()}").resolve()


def runtime_dir() -> Path:
    path = app_home() / "runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = app_home() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifacts_dir() -> Path:
    path = app_home() / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def screenshots_dir() -> Path:
    path = app_home() / "screenshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def browser_profile_root() -> Path:
    """浏览器登录态 profile 根目录。

    优先级：GEO_BROWSER_HOME 环境变量 > 遗留 ~/.geo-ops-browser >
    Windows 固定 %APPDATA%\\GeoOpsGEO\\browser-profiles > app_home()/browser-profiles。

    刻意**不跟随 app_home 的自包含模式**：profile 体积大（本机约 12G）且属于
    "登录态"，迁移到新机器后按需重新登录即可；想随文件夹带走可设置
    GEO_BROWSER_HOME 指向项目内目录。固定 APPDATA 是为了自包含模式下
    （app_home 已切到 runtime-data）不丢现有机器上的登录态。
    """
    override = os.environ.get("GEO_BROWSER_HOME", "").strip()
    if override:
        path = Path(os.path.expanduser(override)).resolve()
    else:
        legacy = Path(os.path.expanduser("~/.geo-ops-browser")).resolve()
        if legacy.exists():
            path = legacy
        elif sys.platform.startswith("win"):
            base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
            path = (Path(base) / APP_DIR_NAME / "browser-profiles") if base else (app_home() / "browser-profiles")
        else:
            path = app_home() / "browser-profiles"
    path.mkdir(parents=True, exist_ok=True)
    return path


def temp_root() -> Path:
    path = Path(tempfile.gettempdir()) / "geo-ops"
    path.mkdir(parents=True, exist_ok=True)
    return path


def pid_file() -> Path:
    return runtime_dir() / "geo_services.pid"
