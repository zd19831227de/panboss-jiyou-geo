#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Static UI/API contract gate.

Checks that visible Vue click handlers are defined and direct frontend API
paths have backend routes. This catches shell buttons and broken endpoint
renames before they reach manual testing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"
ROUTERS = ROOT / "backend" / "app" / "routers"


class GateFailure(Exception):
    pass


def vue_files():
    return sorted((FRONTEND / "views").glob("*.vue")) + [FRONTEND / "App.vue"]


def normalize_click(expr: str) -> str | None:
    expr = expr.strip().strip('"').strip("'")
    if not expr or expr.startswith("$"):
        return None
    if "=" in expr and "=>" not in expr:
        return None
    if expr.startswith("(") or "=>" in expr:
        return None
    name = re.split(r"[\s(?.]", expr, 1)[0]
    if re.match(r"^[A-Za-z_]\w*$", name):
        return name
    return None


def check_click_handlers() -> list[str]:
    failures: list[str] = []
    for path in vue_files():
        text = path.read_text(encoding="utf-8")
        clicks = re.findall(r"@click(?:\.[\w.]+)?\s*=\s*\"([^\"]+)\"", text)
        names = {name for expr in clicks if (name := normalize_click(expr))}
        for name in sorted(names):
            patterns = [
                rf"\bconst\s+{re.escape(name)}\s*=",
                rf"\bfunction\s+{re.escape(name)}\s*\(",
                rf"\basync\s+function\s+{re.escape(name)}\s*\(",
            ]
            if not any(re.search(pattern, text) for pattern in patterns):
                failures.append(f"{path.relative_to(ROOT)} @click handler not defined: {name}")
    return failures


