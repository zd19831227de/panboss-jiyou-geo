#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Commercial acceptance runner.

Runs the production gates in a fixed order so temporary writes from one gate
cannot create false failures in another gate.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
from _geo_ports import BASE_URL
from _geo_auth import auth_headers

PYTHON = os.environ.get("GEO_PYTHON", sys.executable)
# Windows 上 npm 通常是 npm.cmd，shutil.which 在某些 PATH 下找不到，需要补 .cmd/.bat 探测。
NPM = shutil.which("npm") or shutil.which("npm.cmd") or shutil.which("npm.bat") or "npm"
STEP_TIMEOUT_SECONDS = 180
# operations gate 内含 publishing_workflow（浏览器 RPA）+ production_gate，
# 真实运行很可能 >120s，原 override 比默认还短会误判失败，放宽到 600s。
STEP_TIMEOUT_OVERRIDES = {
    "operations gate": 600,
}


def run_step(name: str, cmd: list[str], cwd: Path | None = None) -> dict:
    started = time.time()
    timeout = STEP_TIMEOUT_OVERRIDES.get(name, STEP_TIMEOUT_SECONDS)
    env = os.environ.copy()
    env.setdefault("GEO_BASE_URL", BASE_URL)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd or ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env=env,
            encoding="utf-8",
            errors="replace",
        )
        return {
            "name": name,
            "passed": proc.returncode == 0,
            "seconds": round(time.time() - started, 2),
            "command": " ".join(cmd),
            "output": proc.stdout[-8000:],
        }
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return {
            "name": name,
            "passed": False,
            "seconds": timeout,
            "command": " ".join(cmd),
            "output": f"步骤超时 {timeout}s，已终止。{output[-4000:]}",
        }


def ensure_backend() -> dict:
    try:
        with urllib.request.urlopen(BASE_URL + "/health", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"name": "backend health", "passed": data.get("status") == "healthy", "output": json.dumps(data, ensure_ascii=False)}
    except Exception as exc:
        return {"name": "backend health", "passed": False, "output": str(exc)}


def ensure_platform_browsers() -> dict:
    results = {}
    try:
        for platform in ["doubao", "qianwen", "yuanbao"]:
            req = urllib.request.Request(BASE_URL + f"/api/platform-sessions/{platform}/open-login", headers=auth_headers(), method="POST")
            with urllib.request.urlopen(req, timeout=45) as resp:
                results[platform] = json.loads(resp.read().decode("utf-8"))
        sessions = json.loads(urllib.request.urlopen(urllib.request.Request(BASE_URL + "/api/platform-sessions", headers=auth_headers()), timeout=45).read().decode("utf-8"))
        return {"name": "platform browser recovery", "passed": True, "output": json.dumps(sessions, ensure_ascii=False)}
    except Exception as exc:
        return {"name": "platform browser recovery", "passed": True, "output": f"非阻断预热失败：{exc}"}


def main() -> int:
    step_specs = [
        ("backend health", ensure_backend),
        ("platform browser recovery", ensure_platform_browsers),
        ("frontend build", lambda: run_step("frontend build", [NPM, "run", "build"], ROOT / "frontend")),
        ("full evidence audit", lambda: run_step("full evidence audit", [PYTHON, "scripts/full_evidence_audit.py"])),
        ("data integrity", lambda: run_step("data integrity", [PYTHON, "scripts/data_integrity.py"])),
        ("ui contract gate", lambda: run_step("ui contract gate", [PYTHON, "scripts/ui_contract_gate.py"])),
        ("evidence gate", lambda: run_step("evidence gate", [PYTHON, "scripts/evidence_gate.py"])),
        ("page gate", lambda: run_step("page gate", [PYTHON, "scripts/page_gate.py"])),
        ("publishing workflow gate", lambda: run_step("publishing workflow gate", [PYTHON, "scripts/publishing_workflow_gate.py"])),
        ("operations gate", lambda: run_step("operations gate", [PYTHON, "scripts/operations_gate.py"])),
        ("final delivery audit", lambda: run_step("final delivery audit", [PYTHON, "scripts/final_delivery_audit.py"])),
        ("production gate", lambda: run_step("production gate", [PYTHON, "scripts/production_gate.py"])),
    ]
    steps = []
    for name, runner in step_specs:
        print(json.dumps({"running": name}, ensure_ascii=False), flush=True)
        result = runner()
        steps.append(result)
        print(json.dumps({
            "finished": result["name"],
            "passed": result["passed"],
            "seconds": result.get("seconds"),
        }, ensure_ascii=False), flush=True)

    failed = [step for step in steps if not step["passed"]]
    summary = {
        "status": "passed" if not failed else "failed",
        "passed": [step["name"] for step in steps if step["passed"]],
        "failed": [{"name": step["name"], "output": step["output"]} for step in failed],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
