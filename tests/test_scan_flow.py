"""Scan straight into Klick Klack (reports #23/#18/#13): the /scan page,
the workstation-connector heartbeat registry, arrival watching, the File-as
filename/folder suggestions, and the filing overrides on confirm.
"""
import json
import os

import app as klr
import database
import filing
import scanflow
import scan_connector
from conftest import make_pdf

BILL = {
    "is_cover_sheet": False, "received_date": "2026-07-10",
    "stamped_received_date": None,
    "doc_type": "Bill", "ai_confidence": "High",
    "vendor": "ACME Utilities", "amount": 147.48, "due_date": "2026-07-15",
    "entity_code": "TRUST1", "entity_name": "Test Trust One",
    "property_codes": ["T27"], "property_addresses": ["123 Example Ave, Sometown CA"],
    "bill_type_code": "ELEC", "invoice_number": "INV-99", "account_number": "12-3456",
    "notes": "", "important": False, "important_reason": "",
}


def _result(**overrides):
    r = json.loads(json.dumps(BILL))
    r.update(overrides)
    r.setdefault("extracted_text", "INVOICE amount due $147.48 " + "words " * 30)
    return r


def _set_cfg(env, **kv):
    env.cfg.update(kv)
    with open(env.cfg_path, "w", encoding="utf-8") as f:
        json.dump(env.cfg, f, indent=2)


def _use_archive(env):
    archive = os.path.join(str(env.tmp), "Files")
    os.makedirs(archive, exist_ok=True)
    _set_cfg(env, archive_folder=archive, connector_token="test-connector-token")
    return archive


# ── filing.py: suggestions ─────────────────────────────────────────────────────

def test_suggest_from_history_bumps_year_and_month(env):
    archive = _use_archive(env)
    old_dir = os.path.join(archive, "2025", "VENDORS - INSURANCE", "06. JUNE")
    database.insert_entry({"vendor": "ACME Utilities", "entity_code": "TRUST1",
                           "doc_type": "Bill", "status": "Pending",
                           "date_received": "2025-06-15",
                           "filed_path": os.path.join(old_dir, "old bill.pdf")})
    s = filing.suggest_filed_dir(
        {"vendor": "acme utilities", "entity_code": "TRUST1",
         "doc_type": "Correspondence", "date_received": "2026-07-10"}, env.cfg)
    assert s["source"] == "history"
    assert s["dir"] == os.path.join(archive, "2026", "VENDORS - INSURANCE", "07. JULY")


def test_suggest_prefers_same_entity_history(env):
    archive = _use_archive(env)
    database.insert_entry({"vendor": "ACME Utilities", "entity_code": "PP",
                           "doc_type": "Bill", "status": "Pending",
                           "date_received": "2026-06-15",
                           "filed_path": os.path.join(archive, "2026", "PP", "a.pdf")})
    database.insert_entry({"vendor": "ACME Utilities", "entity_code": "TRUST1",
                           "doc_type": "Bill", "status": "Pending",
                           "date_received": "2026-06-15",
                           "filed_path": os.path.join(archive, "2026", "TRUST", "b.pdf")})
    s = filing.suggest_filed_dir(
        {"vendor": "ACME Utilities", "entity_code": "TRUST1",
         "doc_type": "Bill", "date_received": "2026-07-10"}, env.cfg)
    assert s["dir"] == os.path.join(archive, "2026", "TRUST")


def test_suggest_prefers_same_entity_even_when_it_is_the_older_filing(env):
    """Same-entity history must win even when it's NOT the most recent filing --
    otherwise this degrades to "prefers newest" and silently drops the entity
    preference (2026-07-27 code review: verifies get_recent_vendor_filings'
    caller doesn't just take the first/newest row regardless of entity)."""
    archive = _use_archive(env)
    database.insert_entry({"vendor": "ACME Utilities", "entity_code": "TRUST1",
                           "doc_type": "Bill", "status": "Pending",
                           "date_received": "2026-05-01",
                           "filed_path": os.path.join(archive, "2026", "TRUST", "older.pdf")})
    database.insert_entry({"vendor": "ACME Utilities", "entity_code": "PP",
                           "doc_type": "Bill", "status": "Pending",
                           "date_received": "2026-06-15",
                           "filed_path": os.path.join(archive, "2026", "PP", "newer.pdf")})
    s = filing.suggest_filed_dir(
        {"vendor": "ACME Utilities", "entity_code": "TRUST1",
         "doc_type": "Bill", "date_received": "2026-07-10"}, env.cfg)
    assert s["dir"] == os.path.join(archive, "2026", "TRUST")


