"""Bills and payments — /bills and its lenses, the universal search, the CSV
export, and every "mark paid" path — extracted from app.py in the modular
split (GH #6). Same pattern as routes_karen.py (see its docstring): original
endpoint names via add_url_rule, app-module helpers reached through _M.

The one thing worth knowing before editing here: pay_entry, pay_all_vendor and
bulk_pay all funnel through _mark_paid. They used to each set the paid fields
themselves, which is how FAIR Plan #51 sat "unpaid" for 19 days after Karen
paid its twin #77 on 6/28 — only one path checked for same-bill records still
open. Keep new payment paths going through _mark_paid; that is where the
re-file to Karen's paid-form naming and the open-twin check both live.
"""
from datetime import date

from flask import jsonify, redirect, render_template, request, session, url_for

import bill_filters
import indexer
import queue_state
from database import (find_invoice_twins, get_accounts_for_vendor,
                      get_all_entries, get_bills_by_due_date,
                      get_bills_for_vendor, get_entity_summary, get_entry,
                      get_property_summary, get_vendor_summary, log_activity,
                      save_payment_splits, search_documents, update_entry)

_M = None   # the app module (composition root); set by register()


def register(app, appmod):
    global _M
    _M = appmod
    adm = appmod.admin_required
    r = app.add_url_rule
    r("/bills", "bills", bills)
    r("/bills/reindex", "bills_reindex", adm(bills_reindex), methods=["POST"])
    r("/bills/export.csv", "export_bills_csv", export_bills_csv)
    r("/bills/bulk_pay", "bulk_pay", bulk_pay, methods=["POST"])
    r("/bills/vendor/<path:vendor>", "vendor_bills", vendor_bills)
    r("/bills/vendor/<path:vendor>/pay_all", "pay_all_vendor", pay_all_vendor,
      methods=["POST"])
    r("/entry/<int:entry_id>/pay", "pay_entry", pay_entry, methods=["POST"])


def bills_reindex():
    """Kick a background (re)index of document text. Force re-reads everything
    (e.g. right after installing Tesseract, to OCR the image-only backlog)."""
    force = request.form.get("force") == "1"
    if not queue_state.job_running("reindex"):
        import threading
        threading.Thread(target=_M._run_reindex, kwargs={"force": force},
                         daemon=True).start()
    return redirect(request.referrer or url_for("bills"))


