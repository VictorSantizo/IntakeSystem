"""Record CRUD — /entry/* and /attachment/* — extracted from app.py in the
modular split (GH #6). Same pattern as routes_karen.py (see its docstring):
original endpoint names via add_url_rule, app-module helpers reached through
_M so tests' monkeypatching keeps working.

Everything a person does to a record AFTER it is logged lives here: create by
hand, edit, view, pass to a teammate, move its document, flag it, link an
original, attach a receipt, remove copies, delete. The AI-fill feature comes
along whole — the per-record button, its shared core, and the admin bulk sweep
over the Harvest-Bot backlog — even though the bulk route's URL sits under
/settings; keeping the three apart would split one feature across two modules.

Nothing here writes to the review queue: by this point the document is already
a record. The two places that MOVE a document on disk (move_entry_file and the
receipt store) announce to filing_ledger first, so the cabinet watcher doesn't
mistake our own move for someone hand-filing.
"""
import io
import os
import re
import shutil
import threading
import time
from datetime import date, datetime

from flask import (jsonify, redirect, render_template, request, send_file,
                   session, url_for)

import doc_types
import email_pdf
import filing
import filing_ledger
import queue_state
from classifier import classify_document
from database import (add_attachment, add_record_pass, delete_attachment,
                      delete_attachments, delete_entry, find_invoice_twins,
                      find_link_candidates, find_similar_vendor, get_all_entries,
                      get_attachment, get_attachments, get_entries_by_hash,
                      get_entry, get_pass,
                      get_entry_units, get_passes_for_entry, get_payment_splits,
                      insert_entry, log_activity, mark_pass_done, save_payment_splits,
                      set_entry_units, set_no_original, update_attachment_path,
                      update_entry)
import nameparse
from property_model import entity_unit_weights, get_units_by_property

_M = None   # the app module (composition root); set by register()

_AI_FILL_FIELDS = (("vendor", "Vendor"), ("amount", "Amount"),
                   ("due_date", "Due date"), ("invoice_number", "Invoice #"),
                   ("account_number", "Account #"))

_VALID_STATUSES = {"Pending", "In Progress", "Processed", "Paid", "Filed", "On Hold"}

# ── Starting a manual entry from a document (report #50) ─────────────────────
# Joshua, twice: "This screen should have an option to pull in a scanned file
# and choose whether to move it to a new location or leave it where it is. If
# it's on the user's local drive, then it should be copied into the correct
# location ... OCR completed after and the form filled with that info", then
# the bounce: "i don't see an option on the new entry page to upload a
# document." The queue got an Upload button in 6997a9b; /entry/new never did.
#
# Two origins, and the difference is the whole feature. A file on someone's own
# computer has no server path at all, so its bytes are copied over first and it
# is always filed into the cabinet on save. A file that already lives on the
# file server can be left exactly where the person put it — byte-identical,
# same name (report #90's rule: the app never renames a staff-filed document).
_NEW_ENTRY_STAGING = os.path.join("Uploads", "New Entry")

# What the classifier can actually read. Neither the staging folder nor the
# scan folder's Uploads area is watched (the watcher and both sweeps are
# non-recursive), so a staged file can never surface in the review queue
# behind the person filling in the form.
_SOURCE_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".docx")


def _staging_dir(cfg):
    scan = (cfg.get("scan_folder") or "").strip()
    return os.path.join(scan, _NEW_ENTRY_STAGING) if scan else ""


def _stage_uploaded_document(fs, cfg):
    """Copy a document chosen on someone's own computer onto the file server.
    Returns (path, error) — a C:\\ path on a desk means nothing here, so the
    bytes have to come across before anything else can happen."""
    safe = re.sub(r'[\\/:*?"<>|]', "", os.path.basename(fs.filename or "")).strip()
    base, ext = os.path.splitext(safe)
    if not base or ext.lower() not in _SOURCE_EXTS:
        return None, ("That file type can't be read — PDFs, images and Word "
                      "(.docx) documents only.")
    stage = _staging_dir(cfg)
    if not stage:
        return None, "No scan folder is configured, so there is nowhere to put the file."
    try:
        os.makedirs(stage, exist_ok=True)
        dest, n = os.path.join(stage, safe), 1
        while os.path.exists(dest):
            dest = os.path.join(stage, f"{base} ({n}){ext}")
            n += 1
        fs.save(dest)
        return dest, None
    except OSError as e:
        return None, f"Could not save the file to the file server: {e}"


def _resolve_source_document(raw, cfg):
    """Re-validate the document path the form carries in a hidden field, on
    every POST. Two legal origins and nothing else: a file this app staged
    from an upload, or a file that already lives on the file server and passes
    filing.py's rules. A hidden field is not a permission. Returns
    (server path, staged?) — (None, False) when it is neither."""
    p = (raw or "").strip()
    if not p:
        return None, False
    stage = _staging_dir(cfg)
    if stage:
        pref = os.path.normcase(os.path.normpath(stage)) + os.sep
        if os.path.normcase(os.path.normpath(p)).startswith(pref) and os.path.isfile(p):
            return os.path.normpath(p), True
    resolved = filing.resolve_client_file(p, cfg)
    return (resolved, False) if resolved else (None, False)


