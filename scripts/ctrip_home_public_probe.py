#!/usr/bin/env python3
import json
from playwright.sync_api import sync_playwright


def main() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9422", timeout=5000)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(5000)
        page.set_default_navigation_timeout(12000)
        page.goto("https://we.ctrip.com/home/home", wait_until="domcontentloaded", timeout=12000)
        page.wait_for_timeout(1500)
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight * 0.45)")
        page.wait_for_timeout(800)
        result = {"steps": [], "pages_before": [p.url for p in context.pages]}
        try:
            page.get_by_text("查看详情", exact=True).first.click(timeout=5000, force=True)
            page.wait_for_timeout(1800)
            result["steps"].append({"clicked": "查看详情", "url": page.url, "title": page.title()})
        except Exception as exc:
            result["steps"].append({"error": f"查看详情失败: {exc}"[:200], "url": page.url})
        try:
            detail_page = next((p for p in context.pages if "analyse/singleDetail" in (p.url or "")), page)
            detail_page.bring_to_front()
            detail_page.wait_for_timeout(1000)
            title = detail_page.get_by_text("亲子游玩法推荐", exact=False).first
            box = title.bounding_box()
            if box:
                detail_page.mouse.click(box["x"] + box["width"] + 24, box["y"] + box["height"] / 2)
            else:
                title.click(timeout=5000, force=True)
            detail_page.wait_for_timeout(2500)
            result["steps"].append({"clicked": "标题右箭头/标题区", "url": detail_page.url, "title": detail_page.title()})
        except Exception as exc:
            result["steps"].append({"error": f"右箭头失败: {exc}"[:200], "url": page.url})
        result["pages_after"] = [{"url": p.url, "title": p.title()} for p in context.pages]
        result["text"] = (page.locator("body").inner_text(timeout=3000) or "")[:3000]
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
