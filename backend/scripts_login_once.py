#!/usr/bin/env python3
"""
GEO运营系统 - AI平台登录工具 (v2)

一次性登录豆包/千问/元宝，登录态永久保存到 ~/.geo-ops-browser-data
之后系统自动监测会复用此登录态，无需再次登录。

用法:
    python3 scripts_login_once.py                    # 登录所有平台
    python3 scripts_login_once.py doubao             # 只登录豆包
    python3 scripts_login_once.py yuanbao            # 只登录元宝
    python3 scripts_login_once.py qianwen            # 只登录千问
"""
import os, sys, time, json

USER_DATA_DIR = os.path.expanduser("~/.geo-ops-browser-data")

PLATFORMS = {
    "doubao": ("豆包", "https://www.doubao.com/chat/"),
    "yuanbao": ("元宝", "https://yuanbao.tencent.com/chat"),
    "qianwen": ("千问", "https://www.qianwen.com/"),
}


def login_platform(platform_key: str):
    """Open a single platform for manual login"""
    if platform_key not in PLATFORMS:
        print(f"❌ 未知平台: {platform_key}")
        print(f"   可用平台: {', '.join(PLATFORMS.keys())}")
        return False
    
    name, url = PLATFORMS[platform_key]
    print(f"\n{'='*60}")
    print(f"🔑 正在打开 {name} ({url})")
    print(f"{'='*60}")
    print("\n请在打开的 Chrome 窗口中完成登录")
    print("登录完成后关闭 Chrome 窗口即可")
    
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("\n正在安装 Playwright...")
        os.system("pip3 install playwright")
        os.system("playwright install chromium")
        from playwright.sync_api import sync_playwright
    
    chrome_path = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
    if not os.path.exists(chrome_path):
        print(f"❌ 未找到 Google Chrome: {chrome_path}")
        return False
    
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR,
                executable_path=chrome_path,
                headless=False,
                args=['--no-sandbox', '--disable-setuid-sandbox'],
                viewport={'width': 1280, 'height': 900},
                locale='zh-CN',
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded")
            print(f"\n✅ {name} 页面已打开，请登录后关闭浏览器")
            context.close()  # 用户关闭窗口后 context 会结束
            print(f"✅ {name} 登录完成，登录态已保存")
            return True
        except Exception as e:
            print(f"❌ {name} 登录失败: {e}")
            return False


def main():
    print("=" * 60)
    print("GEO运营系统 - AI平台一次性登录工具 v2")
    print("=" * 60)
    print(f"\n📁 用户数据目录: {USER_DATA_DIR}")
    print("\n📋 说明：")
    print("1. 脚本会依次打开各AI平台的 Chrome 窗口")
    print("2. 在每个窗口中手动完成登录")
    print("3. 登录完成后关闭浏览器窗口")
    print("4. 登录信息永久保存，后续系统自动使用")
    
    # Determine which platforms to login
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(PLATFORMS.keys())
    
    # Filter valid platforms
    valid_targets = [t for t in targets if t in PLATFORMS]
    if not valid_targets:
        print(f"\n❌ 没有有效的平台。可用: {', '.join(PLATFORMS.keys())}")
        sys.exit(1)
    
    print(f"\n🎯 本次登录平台: {', '.join(PLATFORMS[t][0] for t in valid_targets)}")
    input("\n按 Enter 开始...")
    
    success_count = 0
    for platform_key in valid_targets:
        if login_platform(platform_key):
            success_count += 1
    
    print(f"\n{'='*60}")
    print(f"✅ 登录完成: {success_count}/{len(valid_targets)} 个平台")
    print(f"📁 登录态已保存到: {USER_DATA_DIR}")
    print("现在可以启动GEO运营系统进行全平台监测了！")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
