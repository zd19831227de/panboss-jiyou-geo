"""P1 verifiable platform monitoring adapters.

This module deliberately separates real AI-platform evidence from ordinary web
search. If an adapter cannot perform a real platform run, it returns a
diagnostic failure instead of falling back to search-engine data.
"""
import json
import io
import os
import base64
import random
import re
import threading
import time
import html as html_lib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import requests

from app.core.config import settings
from app.core.runtime_paths import runtime_dir
from app.database import get_db, local_now
from app.services.evidence import validate_monitor_evidence, verify_source_url
from app.services.captcha_solver import detect_and_solve
from .sessions import PLATFORMS, check_platform_session, get_port, open_login_browser, url_matches_platform

LOCAL_REQUESTS = requests.Session()
LOCAL_REQUESTS.trust_env = False


class MonitorCancelled(Exception):
    pass


def _check_cancel(cancel_checker: Optional[Callable[[], bool]]) -> None:
    if cancel_checker and cancel_checker():
        raise MonitorCancelled("任务已取消")


class _CdpPage:
    def __init__(self, websocket_url: str, target_id: Optional[str] = None):
        import websocket

        self.ws = websocket.create_connection(websocket_url, timeout=5, http_proxy_host=None, http_proxy_port=None)
        self.seq = 0
        self.target_id = target_id

    def call(self, method: str, params: Optional[dict] = None, timeout: float = 10) -> dict:
        self.seq += 1
        message_id = self.seq
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            payload = json.loads(self.ws.recv())
            if payload.get("id") == message_id:
                if payload.get("error"):
                    raise RuntimeError(payload["error"].get("message") or str(payload["error"]))
                return payload.get("result") or {}
        raise TimeoutError(f"CDP call timeout: {method}")

    def evaluate(self, expression: str, timeout: float = 10):
        result = self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True}, timeout=timeout)
        value = result.get("result") or {}
        if "value" in value:
            return value.get("value")
        return value.get("description")

    def close(self, close_target: bool = True):
        """关闭调试连接；当知道 target_id 且该 tab 是本流程创建时，先真正关闭 tab。

        只关 websocket 会把 tab 永久留在常驻浏览器里——每个关键词一个 tab，
        长跑后浏览器会被残留 tab 撑爆。
        """
        if close_target and self.target_id:
            try:
                self.call("Target.closeTarget", {"targetId": self.target_id}, timeout=3)
            except Exception:
                pass  # target 可能已关闭/连接随 target 销毁而断开，均属正常
        try:
            self.ws.close()
        except Exception:
            pass


@dataclass
class SourceEvidence:
    title: str = ""
    url: str = ""
    domain: str = ""
    source_order: int = 0
    is_brand_owned: bool = False
    is_third_party: bool = True
    source_verified: bool = False
    check_status: str = ""


@dataclass
class PlatformMonitorResult:
    platform: str
    prompt: str
    answer_text: str = ""
    brand_mentioned: bool = False
    mention_count: int = 0
    is_cited: bool = False
    verification_status: str = "not_cited"
    cited_sources: Optional[str] = None
    snippet: Optional[str] = None
    source_url: Optional[str] = None
    source_domain: Optional[str] = None
    sources: List[SourceEvidence] = field(default_factory=list)
    screenshot_path: Optional[str] = None
    raw_artifact_path: Optional[str] = None
    html_excerpt: Optional[str] = None
    elapsed_seconds: float = 0.0
    failure_reason: Optional[str] = None
    captcha_status: str = "none"
    notes: Optional[str] = None
    total_results: int = 0
    source_verified: bool = False
    screenshot_verified: bool = False
    raw_artifact_verified: bool = False
    evidence_status: str = "unverified"
    evidence_failure_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "prompt": self.prompt,
            "answer_text": self.answer_text,
            "brand_mentioned": self.brand_mentioned,
            "mention_count": self.mention_count,
            "is_cited": self.is_cited,
            "verification_status": self.verification_status,
            "cited_sources": self.cited_sources,
            "snippet": self.snippet,
            "source_url": self.source_url,
            "source_domain": self.source_domain,
            "sources": [s.__dict__ for s in self.sources],
            "screenshot_path": self.screenshot_path,
            "raw_artifact_path": self.raw_artifact_path,
            "html_excerpt": self.html_excerpt,
            "elapsed_seconds": self.elapsed_seconds,
            "failure_reason": self.failure_reason,
            "captcha_status": self.captcha_status,
            "notes": self.notes,
            "total_results": self.total_results,
            "has_verified_source": bool(self.brand_mentioned and self.source_verified),
            "source_verified": self.source_verified,
            "screenshot_verified": self.screenshot_verified,
            "raw_artifact_verified": self.raw_artifact_verified,
            "evidence_status": self.evidence_status,
            "evidence_failure_reason": self.evidence_failure_reason,
        }


def _artifact_dir(platform: str) -> Path:
    day = datetime.now().strftime("%Y%m%d")
    path = Path(settings.GEO_ARTIFACT_DIR) / platform / day
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_slug(text: str, limit: int = 42) -> str:
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", text).strip("_")
    return (slug[:limit] or "geo")


def _save_artifact(platform: str, brand_name: str, keyword: str, suffix: str, data: Any, binary: bool = False) -> str:
    ts = datetime.now().strftime("%H%M%S_%f")
    path = _artifact_dir(platform) / f"{ts}_{_safe_slug(brand_name)}_{_safe_slug(keyword)}.{suffix}"
    if binary:
        path.write_bytes(data)
    else:
        path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


# ── 标准化证据截图（文本面板 + 原始页面截图合成） ──────────────────────
# 商业验收要求：截图证据必须包含 AI 回复全文与引用的详细条目名称。
# 平台原始截图受滚动/折叠面板影响不一定包含这些信息，这里统一合成一张
# "证据卡"：上半部分是提问/AI回答/引用条目的文本面板，下半部分贴原始页面截图。

_EVIDENCE_CARD_WIDTH = 1080
_EVIDENCE_CARD_PAGE_SHOT_MAX_HEIGHT = 9000

_evidence_font_cache: Dict[int, Any] = {}


