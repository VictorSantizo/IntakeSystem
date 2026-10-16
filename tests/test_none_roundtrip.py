"""A NULL field must never round-trip through the edit form as the text 'None'.

Production damage (2026-07-22): entry_form.html rendered NULL fields with
`{{ entry.x if entry else '' }}` — with an entry present, Jinja printed the
Python None as the literal text "None" into the input, and saving the form
wrote that string to intake_log (records 780, 791, 796, edited by Nancy).
The string 'None' in account_number then made find_link_candidates treat the
damaged records as sharing a real account and cross-suggest them as each
other's "link original". These tests lock in the 2026-07-23 fixes:
  * the edit form renders NULL fields empty (no value="None" / None textarea),
  * the new/edit POST handlers normalize '' and 'None' to NULL,
  * find_link_candidates ignores placeholder account numbers,
  * the scrub-none server CLI repairs already-damaged rows (and nothing else).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as klr        # noqa: E402
import database          # noqa: E402
from conftest import make_pdf   # noqa: E402


def _entry(**over):
    """Insert a minimal Bill row whose free-text fields are NULL by default."""
    row = {
        "date_received": "2026-07-20",
        "mail_type": "Physical Mail",
        "doc_type": "Bill",
        "status": "Pending",
        "vendor": None,
        "invoice_number": None,
        "account_number": None,
        "due_date": None,
        "notes": None,
    }
    row.update(over)
    return database.insert_entry(row)


def _edit_form(**over):
    """A complete edit-form POST body, mimicking what the browser submits."""
    data = {
        "date_received": "2026-07-20", "mail_type": "Physical Mail",
        "doc_type": "Bill", "bill_type_code": "", "entity_code": "",
        "vendor": "", "amount": "", "due_date": "",
        "invoice_number": "", "account_number": "",
        "status": "Pending", "notes": "",
        "date_ok": "1",   # bypass the year-sanity guard as the fixed date ages
    }
    data.update(over)
    return data


# ---------------------------------------------------------------- the template

def test_edit_form_renders_null_fields_empty(staff_client):
    c, _ = staff_client
    eid = _entry()
    resp = c.get(f"/entry/{eid}/edit")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'name="invoice_number"' in html   # the form really rendered
    assert 'value="None"' not in html
    assert '>None</textarea>' not in html


# ------------------------------------------------------------ the POST handlers

def test_edit_post_normalizes_none_and_blank_to_null(staff_client):
    c, headers = staff_client
    eid = _entry()
    # 'None' is what a pre-fix form echoed back for a NULL field; a blank
    # field must land as NULL too, not ''.
    resp = c.post(f"/entry/{eid}/edit",
                  data=_edit_form(vendor="Goleta Water",
                                  invoice_number="None",
                                  account_number=" none "),
                  headers=headers)
    assert resp.status_code in (302, 303)
    row = database.get_entry(eid)
    assert row["invoice_number"] is None
    assert row["account_number"] is None


def test_edit_post_keeps_real_identifiers(staff_client):
    c, headers = staff_client
    eid = _entry()
    c.post(f"/entry/{eid}/edit",
           data=_edit_form(vendor="Goleta Water",
                           invoice_number="INV-9001",
                           account_number="12-3456"),
           headers=headers)
    row = database.get_entry(eid)
    assert row["invoice_number"] == "INV-9001"
    assert row["account_number"] == "12-3456"


def test_new_entry_post_normalizes_none_to_null(staff_client):
    c, headers = staff_client
    resp = c.post("/entry/new",
                  data=_edit_form(vendor="Acme Utilities",
                                  invoice_number="None",
                                  account_number="None"),
                  headers=headers)
    assert resp.status_code in (302, 303)
    with database.get_conn() as conn:
        row = dict(conn.execute(
            "SELECT * FROM intake_log ORDER BY id DESC LIMIT 1").fetchone())
    assert row["vendor"] == "Acme Utilities"
    assert row["invoice_number"] is None
    assert row["account_number"] is None


def test_review_confirm_normalizes_none_to_null(env, staff_client):
    # 2026-07-26 review finding: new_entry/edit_entry route through _id_field,
    # but confirm_item (the review queue -- the highest-traffic intake path)
    # didn't, so it could still store '' / literal 'None' instead of NULL.
    c, headers = staff_client
    path = os.path.join(env.scan_folder, "confirm_none.pdf")
    make_pdf(path, "invoice text for confirm_none.pdf")
    sid = klr._queue_add({"pdf_path": path, "mail_type": "Email",
                          "vendor": "ACME Utilities", "amount": 100.0,
                          "due_date": "2026-07-15"})
    resp = c.post(f"/review/{sid}/confirm?vendor_ok=1", headers=headers, data={
        "date_received": "2026-07-20", "mail_type": "Physical Mail",
        "doc_type": "Bill", "vendor": "ACME Utilities", "amount": "100.0",
        "due_date": "2026-07-15", "entity_code": "", "property_codes": [],
        "invoice_number": "None", "account_number": " none ",
    })
    assert resp.status_code in (302, 303)
    row = next(e for e in database.get_all_entries() if e["vendor"] == "ACME Utilities")
    assert row["invoice_number"] is None
    assert row["account_number"] is None


def test_bulk_confirm_normalizes_none_to_null(env, staff_client):
    c, headers = staff_client
    path = os.path.join(env.scan_folder, "bulk_none.pdf")
    make_pdf(path, "invoice text for bulk_none.pdf")
    sid = klr._queue_add({"pdf_path": path, "mail_type": "Email",
                          "vendor": "Bulk Vendor", "amount": 50.0,
                          "due_date": "2026-07-15",
                          "invoice_number": "None", "account_number": "None",
                          "entity_code": "", "property_codes": [], "doc_type": "Bill"})
    resp = c.post("/review/bulk_confirm", headers=headers,
                  data={"scan_ids": [str(sid)]})
    assert resp.status_code in (302, 303)
    row = next(e for e in database.get_all_entries() if e["vendor"] == "Bulk Vendor")
    assert row["invoice_number"] is None
    assert row["account_number"] is None


# ------------------------------------------------------- link-original matching

def test_link_candidates_ignore_placeholder_account(env):
    # Two unrelated damaged rows "sharing" account 'None' must not link.
    a = _entry(account_number="None", vendor="Nancy Damage Alpha")
    b = _entry(account_number="None", vendor="Other Vendor Beta")
    ids = [r["id"] for r in database.find_link_candidates(database.get_entry(a))]
    assert b not in ids


def test_link_candidates_still_match_real_accounts(env):
    a = _entry(account_number="12-3456", vendor="Goleta Water")
    b = _entry(account_number="12-3456", vendor="Completely Different")
    ids = [r["id"] for r in database.find_link_candidates(database.get_entry(a))]
    assert b in ids


# ------------------------------------------------------------- the server CLI

def test_scrub_none_cli_repairs_damaged_rows(env, capsys):
    damaged = _entry(invoice_number="None", account_number="None",
                     vendor="None", notes="None")
    # Real values that merely contain/resemble 'None' must survive: equality,
    # not substring, and SQLite's default BINARY collation is case-sensitive.
    keeper = _entry(invoice_number="INV-1", account_number="Nonesuch 22",
                    notes="none")

    assert klr._cli_reports(["scrub-none"]) is True          # dry run
    assert database.get_entry(damaged)["invoice_number"] == "None"
    out = capsys.readouterr().out
    assert f"#{damaged}" in out and "Dry run" in out

    assert klr._cli_reports(["scrub-none", "apply"]) is True
    row = database.get_entry(damaged)
    assert row["invoice_number"] is None
    assert row["account_number"] is None
    assert row["vendor"] is None
    assert row["notes"] is None
    kept = database.get_entry(keeper)
    assert kept["invoice_number"] == "INV-1"
    assert kept["account_number"] == "Nonesuch 22"
    assert kept["notes"] == "none"

    # Idempotent: a second apply finds nothing.
    klr._cli_reports(["scrub-none", "apply"])
    assert "nothing to scrub" in capsys.readouterr().out
