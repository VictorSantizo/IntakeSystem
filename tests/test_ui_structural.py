"""Structural simplification pass (2026-07-07): nav trim, dashboard declutter,
review-form disclosure, filter collapse, bills merge, log column trim.

Prompted by activity_log showing near-zero real usage outside of dev/testing
(Karen: 1 action ever, Nancy: 6 actions on one day, DeeAnna: 0) — proceeding
on these without a Karen walkthrough, per that evidence of first-look bounce.
"""
import database
import app as klr


def test_nav_has_no_manual_entry_or_theme_picker(env, staff_client):
    c, headers = staff_client
    html = c.get("/").get_data(as_text=True)
    assert "Manual Entry" not in html
    assert 'id="theme-picker"' not in html
    # still reachable elsewhere
    assert '/entry/new' in c.get("/log").get_data(as_text=True)


def test_account_page_has_theme_picker(env, staff_client):
    c, headers = staff_client
    html = c.get("/account").get_data(as_text=True)
    assert 'id="theme-picker"' in html


def test_dashboard_has_no_owed_shortcut_cards(env, staff_client):
    c, headers = staff_client
    html = c.get("/").get_data(as_text=True)
    assert "Owed by Vendor" not in html
    assert "Owed by Entity" not in html
    assert "Owed by Property" not in html
    assert "Total Bills" not in html   # KPI folded (duplicated the type table)
    assert "Total Income" not in html


def test_review_form_more_details_collapsed_normally(env, staff_client, tmp_path):
    c, headers = staff_client
    f = tmp_path / "doc.pdf"; f.write_text("x")
    sid = klr._queue_add({"pdf_path": str(f), "vendor": "Test"})
    html = c.get(f"/review/{sid}").get_data(as_text=True)
    assert 'id="more-details"' in html
    assert "display:none" in html.split('id="more-details"')[1][:60]


def test_review_form_assumed_date_warns_in_main_grid(env, staff_client, tmp_path):
    """An assumed received date used to force the More-details box open so its
    warning could be seen. Date Received now lives in the always-visible main
    grid (reports #56/#49/#43) — the warning chip must be there, with no
    force-open needed."""
    c, headers = staff_client
    f = tmp_path / "doc.pdf"; f.write_text("x")
    sid = klr._queue_add({"pdf_path": str(f), "vendor": "Test",
                          "mail_type": "Physical Mail",
                          "received_date_assumed": True})
    html = c.get(f"/review/{sid}").get_data(as_text=True)
    assert "check the stamp on the back" in html
    assert html.index('name="date_received"') < html.index('id="more-details"')


def test_review_queue_filters_collapsed_when_small(env, staff_client, tmp_path):
    c, headers = staff_client
    f = tmp_path / "doc.pdf"; f.write_text("x")
    klr._queue_add({"pdf_path": str(f), "vendor": "Test"})
    html = c.get("/review").get_data(as_text=True)
    assert 'id="filters-box"' in html
    assert "display:none" in html.split('id="filters-box"')[1][:80]


def test_review_queue_filters_open_when_queue_large(env, staff_client, tmp_path):
    c, headers = staff_client
    for i in range(16):
        f = tmp_path / f"doc{i}.pdf"; f.write_text("x")
        klr._queue_add({"pdf_path": str(f), "vendor": f"Vendor{i}"})
    html = c.get("/review").get_data(as_text=True)
    box = html.split('id="filters-box"')[1][:80]
    assert "display:none" not in box


def test_review_queue_filters_open_when_filter_in_url(env, staff_client, tmp_path):
    c, headers = staff_client
    f = tmp_path / "doc.pdf"; f.write_text("x")
    klr._queue_add({"pdf_path": str(f), "vendor": "Test", "doc_type": "Bill"})
    html = c.get("/review?doctype=Bill").get_data(as_text=True)
    box = html.split('id="filters-box"')[1][:80]
    assert "display:none" not in box


def test_log_table_dropped_columns(env, staff_client):
    c, headers = staff_client
    database.insert_entry({"date_received": "2026-07-07", "entity_code": "TRUST1",
                           "property_code": "T27", "doc_type": "Bill",
                           "bill_type_code": "ELEC", "vendor": "Frontier",
                           "mail_type": "Email", "processed_by": "TestUser",
                           "notes": "some note text", "status": "Pending"})
    html = c.get("/log").get_data(as_text=True)
    assert "<th>Bill Code</th>" in html          # kept
    assert "<th>Type</th>" not in html           # mail type column dropped
    assert "<th>By</th>" not in html
    assert "<th>Notes</th>" not in html
    assert 'colspan="11"' in html or "Frontier" in html   # header count matches row count


def test_karen_questions_nav_only_for_karen(env):
    """2026-07-29 (Joshua): the Karen's Questions link is Karen's alone; other
    staff reach /karen by URL if ever needed, but their bar doesn't show it."""
    import time
    c = klr.app.test_client()
    with c.session_transaction() as s:
        s["user"] = "Karen"; s["last_active"] = time.time(); s["csrf"] = "t"
    assert 'data-kk="nav-karen"' in c.get("/log").get_data(as_text=True)

    c2 = klr.app.test_client()
    with c2.session_transaction() as s:
        s["user"] = "TestUser"; s["last_active"] = time.time(); s["csrf"] = "t"
    html = c2.get("/log").get_data(as_text=True)
    assert 'data-kk="nav-karen"' not in html
    # /karen itself stays reachable for everyone
    assert c2.get("/karen").status_code == 200


def test_expected_left_the_nav_for_bills_page(env, staff_client):
    """2026-07-29 (Joshua): Expected is a Bills-page button + a dashboard tile
    when something's late, not a top-bar destination."""
    c, headers = staff_client
    assert 'data-kk="nav-expected"' not in c.get("/log").get_data(as_text=True)
    assert 'href="/expected"' in c.get("/bills").get_data(as_text=True)
