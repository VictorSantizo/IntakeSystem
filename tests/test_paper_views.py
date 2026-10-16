"""Paper views (2026-07-14): the "physical office" series made real over
existing data — /calendar (bills-due wall calendar), /board (corkboard of
flagged/important correspondence, with take-down), and /tray (review-queue
depth gauge as a literal paper tray). All three are read-only presentations
over the same queries the dashboard and Catch Up already use, except the
board's take-down (which acknowledges, never files or deletes).
"""
from datetime import date, timedelta

import database
import app as klr


TODAY = date.today()
THIS_YM = TODAY.strftime("%Y-%m")


def _day(n: int) -> str:
    """ISO date for day n of the current month (clamped to 28 so short
    months like February never overflow)."""
    return TODAY.replace(day=min(max(n, 1), 28)).isoformat()


def _day_in_month(ym: str, n: int) -> str:
    first = date.fromisoformat(ym + "-01")
    return first.replace(day=min(max(n, 1), 28)).isoformat()


def _next_month_ym() -> str:
    first_next = (TODAY.replace(day=1) + timedelta(days=32)).replace(day=1)
    return first_next.strftime("%Y-%m")


def _next_month_day(n: int) -> str:
    return _day_in_month(_next_month_ym(), n)


def _past_due_date_and_ym():
    """A due_date guaranteed to be strictly before today, plus the YYYY-MM
    it falls in. Normally yesterday in the current month; wraps to the last
    day of the previous month only on the 1st of the month."""
    if TODAY.day > 1:
        d = TODAY.replace(day=TODAY.day - 1)
    else:
        d = TODAY - timedelta(days=1)
    return d.isoformat(), d.strftime("%Y-%m")


def _bill(**overrides):
    e = {"date_received": TODAY.isoformat(), "doc_type": "Bill", "vendor": "ZODIAC WATER CO",
         "amount": 88.50, "due_date": _day(20), "status": "Pending",
         "processed_by": "TestUser"}
    e.update(overrides)
    return database.insert_entry(e)


# ── Auth gate ───────────────────────────────────────────────────────────────

def test_anonymous_paper_views_redirect_to_login(client):
    for path in ("/calendar", "/board", "/tray"):
        r = client.get(path)
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]


# ── /calendar ────────────────────────────────────────────────────────────────

def test_calendar_shows_this_months_unpaid_bill(env, staff_client):
    c, headers = staff_client
    _bill(vendor="ZODIAC WATER CO", due_date=_day(20))
    r = c.get("/calendar")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "ZODIAC WATER CO" in html
    assert TODAY.strftime("%B") in html
    assert str(TODAY.year) in html


