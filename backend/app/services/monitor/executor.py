#!/usr/bin/env python3
"""
Subprocess monitor executor — 兼容旧调用方式
新增 monitor_batch 模式: 由 task_queue worker 调用

用法:
  python3 executor.py web <brand> <keyword> [timeout]
  python3 executor.py doubao <brand> <keyword> [timeout]
  python3 executor.py login doubao
  python3 executor.py check-all
"""
import sys, json, os

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(_SCRIPT_DIR)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'


def run():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: executor.py <mode> [args...]"}))
        return

    mode = sys.argv[1]

    if mode == 'login':
        platform = sys.argv[2] if len(sys.argv) > 2 else "doubao"
        from app.services.monitor.ai_monitor import login_platform
        result = login_platform(platform)
        print(json.dumps(result, ensure_ascii=False))

    elif mode == 'check-all':
        from app.services.monitor.ai_monitor import check_all_login_status
        result = check_all_login_status()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif mode == 'web':
        brand_name = sys.argv[2] if len(sys.argv) > 2 else ""
        keyword = sys.argv[3] if len(sys.argv) > 3 else ""
        keyword_word = sys.argv[3] if len(sys.argv) > 3 else ""
        from app.services.monitor.search_engine import WebSearchMonitor
        m = WebSearchMonitor()
        result = m.search(brand_name, keyword)
        m.close()
        print(json.dumps({
            "is_cited": result.is_cited,
            "total_results": result.total_results,
            "cited_sources": result.cited_sources,
            "snippet": result.snippet,
            "source_url": result.source_url,
            "source_domain": result.source_domain,
            "screenshot_path": result.screenshot_path,
            "notes": result.notes,
        }, ensure_ascii=False))

    elif mode in ('doubao', 'yuanbao', 'qianwen'):
        brand_name = sys.argv[2] if len(sys.argv) > 2 else ""
        keyword = sys.argv[3] if len(sys.argv) > 3 else ""
        timeout = int(sys.argv[4]) if len(sys.argv) > 4 else 90
        from app.services.monitor.ai_monitor import monitor_platform
        result = monitor_platform(mode, brand_name, keyword, timeout)
        print(json.dumps(result, ensure_ascii=False))

    else:
        # 尝试作为 platform+brand+keyword 的兼容模式
        from app.services.monitor.search_engine import WebSearchMonitor
        m = WebSearchMonitor()
        result = m.search(mode, sys.argv[2] if len(sys.argv) > 2 else "")
        m.close()
        print(json.dumps({
            "is_cited": result.is_cited,
            "total_results": result.total_results,
            "cited_sources": result.cited_sources,
            "notes": result.notes,
        }, ensure_ascii=False))


if __name__ == '__main__':
    run()
