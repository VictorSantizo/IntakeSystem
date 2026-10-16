"""The review queue and the confirm path — /review and everything under it,
plus the printable batch cover sheet — extracted from app.py in the modular
split (GH #6). Same pattern as routes_karen.py (see its docstring): original
endpoint names via add_url_rule, app-module helpers reached through _M so
tests' monkeypatching keeps working.

This is the chunk the split was really about: the queue is where a document
becomes a record. Everything here funnels into two write paths that stay in
the composition root — _M._finalize_entry (insert + file the document) and the
attach path (copy onto an existing record) — and every one of them claims the
scan first (queue_state.claim_scan) so a double-click or a second reviewer
can't log the same document twice.

Queue state comes from queue_state, NOT from _M: the whole point of moving it
out first (design q1, option b) was that a module wanting the queue no longer
has to reach back into app.py for it. App-level policy — what counts as
overdue, what a duplicate looks like, what filename a document gets — still
lives in app.py and is reached through _M.
"""
import io
import json
import os
import re
import shutil
import threading
from datetime import date, datetime, timedelta

from flask import (jsonify, redirect, render_template, request, send_file,
                   session, url_for)

import doc_types
import email_pdf
import filing
import queue_state
from database import (add_attachment, count_auto_filed_since, find_duplicates,
                      find_similar_vendor, get_entries_by_hash, get_entry,
                      get_filtered_count, get_hash_dupe_groups, log_activity,
                      save_pending_scan)

_M = None   # the app module (composition root); set by register()

# What the Upload button accepts: PDFs plus the image formats classify_document
# can read. Word docs deliberately rejected until #52's LibreOffice converter lands.
_UPLOAD_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff")

# Which comparison labels can be boxed ON the documents themselves (these are
# the fields the hover-highlighter already knows how to locate in a text layer).
_HL_LABELS = {"Vendor": "vendor", "Amount": "amount", "Due date": "due_date",
              "Invoice #": "invoice_number", "Account #": "account_number"}


def register(app, appmod):
    global _M
    _M = appmod
    adm = appmod.admin_required
    r = app.add_url_rule
    r("/review/upload", "review_upload", review_upload, methods=["POST"])
    r("/review", "review_queue", review_queue)
    r("/review/reclassify", "review_reclassify", adm(review_reclassify), methods=["POST"])
    r("/review/triage", "review_triage", review_triage)
    r("/review/triage/resolve_certain", "triage_resolve_certain",
      triage_resolve_certain, methods=["POST"])
    r("/review/triage/set_aside_near/<int:kept_sid>", "triage_set_aside_near",
      triage_set_aside_near, methods=["POST"])
    r("/review/triage/attach_selected", "triage_attach_selected",
      triage_attach_selected, methods=["POST"])
    r("/review/legacy_dupes", "legacy_dupes", adm(legacy_dupes))
    r("/review/legacy_dupes/merge", "legacy_dupes_merge", adm(legacy_dupes_merge),
      methods=["POST"])
    r("/review/legacy_dupes/merge_all", "legacy_dupes_merge_all",
      adm(legacy_dupes_merge_all), methods=["POST"])
    r("/review/bulk_confirm", "bulk_confirm", bulk_confirm, methods=["POST"])
    r("/review/print", "review_print", review_print, methods=["POST"])
    # The <int:scan_id> rules come last so the literal paths above always win.
    r("/review/<int:scan_id>", "review_item", review_item)
    r("/review/<int:scan_id>/packet.pdf", "review_packet_pdf", review_packet_pdf)
    r("/review/<int:scan_id>/skip", "skip_item", skip_item, methods=["POST"])
    r("/review/<int:scan_id>/set_date", "set_scan_date", set_scan_date, methods=["POST"])
    r("/review/<int:scan_id>/compare/<int:entry_id>", "review_compare", review_compare)
    r("/review/<int:scan_id>/attach/<int:entry_id>", "attach_duplicate",
      attach_duplicate, methods=["POST"])
    r("/review/<int:scan_id>/confirm", "confirm_item", confirm_item, methods=["POST"])
    r("/coversheet", "coversheet", coversheet)