def _draft_from_document(path, cfg, staged):
    """Read a picked document with the classifier and shape the answer like an
    entry row, so the New Entry form can render it as pre-filled values —
    #50's last line, "OCR completed after and the form filled with that info".

    The received date follows the house order: the RECEIVED stamp the reader
    found on the document wins (Karen, #107), then the filename-and-folder rule
    the cabinet watcher uses for hand-filed documents (nameparse.received_from_
    filing — the tree owns the year, the filename owns month and day), then
    today. Returns (draft, note)."""
    result = classify_document(path) or {}
    props = [c for c in (result.get("property_codes") or []) if c]
    addr_by = {p["code"]: p["address"] for p in cfg.get("properties", [])}
    entity_code = result.get("entity_code") or ""
    try:
        amount = (float(result["amount"])
                  if result.get("amount") not in (None, "") else None)
    except (TypeError, ValueError):
        amount = None
    received, note = result.get("received_date") or "", None
    if not received:
        doc = nameparse.parse_name(path)
        priors = nameparse.path_priors(path, cfg)
        filed_date, note = nameparse.received_from_filing(doc["date"], priors)
        received = filed_date.isoformat() if filed_date else date.today().isoformat()
    draft = {
        "date_received":    received,
        "mail_type":        result.get("mail_type") or "Physical Mail",
        "entity_code":      entity_code,
        "property_code":    ", ".join(props),
        "property_address": ", ".join(addr_by[c] for c in props if c in addr_by),
        "doc_type":         result.get("doc_type") or "",
        "bill_type_code":   result.get("bill_type_code") or "",
        "vendor":           result.get("vendor") or "",
        "amount":           amount,
        "due_date":         result.get("due_date") or "",
        "invoice_number":   result.get("invoice_number") or "",
        "account_number":   result.get("account_number") or "",
        "status":           "Pending",
        "notes":            result.get("notes") or "",
        "ai_confidence":    result.get("ai_confidence") or "",
    }
    return draft, note


def _render_new_entry(cfg, draft=None, source=None, error=None):
    """One place that renders the blank New Entry form, so the GET, the
    document pick and every error path stay identical apart from what they
    know."""
    return render_template("entry_form.html", cfg=cfg, entry=None,
                           draft=draft, source=source, doc_error=error,
                           today=date.today().isoformat(), nav_qs="",
                           units_by_property=get_units_by_property(),
                           selected_unit_ids=[],
                           payment_splits=[], split_entities=[])


def new_entry_from_document():
    """Report #50: pull a scanned document into the New Entry form, read it,
    and come back with the form filled in. Nothing is logged here and nothing
    on the server is moved — the person still has to check the fields and press
    Log Entry, which is where the document is finally filed (or deliberately
    left where it was)."""
    cfg = _M.load_config()
    placement = request.form.get("placement") or "cabinet"
    filed_dir = (request.form.get("filed_dir") or "").strip()
    up = request.files.get("upload")
    if up and up.filename:
        path, err = _stage_uploaded_document(up, cfg)
        if err:
            return _render_new_entry(cfg, error=err)
        staged = True
        placement = "cabinet"      # a file from a desk has no home here yet
    else:
        raw = (request.form.get("source_path") or "").strip()
        if not raw:
            return _render_new_entry(cfg, error=(
                "Choose a document first — one on this computer, or one that is "
                "already on the file server."))
        path, staged = _resolve_source_document(raw, cfg)
        if not path:
            return _render_new_entry(cfg, error=(
                f"That document isn't on the file server, or it no longer "
                f"exists: {raw}"))
    try:
        draft, date_note = _draft_from_document(path, cfg, staged)
    except Exception as e:                      # a reader failure is not a dead end
        print(f"[NewEntry] could not read {path}: {e}")
        draft, date_note = None, None
    # The same byte-level duplicate check the queue does, before anyone types:
    # the point of #50 is fewer trips, and finding out afterwards that this
    # document is already record #813 is the most expensive trip of all.
    already = []
    try:
        already = get_entries_by_hash(_M._sha256_file(path)) or []
    except OSError:
        pass
    source = {
        "path":      path,
        "name":      os.path.basename(path),
        "unc":       filing.display_path(path, cfg),
        "staged":    staged,
        "placement": placement if not staged else "cabinet",
        "filed_dir": filed_dir,
        "note":      date_note,
        "duplicates": [{"id": r.get("id"), "record_no": r.get("record_no"),
                        "vendor": r.get("vendor")} for r in already],
    }
    return _render_new_entry(cfg, draft=draft, source=source,
                             error=None if draft else (
                                 "The document was picked up, but it couldn't be "
                                 "read — fill the details in by hand."))


def _submitted_splits(form):
    """The edit form's entity-allocation rows as (entity_code, amount) pairs.

    Blank rows are dropped rather than rejected -- the block starts with an empty row
    and "+ Add entity" adds more, so a half-filled row is a normal intermediate state,
    not an error worth blocking a save over."""
    out = []
    for ent, amt in zip(form.getlist("split_entity"), form.getlist("split_amount")):
        ent = (ent or "").strip()
        val = _M._parse_money(amt)
        if ent and val is not None:
            out.append((ent, val))
    return out


def _submitted_unit_ids(form, property_codes):
    """The form's checked units, dropped to those that belong to a property the record
    still names (report #20).

    The form renders every property's units at once and shows/hides them in the browser
    as properties are ticked, so unticking a property leaves its unit boxes checked but
    hidden. Without this filter a record could keep a unit belonging to a property it no
    longer references -- the units equivalent of the orphaned property_code the delete
    guard exists to prevent."""
    try:
        checked = {int(v) for v in form.getlist("unit_ids")}
    except (TypeError, ValueError):
        return []
    allowed = set()
    units_by_property = get_units_by_property()
    for code in property_codes:
        for u in units_by_property.get(code, []):
            allowed.add(u["id"])
    return sorted(checked & allowed)


