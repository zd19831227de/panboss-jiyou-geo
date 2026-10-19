import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from app.core.runtime_paths import screenshots_dir

SCREENSHOT_DIR = screenshots_dir() / "ai_monitoring"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


class DoubaoMonitor:
    """Doubao CDP browser automation for AI monitoring"""

    def __init__(self, cdp_port: int = 9229):
        self.cdp_port = cdp_port
        self.browser = None

    def connect(self):
        """Connect to Doubao browser via direct CDP WebSocket"""
        try:
            import json as _json
            import websocket as _ws
            # 获取当前活动的CDP WebSocket URL
            import requests
            tabs = requests.get(f"http://localhost:{self.cdp_port}/json", timeout=5).json()
            ws_url = None
            for t in tabs:
                if "webSocketDebuggerUrl" in t:
                    ws_url = t.get("webSocketDebuggerUrl")
                    break
            if not ws_url:
                raise Exception("No CDP endpoint found")
            self._ws_url = ws_url
            self._ws = _ws.create_connection(ws_url, timeout=10)
            return True
        except Exception as e:
            print(f"Failed to connect to Doubao: {e}")
            return False

    def _send_cdp(self, method, params=None):
        """Send CDP command and return result"""
        import json as _json
        msg_id = 1
        self._ws.send(_json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            resp = _json.loads(self._ws.recv())
            if resp.get("id") == msg_id:
                return resp.get("result", {})

    def disconnect(self):
        """Disconnect from browser"""
        if hasattr(self, "_ws") and self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def run_search(self, brand_name: str, keyword: str, platform: str = "doubao") -> dict:
        """Run a search on Doubao AI chatbot via direct CDP - types into textarea and presses Enter"""
        result = {
            "is_cited": False,
            "cited_sources": None,
            "screenshot_path": None,
            "notes": None
        }

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = SCREENSHOT_DIR / f"{brand_name}_{keyword}_{platform}_{timestamp}.png"

        try:
            import time

            # Connect if not already connected
            if not hasattr(self, "_ws") or not self._ws:
                if not self.connect():
                    result["notes"] = "Failed to connect to Doubao browser"
                    return result

            # Use existing tab (first tab with doubao.com/chat/)
            import requests as _req
            tabs = _req.get(f"http://localhost:{self.cdp_port}/json", timeout=5).json()
            target_id = None
            for t in tabs:
                if "/chat/" in t.get("url", "") and "login" not in t.get("url", ""):
                    target_id = t.get("id")
                    break
            if not target_id:
                target_id = tabs[0].get("id") if tabs else None
            if not target_id:
                result["notes"] = "No Doubao tab found"
                return result

            # Attach to tab
            import json as _json
            ws_url = f"ws://localhost:{self.cdp_port}/devtools/page/{target_id}"
            tab_ws = __import__("websocket").create_connection(ws_url, timeout=15)
            tab_ws.settimeout(20)

            def tab_cdp(method, params=None):
                msg_id = 10
                tab_ws.send(_json.dumps({"id": msg_id, "method": method, "params": params or {}}))
                deadline = time.time() + 15
                while time.time() < deadline:
                    resp = _json.loads(tab_ws.recv())
                    if resp.get("id") == msg_id:
                        return resp.get("result", {})
                return {}

            # Enable domains
            tab_cdp("Page.enable")
            tab_cdp("Runtime.enable")

            # Inject search query into textarea (React controlled component)
            search_query = f"{keyword} 品牌 {brand_name}"
            # 使用 json.dumps 安全序列化，避免 search_query 含单引号/反斜杠
            # 时破坏 JS 字符串字面量（原 f-string 直接拼接 '{search_query}'）。
            import json as _json
            search_query_js = _json.dumps(search_query)
            inject_script = f"""
            () => {{
                const ta = document.querySelector('.semi-input-textarea');
                if (!ta) return 'NO TEXTAREA';
                const nativeSetter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
                nativeSetter.call(ta, {search_query_js});
                ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
                ta.dispatchEvent(new Event('change', {{ bubbles: true }}));
                // Send by Enter
                ta.dispatchEvent(new KeyboardEvent('keydown', {{ bubbles: true, key: 'Enter', keyCode: 13 }}));
                ta.dispatchEvent(new KeyboardEvent('keyup', {{ bubbles: true, key: 'Enter', keyCode: 13 }}));
                return 'SENT: ' + ta.value;
            }}
            """
            inject_result = tab_cdp("Runtime.evaluate", {"expression": inject_script, "returnByValue": True})
            print(f"Inject result: {inject_result}")

            # Wait for AI response (豆包生成需要时间)
            print(f"Waiting for AI response...")
            time.sleep(12)

            # Check if AI is still generating
            check_script = """
            () => {
                const loading = document.querySelector('[class*="loading"], [class*="generating"], [class*="Thinking"]');
                const msgs = document.querySelectorAll('.message-list-S2Fv2S, [class*="message"]');
                return JSON.stringify({
                    stillLoading: !!loading,
                    msgCount: msgs.length,
                    lastMsg: msgs.length > 0 ? msgs[msgs.length-1].innerText.slice(0, 200) : 'none'
                });
            }
            """
            check_result = tab_cdp("Runtime.evaluate", {"expression": check_script, "returnByValue": True})
            print(f"Check result: {check_result}")

            # Take screenshot
            screenshot_result = tab_cdp("Page.captureScreenshot", {"format": "png", "quality": 80})
            if screenshot_result.get("data"):
                import base64
                img_data = base64.b64decode(screenshot_result["data"])
                with open(screenshot_path, "wb") as f:
                    f.write(img_data)
                result["screenshot_path"] = str(screenshot_path)
                print(f"Screenshot saved: {screenshot_path}")

            tab_ws.close()

            # Analyze screenshot
            if screenshot_path.exists():
                analysis = self.analyze_screenshot(str(screenshot_path), brand_name)
                result["is_cited"] = analysis["is_cited"]
                result["cited_sources"] = analysis["cited_sources"]
                result["notes"] = analysis.get("notes")
            else:
                result["notes"] = "Screenshot capture failed"

        except Exception as e:
            import traceback
            traceback.print_exc()
            result["notes"] = f"Error during search: {str(e)}"

        return result

    def analyze_screenshot(self, screenshot_path: str, brand_name: str) -> dict:
        """Analyze screenshot to check if brand is cited"""
        result = {
            "is_cited": False,
            "cited_sources": None,
            "notes": None
        }

        try:
            # Try using vision_analyze if available
            analysis = self._call_vision_analyze(screenshot_path, brand_name)
            if analysis:
                result = analysis
        except Exception as e:
            # Fallback: try mmx or simple subprocess
            try:
                analysis = self._call_mmx(screenshot_path, brand_name)
                if analysis:
                    result = analysis
            except Exception as e2:
                result["notes"] = f"Vision analysis failed: {e}, mmx failed: {e2}"

        return result

    def _call_vision_analyze(self, screenshot_path: str, brand_name: str) -> Optional[dict]:
        """Call vision_analyze tool"""
        try:
            # Try importing vision_analyze if available in environment
            from vision_analyze import analyze_image
            description = analyze_image(screenshot_path, f"Check if '{brand_name}' brand is mentioned in this AI search results page")
            
            is_cited = brand_name.lower() in description.lower()
            return {
                "is_cited": is_cited,
                "cited_sources": description if is_cited else None,
                "notes": None
            }
        except ImportError:
            return None

    def _call_mmx(self, screenshot_path: str, brand_name: str) -> Optional[dict]:
        """Call mmx CLI tool for vision analysis"""
        try:
            result = subprocess.run(
                ["mmx", "analyze", "--image", screenshot_path, "--prompt", f"Check if brand '{brand_name}' is mentioned"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60
            )
            
            if result.returncode == 0:
                output = result.stdout
                is_cited = brand_name.lower() in output.lower()
                return {
                    "is_cited": is_cited,
                    "cited_sources": output if is_cited else None,
                    "notes": None
                }
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        
        return None


def get_monitor():
    """Factory function for monitor instance"""
    return DoubaoMonitor()