def _evidence_font(size: int):
    from PIL import ImageFont

    cached = _evidence_font_cache.get(size)
    if cached is not None:
        return cached
    candidates = []
    if os.name == "nt":
        font_dir = os.environ.get("WINDIR", r"C:\Windows") + r"\Fonts"
        candidates = [os.path.join(font_dir, name) for name in ("msyh.ttc", "msyh.ttf", "simhei.ttf", "simsun.ttc")]
    else:
        candidates = [
            "/System/Library/Fonts/PingFang.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        ]
    font = None
    for candidate in candidates:
        if os.path.exists(candidate):
            try:
                font = ImageFont.truetype(candidate, size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()
    _evidence_font_cache[size] = font
    return font


def _wrap_text(text: str, font, max_width: int) -> List[str]:
    from PIL import Image, ImageDraw

    canvas = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    lines: List[str] = []
    for raw_line in (text or "").splitlines() or [""]:
        current = ""
        for ch in raw_line:
            if canvas.textlength(current + ch, font=font) <= max_width:
                current += ch
                continue
            lines.append(current)
            current = ch
        lines.append(current)
    return lines or [""]


def _render_evidence_card(platform: str, brand_name: str, result: "PlatformMonitorResult") -> Optional[str]:
    """合成证据卡 PNG：提问/AI回答/引用条目文本面板 + 原始页面截图。"""
    from PIL import Image, ImageDraw

    margin = 36
    content_width = _EVIDENCE_CARD_WIDTH - margin * 2
    font_title = _evidence_font(30)
    font_meta = _evidence_font(22)
    font_body = _evidence_font(21)
    font_section = _evidence_font(25)

    answer_text = (result.answer_text or "").strip()
    # 开发记录 #11：截图必须包含完整 AI 问答，不再截断。
    # 原始证据 HTML/answer_text 已有全文，截图也要完整呈现。
    # 仅在极端超长（>12000 字）时截断，避免单张 PNG 过大无法打开。
    if len(answer_text) > 12000:
        answer_text = answer_text[:12000] + "\n…（正文超长已截断，全文见原始证据 HTML）"
    sources = result.sources or []

    panel_lines: List[tuple[Any, str]] = []
    panel_lines.append((font_title, f"{PLATFORMS.get(platform, {}).get('name', platform)} GEO监测证据"))
    meta = [
        f"平台：{platform}    品牌：{brand_name or '-'}",
        f"关键词/提问：{result.prompt or '-'}",
        f"采集时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}    验证状态：{result.verification_status or '-'}",
    ]
    if result.failure_reason:
        meta.append(f"失败原因：{result.failure_reason[:200]}")
    for line in meta:
        panel_lines.append((font_meta, line))

    panel_lines.append((font_section, "一、用户提问"))
    for line in _wrap_text(result.prompt or "（未记录提问）", font_body, content_width):
        panel_lines.append((font_body, line))

    panel_lines.append((font_section, "二、AI 回答"))
    for line in _wrap_text(answer_text or "（未采集到回答文本）", font_body, content_width):
        panel_lines.append((font_body, line))

    panel_lines.append((font_section, f"三、引用来源条目（共 {len(sources)} 条）"))
    if not sources:
        panel_lines.append((font_body, "（本次未提取到可验证引用来源）"))
    for idx, src in enumerate(sources[:15], start=1):
        title_line = f"{idx}. {src.title or src.domain or src.url}"
        for line in _wrap_text(title_line[:240], font_body, content_width):
            panel_lines.append((font_body, line))
        detail = f"    {src.domain or ''}  {src.url[:180]}"
        panel_lines.append((font_meta, detail[:220]))

    line_heights = []
    for font, text in panel_lines:
        ascent, descent = font.getmetrics() if hasattr(font, "getmetrics") else (font.size, 4)
        line_heights.append(ascent + descent + 6)

    page_image = None
    page_path = result.screenshot_path
    if page_path:
        try:
            candidate = Path(page_path)
            if candidate.exists() and candidate.is_file():
                page_image = Image.open(candidate).convert("RGB")
        except Exception:
            page_image = None

    panel_height = margin * 2 + sum(line_heights) + 30
    if page_image is not None:
        scale = _EVIDENCE_CARD_WIDTH / page_image.width
        scaled = page_image.resize((_EVIDENCE_CARD_WIDTH, max(1, int(page_image.height * scale))))
        if scaled.height > _EVIDENCE_CARD_PAGE_SHOT_MAX_HEIGHT:
            ratio = _EVIDENCE_CARD_PAGE_SHOT_MAX_HEIGHT / scaled.height
            scaled = scaled.resize((_EVIDENCE_CARD_WIDTH, max(1, int(scaled.height * ratio))))
        page_image = scaled
        card_height = panel_height + scaled.height + margin
    else:
        card_height = panel_height

    card = Image.new("RGB", (_EVIDENCE_CARD_WIDTH, card_height), "white")
    draw = ImageDraw.Draw(card)
    y = margin
    for (font, text), line_height in zip(panel_lines, line_heights):
        draw.text((margin, y), text, font=font, fill=(31, 41, 55))
        y += line_height
    if page_image is not None:
        y += 10
        draw.rectangle([margin, y - 2, _EVIDENCE_CARD_WIDTH - margin, y + 2], fill=(220, 228, 240))
        card.paste(page_image, (0, y + 6))

    buffer = io.BytesIO()
    card.save(buffer, format="PNG")
    return _save_artifact(platform, brand_name, result.prompt or "监测", "evidence.png", buffer.getvalue(), binary=True)


def _build_prompt(brand_name: str, keyword: str) -> str:
    prompt = (keyword or "").strip()
    if not prompt:
        return (brand_name or "").strip()
    return prompt


def _clean_aliases(raw) -> List[str]:
    """把 brands.aliases 的逗号分隔文本拆成别名列表。"""
    if not raw:
        return []
    text = str(raw).replace("，", ",").replace("、", ",")
    out = []
    for item in text.split(","):
        item = item.strip()
        if len(item) >= 2 and item not in out:
            out.append(item)
    return out


def _lookup_brand_aliases(brand_name: str) -> List[str]:
    """按品牌名查别名（品牌别名配置在品牌管理里）。查不到就返回空列表。"""
    name = (brand_name or "").strip()
    if not name:
        return []
    try:
        with get_db() as conn:
            row = conn.execute("SELECT aliases FROM brands WHERE name = ?", (name,)).fetchone()
        raw = None
        if row is not None:
            try:
                raw = row["aliases"]
            except Exception:
                raw = row[0] if len(row) else None
        return _clean_aliases(raw)
    except Exception:
        return []


def _count_brand(answer_text: str, brand_name: str, aliases: Optional[List[str]] = None) -> int:
    """统计品牌被提及次数：品牌全名或任一别名命中都算。

    注意：别名通常比全名短（例如全名"某某国际大酒店"，别名"某某"），
    AI 回答往往只用短名。这里取各词命中次数的最大值，避免
    全名/别名互相包含导致重复计数。
    """
    if not answer_text:
        return 0
    terms = []
    for item in [brand_name, *(aliases or [])]:
        item = (item or "").strip()
        if len(item) >= 2 and item not in terms:
            terms.append(item)
    if not terms:
        return 0
    low = answer_text.lower()
    return max(low.count(term.lower()) for term in terms)


TRAILING_URL_CHARS = ".,;:!?，。；：！？、）)]}》>\"'`"
INTERNAL_SOURCE_DOMAINS = {
    "doubao.com",
    "www.doubao.com",
    "qianwen.aliyun.com",
    "yuanbao.tencent.com",
    "cloud.tencent.com",
}
STATIC_SOURCE_SUFFIXES = (
    ".css", ".js", ".mjs", ".json", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".svg", ".ico", ".woff", ".woff2", ".ttf", ".map",
)

# 埋点 / 统计 / CDN 资源域名：这些不是"引用来源"，必须过滤，
# 否则会出现 lf3-short.ibytedapm.com 这类假来源污染验证结果。
TELEMETRY_DOMAIN_SUFFIXES = (
    "ibytedapm.com",
    "bytedapm.com",
    "bytednsdoc.com",
    "zijieapi.com",
    "volces.com",
    "byteoversea.com",
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "umeng.com",
    "umengcloud.com",
    "talkingdata.com",
    "sentry.io",
    "bugly.qq.com",
    "datadoghq.com",
    "newrelic.com",
    "mixpanel.com",
    "amplitude.com",
    # 腾讯系性能监控/配置下发域名：只出现在 preconnect/runtime state 里，
    # 不是 AI 引用来源，不过滤会污染 source 列表（实测 rumt-zh.com 假来源）。
    "rumt-zh.com",
    "rumt.qq.com",
    "snowflake.qq.com",
    "rdelivery.qq.com",
)
TELEMETRY_DOMAIN_KEYWORDS = (
    "apm.", "-apm", "apm-", "analytics", "telemetry", "beacon",
)


def _is_telemetry_domain(domain: str) -> bool:
    d = (domain or "").lower()
    if not d:
        return False
    for suffix in TELEMETRY_DOMAIN_SUFFIXES:
        if d == suffix or d.endswith("." + suffix):
            return True
    return any(keyword in d for keyword in TELEMETRY_DOMAIN_KEYWORDS)


def _normalize_url(url: str) -> str:
    if not url:
        return ""
    cleaned = str(url).strip().strip(TRAILING_URL_CHARS)
    cleaned = cleaned.replace("&amp;", "&")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https"):
        return ""

    # Some AI pages wrap source links behind redirect/proxy URLs. Unwrap the
    # common query parameters only when they contain an absolute URL.
    qs = parse_qs(parsed.query)
    for key in ("url", "target", "redirect", "redirect_url", "source", "u"):
        raw = (qs.get(key) or [None])[0]
        if raw:
            candidate = unquote(raw)
            if candidate.startswith(("http://", "https://")):
                return _normalize_url(candidate)
    return cleaned


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


def _is_internal_domain(domain: str) -> bool:
    domain = (domain or "").lower()
    return any(domain == item or domain.endswith("." + item) for item in INTERNAL_SOURCE_DOMAINS)


def _is_probable_source(url: str) -> bool:
    normalized = _normalize_url(url)
    if not normalized:
        return False
    parsed = urlparse(normalized)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()
    if not domain or _is_internal_domain(domain) or _is_telemetry_domain(domain):
        return False
    if path.endswith(STATIC_SOURCE_SUFFIXES):
        return False
    return True


def _dedupe_sources(items: List[SourceEvidence]) -> List[SourceEvidence]:
    seen = set()
    result = []
    for item in items:
        normalized = _normalize_url(item.url)
        if not normalized or not _is_probable_source(normalized) or normalized in seen:
            continue
        item.url = normalized
        item.domain = _domain(normalized)
        seen.add(normalized)
        item.title = (item.title or item.domain or normalized).strip()[:240]
        item.source_order = len(result) + 1
        result.append(item)
    return result


def _verify_sources(items: List[SourceEvidence]) -> List[SourceEvidence]:
    verified = []
    for item in items:
        ok, reason = verify_source_url(item.url, live_check=True)
        item.source_verified = ok
        item.check_status = "verified" if ok else reason
        if ok:
            verified.append(item)
    return verified


def _save_evidence_page(platform: str, brand_name: str, keyword: str, result: PlatformMonitorResult, page_html: str = "") -> str:
    sources = result.sources or []
    source_items = "\n".join(
        f"<li><a href=\"{html_lib.escape(src.url)}\" target=\"_blank\" rel=\"noopener noreferrer\">"
        f"{html_lib.escape(src.title or src.domain or src.url)}</a>"
        f" <span>{html_lib.escape(src.domain or '')}</span></li>"
        for src in sources[:20]
    ) or "<li>无可验证来源链接</li>"
    screenshot_link = ""
    if result.screenshot_path:
        screenshot_link = f"<p><strong>截图路径：</strong>{html_lib.escape(result.screenshot_path)}</p>"
    raw_excerpt = html_lib.escape(re.sub(r"\s+", " ", page_html or "")[:3000])
    body = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html_lib.escape(PLATFORMS.get(platform, {}).get('name', platform))} GEO监测原始证据</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; margin: 0; background: #f6f8fb; color: #1f2937; }}
    main {{ max-width: 1080px; margin: 24px auto; background: #fff; border: 1px solid #dbe3ef; border-radius: 8px; padding: 28px; }}
    h1 {{ margin: 0 0 18px; font-size: 24px; }}
    h2 {{ margin-top: 28px; font-size: 18px; }}
    .meta {{ display: grid; grid-template-columns: 140px 1fr; gap: 8px 14px; font-size: 14px; }}
    pre {{ white-space: pre-wrap; word-break: break-word; background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px; line-height: 1.7; }}
    a {{ color: #1658dc; }}
    li {{ margin: 8px 0; }}
    .warn {{ color: #b45309; }}
  </style>
</head>
<body>
<main>
  <h1>{html_lib.escape(PLATFORMS.get(platform, {}).get('name', platform))} GEO监测原始证据</h1>
  <section class="meta">
    <strong>平台</strong><span>{html_lib.escape(platform)}</span>
    <strong>品牌</strong><span>{html_lib.escape(brand_name or '')}</span>
    <strong>关键词</strong><span>{html_lib.escape(keyword or '')}</span>
    <strong>实际提问</strong><span>{html_lib.escape(result.prompt or '')}</span>
    <strong>采集时间</strong><span>{html_lib.escape(datetime.now().isoformat(timespec='seconds'))}</span>
    <strong>验证状态</strong><span>{html_lib.escape(result.verification_status or '')}</span>
    <strong>失败原因</strong><span class="warn">{html_lib.escape(result.failure_reason or '')}</span>
  </section>
  {screenshot_link}
  <h2>AI回答文本</h2>
  <pre>{html_lib.escape(result.answer_text or '未采集到回答文本')}</pre>
  <h2>引用/来源链接</h2>
  <ol>{source_items}</ol>
  <h2>页面源码摘录</h2>
  <pre>{raw_excerpt}</pre>
</main>
</body>
</html>"""
    return _save_artifact(platform, brand_name, keyword, "evidence.html", body)


def _source_summary(sources: List[SourceEvidence]) -> Optional[str]:
    if not sources:
        return None
    return " | ".join((s.title or s.domain or s.url)[:120] for s in sources[:5])


def _extract_sources_from_text(text: str) -> List[SourceEvidence]:
    """Extract source URLs that the AI model printed as plain text.

    Yuanbao and other chat UIs often render citations as visible text rather
    than anchors, so relying on `<a href>` misses legitimate evidence.
    """
    if not text:
        return []

    sources: List[SourceEvidence] = []
    markdown_links = re.findall(r"\[([^\]]{1,180})\]\((https?://[^)\s]+)\)", text)
    markdown_urls = {url for _, url in markdown_links}
    for title, url in markdown_links:
        normalized = _normalize_url(url)
        if normalized:
            sources.append(SourceEvidence(title=title.strip(), url=normalized, domain=_domain(normalized)))

    url_pattern = r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+"
    for match in re.finditer(url_pattern, text):
        url = _normalize_url(match.group(0))
        if not url or match.group(0) in markdown_urls:
            continue
        start = max(0, match.start() - 80)
        prefix_lines = text[start:match.start()].splitlines()
        prefix = (prefix_lines[-1] if prefix_lines else "").strip(" -:：|　\t")
        sources.append(SourceEvidence(title=prefix[:120], url=url, domain=_domain(url)))

    return _dedupe_sources(sources)


def _extract_sources_recursive(payload: Any) -> List[SourceEvidence]:
    sources: List[SourceEvidence] = []
    url_keys = {"url", "link", "href", "source_url", "page_url", "reference_url", "referenceUrl"}
    title_keys = {"title", "name", "site_name", "source_name", "text", "source_title", "reference_title"}
    list_title_keys = {"references", "reference", "citations", "citation", "sources", "sourceList", "source_list"}

    def walk(value: Any, parent_key: str = ""):
        if isinstance(value, dict):
            candidate_url = ""
            candidate_title = ""
            for key, raw in value.items():
                key_l = str(key).lower()
                if key_l in url_keys and isinstance(raw, str) and raw.startswith(("http://", "https://")):
                    candidate_url = raw
                if key_l in title_keys and isinstance(raw, str) and not candidate_title:
                    candidate_title = raw
            if candidate_url:
                sources.append(SourceEvidence(title=candidate_title, url=candidate_url, domain=_domain(candidate_url)))
            for key, raw in value.items():
                walk(raw, str(key))
        elif isinstance(value, list):
            parent_l = str(parent_key).lower()
            for item in value:
                if isinstance(item, str) and _normalize_url(item):
                    sources.append(SourceEvidence(title=parent_key[:120] or "来源", url=_normalize_url(item), domain=_domain(_normalize_url(item))))
                elif isinstance(item, dict):
                    candidate_url = ""
                    candidate_title = ""
                    for key, raw in item.items():
                        key_l = str(key).lower()
                        if key_l in url_keys and isinstance(raw, str) and raw.startswith(("http://", "https://")):
                            candidate_url = raw
                        if key_l in title_keys and isinstance(raw, str) and not candidate_title:
                            candidate_title = raw
                    if candidate_url:
                        sources.append(SourceEvidence(title=candidate_title or parent_key[:120] or "来源", url=candidate_url, domain=_domain(candidate_url)))
                    walk(item, parent_key)
                else:
                    walk(item, parent_key)
        elif isinstance(value, str):
            text = value.strip()
            if text and any(marker in parent_key.lower() for marker in list_title_keys):
                urls = re.findall(r"https?://[^\s\"'<>]+", text)
                for url in urls:
                    normalized = _normalize_url(url)
                    if normalized:
                        prefix = text.split(url)[0].strip(" -:：|　	")
                        sources.append(SourceEvidence(title=prefix[:120] or parent_key[:120] or "来源", url=normalized, domain=_domain(normalized)))
            sources.extend(_extract_sources_from_text(text))

    walk(payload)
    return _dedupe_sources(sources)


def _extract_sources_from_runtime_state(page) -> List[SourceEvidence]:
    try:
        payload = page.evaluate(
            """(() => {
                const roots = ['__NEXT_DATA__', '__INITIAL_STATE__', '__NUXT__', '__APOLLO_STATE__', '__PRERENDER_DATA__'];
                const result = {};
                for (const key of roots) {
                    try {
                        const value = window[key];
                        if (value !== undefined && value !== null) result[key] = value;
                    } catch (e) {}
                }
                try {
                    const ldJson = Array.from(document.querySelectorAll('script[type="application/ld+json"]')).map(node => node.textContent || '').filter(Boolean);
                    if (ldJson.length) result.__LD_JSON__ = ldJson;
                } catch (e) {}
                return result;
            })()""",
            timeout=10,
        )
    except Exception:
        payload = None
    if not payload:
        return []
    return _extract_sources_recursive(payload)


def _extract_sources_from_source_section(page) -> List[SourceEvidence]:
    try:
        text = page.evaluate(
            r"""(() => {
                const bodies = [];
                const selectors = ['body', 'main', 'article', '[class*="source"]', '[class*="reference"]', '[class*="citation"]', '[class*="result"]'];
                const seen = new Set();
                for (const selector of selectors) {
                    for (const node of document.querySelectorAll(selector)) {
                        if (!node || seen.has(node)) continue;
                        seen.add(node);
                        const r = node.getBoundingClientRect();
                        const s = getComputedStyle(node);
                        if (r.width < 20 || r.height < 20 || s.display === 'none' || s.visibility === 'hidden') continue;
                        const label = `${node.innerText || node.textContent || ''}`.trim();
                        if (label && (/参考来源|来源|引用|资料/.test(label) || /\d+\s*篇?来源/.test(label))) bodies.push(label);
                    }
                }
                return bodies.slice(-6).join('\n\n');
            })()""",
            timeout=10,
        )
    except Exception:
        text = ""
    if not text:
        return []
    candidates = _extract_sources_from_text(text)
    if candidates:
        return candidates
    urls = re.findall(r"https?://[^\s\"'<>]+", text)
    result = []
    for url in urls:
        normalized = _normalize_url(url)
        if normalized:
            result.append(SourceEvidence(title="来源", url=normalized, domain=_domain(normalized)))
    return _dedupe_sources(result)


def _finalize(result: PlatformMonitorResult, start_time: float, brand_name: str) -> PlatformMonitorResult:
    result.elapsed_seconds = round(time.time() - start_time, 2)
    # 命中品牌全名或任一品牌别名都算"提到了品牌"
    aliases = _lookup_brand_aliases(brand_name)
    result.mention_count = _count_brand(result.answer_text, brand_name, aliases)
    result.brand_mentioned = result.mention_count > 0
    result.is_cited = result.brand_mentioned
    result.sources = _verify_sources(_dedupe_sources(result.sources))
    result.total_results = len(result.sources)
    if result.sources:
        first = result.sources[0]
        result.source_url = first.url
        result.source_domain = first.domain
        result.cited_sources = _source_summary(result.sources)
    # 截图保留原始聊天窗口全页截图（captureBeyondViewport=True 已在 _cdp_screenshot 中设置）。
    # 不再合成证据卡文本面板——文字内容已在 raw_artifact_path 的原始证据 HTML 中，
    # 截图应该是聊天窗口的真实画面（含 AI 回答气泡 + 引用面板完整信息）。
    evidence = validate_monitor_evidence(
        brand_mentioned=result.brand_mentioned,
        is_cited=result.is_cited,
        source_url=result.source_url,
        screenshot_path=result.screenshot_path,
        raw_artifact_path=result.raw_artifact_path,
        live_url_check=False,
    )
    result.source_verified = bool(evidence["source_verified"])
    result.screenshot_verified = bool(evidence["screenshot_verified"])
    result.raw_artifact_verified = bool(evidence["raw_artifact_verified"])
    result.evidence_status = evidence["evidence_status"]
    result.evidence_failure_reason = evidence["evidence_failure_reason"] or None
    if result.failure_reason:
        result.verification_status = "failed"
    elif result.brand_mentioned and result.source_verified and (result.screenshot_verified or result.raw_artifact_verified):
        result.verification_status = "verified_citation"
    elif result.brand_mentioned:
        result.verification_status = "unverified_citation"
    else:
        result.verification_status = "not_cited"
    result.snippet = (result.answer_text or result.html_excerpt or "")[:500] or None
    return result


def run_platform_monitor(platform: str, brand_name: str, keyword: str, timeout: int = 90, run_mode: str = "manual", cancel_checker: Optional[Callable[[], bool]] = None) -> dict:
    _check_cancel(cancel_checker)
    if platform == "qianwen":
        session = check_platform_session("qianwen")
        if session.get("status") in ("logged_in", "browser_open"):
            return _run_browser_platform(platform, brand_name, keyword, timeout, run_mode=run_mode, cancel_checker=cancel_checker).to_dict()
        # 浏览器未就绪时，若已配置 DashScope key，优先走千问官方联网搜索 API，
        # 否则回退浏览器 RPA（可能在内部尝试打开浏览器登录页）。
        if settings.DASHSCOPE_API_KEY:
            return _run_qianwen_api(brand_name, keyword, timeout=timeout).to_dict()
        return _run_browser_platform(platform, brand_name, keyword, timeout, run_mode=run_mode, cancel_checker=cancel_checker).to_dict()
    if platform in ("doubao", "yuanbao"):
        return _run_browser_platform(platform, brand_name, keyword, timeout, run_mode=run_mode, cancel_checker=cancel_checker).to_dict()
    result = PlatformMonitorResult(platform=platform, prompt=_build_prompt(brand_name, keyword))
    result.failure_reason = f"未知或未接入平台: {platform}"
    return _finalize(result, time.time(), brand_name).to_dict()


def _run_qianwen_api(brand_name: str, keyword: str, timeout: int = 90) -> PlatformMonitorResult:
    start = time.time()
    prompt = _build_prompt(brand_name, keyword)
    result = PlatformMonitorResult(platform="qianwen", prompt=prompt)

    if not settings.DASHSCOPE_API_KEY:
        result.failure_reason = "未配置 DASHSCOPE_API_KEY，千问官方联网搜索不可用"
        result.notes = "请在环境变量中配置 DASHSCOPE_API_KEY 后重启后端"
        return _finalize(result, start, brand_name)

    endpoint = settings.DASHSCOPE_BASE_URL.rstrip("/") + "/compatible-mode/v1/chat/completions"
    payload = {
        "model": settings.DASHSCOPE_MODEL,
        "messages": [
            {"role": "system", "content": "你是一个联网搜索助手，回答必须尽量给出引用来源。"},
            {"role": "user", "content": prompt},
        ],
        "enable_search": True,
    }
    try:
        resp = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {settings.DASHSCOPE_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        raw = {
            "status_code": resp.status_code,
            "request": {**payload, "messages": payload["messages"]},
            "response": None,
            "text": resp.text[:5000],
        }
        try:
            raw["response"] = resp.json()
        except Exception:
            raw["response"] = None
        result.raw_artifact_path = _save_artifact("qianwen", brand_name, keyword, "json", raw)
        evidence_sources = _extract_sources_recursive(raw["response"] or {})
        if not evidence_sources:
            evidence_sources = _extract_sources_from_text(raw["text"])
        if not evidence_sources:
            text = str(raw.get("text") or "")
            # Chinese answer snippets frequently render citations as plain lines
            # without explicit URLs. Use the immediate source block to recover
            # nearby URLs or page titles when the API returns a structured list.
            source_block = []
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if any(key in stripped for key in ("来源", "参考", "资料", "引用")):
                    source_block.append(stripped)
                elif source_block and len(source_block) < 20:
                    source_block.append(stripped)
            if source_block:
                block_text = "\n".join(source_block)
                for url in re.findall(r"https?://[^\s\"\'<>]+", block_text):
                    normalized = _normalize_url(url)
                    if normalized:
                        title = block_text.split(url)[0].splitlines()[-1].strip(" -:：|　	") or "来源"
                        evidence_sources.append(SourceEvidence(title=title[:120], url=normalized, domain=_domain(normalized)))
                if not evidence_sources and any(ch.isdigit() for ch in block_text):
                    # Keep the source block as an auditable local artifact even if
                    # the API omits live URLs. This does not upgrade to verified.
                    result.notes = (result.notes or "") + " | 千问返回来源块但未给出可验证URL"
        if resp.status_code >= 400:
            result.failure_reason = f"千问 API 请求失败 HTTP {resp.status_code}: {resp.text[:300]}"
            return _finalize(result, start, brand_name)

        data = raw["response"] or {}
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                result.answer_text = "\n".join(str(item.get("text", item)) if isinstance(item, dict) else str(item) for item in content)
            else:
                result.answer_text = str(content or "")
        result.sources = _dedupe_sources(
            evidence_sources
            + _extract_sources_recursive(data)
            + _extract_sources_from_text(result.answer_text)
        )
        result.notes = "千问官方 API 联网搜索"
    except Exception as exc:
        result.failure_reason = f"千问 API 异常: {exc}"
    return _finalize(result, start, brand_name)


def _cdp_open_page(port: int, target_url: str) -> _CdpPage:
    response = LOCAL_REQUESTS.put(f"http://127.0.0.1:{port}/json/new", params={"url": target_url}, timeout=3)
    if response.status_code >= 400:
        raise RuntimeError(f"Chrome 调试端口创建页面失败 HTTP {response.status_code}")
    tab = response.json()
    ws = tab.get("webSocketDebuggerUrl")
    if not ws:
        tabs = LOCAL_REQUESTS.get(f"http://127.0.0.1:{port}/json", timeout=3).json()
        tab = tabs[-1] if tabs else {}
        ws = tab.get("webSocketDebuggerUrl")
    if not ws:
        raise RuntimeError("Chrome 调试端口未返回页面 websocket")
    page = _CdpPage(ws, target_id=tab.get("id"))
    page.call("Page.enable")
    page.call("Runtime.enable")
    if target_url:
        try:
            page.call("Page.navigate", {"url": target_url}, timeout=5)
        except Exception:
            pass
    return page


# 每平台一把运行锁：同一平台的 CDP 监测必须串行，
# 否则并发任务会在同一个常驻浏览器里互相抢聊天页面。
_PLATFORM_RUN_LOCKS: Dict[str, threading.Lock] = {}


def _platform_run_lock(platform: str) -> threading.Lock:
    # 用 setdefault 保证"取或建"是原子的：get()+[]= 两步在首次并发时
    # 可能各自建一把锁，互斥被静默破坏（而首次并发正是这把锁要防的场景）。
    return _PLATFORM_RUN_LOCKS.setdefault(platform, threading.Lock())


# ---------------------------------------------------------------------------
# 平台风控限额 / 跨进程平台锁
# ---------------------------------------------------------------------------
# 每日提问上限：auto（无人值守轮巡）与 manual（人工单次）**同样生效**——
# 平台风控看的是"提问次数"，不是"谁点的"。此前限额只在 auto 模式生效，
# 导致"人工单次 + 勾一堆关键词"可以连续轰炸而不受限。
# 可用环境变量按平台覆盖，例如 GEO_MONITOR_DAILY_DOUBAO=20。
_RISK_DAILY_LIMITS = {"yuanbao": 6, "doubao": 12, "qianwen": 30}
_RISK_DAILY_DEFAULT = 8

# 两次提问之间的最小间隔（秒）：auto 更保守；manual 允许稍快，但同样必须有下限。
_RISK_MIN_INTERVAL = {
    "yuanbao": {"auto": 8 * 60, "manual": 120},
    "doubao": {"auto": 5 * 60, "manual": 60},
    "qianwen": {"auto": 90, "manual": 30},
}
_RISK_MIN_INTERVAL_DEFAULT = {"auto": 300, "manual": 60}

# 间隔不足时最多等待多久（秒）：超过就直接跳过本次提问并给出原因，
# 不让批量任务在原处长时间空等。可用 GEO_MONITOR_RISK_MAX_WAIT 覆盖。
_RISK_MAX_WAIT = int(os.getenv("GEO_MONITOR_RISK_MAX_WAIT", "180") or 180)

# 跨进程锁参数：陈旧锁（进程崩了没释放）自动回收的时长与等待上限。
_PLATFORM_LOCK_STALE_SECONDS = int(os.getenv("GEO_MONITOR_LOCK_STALE_SECONDS", "900") or 900)
_PLATFORM_LOCK_WAIT_SECONDS = int(os.getenv("GEO_MONITOR_LOCK_WAIT_SECONDS", "240") or 240)


def _daily_limit(platform: str) -> int:
    raw = (os.getenv(f"GEO_MONITOR_DAILY_{platform.upper()}") or "").strip()
    if raw.isdigit():
        return max(1, int(raw))
    return _RISK_DAILY_LIMITS.get(platform, _RISK_DAILY_DEFAULT)


def _min_interval(platform: str, run_mode: str) -> int:
    mode = "auto" if str(run_mode or "manual").lower() == "auto" else "manual"
    table = _RISK_MIN_INTERVAL.get(platform, _RISK_MIN_INTERVAL_DEFAULT)
    return int(table.get(mode) or _RISK_MIN_INTERVAL_DEFAULT[mode])


class platform_browser_lock:
    """跨进程的平台浏览器锁（锁文件 + 陈旧锁自动回收）。

    进程内的 threading.Lock 管不住"后端自检子进程"与"worker 监测进程"同时驱动
    同一个固定浏览器：两边会争抢同一个聊天 tab、输入互相覆盖，并可能触发风控。
    这里用锁文件把并发面收敛到"同一平台同一时刻只有一个提问者"。
    """

    def __init__(self, platform: str, timeout: Optional[int] = None):
        self.platform = platform
        self.timeout = _PLATFORM_LOCK_WAIT_SECONDS if timeout is None else timeout
        self.path = Path(runtime_dir()) / "locks" / f"platform_{platform}.lock"
        self.acquired = False

    def _is_stale(self) -> bool:
        try:
            return (time.time() - self.path.stat().st_mtime) > _PLATFORM_LOCK_STALE_SECONDS
        except OSError:
            return False

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return True  # 目录不可用时退化为"无跨进程锁"，不阻塞监测
        deadline = time.time() + max(0, self.timeout)
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}".encode())
                finally:
                    os.close(fd)
                self.acquired = True
                return True
            except FileExistsError:
                if self._is_stale():
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if time.time() >= deadline:
                    return False
                time.sleep(0.5)
            except OSError:
                return True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except OSError:
            pass
        self.acquired = False

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> bool:
        self.release()
        return False


def _platform_chat_url(platform: str) -> str:
    """平台的"新对话"入口 URL。导航到该 URL 会开启一个全新会话。"""
    if platform == "qianwen":
        return "https://www.qianwen.com/chat"
    return PLATFORMS[platform]["login_url"]


def _cdp_find_platform_tab(port: int, platform: str) -> Optional[dict]:
    """在常驻浏览器里找该平台已有的 tab（优先聊天页）。找不到返回 None。"""
    try:
        tabs = LOCAL_REQUESTS.get(f"http://127.0.0.1:{port}/json", timeout=3).json()
    except Exception:
        return None
    matched = [t for t in tabs if t.get("type") == "page" and url_matches_platform(platform, t.get("url", ""))]
    if not matched:
        return None
    for tab in matched:
        if "/chat" in (tab.get("url") or ""):
            return tab
    return matched[0]


def _cdp_attach_tab(tab: dict) -> _CdpPage:
    """附着到已存在的 tab（不新建窗口）。"""
    ws = tab.get("webSocketDebuggerUrl")
    if not ws:
        raise RuntimeError("平台tab未返回调试websocket")
    page = _CdpPage(ws, target_id=tab.get("id"))
    try:
        page.call("Page.enable")
        page.call("Runtime.enable")
    except Exception:
        # 构造 _CdpPage 时 CDP websocket 已经建立；这里失败必须显式断开，
        # 否则会泄漏一个 socket（且调用方会误判为可复用，另行新开 tab）。
        page.close(close_target=False)
        raise
    return page


def _cdp_reuse_or_open_page(platform: str, port: int) -> tuple:
    """复用该平台已有的聊天 tab；确实没有才新建。

    返回 (page, created)。旧逻辑每个关键词各开一个新 tab，选多个关键词时
    浏览器里会同时冒出多个聊天窗口，极易触发平台风控/封号；改为每平台
    常驻最多一个聊天 tab，后续关键词都在这一个 tab 里开新会话。
    """
    tab = _cdp_find_platform_tab(port, platform)
    if tab:
        try:
            page = _cdp_attach_tab(tab)
        except Exception as exc:
            # 不静默吞掉：附着失败会同时泄漏 socket 并多开一个 tab，
            # 是"浏览器里越开越多窗口"这类问题的第一现场，留痕便于排障。
            print(f"[MONITOR] 复用 {platform} 聊天 tab 失败，改为新建: {str(exc)[:120]}")
        else:
            return page, False
    return _cdp_open_page(port, _platform_chat_url(platform)), True


def _ensure_platform_chat_page(platform: str, page: _CdpPage) -> None:
    """Force a platform login tab onto the actual chat surface when possible.

    Some chat apps land on a home/landing page even when the login browser opens
    correctly. For monitor runs we need the real prompt surface, not a marketing
    or history page, otherwise the captured evidence never becomes a real answer.
    """
    try:
        # _CdpPage 没有 url 属性：旧写法 getattr(page, "url", "") 恒为空字符串，
        # 导致下面 qianwen / yuanbao 两个「拉回聊天页」的分支从未真正生效。
        # 真实地址要问页面本身。
        url = str(page.evaluate("() => location.href") or "")
    except Exception:
        url = ""
    if platform == "qianwen" and "qianwen.com" in url and "/chat" not in url:
        try:
            page.call("Page.navigate", {"url": "https://www.qianwen.com/chat"}, timeout=8)
            page.call("Runtime.evaluate", {"expression": """(() => {
              try {
                const openers = Array.from(document.querySelectorAll('button, [role="button"], a, div, span')).filter(el => {
                  const r = el.getBoundingClientRect();
                  const s = getComputedStyle(el);
                  const text = (el.innerText || el.textContent || ''), aria = el.getAttribute('aria-label') || '', title = el.getAttribute('title') || '';
                  return r.width > 8 && r.height > 8 && s.display !== 'none' && s.visibility !== 'hidden' && /新建对话|开始对话|新聊天|创建对话/.test(text + aria + title);
                });
                if (openers[0]) openers[0].click();
              } catch (e) {}
            })()""", "awaitPromise": True, "returnByValue": True}, timeout=5)
        except Exception:
            pass
    elif platform == "yuanbao" and "yuanbao.tencent.com" in url and "/chat" not in url:
        try:
            page.call("Page.navigate", {"url": "https://yuanbao.tencent.com/chat"}, timeout=8)
            page.call("Runtime.evaluate", {"expression": """(() => {
              try {
                const openers = Array.from(document.querySelectorAll('button, [role="button"], a, div, span')).filter(el => {
                  const r = el.getBoundingClientRect();
                  const s = getComputedStyle(el);
                  const text = (el.innerText || el.textContent || ''), aria = el.getAttribute('aria-label') || '', title = el.getAttribute('title') || '';
                  return r.width > 8 && r.height > 8 && s.display !== 'none' && s.visibility !== 'hidden' && /新建对话|开始对话|新聊天|创建对话|联网搜索/.test(text + aria + title);
                });
                if (openers[0]) openers[0].click();
              } catch (e) {}
            })()""", "awaitPromise": True, "returnByValue": True}, timeout=5)
        except Exception:
            pass


def _cdp_wait_ready(page: _CdpPage, timeout: int = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            state = page.evaluate("document.readyState")
            text_len = page.evaluate("(document.body && document.body.innerText || '').length") or 0
            if state in {"interactive", "complete"} and int(text_len) > 10:
                return
        except Exception:
            pass
        time.sleep(0.5)


_EDITOR_SELECTORS = [
    "textarea",
    '[contenteditable="true"]',
    "div[role='textbox']",
    "div[role='combobox']",
    "input[type='text']",
    ".ql-editor",
    ".tiptap.ProseMirror",
    ".ProseMirror",
    "[data-placeholder]",
]


def _cdp_wait_input_ready(page: _CdpPage, timeout: int = 12) -> bool:
    """等编辑器（输入框）真正渲染出来再输入。

    豆包/元宝新版工作台首屏渲染偏慢，过早输入会一路退到"坐标点击 + 逐字输入"
    的兜底路径，最后报「消息未能成功发送（找不到输入框…）」——实测这种失败
    并非页面不可用，而是时机问题。这里先轮询输入框出现，提高发送成功率。
    """
    deadline = time.time() + max(1, timeout)
    while time.time() < deadline:
        try:
            if _cdp_deep_query(page, _EDITOR_SELECTORS, require_text=False):
                return True
        except Exception:
            pass
        time.sleep(0.6)
    return False


def _cdp_text(page: _CdpPage) -> str:
    return page.evaluate(
        """(() => {
          const nodes = Array.from(document.querySelectorAll('main, article, [class*=message], [class*=chat], body'));
          const text = nodes.map(n => n.innerText || '').filter(Boolean).join('\\n');
          return text.replace(/\\n{3,}/g, '\\n\\n').trim();
        })()""",
        timeout=10,
    ) or ""


def _cdp_deep_query(page: _CdpPage, selectors: List[str], require_text: bool = False) -> Optional[dict]:
    payload = json.dumps(selectors, ensure_ascii=False)
    script = f"""(() => {{
      const selectors = {payload};
      const visible = (el) => {{
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 10 && r.height > 10 && s.display !== 'none' && s.visibility !== 'hidden';
      }};
      const scan = (root) => {{
        const found = [];
        for (const sel of selectors) {{
          try {{
            found.push(...Array.from(root.querySelectorAll(sel)));
          }} catch (e) {{}}
        }}
        for (const node of Array.from(root.querySelectorAll('*'))) {{
          if (node.shadowRoot) found.push(...scan(node.shadowRoot));
        }}
        for (const frame of Array.from(root.querySelectorAll('iframe'))) {{
          try {{
            if (frame.contentDocument && frame.contentDocument.body) found.push(...scan(frame.contentDocument)); 
          }} catch (e) {{}}
        }}
        return found.filter(visible);
      }};
      const items = scan(document);
      const pick = items.find(el => {{
        const t = (el.innerText || el.textContent || '').trim();
        const a = el.getAttribute('aria-label') || '';
        const ti = el.getAttribute('title') || '';
        return {str(require_text).lower()} ? (t.length > 0 || a.length > 0 || ti.length > 0) : true;
      }}) || null;
      if (!pick) return null;
      const r = pick.getBoundingClientRect();
      return {{ tag: pick.tagName, text: (pick.innerText || pick.textContent || '').trim(), aria: pick.getAttribute('aria-label') || '', title: pick.getAttribute('title') || '', x: r.left + r.width / 2, y: r.top + r.height / 2 }};
    }})()"""
    try:
        return page.evaluate(script, timeout=12) or None
    except Exception:
        return None


def _cdp_html(page: _CdpPage) -> str:
    return page.evaluate("document.documentElement.outerHTML", timeout=10) or ""


def _cdp_screenshot(platform: str, brand_name: str, keyword: str, page: _CdpPage) -> Optional[str]:
    try:
        data = page.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}, timeout=12).get("data")
        if not data:
            return None
        return _save_artifact(platform, brand_name, keyword, "png", base64.b64decode(data), binary=True)
    except Exception:
        return None


def _cdp_fill_js(page: _CdpPage, prompt: str) -> dict:
    """用 JS 把提示词直接写进编辑器：focus + execCommand('insertText')。

    元宝/豆包用 Quill（.ql-editor），CDP 逐字按键在 React/Quill 受控编辑器上
    经常不落字（坐标点击又容易命中输入区上方的建议卡片遮罩），导致
    "以为发了其实没发"。execCommand 是 Quill/富文本编辑器的标准插入方式。
    """
    script = f"""(() => {{
      const prompt = {json.dumps(prompt, ensure_ascii=False)};
      const compact = (t) => (t || '').replace(/[\\s\\u200b\\ufeff]+/g, '');
      const visible = el => {{
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 40 && r.height > 8 && s.display !== 'none' && s.visibility !== 'hidden';
      }};
      const all = [];
      const push = (root) => {{
        try {{ all.push(...Array.from(root.querySelectorAll('.tiptap.ProseMirror, .ql-editor, textarea, [contenteditable="true"], div[role="textbox"]'))); }} catch (e) {{}}
        try {{ Array.from(root.querySelectorAll('*')).forEach(n => n.shadowRoot && push(n.shadowRoot)); }} catch (e) {{}}
        try {{ Array.from(root.querySelectorAll('iframe')).forEach(f => f.contentDocument && push(f.contentDocument)); }} catch (e) {{}}
      }};
      push(document);
      const editors = all.filter(visible);
      const scored = editors.map(el => {{
        const r = el.getBoundingClientRect();
        const label = `${{el.innerText || ''}} ${{el.getAttribute('placeholder') || ''}} ${{el.getAttribute('aria-label') || ''}} ${{el.className || ''}}`;
        let score = 1;
        if (el.matches('.ql-editor, [contenteditable="true"]')) score += 8;
        if (/发送消息|有问题|尽管问|提问|向.*提问|输入/.test(label)) score += 4;
        if (r.top > window.innerHeight * 0.5) score += 4;
        return {{ el, score, top: r.top }};
      }}).sort((a, b) => b.score - a.score || b.top - a.top);
      const el = (scored[0] && scored[0].el) || editors.at(-1);
      if (!el) return {{ ok: false, err: 'editor_not_found' }};
      el.focus();
      el.scrollIntoView({{ block: 'nearest' }});
      // 清空既有内容（Quill 用选区+delete，textarea 用原生 setter）
      try {{
        if ('value' in el) {{
          const proto = Object.getPrototypeOf(el);
          const setter = Object.getOwnPropertyDescriptor(proto, 'value') && Object.getOwnPropertyDescriptor(proto, 'value').set;
          if (setter) setter.call(el, ''); else el.value = '';
        }} else {{
          const sel = window.getSelection();
          sel.selectAllChildren(el);
          document.execCommand('delete');
        }}
      }} catch (e) {{}}
      let inserted = false;
      try {{ inserted = document.execCommand('insertText', false, prompt); }} catch (e) {{}}
      if (!inserted || !compact(el.value !== undefined ? el.value : el.innerText).includes(compact(prompt))) {{
        try {{
          if ('value' in el) {{
            const proto = Object.getPrototypeOf(el);
            const setter = Object.getOwnPropertyDescriptor(proto, 'value') && Object.getOwnPropertyDescriptor(proto, 'value').set;
            if (setter) setter.call(el, prompt); else el.value = prompt;
          }} else {{
            el.innerText = prompt;
          }}
        }} catch (e) {{}}
      }}
      el.dispatchEvent(new InputEvent('input', {{ bubbles: true, composed: true }}));
      const value = ('value' in el) ? String(el.value || '') : String(el.innerText || '');
      return {{
        ok: compact(value).includes(compact(prompt)),
        focused: document.activeElement === el,
        inserted,
        kind: ('value' in el) ? 'textarea' : 'contenteditable',
        editor_text: value.slice(0, 80),
      }};
    }})()"""
    try:
        return page.evaluate(script, timeout=12) or {"ok": False, "err": "evaluate_failed"}
    except Exception as exc:
        return {"ok": False, "err": str(exc)[:120]}


def _cdp_focus_editor_js(page: _CdpPage) -> bool:
    """JS 聚焦聊天编辑器（不依赖鼠标事件）。"""
    script = """(() => {
      const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 100 && r.height > 8 && s.display !== 'none' && s.visibility !== 'hidden';
      };
      const els = Array.from(document.querySelectorAll('.tiptap.ProseMirror, .ql-editor, div[contenteditable="true"], div[role="textbox"], textarea')).filter(visible);
      if (!els.length) return false;
      els[0].focus();
      return document.activeElement === els[0];
    })()"""
    try:
        return bool(page.evaluate(script, timeout=8))
    except Exception:
        return False


def _cdp_send_button_state(page: _CdpPage) -> dict:
    """探测发送按钮是否存在且已激活（可发送状态）。

    豆包激活态 = 右下角按钮 class 含 bg-dbx-fill-highlight/bg-primary；
    未激活（灰态）时这些类不存在。元宝/千问用 aria-label"发送"识别。
    找不到可判定按钮时返回 found=False（调用方跳过激活校验，行为同旧版）。
    """
    script = """(() => {
      const visible = el => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 6 && r.height > 6 && s.display !== 'none' && s.visibility !== 'hidden';
      };
      const nodes = Array.from(document.querySelectorAll('button, [role="button"], div, span')).filter(visible);
      let best = null, bestScore = 0;
      for (const el of nodes) {
        const r = el.getBoundingClientRect();
        const aria = el.getAttribute('aria-label') || '';
        const title = el.getAttribute('title') || '';
        const text = (el.innerText || '').trim();
        const cls = String(el.className || '');
        let sc = 0;
        if (/^发送(消息)?$/.test(aria) || /^发送(消息)?$/.test(title)) sc += 14;
        if (/^(发送|Send)$/.test(text)) sc += 12;
        if (/sendbutton|send-btn|icon-send|send_button|btn-send/i.test(cls)) sc += 10;
        if (/fill-highlight|bg-primary|btn-primary|bg-blue/i.test(cls)) sc += 8;
        if (sc <= 0) continue;
        if (/不发送|发送给|发送报告|重新发送|转发/.test(`${aria} ${title} ${text}`)) continue;
        if (r.left > window.innerWidth * 0.5 && r.top > window.innerHeight * 0.4) sc += 4;
        if (el.disabled || el.getAttribute('aria-disabled') === 'true') sc -= 100;
        if (sc > bestScore) { best = el; bestScore = sc; }
      }
      if (!best) {
        // 兜底：右下角小型图标按钮（豆包新版发送键无文本/aria）
        const vh = window.innerHeight, vw = window.innerWidth;
        const iconBtns = nodes.filter(el => {
          if (!visible(el)) return false;
          const r = el.getBoundingClientRect();
          if (r.width > 50 || r.height > 50) return false;
          if (r.left < vw * 0.55 || r.top < vh * 0.6) return false;
          if (!el.querySelector('svg, img, path')) return false;
          if ((el.innerText || '').trim().length > 0) return false;
          return true;
        });
        if (iconBtns.length) {
          iconBtns.sort((a, b) => b.getBoundingClientRect().right - a.getBoundingClientRect().right);
          best = iconBtns[0];
        }
      }
      if (!best) return { found: false, active: false, cls: '' };
      const cls = String(best.className || '');
      const disabled = best.disabled || best.getAttribute('aria-disabled') === 'true';
      const named = /发送|send/i.test(`${best.getAttribute('aria-label') || ''} ${best.getAttribute('title') || ''} ${cls}`.replace(/unsent|notsend/g, ''))
        || /^(发送|Send)$/.test((best.innerText || '').trim());
      const highlight = /fill-highlight|bg-primary|btn-primary|bg-blue/i.test(cls);
      // 激活 = 未禁用且有明确"发送"命名或高亮类。靠位置兜底找到的匿名
      // 灰色图标按钮（豆包假填充态）判为未激活，触发上层恢复路径。
      const active = !disabled && (named || highlight);
      return { found: true, active, cls, disabled: !!disabled };
    })()"""
    try:
        return page.evaluate(script, timeout=8) or {"found": False, "active": False, "cls": ""}
    except Exception:
        return {"found": False, "active": False, "cls": ""}


def _cdp_wait_send_button_active(page: _CdpPage, timeout: float = 6) -> bool:
    """等待发送键进入可发送状态；按钮不可识别时直接返回 True（跳过校验）。"""
    deadline = time.time() + timeout
    while True:
        state = _cdp_send_button_state(page)
        if not state.get("found") or state.get("active"):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def _cdp_hard_clear_editor(page: _CdpPage) -> None:
    """DOM 级清空编辑器（假填充恢复用：内部状态为空但 DOM 残留文字时，
    trusted Ctrl+A 可能选不中残留内容，先在 DOM 层清掉再重灌）。"""
    script = """(() => {
      const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 100 && r.height > 8 && s.display !== 'none' && s.visibility !== 'hidden';
      };
      const els = Array.from(document.querySelectorAll('.tiptap.ProseMirror, .ql-editor, div[contenteditable="true"], div[role="textbox"], textarea')).filter(visible);
      const el = els.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
      if (!el) return false;
      el.focus();
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(el);
      sel.removeAllRanges();
      sel.addRange(range);
      document.execCommand('delete', false, null);
      if (('value' in el) ? String(el.value || '') : String(el.innerText || '').trim()) {
        // execCommand 无效时强制清 DOM（textarea 用 value，contenteditable 用 textContent）
        if ('value' in el) { el.value = ''; }
        else { el.textContent = ''; }
        el.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true }));
      }
      return true;
    })()"""
    try:
        page.evaluate(script, timeout=8)
    except Exception:
        pass


def _cdp_insert_text(page: _CdpPage, text: str) -> bool:
    """CDP Input.insertText 插入文本（trusted input 事件，React 受控编辑器认它）。

    execCommand/innerText 只改 DOM 不进 React 状态；Input.dispatchKeyEvent 的
    char 事件对中文不可靠；insertText 是 Playwright 中文输入的同款机制。
    """
    try:
        page.call("Input.insertText", {"text": text}, timeout=5)
        return True
    except Exception:
        return False


def _cdp_editor_own_text(page: _CdpPage) -> str:
    """只读聊天编辑器元素自身的文本（active_editor_text 会把 [class*=editor]
    容器、侧栏元素全拼进来，做"是否已发送"判定时噪音太大）。"""
    script = """(() => {
      const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 100 && r.height > 8 && s.display !== 'none' && s.visibility !== 'hidden';
      };
      const els = Array.from(document.querySelectorAll('.tiptap.ProseMirror, .ql-editor, div[contenteditable="true"], textarea')).filter(visible);
      const el = els.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
      if (!el) return '';
      return 'value' in el ? String(el.value || '') : String(el.innerText || '');
    })()"""
    try:
        return str(page.evaluate(script, timeout=6) or "")
    except Exception:
        return ""


def _cdp_trusted_clear(page: _CdpPage) -> None:
    """trusted 清空：Ctrl+A + Backspace（CDP键事件，编辑器认 trusted 输入）。"""
    try:
        page.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65, "modifiers": 2}, timeout=5)
        page.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65, "modifiers": 2}, timeout=5)
        page.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8}, timeout=5)
        page.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8}, timeout=5)
        time.sleep(0.2)
    except Exception:
        pass


def _cdp_fill_trusted(page: _CdpPage, prompt: str) -> bool:
    """trusted 输入主路径：JS focus → CDP 全选删除 → Input.insertText。

    新版豆包工作台（tiptap/ProseMirror）和千问（React）都不认 execCommand
    写入的 DOM 文本——编辑器内部状态为空，发送键不生效。Input.insertText
    产生 trusted input 事件，是这三家新编辑器统一可用的写入方式。

    成功标准不是"DOM 里有字"而是"发送键已激活"：水合/视图切换竞态会留下
    "DOM 有字、编辑器内部状态为空、发送键灰色"的假填充（2026-09-29 豆包
    复用 tab 第 2/3 题连续假填充，消息发不出去）。发送键不可识别的平台
    （找不到可判定按钮）退回只验 DOM 文本。最多尝试 3 次。
    """
    for attempt in range(3):
        try:
            if not _cdp_focus_editor_js(page):
                time.sleep(1.5)
                continue
            _cdp_trusted_clear(page)
            page.call("Input.insertText", {"text": prompt}, timeout=5)
            time.sleep(0.8)
            if not _input_contains_prompt(_cdp_editor_own_text(page), prompt):
                # 文本没进去——可能是编辑器还没就绪，等一下重试
                time.sleep(1.5)
                continue
            if _cdp_wait_send_button_active(page, timeout=2.5):
                return True
            # DOM 有字但发送键没激活 = 假填充：DOM 级清空后重灌，
            # 让编辑器内部状态与 DOM 重新对齐
            _cdp_hard_clear_editor(page)
            time.sleep(1.0)
        except Exception:
            time.sleep(1.5)
    return False


def _cdp_click_send_js(page: _CdpPage) -> bool:
    """点发送按钮。不限标签（元宝的发送键是 div[aria-label=发送]，不是 button），
    用 JS click 代替坐标鼠标事件（坐标在弹层/遮罩下会点偏）。

    找不到按钮时回退 Enter 键——部分平台改版后发送键是 SVG 图标无文本/aria。
    豆包新版发送键是 36x36 的 SVG 图标按钮，class 含 bg-dbx-fill-highlight，
    无 aria-label/innerText/data-testid，只能靠位置+样式特征兜底。
    """
    script = """(() => {
      const visible = el => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 6 && r.height > 6 && s.display !== 'none' && s.visibility !== 'hidden';
      };
      const label = el => `${el.innerText || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''} ${el.className || ''}`.trim();
      const nodes = Array.from(document.querySelectorAll('button, [role="button"], a, div, span, svg')).filter(visible);
      let best = null, bestScore = 0;
      for (const el of nodes) {
        const r = el.getBoundingClientRect();
        const l = label(el);
        let sc = 0;
        const aria = el.getAttribute('aria-label') || '';
        const title = el.getAttribute('title') || '';
        const text = (el.innerText || '').trim();
        const cls = String(el.className || '');
        const testid = el.getAttribute('data-testid') || '';
        // 精确匹配"发送"
        if (/^发送(消息)?$/.test(aria) || /^发送(消息)?$/.test(title)) sc += 14;
        if (/^(发送|Send)$/.test(text)) sc += 12;
        // 类名包含 send 相关
        if (/sendbutton|send-btn|icon-send|send_button|btn-send/i.test(cls)) sc += 10;
        if (/send/i.test(cls) && !/unsent|notsend/i.test(cls)) sc += 6;
        // data-testid 包含 send
        if (/send/i.test(testid)) sc += 10;
        // type=submit 在底部区域
        if (el.tagName === 'BUTTON' && el.type === 'submit') sc += 4;
        // 豆包新版：高亮主按钮样式（bg-dbx-fill-highlight）
        if (/fill-highlight|bg-primary|btn-primary/i.test(cls)) sc += 8;
        if (sc <= 0) continue;
        // 排除非发送按钮
        if (/不发送|发送给|发送报告|重新发送|转发/.test(l)) continue;
        // 位置加分：右下角（聊天发送键的常见位置）
        if (r.left > window.innerWidth * 0.5 && r.top > window.innerHeight * 0.4) sc += 4;
        if (r.width <= 110 && r.height <= 70) sc += 2;
        // 禁用状态大减分
        if (el.disabled || el.getAttribute('aria-disabled') === 'true') sc -= 100;
        if (sc > bestScore) { best = el; bestScore = sc; }
      }

      // 兜底：如果精确匹配没找到，按位置+样式特征找最像发送键的按钮
      // 豆包/元宝新版发送键是右下角的图标按钮，无文本/aria
      if (!best || bestScore <= 0) {
        const vh = window.innerHeight;
        const vw = window.innerWidth;
        const iconBtns = Array.from(document.querySelectorAll('button, [role="button"]')).filter(el => {
          if (!visible(el)) return false;
          if (el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
          const r = el.getBoundingClientRect();
          // 小型图标按钮（不超过 50x50）
          if (r.width > 50 || r.height > 50) return false;
          // 在右下区域（x > 60% 宽度，y > 70% 高度）
          if (r.left < vw * 0.55 || r.top < vh * 0.6) return false;
          // 必须含 SVG/IMG 图标
          if (!el.querySelector('svg, img, path')) return false;
          // 排除有文本的按钮（发送键无文本）
          if ((el.innerText || '').trim().length > 0) return false;
          // 排除设置等已知按钮
          const aria = el.getAttribute('aria-label') || '';
          if (/设置|设置|新建|历史|清空|删除|关闭|菜单|更多/.test(aria)) return false;
          return true;
        });
        // 在候选中优先选最靠右的（发送键通常是右下角最后一个按钮）
        if (iconBtns.length > 0) {
          iconBtns.sort((a, b) => b.getBoundingClientRect().right - a.getBoundingClientRect().right);
          best = iconBtns[0];
          bestScore = 1;
        }
      }

      if (best && bestScore > 0) {
        best.click();
        return true;
      }
      // 回退：在当前聚焦的编辑器上派发 Enter 键
      const el = document.activeElement;
      if (!el) return false;
      for (const type of ['keydown', 'keypress', 'keyup']) {
        el.dispatchEvent(new KeyboardEvent(type, {
          key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
          bubbles: true, cancelable: true
        }));
      }
      return false;
    })()"""
    try:
        return bool(page.evaluate(script, timeout=8))
    except Exception:
        return False


def _cdp_press_enter_js(page: _CdpPage) -> None:
    """在当前聚焦的编辑器上派发 Enter 键（Quill 监听 keydown）。"""
    script = """(() => {
      const el = document.activeElement;
      if (!el) return;
      for (const type of ['keydown', 'keypress', 'keyup']) {
        el.dispatchEvent(new KeyboardEvent(type, { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }));
      }
    })()"""
    try:
        page.evaluate(script, timeout=5)
    except Exception:
        pass


def _cdp_fill_and_submit(page: _CdpPage, prompt: str) -> bool:
    try:
        current_url = page.evaluate("location.href", timeout=5) or ""
    except Exception:
        current_url = ""
    if "qianwen.com" in current_url:
        return _cdp_fill_and_submit_qianwen_rpa(page, prompt)

    before = ""
    try:
        before = _cdp_text(page)
    except Exception:
        before = ""

    # 主路径：trusted 输入（豆包新版工作台/千问等新编辑器唯一可靠方式）
    typed = _cdp_fill_trusted(page, prompt)
    if not typed:
        # 降级1：execCommand 直插（元宝 Quill 等老编辑器可用）
        fill = _cdp_fill_js(page, prompt)
        typed = bool(fill.get("ok"))
    if not typed:
        # 降级2：坐标点击 + CDP 逐字输入（最后兜底）
        info = _cdp_deep_query(page, ["textarea", "[contenteditable='true']", "div[role='textbox']", "input[type='text']", "[data-placeholder]", "[placeholder]", ".ql-editor", ".editor", ".input", ".textarea"], require_text=False) or {}
        if not info:
            return False
        try:
            x = float(info.get("x") or 0)
            y = float(info.get("y") or 0)
            page.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, timeout=5)
            page.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1}, timeout=5)
            page.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}, timeout=5)
            for ch in prompt:
                page.call("Input.dispatchKeyEvent", {"type": "char", "text": ch}, timeout=5)
                time.sleep(random.uniform(0.02, 0.08))
        except Exception:
            pass
        time.sleep(0.6)
        if not _input_contains_prompt(_cdp_editor_own_text(page), prompt):
            return False

    # 兜底恢复：三轮填充都停在假填充（DOM 有字、发送键不激活）时，
    # DOM 级清空 + 重新聚焦 + 再灌一次；发送键识别不了的平台跳过此步。
    if not _cdp_wait_send_button_active(page, timeout=1):
        _cdp_hard_clear_editor(page)
        time.sleep(0.5)
        if _cdp_focus_editor_js(page):
            _cdp_trusted_clear(page)
            page.call("Input.insertText", {"text": prompt}, timeout=5)
        _cdp_wait_send_button_active(page, timeout=5)
    # 发送：JS 点发送键（不限标签），确认窗口内以"编辑器自身清空"为准
    _cdp_click_send_js(page)
    send_clicked_again = False
    enter_tried = False
    deadline = time.time() + 12
    while time.time() < deadline:
        time.sleep(0.8)
        own = _cdp_editor_own_text(page)
        # 编辑器已清空且不含提示词 = 消息已发出
        if not _input_contains_prompt(own, prompt) and len(own.strip()) < 200:
            return True
        # 编辑器还有提示词但页面已出现用户气泡 = 已发出
        try:
            page_text = _cdp_text(page)
            own_compact = _compact_input_text(own)
            page_compact = _compact_input_text(page_text)
            # 从页面文本中去掉编辑器自身文本再检查，避免"编辑器里有内容"被误判
            if own_compact and own_compact in page_compact:
                page_without_editor = page_compact.replace(own_compact, "")
            else:
                page_without_editor = page_compact
            if _compact_input_text(prompt) in page_without_editor and not _input_contains_prompt(own, prompt):
                return True
        except Exception:
            pass
        # 编辑器仍有提示词：约 3 秒时重点发送键，约 6 秒时补 trusted Enter
        if _input_contains_prompt(own, prompt):
            if not send_clicked_again and time.time() > deadline - 9:
                _cdp_click_send_js(page)
                send_clicked_again = True
            elif not enter_tried and time.time() > deadline - 6:
                try:
                    page.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}, timeout=5)
                    page.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}, timeout=5)
                except Exception:
                    pass
                enter_tried = True

    # 兜底：trusted Enter（编辑器里有内容时新UI也认）
    try:
        page.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}, timeout=5)
        page.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}, timeout=5)
    except Exception:
        pass
    time.sleep(1.5)
    own = _cdp_editor_own_text(page)
    if not _input_contains_prompt(own, prompt) and len(own.strip()) < 200:
        return True
    # 最终判定：编辑器仍有提示词 = 消息未发出，直接返回 False
    # （旧逻辑检查"prompt 是否出现在整页文本"是假阳性——编辑器内容也是页面文本的一部分）
    if _input_contains_prompt(own, prompt):
        return False
    # 编辑器文本为空但无法确认是否真的发出（可能编辑器选择器失效）
    # 从页面文本中去掉编辑器文本后检查是否有用户气泡
    try:
        page_text = _cdp_text(page)
        own_compact = _compact_input_text(own)
        page_compact = _compact_input_text(page_text)
        page_without_editor = page_compact.replace(own_compact, "") if own_compact else page_compact
        return _compact_input_text(prompt) in page_without_editor
    except Exception:
        return False


def _cdp_click(page: _CdpPage, x: float, y: float) -> None:
    page.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, timeout=5)
    page.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1}, timeout=5)
    page.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}, timeout=5)


def _cdp_type_chars(page: _CdpPage, text: str, low: float = 0.02, high: float = 0.08) -> None:
    for ch in text:
        page.call("Input.dispatchKeyEvent", {"type": "char", "text": ch}, timeout=5)
        time.sleep(random.uniform(low, high))


def _cdp_key_combo(page: _CdpPage, key: str, code: str, vk: int, modifiers: int = 0) -> None:
    # Do not use macOS Command/Meta combinations in RPA. On some Qianwen pages
    # focus can leak outside Chrome and trigger system shortcuts such as
    # "About This Mac".
    if modifiers:
        return
    page.call(
        "Input.dispatchKeyEvent",
        {"type": "keyDown", "key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk, "modifiers": modifiers},
        timeout=5,
    )
    page.call(
        "Input.dispatchKeyEvent",
        {"type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk, "modifiers": modifiers},
        timeout=5,
    )


def _cdp_qianwen_editor_text(page: _CdpPage) -> str:
    script = """(() => {
      const visible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 10 && r.height > 10 && s.display !== 'none' && s.visibility !== 'hidden';
      };
      const main = (el) => {
        const r = el.getBoundingClientRect();
        return r.left >= Math.max(72, window.innerWidth * 0.07) && r.top > window.innerHeight * 0.2 && r.bottom < window.innerHeight * 0.995;
      };
      const valueOf = (el) => {
        if (!el) return '';
        if ('value' in el) return el.value || '';
        return el.innerText || el.textContent || '';
      };
      const nodes = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], div[role="textbox"], input[type="text"], input[type="search"], [data-placeholder], [placeholder], [class*="input"], [class*="editor"], [class*="textarea"]'))
        .filter(visible).filter(main);
      const active = document.activeElement;
      if (active && visible(active)) {
        const activeText = valueOf(active).trim();
        if (activeText) return activeText;
      }
      return nodes.map(valueOf).filter(Boolean).join('\\n').trim();
    })()"""
    try:
        return str(page.evaluate(script, timeout=5) or "")
    except Exception:
        return ""


def _cdp_active_editor_text(page: _CdpPage) -> str:
    script = """(() => {
      const visible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 10 && r.height > 10 && s.display !== 'none' && s.visibility !== 'hidden';
      };
      const valueOf = (el) => {
        if (!el) return '';
        if ('value' in el) return el.value || '';
        return el.innerText || el.textContent || '';
      };
      const active = document.activeElement;
      if (active && visible(active)) {
        const activeText = valueOf(active).trim();
        if (activeText) return activeText;
      }
      const nodes = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], div[role="textbox"], input[type="text"], input[type="search"], [data-placeholder], [placeholder], [class*="input"], [class*="editor"], [class*="textarea"]'))
        .filter(visible)
        .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
      return nodes.map(valueOf).filter(Boolean).join('\\n').trim();
    })()"""
    try:
        return str(page.evaluate(script, timeout=5) or "")
    except Exception:
        return ""


def _compact_input_text(text: str) -> str:
    return re.sub(r"[\s\u200b\ufeff]+", "", text or "")


def _input_contains_prompt(editor_text: str, prompt: str) -> bool:
    return bool(_compact_input_text(prompt) and _compact_input_text(prompt) in _compact_input_text(editor_text))


def _cdp_qianwen_input_target(page: _CdpPage) -> Optional[dict]:
    script = """(() => {
      const visible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 12 && r.height > 12 && s.display !== 'none' && s.visibility !== 'hidden';
      };
      const textOf = (el) => `${el.innerText || el.textContent || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''} ${el.className || ''}`.trim();
      const collect = (root, selector) => {
        const out = [];
        try { out.push(...Array.from(root.querySelectorAll(selector))); } catch (e) {}
        try {
          for (const node of Array.from(root.querySelectorAll('*'))) {
            if (node.shadowRoot) out.push(...collect(node.shadowRoot, selector));
          }
        } catch (e) {}
        try {
          for (const frame of Array.from(root.querySelectorAll('iframe'))) {
            if (frame.contentDocument && frame.contentDocument.body) out.push(...collect(frame.contentDocument, selector));
          }
        } catch (e) {}
        return out;
      };
      const isMain = (el) => {
        const r = el.getBoundingClientRect();
        // 阈值要兼容窄窗口/侧栏收起的布局（实测 929px 宽、编辑器 left=98，
        // 旧阈值 240px 会把真编辑器整排除掉，导致输入目标返回 null）
        if (r.left < Math.max(72, window.innerWidth * 0.07)) return false;
        if (r.top < window.innerHeight * 0.2 || r.bottom > window.innerHeight * 0.995) return false;
        return true;
      };
      const selectors = 'textarea, [contenteditable="true"], div[role="textbox"], div[role="combobox"], input[type="text"], input[type="search"], [data-placeholder], [placeholder], .ql-editor, .editor, .input, .textarea, [class*="input"], [class*="editor"], [class*="textarea"], [class*="chat"], [class*="prompt"]';
      const editors = collect(document, selectors).filter(visible).filter(isMain);
      const preferred = editors
        .filter(el => el.getAttribute('contenteditable') === 'true' || el.getAttribute('role') === 'textbox' || /回车|提问|发送|请输入|有问题|向千问提问|写下你的问题|开始提问|输入问题/.test(textOf(el)))
        .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0] || editors.at(-1);
      if (preferred) {
        const r = preferred.getBoundingClientRect();
        return { x: r.left + Math.min(28, r.width * 0.15), y: r.top + Math.min(24, r.height * 0.45), kind: 'editor' };
      }
      return null;
    })()"""
    try:
        target = page.evaluate(script, timeout=8) or None
        if target:
            return target
        return None
    except Exception:
        return None


def _cdp_qianwen_send_target(page: _CdpPage) -> Optional[dict]:
    script = """(() => {
      const visible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 10 && r.height > 10 && s.display !== 'none' && s.visibility !== 'hidden';
      };
      const direct = Array.from(document.querySelectorAll('button[aria-label="发送消息"], button[title="发送消息"]')).filter(visible)[0];
      if (direct) {
        const r = direct.getBoundingClientRect();
        return { x: r.left + r.width / 2, y: r.top + r.height / 2, score: 100, direct: true };
      }
      const textOf = (el) => `${el.innerText || el.textContent || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''} ${el.className || ''}`.trim();
      const nodes = Array.from(document.querySelectorAll('button')).filter(visible);
      const candidates = nodes.map(el => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        const label = textOf(el);
        let score = 0;
        if (/发送|提问|send|arrow|上箭头/.test(label)) score += 10;
        if (r.right > window.innerWidth * 0.74) score += 4;
        if (r.top > window.innerHeight * 0.34 && r.bottom < window.innerHeight * 0.98) score += 4;
        if (r.width >= 18 && r.width <= 80 && r.height >= 18 && r.height <= 80) score += 2;
        if (el.disabled || el.getAttribute('aria-disabled') === 'true' || s.pointerEvents === 'none') score -= 100;
        return { el, score, x: r.left + r.width / 2, y: r.top + r.height / 2 };
      }).filter(item => item.score > 0).sort((a, b) => b.score - a.score);
      if (!candidates.length) return null;
      return { x: candidates[0].x, y: candidates[0].y, score: candidates[0].score };
    })()"""
    try:
        return page.evaluate(script, timeout=8) or None
    except Exception:
        return None


def _cdp_wait_qianwen_input_ready(page: _CdpPage, timeout: int = 15) -> None:
    """等千问输入区水合稳定：contenteditable 和 发送消息 按钮都出现后再操作。

    React 水合会重建 DOM——过早 insertText 的文本会被直接抹掉，
    表现为"以为发了其实没发"。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ready = page.evaluate(
                """(() => {
                  const ed = document.querySelector('div[contenteditable="true"]');
                  const btn = document.querySelector('button[aria-label="发送消息"]');
                  return !!(ed && btn);
                })()""",
                timeout=4,
            )
            if ready:
                time.sleep(1.2)  # 再留一点渲染稳定时间
                return
        except Exception:
            pass
        time.sleep(0.6)