def register(app, appmod):
    global _M
    _M = appmod
    adm = appmod.admin_required
    # The document-type vocabulary (doc_types.py, reports #51/#57/#58/#59) is
    # published to Jinja here so every dropdown that offers a type — this
    # module's entry form, the Intake Log filter, the review form — reads the
    # one list instead of its own copy of the literal.
    app.jinja_env.globals["doc_type_choices"] = doc_types.choices_for
    r = app.add_url_rule
    r("/entry/new", "new_entry", new_entry, methods=["GET", "POST"])
    r("/entry/new/from-document", "new_entry_from_document",
      new_entry_from_document, methods=["POST"])
    r("/entry/<int:entry_id>/edit", "edit_entry", edit_entry, methods=["GET", "POST"])
    r("/entry/<int:entry_id>/view", "view_entry", view_entry)
    r("/entry/<int:entry_id>/pdf", "entry_pdf", entry_pdf)
    r("/entry/<int:entry_id>/packet.pdf", "entry_packet_pdf", entry_packet_pdf)
    r("/entry/<int:entry_id>/pass", "pass_entry", pass_entry, methods=["POST"])
    r("/entry/<int:entry_id>/move-file", "move_entry_file", move_entry_file,
      methods=["POST"])
    r("/entry/<int:entry_id>/ai_fill", "entry_ai_fill", entry_ai_fill, methods=["POST"])
    r("/entry/<int:entry_id>/important", "toggle_important", toggle_important,
      methods=["POST"])
    r("/entry/<int:entry_id>/turn_into_bill", "turn_into_bill", turn_into_bill,
      methods=["POST"])
    r("/entry/<int:entry_id>/done", "mark_done", mark_done, methods=["POST"])
    r("/entry/<int:entry_id>/no_original", "toggle_no_original", toggle_no_original,
      methods=["POST"])
    r("/entry/<int:entry_id>/link_original/<int:other_id>", "link_original",
      link_original, methods=["POST"])
    r("/entry/<int:entry_id>/attach_receipt", "attach_receipt", attach_receipt,
      methods=["POST"])
    r("/entry/<int:entry_id>/copies/remove_all", "remove_all_copies",
      remove_all_copies, methods=["POST"])
    r("/entry/<int:entry_id>/delete", "delete_entry_route", delete_entry_route,
      methods=["POST"])
    r("/entry/<int:entry_id>/status", "update_status", update_status, methods=["POST"])
    r("/pass/<int:pass_id>/done", "pass_done", pass_done, methods=["POST"])
    r("/attachment/<int:att_id>/pdf", "attachment_pdf", attachment_pdf)
    r("/attachment/<int:att_id>/remove", "remove_attachment", remove_attachment,
      methods=["POST"])
    r("/settings/ai_fill_harvest", "ai_fill_harvest", adm(ai_fill_harvest),
      methods=["POST"])


def new_entry():
    cfg = _M.load_config()
    if request.method == "POST":
        form = request.form
        # Year-sanity guard (record #815) — same rule as review's Confirm & Log.
        # Harvest/backfill inserts call insert_entry directly and never see this.
        if not form.get("date_ok"):
            date_warnings = _M._date_sanity_warnings(form.get("date_received"),
                                                     form.get("due_date"))
            if date_warnings:
                return render_template("date_warning.html",
                                       warnings=date_warnings,
                                       action=url_for("new_entry"),
                                       back_url=None,
                                       form_data=request.form)
        entity_code = form.get("entity_code","")
        entity_name = next((e["name"] for e in cfg["entities"] if e["code"]==entity_code), "")
        property_codes = form.getlist("property_codes")
        addr_by_code = {p["code"]: p["address"] for p in cfg["properties"]}
        property_code = ", ".join(property_codes)
        property_address = ", ".join(addr_by_code[c] for c in property_codes if c in addr_by_code)
        amount = _M._parse_money(form.get("amount"))
        entry = {
            "date_received":    form.get("date_received", date.today().isoformat()),
            "mail_type":        form.get("mail_type","Physical Mail"),
            "entity_code":      entity_code,
            "entity_name":      entity_name,
            "property_code":    property_code,
            "property_address": property_address,
            "doc_type":         form.get("doc_type","Other"),
            "bill_type_code":   form.get("bill_type_code",""),
            "vendor":           form.get("vendor",""),
            "amount":           amount,
            "due_date":         form.get("due_date",""),
            "invoice_number":   _M._id_field(form.get("invoice_number")),
            "account_number":   _M._id_field(form.get("account_number")),
            "status":           form.get("status","Pending"),
            "processed_by":     session.get("user",""),
            "notes":            form.get("notes",""),
        }
        # Non-blocking similarity flag (Joshua, 2026-07-31, design q5): manual
        # entry gets the same twin/duplicate/vendor-drift awareness the review
        # confirm path has, but as an after-the-fact banner — the save always
        # goes through (a hand-typed entry is deliberate), the person just
        # lands on the record with a warning instead of silently back on /log.
        # find_similar_vendor must run BEFORE the insert: it returns None on
        # any exact vendor match, and after the insert the new record's own
        # spelling is on file and would always self-match.
        similar = (find_similar_vendor(entry["vendor"])
                   if (entry["vendor"] or "").strip() else None)
        # Report #50 — the form may have been started from a document. The path
        # is re-validated here, not trusted from the hidden field, and the two
        # placements are genuinely different acts: "leave it where it is"
        # records the file untouched (no move, no rename — #90), "file it into
        # the cabinet" hands it to the same _finalize_entry the review queue
        # uses, so it lands under the archive's own naming convention.
        source_path, staged = _resolve_source_document(form.get("source_path"), cfg)
        placement = "cabinet" if staged else (form.get("placement") or "cabinet")
        if source_path:
            try:
                entry["file_hash"] = _M._sha256_file(source_path)
            except OSError:
                pass
        if source_path and placement == "leave":
            entry["pdf_path"] = source_path
            entry["filed_path"] = source_path
            new_id = insert_entry(entry)
            log_activity(new_id, "Created", session.get("user", ""),
                         f"manual entry from a document left in place: {source_path}")
        elif source_path:
            new_id = _M._finalize_entry(
                entry, source_path, cfg, property_codes,
                f"manual entry from document: {os.path.basename(source_path)}",
                filed_dir=(form.get("filed_dir") or "").strip() or None)
        else:
            new_id = insert_entry(entry)
            log_activity(new_id, "Created", session.get("user", ""), "manual entry")
        set_entry_units(new_id, _submitted_unit_ids(form, property_codes))
        if form.get("split_block"):
            save_payment_splits(new_id, _submitted_splits(form))
        twins = find_invoice_twins(get_entry(new_id))   # excludes self by id
        if twins or similar:
            bits = []
            if twins:
                ids = ", ".join(f"#{t['id']}" for t in twins[:4])
                bits.append(f"this looks similar to record{'s' if len(twins) != 1 else ''} {ids}")
            if similar:
                bits.append(f'the vendor name is close to "{similar["vendor"]}" already on file')
            detail = " and ".join(bits)
            log_activity(new_id, "Possible match on manual entry",
                         session.get("user", ""), detail)
            return redirect(url_for("view_entry", entry_id=new_id,
                                    msg=f"Saved — but {detail}. Check before paying."))
        if source_path:
            # A record that came with a document lands ON the record, so the
            # person can see the document sitting on it — the whole point of
            # #50 was not making a second trip to find out where it went.
            where = ("left where it was" if placement == "leave"
                     else "filed into the cabinet")
            return redirect(url_for("view_entry", entry_id=new_id,
                                    msg=f"Logged — the document was {where}."))
        return redirect(url_for("log"))
    return _render_new_entry(cfg)


