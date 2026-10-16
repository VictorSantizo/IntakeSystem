"""Mark-as-Paid receipt upload — /entry/<id>/pay now accepts a multipart
'receipt' file alongside the payment fields, filing it next to the entry's
(possibly re-filed) document and recording it as a 'Receipt' attachment via
the same _store_receipt_upload() helper attach_receipt already used. Covers:
happy path with a receipt, the two "no receipt" regressions (missing part /
empty-filename part must NOT create an attachment but must still mark Paid),
and the template wiring in entry_view.html / entry_form.html."""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402


def _make_bill(filed_path=None, **over):
    data = dict(date_received="2026-07-01", mail_type="Physical Mail",
                entity_code="TRUST1", doc_type="Bill", vendor="Chase Card Services",
                bill_type_code="ELEC", amount=245.10, status="Pending")
    if filed_path is not None:
        data["filed_path"] = filed_path
    data.update(over)
    return database.insert_entry(data)


def _filed_file(env, entity="TRUST1",
                 name="07-01-26 TRUST1 ELEC - Chase Card Services - $245.10.pdf"):
    """A real file on disk under the temp filed_folder, so _refile_to_convention
    finds something to (maybe) rename and _store_receipt_upload has a real
    sibling directory to file into."""
    d = os.path.join(env.filed_folder, entity)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(b"%PDF-1.4 fake filed doc")
    return p


# ---------------------------------------------------------------- (a) happy path

def test_pay_with_receipt_creates_attachment_next_to_filed_doc(staff_client, env):
    c, headers = staff_client
    filed = _filed_file(env)
    eid = _make_bill(filed_path=filed)

    data = {
        "paid_date": "2026-07-10",
        "paid_amount": "245.10",
        "payment_method": "Check",
        "check_number": "13386",
        "payment_note": "",
        "receipt": (io.BytesIO(b"%PDF-1.4 fake"), "chase receipt.pdf"),
    }
    r = c.post(f"/entry/{eid}/pay", headers=headers, data=data,
               content_type="multipart/form-data")
    assert r.status_code == 302

    e = database.get_entry(eid)
    assert e["status"] == "Paid"

    atts = database.get_attachments(eid)
    assert len(atts) == 1
    assert atts[0]["source"] == "Receipt"
    assert atts[0]["original_name"] == "chase receipt.pdf"

    # _refile_to_convention may have renamed the primary doc (it picked up the
    # CK#13386 token) — assert against the CURRENT filed_path, not the pre-POST one.
    current_dir = os.path.dirname(e["filed_path"])
    receipt_path = atts[0]["file_path"]
    assert os.path.exists(receipt_path)
    assert os.path.normcase(os.path.dirname(receipt_path)) == os.path.normcase(current_dir)
    assert f"(RECEIPT {eid})" in os.path.basename(receipt_path)
    assert receipt_path.lower().endswith(".pdf")


def test_pay_with_receipt_stamps_activity_log(staff_client, env):
    c, headers = staff_client
    filed = _filed_file(env)
    eid = _make_bill(filed_path=filed)
    data = {"paid_date": "2026-07-10",
            "receipt": (io.BytesIO(b"%PDF-1.4 fake"), "chase receipt.pdf")}
    c.post(f"/entry/{eid}/pay", headers=headers, data=data,
           content_type="multipart/form-data")
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT action FROM activity_log WHERE entry_id=? ORDER BY id", (eid,)).fetchall()
    actions = [r["action"] for r in rows]
    assert "Marked paid" in actions
    assert "Receipt attached" in actions


# ---------------------------------------------------------------- (b) no receipt part

def test_pay_without_receipt_part_still_pays_no_attachment(staff_client, env):
    c, headers = staff_client
    eid = _make_bill()
    r = c.post(f"/entry/{eid}/pay", headers=headers, data={
        "paid_date": "2026-07-10", "paid_amount": "245.10",
    })
    assert r.status_code == 302
    e = database.get_entry(eid)
    assert e["status"] == "Paid"
    assert database.get_attachments(eid) == []


# ---------------------------------------------------------------- (c) empty-filename part

def test_pay_with_empty_filename_receipt_no_attachment(staff_client, env):
    c, headers = staff_client
    eid = _make_bill()
    data = {
        "paid_date": "2026-07-10",
        "paid_amount": "245.10",
        "receipt": (io.BytesIO(b""), ""),
    }
    r = c.post(f"/entry/{eid}/pay", headers=headers, data=data,
               content_type="multipart/form-data")
    assert r.status_code == 302
    e = database.get_entry(eid)
    assert e["status"] == "Paid"
    assert database.get_attachments(eid) == []


# ---------------------------------------------------------------- (d) template wiring

def test_view_page_payform_has_multipart_receipt_field(staff_client):
    c, headers = staff_client
    eid = _make_bill()   # Pending -> payForm renders
    r = c.get(f"/entry/{eid}/view", headers=headers)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    m = re.search(r'<form id="payForm".*?</form>', html, re.DOTALL)
    assert m, "payForm not found on the view page for an unpaid entry"
    form_html = m.group(0)
    # enctype lives on the opening <form> tag itself.
    open_tag = form_html.split(">", 1)[0]
    assert 'enctype="multipart/form-data"' in open_tag
    assert 'name="receipt"' in form_html


def test_edit_page_pay_modal_has_receipt_and_save_payment_label(staff_client):
    c, headers = staff_client
    eid = _make_bill()   # Pending -> pay-modal renders
    r = c.get(f"/entry/{eid}/edit", headers=headers)
    assert r.status_code == 200
    html = r.get_data(as_text=True)

    assert 'id="pay-modal"' in html
    m = re.search(r'<div id="pay-modal".*?</div>\s*</div>\s*</div>', html, re.DOTALL)
    modal_html = m.group(0) if m else html[html.find('id="pay-modal"'):]
    assert 'name="receipt"' in modal_html

    assert "Save Payment" in html
    assert "Confirm Payment" not in html

    # Mark as Paid moved to the top header row, ahead of the form-grid.
    idx_mark = html.find('data-kk="mark-paid"')
    idx_grid = html.find('class="form-grid"')
    assert idx_mark != -1 and idx_grid != -1
    assert idx_mark < idx_grid, "Mark as Paid button must appear before the form-grid"

    # Exactly one opener wired to the modal (no leftover duplicate control).
    assert html.count("pay-modal').style.display='flex'") == 1
