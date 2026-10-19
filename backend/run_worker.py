#!/usr/bin/env python3
"""
Worker 启动入口 — 注册所有 handler 后启动 worker 循环
"""
import os
import sys

# OBJC_DISABLE_INITIALIZE_FORK_SAFETY 仅对 macOS（darwin）下 fork+ Objective-C
# 运行时（Playwright/WebKit 子进程）有意义，Windows 上无作用，按平台设置即可。
if sys.platform == "darwin":
    os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'

# 注册 handler
from app.services.task_queue import register_handler, worker_loop, TASK_TYPE_MONITOR, TASK_TYPE_AI_DETECT, TASK_TYPE_AUTO_CONTENT
from app.services.monitor.ai_monitor import run_batch_monitor
from app.services.auto_content import run_auto_content

register_handler(TASK_TYPE_MONITOR, run_batch_monitor)
register_handler(TASK_TYPE_AI_DETECT, run_batch_monitor)
register_handler(TASK_TYPE_AUTO_CONTENT, run_auto_content)

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='GEO Worker')
    parser.add_argument('--workers', type=int, default=3, help='Max concurrent workers')
    parser.add_argument('--poll', type=float, default=2.0, help='Poll interval (seconds)')
    parser.add_argument('--once', action='store_true', help='Run once then exit')
    args = parser.parse_args()

    print(f"[WORKER] Starting with {args.workers} workers, poll={args.poll}s")
    worker_loop(max_workers=args.workers, poll_interval=args.poll, loop_once=args.once)
