#!/usr/bin/env python3
import argparse
import json
import sys
from dataclasses import asdict

# Windows 控制台默认 GBK：结果 JSON 带平台文案/emoji 时 print 直接
# UnicodeEncodeError 崩溃（父进程按 UTF-8 解码，两边必须一致）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, "backend")

from app.services.publishing_rpa import test_publish_workflow


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True)
    parser.add_argument("--publish-url", default="")
    parser.add_argument("--login-url", default="")
    parser.add_argument("--workflow-json", default="{}")
    parser.add_argument("--account-name", default="workflow_default")
    args = parser.parse_args()
    try:
        workflow = json.loads(args.workflow_json or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"status": "failed", "failure_reason": f"工作流 JSON 无效: {exc}"}, ensure_ascii=False))
        return 0
    result = test_publish_workflow(
        args.platform, args.publish_url, args.login_url, workflow, account_name=args.account_name
    )
    print(json.dumps(asdict(result), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
