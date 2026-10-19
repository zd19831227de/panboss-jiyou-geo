from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from contextlib import asynccontextmanager
from pathlib import Path
import os

from app.core.config import settings
from app.core.security import require_user
from app.routers import brands, keywords, monitor, dashboard, auth, tasks, system, captcha, platform_sessions, scheduler, operations, llm
from app.services.scheduler import ensure_monitor_configs


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: 确保数据库配置和浏览器可用"""
    ensure_monitor_configs()

    # 尝试确保持久浏览器在运行 (不阻塞)
    try:
        from app.services.monitor.ai_monitor import ensure_browser_running
        ensure_browser_running()
    except Exception:
        pass

    yield


app = FastAPI(
    title=settings.APP_NAME,
    description="全自动化GEO监测与优化平台 — 异步任务队列版",
    version="2.0.0",
    lifespan=lifespan,
)

# 静态文件 (截图)
os.makedirs(settings.SCREENSHOT_DIR, exist_ok=True)
os.makedirs(settings.GEO_ARTIFACT_DIR, exist_ok=True)
try:
    app.mount("/screenshot", StaticFiles(directory=settings.SCREENSHOT_DIR), name="screenshots")
except Exception:
    pass
try:
    app.mount("/artifacts", StaticFiles(directory=settings.GEO_ARTIFACT_DIR), name="artifacts")
except Exception:
    pass

# CORS：不再接受 "*" 通配（与 allow_credentials=True 组合不安全，且本系统
# 前端走同源/Vite 代理部署）。默认收敛为本机开发地址，可用 CORS_ORIGINS 逗号分隔配置。
_raw_origins = (settings.CORS_ORIGINS or "").strip()
if _raw_origins == "*":
    # 端口跟随配置，改 GEO_FRONTEND_PORT / PORT 后 CORS 自动跟着变。
    _allow_origins = []
    for _port in (settings.GEO_FRONTEND_PORT, settings.PORT):
        _allow_origins.extend([
            f"http://localhost:{_port}",
            f"http://127.0.0.1:{_port}",
        ])
else:
    _allow_origins = [origin.strip() for origin in _raw_origins.split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 业务路由统一强制认证（Bearer JWT）；auth（登录/注册）保持公开，
# /health 与 / 在 app 上直接定义、不经过 router，同样保持公开。
_protected = [Depends(require_user)]

# 路由
app.include_router(brands.router, dependencies=_protected)
app.include_router(keywords.router, dependencies=_protected)
app.include_router(monitor.router, dependencies=_protected)
app.include_router(dashboard.router, dependencies=_protected)
app.include_router(auth.router)
app.include_router(tasks.router, dependencies=_protected)
app.include_router(system.router, dependencies=_protected)
app.include_router(captcha.router, dependencies=_protected)
app.include_router(platform_sessions.router, dependencies=_protected)
app.include_router(scheduler.router, dependencies=_protected)
app.include_router(operations.router, dependencies=_protected)
app.include_router(llm.router, dependencies=_protected)



@app.get("/health")
def health_check():
    return {"status": "healthy", "service": settings.APP_NAME, "version": "2.0.0"}


@app.get("/")
def root():
    # 自包含迁移模式：没有 Node.js/vite 时，后端直接伺服前端页面。
    if _HAS_FRONTEND_DIST:
        return FileResponse(str(_FRONTEND_DIST / "index.html"))
    return {"message": f"{settings.APP_NAME} API", "version": "2.0.0"}


# 前端构建产物位置：backend/app/main.py → 项目根/frontend/dist
_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
_HAS_FRONTEND_DIST = (_FRONTEND_DIST / "index.html").is_file()


class _SPAStaticFiles(StaticFiles):
    """vue-router history 模式的深链（/login、/operations 等）404 时回落 index.html。

    /api、/screenshot、/artifacts 前缀不回落——这些路径 404 应保持 404，
    不能把 HTML 当 JSON 响应返回给前端。
    注意：新版 Starlette 的 StaticFiles 找不到文件时抛 HTTPException(404)
    而不是返回 404 响应，两种情况都要接住。
    """

    async def get_response(self, path: str, scope):
        not_found_response = None
        try:
            not_found_response = await super().get_response(path, scope)
            if not_found_response.status_code != 404:
                return not_found_response
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
        # Windows 下 Mount 传入的子路径分隔符是反斜杠（'api\\foo'），且可能带
        # 前导斜杠；统一转成正斜杠再去掉前导斜杠后比对前缀。
        rel = str(path).replace("\\", "/").lstrip("/")
        if rel.startswith(("api/", "screenshot/", "artifacts/")):
            if not_found_response is not None:
                return not_found_response
            # 直接返回 404 响应而不是 re-raise：在 Mount 内部重新抛出
            # HTTPException 会以 500 冒泡，而不是走 FastAPI 的异常处理器。
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return await super().get_response("index.html", scope)


# 必须挂在所有路由/挂载之后："/" 前缀的 Mount 会吞掉其后注册的一切路径。
if _HAS_FRONTEND_DIST:
    app.mount("/", _SPAStaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
