"""payment_rules.py — the rules miner over the paid-backfill datasets.

Pure-function tests on synthetic payments (cadence, entity dominance, method
rules, check-book gaps, variant clusters) plus one end-to-end pass writing the
artifact from real CSV files.
"""
import csv
import datetime
import os

import database
import paid_backfill as pb
import payment_rules as prm

D = datetime.date


def _row(date, payee, entity="KLR", amount=100.0, check="", method="Check",
         props=None, year=2026):
    return {"date": date, "entity": entity, "method": method, "check": check,
            "amount": amount, "payee": payee, "props": props or [],
            "year": year}


def _monthly(payee, months, day=10, **kw):
    return [_row(D(2026, m, day), payee, **kw) for m in months]


# ── cadence ───────────────────────────────────────────────────────────────────

def test_monthly_cadence_and_next_due():
    groups = prm.group_by_vendor(_monthly("SO CAL EDISON", (1, 2, 3, 4, 5, 6)))
    cad = prm.vendor_cadences(groups, today=D(2026, 6, 20))
    assert len(cad) == 1
    v = cad[0]
    assert v["cadence"] == "monthly"
    assert D(2026, 7, 8) <= v["next"] <= D(2026, 7, 12)   # last + median gap
    assert not v["overdue"]


def test_overdue_rhythm_is_flagged():
    groups = prm.group_by_vendor(_monthly("GONE QUIET WATER", (1, 2, 3, 4)))
    cad = prm.vendor_cadences(groups, today=D(2026, 8, 6))
    assert cad[0]["overdue"] is True


def test_quarterly_and_adhoc():
    rows = [_row(D(2026, m, 5), "QUARTERLY PEST CONTROL") for m in (1, 4, 7, 10)]
    rows += [_row(D(2026, m, d), "RANDOM HANDYMAN")
             for m, d in ((1, 3), (1, 10), (6, 1), (6, 15))]   # gaps 7/142/14
    cad = prm.vendor_cadences(prm.group_by_vendor(rows), today=D(2026, 10, 6))
    names = {v["vendor"]: v["cadence"] for v in cad}
    assert names.get("QUARTERLY PEST CONTROL") == "quarterly"
    assert "RANDOM HANDYMAN" not in names          # no stable beat → no rule


def test_two_payments_is_not_a_rhythm():
    cad = prm.vendor_cadences(prm.group_by_vendor(_monthly("NEWBIE LLC", (1, 2))))
    assert cad == []


# ── entity / method / property rules ─────────────────────────────────────────

def test_dominant_entity_and_split_vendors():
    rows = _monthly("EDISON POWER", (1, 2, 3, 4), entity="TRUST2")
    rows += _monthly("SHARED LANDSCAPING", (1, 3, 5), entity="KLR")
    rows += _monthly("SHARED LANDSCAPING", (2, 4, 6), entity="PP")
    dom, split = prm.entity_rules(prm.group_by_vendor(rows))
    assert [v["vendor"] for v in dom] == ["EDISON POWER"]
    assert dom[0]["entity"] == "TRUST2" and dom[0]["share"] == 1.0
    assert [v["vendor"] for v in split] == ["SHARED LANDSCAPING"]


def test_method_rule_needs_unanimity():
    rows = _monthly("ALWAYS ACH UTILITY", (1, 2, 3), method="ACH")
    rows += _monthly("MIXED PAYEE CORP", (1, 2), method="ACH")
    rows += _monthly("MIXED PAYEE CORP", (3, 4), method="Check")
    meth = prm.method_rules(prm.group_by_vendor(rows))
    assert [v["vendor"] for v in meth] == ["ALWAYS ACH UTILITY"]
    assert meth[0]["method"] == "ACH"


def test_property_ties():
    rows = _monthly("POOL GUY SERVICE", (1, 2, 3), props=["PP58"])
    pr = prm.property_rules(prm.group_by_vendor(rows))
    assert pr[0]["props"][0] == ("PP58", 3)


# ── check books ───────────────────────────────────────────────────────────────

