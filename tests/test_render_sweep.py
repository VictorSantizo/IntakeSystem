"""Full-page render sweep: every GET route renders without a 5xx for both a
staff and an admin session, with representative data seeded (a logged entry,
a queued scan carrying the AI-outage marker, a filtered doc, an expected bill,
an auto-filed entry). 4xx/redirects are fine — the guard is against template
or route crashes reaching production.
"""
import os

import app as klr
import database
from conftest import make_pdf

# String route args that need believable values.
STRING_ARGS = {"group": "vendor", "vendor": "ACME Utilities", "code": "TRUST1"}


def _seed(env):
    eid = database.insert_entry({
        "date_received": "2026-07-01", "doc_type": "Bill", "vendor": "ACME Utilities",
        "amount": 147.48, "due_date": "2026-07-15", "entity_code": "TRUST1",
        "property_code": "T27", "bill_type_code": "ELEC", "status": "Pending",
        "processed_by": "TestUser", "invoice_number": "INV-1", "mail_type": "Email",
    })
    database.insert_entry({
        "date_received": "2026-07-02", "doc_type": "Bill", "vendor": "Robo Vendor",
        "amount": 10.0, "status": "Pending", "processed_by": "Auto",
    })
    pdf = os.path.join(env.scan_folder, "queued.pdf")
    make_pdf(pdf)
    sid = klr._queue_add({
        "pdf_path": pdf, "mail_type": "Email", "doc_type": "Income",
        "ai_confidence": "None", "vendor": None,
        "notes": "[AI unavailable: 'message': 'credit balance too low'] review",
    })
    database.add_filtered_doc(pdf, "queued.pdf", "Other", None, None, "None", "test", "{}")
    database.add_expected_bill("ACME Utilities", "12-3456", "TRUST1", "T27")
    return {"entry_id": eid, "scan_id": sid, "att_id": 1, "eb_id": 1,
            "report_id": 1, "fid": 1, "page_num": 1}


def _sweep(client, int_args):
    failures = []
    for rule in klr.app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.rule.startswith("/static"):
            continue
        kwargs = {}
        skip = False
        for arg in rule.arguments:
            if arg in int_args:
                kwargs[arg] = int_args[arg]
            elif arg in STRING_ARGS:
                kwargs[arg] = STRING_ARGS[arg]
            else:
                skip = True   # unknown converter — not worth a guess
        if skip:
            continue
        from flask import url_for
        with klr.app.test_request_context():
            url = url_for(rule.endpoint, **kwargs)
        r = client.get(url)
        if r.status_code >= 500:
            failures.append(f"{rule.endpoint} {url} -> {r.status_code}")
    return failures


def test_all_pages_render_for_staff(env, staff_client):
    ids = _seed(env)
    c, _ = staff_client
    assert _sweep(c, ids) == []


def test_all_pages_render_for_admin(env, admin_client):
    ids = _seed(env)
    c, _ = admin_client
    assert _sweep(c, ids) == []
