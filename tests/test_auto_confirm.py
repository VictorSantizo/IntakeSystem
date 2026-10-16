"""Tiered auto-confirm + tightened email junk filter + outage re-classify.

Drives the REAL ingest pipeline (enqueue_scan) with classify_document
monkeypatched to return controlled AI results, against temp folders/DB.
"""
import json
import os

import app as klr
import database
from conftest import make_pdf

# A fully-resolved, high-confidence AI result for a bill (the auto-file shape).
HIGH_BILL = {
    "is_cover_sheet": False, "received_date": None, "stamped_received_date": None,
    "doc_type": "Bill", "ai_confidence": "High",
    "vendor": "ACME Utilities", "amount": 147.48, "due_date": "2026-07-15",
    "entity_code": "TRUST1", "entity_name": "Test Trust One",
    "property_codes": ["T27"], "property_addresses": ["123 Example Ave, Sometown CA"],
    "bill_type_code": "ELEC", "invoice_number": "INV-99", "account_number": "12-3456",
    "notes": "",
}


def _result(**overrides):
    r = json.loads(json.dumps(HIGH_BILL))
    r.update(overrides)
    # a believable text layer so the blank-page filter doesn't eat the doc
    r.setdefault("extracted_text", "INVOICE amount due $147.48 " + "words " * 30)
    return r


def _stub_classifier(monkeypatch, result):
    monkeypatch.setattr(klr, "classify_document", lambda path: json.loads(json.dumps(result)))


def _ingest(env, name, source="Email"):
    folder = env.email_folder if source == "Email" else env.scan_folder
    path = os.path.join(folder, name)
    make_pdf(path)
    klr.enqueue_scan(path, source)
    return path


def _teach_vendor(entity="TRUST1", props="T27"):
    database.save_vendor_hint("ACME Utilities", entity, props, "Bill", "ELEC")


# ── Junk filter unit tests ─────────────────────────────────────────────────────

def test_junk_bill_never_junk(env):
    assert klr._email_junk_reason({"doc_type": "Bill", "ai_confidence": "None"}) is None


def test_junk_irrelevant_types_still_junk(env):
    # Other (the marketing/job-application flood) and unknown AI labels stay
    # junk. Receipt and Correspondence stopped being junk 2026-08-10 (Nancy's
    # report / Joshua: "we definitely want to capture" receipts, "even if
    # it's just correspondance") — see the assertions below.
    assert klr._email_junk_reason({"doc_type": "Other", "ai_confidence": "High"})
    assert klr._email_junk_reason({"doc_type": "Newsletter", "ai_confidence": "High"})


def test_junk_receipts_and_correspondence_are_captured(env):
    for dt in ("Receipt", "Correspondence"):
        for conf in ("High", "Medium", "Low"):
            assert klr._email_junk_reason(
                {"doc_type": dt, "ai_confidence": conf}) is None, (dt, conf)


def test_junk_regex_fallback_nonbills_are_junk(env):
    for dt in ("Income", "Bank Statement", "Legal", "Receipt", "Correspondence"):
        assert klr._email_junk_reason({"doc_type": dt, "ai_confidence": "None"}), dt


def test_junk_low_confidence_income_statement_junk_but_legal_kept(env):
    assert klr._email_junk_reason({"doc_type": "Income", "ai_confidence": "Low"})
    assert klr._email_junk_reason({"doc_type": "Bank Statement", "ai_confidence": "Low"})
    assert klr._email_junk_reason({"doc_type": "Legal", "ai_confidence": "Low"}) is None


def test_junk_ai_classified_nonbills_stay_queued(env):
    for dt in ("Income", "Bank Statement", "Legal", "Receipt", "Correspondence"):
        for conf in ("Medium", "High"):
            assert klr._email_junk_reason({"doc_type": dt, "ai_confidence": conf}) is None


