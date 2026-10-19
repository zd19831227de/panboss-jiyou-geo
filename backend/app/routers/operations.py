"""P2 GEO operations: keyword ideation, content assets, publishing, scorecards."""
import json
import os
import subprocess
import html
import ipaddress
import re
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, HTTPException, Query, UploadFile, File
from pydantic import BaseModel, Field

from app.core.config import settings
from app.database import get_db, local_now
from app.models import MonitorRunRequest
from app.services.evidence import (
    verify_local_artifact,
    verify_publish_html,
    verify_publish_screenshot,
    verify_publish_summary_artifact,
    verify_source_url,
)
from app.services.geo_knowledge_base import (
    append_knowledge_record,
    knowledge_base_summary,
    list_knowledge_records,
)
from app.services.llm_service import chat_with_active_provider
from app.services.publish_platform_selection import recommend_publish_platforms
from app.services.publishing_rpa import (
    PublishRpaResult,
    _is_public_content_url,
    check_publish_browser,
    discover_publish_selectors,
    open_publish_browser,
    close_publish_browser,
    prepare_publish_browser,
    publish_port,
    publish_profile_dir,
    recover_ctrip_travel_urls,
    recover_baijiahao_urls,
    recover_zhihu_urls,
    recover_toutiao_urls,
    recover_sohu_urls,
    test_publish_workflow,
    verify_toutiao_published_page_with_browser,
    verify_published_page_url,
)

router = APIRouter(prefix="/api/operations", tags=["operations"])
publish_executor = ThreadPoolExecutor(max_workers=2)


CONTENT_TYPES = {
    "faq": "FAQ问答",
    "guide": "攻略",
    "ranking": "榜单",
    "review": "测评",
    "qa": "问答稿",
    "media": "媒体稿",
}

CATEGORY_LABELS = {
    "brand": "品牌词",
    "product": "产品词",
    "industry": "行业词",
    "longtail": "长尾词",
}

PUBLISH_PLATFORMS = {
    "website": {"name": "官网/落地页", "login_url": "", "publish_url": "", "profile": "website"},
    "ctrip_travel": {"name": "携程内容中心/旅拍", "login_url": "https://we.ctrip.com/account/login", "publish_url": "https://we.ctrip.com/account/login", "profile": "ctrip_travel"},
    "douyin": {"name": "抖音创作者中心", "login_url": "https://creator.douyin.com/", "publish_url": "https://creator.douyin.com/creator-micro/content/upload", "profile": "douyin"},
    "toutiao": {"name": "今日头条号", "login_url": "https://mp.toutiao.com/", "publish_url": "https://mp.toutiao.com/profile_v4/graphic/publish", "profile": "toutiao"},
    "baijiahao": {"name": "百家号", "login_url": "https://baijiahao.baidu.com/", "publish_url": "https://baijiahao.baidu.com/builder/rc/edit?type=news", "profile": "baijiahao"},
    "sohu": {"name": "搜狐号", "login_url": "https://mp.sohu.com/", "publish_url": "https://mp.sohu.com/mpfe/v4/main/news/add", "profile": "sohu"},
    "zhihu": {"name": "知乎", "login_url": "https://www.zhihu.com/", "publish_url": "https://zhuanlan.zhihu.com/write", "profile": "zhihu"},
    "xiaohongshu": {"name": "小红书", "login_url": "https://creator.xiaohongshu.com/", "publish_url": "https://creator.xiaohongshu.com/publish/publish", "profile": "xiaohongshu"},
}

PUBLISH_CAPABILITIES = {
    "website": {"level": "verified_loop", "rpa": "html_generate", "url_recover": True, "url_verify": True, "pc_supported": True, "commercial_ready": True},
    "ctrip_travel": {"level": "rpa_testing", "rpa": "dedicated", "url_recover": True, "url_verify": True, "pc_supported": True, "commercial_ready": False},
    "baijiahao": {"level": "rpa_draft", "rpa": "dedicated", "url_recover": True, "url_verify": True, "pc_supported": True, "commercial_ready": False},
    "zhihu": {"level": "rpa_draft", "rpa": "dedicated", "url_recover": True, "url_verify": True, "pc_supported": True, "commercial_ready": False},
    "douyin": {"level": "rpa_testing", "rpa": "image_note_draft", "url_recover": False, "url_verify": False, "pc_supported": True, "commercial_ready": False},
    "toutiao": {"level": "rpa_draft", "rpa": "dedicated", "url_recover": True, "url_verify": True, "pc_supported": True, "commercial_ready": False},
    "sohu": {"level": "rpa_draft", "rpa": "dedicated", "url_recover": True, "url_verify": True, "pc_supported": True, "commercial_ready": False},
    "xiaohongshu": {"level": "rpa_testing", "rpa": "image_note_draft", "url_recover": False, "url_verify": False, "pc_supported": True, "commercial_ready": False},
}

ECOSYSTEMS = {
    "byte": {
        "name": "字节生态",
        "ai_platforms": ["doubao"],
        "domains": ["douyin.com", "iesdouyin.com", "toutiao.com", "ixigua.com", "xigua.com", "wukong.com", "dongchedi.com"],
        "publish_platforms": ["douyin", "toutiao"],
        "basis": "豆包归属字节生态；系统以真实引用域名命中抖音、头条、西瓜等来源作为发布优先级依据。",
    },
    "alibaba": {
        "name": "阿里/千问生态",
        "ai_platforms": ["qianwen"],
        "domains": ["aliyun.com", "alibaba.com", "taobao.com", "tmall.com", "fliggy.com", "amap.com", "quark.cn", "uc.cn"],
        "publish_platforms": ["website", "zhihu", "baijiahao"],
        "basis": "千问官方联网搜索可返回来源和角标；P1 以可验证全网信源和官网结构化内容为主。",
    },
    "tencent": {
        "name": "腾讯/元宝生态",
        "ai_platforms": ["yuanbao"],
        "domains": ["qq.com", "gtimg.cn", "weixin.qq.com", "mp.weixin.qq.com", "sogou.com", "weixin.sogou.com", "v.qq.com"],
        "publish_platforms": ["website", "zhihu", "sohu"],
        "basis": "元宝公开资料显示接入微信搜一搜、搜狗搜索及腾讯生态信源；公众号不作为当前发布入口，企鹅号不作为优先平台。",
    },
    "travel_local": {
        "name": "酒旅/本地生活信源",
        "ai_platforms": ["doubao", "qianwen", "yuanbao"],
        "domains": ["ctrip.com", "trip.com", "dianping.com", "meituan.com", "amap.com", "booking.com", "agoda.com", "agoda.cn"],
        "publish_platforms": ["ctrip_travel", "website", "xiaohongshu", "zhihu"],
        "basis": "酒店/本地业务当前监测已多次出现携程等信源；携程使用内容中心/旅拍 PC 端，不走 EBK 或 C 端。",
    },
    "general_web": {
        "name": "全网内容信源",
        "ai_platforms": ["doubao", "qianwen", "yuanbao"],
        "domains": ["baidu.com", "sohu.com", "zhihu.com", "sina.cn", "smzdm.com"],
        "publish_platforms": ["baijiahao", "sohu", "zhihu", "xiaohongshu", "website"],
        "basis": "当 AI 引用来自通用网页和内容社区时，使用可公开访问、可验证 URL 的内容平台补信源。",
    },
}

ACTIVE_PUBLISH_STATUSES = {"pending", "assets_ready", "prepared", "running", "manual_required", "login_required"}
FORBIDDEN_CONTENT_TERMS = [
    "GEO内容稿",
    "用于提升",
    "生成式 AI 回答中的可见性",
    "品牌归因",
    "待补充",
    "模板生成",
    "我住过",
    "真实入住",
    "全程无雷",
    "闭眼入",
    "姐妹",
]
FORBIDDEN_STRATEGY_TERMS = [
    "http://",
    "https://",
]
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u27BF"
    "]",
    re.UNICODE,
)
BLOCKED_PUBLISH_DOMAINS = {"example.com", "example.org", "example.net", "localhost"}
STRATEGY_ACTION_TYPES = {
    "generate_keywords",
    "adopt_keywords",
    "run_monitor",
    "generate_content",
    "create_publish_task",
    "open_publish_browser",
    "prepare_rpa_draft",
    "run_recheck",
    "generate_report",
}
STRATEGY_COST_RISKS = {"none", "low", "medium", "high"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}
MAX_PUBLISH_IMAGES = 9
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PUBLISH_VIDEOS = 1
MAX_VIDEO_BYTES = 200 * 1024 * 1024


class KeywordGenerateRequest(BaseModel):
    brand_id: int
    seed: Optional[str] = ""
    count: int = 30


class AdoptIdeasRequest(BaseModel):
    idea_ids: list[int]


class ContentGenerateRequest(BaseModel):
    brand_id: int
    keyword_id: Optional[int] = None
    keyword: Optional[str] = ""
    content_type: str = "guide"
    target_platform: str = "website"


class ContentUpdateRequest(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    status: Optional[str] = None
    generation_notes: Optional[str] = None
    # 发布渠道允许在发布前修改（自动补内容默认落 website，用户可改到已登录
    # 的外部平台如知乎）；发布中/已发布后渠道与既有任务/已发 URL 绑定，不可再改
    target_platform: Optional[str] = None


class ContentRewriteRequest(BaseModel):
    mode: str = "rewrite"
    direction: Optional[str] = ""


class AccountCreateRequest(BaseModel):
    platform: str
    account_name: str
    login_url: Optional[str] = None
    publish_url: Optional[str] = None
    notes: Optional[str] = None


class WorkflowUpdateRequest(BaseModel):
    platform_name: Optional[str] = None
    login_url: Optional[str] = None
    publish_url: Optional[str] = None
    workflow_json: Optional[dict] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class PlatformRecommendRequest(BaseModel):
    brand_id: Optional[int] = None
    name: Optional[str] = ""
    industry: Optional[str] = ""
    products: Optional[str] = ""
    target_audience: Optional[str] = ""
    objective: Optional[str] = ""


class KnowledgeRecordRequest(BaseModel):
    brand_id: Optional[int] = None
    title: str
    category: Optional[str] = None
    content: Optional[str] = None
    source_url: Optional[str] = None
    evidence_type: Optional[str] = None
    question: Optional[str] = None
    intent_type: Optional[str] = None
    platform: Optional[str] = None
    diagnosis_type: Optional[str] = None
    status: Optional[str] = "active"
    metadata: dict = Field(default_factory=dict)


class SelectorDiscoverRequest(BaseModel):
    save: bool = True


class PublishTaskCreateRequest(BaseModel):
    content_id: int
    platform: str
    account_id: Optional[int] = None
    publish_mode: str = "manual_confirm"
    scheduled_at: Optional[str] = None


class PublishConfirmRequest(BaseModel):
    published_url: str
    notes: Optional[str] = None
    trigger_monitor: bool = True


class PublishAutoVerifyRequest(BaseModel):
    trigger_monitor: bool = True


class PublishRecoverUrlRequest(BaseModel):
    published_url: Optional[str] = None
    trigger_monitor: bool = True


class PublishFailRequest(BaseModel):
    failure_reason: str
    notes: Optional[str] = None


class StrategyGenerateRequest(BaseModel):
    brand_id: int
    objective: Optional[str] = ""


class StrategyConfirmActionsRequest(BaseModel):
    action_ids: list[int]


class StrategyExecuteActionsRequest(BaseModel):
    action_ids: Optional[list[int]] = None
    dry_run: bool = False


class AgentActionRequest(BaseModel):
    agent_name: str = "hermes"
    brand_id: int
    action: str
    payload: dict = {}
    dry_run: bool = False


class AgentStrategyRequest(BaseModel):
    agent_name: str = "hermes"
    strategy_id: int
    action_ids: Optional[list[int]] = None
    dry_run: bool = False


def _brand(cursor, brand_id: int) -> dict:
    cursor.execute("SELECT * FROM brands WHERE id = ?", (brand_id,))
    row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Brand not found")
    return dict(row)


def _keyword_context(cursor, brand_id: int, keyword_id: Optional[int], keyword: Optional[str]) -> tuple[Optional[int], str, str]:
    if keyword_id:
        cursor.execute("SELECT * FROM keywords WHERE id = ? AND brand_id = ?", (keyword_id, brand_id))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Keyword not found")
        row = dict(row)
        return row["id"], row["word"], row.get("category") or "longtail"
    word = (keyword or "").strip()
    if not word:
        raise HTTPException(status_code=400, detail="keyword_id or keyword is required")
    return None, word, "longtail"


def _existing_keyword_words(cursor, brand_id: int) -> set[str]:
    cursor.execute("SELECT word FROM keywords WHERE brand_id = ?", (brand_id,))
    return {row["word"] for row in cursor.fetchall()}


def _create_post_publish_monitor(cursor, task: dict) -> dict:
    keyword_id = task.get("keyword_id")
    keyword_word = (task.get("target_keyword") or task.get("title") or "").strip()
    if not keyword_word:
        raise HTTPException(status_code=400, detail="No keyword available for retest")
    if not keyword_id:
        cursor.execute(
            "SELECT id FROM keywords WHERE brand_id = ? AND word = ?",
            (task["brand_id"], keyword_word),
        )
        row = cursor.fetchone()
        if row:
            keyword_id = row["id"]
        else:
            cursor.execute(
                """INSERT INTO keywords
                   (brand_id, word, category, search_volume, ai_status, notes)
                   VALUES (?, ?, 'longtail', 0, 'pending', ?)""",
                (task["brand_id"], keyword_word, "发布后复测自动补充关键词"),
            )
            keyword_id = cursor.lastrowid
    platforms = ["web", "doubao", "qianwen", "yuanbao"]
    cursor.execute(
        """INSERT INTO monitor_runs
           (brand_id, platforms, status, total_keywords, total_operations)
           VALUES (?, ?, 'pending', 1, ?)""",
        (task["brand_id"], ",".join(platforms), len(platforms)),
    )
    run_id = cursor.lastrowid
    params = {
        "brand_name": task.get("brand_name") or "",
        "brand_id": task["brand_id"],
        "keywords": [{"id": keyword_id, "word": keyword_word, "brand_id": task["brand_id"]}],
        "platforms": platforms,
        "run_id": run_id,
        "published_url": task.get("published_url"),
        "source_task_id": task.get("id"),
    }
    cursor.execute(
        """INSERT INTO task_queue
           (task_type, status, params, priority, created_at)
           VALUES ('monitor', 'pending', ?, 1, ?)""",
        (json.dumps(params, ensure_ascii=False), local_now()),
    )
    task_id = cursor.lastrowid
    cursor.execute("UPDATE monitor_runs SET task_id = ? WHERE id = ?", (task_id, run_id))
    return {"task_id": task_id, "run_id": run_id, "keyword_id": keyword_id, "platforms": platforms}


def _agent_run_log_start(agent_name: str, action: str, brand_id: Optional[int], dry_run: bool, request_json: dict) -> int:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO agent_api_runs
               (agent_name, action, brand_id, dry_run, request_json, status)
               VALUES (?, ?, ?, ?, ?, 'pending')""",
            (agent_name, action, brand_id, 1 if dry_run else 0, json.dumps(request_json, ensure_ascii=False, default=str)),
        )
        return cursor.lastrowid


def _agent_run_log_finish(run_id: int, status: str, result_json: dict, failure_reason: str = "") -> None:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE agent_api_runs
               SET status = ?, result_json = ?, failure_reason = ?, completed_at = ?
               WHERE id = ?""",
            (
                status,
                json.dumps(result_json, ensure_ascii=False, default=str),
                failure_reason,
                local_now(),
                run_id,
            ),
        )


