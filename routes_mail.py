r"""routes_mail.py — /mail: every email from every office mailbox, searchable,
filterable, printable (Nancy's ask, 2026-08-10).

Same pattern as routes_karen.py (see its docstring for why these are
add_url_rule registrations under original endpoint names, never Blueprints).
Data comes from mail_archive.py (the local read-only copy of the mailboxes);
the join to the rest of the system is the attachment sha256 against
intake_log.file_hash — exact bytes, never guesswork.

The search box and the filter bar COMPOSE IN BOTH DIRECTIONS (the /bills
report-#93 lesson, taken one step further: here the filter bar also carries a
hidden q, so filtering after a search keeps the search). The mailbox is always
visible on every row — with several inboxes on one screen, whose mail this was
is a first-class fact.

Printing (mail_print) is the standalone-page pattern (coversheet.html): its
own <style>, a screen-only toolbar, @page letter. The printout is header →
"Attachments:" list → body → footer; the attachment list always prints — the
honest full list with UNC paths for filed documents, or the literal
"Attachments: None" — so nobody is ever left wondering (Nancy's spec).
"""
import threading
from datetime import datetime

from flask import redirect, render_template, request, session, url_for

import bill_filters
import doc_types
import email_append
import email_pdf
import filing
import mail_archive
import queue_state
from database import get_entries_by_hashes
from routes_newlook import render_skinned

_M = None   # the app module (composition root); set by register()

# Mailbox badge palette — stable per account (by position in the accounts
# list), distinct enough that Larry's and Karen's mail never look alike.
_BADGE_COLORS = ("#1565C0", "#6A1B9A", "#2E7D32", "#B45309", "#AD1457",
                 "#00695C", "#4527A0")


def register(app, appmod):
    global _M
    _M = appmod
    r = app.add_url_rule
    r("/mail", "mail_page", mail_page)
    r("/mail/sync", "mail_sync", mail_sync, methods=["POST"])
    r("/mail/attachment/<sha>", "mail_attachment_file", mail_attachment_file)
    r("/mail/<int:msg_id>", "mail_view", mail_view)
    r("/mail/<int:msg_id>/print", "mail_print", mail_print)
    r("/mail/<int:msg_id>/append/<int:entry_id>", "mail_append_to_record",
      mail_append_to_record, methods=["POST"])


# ── Shared helpers ────────────────────────────────────────────────────────────

def _filters():
    return {
        "account":   request.args.get("account", ""),
        "doc_type":  request.args.get("doc_type", ""),
        "date_from": request.args.get("date_from", ""),
        "date_to":   request.args.get("date_to", ""),
        "has_att":   request.args.get("has_att", ""),
    }


def _account_names(cfg):
    """Every mailbox name a message could carry: the configured accounts, plus
    any name only the archive remembers (an account renamed or removed)."""
    names = [a.get("name") or a.get("username")
             for a in (cfg.get("mail_accounts") or [])]
    for n in mail_archive.status().get("accounts", []):
        if n and n not in names:
            names.append(n)
    return [n for n in names if n]


def _acct_colors(names):
    return {n: _BADGE_COLORS[i % len(_BADGE_COLORS)] for i, n in enumerate(names)}


