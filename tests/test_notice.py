"""Bill-with-important-correspondence: a "Contains a notice" surface (2026-07-07).

A utility bill often bundles a rate-increase / shutoff / cancellation notice.
The classifier now emits `notice_summary` for such a bundled notice (and forces
the important flag), it rides through confirmation into the record, surfaces the
bill on the dashboard's needs-attention list with a Notice marker, and shows a
callout on the record view.
"""
import json
import sys
import types

import database
import app as klr
import classifier


# ---- classifier normalization (fake Anthropic, no network) ------------------

def _config_with_key():
    return {"claude_api_key": "sk-test", "entities": [], "properties": [],
            "bill_types": []}


def _fake_anthropic(monkeypatch, payload: dict):
    reply = json.dumps(payload)

    class _Messages:
        def create(self, **kw):
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text=reply)])

    class _Client:
        def __init__(self, api_key=None):
            self.messages = _Messages()

    fake = types.ModuleType("anthropic")
    fake.Anthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", fake)


def _base_payload(**over):
    p = {"doc_type": "Bill", "vendor": "SCE", "amount": 42.0, "important": False,
         "important_reason": "", "confidence": "High"}
    p.update(over)
    return p


def test_notice_summary_forces_important(monkeypatch):
    _fake_anthropic(monkeypatch, _base_payload(
        notice_summary="2027 rate increase filed with the CPUC"))
    r = classifier.classify_with_claude("bill text", _config_with_key())
    assert r["notice_summary"] == "2027 rate increase filed with the CPUC"
    assert r["important"] is True                      # forced on
    assert r["important_reason"]                        # backfilled from the notice


def test_blank_notice_summary_becomes_none(monkeypatch):
    _fake_anthropic(monkeypatch, _base_payload(notice_summary="   "))
    r = classifier.classify_with_claude("bill text", _config_with_key())
    assert r["notice_summary"] is None
    assert r["important"] is False                      # nothing to surface


def test_missing_notice_summary_is_none(monkeypatch):
    _fake_anthropic(monkeypatch, _base_payload())       # key absent entirely
    r = classifier.classify_with_claude("bill text", _config_with_key())
    assert r["notice_summary"] is None


def test_regex_fallback_has_notice_summary_none():
    r = classifier.classify_with_regex("some text", {"properties": [], "entities": []})
    assert r["notice_summary"] is None


def test_prompt_documents_notice_summary():
    import inspect
    src = inspect.getsource(classifier.classify_with_claude)
    assert '"notice_summary"' in src
    assert "shutoff" in src and "rate increase" in src


# ---- persistence + surface --------------------------------------------------

def test_bill_with_notice_surfaces_even_if_not_important(env):
    # a bundled notice must reach the needs-attention list even for a Bill that
    # wasn't flagged important (belt-and-suspenders: the query keys off both)
    bid = database.insert_entry({
        "date_received": "2026-07-06", "doc_type": "Bill", "vendor": "SCE",
        "status": "Pending", "important": 0,
        "notice_summary": "2027 ERRA rate increase notice"})
    plain = database.insert_entry({
        "date_received": "2026-07-06", "doc_type": "Bill", "vendor": "Frontier",
        "status": "Pending"})
    ids = [r["id"] for r in database.get_important_correspondence()]
    assert bid in ids
    assert plain not in ids                             # ordinary bill stays out


def test_insert_and_update_round_trip_notice_summary(env):
    bid = database.insert_entry({"date_received": "2026-07-06", "doc_type": "Bill",
                                 "vendor": "SCE", "status": "Pending",
                                 "notice_summary": "shutoff warning"})
    assert database.get_entry(bid)["notice_summary"] == "shutoff warning"
    database.update_entry(bid, {"notice_summary": "cancellation notice"})
    assert database.get_entry(bid)["notice_summary"] == "cancellation notice"


# ---- UI ---------------------------------------------------------------------

def test_record_view_shows_notice_callout(env, staff_client):
    c, headers = staff_client
    bid = database.insert_entry({
        "date_received": "2026-07-06", "doc_type": "Bill", "vendor": "SCE",
        "status": "Pending", "notice_summary": "2027 rate increase filed with CPUC"})
    html = c.get(f"/entry/{bid}/view").get_data(as_text=True)
    assert "Contains a notice" in html
    assert "2027 rate increase filed with CPUC" in html


def test_dashboard_shows_notice_marker(env, staff_client):
    c, headers = staff_client
    database.insert_entry({
        "date_received": "2026-07-06", "doc_type": "Bill", "vendor": "SCE",
        "status": "Pending", "important": 1,
        "notice_summary": "insurance non-renewal notice"})
    html = c.get("/").get_data(as_text=True)
    assert "insurance non-renewal notice" in html
    assert "Notice" in html