def _split_assets(value: Optional[str]) -> list[str]:
    if not value:
        return []
    normalized = value.replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",").replace("\n", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _require_brand_assets(brand: dict) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    missing = []
    products = _split_assets(brand.get("products"))
    audiences = _split_assets(brand.get("target_audience"))
    selling_points = _split_assets(brand.get("selling_points"))
    competitors = _split_assets(brand.get("competitors"))
    owned_sites = _split_assets(brand.get("owned_sites"))
    required = [
        ("核心产品/服务", products),
        ("目标人群", audiences),
        ("核心卖点", selling_points),
        ("竞品参照", competitors),
        ("官网/第三方信源", owned_sites),
    ]
    for label, values in required:
        if not values:
            missing.append(label)
    if missing:
        raise HTTPException(status_code=400, detail=f"品牌资料不完整，需先补充：{'、'.join(missing)}")
    return products, audiences, selling_points, competitors, owned_sites


def _content_quality_issues(content: dict) -> list[str]:
    title = (content.get("title") or "").strip()
    body = (content.get("body") or "").strip()
    issues = []
    if len(title) < 6:
        issues.append("标题过短")
    if len(body) < 260:
        issues.append("正文过短")
    if not EMOJI_RE.search(title):
        issues.append("标题必须包含自然 emoji，提升笔记可读性")
    if len(EMOJI_RE.findall(body)) < 3:
        issues.append("正文至少包含 3 个自然 emoji，提升笔记可读性")
    for term in FORBIDDEN_CONTENT_TERMS:
        if term in title or term in body:
            issues.append(f"包含内部模板词：{term}")
    body_urls = re.findall(r"https?://[^\s，。；、)）]+", body)
    if body_urls:
        issues.append("正文禁止出现URL链接，任何平台正文都不允许带 http/https")
    notes = content.get("generation_notes") or ""
    note_urls = re.findall(r"https?://[^\s，。；、)）\"']+", notes)
    if note_urls and not any(verify_source_url(url, live_check=False)[0] for url in note_urls):
        issues.append("来源字段链接格式无效")
    media = _content_media_assets(content)
    if len([item for item in media if item.get("kind") == "image"]) > MAX_PUBLISH_IMAGES:
        issues.append(f"图片超过上限：最多{MAX_PUBLISH_IMAGES}张")
    if len([item for item in media if item.get("kind") == "video"]) > MAX_PUBLISH_VIDEOS:
        issues.append(f"视频超过上限：最多{MAX_PUBLISH_VIDEOS}个")
    for item in media:
        ok, reason = verify_local_artifact(item.get("path"), "发布素材")
        if not ok:
            issues.append(f"素材无效：{reason}")
    return issues


def _platform_content_rules(content: dict) -> list[str]:
    platform = content.get("target_platform") or "website"
    title = (content.get("title") or "").strip()
    body = (content.get("body") or "").strip()
    media = _content_media_assets(content)
    images = [item for item in media if item.get("kind") == "image"]
    videos = [item for item in media if item.get("kind") == "video"]
    issues: list[str] = []
    if platform in {"ctrip_travel", "xiaohongshu", "douyin"} and not images and not videos:
        issues.append("图文平台必须上传至少 1 个图片或视频素材")
    if platform == "ctrip_travel":
        if len(title) > 40:
            issues.append("携程旅拍标题建议控制在 40 字内")
        if not images:
            issues.append("携程旅拍需要图片素材作为封面/正文图")
        for term in ["最低价", "全网最低", "官方保证", "五星好评"]:
            if term in title or term in body:
                issues.append(f"携程旅拍疑似夸大/绝对化表达：{term}")
    elif platform == "xiaohongshu":
        if len(title) > 28:
            issues.append("小红书标题建议控制在 28 字内")
        if len(images) < 1:
            issues.append("小红书笔记至少需要 1 张图片")
    elif platform == "douyin":
        if len(title) > 30:
            issues.append("抖音图文标题建议控制在 30 字内")
        if len(images) < 1 and len(videos) < 1:
            issues.append("抖音图文/视频至少需要 1 个视觉素材")
    elif platform in {"baijiahao", "toutiao", "sohu"}:
        title_limit = 30 if platform == "toutiao" else 32
        if len(title) > title_limit:
            issues.append(f"{'今日头条' if platform == 'toutiao' else '资讯平台'}标题需控制在 {title_limit} 字内")
        if body.count("\n") < 2:
            issues.append("资讯平台正文需要清晰分段")
    elif platform == "zhihu":
        if len(title) > 28:
            issues.append("知乎标题需控制在 28 字内，否则发布按钮可能不可用")
        if len(images) < 1:
            issues.append("知乎文章需要上传封面图片")
        if len(body) < 500:
            issues.append("知乎回答建议正文不少于 500 字")
        if any(term in body for term in ["强烈推荐", "必住", "必买"]):
            issues.append("知乎内容不宜使用强推硬广词")
    return issues


def _brand_fact_rules(content: dict, brand: dict) -> list[str]:
    title = (content.get("title") or "").strip()
    body = (content.get("body") or "").strip()
    text = f"{title}\n{body}"
    issues: list[str] = []
    brand_name = (brand.get("name") or "").strip()
    products = _split_assets(brand.get("products"))
    selling_points = _split_assets(brand.get("selling_points"))
    owned_sites = _split_assets(brand.get("owned_sites"))
    if brand_name and brand_name not in text:
        issues.append("正文未明确出现目标品牌名")
    if products and not _asset_phrase_hit(text, products):
        issues.append("正文未命中品牌资料中的产品/服务")
    if selling_points and not _asset_phrase_hit(text, selling_points):
        issues.append("正文未命中品牌资料中的核心卖点")
    if not any(verify_source_url(url, live_check=False)[0] for url in owned_sites):
        issues.append("品牌资料缺少可验证官网/第三方信源，事实核验只能人工复核")
    for pattern in [r"\d+(\.\d+)?\s*(米|公里|分钟)", r"\d+(\.\d+)?\s*分", r"\d+(\.\d+)?\s*星"]:
        if re.search(pattern, text) and "evidence_sources=" not in (content.get("generation_notes") or ""):
            issues.append("正文包含距离/评分/星级等量化事实，但缺少来源证据")
            break
    return issues


def _ai_review_content(content: dict, brand: dict) -> dict:
    prompt = f"""
你是 GEO 商业内容终审。请只审查，不重写。根据品牌资料和内容判断是否适合发布、是否有 GEO 价值、是否有违规/事实风险。

品牌资料：
{_brand_llm_context(brand)}

内容：
标题：{content.get("title") or ""}
平台：{content.get("target_platform") or ""}
正文：
{content.get("body") or ""}

返回严格 JSON：
{{"passed":true,"score":85,"risks":["..."],"suggestions":["..."]}}
"""
    try:
        result = chat_with_active_provider(
            [
                {"role": "system", "content": "你只返回严格 JSON，不要 Markdown。"},
                {"role": "user", "content": prompt},
            ],
            operation="content_quality_review",
            temperature=0.2,
            timeout=60,
        )
        text, usage = _llm_content(result)
        payload = _json_from_llm_text(text)
        risks = payload.get("risks") if isinstance(payload.get("risks"), list) else []
        suggestions = payload.get("suggestions") if isinstance(payload.get("suggestions"), list) else []
        return {
            "passed": bool(payload.get("passed")) and not risks,
            "score": int(payload.get("score") or 0),
            "risks": [str(item) for item in risks][:8],
            "suggestions": [str(item) for item in suggestions][:8],
            "model": usage.get("model"),
            "usage_id": usage.get("id"),
            "manual_review_required": False,
        }
    except Exception as exc:
        return {
            "passed": False,
            "score": 0,
            "risks": [f"AI复审未完成：{str(exc)[:180]}"],
            "suggestions": ["请人工复审内容事实、平台规则和发布风险后再审核通过"],
            "model": None,
            "usage_id": None,
            "manual_review_required": True,
        }


def _sanitize_strict_text(value: str, replacements: dict[str, str]) -> str:
    text = value or ""
    for src, dst in replacements.items():
        if src and src in text:
            text = text.replace(src, dst)
    return text


def _sanitize_publish_text(title: str, body: str, brand: dict, platform: str) -> tuple[str, str]:
    competitors = _split_assets(brand.get("competitors"))
    replacements = {competitor: "同商圈同类型酒店" for competitor in competitors if competitor}
    title = _sanitize_strict_text(title, replacements)
    body = _sanitize_strict_text(body, replacements)
    title = re.sub(r"https?://\S+", "", title)
    body = re.sub(r"https?://\S+", "", body)
    title = re.sub(r"\bwww\.[^\s，。；、)）]+", "", title, flags=re.I)
    body = re.sub(r"\bwww\.[^\s，。；、)）]+", "", body, flags=re.I)
    domain_re = r"\b[a-zA-Z0-9.-]+\.(com|cn|net|org|io|co|me|tv|app|site|top|info)\b[^\s，。；、)）]*"
    title = re.sub(domain_re, "", title, flags=re.I)
    body = re.sub(domain_re, "", body, flags=re.I)
    for term in FORBIDDEN_CONTENT_TERMS:
        title = title.replace(term, "")
        body = body.replace(term, "")
    title = re.sub(r"\s+", " ", title).strip()
    body = re.sub(r"[ \t]+", " ", body).strip()
    return _ensure_content_emoji(title, body, platform)


def _asset_phrase_hit(text: str, values: list[str]) -> bool:
    normalized = re.sub(r"\s+", "", text or "")
    for value in values:
        phrase = re.sub(r"\s+", "", value or "")
        if not phrase:
            continue
        if phrase in normalized:
            return True
        tokens = [item for item in re.split(r"[，,、/；;\s]+", value) if len(item.strip()) >= 2]
        if tokens and any(token.strip() in normalized for token in tokens):
            return True
        core_terms = re.findall(r"[\u4e00-\u9fa5]{2,}", phrase)
        for term in core_terms:
            for size in range(min(4, len(term)), 1, -1):
                if any(term[index:index + size] in normalized for index in range(0, len(term) - size + 1)):
                    return True
    return False


def _sanitize_strategy_payload(strategy: dict, actions: list[dict], brand: dict) -> tuple[dict, list[dict], list[str]]:
    competitors = _split_assets(brand.get("competitors"))
    replacements = {competitor: "同商圈同价位酒店" for competitor in competitors if competitor}
    issues: list[str] = []

    def scrub_text(text: str) -> str:
        cleaned = _sanitize_strict_text(text, replacements)
        for term in FORBIDDEN_STRATEGY_TERMS:
            if term in cleaned:
                issues.append(f"策略中包含链接字样：{term}")
        return cleaned

    sanitized_strategy = json.loads(json.dumps(strategy, ensure_ascii=False))
    for key in ["positioning", "retest_cycle"]:
        if key in sanitized_strategy and isinstance(sanitized_strategy[key], str):
            sanitized_strategy[key] = scrub_text(sanitized_strategy[key])
    for key in ["reasoning_summary", "source_gap_diagnosis", "search_intent", "monitoring_plan", "content_plan", "publishing_plan", "success_criteria", "gap_analysis", "knowledge_references", "evidence_gaps"]:
        value = sanitized_strategy.get(key)
        if isinstance(value, list):
            sanitized_strategy[key] = [scrub_text(str(item)) for item in value]
    keyword_matrix = sanitized_strategy.get("keyword_matrix")
    if isinstance(keyword_matrix, list):
        for item in keyword_matrix:
            if isinstance(item, dict):
                for key, value in list(item.items()):
                    if isinstance(value, str):
                        item[key] = scrub_text(value)
                    elif isinstance(value, list):
                        item[key] = [scrub_text(str(v)) for v in value]

    sanitized_actions: list[dict] = []
    for action in actions:
        item = dict(action)
        for key in ["title", "description"]:
            if isinstance(item.get(key), str):
                item[key] = scrub_text(item[key])
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        payload_json = json.loads(json.dumps(payload, ensure_ascii=False))
        for key, value in list(payload_json.items()):
            if key in {"publish_url", "login_url", "published_url", "url"}:
                continue
            if isinstance(value, str):
                payload_json[key] = scrub_text(value)
            elif isinstance(value, list):
                payload_json[key] = [scrub_text(str(v)) for v in value]
        item["payload"] = payload_json
        sanitized_actions.append(item)

    return sanitized_strategy, sanitized_actions, issues


def _content_media_assets(content: dict) -> list[dict]:
    value = content.get("media_assets_json") or "[]"
    try:
        items = json.loads(value)
    except Exception:
        return []
    return items if isinstance(items, list) else []


def _content_has_image(content: dict) -> bool:
    return any(item.get("kind") == "image" for item in _content_media_assets(content))


# 这些平台的编辑器发布时强制要求封面图（知乎专栏题图/百家号封面），
# 建任务时就必须有配图，否则 RPA 跑到编辑器里才卡死
COVER_REQUIRED_PLATFORMS = {"zhihu", "baijiahao"}


def _media_dir(brand_id: int, content_id: int) -> Path:
    path = Path(settings.GEO_ARTIFACT_DIR) / "media" / str(brand_id) / str(content_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _delete_content_media_files(content: dict) -> int:
    removed = 0
    artifact_root = Path(settings.GEO_ARTIFACT_DIR).resolve()
    for item in _content_media_assets(content):
        path = Path(str(item.get("path") or ""))
        try:
            if path.exists() and str(path.resolve()).startswith(str(artifact_root)):
                path.unlink()
                removed += 1
        except Exception:
            pass
    return removed


def _require_content_quality(content: dict) -> None:
    issues = _content_quality_issues(content)
    if issues:
        raise HTTPException(status_code=400, detail=f"内容未通过发布门禁：{'；'.join(issues)}")


def _ensure_content_emoji(title: str, body: str, platform: str) -> tuple[str, str]:
    title = title.strip()
    body = body.strip()
    if not EMOJI_RE.search(title):
        title_prefix = "🏨" if platform in {"ctrip_travel", "xiaohongshu", "douyin"} else "✨"
        title = f"{title_prefix} {title}"
    body_emojis = EMOJI_RE.findall(body)
    if len(body_emojis) >= 3:
        return title, body
    accents = ["📍", "🚇", "🧳", "✨", "✅", "💡"]
    lines = body.splitlines() if body else []
    patched = []
    added = 0
    for line in lines:
        stripped = line.strip()
        if stripped and not EMOJI_RE.search(stripped) and added < 3:
            patched.append(f"{accents[added]} {stripped}")
            added += 1
        else:
            patched.append(line)
    while added < 3:
        fallback = [
            "📍 适合关注位置、交通和商圈便利度的用户重点参考。",
            "✨ 内容建议结合真实图片素材发布，提升平台笔记的可读性。",
            "💡 发布前请人工核对设施、交通和服务信息，避免夸大表达。",
        ][added]
        patched.append(fallback)
        added += 1
    return title, "\n".join(patched).strip()


def _valid_optional_url(url: Optional[str], field_name: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail=f"{field_name} 必须是完整 http/https URL")
    return value


def _domain_matches(domain: str, candidates: list[str]) -> bool:
    value = (domain or "").lower()
    return any(candidate in value for candidate in candidates)


def _ecosystem_source_analysis(cursor, brand_id: Optional[int] = None) -> list[dict]:
    params = []
    result_brand_filter = ""
    answer_brand_filter = ""
    if brand_id:
        result_brand_filter = "AND brand_id = ?"
        answer_brand_filter = "AND ma.brand_id = ?"
        params.append(brand_id)
    cursor.execute(
        f"""SELECT platform, domain, SUM(count) AS count
            FROM (
                SELECT platform, source_domain AS domain, COUNT(*) AS count
                FROM monitor_results
                WHERE platform IN ('doubao','qianwen','yuanbao')
                  AND source_domain IS NOT NULL AND source_domain != ''
                  {result_brand_filter}
                GROUP BY platform, source_domain
                UNION ALL
                SELECT ma.platform, ms.domain AS domain, COUNT(*) AS count
                FROM monitor_sources ms
                JOIN monitor_answers ma ON ma.id = ms.answer_id
                WHERE ma.platform IN ('doubao','qianwen','yuanbao')
                  AND ms.domain IS NOT NULL AND ms.domain != ''
                  {answer_brand_filter}
                GROUP BY ma.platform, ms.domain
            )
            GROUP BY platform, domain
            ORDER BY count DESC""",
        tuple(params * 2 if brand_id else []),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    analysis = []
    for ecosystem_id, ecosystem in ECOSYSTEMS.items():
        source_hits = [
            row for row in rows
            if row["platform"] in ecosystem["ai_platforms"]
            and _domain_matches(row["domain"], ecosystem["domains"])
        ]
        source_hits = sorted(source_hits, key=lambda item: item["count"], reverse=True)[:12]
        evidence_count = sum(item["count"] for item in source_hits)
        recommendations = []
        for platform in ecosystem["publish_platforms"]:
            workflow = _workflow(cursor, platform)
            recommendations.append({
                "platform": platform,
                "platform_name": workflow.get("platform_name") or PUBLISH_PLATFORMS.get(platform, {}).get("name") or platform,
                "status": workflow.get("status") or "template",
                "login_url": workflow.get("login_url") or "",
                "publish_url": workflow.get("publish_url") or "",
            })
        analysis.append({
            "ecosystem": ecosystem_id,
            "ecosystem_name": ecosystem["name"],
            "ai_platforms": ecosystem["ai_platforms"],
            "basis": ecosystem["basis"],
            "evidence_count": evidence_count,
            "source_hits": source_hits,
            "recommendations": recommendations,
        })
    return sorted(analysis, key=lambda item: item["evidence_count"], reverse=True)


def _workflow(cursor, platform: str) -> dict:
    cursor.execute("SELECT * FROM publishing_workflows WHERE platform = ?", (platform,))
    row = cursor.fetchone()
    if row:
        item = dict(row)
        try:
            item["workflow"] = json.loads(item.get("workflow_json") or "{}")
        except Exception:
            item["workflow"] = {}
        return item
    meta = PUBLISH_PLATFORMS.get(platform)
    if not meta:
        raise HTTPException(status_code=400, detail="Invalid platform")
    workflow_json = {
        "steps": ["open_publish_url", "fill_title", "fill_body", "manual_publish", "verify_url"],
        "selectors": {},
        "requires_manual_publish": True,
    }
    cursor.execute(
        """INSERT INTO publishing_workflows
           (platform, platform_name, login_url, publish_url, workflow_json, status, notes, updated_at)
           VALUES (?, ?, ?, ?, ?, 'template', ?, ?)""",
        (
            platform,
            meta["name"],
            meta.get("login_url") or "",
            meta.get("publish_url") or "",
            json.dumps(workflow_json, ensure_ascii=False),
            "默认发布入口；需按账号实际页面测试后固化选择器",
            local_now(),
        ),
    )
    cursor.execute("SELECT * FROM publishing_workflows WHERE platform = ?", (platform,))
    item = dict(cursor.fetchone())
    item["workflow"] = workflow_json
    return item


def _workflow_payload(workflow: dict) -> dict:
    try:
        payload = json.loads(workflow.get("workflow_json") or "{}")
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _validate_workflow_json(platform: str, value: Optional[dict]) -> Optional[dict]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="workflow_json 必须是对象")
    steps = value.get("steps")
    if not isinstance(steps, list) or not steps or not all(isinstance(step, str) and step.strip() for step in steps):
        raise HTTPException(status_code=400, detail="workflow_json.steps 必须是非空字符串数组")
    if platform == "website":
        if "generate_html" not in steps:
            raise HTTPException(status_code=400, detail="官网工作流必须包含 generate_html")
        if "manual_deploy" not in steps:
            raise HTTPException(status_code=400, detail="官网工作流必须保留 manual_deploy 人工上线确认步骤")
    else:
        if "open_publish_url" not in steps:
            raise HTTPException(status_code=400, detail="外部平台工作流必须包含 open_publish_url")
        if platform != "ctrip_travel" and "manual_publish" not in steps:
            raise HTTPException(status_code=400, detail="外部平台工作流必须保留 manual_publish 人工最终确认步骤")
    if value.get("requires_manual_publish") is not True:
        raise HTTPException(status_code=400, detail="requires_manual_publish 必须为 true，禁止伪装全自动发布")
    selectors = value.get("selectors", {})
    if selectors is not None and not isinstance(selectors, dict):
        raise HTTPException(status_code=400, detail="workflow_json.selectors 必须是对象")
    return value


def _is_private_host(hostname: str) -> bool:
    host = (hostname or "").strip().lower()
    if not host:
        return True
    if host in BLOCKED_PUBLISH_DOMAINS or host.endswith(".localhost") or host.endswith(".test") or host.endswith(".invalid"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local or ip.is_multicast
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local or ip.is_multicast:
                return True
        except Exception:
            continue
    return False


def _validate_published_url(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="发布链接必须是完整 http/https URL")
    if _is_private_host(parsed.hostname or ""):
        raise HTTPException(status_code=400, detail="发布链接不能是示例、本机、内网或保留域名")
    try:
        response = requests.head(url, allow_redirects=True, timeout=8, headers={"User-Agent": "GeoOps/2.0"})
        if response.status_code in (405, 403) or response.status_code >= 500:
            response = requests.get(url, allow_redirects=True, timeout=10, headers={"User-Agent": "GeoOps/2.0"}, stream=True)
        status_code = response.status_code
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"发布链接无法访问：{str(exc)[:120]}")
    if status_code >= 500:
        raise HTTPException(status_code=400, detail=f"发布链接服务异常 HTTP {status_code}")
    if status_code == 404:
        raise HTTPException(status_code=400, detail="发布链接不存在 HTTP 404")
    return url, status_code


def _validate_publish_evidence(
    *,
    task_id: Optional[int] = None,
    published_url: Optional[str] = None,
    screenshot_path: Optional[str] = None,
    raw_artifact_path: Optional[str] = None,
    summary_artifact_path: Optional[str] = None,
    live_url_check: bool = False,
) -> dict:
    url_ok, url_reason = verify_source_url(published_url, live_check=live_url_check) if published_url else (False, "无发布链接")
    screenshot_ok, screenshot_reason = verify_publish_screenshot(screenshot_path) if screenshot_path else (False, "无发布截图")
    raw_ok, raw_reason = verify_publish_html(raw_artifact_path) if raw_artifact_path else (False, "无发布HTML")
    summary_ok, summary_reason = verify_publish_summary_artifact(
        summary_artifact_path,
        task_id=task_id,
        raw_artifact_path=raw_artifact_path,
        screenshot_path=screenshot_path,
    ) if summary_artifact_path else (False, "无发布摘要")
    reasons = []
    if published_url and not url_ok:
        reasons.append(url_reason)
    if not screenshot_ok:
        reasons.append(screenshot_reason)
    if not raw_ok:
        reasons.append(raw_reason)
    if not summary_ok:
        reasons.append(summary_reason)
    material_ok = screenshot_ok and raw_ok and summary_ok
    if published_url and not material_ok:
        reasons.append("发布链接缺少HTML/截图/摘要三件套")
    return {
        "published_url_verified": 1 if url_ok else 0,
        "screenshot_verified": 1 if screenshot_ok else 0,
        "raw_artifact_verified": 1 if raw_ok else 0,
        "summary_artifact_verified": 1 if summary_ok else 0,
        "evidence_status": "verified" if url_ok and material_ok else ("material_verified" if material_ok else "unverified"),
        "evidence_failure_reason": "；".join(part for part in reasons if part)[:500],
    }


def _publish_material_status(task: dict) -> str:
    if task.get("screenshot_verified") and task.get("raw_artifact_verified") and task.get("summary_artifact_verified"):
        return "verified"
    if task.get("screenshot_path") or task.get("raw_artifact_path") or task.get("summary_artifact_path"):
        return "invalid"
    return "missing"


def _workflow_has_verified_test(cursor, platform: str) -> bool:
    if platform == "website":
        return True
    cursor.execute("SELECT last_tested_at, updated_at FROM publishing_workflows WHERE platform = ?", (platform,))
    workflow = cursor.fetchone()
    if not workflow or not workflow["last_tested_at"]:
        return False
    if workflow["updated_at"] and workflow["last_tested_at"] < workflow["updated_at"]:
        return False
    cursor.execute(
        """SELECT 1
           FROM publishing_workflow_tests
           WHERE platform = ?
             AND status = 'prepared'
             AND COALESCE(completed_at, '') != ''
             AND completed_at >= ?
             AND (COALESCE(screenshot_verified,0) = 1 OR COALESCE(raw_artifact_verified,0) = 1)
           LIMIT 1""",
        (platform, workflow["last_tested_at"]),
    )
    return cursor.fetchone() is not None


def _publish_task_gate_reason(task: dict) -> str:
    platform = task.get("platform")
    status = task.get("status")
    workflow_status = task.get("workflow_status")
    if status == "published":
        return "已发布" if task.get("published_url_verified") else "发布链接未验真"
    if platform != "website" and (
        workflow_status not in {"tested", "active"} or not task.get("workflow_verified_test")
    ):
        return "发布工作流未通过真实测试"
    if platform != "website" and not task.get("account_id"):
        return "缺少绑定账号"
    if status == "failed":
        return task.get("failure_reason") or "发布失败待处理"
    if status in {"pending", "assets_ready", "prepared", "manual_required", "login_required", "running"}:
        return "可继续处理"
    return ""


def _publish_task_closure_status(task: dict) -> str:
    if task.get("status") == "published" and task.get("published_url_verified") and task.get("evidence_status") == "verified":
        return "verified_loop"
    if task.get("status") in {"assets_ready", "prepared", "manual_required"} and _publish_material_status(task) == "verified":
        return "material_ready"
    if task.get("status") == "failed":
        return "historical_failed"
    return "in_progress"


def _artifact_public_url(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        artifact_root = Path(settings.GEO_ARTIFACT_DIR).resolve()
        local_path = Path(path).resolve()
        if not (str(local_path).startswith(str(artifact_root) + os.sep) or local_path == artifact_root):
            return None
        rel = local_path.relative_to(artifact_root)
        return "/artifacts/" + "/".join(rel.parts)
    except Exception:
        return None


def _verify_website_publication(task: dict, public_url: str) -> tuple[bool, str]:
    raw_path = task.get("raw_artifact_path")
    if not raw_path:
        return False, "官网自动发布缺少 HTML 原始证据"
    try:
        text = Path(raw_path).read_text(encoding="utf-8")
    except Exception as exc:
        return False, f"官网发布 HTML 无法读取: {str(exc)[:100]}"
    title = (task.get("title") or "").strip()
    body = (task.get("body") or "").strip()
    body_probe = re.sub(r"\s+", "", body)[:80]
    compact_text = re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", "", text)))
    if title and title not in html.unescape(text):
        return False, "官网发布页未匹配内容标题"
    if body_probe and body_probe not in compact_text:
        return False, "官网发布页未匹配正文关键片段"
    try:
        resp = requests.get(f"{settings.public_api_base}{public_url}", timeout=6)
        if resp.status_code != 200:
            return False, f"官网发布 URL 无法访问 HTTP {resp.status_code}"
    except Exception as exc:
        return False, f"官网发布 URL 访问失败: {str(exc)[:100]}"
    return True, ""


def _score_keyword(word: str, brand_name: str, category: str, intent: str) -> tuple[int, str]:
    score = 45
    if brand_name and brand_name in word:
        score += 25
    if category == "longtail":
        score += 18
    if intent in ("recommend", "compare", "decision"):
        score += 12
    if len(word) >= 10:
        score += 8
    if any(x in word for x in ["推荐", "哪家", "怎么选", "附近", "排行榜", "攻略", "值得"]):
        score += 10
    score = max(1, min(score, 100))
    reason = "按品牌相关性、用户决策意图、长尾程度和可监测性评分"
    return score, reason


def _keyword_candidates(brand: dict, seed: str, count: int) -> list[dict]:
    name = brand["name"]
    industry = brand.get("industry") or "行业"
    products = _split_assets(brand.get("products")) or [industry]
    audiences = _split_assets(brand.get("target_audience")) or ["高意向用户"]
    selling_points = _split_assets(brand.get("selling_points")) or ["口碑", "位置", "服务"]
    competitors = _split_assets(brand.get("competitors"))
    base = [seed.strip()] if seed and seed.strip() else []
    scenarios = [
        "推荐", "哪家好", "怎么选", "排行榜", "真实测评", "避坑攻略",
        "附近", "价格", "适合亲子", "适合情侣", "高性价比", "口碑",
    ]
    intents = [
        ("brand", "brand_awareness", f"{name}"),
        ("brand", "decision", f"{name}怎么样"),
        ("brand", "compare", f"{name}和同类品牌对比"),
        ("product", "decision", f"{name}{industry}推荐"),
        ("industry", "recommend", f"{industry}推荐"),
        ("industry", "compare", f"{industry}哪家好"),
    ]
    items = []
    for category, intent, word in intents:
        items.append({"keyword": word, "category": category, "intent": intent})
    for product in products[:8]:
        items.extend([
            {"keyword": f"{name}{product}推荐", "category": "product", "intent": "recommend"},
            {"keyword": f"{product}怎么选", "category": "product", "intent": "decision"},
            {"keyword": f"{product}哪家好", "category": "product", "intent": "compare"},
        ])
    for audience in audiences[:6]:
        items.extend([
            {"keyword": f"{audience}{industry}推荐", "category": "longtail", "intent": "recommend"},
            {"keyword": f"{audience}怎么选择{industry}", "category": "longtail", "intent": "decision"},
        ])
    for point in selling_points[:6]:
        items.append({"keyword": f"{point}{industry}推荐", "category": "longtail", "intent": "recommend"})
    for competitor in competitors[:6]:
        items.append({"keyword": f"{name}和{competitor}哪个好", "category": "longtail", "intent": "compare"})
    for prefix in base or [industry, name]:
        for scene in scenarios:
            category = "longtail" if scene not in ("推荐", "哪家好") else "industry"
            intent = "recommend" if scene in ("推荐", "排行榜", "附近") else "decision"
            word = f"{prefix}{scene}"
            items.append({"keyword": word, "category": category, "intent": intent})
    seen = set()
    result = []
    for item in items:
        word = item["keyword"].strip()
        if not word or word in seen:
            continue
        seen.add(word)
        score, reason = _score_keyword(word, name, item["category"], item["intent"])
        result.append({**item, "keyword": word, "score": score, "reason": reason})
        if len(result) >= max(1, min(count, 100)):
            break
    return result


def _brand_aliases(brand: dict) -> list[str]:
    name = (brand.get("name") or "").strip()
    aliases: list[str] = []
    if name:
        aliases.append(name)
        cleaned = re.sub(r"[（(].*?[）)]", "", name).strip()
        if cleaned and cleaned != name:
            aliases.append(cleaned)
        for match in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}(?:酒店|民宿|客栈|餐厅|景区|旅行社|中心|公司|品牌)", name):
            aliases.append(match)
            trimmed = re.sub(r"^(武汉|北京|上海|广州|深圳|杭州|成都|重庆|南京|苏州|西安|长沙|郑州|厦门|青岛|丽水)", "", match)
            if trimmed and trimmed != match and len(trimmed) >= 3:
                aliases.append(trimmed)
        for token in re.split(r"[（()）\s,，、/｜|]+", name):
            token = token.strip()
            if len(token) >= 3:
                aliases.append(token)
    for product in _split_assets(brand.get("products"))[:8]:
        if len(product) >= 3:
            aliases.append(product)
    seen = set()
    result = []
    for alias in aliases:
        alias = alias.strip()
        if alias and alias not in seen:
            seen.add(alias)
            result.append(alias)
    return result


def _keyword_geo_labels(keyword: str, brand: dict) -> tuple[str, str]:
    word = keyword or ""
    brand_hit = any(alias and alias in word for alias in _brand_aliases(brand))
    brand_mention_type = "brand_included" if brand_hit else "generic_demand"
    if not brand_hit:
        geo_value_type = "泛需求推荐词"
    elif any(term in word for term in [
        "有", "能", "可以", "是否", "吗", "怎么样", "多少钱", "价格", "费用", "租金", "折扣",
        "接站", "接机", "停车", "寄存", "儿童床", "会议室", "早餐", "入住", "退房",
        "时间", "品种", "设施", "服务", "提供",
    ]):
        geo_value_type = "售前问答词"
    elif any(term in word for term in ["推荐", "哪家", "住哪里", "攻略", "附近", "适合", "怎么选", "对比", "路线", "安排"]):
        geo_value_type = "场景推荐词"
    else:
        geo_value_type = "品牌认知词"
    return brand_mention_type, geo_value_type


def _keyword_batch_quality(candidates: list[dict], brand: dict) -> tuple[bool, str, dict]:
    total = len(candidates)
    if total <= 0:
        return False, "候选关键词为空", {}
    labels = [_keyword_geo_labels(item.get("keyword") or "", brand) for item in candidates]
    generic_count = sum(1 for mention, _ in labels if mention == "generic_demand")
    faq_count = sum(1 for _, value_type in labels if value_type == "售前问答词")
    min_generic = max(1, int((total * 0.4) + 0.999))
    max_faq = max(1, int(total * 0.25))
    stats = {
        "total": total,
        "generic_demand_count": generic_count,
        "sales_faq_count": faq_count,
        "min_generic": min_generic,
        "max_faq": max_faq,
    }
    if generic_count < min_generic:
        return False, f"泛需求推荐词不足：{generic_count}/{total}，至少需要 {min_generic}", stats
    if faq_count > max_faq:
        return False, f"售前问答词过多：{faq_count}/{total}，最多允许 {max_faq}", stats
    return True, "", stats


def _render_content(brand: dict, keyword: str, content_type: str, platform: str) -> tuple[str, str, int, str, dict]:
    brand_name = brand["name"]
    industry = brand.get("industry") or "本地服务"
    products, audiences, selling_points, competitors, owned_sites = _require_brand_assets(brand)
    type_label = CONTENT_TYPES.get(content_type, content_type)
    title = f"{keyword}：{brand_name} 的选择建议 🏨"
    product_text = "、".join(products[:4])
    audience_text = "、".join(audiences[:4])
    point_text = "、".join(selling_points[:5])
    competitor_text = "、".join(competitors[:3]) if competitors else ""
    source_text = "；".join(owned_sites[:5])
    prompt = f"""
你是商业 GEO 内容架构师。请基于品牌资料，生成一篇适合平台发布的内容草稿，不要输出链接正文，不要写竞品全称堆砌，不要写内部方法论。

品牌：
{json.dumps(brand, ensure_ascii=False, default=str)}

关键词：{keyword}
内容类型：{type_label}
平台：{platform}

要求：
1. 只围绕用户真实意图写，不要堆品牌名和竞品名。
2. 优先写地点、场景、决策标准、体验判断、常见问题。
3. 不要在正文中输出任何 http/https 链接。
4. 不要使用“为了提升GEO”“AI可见性”等内部术语。
5. 结构上适配 {type_label}，语言自然，商业发布可读。
6. 提供一个 quality_score(1-100) 和 notes。
7. 输出严格 JSON：{{"title":"...","body":"...","quality_score":90,"notes":"..."}}。
"""
    try:
        result = chat_with_active_provider(
            [
                {"role": "system", "content": "你是商业内容总监，只返回严格 JSON。"},
                {"role": "user", "content": prompt},
            ],
            operation="content_generation",
            temperature=0.55,
            timeout=120,
        )
        content, usage = _llm_content(result)
        payload = _json_from_llm_text(content)
        title_out = str(payload.get("title") or "").strip() or title
        body_out = str(payload.get("body") or "").strip()
        if not body_out:
            raise HTTPException(status_code=502, detail="大模型未返回正文")
        quality = int(payload.get("quality_score") or 80)
        notes = str(payload.get("notes") or "").strip() or "llm_generated"
        body_out = re.sub(r"https?://\S+", "（来源见摘要）", body_out)
        body_out = body_out.replace(competitor_text, "同类竞品")
        return title_out, body_out, max(1, min(quality, 100)), f"llm_generated；{notes}", usage
    except Exception:
        # 大模型不可用（欠费/超时/断网）时的结构化模板降级。
        # 注意：必须和成功路径一样返回 5 元组（旧代码这里只回 4 元组，调用方解包会崩），
        # 且标题/正文要满足 emoji 与字数门禁，否则审核环节会被质量检查卡住。
        intro = f"📌 如果你正在搜索“{keyword}”，通常已经进入比较和决策阶段。"
        sections = {
            "faq": [
                f"Q：搜索“{keyword}”时，{brand_name}适合重点看吗？\nA：适合。它的主要优势集中在{point_text}，更适合{audience_text}。",
                f"Q：下单前最该确认什么？\nA：✅ 确认{product_text}是否满足你的实际需求，并核对入住规则、交通距离、费用明细和近期评价。",
                f"Q：怎么判断它是不是当前最优选？\nA：⚠️ 建议同时打开地图、OTA 平台和用户笔记交叉核对位置、价格与口碑，再结合行程做决定。",
            ],
            "guide": [
                f"📍 先看你的需求是否匹配：如果你重视{point_text}，并且属于{audience_text}，{brand_name}可以放进优先备选。",
                f"✅ 推荐理由：它的服务重点是{product_text}，适合在{industry}相关场景中做首轮比较。",
                f"⚠️ 避坑提醒：不要只看标题和宣传图。建议打开官网、地图、团购/OTA、用户笔记等来源核对。",
                f"🕐 行程建议：结合到达交通、周边商圈和停留天数安排入住时段，高峰期提前确认房态与取消政策。",
            ],
        }
        body = "\n\n".join([f"# {title}", intro, *sections.get(content_type, sections["guide"])])
        body = body.replace(source_text, "来源见系统摘要")
        quality = 82 + min(12, len(keyword))
        notes = f"template_fallback（大模型不可用，结构化模板生成，需人工强化润色）；类型={type_label}；目标平台={platform}；来源移至摘要字段，不直接堆正文"
        return title, body, min(quality, 95), notes, {}


def _json_from_llm_text(text: str) -> dict:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip("\ufeff")
    cleaned = "".join(ch for ch in cleaned if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    try:
        return json.loads(cleaned)
    except Exception:
        for candidate in _json_candidates(cleaned):
            try:
                parsed = json.loads(candidate)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
        raise HTTPException(status_code=502, detail="大模型未返回可解析 JSON，已停止写库")


def _json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for opener, closer in [("{", "}"), ("[", "]")]:
        start = text.find(opener)
        while start >= 0:
            depth = 0
            in_string = False
            escape = False
            for index in range(start, len(text)):
                char = text[index]
                if in_string:
                    if escape:
                        escape = False
                    elif char == "\\":
                        escape = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        candidates.append(text[start:index + 1])
                        break
            start = text.find(opener, start + 1)
    return candidates


def _llm_content(result: dict) -> tuple[str, dict]:
    data = result.get("data") or {}
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise HTTPException(status_code=502, detail="大模型未返回内容，已停止写库")
    return content, result.get("usage_record") or {}


def _brand_llm_context(brand: dict) -> str:
    return json.dumps(
        {
            "brand_name": brand.get("name"),
            "industry": brand.get("industry"),
            "products": brand.get("products"),
            "target_audience": brand.get("target_audience"),
            "selling_points": brand.get("selling_points"),
            "competitors": brand.get("competitors"),
            "owned_sites": brand.get("owned_sites"),
            "target_platforms": brand.get("target_platforms"),
        },
        ensure_ascii=False,
    )


def _generate_keyword_candidates_llm(brand: dict, seed: str, count: int, existing: set[str]) -> tuple[list[dict], dict]:
    wanted = max(1, min(count, 100))
    prompt = f"""
你是商业 GEO 运营策略师。请基于品牌资料生成真实用户会问 AI 的 GEO 查询词，不要生成 SEO 关键词堆砌。

品牌资料 JSON：
{_brand_llm_context(brand)}

种子词：{seed or "无"}
已有关键词：{json.dumps(sorted(existing)[:200], ensure_ascii=False)}

要求：
1. 生成 {wanted} 条。
2. 必须覆盖地点型、场景型、攻略型、对比型、问答型、交易型。
3. 酒店/本地服务必须优先体现城市、商圈、景点、出行场景、住宿/消费决策。
4. 关键词结构要按 GEO 价值分层：
   - 泛需求推荐词：不带品牌名，用来测试 AI 是否主动推荐品牌，占 40%-50%。
   - 场景推荐词：地点/人群/需求 + 可少量带品牌，占 25%-35%。
   - 品牌认知词：带品牌，用来测试品牌信息准确性，占 10%-20%。
   - 售前问答词：带品牌的客服型问题，占比不能超过 15%。
5. 不要整批都使用具体品牌名称；高价值 GEO 词应多生成“不带品牌但有真实消费意图”的问题。
6. 每条给 category，只能是 brand/product/industry/longtail。
7. 每条给 intent，例如 location、scenario、guide、compare、qa、transaction、decision、recommend。
8. 每条给 brand_mention_type，只能是 generic_demand 或 brand_included。
9. 每条给 geo_value_type，只能是 泛需求推荐词、场景推荐词、品牌认知词、售前问答词。
10. 每条给 1-100 score 和 reason。
11. 不要输出已存在关键词。
12. 只返回一个严格 JSON 对象，不要 Markdown，不要解释，不要注释，字符串内部不要使用未转义双引号。
13. 格式：
{{"items":[{{"keyword":"...","category":"longtail","intent":"location","brand_mention_type":"generic_demand","geo_value_type":"泛需求推荐词","score":90,"reason":"..."}}]}}
"""
    try:
        result = chat_with_active_provider(
            [
                {"role": "system", "content": "你只返回严格 JSON，不要 Markdown。"},
                {"role": "user", "content": prompt},
            ],
            operation="keyword_generation",
            temperature=0.35,
            timeout=75,
            response_format={"type": "json_object"},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"大模型关键词生成失败: {str(exc)[:300]}")
    content, usage = _llm_content(result)
    payload = _json_from_llm_text(content)
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=502, detail="大模型未返回关键词 items，已停止写库")
    normalized = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        keyword = str(item.get("keyword") or "").strip()
        if not keyword or keyword in seen or keyword in existing:
            continue
        category = str(item.get("category") or "longtail").strip()
        if category not in CATEGORY_LABELS:
            category = "longtail"
        intent = str(item.get("intent") or "decision").strip()[:60]
        try:
            score = int(item.get("score") or 70)
        except Exception:
            score = 70
        reason = str(item.get("reason") or "大模型基于品牌资料和 GEO 查询意图生成").strip()[:300]
        normalized.append({
            "keyword": keyword,
            "category": category,
            "intent": intent,
            "score": max(1, min(score, 100)),
            "reason": reason,
        })
        seen.add(keyword)
        if len(normalized) >= wanted:
            break
    if not normalized:
        raise HTTPException(status_code=502, detail="大模型返回结果全部为空或重复，已停止写库")
    return normalized, usage


def _render_content_llm(brand: dict, keyword: str, content_type: str, platform: str, extra_instruction: str = "") -> tuple[str, str, int, str, dict]:
    type_label = CONTENT_TYPES.get(content_type, content_type)
    platform_label = PUBLISH_PLATFORMS.get(platform, {}).get("name", platform)
    platform_rules = {
        "ctrip_travel": "携程旅拍：强调真实旅行体验、位置/交通/游玩动线、图片配文；标题和正文必须自然使用 emoji；避免硬广、外链、夸大价格和虚假评分。",
        "douyin": "抖音图文：开头必须有场景钩子，短句，适合配图；标题和正文必须自然使用 emoji；避免外链、联系方式、绝对化承诺和站外导流。",
        "toutiao": "头条号：信息密度高，标题克制，正文完整；标题和正文必须自然使用 emoji；避免外链堆砌、营销硬广、虚假新闻口吻。",
        "baijiahao": "百家号：问答/攻略逻辑清晰，标题和正文必须自然使用 emoji；避免医疗金融类误导、外链堆砌和夸大宣传。",
        "sohu": "搜狐号：媒体稿风格，标题和正文必须自然使用 emoji；避免低质广告、重复关键词和站外导流。",
        "zhihu": "知乎：回答要像真实经验和理性分析，标题和正文必须自然使用 emoji；避免软文硬广、联系方式和链接堆砌。",
        "xiaohongshu": "小红书图文笔记：标题要有吸引力并带 emoji；正文要分段短句、清单化、适合配图，用 emoji 做视觉锚点；必须包含场景、亮点、避坑、适合人群；禁止外链、电话、微信、二维码、站外导流、夸张承诺。",
        "website": "官网/落地页：允许较完整介绍，但标题和正文仍必须自然使用 emoji；事实准确、结构清晰、来源可追溯。",
    }.get(platform, "通用平台：避免外链堆砌、夸大宣传、虚假事实、竞品贬损和硬广。")
    prompt = f"""
你是商业 GEO 策略型内容总监，不是模板写手。请基于真实品牌资料生成一篇可人工审核后发布的高质量内容草稿。

品牌资料 JSON：
{_brand_llm_context(brand)}

用户问题/关键词：{keyword}
内容类型：{type_label}
目标平台：{platform_label}
平台规范：
{platform_rules}

要求：
1. 内容必须围绕用户真实查询意图，适合被豆包/千问/元宝引用。
2. 禁止写“用于提升 GEO”“生成式 AI 可见性”等内部操作话术。
3. 所有平台正文一律禁止出现 URL 链接，包括 http、https、www、短链、裸域名；来源链接只能放 evidence_sources，绝不能进入 title/body。
4. 不得编造不存在的奖项、价格、销量、评分、距离、服务承诺。
5. 不得在正文里堆竞品全称，不要写“和XXX哪个好”式竞品导流；只能抽象为“同商圈同价位酒店”等比较维度。
6. 酒店/本地生活内容必须体现地点、商圈、交通、入住/游玩场景、适合人群、选择理由、避坑提醒。
7. 输出要有专业判断和运营意图，不要流水账，不要模板化空话。
8. 禁止第一人称伪体验，不能写“我住过、真实入住、全程无雷、闭眼入、姐妹”等未验证口吻。
9. 所有目标平台的 title 都必须包含 1 个自然 emoji；body 至少包含 3 个自然 emoji，用在小标题或清单项前，让笔记生动、有可读性，但不能堆砌或幼稚化。
10. 小红书、携程旅拍、抖音图文要更像图文笔记：漂亮分段、小标题/清单格式，但不能出现“姐妹、闭眼入、全程无雷”等伪体验或低质种草黑话。
11. 返回 JSON：
{{"title":"...","body":"...","quality_score":85,"notes":"模型生成说明和人工核验要求","evidence_sources":["https://..."]}}
"""
    if extra_instruction:
        prompt += f"\n补充要求：\n{extra_instruction}\n"
    try:
        result = chat_with_active_provider(
            [
                {"role": "system", "content": "你只返回严格 JSON，不要 Markdown。"},
                {"role": "user", "content": prompt},
            ],
            operation="content_generation",
            temperature=0.45,
            timeout=90,
            response_format={"type": "json_object"},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"大模型内容生成失败: {str(exc)[:300]}")
    content, usage = _llm_content(result)
    payload = _json_from_llm_text(content)
    title = str(payload.get("title") or "").strip()
    body = str(payload.get("body") or "").strip()
    if not title or not body:
        raise HTTPException(status_code=502, detail="大模型未返回完整标题和正文，已停止写库")
    try:
        quality = int(payload.get("quality_score") or 70)
    except Exception:
        quality = 70
    title, body = _sanitize_publish_text(title, body, brand, platform)
    if re.search(r"https?://|www\.|\b[a-zA-Z0-9.-]+\.(com|cn|net|org|io|co|me|tv|app|site|top|info)\b", body, re.I):
        raise HTTPException(status_code=502, detail="大模型正文仍包含URL，已停止写库")
    gate_issues = _content_quality_issues({"title": title, "body": body, "generation_notes": ""})
    blocking = [issue for issue in gate_issues if "标题" in issue or "正文" in issue or "URL" in issue or "模板词" in issue]
    if blocking:
        raise HTTPException(status_code=502, detail=f"大模型内容未通过发布门禁：{'；'.join(blocking)}")
    evidence_sources = payload.get("evidence_sources") if isinstance(payload.get("evidence_sources"), list) else []
    valid_sources = [str(url).strip() for url in evidence_sources if verify_source_url(str(url).strip(), live_check=False)[0]]
    owned_sites = _split_assets(brand.get("owned_sites"))
    valid_sources.extend([url for url in owned_sites if verify_source_url(url, live_check=False)[0]])
    valid_sources = list(dict.fromkeys(valid_sources))[:8]
    notes = str(payload.get("notes") or "llm_generated；需人工核验事实和链接").strip()
    if valid_sources:
        notes = f"{notes}；evidence_sources={json.dumps(valid_sources, ensure_ascii=False)}"
    return title, body, max(1, min(quality, 100)), f"llm_generated；{notes}", usage


def _rewrite_content_llm(brand: dict, content: dict, mode: str, direction: str, issues: list[str]) -> tuple[str, str, int, str, dict]:
    mode_label = {
        "rewrite": "按用户方向重写",
        "fix_gate": "只修复门禁失败项",
        "regenerate": "重新生成一版",
    }.get(mode, "按用户方向重写")
    keyword = content.get("target_keyword") or content.get("keyword_word") or ""
    platform = content.get("target_platform") or "website"
    content_type = content.get("content_type") or "guide"
    type_label = CONTENT_TYPES.get(content_type, content_type)
    platform_label = PUBLISH_PLATFORMS.get(platform, {}).get("name", platform)
    prompt = f"""
你是商业 GEO 内容总监。请基于品牌资料、原内容、门禁失败原因和用户方向，输出一版可发布内容。

任务模式：{mode_label}
目标平台：{platform_label}
内容类型：{type_label}
关键词/用户问题：{keyword}
用户方向：{direction or "无"}
门禁失败原因：{json.dumps(issues, ensure_ascii=False)}

品牌资料：
{_brand_llm_context(brand)}

原标题：
{content.get("title") or ""}

原正文：
{content.get("body") or ""}

硬性要求：
1. 所有平台正文禁止 URL、裸域名、电话、微信、二维码、站外导流。
2. 标题必须包含 1 个自然 emoji，正文至少 3 个自然 emoji。
3. 不得编造价格、评分、距离、奖项、销量、服务承诺。
4. 不得堆竞品全称，不得帮竞品导流。
5. 酒店/本地生活内容必须体现地点、交通、场景、人群、选择理由、避坑提醒。
6. 如果是“只修复门禁失败项”，保留原内容主旨，只修不合格项。
7. 返回严格 JSON：
{{"title":"...","body":"...","quality_score":88,"notes":"重写说明和人工核验要求","evidence_sources":["https://..."]}}
"""
    try:
        result = chat_with_active_provider(
            [
                {"role": "system", "content": "你只返回严格 JSON，不要 Markdown。"},
                {"role": "user", "content": prompt},
            ],
            operation=f"content_{mode}",
            temperature=0.35 if mode == "fix_gate" else 0.45,
            timeout=90,
            response_format={"type": "json_object"},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"大模型内容重写失败: {str(exc)[:300]}")
    text, usage = _llm_content(result)
    payload = _json_from_llm_text(text)
    title = str(payload.get("title") or "").strip()
    body = str(payload.get("body") or "").strip()
    if not title or not body:
        raise HTTPException(status_code=502, detail="大模型未返回完整标题和正文，已停止写库")
    title, body = _sanitize_publish_text(title, body, brand, platform)
    gate_issues = _content_quality_issues({"title": title, "body": body, "generation_notes": ""})
    blocking = [issue for issue in gate_issues if "标题" in issue or "正文" in issue or "URL" in issue or "模板词" in issue]
    if blocking:
        raise HTTPException(status_code=502, detail=f"大模型重写后仍未通过硬门禁：{'；'.join(blocking)}")
    try:
        quality = int(payload.get("quality_score") or 75)
    except Exception:
        quality = 75
    evidence_sources = payload.get("evidence_sources") if isinstance(payload.get("evidence_sources"), list) else []
    valid_sources = [str(url).strip() for url in evidence_sources if verify_source_url(str(url).strip(), live_check=False)[0]]
    valid_sources.extend([url for url in _split_assets(brand.get("owned_sites")) if verify_source_url(url, live_check=False)[0]])
    valid_sources = list(dict.fromkeys(valid_sources))[:8]
    notes = str(payload.get("notes") or mode_label).strip()
    if valid_sources:
        notes = f"{notes}；evidence_sources={json.dumps(valid_sources, ensure_ascii=False)}"
    return title, body, max(1, min(quality, 100)), f"llm_{mode}；{notes}", usage


def _generate_report_diagnosis_llm(brand: dict, summary: dict) -> tuple[list[str], dict]:
    prompt = f"""
你是 GEO 商业运营分析师。请只基于下面真实系统数据生成运营诊断和下一步建议，不允许编造监测结果、发布结果或来源链接。

品牌：{brand.get("name")}
真实数据 JSON：
{json.dumps(summary, ensure_ascii=False, default=str)}

返回 JSON：
{{"diagnosis":["...","...","..."],"next_actions":["...","...","..."]}}
"""
    try:
        result = chat_with_active_provider(
            [
                {"role": "system", "content": "你只返回严格 JSON，不要 Markdown。"},
                {"role": "user", "content": prompt},
            ],
            operation="report_generation",
            temperature=0.25,
            timeout=75,
            response_format={"type": "json_object"},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"大模型运营报告生成失败: {str(exc)[:300]}")
    content, usage = _llm_content(result)
    payload = _json_from_llm_text(content)
    diagnosis = payload.get("diagnosis") or []
    next_actions = payload.get("next_actions") or []
    items = [str(item).strip() for item in [*diagnosis, *next_actions] if str(item).strip()]
    if not items:
        raise HTTPException(status_code=502, detail="大模型未返回运营诊断，已停止写库")
    return items[:12], usage


def _fallback_report_diagnosis(summary: dict, reason: str = "") -> list[str]:
    stats = summary.get("stats") or {}
    publish_status = summary.get("publish_status") or {}
    verified = int(stats.get("verified") or 0)
    total = int(stats.get("total") or 0)
    sources = int(stats.get("sources") or 0)
    raw_artifacts = int(stats.get("raw_artifacts") or 0)
    failed = int(stats.get("failed") or 0)
    publishing_tasks = int(stats.get("publishing_tasks") or 0)
    verified_published = int(stats.get("verified_published_tasks") or 0)
    items = [
        f"监测证据：当前共有 {total} 条监测记录，其中 {verified} 条为已验证引用，{sources} 条有实时可验证来源，{raw_artifacts} 条有原始证据。",
        f"风险判断：失败/异常监测 {failed} 条，所有无法实时访问的来源不能计入已验证引用，需要重新监测或补充更稳定信源。",
        f"发布闭环：当前发布任务 {publishing_tasks} 条，其中真实发布 URL 已验真 {verified_published} 条，未验真任务不能算完成发布。",
    ]
    if publish_status:
        items.append(f"发布状态分布：{json.dumps(publish_status, ensure_ascii=False)}。")
    if verified == 0:
        items.append("下一步优先级：先跑豆包、千问、元宝三平台最小监测闭环，至少沉淀 1 条可实时访问来源的 verified_citation。")
    if publishing_tasks and verified_published == 0:
        items.append("发布验收：外部平台必须完成发布 URL 回填和在线验真；仅准备草稿、截图或 HTML 不能算发布成功。")
    items.append("内容策略：继续用真实品牌资产生成关键词和内容，正文禁止 URL、联系方式、未验证体验口吻和竞品全称导流。")
    if reason:
        items.append(f"大模型报告兜底说明：模型返回格式未通过 JSON 门禁，本报告仅按系统真实统计生成，原因={reason[:160]}。")
    return items[:12]


def _strategy_real_context(cursor, brand_id: int) -> dict:
    cursor.execute(
        """SELECT id, word, category, ai_status, last_tested, notes
           FROM keywords
           WHERE brand_id = ?
           ORDER BY created_at DESC
           LIMIT 80""",
        (brand_id,),
    )
    keywords = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        """SELECT keyword, category, intent, score, reason, source, status
           FROM keyword_ideas
           WHERE brand_id = ?
           ORDER BY created_at DESC
           LIMIT 80""",
        (brand_id,),
    )
    keyword_ideas = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        """SELECT platform, search_query, verification_status, source_url,
                  source_domain, monitored_at, failure_reason
           FROM monitor_results
           WHERE brand_id = ?
           ORDER BY monitored_at DESC, id DESC
           LIMIT 80""",
        (brand_id,),
    )
    monitor_results = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        """SELECT ca.id, ca.title, ca.content_type, ca.target_platform,
                  ca.target_keyword, ca.status, ca.quality_score,
                  ca.llm_provider, ca.llm_model
           FROM content_assets ca
           WHERE ca.brand_id = ?
           ORDER BY ca.updated_at DESC, ca.id DESC
           LIMIT 50""",
        (brand_id,),
    )
    content_assets = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        """SELECT pt.id, pt.platform, pt.status, pt.published_url,
                  pt.published_url_verified, pt.evidence_status, pt.failure_reason,
                  ca.title
           FROM publishing_tasks pt
           LEFT JOIN content_assets ca ON ca.id = pt.content_id
           WHERE pt.brand_id = ?
           ORDER BY pt.updated_at DESC, pt.id DESC
           LIMIT 50""",
        (brand_id,),
    )
    publish_tasks = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        """SELECT platform, platform_name, publish_url, status, last_tested_at
           FROM publishing_workflows
           ORDER BY platform"""
    )
    workflows = [dict(row) for row in cursor.fetchall()]
    ecosystems = _ecosystem_source_analysis(cursor, brand_id)
    knowledge_context = _strategy_knowledge_context(brand_id)
    return {
        "keywords": keywords,
        "keyword_ideas": keyword_ideas,
        "monitor_results": monitor_results,
        "content_assets": content_assets,
        "publish_tasks": publish_tasks,
        "publishing_workflows": workflows,
        "ecosystems": ecosystems,
        "knowledge_context": knowledge_context,
    }