def review_upload():
    """Upload documents straight into intake (Joshua 2026-08-03; the queue half
    of report #50). Files are saved under the watched scan folder's Uploads
    subfolder, then fed through the SAME enqueue_scan path as a physical scan
    — split, classify (OCR/vision), hash dedup, auto-confirm — so an upload
    can't bypass any guard. mail_type lands as 'Upload' so the source column
    stays honest (#15). Synchronous on purpose: when the redirect returns, the
    batch page already shows the result."""
    files = [f for f in request.files.getlist("documents") if f and f.filename]
    if not files:
        return redirect(url_for("review_queue", msg="No files chosen."))
    cfg = _M.load_config()
    up_dir = os.path.join(cfg.get("scan_folder", ""), "Uploads")
    os.makedirs(up_dir, exist_ok=True)
    saved, skipped = [], []
    for f in files:
        safe = re.sub(r'[\\/:*?"<>|]', "", os.path.basename(f.filename)).strip()
        base, ext = os.path.splitext(safe)
        if ext.lower() not in _UPLOAD_EXTS or not base:
            skipped.append(f.filename)
            continue
        dest, c = os.path.join(up_dir, safe), 1
        while os.path.exists(dest):
            dest = os.path.join(up_dir, f"{base} ({c}){ext}")
            c += 1
        try:
            f.save(dest)
            saved.append(dest)
        except Exception as e:
            print(f"[Upload] could not save {safe}: {e}")
            skipped.append(f.filename)
    before = {sid for sid, _ in queue_state.queue_items()}
    for p in saved:
        _M.enqueue_scan(p, source="Upload")
    fresh = [(sid, d) for sid, d in queue_state.queue_items() if sid not in before]
    queued, handled = len(fresh), len(saved) - len(fresh)
    batch = next((d.get("batch_number") for _, d in fresh if d.get("batch_number")), None)
    log_activity(None, "Documents uploaded", session.get("user", ""),
                 f"{len(saved)} file(s): {queued} queued, {handled} auto-handled")
    msg = f"Uploaded {len(saved)} file{'s' if len(saved) != 1 else ''}: {queued} in the queue below"
    if handled:
        msg += f", {handled} handled automatically (already on file, or auto-confirmed)"
    if skipped:
        msg += f". Skipped {len(skipped)} unsupported file(s) — PDFs and images only"
    args = {"msg": msg}
    if batch:
        args["batch"] = batch
    return redirect(url_for("review_queue", **args))


def review_queue():
    cfg = _M.load_config()
    today_iso = date.today().isoformat()
    items = queue_state.queue_items()  # [(scan_id, data), ...]
    urgent_ids = {sid for sid, d in items if _M._scan_is_overdue(d)}
    # Past-due bills float to the top, then AI-flagged-important documents.
    items.sort(key=lambda kv: (kv[0] not in urgent_ids,
                               not kv[1].get("important"), kv[0]))
    # Bills first, housekeeping apart (Joshua 8/6, from the 8/3 training
    # session): the queue mixes "bills that need paying" with "records that
    # need tidying", and Karen experiences the second as homework. Bills get
    # their own section — past-due and flagged first, then OLDEST RECEIVED
    # first, because that is the order Karen pays in. Everything else
    # (statements, notices, correspondence) collapses below it.
    # doc_types.is_payable, not a literal "Bill" here: the same question is
    # asked by the past-due flag (#111), and the two answers must not drift.
    bill_items = [(s, d) for s, d in items if doc_types.is_payable(d.get("doc_type"))]
    other_items = [(s, d) for s, d in items if not doc_types.is_payable(d.get("doc_type"))]
    bill_items.sort(key=lambda kv: (kv[0] not in urgent_ids,
                                    not kv[1].get("important"),
                                    kv[1].get("received_date") or "9999-99-99",
                                    kv[0]))
    # Outage banner: items classified by the regex fallback are keyword guesses.
    ai_failed = [d for _, d in items if _M._AI_UNAVAILABLE_MARK in (d.get("notes") or "")]
    ai_fail_reason = ""
    if ai_failed:
        n = ai_failed[0].get("notes") or ""
        m = re.search(r"'message':\s*'([^']+)'", n)
        start = n.find(_M._AI_UNAVAILABLE_MARK)
        ai_fail_reason = m.group(1) if m else n[start:start + 160]
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    return render_template("review_queue.html", items=items, cfg=cfg,
                           bill_items=bill_items, other_items=other_items,
                           today=today_iso, urgent_ids=urgent_ids,
                           filtered_count=get_filtered_count(),
                           ai_failed_count=len(ai_failed),
                           ai_fail_reason=ai_fail_reason,
                           reclassify_running=queue_state.job_running("reclassify"),
                           auto_filed_recent=count_auto_filed_since(week_ago),
                           msg=request.args.get("msg"))


def review_reclassify():
    """Re-run AI classification on the queue items that fell back to the keyword
    regex during an API outage. Admin clicks this AFTER fixing the API (key or
    billing); the work runs on a background thread, one API call per item."""
    scope = (request.form.get("scope") or "outage").strip().lower()
    if scope == "all":
        # Stale-queue sweep: items classified under OLDER rules stay stale until
        # re-run — this pushes the whole queue through the current classifier,
        # junk filter, and auto-confirm. One API call per item.
        sids = [sid for sid, _ in queue_state.queue_items()]
    else:
        sids = _M._ai_outage_sids()
    if not sids:
        return redirect(url_for("review_queue", msg="No items need re-classification."))
    if not queue_state.try_start_job("reclassify"):
        return redirect(url_for("review_queue",
                        msg="A re-classification pass is already running — refresh in a minute."))
    threading.Thread(target=_M._reclassify_items, args=(sids,), daemon=True).start()
    return redirect(url_for("review_queue",
                    msg=f"Re-running AI on {len(sids)} document{'s' if len(sids) != 1 else ''} "
                        "in the background — junk drains to Filtered and predictable bills "
                        "auto-file. Refresh in a minute."))