def bills():
    """The foundational bills page. Default view is ALL bills (paid + unpaid)
    with a filter bar (status/entity/property/date); the universal search box
    searches every logged document. By Due Date / Vendor / Entity / Property are
    aggregate lenses on the unpaid stack (what's still owed)."""
    # A search query takes over the page (universal search across all logged
    # records, any doc type); otherwise All Bills is the default landing view.
    q = (request.args.get("q") or "").strip()
    view = "search" if q else request.args.get("view", "all")
    if view not in ("all", "search", "due", "vendor", "entity", "property"):
        view = "all"
    cfg = _M.load_config()
    search_results = search_documents(q) if view == "search" else None
    # Option B: records whose document TEXT matches (not just the logged fields),
    # shown as a second "inside the documents" section. Skip records already
    # surfaced by the field search so they aren't listed twice.
    doc_matches = []
    index_status = None
    # Report #16 (Nancy): the universal search has to reach the review queue
    # too. A document waiting there has no logged record, so it cannot appear
    # in ANY of the searches above — which is exactly how three of Nancy's
    # eight ACS invoices went missing in report #95. Shown as its own section,
    # never mixed in with logged records: these are not records yet.
    queue_matches = []
    if view == "search":
        index_status = indexer.status()
        seen = {r["id"] for r in (search_results or [])}
        for m in indexer.search(q, limit=30):
            e = get_entry(m["entry_id"])
            if e and e["id"] not in seen:
                e = dict(e)
                e["_snippet"] = m.get("snippet") or ""
                doc_matches.append(e)
        queue_matches = queue_state.search_queue(q, limit=12)

    # ── All Bills: every bill, filtered by the visible controls ──────────────
    filters = {
        "status":        request.args.get("status", ""),
        "entity_code":   request.args.get("entity_code", ""),
        "property_code": request.args.get("property_code", ""),
        # #77 (Joshua): filter by how the bill arrived. get_all_entries already
        # knows mail_type; the Bills page just never offered it.
        "mail_type":     request.args.get("mail_type", ""),
        "date_from":     request.args.get("date_from", ""),
        "date_to":       request.args.get("date_to", ""),
    }
    # Report #93: a search used to take the page over and drop the filter bar
    # on the floor — filter to Paid, search a vendor, and every one of that
    # vendor's bills came back. The filters travel with the search now (the
    # form carries them; bill_filters applies the same rules get_all_entries
    # applies in SQL), and the page says which ones are still on.
    filters_on = bill_filters.active(filters)
    if view == "search" and filters_on:
        search_results = bill_filters.apply_filters(search_results, filters)
        doc_matches = bill_filters.apply_filters(doc_matches, filters)

    bill_rows = bill_summary = None
    if view == "all":
        bill_rows = get_all_entries({"doc_type": "Bill", **filters})
        paid = [b for b in bill_rows if b.get("status") in ("Paid", "Filed")]
        unpaid = [b for b in bill_rows if b.get("status") not in ("Paid", "Filed")]
        bill_summary = {
            "count": len(bill_rows),
            "outstanding": sum((b.get("amount") or 0) for b in unpaid),
            "paid": sum((b.get("paid_amount") or b.get("amount") or 0) for b in paid),
        }

    # ── Aggregate lenses (unpaid "owed" summaries) — only build what's shown ──
    by_due = get_bills_by_due_date() if view == "due" else None
    vendors = get_vendor_summary() if view == "vendor" else None
    entity_items = property_items = None
    if view == "entity":
        entity_items = [{"label": e["entity_code"], "sublabel": e.get("entity_name") or "",
                         "count": e["bill_count"], "total": e["total_amount"],
                         "next_due": e.get("next_due"),
                         "link": f"/log?entity_code={e['entity_code']}&doc_type=Bill"}
                        for e in get_entity_summary()]
    elif view == "property":
        addr = {p["code"]: p["address"] for p in cfg.get("properties", [])}
        property_items = [{"label": p["property_code"], "sublabel": addr.get(p["property_code"], ""),
                           "count": p["bill_count"], "total": p["total_amount"],
                           "next_due": p.get("next_due"),
                           "link": f"/log?property_code={p['property_code']}&doc_type=Bill"}
                          for p in get_property_summary()]
    # report #10 (Nancy) — the Entity column showed the raw code only; every
    # other page pairs it with the friendly name from config.json, this one just
    # hadn't been wired up the same way.
    entity_names = {e["code"]: e.get("name", "") for e in cfg.get("entities", [])}
    return render_template("bills.html", vendors=vendors, by_due=by_due, view=view,
                           bill_rows=bill_rows, bill_summary=bill_summary, filters=filters,
                           search_results=search_results, q=q, cfg=cfg,
                           filters_on=filters_on, queue_matches=queue_matches,
                           doc_matches=doc_matches, index_status=index_status,
                           reindex_running=queue_state.job_running("reindex"),
                           entity_names=entity_names,
                           entity_items=entity_items, property_items=property_items,
                           today=date.today().isoformat(), nav_qs="?ctx=bills")


def vendor_bills(vendor):
    entries = get_bills_for_vendor(vendor)
    from urllib.parse import urlencode
    nav_qs = "?" + urlencode({"ctx": "vendor", "vendor": vendor})
    return render_template("vendor_bills.html", vendor=vendor, entries=entries,
                           accounts=get_accounts_for_vendor(vendor),
                           today=date.today().isoformat(), cfg=_M.load_config(),
                           nav_qs=nav_qs)


def _mark_paid(entry_id, *, paid_date, paid_amount, paid_by, detail,
               method=None, check_number=None, payment_note=None):
    """Shared core of every 'mark paid' path (pay_entry, pay_all_vendor, bulk_pay):
    sets the paid fields, re-files the document to Karen's paid-form naming
    convention, and reports back any other open records that look like the same
    bill so a bulk/vendor-wide payment can't silently leave a twin sitting
    "unpaid" the way FAIR Plan #50 did for 19 days after #77 was paid. Returns
    (fresh_entry, open_twins)."""
    update_entry(entry_id, {
        "status":         "Paid",
        "paid_date":      paid_date,
        "paid_amount":    paid_amount,
        "paid_by":        paid_by,
        "payment_method": method or None,
        "check_number":   check_number if method == "Check" else None,
        "payment_note":   payment_note or None,
    })
    log_activity(entry_id, "Marked paid", paid_by or "", detail)
    _M._refile_to_convention(entry_id)
    fresh = get_entry(entry_id)
    twins = [t for t in (find_invoice_twins(fresh) if fresh else [])
             if (t.get("status") or "") not in ("Paid", "Filed")]
    if twins:
        ids = ", ".join(f"#{t['id']}" for t in twins)
        log_activity(entry_id, "Same-bill records still open", paid_by or "", ids)
    return fresh, twins


def pay_all_vendor(vendor):
    f = request.form
    paid_date = f.get("paid_date") or date.today().isoformat()
    paid_by   = session.get("user") or None
    entries = get_bills_for_vendor(vendor)
    for e in entries:
        if e["status"] not in ("Paid", "Filed"):
            _mark_paid(e["id"], paid_date=paid_date, paid_amount=e["amount"],
                       paid_by=paid_by, detail=f"pay-all: {vendor}")
    return redirect(url_for("vendor_bills", vendor=vendor))


