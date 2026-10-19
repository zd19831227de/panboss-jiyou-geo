"""补充复查：td hover 色 + dashboard warning 内容 + 禁用按钮实际观感截图"""
import time
from playwright.sync_api import sync_playwright

BASE = "http://localhost:15173"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()

    msgs = []
    page.on("console", lambda m: msgs.append((m.type, m.text)))

    page.goto(f"{BASE}/login", wait_until="networkidle")
    page.fill("input[placeholder='用户名']", "admin")
    page.fill("input[placeholder='密码']", "admin123")
    page.click("button.submit-btn")
    page.wait_for_url("**/brands", timeout=10000)
    time.sleep(1)

    # dashboard warnings
    msgs.clear()
    page.goto(f"{BASE}/dashboard", wait_until="networkidle")
    time.sleep(2)
    print("--- dashboard console warnings ---")
    for t, txt in msgs:
        if t == "warning":
            print(f"  {txt[:300]}")

    # td hover 背景
    page.goto(f"{BASE}/brands", wait_until="networkidle")
    time.sleep(1.5)
    row = page.locator(".el-table__row").first
    row.hover()
    time.sleep(0.4)
    td_bg = page.locator(".el-table__row td").first.evaluate("el => getComputedStyle(el).backgroundColor")
    print(f"\ntd hover 背景色: {td_bg}")

    # 关键词页: 表格 + 分页圆角 + 复选框截图(放大操作列)
    page.goto(f"{BASE}/keywords", wait_until="networkidle")
    time.sleep(1.5)
    page.locator(".el-table__row").first.hover()
    time.sleep(0.3)
    page.screenshot(path=r"ui_check_hover.png", clip={"x": 150, "y": 100, "width": 1290, "height": 500})
    print("hover 截图已存")

    browser.close()
print("done")