def _email_header_date_iso(raw):
    """The email's own Date header as a local ISO date ('' when unparseable) —
    the same rule the poller stamps received_date with. Karen's rule from the
    2026-08-03 session (reports #56/#49): 'whatever the email date is, that's
    the date received', so the review form shows this next to the received-date
    field and offers it as a one-click correction."""
    if not raw:
        return ""
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return ""
        try:
            dt = dt.astimezone()
        except Exception:
            pass
        return dt.date().isoformat()
    except Exception:
        return ""


def review_packet_pdf(scan_id):
    """Reports #55/#57, at the queue stage — the queued document with its
    source email typeset behind it, one printable PDF. Falls back to the bare
    file when no email rode in with the scan."""
    data = queue_state.get_scan(scan_id)
    if not data:
        return "Not found", 404
    path = data.get("pdf_path") or ""
    if not path or not os.path.exists(path):
        return "File not found on disk", 404
    pdf = email_pdf.packet_pdf_bytes(path, data)
    if pdf is None:
        return send_file(path)
    name = os.path.splitext(os.path.basename(path))[0] + " with email.pdf"
    return send_file(io.BytesIO(pdf), mimetype="application/pdf",
                     download_name=name)


def review_item(scan_id):
    cfg = _M.load_config()
    data = queue_state.get_scan(scan_id)
    if not data:
        return redirect(url_for("review_queue"))
    if not data.get("received_date"):
        # Legacy queue row (queued before ingest stamped a received date): the
        # form used to default the date to TODAY, which is how four stranded
        # June scans became August mail (the 08. AUGUST incident). The scan
        # file's own date is the honest default; marked assumed so the
        # provenance chip and the year-sanity guard make a person look.
        fb = _M._fallback_received_date(data.get("pdf_path"))
        if fb != date.today().isoformat():
            data["received_date"] = fb
            data["received_date_assumed"] = True
            data["received_date_source"] = "file-date"
    ids = _M._filtered_ids(request.args)
    if scan_id not in ids:           # filter no longer includes this one — fall back to full queue
        ids = sorted(sid for sid, _ in queue_state.queue_items())
    idx = ids.index(scan_id) if scan_id in ids else 0
    prev_id = ids[idx - 1] if idx > 0 else None
    next_id = ids[idx + 1] if idx < len(ids) - 1 else None
    # Flag likely duplicates up front, using the AI-extracted values. The confirm
    # step re-checks against whatever the user finally submits.
    dupes = find_duplicates(data.get("vendor"), data.get("amount"),
                            data.get("due_date"), data.get("invoice_number"),
                            data.get("pdf_path"), data.get("received_date"))
    dup_cmps = {d["id"]: _M._dup_comparison(data, d) for d in dupes}
    dupes.sort(key=lambda d: -dup_cmps[d["id"]]["verdict"]["score"])
    # "File as" proposal (reports #23/#18): filename in Karen's convention +
    # a folder in her archive, from the AI-extracted values. If the reviewer
    # leaves them untouched, confirm_item re-derives both from the FINAL form
    # values, so correcting a field never files under a stale name.
    s_entry, _s_props = _M._entry_from_scan_data(data, cfg, session.get("user", ""))
    suggestion = filing.suggest_filed_dir(s_entry, cfg)
    # The proposed name follows the proposed FOLDER's own grammar (incoming /
    # paid / global — FOLDER_NAMING_STANDARDS.md). If the reviewer picks a
    # different folder, confirm re-derives the name against that final folder.
    suggested_name = _M._make_filename(s_entry, suggestion.get("dir"))
    if data.get("hand_filed") and data.get("pdf_path"):
        # A hand-filed pickup (cabinet_watch) defaults to staying where — and
        # named how — the filer put it; the convention suggestion is offered
        # as an alternative in the candidates list.
        cur_dir = os.path.dirname(data["pdf_path"])
        cur_name = os.path.splitext(os.path.basename(data["pdf_path"]))[0]
        cands = [cur_dir] + [c for c in suggestion.get("candidates", [])
                             if os.path.normcase(c) != os.path.normcase(cur_dir)]
        suggestion = {"dir": cur_dir, "source": "hand-filed", "candidates": cands}
        suggested_name = cur_name
    # How many QUEUED files share this batch — the apply-to-batch date prompt
    # only makes sense when changing the date could touch more than this file.
    batch_no = data.get("batch_number") or ""
    batch_count = (sum(1 for _s, d in queue_state.queue_items()
                       if (d.get("batch_number") or "") == batch_no)
                   if batch_no else 1)
    # "What just happened" feedback: confirm_item redirects here with
    # ?logged=<entry id> so the next item can show a green "Logged ✓" toast —
    # the silent auto-advance read as "did it save? where did it go?" (Karen,
    # training session 2026-08-03).
    logged_entry = None
    try:
        logged_id = int(request.args.get("logged", ""))
        logged_entry = get_entry(logged_id)
    except (ValueError, TypeError):
        pass
    return render_template("review.html", data=data, scan_id=scan_id, cfg=cfg,
                           today=date.today().isoformat(),
                           prev_id=prev_id, next_id=next_id, dupes=dupes,
                           dup_cmps=dup_cmps,
                           email_received_iso=_email_header_date_iso(
                               data.get("email_date")),
                           logged_entry=logged_entry,
                           logged_twins=request.args.get("twins", ""),
                           overdue=_M._scan_is_overdue(data),
                           queue_pos=idx+1, queue_total=len(ids),
                           filter_qs=_M._filter_qs(request.args),
                           suggested_name=suggested_name,
                           filing_suggestion=suggestion,
                           batch_count=batch_count)


