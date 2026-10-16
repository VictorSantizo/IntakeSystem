r"""desk_commands.py — the connector command channel (reports #34 + #22).

A web page cannot open a native Windows dialog or File Explorer: upload
pickers never reveal a folder path, browsers expose no folder dialog on plain
HTTP, and even the HTTPS-only API returns a folder NAME, not a path. But every
desk already runs our own native program — the scan connector — and it CAN.

So the browser asks the SERVER, and the desk's connector picks the request up
and does the native work:

    staff browser ──POST /api/desk/...──▶ server queue (this module)
    connector     ──POST /api/connector/commands (every ~2.5s)──▶ takes them
    connector     ──POST /api/connector/command_result──▶ result stored
    staff browser ──GET  /api/desk/pick_folder/<id> (1/s)──▶ gets the path

Everything stays outbound-from-the-desk (no listener on workstations, no
firewall prompts, no browser localhost rules); the browser↔desk match is by
client IP, exactly how /scan matches a browser to its machine's connector.
Commands the connector understands (scan_connector.py ≥ 1.2):

  * pick_folder   — pop the real Windows folder chooser (the modern
                    Explorer-style IFileDialog); returns the chosen path.
  * open_explorer — open File Explorer at a cabinet path (report #22),
                    with the document pre-selected when it names a file.

Trust surface: staff endpoints ride the normal login session + CSRF; the two
connector endpoints are token-authed with connector_token (same as the
heartbeat) and exempted from the login/CSRF gates in app.py. A command can
only reach the connector at the SAME client IP that filed it never another
desk. open_explorer only ever sends paths that resolve inside the file
cabinet, as full UNC (house rule, #33/#36) so every desk can open them.

Nothing here writes files or touches the database; the chosen folder lands in
a form field, and the move/file that follows goes through the same validated
paths as ever (filing.validate_filed_dir).
"""
import os
import threading
import time
import uuid

from flask import jsonify, request

import filing

_M = None                     # the app module; set by register()

_LOCK = threading.Lock()
_PENDING = {}                 # client ip -> [command dict, ...]
_RESULTS = {}                 # cmd_id -> result dict (+ "ts")

# A dialog can legitimately sit open for minutes while someone browses; but a
# command nobody collected, or a result nobody read, is garbage after this.
COMMAND_TTL_SECS = 180        # uncollected command: connector gone -> give up
RESULT_TTL_SECS = 15 * 60


def register(app, appmod):
    global _M
    _M = appmod
    r = app.add_url_rule
    # Staff side (login session + CSRF, like every page).
    r("/api/desk/status", "desk_status", desk_status)
    r("/api/desk/pick_folder", "desk_pick_folder", desk_pick_folder,
      methods=["POST"])
    r("/api/desk/pick_folder/<cmd_id>", "desk_pick_folder_result",
      desk_pick_folder_result)
    r("/api/desk/open_explorer", "desk_open_explorer", desk_open_explorer,
      methods=["POST"])
    # Connector side (connector_token; exempted from login/CSRF in app.py).
    r("/api/connector/commands", "connector_commands", connector_commands,
      methods=["POST"])
    r("/api/connector/command_result", "connector_command_result",
      connector_command_result, methods=["POST"])


def _gc(now=None):
    """Call with _LOCK held."""
    now = now or time.time()
    for ip in list(_PENDING):
        _PENDING[ip] = [c for c in _PENDING[ip]
                        if now - c["ts"] < COMMAND_TTL_SECS]
        if not _PENDING[ip]:
            del _PENDING[ip]
    for cid in list(_RESULTS):
        if now - _RESULTS[cid]["ts"] > RESULT_TTL_SECS:
            del _RESULTS[cid]


def _desk_connector():
    """The fresh connector for the browser's own desk, or None. Import is at
    call time because scanflow registers after this module is loaded."""
    import scanflow
    state = scanflow._connector_for(request.remote_addr or "?")
    return state if (state and state.get("fresh")) else None


def _enqueue(cmd_type, **fields):
    cmd = {"id": uuid.uuid4().hex, "type": cmd_type, "ts": time.time(), **fields}
    ip = request.remote_addr or "?"
    with _LOCK:
        _gc()
        _PENDING.setdefault(ip, []).append(cmd)
    return cmd["id"]


# ── Staff side ────────────────────────────────────────────────────────────────

