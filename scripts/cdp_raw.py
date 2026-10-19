"""原生 CDP websocket 工具：按 URL 子串选标签页执行 JS（整数消息 id）。

用法:
  python scripts/cdp_raw.py <port> <tab_substr> "<js表达式>"
  python scripts/cdp_raw.py <port> <tab_substr> --shot <name.png>
JS 里可用 clickEl(selector) / shot() 不可用——仅表达式；点击请用
  document.querySelectorAll(sel)[i].click() 组合进表达式。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import websocket  # websocket-client


def main() -> int:
    port = sys.argv[1]
    substr = sys.argv[2].lower()
    js = sys.argv[3] if len(sys.argv) > 3 else "({href: location.href})"
    shot = None
    press_key = None
    focus_js = None
    if len(sys.argv) > 3 and sys.argv[3] == "--press":
        # --press <key> --focus-js <expr>：先在页面里执行 focus 表达式，
        # 再发 trusted 键盘事件（本机 CDP 鼠标事件会卡死，键盘可用）
        press_key = sys.argv[4] if len(sys.argv) > 4 else "Enter"
        if "--focus-js" in sys.argv:
            focus_js = sys.argv[sys.argv.index("--focus-js") + 1]
    elif js == "--shot":
        shot = sys.argv[4]

    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5))
    page_targets = [t for t in targets if t.get("type") == "page" and substr in (t.get("url") or "").lower()]
    if not page_targets:
        print(json.dumps({"error": "no tab matches", "tabs": [(t.get('url') or '')[:60] for t in targets if t.get('type') == 'page']}, ensure_ascii=False))
        return 1
    target = page_targets[0]
    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=20)

    def send(method: str, params: dict, msg_id: int):
        ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            raw = ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == msg_id:
                return msg

    if len(sys.argv) > 3 and sys.argv[3] == "--type":
        # --type <text> --focus-js <expr> [--enter]：focus 后全选清空并 trusted 输入文本
        text = sys.argv[4] if len(sys.argv) > 4 else ""
        focus_js = sys.argv[sys.argv.index("--focus-js") + 1] if "--focus-js" in sys.argv else None
        if focus_js:
            resp0 = send("Runtime.evaluate", {"expression": focus_js, "returnByValue": True, "awaitPromise": True}, 1)
            print(json.dumps({"focus": resp0.get("result", {}).get("result", {}).get("value")}, ensure_ascii=False))
        send("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": "Control", "code": "ControlLeft", "windowsVirtualKeyCode": 17, "nativeVirtualKeyCode": 17, "modifiers": 2}, 2)
        send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65, "modifiers": 2}, 3)
        send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65, "modifiers": 2}, 4)
        send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Control", "code": "ControlLeft", "windowsVirtualKeyCode": 17, "nativeVirtualKeyCode": 17}, 5)
        send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8}, 6)
        send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8}, 7)
        send("Input.insertText", {"text": text}, 8)
        if "--enter" in sys.argv:
            send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}, 9)
            send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}, 10)
        print(json.dumps({"typed": text}, ensure_ascii=False))
        ws.close()
        return 0

    if press_key:
        if focus_js:
            resp0 = send("Runtime.evaluate", {"expression": focus_js, "returnByValue": True, "awaitPromise": True}, 1)
            print(json.dumps({"focus": resp0.get("result", {}).get("result", {}).get("value")}, ensure_ascii=False))
        codes = {"Enter": (13, "Enter"), " ": (32, "Space"), "Space": (32, "Space")}
        vk, code = codes.get(press_key, (13, press_key))
        send("Input.dispatchKeyEvent", {"type": "keyDown", "key": press_key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}, 2)
        send("Input.dispatchKeyEvent", {"type": "keyUp", "key": press_key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}, 3)
        print(json.dumps({"pressed": press_key}, ensure_ascii=False))
        ws.close()
        return 0

    if shot:
        out_dir = Path(__file__).resolve().parents[1] / "runtime-data" / "artifacts" / "debug"
        out_dir.mkdir(parents=True, exist_ok=True)
        resp = send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}, 1)
        data = resp.get("result", {}).get("data")
        if data:
            import base64
            (out_dir / shot).write_bytes(base64.b64decode(data))
            print(json.dumps({"shot": str(out_dir / shot)}, ensure_ascii=False))
        else:
            print(json.dumps({"error": "capture failed", "resp": str(resp)[:200]}))
        ws.close()
        return 0

    resp = send("Runtime.evaluate", {"expression": js, "returnByValue": True, "awaitPromise": True}, 1)
    ws.close()
    result = resp.get("result", {})
    if "exceptionDetails" in result:
        print(json.dumps({"error": result["exceptionDetails"].get("exception", {}).get("description", str(result["exceptionDetails"]))[:300]}, ensure_ascii=False))
        return 1
    print(json.dumps(result.get("result", {}).get("value"), ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