def test_junk_ai_outage_marker_exempts_regardless_of_confidence(env):
    # 2026-07-26: classify_with_claude's outage path forces important=True and
    # stamps "[AI unavailable" into notes, but ai_confidence is always "None"
    # for the regex fallback — so the confidence-gated exemption above never
    # covers it, and without this check the document would still hit the
    # "conf == none" junk rule and land in Filtered, silently defeating the
    # whole point of forcing manual review during an outage.
    for dt in ("Income", "Bank Statement", "Correspondence", "Other"):
        result = {"doc_type": dt, "ai_confidence": "None", "important": True,
                  "notes": "[AI unavailable: rate limited] some other note"}
        assert klr._email_junk_reason(result) is None, dt
    # The existing confidence-based rules are untouched for anything that
    # doesn't carry the marker — this is an additional OR, not a replacement.
    assert klr._email_junk_reason({"doc_type": "Income", "ai_confidence": "None",
                                   "notes": "ordinary notes, no outage"})


def test_ingest_regex_income_email_goes_to_filtered(env, monkeypatch):
    # Plain regex fallback with no outage marker in notes — e.g. no API key
    # configured at all, an intentional degraded mode, not an outage. The
    # general "keyword-guess doc types are junk" rule still applies here.
    _stub_classifier(monkeypatch, _result(
        doc_type="Income", ai_confidence="None", vendor=None, amount=None,
        entity_code=None, property_codes=[],
        notes="No API key configured."))
    _ingest(env, "junkmail.pdf")
    assert not klr._queue_items()
    filtered = database.get_filtered_docs()
    assert len(filtered) == 1
    assert "keyword fallback" in filtered[0]["reason"]
    assert os.path.exists(filtered[0]["file_path"])          # recoverable, not deleted


def test_ingest_ai_outage_email_reaches_queue_not_filtered(env, monkeypatch):
    # 2026-07-26: the SAME regex-fallback shape, but notes now carry the
    # AI-outage marker (set by classify_with_claude's except path, which also
    # forces important=True) — this must now reach the review queue instead
    # of silently landing in Filtered, or forcing important=True during an
    # outage would have no actual effect for email-sourced non-Bill documents.
    _stub_classifier(monkeypatch, _result(
        doc_type="Income", ai_confidence="None", vendor=None, amount=None,
        entity_code=None, property_codes=[], important=True,
        notes="[AI unavailable: boom] Manually review."))
    _ingest(env, "outagemail.pdf")
    assert klr._queue_items()
    assert database.get_filtered_docs() == []


# ── Auto-confirm eligibility (unit) ────────────────────────────────────────────

def test_should_not_auto_confirm_without_hint_in_default_mode(env):
    assert not klr._should_auto_confirm(_result(), env.cfg)


def test_should_auto_confirm_known_vendor(env):
    _teach_vendor()
    assert klr._should_auto_confirm(_result(), env.cfg)


def test_should_auto_confirm_known_account(env):
    database.save_account_hint("12-3456", "TRUST1", "T27")
    assert klr._should_auto_confirm(_result(), env.cfg)


def test_blockers_confidence_amount_entity_property_date(env):
    _teach_vendor()
    assert not klr._should_auto_confirm(_result(ai_confidence="Medium"), env.cfg)
    assert not klr._should_auto_confirm(_result(doc_type="Income"), env.cfg)
    assert not klr._should_auto_confirm(_result(amount=None), env.cfg)
    assert not klr._should_auto_confirm(_result(amount=0), env.cfg)
    assert not klr._should_auto_confirm(_result(entity_code=""), env.cfg)
    assert not klr._should_auto_confirm(_result(property_codes=[]), env.cfg)
    assert not klr._should_auto_confirm(_result(received_date_assumed=True), env.cfg)


def test_hint_disagreement_vetoes_even_aggressive(env):
    cfg = dict(env.cfg, auto_confirm="aggressive")
    assert klr._should_auto_confirm(_result(), cfg)          # no hint: aggressive allows
    _teach_vendor(entity="PP", props="PP16")                 # human taught a DIFFERENT entity
    assert not klr._should_auto_confirm(_result(), cfg)


def test_mode_off_blocks_everything(env):
    _teach_vendor()
    assert not klr._should_auto_confirm(_result(), dict(env.cfg, auto_confirm="off"))


# ── Auto-confirm through the real ingest pipeline ─────────────────────────────

