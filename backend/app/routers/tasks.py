"""Tasks router — 任务队列查询接口"""
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from datetime import datetime
from app.database import get_db
from app.services.task_queue import get_task, list_tasks, cancel_task

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("")
def list_all_tasks(
    task_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(default=20),
):
    """列出任务"""
    tasks = list_tasks(task_type=task_type, status=status, limit=limit)
    monitor_task_ids = [task["id"] for task in tasks if task.get("task_type") == "monitor"]
    business_by_task = {}
    if monitor_task_ids:
        with get_db() as conn:
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in monitor_task_ids)
            cursor.execute(
                f"""SELECT task_id, business_status, business_failure_reason,
                           effective_source_count, brand_mention_count, brand_mention_rate
                    FROM monitor_runs
                    WHERE task_id IN ({placeholders})
                    ORDER BY id DESC""",
                monitor_task_ids,
            )
            for row in cursor.fetchall():
                d = dict(row)
                business_by_task.setdefault(d["task_id"], d)
    for task in tasks:
        business = business_by_task.get(task["id"], {})
        task["business_status"] = business.get("business_status")
        task["business_failure_reason"] = business.get("business_failure_reason")
        task["effective_source_count"] = business.get("effective_source_count", 0)
        task["brand_mention_count"] = business.get("brand_mention_count", 0)
        task["brand_mention_rate"] = business.get("brand_mention_rate", 0)
    return tasks


@router.get("/{task_id}")
def get_task_info(task_id: int):
    """获取任务详情"""
    import json
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    result_data = task.get("result")
    if isinstance(result_data, str):
        try:
            result_data = json.loads(result_data)
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "task_id": task["id"],
        "task_type": task.get("task_type"),
        "status": task.get("status"),
        "progress_pct": task.get("progress_pct", 0),
        "progress_msg": task.get("progress_msg", ""),
        "error_msg": task.get("error_msg"),
        "result": result_data,
        "business_status": (result_data or {}).get("business_status") if isinstance(result_data, dict) else None,
        "business_failure_reason": (result_data or {}).get("business_failure_reason") if isinstance(result_data, dict) else None,
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
    }


@router.post("/{task_id}/cancel")
def cancel_task_endpoint(task_id: int):
    """取消任务"""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    ok = cancel_task(task_id)
    if ok and task.get("task_type") == "monitor":
        with get_db() as conn:
            conn.execute(
                "UPDATE monitor_runs SET status = 'cancelled', completed_at = ?, error_msg = ? WHERE task_id = ?",
                (datetime.now().isoformat(), "任务已取消", task_id),
            )
    return {
        "task_id": task_id,
        "cancelled": ok,
        "status": "cancelled" if ok else task.get("status"),
        "message": "任务已取消" if ok else "任务已完成或失败，不能取消",
    }