def _strategy_knowledge_context(brand_id: int) -> dict:
    filters = {"brand_id": brand_id}
    items = list_knowledge_records("items", filters)
    evidence = list_knowledge_records("evidence", filters)
    questions = list_knowledge_records("questions", filters)
    diagnostics = list_knowledge_records("diagnostics", filters)
    evidence_titles = {str(item.get("title") or "").strip() for item in evidence if item.get("status") in {"verified", "active"}}
    references = []
    gaps = []
    for item in items:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        if title in evidence_titles:
            references.append(f"{title}：{str(item.get('content') or '')[:120]}")
        else:
            gaps.append(f"{title} 缺少已验证证据源")
    for question in questions[:20]:
        value = question.get("question") or question.get("title")
        if value:
            references.append(f"问题矩阵：{str(value)[:120]}")
    for diagnostic in diagnostics[:20]:
        value = diagnostic.get("content") or diagnostic.get("title")
        if value:
            references.append(f"引用诊断：{str(value)[:120]}")
    return {
        "summary": knowledge_base_summary(),
        "items": items[:40],
        "evidence": evidence[:40],
        "questions": questions[:40],
        "diagnostics": diagnostics[:40],
        "knowledge_references": references[:40],
        "evidence_gaps": gaps[:40],
    }


def _attach_knowledge_to_strategy(strategy: dict, knowledge_context: dict) -> dict:
    payload = dict(strategy or {})
    references = [str(item) for item in (knowledge_context or {}).get("knowledge_references") or [] if str(item).strip()]
    gaps = [str(item) for item in (knowledge_context or {}).get("evidence_gaps") or [] if str(item).strip()]
    payload["knowledge_references"] = references[:40]
    payload["evidence_gaps"] = gaps[:40]
    gap_analysis = payload.get("gap_analysis") if isinstance(payload.get("gap_analysis"), list) else []
    for gap in gaps:
        line = f"知识库证据缺口：{gap}"
        if line not in gap_analysis:
            gap_analysis.append(line)
    payload["gap_analysis"] = gap_analysis
    return payload