def test_ingest_auto_files_known_vendor_bill(env, monkeypatch):
    _teach_vendor()
    _stub_classifier(monkeypatch, _result())
    _ingest(env, "acme-july.pdf")
    assert not klr._queue_items()                            # skipped the queue
    entries = database.get_all_entries()
    assert len(entries) == 1
    e = entries[0]
    assert e["processed_by"] == "Auto"
    assert e["status"] == "Pending"                          # still an unpaid bill
    assert e["filed_path"] and os.path.exists(e["filed_path"])
    assert e["file_hash"] and len(e["file_hash"]) == 64
    acts = [a for a in database.get_activity_feed(10) if a["action"] == "Created"]
    assert acts and "auto-confirm" in acts[0]["detail"]


def test_ingest_unknown_vendor_stays_in_review(env, monkeypatch):
    _stub_classifier(monkeypatch, _result())
    _ingest(env, "acme-july.pdf")
    assert len(klr._queue_items()) == 1
    assert not database.get_all_entries()


def test_auto_confirm_never_learns_hints(env, monkeypatch):
    _teach_vendor()                                          # human taught ELEC
    _stub_classifier(monkeypatch, _result(bill_type_code="GAS"))
    _ingest(env, "acme-july.pdf")
    assert database.get_all_entries()[0]["processed_by"] == "Auto"
    hint = database.get_vendor_hint_exact("ACME Utilities")
    assert hint["bill_type_code"] == "ELEC"                  # not overwritten by the machine


def test_duplicate_suspicion_goes_to_review(env, monkeypatch):
    _teach_vendor()
    database.insert_entry({"date_received": "2026-07-01", "doc_type": "Bill",
                           "vendor": "ACME Utilities", "amount": 147.48,
                           "due_date": "2026-07-15", "invoice_number": "INV-99",
                           "status": "Pending", "processed_by": "TestUser"})
    _stub_classifier(monkeypatch, _result())
    _ingest(env, "acme-again.pdf")
    assert len(klr._queue_items()) == 1                      # left for a person
    assert len(database.get_all_entries()) == 1              # no second entry


def test_aggressive_mode_files_unknown_vendor(env, monkeypatch):
    cfg = json.loads(open(env.cfg_path, encoding="utf-8").read())
    cfg["auto_confirm"] = "aggressive"
    open(env.cfg_path, "w", encoding="utf-8").write(json.dumps(cfg))
    _stub_classifier(monkeypatch, _result())
    _ingest(env, "new-vendor.pdf")
    assert not klr._queue_items()
    assert database.get_all_entries()[0]["processed_by"] == "Auto"


# ── Outage re-classify ─────────────────────────────────────────────────────────

def _seed_outage_item(env, name="outage.pdf"):
    path = os.path.join(env.email_folder, name)
    make_pdf(path)
    sid = klr._queue_add({
        "pdf_path": path, "mail_type": "Email", "doc_type": "Income",
        "ai_confidence": "None", "vendor": None, "amount": None,
        "received_date": "2026-07-01", "file_hash": "x" * 64,
        "notes": "[AI unavailable: Error code: 400 - {'type': 'error', 'error': "
                 "{'type': 'invalid_request_error', 'message': 'Your credit balance "
                 "is too low to access the Anthropic API.'}}] Manually review.",
    })
    return sid, path


def test_outage_sids_detects_marker(env):
    sid, _ = _seed_outage_item(env)
    assert klr._ai_outage_sids() == [sid]


def test_reclassify_junk_drains_to_filtered(env, monkeypatch):
    # "Other" — Correspondence stopped being junk 2026-08-10 (Nancy's report);
    # the drain mechanism under test is unchanged.
    sid, path = _seed_outage_item(env)
    _stub_classifier(monkeypatch, _result(doc_type="Other", ai_confidence="High"))
    klr._reclassify_items([sid])
    assert sid not in klr.pending_scans
    filtered = database.get_filtered_docs()
    assert len(filtered) == 1 and "Other" in filtered[0]["reason"]


