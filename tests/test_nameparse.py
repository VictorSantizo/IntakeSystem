"""nameparse.py: Karen's filename convention + folder priors. Regression-pins
the scarred parsing rules ported from harvest_daily_filing.py (amount comma
bug, check-number boundaries, entity token boundaries) and the path-prior
fixes (AP-incoming must match directory segments, not the whole path)."""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nameparse  # noqa: E402


def _p(name, folder=r"F:\File-Cabinet\Files\2026\ACCOUNTS PAYABLE - INCOMING BILLS\07. JULY"):
    return os.path.join(folder, name)


# ── parse_name ────────────────────────────────────────────────────────────────

def test_full_convention_name():
    d = nameparse.parse_name(_p("07-24-26 TRUST3 CK#3012 - SWAN CABINETS - $1,230.00 - TB2.pdf"))
    assert d["date"] == datetime.date(2026, 7, 24)
    assert d["entity"] == "TRUST3"
    assert d["amounts"] == [1230.0]
    assert d["payee"] == "SWAN CABINETS"
    assert d["check"] == "3012"
    assert d["method"] == "Check"
    assert d["props"] == ["TB002"]
    assert d["is_payment"] and not d["is_nonbill"]


def test_amount_without_comma_regression():
    # The RE_AMT lesson: "$1230.00" must parse as 1230, never 123 (the old
    # zero-or-more comma branch let the bare-digits alternative win at "123").
    d = nameparse.parse_name(_p("07-24-26 TRUST3 CK#3012 - SWAN - $1230.00.pdf"))
    assert d["amounts"] == [1230.0]


def test_date_variants():
    assert nameparse.parse_name(_p("2026_07_24 KLR THING.pdf"))["date"] == datetime.date(2026, 7, 24)
    assert nameparse.parse_name(_p("07242026.pdf"))["date"] == datetime.date(2026, 7, 24)
    assert nameparse.parse_name(_p("7.24.26 KLR THING.pdf"))["date"] == datetime.date(2026, 7, 24)
    # An account-number-ish stem must NOT read as a scanner date.
    assert nameparse.parse_name(_p("01016854.pdf"))["date"] is None


def test_typoed_year_flags_never_parses():
    # Record #941 (2026-08-06): "06-30-256" — a year typo for 26 — parsed as
    # the year 256 and a June 2026 cash-flow statement was logged as 0256-06-30
    # (permanent record_no 560630-001). A date-shaped prefix that can't be a
    # real date must yield NO date and raise bad_date, never a guess (spec §3.1).
    d = nameparse.parse_name(_p("06-30-256 NOTE TO CINDY WITH STATEMENTS.pdf"))
    assert d["date"] is None
    assert d["bad_date"] is True
    # Same for the underscore form and for impossible month/day typos.
    assert nameparse.parse_name(_p("0256_06_30 KLR THING.pdf"))["bad_date"] is True
    assert nameparse.parse_name(_p("06-45-26 KLR THING.pdf"))["bad_date"] is True
    assert nameparse.parse_name(_p("13-01-26 KLR THING.pdf"))["bad_date"] is True


def test_typoed_year_still_strips_from_payee():
    # The pseudo-date is still removed from the payee guess even though it
    # didn't parse — "06-30-256" must not smear into the vendor name.
    d = nameparse.parse_name(_p("06-30-256 NOTE TO CINDY WITH STATEMENTS.pdf"))
    assert d["payee"] == "NOTE TO CINDY WITH STATEMENTS"


def test_real_and_absent_dates_never_flag():
    for name in ("07-24-26 KLR THING.pdf", "07-24-2026 KLR THING.pdf",
                 "2026_07_24 KLR THING.pdf", "07242026.pdf",
                 "NO DATE HERE AT ALL.pdf", "01016854.pdf"):
        assert nameparse.parse_name(_p(name))["bad_date"] is False, name


def test_check_number_boundaries():
    assert nameparse.parse_name(_p("06-01-26 KLR DECK 2026 REPAIR.pdf"))["check"] is None
    assert nameparse.parse_name(_p("06-01-26 KLR CONF 160379009.pdf"))["check"] is None


def test_entity_token_boundaries():
    assert nameparse.parse_name(_p("06-01-26 TRUSTWORTHY PLUMBING.pdf"))["entity"] is None
    assert nameparse.parse_name(_p("06-01-26 KLR - BRITTANY SMITH.pdf"))["entity"] == "KLR"


def test_property_prefix_infers_entity_and_att_guard():
    d = nameparse.parse_name(_p("06-01-26 ELEC - FRONTIER - TL222.pdf"))
    assert d["entity"] == "TRUST2" and d["props"] == ["TL222"]
    d = nameparse.parse_name(_p("06-01-26 KLR AT&T 805-123-4567.pdf"))
    assert d["props"] == []


def test_nonbill_and_summary_vetoes():
    assert nameparse.parse_name(_p("06-01-26 TRUST OWNER DRAW $500.pdf"))["is_nonbill"]
    assert nameparse.parse_name(_p("06-01-26 TRUST TRANSFER TO PP.pdf"))["is_nonbill"]
    assert nameparse.parse_name(_p("JUNE RECAP CK#3001-3050 $45,210.pdf"))["is_summary"]


# ── path_priors ───────────────────────────────────────────────────────────────

def test_paid_tree_detection():
    p = nameparse.path_priors(
        r"F:\File-Cabinet\Files\2026\TRUST 3\TRUST3  PAID BILLS\07. JULY\x.pdf")
    assert p["in_paid_tree"] and p["entity"] == "TRUST3"
    assert p["year"] == 2026 and p["month"] == 7


def test_unpaid_tree_is_not_paid():
    p = nameparse.path_priors(
        r"F:\File-Cabinet\Files\2026\KLR\KLR UNPAID BILLS\x.pdf")
    assert not p["in_paid_tree"]
    assert p["entity"] == "KLR"


def test_ap_incoming_matches_segments_not_filenames():
    # Harvest's whole-path bug: a FILE merely named after the folder must not
    # count as being filed in it.
    inside = nameparse.path_priors(
        r"F:\File-Cabinet\Files\2026\ACCOUNTS PAYABLE - INCOMING BILLS\07. JULY\x.pdf")
    assert inside["in_ap_incoming"]
    named = nameparse.path_priors(
        r"F:\File-Cabinet\Files\2026\FAMILY\ACCOUNTS PAYABLE - INCOMING BILLS.pdf")
    assert not named["in_ap_incoming"]


def test_entity_from_spaced_tree():
    assert nameparse.path_priors(r"F:\FC\Files\2026\TRUST 2\x.pdf")["entity"] == "TRUST2"
    assert nameparse.path_priors(r"F:\FC\Files\2026\KLR\KLR PAID BILLS\x.pdf")["entity"] == "KLR"


def test_category_segment():
    p = nameparse.path_priors(r"F:\FC\Files\2026\VENDORS - INSURANCE\x.pdf")
    assert p["category"] == "VENDORS - INSURANCE"
