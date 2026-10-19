"""
验证码处理方案 — 商用级 + 免费兜底

方案选型:
  [付费] Capsolver API (https://www.capsolver.com/)
    - 支持: reCAPTCHA v2/v3, hCaptcha, FunCaptcha, Cloudflare Turnstile
    - 价格: $0.5-$3/1000次 (按类型)
    - 速度: 5-30s
    - 接入: 简单 HTTP API
    
  [付费备选] 2Captcha (https://2captcha.com/)
    - 价格: $2.99/1000次
    - 俄罗斯服务, 速度慢一些
    
  [免费] 手动标记 + 浏览器通知
    - 监测到验证码时:
      a) 打开浏览器弹窗通知用户
      b) 在任务结果中标记 "captcha_blocked"
      c) 跳过该平台本轮扫描, 下次重试

配置:
  设置环境变量 CAPSOLVER_API_KEY 来启用付费方案
  不设置时自动降级为手动模式
"""
import os
import json
import time
import base64
import urllib.request
import urllib.parse
from typing import Optional, Dict

from app.core.config import settings


# ── 配置 ──────────────────────────────────────────────────

CAPSOLVER_API_KEY = settings.CAPSOLVER_API_KEY or os.environ.get("CAPSOLVER_API_KEY", "")

# 付费方案可用性
PAID_AVAILABLE = bool(CAPSOLVER_API_KEY)

# 如果没设 key, 提示用户
if not PAID_AVAILABLE:
    print("[CAPTCHA] ⚠️ 未设置 CAPSOLVER_API_KEY, 使用手动模式")
    print("[CAPTCHA]   付费方案: https://www.capsolver.com/")
    print("[CAPTCHA]   Capsolver 价格: $0.5-3/千次")
    print("[CAPTCHA]   设置: export CAPSOLVER_API_KEY='your_key'")


# ═══════════════════════════════════════════════════════════
#  Capsolver API
# ═══════════════════════════════════════════════════════════

def _capsolver_request(payload: dict, timeout: int = 120) -> Optional[dict]:
    """向 Capsolver 发送请求"""
    payload["clientKey"] = CAPSOLVER_API_KEY
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.capsolver.com/createTask",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode())
    except Exception as e:
        return {"errorId": True, "errorDescription": str(e)}


