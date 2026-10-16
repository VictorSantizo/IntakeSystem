"""Twin-bill detection + merge — the FAIR Plan #51-vs-#77 problem (7/17):
one real-world bill logged as several records whose bytes differ (insurer's
copy vs the agent's copy), so hash dedup never pairs them; Karen paid one and
the twins kept the debt alive on the owed list for 19 days. Covers
database.find_invoice_twins, the record-view banners, the pay-flow detour to
the warning, and the server-console `merge-record` CLI."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402
import app as klr  # noqa: E402


def _bill(**over):
    data = dict(date_received="2026-06-27", mail_type="Email", doc_type="Bill",
                entity_code="TRUST1", vendor="California FAIR Plan Association",
                amount=1026.0, due_date="2026-06-29", status="Pending",
                invoice_number="CFP 0102713675")
    data.update(over)
    return database.insert_entry(data)


# ---------------------------------------------------------------- find_invoice_twins

def test_same_invoice_pairs_across_vendor_spellings(env):
    """The exact 7/17 shape: agency copy and insurer copy share the invoice #
    but not the vendor string, amount-due-date row, or file bytes."""
    a = _bill(vendor="California FAIR Plan Association / Christopher Baxter "
                     "Ins. Agy. Inc.", due_date="2026-06-29")
    b = _bill(vendor="California FAIR Plan Association", due_date="2026-07-04")
    twins = database.find_invoice_twins(database.get_entry(a))
    assert [t["id"] for t in twins] == [b]


def test_invoice_suffix_tolerance(env):
    """'CFP 0102713675' vs 'CFP 0102713675 00' — one invoice, two prints."""
    a = _bill()
    b = _bill(invoice_number="CFP 0102713675 00", amount=None, due_date=None)
    assert [t["id"] for t in database.find_invoice_twins(database.get_entry(a))] == [b]


def test_short_or_placeholder_invoices_never_pair(env):
    a = _bill(invoice_number="123", vendor="Wolfe & Associates", amount=100.0)
    _bill(invoice_number="123", vendor="Ronald L. Wolfe & Associates, Inc.",
          amount=999.0, date_received="2026-05-01", due_date=None)
    _bill(invoice_number="TBD", vendor="Wolfe & Associates", amount=888.0,
          date_received="2026-05-02", due_date=None)
    assert database.find_invoice_twins(database.get_entry(a)) == []


def test_amount_entity_vendor_word_needs_shared_date(env):
    """Same payee + same amount + same entity is only a twin when the received
    or due date also agrees — a monthly recurring bill's next cycle must NOT
    read as a duplicate."""
    a = _bill(invoice_number=None)
    same_day = _bill(invoice_number=None, vendor="FAIR Plan / Baxter Agy.",
                     due_date="2026-07-04")               # same received date
    _bill(invoice_number=None, date_received="2026-07-27",
          due_date="2026-07-29")                          # next month's cycle
    twins = database.find_invoice_twins(database.get_entry(a))
    assert [t["id"] for t in twins] == [same_day]


def test_short_vendor_abbreviation_still_matches(env):
    """2026-07-25 code review: _vendor_words used to drop every <=3-char token,
    disabling the word-overlap fallback for exactly the short abbreviations
    (SCE, DWP) real bills get logged under -- 'Southern California Edison'
    sometimes as just 'SCE'. Lowering the length floor to 3 must let this pair,
    without reintroducing generic short words (still excluded by name via the
    stopword set, covered by the sibling assertion below)."""
    a = _bill(invoice_number=None, vendor="Southern California Edison (SCE)")
    b = _bill(invoice_number=None, vendor="SCE", due_date="2026-06-29")
    twins = database.find_invoice_twins(database.get_entry(a))
    assert [t["id"] for t in twins] == [b]


def test_generic_short_stopwords_still_do_not_cause_false_matches(env):
    """Lowering the length floor to 3 must not reintroduce THE/INC/CO/AND/FOR --
    those stay excluded by the explicit stopword set (_TWIN_STOP_WORDS), by
    name, independent of length. These two vendors share only the word 'and'
    (and otherwise have nothing in common) -- they must NOT pair."""
    a = _bill(invoice_number=None, vendor="Falcon and Hawk Roofing")
    b = _bill(invoice_number=None, vendor="Otter and Beaver Plumbing",
              due_date="2026-06-29")
    twins = database.find_invoice_twins(database.get_entry(a))
    assert twins == []


def test_find_link_candidates_shares_the_same_fallback_match(env):
    """2026-07-25 code review: find_invoice_twins and find_link_candidates used
    to duplicate this amount+entity+vendor+date-window condition verbatim;
    extracted into _amount_entity_vendor_match so both stay in sync. Same setup
    as test_amount_entity_vendor_word_needs_shared_date above, run through
    find_link_candidates instead (which isn't restricted to doc_type='Bill')."""
    a = _bill(invoice_number=None)
    same_day = _bill(invoice_number=None, vendor="FAIR Plan / Baxter Agy.",
                     due_date="2026-07-04")               # same received date
    _bill(invoice_number=None, date_received="2026-07-27",
          due_date="2026-07-29")                          # next month's cycle
    ids = [r["id"] for r in database.find_link_candidates(database.get_entry(a))]
    assert ids == [same_day]


def test_non_bills_and_self_are_excluded(env):
    a = _bill()
    database.insert_entry({"date_received": "2026-06-27", "doc_type": "Legal",
                           "vendor": "California FAIR Plan Association",
                           "invoice_number": "CFP 0102713675", "status": "Pending"})
    twins = database.find_invoice_twins(database.get_entry(a))
    assert twins == []


# ---------------------------------------------------------------- record-view banners

def test_paid_record_warns_of_open_twin(staff_client, env):
    c, _ = staff_client
    paid = _bill(status="Paid", paid_date="2026-06-28", paid_amount=1026.0,
                 payment_method="ACH")
    open_id = _bill(vendor="California FAIR Plan Association", due_date="2026-07-04")
    html = c.get(f"/entry/{paid}/view").get_data(as_text=True)
    assert 'data-kk="twin-open-warning"' in html
    assert f"/entry/{open_id}/view" in html
    assert f"merge-record {open_id} {paid}" in html          # dup first, keeper second


def test_open_record_warns_it_may_already_be_paid(staff_client, env):
    c, _ = staff_client
    paid = _bill(status="Paid", paid_date="2026-06-28", paid_amount=1026.0,
                 payment_method="ACH")
    open_id = _bill(vendor="California FAIR Plan Association", due_date="2026-07-04")
    html = c.get(f"/entry/{open_id}/view").get_data(as_text=True)
    assert 'data-kk="twin-paid-warning"' in html
    assert "already be paid" in html and "paid 2026-06-28" in html
    assert f"merge-record {open_id} {paid}" in html


def test_no_banner_without_twins(staff_client, env):
    c, _ = staff_client
    a = _bill()
    html = c.get(f"/entry/{a}/view").get_data(as_text=True)
    assert "twin-open-warning" not in html and "twin-paid-warning" not in html


# ---------------------------------------------------------------- pay-flow detour

def test_pay_with_open_twin_lands_on_warning(staff_client, env):
    c, headers = staff_client
    a = _bill()
    b = _bill(vendor="California FAIR Plan Association", due_date="2026-07-04")
    r = c.post(f"/entry/{a}/pay", headers=headers,
               data={"paid_date": "2026-06-28", "paid_amount": "1026.00",
                     "payment_method": "ACH"})
    assert r.status_code == 302
    assert f"/entry/{a}/view" in r.headers["Location"]
    assert f"%23{b}" in r.headers["Location"]        # "#<id>" urlencoded in msg
    with database.get_conn() as conn:
        actions = [row["action"] for row in conn.execute(
            "SELECT action FROM activity_log WHERE entry_id=? ORDER BY id", (a,))]
    assert "Same-bill records still open" in actions


def test_pay_without_twins_keeps_normal_redirect(staff_client, env):
    c, headers = staff_client
    a = _bill()
    r = c.post(f"/entry/{a}/pay", headers=headers,
               data={"paid_date": "2026-06-28"},
               environ_base={"HTTP_REFERER": "/bills"})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/bills")


# ------------------------------------------------- shared _mark_paid (fix #3):
# pay_all_vendor and bulk_pay used to skip the refile + twin-check pay_entry
# already did — a bulk/vendor-wide payment could leave a twin silently "unpaid"
# the same way FAIR Plan #51 did. Both paths now go through _mark_paid.

def test_pay_all_vendor_flags_open_twin(staff_client, env):
    c, headers = staff_client
    a = _bill()
    b = _bill(vendor="California FAIR Plan Association", due_date="2026-07-04")
    c.post("/bills/vendor/California FAIR Plan Association/pay_all",
           headers=headers, data={"paid_date": "2026-06-28"})
    with database.get_conn() as conn:
        actions = [row["action"] for row in conn.execute(
            "SELECT action FROM activity_log WHERE entry_id=? ORDER BY id", (a,))]
    assert "Same-bill records still open" in actions
    assert database.get_entry(a)["status"] == "Paid"
    assert database.get_entry(b)["status"] == "Paid"


def test_bulk_pay_flags_open_twin_in_response(staff_client, env):
    c, headers = staff_client
    a = _bill()
    b = _bill(vendor="California FAIR Plan Association", due_date="2026-07-04")
    r = c.post("/bills/bulk_pay", headers=headers, data={"entry_ids": [str(a), str(b)]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["paid"] == 2
    assert body["twin_warning"] is not None
    assert f"#{a}" in body["twin_warning"] or f"#{b}" in body["twin_warning"]
    with database.get_conn() as conn:
        actions = [row["action"] for row in conn.execute(
            "SELECT action FROM activity_log WHERE entry_id IN (?, ?)", (a, b))]
    assert "Same-bill records still open" in actions


def test_bulk_pay_without_twins_reports_none(staff_client, env):
    c, headers = staff_client
    a = _bill()
    r = c.post("/bills/bulk_pay", headers=headers, data={"entry_ids": [str(a)]})
    body = r.get_json()
    assert body["paid"] == 1
    assert body["twin_warning"] is None


# ---------------------------------------------------------------- merge-record CLI

def test_cli_merge_record_folds_dup_into_keeper(env, capsys):
    keeper = _bill(status="Paid", paid_date="2026-06-28", paid_amount=1026.0,
                   payment_method="ACH", account_number=None)
    dup = _bill(vendor="California FAIR Plan Association", due_date="2026-07-04",
                account_number="CFP 0102713675")
    database.add_attachment(dup, "X:\\somewhere\\copy.pdf", "Email", "copy.pdf", "T")
    assert klr._cli_reports(["merge-record", str(dup), str(keeper)]) is True
    out = capsys.readouterr().out
    assert f"Merged #{dup} into #{keeper}" in out
    assert database.get_entry(dup) is None
    kept = database.get_entry(keeper)
    assert kept["status"] == "Paid"
    assert kept["account_number"] == "CFP 0102713675"      # blank filled from dup
    assert any(att["original_name"] == "copy.pdf"
               for att in database.get_attachments(keeper))
    with database.get_conn() as conn:
        actions = [row["action"] for row in conn.execute(
            "SELECT action FROM activity_log WHERE entry_id=?", (keeper,))]
    assert "Merged duplicate record" in actions


def test_cli_merge_refuses_conflicting_payments(env, capsys):
    keeper = _bill(status="Paid", paid_date="2026-06-28", paid_amount=1026.0)
    dup = _bill(status="Paid", paid_date="2026-07-01", paid_amount=1026.0,
                check_number="9999")
    assert klr._cli_reports(["merge-record", str(dup), str(keeper)]) is True
    assert "NOT merged" in capsys.readouterr().out
    assert database.get_entry(dup) is not None             # nothing deleted


def test_cli_merge_bad_args(env, capsys):
    a = _bill()
    for argv in (["merge-record"], ["merge-record", "x", "y"],
                 ["merge-record", str(a), str(a)], ["merge-record", str(a), "99999"]):
        assert klr._cli_reports(argv) is True
    assert database.get_entry(a) is not None


# ------------------------------------------- manual entry: non-blocking banner
# (Joshua, 2026-07-31, design q5): a hand-typed /entry/new that looks like an
# existing record SAVES anyway, then lands on the record view with a warning
# banner -- informational, never an interstitial. No match -> plain /log
# redirect exactly as before.

def test_manual_entry_matching_existing_saves_with_banner(staff_client, env):
    from datetime import date
    c, headers = staff_client
    a = _bill()                                   # existing FAIR Plan record
    r = c.post("/entry/new", headers=headers, data={
        "date_received": date.today().isoformat(),
        "doc_type": "Bill", "vendor": "California FAIR Plan Association",
        "amount": "1026.00", "invoice_number": "CFP 0102713675",
        "entity_code": "TRUST1",
    })
    assert r.status_code == 302
    loc = r.headers["Location"]
    assert "/view" in loc and "msg=" in loc       # to the record, with the banner
    # The save went through -- both records exist.
    entries = database.get_all_entries()
    assert len(entries) == 2
    new_id = next(e["id"] for e in entries if e["id"] != a)
    html = (c.get(loc).get_data(as_text=True)
            .replace("&#39;", "'").replace("&#34;", '"').replace("&amp;", "&"))
    assert "looks similar to record" in html
    assert f"#{a}" in html
    assert "Check before paying" in html


def test_manual_entry_without_match_plain_redirect(staff_client, env):
    from datetime import date
    c, headers = staff_client
    _bill()                                       # unrelated existing record
    r = c.post("/entry/new", headers=headers, data={
        "date_received": date.today().isoformat(),
        "doc_type": "Bill", "vendor": "Totally Unrelated Landscaping",
        "amount": "55.00", "invoice_number": "XYZ-777",
        "entity_code": "TRUST2",
    })
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/log")
    assert "msg=" not in r.headers["Location"]
    assert len(database.get_all_entries()) == 2   # still saved, no banner detour
