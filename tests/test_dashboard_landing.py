"""New dashboard landing page (2026-07-07): merged ranked-urgency stack +
persistent action menu, plus the important-correspondence surface (Legal /
Correspondence documents not yet filed).
"""
import database
import app as klr


def test_important_correspondence_query_scope(env):
    legal_id = database.insert_entry({"date_received": "2026-07-05", "doc_type": "Legal",
                                      "vendor": "Smith & Associates", "status": "Pending"})
    corr_id = database.insert_entry({"date_received": "2026-07-06", "doc_type": "Correspondence",
                                     "vendor": "Tenant Notice", "status": "Processed"})
    filed_id = database.insert_entry({"date_received": "2026-07-04", "doc_type": "Legal",
                                      "vendor": "Old Matter", "status": "Filed"})
    bill_id = database.insert_entry({"date_received": "2026-07-06", "doc_type": "Bill",
                                     "vendor": "Frontier", "status": "Pending"})
    ids = [r["id"] for r in database.get_important_correspondence()]
    assert corr_id in ids and legal_id in ids
    assert filed_id not in ids   # already filed away - no longer "needs attention"
    assert bill_id not in ids    # bills belong in the money views, not here
    assert ids[0] == corr_id     # most recent (by date_received) first


def test_important_correspondence_respects_limit(env):
    for i in range(10):
        database.insert_entry({"date_received": f"2026-07-{i+1:02d}", "doc_type": "Legal",
                               "vendor": f"Matter {i}", "status": "Pending"})
    assert len(database.get_important_correspondence(limit=3)) == 3
    assert len(database.get_important_correspondence(limit=10)) == 10


def test_dashboard_shows_important_correspondence_section(env, staff_client):
    c, headers = staff_client
    database.insert_entry({"date_received": "2026-07-07", "doc_type": "Legal",
                           "vendor": "Smith & Associates", "property_code": "T27",
                           "status": "Pending"})
    html = c.get("/").get_data(as_text=True)
    assert "legal or important document" in html
    assert "Smith &amp; Associates" in html or "Smith & Associates" in html
    assert 'doc_type=Legal' in html and 'doc_type=Correspondence' in html


def test_dashboard_hides_important_section_when_empty(env, staff_client):
    c, headers = staff_client
    html = c.get("/").get_data(as_text=True)
    assert "legal or important document" not in html


def test_dashboard_all_caught_up_state(env, staff_client):
    c, headers = staff_client
    html = c.get("/").get_data(as_text=True)
    assert "You're all caught up" in html


def test_dashboard_review_row_hidden_when_queue_empty(env, staff_client):
    c, headers = staff_client
    html = c.get("/").get_data(as_text=True)
    assert 'id="review-tile"' in html
    row = html.split('id="review-tile"')[1][:60]
    assert "display:none" in row


def test_dashboard_review_row_shown_and_urgent_styled(env, staff_client, tmp_path):
    c, headers = staff_client
    f = tmp_path / "doc.pdf"; f.write_text("x")
    # doc_type matters since report #111: only a BILL can be past due, so an
    # unclassified queue row no longer turns the dashboard red.
    klr._queue_add({"pdf_path": str(f), "vendor": "Test", "doc_type": "Bill",
                    "due_date": "2020-01-01"})
    html = c.get("/").get_data(as_text=True)
    assert "You're all caught up" not in html
    assert 'class="today-row danger"' in html   # overdue queue item -> danger styling
    assert "past due" in html


def test_dashboard_action_menu_present_and_search_form_valid(env, staff_client):
    c, headers = staff_client
    html = c.get("/").get_data(as_text=True)
    assert "Review new mail" in html
    assert "Pay bills" in html
    assert "Find a record" in html
    assert "Log something new" in html
    # the search form must not be nested inside an <a> (invalid HTML)
    form_start = html.index('<form method="GET" action="/log"')
    preceding = html[:form_start]
    last_a_open = preceding.rfind("<a ")
    last_a_close = preceding.rfind("</a>")
    assert last_a_close > last_a_open, "search form appears nested inside an <a> card"


def test_find_a_record_search_actually_works(env, staff_client):
    c, headers = staff_client
    database.insert_entry({"date_received": "2026-07-07", "doc_type": "Bill",
                           "vendor": "Very Unique Vendor Name", "status": "Pending"})
    r = c.get("/log?search=Very+Unique+Vendor")
    assert r.status_code == 200
    assert b"Very Unique Vendor Name" in r.data


def test_dashboard_no_status_kpi_row(env, staff_client):
    c, headers = staff_client
    html = c.get("/").get_data(as_text=True)
    assert "Total Logged" not in html
    assert "Processed / Paid" not in html