def test_calendar_past_due_and_paid_treatment(env, staff_client):
    c, headers = staff_client
    past_due, ym = _past_due_date_and_ym()
    _bill(vendor="OVERDUE PIPE CO", due_date=past_due, status="Pending")
    _bill(vendor="SETTLED FENCE CO", due_date=_day_in_month(ym, 5), status="Paid",
          paid_date=TODAY.isoformat(), check_number="778523")
    r = c.get(f"/calendar?month={ym}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "OVERDUE PIPE CO" in html
    assert "past due" in html
    assert "778523" in html


def test_calendar_month_nav_excludes_other_months_bill(env, staff_client):
    c, headers = staff_client
    _bill(vendor="ZODIAC WATER CO", due_date=_day(20))
    next_ym = _next_month_ym()
    r = c.get(f"/calendar?month={next_ym}")
    assert r.status_code == 200
    assert "ZODIAC WATER CO" not in r.get_data(as_text=True)


def test_calendar_shows_expected_bill_not_yet_arrived(env, staff_client):
    c, headers = staff_client
    database.add_expected_bill(vendor="GHOST UTILITIES INC", cadence="monthly")
    r = c.get("/calendar")
    assert r.status_code == 200
    assert "GHOST UTILITIES INC" in r.get_data(as_text=True)


# ── /board ───────────────────────────────────────────────────────────────────

def test_board_shows_manual_and_ai_flag_lines(env, staff_client):
    c, headers = staff_client
    manual_id = _bill(vendor="MANUAL FLAG VENDOR")
    c.post(f"/entry/{manual_id}/important", headers=headers)
    _bill(vendor="AI FLAG VENDOR", important=1, important_reason="deadline Aug 4")
    r = c.get("/board")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "MANUAL FLAG VENDOR" in html
    assert "flagged by staff" in html
    assert "AI FLAG VENDOR" in html
    assert "AI flagged" in html


def test_board_takedown_removes_from_board_and_important_list(env, staff_client):
    c, headers = staff_client
    bill_id = _bill(vendor="TAKEDOWN VENDOR", important=1)
    r = c.post(f"/board/{bill_id}/takedown", headers=headers, follow_redirects=False)
    assert r.status_code == 302
    entry = database.get_entry(bill_id)
    assert entry["acknowledged_at"]
    html = c.get("/board").get_data(as_text=True)
    assert "TAKEDOWN VENDOR" not in html
    ids = [row["id"] for row in database.get_important_correspondence()]
    assert bill_id not in ids


def test_board_taken_down_count_shown(env, staff_client):
    c, headers = staff_client
    _bill(vendor="STILL PINNED VENDOR", important=1)
    gone_id = _bill(vendor="REMOVED VENDOR", important=1)
    c.post(f"/board/{gone_id}/takedown", headers=headers)
    assert database.count_taken_down_since("2000-01-01 00:00:00") == 1
    html = c.get("/board").get_data(as_text=True)
    assert "1 taken down" in html
    assert "STILL PINNED VENDOR" in html


def test_board_sweep_takes_down_multiple(env, staff_client):
    c, headers = staff_client
    a = _bill(vendor="SWEEP VENDOR A", important=1)
    b = _bill(vendor="SWEEP VENDOR B", important=1)
    r = c.post("/board/sweep", data={"ids": [str(a), str(b)]}, headers=headers,
               follow_redirects=False)
    assert r.status_code == 302
    assert database.get_entry(a)["acknowledged_at"]
    assert database.get_entry(b)["acknowledged_at"]


def test_reflagging_after_takedown_clears_acknowledged_and_repins(env, staff_client):
    c, headers = staff_client
    bill_id = _bill(vendor="REPIN VENDOR", important=1)
    c.post(f"/board/{bill_id}/takedown", headers=headers)
    assert database.get_entry(bill_id)["acknowledged_at"]
    assert "REPIN VENDOR" not in c.get("/board").get_data(as_text=True)
    # unflag, then flag again -> fresh pin, acknowledged_at cleared
    c.post(f"/entry/{bill_id}/important", headers=headers)
    c.post(f"/entry/{bill_id}/important", headers=headers)
    entry = database.get_entry(bill_id)
    assert entry["important"] == 1
    assert entry["flagged_at"]
    assert not entry["acknowledged_at"]
    assert "REPIN VENDOR" in c.get("/board").get_data(as_text=True)


def test_board_includes_legal_doc_type_same_as_dashboard(env, staff_client):
    c, headers = staff_client
    database.insert_entry({"date_received": TODAY.isoformat(), "doc_type": "Legal",
                           "vendor": "SMITH ASSOCIATES LEGAL", "status": "Pending"})
    r = c.get("/board")
    assert r.status_code == 200
    assert "SMITH ASSOCIATES LEGAL" in r.get_data(as_text=True)


# ── /tray ────────────────────────────────────────────────────────────────────

def test_tray_empty_state_and_records_history(env, staff_client):
    c, headers = staff_client
    r = c.get("/tray")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "the tray is empty" in html
    assert "Nothing waiting for review." in html
    history = database.get_queue_depth_history(3)
    todays = [h for h in history if h["day"] == TODAY.isoformat()]
    assert todays and todays[0]["depth"] == 0


# ── Supporting database helpers ─────────────────────────────────────────────

def test_record_queue_depth_upsert_same_day(env):
    day = TODAY.isoformat()
    database.record_queue_depth(day, 5, 2)
    database.record_queue_depth(day, 9, 3)
    rows = [h for h in database.get_queue_depth_history(5) if h["day"] == day]
    assert len(rows) == 1
    assert rows[0]["depth"] == 9
    assert rows[0]["urgent"] == 3


def test_get_bills_for_month_scopes_to_month(env):
    _bill(vendor="MONTH ONE VENDOR", due_date=_day(10))
    _bill(vendor="MONTH TWO VENDOR", due_date=_next_month_day(10))
    vendors = [b["vendor"] for b in database.get_bills_for_month(THIS_YM)]
    assert "MONTH ONE VENDOR" in vendors
    assert "MONTH TWO VENDOR" not in vendors