def _capsolver_get_result(task_id: str, timeout: int = 120) -> Optional[dict]:
    """轮询获取结果"""
    payload = json.dumps({
        "clientKey": CAPSOLVER_API_KEY,
        "taskId": task_id,
    }).encode()
    
    deadline = time.time() + timeout
    while time.time() < deadline:
        req = urllib.request.Request(
            "https://api.capsolver.com/getTaskResult",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            result = json.loads(resp.read().decode())
            if result.get("status") == "ready":
                return result
            elif result.get("status") == "failed":
                return {"error": result.get("errorDescription", "unknown")}
            # Capsolver 失败时会返回 errorId != 0 + errorDescription，
            # 而非 status=="failed"。提前识别，避免空转到 timeout。
            if result.get("errorId"):
                return {"error": result.get("errorDescription", "unknown")}
        except Exception:
            pass
        time.sleep(3)
    return {"error": "timeout"}


def solve_recaptcha_v2(site_key: str, page_url: str) -> Optional[str]:
    """解决 reCAPTCHA v2, 返回 token"""
    if not PAID_AVAILABLE:
        return None
    
    result = _capsolver_request({
        "task": {
            "type": "ReCaptchaV2Task",
            "websiteURL": page_url,
            "websiteKey": site_key,
        }
    })
    
    if result.get("errorId"):
        print(f"[CAPTCHA] Capsolver error: {result.get('errorDescription')}")
        return None
    
    task_id = result.get("taskId")
    if not task_id:
        return None
    
    solution = _capsolver_get_result(task_id)
    return solution.get("solution", {}).get("gRecaptchaResponse")


def solve_hcaptcha(site_key: str, page_url: str) -> Optional[str]:
    """解决 hCaptcha, 返回 token"""
    if not PAID_AVAILABLE:
        return None
    
    result = _capsolver_request({
        "task": {
            "type": "HCaptchaTask",
            "websiteURL": page_url,
            "websiteKey": site_key,
        }
    })
    
    if result.get("errorId"):
        print(f"[CAPTCHA] Capsolver hCaptcha error: {result.get('errorDescription')}")
        return None
    
    task_id = result.get("taskId")
    solution = _capsolver_get_result(task_id)
    return solution.get("solution", {}).get("gRecaptchaResponse")


def solve_turnstile(site_key: str, page_url: str) -> Optional[str]:
    """解决 Cloudflare Turnstile"""
    if not PAID_AVAILABLE:
        return None
    
    result = _capsolver_request({
        "task": {
            "type": "AntiTurnstileTaskProxyLess",
            "websiteURL": page_url,
            "websiteKey": site_key,
        }
    })
    
    if result.get("errorId"):
        print(f"[CAPTCHA] Capsolver Turnstile error: {result.get('errorDescription')}")
        return None
    
    task_id = result.get("taskId")
    solution = _capsolver_get_result(task_id)
    return solution.get("solution", {}).get("token")


# ═══════════════════════════════════════════════════════════
#  自动检测并解决验证码
# ═══════════════════════════════════════════════════════════

def detect_and_solve(page_url: str, page_source: str, visible_text: Optional[str] = None) -> dict:
    """
    检测页面上的验证码并尝试解决
    
    返回:
        {
            "detected": bool,
            "type": str | None,     # recaptcha_v2, hcaptcha, turnstile, unknown
            "solved": bool,
            "token": str | None,
            "notes": str,
        }
    """
    result = {
        "detected": False,
        "type": None,
        "site_key": None,
        "solved": False,
        "token": None,
        "notes": "",
    }
    
    # 检测验证码类型
    import re
    
    # reCAPTCHA v2
    recaptcha_match = re.search(r'data-sitekey="([^"]+)"', page_source)
    if recaptcha_match:
        result["detected"] = True
        result["type"] = "recaptcha_v2"
        result["site_key"] = recaptcha_match.group(1)
    
    # hCaptcha
    hcaptcha_match = re.search(r'data-sitekey="([^"]+)".*hcaptcha', page_source, re.IGNORECASE)
    if not hcaptcha_match:
        hcaptcha_match = re.search(r'hcaptcha.*data-sitekey="([^"]+)"', page_source, re.IGNORECASE)
    if hcaptcha_match:
        result["detected"] = True
        result["type"] = "hcaptcha"
        result["site_key"] = hcaptcha_match.group(1)
    
    # Turnstile
    turnstile_match = re.search(r'data-sitekey="([^"]+)".*turnstile', page_source, re.IGNORECASE)
    if turnstile_match:
        result["detected"] = True
        result["type"] = "turnstile"
        result["site_key"] = turnstile_match.group(1)
    
    # 通用关键词检测只看页面可见文本，避免命中前端脚本/隐藏模板里的“验证”文案。
    if not result["detected"]:
        generic_text = visible_text if visible_text is not None else page_source
        captcha_kw = ["验证码", "captcha", "安全验证", "请完成验证",
                      "请按住滑块", "拖动验证", "人机验证"]
        for kw in captcha_kw:
            if kw in generic_text:
                result["detected"] = True
                result["type"] = "unknown"
                result["notes"] = f"检测到验证码关键词: {kw}"
                break
    
    if not result["detected"]:
        result["notes"] = "未检测到验证码"
        return result
    
    # 尝试自动解决
    if PAID_AVAILABLE and result["site_key"]:
        if result["type"] == "recaptcha_v2":
            token = solve_recaptcha_v2(result["site_key"], page_url)
        elif result["type"] == "hcaptcha":
            token = solve_hcaptcha(result["site_key"], page_url)
        elif result["type"] == "turnstile":
            token = solve_turnstile(result["site_key"], page_url)
        else:
            token = None
        
        if token:
            result["solved"] = True
            result["token"] = token
            result["notes"] = f"✅ {result['type']} 自动解决成功"
        else:
            result["notes"] = f"❌ {result['type']} 解决失败，需要人工处理"
    else:
        result["notes"] = f"检测到 {result['type'] or 'unknown'} 验证码，"
        if not PAID_AVAILABLE:
            result["notes"] += "未配置付费方案(Capsolver)，请手动处理"
        else:
            result["notes"] += "缺少 site_key 无法自动解决"
    
    return result


def get_captcha_status() -> dict:
    """获取验证码处理能力状态"""
    return {
        "paid_available": PAID_AVAILABLE,
        "provider": "capsolver" if PAID_AVAILABLE else "none",
        "supported_types": ["recaptcha_v2", "hcaptcha", "turnstile"] if PAID_AVAILABLE else [],
        "fallback_mode": "manual" if not PAID_AVAILABLE else "auto+manual",
        "notes": "付费方案: Capsolver $0.5-3/千次" if PAID_AVAILABLE else "未配置，使用手动模式",
    }