def _enrich(messages, cfg, blobs=False):
    """Hang the system's knowledge on each message: for every attachment,
    whether those exact bytes are a filed record (record #, doc type, UNC
    path, whether this email is already at the back of its PDF), still in the
    review queue, or not saved at all."""
    shas = {a["sha256"] for m in messages
            for a in m.get("attachments") or [] if a.get("sha256")}
    sends = mail_archive.distinct_sends_for_hashes(list(shas))
    by_hash = {}
    for e in get_entries_by_hashes(list(shas)):
        by_hash.setdefault(e.get("file_hash") or "", e)
        if e.get("filed_sha256"):
            by_hash.setdefault(e["filed_sha256"], e)
    queue_by_hash = {}
    for sid, d in queue_state.queue_items():
        fh = d.get("file_hash")
        if fh and fh not in queue_by_hash:
            queue_by_hash[fh] = {"sid": sid, "doc_type": d.get("doc_type") or "",
                                 "vendor": d.get("vendor") or ""}
    for m in messages:
        key = f"{m.get('account_id')}:{m.get('message_key')}"
        doc_types_here, records = set(), []
        resend, resend_sha, flagged_reason = 0, "", ""
        for a in m.get("attachments") or []:
            a["resend_count"] = sends.get(a.get("sha256") or "", 0)
            if a["resend_count"] > resend:
                resend, resend_sha = a["resend_count"], a.get("sha256") or ""
            # The shared per-unique-attachment copy (detail/print views only —
            # the list never renders per-attachment links).
            a["blob"] = bool(blobs and a.get("sha256")
                             and mail_archive.blob_for(a["sha256"]))
            e = by_hash.get(a.get("sha256") or "")
            if e:
                a["match"] = {
                    "kind": "record", "entry_id": e["id"],
                    "record_no": e.get("record_no") or f"#{e['id']}",
                    "doc_type": e.get("doc_type") or "", "status": e.get("status"),
                    "vendor": e.get("vendor") or "",
                    "important": bool(e.get("important")),
                    "important_reason": e.get("important_reason") or "",
                    "unc": filing.display_path(
                        e.get("filed_path") or e.get("pdf_path") or "", cfg),
                    "appended": key in email_append.appended_keys(e),
                }
                if e.get("important") and not flagged_reason:
                    flagged_reason = e.get("important_reason") or "Flagged important"
                if e.get("doc_type"):
                    doc_types_here.add(e["doc_type"])
                if e["id"] not in [r["entry_id"] for r in records]:
                    records.append(a["match"])
            elif a.get("sha256") in queue_by_hash:
                qi = queue_by_hash[a["sha256"]]
                a["match"] = {"kind": "queue", "sid": qi["sid"],
                              "doc_type": qi["doc_type"], "vendor": qi["vendor"]}
                if qi["doc_type"]:
                    doc_types_here.add(qi["doc_type"])
            else:
                a["match"] = None
        m["doc_types"] = sorted(doc_types_here)
        m["records"] = records
        # The mailbox wears the flags too (Joshua 2026-08-10): a re-sent
        # attachment and a flagged matched record both show on the row. The
        # badge links to /mail?sha=… — the list narrowed to exactly the
        # emails carrying those identical bytes.
        m["resend"] = resend
        m["resend_sha"] = resend_sha
        m["flagged"] = bool(flagged_reason)
        m["flagged_reason"] = flagged_reason
    return messages


def _load(filters, q, limit=300):
    """Query the archive and apply the one filter it can't know about — the
    document type, which lives on the records/queue items the attachments
    became. Search and filters compose because each is a plain narrowing of
    whatever the other produced."""
    cfg = _M.load_config()
    messages = mail_archive.query(q=q, account=filters["account"],
                                  date_from=filters["date_from"],
                                  date_to=filters["date_to"],
                                  has_att=filters["has_att"], limit=limit)
    messages = _enrich(messages, cfg)
    if filters["doc_type"]:
        messages = [m for m in messages if filters["doc_type"] in m["doc_types"]]
    return messages, cfg


# ── Views ─────────────────────────────────────────────────────────────────────

def mail_page():
    q = (request.args.get("q") or "").strip()
    filters = _filters()
    sha = (request.args.get("sha") or "").strip()
    sha_file = ""
    if sha:
        # The clicked re-send bubble: exactly the emails carrying these bytes,
        # every mailbox, newest first (Joshua 2026-08-10).
        cfg = _M.load_config()
        messages = mail_archive.messages_for_hash(sha)
        messages.sort(key=lambda m: (m.get("date_iso") or "", m["id"]),
                      reverse=True)
        _enrich(messages, cfg)
        sha_file = next((a.get("filename") for m in messages
                         for a in m.get("attachments") or []
                         if a.get("sha256") == sha), "")
    else:
        messages, cfg = _load(filters, q)
    names = _account_names(cfg)
    st = mail_archive.status()
    return render_skinned(
        "mail.html", messages=messages, q=q, filters=filters,
        filters_on=bill_filters.active(filters), accounts=names,
        acct_colors=_acct_colors(names), status=st, sha=sha, sha_file=sha_file,
        syncing=mail_archive.sync_running() or queue_state.job_running("mail_sync"),
        doc_types=doc_types.DOC_TYPES,
        qs=request.query_string.decode("utf-8", "ignore"))


