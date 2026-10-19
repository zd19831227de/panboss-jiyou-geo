"""自动监测循环服务 — 后台线程周期性执行每日增量扫描 + 登录态检查。

设计：
- 用 threading.Event 控制停止（兼容后台线程，不用 signal）
- daemon 线程，随主进程退出
- 状态持久化到内存（running / last_run_at / last_task_count / interval_hours）
- 对外提供 start / stop / status 三个方法

用法（API 层调用）：
    from app.services.auto_monitor import auto_monitor
    auto_monitor.start(interval_hours=6)
    auto_monitor.stop()
    auto_monitor.status()
"""
import threading
import time
from datetime import datetime

from app.database import local_now


class AutoMonitorManager:
    def __init__(self):
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.interval_hours = 6
        self.running = False
        self.last_run_at = None
        self.last_task_count = 0
        self.last_login_status = {}
        self.error = None

    def start(self, interval_hours: int = 6):
        with self._lock:
            if self.running:
                return {"success": False, "message": "自动监测已在运行中"}
            self._stop_event.clear()
            self.interval_hours = max(1, min(24, interval_hours))
            self.running = True
            self.error = None
            self._thread = threading.Thread(target=self._loop, daemon=True, name="geo-auto-monitor")
            self._thread.start()
            return {
                "success": True,
                "message": f"自动监测已启动（每 {self.interval_hours} 小时执行一次）",
                "interval_hours": self.interval_hours,
            }

    def stop(self):
        with self._lock:
            if not self.running:
                return {"success": False, "message": "自动监测未在运行"}
            self._stop_event.set()
            self.running = False
            return {"success": True, "message": "自动监测已停止"}

    def status(self):
        return {
            "running": self.running,
            "interval_hours": self.interval_hours,
            "last_run_at": self.last_run_at,
            "last_task_count": self.last_task_count,
            "last_login_status": self.last_login_status,
            "error": self.error,
        }

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                from app.services.scheduler import run_daily_increment_scan, run_login_check

                task_ids = run_daily_increment_scan() or []
                login_status = run_login_check() or {}
                self.last_run_at = local_now()
                self.last_task_count = len(task_ids)
                self.last_login_status = login_status
                self.error = None
            except Exception as e:
                self.error = str(e)[:300]

            # 等待 interval_hours，每 30 秒检查一次停止信号
            wait_seconds = self.interval_hours * 3600
            elapsed = 0
            while elapsed < wait_seconds and not self._stop_event.is_set():
                time.sleep(30)
                elapsed += 30

        self.running = False


auto_monitor = AutoMonitorManager()