def edit_entry(entry_id):
    cfg = _M.load_config()
    entry = get_entry(entry_id)
    if not entry:
        return redirect(url_for("log"))
    if request.method == "POST":
        form = request.form
        entity_code = form.get("entity_code","")
        entity_name = next((e["name"] for e in cfg["entities"] if e["code"]==entity_code), "")
        property_codes = form.getlist("property_codes")
        addr_by_code = {p["code"]: p["address"] for p in cfg["properties"]}
        property_code = ", ".join(property_codes)
        property_address = ", ".join(addr_by_code[c] for c in property_codes if c in addr_by_code)
        amount = _M._parse_money(form.get("amount"))
        upd = {
            "date_received":    form.get("date_received"),
            "mail_type":        form.get("mail_type"),
            "entity_code":      entity_code,
            "entity_name":      entity_name,
            "property_code":    property_code,
            "property_address": property_address,
            "doc_type":         form.get("doc_type"),
            "bill_type_code":   form.get("bill_type_code"),
            "vendor":           form.get("vendor"),
            "amount":           amount,
            "due_date":         form.get("due_date"),
            "invoice_number":   _M._id_field(form.get("invoice_number")),
            "account_number":   _M._id_field(form.get("account_number")),
            "status":           form.get("status"),
            # Preserve the original processor for audit integrity; only stamp
            # the current user if the entry never had one.
            "processed_by":     entry.get("processed_by") or session.get("user",""),
            "notes":            form.get("notes"),
        }
        # The payment block only renders for a Paid record, so only persist payment
        # fields when it was actually submitted — otherwise we'd wipe them on a
        # non-paid edit. (This also makes the paid date/amount inputs actually save.)
        if "paid_date" in form:
            paid_amount = _M._parse_money(form.get("paid_amount"))
            method = (form.get("payment_method") or "").strip()
            upd.update({
                "paid_date":      form.get("paid_date") or None,
                "paid_amount":    paid_amount,
                "payment_method": method or None,
                "check_number":   (form.get("check_number") or "").strip() if method == "Check" else None,
                "payment_note":   (form.get("payment_note") or "").strip() or None,
            })
        # 2026-07-14 fix: flipping status to Paid on a record whose form didn't
        # render the payment block used to save NO paid metadata — a "ghost
        # payment" that paid-date reports silently drop. Stamp the minimum;
        # details can be refined on the next edit (the block renders once Paid).
        if (form.get("status") == "Paid" and entry.get("status") != "Paid"
                and "paid_date" not in form):
            upd["paid_date"] = entry.get("paid_date") or date.today().isoformat()
            if not entry.get("paid_by"):
                upd["paid_by"] = session.get("user", "")
            if entry.get("paid_amount") is None and entry.get("amount") is not None:
                upd["paid_amount"] = entry.get("amount")
        update_entry(entry_id, upd)
        set_entry_units(entry_id, _submitted_unit_ids(form, property_codes))
        # Report #54 — the entity split, saved from the edit form. Guarded on the
        # block's marker for the same reason the payment fields are: a form that
        # didn't render it must not wipe an existing split.
        if form.get("split_block"):
            save_payment_splits(entry_id, _submitted_splits(form))
        new_status = form.get("status")
        detail = f"status changed to {new_status}" if new_status and new_status != entry.get("status") else ""
        log_activity(entry_id, "Edited", session.get("user",""), detail)
        # Stay on the record after saving (don't dump back to the list), preserving
        # the nav context the form carried in its URL so Back/Prev/Next still work.
        return redirect(url_for("view_entry", entry_id=entry_id) + _M._nav_qs(request.args))
    # Report #54 — the entity split, editable here rather than only in the pay panel.
    # split_entities is the "By units" suggestion; it's empty when the bill's properties
    # all belong to one entity, in which case the person picks the entities by hand.
    prop_codes = [c.strip() for c in (entry.get("property_code") or "").split(",") if c.strip()]
    weights = entity_unit_weights(prop_codes)
    return render_template("entry_form.html", cfg=cfg, entry=entry,
                           today=date.today().isoformat(),
                           nav_qs=_M._nav_qs(request.args),
                           units_by_property=get_units_by_property(),
                           selected_unit_ids=[u["id"] for u in get_entry_units(entry_id)],
                           payment_splits=get_payment_splits(entry_id),
                           split_entities=(sorted(weights.items(), key=lambda kv: -kv[1])
                                           if len(weights) > 1 else []))


