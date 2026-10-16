"""FOLDER_NAMING_STANDARDS.md — folders encode context; names carry only what
the folder doesn't (approved by Joshua 2026-08-06, after the 08. AUGUST
incident: four June scans confirmed 8/5 were filed into INCOMING BILLS under
the app's one global shape — entity-lead, bill-type token, $amount — in a
folder whose 7,712-file human standard has none of those).

_make_filename(entry, dest_dir) now picks the grammar from the DESTINATION:
  incoming trees:  MM-DD-YY PAYEE - [INV# | doc kind] - [PROP | ENTITY]
  paid trees:      MM-DD-YY ENTITY CK#n|ACH - PAYEE - [detail] - $AMT[ - PROP]
                   (property tail only in the trees that use one)
  everywhere else: the unchanged KAREN_FILENAME_SPEC.md §5 shape.
"""
import os

import app as klr
import database
import filing
import json

from conftest import make_pdf

INCOMING = r"F:\File-Cabinet\Files\2026\ACCOUNTS PAYABLE - INCOMING BILLS\08. AUGUST"
INCOMING_UNC = (r"\\LACKSSERVER2021\File-Cabinet\Files\2026"
                r"\ACCOUNTS PAYABLE - INCOMING BILLS\06. JUNE")
TIMECARDS = r"F:\File-Cabinet\Files\2026\ACCOUNTS PAYABLE - INCOMING TIME CARDS"
PAID_KLR = r"F:\File-Cabinet\Files\2026\KLR\KLR PAID BILLS\8. AUGUST"
PAID_TRUST2 = r"F:\File-Cabinet\Files\2026\TRUST 2\TRUST 2 PAID BILLS\6. JUNE"
PAID_PP = r"F:\File-Cabinet\Files\2026\PP\PP PAID BILLS ALL PROPERTIES\6. JUNE"


# ── profile detection ─────────────────────────────────────────────────────────

def test_folder_profile_detection():
    assert klr._folder_profile(INCOMING) == ("incoming", None)
    assert klr._folder_profile(INCOMING_UNC) == ("incoming", None)
    assert klr._folder_profile(TIMECARDS) == ("incoming", None)
    assert klr._folder_profile(PAID_KLR) == ("paid", "KLR")
    assert klr._folder_profile(PAID_TRUST2) == ("paid", "TRUST2")   # space folded
    assert klr._folder_profile(PAID_PP) == ("paid", "PP")           # suffix after PAID BILLS ok
    assert klr._folder_profile(r"F:\Files\2026\VENDORS - INSURANCE") == ("global", None)
    assert klr._folder_profile(r"F:\File-Cabinet\IntakeSystem\Filed\KLR\GENERAL\2026") \
        == ("global", None)
    assert klr._folder_profile(None) == ("global", None)
    assert klr._folder_profile("") == ("global", None)


# ── the incoming grammar (the incident's regression test) ─────────────────────

def test_incoming_never_entity_lead_billtype_or_amount():
    # Record #999's fields. The app used to emit
    # "08-05-26 KLR REPAIR - CARROLL PLUMBING & MAINT, INC - $698.92 - …"
    # into a folder whose standard has no entity lead, no bill-type word, no $.
    e = {"date_received": "2026-06-24", "entity_code": "KLR",
         "bill_type_code": "REPAIR", "vendor": "Carroll Plumbing & Maint, Inc",
         "amount": 698.92, "doc_type": "Bill"}
    name = klr._make_filename(e, INCOMING)
    assert name == "06-24-26 CARROLL PLUMBING & MAINT, INC - KLR.pdf"
    assert "$" not in name and "REPAIR" not in name
    assert not name.startswith("06-24-26 KLR ")


def test_incoming_with_invoice_and_property():
    e = {"date_received": "2026-08-01", "entity_code": "PP",
         "bill_type_code": "HOA", "vendor": "Advanced Community Services",
         "amount": 350.0, "invoice_number": "2612", "property_code": "PP58",
         "doc_type": "Bill"}
    assert klr._make_filename(e, INCOMING) == \
        "08-01-26 ADVANCED COMMUNITY SERVICES - INV# 2612 - PP58.pdf"


def test_incoming_statement_gets_doc_kind_and_entity_tail():
    # No property applies → the trailing attribution is the entity, and a
    # non-bill document's kind word fills the detail slot.
    e = {"date_received": "2026-07-06", "entity_code": "KLR-LRC",
         "vendor": "Carpinteria Valley Water District", "doc_type": "Statement"}
    assert klr._make_filename(e, INCOMING) == \
        "07-06-26 CARPINTERIA VALLEY WATER DISTRICT - STATEMENT - KLR-LRC.pdf"


def test_incoming_ignores_payment_method_tokens():
    # Even a paid bill filed into INCOMING BILLS carries no CK#/ACH — that
    # shape belongs to the PAID trees (the ~20 pay-rename intruders on
    # ROLLBACK_RENAMES.csv are exactly this mistake).
    e = {"date_received": "2026-06-24", "entity_code": "KLR",
         "vendor": "Frontier", "amount": 55.0, "doc_type": "Bill",
         "payment_method": "Check", "check_number": "44107"}
    name = klr._make_filename(e, INCOMING)
    assert "CK" not in name and "$" not in name


# ── the paid grammar ──────────────────────────────────────────────────────────