def skip_item(scan_id):
    """Discard a queued item. The file is moved to a _Skipped folder beside it —
    recoverable, but OUT of the watched folder. (It used to stay in place, so
    every startup sweep / Check for new items silently brought the item back.)"""
    data = queue_state.claim_scan(scan_id)
    if data:
        try:
            p = data.get("pdf_path")
            if p and os.path.exists(p):
                _M._move_aside(p, "_Skipped")
            queue_state.queue_remove(scan_id)
        finally:
            queue_state.release_scan(scan_id)
    # The in-tray's "set aside" uses this same route; send it back there.
    if request.form.get("return_to") == "intray":
        return redirect("/intray")
    fqs = _M._filter_qs(request.args)
    ids = _M._filtered_ids(request.args)
    if ids:
        return redirect(url_for("review_item", scan_id=ids[0]) + fqs)
    return redirect(url_for("review_queue") + fqs)


def set_scan_date(scan_id):
    """Override a queued scan's received/scanned date — for this file only, or for
    every file in its batch (the date-picker backup to the cover sheet)."""
    field = request.form.get("field", "")
    value = request.form.get("value", "").strip()
    scope = request.form.get("scope", "file")
    if field not in ("received_date", "scanned_date"):
        return jsonify(ok=False, error="invalid field"), 400
    try:
        value = date.fromisoformat(value).isoformat()
    except ValueError:
        return jsonify(ok=False, error="invalid date"), 400
    data = queue_state.get_scan(scan_id)
    if not data:
        return jsonify(ok=False, error="not found"), 404
    batch = data.get("batch_number")
    if scope == "batch" and batch:
        targets = [sid for sid, d in queue_state.queue_items()
                   if d.get("batch_number") == batch]
    else:
        targets = [scan_id]
    for sid in targets:
        d = queue_state.get_scan(sid)
        if d is None:
            continue
        d[field] = value
        d.pop("received_date_assumed", None)
        try:
            save_pending_scan(sid, json.dumps(d))
        except Exception as e:
            print(f"[App] set_date persist failed for {sid}: {e}")
    return jsonify(ok=True, updated=len(targets), scope=scope, batch=batch)


def coversheet():
    """Printable batch cover sheet. Karen prints one per folder, writes the
    received date, and places it on top before scanning."""
    return render_template("coversheet.html", today=date.today())


# ── Duplicate triage (bulk cleanup of a duplicate-heavy queue) ─────────────────
# The grouping itself (_triage_groups) stays in app.py: the in-tray paper view
# reads the same groups through _M, and route module reaching into route module
# is a coupling this split has deliberately avoided.

def review_triage():
    certain, queue_dups, probable, near_queue = _M._triage_groups()
    total = queue_state.queue_size()
    near_members = sum(1 + len(g["extras"]) for g in near_queue)
    rest = total - len(certain) - len(queue_dups) - len(probable) - near_members
    return render_template("triage.html", certain=certain, queue_dups=queue_dups,
                           probable=probable, near_queue=near_queue,
                           rest=rest, total=total, msg=request.args.get("msg", ""))


def triage_resolve_certain():
    """Bulk-resolve the byte-identical groups. Recomputed server-side from the
    CURRENT queue (never from the submitted page), so a stale tab can't attach
    the wrong thing. Only exact-hash groups are ever bulk-resolved."""
    certain, queue_dups, _, _ = _M._triage_groups()
    attached = aside = 0
    for item in certain:
        sid = item["sid"]
        d = queue_state.claim_scan(sid)
        if not d:
            continue
        try:
            p = d.get("pdf_path") or ""
            if p and os.path.exists(p):
                _M._auto_attach_copy(item["entry"], p, d.get("mail_type", "Physical Mail"),
                                     by=session.get("user", ""))
            queue_state.queue_remove(sid)
            attached += 1
        finally:
            queue_state.release_scan(sid)
    for item in queue_dups:
        sid = item["sid"]
        d = queue_state.claim_scan(sid)
        if not d:
            continue
        try:
            p = d.get("pdf_path") or ""
            if p and os.path.exists(p):
                _M._move_aside(p, "_Duplicates")
            queue_state.queue_remove(sid)
            aside += 1
        finally:
            queue_state.release_scan(sid)
    msg = f"Filed {attached} exact duplicate(s) as copies on their records."
    if aside:
        msg += f" Set aside {aside} repeat scan(s)."
    return redirect(url_for("review_queue", msg=msg))