def mail_attachment_file(sha):
    """The shared copy of one unique attachment (content-addressed by sha256).
    Every email carrying these exact bytes serves the same file."""
    import re as _re
    from flask import send_file
    if not _re.fullmatch(r"[0-9a-f]{64}", (sha or "").lower()):
        return "Not found", 404
    b = mail_archive.blob_for(sha.lower())
    if not b:
        return ("No stored copy of this attachment yet — the rehydrate sweep "
                "brings copies in for older mail."), 404
    return send_file(b["blob_path"], mimetype=b.get("content_type") or None,
                     download_name=b.get("filename") or None)


def mail_sync():
    """Pull all mailboxes into the archive now (background), then run the
    append/flag pass over whatever arrived. Read-only on the mail side —
    IMAP PEEK / POP3 TOP+UIDL, nothing is ever marked read."""
    if not queue_state.try_start_job("mail_sync"):
        return redirect(url_for("mail_page"))

    def worker():
        try:
            cfg = _M.load_config()
            new_ids = []
            mail_archive.init_archive()
            mail_archive.sync_all(cfg, since=mail_archive.incremental_since(
                cfg.get("mail_archive_since") or mail_archive.DEFAULT_SINCE),
                on_new=new_ids.append)
            if new_ids:
                email_append.process_new_messages(new_ids, cfg)
            if mail_archive.blobless_count() > 0:
                mail_archive.rehydrate_all(cfg)
        except Exception as e:
            print(f"[MailArchive] sync worker failed: {type(e).__name__}: {e}")
        finally:
            queue_state.finish_job("mail_sync")

    threading.Thread(target=worker, daemon=True, name="mail-sync").start()
    return redirect(url_for("mail_page"))


def _one(msg_id):
    m = mail_archive.get_message(msg_id)
    if not m:
        return None, None
    cfg = _M.load_config()
    _enrich([m], cfg, blobs=True)
    return m, cfg


def mail_view(msg_id):
    m, cfg = _one(msg_id)
    if not m:
        return "No such email in the archive", 404
    names = _account_names(cfg)
    return render_skinned("mail_view.html", m=m,
                          acct_colors=_acct_colors(names),
                          back_qs=request.args.get("back", ""))


def mail_print(msg_id):
    """The printable email: header → Attachments (the honest list, with UNC
    paths for filed documents, or the literal 'Attachments: None') → body →
    footer. A standalone page (no app chrome) opened in a new tab."""
    m, cfg = _one(msg_id)
    if not m:
        return "No such email in the archive", 404
    atts = []
    for a in m.get("attachments") or []:
        match, note = a.get("match"), ""
        if match and match.get("kind") == "record":
            note = f"filed as {match['record_no']}"
            if match.get("unc"):
                note += f" — {match['unc']}"
        elif match and match.get("kind") == "queue":
            note = f"in Needs Review (queue #{match['sid']}) — no record yet"
        elif a.get("blob"):
            note = "not filed as a document — a copy from the email is kept"
        else:
            note = "not saved in the system"
        if (a.get("resend_count") or 0) > 1:
            note += (f" — this exact file has arrived under "
                     f"{a['resend_count']} separate emails")
        atts.append({"filename": a.get("filename"),
                     "size": email_pdf._fmt_size(a.get("size")), "note": note,
                     "match": match, "sha": a.get("sha256") or "",
                     "blob": a.get("blob")})
    return render_template("mail_print.html", m=m, atts=atts,
                           printed_by=session.get("user", ""),
                           printed_at=datetime.now().strftime("%Y-%m-%d %H:%M"))


def mail_append_to_record(msg_id, entry_id):
    """The per-attachment option: put the email(s) behind this document at the
    back of its filed PDF — permanently, same engine as filing-time appending.
    Idempotent; appends every not-yet-appended mailbox copy, not only this
    message's."""
    m = mail_archive.get_message(msg_id)
    back = url_for("mail_view", msg_id=msg_id, back=request.args.get("back", ""))
    if not m:
        return redirect(url_for("mail_page"))
    hashes = {a.get("sha256") for a in m.get("attachments") or []}
    owned = any(e["id"] == entry_id
                for e in get_entries_by_hashes(list(hashes)))
    if owned:
        res = email_append.append_to_entry(entry_id,
                                           by=session.get("user", "") or "Mail page",
                                           cfg=_M.load_config())
        if res.get("skipped"):
            print(f"[EmailAppend] /mail append #{entry_id}: {res['skipped']}")
    return redirect(back)
