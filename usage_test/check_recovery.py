import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
report = ROOT / "RUNTIME_RECOVERY_STATUS.md"
if not report.exists():
    print("report not found")
    raise SystemExit(1)

text = report.read_text(encoding="utf-8")
parts = text.split("```json")
if len(parts) < 2:
    print("no json block")
    raise SystemExit(1)

data = json.loads(parts[1].split("```")[0])
print("status:", data["status"])
print("actions:", data["actions"])
print("warnings count:", len(data["warnings"]))
