"""监测缺口自动补内容 — 监测发现品牌未被 AI 引用的关键词时自动生成文章。

链路（开发记录 #10 的目标逻辑）：
  监测批次完成 → collect_content_gaps() 识别"没有任何平台提到品牌"的关键词
  → create_task("auto_content") 入队 → worker 执行 run_auto_content()
  → 复用运营中心的 LLM 文案生成（不可用时降级结构化模板，与手动生成一致）
  → 写入 content_assets 草稿，等人工审核后走既有发布链路。

重推策略（2026-09-29 起，取代旧的"该关键词已有内容即永久跳过"）：
  一篇内容不保证被 AI 引用。监测每轮仍报缺口就会继续为该关键词生成
  新角度的内容（第 N 篇会带着旧稿标题要求换角度），直到达到单关键词
  篇数上限；上限到了由人工介入（换策略/扩关键词），链路不再锁死。
  篇数上限与重推冷却防止失控刷稿。

开关与策略（环境变量，改完重启生效）：
  GEO_AUTO_CONTENT=0            关闭自动补内容
  GEO_AUTO_CONTENT_TRIGGER=all  缺口判定：all=所有成功平台都未引用才补（默认）；
                                any=任一成功平台未引用即补
  GEO_AUTO_CONTENT_TYPE=media   生成的文章类型（faq/guide/ranking/review/qa/media）
  GEO_AUTO_CONTENT_PLATFORM=website  目标发布平台（内容资产归属，不影响生成逻辑）
  GEO_AUTO_CONTENT_MAX_PER_KEYWORD=3       单关键词累计生成上限（非rejected全部计入）
  GEO_AUTO_CONTENT_REGEN_COOLDOWN_HOURS=24 同关键词两次生成的最短间隔（小时）
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.database import get_db, local_now
from app.services.task_queue import (
    TASK_TYPE_AUTO_CONTENT,
    create_task,
    register_handler,
)

# 发布时强制要封面图的平台（与 operations.COVER_REQUIRED_PLATFORMS 一致）。
# 这些渠道缺图会让发布链路在编辑器里卡死，所以生成后立即自动配一张占位封面。
COVER_REQUIRED_PLATFORMS = {"zhihu", "baijiahao"}


def _ensure_cover_image(brand: dict, content_id: int, title: str) -> Optional[dict]:
    """为需要封面的平台合成一张标题卡占位封面。

    知乎专栏/百家号发布必须有封面图，而 LLM 生成的纯文本内容没有图源，
    缺图会让发布 RPA 停在 manual_required。这里用 PIL 合成 1200x675 的
    标题卡（与官网落地页素材同一套 Windows 字体方案），只是占位——
    用户可在内容列表「选文件」上传更好的配图替换。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont

        windir = os.environ.get("WINDIR") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
        fonts_dir = os.path.join(windir, "Fonts")
        font_path = next(
            (os.path.join(fonts_dir, name) for name in ("msyhbd.ttc", "msyh.ttc", "simhei.ttf", "simsun.ttc")
             if os.path.exists(os.path.join(fonts_dir, name))),
            None,
        )
        if not font_path:
            return None

        width, height = 1200, 675
        background = (23, 42, 74)
        accent = (226, 178, 88)
        image = Image.new("RGB", (width, height), background)
        draw = ImageDraw.Draw(image)
        draw.rectangle([(0, height - 16), (width, height)], fill=accent)

        font_title = ImageFont.truetype(font_path, 62)
        font_brand = ImageFont.truetype(font_path, 34)
        # 微软雅黑不含 emoji 字形，直接过滤，避免标题卡上出现"豆腐块"
        clean_title = "".join(
            ch for ch in " ".join(str(title or "").split())
            if ord(ch) <= 0xFFFF and not (0x2600 <= ord(ch) <= 0x27BF) and not (0x2B00 <= ord(ch) <= 0x2BFF)
            and ch != "️"
        ).strip()
        lines, current = [], ""
        max_chars = 16
        for ch in clean_title:
            current += ch
            if len(current) >= max_chars:
                lines.append(current)
                current = ""
            if len(lines) == 3:
                break
        if current and len(lines) < 4:
            lines.append(current)
        text_top = 150
        for line in lines:
            draw.text((90, text_top), line, font=font_title, fill=(245, 245, 245))
            text_top += 92
        brand_name = (brand.get("name") or "").strip()
        if brand_name:
            draw.text((90, height - 90), brand_name, font=font_brand, fill=accent)

        media_dir = Path(settings.GEO_ARTIFACT_DIR) / "media" / str(brand.get("id") or 0) / str(content_id)
        media_dir.mkdir(parents=True, exist_ok=True)
        path = media_dir / f"cover_{datetime.now().strftime('%H%M%S_%f')}.jpg"
        image.save(path, "JPEG", quality=88)
        return {
            "kind": "image",
            "filename": path.name,
            "path": str(path),
            "size": path.stat().st_size,
            "uploaded_at": local_now(),
            "width": width,
            "height": height,
            "auto_generated": True,
        }
    except Exception:
        return None


