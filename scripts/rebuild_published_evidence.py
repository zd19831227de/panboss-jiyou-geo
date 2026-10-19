#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "geo_data.db"
sys.path.insert(0, str(ROOT / "backend"))

from app.database import local_now  # noqa: E402
from app.services.publishing_rpa import (  # noqa: E402
    _make_publish_summary,
    _new_page_from_cdp_context,
    _select_existing_page,
    _try_close_page,
    _try_screenshot,
    _visible_text,
    _save_artifact,
    open_publish_browser,
    verify_toutiao_published_page_with_browser,
)


LOGIN_MARKERS = ["登录", "扫码", "验证码", "安全验证", "请登录"]


def fetch_tasks(task_ids: list[int]) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in task_ids)
        rows = conn.execute(
            f"""
            SELECT pt.*, ca.title, ca.body, ca.target_keyword,
                   pa.account_name, pa.login_url, pa.publish_url, pa.port, pa.profile_dir
            FROM publishing_tasks pt
            LEFT JOIN content_assets ca ON ca.id = pt.content_id
            LEFT JOIN publishing_accounts pa ON pa.id = pt.account_id
            WHERE pt.id IN ({placeholders})
            ORDER BY pt.id
            """,
            task_ids,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def verify_text(platform: str, title: str, body: str, visible: str, html: str) -> tuple[bool, str]:
    title = (title or "").strip()
    body = (body or "").strip()
    plain_visible = re.sub(r"\s+", "", visible or "")
    plain_html = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", html or ""))
    title_plain = re.sub(r"\s+", "", title)
    title_clean = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", title_plain)
    # 标题探针按由长到短逐级降级：平台公开页常把长标题截断展示
    # （例如携程旅拍只显示标题前半段），只用 8 字以上的探针会漏判真实作品页。
    # 探针不低于 4 字，且仍需正文命中 ≥3 个词组，避免误认成别的页面。
    title_probes = []
    for probe in [title_plain, title_clean] + [title_clean[:size] for size in (16, 14, 12, 10, 8, 7, 6, 5, 4)]:
        if probe and len(probe) >= 4 and probe not in title_probes:
            title_probes.append(probe)
    title_ok = not title_probes or any(probe in plain_visible or probe in plain_html for probe in title_probes)
    if not title_ok:
        return False, "页面未匹配标题"

    body_plain = re.sub(r"\s+", "", body)
    body_terms = []
    for term in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}", body_plain):
        if len(body_terms) >= 8:
            break
        if term not in body_terms and term not in {title_clean, "GEO", "工作流", "测试"}:
            body_terms.append(term)
    matched = sum(1 for term in body_terms if term in plain_visible or term in plain_html)
    if body_terms and matched < min(3, len(body_terms)):
        return False, f"页面正文匹配不足:{matched}/{min(3, len(body_terms))}"

    # 公开文章页（知乎/搜狐/头条/百家号等）页头普遍常驻「登录/注册」入口，
    # 只要标题与正文都能在页面上命中，就说明抓到的是真实作品页而不是登录墙。
    # 因此登录/验证标记只在「可见正文过少」时才判为登录态失效，避免把真实
    # 已发布页面误判为失败，导致三件套永远重建不出来。
    marker = next((item for item in LOGIN_MARKERS if item in (visible or "")), "")
    if marker and len(plain_visible) < 200:
        return False, f"页面疑似登录态失效（命中“{marker}”且可见正文仅 {len(plain_visible)} 字）"
    return True, ""


def _rebuild_toutiao(task: dict) -> dict:
    verified = verify_toutiao_published_page_with_browser(
        task.get("account_name") or "default",
        (task.get("published_url") or "").strip(),
        task.get("title") or "",
        task.get("body") or "",
        task_id=task["id"],
    )
    if not verified.get("verified"):
        return {
            "task_id": task["id"],
            "status": "failed",
            "reason": verified.get("failure_reason") or "头条登录态验真失败",
        }
    raw = verified.get("raw_artifact_path")
    screenshot = verified.get("screenshot_path")
    summary = _make_publish_summary(task, task, raw, screenshot, "published")
    return {
        "task_id": task["id"],
        "status": "rebuilt",
        "screenshot": screenshot,
        "raw": raw,
        "summary": summary,
    }


