"""
异步任务队列 — SQLite 作为消息队列，Worker 独立进程轮询
替代 Celery（需要Redis/RabbitMQ），零依赖，适合单机部署

任务状态流转:
  pending → running → completed / failed

Worker 进程通过 `python3 -m app.services.task_queue worker` 启动
"""
import json
import time
import sys
import os
import signal
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

from app.database import get_db, local_now

# ── 任务类型 ──────────────────────────────────────────────
TASK_TYPE_MONITOR = "monitor"        # AI平台+Web监测
TASK_TYPE_AI_DETECT = "ai_detect"    # 仅AI平台
TASK_TYPE_WEB_SCAN = "web_scan"      # 仅Web
TASK_TYPE_LOGIN_CHECK = "login_check"  # 登录态检查
TASK_TYPE_AUTO_CONTENT = "auto_content"  # 监测缺口自动补内容（品牌×关键词文章生成）

# ── 任务状态 ──────────────────────────────────────────────
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"


def ensure_task_tables():
    """确保任务队列表存在"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                priority INTEGER DEFAULT 0,
                params TEXT NOT NULL,       -- JSON
                result TEXT,                -- JSON (完成时写入)
                progress_pct INTEGER DEFAULT 0,
                progress_msg TEXT,
                error_msg TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                worker_id TEXT
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_task_queue_status
            ON task_queue(status, priority)
        """)
        conn.commit()


ensure_task_tables()


# ═══════════════════════════════════════════════════════════
#  API 接口 — 创建/查询/取消任务
# ═══════════════════════════════════════════════════════════

def create_task(task_type: str, params: dict, priority: int = 0) -> int:
    """创建任务，返回 task_id"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO task_queue
               (task_type, status, params, priority, created_at)
               VALUES (?, 'pending', ?, ?, ?)""",
            (task_type, json.dumps(params, ensure_ascii=False), priority, local_now()),
        )
        conn.commit()
        return cursor.lastrowid


