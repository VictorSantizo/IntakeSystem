"""The connector command channel (reports #34 + #22, desk_commands.py).

#34 (Joshua, bounced #34's ancestor): "Can we get it to open a standard
windows upload file picker? So it feels familiar." A web page can't — but the
desk's scan connector can pop the real Explorer-style folder chooser and hand
the path back through the server.

#22: "Can we make it an 'Open Location' aka open in File Explorer button?"
Same channel, second verb.

The browser↔desk match is by client IP (exactly how /scan works); commands
queue on the server and the connector short-polls them with connector_token.
"""
import json
import os

import pytest

import desk_commands
import scan_connector
import scanflow
from test_folder_browser import _cabinet

TOKEN = "test-connector-token"
DESK = {"REMOTE_ADDR": "127.0.0.1"}          # the test client's own address
OTHER_DESK = {"REMOTE_ADDR": "10.9.9.9"}


@pytest.fixture(autouse=True)
def _fresh_channel():
    with desk_commands._LOCK:
        desk_commands._PENDING.clear()
        desk_commands._RESULTS.clear()
    yield


def _set_cfg(env, **kv):
    env.cfg.update(kv)
    with open(env.cfg_path, "w", encoding="utf-8") as f:
        json.dump(env.cfg, f, indent=2)


def _heartbeat(client, version="1.2", environ=DESK):
    _ = client.post("/api/connector/heartbeat", environ_overrides=dict(environ),
                    json={"token": TOKEN, "machine": "FRONT-DESK",
                          "user": "nancy", "scanner_present": True,
                          "scansnap_running": True, "profile_ok": True,
                          "version": version})
    assert _.status_code == 200


# ── desk status: can Browse go native on this desk? ──────────────────────────

def test_desk_status_reflects_connector_and_version(env, client, staff_client):
    _set_cfg(env, connector_token=TOKEN)
    c, _h = staff_client
    assert c.get("/api/desk/status").get_json() == {
        "connector": False, "commands": False, "machine": ""}
    _heartbeat(client, version="1.1")     # pre-command-channel connector
    st = c.get("/api/desk/status").get_json()
    assert st["connector"] is True and st["commands"] is False
    _heartbeat(client, version="1.2")
    st = c.get("/api/desk/status").get_json()
    assert st["commands"] is True and st["machine"] == "FRONT-DESK"


# ── pick_folder: the full round trip ─────────────────────────────────────────

def test_pick_folder_round_trip(env, client, staff_client):
    """Staff queues the dialog, the desk's connector collects it, the chosen
    path comes back to the staff poll."""
    root = _cabinet(env)
    _set_cfg(env, connector_token=TOKEN)
    _heartbeat(client)
    c, _h = staff_client

    seed = os.path.join(root, "Files")
    r = c.post("/api/desk/pick_folder", json={"initial": seed}, headers=_h)
    assert r.status_code == 200
    cmd_id = r.get_json()["cmd_id"]
    assert c.get(f"/api/desk/pick_folder/{cmd_id}").get_json()["status"] == "waiting"

    # The connector (no login, token only) collects — and the initial dir
    # was converted to UNC so the desk understands it (house rule #33/#36).
    got = client.post("/api/connector/commands", environ_overrides=dict(DESK),
                      json={"token": TOKEN, "machine": "FRONT-DESK"}).get_json()
    assert len(got["commands"]) == 1
    cmd = got["commands"][0]
    assert cmd["type"] == "pick_folder" and cmd["id"] == cmd_id
    assert cmd["initial"].startswith("\\\\TESTSRV\\Cabinet")
    # collected means gone — the next poll is empty
    again = client.post("/api/connector/commands", environ_overrides=dict(DESK),
                        json={"token": TOKEN, "machine": "FRONT-DESK"}).get_json()
    assert again["commands"] == []

    chosen = r"Z:\Cabinet\Documents\BANK STATEMENTS"
    client.post("/api/connector/command_result",
                json={"token": TOKEN, "id": cmd_id, "status": "done",
                      "path": chosen})
    res = c.get(f"/api/desk/pick_folder/{cmd_id}").get_json()
    assert res == {"status": "done", "path": chosen}


def test_pick_folder_cancel_comes_back_as_cancelled(env, client, staff_client):
    _cabinet(env)
    _set_cfg(env, connector_token=TOKEN)
    _heartbeat(client)
    c, _h = staff_client
    cmd_id = c.post("/api/desk/pick_folder", json={}, headers=_h).get_json()["cmd_id"]
    client.post("/api/connector/command_result",
                json={"token": TOKEN, "id": cmd_id, "status": "cancelled"})
    assert c.get(f"/api/desk/pick_folder/{cmd_id}").get_json()["status"] == "cancelled"


def test_pick_folder_without_a_connector_is_refused(env, staff_client):
    _set_cfg(env, connector_token=TOKEN)
    c, _h = staff_client
    assert c.post("/api/desk/pick_folder", json={}, headers=_h).status_code == 409


def test_commands_stay_on_their_own_desk(env, client, staff_client):
    """A command queued from desk A must never reach desk B's connector."""
    _cabinet(env)
    _set_cfg(env, connector_token=TOKEN)
    _heartbeat(client)
    c, _h = staff_client
    c.post("/api/desk/pick_folder", json={}, headers=_h)
    other = client.post("/api/connector/commands",
                        environ_overrides=dict(OTHER_DESK),
                        json={"token": TOKEN, "machine": "BACK-DESK"}).get_json()
    assert other["commands"] == []
    mine = client.post("/api/connector/commands", environ_overrides=dict(DESK),
                       json={"token": TOKEN, "machine": "FRONT-DESK"}).get_json()
    assert len(mine["commands"]) == 1


