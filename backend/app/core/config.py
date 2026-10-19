"""Centralized configuration via pydantic-settings"""
import os
import secrets
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings
from typing import Optional

from app.core.runtime_paths import artifacts_dir, browser_profile_root, screenshots_dir

# 历史版本曾把密钥硬编码为可预测的占位值。只要 SECRET_KEY 命中这些占位
# 特征，就必须替换为随机密钥，否则任何人都能离线伪造有效 JWT。
# 用特征匹配而非精确值比对：既覆盖历史版本遗留的旧值，也不用把旧值写进代码。
_SECRET_PLACEHOLDER_MARKERS = ("change-in-production", "changeme", "secret-key")


def _persistent_secret_key() -> str:
    """加载或生成持久化的随机 JWT 密钥。

    优先级：.secret_key 文件中已生成的密钥 > 现场生成随机密钥并写盘。
    写盘失败（只读目录等）时退化为进程内随机密钥：重启后旧 token 全部失效，
    也远比可预测的硬编码默认值安全。
    """
    key_file = Path(__file__).resolve().parents[2] / ".secret_key"
    try:
        if key_file.is_file():
            value = key_file.read_text(encoding="utf-8").strip()
            if len(value) >= 32:
                return value
        value = secrets.token_hex(32)
        key_file.write_text(value, encoding="utf-8")
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass  # Windows 上 chmod 语义有限，尽力而为
        return value
    except Exception:
        return secrets.token_hex(32)


class Settings(BaseSettings):
    # Application
    APP_NAME: str = "GEO运营系统"
    APP_VERSION: str = "1.1.0"
    DEBUG: bool = False

    # Database
    DATABASE_URL: str = "sqlite:///./geo_data.db"
    DATABASE_PATH: str = "geo_data.db"

    # Platform monitoring
    PLATFORM_PROFILE_ROOT: str = str(browser_profile_root())
    # AI 监测浏览器端口，格式 "platform:port,platform:port"，可用 .env 覆盖
    GEO_PLATFORM_PORTS: str = "doubao:9321,qianwen:9322,yuanbao:9323"
    # 发布平台浏览器端口，格式 "platform:port,..."，可用 .env 覆盖。
    # 若本机这些端口被别的程序占用，改这里即可（改完重启服务）。
    GEO_PUBLISH_PORTS: str = (
        "website:9521,ctrip_travel:9522,baijiahao:9523,sohu:9524,"
        "zhihu:9525,xiaohongshu:9526,douyin:9527,toutiao:9528"
    )
    GEO_RPA_HEADLESS: bool = False
    GEO_ARTIFACT_DIR: str = str(artifacts_dir())
    # 监测自动补内容（app/services/auto_content.py 以 os.getenv 读取）。
    # 列入 Settings 是为了允许写入 .env——本 Settings 未放开 extra，
    # .env 里出现未声明变量会直接拒绝启动。
    GEO_AUTO_CONTENT: str = "1"
    GEO_AUTO_CONTENT_TRIGGER: str = "all"
    GEO_AUTO_CONTENT_TYPE: str = "media"
    GEO_AUTO_CONTENT_PLATFORM: str = "website"
    GEO_AUTO_CONTENT_MAX_PER_KEYWORD: str = "3"
    GEO_AUTO_CONTENT_REGEN_COOLDOWN_HOURS: str = "24"
    CAPSOLVER_API_KEY: Optional[str] = None
    DASHSCOPE_API_KEY: Optional[str] = None
    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com"
    DASHSCOPE_MODEL: str = "qwen-plus"

    # Security
    SECRET_KEY: str = ""  # 留空/等于旧默认值时自动加载或生成随机密钥
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480  # 8 hours

    # Server（与仓库根 start_services.py 一致）
    HOST: str = "0.0.0.0"
    # 端口可用 PORT 或 GEO_BACKEND_PORT 覆盖（二者等价）
    PORT: int = Field(default=18770, validation_alias=AliasChoices("PORT", "GEO_BACKEND_PORT"))
    GEO_FRONTEND_PORT: int = 15173
    CORS_ORIGINS: str = "*"

    @property
    def public_api_base(self) -> str:
        return f"http://127.0.0.1:{self.PORT}"

    # Monitoring
    MONITOR_TIMEOUT: int = 60  # seconds per keyword
    MONITOR_HEADLESS: bool = True
    SCHEDULER_ENABLED: bool = True
    DAILY_SCAN_HOUR: int = 2  # 2 AM daily increment scan
    WEEKLY_SCAN_DAY: int = 0  # Monday weekly full scan
    WEEKLY_SCAN_HOUR: int = 3

    # Screenshots
    SCREENSHOT_DIR: str = str(screenshots_dir())

    class Config:
        # 用绝对路径读取 .env：pydantic 的 env_file 默认按「当前工作目录」解析，
        # 而后端以 cwd=backend 启动、门禁脚本以 cwd=仓库根 启动，两边会读到不同
        # 结果（门禁侧回落硬编码默认端口 → publishing_workflow_gate 误报「端口
        # 漂移」）。按「仓库根 .env → backend/.env」顺序加载，后者优先，与
        # scripts/_geo_ports.py 的加载顺序保持一致。
        env_file = (
            str(Path(__file__).resolve().parents[3] / ".env"),
            str(Path(__file__).resolve().parents[2] / ".env"),
        )
        env_file_encoding = "utf-8"


settings = Settings()

if not settings.SECRET_KEY or any(
    marker in settings.SECRET_KEY.lower() for marker in _SECRET_PLACEHOLDER_MARKERS
):
    settings.SECRET_KEY = _persistent_secret_key()


# ---------------------------------------------------------------------------
# 端口配置解析 —— 所有需要浏览器端口的地方统一从这里取，避免多份硬编码端口表。
# 优先级：环境变量 / .env > 默认值。
# ---------------------------------------------------------------------------
DEFAULT_AI_PORTS = {"doubao": 9321, "qianwen": 9322, "yuanbao": 9323}
DEFAULT_PUBLISH_PORTS = {
    "website": 9521,
    "ctrip_travel": 9522,
    "baijiahao": 9523,
    "sohu": 9524,
    "zhihu": 9525,
    "xiaohongshu": 9526,
    "douyin": 9527,
    "toutiao": 9528,
}


def parse_port_map(raw: str, defaults: dict) -> dict:
    """把 "doubao:9321,qianwen:9322" 解析成 {"doubao": 9321, ...}。

    只覆盖 defaults 里已有的 key；端口非法时保留默认值，避免写错配置把系统弄挂。
    """
    result = dict(defaults)
    for item in str(raw or "").split(","):
        if ":" not in item:
            continue
        key, _, value = item.partition(":")
        key = key.strip()
        if key not in result:
            continue
        try:
            port = int(value.strip())
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            result[key] = port
    return result


def ai_port_map() -> dict:
    """AI 监测平台端口表（豆包/千问/元宝）。"""
    return parse_port_map(settings.GEO_PLATFORM_PORTS, DEFAULT_AI_PORTS)


def publish_port_map() -> dict:
    """发布平台浏览器端口表。"""
    return parse_port_map(settings.GEO_PUBLISH_PORTS, DEFAULT_PUBLISH_PORTS)


def reserved_port_set() -> set:
    """发布浏览器禁止使用的端口：Hermes 段、旧发布段、当前 AI 监测端口。"""
    return (
        set(range(9221, 9230))
        | set(range(9421, 9430))
        | set(ai_port_map().values())
    )