def triage_set_aside_near(kept_sid):
    """Set aside the repeat copies of one same-bill-queued-twice group, keeping
    the earliest-received copy in the queue. The group is recomputed server-side
    from the CURRENT queue and addressed by the kept item's scan id, so a stale
    tab can't set aside the wrong items. Files move to _Duplicates (recoverable);
    nothing is deleted."""
    _, _, _, near_queue = _M._triage_groups()
    group = next((g for g in near_queue if g["kept"]["sid"] == kept_sid), None)
    if not group:
        return redirect(url_for("review_triage",
                                msg="That group changed or was already handled — nothing was set aside."))
    aside = 0
    for it in group["extras"]:
        d = queue_state.claim_scan(it["sid"])
        if not d:
            continue
        try:
            p = d.get("pdf_path") or ""
            if p and os.path.exists(p):
                _M._move_aside(p, "_Duplicates")
            queue_state.queue_remove(it["sid"])
            aside += 1
        finally:
            queue_state.release_scan(it["sid"])
    return redirect(url_for("review_triage",
                            msg=f"Set aside {aside} repeat cop{'ies' if aside != 1 else 'y'}; "
                                f"kept the earliest for review."))


def triage_attach_selected():
    """Bulk-attach the SELECTED likely-duplicate rows, each to its own best
    match. A person ticked every row, so this is a human decision applied in
    bulk — not auto-resolution — and attaching is reversible (the copy stays
    on the record, the file is never deleted). Matches are recomputed
    server-side from the CURRENT queue, so a stale tab can't attach the wrong
    thing; a selected row whose duplicate signals have vanished (or whose
    file is missing on disk) is left in the queue, never guessed at."""
    wanted = set()
    for s in request.form.getlist("sids"):
        try:
            wanted.add(int(s))
        except ValueError:
            continue
    _, _, probable, _ = _M._triage_groups()
    attached = 0
    for it in probable:
        if it["sid"] not in wanted:
            continue
        d = queue_state.claim_scan(it["sid"])
        if not d:
            continue
        try:
            p = d.get("pdf_path") or ""
            if p and os.path.exists(p):
                _M._auto_attach_copy(it["matches"][0], p,
                                     d.get("mail_type", "Physical Mail"),
                                     by=session.get("user", ""))
                queue_state.queue_remove(it["sid"])
                attached += 1
        finally:
            queue_state.release_scan(it["sid"])
    msg = f"Attached {attached} selected duplicate(s) to their records."
    left = len(wanted) - attached
    if left > 0:
        msg += (f" {left} selected item(s) were left in the queue — their "
                f"duplicate signals changed or their file is missing.")
    return redirect(url_for("review_triage", msg=msg))


# ── Legacy duplicate merge (double-logged records from before dedup) ──────────
# Before 2026-07-07 there was no ingest-time deduplication, so the same
# document could be confirmed twice and live on as two records — inflating the
# owed totals. These tools fold each byte-identical group into ONE record.
# Documents are never touched on disk; the absorbed record's file is kept
# listed on the survivor as a copy, and everything is activity-logged.
# The merge machinery itself stays in app.py — the server-side CLI merges
# groups through the same _merge_dupe_group.

def legacy_dupes():
    groups = get_hash_dupe_groups()
    view = [{"records": g, "keeper": _M._pick_keeper(g),
             "conflict": _M._merge_conflict(g), "hash": g[0]["file_hash"]}
            for g in groups]
    safe = sum(1 for v in view if not v["conflict"])
    return render_template("legacy_dupes.html", groups=view, safe_count=safe,
                           msg=request.args.get("msg", ""))


def legacy_dupes_merge():
    """Merge ONE group, recomputed server-side by its document hash so a stale
    tab can't merge the wrong records."""
    h = (request.form.get("hash") or "").strip()
    group = get_entries_by_hash(h)
    if len(group) < 2:
        return redirect(url_for("legacy_dupes",
                                msg="That group is already merged."))
    if _M._merge_conflict(group):
        return redirect(url_for("legacy_dupes",
                                msg="Not merged — the copies carry different "
                                    "payments; review them by hand."))
    kid = _M._merge_dupe_group(group, by=session.get("user", ""))
    return redirect(url_for("legacy_dupes",
                            msg=f"Merged {len(group)} records into #{kid}."))


def legacy_dupes_merge_all():
    """Merge every conflict-free group in one click. Conflicted groups (the
    possible double-payments) are left alone and stay listed."""
    merged = skipped = 0
    for g in get_hash_dupe_groups():
        if _M._merge_conflict(g):
            skipped += 1
            continue
        _M._merge_dupe_group(g, by=session.get("user", ""))
        merged += 1
    msg = f"Merged {merged} duplicate group{'s' if merged != 1 else ''}."
    if skipped:
        msg += (f" Left {skipped} group{'s' if skipped != 1 else ''} with "
                f"conflicting payment details for hand review.")
    return redirect(url_for("legacy_dupes", msg=msg))