def _cdp_fill_and_submit_qianwen_rpa(page: _CdpPage, prompt: str) -> bool:
    try:
        before_text = _cdp_text(page)
    except Exception:
        before_text = ""
    # 千问（www.qianwen.com，React 受控编辑器）实测可行链路：
    #   等水合稳定 → JS focus → Input.insertText（trusted，React 状态同步、发送按钮点亮）
    #   → JS click button[aria-label=发送消息]
    # 注意：本机 CDP 的 Input.dispatchMouseEvent 无响应（超时），不能靠坐标点击；
    # execCommand 只改 DOM 不进 React 状态，不能用。
    _cdp_wait_qianwen_input_ready(page)

    def _prompt_in_page() -> bool:
        try:
            return _compact_input_text(prompt) in _compact_input_text(_cdp_text(page))
        except Exception:
            return False

    def _sent() -> bool:
        # 发送成功的硬证据：提示词作为用户气泡出现在页面里（或页面显著增长）。
        # 只看"编辑器清空"不可靠——React 水合重建 DOM 也会把没发出去的文本抹掉。
        if _prompt_in_page():
            return True
        try:
            after_text = _cdp_text(page)
        except Exception:
            after_text = ""
        return len(after_text) > len(before_text) + 60

    for attempt in range(2):
        typed = False
        if _cdp_focus_editor_js(page):
            if _cdp_insert_text(page, prompt):
                time.sleep(0.8)
                typed = _input_contains_prompt(_cdp_qianwen_editor_text(page), prompt)
        if not typed and attempt == 0:
            # 兜底：可信键盘逐字输入
            target = _cdp_qianwen_input_target(page)
            if target:
                try:
                    _cdp_click(page, float(target["x"]), float(target["y"]))
                    time.sleep(0.4)
                except Exception:
                    pass
                try:
                    _cdp_type_chars(page, prompt, 0.04, 0.1)
                    time.sleep(0.8)
                    typed = _input_contains_prompt(_cdp_qianwen_editor_text(page), prompt)
                except Exception:
                    typed = False
        if not typed:
            continue

        # 精准点 发送消息 按钮（只点未禁用的），失败走通用发送匹配
        clicked = False
        try:
            clicked = bool(page.evaluate(
                """(() => {
                  const btn = Array.from(document.querySelectorAll('button[aria-label="发送消息"]'))
                    .find(el => !el.disabled && el.getAttribute('aria-disabled') !== 'true');
                  if (!btn) return false;
                  btn.click();
                  return true;
                })()""",
                timeout=8,
            ))
        except Exception:
            clicked = False
        if not clicked:
            _cdp_click_send_js(page)

        # 千问联网搜索回答启动较慢：给足 25 秒确认发送是否生效，
        # 期间提示词一旦作为用户气泡出现在页面即算发出。
        deadline = time.time() + 25
        while time.time() < deadline:
            time.sleep(1.0)
            if _sent():
                return True

        # 可信 Enter 兜底（此时 React 状态已有文本，Enter 可发送）
        try:
            page.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}, timeout=5)
            page.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}, timeout=5)
        except Exception:
            pass
        deadline = time.time() + 10
        while time.time() < deadline:
            time.sleep(1.0)
            if _sent():
                return True

        # 只有编辑器仍残留提示词（明确没发出去）才重试，避免重复发送
        if not _input_contains_prompt(_cdp_qianwen_editor_text(page), prompt):
            return True
    return False


