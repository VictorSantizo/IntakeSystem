"""The review queue separates bills from housekeeping (Joshua 8/6, from the
8/3 training session): payable work first — past-due, then flagged, then
OLDEST RECEIVED first, the order Karen pays in — with statements, notices and
other tidy-up collapsed below it, clearly labeled as carrying no payments.
"""
import re

import app as klr

from test_dedup_triage import _queue_bill


def _queue_doc(env, name, doc_type, **fields):
    return _queue_bill(env, name, f"{doc_type} text", doc_type=doc_type,
                       **fields)


def test_bills_section_comes_before_housekeeping(env, staff_client):
    c, _h = staff_client
    _queue_doc(env, "b1.pdf", "Bill", vendor="Water Co")
    _queue_doc(env, "s1.pdf", "Statement", vendor="Plumber Statement")
    html = c.get("/review").get_data(as_text=True)
    assert html.index("Bills &mdash;") < html.index("Housekeeping &mdash;")
    assert "1 bill" in html and "1 housekeeping" in html
    # housekeeping sits inside a collapsed <details> when bills exist
    m = re.search(r'<details id="hk-details"[^>]*>', html)
    assert m and "open" not in m.group(0)


def test_bills_ordered_by_received_date(env, staff_client):
    c, _h = staff_client
    newer = _queue_doc(env, "new.pdf", "Bill", vendor="Newer",
                       received_date="2026-08-01")
    older = _queue_doc(env, "old.pdf", "Bill", vendor="Older",
                       received_date="2026-07-01")
    html = c.get("/review").get_data(as_text=True)
    assert html.index("old.pdf") < html.index("new.pdf")   # oldest received first


def test_no_bills_opens_housekeeping(env, staff_client):
    c, _h = staff_client
    _queue_doc(env, "s1.pdf", "Statement", vendor="Only Statement")
    html = c.get("/review").get_data(as_text=True)
    assert "No bills waiting" in html
    m = re.search(r'<details id="hk-details"[^>]*>', html)
    assert m and "open" in m.group(0)                      # nothing hidden


def test_every_item_renders_exactly_once(env, staff_client):
    c, _h = staff_client
    _queue_doc(env, "b1.pdf", "Bill", vendor="Water Co")
    _queue_doc(env, "s1.pdf", "Statement", vendor="Some Statement")
    _queue_doc(env, "c1.pdf", "Correspondence", vendor="A Letter")
    html = c.get("/review").get_data(as_text=True)
    assert html.count('class="card review-card"') == 3     # one card each
    for name in ("b1.pdf", "s1.pdf", "c1.pdf"):
        assert name in html