def view_entry(entry_id):
    cfg = _M.load_config()
    entry = get_entry(entry_id)
    if not entry:
        return redirect(url_for("log"))
    pdf_on_disk = entry.get("filed_path") or entry.get("pdf_path") or ""
    has_pdf = bool(pdf_on_disk) and os.path.exists(pdf_on_disk)
    # Heal-on-view: a hand-moved/renamed document used to stay broken until the
    # next server restart ran the startup self-heal — record #854 sat as a "BIG
    # PROBLEM" for 46 minutes (report #46). Try the same conservative hash
    # relink right now, while the person who moved the file is looking at it.
    if not has_pdf and pdf_on_disk:
        healed = _M._relink_entry_now(entry)
        if healed:
            entry = get_entry(entry_id)
            pdf_on_disk = healed
            has_pdf = True
    # Prev/Next within the list this record was opened from, + a Back link.
    back_url, back_label, ids = _M._nav_context(request.args)
    prev_id = next_id = None
    if entry_id in ids:
        i = ids.index(entry_id)
        prev_id = ids[i-1] if i > 0 else None
        next_id = ids[i+1] if i < len(ids)-1 else None
    # #2 split: if this bill's properties span >1 entity, offer to split the payment.
    # split_entities = [(entity_code, unit_weight), …]; empty when it's a single entity.
    prop_codes = [c.strip() for c in (entry.get("property_code") or "").split(",") if c.strip()]
    weights = entity_unit_weights(prop_codes)
    split_entities = sorted(weights.items(), key=lambda kv: -kv[1]) if len(weights) > 1 else []
    # Records registered without extraction (e.g. the harvest of manually-
    # filed PDFs) can have blank details the AI can read off the document.
    ai_fillable = has_pdf and any(entry.get(f) in (None, "")
                                  for f, _ in _AI_FILL_FIELDS)
    # Twin records of this same bill (FAIR Plan #51-vs-#77): open twins keep a
    # paid bill on the owed list; a paid twin means an open bill may already be
    # settled. Both deserve a loud banner before anyone pays twice.
    twins = find_invoice_twins(entry) if entry.get("doc_type") == "Bill" else []
    open_twins = [t for t in twins if (t.get("status") or "") not in ("Paid", "Filed")]
    paid_twins = [t for t in twins if (t.get("status") or "") in ("Paid", "Filed")]
    attachments = get_attachments(entry_id)
    # #5 — records that might be this one's original document (or vice versa),
    # for the Link original action. Skip anything already attached here.
    # Gated: find_link_candidates scans the whole table, so only bother when
    # this entry actually carries a signal it could match on -- an entry with
    # no invoice/account number and no (amount + entity) can never match
    # anything anyway (same logic find_link_candidates applies per-row).
    has_match_signal = bool((entry.get("invoice_number") or "").strip()
                             or (entry.get("account_number") or "").strip()
                             or (entry.get("amount") is not None
                                 and (entry.get("entity_code") or "").strip()))
    attached_paths = {a.get("file_path") for a in attachments}
    link_candidates = [c for c in find_link_candidates(entry)
                       if c.get("filed_path") not in attached_paths
                       and c.get("pdf_path") not in attached_paths][:10] \
                      if has_match_signal else []
    # Move-document picker: best filing suggestions first, current folder shown
    # as the field's starting value (reports #25/#37).
    move_candidates, move_default = [], ""
    if has_pdf:
        move_default = os.path.dirname(pdf_on_disk)
        move_candidates = [c for c in filing.suggest_filed_dir(entry, cfg)["candidates"]
                           if os.path.normcase(c) != os.path.normcase(move_default)]
    # Pass-to-teammate: everyone except yourself as recipients, plus this
    # record's pass history (open passes render as sticky notes up top).
    me = session.get("user", "")
    staff_names = [s["name"] for s in cfg.get("staff", []) if s.get("name") != me]
    return render_template("entry_view.html", entry=entry, cfg=cfg, has_pdf=has_pdf,
                           move_candidates=move_candidates, move_default=move_default,
                           open_twins=open_twins, paid_twins=paid_twins,
                           attachments=attachments,
                           link_candidates=link_candidates,
                           staff_names=staff_names,
                           entry_passes=get_passes_for_entry(entry_id),
                           today=date.today().isoformat(),
                           back_url=back_url, back_label=back_label,
                           prev_id=prev_id, next_id=next_id,
                           split_entities=split_entities,
                           payment_splits=get_payment_splits(entry_id),
                           entry_units=get_entry_units(entry_id),
                           nav_qs=_M._nav_qs(request.args),
                           ai_fillable=ai_fillable,
                           msg=request.args.get("msg", ""))


def pass_entry(entry_id):
    """Pass a record to a teammate with a note — the office's hand-them-the-
    paper habit, in-app. Shows up in the recipient's nav badge + dashboard."""
    entry = get_entry(entry_id)
    if not entry:
        return redirect(url_for("log"))
    to_user = (request.form.get("to_user") or "").strip()
    note = (request.form.get("note") or "").strip()
    valid = {s["name"] for s in _M.load_config().get("staff", [])}
    if to_user not in valid:
        return redirect(url_for("view_entry", entry_id=entry_id,
                                msg="Pick who to pass this to."))
    me = session.get("user", "")
    add_record_pass(entry_id, me, to_user, note)
    log_activity(entry_id, f"Passed to {to_user}", me, note)
    return redirect(url_for("view_entry", entry_id=entry_id,
                            msg=f"Passed to {to_user}."))