def test_reclassify_bill_refreshes_in_place_preserving_ingest_facts(env, monkeypatch):
    sid, path = _seed_outage_item(env)
    _stub_classifier(monkeypatch, _result())                 # unknown vendor -> stays queued
    klr._reclassify_items([sid])
    d = klr.pending_scans[sid]
    assert d["doc_type"] == "Bill" and d["ai_confidence"] == "High"
    assert d["received_date"] == "2026-07-01"                # ingest fact preserved
    assert d["file_hash"] == "x" * 64
    assert d["mail_type"] == "Email"
    assert klr._AI_UNAVAILABLE_MARK not in (d.get("notes") or "")
    # persisted too
    row = dict(database.get_all_pending_scans())[sid]
    assert json.loads(row)["doc_type"] == "Bill"


def test_reclassify_auto_files_known_vendor(env, monkeypatch):
    _teach_vendor()
    sid, path = _seed_outage_item(env)
    _stub_classifier(monkeypatch, _result())
    klr._reclassify_items([sid])
    assert sid not in klr.pending_scans
    assert database.get_all_entries()[0]["processed_by"] == "Auto"


def test_reclassify_still_failing_leaves_item(env, monkeypatch):
    sid, path = _seed_outage_item(env)
    _stub_classifier(monkeypatch, _result(
        doc_type="Other", ai_confidence="None",
        notes="[AI unavailable: still broke] Manually review."))
    klr._reclassify_items([sid])
    d = klr.pending_scans[sid]
    assert d["doc_type"] == "Income"                         # untouched
    assert klr._ai_outage_sids() == [sid]                    # banner stays


def test_reclassify_route_admin_only_and_noop_message(env, staff_client, admin_client):
    # Non-admin staff get bounced to the dashboard (admin_required page contract).
    c, headers = staff_client
    r = c.post("/review/reclassify", headers=headers, follow_redirects=False)
    assert r.status_code == 302 and r.headers["Location"].rstrip("/") in ("", "http://localhost")
    ca, aheaders = admin_client
    r = ca.post("/review/reclassify", headers=aheaders, follow_redirects=False)
    assert r.status_code == 302 and "No+items" in r.headers["Location"]


# ── Feed + UI wiring ───────────────────────────────────────────────────────────

def test_log_filters_by_processed_by(env, staff_client):
    database.insert_entry({"date_received": "2026-07-01", "doc_type": "Bill",
                           "vendor": "Manual Vendor", "status": "Pending",
                           "processed_by": "TestUser"})
    database.insert_entry({"date_received": "2026-07-02", "doc_type": "Bill",
                           "vendor": "Robo Vendor", "status": "Pending",
                           "processed_by": "Auto"})
    only_auto = database.get_all_entries({"processed_by": "Auto"})
    assert [e["vendor"] for e in only_auto] == ["Robo Vendor"]
    c, _ = staff_client
    r = c.get("/log?processed_by=Auto")
    assert r.status_code == 200
    assert b"Robo Vendor" in r.data and b"Manual Vendor" not in r.data


def test_review_queue_shows_spotcheck_feed_and_outage_banner(env, admin_client):
    database.insert_entry({"date_received": "2026-07-07", "doc_type": "Bill",
                           "vendor": "Robo Vendor", "status": "Pending",
                           "processed_by": "Auto"})
    _seed_outage_item(env)
    c, headers = admin_client
    r = c.get("/review")
    assert r.status_code == 200
    assert b"auto-filed in the last 7 days" in r.data
    assert b"AI classification was unavailable" in r.data
    assert b"Re-run AI on these" in r.data                   # admin sees the button
    assert b"credit balance is too low" in r.data            # readable reason


def test_review_queue_staff_sees_banner_without_button(env, staff_client):
    _seed_outage_item(env)
    c, headers = staff_client
    r = c.get("/review")
    assert r.status_code == 200
    assert b"AI classification was unavailable" in r.data
    assert b"Re-run AI on these" not in r.data


def test_settings_saves_auto_confirm_mode(env, admin_client):
    c, headers = admin_client
    r = c.post("/settings", headers=headers, data={"auto_confirm": "aggressive"})
    assert r.status_code == 302
    cfg = json.loads(open(env.cfg_path, encoding="utf-8").read())
    assert cfg["auto_confirm"] == "aggressive"
    # bogus values are ignored, not saved
    c.post("/settings", headers=headers, data={"auto_confirm": "yolo"})
    cfg = json.loads(open(env.cfg_path, encoding="utf-8").read())
    assert cfg["auto_confirm"] == "aggressive"