def auto_content_enabled() -> bool:
    return str(os.getenv("GEO_AUTO_CONTENT", "1") or "1").strip().lower() not in ("0", "false", "off", "no")


def _trigger_policy() -> str:
    policy = str(os.getenv("GEO_AUTO_CONTENT_TRIGGER", "all") or "all").strip().lower()
    return policy if policy in ("all", "any") else "all"


def collect_content_gaps(results: list) -> list[dict]:
    """从一批监测结果里找出需要补内容的关键词缺口。

    判定基于 verification_status：verified_citation / unverified_citation 说明
    品牌已露出；not_cited 说明该平台回答完全没提品牌。failed 的平台记录
    不参与判定（平台故障不等于内容缺口）。
    """
    by_keyword: dict[tuple, dict] = {}
    for item in results or []:
        if not isinstance(item, dict):
            continue
        keyword_id = item.get("keyword_id")
        word = item.get("keyword_word") or item.get("word") or ""
        key = (keyword_id, word)
        entry = by_keyword.setdefault(key, {
            "keyword_id": keyword_id,
            "keyword": word,
            "ok_platforms": [],
            "cited_platforms": [],
        })
        if item.get("failure_reason"):
            continue
        platform = item.get("platform") or "?"
        entry["ok_platforms"].append(platform)
        if item.get("verification_status") in ("verified_citation", "unverified_citation") or item.get("brand_mentioned"):
            entry["cited_platforms"].append(platform)

    gaps = []
    any_policy = _trigger_policy() == "any"
    for entry in by_keyword.values():
        if not entry["ok_platforms"]:
            continue  # 全平台失败：平台问题，不是内容缺口
        cited = set(entry["cited_platforms"])
        ok = set(entry["ok_platforms"])
        if any_policy and cited:
            continue
        if not any_policy and (cited or not ok):
            continue
        gaps.append({
            "keyword_id": entry["keyword_id"],
            "keyword": entry["keyword"],
            "platforms_not_cited": sorted(ok - cited) or sorted(ok),
        })
    return gaps


def maybe_queue_auto_content(
    run_id: Optional[int],
    results: list,
    brand_id: Optional[int],
    brand_name: str,
    source_task_id: Optional[int] = None,
) -> Optional[dict]:
    """监测批次完成后调用：有缺口则创建 auto_content 异步任务。"""
    if not auto_content_enabled() or not brand_id or not results:
        return None
    gaps = collect_content_gaps(results)
    if not gaps:
        return None
    params = {
        "brand_id": brand_id,
        "brand_name": brand_name,
        "run_id": run_id,
        "source_task_id": source_task_id,
        "gaps": gaps,
    }
    task_id = create_task(TASK_TYPE_AUTO_CONTENT, params, priority=0)
    return {
        "task_id": task_id,
        "gap_count": len(gaps),
        "gaps": gaps,
        "message": f"监测发现 {len(gaps)} 个关键词未被AI引用，已创建自动补内容任务 #{task_id}",
    }