def review_compare(scan_id, entry_id):
    """Visual duplicate check: the queued scan and the logged record rendered
    side by side, with the compared characteristics boxed on both documents —
    green where they agree, red where they meaningfully differ, dashed grey
    where the difference is normal for a second copy. Both full server paths
    are shown so the manual process can go straight to the files."""
    data = queue_state.get_scan(scan_id)
    entry = get_entry(entry_id)
    if not data or not entry:
        return redirect(url_for("review_queue"))
    cmp = _M._dup_comparison(data, entry)
    box_states = {}
    for r in cmp["rows"]:
        f = _HL_LABELS.get(r["label"])
        if not f:
            continue
        if r["state"] == "same":
            box_states[f] = "same"
        elif r["state"] == "diff" and r["signal"] in ("strong", "medium"):
            box_states[f] = "diff"
        else:
            box_states[f] = "expected"
    return render_template("compare.html", data=data, entry=entry, cmp=cmp,
                           scan_id=scan_id, box_states=box_states,
                           filter_qs=_M._filter_qs(request.args))


def attach_duplicate(scan_id, entry_id):
    """Attach this scanned copy to an existing record as a duplicate, instead of
    logging a new entry. Files the PDF alongside the record's primary copy and
    records who/when/source."""
    data = queue_state.claim_scan(scan_id)
    entry = get_entry(entry_id)
    if not data or not entry:
        if data:
            queue_state.release_scan(scan_id)
        return redirect(url_for("review_queue"))
    try:
        src_path = data["pdf_path"]
        source = data.get("mail_type", "Physical Mail")
        orig_name = os.path.basename(src_path)
        # File the copy next to the record's primary document, else under a COPIES folder.
        primary = entry.get("filed_path") or ""
        if primary and os.path.dirname(primary):
            dest_dir = os.path.dirname(primary)
        else:
            cfg = _M.load_config()
            filed_base = cfg.get("filed_folder", "F:\\File-Cabinet\\IntakeSystem\\Filed")
            dest_dir = os.path.join(filed_base, entry.get("entity_code") or "UNKNOWN", "COPIES")
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except Exception:
            pass
        base, ext = os.path.splitext(orig_name)
        dest_path = os.path.join(dest_dir, f"{base} ({source} copy){ext}")
        counter = 1
        while os.path.exists(dest_path):
            dest_path = os.path.join(dest_dir, f"{base} ({source} copy {counter}){ext}")
            counter += 1
        try:
            shutil.move(src_path, dest_path)
        except Exception as e:
            print(f"[App] Could not file attached copy: {e}")
            dest_path = src_path
        add_attachment(entry_id, dest_path, source, orig_name, session.get("user", ""))
        log_activity(entry_id, "Duplicate copy attached", session.get("user", ""), source)
        queue_state.queue_remove(scan_id)
    finally:
        queue_state.release_scan(scan_id)
    fqs = _M._filter_qs(request.args)
    remaining = _M._filtered_ids(request.args)
    if remaining:
        return redirect(url_for("review_item", scan_id=remaining[0]) + fqs)
    return redirect(url_for("review_queue") + fqs)