def test_check_books_split_on_big_jumps_and_count_gaps():
    rows = [_row(D(2026, 1, i + 1), "V", check=str(n))
            for i, n in enumerate((100, 101, 102, 104, 105, 106))]  # 103 missing
    rows += [_row(D(2026, 2, i + 1), "V", check=str(n))
             for i, n in enumerate((5000, 5001, 5002, 5003, 5004))]
    series = prm.check_series(rows)
    assert len(series) == 1
    books = series[0]["books"]
    assert len(books) == 2
    assert books[0]["lo"] == 100 and books[0]["hi"] == 106 and books[0]["missing"] == 1
    assert books[1]["missing"] == 0


def test_reused_check_numbers_surface():
    rows = [_row(D(2026, 1, d), "V", check="777") for d in (1, 2, 3)]
    rows += [_row(D(2026, 1, d), "V", check=str(700 + d)) for d in (4, 5, 6, 7)]
    series = prm.check_series(rows)
    assert 777 in series[0]["reused"]


# ── vendor grouping / variants ────────────────────────────────────────────────

def test_spelling_variants_cluster_but_different_vendors_do_not():
    rows = [_row(D(2026, 1, 1), "SO. CALIF. EDISON"),
            _row(D(2026, 2, 1), "SO CALIF EDISON"),
            _row(D(2026, 3, 1), "CARROLL PLUMBING")]
    groups = prm.group_by_vendor(rows)
    assert len(groups) == 2
    var = prm.name_variants(groups)
    assert len(var) == 1 and len(var[0]["variants"]) == 2


# ── lag from the app's own records ────────────────────────────────────────────

def test_receipt_to_payment_lag_reads_paid_records(env):
    database.insert_entry({"vendor": "A", "status": "Paid", "doc_type": "Bill",
                           "date_received": "2026-06-01", "paid_date": "2026-06-15"})
    database.insert_entry({"vendor": "B", "status": "Paid", "doc_type": "Bill",
                           "date_received": "2026-06-01", "paid_date": "2026-05-01"})
    database.insert_entry({"vendor": "C", "status": "Pending", "doc_type": "Bill",
                           "date_received": "2026-06-01"})
    assert prm.receipt_to_payment_lag(env.db) == [14]   # typo + pending dropped


# ── end to end ────────────────────────────────────────────────────────────────

def test_analyze_and_artifact_from_real_csvs(env, tmp_path):
    ops = tmp_path / "ops"
    ops.mkdir()
    rows = []
    for m in (1, 2, 3, 4, 5, 6):
        rows.append({"disposition": "create-paid", "reason": "clean",
                     "unc_path": f"\\\\X\\{m}.pdf", "name_date": f"2026-{m:02d}-10",
                     "entity": "KLR", "method": "Check", "check": str(44100 + m),
                     "amount": "147.48", "payee": "ACME UTILITIES", "props": "",
                     "matched_id": "", "matched_status": "", "matched_vendor": "",
                     "matched_amount": "", "matched_check": ""})
    rows.append({"disposition": "skip-movement", "reason": "deposit",
                 "unc_path": "\\\\X\\d.pdf", "name_date": "2026-01-05",
                 "entity": "KLR", "method": "", "check": "", "amount": "500.00",
                 "payee": "KLR DEPOSIT", "props": "", "matched_id": "",
                 "matched_status": "", "matched_vendor": "",
                 "matched_amount": "", "matched_check": ""})
    with open(ops / "PAID_BACKFILL_2026.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=pb._CSV_COLS)
        w.writeheader()
        w.writerows(rows)
    res = prm.analyze(str(ops), [2025, 2026], today=D(2026, 6, 20))
    assert len(res["rows"]) == 6                       # movement excluded
    assert res["cadences"][0]["vendor"] == "ACME UTILITIES"
    out = tmp_path / "PAYMENT_RULES.html"
    prm.write_html(res, str(out))
    page = out.read_text(encoding="utf-8")
    assert "ACME UTILITIES" in page and "monthly" in page
    assert "Expected Bills" in page
