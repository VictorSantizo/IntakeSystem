"""night_api.py — command-line for the issue-report dev API.

The ONE way unattended night-shift / dev sessions write report state, so the
command shape is always the same and sits on the project permission allowlist
(.claude/settings.json) — ad-hoc token-posting scripts trip permission
prompts that strand unattended runs (the 2026-08-05 1:30 AM lesson).
Spec: VALIDATION_WORKFLOW.md. Server side: routes_reports.py.

Usage (run from T:\\IntakeSystem, exactly this shape):
    ./python/python.exe night_api.py probe
    ./python/python.exe night_api.py note <report_id> <text...>
    ./python/python.exe night_api.py status <report_id> "In progress"|"Dev-resolved" [note...]
    ./python/python.exe night_api.py new <description...> [--page URL]
Optional on all but probe: --actor "Name" (default "Night Shift").

probe exits 0 when the running app serves the dev API, 3 when it predates it
(fall back to morning-report + queued server-console lines). Reads app_url +
night_shift_token from config.json beside this file. Never touches intake.db.
"""
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

USAGE = __doc__.split("Usage", 1)[1]

# Client-side mirror of the server's rule: Verified/closed are human-only.
ALLOWED_STATUSES = ("In progress", "Dev-resolved")


def build_payload(argv):
    """(api_path, payload) from CLI words — pure, tested. SystemExit on bad
    input, with usage on stderr."""
    args = list(argv)
    actor = "Night Shift"
    if "--actor" in args:
        i = args.index("--actor")
        if i + 1 >= len(args):
            raise SystemExit("--actor needs a value\nUsage" + USAGE)
        actor = args[i + 1].strip() or actor
        del args[i:i + 2]
    page = ""
    if "--page" in args:
        i = args.index("--page")
        if i + 1 >= len(args):
            raise SystemExit("--page needs a value\nUsage" + USAGE)
        page = args[i + 1].strip()
        del args[i:i + 2]
    if not args:
        raise SystemExit("Usage" + USAGE)
    cmd = args[0]
    if cmd == "probe":
        return ("probe", None)
    if cmd == "note":
        if len(args) < 3:
            raise SystemExit("note needs <report_id> <text...>\nUsage" + USAGE)
        try:
            rid = int(args[1])
        except ValueError:
            raise SystemExit(f"report_id must be a number, got {args[1]!r}")
        return ("/api/dev/report_note",
                {"report_id": rid, "text": " ".join(args[2:]), "actor": actor})
    if cmd == "status":
        if len(args) < 3:
            raise SystemExit("status needs <report_id> <status> [note...]\nUsage" + USAGE)
        try:
            rid = int(args[1])
        except ValueError:
            raise SystemExit(f"report_id must be a number, got {args[1]!r}")
        status = args[2]
        if status not in ALLOWED_STATUSES:
            raise SystemExit(f"status must be one of {list(ALLOWED_STATUSES)} - "
                             "Verified/closed are set by a person in the app, never here")
        payload = {"report_id": rid, "status": status, "actor": actor}
        note = " ".join(args[3:]).strip()
        if note:
            payload["note"] = note
        return ("/api/dev/report_status", payload)
    if cmd == "new":
        desc = " ".join(args[1:]).strip()
        if not desc:
            raise SystemExit("new needs <description...>\nUsage" + USAGE)
        payload = {"description": desc, "actor": actor}
        if page:
            payload["page_url"] = page
        return ("/api/dev/report_new", payload)
    raise SystemExit(f"unknown command {cmd!r}\nUsage" + USAGE)


def _post(app_url, path, payload, timeout=20):
    req = urllib.request.Request(app_url + path,
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main(argv):
    path, payload = build_payload(argv)
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    app_url = (cfg.get("app_url") or "http://192.168.10.240:5050").rstrip("/")
    if path == "probe":
        # Tokenless POST: new code answers 403/400; pre-API code 404/401.
        try:
            code, _ = _post(app_url, "/api/dev/report_note", {}, timeout=10)
        except Exception as e:
            print(f"not-live (app unreachable: {e})")
            return 3
        if code in (403, 400):
            print("live")
            return 0
        print(f"not-live (HTTP {code} - running app predates the dev API)")
        return 3
    token = (cfg.get("night_shift_token") or "").strip()
    if not token:
        raise SystemExit("night_shift_token missing from config.json - API off")
    payload["token"] = token
    code, body = _post(app_url, path, payload)
    print(f"HTTP {code} {body}")
    return 0 if code == 200 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