def test_suggest_rule_for_bills_without_history(env):
    archive = _use_archive(env)
    s = filing.suggest_filed_dir(
        {"vendor": "Nobody Ever Seen", "entity_code": "TRUST1",
         "doc_type": "Bill", "date_received": "2026-07-10"}, env.cfg)
    assert s["source"] == "rule"
    assert s["dir"] == os.path.join(archive, "2026",
                                    "ACCOUNTS PAYABLE - INCOMING BILLS", "07. JULY")


def test_suggest_default_without_archive(env):
    # No archive_folder configured -> legacy Filed tree, still a suggestion.
    s = filing.suggest_filed_dir(
        {"vendor": "ACME Utilities", "entity_code": "TRUST1",
         "property_code": "T27", "doc_type": "Bill",
         "date_received": "2026-07-10"}, env.cfg)
    assert s["source"] == "default"
    assert s["dir"].startswith(env.filed_folder)


def test_app_filed_tree_never_teaches_the_archive(env):
    _use_archive(env)
    database.insert_entry({"vendor": "ACME Utilities", "entity_code": "TRUST1",
                           "doc_type": "Bill", "status": "Pending",
                           "date_received": "2026-06-15",
                           "filed_path": os.path.join(env.filed_folder, "TRUST1",
                                                      "T27", "2026", "x.pdf")})
    s = filing.suggest_filed_dir(
        {"vendor": "ACME Utilities", "entity_code": "TRUST1",
         "doc_type": "Correspondence", "date_received": "2026-07-10"}, env.cfg)
    assert s["source"] == "default"


# ── filing.py: validation + names ──────────────────────────────────────────────

def test_validate_filed_dir_roots(env):
    archive = _use_archive(env)
    ok = filing.validate_filed_dir(os.path.join(archive, "2026", "TRUST"), env.cfg)
    assert ok == os.path.join(archive, "2026", "TRUST")
    assert filing.validate_filed_dir(os.path.join(env.filed_folder, "PP"), env.cfg)
    assert filing.validate_filed_dir(os.path.join(str(env.tmp), "elsewhere"), env.cfg) is None
    assert filing.validate_filed_dir(archive + os.sep + ".." + os.sep + "escape", env.cfg) is None
    assert filing.validate_filed_dir("relative\\path", env.cfg) is None
    assert filing.validate_filed_dir("", env.cfg) is None


def test_sanitize_filename():
    assert filing.sanitize_filename("07-10-26 TRUST1 ELEC - ACME - $147.48 - T27.pdf") == \
        "07-10-26 TRUST1 ELEC - ACME - $147.48 - T27"
    # basename → last segment; illegal chars and leading dots stripped
    assert filing.sanitize_filename(r"..\..\evil<name>?.pdf") == "evilname"
    assert filing.sanitize_filename("   ") == ""
    assert "/" not in filing.sanitize_filename("a/b\\c:d.pdf")


# ── connector heartbeat + status ───────────────────────────────────────────────

def test_heartbeat_rejects_bad_or_missing_token(env, client):
    _use_archive(env)
    r = client.post("/api/connector/heartbeat", json={"token": "wrong"})
    assert r.status_code == 403
    # No token configured at all -> fail closed even if sender guesses "".
    _set_cfg(env, connector_token="")
    r = client.post("/api/connector/heartbeat", json={"token": ""})
    assert r.status_code == 403
    assert not scanflow._connectors


def test_heartbeat_needs_no_login_and_feeds_status(env, client, staff_client):
    _use_archive(env)
    # Anonymous workstation connector: no session, no CSRF token — must work.
    r = client.post("/api/connector/heartbeat", json={
        "token": "test-connector-token", "machine": "FRONT-DESK",
        "user": "nancy", "scanner_present": True, "scanner_model": "ScanSnap iX1600",
        "scansnap_running": True, "profile_ok": True, "version": "1.0"})
    assert r.status_code == 200
    c, _headers = staff_client
    st = c.get("/api/scan/status").get_json()
    assert st["connector"]["machine"] == "FRONT-DESK"
    assert st["connector"]["fresh"] is True
    assert st["connector"]["ready"] is True