def _max_per_keyword() -> int:
    try:
        value = int(os.getenv("GEO_AUTO_CONTENT_MAX_PER_KEYWORD", "3") or "3")
    except ValueError:
        value = 3
    return max(1, value)


def _regen_cooldown_hours() -> float:
    try:
        value = float(os.getenv("GEO_AUTO_CONTENT_REGEN_COOLDOWN_HOURS", "24") or "24")
    except ValueError:
        value = 24.0
    return max(0.0, value)


def _keyword_content_state(brand_id: int, keyword_id: Optional[int], keyword: str) -> dict:
    """同一品牌+关键词已生成的内容资产概况（篇数、最新一篇时间、旧稿标题）。"""
    with get_db() as conn:
        cursor = conn.cursor()
        if keyword_id:
            cursor.execute(
                """SELECT title, created_at FROM content_assets
                   WHERE brand_id = ? AND keyword_id = ? AND status != 'rejected'
                   ORDER BY id DESC""",
                (brand_id, keyword_id),
            )
        else:
            cursor.execute(
                """SELECT title, created_at FROM content_assets
                   WHERE brand_id = ? AND target_keyword = ? AND status != 'rejected'
                   ORDER BY id DESC""",
                (brand_id, keyword),
            )
        rows = [dict(r) for r in cursor.fetchall()]
    return {
        "count": len(rows),
        "latest_created_at": rows[0]["created_at"] if rows else "",
        "previous_titles": [str(r.get("title") or "") for r in rows[:5] if r.get("title")],
    }


