"""
scanflow.py – "Scan straight into Klick Klack" (reports #23/#18/#13).

The /scan page plus the per-workstation connector registry behind it.

How a scan flows: a ScanSnap Home profile on each workstation saves straight
into the server's watched Unfiled Scans folder (from a workstation:
\\\\LACKSSERVER2021\\File-Cabinet\\IntakeSystem\\Unfiled Scans — always UNC,
drive letters differ per desk); the existing watcher/classifier queue it like
any other scan. Nothing in this module writes files or touches the queue — it only
OBSERVES, so the proven ingest path stays the only write path.

What this module adds:
  * /api/connector/heartbeat — each workstation runs scan_connector.py (started
    once by "Connect My Scanner.bat"); every ~20s it reports whether a ScanSnap
    is plugged in, ScanSnap software is running, and the Klick Klack profile
    exists. Token-authed (connector_token in config.json) because the connector
    has no login session; the server matches a browser to its workstation's
    connector by client IP address.
  * /scan — the page staff use: shows THIS machine's readiness, walks through
    connecting a machine that isn't set up yet, and while ready live-waits for
    the next scan to land, then jumps straight to its review form.
  * /api/scan/status + /api/scan/arrivals — the polling endpoints for that page.

Registered from app.py via register(app, ctx) so this file never imports app
(no circular import); ctx supplies load_config and a queue snapshot callable.
"""
import hmac
import os
import threading
import time

from flask import Blueprint, jsonify, render_template, request, send_file, session

import filing
import longpaths
from watcher import SUPPORTED_EXTS

bp = Blueprint("scanflow", __name__)

_ctx = {}                     # filled by register(): load_config, queue_items
_connectors = {}              # client IP -> last heartbeat dict (+ "ts")
_connectors_lock = threading.Lock()

# A connector heartbeats every ~20s; three missed beats = not connected.
FRESH_SECS = 75
CONNECTOR_FIELDS = ("machine", "user", "scanner_present", "scanner_model",
                    "scansnap_running", "profile_ok", "version")


def register(app, ctx):
    """Wire the blueprint into the Flask app. ctx must provide:
    load_config() -> dict, and queue_items() -> list[(scan_id, data_dict)]."""
    _ctx.update(ctx)
    app.register_blueprint(bp)


def _connector_for(ip):
    with _connectors_lock:
        state = dict(_connectors.get(ip) or {})
    if not state:
        return None
    state["fresh"] = (time.time() - state.get("ts", 0)) < FRESH_SECS
    state["ready"] = bool(state["fresh"] and state.get("scanner_present")
                          and state.get("scansnap_running"))
    return state


def _queue_baseline():
    ids = [sid for sid, _ in _ctx["queue_items"]()]
    return max(ids) if ids else 0


# A file this young is probably still being written or mid-classification —
# don't alarm anyone about it yet.
STRANDED_MIN_AGE_SECS = 180


