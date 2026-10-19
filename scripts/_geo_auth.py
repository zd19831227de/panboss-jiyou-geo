# -*- coding: utf-8 -*-
"""验收脚本共享的登录辅助。

后端业务接口已强制 Bearer JWT 认证；脚本通过本模块登录获取 token。
账号可用环境变量 GEO_ADMIN_USER / GEO_ADMIN_PASS 覆盖，默认 admin/admin123。
"""
from __future__ import annotations

import json
import os
import urllib.request

from _geo_ports import BASE_URL

_token_cache: dict[str, str] = {}


def auth_token() -> str:
    """登录并缓存 token；后端不可达或凭据错误时抛异常终止脚本。"""
    if _token_cache.get("token"):
        return _token_cache["token"]
    username = os.environ.get("GEO_ADMIN_USER", "admin")
    password = os.environ.get("GEO_ADMIN_PASS", "admin123")
    body = json.dumps({"username": username, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + "/api/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    token = payload.get("access_token") or ""
    if not token:
        raise RuntimeError("登录失败：后端未返回 access_token")
    _token_cache["token"] = token
    return token


def auth_headers(extra: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {auth_token()}"}
    if extra:
        headers.update(extra)
    return headers
