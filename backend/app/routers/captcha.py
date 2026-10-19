"""验证码路由 — Capsolver 集成接口"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
from app.services.captcha_solver import (
    get_captcha_status,
    detect_and_solve,
    solve_recaptcha_v2,
    solve_hcaptcha,
    solve_turnstile,
    CAPSOLVER_API_KEY,
    PAID_AVAILABLE,
)

router = APIRouter(prefix="/api/captcha", tags=["captcha"])


class SolveRequest(BaseModel):
    """验证码解决请求"""
    type: str  # recaptcha_v2, hcaptcha, turnstile
    site_key: str
    page_url: str
    timeout: Optional[int] = 120


@router.get("/status")
def captcha_status():
    """获取验证码处理能力状态"""
    return get_captcha_status()


@router.post("/solve")
def solve_captcha(req: SolveRequest):
    """提交验证码到 Capsolver 解决"""
    if not PAID_AVAILABLE:
        raise HTTPException(
            status_code=400,
            detail="Capsolver 未配置。请设置环境变量 CAPSOLVER_API_KEY，或在 https://www.capsolver.com/ 注册获取 API Key"
        )
    
    if req.type == "recaptcha_v2":
        token = solve_recaptcha_v2(req.site_key, req.page_url)
    elif req.type == "hcaptcha":
        token = solve_hcaptcha(req.site_key, req.page_url)
    elif req.type == "turnstile":
        token = solve_turnstile(req.site_key, req.page_url)
    else:
        raise HTTPException(status_code=400, detail=f"不支持的验证码类型: {req.type}")

    if token:
        return {"solved": True, "token": token}
    else:
        return {"solved": False, "token": None, "error": "Capsolver 解决失败"}


@router.get("/config")
def captcha_config():
    """获取配置指引"""
    return {
        "api_key_configured": PAID_AVAILABLE,
        "configured_key": f"{CAPSOLVER_API_KEY[:4]}...{CAPSOLVER_API_KEY[-4:]}" if CAPSOLVER_API_KEY and len(CAPSOLVER_API_KEY) > 8 else None,
        "register_url": "https://www.capsolver.com/",
        "pricing": "reCAPTCHA v2: $0.5/1000, hCaptcha: $0.5/1000, Turnstile: $1/1000",
        "setup_guide": "export CAPSOLVER_API_KEY='your_api_key' 然后重启后端",
    }
