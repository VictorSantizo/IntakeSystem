"""Source on the Bills page — Nancy's #15 and Joshua's #77.

#15 (Nancy, filed from /bills?q=montie+wayne): "instead of the type column we
wanted the 'source' column telling us if it came from email or scan or other."
That page is the SEARCH results table; All Bills already showed Source.

#77 (Joshua, filed from /bills): "we also need to filter by 'source'."
"""
import database
from database import insert_entry


def _bill(vendor, mail_type, **kw):
    return insert_entry({"vendor": vendor, "doc_type": "Bill",
                         "mail_type": mail_type, "amount": kw.get("amount", 100.0),
                         "date_received": kw.get("date_received", "2026-08-01"),
                         "status": "Pending"})


def test_search_results_show_source_instead_of_type(env, staff_client):
    c, _ = staff_client
    _bill("Montie Wayne Plumbing", "Email")
    html = c.get("/bills?q=Montie").get_data(as_text=True)
    assert "Montie Wayne Plumbing" in html
    table_head = html.split("<thead>")[1].split("</thead>")[0]
    assert "Source" in table_head
    assert "<th>Type</th>" not in table_head
    # the row says how it arrived...
    assert "Email" in html
    # ...and the document type is not lost, just moved next to the title
    assert "Bill" in html


def test_all_bills_can_be_filtered_by_source(env, staff_client):
    c, _ = staff_client
    _bill("Emailed Vendor Inc", "Email")
    _bill("Paper Vendor Inc", "Physical Mail")
    _bill("Uploaded Vendor Inc", "Upload")

    html = c.get("/bills?view=all&mail_type=Email").get_data(as_text=True)
    assert "Emailed Vendor Inc" in html
    assert "Paper Vendor Inc" not in html
    assert "Uploaded Vendor Inc" not in html

    html = c.get("/bills?view=all&mail_type=Upload").get_data(as_text=True)
    assert "Uploaded Vendor Inc" in html
    assert "Emailed Vendor Inc" not in html


def test_source_filter_is_offered_and_remembers_the_choice(env, staff_client):
    c, _ = staff_client
    _bill("Somebody", "Email")
    html = c.get("/bills?view=all").get_data(as_text=True)
    assert 'name="mail_type"' in html
    html = c.get("/bills?view=all&mail_type=Email").get_data(as_text=True)
    assert 'value="Email" selected' in html


def test_no_source_filter_shows_everything(env, staff_client):
    c, _ = staff_client
    _bill("Emailed Vendor Inc", "Email")
    _bill("Paper Vendor Inc", "Physical Mail")
    html = c.get("/bills?view=all").get_data(as_text=True)
    assert "Emailed Vendor Inc" in html and "Paper Vendor Inc" in html