def confirm_item(scan_id):
    cfg = _M.load_config()
    # Claimed, not just read: a double-click (or a second reviewer) used to
    # insert the same entry twice; the loser now just lands on the queue page.
    data = queue_state.claim_scan(scan_id)
    if not data:
        return redirect(url_for("review_queue"))
    pdf_path = data["pdf_path"]
    form = request.form

    entity_code = form.get("entity_code","")
    entity_name = next((e["name"] for e in cfg["entities"] if e["code"]==entity_code), "")

    # Multi-select properties
    property_codes = form.getlist("property_codes")  # list from checkboxes
    property_addresses = [
        p["address"] for p in cfg["properties"] if p["code"] in property_codes
    ]
    property_code_str    = ", ".join(property_codes)
    property_address_str = ", ".join(property_addresses)

    amount = _M._parse_money(form.get("amount"))

    vendor = form.get("vendor","")
    doc_type = form.get("doc_type","Other")
    bill_type_code = form.get("bill_type_code","")
    invoice_number = _M._id_field(form.get("invoice_number"))
    account_number = _M._id_field(form.get("account_number"))

    entry = {
        "date_received":    (form.get("date_received")
                             or _M._fallback_received_date(pdf_path)),
        "scanned_date":     form.get("scanned_date", data.get("scanned_date","")),
        "batch_number":     form.get("batch_number", data.get("batch_number","")),
        "mail_type":        form.get("mail_type","Physical Mail"),
        "entity_code":      entity_code,
        "entity_name":      entity_name,
        "property_code":    property_code_str,
        "property_address": property_address_str,
        "doc_type":         doc_type,
        "bill_type_code":   bill_type_code,
        "vendor":           vendor,
        "amount":           amount,
        "due_date":         form.get("due_date",""),
        "status":           "Pending",
        "processed_by":     session.get("user",""),
        "notes":            form.get("notes",""),
        "pdf_path":         pdf_path,
        "ai_confidence":    data.get("ai_confidence",""),
        "invoice_number":   invoice_number,
        "account_number":   account_number,
        # Source-email details ride along from the scan (not user-editable) so the
        # original email is stored + shown with the bill (#14/#17).
        "email_from":       data.get("email_from",""),
        "email_to":         data.get("email_to",""),
        "email_account":    data.get("email_account",""),
        "email_subject":    data.get("email_subject",""),
        "email_date":       data.get("email_date",""),
        "email_body":       data.get("email_body",""),
        "file_hash":        data.get("file_hash",""),
        # The reviewer can untick the AI's importance flag on the form; the
        # reason only ever persists alongside a set flag.
        "important":        1 if form.get("important") else 0,
        "important_reason": (form.get("important_reason","").strip()
                             if form.get("important") else ""),
        # AI-only passthrough: the gist of an important notice bundled with this
        # bill (rate change / shutoff / cancellation). Not a form field.
        "notice_summary":   data.get("notice_summary") or None,
    }

    try:
        # Year-sanity guard (record #815): the review form's inline checkbox
        # posts date_ok; this is the backstop for anything that skips it. Runs
        # before the vendor/duplicate interstitials, whose hidden-field
        # round-trips carry date_ok through untouched.
        if not form.get("date_ok"):
            date_warnings = _M._date_sanity_warnings(entry["date_received"],
                                                     entry["due_date"])
            if date_warnings:
                fqs = _M._filter_qs(request.args)
                return render_template("date_warning.html",
                                       warnings=date_warnings,
                                       action=url_for("confirm_item", scan_id=scan_id) + fqs,
                                       back_url=url_for("review_item", scan_id=scan_id) + fqs,
                                       form_data=request.form)
        # Vendor spelling drift (Karen, 2026-07-18: saw several spellings of
        # Southern California Edison) — before anything else, check whether
        # this vendor name is a likely near-duplicate of one already on file
        # and let a person confirm, rather than silently fragmenting that
        # vendor's history across another spelling. ?vendor_ok=1 skips this on
        # the resubmit from either button on that interstitial.
        if not request.args.get("vendor_ok") and vendor.strip():
            match = find_similar_vendor(vendor)
            if match:
                return render_template("vendor_match.html", scan_id=scan_id,
                                       typed_vendor=vendor, match=match,
                                       form_data=request.form,
                                       filter_qs=_M._filter_qs(request.args))
        # Duplicate check — warn but allow override via ?force=1. Matches on the
        # bill's identity (invoice # / vendor+amount+due date) or same-day receipt.
        if not request.args.get("force"):
            dupes = find_duplicates(vendor, amount, entry["due_date"], invoice_number,
                                    pdf_path, entry["date_received"])
            if dupes:
                dup_cmps = {d["id"]: _M._dup_comparison(entry, d) for d in dupes}
                dupes.sort(key=lambda d: -dup_cmps[d["id"]]["verdict"]["score"])
                return render_template("duplicate_warning.html",
                                       dupes=dupes, scan_id=scan_id, entry=entry,
                                       dup_cmps=dup_cmps,
                                       form_data=request.form,
                                       filter_qs=_M._filter_qs(request.args))

        # "File as" panel (reports #23/#18): a value left at its suggestion
        # means "auto" — re-derive from the FINAL form values so an edited
        # vendor/date never files under a stale name or folder. An edited
        # value is an explicit override (validated in _finalize_entry).
        sub_name = form.get("filed_name", "").strip()
        sug_name = form.get("suggested_filed_name", "").strip()
        sub_dir = form.get("filed_dir", "").strip()
        sug_dir = form.get("suggested_filed_dir", "").strip()
        filed_name = sub_name if (sub_name and sub_name != sug_name) else None
        if data.get("hand_filed"):
            # A hand-filed pickup (cabinet_watch) is already exactly where —
            # and named how — the filer wanted it. Untouched fields mean
            # "leave it in place", not "re-derive the convention defaults".
            if not filed_name:
                filed_name = os.path.splitext(os.path.basename(pdf_path or ""))[0]
            filed_dir = sub_dir or os.path.dirname(pdf_path or "")
        elif sub_dir and sub_dir == sug_dir:
            filed_dir = filing.suggest_filed_dir(entry, cfg)["dir"]
        else:
            filed_dir = sub_dir or None
        entry_id = _M._finalize_entry(entry, pdf_path, cfg, property_codes,
                                      "from scan",
                                      filed_dir=filed_dir, filed_name=filed_name,
                                      copy_for=session.get("user", ""))
        queue_state.queue_remove(scan_id)
        # "Already paid" (training session 2026-08-03): Karen's backlog is full
        # of bills she paid weeks ago by hand; logging one used to take two
        # trips (confirm, then find the record and Mark Paid). One checkbox on
        # the review form now routes through the SAME _mark_paid core as every
        # other payment path — twin detection and paid-form refiling included.
        paid_twins = 0
        if form.get("already_paid") and doc_type == "Bill" and entry_id:
            import routes_bills
            method = form.get("paid_method") or None
            _fresh, twins = routes_bills._mark_paid(
                entry_id,
                paid_date=form.get("paid_date") or date.today().isoformat(),
                paid_amount=amount,
                paid_by=session.get("user", ""),
                detail="paid before logging (review form)",
                method=method,
                check_number=form.get("check_number") or None)
            paid_twins = len(twins)
    finally:
        queue_state.release_scan(scan_id)
    # The in-tray stamps through this same route; send it back to its own page.
    if request.form.get("return_to") == "intray":
        return redirect("/intray")
    # Advance to the next item IN THE SAME FILTERED GROUP (carrying the filter),
    # telling the next page what was just logged so it can say so out loud.
    fqs = _M._filter_qs(request.args)
    remaining = _M._filtered_ids(request.args)
    if remaining:
        url = url_for("review_item", scan_id=remaining[0]) + fqs
        url += ("&" if fqs else "?") + f"logged={entry_id}"
        if paid_twins:
            url += f"&twins={paid_twins}"
        return redirect(url)
    logged = get_entry(entry_id) if entry_id else None
    done_msg = "Queue is empty — all caught up."
    if logged:
        done_msg = (f"Logged {logged.get('record_no') or ('#%s' % entry_id)}"
                    f" ({logged.get('vendor') or logged.get('doc_type') or 'record'})."
                    f" Queue is empty — all caught up.")
    from urllib.parse import quote
    return redirect(url_for("review_queue") + fqs
                    + ("&" if fqs else "?") + "msg=" + quote(done_msg))