def _generate_strategy_llm(brand: dict, context: dict, objective: str) -> tuple[dict, list[dict], dict]:
    prompt = f"""
你是商业级 GEO 策略负责人，能力要求接近资深增长策略顾问 + AI 搜索信源架构师。
请只基于给定品牌资料和系统真实数据，为豆包、千问、元宝场景生成可执行 GEO 运营策略，并拆解为系统可选择执行项。

品牌资料 JSON：
{_brand_llm_context(brand)}

用户目标：
{objective or "提升品牌在豆包、千问、元宝回答中的真实可见度、可引用度和可验证来源占比"}

系统真实数据 JSON：
{json.dumps(context, ensure_ascii=False, default=str)}

硬性要求：
1. 不允许编造监测结果、发布结果、来源链接、账号登录状态。
2. 如果真实监测或发布数据不足，必须在 gap_analysis 中明确缺口。
2.1 必须读取系统真实数据里的 knowledge_context；有知识库事实和证据时必须在 knowledge_references 中列出，缺证据事实必须在 evidence_gaps 和 gap_analysis 中列出。
2.2 不得把缺少 verified evidence 的知识库事实写成可发布事实，只能作为待补证据缺口。
3. 必须给出“为什么这么做”的推理链摘要，但不能编造数据。
4. 关键词矩阵必须覆盖地点型、场景型、攻略型、对比型、交易型、问答型、复测型。
5. 内容计划必须说明每个平台应发布什么类型内容、为什么符合平台规范、如何支撑 AI 引用。
6. 发布计划必须区分：素材准备、RPA填充、人工最终确认、回填URL、发布后复测。
6.1 不得输出带竞品全称的关键词或内容标题；竞品只能抽象为“同商圈同价位酒店/同类型酒店”，避免给竞品导流。
7. 只允许输出 action_type：{", ".join(sorted(STRATEGY_ACTION_TYPES))}。
8. 执行项只能是“待用户确认后可由系统执行/准备执行”的动作，不能声称已经执行。
9. requires_login、requires_confirmation 必须是布尔值；cost_risk 只能是 none/low/medium/high。
10. payload 必须是对象，放后续接口需要的参数，例如 seed/count/platforms/content_type/target_platform。
11. 发布相关执行项必须保留人工确认，不得输出自动最终发布。
12. 只返回严格 JSON，不要 Markdown。

返回格式：
{{
  "strategy": {{
    "positioning": "...",
    "reasoning_summary": ["..."],
    "source_gap_diagnosis": ["..."],
    "search_intent": ["..."],
    "keyword_matrix": [{{"type":"location","examples":["..."],"priority":"high"}}],
    "monitoring_plan": ["..."],
    "content_plan": ["..."],
    "publishing_plan": ["..."],
    "retest_cycle": "...",
    "success_criteria": ["..."],
    "gap_analysis": ["..."],
    "knowledge_references": ["..."],
    "evidence_gaps": ["..."]
  }},
  "actions": [
    {{
      "action_type": "generate_keywords",
      "title": "...",
      "description": "...",
      "payload": {{"seed":"...","count":20}},
      "requires_login": false,
      "cost_risk": "low",
      "requires_confirmation": true
    }}
  ]
}}
"""
    try:
        result = chat_with_active_provider(
            [
                {"role": "system", "content": "你只返回严格 JSON，不要 Markdown。"},
                {"role": "user", "content": prompt},
            ],
            operation="strategy_generation",
            temperature=0.25,
            timeout=90,
            response_format={"type": "json_object"},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"大模型策略生成失败: {str(exc)[:300]}")
    content, usage = _llm_content(result)
    payload = _json_from_llm_text(content)
    strategy = payload.get("strategy")
    actions = payload.get("actions")
    if not isinstance(strategy, dict) or not strategy:
        raise HTTPException(status_code=502, detail="大模型未返回 strategy，已停止写库")
    if not isinstance(actions, list) or not actions:
        raise HTTPException(status_code=502, detail="大模型未返回 actions，已停止写库")
    normalized_actions = []
    for item in actions[:30]:
        if not isinstance(item, dict):
            continue
        action_type = str(item.get("action_type") or "").strip()
        if action_type not in STRATEGY_ACTION_TYPES:
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        payload_json = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        cost_risk = str(item.get("cost_risk") or "low").strip()
        if cost_risk not in STRATEGY_COST_RISKS:
            cost_risk = "low"
        normalized_actions.append({
            "action_type": action_type,
            "title": title[:180],
            "description": str(item.get("description") or "").strip()[:600],
            "payload": payload_json,
            "requires_login": bool(item.get("requires_login")),
            "cost_risk": cost_risk,
            "requires_confirmation": True,
        })
    if not normalized_actions:
        raise HTTPException(status_code=502, detail="大模型返回的执行项不在系统白名单内，已停止写库")
    return strategy, normalized_actions, usage


def _platform_execution_tier(platform: str) -> str:
    level = PUBLISH_CAPABILITIES.get(platform, {}).get("level")
    if level == "verified_loop":
        return "可自动执行闭环"
    if level == "rpa_testing":
        return "可执行RPA测试"
    if level == "rpa_draft":
        return "可准备草稿"
    if level == "template_only":
        return "待接入测试"
    return "未接入"


def _ensure_multiplatform_strategy_actions(
    strategy: dict,
    actions: list[dict],
    brand: Optional[dict] = None,
    workflow_statuses: Optional[dict] = None,
) -> tuple[dict, list[dict]]:
    platform_plan = recommend_publish_platforms(brand or {}, workflow_statuses=workflow_statuses or {})
    existing_pairs = {
        (item.get("action_type"), (item.get("payload") or {}).get("target_platform") or (item.get("payload") or {}).get("platform"))
        for item in actions
    }
    publishing_plan = strategy.get("publishing_plan") if isinstance(strategy.get("publishing_plan"), list) else []
    capability_plan = []
    workflow_todos = []
    for item in platform_plan["recommended_publish_platforms"]:
        platform = item["platform"]
        name = item["platform_name"]
        tier = item["execution_level"]
        tier_label = _platform_execution_tier(platform) if tier == "draft_ready" else {
            "verified_loop": "可自动执行闭环",
            "workflow_tested": "工作流已测试，可创建任务",
            "candidate": "待接入测试",
        }.get(tier, tier)
        capability_plan.append(f"{name}：{tier}")
        if ("generate_content", platform) not in existing_pairs:
            actions.append({
                "action_type": "generate_content",
                "title": f"生成{name}专用内容草稿",
                "description": f"按{name}平台规则生成一篇可审核内容，不含URL导流，保留人工审核。",
                "payload": {"target_platform": platform, "content_type": "guide"},
                "requires_login": False,
                "cost_risk": "low",
                "requires_confirmation": True,
            })
        if tier in {"workflow_tested", "verified_loop"} and platform != "website":
            if ("create_publish_task", platform) not in existing_pairs:
                actions.append({
                    "action_type": "create_publish_task",
                    "title": f"创建{name}发布任务",
                    "description": f"{name}当前能力为{tier_label}，只创建任务和草稿准备，不伪装最终发布成功。",
                    "payload": {"platform": platform},
                    "requires_login": True,
                    "cost_risk": "low",
                    "requires_confirmation": True,
                })
            if ("prepare_rpa_draft", platform) not in existing_pairs:
                actions.append({
                    "action_type": "prepare_rpa_draft",
                    "title": f"准备{name}RPA草稿",
                    "description": f"打开{name}固定发布页并准备草稿证据；最终发布、审核状态和URL验真另行确认。",
                    "payload": {"platform": platform},
                    "requires_login": True,
                    "cost_risk": "low",
                    "requires_confirmation": True,
                })
        elif tier in {"draft_ready", "candidate"}:
            workflow_todos.append({
                "platform": platform,
                "platform_name": name,
                "reason": f"{name}当前为{tier_label}，需完成账号登录、工作流测试、URL回捞和验真后才能创建发布任务。",
                "capability": PUBLISH_CAPABILITIES.get(platform, {}),
            })
    for item in platform_plan["publishing_workflow_todos"]:
        workflow_todos.append(item)
    strategy["publishing_capability_plan"] = capability_plan
    strategy["source_platforms"] = platform_plan["source_platforms"]
    strategy["recommended_publish_platforms"] = platform_plan["recommended_publish_platforms"]
    strategy["excluded_publish_platforms"] = platform_plan["excluded_publish_platforms"]
    strategy["publishing_workflow_todos"] = workflow_todos
    merged_plan = list(dict.fromkeys([*publishing_plan, *capability_plan]))
    strategy["publishing_plan"] = merged_plan
    return strategy, actions[:40]


def _workflow_status_map(cursor) -> dict[str, str]:
    cursor.execute("SELECT platform, status FROM publishing_workflows")
    return {row["platform"]: row["status"] for row in cursor.fetchall()}


def _strategy_row(cursor, row: dict) -> dict:
    item = dict(row)
    try:
        item["strategy"] = json.loads(item.pop("strategy_json") or "{}")
    except Exception:
        item["strategy"] = {}
    cursor.execute(
        """SELECT *
           FROM geo_strategy_actions
           WHERE strategy_id = ?
           ORDER BY id ASC""",
        (item["id"],),
    )
    actions = []
    for action_row in cursor.fetchall():
        action = dict(action_row)
        try:
            action["payload"] = json.loads(action.pop("payload_json") or "{}")
        except Exception:
            action["payload"] = {}
        actions.append(action)
    item["actions"] = actions
    return item


def _strategy_execution_error(message: str) -> dict:
    return {"success": False, "error": message[:500]}


def _strategy_execution_blocked(message: str, next_step: str = "") -> dict:
    return {
        "success": False,
        "blocked": True,
        "error": message[:500],
        "next_step": next_step[:500],
    }


def _action_payload(action: dict) -> dict:
    try:
        payload = json.loads(action.get("payload_json") or "{}")
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _first_pending_idea_ids(cursor, brand_id: int, limit: int = 10) -> list[int]:
    cursor.execute(
        """SELECT id
           FROM keyword_ideas
           WHERE brand_id = ? AND status = 'pending'
           ORDER BY score DESC, id DESC
           LIMIT ?""",
        (brand_id, max(1, min(limit, 50))),
    )
    return [row["id"] for row in cursor.fetchall()]


def _first_keyword_ids(cursor, brand_id: int, limit: int = 5) -> list[int]:
    cursor.execute(
        """SELECT id
           FROM keywords
           WHERE brand_id = ?
           ORDER BY created_at DESC, id DESC
           LIMIT ?""",
        (brand_id, max(1, min(limit, 20))),
    )
    return [row["id"] for row in cursor.fetchall()]