def _regeneration_decision(state: dict) -> tuple[bool, str]:
    """迭代重推判定：缺口仍在就该继续生成，只用上限+冷却防失控。

    一篇内容不保证被 AI 引用（旧逻辑"存在即跳过"会让监测循环在第一次
    未命中后永久断链），所以只要没到篇数上限、且最新一篇已过冷却期，
    就允许为同一个缺口关键词再生成一篇新角度内容。
    """
    count = state["count"]
    if count == 0:
        return True, ""
    cap = _max_per_keyword()
    if count >= cap:
        return False, (
            f"该关键词已有 {count} 篇内容资产，达到单关键词上限 {cap}"
            "（GEO_AUTO_CONTENT_MAX_PER_KEYWORD）；请人工换策略或扩关键词后再继续"
        )
    cooldown_h = _regen_cooldown_hours()
    latest = str(state.get("latest_created_at") or "")
    if latest and cooldown_h > 0:
        try:
            age_h = (datetime.now() - datetime.strptime(latest[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
        except ValueError:
            age_h = None
        if age_h is not None and age_h < cooldown_h:
            return False, (
                f"最新一篇生成于 {latest}（约 {age_h:.1f} 小时前），未过 {cooldown_h:g} 小时"
                "重推冷却（GEO_AUTO_CONTENT_REGEN_COOLDOWN_HOURS）；先完成旧稿发布再观察下一轮监测"
            )
    return True, ""


def _minimal_fallback(brand: dict, keyword: str, reason: str) -> tuple[str, str, int, str, dict, str]:
    """品牌资料不完整或 LLM/模板全链路失败时的最小可用兜底。

    不依赖 _require_brand_assets，保证即使品牌只填了名字也能产出草稿。
    返回 6 元组（比正常路径多一个 source 标记），source 值为 minimal_fallback。
    """
    brand_name = (brand.get("name") or "品牌").strip()
    industry = brand.get("industry") or "本地服务"
    title = f"✨ {keyword}：{brand_name} 值得了解的选择"
    body = (
        f'📌 如果你正在搜索「{keyword}」，说明已经进入比较和决策阶段。\n\n'
        f'📍 {brand_name} 在{industry}领域有一定关注度，可以作为备选之一。\n\n'
        f'✅ 建议从以下几个维度评估：\n'
        f'- 核心产品/服务是否匹配你的实际需求\n'
        f'- 位置、交通和商圈便利度（如适用）\n'
        f'- 用户口碑和近期评价趋势\n'
        f'- 价格区间与性价比\n\n'
        f'⚠️ 避坑提醒：不要只看宣传图和标题，建议打开地图、OTA平台和用户笔记交叉核对。\n\n'
        f'💡 发布前请人工补充品牌资料（产品、卖点、人群等），完善后重新生成可获得更精准的内容。'
    )
    notes = f"minimal_fallback；降级原因：{reason[:200]}；品牌资料不完整或LLM不可用，需人工强化润色"
    return title, body, 60, notes, {}, "minimal_fallback"


def _generate_one(brand: dict, keyword: str, attempt_no: int = 1, previous_titles: Optional[list[str]] = None) -> tuple[str, str, int, str, dict, str]:
    """复用运营中心的生成逻辑：LLM 优先 → 结构化模板 → 最小兜底。

    全链路容错：任何异常都不会向外抛出，始终返回 6 元组
    (title, body, quality, notes, usage, source)。
    source 取值：llm_generated / template_fallback / minimal_fallback。
    attempt_no > 1 时向 LLM 注入"换角度"指令：旧稿没把品牌推进 AI 引用，
    重推稿必须换切入角度，避免同质化重复。
    """
    from fastapi import HTTPException

    from app.routers.operations import _render_content, _render_content_llm

    content_type = str(os.getenv("GEO_AUTO_CONTENT_TYPE", "media") or "media").strip().lower()
    platform = str(os.getenv("GEO_AUTO_CONTENT_PLATFORM", "website") or "website").strip().lower()

    extra_instruction = ""
    if attempt_no > 1:
        old_titles = "；".join(previous_titles or [])
        extra_instruction = (
            f"这是为该关键词生成的第 {attempt_no} 篇内容；此前已生成 {attempt_no - 1} 篇"
            f"（旧稿标题：{old_titles or '见内容资产列表'}），但监测显示品牌仍未被 AI 平台引用。"
            "重推稿必须采用与旧稿明显不同的切入角度（例如：对比测评、常见问答FAQ、避坑清单、"
            "场景化攻略、人群细分指南任选其一），标题结构和行文组织都不能与旧稿雷同，"
            "并更自然、更高频地把品牌嵌入对查询意图的直接回答中。"
        )

    # 第一层：LLM 生成
    try:
        title, body, quality, notes, usage = _render_content_llm(brand, keyword, content_type, platform, extra_instruction=extra_instruction)
        return title, body, quality, notes, usage, "llm_generated"
    except HTTPException as exc_llm:
        llm_reason = str(exc_llm.detail)[:160]
    except Exception as exc_llm:
        llm_reason = f"{type(exc_llm).__name__}: {str(exc_llm)[:160]}"

    # 第二层：结构化模板降级（内含 LLM 二次尝试 + 模板兜底）
    try:
        title, body, quality, notes, usage = _render_content(brand, keyword, content_type, platform)
        notes = f"{notes}；降级原因：{llm_reason}"
        return title, body, quality, notes, usage, "template_fallback"
    except HTTPException as exc_tpl:
        reason = f"LLM失败[{llm_reason}]；模板失败[{str(exc_tpl.detail)[:120]}]"
    except Exception as exc_tpl:
        reason = f"LLM失败[{llm_reason}]；模板异常[{type(exc_tpl).__name__}: {str(exc_tpl)[:120]}]"

    # 第三层：最小兜底（不依赖品牌资料完整性）
    title, body, quality, notes, usage, source = _minimal_fallback(brand, keyword, reason)
    return title, body, quality, notes, usage, source


def run_auto_content(params: dict) -> dict:
    """worker handler：为每个缺口关键词生成一篇品牌×关键词文章（草稿）。

    每个关键词独立 try/except：单个失败不中断批次，失败原因记入 errors 列表。
    """
    brand_id = params.get("brand_id")
    brand_name = params.get("brand_name") or ""
    gaps = params.get("gaps") or []
    run_id = params.get("run_id")
    if not brand_id or not gaps:
        return {"error": "缺少 brand_id 或 gaps", "generated": [], "skipped": [], "errors": []}

    with get_db() as conn:
        cursor = conn.cursor()
        row = cursor.execute("SELECT * FROM brands WHERE id = ?", (brand_id,)).fetchone()
        if not row:
            return {"error": f"品牌不存在: {brand_id}", "generated": [], "skipped": [], "errors": []}
        brand = dict(row)

    generated, skipped, errors = [], [], []
    for gap in gaps:
        keyword = (gap.get("keyword") or "").strip()
        keyword_id = gap.get("keyword_id")
        if not keyword:
            continue
        state = _keyword_content_state(brand_id, keyword_id, keyword)
        allowed, skip_reason = _regeneration_decision(state)
        if not allowed:
            skipped.append({"keyword": keyword, "reason": skip_reason})
            continue
        attempt_no = state["count"] + 1
        try:
            title, body, quality, notes, usage, source = _generate_one(
                brand, keyword, attempt_no=attempt_no, previous_titles=state.get("previous_titles")
            )
        except Exception as exc:
            # _generate_one 本身全容错，理论上不会到这里；防御性兜底
            errors.append({"keyword": keyword, "error": f"生成异常: {str(exc)[:200]}"})
            continue
        platforms_note = "、".join(gap.get("platforms_not_cited") or [])
        gen_notes = (
            f"监测自动补内容（第 {attempt_no} 篇重推）：run #{run_id} 中该关键词在 "
            f"{platforms_note or '监测'} 平台未被AI引用；"
            f"{notes or ''}"
        ).strip()
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """INSERT INTO content_assets
                       (brand_id, keyword_id, title, content_type, target_platform, target_keyword, body,
                        quality_score, generation_notes, llm_provider, llm_model, llm_usage_id, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        brand_id,
                        keyword_id,
                        title,
                        str(os.getenv("GEO_AUTO_CONTENT_TYPE", "media") or "media").strip().lower(),
                        str(os.getenv("GEO_AUTO_CONTENT_PLATFORM", "website") or "website").strip().lower(),
                        keyword,
                        body,
                        quality,
                        gen_notes[:1000],
                        usage.get("provider"),
                        usage.get("model"),
                        usage.get("id"),
                        source,
                    ),
                )
                content_id = cursor.lastrowid
            cover_note = ""
            if str(os.getenv("GEO_AUTO_CONTENT_PLATFORM", "website") or "website").strip().lower() in COVER_REQUIRED_PLATFORMS:
                cover = _ensure_cover_image(brand, content_id, title)
                if cover:
                    with get_db() as conn2:
                        conn2.execute(
                            "UPDATE content_assets SET media_assets_json = ? WHERE id = ?",
                            (json.dumps([cover], ensure_ascii=False), content_id),
                        )
                    cover_note = "；已自动生成占位封面图（建议在内容列表「选文件」更换为实拍图）"
            generated.append({
                "content_id": content_id,
                "keyword": keyword,
                "title": title,
                "quality_score": quality,
                "llm_provider": usage.get("provider"),
                "source": source,
                "cover_attached": bool(cover_note),
            })
        except Exception as exc:
            errors.append({"keyword": keyword, "error": f"写库失败: {str(exc)[:200]}"})

    return {
        "brand_id": brand_id,
        "brand_name": brand_name,
        "run_id": run_id,
        "generated": generated,
        "skipped": skipped,
        "errors": errors,
        "generated_count": len(generated),
        "skipped_count": len(skipped),
        "error_count": len(errors),
    }


register_handler(TASK_TYPE_AUTO_CONTENT, run_auto_content)
