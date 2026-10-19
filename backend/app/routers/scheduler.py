"""Compatibility routes for scheduler actions used by the frontend."""
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


@router.post("/run")
def run_scheduler_compat(mode: str = Query("daily")):
    try:
        if mode == "daily":
            from app.services.scheduler import run_daily_increment_scan
            task_ids = run_daily_increment_scan()
            return {"message": "每日扫描已触发", "task_ids": task_ids, "task_count": len(task_ids)}
        if mode == "weekly":
            from app.services.scheduler import run_weekly_full_scan
            task_ids = run_weekly_full_scan()
            return {"message": "每周全量扫描已触发", "task_ids": task_ids, "task_count": len(task_ids)}
        raise HTTPException(status_code=400, detail=f"未知模式: {mode}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:300])
