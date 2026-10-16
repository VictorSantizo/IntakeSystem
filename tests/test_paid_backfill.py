"""paid_backfill.py — the phase-1 dry run that matches a year's PAID BILLS
trees against the intake log WITHOUT writing anything.

Each disposition gets a real file in a temp paid tree and a real record in a
temp DB; the assertions pin what apply WOULD do, because the report is the
only thing standing between the archive and 1,300 DB writes.
"""
import csv
import hashlib
import os

import database
import paid_backfill


def _tree(tmp_path):
    d = tmp_path / "Files" / "2026" / "KLR" / "KLR PAID BILLS" / "6. JUNE"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file(folder, name, content=b"%PDF-1.4 fake"):
    p = folder / name
    p.write_bytes(content)
    return str(p)


def _run(env, tmp_path, **kw):
    out = tmp_path / "out"
    return paid_backfill.run_dryrun(str(tmp_path / "Files"), 2026, env.db,
                                    str(out), **kw)


def _csv_rows(res):
    with open(res["csv"], encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _by_disp(res):
    return {r["unc_path"].split("\\")[-1]: r["disposition"] for r in _csv_rows(res)}


# ── each disposition, one honest case ─────────────────────────────────────────

def test_pending_bill_matches_and_would_be_marked_paid(env, tmp_path):
    database.insert_entry({"vendor": "ACME Utilities", "amount": 147.48,
                           "date_received": "2026-06-10", "status": "Pending",
                           "doc_type": "Bill", "entity_code": "KLR"})
    _file(_tree(tmp_path), "06-14-26 KLR CK#12345 - ACME UTILITIES - $147.48.pdf")
    res = _run(env, tmp_path)
    assert res["summary"] == {"match": 1}
    row = _csv_rows(res)[0]
    assert row["matched_status"] == "Pending"
    assert "mark Paid" in row["reason"] and "CK#12345" in row["reason"]


def test_paid_under_a_different_check_is_held_not_matched(env, tmp_path):
    database.insert_entry({"vendor": "ACME Utilities", "amount": 147.48,
                           "date_received": "2026-06-10", "status": "Paid",
                           "check_number": "111", "doc_type": "Bill"})
    _file(_tree(tmp_path), "06-14-26 KLR CK#222 - ACME UTILITIES - $147.48.pdf")
    res = _run(env, tmp_path)
    assert res["summary"] == {"held": 1}
    assert "different cycle" in _csv_rows(res)[0]["reason"]


def test_unmatched_payment_would_create_a_paid_record(env, tmp_path):
    _file(_tree(tmp_path), "06-14-26 KLR CK#333 - BRAND NEW VENDOR GROUP - $50.00.pdf")
    res = _run(env, tmp_path)
    assert res["summary"] == {"create-paid": 1}


def test_blank_amount_record_vetoes_a_fresh_paid_record(env, tmp_path):
    """cabinet_watch's near-match rule: a vendor-overlap record with NO amount
    can never exact-match, so registering fresh would mint a twin — hold."""
    database.insert_entry({"vendor": "Blank Amount Vendor Co", "amount": None,
                           "date_received": "2026-06-01", "status": "Pending",
                           "doc_type": "Bill"})
    _file(_tree(tmp_path), "06-14-26 KLR CK#555 - BLANK AMOUNT VENDOR - $80.00.pdf")
    res = _run(env, tmp_path)
    assert res["summary"] == {"held": 1}
    assert "twin" in _csv_rows(res)[0]["reason"]


def test_unpayable_records_never_veto(env, tmp_path):
    """A bank statement or legal record is never 'paid' — vendor overlap with
    one must not hold a packet (the first live dry run held 146 TRUST-tree
    packets against one blank-amount bank-statement record)."""
    database.insert_entry({"vendor": "Montecito Bank & Trust", "amount": None,
                           "date_received": "2026-06-01", "status": "Pending",
                           "doc_type": "Bank Statement"})
    _file(_tree(tmp_path), "06-14-26 KLR CK#888 - MONTECITO BANK - $40.00.pdf")
    res = _run(env, tmp_path)
    assert res["summary"] == {"create-paid": 1}


def test_one_generic_shared_token_is_not_a_twin_risk(env, tmp_path):
    """'LADOUCE ELECTRIC' and 'ACME ELECTRIC' share only ELECTRIC — different
    vendors, no veto. (CHASE↔CHASE still vetoes: the whole name matches.)"""
    database.insert_entry({"vendor": "Ladouce Electric Contractors", "amount": None,
                           "date_received": "2026-06-01", "status": "Pending",
                           "doc_type": "Bill"})
    _file(_tree(tmp_path), "06-14-26 KLR CK#889 - ACME ELECTRIC - $30.00.pdf")
    res = _run(env, tmp_path)
    assert res["summary"] == {"create-paid": 1}


def test_veto_capacity_releases_the_other_cycles(env, tmp_path):
    """One blank-amount pending bill may hold only its two closest-dated
    packets — the rest of the vendor's year is other cycles → create-paid.
    (First live dry run: one Edison record held 99 packets.)"""
    database.insert_entry({"vendor": "So Cal Edison Co", "amount": None,
                           "date_received": "2026-06-05", "status": "Pending",
                           "doc_type": "Bill"})
    d = _tree(tmp_path)
    for mm, ck in (("06", "801"), ("07", "802"), ("09", "803"), ("10", "804")):
        _file(d, f"{mm}-10-26 KLR CK#{ck} - EDISON CO - $25.00.pdf")
    res = _run(env, tmp_path)
    assert res["summary"] == {"held": 2, "create-paid": 2}
    disp = _by_disp(res)
    assert disp["06-10-26 KLR CK#801 - EDISON CO - $25.00.pdf"] == "held"
    assert disp["07-10-26 KLR CK#802 - EDISON CO - $25.00.pdf"] == "held"
    assert disp["10-10-26 KLR CK#804 - EDISON CO - $25.00.pdf"] == "create-paid"


def test_two_packets_one_record_keeps_the_closer_date(env, tmp_path):
    """A monthly bill logged once must not collect every month's packet — the
    closest-dated one matches, the rest go to a person."""
    database.insert_entry({"vendor": "Monthly Water Co", "amount": 60.0,
                           "date_received": "2026-06-05", "status": "Pending",
                           "doc_type": "Bill"})
    d = _tree(tmp_path)
    _file(d, "06-10-26 KLR CK#601 - MONTHLY WATER - $60.00.pdf")
    _file(d, "07-10-26 KLR CK#602 - MONTHLY WATER - $60.00.pdf")
    res = _run(env, tmp_path)
    assert res["summary"] == {"match": 1, "held": 1}
    disp = _by_disp(res)
    assert disp["06-10-26 KLR CK#601 - MONTHLY WATER - $60.00.pdf"] == "match"
    assert disp["07-10-26 KLR CK#602 - MONTHLY WATER - $60.00.pdf"] == "held"


def test_movements_summaries_and_unmarked_files_sort_themselves(env, tmp_path):
    d = _tree(tmp_path)
    _file(d, "06-14-26 KLR DEPOSIT - $500.00.pdf")               # money movement
    _file(d, "06-30-26 KLR PAID BILLS SUMMARY.pdf")              # recap sheet
    _file(d, "06-14-26 KLR - SOME CONSULTANT - $75.00.pdf")      # no CK/ACH marker
    res = _run(env, tmp_path)
    assert res["summary"] == {"skip-movement": 1, "skip-summary": 1, "needs-ocr": 1}


def test_out_of_year_payment_date_is_held(env, tmp_path):
    _file(_tree(tmp_path), "06-14-25 KLR CK#444 - WHOEVER GROUP - $20.00.pdf")
    res = _run(env, tmp_path)
    assert res["summary"] == {"held": 1}
    assert "outside 2026" in _csv_rows(res)[0]["reason"]


def test_already_registered_and_byte_identical_files_are_left_alone(env, tmp_path):
    d = _tree(tmp_path)
    known = _file(d, "06-01-26 KLR CK#700 - KNOWN VENDOR - $10.00.pdf")
    database.insert_entry({"vendor": "Known Vendor", "amount": 10.0,
                           "date_received": "2026-06-01", "status": "Paid",
                           "check_number": "700", "doc_type": "Bill",
                           "filed_path": known})
    twin_bytes = b"%PDF-1.4 twin"
    _file(d, "06-02-26 KLR CK#701 - HASH TWIN LLC - $11.00.pdf", twin_bytes)
    database.insert_entry({"vendor": "Different Name Entirely", "amount": 999.0,
                           "date_received": "2026-06-02", "status": "Paid",
                           "doc_type": "Bill",
                           "file_hash": hashlib.sha256(twin_bytes).hexdigest()})
    res = _run(env, tmp_path)
    assert res["summary"] == {"known": 1, "linked-by-hash": 1}


def test_no_hash_mode_still_matches_by_name(env, tmp_path):
    database.insert_entry({"vendor": "ACME Utilities", "amount": 147.48,
                           "date_received": "2026-06-10", "status": "Pending",
                           "doc_type": "Bill"})
    _file(_tree(tmp_path), "06-14-26 KLR CK#12345 - ACME UTILITIES - $147.48.pdf")
    res = _run(env, tmp_path, do_hash=False)
    assert res["summary"] == {"match": 1}


# ── the artifacts ─────────────────────────────────────────────────────────────

def test_artifacts_use_unc_paths_and_say_nothing_was_changed(env, tmp_path):
    _file(_tree(tmp_path), "06-14-26 KLR CK#333 - SOLO VENDOR GROUP - $50.00.pdf")
    res = _run(env, tmp_path)
    with open(res["html"], encoding="utf-8") as f:
        page = f.read()
    assert "Nothing was changed" in page
    assert "\\\\LACKSSERVER2021\\File-Cabinet\\Files" in page
    assert "T:\\" not in page.replace("T:\\_OPS", "")  # no drive letters leak
    assert _csv_rows(res)[0]["unc_path"].startswith("\\\\LACKSSERVER2021")


def test_apply_mode_refuses_to_run(env, tmp_path, capsys):
    import pytest
    with pytest.raises(SystemExit) as ex:
        paid_backfill.main(["apply", "--db", env.db,
                            "--archive", str(tmp_path / "Files"),
                            "--out", str(tmp_path / "out")])
    assert "phase 2" in str(ex.value)
