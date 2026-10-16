"""Shared fixtures: a fully isolated app environment per test.

Every test runs against a TEMP database, TEMP config.json, and TEMP folders —
nothing here may ever touch the production intake.db or config.json. The app
modules are imported once (cheap) and re-pointed per test via monkeypatch.
"""
import json
import os
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database            # noqa: E402
import property_model      # noqa: E402
import classifier          # noqa: E402
import queue_state         # noqa: E402
import app as klr          # noqa: E402
import routes_ops_reports  # noqa: E402
import scanflow            # noqa: E402
import folder_index        # noqa: E402
import cabinet_watch       # noqa: E402

from werkzeug.security import generate_password_hash  # noqa: E402

TEST_CONFIG = {
    "scan_folder": "",     # filled per-test
    "email_folder": "",
    "filed_folder": "",
    "mail_accounts": [],
    "claude_api_key": "YOUR_API_KEY_HERE",   # regex fallback path in tests
    "entities": [
        {"code": "TRUST1", "name": "Test Trust One"},
        {"code": "PP", "name": "PP Properties"},
    ],
    "properties": [
        {"code": "T27", "address": "123 Example Ave, Sometown CA", "entity_code": "TRUST1"},
        {"code": "PP16", "address": "456 Sample St, Sometown CA", "entity_code": "PP"},
    ],
    "bill_types": [
        {"code": "ELEC", "description": "Electricity", "category": "Bill"},
        {"code": "RENT", "description": "Rent received", "category": "Income"},
    ],
    "staff": [
        {"name": "TestAdmin", "password": generate_password_hash("adminpw", method="pbkdf2:sha256"),
         "admin": True},
        {"name": "TestUser", "password": generate_password_hash("userpw", method="pbkdf2:sha256")},
    ],
    "session_timeout_minutes": 30,
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated environment. Yields a namespace with the temp paths."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))

    cfg = json.loads(json.dumps(TEST_CONFIG))   # deep copy
    for key in ("scan_folder", "email_folder", "filed_folder"):
        folder = tmp_path / key
        folder.mkdir()
        cfg[key] = str(folder)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    monkeypatch.setattr(klr, "CONFIG_PATH", str(cfg_path))
    monkeypatch.setattr(property_model, "CONFIG_PATH", str(cfg_path))
    monkeypatch.setattr(classifier, "load_config",
                        lambda: json.loads(cfg_path.read_text(encoding="utf-8")))

    database.init_db()
    property_model.init_property_schema()

    # shared in-memory state must not leak between tests. The queue/batch state
    # lives in queue_state now; klr.pending_scans is an alias for the same dict
    # (fine to clear through either), but _next_scan_id and _current_batch get
    # REBOUND, so they must be reset on the owning module or the reset is lost.
    queue_state.pending_scans.clear()
    queue_state._next_scan_id = 1
    queue_state._current_batch = None
    queue_state._jobs_running.clear()
    klr._login_failures.clear()
    klr._hl_cache.clear()
    klr._recent_auto_filed.clear()
    klr._mail_alert_cache.update({"at": 0.0, "problems": []})
    with scanflow._connectors_lock:
        scanflow._connectors.clear()
    with folder_index._idx_lock:
        folder_index._idx.update({"at": 0.0, "dirs": []})
    cabinet_watch._pending.clear()
    cabinet_watch._fail_counts.clear()
    cabinet_watch._entries_cache.update({"at": 0.0, "rows": []})
    while not cabinet_watch._work_q.empty():
        cabinet_watch._work_q.get_nowait()
    monkeypatch.setattr(queue_state, "BATCH_STATE_FILE",
                        str(tmp_path / "current_batch.json"))
    monkeypatch.setattr(klr, "_REPORT_SHOTS_DIR", str(tmp_path / "Bug Reports"))
    # Ops Reports must never read the real _OPS INDEX during tests.
    ops_dir = tmp_path / "ops_index"
    ops_dir.mkdir()
    monkeypatch.setattr(routes_ops_reports, "DEFAULT_OPS_DIR", str(ops_dir))

    yield types.SimpleNamespace(
        tmp=tmp_path, db=str(db_path), cfg_path=str(cfg_path), cfg=cfg,
        scan_folder=cfg["scan_folder"], email_folder=cfg["email_folder"],
        filed_folder=cfg["filed_folder"],
    )
    klr.pending_scans.clear()


@pytest.fixture()
def client(env):
    """Test client with no session (anonymous)."""
    return klr.app.test_client()


def _login_session(c, user):
    with c.session_transaction() as s:
        s["user"] = user
        s["last_active"] = time.time()
        s["csrf"] = "test-csrf-token"
    return {"X-CSRF-Token": "test-csrf-token"}


@pytest.fixture()
def staff_client(env):
    """Logged-in non-admin client. Returns (client, csrf_headers)."""
    c = klr.app.test_client()
    headers = _login_session(c, "TestUser")
    return c, headers


@pytest.fixture()
def admin_client(env):
    """Logged-in admin client. Returns (client, csrf_headers)."""
    c = klr.app.test_client()
    headers = _login_session(c, "TestAdmin")
    return c, headers


def make_pdf(path, text="INVOICE from ACME Utilities account 12-3456 amount due "
                        "$147.48 due date 07/15/2026 service address 123 Example Ave "
                        "please remit payment promptly thank you for your business"):
    """Write a minimal but valid one-page PDF with a real text layer."""
    # A hand-rolled PDF: catalog -> pages -> page -> contents (Helvetica text).
    lines = [t.encode("latin-1", "replace") for t in _wrap(text, 70)]
    parts = [b"BT /F1 10 Tf 40 750 Td 12 TL"]
    for ln in lines:
        parts.append(b"(" + ln.replace(b"(", b"[").replace(b")", b"]") + b") Tj T*")
    parts.append(b"ET")
    stream = b"\n".join(parts)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF").encode()
    with open(path, "wb") as f:
        f.write(bytes(out))
    return str(path)


def _wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines
