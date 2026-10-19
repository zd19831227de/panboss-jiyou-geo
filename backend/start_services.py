"""DEPRECATED — do not use this script.

Canonical launcher (Windows ports 18770 / 15173 + worker):

    backend\\.venv\\Scripts\\python.exe start_services.py

run from the repository root (any path, the launcher resolves paths itself).
"""
import sys

print(
    "ERROR: backend/start_services.py is deprecated (old ports 8000/5173).\n"
    "Use the repo-root start_services.py instead:\n"
    "  backend\\.venv\\Scripts\\python.exe start_services.py\n",
    file=sys.stderr,
)
sys.exit(1)
