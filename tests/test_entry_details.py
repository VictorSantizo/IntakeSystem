"""Record view: the visible Document File path row, and 'Fill missing details
with AI' for records registered without extraction (blank fields only —
existing values are never overwritten)."""
import os

import database
import routes_entry
import app as klr

from test_dedup_triage import _ingest_one, _confirm_first_item


def test_entry_view_shows_document_path(env, staff_client):
    c, headers = staff_client
    _ingest_one(env, "bill.pdf")
    entry = _confirm_first_item(env, staff_client)
    r = c.get(f"/entry/{entry['id']}/view")
    assert r.status_code == 200
    assert b"Document File" in r.data
    assert entry["filed_path"].encode() in r.data            # full path visible
    assert b"Copy path" in r.data
    # this record has no blank details, so no AI-fill offer
    assert b"Fill missing details with AI" not in r.data


def test_ai_fill_fills_only_blank_fields(env, staff_client, monkeypatch):
    c, headers = staff_client
    _ingest_one(env, "bill.pdf")
    entry = _confirm_first_item(env, staff_client)
    # blank out two fields, like a record harvested from manual filing
    database.update_entry(entry["id"], {"amount": None, "account_number": ""})
    r = c.get(f"/entry/{entry['id']}/view")
    assert b"Fill missing details with AI" in r.data

    monkeypatch.setattr(routes_entry, "classify_document", lambda path: {
        "vendor": "WRONG VENDOR", "amount": "50.00", "due_date": "2099-01-01",
        "invoice_number": "SHOULD-NOT-OVERWRITE", "account_number": "10329"})
    r = c.post(f"/entry/{entry['id']}/ai_fill", headers=headers)
    assert r.status_code == 302
    e = database.get_entry(entry["id"])
    assert e["amount"] == 50.0                       # blank -> filled
    assert e["account_number"] == "10329"            # blank -> filled
    assert e["vendor"] == "ACME Utilities"           # existing -> untouched
    assert e["due_date"] == "2026-07-15"             # existing -> untouched
    assert e["invoice_number"] == "INV-1"            # existing -> untouched


def test_ai_fill_with_missing_file_changes_nothing(env, staff_client, monkeypatch):
    c, headers = staff_client
    _ingest_one(env, "bill.pdf")
    entry = _confirm_first_item(env, staff_client)
    database.update_entry(entry["id"], {"amount": None})
    os.remove(entry["filed_path"])
    called = []
    monkeypatch.setattr(routes_entry, "classify_document",
                        lambda p: called.append(p) or {})
    r = c.post(f"/entry/{entry['id']}/ai_fill", headers=headers)
    assert r.status_code == 302
    assert not called                                # no file -> no AI call
    assert database.get_entry(entry["id"])["amount"] is None
