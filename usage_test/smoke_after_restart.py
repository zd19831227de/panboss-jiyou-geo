import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:18770"


def req(method, path, data=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None if data is None else json.dumps(data).encode()
    r = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw[:300]}
        return e.code, payload


def main():
    print("1. health", req("GET", "/health")[0])

    code, login = req("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
    print("2. login", code, list(login.keys()) if isinstance(login, dict) else login)
    token = None
    if isinstance(login, dict):
        token = login.get("access_token") or login.get("token")
        if not token:
            token = (login.get("data") or {}).get("access_token") or (login.get("data") or {}).get("token")
    print("   token?", bool(token))

    code, sessions = req("GET", "/api/platform-sessions", token=token)
    print("3. platform-sessions", code)
    if isinstance(sessions, dict):
        items = sessions.get("sessions") or sessions.get("items") or sessions
        if isinstance(items, dict):
            for k, v in items.items():
                if isinstance(v, dict):
                    print(f"   {k}: status={v.get('status')} logged_in={v.get('is_logged_in')}")
        elif isinstance(items, list):
            for v in items[:8]:
                print(f"   {v.get('platform')}: status={v.get('status')} logged_in={v.get('is_logged_in')}")
        else:
            print("   keys", list(sessions.keys()))
    elif isinstance(sessions, list):
        for v in sessions[:8]:
            print(f"   {v.get('platform')}: status={v.get('status')} logged_in={v.get('is_logged_in')}")

    code, sysinfo = req("GET", "/api/system/status", token=token)
    print("4. system/status", code, str(sysinfo)[:200])

    code, am = req("GET", "/api/system/auto-monitor", token=token)
    print("5. auto-monitor", code, am)

    code, wf = req("GET", "/api/operations/publish/workflows", token=token)
    print("6. publish workflows", code)
    rows = wf if isinstance(wf, list) else (wf.get("items") or wf.get("workflows") or []) if isinstance(wf, dict) else []
    for row in rows:
        cap = row.get("capability") or {}
        print(
            f"   {row.get('platform')}: level={cap.get('level')} recover={cap.get('url_recover')} rpa={cap.get('rpa')}"
        )

    for path in ["/", "/login", "/settings"]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:15173{path}", timeout=10) as resp:
                print(f"7. frontend {path}", resp.status, "len", len(resp.read()))
        except Exception as e:
            print(f"7. frontend {path} FAIL", e)

    # config sanity via import in same venv process after API checks
    from app.core.config import settings

    print("8. settings.PORT", settings.PORT, settings.public_api_base)
    print("DONE")


if __name__ == "__main__":
    main()
