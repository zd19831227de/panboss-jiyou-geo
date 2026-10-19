# -*- coding: utf-8 -*-
"""验收脚本共享的端口/地址配置。

端口可通过环境变量或 `.env` 覆盖（与后端 app.core.config 保持一致）：
    GEO_BACKEND_PORT   后端 API 端口，默认 18770
    GEO_FRONTEND_PORT  前端页面端口，默认 15173
    GEO_PLATFORM_PORTS AI 监测端口，默认 doubao:9321,qianwen:9322,yuanbao:9323
    GEO_PUBLISH_PORTS  发布端口，默认 website:9521,...（8 个平台）
    GEO_BASE_URL / GEO_FRONTEND_URL 也可直接指定完整地址。

脚本会先尝试加载项目根 `.env` 与 `backend/.env`，已存在的环境变量优先。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

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


def _load_env_file(path: Path) -> None:
    """把简单的 KEY=VALUE 载入 os.environ（不覆盖已存在的变量）。"""
    try:
        if not path.is_file():
            return
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


for _env_path in (ROOT / ".env", ROOT / "backend" / ".env"):
    _load_env_file(_env_path)


def parse_port_map(raw: str, defaults: dict) -> dict:
    """解析 "doubao:9321,qianwen:9322"，非法值保留默认端口。"""
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


def _read_port(*keys: str, default: int) -> int:
    for key in keys:
        raw = (os.environ.get(key) or "").strip()
        if raw.isdigit() and 1 <= int(raw) <= 65535:
            return int(raw)
    return default


AI_PORTS = parse_port_map(os.environ.get("GEO_PLATFORM_PORTS", ""), DEFAULT_AI_PORTS)
PUBLISH_PORTS = parse_port_map(os.environ.get("GEO_PUBLISH_PORTS", ""), DEFAULT_PUBLISH_PORTS)

BACKEND_PORT = _read_port("GEO_BACKEND_PORT", "PORT", default=18770)
FRONTEND_PORT = _read_port("GEO_FRONTEND_PORT", "VITE_PORT", default=15173)

BASE_URL = os.environ.get("GEO_BASE_URL", f"http://127.0.0.1:{BACKEND_PORT}").rstrip("/")
FRONTEND_URL = os.environ.get("GEO_FRONTEND_URL", f"http://127.0.0.1:{FRONTEND_PORT}").rstrip("/")