def test_status_goes_stale_after_missed_beats(env, client, staff_client):
    _use_archive(env)
    client.post("/api/connector/heartbeat", json={
        "token": "test-connector-token", "machine": "FRONT-DESK",
        "scanner_present": True, "scansnap_running": True, "profile_ok": True})
    with scanflow._connectors_lock:
        for v in scanflow._connectors.values():
            v["ts"] -= scanflow.FRESH_SECS + 5
    c, _headers = staff_client
    st = c.get("/api/scan/status").get_json()
    assert st["connector"]["fresh"] is False
    assert st["connector"]["ready"] is False


def test_scan_status_requires_login(env, client):
    r = client.get("/api/scan/status")
    assert r.status_code == 401


# ── arrivals: only physical scans count ────────────────────────────────────────

def test_arrivals_ignores_email_and_respects_since(env, staff_client):
    c, _headers = staff_client
    scan_pdf = os.path.join(env.scan_folder, "paper.pdf")
    email_pdf = os.path.join(env.email_folder, "attach.pdf")
    make_pdf(scan_pdf)
    make_pdf(email_pdf)
    sid_scan = klr._queue_add(_result(pdf_path=scan_pdf))
    klr._queue_add(_result(pdf_path=email_pdf, vendor="Mail Vendor"))
    items = c.get("/api/scan/arrivals?since=0").get_json()["items"]
    assert [i["id"] for i in items] == [sid_scan]
    assert items[0]["label"] == "ACME Utilities"
    items = c.get(f"/api/scan/arrivals?since={sid_scan}").get_json()["items"]
    assert items == []


# ── the /scan page ─────────────────────────────────────────────────────────────

def test_scan_page_renders_walkthrough(env, staff_client):
    c, _headers = staff_client
    r = c.get("/scan")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Connect this computer" in body
    assert "Connect My Scanner" in body
    assert "Klick Klack" in body


# ── review page shows the File-as suggestion ───────────────────────────────────

def test_review_page_shows_file_as(env, staff_client):
    archive = _use_archive(env)
    c, _headers = staff_client
    pdf = os.path.join(env.scan_folder, "doc.pdf")
    make_pdf(pdf)
    sid = klr._queue_add(_result(pdf_path=pdf))
    body = c.get(f"/review/{sid}").get_data(as_text=True)
    assert "File as" in body
    assert "ACCOUNTS PAYABLE - INCOMING BILLS" in body       # rule suggestion
    assert 'name="filed_name"' in body and 'name="filed_dir"' in body


def test_batch_date_prompt_only_for_multi_file_batches(env, staff_client):
    """A one-file batch must not ask "apply to the whole batch?" — the page
    gates the modal on BATCH_COUNT, the number of QUEUED files sharing the
    batch (confirmed batch-mates are untouched by set_date either way)."""
    c, _headers = staff_client
    solo = os.path.join(env.scan_folder, "solo.pdf")
    make_pdf(solo)
    sid = klr._queue_add(_result(pdf_path=solo, batch_number="2026-0724-01"))
    body = c.get(f"/review/{sid}").get_data(as_text=True)
    assert "BATCH_COUNT = 1" in body

    mate = os.path.join(env.scan_folder, "mate.pdf")
    make_pdf(mate)
    klr._queue_add(_result(pdf_path=mate, batch_number="2026-0724-01"))
    body = c.get(f"/review/{sid}").get_data(as_text=True)
    assert "BATCH_COUNT = 2" in body


# ── confirm: overrides, auto-recompute, fallback ───────────────────────────────

def _confirm(c, headers, sid, extra):
    data = {"date_received": "2026-07-10", "doc_type": "Bill",
            "vendor": "ACME Utilities", "entity_code": "TRUST1",
            "property_codes": ["T27"], "amount": "147.48",
            "date_ok": "1"}   # bypass the year-sanity guard as the fixed date ages
    data.update(extra)
    return c.post(f"/review/{sid}/confirm?vendor_ok=1&force=1",
                  headers=headers, data=data)


def test_confirm_files_into_chosen_folder_and_name(env, staff_client):
    archive = _use_archive(env)
    c, headers = staff_client
    pdf = os.path.join(env.scan_folder, "doc.pdf")
    make_pdf(pdf)
    sid = klr._queue_add(_result(pdf_path=pdf))
    chosen = os.path.join(archive, "2026", "TRUST", "SPECIAL")
    r = _confirm(c, headers, sid, {
        "filed_dir": chosen, "suggested_filed_dir": "something else",
        "filed_name": "My Custom Name.pdf", "suggested_filed_name": "other.pdf"})
    assert r.status_code == 302
    e = database.get_all_entries()[0]
    assert e["filed_path"] == os.path.join(chosen, "MY CUSTOM NAME.pdf")   # caps: #45
    assert os.path.exists(e["filed_path"])
    assert not os.path.exists(pdf)                    # moved, not copied


