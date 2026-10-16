"""The document-type vocabulary (reports #51, #57, #58, #59).

Karen and Joshua asked for four types the system didn't have:
  #57 Statement (a vendor account statement — "not bank statement")
  #58 Audit
  #59 Insurance
  #51 Tax

They sat queued because the list was copy-pasted into six places. It now lives
in doc_types.py; these tests pin both the vocabulary and the single-source rule
so the next type is one edit.
"""
import os
import re

import anthropic

import classifier
import doc_types
from database import insert_entry

NEW_TYPES = ["Statement", "Insurance", "Tax", "Audit"]


def test_the_four_requested_types_exist():
    for t in NEW_TYPES:
        assert t in doc_types.DOC_TYPES
    # and nothing that already worked was dropped
    for t in ("Bill", "Receipt", "Income", "Bank Statement", "Legal",
              "Correspondence", "Other"):
        assert t in doc_types.DOC_TYPES
    assert doc_types.DOC_TYPES[-1] == "Other"
    assert set(doc_types.DOC_TYPE_GUIDE) == set(doc_types.DOC_TYPES)


def test_normalize_maps_casing_but_keeps_the_unknown_visible():
    assert doc_types.normalize("insurance") == "Insurance"
    assert doc_types.normalize("  TAX ") == "Tax"
    assert doc_types.normalize("") == ""
    assert doc_types.normalize(None) == ""
    # An unrecognized label a person can see and fix beats a silently blank one.
    assert doc_types.normalize("Weird Thing") == "Weird Thing"


def test_choices_keep_a_records_own_legacy_value():
    assert doc_types.choices_for("Bill") == doc_types.DOC_TYPES
    assert doc_types.choices_for("Ancient Type")[-1] == "Ancient Type"


# ── the three staff-facing dropdowns ──────────────────────────────────────────

def test_new_entry_form_offers_the_new_types(env, staff_client):
    c, _ = staff_client
    html = c.get("/entry/new").get_data(as_text=True)
    for t in NEW_TYPES:
        assert f'value="{t}"' in html


def test_intake_log_filter_offers_the_new_types(env, staff_client):
    c, _ = staff_client
    html = c.get("/log").get_data(as_text=True)
    for t in NEW_TYPES:
        assert f'value="{t}"' in html


def test_review_form_offers_the_new_types(env, staff_client, tmp_path):
    import app as klr
    c, _ = staff_client
    f = tmp_path / "doc.pdf"
    f.write_text("x")
    sid = klr._queue_add({"pdf_path": str(f), "vendor": "Test"})
    html = c.get(f"/review/{sid}").get_data(as_text=True)
    for t in NEW_TYPES:
        assert f'value="{t}"' in html


def test_a_record_saved_with_a_new_type_keeps_it(env, staff_client, monkeypatch):
    c, headers = staff_client
    eid = insert_entry({"vendor": "State Farm", "doc_type": "Insurance",
                        "date_received": "2026-08-05", "status": "Pending"})
    html = c.get(f"/entry/{eid}/edit").get_data(as_text=True)
    assert 'value="Insurance" selected' in html.replace("selected>", "selected >") \
        or 'value="Insurance" selected' in html


def test_a_legacy_type_not_in_the_list_survives_an_edit_page(env, staff_client):
    """Nothing is ever rewritten behind a person's back: a record holding an
    old label still shows that label as the selected option."""
    c, _ = staff_client
    eid = insert_entry({"vendor": "Someone", "doc_type": "Ancient Type",
                        "date_received": "2026-08-05"})
    html = c.get(f"/entry/{eid}/edit").get_data(as_text=True)
    assert "Ancient Type" in html


# ── the classifier speaks the same vocabulary ─────────────────────────────────

def test_classifier_taught_types_stay_email_relevant(env, monkeypatch):
    """The classifier was taught the four new types on 2026-08-05 as a paired
    change: the email junk filter (app._EMAIL_RELEVANT_DOCTYPES) is an
    allow-list, so a type the prompt can emit but that list doesn't know about
    gets that type's emails quietly filed aside as junk. This test fails the
    day the prompt and the allow-list drift apart, and points at the pairing."""
    captured = {}

    class _CapturingMessages:
        def create(self, **kw):
            captured["prompt"] = kw["messages"][0]["content"]
            raise anthropic.AnthropicError("captured — no real call")

    class _CapturingClient:
        def __init__(self, *a, **kw):
            self.messages = _CapturingMessages()

    monkeypatch.setattr(anthropic, "Anthropic", _CapturingClient)
    classifier.classify_with_claude(
        "ACME Utilities invoice",
        {"claude_api_key": "sk-test-key", "entities": [], "properties": [],
         "bill_types": []})
    prompt = captured.get("prompt") or ""
    if isinstance(prompt, list):                       # image/text block form
        prompt = " ".join(b.get("text", "") for b in prompt if isinstance(b, dict))
    assert prompt, "the classifier prompt was never built"
    offered = re.search(r'"doc_type":\s*"([^"]+)"', prompt)
    assert offered, "the prompt no longer states the doc_type options"
    offered = {s.strip() for s in offered.group(1).split("|")}
    import app as klr
    for t in NEW_TYPES:
        if t in offered:
            assert t in klr._EMAIL_RELEVANT_DOCTYPES, (
                f"the AI can now answer {t!r}, so {t!r} must also be in "
                "app._EMAIL_RELEVANT_DOCTYPES — otherwise every emailed "
                f"{t.lower()} is silently filed aside as junk "
                "(see doc_types.EMAIL_RELEVANT_WHEN_AI_LEARNS_THESE)")


def test_ai_doc_type_is_normalized(env):
    assert doc_types.normalize("bank statement") == "Bank Statement"
    assert doc_types.normalize("AUDIT") == "Audit"


# ── the single-source rule ────────────────────────────────────────────────────

_LIVE = ("classifier.py", "assistant.py", "app.py", "database.py",
         "routes_bills.py", "routes_entry.py", "routes_review.py",
         "routes_settings.py", "routes_filtered.py", "routes_home.py")


def test_no_live_module_or_template_hardcodes_the_type_list():
    """(install.py, the sixth copy, was deleted outright — report #82.)"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # The tail of the vocabulary list, in order — precise enough not to catch
    # the deliberately-different subsets that also name some of these types
    # (app's email-relevance allow-list, review.html's SIMPLE_TYPES).
    pattern = re.compile(
        r"['\"]Legal['\"]\s*,\s*['\"]Correspondence['\"]\s*,\s*['\"]Other['\"]")
    offenders = []
    for rel in _LIVE:
        text = open(os.path.join(root, rel), encoding="utf-8").read()
        if pattern.search(text):
            offenders.append(rel)
    tpl = os.path.join(root, "templates")
    for name in sorted(os.listdir(tpl)):
        if name.endswith(".html"):
            text = open(os.path.join(tpl, name), encoding="utf-8").read()
            if pattern.search(text):
                offenders.append("templates/" + name)
    assert not offenders, ("the document-type list belongs in doc_types.py only: "
                           + ", ".join(offenders))