def bulk_confirm():
    cfg = _M.load_config()
    user = session.get("user", "")
    confirmed = skipped = vendor_flagged = date_flagged = 0
    for sid_s in request.form.getlist("scan_ids"):
        try:
            sid = int(sid_s)
        except ValueError:
            continue
        data = queue_state.claim_scan(sid)
        if not data:
            continue
        try:
            entry, pcs = _M._entry_from_scan_data(data, cfg, user)
            # Leave likely duplicates in the queue for individual review (never bulk-log a dup).
            if find_duplicates(entry["vendor"], entry["amount"], entry["due_date"],
                               entry["invoice_number"], entry["pdf_path"],
                               entry["date_received"]):
                skipped += 1
                continue
            # Same for vendor spelling drift — nobody's present in bulk to answer
            # "same vendor?", so it waits for individual review instead of
            # silently logging a new spelling.
            if entry["vendor"].strip() and find_similar_vendor(entry["vendor"]):
                vendor_flagged += 1
                continue
            # Same for far-off dates (Joshua, 2026-07-31 — ends bulk's blanket
            # exemption from the year-sanity guard): a misread year must not
            # ride a bulk click into a permanent record number the way record
            # #815 did. Clean items proceed; a flagged one waits in the queue,
            # where the single-confirm form shows the warning and its explicit
            # "I checked the document" date_ok checkbox.
            if _M._date_sanity_warnings(entry["date_received"], entry["due_date"]):
                date_flagged += 1
                continue
            _M._finalize_entry(entry, entry["pdf_path"], cfg, pcs, "bulk confirm",
                               copy_for=session.get("user", ""))
            queue_state.queue_remove(sid)
            confirmed += 1
        finally:
            queue_state.release_scan(sid)
    msg = f"Confirmed {confirmed} item{'s' if confirmed != 1 else ''}."
    if skipped:
        msg += f" Left {skipped} possible duplicate{'s' if skipped != 1 else ''} for individual review."
    if vendor_flagged:
        msg += f" Left {vendor_flagged} with a possible vendor-name match for individual review."
    if date_flagged:
        msg += (f" Left {date_flagged} with a suspicious date for individual review"
                f" — check the date against the document.")
    return redirect(url_for("review_queue", msg=msg))


def review_print():
    """Tier 3 #5 — merge the selected queue documents into one PDF to print on paper.
    PDFs are appended page-for-page; image scans are converted to a PDF page. Returns
    the combined PDF inline so the browser opens it and the user can just hit print."""
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    added = skipped = 0
    for sid_s in request.form.getlist("scan_ids"):
        try:
            sid = int(sid_s)
        except ValueError:
            continue
        d = queue_state.get_scan(sid)
        p = d.get("pdf_path") if d else None
        if not p or not os.path.exists(p):
            skipped += 1
            continue
        ext = os.path.splitext(p)[1].lower()
        try:
            if ext == ".pdf":
                for page in PdfReader(p).pages:
                    writer.add_page(page)
                added += 1
            elif ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                from PIL import Image
                buf = io.BytesIO()
                Image.open(p).convert("RGB").save(buf, format="PDF")
                buf.seek(0)
                for page in PdfReader(buf).pages:
                    writer.add_page(page)
                added += 1
            else:
                skipped += 1   # e.g. .docx — can't render to PDF without a converter
        except Exception as e:
            print(f"[Print] skipped {os.path.basename(p)}: {e}")
            skipped += 1
    if not added:
        return ("Nothing printable in the selection (PDFs and image scans can be "
                "printed; Word docs can't)."), 400
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return send_file(out, mimetype="application/pdf",
                     download_name="selected_documents.pdf", as_attachment=False)