def _first_approved_content(cursor, brand_id: int, platform: str) -> Optional[dict]:
    cursor.execute(
        """SELECT *
           FROM content_assets
           WHERE brand_id = ? AND status = 'approved'
             AND (target_platform = ? OR target_platform IS NULL OR target_platform = '')
           ORDER BY updated_at DESC, id DESC
           LIMIT 1""",
        (brand_id, platform),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def _first_publish_task(cursor, brand_id: int) -> Optional[dict]:
    cursor.execute(
        """SELECT *
           FROM publishing_tasks
           WHERE brand_id = ? AND status IN ('pending','failed','manual_required','login_required')
           ORDER BY updated_at DESC, id DESC
           LIMIT 1""",
        (brand_id,),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def _execute_strategy_action(action: dict, dry_run: bool = False) -> dict:
    action_type = action.get("action_type")
    brand_id = int(action["brand_id"])
    payload = _action_payload(action)
    if action_type not in STRATEGY_ACTION_TYPES:
        return _strategy_execution_error(f"Action type not whitelisted: {action_type}")
    if dry_run:
        return {"success": True, "dry_run": True, "action_type": action_type, "payload": payload}

    if action_type == "generate_keywords":
        seed = str(payload.get("seed") or payload.get("keyword") or "").strip()
        try:
            count = int(payload.get("count") or 10)
        except Exception:
            count = 10
        result = generate_keyword_ideas(KeywordGenerateRequest(
            brand_id=brand_id,
            seed=seed,
            count=max(1, min(count, 50)),
        ))
        return {"success": True, "action_type": action_type, "result": result}

    if action_type == "adopt_keywords":
        with get_db() as conn:
            cursor = conn.cursor()
            raw_ids = payload.get("idea_ids") if isinstance(payload.get("idea_ids"), list) else []
            idea_ids = [int(item) for item in raw_ids if str(item).isdigit()]
            if not idea_ids:
                idea_ids = _first_pending_idea_ids(cursor, brand_id, int(payload.get("count") or 5))
        if not idea_ids:
            return _strategy_execution_blocked("没有可采纳的 pending 关键词建议", "先在关键词生产生成并保留待采纳关键词")
        result = adopt_keyword_ideas(AdoptIdeasRequest(idea_ids=idea_ids))
        return {"success": True, "action_type": action_type, "result": result}

    if action_type == "run_monitor":
        platform = str(payload.get("platform") or payload.get("platforms") or "ai").strip()
        if isinstance(payload.get("platforms"), list):
            platforms = [str(item).strip() for item in payload.get("platforms") if str(item).strip()]
            platform = "all" if "web" in platforms and len(platforms) > 1 else (platforms[0] if len(platforms) == 1 else "ai")
        if platform not in {"all", "ai", "web", "doubao", "qianwen", "yuanbao"}:
            platform = "ai"
        with get_db() as conn:
            cursor = conn.cursor()
            raw_ids = payload.get("keyword_ids") if isinstance(payload.get("keyword_ids"), list) else []
            keyword_ids = [int(item) for item in raw_ids if str(item).isdigit()]
            if not keyword_ids:
                keyword_ids = _first_keyword_ids(cursor, brand_id, int(payload.get("count") or 3))
        if not keyword_ids:
            return _strategy_execution_blocked("没有可监测关键词", "先创建或采纳关键词，再运行监测")
        from app.routers.monitor import run_monitor
        result = run_monitor(MonitorRunRequest(brand_id=brand_id, keyword_ids=keyword_ids, platform=platform))
        return {"success": True, "action_type": action_type, "result": result}

    if action_type == "generate_content":
        content_type = str(payload.get("content_type") or "guide").strip()
        if content_type not in CONTENT_TYPES:
            content_type = "guide"
        target_platform = str(payload.get("target_platform") or payload.get("platform") or "website").strip()
        if target_platform not in PUBLISH_PLATFORMS:
            target_platform = "website"
        keyword_id = payload.get("keyword_id")
        keyword = str(payload.get("keyword") or payload.get("question") or "").strip()
        with get_db() as conn:
            cursor = conn.cursor()
            if not keyword_id:
                ids = _first_keyword_ids(cursor, brand_id, 1)
                keyword_id = ids[0] if ids else None
        result = generate_content(ContentGenerateRequest(
            brand_id=brand_id,
            keyword_id=int(keyword_id) if keyword_id else None,
            keyword=keyword,
            content_type=content_type,
            target_platform=target_platform,
        ))
        return {"success": True, "action_type": action_type, "result": result}

    if action_type == "approve_content":
        content_id = payload.get("content_id")
        if not content_id or not str(content_id).isdigit():
            return _strategy_execution_error("approve_content 缺少有效 content_id")
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM content_assets WHERE id = ? AND brand_id = ?", (int(content_id), brand_id))
            row = cursor.fetchone()
            if not row:
                return _strategy_execution_error("内容不存在，或不属于该品牌")
            content = dict(row)
            if content.get("status") == "published":
                return _strategy_execution_blocked("已发布内容不能重新审核", "请新建内容版本后再审核")
        result = approve_content(int(content_id))
        return {"success": True, "action_type": action_type, "result": result}

    if action_type == "create_publish_task":
        platform = str(payload.get("platform") or payload.get("target_platform") or "website").strip()
        if platform not in PUBLISH_PLATFORMS:
            return _strategy_execution_error(f"发布平台未接入: {platform}")
        content_id = payload.get("content_id")
        account_id = payload.get("account_id")
        with get_db() as conn:
            cursor = conn.cursor()
            if not content_id:
                content = _first_approved_content(cursor, brand_id, platform)
                if not content:
                    return _strategy_execution_blocked("没有审核通过且可发布的内容", "先到内容生产审核通过 AI 草稿，再创建发布任务")
                content_id = content["id"]
            if platform != "website" and not account_id:
                cursor.execute(
                    "SELECT id FROM publishing_accounts WHERE platform = ? ORDER BY updated_at DESC LIMIT 1",
                    (platform,),
                )
                row = cursor.fetchone()
                account_id = row["id"] if row else None
        result = create_publish_task(PublishTaskCreateRequest(
            content_id=int(content_id),
            platform=platform,
            account_id=int(account_id) if account_id else None,
            publish_mode="manual_confirm",
        ))
        return {"success": True, "action_type": action_type, "result": result}

    if action_type in {"prepare_rpa_draft", "open_publish_browser"}:
        task_id = payload.get("task_id")
        with get_db() as conn:
            cursor = conn.cursor()
            task = None
            if task_id:
                cursor.execute("SELECT * FROM publishing_tasks WHERE id = ? AND brand_id = ?", (task_id, brand_id))
                row = cursor.fetchone()
                task = dict(row) if row else None
            if not task:
                task = _first_publish_task(cursor, brand_id)
        if not task:
            return _strategy_execution_blocked("没有可准备的发布任务", "先创建发布任务，再准备 RPA 草稿")
        if action_type == "open_publish_browser":
            result = run_publish_task(int(task["id"]))
        else:
            result = prepare_publish_task(int(task["id"]))
        return {"success": True, "action_type": action_type, "result": result}

    if action_type == "run_recheck":
        task_id = payload.get("task_id")
        with get_db() as conn:
            cursor = conn.cursor()
            if not task_id:
                cursor.execute(
                    """SELECT id
                       FROM publishing_tasks
                       WHERE brand_id = ? AND status = 'published' AND published_url IS NOT NULL
                       ORDER BY updated_at DESC, id DESC
                       LIMIT 1""",
                    (brand_id,),
                )
                row = cursor.fetchone()
                task_id = row["id"] if row else None
        if not task_id:
            return _strategy_execution_blocked("没有已发布且带真实 URL 的发布任务可复测", "先完成发布并回填真实 URL，再做发布后复测")
        result = retest_publish_task(int(task_id))
        return {"success": True, "action_type": action_type, "result": result}

    if action_type == "generate_report":
        result = create_scorecard(brand_id=brand_id)
        return {"success": True, "action_type": action_type, "result": result}

    return _strategy_execution_error(f"暂未实现的白名单动作: {action_type}")


@router.post("/strategy/{strategy_id}/actions/execute")
def execute_geo_strategy_actions(strategy_id: int, request: StrategyExecuteActionsRequest = StrategyExecuteActionsRequest()):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM geo_strategies WHERE id = ?", (strategy_id,))
        strategy = cursor.fetchone()
        if not strategy:
            raise HTTPException(status_code=404, detail="Strategy not found")
        strategy = dict(strategy)
        where = "strategy_id = ? AND status = 'confirmed'"
        params: list = [strategy_id]
        if request.action_ids:
            placeholders = ",".join("?" for _ in request.action_ids)
            where += f" AND id IN ({placeholders})"
            params.extend(request.action_ids)
        cursor.execute(
            f"""SELECT *
                FROM geo_strategy_actions
                WHERE {where}
                ORDER BY id ASC""",
            params,
        )
        actions = [dict(row) for row in cursor.fetchall()]
    if request.action_ids and len(actions) != len(set(request.action_ids)):
        raise HTTPException(status_code=400, detail="只能执行已确认且属于该策略的执行项")
    if not actions:
        raise HTTPException(status_code=400, detail="没有已确认的执行项可执行")

    results = []
    for action in actions:
        result = _execute_strategy_action(action, dry_run=request.dry_run)
        status = "executed" if result.get("success") else ("blocked" if result.get("blocked") else "failed")
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """UPDATE geo_strategy_actions
                   SET status = ?, execution_result_json = ?, failure_reason = ?,
                       executed_at = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    status if not request.dry_run else action.get("status"),
                    json.dumps(result, ensure_ascii=False, default=str),
                    "" if result.get("success") else result.get("error", "执行失败"),
                    local_now() if not request.dry_run else action.get("executed_at"),
                    local_now(),
                    action["id"],
                ),
            )
        results.append({"action_id": action["id"], **result})
    failed_count = sum(1 for item in results if not item.get("success") and not item.get("blocked"))
    blocked_count = sum(1 for item in results if item.get("blocked"))
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE geo_strategies SET status = ?, updated_at = ? WHERE id = ?",
            ("executed" if all(item.get("success") for item in results) and not request.dry_run else strategy["status"], local_now(), strategy_id),
        )
        cursor.execute("SELECT * FROM geo_strategies WHERE id = ?", (strategy_id,))
        updated = _strategy_row(cursor, dict(cursor.fetchone()))
    return {
        "strategy": updated,
        "executed_count": sum(1 for item in results if item.get("success")),
        "blocked_count": blocked_count,
        "failed_count": failed_count,
        "dry_run": request.dry_run,
        "results": results,
    }


@router.get("/agent/capabilities")
def get_agent_capabilities():
    return {
        "agent_entrypoints": {
            "execute_action": "/api/operations/agent/execute",
            "execute_strategy": "/api/operations/agent/strategy/execute",
        },
        "allowed_action_types": sorted(STRATEGY_ACTION_TYPES),
        "publish_platforms": sorted(PUBLISH_PLATFORMS.keys()),
        "content_types": CONTENT_TYPES,
        "notes": [
            "这是 Hermes agent 的统一操作入口，不需要前端页面参与。",
            "发布仍遵循现有真实边界：系统准备草稿，人工最终确认，系统回捞URL与验真。",
            "dry_run=true 时只返回可执行判断与标准化 payload，不落库执行。",
        ],
    }


@router.post("/agent/execute")
def execute_agent_action(request: AgentActionRequest):
    run_id = _agent_run_log_start(
        request.agent_name,
        request.action,
        request.brand_id,
        request.dry_run,
        request.model_dump(),
    )
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            _brand(cursor, request.brand_id)
        action = {
            "action_type": request.action,
            "brand_id": request.brand_id,
            "payload_json": json.dumps(request.payload or {}, ensure_ascii=False),
        }
        result = _execute_strategy_action(action, dry_run=request.dry_run)
        status = "executed" if result.get("success") else ("blocked" if result.get("blocked") else "failed")
        response = {
            "run_id": run_id,
            "agent_name": request.agent_name,
            "brand_id": request.brand_id,
            "action": request.action,
            "dry_run": request.dry_run,
            "status": status,
            "result": result,
        }
        _agent_run_log_finish(run_id, status, response, "" if result.get("success") else result.get("error", "执行失败"))
        return response
    except HTTPException as exc:
        response = {
            "run_id": run_id,
            "agent_name": request.agent_name,
            "brand_id": request.brand_id,
            "action": request.action,
            "dry_run": request.dry_run,
            "status": "failed",
            "error": exc.detail,
        }
        _agent_run_log_finish(run_id, "failed", response, str(exc.detail))
        raise
    except Exception as exc:
        response = {
            "run_id": run_id,
            "agent_name": request.agent_name,
            "brand_id": request.brand_id,
            "action": request.action,
            "dry_run": request.dry_run,
            "status": "failed",
            "error": str(exc),
        }
        _agent_run_log_finish(run_id, "failed", response, str(exc))
        raise HTTPException(status_code=500, detail=f"Agent execute failed: {exc}")


@router.post("/agent/strategy/execute")
def execute_agent_strategy(request: AgentStrategyRequest):
    run_id = _agent_run_log_start(
        request.agent_name,
        "strategy_execute",
        None,
        request.dry_run,
        request.model_dump(),
    )
    try:
        response = execute_geo_strategy_actions(
            request.strategy_id,
            StrategyExecuteActionsRequest(action_ids=request.action_ids, dry_run=request.dry_run),
        )
        wrapped = {
            "run_id": run_id,
            "agent_name": request.agent_name,
            "strategy_id": request.strategy_id,
            "dry_run": request.dry_run,
            "status": "executed" if response.get("failed_count", 0) == 0 else "partial",
            **response,
        }
        _agent_run_log_finish(run_id, wrapped["status"], wrapped, "")
        return wrapped
    except HTTPException as exc:
        response = {
            "run_id": run_id,
            "agent_name": request.agent_name,
            "strategy_id": request.strategy_id,
            "dry_run": request.dry_run,
            "status": "failed",
            "error": exc.detail,
        }
        _agent_run_log_finish(run_id, "failed", response, str(exc.detail))
        raise
    except Exception as exc:
        response = {
            "run_id": run_id,
            "agent_name": request.agent_name,
            "strategy_id": request.strategy_id,
            "dry_run": request.dry_run,
            "status": "failed",
            "error": str(exc),
        }
        _agent_run_log_finish(run_id, "failed", response, str(exc))
        raise HTTPException(status_code=500, detail=f"Agent strategy execute failed: {exc}")


@router.post("/strategy/generate")
def generate_geo_strategy(request: StrategyGenerateRequest):
    with get_db() as conn:
        cursor = conn.cursor()
        brand = _brand(cursor, request.brand_id)
        _require_brand_assets(brand)
        context = _strategy_real_context(cursor, request.brand_id)
        strategy, actions, usage = _generate_strategy_llm(brand, context, request.objective or "")
        strategy, actions, strategy_issues = _sanitize_strategy_payload(strategy, actions, brand)
        strategy = _attach_knowledge_to_strategy(strategy, context.get("knowledge_context") or {})
        strategy, actions = _ensure_multiplatform_strategy_actions(strategy, actions, brand, _workflow_status_map(cursor))
        strategy_payload = {
            **strategy,
            "real_data_summary": {
                "keywords": len(context["keywords"]),
                "keyword_ideas": len(context["keyword_ideas"]),
                "monitor_results": len(context["monitor_results"]),
                "content_assets": len(context["content_assets"]),
                "publish_tasks": len(context["publish_tasks"]),
            },
            "constraints": {
                "allowed_action_types": sorted(STRATEGY_ACTION_TYPES),
                "no_auto_side_effects_in_p3": True,
                "postprocess_issues": strategy_issues[:20],
                "competitor_full_names_sanitized": True,
            },
        }
        cursor.execute(
            """INSERT INTO geo_strategies
               (brand_id, strategy_json, status, llm_provider, llm_model, llm_usage_id, updated_at)
               VALUES (?, ?, 'draft', ?, ?, ?, ?)""",
            (
                request.brand_id,
                json.dumps(strategy_payload, ensure_ascii=False),
                usage.get("provider"),
                usage.get("model"),
                usage.get("id"),
                local_now(),
            ),
        )
        strategy_id = cursor.lastrowid
        for action in actions:
            cursor.execute(
                """INSERT INTO geo_strategy_actions
                   (strategy_id, brand_id, action_type, title, description, payload_json,
                    requires_login, cost_risk, requires_confirmation, status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'pending', ?)""",
                (
                    strategy_id,
                    request.brand_id,
                    action["action_type"],
                    action["title"],
                    action["description"],
                    json.dumps(action["payload"], ensure_ascii=False),
                    1 if action["requires_login"] else 0,
                    action["cost_risk"],
                    local_now(),
                ),
            )
        cursor.execute("SELECT * FROM geo_strategies WHERE id = ?", (strategy_id,))
        item = _strategy_row(cursor, dict(cursor.fetchone()))
        return {
            **item,
            "source": "llm_generated_from_real_system_data",
            "llm_provider": usage.get("provider"),
            "llm_model": usage.get("model"),
            "llm_usage_id": usage.get("id"),
            "total_tokens": usage.get("total_tokens"),
            "cost": usage.get("cost"),
        }


@router.get("/strategy")
def list_geo_strategies(brand_id: int = Query(...), limit: int = 10):
    with get_db() as conn:
        cursor = conn.cursor()
        _brand(cursor, brand_id)
        cursor.execute(
            """SELECT *
               FROM geo_strategies
               WHERE brand_id = ?
               ORDER BY created_at DESC, id DESC
               LIMIT ?""",
            (brand_id, limit),
        )
        return [_strategy_row(cursor, dict(row)) for row in cursor.fetchall()]


@router.post("/strategy/{strategy_id}/actions/confirm")
def confirm_geo_strategy_actions(strategy_id: int, request: StrategyConfirmActionsRequest):
    if not request.action_ids:
        raise HTTPException(status_code=400, detail="action_ids is required")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM geo_strategies WHERE id = ?", (strategy_id,))
        strategy = cursor.fetchone()
        if not strategy:
            raise HTTPException(status_code=404, detail="Strategy not found")
        placeholders = ",".join("?" for _ in request.action_ids)
        cursor.execute(
            f"""SELECT id, action_type
                FROM geo_strategy_actions
                WHERE strategy_id = ? AND id IN ({placeholders})""",
            (strategy_id, *request.action_ids),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        if len(rows) != len(set(request.action_ids)):
            raise HTTPException(status_code=400, detail="存在不属于该策略的执行项")
        cursor.execute(
            "UPDATE geo_strategy_actions SET status = 'pending', updated_at = ? WHERE strategy_id = ?",
            (local_now(), strategy_id),
        )
        cursor.execute(
            f"""UPDATE geo_strategy_actions
                SET status = 'confirmed', updated_at = ?
                WHERE strategy_id = ? AND id IN ({placeholders})""",
            (local_now(), strategy_id, *request.action_ids),
        )
        cursor.execute(
            "UPDATE geo_strategies SET status = 'confirmed', updated_at = ? WHERE id = ?",
            (local_now(), strategy_id),
        )
        cursor.execute("SELECT * FROM geo_strategies WHERE id = ?", (strategy_id,))
        item = _strategy_row(cursor, dict(cursor.fetchone()))
        return {
            "strategy": item,
            "confirmed_count": len(rows),
            "side_effects_executed": False,
            "next_phase": "P4 AI 执行器会按白名单 API/脚本执行已确认动作",
        }


@router.post("/keywords/generate")
def generate_keyword_ideas(request: KeywordGenerateRequest):
    with get_db() as conn:
        cursor = conn.cursor()
        brand = _brand(cursor, request.brand_id)
        _require_brand_assets(brand)
        existing = _existing_keyword_words(cursor, request.brand_id)
        fallback_reason = ""
        try:
            candidates, usage = _generate_keyword_candidates_llm(brand, request.seed or "", request.count, existing)
            source = "llm_generated"
        except HTTPException as exc:
            # 大模型不可用（余额耗尽/断网/超时）时降级为规则引擎生成，
            # 避免 LLM 单点故障直接瘫痪运营中台；结果如实标注 source 供人工复核。
            fallback_reason = str(exc.detail)[:200]
            candidates = [item for item in _keyword_candidates(brand, request.seed or "", request.count) if item["keyword"] not in existing]
            usage = {}
            source = "rule_fallback"
        if not candidates:
            raise HTTPException(status_code=502, detail=fallback_reason or "关键词生成失败：无候选词")
        quality_ok, quality_reason, quality_stats = _keyword_batch_quality(candidates, brand)
        if not quality_ok:
            raise HTTPException(
                status_code=502,
                detail=f"关键词批次未通过 GEO 质量门禁，已拒绝写库：{quality_reason}；stats={quality_stats}",
            )
        batch_time = datetime.now().strftime("%Y%m%d%H%M%S")
        generation_batch = f"kw-{request.brand_id}-{batch_time}"
        seed_text = (request.seed or "").strip()
        batch_label = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} 关键词生产"
        if seed_text:
            batch_label = f"{batch_label}：{seed_text[:24]}"
        if source == "rule_fallback":
            batch_label = f"{batch_label}（规则降级：大模型不可用）"
        inserted = []
        for item in candidates:
            status = "duplicate" if item["keyword"] in existing else "pending"
            brand_mention_type, geo_value_type = _keyword_geo_labels(item["keyword"], brand)
            cursor.execute(
                """INSERT INTO keyword_ideas
                   (brand_id, keyword, category, intent, score, reason, source, status,
                    llm_provider, llm_model, llm_usage_id, generation_batch, batch_label,
                    seed_text, is_duplicate, brand_mention_type, geo_value_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    request.brand_id,
                    item["keyword"],
                    item["category"],
                    item["intent"],
                    item["score"],
                    item["reason"],
                    source,
                    status,
                    usage.get("provider"),
                    usage.get("model"),
                    usage.get("id"),
                    generation_batch,
                    batch_label,
                    seed_text,
                    1 if status == "duplicate" else 0,
                    brand_mention_type,
                    geo_value_type,
                ),
            )
            inserted.append({
                **item,
                "id": cursor.lastrowid,
                "status": status,
                "generation_batch": generation_batch,
                "batch_label": batch_label,
                "seed_text": seed_text,
                "is_duplicate": 1 if status == "duplicate" else 0,
                "brand_mention_type": brand_mention_type,
                "geo_value_type": geo_value_type,
            })
        return {
            "count": len(inserted),
            "items": inserted,
            "generation_batch": generation_batch,
            "batch_label": batch_label,
            "source": source,
            "fallback_reason": fallback_reason or None,
            "llm_provider": usage.get("provider"),
            "llm_model": usage.get("model"),
            "llm_usage_id": usage.get("id"),
            "total_tokens": usage.get("total_tokens"),
            "cost": usage.get("cost"),
        }


@router.get("/brands")
def list_operation_brands():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT b.*,
                      (SELECT COUNT(*) FROM keyword_ideas WHERE brand_id = b.id AND status IN ('pending', 'adopted')) AS keywordIdeaCount,
                      (SELECT COUNT(*) FROM content_assets WHERE brand_id = b.id AND status != 'rejected') AS contentAssetCount,
                      (SELECT COUNT(*) FROM publishing_tasks WHERE brand_id = b.id) AS publishingTaskCount,
                      (SELECT COUNT(*) FROM geo_reports WHERE brand_id = b.id) AS reportCount,
                      (SELECT COUNT(*) FROM keywords WHERE brand_id = b.id) AS keywordCount,
                      (SELECT COUNT(*) FROM monitor_results WHERE brand_id = b.id) AS totalMonitorCount,
                      ((SELECT COUNT(*) FROM keyword_ideas WHERE brand_id = b.id AND status IN ('pending', 'adopted')) +
                       (SELECT COUNT(*) FROM content_assets WHERE brand_id = b.id AND status != 'rejected') +
                       (SELECT COUNT(*) FROM publishing_tasks WHERE brand_id = b.id) +
                       (SELECT COUNT(*) FROM geo_reports WHERE brand_id = b.id)) AS operationScore
               FROM brands b
               ORDER BY operationScore DESC, totalMonitorCount DESC, b.created_at DESC"""
        )
        return [dict(row) for row in cursor.fetchall()]


@router.get("/keywords/ideas")
def list_keyword_ideas(
    brand_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    batch: Optional[str] = Query(None),
    geo_value_type: Optional[str] = Query(None),
    brand_mention_type: Optional[str] = Query(None),
    latest_batch: bool = False,
    limit: int = 100,
):
    with get_db() as conn:
        cursor = conn.cursor()
        where, params = [], []
        if brand_id:
            where.append("ki.brand_id = ?")
            params.append(brand_id)
        if status:
            where.append("ki.status = ?")
            params.append(status)
        if latest_batch:
            batch_sql = "SELECT generation_batch FROM keyword_ideas WHERE generation_batch IS NOT NULL"
            batch_params = []
            if brand_id:
                batch_sql += " AND brand_id = ?"
                batch_params.append(brand_id)
            batch_sql += " ORDER BY id DESC LIMIT 1"
            row = cursor.execute(batch_sql, batch_params).fetchone()
            batch = row["generation_batch"] if row else "__none__"
        if batch:
            where.append("ki.generation_batch = ?")
            params.append(batch)
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        cursor.execute(
            f"""SELECT ki.*, b.name AS brand_name
                FROM keyword_ideas ki
                LEFT JOIN brands b ON b.id = ki.brand_id
                {where_sql}
                ORDER BY ki.score DESC, ki.created_at DESC
                LIMIT ?""",
            params + [min(max(limit * 5, limit), 1000) if (geo_value_type or brand_mention_type) else limit],
        )
        rows = [dict(row) for row in cursor.fetchall()]
        brand_cache = {}
        for row in rows:
            bid = row.get("brand_id")
            if bid not in brand_cache:
                brand_cache[bid] = _brand(cursor, bid)
            computed_brand_mention_type, computed_geo_value_type = _keyword_geo_labels(row.get("keyword") or "", brand_cache[bid])
            if row.get("brand_mention_type") != computed_brand_mention_type or row.get("geo_value_type") != computed_geo_value_type:
                row["brand_mention_type"] = computed_brand_mention_type
                row["geo_value_type"] = computed_geo_value_type
                try:
                    cursor.execute(
                        """UPDATE keyword_ideas
                           SET brand_mention_type = ?,
                               geo_value_type = ?
                           WHERE id = ?""",
                        (computed_brand_mention_type, computed_geo_value_type, row["id"]),
                    )
                except Exception:
                    pass
        if geo_value_type:
            rows = [row for row in rows if row.get("geo_value_type") == geo_value_type]
        if brand_mention_type:
            rows = [row for row in rows if row.get("brand_mention_type") == brand_mention_type]
        return rows[:limit]


@router.get("/keywords/idea-batches")
def list_keyword_idea_batches(brand_id: Optional[int] = Query(None), limit: int = 50):
    with get_db() as conn:
        cursor = conn.cursor()
        where, params = ["generation_batch IS NOT NULL"], []
        if brand_id:
            where.append("brand_id = ?")
            params.append(brand_id)
        cursor.execute(
            f"""SELECT generation_batch, COALESCE(batch_label, generation_batch) AS batch_label,
                       COALESCE(seed_text, '') AS seed_text,
                       MAX(created_at) AS created_at,
                       COUNT(*) AS total_count,
                       SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                       SUM(CASE WHEN status = 'adopted' THEN 1 ELSE 0 END) AS adopted_count,
                       SUM(CASE WHEN status = 'duplicate' OR COALESCE(is_duplicate,0)=1 THEN 1 ELSE 0 END) AS duplicate_count
                FROM keyword_ideas
                WHERE {" AND ".join(where)}
                GROUP BY generation_batch, batch_label, seed_text
                ORDER BY MAX(id) DESC
                LIMIT ?""",
            params + [limit],
        )
        return [dict(row) for row in cursor.fetchall()]


@router.post("/keywords/adopt")
def adopt_keyword_ideas(request: AdoptIdeasRequest):
    if not request.idea_ids:
        raise HTTPException(status_code=400, detail="No idea ids provided")
    with get_db() as conn:
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in request.idea_ids)
        cursor.execute(f"SELECT * FROM keyword_ideas WHERE id IN ({placeholders})", request.idea_ids)
        ideas = [dict(row) for row in cursor.fetchall()]
        created = []
        for idea in ideas:
            if idea.get("status") != "pending":
                continue
            cursor.execute(
                "SELECT id FROM keywords WHERE brand_id = ? AND word = ?",
                (idea["brand_id"], idea["keyword"]),
            )
            row = cursor.fetchone()
            if row:
                keyword_id = row["id"]
            else:
                cursor.execute(
                    """INSERT INTO keywords
                       (brand_id, word, category, search_volume, ai_status, notes)
                       VALUES (?, ?, ?, ?, 'pending', ?)""",
                    (idea["brand_id"], idea["keyword"], idea["category"], idea["score"], idea["reason"]),
                )
                keyword_id = cursor.lastrowid
            cursor.execute(
                "UPDATE keyword_ideas SET status = 'adopted', created_keyword_id = ? WHERE id = ?",
                (keyword_id, idea["id"]),
            )
            created.append({"idea_id": idea["id"], "keyword_id": keyword_id, "keyword": idea["keyword"]})
        if not created:
            raise HTTPException(status_code=400, detail="No pending keyword ideas can be adopted")
        return {"count": len(created), "items": created}


@router.post("/content/generate")
def generate_content(request: ContentGenerateRequest):
    if request.content_type not in CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Invalid content_type")
    if request.target_platform not in PUBLISH_PLATFORMS:
        raise HTTPException(status_code=400, detail="Invalid target_platform")
    with get_db() as conn:
        cursor = conn.cursor()
        brand = _brand(cursor, request.brand_id)
        keyword_id, keyword, _ = _keyword_context(cursor, request.brand_id, request.keyword_id, request.keyword)
        try:
            title, body, quality, notes, usage = _render_content_llm(brand, keyword, request.content_type, request.target_platform)
            content_source = "llm_generated"
        except HTTPException as exc:
            # 大模型不可用时降级为结构化模板（_render_content 内部自带模板兜底），
            # notes 里明确标注 template_fallback，提醒人工强化润色后再发布。
            title, body, quality, notes, usage = _render_content(brand, keyword, request.content_type, request.target_platform)
            notes = f"{notes}；降级原因：{str(exc.detail)[:120]}"
            content_source = "template_fallback"
        cursor.execute(
            """INSERT INTO content_assets
               (brand_id, keyword_id, title, content_type, target_platform, target_keyword, body,
                quality_score, generation_notes, llm_provider, llm_model, llm_usage_id, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request.brand_id,
                keyword_id,
                title,
                request.content_type,
                request.target_platform,
                keyword,
                body,
                quality,
                notes,
                usage.get("provider"),
                usage.get("model"),
                usage.get("id"),
                content_source,
            ),
        )
        content_id = cursor.lastrowid
        return {
            "id": content_id,
            "title": title,
            "body": body,
            "quality_score": quality,
            "notes": notes,
            "source": content_source,
            "llm_provider": usage.get("provider"),
            "llm_model": usage.get("model"),
            "llm_usage_id": usage.get("id"),
            "total_tokens": usage.get("total_tokens"),
            "cost": usage.get("cost"),
        }


@router.get("/content")
def list_content(
    brand_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    content_type: Optional[str] = Query(None),
    target_platform: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = 100,
):
    with get_db() as conn:
        cursor = conn.cursor()
        where, params = [], []
        if brand_id:
            where.append("ca.brand_id = ?")
            params.append(brand_id)
        if status:
            where.append("ca.status = ?")
            params.append(status)
        if keyword:
            where.append("(ca.title LIKE ? OR ca.target_keyword LIKE ? OR k.word LIKE ?)")
            kw = f"%{keyword}%"
            params.extend([kw, kw, kw])
        if content_type:
            where.append("ca.content_type = ?")
            params.append(content_type)
        if target_platform:
            where.append("ca.target_platform = ?")
            params.append(target_platform)
        if source:
            where.append("ca.source = ?")
            params.append(source)
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        cursor.execute(
            f"""SELECT ca.*, b.name AS brand_name, k.word AS keyword_word
                FROM content_assets ca
                LEFT JOIN brands b ON b.id = ca.brand_id
                LEFT JOIN keywords k ON k.id = ca.keyword_id
                {where_sql}
                ORDER BY ca.created_at DESC
                LIMIT ?""",
            params + [limit],
        )
        return [dict(row) for row in cursor.fetchall()]


@router.put("/content/{content_id}")
def update_content(content_id: int, request: ContentUpdateRequest):
    allowed_status = {"draft", "approved", "queued", "publishing", "published", "rejected"}
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM content_assets WHERE id = ?", (content_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Content not found")
        current = dict(row)
        candidate = dict(current)
        for field in ["title", "body", "generation_notes"]:
            value = getattr(request, field)
            if value is not None:
                candidate[field] = value
        candidate_issues = _content_quality_issues(candidate)
        blocking_issues = [issue for issue in candidate_issues if "URL" in issue or "链接" in issue]
        if blocking_issues:
            raise HTTPException(status_code=400, detail=f"内容保存失败：{'；'.join(blocking_issues)}")
        updates, params = [], []
        for field in ["title", "body", "generation_notes"]:
            value = getattr(request, field)
            if value is not None:
                updates.append(f"{field} = ?")
                params.append(value)
        if request.status is not None:
            if request.status not in allowed_status:
                raise HTTPException(status_code=400, detail="Invalid content status")
            updates.append("status = ?")
            params.append(request.status)
        if request.target_platform is not None and request.target_platform != current.get("target_platform"):
            if current.get("status") in ("publishing", "published"):
                raise HTTPException(status_code=400, detail="内容已进入发布流程，发布渠道不可再修改")
            if request.target_platform not in PUBLISH_PLATFORMS:
                raise HTTPException(status_code=400, detail="Invalid target_platform")
            active_task = cursor.execute(
                """SELECT id, status FROM publishing_tasks
                   WHERE content_id = ? AND status NOT IN ('published', 'failed', 'cancelled')""",
                (content_id,),
            ).fetchone()
            if active_task:
                raise HTTPException(
                    status_code=400,
                    detail=f"该内容已有进行中的发布任务 #{dict(active_task)['id']}（{dict(active_task)['status']}），请先处理该任务再改渠道",
                )
            updates.append("target_platform = ?")
            params.append(request.target_platform)
        if updates:
            updates.append("updated_at = ?")
            params.append(local_now())
            params.append(content_id)
            cursor.execute(f"UPDATE content_assets SET {', '.join(updates)} WHERE id = ?", params)
        cursor.execute("SELECT * FROM content_assets WHERE id = ?", (content_id,))
        return dict(cursor.fetchone())


