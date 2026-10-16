"""AI-estimated importance: the classifier returns important/important_reason;
flagged documents are exempt from the email junk filter (with a real AI read),
never auto-confirm, float up the review queue with a badge, and carry the flag
+ reason into the logged record via both confirm paths — landing on the
dashboard's needs-attention list like a manual flag.
"""
import json
import os

import app as klr
import classifier
import database
from conftest import make_pdf

HIGH_BILL = {
    "is_cover_sheet": False, "received_date": None, "stamped_received_date": None,
    "doc_type": "Bill", "ai_confidence": "High",
    "vendor": "ACME Utilities", "amount": 147.48, "due_date": "2026-07-15",
    "entity_code": "TRUST1", "entity_name": "Test Trust One",
    "property_codes": ["T27"], "property_addresses": ["123 Example Ave, Sometown CA"],
    "bill_type_code": "ELEC", "invoice_number": "INV-99", "account_number": "12-3456",
    "notes": "", "important": False, "important_reason": "",
}


def _result(**overrides):
    r = json.loads(json.dumps(HIGH_BILL))
    r.update(overrides)
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


# ── Classifier contract ────────────────────────────────────────────────────────

def test_regex_fallback_never_flags_important(env):
    r = classifier.classify_with_regex("URGENT FINAL NOTICE amount due $99", {"properties": []})
    assert r["important"] is False
    assert r["important_reason"] == ""


def test_prompt_asks_for_importance(env):
    # The JSON schema and the guardrail guidance both live in the prompt source.
    import inspect
    src = inspect.getsource(classifier.classify_with_claude)
    assert '"important"' in src
    assert "important_reason" in src
    assert "NOT important" in src          # the marketing-urgency guardrail


# ── Junk-filter exemption ──────────────────────────────────────────────────────

# 2026-08-10: Correspondence joined the email-relevant set (Nancy's report),
# so the exemption tests below exercise "Other" — the type that still needs
# the importance flag to escape the filter.

def test_important_exempts_junk_filter_with_real_ai_read(env):
    for conf in ("High", "Medium"):
        assert klr._email_junk_reason({"doc_type": "Other",
                                       "ai_confidence": conf, "important": True}) is None


def test_important_needs_ai_attestation_to_exempt(env):
    # Low/None confidence importance doesn't bypass the filter.
    for conf in ("Low", "None"):
        assert klr._email_junk_reason({"doc_type": "Other",
                                       "ai_confidence": conf, "important": True})


def test_unimportant_junk_still_filtered(env):
    assert klr._email_junk_reason({"doc_type": "Other",
                                   "ai_confidence": "High", "important": False})


def test_ingest_important_correspondence_reaches_queue(env, monkeypatch):
    _stub_classifier(monkeypatch, _result(
        doc_type="Correspondence", important=True,
        important_reason="Insurance non-renewal notice for 123 Example Ave"))
    _ingest(env, "cancellation.pdf")
    items = klr._queue_items()
    assert len(items) == 1                                   # not junk-filtered
    assert not database.get_filtered_docs()
    assert items[0][1]["important"] is True
    assert "non-renewal" in items[0][1]["important_reason"]


# ── Auto-confirm veto ──────────────────────────────────────────────────────────

def test_important_blocks_auto_confirm(env, monkeypatch):
    database.save_vendor_hint("ACME Utilities", "TRUST1", "T27", "Bill", "ELEC")
    assert klr._should_auto_confirm(_result(), env.cfg)                  # baseline: eligible
    assert not klr._should_auto_confirm(_result(important=True), env.cfg)
    # through the real pipeline: it must land in review, not auto-file
    _stub_classifier(monkeypatch, _result(important=True,
                                          important_reason="Disputed amount"))
    _ingest(env, "disputed.pdf")
    assert len(klr._queue_items()) == 1
    assert not database.get_all_entries()


# ── Confirm paths carry the flag ───────────────────────────────────────────────

