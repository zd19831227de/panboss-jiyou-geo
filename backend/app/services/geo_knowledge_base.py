"""Project-embedded GEO knowledge base file store."""
from __future__ import annotations

import json
import platform
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

if platform.system() != "Windows":
    import fcntl


ROOT = Path(__file__).resolve().parents[3]
KNOWLEDGE_DIR = ROOT / "geo" / "knowledge_base"
MANIFEST_PATH = KNOWLEDGE_DIR / "manifest.json"

RESOURCE_FILES = {
    "items": "brand_facts.json",
    "evidence": "evidence_sources.json",
    "questions": "question_matrix.json",
    "diagnostics": "citation_diagnostics.json",
}

SECRET_MARKERS = ("password", "token", "cookie", "secret", "api_key", "apikey", ".env")


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _resource_path(resource: str) -> Path:
    if resource not in RESOURCE_FILES:
        raise ValueError(f"unknown knowledge resource: {resource}")
    return KNOWLEDGE_DIR / RESOURCE_FILES[resource]


@contextmanager
def _resource_lock(resource: str):
    if resource not in RESOURCE_FILES:
        raise ValueError(f"unknown knowledge resource: {resource}")
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = KNOWLEDGE_DIR / f".{resource}.lock"
    if platform.system() != "Windows":
        with lock_path.open("w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    else:
        # Windows: no-op lock (single-user knowledge base, no concurrent writes expected)
        yield


def _load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp_path.replace(path)


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in SECRET_MARKERS):
                return True
            if _contains_secret_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def ensure_knowledge_files() -> None:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    for resource in RESOURCE_FILES:
        path = _resource_path(resource)
        if not path.exists():
            _write_json(path, [])


def knowledge_base_summary() -> dict[str, Any]:
    ensure_knowledge_files()
    manifest = _load_json(MANIFEST_PATH, {})
    resources = {}
    for resource, filename in RESOURCE_FILES.items():
        path = KNOWLEDGE_DIR / filename
        records = _load_json(path, [])
        resources[resource] = {
            "path": str(path.relative_to(ROOT)),
            "exists": path.exists(),
            "count": len(records) if isinstance(records, list) else 0,
        }
    return {
        "root": str(KNOWLEDGE_DIR.relative_to(ROOT)),
        "resource_scope": manifest.get("resource_scope"),
        "packaging_required": bool(manifest.get("packaging_required")),
        "resources": resources,
    }


def list_knowledge_records(resource: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    ensure_knowledge_files()
    records = _load_json(_resource_path(resource), [])
    if not isinstance(records, list):
        return []
    filters = {key: value for key, value in (filters or {}).items() if value not in (None, "")}
    if not filters:
        return records
    result = []
    for row in records:
        if all(str(row.get(key, "")) == str(value) for key, value in filters.items()):
            result.append(row)
    return result


def append_knowledge_record(resource: str, payload: dict[str, Any]) -> dict[str, Any]:
    ensure_knowledge_files()
    if _contains_secret_key(payload):
        raise ValueError("knowledge base records must not contain secret-like fields")
    with _resource_lock(resource):
        records = _load_json(_resource_path(resource), [])
        if not isinstance(records, list):
            records = []
        record = {
            "id": payload.get("id") or f"{resource}_{uuid.uuid4().hex[:12]}",
            "created_at": payload.get("created_at") or _now(),
            "updated_at": _now(),
            **payload,
        }
        records.append(record)
        _write_json(_resource_path(resource), records)
        return record


def delete_knowledge_record(resource: str, record_id: str) -> bool:
    ensure_knowledge_files()
    with _resource_lock(resource):
        records = _load_json(_resource_path(resource), [])
        if not isinstance(records, list):
            return False
        kept = [row for row in records if str(row.get("id")) != str(record_id)]
        if len(kept) == len(records):
            return False
        _write_json(_resource_path(resource), kept)
        return True