@router.post("/content/{content_id}/rewrite")
def rewrite_content(content_id: int, request: ContentRewriteRequest):
    mode = (request.mode or "rewrite").strip()
    if mode not in {"rewrite", "fix_gate", "regenerate"}:
        raise HTTPException(status_code=400, detail="Invalid rewrite mode")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT ca.*, k.word AS keyword_word
               FROM content_assets ca
               LEFT JOIN keywords k ON k.id = ca.keyword_id
               WHERE ca.id = ?""",
            (content_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Content not found")
        content = dict(row)
        if content.get("status") == "published":
            raise HTTPException(status_code=400, detail="已发布内容不能重写；请新建内容版本")
        cursor.execute(
            """SELECT COUNT(*) AS count
               FROM publishing_tasks
               WHERE content_id = ? AND status = 'published' AND COALESCE(published_url_verified,0)=1""",
            (content_id,),
        )
        if cursor.fetchone()["count"]:
            raise HTTPException(status_code=400, detail="内容已有已验真发布记录，不能覆盖重写")
        brand = _brand(cursor, content["brand_id"])
        issues = _content_quality_issues(content) + _platform_content_rules(content)
    title, body, quality, notes, usage = _rewrite_content_llm(
        brand,
        content,
        mode,
        request.direction or "",
        issues,
    )
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE content_assets
               SET title = ?, body = ?, quality_score = ?, generation_notes = ?,
                   llm_provider = ?, llm_model = ?, llm_usage_id = ?,
                   status = CASE WHEN status IN ('queued','publishing') THEN 'draft' ELSE status END,
                   updated_at = ?
               WHERE id = ?""",
            (
                title,
                body,
                quality,
                notes,
                usage.get("provider"),
                usage.get("model"),
                usage.get("id"),
                local_now(),
                content_id,
            ),
        )
        cursor.execute(
            """UPDATE publishing_tasks
               SET status = 'failed',
                   failure_reason = '内容已被AI重写，原发布任务失效，请重新创建发布任务',
                   evidence_status = 'unverified',
                   updated_at = ?
               WHERE content_id = ?
                 AND status IN ('pending','assets_ready','prepared','manual_required','login_required','running','failed')""",
            (local_now(), content_id),
        )
        cursor.execute("SELECT * FROM content_assets WHERE id = ?", (content_id,))
        updated = dict(cursor.fetchone())
    return {
        **updated,
        "source": f"llm_{mode}",
        "llm_provider": usage.get("provider"),
        "llm_model": usage.get("model"),
        "llm_usage_id": usage.get("id"),
        "total_tokens": usage.get("total_tokens"),
        "cost": usage.get("cost"),
    }


@router.post("/content/{content_id}/approve")
def approve_content(content_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM content_assets WHERE id = ?", (content_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Content not found")
        content = dict(row)
        _require_content_quality(content)
        cursor.execute(
            "UPDATE content_assets SET status = 'approved', updated_at = ? WHERE id = ?",
            (local_now(), content_id),
        )
        cursor.execute("SELECT * FROM content_assets WHERE id = ?", (content_id,))
        return dict(cursor.fetchone())


@router.get("/content/{content_id}/quality-check")
def content_quality_check(content_id: int, ai: bool = Query(True)):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT ca.*, b.name AS brand_name
               FROM content_assets ca
               LEFT JOIN brands b ON b.id = ca.brand_id
               WHERE ca.id = ?""",
            (content_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Content not found")
        content = dict(row)
        brand = _brand(cursor, content["brand_id"])
        basic_issues = _content_quality_issues(content)
        platform_issues = _platform_content_rules(content)
        fact_issues = _brand_fact_rules(content, brand)
    if ai:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_ai_review_content, content, brand)
            try:
                ai_review = future.result(timeout=35)
            except Exception:
                ai_review = {
                    "passed": False,
                    "score": 0,
                    "risks": ["AI复审超过35秒未完成，已转人工复审，不阻断本地硬门禁"],
                    "suggestions": ["请人工复核平台规则、事实依据和发布风险"],
                    "model": None,
                    "usage_id": None,
                    "manual_review_required": True,
                }
    else:
        ai_review = {
            "passed": False,
            "score": 0,
            "risks": [],
            "suggestions": ["本次门禁跳过 AI 复审，仅验证本地硬规则"],
            "model": None,
            "usage_id": None,
            "manual_review_required": True,
        }
    issues = basic_issues + platform_issues + fact_issues + ai_review.get("risks", [])
    hard_issues = basic_issues + platform_issues
    sections = [
        {
            "name": "基础规则",
            "passed": not basic_issues,
            "issues": basic_issues,
            "mechanism": "本地规则：长度、emoji、模板词、URL、素材文件、数量和大小",
        },
        {
            "name": "平台规则",
            "passed": not platform_issues,
            "issues": platform_issues,
            "mechanism": f"按 {content.get('target_platform') or 'website'} 的标题长度、素材、营销词、分段要求检查",
        },
        {
            "name": "事实依据",
            "passed": not fact_issues,
            "issues": fact_issues,
            "mechanism": "基于品牌资料、产品/服务、卖点、信源字段做一致性检查；不伪装外部实时核验",
        },
        {
            "name": "AI复审",
            "passed": ai_review.get("passed") is True,
            "issues": ai_review.get("risks") or [],
            "suggestions": ai_review.get("suggestions") or [],
            "mechanism": "调用当前已连接大模型做发布风险、GEO价值、内容质量复审",
            "model": ai_review.get("model"),
            "usage_id": ai_review.get("usage_id"),
            "manual_review_required": ai_review.get("manual_review_required"),
        },
    ]
    return {
        "passed": not hard_issues,
        "hard_passed": not hard_issues,
        "ai_review_passed": ai_review.get("passed") is True,
        "issues": issues,
        "blocking_issues": hard_issues,
        "sections": sections,
        "mechanism": "四段式质检：基础规则 + 平台规则 + 品牌事实依据 + AI复审；事实依据不通过时需人工补证，AI失败不伪装通过",
        "media_assets": _content_media_assets(content),
    }


@router.post("/content/{content_id}/media")
async def upload_content_media(content_id: int, files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM content_assets WHERE id = ?", (content_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Content not found")
        content = dict(row)
        existing = _content_media_assets(content)
        image_count = len([item for item in existing if item.get("kind") == "image"])
        video_count = len([item for item in existing if item.get("kind") == "video"])
        saved = []
        for file in files:
            original = Path(file.filename or "").name
            suffix = Path(original).suffix.lower()
            if suffix in IMAGE_EXTENSIONS:
                kind = "image"
                limit = MAX_IMAGE_BYTES
                if image_count >= MAX_PUBLISH_IMAGES:
                    raise HTTPException(status_code=400, detail=f"图片最多上传 {MAX_PUBLISH_IMAGES} 张")
                image_count += 1
            elif suffix in VIDEO_EXTENSIONS:
                kind = "video"
                limit = MAX_VIDEO_BYTES
                if video_count >= MAX_PUBLISH_VIDEOS:
                    raise HTTPException(status_code=400, detail=f"视频最多上传 {MAX_PUBLISH_VIDEOS} 个")
                video_count += 1
            else:
                raise HTTPException(status_code=400, detail=f"不支持的素材格式：{suffix or original}")
            data = await file.read()
            if not data:
                raise HTTPException(status_code=400, detail=f"空文件：{original}")
            if len(data) > limit:
                raise HTTPException(status_code=400, detail=f"{original} 超过大小上限")
            if kind == "image":
                try:
                    from PIL import Image
                    import io

                    with Image.open(io.BytesIO(data)) as image:
                        width, height = image.size
                    if width < 400 or height < 300:
                        raise HTTPException(status_code=400, detail=f"{original} 图片尺寸过小，至少 400x300")
                except HTTPException:
                    raise
                except Exception:
                    raise HTTPException(status_code=400, detail=f"{original} 不是可解析图片")
            safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", original)[:120] or f"asset{suffix}"
            target = _media_dir(content["brand_id"], content_id) / f"{datetime.now().strftime('%H%M%S_%f')}_{safe_name}"
            target.write_bytes(data)
            item = {
                "kind": kind,
                "filename": original,
                "path": str(target),
                "size": len(data),
                "uploaded_at": local_now(),
            }
            if kind == "image":
                item["width"] = width
                item["height"] = height
            saved.append(item)
            existing.append(item)
        cursor.execute(
            "UPDATE content_assets SET media_assets_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(existing, ensure_ascii=False), local_now(), content_id),
        )
        return {"content_id": content_id, "saved": saved, "media_assets": existing}


@router.delete("/content/{content_id}/media")
def clear_content_media(content_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM content_assets WHERE id = ?", (content_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Content not found")
        content = dict(row)
        removed = _delete_content_media_files(content)
        cursor.execute("UPDATE content_assets SET media_assets_json = NULL, updated_at = ? WHERE id = ?", (local_now(), content_id))
        return {"content_id": content_id, "removed": removed}


@router.delete("/content/{content_id}")
def delete_content(content_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM content_assets WHERE id = ?", (content_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Content not found")
        content = dict(row)
        cursor.execute(
            """SELECT id, status, published_url_verified
               FROM publishing_tasks
               WHERE content_id = ?
               ORDER BY id DESC""",
            (content_id,),
        )
        tasks = [dict(item) for item in cursor.fetchall()]
        verified_tasks = [
            item for item in tasks
            if item.get("status") == "published" and int(item.get("published_url_verified") or 0) == 1
        ]
        if verified_tasks:
            ids = "、".join(str(item["id"]) for item in verified_tasks)
            raise HTTPException(status_code=400, detail=f"内容已有已验真发布任务 {ids}，不能删除；请保留交付记录")
        removed_media = _delete_content_media_files(content)
        cursor.execute("DELETE FROM publishing_tasks WHERE content_id = ?", (content_id,))
        deleted_tasks = cursor.rowcount
        cursor.execute("DELETE FROM content_assets WHERE id = ?", (content_id,))
        return {"deleted": content_id, "deleted_publish_tasks": deleted_tasks, "removed_media": removed_media}


@router.get("/publish/workflows")
def list_publish_workflows():
    with get_db() as conn:
        cursor = conn.cursor()
        workflows = []
        for platform in PUBLISH_PLATFORMS:
            item = _workflow(cursor, platform)
            item["capability"] = PUBLISH_CAPABILITIES.get(platform, {"level": "unknown", "commercial_ready": False})
            workflows.append(item)
        return workflows


@router.get("/publish/ecosystems")
def list_publish_ecosystems(brand_id: Optional[int] = Query(None)):
    with get_db() as conn:
        cursor = conn.cursor()
        if brand_id:
            _brand(cursor, brand_id)
        return _ecosystem_source_analysis(cursor, brand_id)


@router.get("/knowledge")
def get_knowledge_base_summary():
    return knowledge_base_summary()


@router.get("/knowledge/{resource}")
def list_knowledge_base_records(
    resource: str,
    brand_id: Optional[int] = Query(None),
    title: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
):
    try:
        return list_knowledge_records(resource, {"brand_id": brand_id, "title": title, "status": status})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/knowledge/{resource}")
def create_knowledge_base_record(resource: str, request: KnowledgeRecordRequest):
    try:
        payload = request.model_dump(exclude_none=True)
        return append_knowledge_record(resource, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/publish/platforms/recommend")
def recommend_publish_platforms_endpoint(request: PlatformRecommendRequest):
    with get_db() as conn:
        cursor = conn.cursor()
        if request.brand_id:
            brand = _brand(cursor, request.brand_id)
        else:
            brand = {
                "name": request.name or "",
                "industry": request.industry or "",
                "products": request.products or "",
                "target_audience": request.target_audience or "",
                "objective": request.objective or "",
            }
        return recommend_publish_platforms(brand, workflow_statuses=_workflow_status_map(cursor))


@router.put("/publish/workflows/{platform}")
def update_publish_workflow(platform: str, request: WorkflowUpdateRequest):
    if platform not in PUBLISH_PLATFORMS:
        raise HTTPException(status_code=400, detail="Invalid platform")
    login_url = _valid_optional_url(request.login_url, "login_url") if request.login_url is not None else None
    publish_url = _valid_optional_url(request.publish_url, "publish_url") if request.publish_url is not None else None
    if request.status and request.status not in {"template", "tested", "active", "disabled", "draft"}:
        raise HTTPException(status_code=400, detail="Invalid workflow status")
    if request.status in {"tested", "active"}:
        raise HTTPException(status_code=400, detail="tested/active 只能由真实工作流测试产生，不能手动设置")
    workflow_json = _validate_workflow_json(platform, request.workflow_json)
    with get_db() as conn:
        cursor = conn.cursor()
        current_workflow = _workflow(cursor, platform)
        updates, params = [], []
        for field, value in [
            ("platform_name", request.platform_name),
            ("login_url", login_url),
            ("publish_url", publish_url),
            ("status", request.status),
            ("notes", request.notes),
        ]:
            if value is not None:
                updates.append(f"{field} = ?")
                params.append(value)
        workflow_changed = False
        if workflow_json is not None:
            try:
                current_payload = json.loads(current_workflow.get("workflow_json") or "{}")
            except Exception:
                current_payload = {}
            workflow_changed = current_payload != workflow_json
            updates.append("workflow_json = ?")
            params.append(json.dumps(workflow_json, ensure_ascii=False))
        if workflow_changed and current_workflow.get("status") in {"tested", "active"}:
            updates.append("status = ?")
            params.append("draft")
            updates.append("last_tested_at = ?")
            params.append(None)
        if updates:
            updates.append("updated_at = ?")
            params.append(local_now())
            params.append(platform)
            cursor.execute(f"UPDATE publishing_workflows SET {', '.join(updates)} WHERE platform = ?", params)
        if login_url is not None or publish_url is not None:
            cursor.execute(
                """UPDATE publishing_accounts
                   SET login_url = COALESCE(NULLIF(?, ''), login_url),
                       publish_url = COALESCE(NULLIF(?, ''), publish_url),
                       updated_at = ?
                   WHERE platform = ?""",
                (login_url or "", publish_url or "", local_now(), platform),
            )
        return _workflow(cursor, platform)


@router.post("/publish/workflows/{platform}/test")
def test_workflow(platform: str):
    if platform not in PUBLISH_PLATFORMS:
        raise HTTPException(status_code=400, detail="Invalid platform")
    with get_db() as conn:
        cursor = conn.cursor()
        workflow = _workflow(cursor, platform)
        # 工作流测试要在「已绑定且已打开」的发布浏览器上跑：端口按 profile
        # 归属校验，绑定账号若为自定义名（如 搜狐号/小红书号），用默认
        # workflow_default profile 会被误判为端口冲突（2026-10-12 实测）。
        cursor.execute(
            "SELECT account_name FROM publishing_accounts WHERE platform = ? ORDER BY updated_at DESC LIMIT 1",
            (platform,),
        )
        bound = cursor.fetchone()
    account_name = (bound["account_name"] if bound else "") or "workflow_default"
    result = _run_publish_workflow_test_subprocess(platform, workflow, account_name=account_name)
    evidence = _validate_publish_evidence(
        screenshot_path=result.screenshot_path,
        raw_artifact_path=result.raw_artifact_path,
        live_url_check=False,
    )
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO publishing_workflow_tests
               (platform, status, tested_url, screenshot_path, screenshot_verified,
                raw_artifact_path, raw_artifact_verified, evidence_status,
                failure_reason, notes, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                platform,
                result.status,
                workflow.get("publish_url") or "",
                result.screenshot_path,
                evidence["screenshot_verified"],
                result.raw_artifact_path,
                evidence["raw_artifact_verified"],
                evidence["evidence_status"],
                result.failure_reason,
                result.notes,
                local_now(),
            ),
        )
        test_id = cursor.lastrowid
        workflow_status = "tested" if result.status == "prepared" else "draft"
        cursor.execute(
            """UPDATE publishing_workflows
               SET status = ?, last_tested_at = ?, updated_at = ?,
                   notes = ?
               WHERE platform = ?""",
            (
                workflow_status,
                local_now(),
                local_now(),
                result.notes or result.failure_reason or workflow.get("notes"),
                platform,
            ),
        )
        cursor.execute("SELECT * FROM publishing_workflow_tests WHERE id = ?", (test_id,))
        row = dict(cursor.fetchone())
    return row


def _run_publish_workflow_test_subprocess(platform: str, workflow: dict, account_name: str = "workflow_default") -> PublishRpaResult:
    script = Path(__file__).resolve().parents[3] / "scripts" / "run_publish_workflow_test.py"
    cmd = [
        sys.executable,
        str(script),
        "--platform",
        platform,
        "--publish-url",
        workflow.get("publish_url") or "",
        "--login-url",
        workflow.get("login_url") or "",
        "--workflow-json",
        json.dumps(workflow.get("workflow") or {}, ensure_ascii=False),
        "--account-name",
        account_name or "workflow_default",
    ]
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(Path(__file__).resolve().parents[3]),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # 抖音图文流程含上传/封面/自主声明/保存权限/发布时间多步等待，
            # 冷加载 45 秒不够（2026-10-12 实测超时但页面已填好）；与任务
            # 执行子进程统一 120 秒。
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return PublishRpaResult(
            status="failed",
            failure_reason="发布工作流测试超时，页面加载或弹框阻塞",
            notes="请确认发布页已打开、关闭弹框后重试",
        )
    if completed.returncode != 0:
        return PublishRpaResult(
            status="failed",
            failure_reason=(completed.stderr or completed.stdout or "发布工作流测试子进程失败")[:500],
        )
    lines = [line for line in (completed.stdout or "").splitlines() if line.strip().startswith("{")]
    if not lines:
        return PublishRpaResult(
            status="failed",
            failure_reason=(completed.stderr or completed.stdout or "发布工作流测试没有返回 JSON")[:500],
        )
    try:
        payload = json.loads(lines[-1])
    except Exception as exc:
        return PublishRpaResult(status="failed", failure_reason=f"发布工作流测试 JSON 解析失败: {exc}")
    return PublishRpaResult(**payload)


@router.post("/publish/workflows/{platform}/open-login")
def open_workflow_login(platform: str):
    if platform not in PUBLISH_PLATFORMS:
        raise HTTPException(status_code=400, detail="Invalid platform")
    with get_db() as conn:
        cursor = conn.cursor()
        workflow = _workflow(cursor, platform)
    login_url = workflow.get("login_url") or workflow.get("publish_url") or ""
    if platform != "website" and not login_url:
        raise HTTPException(status_code=400, detail="登录URL为空，先在发布平台规划中填写")
    result = open_publish_browser(platform, "workflow_default", login_url)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("notes") or "打开发布登录浏览器失败")
    return result


@router.post("/publish/workflows/{platform}/close-browser")
def close_workflow_browser(platform: str):
    if platform not in PUBLISH_PLATFORMS:
        raise HTTPException(status_code=400, detail="Invalid platform")
    return close_publish_browser(platform)


@router.post("/publish/workflows/{platform}/open-publish")
def open_workflow_publish(platform: str):
    if platform not in PUBLISH_PLATFORMS:
        raise HTTPException(status_code=400, detail="Invalid platform")
    with get_db() as conn:
        cursor = conn.cursor()
        workflow = _workflow(cursor, platform)
    publish_url = workflow.get("publish_url") or workflow.get("login_url") or ""
    if platform != "website" and not publish_url:
        raise HTTPException(status_code=400, detail="固定发布URL为空，先在发布平台规划中填写")
    result = open_publish_browser(platform, "workflow_default", publish_url)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("notes") or "打开固定发布页失败")
    return result


@router.post("/publish/workflows/{platform}/discover-selectors")
def discover_workflow_selectors(platform: str, request: SelectorDiscoverRequest = SelectorDiscoverRequest()):
    if platform not in PUBLISH_PLATFORMS:
        raise HTTPException(status_code=400, detail="Invalid platform")
    if platform == "website":
        raise HTTPException(status_code=400, detail="官网/落地页不需要外部发布页选择器")
    with get_db() as conn:
        cursor = conn.cursor()
        workflow = _workflow(cursor, platform)
    publish_url = workflow.get("publish_url") or workflow.get("login_url") or ""
    if not publish_url:
        raise HTTPException(status_code=400, detail="固定发布URL为空，先在发布平台规划中填写")
    result = discover_publish_selectors(platform, publish_url, workflow.get("login_url") or "")
    selectors = result.get("selectors") or {}
    if request.save and selectors.get("title") and selectors.get("body"):
        workflow_json = workflow.get("workflow") or {}
        workflow_json["selectors"] = {
            **(workflow_json.get("selectors") or {}),
            "title": selectors["title"],
            "body": selectors["body"],
        }
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """UPDATE publishing_workflows
                   SET workflow_json = ?, updated_at = ?, notes = ?
                   WHERE platform = ?""",
                (
                    json.dumps(workflow_json, ensure_ascii=False),
                    local_now(),
                    "已识别并保存发布页 title/body 候选选择器；仍需执行工作流测试验真",
                    platform,
                ),
            )
        result["saved"] = True
    else:
        result["saved"] = False
    return result


@router.get("/publish/workflows/{platform}/tests")
def list_workflow_tests(platform: str, limit: int = Query(20)):
    if platform not in PUBLISH_PLATFORMS:
        raise HTTPException(status_code=400, detail="Invalid platform")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT * FROM publishing_workflow_tests
               WHERE platform = ?
               ORDER BY created_at DESC, id DESC
               LIMIT ?""",
            (platform, limit),
        )
        items = []
        for row in cursor.fetchall():
            item = dict(row)
            if item.get("status") == "prepared":
                screenshot_path = item.get("screenshot_path")
                raw_artifact_path = item.get("raw_artifact_path")
                screenshot_ok = bool(screenshot_path and Path(str(screenshot_path)).exists() and Path(str(screenshot_path)).stat().st_size > 128)
                raw_ok = bool(raw_artifact_path and Path(str(raw_artifact_path)).exists() and Path(str(raw_artifact_path)).stat().st_size > 128)
                if screenshot_ok != bool(item.get("screenshot_verified")) or raw_ok != bool(item.get("raw_artifact_verified")):
                    cursor.execute(
                        """UPDATE publishing_workflow_tests
                           SET screenshot_verified = ?, raw_artifact_verified = ?, evidence_status = ?, updated_at = ?
                           WHERE id = ?""",
                        (
                            1 if screenshot_ok else 0,
                            1 if raw_ok else 0,
                            "verified" if (screenshot_ok or raw_ok) else "unverified",
                            local_now(),
                            item["id"],
                        ),
                    )
                    item["screenshot_verified"] = 1 if screenshot_ok else 0
                    item["raw_artifact_verified"] = 1 if raw_ok else 0
                    item["evidence_status"] = "verified" if (screenshot_ok or raw_ok) else "unverified"
            items.append(item)
        return items


@router.post("/accounts")
def create_account(request: AccountCreateRequest):
    if request.platform not in PUBLISH_PLATFORMS:
        raise HTTPException(status_code=400, detail="Invalid platform")
    account_name = request.account_name.strip()
    if not account_name:
        raise HTTPException(status_code=400, detail="account_name is required")
    meta = PUBLISH_PLATFORMS.get(request.platform, {})
    root = Path(publish_profile_dir(request.platform, account_name))
    root.mkdir(parents=True, exist_ok=True)
    port = publish_port(request.platform)
    with get_db() as conn:
        cursor = conn.cursor()
        workflow = _workflow(cursor, request.platform)
        login_url = request.login_url if request.login_url is not None else (workflow.get("login_url") or meta.get("login_url") or "")
        publish_url = request.publish_url if request.publish_url is not None else (workflow.get("publish_url") or meta.get("publish_url") or "")
        login_url = _valid_optional_url(login_url, "login_url") if login_url else ""
        publish_url = _valid_optional_url(publish_url, "publish_url") if publish_url else ""
        cursor.execute(
            """INSERT OR REPLACE INTO publishing_accounts
               (platform, account_name, profile_dir, port, login_url, publish_url, status, notes, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'unknown', ?, ?)""",
            (
                request.platform,
                account_name,
                str(root),
                port,
                login_url,
                publish_url,
                request.notes or "账号已绑定，需在发布浏览器中完成登录；发布时按固定发布URL执行RPA",
                local_now(),
            ),
        )
        return {
            "id": cursor.lastrowid,
            "platform": request.platform,
            "account_name": account_name,
            "profile_dir": str(root),
            "port": port,
            "login_url": login_url,
            "publish_url": publish_url,
        }


@router.get("/accounts")
def list_accounts(platform: Optional[str] = Query(None)):
    with get_db() as conn:
        cursor = conn.cursor()
        if platform:
            cursor.execute("SELECT * FROM publishing_accounts WHERE platform = ? ORDER BY updated_at DESC", (platform,))
        else:
            cursor.execute("SELECT * FROM publishing_accounts ORDER BY updated_at DESC")
        return [dict(row) for row in cursor.fetchall()]


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM publishing_accounts WHERE id = ?", (account_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Account not found")
        cursor.execute("DELETE FROM publishing_accounts WHERE id = ?", (account_id,))
        return {"deleted": account_id}