def test_form_confirm_carries_flag_and_reason(env, staff_client):
    c, headers = staff_client
    pdf = os.path.join(env.scan_folder, "doc.pdf")
    make_pdf(pdf)
    sid = klr._queue_add(_result(pdf_path=pdf, important=True,
                                 important_reason="Court filing deadline"))
    r = c.post(f"/review/{sid}/confirm", headers=headers, data={
        "date_received": "2026-07-07", "doc_type": "Legal", "vendor": "Smith LLP",
        "important": "1", "important_reason": "Court filing deadline",
        "date_ok": "1",   # bypass the year-sanity guard as the fixed date ages
    })
    assert r.status_code == 302
    e = database.get_all_entries()[0]
    assert e["important"] == 1
    assert e["important_reason"] == "Court filing deadline"
    assert e["id"] in [x["id"] for x in database.get_important_correspondence()]


def test_form_confirm_unticked_drops_flag(env, staff_client):
    c, headers = staff_client
    pdf = os.path.join(env.scan_folder, "doc.pdf")
    make_pdf(pdf)
    sid = klr._queue_add(_result(pdf_path=pdf, important=True,
                                 important_reason="Something scary"))
    c.post(f"/review/{sid}/confirm", headers=headers, data={
        "date_received": "2026-07-07", "doc_type": "Bill", "vendor": "ACME Utilities",
        "important_reason": "Something scary",      # reason posted but box unticked
        "date_ok": "1",   # bypass the year-sanity guard as the fixed date ages
    })
    e = database.get_all_entries()[0]
    assert e["important"] == 0
    assert (e["important_reason"] or "") == ""


def test_bulk_confirm_carries_flag(env, staff_client):
    c, headers = staff_client
    pdf = os.path.join(env.scan_folder, "doc.pdf")
    make_pdf(pdf)
    sid = klr._queue_add(_result(pdf_path=pdf, important=True,
                                 important_reason="Shutoff warning"))
    r = c.post("/review/bulk_confirm", headers=headers, data={"scan_ids": [str(sid)]})
    assert r.status_code == 302
    e = database.get_all_entries()[0]
    assert e["important"] == 1 and e["important_reason"] == "Shutoff warning"


# ── UI surfacing ───────────────────────────────────────────────────────────────

def test_review_detail_shows_banner_and_prechecked_box(env, staff_client, tmp_path):
    c, headers = staff_client
    f = tmp_path / "doc.pdf"; f.write_text("x")
    sid = klr._queue_add({"pdf_path": str(f), "vendor": "Stone Insurance",
                          "important": True,
                          "important_reason": "Policy cancellation effective 8/1"})
    html = c.get(f"/review/{sid}").get_data(as_text=True)
    assert "flagged this as important" in html
    assert "Policy cancellation effective 8/1" in html
    assert 'name="important" value="1" checked' in html


def test_review_queue_badge_and_priority_sort(env, staff_client, tmp_path):
    c, headers = staff_client
    plain = tmp_path / "plain.pdf"; plain.write_text("x")
    flagged = tmp_path / "flagged.pdf"; flagged.write_text("x")
    klr._queue_add({"pdf_path": str(plain), "vendor": "Plain Vendor"})
    klr._queue_add({"pdf_path": str(flagged), "vendor": "Flagged Vendor",
                    "important": True, "important_reason": "Code violation notice"})
    html = c.get("/review").get_data(as_text=True)
    assert "IMPORTANT" in html
    assert "Code violation notice" in html                   # reason on the badge tooltip
    # flagged item floats above the plain one despite its higher scan id
    assert html.index("Flagged Vendor") < html.index("Plain Vendor")


def test_entry_view_shows_ai_reason(env, staff_client):
    c, headers = staff_client
    eid = database.insert_entry({"date_received": "2026-07-07", "doc_type": "Bill",
                                 "vendor": "ACME Utilities", "status": "Pending",
                                 "important": 1,
                                 "important_reason": "Abnormally large amount vs history"})
    html = c.get(f"/entry/{eid}/view").get_data(as_text=True)
    assert "Important" in html
    assert "Abnormally large amount vs history" in html