def test_confirm_untouched_suggestion_recomputes_from_final_fields(env, staff_client):
    archive = _use_archive(env)
    c, headers = staff_client
    pdf = os.path.join(env.scan_folder, "doc.pdf")
    make_pdf(pdf)
    sid = klr._queue_add(_result(pdf_path=pdf))
    # Reviewer fixed the vendor but left File-as at its suggestion: the filed
    # name must use the FINAL vendor, and the folder re-derives (bill rule).
    r = _confirm(c, headers, sid, {
        "vendor": "Corrected Vendor Co",
        "filed_dir": "whatever was shown", "suggested_filed_dir": "whatever was shown",
        "filed_name": "whatever name.pdf", "suggested_filed_name": "whatever name.pdf"})
    assert r.status_code == 302
    e = database.get_all_entries()[0]
    assert "CORRECTED VENDOR CO" in os.path.basename(e["filed_path"])   # caps: #45
    assert os.path.dirname(e["filed_path"]) == os.path.join(
        archive, "2026", "ACCOUNTS PAYABLE - INCOMING BILLS", "07. JULY")
    assert os.path.exists(e["filed_path"])


def test_confirm_invalid_folder_falls_back_with_log_note(env, staff_client):
    _use_archive(env)
    c, headers = staff_client
    pdf = os.path.join(env.scan_folder, "doc.pdf")
    make_pdf(pdf)
    sid = klr._queue_add(_result(pdf_path=pdf))
    outside = os.path.join(str(env.tmp), "not-a-filing-tree")
    r = _confirm(c, headers, sid, {
        "filed_dir": outside, "suggested_filed_dir": "different",
        "filed_name": "n.pdf", "suggested_filed_name": "n.pdf"})
    assert r.status_code == 302
    e = database.get_all_entries()[0]
    assert e["filed_path"].startswith(env.filed_folder)   # legacy tree
    assert os.path.exists(e["filed_path"])
    with database.get_conn() as conn:
        notes = [row[0] for row in conn.execute(
            "SELECT detail FROM activity_log WHERE entry_id=? AND action='Filing'",
            (e["id"],))]
    assert any("outside" in n for n in notes)


def test_confirm_without_file_as_fields_is_unchanged(env, staff_client):
    # Old-style POST (bulk paths, tests, cached pages): no panel fields at all.
    c, headers = staff_client
    pdf = os.path.join(env.scan_folder, "doc.pdf")
    make_pdf(pdf)
    sid = klr._queue_add(_result(pdf_path=pdf))
    r = _confirm(c, headers, sid, {})
    assert r.status_code == 302
    e = database.get_all_entries()[0]
    assert e["filed_path"].startswith(env.filed_folder)
    assert os.path.exists(e["filed_path"])


# ── the workstation connector (pure pieces only) ───────────────────────────────

def test_connector_profile_detection(tmp_path, monkeypatch):
    xml = tmp_path / "PfuSsHome.xml"
    xml.write_text("<Profiles><ProfileName RESTRCTION=\"0\">Scan to Folder"
                   "</ProfileName><ProfileName>Klick Klack</ProfileName></Profiles>",
                   encoding="utf-8")
    monkeypatch.setattr(scan_connector, "PROFILE_XML", str(xml))
    assert scan_connector.profile_ok() is True
    xml.write_text("<Profiles><ProfileName>Receipts</ProfileName></Profiles>",
                   encoding="utf-8")
    assert scan_connector.profile_ok() is False
    monkeypatch.setattr(scan_connector, "PROFILE_XML", str(tmp_path / "missing.xml"))
    assert scan_connector.profile_ok() is False


# ── the installer download (no share-chasing) ──────────────────────────────────

def test_connector_setup_download(env, client, staff_client):
    # Anonymous browsers get bounced to sign-in like any page.
    r = client.get("/scan/connector-setup")
    assert r.status_code in (301, 302)
    c, _headers = staff_client
    r = c.get("/scan/connector-setup")
    assert r.status_code == 200
    assert "attachment" in (r.headers.get("Content-Disposition") or "")
    body = r.get_data(as_text=True)
    # The downloaded copy must find the share on its own and never carry secrets.
    assert "scan_connector.py" in body
    # UNC-first: drive letters differ between desks (Joshua, 7/24).
    assert "\\\\LACKSSERVER2021\\File-Cabinet\\IntakeSystem" in body
    assert "connector_token" not in body