def move_entry_file(entry_id):
    """Move a record's document to another folder, in-app (reports #25/#37).
    The file move and the record's path update happen together on the server,
    so a move can never orphan the record the way a hand-move in File Explorer
    did (report #46 / record #854). The destination must resolve into one of
    the two allowed trees; folders pasted from another desk (different drive
    letter, or the UNC share) are understood via resolve_client_dir."""
    entry = get_entry(entry_id)
    if not entry:
        return redirect(url_for("log"))

    def back(msg):
        return redirect(url_for("view_entry", entry_id=entry_id, msg=msg))

    src = entry.get("filed_path") or entry.get("pdf_path") or ""
    if not (src and os.path.exists(src)):
        return back("There is no document file on disk to move.")
    cfg = _M.load_config()
    raw = (request.form.get("dest_dir") or "").strip()
    dest_dir = filing.resolve_client_dir(raw, cfg)
    if not dest_dir:
        return back("That folder isn't on the file server — nothing was moved.")
    if request.form.get("rename_convention"):
        # Server filenames are ALL CAPS per the office convention (report #45).
        # The convention name follows the DESTINATION folder's own grammar
        # (incoming / paid / global — FOLDER_NAMING_STANDARDS.md).
        base = os.path.splitext(_M._make_filename(entry, dest_dir))[0].upper()
        dest = os.path.join(dest_dir, base + (os.path.splitext(src)[1] or ".pdf"))
    else:
        dest = os.path.join(dest_dir, os.path.basename(src))
    if os.path.normcase(os.path.normpath(dest)) == os.path.normcase(os.path.normpath(src)):
        return back("The document is already in that folder under that name.")
    if os.path.exists(dest):
        return back("A file with this name already exists in that folder — "
                    "nothing was moved.")
    try:
        os.makedirs(dest_dir, exist_ok=True)
        filing_ledger.announce(dest)
        shutil.move(src, dest)
    except OSError as e:
        return back(f"Move failed: {e}")
    update_entry(entry_id, {"filed_path": dest})
    log_activity(entry_id, "Document moved", session.get("user", ""),
                 f"{src} → {dest}")
    # Copies and receipts living beside the document travel with it — a moved
    # record must not strand its attachments in the old folder (same family as
    # report #46). Best-effort per file; a failed one keeps its old valid path.
    moved_copies = 0
    src_dir = os.path.normcase(os.path.dirname(src))
    for att in get_attachments(entry_id):
        ap = att.get("file_path") or ""
        if not ap or os.path.normcase(os.path.dirname(ap)) != src_dir \
                or not os.path.exists(ap):
            continue
        abase, aext = os.path.splitext(os.path.basename(ap))
        adest = os.path.join(dest_dir, abase + aext)
        c = 1
        while os.path.exists(adest):
            adest = os.path.join(dest_dir, f"{abase} ({c}){aext}")
            c += 1
        try:
            filing_ledger.announce(adest)
            shutil.move(ap, adest)
        except OSError:
            continue
        update_attachment_path(att["id"], adest)
        moved_copies += 1
    if moved_copies:
        log_activity(entry_id, "Document moved", session.get("user", ""),
                     f"{moved_copies} attached cop{'y' if moved_copies == 1 else 'ies'} "
                     f"moved along to {dest_dir}")
        return back(f"Document moved, along with {moved_copies} attached "
                    f"cop{'y' if moved_copies == 1 else 'ies'}.")
    return back("Document moved.")


def pass_done(pass_id):
    """Mark a pass handled. Allowed from the dashboard list or the record."""
    me = session.get("user", "")
    p = get_pass(pass_id)
    if p and mark_pass_done(pass_id, me):
        log_activity(p["entry_id"], "Pass marked done", me,
                     f"was passed to {p['to_user']} by {p['from_user'] or '?'}")
    return redirect(request.referrer or url_for("dashboard"))


def _ai_fill_entry(entry, user=None) -> list:
    """Core of 'Fill missing details with AI': re-read entry's document with
    the classifier and fill only its BLANK fields (vendor/amount/due date/
    invoice #/account #) — a value a person entered or confirmed is never
    overwritten, and nothing on disk is moved or renamed. Caller is responsible
    for confirming the document file exists before calling (classify_document
    is not free). Returns the "Label value" strings it filled — empty list if
    nothing new was found. `user` lets a background caller (no request/session
    context) attribute the activity-log entry; the per-record route leaves it
    None and falls back to the logged-in session."""
    path = entry.get("filed_path") or entry.get("pdf_path") or ""
    result = classify_document(path) or {}
    updates, filled = {}, []
    for f, label in _AI_FILL_FIELDS:
        if entry.get(f) not in (None, ""):
            continue                        # never overwrite an existing value
        v = result.get(f)
        if f == "amount":
            try:
                v = float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                v = None
        if v in (None, ""):
            continue
        updates[f] = v
        filled.append(f"{label} {v}")
    if updates:
        update_entry(entry["id"], updates)
        log_activity(entry["id"], "AI filled missing details",
                     user if user is not None else session.get("user", ""),
                     "; ".join(filled))
    return filled


def entry_ai_fill(entry_id):
    """Fill a logged record's BLANK details by re-reading its document with
    the classifier — for records that were registered without extraction.
    Only empty fields are filled: a value a person entered or confirmed is
    never overwritten, and nothing on disk is moved or renamed."""
    entry = get_entry(entry_id)
    if not entry:
        return redirect(url_for("log"))
    path = entry.get("filed_path") or entry.get("pdf_path") or ""
    if not (path and os.path.exists(path)):
        return redirect(url_for("view_entry", entry_id=entry_id,
                                msg="No document file on disk to read."))
    try:
        filled = _ai_fill_entry(entry)
    except Exception as e:
        return redirect(url_for("view_entry", entry_id=entry_id,
                                msg=f"The AI could not read the document: {e}"))
    if filled:
        msg = ("AI read the document and filled: " + ", ".join(filled)
               + ". Fields that already had values were not touched.")
    else:
        msg = "The AI could not read anything new off the document."
    return redirect(url_for("view_entry", entry_id=entry_id, msg=msg))


# ── Bulk AI fill for Harvest-Bot records (admin-triggered, background) ────────
# The office's manual-filing-cabinet harvest registered ~80 bills with no AI
# extraction (processed_by='Harvest-Bot'), leaving blanks like amount/due date.
# This runs the same fill-the-blanks pass as the button above over all of them
# in one background sweep, one API call per record, so nobody has to click
# through each one by hand.