@router.post("/accounts/{account_id}/open-login")
def open_account_login(account_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM publishing_accounts WHERE id = ?", (account_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Account not found")
        account = dict(row)
        result = open_publish_browser(account["platform"], account["account_name"], account.get("login_url") or "")
        if result.get("success"):
            # open_publish_browser 已叠加登录态：logged_in / browser_open
            status = result.get("status") or "browser_open"
        else:
            status = "not_connected"
        notes = result.get("notes")
        if not notes and result.get("success"):
            notes = "发布浏览器已打开，已检测到登录态" if status == "logged_in" else "发布浏览器已打开"
        cursor.execute(
            """UPDATE publishing_accounts
               SET status = ?, profile_dir = ?, port = ?, last_checked_at = ?, notes = ?, updated_at = ?
               WHERE id = ?""",
            (
                status,
                result.get("profile_dir") or account.get("profile_dir"),
                result.get("port") or account.get("port"),
                local_now(),
                notes or "发布浏览器打开失败",
                local_now(),
                account_id,
            ),
        )
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("notes") or "打开发布登录浏览器失败")
        return result


@router.get("/accounts/{account_id}/status")
def get_account_status(account_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM publishing_accounts WHERE id = ?", (account_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Account not found")
        account = dict(row)
        result = check_publish_browser(account["platform"], account["account_name"])
        cursor.execute(
            """UPDATE publishing_accounts
               SET status = ?, profile_dir = ?, port = ?, last_checked_at = ?, updated_at = ?
               WHERE id = ?""",
            (
                result.get("status"),
                result.get("profile_dir"),
                result.get("port"),
                local_now(),
                local_now(),
                account_id,
            ),
        )
        return {**account, **result}