def _rebuild_xiaohongshu(task: dict) -> dict:
    account_name = task.get("account_name") or "default"
    opened = open_publish_browser("xiaohongshu", account_name, "https://creator.xiaohongshu.com/new/home")
    if not opened.get("success"):
        return {"task_id": task["id"], "status": "failed", "reason": opened.get("notes") or "打开小红书浏览器失败"}

    from playwright.sync_api import sync_playwright

    title = (task.get("title") or "").strip()
    title_probe = re.sub(r"\s+", "", title)[:18]
    public_title_probe = title[:18]
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{opened['port']}", timeout=4000)
        try:
            context = browser.contexts[0] if browser.contexts else None
            page = _select_existing_page(context, "https://creator.xiaohongshu.com/new/home") if context else None
            page = page or _new_page_from_cdp_context(browser, opened["port"], "https://creator.xiaohongshu.com/new/home")[1]
            page.set_default_timeout(6000)
            page.goto("https://creator.xiaohongshu.com/new/home", wait_until="domcontentloaded", timeout=12000)
            page.wait_for_timeout(2500)
            visible = _visible_text(page)
            html = page.content()
            compact = re.sub(r"\s+", "", visible or "")
            if not title_probe or title_probe not in compact:
                _try_close_page(page)
                return {"task_id": task["id"], "status": "failed", "reason": "小红书创作者后台未匹配到目标笔记标题"}

            public_page = _new_page_from_cdp_context(browser, opened["port"], task.get("published_url") or "")[1]
            public_visible = ""
            public_html = ""
            try:
                public_page.set_default_timeout(6000)
                public_page.goto(task.get("published_url") or "", wait_until="domcontentloaded", timeout=12000)
                public_page.wait_for_timeout(2000)
                public_visible = _visible_text(public_page)
                public_html = public_page.content()
            except Exception:
                pass
            finally:
                _try_close_page(public_page)

            public_ok = (
                (public_title_probe and public_title_probe in (public_visible or ""))
                or (title_probe and title_probe in re.sub(r"\s+", "", public_html or ""))
            )
            if not public_ok:
                _try_close_page(page)
                return {"task_id": task["id"], "status": "failed", "reason": "小红书公开页未匹配到目标标题"}

            screenshot = _try_screenshot(task["id"], page)
            raw = _save_artifact(task["id"], "xiaohongshu_creator_reverified.html", html)
            summary = _make_publish_summary(task, task, raw, screenshot, "published")
            _try_close_page(page)
        finally:
            browser.close()

    if not (screenshot and raw and summary):
        return {"task_id": task["id"], "status": "failed", "reason": "小红书三件套生成不完整"}
    return {
        "task_id": task["id"],
        "status": "rebuilt",
        "screenshot": screenshot,
        "raw": raw,
        "summary": summary,
    }


def rebuild_one(task: dict) -> dict:
    platform = task["platform"]
    published_url = (task.get("published_url") or "").strip()
    if not published_url:
        return {"task_id": task["id"], "status": "skipped", "reason": "无 published_url"}
    if not task.get("account_name"):
        return {"task_id": task["id"], "status": "skipped", "reason": "无绑定账号"}

    if platform == "toutiao":
        result = _rebuild_toutiao(task)
    elif platform == "xiaohongshu":
        result = _rebuild_xiaohongshu(task)
    else:
        result = None
    if result is not None:
        if result.get("status") != "rebuilt":
            return result
        screenshot = result.get("screenshot")
        raw = result.get("raw")
        summary = result.get("summary")
    else:
        opened = open_publish_browser(platform, task["account_name"], task.get("login_url") or published_url)
        if not opened.get("success"):
            return {"task_id": task["id"], "status": "failed", "reason": opened.get("notes") or "打开浏览器失败"}

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{opened['port']}", timeout=4000)
            try:
                context = browser.contexts[0] if browser.contexts else None
                page = _select_existing_page(context, published_url) if context else None
                page = page or _new_page_from_cdp_context(browser, opened["port"], published_url)[1]
                page.set_default_timeout(6000)
                try:
                    page.goto(published_url, wait_until="domcontentloaded", timeout=12000)
                except Exception:
                    page.goto(published_url, wait_until="commit", timeout=8000)
                page.wait_for_timeout(2500)
                visible = _visible_text(page)
                html = page.content()
                ok, reason = verify_text(platform, task.get("title") or "", task.get("body") or "", visible, html)
                if not ok:
                    _try_close_page(page)
                    return {"task_id": task["id"], "status": "failed", "reason": reason, "visible": visible[:400]}

                screenshot = _try_screenshot(task["id"], page)
                raw = _save_artifact(task["id"], f"{platform}_reverified.html", html)
                summary = _make_publish_summary(task, task, raw, screenshot, "published")
                _try_close_page(page)
            finally:
                browser.close()

    if not (screenshot and raw and summary):
        return {"task_id": task["id"], "status": "failed", "reason": "三件套生成不完整"}

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            UPDATE publishing_tasks
            SET screenshot_path = ?, raw_artifact_path = ?, summary_artifact_path = ?,
                screenshot_verified = 1, raw_artifact_verified = 1, summary_artifact_verified = 1,
                evidence_status = 'verified', evidence_failure_reason = NULL,
                notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                screenshot,
                raw,
                summary,
                f"{platform} 已发布URL重新验真并重建三件套",
                local_now(),
                task["id"],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {"task_id": task["id"], "status": "rebuilt", "screenshot": screenshot, "raw": raw, "summary": summary}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", type=int, action="append", required=True)
    args = parser.parse_args()
    tasks = fetch_tasks(args.task_id)
    results = [rebuild_one(task) for task in tasks]
    failed = [item for item in results if item.get("status") != "rebuilt"]
    print(json.dumps({"status": "passed" if not failed else "failed", "results": results}, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