def _harvest_ai_fill_candidates():
    """Harvest-Bot bills that still have at least one blank AI-fill field —
    the population the bulk run offers to fix. File existence isn't checked
    here; the worker checks per record so it can count file-missing separately."""
    return [e for e in get_all_entries({"processed_by": "Harvest-Bot", "doc_type": "Bill"})
            if any(e.get(f) in (None, "") for f, _ in _AI_FILL_FIELDS)]


def _ai_fill_harvest_worker(entry_ids, user=""):
    """Background worker: run _ai_fill_entry over each given record. Re-fetches
    the record fresh (in case it changed since the route queried), skips it if
    its document file is gone, and swallows any per-record exception (a bad PDF
    or a flaky API call must not sink the whole run) so the sweep always
    finishes and reports a summary. Caller must have already claimed the
    'harvest_ai_fill' job slot via queue_state.try_start_job; this releases it
    when done."""
    n_filled = n_nothing = n_missing = n_errors = 0
    try:
        for entry_id in entry_ids:
            entry = get_entry(entry_id)
            if not entry:
                continue                    # deleted since the run started
            path = entry.get("filed_path") or entry.get("pdf_path") or ""
            if not (path and os.path.exists(path)):
                n_missing += 1
                continue
            try:
                filled = _ai_fill_entry(entry, user=user)
            except Exception as e:
                n_errors += 1
                print(f"[Bulk AI fill] entry {entry_id} failed: {e}")
                continue
            if filled:
                n_filled += 1
            else:
                n_nothing += 1
            time.sleep(0.3)              # rate courtesy — one API call each
    finally:
        queue_state.finish_job("harvest_ai_fill")
    summary = (f"{n_filled} filled, {n_nothing} had nothing new, "
               f"{n_missing} missing their file, {n_errors} errored "
               f"(of {len(entry_ids)} harvested records)")
    log_activity(None, "Bulk AI fill", user, summary)
    print(f"[Bulk AI fill] Done: {summary}.")


def ai_fill_harvest():
    """Kick off the bulk Harvest-Bot AI-fill sweep on a background thread —
    same rules as the single-record button (blanks only, nothing overwritten,
    nothing on disk touched), just run over every Harvest-Bot bill at once."""
    if not queue_state.try_start_job("harvest_ai_fill"):
        return redirect(url_for("settings",
            err="A bulk AI-fill run is already in progress — check back in a bit.",
            _anchor="tab=app"))
    candidates = _harvest_ai_fill_candidates()
    if not candidates:
        queue_state.finish_job("harvest_ai_fill")   # no thread will start — release the claim
        return redirect(url_for("settings",
            msg="No harvested records have blank details to fill.", _anchor="tab=app"))
    entry_ids = [e["id"] for e in candidates]
    user = session.get("user", "")
    threading.Thread(target=_ai_fill_harvest_worker, args=(entry_ids, user), daemon=True).start()
    return redirect(url_for("settings",
        msg=f"Filling blanks on {len(entry_ids)} harvested record"
            f"{'s' if len(entry_ids) != 1 else ''} in the background — "
            "details land in each record's activity log.",
        _anchor="tab=app"))


