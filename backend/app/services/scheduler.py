"""
GEO定时调度器 — 独立进程运行, 通过 task_queue 提交批量监测任务
支持:
  - python3 -m app.services.scheduler daily    # 每日增量
  - python3 -m app.services.scheduler weekly   # 每周全量
  - python3 -m app.services.scheduler all      # 全部
  - python3 -m app.services.scheduler check    # 登录态检查
  - python3 -m app.services.scheduler auto     # 自动循环 (每6小时)
  
安装 crontab:
  0 2 * * * cd /path && python3 -m app.services.scheduler daily >> /tmp/geo_daily.log 2>&1
  0 3 * * 0 cd /path && python3 -m app.services.scheduler weekly >> /tmp/geo_weekly.log 2>&1
"""
import os
import sys
import json
import time
import signal
from datetime import datetime
from pathlib import Path
from typing import Optional

BACKEND_DIR = str(Path(__file__).parent.parent)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'

from app.database import get_db, local_now
from app.services.task_queue import create_task


def ensure_monitor_configs():
    """确保所有活跃品牌都有监测配置"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO monitor_configs (brand_id)
            SELECT id FROM brands WHERE status = 'active'
        """)
        conn.commit()


def _brand_scan_in_flight(brand_id: int) -> bool:
    """该品牌是否已有 pending/running 的监测任务。

    调度器进程、auto-monitor 线程、手动接口三个入口可能并发触发同一品牌的
    扫描；重复提交会导致重复 RPA 提问，按次消耗平台风控额度。
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT params FROM task_queue "
            "WHERE task_type = 'monitor' AND status IN ('pending', 'running') "
            "ORDER BY id DESC LIMIT 50",
        )
        for row in cursor.fetchall():
            try:
                params = json.loads(row["params"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if params.get("brand_id") == brand_id:
                return True
    return False


def submit_brand_scan(brand_id: int, brand_name: str, platforms: list) -> int:
    """为品牌提交扫描任务, 返回 task_id。

    返回 -1：品牌没有关键词；返回 -2：该品牌已有未完成的监测任务（幂等跳过）。
    """
    if _brand_scan_in_flight(brand_id):
        return -2
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, word FROM keywords WHERE brand_id = ?", (brand_id,))
        keywords = [{"id": r["id"], "word": r["word"]} for r in cursor.fetchall()]

    if not keywords:
        return -1

    task_id = create_task("monitor", {
        "brand_name": brand_name,
        "brand_id": brand_id,
        "keywords": keywords,
        "platforms": platforms,
    })
    return task_id


def run_daily_increment_scan():
    """每日增量扫描 — 所有活跃品牌 × web + AI平台"""
    ensure_monitor_configs()
    task_ids = []

    with get_db() as conn:
        cursor = conn.cursor()
        rows = cursor.execute(
            "SELECT b.id, b.name, b.target_platforms FROM brands b "
            "JOIN monitor_configs mc ON mc.brand_id = b.id "
            "WHERE b.status = 'active' AND mc.is_active = 1"
        ).fetchall()

    for row in rows:
        brand = dict(row)
        if brand.get("target_platforms"):
            plats = [p.strip() for p in brand["target_platforms"].split(",")]
        else:
            plats = ["web"]

        tid = submit_brand_scan(brand["id"], brand["name"], plats)
        if tid > 0:
            task_ids.append(tid)
            with get_db() as conn:
                cursor = conn.cursor()
                now = local_now()
                cursor.execute(
                    "UPDATE monitor_configs SET last_increment_scan = ? WHERE brand_id = ?",
                    (now, brand["id"]),
                )

    return task_ids


def run_weekly_full_scan():
    """每周全量扫描 — 全部平台"""
    ensure_monitor_configs()
    task_ids = []

    with get_db() as conn:
        cursor = conn.cursor()
        rows = cursor.execute(
            "SELECT b.id, b.name FROM brands b "
            "JOIN monitor_configs mc ON mc.brand_id = b.id "
            "WHERE b.status = 'active' AND mc.is_active = 1"
        ).fetchall()

    for row in rows:
        brand = dict(row)
        tid = submit_brand_scan(brand["id"], brand["name"],
                                ["web", "doubao", "qianwen", "yuanbao"])
        if tid > 0:
            task_ids.append(tid)
            with get_db() as conn:
                cursor = conn.cursor()
                now = local_now()
                cursor.execute(
                    "UPDATE monitor_configs SET last_full_scan = ? WHERE brand_id = ?",
                    (now, brand["id"]),
                )

    return task_ids


def run_login_check():
    """检查所有平台登录状态"""
    from app.services.monitor.ai_monitor import check_all_login_status
    statuses = check_all_login_status()
    offline = [k for k, v in statuses.items() if not v.get("is_logged_in")]
    if offline:
        print(f"[SCHEDULER] ⚠️ 以下平台未登录: {', '.join(offline)}")
        # 创建告警
        for plat in offline:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO alerts (brand_id, alert_type, message, severity) "
                    "VALUES (0, 'login_expired', ?, 'warning')",
                    (f"AI平台 {plat} 登录状态失效，请重新登录",),
                )
    return {k: v.get("is_logged_in") for k, v in statuses.items()}


def auto_loop(interval_hours: int = 6):
    """自动循环 — 每 N 小时执行一次 daily scan + login check"""
    running = True

    def _shutdown(sig, frame):
        nonlocal running
        print("\n[SCHEDULER] Auto loop stopped")
        running = False

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(f"[SCHEDULER] Auto loop started (interval={interval_hours}h)")
    while running:
        print(f"[{datetime.now()}] Running daily scan...")
        tids = run_daily_increment_scan()
        print(f"[{datetime.now()}] Submitted {len(tids)} tasks: {tids}")

        print(f"[{datetime.now()}] Running login check...")
        status = run_login_check()
        print(f"[{datetime.now()}] Login status: {status}")

        # 等待直至下一个周期 (每隔interval/24检查一次)
        for _ in range(int(interval_hours * 3600 / 30)):
            if not running:
                break
            time.sleep(30)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="GEO运营系统定时扫描器")
    parser.add_argument("mode", nargs="?", default="daily",
                        choices=["daily", "weekly", "all", "check", "auto"],
                        help="运行模式")
    parser.add_argument("--interval", type=int, default=6,
                        help="auto模式: 循环间隔(小时)")
    args = parser.parse_args()

    ensure_monitor_configs()

    if args.mode in ("daily", "all"):
        print(f"[{datetime.now()}] 开始每日增量扫描...")
        tids = run_daily_increment_scan()
        print(f"[{datetime.now()}] 完成: 提交 {len(tids)} 个任务, task_ids={tids}")

    if args.mode in ("weekly", "all"):
        print(f"[{datetime.now()}] 开始每周全量扫描...")
        tids = run_weekly_full_scan()
        print(f"[{datetime.now()}] 完成: 提交 {len(tids)} 个任务, task_ids={tids}")

    if args.mode == "check":
        print(f"[{datetime.now()}] 登录态检查...")
        status = run_login_check()
        print(f"登录状态: {json.dumps(status, ensure_ascii=False, indent=2)}")

    if args.mode == "auto":
        auto_loop(interval_hours=args.interval)
