r"""Desktop\SCANS copies (Nancy's ask, 2026-07-29): confirm-time per-user
Scan Archive copies, the connector's heartbeat mirror instruction, the
connector's local mirror step, and the /scan page's auto-filed visibility."""
import json
import os

import app as klr
import database
import filing
import scan_connector
import scanflow
from conftest import make_pdf

BILL = {
    "is_cover_sheet": False, "received_date": "2026-07-10",
    "stamped_received_date": None,
    "doc_type": "Bill", "ai_confidence": "High",
    "vendor": "ACME Utilities", "amount": 147.48, "due_date": "2026-07-15",
    "entity_code": "TRUST1", "entity_name": "Test Trust One",
    "property_codes": ["T27"], "property_addresses": [],
    "bill_type_code": "ELEC", "invoice_number": "INV-99", "account_number": "12-3456",
    "notes": "", "important": False, "important_reason": "",
    "extracted_text": "INVOICE amount due $147.48 " + "words " * 30,
}


def _queue(env, **overrides):
    r = json.loads(json.dumps(BILL))
    r.update(overrides)
    pdf = make_pdf(os.path.join(env.scan_folder, overrides.get("_name", "doc.pdf")))
    r["pdf_path"] = pdf
    r.setdefault("mail_type", "Physical Mail")
    return klr._queue_add(r)


def _confirm(c, headers, sid, extra=None):
    data = {"date_received": "2026-07-10", "doc_type": "Bill",
            "vendor": "ACME Utilities", "entity_code": "TRUST1",
            "property_codes": ["T27"], "amount": "147.48", "date_ok": "1"}
    data.update(extra or {})
    return c.post(f"/review/{sid}/confirm?vendor_ok=1&force=1",
                  headers=headers, data=data)


def _archive_root(env):
    return os.path.join(os.path.dirname(env.filed_folder), "Scan Archive")


# ── confirm-time copies ───────────────────────────────────────────────────────

def test_confirm_leaves_copy_in_users_scan_archive(env, staff_client):
    c, headers = staff_client
    sid = _queue(env)
    assert _confirm(c, headers, sid).status_code == 302
    e = database.get_all_entries()[0]
    copy_dir = os.path.join(_archive_root(env), "TestUser", "2026")
    copies = os.listdir(copy_dir)
    assert copies == [os.path.basename(e["filed_path"])]
    assert os.path.exists(e["filed_path"])            # the filed original stays


def test_email_confirm_makes_no_copy(env, staff_client):
    c, headers = staff_client
    sid = _queue(env, mail_type="Email")
    assert _confirm(c, headers, sid, {"mail_type": "Email"}).status_code == 302
    assert not os.path.isdir(_archive_root(env))


def test_scan_archive_dir_sanitizes_user(env):
    cfg = klr.load_config()
    d = filing.scan_archive_dir(cfg, 'Na/ncy:*?')
    assert os.path.basename(d) == "Nancy"
    assert filing.scan_archive_dir(cfg, "") is None


# ── auto-filed ring buffer ────────────────────────────────────────────────────

def test_auto_filed_since_filters(env):
    klr._recent_auto_filed.append({"at": 100.0, "id": 1, "record_no": "260728-001",
                                   "mail_type": "Physical Mail", "name": "A.PDF",
                                   "dir_unc": r"\\S\C\F"})
    klr._recent_auto_filed.append({"at": 200.0, "id": 2, "record_no": "",
                                   "mail_type": "Email", "name": "B.PDF",
                                   "dir_unc": ""})
    assert [x["id"] for x in klr._auto_filed_since(50)] == [1]
    assert klr._auto_filed_since(150) == []


def test_arrivals_reports_auto_filed(env, staff_client):
    c, _h = staff_client
    klr._recent_auto_filed.append({"at": 1e9, "id": 7, "record_no": "260729-001",
                                   "mail_type": "Physical Mail", "name": "X.PDF",
                                   "dir_unc": r"\\S\C\F"})
    d = c.get("/api/scan/arrivals?since=0&since_ts=0").get_json()
    assert [x["id"] for x in d["auto_filed"]] == [7]
    assert d["now"] > 0
    # Old clients (no since_ts) see none — nothing changes for them.
    d = c.get("/api/scan/arrivals?since=0").get_json()
    assert d["auto_filed"] == []


# ── heartbeat mirror instruction ──────────────────────────────────────────────

def _beat(c, machine):
    return c.post("/api/connector/heartbeat", json={
        "token": "test-connector-token", "machine": machine, "user": "nancy",
        "scanner_present": True, "scanner_model": "iX1600",
        "scansnap_running": True, "profile_ok": True, "version": "1.1"})


def test_heartbeat_returns_mirror_for_mapped_machine(env, client):
    env.cfg.update({"connector_token": "test-connector-token",
                    "scan_copy_mirrors": {"Nancy-PC": "Nancy"},
                    "server_root": str(env.tmp),
                    "server_root_unc": r"\\TESTSRV\Cabinet"})
    with open(env.cfg_path, "w", encoding="utf-8") as f:
        json.dump(env.cfg, f, indent=2)
    d = _beat(client, "NANCY-PC").get_json()            # case-insensitive match
    assert d["mirror"]["dest"] == r"%USERPROFILE%\Desktop\SCANS"
    assert d["mirror"]["src"].endswith(os.path.join("Scan Archive", "Nancy"))
    assert d["mirror"]["src"].startswith(r"\\TESTSRV\Cabinet")
    d = _beat(client, "OTHER-PC").get_json()
    assert "mirror" not in d


# ── connector mirror step (pure-local unit test) ──────────────────────────────

def test_maybe_mirror_copies_once_and_respects_deletions(env, tmp_path, monkeypatch):
    src = tmp_path / "Scan Archive" / "Nancy" / "2026"
    src.mkdir(parents=True)
    make_pdf(str(src / "07-10-26 TRUST1 ELEC - ACME - $147.48.PDF"))
    dest = tmp_path / "Desktop" / "SCANS"
    monkeypatch.setattr(scan_connector, "STATUS_DIR", str(tmp_path / "state"))
    mir = {"src": str(tmp_path / "Scan Archive" / "Nancy"), "dest": str(dest)}
    assert scan_connector.maybe_mirror(mir) == 1
    assert os.listdir(dest) == ["07-10-26 TRUST1 ELEC - ACME - $147.48.PDF"]
    # Second pass: nothing new — and a locally deleted file stays deleted.
    os.remove(dest / "07-10-26 TRUST1 ELEC - ACME - $147.48.PDF")
    assert scan_connector.maybe_mirror(mir) == 0
    assert os.listdir(dest) == []