def toggle_important(entry_id):
    """Manually flag (or unflag) a record as important. Flagged records join
    Legal/Correspondence on the dashboard's needs-attention list regardless of
    doc_type — for the specific bill that needs eyes even though it isn't
    legal. Doc-type-driven importance needs no flag and is unaffected."""
    entry = get_entry(entry_id)
    if not entry:
        return redirect(url_for("log"))
    flag = 0 if entry.get("important") else 1
    changes = {"important": flag}
    if flag:
        # Fresh pin: age from now, and re-pin if it had been taken down.
        changes["flagged_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        changes["acknowledged_at"] = ""
        changes["acknowledged_by"] = ""
    update_entry(entry_id, changes)
    log_activity(entry_id, "Flagged important" if flag else "Important flag removed",
                 session.get("user", ""))
    return redirect(f"/entry/{entry_id}/view{_M._nav_qs(request.args)}")


def turn_into_bill(entry_id):
    """#5 — a record logged under some other doc_type (e.g. a harvested payment
    receipt, or a misclassified scan) turns out to actually BE an unpaid bill.
    Reclassify it in place rather than deleting and re-registering, so its
    attachments/activity history stay put."""
    entry = get_entry(entry_id)
    if not entry:
        return redirect(url_for("log"))
    if entry.get("doc_type") != "Bill":
        was = entry.get("doc_type") or "(blank)"
        update_entry(entry_id, {"doc_type": "Bill"})
        log_activity(entry_id, "Turned into bill", session.get("user", ""),
                     f"was: {was}")
    return redirect(f"/entry/{entry_id}/view{_M._nav_qs(request.args)}")


def mark_done(entry_id):
    """Report #27 (Joshua) — the non-bill counterpart of Mark Paid. A bank
    statement, a receipt or a blank lease is never "paid"; it is just handled.
    This files the record so it drops off the open lists, without inventing a
    payment date/amount for a document that has none.

    Deliberately refuses on Bills: those go through the payment form so a real
    paid date, amount and method are captured (the FAIR-Plan lesson — a status
    flipped by hand is a payment nobody can reconcile later)."""
    entry = get_entry(entry_id)
    if not entry:
        return redirect(url_for("log"))
    nav = _M._nav_qs(request.args)
    if (entry.get("doc_type") or "") == "Bill":
        return redirect(f"/entry/{entry_id}/view{nav}{'&' if nav else '?'}pay=1")
    if entry.get("status") != "Filed":
        update_entry(entry_id, {"status": "Filed"})
        log_activity(entry_id, "Marked done", session.get("user", ""),
                     f"{entry.get('doc_type') or 'Document'} — filed, not a bill")
    return redirect(f"/entry/{entry_id}/view{nav}")


def toggle_no_original(entry_id):
    """#5 — mark 'searched, no source invoice on file' (or clear the flag).
    Distinct from simply not having attachments: this says a person looked and
    confirmed there's nothing to find, so the gap stops reading as an open item."""
    entry = get_entry(entry_id)
    if not entry:
        return redirect(url_for("log"))
    flag = not entry.get("no_original_on_file")
    set_no_original(entry_id, flag, session.get("user", ""))
    log_activity(entry_id, "No original on file" if flag else "No-original flag cleared",
                 session.get("user", ""))
    return redirect(f"/entry/{entry_id}/view{_M._nav_qs(request.args)}")


def link_original(entry_id, other_id):
    """#5 — a person confirms `other_id` is this record's original (or vice
    versa, e.g. entry is a payment receipt and other_id is the underlying
    bill). Attaches the other record's file here, the same way the harvest's
    automatic LINK does, so it shows under 'Copies on file'. Clears any stale
    no-original flag now that one's been found."""
    entry = get_entry(entry_id)
    other = get_entry(other_id)
    if not entry or not other:
        return redirect(url_for("log"))
    path = other.get("filed_path") or other.get("pdf_path") or ""
    if path and os.path.exists(path):
        add_attachment(entry_id, path, f"Linked original (#{other_id})",
                       os.path.basename(path), session.get("user", ""))
        if entry.get("no_original_on_file"):
            set_no_original(entry_id, False)
        log_activity(entry_id, "Linked to original", session.get("user", ""),
                     f"#{other_id} — {other.get('vendor') or ''}")
    return redirect(f"/entry/{entry_id}/view{_M._nav_qs(request.args)}")


def entry_pdf(entry_id):
    entry = get_entry(entry_id)
    if not entry:
        return "Not found", 404
    path = entry.get("filed_path") or entry.get("pdf_path") or ""
    if not path or not os.path.exists(path):
        return "File not found on disk", 404
    return send_file(path)


def entry_packet_pdf(entry_id):
    """Reports #55/#57 — the document with its source email typeset behind it,
    as one printable PDF (Karen pays a bill by printing it with the email).
    Falls back to the bare document when no email is stored (pre-July-2026
    records never captured one) or the packet can't be built."""
    entry = get_entry(entry_id)
    if not entry:
        return "Not found", 404
    path = entry.get("filed_path") or entry.get("pdf_path") or ""
    if not path or not os.path.exists(path):
        return "File not found on disk", 404
    data = email_pdf.packet_pdf_bytes(path, entry)
    if data is None:
        return redirect(url_for("entry_pdf", entry_id=entry_id))
    name = f"{entry.get('record_no') or entry_id} with email.pdf"
    return send_file(io.BytesIO(data), mimetype="application/pdf",
                     download_name=name)


def attach_receipt(entry_id):
    """Tier 4 #4 — attach a scanned/photographed receipt to an existing bill. Files
    it next to the record's primary document (or a RECEIPTS folder) and records it as
    a 'Receipt' attachment, so it shows under 'Copies on file'."""
    entry = get_entry(entry_id)
    back = url_for("view_entry", entry_id=entry_id) + _M._nav_qs(request.args)
    if not entry:
        return redirect(url_for("log"))
    f = request.files.get("receipt")
    if f and f.filename:
        _M._store_receipt_upload(entry, f)
    return redirect(back)


def attachment_pdf(att_id):
    att = get_attachment(att_id)
    if not att:
        return "Not found", 404
    path = att.get("file_path") or ""
    if not path or not os.path.exists(path):
        return "File not found on disk", 404
    return send_file(path)


def _retire_copy_file(att: dict):
    """Set a removed copy's file aside into a recoverable _Removed Copies folder
    (never hard-delete — the copy is byte-identical to the primary, but the
    pipeline never destroys a document). Best-effort; a missing file is fine."""
    p = (att or {}).get("file_path") or ""
    if p and os.path.exists(p):
        _M._move_aside(p, "_Removed Copies")


def remove_attachment(att_id):
    """Remove a single duplicate copy from a record's 'Copies on file'. The row
    is deleted and its file is set aside (recoverable), leaving the primary
    document and the record itself untouched."""
    att = delete_attachment(att_id)
    if att:
        _retire_copy_file(att)
        log_activity(att["entry_id"], "Duplicate copy removed",
                     session.get("user", ""), att.get("original_name") or "")
        return redirect(f"/entry/{att['entry_id']}/view{_M._nav_qs(request.args)}")
    return redirect(url_for("log"))


def remove_all_copies(entry_id):
    """Remove every duplicate copy attached to a record in one click — for a
    record that accumulated many byte-identical duplicates (e.g. the same scan
    ingested repeatedly). The primary document and the record stay; each copy's
    file is set aside, recoverable."""
    removed = delete_attachments(entry_id)
    for att in removed:
        _retire_copy_file(att)
    if removed:
        log_activity(entry_id, "Duplicate copies removed", session.get("user", ""),
                     f"{len(removed)} cop{'y' if len(removed) == 1 else 'ies'}")
    return redirect(f"/entry/{entry_id}/view{_M._nav_qs(request.args)}")


def delete_entry_route(entry_id):
    victim = get_entry(entry_id)
    label = (victim.get("vendor") or victim.get("doc_type") or "record") if victim else "record"
    removed = delete_attachments(entry_id)
    for att in removed:
        _retire_copy_file(att)
    delete_entry(entry_id)
    log_activity(entry_id, "Deleted", session.get("user",""), label)
    return redirect(url_for("log"))


def update_status(entry_id):
    new_status = (request.get_json(silent=True) or {}).get("status")
    if new_status not in _VALID_STATUSES:
        return jsonify({"ok": False, "error": "invalid status"}), 400
    update_entry(entry_id, {"status": new_status})
    log_activity(entry_id, f"Status changed to {new_status}", session.get("user",""))
    return jsonify({"ok": True})