GENERATING_MARKERS = (
    "停止生成", "停止回答", "停止输出", "正在生成", "生成中",
    "正在思考", "思考中", "深度思考", "正在深度思考", "深度思考中",
    "正在搜索", "查找中", "正在查找",
)


def _unique_content_len(text: str) -> int:
    """去掉重复行后的内容长度。

    豆包在思考阶段会先渲染一张"思考卡片"（例如"我来查一下…掌握该酒店信息"），
    这张卡片会在页面上停留几十秒。如果只看原始长度，很容易把它误当成答案收工；
    去重后它只有几十个字，而真正的回答有几百字，用它区分更稳。
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    seen = set()
    kept = []
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        kept.append(line)
    return len("\n".join(kept))


def _cdp_has_stop_button(page: _CdpPage) -> bool:
    """页面上是否还有"停止生成"按钮（说明 AI 仍在输出）。"""
    try:
        return bool(page.evaluate(
            """(() => {
                const nodes = document.querySelectorAll('button, [role="button"], [aria-label], [title]');
                for (const el of nodes) {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    if (r.width < 4 || r.height < 4 || s.display === 'none' || s.visibility === 'hidden') continue;
                    const blob = ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).trim();
                    if (/停止生成|停止回答|停止输出|停止响应/.test(blob)) return true;
                }
                return false;
            })()""",
            timeout=4,
        ))
    except Exception:
        return False


def _cdp_wait_for_answer(page: _CdpPage, before_text: str, prompt: str, timeout: int, cancel_checker: Optional[Callable[[], bool]] = None) -> None:
    """等待 AI 回答结束。

    注意：不能只看"文本不再增长就收工"。侧边栏、用户气泡、时间戳、以及豆包的
    "思考卡片"都会先出现然后稳定下来；阈值太低就会在 AI 还没答完时提前返回，最后
    抓到一堆残渣，被误判成 not_cited（实测豆包答了 55 秒，旧逻辑 18 秒就收工）。

    判定"答完"的条件：
      - 去重后的有效内容达到 150 字（思考卡片去重后通常只有几十字）
      - 连续 8 秒不再增长
      - 页面上没有"正在生成/停止生成"这类仍在输出的标记
      - 至少等待 15 秒
    """
    start = time.time()
    deadline = start + max(int(timeout or 0), 60)
    previous_len = 0
    stable_count = 0
    while time.time() < deadline:
        _check_cancel(cancel_checker)
        try:
            page_text = _cdp_text(page)
            current = _suffix_after_baseline(before_text, page_text, prompt) if before_text else page_text
            generating = any(marker in (page_text or "") for marker in GENERATING_MARKERS)
            if (not generating) and stable_count >= 8:
                generating = _cdp_has_stop_button(page)
            if len(current) > previous_len:
                previous_len = len(current)
                stable_count = 0
            else:
                stable_count += 1
            unique_len = _unique_content_len(current)
            if (
                not generating
                and unique_len >= 150
                and stable_count >= 8
                and (time.time() - start) >= 15
            ):
                return
        except Exception:
            pass
        time.sleep(1)


def _cdp_pick_answer_candidate(page: _CdpPage, prompt: str) -> str:
    """从页面里挑最像"AI 回答正文"的节点，避免把侧边栏/用户气泡当答案。"""
    try:
        items = page.evaluate(
            """(() => {
                const selectors = [
                    '[data-testid*="message" i]', '[data-testid*="answer" i]',
                    '[class*="message" i]', '[class*="answer" i]', '[class*="assistant" i]',
                    '[class*="markdown" i]', '[class*="receive" i]', '[role="article"] article', 'article'
                ];
                const seen = new Set(); const out = [];
                for (const sel of selectors) {
                    for (const node of document.querySelectorAll(sel)) {
                        if (seen.has(node)) continue;
                        seen.add(node);
                        const r = node.getBoundingClientRect();
                        const s = window.getComputedStyle(node);
                        if (r.width < 20 || r.height < 20 || s.display === 'none' || s.visibility === 'hidden') continue;
                        const t = (node.innerText || node.textContent || '').trim();
                        if (t.length >= 40) out.push(t);
                    }
                }
                return out.slice(-24);
            })()""",
            timeout=8,
        ) or []
    except Exception:
        return ""

    best = ""
    best_score = (False, 0)
    for raw in items:
        cleaned = _clean_answer_text(str(raw), prompt)
        if len(cleaned) < 40:
            continue
        score = (bool(_extract_sources_from_text(cleaned)), len(cleaned))
        if score > best_score:
            best, best_score = cleaned, score
    return best[-8000:]


def _cdp_sources(page: _CdpPage, answer_text: str = "") -> List[SourceEvidence]:
    try:
        links = page.evaluate(
            """(() => {
              const pick = [];
              const attrs = ['href', 'data-url', 'data-href', 'data-link', 'data-src', 'data-target-url'];
              const toAbs = (raw) => {
                if (!raw) return '';
                try {
                  const value = String(raw).trim();
                  if (/^https?:\\/\\//i.test(value)) return value;
                  if (value.startsWith('//')) return new URL(location.protocol + value).href;
                  if (value.startsWith('/')) return new URL(value, location.origin).href;
                } catch (e) {}
                return '';
              };
              const add = (el, url) => {
                const abs = toAbs(url);
                if (!abs || !/^https?:/.test(abs)) return;
                pick.push({
                  title: (el.innerText || el.textContent || el.title || el.getAttribute('aria-label') || '').trim(),
                  url: abs
                });
              };
              Array.from(document.querySelectorAll('a[href]')).forEach(a => add(a, a.href));
              Array.from(document.querySelectorAll('*')).forEach(el => {
                attrs.forEach(name => {
                  const raw = el.getAttribute && el.getAttribute(name);
                  if (raw) add(el, raw);
                });
                const onclick = (el.getAttribute && el.getAttribute('onclick')) || '';
                const match = onclick.match(/https?:\\/\\/[^\\s"'<>]+/);
                if (match) add(el, match[0]);
              });
              return pick.slice(0, 40);
            })()""",
            timeout=10,
        ) or []
        anchor_sources = [SourceEvidence(title=item.get("title") or "", url=item.get("url") or "", domain=_domain(item.get("url") or "")) for item in links]
        runtime_sources = _extract_sources_from_runtime_state(page)
        section_sources = _extract_sources_from_source_section(page)
        return _dedupe_sources(anchor_sources + runtime_sources + section_sources + _extract_sources_from_text(answer_text))
    except Exception:
        return _extract_sources_from_text(answer_text)


# 新版豆包（2026-09 "豆包工作"UI）的参考面板没有稳定类名："参考 N 篇资料"
# 是一段文本，外层是 cursor-pointer 的 div，点击后引用来源的外链 a[href]
# 才会渲染进 DOM；再点一次即收起（toggle）。
_REF_TOGGLE_JS = r"""(() => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const hits = [];
    while (walker.nextNode()) {
        const t = (walker.currentNode.textContent || '').trim();
        if (/参考\s*\d+\s*篇/.test(t)) hits.push(walker.currentNode.parentElement);
    }
    if (!hits.length) return 'no-hit';
    // 只操作最后一条（当前回答）；从文本节点向上找最近的 cursor-pointer
    // 容器——事件只冒泡不下传，点过高的祖先无法触发 toggle。
    let target = hits[hits.length - 1];
    for (let i = 0; i < 4 && target; i++) {
        if (getComputedStyle(target).cursor === 'pointer') break;
        target = target.parentElement;
    }
    (target || hits[hits.length - 1]).click();
    return 'clicked';
})()"""

_LEGACY_REF_CLICK_JS = r"""(() => {
    const candidates = Array.from(document.querySelectorAll(
        '.reference-wrap-iEjeb3, .link-title-igf0OC, .search-content-iMifAk, .reference-wrap, [class*="reference-wrap"], [class*="link-title"]'));
    const visible = el => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 10 && r.height > 10 && s.display !== 'none' && s.visibility !== 'hidden';
    };
    const target = candidates.find(visible);
    if (target) { target.click(); return true; }
    return false;
})()"""

_EXT_LINK_COUNT_JS = r"""(() => {
    let n = 0;
    document.querySelectorAll('a[href]').forEach(a => {
        try {
            if (new URL(a.href).hostname !== location.hostname) n++;
        } catch (e) {}
    });
    return n;
})()"""


def _cdp_eval_js(page: _CdpPage, expression: str, timeout: int = 6):
    try:
        res = page.call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, timeout=timeout)
        return (res or {}).get("result", {}).get("value")
    except Exception:
        return None


def _external_link_count(page: _CdpPage) -> int:
    try:
        return int(_cdp_eval_js(page, _EXT_LINK_COUNT_JS) or 0)
    except (TypeError, ValueError):
        return 0


def _expand_reference_panel_cdp(page: _CdpPage) -> None:
    """展开参考来源面板，让引用外链进入 DOM 供 _cdp_sources 抓取。

    外链数从 0 变为 >0 视为展开成功；重试是给面板晚于正文渲染留时间。
    千问/元宝的引用链接天然在 DOM 里，进来发现已有外链会直接返回。
    """
    for _ in range(3):
        if _external_link_count(page) > 0:
            return
        outcome = _cdp_eval_js(page, _REF_TOGGLE_JS)
        if outcome != "clicked":
            _cdp_eval_js(page, _LEGACY_REF_CLICK_JS)
        time.sleep(1.2)
        if _external_link_count(page) > 0:
            return


def _collapse_reference_panel_cdp(page: _CdpPage) -> None:
    """收起豆包参考面板（toggle 再点一次）。

    会话 tab 跨关键词复用，收起可避免上一关键词展开的来源
    混入下一关键词的抓取。
    """
    _cdp_eval_js(page, _REF_TOGGLE_JS)
    time.sleep(1.5)


def _run_browser_platform_cdp(platform: str, brand_name: str, keyword: str, timeout: int = 90, run_mode: str = "manual", cancel_checker: Optional[Callable[[], bool]] = None) -> PlatformMonitorResult:
    start = time.time()
    prompt = _build_prompt(brand_name, keyword)
    result = PlatformMonitorResult(platform=platform, prompt=prompt)
    page = None
    # 是否真的把问题发出去了：决定是否消耗"每日提问额度"。
    submitted = False
    # 是否走到过"发送"这一步：没发出去也算一次节流，避免失败诱发高频重试。
    attempted_send = False
    try:
        _check_cancel(cancel_checker)
        risk_block = _preflight_risk_gate(platform, run_mode=run_mode)
        if risk_block:
            result.failure_reason = risk_block
            result.notes = "平台风控/安全限额熔断，已停止本次浏览器查询"
            return _finalize(result, start, brand_name)
        # 固定浏览器没开时自动拉起（否则下面 CDP 连接会直接报
        # "Max retries exceeded ... NewConnectionError"）。
        session = _ensure_browser_session(platform)
        if session.get("status") in ("not_connected", "expired"):
            plat_name = PLATFORMS[platform]["name"]
            result.failure_reason = session.get("notes") or f"{plat_name} 固定浏览器未启动"
            result.notes = (
                f"已尝试自动打开 {plat_name} 浏览器但未成功；"
                "请到「系统设置 → 监测引擎状态」点「打开登录」，"
                "在弹出的浏览器里完成人工登录后再重试。"
            )
            return _finalize(result, start, brand_name)
        page, _created_tab = _cdp_reuse_or_open_page(platform, get_port(platform))
        # 复用的 tab 很可能处于后台；后台 tab 的 CDP Input 域事件可能静默不生效，
        # 表现为"消息未能成功发送"。统一把 tab/窗口调到前台再操作。
        try:
            page.call("Page.bringToFront", timeout=5)
        except Exception:
            pass
        # 复用tab时导航到聊天入口 URL 开启全新会话，避免上一个关键词的
        # 对话内容污染本次提问；新建tab时这步是无害的同URL导航。
        try:
            page.call("Page.navigate", {"url": _platform_chat_url(platform)}, timeout=8)
        except Exception as exc:
            print(f"[MONITOR] {platform} 导航到聊天入口失败，继续尝试当前页面: {str(exc)[:100]}")
        _ensure_platform_chat_page(platform, page)
        _cdp_wait_ready(page, timeout=20)
        # 输入框就绪再输入（否则会退到不可靠的坐标点击兜底路径）
        input_ready = _cdp_wait_input_ready(page)
        # 编辑器出现后额外等 3 秒，确保 React/tiptap 组件完全挂载可交互
        # （_cdp_wait_input_ready 只确认 DOM 元素存在，不保证组件已就绪）
        time.sleep(3)
        _human_pause(platform, 1.8, 4.2)
        html = _cdp_html(page)
        visible_text = _cdp_text(page)
        risk = _detect_platform_risk(platform, PLATFORMS[platform]["login_url"], visible_text, html, stage="pre_send")
        if risk:
            _trip_risk_gate(platform, risk, cooldown_hours=24 if platform == "yuanbao" else 8)
            result.failure_reason = risk
            result.html_excerpt = re.sub(r"\s+", " ", html)[:1200]
            result.screenshot_path = _cdp_screenshot(platform, brand_name, keyword, page)
            result.raw_artifact_path = _save_evidence_page(platform, brand_name, keyword, result, html)
            return _finalize(result, start, brand_name)
        before_text = visible_text
        _check_cancel(cancel_checker)
        attempted_send = True
        if not _cdp_fill_and_submit(page, prompt):
            hint = "" if input_ready else "；输入框在等待期内始终未出现（页面可能未加载完或已改版）"
            result.failure_reason = (
                "消息未能成功发送（找不到输入框、发送键无效或页面改版），可能未登录或页面结构已变化" + hint
            )
            result.html_excerpt = re.sub(r"\s+", " ", html)[:1200]
            result.screenshot_path = _cdp_screenshot(platform, brand_name, keyword, page)
            result.raw_artifact_path = _save_evidence_page(platform, brand_name, keyword, result, html)
            return _finalize(result, start, brand_name)
        # 问题已真实发出：这一次才算消耗平台提问额度
        submitted = True
        # 豆包/千问/元宝的深度回答实测可能超过 60 秒，给足等待时间。
        _cdp_wait_for_answer(page, before_text, prompt, max(int(timeout or 0), 150), cancel_checker=cancel_checker)
        _check_cancel(cancel_checker)
        html = _cdp_html(page)
        text = _cdp_text(page)
        suffix = _clean_answer_text(_suffix_after_baseline(before_text, text, prompt), prompt)
        if len(suffix) >= 40:
            result.answer_text = suffix
        else:
            # 拿不到"新增回答"时，退回到页面里最像回答的节点，
            # 而不是把整页（含侧边栏/用户气泡）当答案。
            result.answer_text = _cdp_pick_answer_candidate(page, prompt)
        if len(result.answer_text) < 40:
            result.failure_reason = "未捕获到 AI 回答正文（可能仍在生成、未登录或被风控）"
        _expand_reference_panel_cdp(page)
        html = _cdp_html(page)
        text = _cdp_text(page)
        result.html_excerpt = re.sub(r"\s+", " ", html)[:1200]
        result.sources = _cdp_sources(page, result.answer_text)
        result.screenshot_path = _cdp_screenshot(platform, brand_name, keyword, page)
        result.raw_artifact_path = _save_evidence_page(platform, brand_name, keyword, result, html)
        if platform == "doubao":
            # 取证完成后再收起面板，避免外链残留混入下一关键词的抓取
            _collapse_reference_panel_cdp(page)
        result.notes = f"{PLATFORMS[platform]['name']} 固定浏览器真实查询（CDP）"
    except Exception as exc:
        if isinstance(exc, MonitorCancelled):
            result.failure_reason = "任务已取消"
            result.notes = "用户取消任务，平台监测已中断"
        else:
            result.failure_reason = f"{PLATFORMS.get(platform, {}).get('name', platform)} CDP监测异常: {exc}"
    finally:
        # 记账：真实发出 → 计入当日额度；只尝试过没发出 → 只更新节流时间。
        # 放在 finally 里保证异常/早退路径也会记账（否则限额会漏计）。
        if submitted or attempted_send:
            try:
                _mark_platform_run(platform, daily_increment=submitted)
            except Exception:
                pass
        if page:
            # 只断开调试连接，保留平台聊天 tab 给后续关键词复用
            # （每平台常驻最多一个聊天tab，避免多窗口风控）。
            page.close(close_target=False)
    return _finalize(result, start, brand_name)


def _run_browser_platform(platform: str, brand_name: str, keyword: str, timeout: int = 90, run_mode: str = "manual", cancel_checker: Optional[Callable[[], bool]] = None) -> PlatformMonitorResult:
    if platform in ("doubao", "qianwen", "yuanbao"):
        # 同一平台串行执行：并发任务共用常驻聊天tab会互相覆盖输入。
        # 进程内用 _platform_run_lock，进程外用锁文件——后端的"运行自检"是子进程
        # （platform_sessions._do_self_test_chat），只靠进程内锁挡不住它与监测抢标签。
        with _platform_run_lock(platform):
            with platform_browser_lock(platform) as got_lock:
                if not got_lock:
                    name = PLATFORMS.get(platform, {}).get("name", platform)
                    blocked = PlatformMonitorResult(platform=platform, prompt=_build_prompt(brand_name, keyword))
                    blocked.failure_reason = (
                        f"{name} 固定浏览器正被其它进程用于提问（自检或另一批监测），"
                        "本次提问已跳过以避免并发抢标签触发风控"
                    )
                    blocked.notes = "跨进程平台锁未获取到；稍后重试，或等对方结束后再跑"
                    return _finalize(blocked, time.time(), brand_name)
                return _run_browser_platform_cdp(platform, brand_name, keyword, timeout, run_mode=run_mode, cancel_checker=cancel_checker)
    start = time.time()
    prompt = _build_prompt(brand_name, keyword)
    result = PlatformMonitorResult(platform=platform, prompt=prompt)
    risk_block = _preflight_risk_gate(platform, run_mode=run_mode)
    if risk_block:
        result.failure_reason = risk_block
        result.notes = "平台风控熔断，需人工确认账号恢复后再运行"
        return _finalize(result, start, brand_name)
    session = _ensure_browser_session(platform)
    if session.get("status") in ("not_connected", "expired"):
        result.failure_reason = session.get("notes") or "固定浏览器未连接或登录态未知"
        result.notes = f"请先在设置页打开 {PLATFORMS[platform]['name']} 登录窗口"
        return _finalize(result, start, brand_name)
    if session.get("status") == "captcha_required":
        result.captcha_status = "detected"
        result.failure_reason = "平台当前处于验证码/安全验证状态"
        return _finalize(result, start, brand_name)

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        result.failure_reason = f"Playwright 不可用: {exc}"
        return _finalize(result, start, brand_name)

    cdp_url = f"http://127.0.0.1:{get_port(platform)}"
    try:
        _check_cancel(cancel_checker)
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url, timeout=5000)
            context = browser.contexts[0] if browser.contexts else None
            page = None
            if context:
                for candidate in context.pages:
                    if url_matches_platform(platform, candidate.url):
                        page = candidate
                        break
                if page is None and context.pages:
                    page = context.pages[0]
            if page is None:
                if not context:
                    raise RuntimeError("无法从 Chrome 调试端口取得可操作页面")
                before_ids = {id(candidate) for candidate in context.pages}
                try:
                    LOCAL_REQUESTS.put(
                        f"http://127.0.0.1:{get_port(platform)}/json/new",
                        params={"url": PLATFORMS[platform]["login_url"]},
                        timeout=2,
                    )
                    time.sleep(0.8)
                except Exception:
                    pass
                context = browser.contexts[0] if browser.contexts else context
                page = next((candidate for candidate in reversed(context.pages) if id(candidate) not in before_ids), None)
                if page is None:
                    raise RuntimeError("无法从 Chrome 调试端口创建可操作页面")
            if not url_matches_platform(platform, page.url):
                page.goto(PLATFORMS[platform]["login_url"], wait_until="domcontentloaded", timeout=timeout * 1000)
            _ensure_platform_chat_page(platform, page)

            _human_pause(platform, 1.8, 4.2)
            html = page.content()
            visible_text = _extract_visible_text(page)
            risk = _detect_platform_risk(platform, page.url, visible_text, html, stage="pre_send")
            if risk:
                _trip_risk_gate(platform, risk, cooldown_hours=24 if platform == "yuanbao" else 8)
                result.captcha_status = "detected" if "验证" in risk or "验证码" in risk else "none"
                result.failure_reason = risk
                result.notes = "已触发风控熔断，系统停止自动重试"
                result.html_excerpt = re.sub(r"\s+", " ", html)[:1200]
                try:
                    result.screenshot_path = _save_artifact(platform, brand_name, keyword, "png", page.screenshot(full_page=True, timeout=8000), binary=True)
                except Exception:
                    pass
                result.raw_artifact_path = _save_evidence_page(platform, brand_name, keyword, result, html)
                browser.close()
                return _finalize(result, start, brand_name)
            captcha = detect_and_solve(page.url, html, visible_text=visible_text)
            if captcha.get("detected"):
                _trip_risk_gate(platform, captcha.get("notes") or "检测到验证码/安全验证", cooldown_hours=24 if platform == "yuanbao" else 8)
                result.captcha_status = "solved" if captcha.get("solved") else "detected"
                result.failure_reason = captcha.get("notes") or "检测到验证码"
                result.html_excerpt = re.sub(r"\s+", " ", html)[:1200]
                try:
                    result.screenshot_path = _save_artifact(platform, brand_name, keyword, "png", page.screenshot(full_page=True, timeout=8000), binary=True)
                except Exception:
                    pass
                result.raw_artifact_path = _save_evidence_page(platform, brand_name, keyword, result, html)
                browser.close()
                return _finalize(result, start, brand_name)

            before_text = _extract_visible_text(page)
            _check_cancel(cancel_checker)
            if not _fill_and_submit(page, prompt):
                result.failure_reason = "未找到可输入的聊天框，可能未登录或页面结构已变化"
                result.html_excerpt = re.sub(r"\s+", " ", html)[:1200]
                try:
                    result.screenshot_path = _save_artifact(platform, brand_name, keyword, "png", page.screenshot(full_page=True, timeout=8000), binary=True)
                except Exception:
                    pass
                result.raw_artifact_path = _save_evidence_page(platform, brand_name, keyword, result, html)
                browser.close()
                return _finalize(result, start, brand_name)

            _wait_for_answer(page, before_text=before_text, prompt=prompt, timeout=timeout, cancel_checker=cancel_checker)
            _check_cancel(cancel_checker)
            html = page.content()
            text = _extract_visible_text(page)
            risk = _detect_platform_risk(platform, page.url, text, html)
            if risk:
                _trip_risk_gate(platform, risk, cooldown_hours=24 if platform == "yuanbao" else 8)
                result.failure_reason = risk
            result.answer_text = _extract_answer_text(page, before_text, text, prompt)
            result.html_excerpt = re.sub(r"\s+", " ", html)[:1200]
            result.sources = _extract_page_sources(page, result.answer_text)
            try:
                result.screenshot_path = _save_artifact(platform, brand_name, keyword, "png", page.screenshot(full_page=True, timeout=8000), binary=True)
            except Exception:
                pass
            result.raw_artifact_path = _save_evidence_page(platform, brand_name, keyword, result, html)
            result.notes = f"{PLATFORMS[platform]['name']} 固定浏览器真实查询"
            if not result.failure_reason and str(run_mode or "manual").lower() == "auto":
                _mark_platform_run(platform)
            browser.close()
    except Exception as exc:
        try:
            if "page" in locals() and page:
                result.html_excerpt = re.sub(r"\s+", " ", page.content())[:1200]
                if not result.raw_artifact_path:
                    result.raw_artifact_path = _save_evidence_page(platform, brand_name, keyword, result, page.content())
                if not result.screenshot_path:
                    result.screenshot_path = _save_artifact(platform, brand_name, keyword, "png", page.screenshot(full_page=True, timeout=8000), binary=True)
        except Exception:
            pass
        if isinstance(exc, MonitorCancelled):
            result.failure_reason = "任务已取消"
            result.notes = "用户取消任务，平台监测已中断"
        else:
            result.failure_reason = f"{PLATFORMS.get(platform, {}).get('name', platform)} 浏览器监测异常: {exc}"
    return _finalize(result, start, brand_name)


def _clear_risk_gate(platform: str, note: str) -> None:
    """解除风控熔断（字段更新与「重置风控」接口一致，不动每日计数与连接状态）。"""
    with get_db() as conn:
        conn.execute(
            """UPDATE platform_sessions
               SET risk_status = 'normal', risk_reason = NULL, run_after = NULL,
                   captcha_required = 0, notes = ?, updated_at = ?
               WHERE platform = ?""",
            (note, local_now(), platform),
        )


def _preflight_risk_gate(platform: str, run_mode: str = "manual") -> Optional[str]:
    """平台风控前置门禁（auto 与 manual 都生效）。

    - selftest：人工主动触发的登录态自检，不受限额约束（仍会计数、仍抢跨进程锁）。
    - 风控熔断 / 封号 / 验证码状态：auto 模式在冷却期内直接拒绝；冷却期满自动
      解除熔断（标准熔断器 half-open：放行一次，真风控会被重新检出并再次熔断）。
      manual 模式是用户亲眼确认后的主动重测，视为人工确认，直接解除并放行
      ——否则一次误判就要等满冷却期 + 人工重置，重测按钮形同虚设。
    - 每日提问上限：到顶即拒绝（这是平台安全上限，与"谁点的"无关）。
    - 最小间隔：不足时**先等待**（最多 _RISK_MAX_WAIT 秒），让批量任务自然放慢到
      安全频率；超过等待上限才拒绝，避免任务在原地长时间空等。
    """
    name = PLATFORMS.get(platform, {}).get("name", platform)
    mode = str(run_mode or "manual").lower()
    if mode == "selftest":
        return None

    today = datetime.now().strftime("%Y-%m-%d")
    daily_limit = _daily_limit(platform)
    with get_db() as conn:
        row = conn.execute("SELECT * FROM platform_sessions WHERE platform = ?", (platform,)).fetchone()
        item = dict(row) if row else {}
    risk_status = item.get("risk_status") or "normal"
    if risk_status in {"risk_blocked", "account_restricted", "captcha_required"}:
        run_after = item.get("run_after") or ""
        if mode == "manual":
            _clear_risk_gate(platform, "用户手动重测，视为人工确认，风控熔断已解除")
        elif run_after and run_after > local_now():
            return f"{name} 风控熔断中：{item.get('risk_reason') or risk_status}，可恢复时间 {run_after}"
        else:
            _clear_risk_gate(platform, f"风控冷却期已满自动解除（原因：{item.get('risk_reason') or risk_status}），本次运行重新校验")
    if item.get("daily_run_date") == today and int(item.get("daily_run_count") or 0) >= daily_limit:
        return (
            f"{name} 今日安全提问上限已达 {daily_limit} 次"
            f"（可用 GEO_MONITOR_DAILY_{platform.upper()} 调整；确认账号无风险后也可点「重置风控」）"
        )
    last_run = item.get("last_run_at")
    if last_run:
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(str(last_run).replace(" ", "T"))).total_seconds()
            remaining = _min_interval(platform, mode) - elapsed
            if remaining > 0:
                if remaining > _RISK_MAX_WAIT:
                    return (
                        f"{name} 安全提问间隔未到，还需等待约 {int(remaining)} 秒"
                        f"（超过单次最大等待 {_RISK_MAX_WAIT} 秒，已跳过本次提问）"
                    )
                # 主动等待而不是报失败：批量任务由此自然放慢到安全频率
                time.sleep(remaining + random.uniform(0.5, 2.5))
        except Exception:
            pass
    return None


def _trip_risk_gate(platform: str, reason: str, cooldown_hours: int = 24) -> None:
    run_after = (datetime.now() + timedelta(hours=cooldown_hours)).strftime("%Y-%m-%d %H:%M:%S")
    status = "account_restricted" if any(word in reason for word in ["封", "限制", "违规", "异常", "账号"]) else "risk_blocked"
    if any(word in reason for word in ["验证码", "安全验证", "人机验证", "滑块"]):
        status = "captcha_required"
    with get_db() as conn:
        conn.execute(
            """UPDATE platform_sessions
               SET risk_status = ?, risk_reason = ?, run_after = ?, status = ?,
                   captcha_required = ?, notes = ?, updated_at = ?
               WHERE platform = ?""",
            (
                status,
                reason[:300],
                run_after,
                "captcha_required" if status == "captcha_required" else "expired",
                1 if status == "captcha_required" else 0,
                f"风控熔断：{reason[:180]}",
                local_now(),
                platform,
            ),
        )


def _mark_platform_run(platform: str, daily_increment: bool = True) -> None:
    """记录一次平台操作：更新 last_run_at（最小间隔的依据），并按需累加当日计数。

    参数 daily_increment=False 用于"试过但消息没发出去"的情况：这类操作不消耗
    平台提问额度，所以不计入每日上限；但它同样要参与最小间隔节流，否则一次失败
    会立刻诱发下次高频重试。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        row = conn.execute("SELECT daily_run_date, daily_run_count FROM platform_sessions WHERE platform = ?", (platform,)).fetchone()
        item = dict(row) if row else {}
        if not daily_increment:
            conn.execute(
                "UPDATE platform_sessions SET last_run_at = ?, updated_at = ? WHERE platform = ?",
                (local_now(), local_now(), platform),
            )
            return
        count = (int(item.get("daily_run_count") or 0) if item.get("daily_run_date") == today else 0) + 1
        conn.execute(
            """UPDATE platform_sessions
               SET last_run_at = ?, daily_run_date = ?, daily_run_count = ?,
                   risk_status = CASE WHEN risk_status IS NULL OR risk_status = '' THEN 'normal' ELSE risk_status END,
                   updated_at = ?
               WHERE platform = ?""",
            (local_now(), today, count, local_now(), platform),
        )


def _detect_platform_risk(platform: str, url: str, text: str, html: str = "", stage: str = "general") -> Optional[str]:
    combined = f"{url}\n{text}\n{html[:3000]}"
    # 强信号：这些词单独出现基本就是风控/限流拦截，整页匹配
    markers = [
        "验证码", "安全验证", "人机验证", "滑块", "访问过于频繁", "操作过于频繁",
        "账号异常", "账号受限", "账号已被限制", "请稍后再试",
        "当前环境存在异常", "登录状态已失效",
    ]
    for marker in markers:
        if marker in combined:
            return f"{PLATFORMS.get(platform, {}).get('name', platform)} 风控命中：{marker}"
    # 弱信号分两档：
    # - 带明确主语的"账号/环境…风险"（真风控横幅文案）全阶段生效，资讯标题
    #   几乎不会带这种主语搭配；
    # - "违规/封禁"族只在拿到回答后的整页（general）阶段使用。发送前的新会话页
    #   （pre_send）没有"判定+后果"语境，页面文本全是侧边栏历史标题和
    #   推荐流资讯（如"AI 撒谎有哪些安全风险"），任何弱模式都会误熔断
    #   （2026-09-29 豆包推荐标题命中"安全风险"误熔断 8 小时）。
    subject_risk_patterns = [
        r"账号.{0,8}风险",
        r"环境.{0,8}风险",
    ]
    if stage == "pre_send":
        risk_patterns = subject_risk_patterns
    else:
        # "违规"/"封禁"是弱信号：资讯标题（如"强制违规抢票"）、社区规范链接、
        # 规则提醒（"请勿发布违规内容"）里都会出现，整页匹配会误熔断
        # （2026-09-28 豆包推荐流资讯标题误判）。只有"判定+后果"的风控
        # 短语才算命中：违规行为/违规无法/账号违规/存在违规/涉嫌违规。
        # 裸的"安全风险/风险提示"不带主语，资讯标题里太常见，不再作为信号。
        risk_patterns = subject_risk_patterns + [
            r"违规.{0,4}(行为|无法|禁止|限制|封禁|不能)",
            r"(账号|内容|行为|评论|涉嫌|存在|因).{0,8}违规",
            r"(账号|被|已|永久|临时|恶意).{0,4}封禁",
        ]
    for pattern in risk_patterns:
        match = re.search(pattern, combined)
        if match:
            return f"{PLATFORMS.get(platform, {}).get('name', platform)} 风控命中：{match.group(0)}"
    return None


def _human_pause(platform: str, low: float, high: float) -> None:
    factor = 1.8 if platform == "yuanbao" else 1.0
    time.sleep(random.uniform(low * factor, high * factor))


def _ensure_browser_session(platform: str) -> dict:
    session = check_platform_session(platform)
    if session.get("status") != "not_connected":
        return session

    opened = open_login_browser(platform)
    if not opened.get("success"):
        return {**session, "notes": opened.get("notes") or session.get("notes")}

    deadline = time.time() + 8
    latest = session
    while time.time() < deadline:
        time.sleep(1)
        latest = check_platform_session(platform)
        if latest.get("status") != "not_connected":
            return latest
    return latest


def _fill_and_submit(page, prompt: str) -> bool:
    if "qianwen.com" in (page.url or "") and _fill_and_submit_qianwen(page, prompt):
        return True
    before_text = _extract_visible_text(page)
    selectors = [
        "textarea[placeholder*='发消息']",
        "textarea[placeholder*='消息']",
        "textarea.semi-input-textarea",
        "textarea",
        "[contenteditable='true']",
        "div[role='textbox']",
        "input[type='text']",
    ]
    for selector in selectors:
        try:
            candidates = page.locator(selector)
            count = candidates.count()
            if count <= 0:
                continue
            for index in range(count - 1, -1, -1):
                loc = candidates.nth(index)
                if not _is_editable_visible(loc):
                    continue
                loc.scroll_into_view_if_needed(timeout=3000)
                _human_mouse_move(page, loc)
                loc.click(timeout=3000)
                _human_type_locator(page, loc, prompt)
                if _submit_prompt(page, loc):
                    after_text = _extract_visible_text(page)
                    if len(after_text) > len(before_text) + 20 or prompt not in after_text:
                        return True
                try:
                    loc.click(timeout=2000)
                    if _send_prompt_by_click(page, prompt, before_text):
                        return True
                except Exception:
                    pass
                if _submit_prompt(page, loc):
                    return True
        except Exception:
            continue
    return False


def _fill_and_submit_qianwen(page, prompt: str) -> bool:
    try:
        if "qianwen.com" in (page.url or "") and "/chat" not in (page.url or ""):
            try:
                page.goto("https://www.qianwen.com/chat", wait_until="domcontentloaded", timeout=8000)
                page.wait_for_timeout(1200)
            except Exception:
                pass
        try:
            page.evaluate(
                """() => {
                    const visible = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 8 && r.height > 8 && s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    const label = el => `${el.innerText || el.textContent || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''} ${el.className || ''}`.trim();
                    const clickables = Array.from(document.querySelectorAll('button, [role="button"], a, div, span')).filter(visible);
                    const target = clickables.find(el => /新建对话|开始对话|新聊天|创建对话/i.test(label(el)));
                    if (!target) return false;
                    let node = target;
                    for (let i = 0; i < 6 && node; i += 1) {
                        const parent = node.parentElement;
                        if (!parent) break;
                        const parentLabel = label(parent);
                        const style = getComputedStyle(parent);
                        if (parent.matches('button, [role="button"], a') || parent.hasAttribute('onclick') || style.cursor === 'pointer' || /发送|提问|对话/.test(parentLabel)) {
                            node = parent;
                        } else {
                            break;
                        }
                    }
                    node.click();
                    return true;
                }"""
            )
            page.wait_for_timeout(900)
        except Exception:
            pass
        before_text = _cdp_text(page) if hasattr(page, "evaluate") else ""
        rect = page.evaluate(
            """() => {
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 10 && r.height > 10 && s.display !== 'none' && s.visibility !== 'hidden';
                };
                const main = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.left >= Math.max(220, window.innerWidth * 0.16) && r.top > window.innerHeight * 0.22 && r.bottom < window.innerHeight * 0.99;
                };
                const textOf = (el) => `${el.innerText || el.textContent || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''} ${el.className || ''}`.trim();
                const editors = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], div[role="textbox"], input[type="text"], input[type="search"], [data-placeholder], [placeholder], [class*="input"], [class*="editor"], [class*="textarea"]'))
                    .filter(visible).filter(main);
                const el = editors
                    .filter(node => node.getAttribute('contenteditable') === 'true' || node.getAttribute('role') === 'textbox' || /回车|提问|发送|请输入|有问题|向千问提问|写下你的问题|开始提问|输入问题/.test(textOf(node)))
                    .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0] || editors.at(-1);
                if (!el) return { x: window.innerWidth * 0.34, y: window.innerHeight * 0.86, fallback: true };
                const r = el.getBoundingClientRect();
                return { x: r.left + Math.min(28, r.width * 0.15), y: r.top + Math.min(24, r.height * 0.45), fallback: false };
            }"""
        )
        if not rect:
            return False
        page.mouse.move(rect["x"], rect["y"], steps=random.randint(8, 18))
        page.wait_for_timeout(random.randint(250, 700))
        page.mouse.click(rect["x"], rect["y"])
        page.wait_for_timeout(300)
        try:
            for _ in range(180):
                current = _qianwen_editor_text_playwright(page)
                if not current.strip() or "向千问提问" in current:
                    break
                page.keyboard.press("Backspace", timeout=1000)
        except Exception:
            pass
        page.keyboard.type(prompt, delay=random.randint(55, 115), timeout=30000)
        page.wait_for_timeout(800)
        editor_text = _qianwen_editor_text_playwright(page)
        if not _input_contains_prompt(editor_text, prompt):
            return False
        if _send_prompt_by_click(page, prompt, before_text):
            after_text = _cdp_text(page) if hasattr(page, "evaluate") else ""
            editor_text = _qianwen_editor_text_playwright(page)
            if not _input_contains_prompt(editor_text, prompt) and len(after_text) > len(before_text) + 20:
                return True
        after_text = _cdp_text(page) if hasattr(page, "evaluate") else ""
        editor_text = _qianwen_editor_text_playwright(page)
        if not _input_contains_prompt(editor_text, prompt) and len(after_text) > len(before_text) + 20:
            return True
        return False
    except Exception:
        return False


def _qianwen_editor_text_playwright(page) -> str:
    try:
        return str(page.evaluate(
            """() => {
                const visible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 10 && r.height > 10 && s.display !== 'none' && s.visibility !== 'hidden';
                };
                const main = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.left >= Math.max(220, window.innerWidth * 0.16) && r.top > window.innerHeight * 0.22 && r.bottom < window.innerHeight * 0.99;
                };
                const valueOf = (el) => {
                    if (!el) return '';
                    if ('value' in el) return el.value || '';
                    return el.innerText || el.textContent || '';
                };
                const active = document.activeElement;
                if (active && visible(active)) {
                    const activeText = valueOf(active).trim();
                    if (activeText) return activeText;
                }
                return Array.from(document.querySelectorAll('textarea, [contenteditable="true"], div[role="textbox"], input[type="text"], input[type="search"], [data-placeholder], [placeholder], [class*="input"], [class*="editor"], [class*="textarea"]'))
                    .filter(visible).filter(main).map(valueOf).filter(Boolean).join('\\n').trim();
            }"""
        ) or "")
    except Exception:
        return ""


def _is_editable_visible(locator) -> bool:
    try:
        if not locator.is_visible(timeout=800):
            return False
        if not locator.is_enabled(timeout=800):
            return False
        box = locator.bounding_box(timeout=800)
        return bool(box and box.get("width", 0) > 2 and box.get("height", 0) > 2)
    except Exception:
        return False


def _submit_prompt(page, input_locator) -> bool:
    _human_pause("generic", 0.8, 2.4)
    try:
        input_locator.press("Enter", timeout=3000)
        page.wait_for_timeout(800)
        if _input_has_been_submitted(input_locator):
            return True
    except Exception:
        pass

    return _click_send_button(page)


def _human_mouse_move(page, locator) -> None:
    try:
        box = locator.bounding_box(timeout=1000)
        if not box:
            return
        x = box["x"] + random.uniform(8, max(9, box["width"] - 8))
        y = box["y"] + random.uniform(6, max(7, box["height"] - 6))
        page.mouse.move(x, y, steps=random.randint(8, 18))
        page.wait_for_timeout(random.randint(250, 900))
    except Exception:
        pass


def _human_type_locator(page, locator, text: str) -> None:
    locator.click(timeout=3000)
    try:
        for _ in range(80):
            value = locator.evaluate("""el => ('value' in el ? el.value : (el.innerText || el.textContent || ''))""")
            if not str(value or "").strip():
                break
            locator.press("Backspace", timeout=1000)
    except Exception:
        pass
    chunks = _typing_chunks(text)
    for chunk in chunks:
        page.keyboard.type(chunk, delay=random.randint(45, 120), timeout=30000)
        page.wait_for_timeout(random.randint(180, 650))


def _typing_chunks(text: str) -> list[str]:
    value = text or ""
    chunks = []
    index = 0
    while index < len(value):
        size = random.randint(5, 16)
        chunks.append(value[index:index + size])
        index += size
    return chunks or [""]


def _input_has_been_submitted(input_locator) -> bool:
    try:
        value = input_locator.evaluate(
            """el => {
                if ('value' in el) return el.value || '';
                return el.innerText || el.textContent || '';
            }"""
        )
        return not str(value or "").strip()
    except Exception:
        return False


def _click_send_button(page) -> bool:
    try:
        clicked = page.evaluate(
            """() => {
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 8 && r.height > 8 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
                };
                const direct = Array.from(document.querySelectorAll('button[aria-label="发送消息"], button[title="发送消息"]')).filter(visible)[0];
                if (direct) {
                    const rect = direct.getBoundingClientRect();
                    return { ok: true, x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, direct: true };
                }
                const buttons = Array.from(document.querySelectorAll('button')).filter(visible);
                const scored = buttons.map((button) => {
                    const text = (button.innerText || button.textContent || '').trim();
                    const aria = button.getAttribute('aria-label') || '';
                    const title = button.getAttribute('title') || '';
                    const html = button.outerHTML || '';
                    const rect = button.getBoundingClientRect();
                    let score = 0;
                    if (/发送|提交|Send|提问|arrow|上箭头/i.test(text + aria + title + html)) score += 8;
                    if (rect.right > window.innerWidth * 0.70) score += 3;
                    if (rect.top > window.innerHeight * 0.32 && rect.bottom < window.innerHeight * 0.99) score += 3;
                    if (rect.width >= 18 && rect.width <= 90 && rect.height >= 18 && rect.height <= 90) score += 2;
                    if (button.disabled || button.getAttribute('aria-disabled') === 'true') score -= 10;
                    return { button, score, x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
                }).filter((item) => item.score > 0).sort((a, b) => b.score - a.score);
                if (scored.length) return { ok: true, x: scored[0].x, y: scored[0].y };
                return { ok: false };
            }"""
        )
        if clicked and clicked.get("ok"):
            try:
                page.mouse.move(clicked["x"], clicked["y"], steps=random.randint(6, 14))
                page.wait_for_timeout(random.randint(180, 500))
                page.mouse.click(clicked["x"], clicked["y"])
            except Exception:
                page.evaluate("""({ x, y }) => {
                    const el = document.elementFromPoint(x, y);
                    if (el) el.click();
                }""", {"x": clicked["x"], "y": clicked["y"]})
            page.wait_for_timeout(800)
            return True
    except Exception:
        pass
    return False


def _send_prompt_by_click(page, prompt: str, before_text: str = "") -> bool:
    if _click_send_button(page):
        page.wait_for_timeout(1600)
        after_text = _extract_visible_text(page)
        if len(after_text) > len(before_text) + 20 or prompt not in after_text:
            return True
    return False


def _wait_for_answer(page, before_text: str = "", prompt: str = "", timeout: int = 90, cancel_checker: Optional[Callable[[], bool]] = None) -> None:
    deadline = time.time() + timeout
    previous_len = 0
    stable_count = 0
    while time.time() < deadline:
        _check_cancel(cancel_checker)
        try:
            text = _extract_visible_text(page)
            current = _suffix_after_baseline(before_text, text, prompt) if before_text else text
            current_len = len(current)
            if current_len > previous_len:
                previous_len = current_len
                stable_count = 0
            else:
                stable_count += 1
            if previous_len > 40 and stable_count >= 3:
                return
        except Exception:
            pass
        time.sleep(1)


def _dedupe_repeated_lines(text: str) -> str:
    """去掉重复内容。

    页面 DOM 里同一条消息可能有多个副本，抽取时会拿到重复文本
    （实测同一段回答被重复了 3 次）。这里先去掉连续重复行，再检测
    "整段由同一块重复构成"的情况，只保留一块。
    """
    lines = [line.rstrip() for line in (text or "").splitlines()]
    compact: List[str] = []
    for line in lines:
        if compact and line.strip() and line == compact[-1]:
            continue
        compact.append(line)
    n = len(compact)
    if n >= 6:
        for size in range(3, n // 2 + 1):
            if n % size:
                continue
            block = compact[:size]
            if sum(len(x) for x in block) < 80:
                continue
            if all(compact[i:i + size] == block for i in range(0, n, size)):
                compact = block
                break
    return "\n".join(compact)


def _clean_answer_text(text: str, prompt: str) -> str:
    if not text:
        return ""
    cleaned = text.replace("\u200b", "").replace("\ufeff", "")
    cleaned = cleaned.replace(prompt, "")
    for line in prompt.splitlines():
        if line.strip():
            cleaned = cleaned.replace(line.strip(), "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = _dedupe_repeated_lines(cleaned)
    cleaned = cleaned.strip()
    return cleaned[-8000:] if len(cleaned) > 8000 else cleaned


def _suffix_after_baseline(before_text: str, after_text: str, prompt: str) -> str:
    if not after_text:
        return ""
    before_text = before_text or ""
    if before_text and after_text.startswith(before_text):
        return _clean_answer_text(after_text[len(before_text):], prompt)

    # Fall back to a line-level diff. This tolerates timestamps/counters moving
    # around while still removing pre-existing sidebar/history content.
    before_lines = {line.strip() for line in before_text.splitlines() if line.strip()}
    new_lines = []
    for line in after_text.splitlines():
        stripped = line.strip()
        if not stripped:
            if new_lines and new_lines[-1] != "":
                new_lines.append("")
            continue
        if stripped in before_lines:
            continue
        new_lines.append(line)
    candidate = _clean_answer_text("\n".join(new_lines), prompt)
    return candidate


def _extract_candidate_messages(page) -> List[str]:
    try:
        return page.evaluate(
            """() => {
                const selectors = [
                    '[data-testid*="message" i]',
                    '[data-testid*="answer" i]',
                    '[class*="message" i]',
                    '[class*="answer" i]',
                    '[class*="assistant" i]',
                    '[class*="markdown" i]',
                    '[class*="chat-item" i]',
                    '[role="article"]',
                    'article'
                ];
                const seen = new Set();
                const items = [];
                for (const selector of selectors) {
                    for (const node of document.querySelectorAll(selector)) {
                        if (seen.has(node)) continue;
                        seen.add(node);
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        if (rect.width < 20 || rect.height < 20 || style.visibility === 'hidden' || style.display === 'none') continue;
                        const text = (node.innerText || node.textContent || '').trim();
                        if (text.length >= 40) items.push(text);
                    }
                }
                return items.slice(-24);
            }"""
        ) or []
    except Exception:
        return []


def _extract_answer_text(page, before_text: str, after_text: str, prompt: str) -> str:
    suffix = _suffix_after_baseline(before_text, after_text, prompt)
    candidates = []
    for item in _extract_candidate_messages(page):
        cleaned = _clean_answer_text(item, prompt)
        if len(cleaned) < 60:
            continue
        if suffix and cleaned in suffix:
            candidates.append(cleaned)
        elif not suffix and len(cleaned) < max(len(after_text or ""), 1) * 0.8:
            candidates.append(cleaned)

    if candidates:
        # Prefer the latest reasonably sized message; a brand/source-bearing
        # candidate is more useful than a generic UI wrapper.
        candidates = candidates[-8:]
        candidates.sort(key=lambda s: (bool(_extract_sources_from_text(s)), len(s)), reverse=True)
        return candidates[0][-8000:]

    if suffix:
        return suffix[-8000:]
    return _clean_answer_text((after_text or "")[-8000:], prompt)


def _extract_visible_text(page) -> str:
    try:
        return page.evaluate(
            """() => {
                const nodes = Array.from(document.querySelectorAll('main, article, [class*=message], [class*=chat], body'));
                const text = nodes.map(n => n.innerText || '').filter(Boolean).join('\\n');
                return text.replace(/\\n{3,}/g, '\\n\\n').trim();
            }"""
        )
    except Exception:
        return ""


def _extract_page_sources(page, answer_text: str = "") -> List[SourceEvidence]:
    try:
        links = page.evaluate(
            """() => {
                const pick = [];
                const attrs = ['href', 'data-url', 'data-href', 'data-link', 'data-src', 'data-target-url'];
                const toAbs = (raw) => {
                    if (!raw) return '';
                    try {
                        const value = String(raw).trim();
                        if (/^https?:\\/\\//i.test(value)) return value;
                        if (value.startsWith('//')) return new URL(location.protocol + value).href;
                        if (value.startsWith('/')) return new URL(value, location.origin).href;
                    } catch (e) {}
                    return '';
                };
                const add = (el, url) => {
                    const abs = toAbs(url);
                    if (!abs || !/^https?:/.test(abs)) return;
                    pick.push({
                        title: (el.innerText || el.textContent || el.title || el.getAttribute('aria-label') || '').trim(),
                        url: abs
                    });
                };
                Array.from(document.querySelectorAll('a[href]')).forEach(a => add(a, a.href));
                Array.from(document.querySelectorAll('*')).forEach(el => {
                    attrs.forEach(name => {
                        const raw = el.getAttribute && el.getAttribute(name);
                        if (raw) add(el, raw);
                    });
                    const onclick = (el.getAttribute && el.getAttribute('onclick')) || '';
                    const match = onclick.match(/https?:\\/\\/[^\\s"'<>]+/);
                    if (match) add(el, match[0]);
                });
                return pick.slice(0, 40);
            }"""
        )
        anchor_sources = [
            SourceEvidence(title=item.get("title") or "", url=item.get("url") or "", domain=_domain(item.get("url") or ""))
            for item in links
        ]
        runtime_sources = _extract_sources_from_runtime_state(page)
        section_sources = _extract_sources_from_source_section(page)
        return _dedupe_sources(anchor_sources + runtime_sources + section_sources + _extract_sources_from_text(answer_text))
    except Exception:
        return _extract_sources_from_text(answer_text)


def _expand_reference_panel_playwright(page) -> None:
    patterns = re.compile(r"\d+\s*篇?来源|参考\s*\d+\s*篇?资料|参考资料|相关资料|引用来源|引用|来源")
    try:
        page.locator(
            "button, [role='button'], a, span, div"
        ).filter(
            has_text=patterns
        ).first.click(timeout=2000)
    except Exception:
        try:
            page.evaluate(
                """() => {
                    const visible = el => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 10 && r.height > 10 && s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    const label = el => `${el.innerText || el.textContent || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''}`.trim();
                    const target = Array.from(document.querySelectorAll('button, [role="button"], a, span, div'))
                        .filter(visible)
                        .find(el => /\\d+\\s*篇?来源|参考\\s*\\d+\\s*篇?资料|参考资料|相关资料|引用来源|引用|来源/.test(label(el)));
                    if (target) target.click();
                }"""
            )
        except Exception:
            pass
    time.sleep(1.2)
