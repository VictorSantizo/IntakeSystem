"""Bulk 'Fill missing details with AI' over Harvest-Bot records: the office's
manual-filing-cabinet harvest logged ~80 bills with no AI extraction
(processed_by='Harvest-Bot'), leaving fields like amount/due date blank. This
covers the shared _ai_fill_entry helper's worker path directly (fills blanks
only, skips a record whose file is gone, survives a per-record exception) and
the admin-gated /settings/ai_fill_harvest route (admin-only, one run at a
time, spawns a background thread on success)."""
import os
from urllib.parse import unquote_plus

import database
import routes_entry
import app as klr

from conftest import make_pdf


def _seed_harvest_entry(env, name, processed_by="Harvest-Bot", doc_type="Bill",
                         blank=("amount", "due_date", "invoice_number", "account_number"),
                         **overrides):
    """A Harvest-Bot-style record: a real file on disk, missing the fields the
    bulk AI fill is meant to backfill (vendor stays populated by default so
    tests can prove existing values are never touched)."""
    path = os.path.join(env.filed_folder, name)
    make_pdf(path)
    data = {
        "date_received": "2026-06-01", "mail_type": "Physical Mail",
        "entity_code": "TRUST1", "property_code": "T27",
        "doc_type": doc_type, "vendor": "ACME Utilities",
        "amount": 147.48, "due_date": "2026-07-15",
        "invoice_number": "INV-1", "account_number": "12-3456",
        "status": "Processed", "processed_by": processed_by,
        "filed_path": path,
    }
    for f in blank:
        data[f] = None
    data.update(overrides)
    entry_id = database.insert_entry(data)
    return entry_id, path


class _FakeThread:
    """Runs the target synchronously on .start() so route tests are deterministic."""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}
    def start(self):
        self._target(*self._args, **self._kwargs)


def _run_thread_inline(monkeypatch):
    monkeypatch.setattr(routes_entry, "threading", type("FakeThreadingModule", (), {"Thread": _FakeThread}))


# ── _harvest_ai_fill_candidates: the population the bulk run offers to fix ───

def test_candidates_filters_processed_by_doctype_and_blank(env):
    target, _ = _seed_harvest_entry(env, "a.pdf")
    _seed_harvest_entry(env, "b.pdf", blank=())                      # nothing blank -> excluded
    _seed_harvest_entry(env, "c.pdf", processed_by="TestUser")       # not Harvest-Bot -> excluded
    _seed_harvest_entry(env, "d.pdf", doc_type="Income")             # not a Bill -> excluded
    ids = [e["id"] for e in routes_entry._harvest_ai_fill_candidates()]
    assert ids == [target]


# ── Worker ────────────────────────────────────────────────────────────────────

def test_worker_fills_blanks_only(env, monkeypatch):
    entry_id, _ = _seed_harvest_entry(env, "bill.pdf")   # vendor set, other four blank
    monkeypatch.setattr(routes_entry, "classify_document", lambda path: {
        "vendor": "WRONG VENDOR", "amount": "50.00", "due_date": "2030-01-01",
        "invoice_number": "INV-99", "account_number": "10329"})
    routes_entry._ai_fill_harvest_worker([entry_id], user="TestAdmin")
    e = database.get_entry(entry_id)
    assert e["vendor"] == "ACME Utilities"           # existing -> untouched
    assert e["amount"] == 50.0                       # blank -> filled
    assert e["due_date"] == "2030-01-01"
    assert e["invoice_number"] == "INV-99"
    assert e["account_number"] == "10329"
    feed = database.get_activity_feed(20)
    per_entry = [a for a in feed if a["entry_id"] == entry_id]
    assert any(a["action"] == "AI filled missing details" and a["user"] == "TestAdmin"
               for a in per_entry)
    summary = next(a for a in feed if a["action"] == "Bulk AI fill")
    assert summary["entry_id"] is None
    assert "1 filled" in summary["detail"]


def test_worker_skips_record_with_missing_file(env, monkeypatch):
    entry_id, path = _seed_harvest_entry(env, "bill.pdf")
    os.remove(path)
    called = []
    monkeypatch.setattr(routes_entry, "classify_document", lambda p: called.append(p) or {})
    routes_entry._ai_fill_harvest_worker([entry_id], user="TestAdmin")
    assert not called                                # file missing -> no AI call
    assert database.get_entry(entry_id)["amount"] is None
    summary = next(a for a in database.get_activity_feed(20) if a["action"] == "Bulk AI fill")
    assert "1 missing their file" in summary["detail"]


def test_worker_survives_per_record_exception(env, monkeypatch):
    bad_id, bad_path = _seed_harvest_entry(env, "bad.pdf")
    good_id, good_path = _seed_harvest_entry(env, "good.pdf")

    def fake_classify(path):
        if path == bad_path:
            raise ValueError("boom — corrupt PDF")
        return {"amount": "88.00", "due_date": "2030-05-05",
                "invoice_number": "INV-5", "account_number": "555"}

    monkeypatch.setattr(routes_entry, "classify_document", fake_classify)
    routes_entry._ai_fill_harvest_worker([bad_id, good_id], user="TestAdmin")
    assert database.get_entry(bad_id)["amount"] is None      # exception swallowed, untouched
    assert database.get_entry(good_id)["amount"] == 88.0     # run kept going
    assert klr._job_running("harvest_ai_fill") is False      # flag always released
    summary = next(a for a in database.get_activity_feed(20) if a["action"] == "Bulk AI fill")
    assert "1 filled" in summary["detail"] and "1 errored" in summary["detail"]


# ── Route: /settings/ai_fill_harvest ─────────────────────────────────────────

def test_route_blocks_non_admin(env, staff_client):
    c, headers = staff_client
    r = c.post("/settings/ai_fill_harvest", headers=headers, follow_redirects=False)
    assert r.status_code == 302
    assert database.get_activity_feed(5) == []       # never ran


def test_route_refuses_second_run_while_in_progress(env, admin_client, monkeypatch):
    monkeypatch.setitem(klr._jobs_running, "harvest_ai_fill", True)
    c, headers = admin_client
    r = c.post("/settings/ai_fill_harvest", headers=headers, follow_redirects=False)
    assert r.status_code == 302
    location = unquote_plus(r.headers["Location"])
    assert "already in progress" in location


def test_route_no_candidates_message(env, admin_client):
    c, headers = admin_client
    r = c.post("/settings/ai_fill_harvest", headers=headers, follow_redirects=False)
    assert r.status_code == 302
    location = unquote_plus(r.headers["Location"])
    assert "No harvested records have blank details to fill" in location
    assert "tab=app" in location


def test_route_success_spawns_thread_and_fills(env, admin_client, monkeypatch):
    entry_id, _ = _seed_harvest_entry(env, "bill.pdf")
    monkeypatch.setattr(routes_entry, "classify_document", lambda path: {
        "amount": "50.00", "due_date": "2030-01-01",
        "invoice_number": "INV-9", "account_number": "999"})
    _run_thread_inline(monkeypatch)
    c, headers = admin_client
    r = c.post("/settings/ai_fill_harvest", headers=headers, follow_redirects=False)
    assert r.status_code == 302
    location = unquote_plus(r.headers["Location"])
    assert "Filling blanks on 1 harvested record in the background" in location
    assert "tab=app" in location
    e = database.get_entry(entry_id)
    assert e["amount"] == 50.0                       # the (fake) thread actually ran
    assert klr._job_running("harvest_ai_fill") is False  # flag released when done