def desk_status():
    """Is this desk's connector alive (and new enough to run commands)?
    The pages use it to decide whether Browse can go native."""
    state = _desk_connector()
    try:
        version = float(state.get("version") or 0) if state else 0.0
    except ValueError:
        version = 0.0
    return jsonify({"connector": bool(state),
                    "commands": bool(state) and version >= 1.2,
                    "machine": (state or {}).get("machine") or ""})


def desk_pick_folder():
    """Ask this desk's connector to pop the real Windows folder chooser.
    Returns a cmd_id the page polls; the browser never talks to the desk."""
    if not _desk_connector():
        return jsonify({"error": "no connector on this desk"}), 409
    initial = (request.get_json(silent=True) or {}).get("initial") or ""
    # The dialog starts where the field pointed; a desk understands the UNC
    # form of a server path (drive letters differ per desk, #33/#36).
    if initial:
        initial = filing.display_path(initial, _M.load_config())
    cmd_id = _enqueue("pick_folder", initial=initial)
    return jsonify({"ok": True, "cmd_id": cmd_id})


def desk_pick_folder_result(cmd_id):
    with _LOCK:
        _gc()
        res = _RESULTS.get(cmd_id)
        ip_queue = _PENDING.get(request.remote_addr or "?", [])
        still_queued = any(c["id"] == cmd_id for c in ip_queue)
    if res:
        return jsonify({"status": res.get("status") or "error",
                        "path": res.get("path") or ""})
    # Not picked up before its TTL means the connector never came for it.
    return jsonify({"status": "waiting" if still_queued else "pending"})


def desk_open_explorer():
    """Report #22: open File Explorer at a record's document (or a folder).
    Fire-and-forget — Explorer opening on the desk IS the result."""
    if not _desk_connector():
        return jsonify({"error": "no connector on this desk"}), 409
    cfg = _M.load_config()
    raw = (request.get_json(silent=True) or {}).get("path") or ""
    # Only file-cabinet locations, and always as UNC so the desk can open it.
    resolved = filing.resolve_client_dir(raw, cfg) if raw else None
    if resolved is None and raw:
        # A document PATH: re-root by its parent folder, keep the filename.
        parent = filing.resolve_client_dir(os.path.dirname(raw), cfg)
        if parent:
            resolved = os.path.join(parent, os.path.basename(raw))
    if not resolved:
        return jsonify({"error": "not a file-cabinet path"}), 400
    # File vs folder by what's actually on disk (validate_filed_dir checks
    # containment, not existence, so a document path can pass it whole).
    # A file gets `explorer /select,` — plain `explorer <file>` would LAUNCH
    # the document instead of showing it in its folder.
    is_file = not os.path.isdir(resolved)
    if is_file and not os.path.isdir(os.path.dirname(resolved)):
        return jsonify({"error": "not a file-cabinet path"}), 400
    unc = filing.display_path(resolved, cfg)
    _enqueue("open_explorer", path=unc, select=is_file)
    return jsonify({"ok": True})


# ── Connector side (token-authed) ─────────────────────────────────────────────

def _check_token():
    import hmac
    expected = (_M.load_config().get("connector_token") or "").strip()
    sent = str((request.get_json(silent=True) or {}).get("token") or "")
    return bool(expected) and hmac.compare_digest(expected, sent)


def connector_commands():
    """The desk's connector collects whatever its browser-mates queued.
    Short-poll on purpose: waitress has 8 threads and a long-poll from every
    desk would hold them; this is a dict lookup every ~2.5s instead."""
    if not _check_token():
        return jsonify({"error": "bad token"}), 403
    ip = request.remote_addr or "?"
    with _LOCK:
        _gc()
        cmds = _PENDING.pop(ip, [])
    return jsonify({"commands": [{k: v for k, v in c.items() if k != "ts"}
                                 for c in cmds],
                    "poll_seconds": 2.5})


def connector_command_result():
    if not _check_token():
        return jsonify({"error": "bad token"}), 403
    payload = request.get_json(silent=True) or {}
    cmd_id = str(payload.get("id") or "")
    if not cmd_id:
        return jsonify({"error": "no command id"}), 400
    status = str(payload.get("status") or "error")
    if status not in ("done", "cancelled", "error"):
        status = "error"
    with _LOCK:
        _gc()
        _RESULTS[cmd_id] = {"status": status,
                            "path": str(payload.get("path") or ""),
                            "ts": time.time()}
    return jsonify({"ok": True})