@router.post("/publish/tasks")
def create_publish_task(request: PublishTaskCreateRequest):
    if request.platform not in PUBLISH_PLATFORMS:
        raise HTTPException(status_code=400, detail="Invalid platform")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM content_assets WHERE id = ?", (request.content_id,))
        content = cursor.fetchone()
        if not content:
            raise HTTPException(status_code=404, detail="Content not found")
        content = dict(content)
        if content.get("status") != "approved":
            raise HTTPException(status_code=400, detail="Content must be approved before creating a publish task")
        _require_content_quality(content)
        if content.get("target_platform") and content["target_platform"] != request.platform:
            raise HTTPException(status_code=400, detail="Publish platform must match content target_platform")
        if request.platform in COVER_REQUIRED_PLATFORMS and not _content_has_image(content):
            name = PUBLISH_PLATFORMS[request.platform]["name"]
            raise HTTPException(
                status_code=400,
                detail=f"{name} 发布需要封面图：该内容还没有配图，请先在「内容生产」列表的「图片/视频」列点「选文件」上传至少 1 张图片（≥400×300）",
            )
        workflow = _workflow(cursor, request.platform)
        capability = PUBLISH_CAPABILITIES.get(request.platform, {})
        if request.platform != "website" and capability.get("level") in {"template_only", "unknown"}:
            raise HTTPException(status_code=400, detail=f"{PUBLISH_PLATFORMS[request.platform]['name']} 目前只有模板/规划，未完成真实发布闭环，暂不能创建商业发布任务")
        if request.platform != "website":
            if workflow.get("status") not in {"tested", "active"}:
                raise HTTPException(status_code=400, detail="Publish workflow must be tested or active before creating external publish tasks")
            if not _workflow_has_verified_test(cursor, request.platform):
                raise HTTPException(status_code=400, detail="Publish workflow must have verified test evidence before creating external publish tasks")
            if not workflow.get("publish_url"):
                raise HTTPException(status_code=400, detail="External publish workflow requires a fixed publish URL")
        if request.platform != "website" and not request.account_id:
            raise HTTPException(status_code=400, detail="External publishing requires a bound account")
        if request.account_id:
            cursor.execute("SELECT * FROM publishing_accounts WHERE id = ?", (request.account_id,))
            account = cursor.fetchone()
            if not account:
                raise HTTPException(status_code=404, detail="Publishing account not found")
            if account["platform"] != request.platform:
                raise HTTPException(status_code=400, detail="Publishing account platform mismatch")
        cursor.execute(
            f"""SELECT id, status FROM publishing_tasks
                WHERE content_id = ? AND platform = ?
                  AND status IN ({",".join("?" for _ in ACTIVE_PUBLISH_STATUSES)})
                ORDER BY id DESC LIMIT 1""",
            (request.content_id, request.platform, *ACTIVE_PUBLISH_STATUSES),
        )
        existing = cursor.fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"Active publish task already exists: {existing['id']} ({existing['status']})")
        cursor.execute(
            """INSERT INTO publishing_tasks
               (content_id, account_id, brand_id, platform, status, publish_mode, scheduled_at, notes)
               VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (
                request.content_id,
                request.account_id,
                content["brand_id"],
                request.platform,
                request.publish_mode,
                request.scheduled_at,
                "已创建发布任务；当前默认人工确认发布，执行时保存截图/HTML/失败原因",
            ),
        )
        task_id = cursor.lastrowid
        cursor.execute("UPDATE content_assets SET status = 'queued', updated_at = ? WHERE id = ?", (local_now(), request.content_id))
        return {"id": task_id, "status": "pending"}


@router.post("/publish/tasks/{task_id}/prepare")
def prepare_publish_task(task_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT pt.*, ca.title, ca.body, ca.target_keyword, pa.login_url
               FROM publishing_tasks pt
               LEFT JOIN content_assets ca ON ca.id = pt.content_id
               LEFT JOIN publishing_accounts pa ON pa.id = pt.account_id
               WHERE pt.id = ?""",
            (task_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Publish task not found")
        item = dict(row)
        if item.get("status") == "published":
            raise HTTPException(status_code=400, detail="Published task cannot be prepared again")
        _require_content_quality(item)
        artifact_dir = Path(settings.GEO_ARTIFACT_DIR) / "publishing" / datetime.now().strftime("%Y%m%d")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        html_path = artifact_dir / f"publish_task_{task_id}.html"
        html_doc = _content_html(item["title"], item["body"])
        html_path.write_text(html_doc, encoding="utf-8")
        screenshot_path = None
        summary_path = None
        try:
            from app.services.publishing_rpa import _make_html_screenshot, _make_publish_summary
            screenshot_path = _make_html_screenshot(task_id, str(html_path))
            summary_path = _make_publish_summary(item, item, str(html_path), screenshot_path, "assets_ready")
        except Exception:
            screenshot_path = None
            summary_path = None
        evidence = _validate_publish_evidence(
            task_id=task_id,
            screenshot_path=screenshot_path,
            raw_artifact_path=str(html_path),
            summary_artifact_path=summary_path,
        )
        cursor.execute(
            """UPDATE publishing_tasks
               SET status = 'assets_ready', screenshot_path = ?, screenshot_verified = ?,
                   raw_artifact_path = ?, raw_artifact_verified = ?,
                   summary_artifact_path = ?, summary_artifact_verified = ?,
                   evidence_status = ?, evidence_failure_reason = ?, updated_at = ?, notes = ?
               WHERE id = ?""",
            (
                screenshot_path,
                evidence["screenshot_verified"],
                str(html_path),
                evidence["raw_artifact_verified"],
                summary_path,
                evidence["summary_artifact_verified"],
                evidence["evidence_status"],
                evidence["evidence_failure_reason"],
                local_now(),
                "仅生成本地 HTML/截图/摘要素材，未打开平台浏览器、未跑 RPA；请再点「运行RPA」填草稿",
                task_id,
            ),
        )
        return {
            "id": task_id,
            "status": "assets_ready",
            "raw_artifact_path": str(html_path),
            "screenshot_path": screenshot_path,
            "summary_artifact_path": summary_path,
            "login_url": item.get("login_url"),
        }


def _finalize_publish_run(task_id: int, task: dict, result: PublishRpaResult) -> None:
    with get_db() as conn:
        cursor = conn.cursor()
        evidence = _validate_publish_evidence(
            task_id=task_id,
            published_url=result.published_url,
            screenshot_path=result.screenshot_path,
            raw_artifact_path=result.raw_artifact_path,
            summary_artifact_path=result.summary_artifact_path,
            live_url_check=False,
        )
        cursor.execute(
            """UPDATE publishing_tasks
               SET status = ?, completed_at = ?, published_url = ?, screenshot_path = ?,
                   raw_artifact_path = ?, summary_artifact_path = ?,
                   published_url_verified = ?, screenshot_verified = ?,
                   raw_artifact_verified = ?, summary_artifact_verified = ?,
                   evidence_status = ?, evidence_failure_reason = ?,
                   failure_reason = ?, notes = ?, updated_at = ?
               WHERE id = ?""",
            (
                result.status,
                local_now(),
                result.published_url,
                result.screenshot_path,
                result.raw_artifact_path,
                result.summary_artifact_path,
                evidence["published_url_verified"],
                evidence["screenshot_verified"],
                evidence["raw_artifact_verified"],
                evidence["summary_artifact_verified"],
                evidence["evidence_status"],
                evidence["evidence_failure_reason"],
                result.failure_reason,
                result.notes,
                local_now(),
                task_id,
            ),
        )
        next_content_status = "publishing" if result.status in ("prepared", "manual_required", "login_required", "running") else "draft"
        if result.status == "published":
            next_content_status = "published"
        cursor.execute("UPDATE content_assets SET status = ?, updated_at = ? WHERE id = ?", (next_content_status, local_now(), task["content_id"]))


def _release_stale_publish_runs(cursor, brand_id: Optional[int] = None) -> int:
    cutoff = (datetime.now() - timedelta(seconds=90)).strftime("%Y-%m-%d %H:%M:%S")
    where = "status = 'running' AND COALESCE(started_at, updated_at) < ?"
    params: list = [cutoff]
    if brand_id:
        where += " AND brand_id = ?"
        params.append(brand_id)
    cursor.execute(
        f"""UPDATE publishing_tasks
            SET status = 'manual_required',
                failure_reason = '发布 RPA 后台执行超过 90 秒未完成，已自动释放；请检查平台浏览器弹框、验证、上传或必填项',
                notes = '未伪装发布成功；可重试、回捞验真或删除任务重建',
                updated_at = ?
            WHERE {where}""",
        [local_now(), *params],
    )
    return cursor.rowcount


def _run_publish_task_background(task_id: int, task: dict, content: dict, account: Optional[dict], workflow_payload: dict) -> None:
    script = Path(__file__).resolve().parents[3] / "scripts" / "run_publish_task.py"
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "--task-id", str(task_id)],
            cwd=str(Path(__file__).resolve().parents[3]),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        result = PublishRpaResult(
            status="manual_required",
            failure_reason="发布 RPA 子进程超过 120 秒未完成；请检查平台弹框、验证或必填项",
            notes="系统未伪装发布成功；可重试、回捞验真或删除任务重建",
        )
        _finalize_publish_run(task_id, task, result)
        return
    if completed.returncode != 0:
        result = PublishRpaResult(
            status="failed",
            failure_reason=(completed.stderr or completed.stdout or "发布 RPA 子进程失败")[:500],
            notes="系统未伪装发布成功，已保留任务供重试",
        )
        _finalize_publish_run(task_id, task, result)


@router.post("/publish/tasks/{task_id}/run")
def run_publish_task(task_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        _release_stale_publish_runs(cursor)
        cursor.execute(
            """SELECT pt.*, ca.title, ca.body, ca.target_keyword, ca.media_assets_json
               FROM publishing_tasks pt
               LEFT JOIN content_assets ca ON ca.id = pt.content_id
               WHERE pt.id = ?""",
            (task_id,),
        )
        task_row = cursor.fetchone()
        if not task_row:
            raise HTTPException(status_code=404, detail="Publish task not found")
        task = dict(task_row)
        if task.get("status") == "published":
            raise HTTPException(status_code=400, detail="Published task cannot be run again; use retest")
        if task.get("status") == "running":
            return {
                "id": task_id,
                "status": "running",
                "notes": "发布 RPA 已在后台执行中，请勿重复点击；90 秒未完成会自动释放为需人工处理",
                "async_run": True,
            }
        cursor.execute("SELECT * FROM content_assets WHERE id = ?", (task["content_id"],))
        content = dict(cursor.fetchone())
        _require_content_quality(content)
        account = None
        if task.get("account_id"):
            cursor.execute("SELECT * FROM publishing_accounts WHERE id = ?", (task["account_id"],))
            row = cursor.fetchone()
            account = dict(row) if row else None
        workflow = _workflow(cursor, task["platform"])
        workflow_payload = _workflow_payload(workflow)
        if account:
            account["login_url"] = account.get("login_url") or workflow.get("login_url") or ""
            account["publish_url"] = account.get("publish_url") or workflow.get("publish_url") or ""
        if task["platform"] != "website" and not account:
            raise HTTPException(status_code=400, detail="External publishing requires a bound account")
        cursor.execute(
            "UPDATE publishing_tasks SET status = 'running', started_at = ?, updated_at = ? WHERE id = ?",
            (local_now(), local_now(), task_id),
        )

    publish_executor.submit(_run_publish_task_background, task_id, task, content, account, workflow_payload)
    return {
        "id": task_id,
        "status": "running",
        "notes": "发布 RPA 已进入后台执行；请等待任务状态刷新，系统不会伪装发布成功",
        "async_run": True,
    }


@router.get("/publish/tasks")
def list_publish_tasks(
    brand_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    platform: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    limit: int = 100,
):
    with get_db() as conn:
        cursor = conn.cursor()
        _release_stale_publish_runs(cursor, brand_id)
        where, params = [], []
        if brand_id:
            where.append("pt.brand_id = ?")
            params.append(brand_id)
        if status:
            where.append("pt.status = ?")
            params.append(status)
        if platform:
            where.append("pt.platform = ?")
            params.append(platform)
        if keyword:
            where.append("(ca.title LIKE ? OR ca.target_keyword LIKE ?)")
            kw = f"%{keyword}%"
            params.extend([kw, kw])
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        cursor.execute(
            f"""SELECT pt.*, ca.title, ca.content_type, b.name AS brand_name,
                       pa.account_name, pa.publish_url, pw.publish_url AS workflow_publish_url,
                       pw.status AS workflow_status
                FROM publishing_tasks pt
                LEFT JOIN content_assets ca ON ca.id = pt.content_id
                LEFT JOIN brands b ON b.id = pt.brand_id
                LEFT JOIN publishing_accounts pa ON pa.id = pt.account_id
                LEFT JOIN publishing_workflows pw ON pw.platform = pt.platform
                {where_sql}
                ORDER BY pt.created_at DESC
                LIMIT ?""",
            params + [limit],
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["publish_gate_reason"] = _publish_task_gate_reason(item)
            item["material_evidence_status"] = _publish_material_status(item)
            item["closure_status"] = _publish_task_closure_status(item)
            item["workflow_needs_retest"] = bool(
                item.get("closure_status") == "verified_loop"
                and item.get("platform") != "website"
                and item.get("workflow_status") not in {"tested", "active"}
            )
            rows.append(item)
        return rows


@router.get("/publish/tasks/{task_id}")
def get_publish_task(task_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT pt.*, ca.title, ca.body, ca.target_keyword, pa.account_name, pa.profile_dir, pa.port,
                      pa.publish_url, pw.publish_url AS workflow_publish_url,
                      pw.status AS workflow_status
               FROM publishing_tasks pt
               LEFT JOIN content_assets ca ON ca.id = pt.content_id
               LEFT JOIN publishing_accounts pa ON pa.id = pt.account_id
               LEFT JOIN publishing_workflows pw ON pw.platform = pt.platform
               WHERE pt.id = ?""",
            (task_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Publish task not found")
        item = dict(row)
        item["publish_gate_reason"] = _publish_task_gate_reason(item)
        item["material_evidence_status"] = _publish_material_status(item)
        item["closure_status"] = _publish_task_closure_status(item)
        item["workflow_needs_retest"] = bool(
            item.get("closure_status") == "verified_loop"
            and item.get("platform") != "website"
            and item.get("workflow_status") not in {"tested", "active"}
        )
        return item


@router.post("/publish/tasks/{task_id}/confirm")
def confirm_publish_task(task_id: int, request: PublishConfirmRequest):
    url = (request.published_url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="published_url must start with http:// or https://")
    url, http_status = _validate_published_url(url)
    monitor_task = None
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT pt.*, ca.keyword_id, ca.target_keyword, ca.title, b.name AS brand_name
               FROM publishing_tasks pt
               LEFT JOIN content_assets ca ON ca.id = pt.content_id
               LEFT JOIN brands b ON b.id = pt.brand_id
               WHERE pt.id = ?""",
            (task_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Publish task not found")
        task = dict(row)
        task["published_url"] = url
        evidence = _validate_publish_evidence(
            task_id=task_id,
            published_url=url,
            screenshot_path=task.get("screenshot_path"),
            raw_artifact_path=task.get("raw_artifact_path"),
            summary_artifact_path=task.get("summary_artifact_path"),
            live_url_check=False,
        )
        cursor.execute(
            """UPDATE publishing_tasks
               SET status = 'published', published_url = ?, completed_at = ?,
                   published_url_verified = ?, screenshot_verified = ?, raw_artifact_verified = ?,
                   summary_artifact_verified = ?,
                   evidence_status = ?, evidence_failure_reason = ?,
                   failure_reason = NULL, notes = ?, updated_at = ?
               WHERE id = ?""",
            (
                url,
                local_now(),
                evidence["published_url_verified"],
                evidence["screenshot_verified"],
                evidence["raw_artifact_verified"],
                evidence["summary_artifact_verified"],
                evidence["evidence_status"],
                evidence["evidence_failure_reason"],
                request.notes or f"已回填真实发布链接，HTTP {http_status}",
                local_now(),
                task_id,
            ),
        )
        cursor.execute(
            "UPDATE content_assets SET status = 'published', updated_at = ? WHERE id = ?",
            (local_now(), task["content_id"]),
        )
        if request.trigger_monitor:
            monitor_task = _create_post_publish_monitor(cursor, task)
        cursor.execute("SELECT * FROM publishing_tasks WHERE id = ?", (task_id,))
        updated = dict(cursor.fetchone())
    return {"task": updated, "monitor_task": monitor_task}


@router.post("/publish/tasks/{task_id}/auto-verify")
def auto_verify_website_publish_task(task_id: int, request: PublishAutoVerifyRequest):
    monitor_task = None
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT pt.*, ca.keyword_id, ca.target_keyword, ca.title, ca.body, b.name AS brand_name
               FROM publishing_tasks pt
               LEFT JOIN content_assets ca ON ca.id = pt.content_id
               LEFT JOIN brands b ON b.id = pt.brand_id
               WHERE pt.id = ?""",
            (task_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Publish task not found")
        task = dict(row)
        if task.get("platform") != "website":
            raise HTTPException(status_code=400, detail="当前自动验真仅支持官网/落地页；外部平台需平台专属 RPA 回捞 URL")
        if task.get("status") == "published" and task.get("published_url_verified"):
            return {"task": task, "monitor_task": None, "status": "already_verified"}
        # 「生成素材」落库状态是 assets_ready（见 prepare_publish_task），而历史实现只放行
        # prepared/pending/manual_required，导致前端可以点击的「自动验真」在后端被 400 拒绝，
        # 发布闭环断在最后一步。这里与前端 canAutoVerifyWebsite 的放行集合对齐：
        # pending / assets_ready / prepared / manual_required / failed 均允许重新生成素材并验真。
        if task.get("status") not in {"pending", "assets_ready", "prepared", "manual_required", "failed"}:
            raise HTTPException(status_code=400, detail=f"任务状态不可自动发布验真: {task.get('status')}")

    if not task.get("raw_artifact_path") or not task.get("screenshot_path") or not task.get("summary_artifact_path"):
        prepared = prepare_publish_task(task_id)
        task.update(prepared)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT pt.*, ca.keyword_id, ca.target_keyword, ca.title, ca.body, b.name AS brand_name
               FROM publishing_tasks pt
               LEFT JOIN content_assets ca ON ca.id = pt.content_id
               LEFT JOIN brands b ON b.id = pt.brand_id
               WHERE pt.id = ?""",
            (task_id,),
        )
        task = dict(cursor.fetchone())
        public_url = _artifact_public_url(task.get("raw_artifact_path"))
        if not public_url:
            cursor.execute(
                """UPDATE publishing_tasks
                   SET status = 'failed', failure_reason = ?, evidence_status = 'unverified',
                       evidence_failure_reason = ?, updated_at = ?
                   WHERE id = ?""",
                ("官网发布 HTML 不在证据目录，无法生成系统发布 URL", "官网发布 HTML 不在证据目录", local_now(), task_id),
            )
            raise HTTPException(status_code=400, detail="官网发布 HTML 不在证据目录，无法生成系统发布 URL")
        ok, reason = _verify_website_publication(task, public_url)
        evidence = _validate_publish_evidence(
            task_id=task_id,
            published_url=f"{settings.public_api_base}{public_url}",
            screenshot_path=task.get("screenshot_path"),
            raw_artifact_path=task.get("raw_artifact_path"),
            summary_artifact_path=task.get("summary_artifact_path"),
            live_url_check=False,
        )
        if not ok:
            cursor.execute(
                """UPDATE publishing_tasks
                   SET status = 'failed', published_url = ?, published_url_verified = 0,
                       screenshot_verified = ?, raw_artifact_verified = ?, summary_artifact_verified = ?,
                       evidence_status = 'unverified', evidence_failure_reason = ?,
                       failure_reason = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    public_url,
                    evidence["screenshot_verified"],
                    evidence["raw_artifact_verified"],
                    evidence["summary_artifact_verified"],
                    reason,
                    reason,
                    local_now(),
                    task_id,
                ),
            )
            raise HTTPException(status_code=400, detail=reason)
        cursor.execute(
            """UPDATE publishing_tasks
               SET status = 'published', published_url = ?, completed_at = ?,
                   published_url_verified = 1, screenshot_verified = ?, raw_artifact_verified = ?,
                   summary_artifact_verified = ?, evidence_status = 'verified',
                   evidence_failure_reason = NULL, failure_reason = NULL,
                   notes = ?, updated_at = ?
               WHERE id = ?""",
            (
                public_url,
                local_now(),
                evidence["screenshot_verified"],
                evidence["raw_artifact_verified"],
                evidence["summary_artifact_verified"],
                "官网/落地页已由系统自动生成发布 URL，并完成标题/正文/三件套验真",
                local_now(),
                task_id,
            ),
        )
        cursor.execute(
            "UPDATE content_assets SET status = 'published', updated_at = ? WHERE id = ?",
            (local_now(), task["content_id"]),
        )
        task["published_url"] = public_url
        if request.trigger_monitor:
            monitor_task = _create_post_publish_monitor(cursor, task)
        cursor.execute("SELECT * FROM publishing_tasks WHERE id = ?", (task_id,))
        updated = dict(cursor.fetchone())
    return {"task": updated, "monitor_task": monitor_task, "status": "verified"}


@router.post("/publish/tasks/{task_id}/recover-url")
def recover_external_publish_url(task_id: int, request: PublishRecoverUrlRequest):
    monitor_task = None
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT pt.*, ca.keyword_id, ca.target_keyword, ca.title, ca.body,
                      pa.account_name, pa.profile_dir, pa.port, b.name AS brand_name
               FROM publishing_tasks pt
               LEFT JOIN content_assets ca ON ca.id = pt.content_id
               LEFT JOIN publishing_accounts pa ON pa.id = pt.account_id
               LEFT JOIN brands b ON b.id = pt.brand_id
               WHERE pt.id = ?""",
            (task_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Publish task not found")
        task = dict(row)
        if task.get("platform") not in {"baijiahao", "ctrip_travel", "zhihu", "toutiao", "sohu"}:
            raise HTTPException(status_code=400, detail="当前URL回捞仅支持百家号/携程/知乎/今日头条/搜狐号；其他平台需接入专属回捞脚本")
        if not task.get("account_id"):
            raise HTTPException(status_code=400, detail=f"{task.get('platform')}URL回捞需要绑定账号")
        if task.get("status") not in {"prepared", "manual_required", "login_required", "running", "published"}:
            raise HTTPException(status_code=400, detail=f"任务状态不可回捞URL: {task.get('status')}")

    candidate_url = (request.published_url or "").strip()
    recovery = None
    if not candidate_url:
        script = Path(__file__).resolve().parents[3] / "scripts" / "recover_publish_url.py"
        try:
            completed = subprocess.run(
                [sys.executable, str(script), "--task-id", str(task_id)],
                cwd=str(Path(__file__).resolve().parents[3]),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=45,
            )
            if completed.returncode != 0:
                recovery = {"status": "failed", "failure_reason": (completed.stderr or completed.stdout or "URL回捞子进程失败")[:500], "candidates": []}
            else:
                recovery = json.loads((completed.stdout or "{}").strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            recovery = {"status": "failed", "failure_reason": "URL回捞超过45秒未完成；请刷新平台内容管理页后重试", "candidates": []}
        except Exception as exc:
            recovery = {"status": "failed", "failure_reason": f"URL回捞异常: {str(exc)[:180]}", "candidates": []}
        candidates = recovery.get("candidates") or []
        candidate_url = (candidates[0] or {}).get("href") if candidates else ""
    if not candidate_url:
        recovery_status = (recovery or {}).get("status") if recovery else ""
        if recovery_status in {"pending_review", "rejected", "platform_published_no_public_url"}:
            from app.services.publishing_rpa import _make_publish_summary

            notes_by_status = {
                "pending_review": "携程作品已提交，平台仍在审核中；稍后刷新并重新回捞URL",
                "rejected": "携程作品审核未通过；请在平台查看原因后修改内容或素材",
                "platform_published_no_public_url": "携程后台显示已发布，但PC内容中心未暴露公开URL；需接入分享链接回捞或人工回填公开URL",
            }
            evidence_status = "pending_review" if recovery_status == "pending_review" else ("platform_published" if recovery_status == "platform_published_no_public_url" else "unverified")
            summary_path = _make_publish_summary(
                task,
                task,
                (recovery or {}).get("raw_artifact_path") or task.get("raw_artifact_path"),
                (recovery or {}).get("screenshot_path") or task.get("screenshot_path"),
                recovery_status,
            )
            with get_db() as conn:
                conn.execute(
                    """UPDATE publishing_tasks
                       SET status = 'manual_required', failure_reason = ?, evidence_failure_reason = ?,
                           screenshot_path = COALESCE(?, screenshot_path),
                           raw_artifact_path = COALESCE(?, raw_artifact_path),
                           summary_artifact_path = COALESCE(?, summary_artifact_path),
                           screenshot_verified = CASE WHEN ? IS NOT NULL THEN 1 ELSE screenshot_verified END,
                           raw_artifact_verified = CASE WHEN ? IS NOT NULL THEN 1 ELSE raw_artifact_verified END,
                           summary_artifact_verified = CASE WHEN ? IS NOT NULL THEN 1 ELSE summary_artifact_verified END,
                           evidence_status = ?, notes = ?, updated_at = ?
                       WHERE id = ?""",
                    (
                        (recovery or {}).get("failure_reason") or notes_by_status[recovery_status],
                        (recovery or {}).get("failure_reason") or notes_by_status[recovery_status],
                        (recovery or {}).get("screenshot_path"),
                        (recovery or {}).get("raw_artifact_path"),
                        summary_path,
                        (recovery or {}).get("screenshot_path"),
                        (recovery or {}).get("raw_artifact_path"),
                        summary_path,
                        evidence_status,
                        notes_by_status[recovery_status],
                        local_now(),
                        task_id,
                    ),
                )
            return {
                "status": recovery_status,
                "failure_reason": (recovery or {}).get("failure_reason"),
                "candidates": [],
                "recovery": recovery,
            }
        with get_db() as conn:
            conn.execute(
                """UPDATE publishing_tasks
                   SET status = 'manual_required', failure_reason = ?, evidence_failure_reason = ?, updated_at = ?
                   WHERE id = ?""",
                (f"{task.get('platform')}未回捞到发布URL，请人工打开内容管理页确认作品链接", f"{task.get('platform')}未回捞到发布URL", local_now(), task_id),
            )
        return {
            "status": "manual_required",
            "failure_reason": f"{task.get('platform')}未回捞到发布URL",
            "candidates": [],
            "recovery": recovery,
        }

    if task.get("platform") == "toutiao" and request.published_url:
        # 人工回填的头条链接同样必须验真：先校验是头条公开内容页，再校验可访问。
        # 不允许直接标记 verified=True（会架空"不伪装发布成功"的验真承诺）。
        verified = {
            "verified": False,
            "failure_reason": "",
            "http_status": 0,
            "screenshot_path": task.get("screenshot_path"),
            "raw_artifact_path": task.get("raw_artifact_path"),
        }
        if not _is_public_content_url("toutiao", candidate_url):
            verified["failure_reason"] = "回填链接不是今日头条公开内容页（需为 toutiao.com 的 article/group 页）"
        else:
            try:
                response = requests.head(
                    candidate_url, allow_redirects=True, timeout=8,
                    headers={"User-Agent": "GeoOps/2.0"},
                )
                if response.status_code in (405, 403) or response.status_code >= 500:
                    response = requests.get(
                        candidate_url, allow_redirects=True, timeout=10, stream=True,
                        headers={"User-Agent": "GeoOps/2.0"},
                    )
                    response.close()
                verified["http_status"] = response.status_code
                if response.status_code >= 500 or response.status_code == 404:
                    verified["failure_reason"] = f"回填链接无法访问 HTTP {response.status_code}"
                else:
                    verified["verified"] = True
            except Exception as exc:
                verified["failure_reason"] = f"回填链接无法访问：{str(exc)[:120]}"
    elif task.get("platform") == "toutiao":
        verified = verify_toutiao_published_page_with_browser(
            task.get("account_name") or "default",
            candidate_url,
            task.get("title") or "",
            task.get("body") or "",
            task_id=task_id,
        )
    else:
        verified = verify_published_page_url(candidate_url, task.get("title") or "", task.get("body") or "", task.get("platform") or "")
    with get_db() as conn:
        cursor = conn.cursor()
        if not verified.get("verified"):
            cursor.execute(
                """UPDATE publishing_tasks
                   SET status = 'manual_required', published_url = ?, published_url_verified = 0,
                       evidence_status = 'unverified', evidence_failure_reason = ?,
                       failure_reason = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    candidate_url,
                    verified.get("failure_reason") or f"{task.get('platform')}发布URL验真失败",
                    verified.get("failure_reason") or f"{task.get('platform')}发布URL验真失败",
                    local_now(),
                    task_id,
                ),
            )
            return {
                "status": "manual_required",
                "published_url": candidate_url,
                "verified": False,
                "failure_reason": verified.get("failure_reason"),
                "recovery": recovery,
            }
        from app.services.publishing_rpa import _make_publish_summary

        summary_artifact_path = _make_publish_summary(
            task,
            task,
            verified.get("raw_artifact_path") or task.get("raw_artifact_path"),
            verified.get("screenshot_path") or task.get("screenshot_path"),
            "published",
        )
        evidence = _validate_publish_evidence(
            task_id=task_id,
            published_url=candidate_url,
            screenshot_path=verified.get("screenshot_path") or task.get("screenshot_path"),
            raw_artifact_path=verified.get("raw_artifact_path") or task.get("raw_artifact_path"),
            summary_artifact_path=summary_artifact_path or task.get("summary_artifact_path"),
            live_url_check=False,
        )
        cursor.execute(
            """UPDATE publishing_tasks
               SET status = 'published', published_url = ?, completed_at = ?,
                   published_url_verified = 1, screenshot_path = ?, raw_artifact_path = ?,
                   screenshot_verified = ?, raw_artifact_verified = ?,
                   summary_artifact_path = ?, summary_artifact_verified = ?, evidence_status = ?,
                   evidence_failure_reason = ?, failure_reason = NULL, notes = ?, updated_at = ?
               WHERE id = ?""",
            (
                candidate_url,
                local_now(),
                verified.get("screenshot_path") or task.get("screenshot_path"),
                verified.get("raw_artifact_path") or task.get("raw_artifact_path"),
                evidence["screenshot_verified"],
                evidence["raw_artifact_verified"],
                summary_artifact_path or task.get("summary_artifact_path"),
                evidence["summary_artifact_verified"],
                evidence["evidence_status"],
                evidence["evidence_failure_reason"],
                f"{task.get('platform')}发布URL已回捞并通过标题/正文/证据验真",
                local_now(),
                task_id,
            ),
        )
        cursor.execute("UPDATE content_assets SET status = 'published', updated_at = ? WHERE id = ?", (local_now(), task["content_id"]))
        task["published_url"] = candidate_url
        if request.trigger_monitor:
            monitor_task = _create_post_publish_monitor(cursor, task)
        cursor.execute("SELECT * FROM publishing_tasks WHERE id = ?", (task_id,))
        updated = dict(cursor.fetchone())
    return {"status": "verified", "task": updated, "monitor_task": monitor_task, "recovery": recovery}


@router.post("/publish/tasks/{task_id}/fail")
def fail_publish_task(task_id: int, request: PublishFailRequest):
    if not request.failure_reason.strip():
        raise HTTPException(status_code=400, detail="failure_reason is required")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT content_id FROM publishing_tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Publish task not found")
        cursor.execute(
            """UPDATE publishing_tasks
               SET status = 'failed', failure_reason = ?, notes = ?, completed_at = ?, updated_at = ?
               WHERE id = ?""",
            (request.failure_reason, request.notes or "发布失败，等待处理", local_now(), local_now(), task_id),
        )
        cursor.execute(
            "UPDATE content_assets SET status = 'approved', updated_at = ? WHERE id = ?",
            (local_now(), row["content_id"]),
        )
        cursor.execute("SELECT * FROM publishing_tasks WHERE id = ?", (task_id,))
        return dict(cursor.fetchone())


@router.delete("/publish/tasks/{task_id}")
def delete_publish_task(task_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM publishing_tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Publish task not found")
        task = dict(row)
        if task.get("status") == "published" and int(task.get("published_url_verified") or 0) == 1:
            raise HTTPException(status_code=400, detail="已验真发布成功的任务不能删除；请保留真实交付记录")
        cursor.execute("DELETE FROM publishing_tasks WHERE id = ?", (task_id,))
        cursor.execute("SELECT COUNT(*) AS count FROM publishing_tasks WHERE content_id = ?", (task["content_id"],))
        remaining = cursor.fetchone()["count"] or 0
        if remaining == 0:
            cursor.execute(
                "UPDATE content_assets SET status = 'approved', updated_at = ? WHERE id = ? AND status != 'published'",
                (local_now(), task["content_id"]),
            )
        else:
            cursor.execute(
                """SELECT status, published_url_verified
                   FROM publishing_tasks
                   WHERE content_id = ?
                   ORDER BY id DESC""",
                (task["content_id"],),
            )
            rows = [dict(item) for item in cursor.fetchall()]
            has_verified = any(item.get("status") == "published" and int(item.get("published_url_verified") or 0) == 1 for item in rows)
            has_active = any(item.get("status") in ACTIVE_PUBLISH_STATUSES for item in rows)
            next_status = "published" if has_verified else ("queued" if has_active else "approved")
            cursor.execute(
                "UPDATE content_assets SET status = ?, updated_at = ? WHERE id = ?",
                (next_status, local_now(), task["content_id"]),
            )
        return {"deleted": task_id, "content_id": task["content_id"], "remaining_tasks": remaining}


@router.post("/publish/tasks/{task_id}/retest")
def retest_publish_task(task_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT pt.*, ca.keyword_id, ca.target_keyword, ca.title, b.name AS brand_name
               FROM publishing_tasks pt
               LEFT JOIN content_assets ca ON ca.id = pt.content_id
               LEFT JOIN brands b ON b.id = pt.brand_id
               WHERE pt.id = ?""",
            (task_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Publish task not found")
        task = dict(row)
        if not task.get("published_url"):
            raise HTTPException(status_code=400, detail="published_url is required before retest")
        if not int(task.get("published_url_verified") or 0):
            _validate_published_url(task["published_url"])
        monitor_task = _create_post_publish_monitor(cursor, task)
        cursor.execute(
            "UPDATE publishing_tasks SET notes = ?, updated_at = ? WHERE id = ?",
            ("已提交发布后复测任务", local_now(), task_id),
        )
        return monitor_task


@router.get("/workbench")
def get_workbench(brand_id: Optional[int] = Query(None)):
    with get_db() as conn:
        cursor = conn.cursor()
        where = "WHERE brand_id = ?" if brand_id else ""
        params = [brand_id] if brand_id else []
        cursor.execute(f"SELECT status, COUNT(*) AS count FROM content_assets {where} GROUP BY status", params)
        content_status = {row["status"]: row["count"] for row in cursor.fetchall()}
        cursor.execute(f"SELECT status, COUNT(*) AS count FROM publishing_tasks {where} GROUP BY status", params)
        publish_status = {row["status"]: row["count"] for row in cursor.fetchall()}
        current_publish_status = {
            key: value for key, value in publish_status.items()
            if key in {"pending", "prepared", "running", "manual_required", "login_required", "published"}
        }
        historical_publish_status = {
            key: value for key, value in publish_status.items()
            if key not in current_publish_status
        }
        cursor.execute(f"SELECT status, COUNT(*) AS count FROM keyword_ideas {where} GROUP BY status", params)
        keyword_status = {row["status"]: row["count"] for row in cursor.fetchall()}
        cursor.execute(
            f"""SELECT pt.id, pt.status, pt.platform, pt.published_url, pt.failure_reason,
                      ca.title, b.name AS brand_name
               FROM publishing_tasks pt
               LEFT JOIN content_assets ca ON ca.id = pt.content_id
               LEFT JOIN brands b ON b.id = pt.brand_id
               {where.replace('brand_id', 'pt.brand_id') if where else ''}
               ORDER BY pt.updated_at DESC, pt.id DESC
               LIMIT 8""",
            params,
        )
        latest_publish_tasks = [dict(row) for row in cursor.fetchall()]
    next_actions = []
    if keyword_status.get("pending", 0):
        next_actions.append(f"待采纳关键词 {keyword_status['pending']} 条")
    if content_status.get("draft", 0):
        next_actions.append(f"待审核内容 {content_status['draft']} 篇")
    if content_status.get("approved", 0):
        next_actions.append(f"可创建发布任务内容 {content_status['approved']} 篇")
    if publish_status.get("pending", 0):
        next_actions.append(f"待准备发布任务 {publish_status['pending']} 个")
    if publish_status.get("prepared", 0):
        next_actions.append(f"待回填真实发布链接 {publish_status['prepared']} 个")
    if publish_status.get("failed", 0):
        next_actions.append(f"历史发布失败 {publish_status['failed']} 个，可按需重建任务")
    return {
        "keyword_status": keyword_status,
        "content_status": content_status,
        "publish_status": publish_status,
        "current_publish_status": current_publish_status,
        "historical_publish_status": historical_publish_status,
        "latest_publish_tasks": latest_publish_tasks,
        "next_actions": next_actions,
    }


@router.post("/reports/scorecard")
def create_scorecard(brand_id: int = Query(...)):
    with get_db() as conn:
        cursor = conn.cursor()
        brand = _brand(cursor, brand_id)
        cursor.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN verification_status = 'verified_citation' AND source_verified = 1 THEN 1 ELSE 0 END) AS verified,
                      SUM(CASE WHEN verification_status = 'unverified_citation' THEN 1 ELSE 0 END) AS unverified,
                      SUM(CASE WHEN source_verified = 1 THEN 1 ELSE 0 END) AS sources,
                      SUM(CASE WHEN screenshot_verified = 1 THEN 1 ELSE 0 END) AS screenshots,
                      SUM(CASE WHEN raw_artifact_verified = 1 THEN 1 ELSE 0 END) AS raw_artifacts,
                      SUM(CASE WHEN failure_reason IS NOT NULL AND failure_reason != '' THEN 1 ELSE 0 END) AS failed
               FROM monitor_results WHERE brand_id = ?""",
            (brand_id,),
        )
        stats = dict(cursor.fetchone())
        cursor.execute("SELECT COUNT(*) AS count FROM keyword_ideas WHERE brand_id = ?", (brand_id,))
        stats["keyword_ideas"] = cursor.fetchone()["count"]
        cursor.execute("SELECT COUNT(*) AS count FROM content_assets WHERE brand_id = ?", (brand_id,))
        stats["content_assets"] = cursor.fetchone()["count"]
        cursor.execute("SELECT COUNT(*) AS count FROM publishing_tasks WHERE brand_id = ?", (brand_id,))
        stats["publishing_tasks"] = cursor.fetchone()["count"]
        cursor.execute(
            """SELECT COUNT(*) AS count
               FROM publishing_tasks
               WHERE brand_id = ? AND status = 'published' AND published_url_verified = 1""",
            (brand_id,),
        )
        stats["verified_published_tasks"] = cursor.fetchone()["count"]
        cursor.execute("SELECT status, COUNT(*) AS count FROM publishing_tasks WHERE brand_id = ? GROUP BY status", (brand_id,))
        publish_status = {row["status"]: row["count"] for row in cursor.fetchall()}
        cursor.execute(
            """SELECT pt.id, pt.status, pt.platform, pt.published_url, pt.screenshot_path,
                      pt.raw_artifact_path, pt.failure_reason, pt.updated_at,
                      ca.title
               FROM publishing_tasks pt
               LEFT JOIN content_assets ca ON ca.id = pt.content_id
               WHERE pt.brand_id = ?
               ORDER BY pt.updated_at DESC, pt.id DESC
               LIMIT 20""",
            (brand_id,),
        )
        publish_tasks = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            """SELECT mr.platform, mr.search_query, mr.verification_status,
                      mr.source_url, mr.source_domain, mr.monitored_at
               FROM monitor_results mr
               WHERE mr.brand_id = ?
               ORDER BY mr.monitored_at DESC, mr.id DESC
               LIMIT 20""",
            (brand_id,),
        )
        monitor_evidence = [dict(row) for row in cursor.fetchall()]
        total = stats.get("total") or 0
        summary = {
            "brand": brand,
            "stats": stats,
            "publish_status": publish_status,
            "publish_tasks": publish_tasks,
            "monitor_evidence": monitor_evidence,
            "verified_rate": round((stats.get("verified") or 0) / total * 100, 2) if total else 0,
            "evidence_rate": round((stats.get("sources") or 0) / total * 100, 2) if total else 0,
            "publish_ready_rate": round(((publish_status.get("prepared", 0) + publish_status.get("published", 0)) / (stats.get("publishing_tasks") or 1)) * 100, 2) if stats.get("publishing_tasks") else 0,
        }
        try:
            diagnosis, usage = _generate_report_diagnosis_llm(brand, summary)
            report_source = "llm_generated_from_real_data"
        except HTTPException as exc:
            diagnosis = _fallback_report_diagnosis(summary, str(exc.detail))
            usage = {}
            report_source = "rule_fallback_from_real_data"
        summary["diagnosis"] = diagnosis
        summary["llm_provider"] = usage.get("provider")
        summary["llm_model"] = usage.get("model")
        summary["llm_usage_id"] = usage.get("id")
        summary["source"] = report_source
        artifact_path = _write_scorecard_artifact(brand, summary)
        cursor.execute(
            """INSERT INTO geo_reports
               (brand_id, summary_json, artifact_path, llm_provider, llm_model, llm_usage_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                brand_id,
                json.dumps(summary, ensure_ascii=False),
                artifact_path,
                usage.get("provider"),
                usage.get("model"),
                usage.get("id"),
            ),
        )
        return {"id": cursor.lastrowid, "artifact_path": artifact_path, **summary}


@router.get("/reports")
def list_reports(brand_id: Optional[int] = Query(None), limit: int = 20):
    with get_db() as conn:
        cursor = conn.cursor()
        if brand_id:
            cursor.execute("SELECT * FROM geo_reports WHERE brand_id = ? ORDER BY created_at DESC LIMIT ?", (brand_id, limit))
        else:
            cursor.execute("SELECT * FROM geo_reports ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["summary"] = json.loads(item.pop("summary_json") or "{}")
            rows.append(item)
        return rows


def _content_html(title: str, body: str) -> str:
    safe_title = html.escape(title or "")
    safe_body = html.escape(body or "")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{safe_title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif; max-width: 860px; margin: 32px auto; line-height: 1.75; color: #1f2937; }}
    h1 {{ font-size: 28px; }}
    pre {{ white-space: pre-wrap; font-family: inherit; background: #f8fafc; border: 1px solid #e5e7eb; padding: 16px; border-radius: 8px; }}
  </style>
</head>
<body><h1>{safe_title}</h1><pre>{safe_body}</pre></body>
</html>"""


def _publish_timeout_result(task: dict):
    class PublishTimeoutResult:
        status = "failed"
        published_url = None
        screenshot_path = None
        raw_artifact_path = task.get("raw_artifact_path")
        summary_artifact_path = task.get("summary_artifact_path")
        failure_reason = "发布 RPA 超时，请检查固定浏览器或人工打开草稿"
        notes = "未伪装发布成功，已保留已有草稿证据"

        @property
        def __dict__(self):
            return {
                "status": self.status,
                "published_url": self.published_url,
                "screenshot_path": self.screenshot_path,
                "raw_artifact_path": self.raw_artifact_path,
                "summary_artifact_path": self.summary_artifact_path,
                "failure_reason": self.failure_reason,
                "notes": self.notes,
            }

    return PublishTimeoutResult()


def _write_scorecard_artifact(brand: dict, summary: dict) -> str:
    artifact_dir = Path(settings.GEO_ARTIFACT_DIR) / "reports" / datetime.now().strftime("%Y%m%d")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"geo_scorecard_{brand['id']}_{datetime.now().strftime('%H%M%S')}.html"
    stats = summary.get("stats", {})
    publish_status = summary.get("publish_status", {})
    monitor_rows = "".join(
        f"<tr><td>{html.escape(str(item.get('platform') or ''))}</td>"
        f"<td>{html.escape(str(item.get('search_query') or ''))}</td>"
        f"<td>{html.escape(str(item.get('verification_status') or ''))}</td>"
        f"<td>{html.escape(str(item.get('source_domain') or ''))}</td></tr>"
        for item in summary.get("monitor_evidence", [])
    )
    publish_rows = "".join(
        f"<tr><td>{html.escape(str(item.get('platform') or ''))}</td>"
        f"<td>{html.escape(str(item.get('title') or ''))}</td>"
        f"<td>{html.escape(str(item.get('status') or ''))}</td>"
        f"<td>{html.escape(str(item.get('failure_reason') or ''))}</td></tr>"
        for item in summary.get("publish_tasks", [])
    )
    doc = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>{html.escape(brand['name'])} GEO成绩单</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif; margin: 32px; color: #111827; }}
h1 {{ margin-bottom: 8px; }}
.cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 20px 0; }}
.card {{ border: 1px solid #e5e7eb; border-radius: 8px; padding: 14px; }}
.card b {{ display: block; font-size: 26px; margin-top: 8px; }}
table {{ width: 100%; border-collapse: collapse; margin: 14px 0 24px; }}
th, td {{ border: 1px solid #e5e7eb; padding: 8px; text-align: left; font-size: 13px; }}
th {{ background: #f8fafc; }}
</style></head>
<body>
<h1>{html.escape(brand['name'])} GEO成绩单</h1>
<p>生成时间：{local_now()}</p>
<div class="cards">
  <div class="card">已验证引用率<b>{summary.get('verified_rate', 0)}%</b></div>
  <div class="card">来源证据率<b>{summary.get('evidence_rate', 0)}%</b></div>
  <div class="card">发布准备率<b>{summary.get('publish_ready_rate', 0)}%</b></div>
  <div class="card">内容资产<b>{stats.get('content_assets', 0)}</b></div>
</div>
<h2>发布状态</h2>
<p>{html.escape(json.dumps(publish_status, ensure_ascii=False))}</p>
<h2>最新发布任务</h2>
<table><thead><tr><th>平台</th><th>标题</th><th>状态</th><th>失败原因</th></tr></thead><tbody>{publish_rows}</tbody></table>
<h2>最新监测证据</h2>
<table><thead><tr><th>平台</th><th>问题</th><th>验证状态</th><th>来源域名</th></tr></thead><tbody>{monitor_rows}</tbody></table>
<h2>诊断</h2>
<ul>{"".join(f"<li>{html.escape(item)}</li>" for item in summary.get("diagnosis", []))}</ul>
</body></html>"""
    path.write_text(doc, encoding="utf-8")
    return str(path)
