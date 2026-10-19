"""UI 复查：抓每页 console 错误/警告 + 交互态抽查"""
import time
from playwright.sync_api import sync_playwright

BASE = "http://localhost:15173"
PAGES = ["/brands", "/keywords", "/monitor", "/dashboard", "/command-center", "/operations", "/settings"]

issues = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()

    console_msgs = []
    page.on("console", lambda m: console_msgs.append((m.type, m.text)))
    page.on("pageerror", lambda e: console_msgs.append(("pageerror", str(e))))

    # 登录页
    page.goto(f"{BASE}/login", wait_until="networkidle")
    time.sleep(1)
    login_msgs = [m for m in console_msgs if m[0] in ("error", "pageerror")]
    if login_msgs:
        issues.append(("login", login_msgs))
    console_msgs.clear()

    # 登录
    page.fill("input[placeholder='用户名']", "admin")
    page.fill("input[placeholder='密码']", "admin123")
    page.click("button.submit-btn")
    page.wait_for_url("**/brands", timeout=10000)
    time.sleep(1)

    for path in PAGES:
        console_msgs.clear()
        page.goto(f"{BASE}{path}", wait_until="networkidle")
        time.sleep(1.5)
        errs = [m for m in console_msgs if m[0] in ("error", "pageerror")]
        warns = [m for m in console_msgs if m[0] == "warning"]
        if errs:
            issues.append((path, errs))
        print(f"[{path}] errors={len(errs)} warnings={len(warns)}")
        for t, txt in errs:
            print(f"    ERROR: {txt[:200]}")

    # ---- 交互态抽查 ----
    print("\n--- 交互抽查 ---")
    # 1. 品牌管理: 打开新增品牌对话框
    page.goto(f"{BASE}/brands", wait_until="networkidle")
    time.sleep(1)
    btn = page.locator("button:has-text('新增品牌')")
    if btn.count():
        btn.first.click()
        time.sleep(0.8)
        dlg = page.locator(".el-dialog")
        print(f"新增品牌对话框可见: {dlg.is_visible()}")
        page.screenshot(path=r"ui_check_dialog.png")
        page.keyboard.press("Escape")
        time.sleep(0.5)

    # 2. 主按钮 hover 颜色检查（计算样式）
    page.goto(f"{BASE}/dashboard", wait_until="networkidle")
    time.sleep(1.5)
    primary_btn = page.locator("button.el-button--primary").first
    if primary_btn.count():
        bg = primary_btn.evaluate("el => getComputedStyle(el).backgroundImage")
        print(f"主按钮背景(渐变): {bg[:80]}")
        disabled_css = page.evaluate("""() => {
            const b = document.createElement('button');
            b.className = 'el-button el-button--primary is-disabled';
            document.body.appendChild(b);
            const s = getComputedStyle(b);
            const r = {bg: s.backgroundImage.slice(0,60), opacity: s.opacity, cursor: s.cursor};
            b.remove();
            return r;
        }""")
        print(f"禁用主按钮样式: {disabled_css}")

    # 3. 表格行 hover
    row = page.locator(".el-table__row").first
    if row.count():
        row.hover()
        time.sleep(0.4)
        bg = row.evaluate("el => getComputedStyle(el).backgroundColor")
        print(f"表格行hover背景: {bg}")

    browser.close()

print("\n=== 汇总 ===")
if not issues:
    print("所有页面无 console error/pageerror")
else:
    for path, errs in issues:
        print(f"{path}: {len(errs)} errors")
        for t, txt in errs:
            print(f"   {t}: {txt[:200]}")
