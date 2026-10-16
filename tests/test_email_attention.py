"""Dashboard "From email — worth your eyes" box: Karen's worry is things
arriving by EMAIL slipping past unseen. The box shows email-only attention
items (flagged important / bundled notices / Legal & Correspondence) from both
the review queue and the log, plus the week's junk-filter count. Routine email
bills stay out."""

import app as klr
import database

BOX = "From email"


def _queue_item(sid, **over):
    d = {"mail_type": "Email", "doc_type": "Bill", "vendor": "City Water",
         "received_date": "2026-07-14", "pdf_path": "x"}
    d.update(over)
    with klr._queue_lock:
        klr.pending_scans[sid] = d


def test_flagged_email_queue_item_shows(staff_client):
    c, _ = staff_client
    _queue_item(901, doc_type="Correspondence", vendor="Wolfe & Associates",
                important=True, important_reason="Attorney letter re TL222")
    html = c.get("/").get_data(as_text=True)
    assert BOX in html
    assert "/review/901" in html
    assert "Wolfe" in html and "awaiting review" in html


def test_physical_mail_never_shows_in_email_box(staff_client):
    c, _ = staff_client
    _queue_item(902, mail_type="Physical Mail", doc_type="Legal",
                vendor="Superior Court", important=True)
    html = c.get("/").get_data(as_text=True)
    assert BOX not in html          # only signal was physical → box absent


def test_routine_email_bill_stays_out(staff_client):
    c, _ = staff_client
    _queue_item(903)                # plain email bill, no flags
    html = c.get("/").get_data(as_text=True)
    assert BOX not in html


def test_logged_email_important_doc_shows(staff_client):
    c, _ = staff_client
    eid = database.insert_entry(dict(
        date_received="2026-07-13", mail_type="Email", doc_type="Legal",
        vendor="First American Title", status="Pending",
        email_subject="Escrow instructions"))
    html = c.get("/").get_data(as_text=True)
    assert BOX in html
    assert f"/entry/{eid}/view" in html


def test_helper_filters_by_mail_type(env):
    database.insert_entry(dict(date_received="2026-07-13", mail_type="Email",
                               doc_type="Legal", vendor="EmailOnly", status="Pending"))
    database.insert_entry(dict(date_received="2026-07-13", mail_type="Physical Mail",
                               doc_type="Legal", vendor="PaperOnly", status="Pending"))
    got = database.get_important_correspondence(limit=10, mail_type="Email")
    names = {r["vendor"] for r in got}
    assert "EmailOnly" in names and "PaperOnly" not in names
    # and the unfiltered call still returns both (backwards compatible)
    both = {r["vendor"] for r in database.get_important_correspondence(limit=10)}
    assert {"EmailOnly", "PaperOnly"} <= both


def test_junk_footer_counts_week(staff_client):
    c, _ = staff_client
    _queue_item(904, doc_type="Legal", vendor="Court")   # make the box render
    with database.get_conn() as conn:
        conn.execute("INSERT INTO filtered_docs (file_name, doc_type, reason)"
                     " VALUES ('spam.pdf','Marketing','promo')")
        conn.commit()
    html = c.get("/").get_data(as_text=True)
    assert "auto-filtered this week" in html
    assert "1 junk email doc" in html
