"""System info and control router"""
from fastapi import APIRouter, HTTPException, Query, Depends
from app.services.captcha_solver import get_captcha_status
from app.services.monitor.ai_monitor import check_all_login_status
from app.database import DATABASE_PATH
from app.services.platform_adapters.sessions import check_all_platform_sessions
from app.services.artifact_retention import cleanup_artifacts, inspect_artifacts
from app.core.runtime_paths import browser_profile_root, pid_file
from app.core.config import ai_port_map, publish_port_map
from app.core.security import require_admin
import os
import json

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/captcha-status")
def captcha_status():
    return get_captcha_status()


@router.get("/login-status")
def login_status():
    return check_all_login_status()


@router.get("/browser-status")
def browser_status():
    ai_ports = ai_port_map()
    publish_ports = publish_port_map()
    ai_desc = "、".join(f"{name}:{port}" for name, port in ai_ports.items())
    publish_desc = "、".join(f"{name}:{port}" for name, port in publish_ports.items())
    return {
        "platforms": check_all_platform_sessions(),
        "profile_root": str(browser_profile_root()),
        "ai_ports": ai_ports,
        "publish_ports": publish_ports,
        "notes": (
            f"AI平台浏览器端口：{ai_desc}；发布平台浏览器端口：{publish_desc}。"
            "以上端口可用 .env 的 GEO_PLATFORM_PORTS / GEO_PUBLISH_PORTS 修改（改完重启服务）。"
            "请避开 Hermes 的 9221-9229 和旧发布端口 9421-9429。"
        ),
    }


@router.get("/status")
def system_status():
    pid_file_path = str(pid_file())
    pids = {}
    if os.path.exists(pid_file_path):
        try:
            with open(pid_file_path, "r", encoding="utf-8") as f:
                pids = json.load(f)
        except Exception:
            pids = {}

    def alive(pid):
        try:
            import psutil
            return psutil.Process(int(pid)).is_running()
        except Exception:
            try:
                os.kill(int(pid), 0)
                return True
            except Exception:
                return False

    return {
        "backend": {"status": "running", "pid": os.getpid()},
        "worker": {
            "status": "running" if pids.get("worker") and alive(pids.get("worker")) else "offline",
            "pid": pids.get("worker"),
        },
        "frontend": {
            "status": "running" if pids.get("frontend") and alive(pids.get("frontend")) else "unknown",
            "pid": pids.get("frontend"),
        },
        "database_path": str(DATABASE_PATH.resolve()),
    }


@router.post("/ensure-browser")
def start_browser():
    platforms = check_all_platform_sessions()
    return {
        "started": False,
        "status": "manual_required",
        "platforms": platforms,
        "message": "系统不会伪造自动启动成功；请到平台登录态中分别打开豆包/千问/元宝固定浏览器",
    }


@router.post("/run-scheduler")
def run_scheduler(mode: str = Query("daily")):
    """手动触发调度扫描"""
    try:
        if mode == "daily":
            from app.services.scheduler import run_daily_increment_scan
            task_ids = run_daily_increment_scan() or []
            return {"message": "每日扫描已触发", "task_ids": task_ids, "task_count": len(task_ids)}
        elif mode == "weekly":
            from app.services.scheduler import run_weekly_full_scan
            task_ids = run_weekly_full_scan() or []
            return {"message": "每周全量扫描已触发", "task_ids": task_ids, "task_count": len(task_ids)}
        else:
            raise HTTPException(status_code=400, detail=f"未知模式: {mode}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@router.get("/artifact-retention")
def artifact_retention_status(days: int = Query(30, ge=1, le=365)):
    """预览可清理的本地证据文件。不会删除任何文件。"""
    try:
        return inspect_artifacts(days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@router.post("/artifact-retention/cleanup")
def artifact_retention_cleanup(days: int = Query(30, ge=1, le=365)):
    """清理未被数据库引用的过期证据文件。"""
    try:
        return cleanup_artifacts(days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ---- 测试数据重置（开发记录 #9） ----

@router.get("/history-reset/preview")
def history_reset_preview():
    """预览监测历史重置将清理的内容；不修改任何数据。"""
    from app.services.history_reset import preview_reset
    try:
        return preview_reset()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@router.post("/history-reset/execute")
def history_reset_execute(confirm: str = Query(...), backup: bool = Query(True), _admin: dict = Depends(require_admin)):
    """执行监测历史重置。confirm 必须传 RESET（防误触）。

    执行前自动把数据库备份到 backend/db_archive/；有等待中/运行中任务时拒绝执行。
    """
    if confirm != "RESET":
        raise HTTPException(status_code=400, detail="请传 confirm=RESET 确认执行")
    from app.services.history_reset import execute_reset
    try:
        result = execute_reset(backup=backup)
        if result.get("status") == "refused":
            raise HTTPException(status_code=409, detail=result.get("reason"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ---- 自动监测循环 ----

@router.post("/auto-monitor")
def toggle_auto_monitor(action: str = Query("status"), interval: int = Query(6, ge=1, le=24)):
    """启动/停止/查看自动监测循环。

    - action=start: 启动后台自动监测（每 interval 小时跑一次增量扫描+登录检查）
    - action=stop:  停止自动监测
    - action=status: 仅返回当前状态
    """
    from app.services.auto_monitor import auto_monitor
    if action == "start":
        return auto_monitor.start(interval_hours=interval)
    if action == "stop":
        return auto_monitor.stop()
    return auto_monitor.status()


@router.get("/auto-monitor")
def get_auto_monitor_status():
    """查询自动监测运行状态。"""
    from app.services.auto_monitor import auto_monitor
    return auto_monitor.status()
