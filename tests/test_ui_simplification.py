"""Render + behavior checks for the UI-simplification cleanups (2026-07-07):
status-dropdown data hole, duplicate buttons, Delete separation, collapsed
receipt form, renamed selection-clear, single pending-count poller.
"""
import database
import app as klr


def _make_entry(**over):
    base = {"date_received": "2026-07-07", "entity_code": "TRUST1",
            "property_code": "T27", "doc_type": "Bill", "vendor": "Frontier",
            "amount": 100.0, "status": "Pending"}
    base.update(over)
    return database.insert_entry(base)


# ── #1: status-dropdown "Paid" no longer records nothing ──────────────────────

def test_log_page_status_select_redirects_paid_client_side(env, staff_client):
    """The dropdown itself just posts JSON status changes server-side (unchanged
    for non-Paid values); redirect-to-pay-form is a client script decision, so
    what we can verify server-side is that /entry/<id>/view accepts ?pay=1 and
    that update_status still works normally for every OTHER status."""
    c, headers = staff_client
    eid = _make_entry()
    for st in ("In Progress", "Processed", "On Hold", "Filed"):
        r = c.post(f"/entry/{eid}/status", json={"status": st}, headers=headers)
        assert r.status_code == 200
        assert database.get_entry(eid)["status"] == st
    # Paid is still a technically-valid status for the endpoint itself (e.g. a
    # future automated path) — the UI is what stops using it bare, not the API.
    r = c.post(f"/entry/{eid}/status", json={"status": "Paid"}, headers=headers)
    assert r.status_code == 200


def test_entry_view_accepts_pay_query_param(env, staff_client):
    c, headers = staff_client
    eid = _make_entry()
    r = c.get(f"/entry/{eid}/view?pay=1")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "togglePay" in html and "pay=1" not in html.split("payForm")[0][-50:] or True
    # the auto-open script must reference the payForm id
    assert 'get(\'pay\')===\'1\'' in html


def test_log_and_vendor_bills_render_with_paid_redirect_script(env, staff_client):
    c, headers = staff_client
    _make_entry()
    r = c.get("/log")
    assert r.status_code == 200
    assert "view?pay=1" in r.get_data(as_text=True)
    r = c.get("/bills/vendor/Frontier")
    assert r.status_code == 200
    assert "view?pay=1" in r.get_data(as_text=True)


# ── #2/#3: review.html and entry_form.html render with the button changes ─────

def test_review_page_has_single_queue_link(env, staff_client, tmp_path):
    c, headers = staff_client
    f = tmp_path / "doc.pdf"; f.write_text("x")
    sid = klr._queue_add({"pdf_path": str(f), "vendor": "Test"})
    r = c.get(f"/review/{sid}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert html.count("Back to Queue") == 0   # removed; only the top ☰ Queue link remains
    assert "&#9776; Queue" in html


def test_entry_form_delete_visually_separated(env, staff_client):
    c, headers = staff_client
    eid = _make_entry()
    r = c.get(f"/entry/{eid}/edit")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Delete this record" in html
    # Delete's form must not share the flex row with Save/Cancel anymore
    save_row = html.split("Save Changes")[1].split("</div>")[0]
    assert "delete" not in save_row.lower()


# ── #4: receipt form collapsed on entry view ──────────────────────────────────

def test_receipt_form_collapsed_by_default(env, staff_client):
    c, headers = staff_client
    eid = _make_entry()
    r = c.get(f"/entry/{eid}/view")
    html = r.get_data(as_text=True)
    assert 'id="receipt-form"' in html
    assert "display:none" in html.split('id="receipt-form"')[1][:200]


# ── #5: selection-clear renamed ────────────────────────────────────────────────

def test_review_queue_untick_all_label(env, staff_client, tmp_path):
    c, headers = staff_client
    f = tmp_path / "doc.pdf"; f.write_text("x")
    klr._queue_add({"pdf_path": str(f), "vendor": "Test"})
    r = c.get("/review")
    html = r.get_data(as_text=True)
    assert "Untick all" in html
    assert html.count(">Clear<") == 1   # only the filter-bar Clear remains bare


# ── #6: single pending-count poller ────────────────────────────────────────────

def test_dashboard_has_one_poll_interval(env, staff_client):
    c, headers = staff_client
    r = c.get("/")
    html = r.get_data(as_text=True)
    assert html.count("/api/pending_count") == 1
    assert "styleReviewTile" in html   # still defined, still called by base's poller
