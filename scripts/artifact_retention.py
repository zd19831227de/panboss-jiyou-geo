#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Clean unreferenced local artifacts.

Default is dry-run. Referenced evidence/media files are never deleted.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.artifact_retention import cleanup_artifacts, inspect_artifacts  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()

    if args.delete:
        result = cleanup_artifacts(args.days)
    else:
        result = inspect_artifacts(args.days)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