def test_connector_endpoints_reject_bad_token(env, client, staff_client):
    _set_cfg(env, connector_token=TOKEN)
    assert client.post("/api/connector/commands",
                       json={"token": "wrong"}).status_code == 403
    assert client.post("/api/connector/command_result",
                       json={"token": "wrong", "id": "x"}).status_code == 403
    # No token configured -> fail closed.
    _set_cfg(env, connector_token="")
    assert client.post("/api/connector/commands",
                       json={"token": ""}).status_code == 403


def test_uncollected_command_expires_to_pending(env, client, staff_client):
    """Connector died between heartbeat and pickup: the page's poll must not
    wait forever — after the TTL the command is gone and the status says so."""
    _cabinet(env)
    _set_cfg(env, connector_token=TOKEN)
    _heartbeat(client)
    c, _h = staff_client
    cmd_id = c.post("/api/desk/pick_folder", json={}, headers=_h).get_json()["cmd_id"]
    with desk_commands._LOCK:
        for cmds in desk_commands._PENDING.values():
            for cmd in cmds:
                cmd["ts"] -= desk_commands.COMMAND_TTL_SECS + 5
    assert c.get(f"/api/desk/pick_folder/{cmd_id}").get_json()["status"] == "pending"


# ── open_explorer (#22) ──────────────────────────────────────────────────────

def test_open_explorer_folder_and_document(env, client, staff_client):
    root = _cabinet(env)
    _set_cfg(env, connector_token=TOKEN)
    _heartbeat(client)
    c, _h = staff_client

    folder = os.path.join(root, "Documents", "BANK STATEMENTS")
    assert c.post("/api/desk/open_explorer",
                  json={"path": folder}, headers=_h).status_code == 200
    doc = os.path.join(root, "Files", "somebill.pdf")   # parent exists = enough
    assert c.post("/api/desk/open_explorer",
                  json={"path": doc}, headers=_h).status_code == 200

    got = client.post("/api/connector/commands", environ_overrides=dict(DESK),
                      json={"token": TOKEN, "machine": "FRONT-DESK"}).get_json()
    by_select = {cmd["select"]: cmd for cmd in got["commands"]}
    assert by_select[False]["path"] == r"\\TESTSRV\Cabinet\Documents\BANK STATEMENTS"
    assert by_select[True]["path"] == r"\\TESTSRV\Cabinet\Files\somebill.pdf"


def test_open_explorer_refuses_paths_outside_the_cabinet(env, client, staff_client):
    _cabinet(env)
    _set_cfg(env, connector_token=TOKEN)
    _heartbeat(client)
    c, _h = staff_client
    for bad in (r"C:\Windows\System32", "", r"\\EVILSERVER\share\x"):
        r = c.post("/api/desk/open_explorer", json={"path": bad}, headers=_h)
        assert r.status_code == 400, bad
    # nothing reached the queue
    got = client.post("/api/connector/commands", environ_overrides=dict(DESK),
                      json={"token": TOKEN, "machine": "FRONT-DESK"}).get_json()
    assert got["commands"] == []


# ── connector-side pure pieces ───────────────────────────────────────────────

def test_explorer_invocation_shapes():
    assert scan_connector._explorer_args(r"\\S\c\Files", False) == \
        ["explorer", r"\\S\c\Files"]
    assert scan_connector._explorer_args(r"\\S\c\Files\doc.pdf", True) == \
        ["explorer", r"/select,\\S\c\Files\doc.pdf"]


def test_connector_ignores_unknown_and_non_unc_commands(monkeypatch):
    """A newer server or a poisoned path must not crash the loop or run
    anything: open_explorer only ever executes UNC paths."""
    launched = []
    monkeypatch.setattr(scan_connector.subprocess, "Popen",
                        lambda args, **kw: launched.append(args))
    scan_connector._handle_command("http://x", "t", {"type": "sing_a_song"})
    scan_connector._handle_command("http://x", "t",
                                   {"type": "open_explorer", "path": r"C:\Windows"})
    assert launched == []
    scan_connector._handle_command("http://x", "t",
                                   {"type": "open_explorer",
                                    "path": r"\\SRV\share\f", "select": False})
    assert launched == [["explorer", r"\\SRV\share\f"]]


# ── the page side ────────────────────────────────────────────────────────────

def test_entry_view_carries_the_open_explorer_button(env, staff_client, tmp_path):
    from database import insert_entry
    c, _h = staff_client
    f = tmp_path / "doc.pdf"
    f.write_text("x")
    eid = insert_entry({"vendor": "ACME", "doc_type": "Bill",
                        "date_received": "2026-08-05", "filed_path": str(f)})
    html = c.get(f"/entry/{eid}/view").get_data(as_text=True)
    assert 'data-kk="open-explorer"' in html
    assert "/api/desk/open_explorer" in html
    assert "/api/desk/status" in html


def test_folder_picker_goes_native_first(env, staff_client, tmp_path):
    """The Browse flow asks the desk before falling back to the modal."""
    import app as klr
    c, _h = staff_client
    f = tmp_path / "doc.pdf"
    f.write_text("x")
    sid = klr._queue_add({"pdf_path": str(f), "vendor": "Test"})
    html = c.get(f"/review/{sid}").get_data(as_text=True)
    assert "/api/desk/pick_folder" in html
    assert "fpNativeStart" in html
    assert "Select Folder" in html          # the Explorer-styled fallback