def test_paid_tree_carries_property_where_the_tree_does():
    e = {"date_received": "2026-06-26", "entity_code": "TRUST2",
         "bill_type_code": "ELEC", "vendor": "Frontier", "amount": 147.48,
         "property_code": "TL222", "payment_method": "Check",
         "check_number": "13386"}
    assert klr._make_filename(e, PAID_TRUST2) == \
        "06-26-26 TRUST2 CK#13386 - FRONTIER - $147.48 - TL222.pdf"


def test_paid_klr_tree_never_carries_property():
    e = {"date_received": "2026-08-05", "entity_code": "KLR",
         "vendor": "Carroll Plumbing & Maint, Inc", "amount": 698.92,
         "property_code": "T27", "payment_method": "Check",
         "check_number": "44107"}
    name = klr._make_filename(e, PAID_KLR)
    assert name == "08-05-26 KLR CK#44107 - CARROLL PLUMBING & MAINT, INC - $698.92.pdf"
    assert "T27" not in name


def test_paid_ach_keeps_the_archive_double_space():
    e = {"date_received": "2026-08-01", "entity_code": "KLR",
         "vendor": "United Visa", "amount": 1200.0, "payment_method": "ACH"}
    assert klr._make_filename(e, PAID_KLR).startswith("08-01-26 KLR  ACH - UNITED VISA")


def test_paid_detail_slot_uses_invoice_then_account():
    e = {"date_received": "2026-06-26", "entity_code": "PP", "vendor": "Frontier",
         "amount": 10.0, "property_code": "PP16", "payment_method": "Check",
         "check_number": "1", "account_number": "9007"}
    n = klr._make_filename(e, PAID_PP)
    assert n == "06-26-26 PP CK#1 - FRONTIER - ACCT.#9007 - $10.00 - PP16.pdf"
    e["invoice_number"] = "555"
    assert " - INV# 555 - " in klr._make_filename(e, PAID_PP)


# ── everywhere else: the spec-§5 shape is untouched ───────────────────────────

def test_global_profile_unchanged_for_other_folders():
    e = {"date_received": "2026-06-26", "entity_code": "TRUST2",
         "bill_type_code": "ELEC", "vendor": "Frontier",
         "amount": 147.48, "property_code": "TL222"}
    expected = "06-26-26 TRUST2 ELEC - FRONTIER - $147.48 - TL222.pdf"
    assert klr._make_filename(e) == expected
    assert klr._make_filename(
        e, r"F:\File-Cabinet\IntakeSystem\Filed\TRUST2\TL222\2026") == expected


# ── the File-as proposal follows the proposed folder's grammar ────────────────

def _use_archive(env):
    archive = os.path.join(str(env.tmp), "Files")
    os.makedirs(archive, exist_ok=True)
    env.cfg["archive_folder"] = archive
    with open(env.cfg_path, "w", encoding="utf-8") as f:
        json.dump(env.cfg, f, indent=2)
    return archive


def test_file_as_proposal_uses_incoming_grammar_for_bill_rule(env, staff_client):
    _use_archive(env)
    c, _headers = staff_client
    pdf = os.path.join(env.scan_folder, "doc.pdf")
    make_pdf(pdf)
    sid = klr._queue_add({
        "pdf_path": pdf, "mail_type": "Physical Mail", "doc_type": "Bill",
        "vendor": "ACME Utilities", "amount": 147.48, "received_date": "2026-07-10",
        "invoice_number": "INV-99", "entity_code": "TRUST1",
        "property_codes": ["T27"], "bill_type_code": "ELEC"})
    body = c.get(f"/review/{sid}").get_data(as_text=True)
    assert "ACCOUNTS PAYABLE - INCOMING BILLS" in body        # rule folder
    assert "07-10-26 ACME UTILITIES - INV# INV-99 - T27.pdf" in body
    assert "07-10-26 TRUST1 ELEC" not in body                 # dead incident shape


def test_confirm_into_incoming_folder_files_under_incoming_name(env, staff_client):
    archive = _use_archive(env)
    c, headers = staff_client
    pdf = os.path.join(env.scan_folder, "doc.pdf")
    make_pdf(pdf)
    sid = klr._queue_add({
        "pdf_path": pdf, "mail_type": "Physical Mail", "doc_type": "Bill",
        "vendor": "ACME Utilities", "amount": 147.48,
        "received_date": "2026-07-10", "entity_code": "TRUST1",
        "property_codes": ["T27"], "bill_type_code": "ELEC"})
    r = c.post(f"/review/{sid}/confirm?vendor_ok=1&force=1", headers=headers, data={
        "date_received": "2026-07-10", "doc_type": "Bill",
        "vendor": "ACME Utilities", "entity_code": "TRUST1",
        "property_codes": ["T27"], "amount": "147.48", "date_ok": "1",
        # File-as left untouched → server re-derives both from final fields
        "filed_dir": "same", "suggested_filed_dir": "same",
        "filed_name": "same.pdf", "suggested_filed_name": "same.pdf"})
    assert r.status_code == 302
    e = database.get_all_entries()[0]
    assert os.path.dirname(e["filed_path"]) == os.path.join(
        archive, "2026", "ACCOUNTS PAYABLE - INCOMING BILLS", "07. JULY")
    assert os.path.basename(e["filed_path"]) == "07-10-26 ACME UTILITIES - T27.pdf"
    assert os.path.exists(e["filed_path"])
