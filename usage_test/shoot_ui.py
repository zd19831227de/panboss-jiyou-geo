"""临时截图脚本：验证 UI 美化后的实际渲染效果"""
import sys, time
from playwright.sync_api import sync_playwright

BASE = "http://localhost:15173"
OUT = "."

def shot(page, name, full=True):
    p = f"{OUT}\\ui_{name}.png"
    page.screenshot(path=p, full_page=full)
    print(f"[saved] {p}")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
    page = ctx.new_page()

    # 登录页
    page.goto(f"{BASE}/login", wait_until="networkidle")
    time.sleep(1.2)
    shot(page, "01_login", full=False)

    # 登录
    page.fill("input[placeholder='用户名']", "admin")
    page.fill("input[placeholder='密码']", "admin123")
    page.click("button.submit-btn")
    page.wait_for_url("**/brands", timeout=10000)
    time.sleep(1.5)

    # 各页面
    for path, name in [("/brands","02_brands"),("/keywords","03_keywords"),
                       ("/monitor","04_monitor"),("/dashboard","05_dashboard"),
                       ("/command-center","06_command"),("/operations","07_operations"),
                       ("/settings","08_settings")]:
        page.goto(f"{BASE}{path}", wait_until="networkidle")
        time.sleep(1.5)
        shot(page, name, full=False)

    browser.close()
print("done")
