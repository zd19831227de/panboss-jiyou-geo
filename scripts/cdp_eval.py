"""发布浏览器 CDP 快捷操作工具（供发布收尾/调试用）。

用法:
  python scripts/cdp_eval.py <port> --pages
      列出该发布浏览器所有标签页（url/title/active）
  python scripts/cdp_eval.py <port> --tab <substr> --eval "<js>"
      在 URL 含 substr 的第一个标签页执行 JS，打印 JSON 结果
  python scripts/cdp_eval.py <port> --tab <substr> --click "<selector-or-js>"
      JS click（不走 Input.dispatchMouseEvent，本机 CDP 鼠标事件会卡死）
  python scripts/cdp_eval.py <port> --tab <substr> --shot <name.png>
      全页截图存 runtime-data/artifacts/debug/
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 2:
        print(__doc__)
        return 2
    port = args[0]
    mode = args[1]

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=5000)
        context = browser.contexts[0] if browser.contexts else None
        pages = context.pages if context else []

        if mode == "--pages":
            for i, p in enumerate(pages):
                print(json.dumps({"i": i, "url": p.url, "title": p.title()}, ensure_ascii=False))
            return 0

        tab_substr, js, shot, new_url = "", None, None, None
        rest = args[2:]
        it = iter(rest)
        for tok in it:
            if tok == "--tab":
                tab_substr = next(it, "")
            elif tok == "--eval":
                js = next(it, "")
            elif tok == "--new":
                new_url = next(it, "")
            elif tok in ("--click",):
                selector = next(it, "")
                js = (
                    "(() => {const els = document.querySelectorAll("
                    + json.dumps(selector)
                    + "); if (!els.length) return {clicked: 0}; const el = els[els.length-1]; el.click();"
                    " return {clicked: els.length, text: (el.innerText||'').slice(0,40)};})()"
                )
            elif tok == "--shot":
                shot = next(it, "")
        if not js and not shot:
            print("nothing to do: pass --eval / --click / --shot")
            return 2

        page = None
        if new_url:
            page = context.new_page()
            page.goto(new_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
        else:
            for p in pages:
                if tab_substr.lower() in p.url.lower():
                    page = p
                    break
            if page is None and pages:
                page = pages[0]
        if page is None:
            print("no tabs")
            return 1

        if shot:
            out_dir = Path(__file__).resolve().parents[1] / "runtime-data" / "artifacts" / "debug"
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / shot
            page.screenshot(path=str(out), full_page=True)
            print(json.dumps({"shot": str(out)}, ensure_ascii=False))
        if js:
            try:
                result = page.evaluate(js)
                print(json.dumps(result, ensure_ascii=False, default=str))
            except Exception as exc:
                print(json.dumps({"error": str(exc)}, ensure_ascii=False))
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
