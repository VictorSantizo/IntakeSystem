"""Report #16 (Nancy): "could we have the top universal search also search the
review queue" — with Joshua's note from 8/4, "not working yet".

Report #95 is why it matters. Nancy entered eight Advanced Community Services
invoices and three did not show up. They were not lost: they were HELD in the
review queue, and a held document has no logged record, so no search over the
Log could ever have found them. Worse, the queue row carried the AI's reading
of the vendor ("Midvalley Station") instead of the name on the file she was
typing — so the file NAME has to be searched too, not only the fields the AI
extracted.
"""
import os

import queue_state


def _queue(**fields):
    data = {"pdf_path": r"C:\scans\a.pdf", "doc_type": "Bill",
            "vendor": "ACME UTILITIES", "amount": 147.48,
            "property_codes": ["T27"], "mail_type": "Email"}
    data.update(fields)
    return queue_state.queue_add(data)


# ── the matcher ───────────────────────────────────────────────────────────────

def test_a_held_document_is_found_by_the_name_on_the_file(env):
    """#95 exactly: the AI read one vendor, the filename says another, and the
    person searches for what she typed."""
    sid = _queue(vendor="Midvalley Station",
                 pdf_path=r"Z:\scans\08-06-26 ADVANCED COMMUNITY SERVICES PP52.pdf")
    _queue(vendor="Someone Else", pdf_path=r"Z:\scans\other.pdf")
    hits = queue_state.search_queue("advanced community")
    assert [s for s, _ in hits] == [sid]


def test_every_word_must_match(env):
    _queue(vendor="ACME UTILITIES", invoice_number="INV-9",
           pdf_path=r"Z:\scans\acme.pdf")
    assert queue_state.search_queue("acme inv-9")
    assert not queue_state.search_queue("acme nothinglikethis")


def test_found_by_invoice_amount_batch_or_scan_id(env):
    sid = _queue(invoice_number="99685879", amount=1204.5,
                 batch_number="2026-0806-01")
    assert [s for s, _ in queue_state.search_queue("99685879")] == [sid]
    assert [s for s, _ in queue_state.search_queue("1204.5")] == [sid]
    assert [s for s, _ in queue_state.search_queue("2026-0806-01")] == [sid]
    assert [s for s, _ in queue_state.search_queue(str(sid))] == [sid]


def test_an_empty_search_matches_nothing(env):
    _queue()
    assert queue_state.search_queue("") == []
    assert queue_state.search_queue("   ") == []


def test_the_limit_is_respected(env):
    for i in range(6):
        _queue(pdf_path=os.path.join("Z:", "scans", f"acme {i}.pdf"))
    assert len(queue_state.search_queue("acme", limit=3)) == 3


# ── the universal search box reaches it ───────────────────────────────────────

def test_universal_search_shows_waiting_documents_as_their_own_section(env, staff_client):
    c, _h = staff_client
    sid = _queue(vendor="Midvalley Station",
                 pdf_path=r"Z:\scans\08-06-26 ADVANCED COMMUNITY SERVICES PP52.pdf")
    html = c.get("/bills", query_string={"q": "advanced community"}).get_data(as_text=True)
    assert "Still waiting in Needs Review" in html
    assert f"/review/{sid}" in html
    # ...and it is not passed off as a logged record.
    assert "have not been logged yet" in html


def test_a_search_with_no_matches_anywhere_says_so_plainly(env, staff_client):
    c, _h = staff_client
    _queue(vendor="ACME UTILITIES")
    html = c.get("/bills", query_string={"q": "nothing at all"}).get_data(as_text=True)
    assert "No records match" in html
    assert "Still waiting in Needs Review" not in html


def test_browsing_bills_without_a_search_costs_no_queue_scan(env, staff_client):
    c, _h = staff_client
    _queue()
    html = c.get("/bills").get_data(as_text=True)
    assert "Still waiting in Needs Review" not in html
