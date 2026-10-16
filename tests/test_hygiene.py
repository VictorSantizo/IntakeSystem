"""Pure-function hygiene: dates, CSV guard, passwords, filenames, variants."""
import classifier
import routes_bills
import app as klr


def test_iso_date_normalization():
    f = classifier._iso_date_or_none
    assert f("6/15/26") == "2026-06-15"
    assert f("06-15-2026") == "2026-06-15"
    assert f("July 15, 2026") == "2026-07-15"
    assert f("Jul 15 2026") == "2026-07-15"
    assert f("2026-07-15") == "2026-07-15"
    assert f("13/45/26") is None
    assert f("next Tuesday") is None
    assert f("") is None
    assert f(None) is None


def test_regex_fallback_stores_iso_dates(env):
    r = classifier.classify_with_regex(
        "ACME Power. Amount due $120.50. due date: 6/15/26",
        {"properties": [], "entities": []})
    assert r["due_date"] == "2026-06-15"
    assert r["amount"] == 120.50


def test_regex_fallback_unparseable_date_goes_to_notes(env):
    r = classifier.classify_with_regex(
        "Pay by: bogus-date-here invoice",
        {"properties": [], "entities": []})
    assert r["due_date"] is None


def test_csv_formula_guard():
    assert routes_bills._csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert routes_bills._csv_safe("+1") == "'+1"
    assert routes_bills._csv_safe("@cmd") == "'@cmd"
    assert routes_bills._csv_safe("-5 Main St") == "'-5 Main St"
    assert routes_bills._csv_safe("SoCal Gas") == "SoCal Gas"
    assert routes_bills._csv_safe(None) == ""


def test_password_edge_cases():
    assert klr._check_staff_password({"password": ""}, "") is False
    assert klr._check_staff_password({}, "anything") is False
    assert klr._check_staff_password({"password": "plain"}, "plain") is True
    assert klr._check_staff_password({"password": "plain"}, "wrong") is False


def test_make_filename_convention():
    entry = {"date_received": "2026-06-26", "entity_code": "TRUST2",
             "bill_type_code": "ELEC", "vendor": "Frontier",
             "amount": 147.48, "property_code": "TL222"}
    # ALL CAPS on the server per report #45.
    assert klr._make_filename(entry) == "06-26-26 TRUST2 ELEC - FRONTIER - $147.48 - TL222.pdf"


def test_make_filename_paid_check():
    entry = {"date_received": "2026-06-26", "entity_code": "TRUST2",
             "bill_type_code": "ELEC", "vendor": "Frontier", "amount": 147.48,
             "property_code": "TL222", "payment_method": "Check", "check_number": "13386"}
    assert klr._make_filename(entry).startswith("06-26-26 TRUST2 CK#13386 - FRONTIER")


def test_make_filename_omits_empty_fields():
    entry = {"date_received": "2026-06-26", "doc_type": "Legal"}
    assert klr._make_filename(entry) == "06-26-26 - LEGAL.pdf"


def test_make_filename_keeps_zero_amount():
    # amount == 0 is a real $0.00 bill, not a missing one — `if amount` alone
    # treats 0 as falsy and used to drop it from the filename silently.
    entry = {"date_received": "2026-06-26", "entity_code": "TRUST2",
             "vendor": "Frontier", "amount": 0.0, "property_code": "TL222"}
    assert "$0.00" in klr._make_filename(entry)


def test_highlight_variants():
    assert klr._highlight_variants("amount", 14927.14) == ["$14,927.14", "14,927.14", "14927.14"]
    v = klr._highlight_variants("due_date", "2026-07-15")
    assert "07/15/2026" in v and "July 15, 2026" in v
    assert klr._highlight_variants("vendor", None) == []
    assert klr._highlight_variants("invoice_number", "AB 123") == ["AB 123", "AB123"]


def test_restart_waits_for_inflight_finalize(env, monkeypatch):
    """2026-07-27 code review (GH #8): /api/restart must not os._exit while a
    confirm-path _finalize_entry (insert + file move) is mid-flight in another
    request -- the drain loop waits for the in-flight counter to hit zero."""
    import threading
    import time

    exited = threading.Event()
    monkeypatch.setattr(klr.os, "_exit", lambda code: exited.set())
    monkeypatch.setitem(klr.__dict__, "_inflight_finalizes", 1)

    c = klr.app.test_client()
    with c.session_transaction() as s:
        s["user"] = "TestAdmin"
        s["last_active"] = time.time()
        s["csrf"] = "t"
    r = c.post("/api/restart", headers={"X-CSRF-Token": "t"})
    assert r.status_code == 200

    # While a finalize is in flight, the exit must NOT fire...
    time.sleep(1.4)                     # past the 0.8s response-completion delay
    assert not exited.is_set()

    # ...and must fire promptly once it drains.
    with klr._finalize_lock:
        klr._inflight_finalizes = 0
    assert exited.wait(timeout=3)
