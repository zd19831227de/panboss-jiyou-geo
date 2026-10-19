"""Evidence validation helpers.

Commercial rule: a citation is only verified when the brand is mentioned and
the evidence can be checked. We never count a non-empty string as proof.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse
import json
import re

import requests
from app.core.runtime_paths import APP_DIR_NAME, app_home, artifacts_dir, screenshots_dir


TRUSTED_LOCAL_ROOTS = (
    str(artifacts_dir()),
    str(screenshots_dir()),
    "/tmp/hermes/geo/artifacts",
    "/tmp/hermes/geo/screenshots",
    "/tmp/hermes/team/ai_monitoring",
)

# 本地证据根 → 后端静态挂载点（main.py 里 StaticFiles 挂载 /artifacts、/screenshot）
_WEB_MOUNT_BY_ROOT = (
    (artifacts_dir(), "/artifacts"),
    (screenshots_dir(), "/screenshot"),
)


def build_evidence_web_url(path: Optional[str]) -> str:
    """把任意平台的本地证据路径转成浏览器可直接打开的相对 URL。

    统一在后端完成映射：兼容 Windows 反斜杠绝对路径、macOS/Linux 的
    /tmp/hermes/* 旧路径；文件名里的中文/特殊字符做 URL 编码。
    映射不到可信目录时返回空串（前端据此显示"不可用"而不是死链）。
    """
    local_path = _safe_local_path(path)
    if not local_path:
        return ""
    for root, mount in _WEB_MOUNT_BY_ROOT:
        try:
            root_resolved = Path(root).resolve()
            rel = local_path.relative_to(root_resolved)
        except ValueError:
            continue
        encoded = "/".join(quote(part) for part in rel.parts)
        return f"{mount}/{encoded}"
    return ""


def _direct_get(url: str, timeout: int):
    session = requests.Session()
    session.trust_env = False
    try:
        return session.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 GeoOps/1.0"},
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        )
    finally:
        session.close()


def normalize_evidence_url(url: Optional[str]) -> str:
    if not url:
        return ""
    value = str(url).strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return value


def verify_source_url(url: Optional[str], live_check: bool = False, timeout: int = 10) -> tuple[bool, str]:
    """Validate that a source URL is real enough to be used as proof.

    By default this checks URL structure. For new monitor writes we enable a
    live HTTP check so bad/fake links are downgraded before they enter stats.
    """
    normalized = normalize_evidence_url(url)
    if not normalized:
        return False, "来源链接不是有效 http/https URL"
    if not live_check:
        return True, ""
    last_reason = ""
    for _ in range(2):
        try:
            resp = _direct_get(normalized, timeout)
            status = int(resp.status_code)
            resp.close()
            if 200 <= status < 400:
                return True, ""
            if status in (401, 403, 405, 429):
                return True, f"来源需要权限或限制访问 HTTP {status}"
            last_reason = f"来源链接不可访问 HTTP {status}"
        except Exception as exc:
            last_reason = f"来源链接访问失败: {str(exc)[:120]}"
    return False, last_reason


def _safe_local_path(path: Optional[str]) -> Optional[Path]:
    if not path:
        return None
    value = str(path).strip()
    # Cross-platform remapping: macOS/Linux dev paths under /tmp/hermes/* map to
    # the local app-home artifact/screenshot directories.
    if value.startswith("/tmp/hermes/geo/screenshots/"):
        value = str(screenshots_dir() / value.replace("/tmp/hermes/geo/screenshots/", "", 1))
    elif value.startswith("/tmp/hermes/geo/artifacts/"):
        value = str(artifacts_dir() / value.replace("/tmp/hermes/geo/artifacts/", "", 1))
    elif value.startswith("/tmp/hermes/team/ai_monitoring/"):
        value = str(screenshots_dir() / value.replace("/tmp/hermes/team/ai_monitoring/", "", 1))
    elif value.startswith("/artifacts/"):
        value = str(artifacts_dir() / value.replace("/artifacts/", "", 1))
    elif value.startswith("/screenshot/"):
        value = str(screenshots_dir() / value.replace("/screenshot/", "", 1))
    elif APP_DIR_NAME in value:
        # Cross-platform remapping: artifact paths created on macOS/Linux can be
        # verified against the same relative location under the local app home.
        rel = value.split(APP_DIR_NAME, 1)[1].lstrip("/\\")
        value = str(app_home() / rel.replace("/", os.sep))
    try:
        resolved = Path(os.path.expanduser(value)).resolve()
    except Exception:
        return None
    allowed = [Path(root).resolve() for root in TRUSTED_LOCAL_ROOTS]
    if not any(str(resolved).startswith(str(root) + os.sep) or resolved == root for root in allowed):
        return None
    return resolved


def verify_local_artifact(path: Optional[str], kind: str = "file") -> tuple[bool, str]:
    local_path = _safe_local_path(path)
    if not local_path:
        return False, f"{kind}路径无效或不在证据目录"
    if not local_path.exists() or not local_path.is_file():
        return False, f"{kind}文件不存在"
    size = local_path.stat().st_size
    if size < 128:
        return False, f"{kind}文件过小"
    try:
        head = local_path.read_bytes()[:16]
    except Exception as exc:
        return False, f"{kind}文件无法读取: {str(exc)[:80]}"
    suffix = local_path.suffix.lower()
    if suffix == ".png" and not head.startswith(b"\x89PNG\r\n\x1a\n"):
        return False, f"{kind}不是有效 PNG"
    if suffix in (".jpg", ".jpeg") and not head.startswith(b"\xff\xd8"):
        return False, f"{kind}不是有效 JPEG"
    if suffix in (".html", ".htm") and b"<" not in head:
        return False, f"{kind}不是有效 HTML"
    if suffix == ".json" and head[:1] not in (b"{", b"["):
        return False, f"{kind}不是有效 JSON"
    return True, ""


def verify_publish_html(path: Optional[str]) -> tuple[bool, str]:
    ok, reason = verify_local_artifact(path, "发布HTML")
    if not ok:
        return ok, reason
    local_path = _safe_local_path(path)
    try:
        text = local_path.read_text(encoding="utf-8")
    except Exception as exc:
        return False, f"发布HTML无法读取: {str(exc)[:80]}"
    if "<html" not in text.lower() or "<title" not in text.lower():
        return False, "发布HTML缺少基础结构"
    return True, ""


def verify_publish_screenshot(path: Optional[str]) -> tuple[bool, str]:
    ok, reason = verify_local_artifact(path, "发布截图")
    if not ok:
        return ok, reason
    local_path = _safe_local_path(path)
    try:
        from PIL import Image, ImageStat

        with Image.open(local_path) as image:
            width, height = image.size
            if width < 900 or height < 500:
                return False, "发布截图尺寸过小，无法作为可读证据"
            stat = ImageStat.Stat(image.convert("L"))
            if (stat.var[0] if stat.var else 0) < 8:
                return False, "发布截图疑似空白或不可读"
    except Exception as exc:
        return False, f"发布截图无法解析: {str(exc)[:80]}"
    return True, ""


def verify_publish_summary_artifact(
    path: Optional[str],
    *,
    task_id: Optional[int] = None,
    raw_artifact_path: Optional[str] = None,
    screenshot_path: Optional[str] = None,
) -> tuple[bool, str]:
    ok, reason = verify_local_artifact(path, "发布摘要")
    if not ok:
        return ok, reason
    local_path = _safe_local_path(path)
    text = local_path.read_text(encoding="utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except Exception:
        # 兼容 summary.html：正文为人类可读证据卡，同 payload 内嵌在 application/json script 中
        match = re.search(
            r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
            text,
            re.S | re.I,
        )
        if not match:
            return False, "发布摘要无法解析 JSON"
        try:
            payload = json.loads(match.group(1))
        except Exception as exc:
            return False, f"发布摘要无法解析 JSON: {str(exc)[:80]}"
    required = ["task_id", "title", "raw_artifact_path", "screenshot_path", "content_excerpt", "generated_at"]
    missing = [key for key in required if not payload.get(key)]
    if missing:
        return False, f"发布摘要缺字段: {','.join(missing)}"
    if task_id is not None and int(payload.get("task_id") or 0) != int(task_id):
        return False, "发布摘要 task_id 与任务不一致"
    if raw_artifact_path and str(payload.get("raw_artifact_path")) != str(raw_artifact_path):
        return False, "发布摘要 HTML 路径与任务不一致"
    if screenshot_path and str(payload.get("screenshot_path")) != str(screenshot_path):
        return False, "发布摘要截图路径与任务不一致"
    return True, ""


def validate_monitor_evidence(
    *,
    brand_mentioned: bool,
    is_cited: bool,
    source_url: Optional[str],
    screenshot_path: Optional[str],
    raw_artifact_path: Optional[str],
    live_url_check: bool = False,
) -> dict:
    source_ok, source_reason = verify_source_url(source_url, live_check=live_url_check) if source_url else (False, "无来源链接")
    screenshot_ok, screenshot_reason = verify_local_artifact(screenshot_path, "截图") if screenshot_path else (False, "无截图")
    raw_ok, raw_reason = verify_local_artifact(raw_artifact_path, "原始证据") if raw_artifact_path else (False, "无原始证据")

    mentioned = bool(brand_mentioned or is_cited)
    failure_parts = []
    if mentioned and not source_ok:
        failure_parts.append(source_reason)
    if screenshot_path and not screenshot_ok:
        failure_parts.append(screenshot_reason)
    if raw_artifact_path and not raw_ok:
        failure_parts.append(raw_reason)

    artifact_ok = bool(screenshot_ok or raw_ok)

    if mentioned and source_ok and artifact_ok:
        status = "verified_citation"
    elif mentioned:
        status = "unverified_citation"
    else:
        status = "not_cited"

    return {
        "source_verified": 1 if source_ok else 0,
        "screenshot_verified": 1 if screenshot_ok else 0,
        "raw_artifact_verified": 1 if raw_ok else 0,
        "evidence_status": "verified" if mentioned and source_ok and artifact_ok else "unverified",
        "verification_status": status,
        "has_verified_source": 1 if mentioned and source_ok and artifact_ok else 0,
        "evidence_failure_reason": "；".join(part for part in failure_parts if part)[:500],
    }


def enrich_evidence_row(row: dict) -> dict:
    checked = validate_monitor_evidence(
        brand_mentioned=bool(row.get("brand_mentioned")),
        is_cited=bool(row.get("is_cited")),
        source_url=row.get("source_url"),
        screenshot_path=row.get("screenshot_path"),
        raw_artifact_path=row.get("raw_artifact_path"),
        live_url_check=False,
    )
    result = dict(row)
    for key, value in checked.items():
        if result.get(key) in (None, ""):
            result[key] = value
    if checked["verification_status"] != "verified_citation" and result.get("verification_status") == "verified_citation":
        result["verification_status"] = checked["verification_status"]
    # 可打开的证据 URL 由后端统一计算（Windows 反斜杠路径前端无法转换）
    screenshot_url = build_evidence_web_url(result.get("screenshot_path"))
    raw_url = build_evidence_web_url(result.get("raw_artifact_path"))
    if screenshot_url:
        result["screenshot_url"] = screenshot_url
    if raw_url:
        result["raw_artifact_url"] = raw_url
    return result