def get_task(task_id: int) -> Optional[dict]:
    """查询任务状态"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM task_queue WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        if row:
            d = dict(row)
            if d.get("params"):
                try:
                    d["params"] = json.loads(d["params"])
                except (json.JSONDecodeError, TypeError):
                    pass
            if d.get("result"):
                try:
                    d["result"] = json.loads(d["result"])
                except (json.JSONDecodeError, TypeError):
                    pass
            return d
        return None


def list_tasks(task_type: Optional[str] = None, status: Optional[str] = None,
               limit: int = 50) -> list:
    """列出任务"""
    with get_db() as conn:
        cursor = conn.cursor()
        where = []
        params = []
        if task_type:
            where.append("task_type = ?")
            params.append(task_type)
        if status:
            where.append("status = ?")
            params.append(status)
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        cursor.execute(
            f"""SELECT * FROM task_queue {where_sql}
                ORDER BY
                  CASE status
                    WHEN 'running' THEN 0
                    WHEN 'pending' THEN 1
                    ELSE 2
                  END,
                  datetime(created_at) DESC,
                  id DESC
                LIMIT ?""",
            params + [limit],
        )
        return [dict(row) for row in cursor.fetchall()]


def cancel_task(task_id: int) -> bool:
    """取消任务"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE task_queue
               SET status = 'cancelled',
                   progress_msg = '已取消',
                   completed_at = COALESCE(completed_at, ?)
               WHERE id = ? AND status IN ('pending', 'running')""",
            (local_now(), task_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def is_task_cancelled(task_id: Optional[int]) -> bool:
    if not task_id:
        return False
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM task_queue WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        return bool(row and row["status"] == STATUS_CANCELLED)


def delete_task(task_id: int, task_type: Optional[str] = None) -> bool:
    """删除任务列表记录；运行中任务需先取消，历史业务结果不在这里删除。"""
    with get_db() as conn:
        cursor = conn.cursor()
        params = [task_id]
        where = "id = ? AND status NOT IN ('running', 'pending')"
        if task_type:
            where += " AND task_type = ?"
            params.append(task_type)
        cursor.execute(f"DELETE FROM task_queue WHERE {where}", params)
        conn.commit()
        return cursor.rowcount > 0


# ═══════════════════════════════════════════════════════════
#  Worker — 独立进程运行
# ═══════════════════════════════════════════════════════════

# 任务处理器注册表
_task_handlers: Dict[str, Callable] = {}

def register_handler(task_type: str, handler: Callable):
    _task_handlers[task_type] = handler


def _do_task(task_id: int, task_type: str, params: dict) -> dict:
    """执行单个任务，返回结果"""
    handler = _task_handlers.get(task_type)
    if not handler:
        return {"error": f"No handler for task type: {task_type}"}
    return handler(params)


def _update_task(task_id: int, **kwargs):
    """更新任务字段"""
    with get_db() as conn:
        cursor = conn.cursor()
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [task_id]
        cursor.execute(f"UPDATE task_queue SET {sets} WHERE id = ?", values)
        conn.commit()


# 单批次总超时（秒）：防止一个挂起的浏览器/CDP 操作永久阻塞 worker 主循环
_BATCH_TIMEOUT_SECONDS = int(os.getenv("GEO_TASK_BATCH_TIMEOUT", "3600"))
# running 任务被视为僵尸的时长（小时）；worker 崩溃后由新 worker 启动时回收
_ZOMBIE_TASK_MAX_AGE_HOURS = int(os.getenv("GEO_TASK_ZOMBIE_HOURS", "6"))


def recover_stale_running_tasks(max_age_hours: int = _ZOMBIE_TASK_MAX_AGE_HOURS) -> int:
    """把超时的 running 任务复位为 failed。

    worker 被 kill/断电后，status='running' 的任务不会再被原进程收尾，
    前端将永远显示"运行中"。新 worker 启动时按 started_at/created_at
    超时判定回收，避免任务永久假死。返回回收数量。
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""UPDATE task_queue
                SET status = ?,
                    error_msg = 'worker 异常终止，任务已回收',
                    progress_msg = '失败: worker 崩溃残留，已回收',
                    completed_at = ?
                WHERE status = ? AND (
                    (started_at IS NOT NULL
                     AND datetime(started_at, '+{int(max_age_hours)} hours') < datetime('now','localtime'))
                    OR (started_at IS NULL AND created_at IS NOT NULL
                        AND datetime(created_at, '+{int(max_age_hours)} hours') < datetime('now','localtime'))
                )""",
            (STATUS_FAILED, local_now(), STATUS_RUNNING),
        )
        conn.commit()
        return cursor.rowcount


def worker_loop(max_workers: int = 3, poll_interval: float = 2.0,
                loop_once: bool = False):
    """
    Worker 主循环 — 轮询 pending 任务并执行
    启动方式: python3 -m app.services.task_queue worker

    Args:
        max_workers: 并行执行数
        poll_interval: 轮询间隔(秒)
        loop_once: True 只跑一轮（用于调试）
    """
    print(f"[TASK-Q] Worker started (max_workers={max_workers}, poll={poll_interval}s)")
    print(f"[TASK-Q] Registered handlers: {list(_task_handlers.keys())}")

    recovered = recover_stale_running_tasks()
    if recovered:
        print(f"[TASK-Q] Recovered {recovered} stale running task(s) from crashed worker")

    running = True

    def _shutdown(sig, frame):
        nonlocal running
        print(f"\n[TASK-Q] Shutting down...")
        running = False

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while running:
        try:
            # 候选 → 逐个原子认领：UPDATE 带 status='pending' 条件并检查 rowcount，
            # 防止多 worker 并发轮询时重复执行同一任务（重复 RPA 会触发平台风控）。
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM task_queue WHERE status = 'pending' "
                    "ORDER BY priority DESC, created_at ASC LIMIT ?",
                    (max_workers,),
                )
                candidates = [dict(row) for row in cursor.fetchall()]

            pending = []
            if candidates:
                worker_id = f"worker-{os.getpid():x}"
                for task in candidates:
                    with get_db() as conn:
                        cursor = conn.cursor()
                        cursor.execute(
                            "UPDATE task_queue SET status = ?, started_at = ?, worker_id = ? "
                            "WHERE id = ? AND status = ?",
                            (STATUS_RUNNING, local_now(), worker_id, task["id"], STATUS_PENDING),
                        )
                        conn.commit()
                        if cursor.rowcount > 0:
                            pending.append(task)

            if not pending:
                if loop_once:
                    break
                time.sleep(poll_interval)
                continue

            print(f"[TASK-Q] Claimed {len(pending)} pending tasks")

            # 并行执行。不使用 with 块：批次总超时后 shutdown(wait=False)
            # 放弃等待，保证单个挂起任务不会永久阻塞 worker 主循环。
            pool = ThreadPoolExecutor(max_workers=max_workers)
            futures = {}
            try:
                for task in pending:
                    task_id = task["id"]
                    params = json.loads(task["params"]) if isinstance(task["params"], str) else task.get("params", {})
                    if isinstance(params, dict):
                        params.setdefault("task_id", task_id)
                    print(f"[TASK-Q] Starting task #{task_id}: {task['task_type']}")
                    future = pool.submit(_do_task, task_id, task["task_type"], params)
                    futures[future] = task

                remaining = dict(futures)
                try:
                    for future in as_completed(futures, timeout=_BATCH_TIMEOUT_SECONDS):
                        task = remaining.pop(future)
                        task_id = task["id"]
                        try:
                            result = future.result(timeout=300)
                            if is_task_cancelled(task_id):
                                _update_task(task_id,
                                             progress_msg="已取消",
                                             completed_at=local_now())
                                print(f"[TASK-Q] Task #{task_id} cancelled")
                                continue
                            is_error = "error" in result and result["error"]
                            status = STATUS_FAILED if is_error else STATUS_COMPLETED
                            progress_msg = "完成" if not is_error else f"失败: {result['error'][:200]}"
                            business_status = result.get("business_status") if isinstance(result, dict) else None
                            if business_status == "failed_quality_gate":
                                progress_msg = f"业务失败: {(result.get('business_failure_reason') or '质量门禁未通过')[:200]}"
                            elif business_status == "brand_mentioned_unverified":
                                progress_msg = f"完成(品牌已露出,来源未验证): {(result.get('business_failure_reason') or '')[:120]}"
                            _update_task(task_id, status=status,
                                         result=json.dumps(result, ensure_ascii=False),
                                         progress_pct=100,
                                         progress_msg=progress_msg,
                                         completed_at=local_now())
                            print(f"[TASK-Q] Task #{task_id} {status}")
                        except Exception as e:
                            if is_task_cancelled(task_id):
                                _update_task(task_id,
                                             progress_msg="已取消",
                                             completed_at=local_now())
                                print(f"[TASK-Q] Task #{task_id} cancelled")
                                continue
                            _update_task(task_id, status=STATUS_FAILED,
                                         error_msg=str(e)[:500],
                                         progress_msg=f"异常: {str(e)[:200]}",
                                         completed_at=local_now())
                            print(f"[TASK-Q] Task #{task_id} FAILED: {e}")
                except FuturesTimeoutError:
                    # 批次总超时：未完成的任务直接判失败；对应线程可能仍在后台
                    # 挂着（其结果不会再写回 task_queue），由平台侧重试或人工处理。
                    for task in remaining.values():
                        _update_task(task["id"], status=STATUS_FAILED,
                                     error_msg=f"批次总超时（{_BATCH_TIMEOUT_SECONDS}s），任务被标记失败",
                                     progress_msg=f"失败: 批次总超时（{_BATCH_TIMEOUT_SECONDS}s）",
                                     completed_at=local_now())
                        print(f"[TASK-Q] Task #{task['id']} BATCH-TIMEOUT")
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

            if loop_once:
                break

        except Exception as e:
            print(f"[TASK-Q] Worker loop error: {e}")
            traceback.print_exc()
            if loop_once:
                break
            time.sleep(poll_interval * 2)

    print("[TASK-Q] Worker stopped")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "worker"

    if mode == "worker":
        max_w = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        loop_once = "--once" in sys.argv
        worker_loop(max_workers=max_w, loop_once=loop_once)
    elif mode == "create":
        # python3 -m app.services.task_queue create <type> '<json_params>'
        task_type = sys.argv[2]
        params = json.loads(sys.argv[3])
        task_id = create_task(task_type, params)
        print(json.dumps({"task_id": task_id}))
    elif mode == "get":
        task_id = int(sys.argv[2])
        task = get_task(task_id)
        print(json.dumps(task, ensure_ascii=False, indent=2, default=str))
    elif mode == "list":
        t = sys.argv[2] if len(sys.argv) > 2 else None
        s = sys.argv[3] if len(sys.argv) > 3 else None
        tasks = list_tasks(t, s)
        print(json.dumps(tasks, ensure_ascii=False, indent=2, default=str))
    elif mode == "cancel":
        task_id = int(sys.argv[2])
        ok = cancel_task(task_id)
        print(json.dumps({"cancelled": ok}))
    else:
        print(f"Usage: {sys.argv[0]} [worker|create|get|list|cancel] [args...]")