def _stranded_files():
    """Supported files sitting in the scan folder that the review queue doesn't
    know about — the 'my scan never showed up' case (reports #42/#48). Purely
    an observation (nothing here writes); the fix path is the Check for new items sweep."""
    cfg = _ctx["load_config"]()
    folder = cfg.get("scan_folder", "")
    if not folder or not os.path.isdir(folder):
        return []
    queued = {os.path.normcase(os.path.normpath(d.get("pdf_path") or ""))
              for _, d in _ctx["queue_items"]()}
    now = time.time()
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    for name in names:
        if not name.lower().endswith(SUPPORTED_EXTS):
            continue
        p = os.path.join(folder, name)
        if not longpaths.isfile(p):
            continue
        if os.path.normcase(os.path.normpath(p)) in queued:
            continue
        try:
            age = now - longpaths.getmtime(p)
        except OSError:
            age = 0
        if age < STRANDED_MIN_AGE_SECS:
            continue
        out.append({
            "name": name if len(name) <= 90 else name[:87] + "...",
            "age_minutes": int(age // 60),
        })
    return out


@bp.route("/api/connector/heartbeat", methods=["POST"])
def connector_heartbeat():
    """Status report from a workstation's scan connector. No login session —
    authenticated by the shared connector_token instead (app.py exempts this
    path from the login and CSRF gates)."""
    cfg = _ctx["load_config"]()
    expected = (cfg.get("connector_token") or "").strip()
    payload = request.get_json(silent=True) or {}
    sent = str(payload.get("token") or "")
    if not expected or not hmac.compare_digest(expected, sent):
        return jsonify({"error": "bad token"}), 403
    state = {k: payload.get(k) for k in CONNECTOR_FIELDS}
    state["ts"] = time.time()
    ip = request.remote_addr or "?"
    with _connectors_lock:
        _connectors[ip] = state
        # Bound memory: this only ever holds one row per office machine.
        if len(_connectors) > 500:
            stale = sorted(_connectors.items(), key=lambda kv: kv[1].get("ts", 0))
            for k, _ in stale[:100]:
                _connectors.pop(k, None)
    resp = {"ok": True, "poll_seconds": 20}
    # Desktop\SCANS mirroring (Nancy's historical-reference ask, 2026-07-29):
    # config maps a MACHINE name to a staff member; that desk's connector is
    # told to copy the staff member's server-side Scan Archive down to the
    # local Desktop\SCANS folder. UNC (house rule) so any desk can reach it.
    machine = (payload.get("machine") or "").strip().upper()
    if machine:
        mirrors = cfg.get("scan_copy_mirrors") or {}
        staff = next((v for k, v in mirrors.items()
                      if (k or "").strip().upper() == machine), None)
        src = filing.scan_archive_dir(cfg, staff) if staff else None
        if src:
            resp["mirror"] = {"src": filing.display_path(src, cfg),
                              "dest": r"%USERPROFILE%\Desktop\SCANS"}
    return jsonify(resp)


@bp.route("/api/scan/status")
def scan_status():
    """This browser's workstation readiness + the queue baseline for arrival
    watching. Polled by /scan every few seconds (exempt from the idle clock)."""
    return jsonify({
        "connector": _connector_for(request.remote_addr or "?"),
        "baseline": _queue_baseline(),
        "stranded": _stranded_files(),
    })


@bp.route("/api/scan/arrivals")
def scan_arrivals():
    """Queue items newer than ?since= that came from the PHYSICAL scan folder.
    Email attachments arriving mid-wait must not hijack the jump to review,
    so anything outside scan_folder is ignored."""
    try:
        since = int(request.args.get("since", 0))
    except ValueError:
        since = 0
    cfg = _ctx["load_config"]()
    scan_folder = cfg.get("scan_folder", "")
    items = []
    for sid, d in _ctx["queue_items"]():
        if sid <= since:
            continue
        p = (d.get("pdf_path") or "")
        if not scan_folder or not os.path.normcase(os.path.normpath(p)).startswith(
                os.path.normcase(os.path.normpath(scan_folder)) + os.sep):
            continue
        items.append({
            "id": sid,
            "label": (d.get("vendor") or os.path.basename(p) or f"Scan {sid}"),
            "batch": d.get("batch_number") or "",
        })
    items.sort(key=lambda x: x["id"])
    # Auto-confirmed scans never enter the queue, so they'd never "arrive" —
    # report recent ones separately so the page can say "filed itself" instead
    # of leaving the person at the scanner staring at silence (#35's spirit).
    try:
        since_ts = float(request.args.get("since_ts", "1e15"))
    except ValueError:
        since_ts = 1e15
    auto = _ctx.get("auto_filed", lambda ts: [])(since_ts)
    return jsonify({"items": items, "auto_filed": auto, "now": time.time()})


@bp.route("/scan/connector-setup")
def connector_setup_download():
    """Serve the "Connect My Scanner" installer as a download, so joining a
    new machine never means browsing the server share (Joshua, 7/24). The
    downloaded copy locates the share on T: by itself; login required like
    every page (the connector token never travels in this file)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "Connect My Scanner.bat")
    return send_file(path, as_attachment=True,
                     download_name="Connect My Scanner.bat",
                     mimetype="application/octet-stream")


@bp.route("/scan")
def scan_page():
    cfg = _ctx["load_config"]()
    connector = _connector_for(request.remote_addr or "?")
    return render_template("scan.html",
                           connector=connector,
                           baseline=_queue_baseline(),
                           scan_share=r"\\LACKSSERVER2021\File-Cabinet\IntakeSystem",
                           user=session.get("user", ""),
                           cfg=cfg)