def router_prefixes() -> dict[str, list[tuple[str, str]]]:
    routes: dict[str, list[tuple[str, str]]] = {}
    for path in sorted(ROUTERS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        prefix_match = re.search(r"APIRouter\(prefix\s*=\s*['\"]([^'\"]+)['\"]", text)
        prefix = prefix_match.group(1) if prefix_match else ""
        for method, route in re.findall(r"@router\.(get|post|put|delete|patch)\(\s*['\"]([^'\"]*)['\"]", text):
            full = (prefix + route).replace("//", "/") or "/"
            routes.setdefault(method.upper(), []).append((full, str(path.relative_to(ROOT))))
    return routes


def path_matches(frontend_path: str, backend_path: str) -> bool:
    f = frontend_path.split("?", 1)[0].strip("/")
    b = backend_path.split("?", 1)[0].strip("/")
    fs = f.split("/") if f else []
    bs = b.split("/") if b else []
    if len(fs) != len(bs):
        return False
    for a, z in zip(fs, bs):
        if z.startswith("{") and z.endswith("}"):
            continue
        if a.startswith("${") or "${" in a:
            continue
        if a != z:
            return False
    return True


def extract_api_calls() -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []
    for path in sorted(FRONTEND.rglob("*.js")) + sorted((FRONTEND / "views").glob("*.vue")) + [FRONTEND / "stores" / "auth.js"]:
        text = path.read_text(encoding="utf-8")
        for method, _quote, url in re.findall(r"api\.(get|post|put|delete|patch)\(\s*([`'\"])(/api/.*?)(?:\2)", text):
            calls.append((method.upper(), url, str(path.relative_to(ROOT))))
        for quote, url in re.findall(r"fetch\(\s*([`'\"])(/api/.*?)(?:\1)", text):
            method = "GET"
            tail = text[text.find(f"fetch({quote}{url}") : text.find(f"fetch({quote}{url}") + 220]
            m = re.search(r"method\s*:\s*['\"](\w+)['\"]", tail)
            if m:
                method = m.group(1).upper()
            calls.append((method, url, str(path.relative_to(ROOT))))
    return calls


def normalize_frontend_url(url: str) -> str:
    url = re.sub(r"\$\{\s*query\s*\}$", "", url)
    url = re.sub(r"\$\{\s*[^}]*query[^}]*\}$", "", url)
    url = url.replace("${platform}", "{platform}")
    url = url.replace("${row.platform}", "{platform}")
    url = re.sub(r"\$\{[^}]+id[^}]*\}", "{id}", url)
    url = re.sub(r"\$\{[^}]+Id[^}]*\}", "{id}", url)
    url = re.sub(r"\$\{[^}]+\}", "{value}", url)
    return url


def check_api_routes() -> list[str]:
    routes = router_prefixes()
    failures: list[str] = []
    for method, raw_url, source in extract_api_calls():
        url = normalize_frontend_url(raw_url)
        if method not in routes:
            failures.append(f"{source} {method} {raw_url} has no backend method")
            continue
        if not any(path_matches(url, backend) for backend, _ in routes[method]):
            failures.append(f"{source} {method} {raw_url} has no backend route")
    return failures


def check_no_shell_ui_regressions() -> list[str]:
    failures: list[str] = []
    keyword_view = FRONTEND / "views" / "KeywordList.vue"
    text = keyword_view.read_text(encoding="utf-8")
    if "品牌词示例" in text or "产品词示例" in text:
        failures.append("KeywordList template download must not contain example business data")
    if "window.location.href" in text:
        failures.append("KeywordList batch monitor must use router navigation, not raw window.location.href")
    if "router.push({ path: '/monitor'" not in text or "keyword_ids: selectedIds.value.join(',')" not in text:
        failures.append("KeywordList batch monitor must pass brand_id and keyword_ids to MonitorCenter")
    if '@click="handleImport"' in text and ':disabled="!filterBrandId"' not in text:
        failures.append("Keyword import button must be disabled until a brand is selected")
    router = (FRONTEND / "router" / "index.js").read_text(encoding="utf-8")
    if "path: '/command'" not in router or "redirect: '/command-center'" not in router:
        failures.append("legacy /command route must redirect to /command-center")
    route_paths = set(re.findall(r"path:\s*['\"]([^'\"]+)['\"]", router))
    menu_text = (FRONTEND / "App.vue").read_text(encoding="utf-8")
    menu_paths = set(re.findall(r"<el-menu-item\s+index=\"([^\"]+)\"", menu_text))
    missing = sorted(path for path in menu_paths if path not in route_paths)
    if missing:
        failures.append(f"sidebar menu route missing from router: {', '.join(missing)}")
    command_view = FRONTEND / "views" / "CommandCenter.vue"
    command_text = command_view.read_text(encoding="utf-8")
    if "if (!res.ok) throw new Error" not in command_text:
        failures.append("CommandCenter must surface backend failure reasons")
    operations_text = (FRONTEND / "views" / "Operations.vue").read_text(encoding="utf-8")
    if "workflow_json_text" not in operations_text or "requires_manual_publish" not in operations_text:
        failures.append("Operations workflow editor must expose RPA workflow JSON and manual publish gate")
    if 'label="已测试" value="tested"' in operations_text or 'label="启用" value="active"' in operations_text:
        failures.append("Operations workflow editor must not allow manual tested/active selection")
    if "workflowForm.status" in operations_text or "status: workflowForm.status" in operations_text:
        failures.append("Operations workflow editor must not submit workflow status directly")
    if "current_status" not in operations_text or "manual_status" not in operations_text:
        failures.append("Operations workflow editor must show read-only current status and separate manual downgrade status")
    if "workflowSelectorReady" not in operations_text or "canTestWorkflow" not in operations_text or "workflowTestGateReason" not in operations_text:
        failures.append("Operations workflow test button must enforce fixed selector readiness")
    if "selectedWorkflowPlatform" not in operations_text or "loadWorkflowTestsByPlatform" not in operations_text or "@click=\"loadWorkflowTests(row)\"" not in operations_text:
        failures.append("Operations workflow test records must be scoped to an explicit selected platform")
    if "discoverWorkflowSelectors" not in operations_text or "discover-selectors" not in operations_text:
        failures.append("Operations must expose selector discovery for external publishing workflows")
    if "activeWorkflows" not in operations_text or "v-for=\"workflow in activeWorkflows\"" not in operations_text:
        failures.append("Operations platform selects must be driven by backend workflows")
    if 'value="wechat"' in operations_text or '公众号草稿' in operations_text:
        failures.append("Operations must not expose wechat as an active publishing option")
    if "['tested', 'active'].includes(workflow.status)" not in operations_text or "!!account" not in operations_text:
        failures.append("Operations create publish button must enforce workflow status and account binding")
    if "publishGateReason" not in operations_text or "发布门禁" not in operations_text:
        failures.append("Operations content table must show publish gate reasons")
    if "publish_gate_reason" not in operations_text or "workflow_status" not in operations_text:
        failures.append("Operations publish task table must show workflow status and publish gate reason")
    if "brandScore(savedBrand)" in operations_text or "savedBrand && brandScore" in operations_text:
        failures.append("Operations must preserve the user's saved brand selection even before operationScore is nonzero")
    if "executeStrategyActions" not in operations_text or "canExecuteStrategyActions" not in operations_text:
        failures.append("Operations must expose P4 confirmed strategy execution controls")
    if "先勾选执行项并点击确认" not in operations_text:
        failures.append("Operations must explain why strategy execution is disabled when no action is confirmed")
    if "executingStrategy" not in operations_text or ":loading=\"executingStrategy\"" not in operations_text:
        failures.append("Operations strategy execution button must show a loading state")
    if "执行结果摘要" not in operations_text or "strategyExecutionSummary" not in operations_text or "strategyFailedActions" not in operations_text:
        failures.append("Operations must persistently show strategy execution result and failed reasons")
    if "strategyBlockedActions" not in operations_text or "待处理原因" not in operations_text or "row.status === 'blocked'" not in operations_text:
        failures.append("Operations must show business-gate blocked strategy actions separately from failures")
    if "先到“内容生产”审核通过 AI 草稿" not in operations_text:
        failures.append("Operations must explain the next business gate after strategy execution")
    if 'label="GEO知识库"' not in operations_text or "knowledgeResourceOptions" not in operations_text:
        failures.append("Operations must expose a GEO knowledge base management tab")
    for resource in ["items", "evidence", "questions", "diagnostics"]:
        if f"value: '{resource}'" not in operations_text and f"label=\"{resource}\"" not in operations_text:
            failures.append(f"Operations knowledge base UI must expose resource: {resource}")
    if "/api/operations/knowledge" not in operations_text or "/api/operations/knowledge/${knowledgeResource.value}" not in operations_text:
        failures.append("Operations knowledge base UI must call knowledge summary and resource endpoints")
    if "method: 'POST'" not in operations_text or "saveKnowledgeRecord" not in operations_text or "写入知识库" not in operations_text:
        failures.append("Operations knowledge base UI must provide a real write action")
    if "geo/knowledge_base" not in operations_text or "封装必须包含 geo/" not in operations_text:
        failures.append("Operations knowledge base UI must state project-embedded packaging requirement")
    monitor_text = (FRONTEND / "views" / "MonitorCenter.vue").read_text(encoding="utf-8")
    if "applyRouteSelection" not in monitor_text or "route.query.brand_id" not in monitor_text or "route.query.keyword_ids" not in monitor_text:
        failures.append("MonitorCenter must apply brand_id and keyword_ids from route query")
    if "catch { /* ignore */ }" in monitor_text:
        failures.append("MonitorCenter must not silently ignore task list failures")
    if "catch { ElMessage.error('获取结果失败') }" in monitor_text:
        failures.append("MonitorCenter result detail failures must include the backend reason")
    if "catch {}" in text:
        failures.append("KeywordList must not silently ignore search failures")
    return failures


def main() -> int:
    failures = check_click_handlers() + check_api_routes() + check_no_shell_ui_regressions()
    if failures:
        print(json.dumps({"status": "failed", "failures": failures}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"status": "passed", "checks": ["click handlers", "frontend api routes", "menu routes", "no shell regressions"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