def bulk_pay():
    paid_by = session.get("user")
    paid_date = date.today().isoformat()
    n = 0
    twin_ids = set()
    for s in request.form.getlist("entry_ids"):
        try:
            eid = int(s)
        except ValueError:
            continue
        e = get_entry(eid)
        if e and e["status"] not in ("Paid", "Filed"):
            _, twins = _mark_paid(eid, paid_date=paid_date, paid_amount=e.get("amount"),
                                  paid_by=paid_by, detail="bulk")
            twin_ids.update(t["id"] for t in twins)
            n += 1
    return jsonify({"ok": True, "paid": n,
                    "twin_warning": (f"{n} paid, but "
                        + ", ".join(f"#{i}" for i in sorted(twin_ids))
                        + " still show as open and may be the same bill.")
                    if twin_ids else None})


def _csv_safe(v):
    """Excel/Sheets execute cells starting with = + - @ as formulas — a vendor
    named '=HYPERLINK(...)' would run on Karen's machine when she opens the
    export. Prefix such values with a quote (the spreadsheet shows them as text)."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def export_bills_csv():
    import csv, io
    from flask import Response
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Received", "Due Date", "Vendor", "Entity", "Property", "Bill Type",
                "Invoice #", "Account #", "Amount"])
    # Row order mirrors the Catch Up view it was exported from (?order=due for
    # the due-date buckets; default is Karen's received-order stack).
    rows = get_bills_by_due_date() if request.args.get("order") == "due" \
        else _M._catchup_received_rows()
    for b in rows:
        w.writerow([_csv_safe(b.get("date_received", "")),
                    _csv_safe(b.get("due_date", "")), _csv_safe(b.get("vendor", "")),
                    _csv_safe(b.get("entity_code", "")), _csv_safe(b.get("property_code", "")),
                    _csv_safe(b.get("bill_type_code", "")), _csv_safe(b.get("invoice_number", "")),
                    _csv_safe(b.get("account_number", "")),
                    b.get("amount") if b.get("amount") is not None else ""])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=unpaid_bills.csv"})


def pay_entry(entry_id):
    f = request.form
    paid_amount = _M._parse_money(f.get("paid_amount"))
    method = (f.get("payment_method") or "").strip()
    check_number = (f.get("check_number") or "").strip()
    payment_note = (f.get("payment_note") or "").strip()
    bits = []
    if paid_amount is not None: bits.append(f"${paid_amount:,.2f}")
    if method: bits.append(method + (f" #{check_number}" if method == "Check" and check_number else ""))
    # #2 split: record the per-entity allocation when the form submitted one.
    split = []
    for ent, amt in zip(f.getlist("split_entity"), f.getlist("split_amount")):
        val = _M._parse_money(amt)
        if ent and val is not None:
            split.append((ent, val))
    # Only overwrite when this form actually carried split rows. Since report #54 the
    # split can also be set on the edit form, and the pay panel only renders its split
    # inputs when the bill's properties span >1 entity -- so an unconditional save here
    # would silently wipe a hand-entered split the moment the bill was marked paid.
    if split:
        save_payment_splits(entry_id, split)
        bits.append("split " + " / ".join(f"{e} ${v:,.2f}" for e, v in split))
    # _mark_paid sets the paid fields, re-files to Karen's paid-form naming
    # convention (e.g. CK#13386), and flags any other open records that look
    # like this same bill.
    fresh, twins = _mark_paid(entry_id,
        paid_date=f.get("paid_date") or date.today().isoformat(),
        paid_amount=paid_amount, paid_by=session.get("user") or None,
        method=method, check_number=check_number, payment_note=payment_note,
        detail=" · ".join(bits))
    # A receipt chosen in the Mark as Paid form uploads with Save Payment — after
    # the re-file, so it lands next to the document's final paid-form name.
    receipt = request.files.get("receipt")
    if receipt and receipt.filename and fresh:
        _M._store_receipt_upload(fresh, receipt)
    # If open records still duplicate this bill, land the payer on the record
    # view where the twins banner is visible — otherwise the owed list keeps
    # claiming the money is due (how FAIR Plan #51 stayed "unpaid" for 19 days
    # after Karen paid #77 on 6/28).
    if twins:
        ids = ", ".join(f"#{t['id']}" for t in twins)
        verb = ("look like copies of this same bill and are"
                if len(twins) > 1 else "looks like this same bill and is")
        return redirect(url_for("view_entry", entry_id=entry_id,
                                msg=f"Payment saved — but {ids} {verb} still "
                                    f"open (see the warning below)."))
    return redirect(request.referrer or url_for("log"))
