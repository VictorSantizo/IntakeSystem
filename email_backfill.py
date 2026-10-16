r"""email_backfill.py — retro-fetch source emails for documents that predate
email storage.

Reports #55/#8/#26, Joshua's go-ahead 2026-08-06 ("can we go back through the
email mailboxes to see if we can pull those emails in?"): records and queue
items ingested before 2026-07-07 never had their source email stored — the
columns exist but are empty, so the email panel and the Print-with-email
packet have nothing to show. The emails themselves are still sitting in the
office mailboxes. This module re-reads those mailboxes and puts the two back
together.

HOW THE MATCH WORKS — by bytes, never by guesswork. Every attachment in every
message since --since is hashed (sha256, same as ingest) and looked up against
(a) intake_log rows whose file_hash is set but whose email columns are empty,
and (b) review-queue items in the same state. A hash hit is certainty: that
exact file came from that exact email. Anything weaker (vendor+date
similarity) is NOT matched — attaching the wrong email to a bill is worse
than attaching none. Documents whose bytes changed at ingest (multi-document
PDFs that were split) can never hash-match; the artifact reports them as the
honest remainder.

MAILBOX SAFETY — read-only, always: IMAP selects INBOX readonly with
BODY.PEEK (flags untouched), POP3 fetches via TOP only and never issues DELE
(the MagicMail \Seen lesson, 2026-07-07). processed_emails is neither
consulted nor written: this is not intake, nothing is ever enqueued, and a
message being "already processed" is exactly what we expect here.

WHERE IT RUNS — dry-run anywhere (reads only: mailboxes + a read-only DB
look); APPLY only inside the running app (Settings → App → the backfill
button), because queue items live in the app's memory and a workstation CLI
writing pending_scans under a live app would be overwritten on its next save.
Queue items additionally get their received date corrected to the email's own
date (Karen's rule) — records do NOT: their record numbers were minted from
date_received, so date disagreements are only REPORTED, for a person.

The run writes EMAIL_BACKFILL.html into T:\_OPS INDEX (registered on
/ops-reports) and returns a summary dict.
"""
import hashlib
import html as _html
import json
import os
import re
from datetime import date, datetime

import email as email_lib
import imaplib
import poplib

import email_poller
from email_poller import (DOCX_CTYPE, IMG_EXTS, _decode_filename, _extract_body,
                          _msg_local_date)

DEFAULT_SINCE = "2026-06-01"     # the app's first live intake month
MAX_MESSAGES_PER_BOX = 4000      # safety valve, reported when hit
# The ops index sits BESIDE the app folder, derived — never a desk drive
# letter. This used to be r"T:\_OPS INDEX", which exists on the desks but not
# for the server app: the 2026-08-07 APPLY run wrote its 94 records and then
# could not write this artifact (the OSError was swallowed quietly below), so
# /ops-reports kept showing the 08-06 DRY RUN and Karen read "nothing was
# written" about a run that had, in fact, applied (2026-08-10). Same
# derivation as routes_ops_reports.DEFAULT_OPS_DIR: server F:\File-Cabinet\
# _OPS INDEX, a desk <drive>:\_OPS INDEX. config.json "ops_index_dir" (a
# server-local path, per the config-paths convention) overrides it in run().
_OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_OPS INDEX")


def _attachment_hashes(msg):
    """[(filename, sha256hex)] for every bill-shaped attachment — the same
    part filter ingest applies (_save_attachments): PDF / photo / .docx, tiny
    inline images skipped."""
    out = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        fname = _decode_filename(part.get_filename())
        low = fname.lower()
        ctype = part.get_content_type()
        is_pdf = low.endswith(".pdf") or ctype == "application/pdf"
        is_docx = low.endswith(".docx") or ctype == DOCX_CTYPE
        is_image = low.endswith(IMG_EXTS) or ctype.startswith("image/")
        if not (is_pdf or is_docx or is_image):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        if is_image and not (is_pdf or is_docx) and len(payload) < 25000:
            continue
        out.append((fname or "email_attachment.pdf",
                    hashlib.sha256(payload).hexdigest()))
    return out


def _meta_from_msg(msg, account):
    """The email columns as ingest would have stamped them (mirror of
    _save_attachments' sidecar), keyed by intake_log column names."""
    received = _msg_local_date(msg)
    return {
        "received_iso":  received.isoformat() if received else "",
        "email_from":    _decode_filename(msg.get("From")),
        "email_to":      _decode_filename(msg.get("To")),
        "email_account": (account or {}).get("name") or "",
        "email_subject": _decode_filename(msg.get("Subject")),
        "email_date":    msg.get("Date") or "",
        "email_body":    _extract_body(msg),
    }


def _wants_full_fetch(content_type: str) -> bool:
    """Cheap header-level filter: only multipart/mixed (and the odd bare
    application/pdf message) can carry a bill attachment — plain text and
    multipart/alternative bodies can't, so they're never fully fetched."""
    low = (content_type or "").lower()
    return "multipart/mixed" in low or "application/" in low


def _scan_imap(account, since_iso, on_message):
    """Walk one IMAP INBOX read-only, calling on_message(msg) for each message
    dated on/after since_iso that might carry attachments. Returns
    (messages_seen, error_or_None)."""
    seen = 0
    M = None
    try:
        opener = imaplib.IMAP4_SSL if account.get("ssl", True) else imaplib.IMAP4
        M = opener(account["host"],
                   int(account.get("port") or (993 if account.get("ssl", True) else 143)),
                   timeout=60)
        M.login(account["username"], account["password"])
        M.select("INBOX", readonly=True)
        since = datetime.strptime(since_iso, "%Y-%m-%d").strftime("%d-%b-%Y")
        typ, data = M.search(None, f'(SINCE {since})')
        nums = data[0].split() if (data and data[0]) else []
        for num in nums[:MAX_MESSAGES_PER_BOX]:
            seen += 1
            typ, hdr = M.fetch(num, '(BODY.PEEK[HEADER.FIELDS (CONTENT-TYPE)])')
            ct = ""
            if hdr and hdr[0] and isinstance(hdr[0], tuple):
                ct = hdr[0][1].decode("utf-8", "ignore")
            if not _wants_full_fetch(ct):
                continue
            typ, full = M.fetch(num, '(BODY.PEEK[])')
            if not full or not full[0] or not isinstance(full[0], tuple):
                continue
            on_message(email_lib.message_from_bytes(full[0][1]))
        err = (f"stopped at the {MAX_MESSAGES_PER_BOX}-message safety cap"
               if len(nums) > MAX_MESSAGES_PER_BOX else None)
        return seen, err
    except imaplib.IMAP4.error as e:
        return seen, f"IMAP login error: {e}"
    except Exception as e:
        return seen, f"{type(e).__name__}: {e}"
    finally:
        try:
            if M is not None:
                M.logout()
        except Exception:
            pass


def _pop3_top_only(conn, num, nlines):
    """TOP, and ONLY TOP. The poller falls back to RETR when a server has no
    TOP — but RETR is exactly what marked a whole mailbox read in the
    2026-07-07 MagicMail incident (Joshua, 8/6: never again on Larry's or
    anyone's box). A backfill is optional work: on a TOP-less server we skip
    the message rather than risk flagging it."""
    return conn.top(num, nlines)[1]      # poplib.error_proto → caller skips


def _scan_pop3(account, since_iso, on_message):
    """Walk one POP3 mailbox via TOP only (never RETR, never DELE — mail is
    left exactly as found, unread stays unread). POP3 has no server-side
    search: headers are TOPped first and the Date header decides whether the
    full message is worth pulling."""
    seen = 0
    skipped_no_top = 0
    conn = None
    try:
        opener = poplib.POP3_SSL if account.get("ssl", True) else poplib.POP3
        conn = opener(account["host"],
                      int(account.get("port") or (995 if account.get("ssl", True) else 110)),
                      timeout=60)
        conn.user(account["username"])
        conn.pass_(account["password"])
        count = len(conn.list()[1])
        # Newest first, so the since-cutoff ends the walk early.
        for num in range(count, max(0, count - MAX_MESSAGES_PER_BOX), -1):
            seen += 1
            try:
                hdr = email_lib.message_from_bytes(
                    b"\r\n".join(_pop3_top_only(conn, num, 0)))
                d = _msg_local_date(hdr)
                if d and d.isoformat() < since_iso:
                    break                  # older than the window — done
                if not _wants_full_fetch(hdr.get("Content-Type", "")):
                    continue
                on_message(email_lib.message_from_bytes(
                    b"\r\n".join(_pop3_top_only(conn, num, 100000))))
            except poplib.error_proto:
                skipped_no_top += 1        # TOP unsupported/refused — skip, never RETR
        err = (f"{skipped_no_top} messages skipped (server refused TOP; "
               f"RETR is never used — it can mark mail read)"
               if skipped_no_top else None)
        return seen, err
    except poplib.error_proto as e:
        return seen, f"POP3 error: {e}"
    except Exception as e:
        return seen, f"{type(e).__name__}: {e}"
    finally:
        try:
            if conn is not None:
                conn.quit()
        except Exception:
            pass


def _record_targets():
    """{file_hash: [entry ids]} for logged records missing their email."""
    from database import get_email_meta_targets
    out = {}
    for eid, fhash in get_email_meta_targets():
        out.setdefault(fhash, []).append(eid)
    return out


def _queue_targets(queue_items):
    """{file_hash: [scan ids]} for queue items missing their email."""
    out = {}
    for sid, d in queue_items:
        fhash = d.get("file_hash") or ""
        if not fhash:
            continue
        if d.get("email_body") or d.get("email_from"):
            continue
        out.setdefault(fhash, []).append(sid)
    return out


def run(cfg, apply=False, since=DEFAULT_SINCE, out_dir=None,
        queue_items=None, record_targets=None, progress=print):
    """The whole backfill: scan every enabled mailbox, hash-match attachments
    to records and queue items missing their email, and (apply=True, in-app
    only) write the email columns back. Returns the summary dict and writes
    EMAIL_BACKFILL.html into out_dir.

    queue_items: [(sid, data)] — pass queue_state.queue_items() when running
    inside the app. record_targets: {file_hash: [entry ids]} — pass data read
    via night_db when dry-running from a workstation (the house rule: off the
    server, night_db is the only way to touch the live DB). Left None, both
    are read through the database module — server/in-app use."""
    out_dir = out_dir or (cfg or {}).get("ops_index_dir") or _OUT_DIR
    if queue_items is None:
        from database import get_all_pending_scans
        queue_items = [(sid, json.loads(raw)) for sid, raw in
                       get_all_pending_scans()]
    rec_targets = record_targets if record_targets is not None else _record_targets()
    q_targets = _queue_targets(queue_items)
    q_data = dict(queue_items)
    summary = dict(
        since=since, apply=apply,
        record_targets=sum(len(v) for v in rec_targets.values()),
        queue_targets=sum(len(v) for v in q_targets.values()),
        accounts=[], record_matches=[], queue_matches=[],
        date_mismatches=[], queue_date_fixes=0,
    )
    if not rec_targets and not q_targets:
        progress("Nothing to do: every record and queue item either has its "
                 "email or has no file hash to match by.")
        _write_artifact(summary, out_dir)
        return summary

    matched_rec_ids = set()
    matched_q_sids = set()

    def on_message(msg):
        hits_r, hits_q = [], []
        for _fname, fhash in _attachment_hashes(msg):
            hits_r += [(eid, fhash) for eid in rec_targets.get(fhash, [])]
            hits_q += [(sid, fhash) for sid in q_targets.get(fhash, [])]
        if not hits_r and not hits_q:
            return
        meta = _meta_from_msg(msg, on_message.account)
        cols = {k: meta[k] for k in ("email_from", "email_to", "email_account",
                                     "email_subject", "email_date", "email_body")}
        for eid, fhash in hits_r:
            if eid in matched_rec_ids:
                continue
            matched_rec_ids.add(eid)
            row = dict(entry_id=eid, subject=meta["email_subject"],
                       email_received=meta["received_iso"])
            if apply:
                from database import get_entry, log_activity, update_entry
                update_entry(eid, cols)
                log_activity(eid, "Source email retro-attached from mailbox",
                             "Email Backfill", meta["email_subject"][:120])
                e = get_entry(eid) or {}
                if (meta["received_iso"] and e.get("date_received")
                        and e["date_received"] != meta["received_iso"]):
                    # Record numbers were minted from date_received — report,
                    # never rewrite (a person decides, per record).
                    summary["date_mismatches"].append(dict(
                        entry_id=eid, record=e.get("date_received"),
                        email=meta["received_iso"]))
            summary["record_matches"].append(row)
        for sid, fhash in hits_q:
            if sid in matched_q_sids:
                continue
            matched_q_sids.add(sid)
            d = q_data.get(sid) or {}
            fix = (meta["received_iso"]
                   and (d.get("mail_type") == "Email")
                   and d.get("received_date") != meta["received_iso"])
            summary["queue_matches"].append(dict(
                scan_id=sid, subject=meta["email_subject"],
                email_received=meta["received_iso"], date_fix=bool(fix)))
            if apply:
                d.update(cols)
                if fix:
                    # A queue item has no record number yet — the email's own
                    # date IS the received date (Karen's rule), safe to set.
                    d["received_date"] = meta["received_iso"]
                    d.pop("received_date_assumed", None)
                    d.pop("received_date_source", None)
                    summary["queue_date_fixes"] += 1
                from database import save_pending_scan
                save_pending_scan(sid, json.dumps(d))

    try:
        for account in (cfg.get("mail_accounts") or []):
            if not account.get("enabled", True):
                continue
            on_message.account = account
            scanner = _scan_pop3 if account.get("protocol") == "pop3" else _scan_imap
            progress(f"Scanning {account.get('name') or account.get('username')} "
                     f"({account.get('protocol', 'imap')}) since {since}…")
            seen, err = scanner(account, since, on_message)
            summary["accounts"].append(dict(
                name=account.get("name") or account.get("username"),
                protocol=account.get("protocol", "imap"), seen=seen, error=err))
            progress(f"  {seen} messages looked at"
                     + (f" — {err}" if err else ""))

        progress(f"Matched {len(summary['record_matches'])} of "
                 f"{summary['record_targets']} email-less records and "
                 f"{len(summary['queue_matches'])} of {summary['queue_targets']} "
                 f"queue items"
                 + (f"; corrected {summary['queue_date_fixes']} queue received "
                    f"dates" if apply else " (dry run — nothing written)") + ".")
    except BaseException as exc:
        # Whatever cuts the scan short, the artifact below still gets what the
        # run accomplished, marked as cut short — an applied run must never
        # again leave /ops-reports showing an older run's page (2026-08-07).
        summary["aborted"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _write_artifact(summary, out_dir)
    return summary


def _write_artifact(summary, out_dir):
    """EMAIL_BACKFILL.html in the ops index — what matched, what can't ever
    match (split documents), and the record-date disagreements a person still
    has to look at. Failure to write is LOUD: a quiet failure here is how the
    2026-08-07 applied run stayed invisible."""
    e = _html.escape
    ran = datetime.now().strftime("%Y-%m-%d %H:%M")
    mode = "APPLIED" if summary["apply"] else "DRY RUN — nothing was written"
    aborted_html = ""
    if summary.get("aborted"):
        aborted_html = (
            f'<p class="note" style="border-color:#c33;background:#fdecec;">'
            f'<strong>This run stopped early:</strong> {e(summary["aborted"])}. '
            f'The tables below hold only what it finished before stopping.</p>')
    rows_r = "".join(
        f"<tr><td><a href='/entry/{m['entry_id']}/view'>#{m['entry_id']}</a></td>"
        f"<td>{e(m['subject'] or '')}</td><td>{e(m['email_received'] or '')}</td></tr>"
        for m in summary["record_matches"]) or "<tr><td colspan=3>none</td></tr>"
    rows_q = "".join(
        f"<tr><td><a href='/review/{m['scan_id']}'>queue #{m['scan_id']}</a></td>"
        f"<td>{e(m['subject'] or '')}</td><td>{e(m['email_received'] or '')}"
        f"{' &middot; received date corrected' if m['date_fix'] else ''}</td></tr>"
        for m in summary["queue_matches"]) or "<tr><td colspan=3>none</td></tr>"
    rows_d = "".join(
        f"<tr><td><a href='/entry/{m['entry_id']}/view'>#{m['entry_id']}</a></td>"
        f"<td>{e(m['record'])}</td><td>{e(m['email'])}</td></tr>"
        for m in summary["date_mismatches"]) or "<tr><td colspan=3>none</td></tr>"
    accts = "".join(
        f"<li>{e(a['name'])} ({a['protocol']}): {a['seen']} messages"
        + (f" — <strong>{e(a['error'])}</strong>" if a["error"] else "") + "</li>"
        for a in summary["accounts"]) or "<li>no accounts scanned</li>"
    unmatched_r = summary["record_targets"] - len(summary["record_matches"])
    unmatched_q = summary["queue_targets"] - len(summary["queue_matches"])
    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Email backfill — {e(mode)}</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#222}}
table{{border-collapse:collapse;margin:8px 0 18px}}td,th{{border:1px solid #ccc;
padding:4px 10px;font-size:.9rem}}th{{background:#f0f4f8;text-align:left}}
.note{{background:#fff8e6;border:1px solid #e5d9a8;padding:10px 14px;border-radius:8px}}</style>
</head><body>
<h1>Email backfill — {e(mode)}</h1>
{aborted_html}
<p>Run {e(ran)}, mailboxes since {e(summary['since'])}. Reports #55/#8/#26:
source emails re-fetched from the office mailboxes and matched to documents
by exact file hash — never by guesswork.</p>
<ul>{accts}</ul>
<h2>Logged records matched — {len(summary['record_matches'])} of {summary['record_targets']}</h2>
<table><tr><th>Record</th><th>Email subject</th><th>Email date</th></tr>{rows_r}</table>
<h2>Queue items matched — {len(summary['queue_matches'])} of {summary['queue_targets']}</h2>
<table><tr><th>Queue item</th><th>Email subject</th><th>Email date</th></tr>{rows_q}</table>
<h2>Received-date disagreements on logged records — {len(summary['date_mismatches'])}</h2>
<p class="note">These records' stored received date differs from their email's
own date. Record numbers were minted from the stored date, so nothing was
changed — correct each on its record page if the email is right.</p>
<table><tr><th>Record</th><th>Stored date</th><th>Email says</th></tr>{rows_d}</table>
<h2>The honest remainder</h2>
<p>{unmatched_r} record{'s' if unmatched_r != 1 else ''} and {unmatched_q}
queue item{'s' if unmatched_q != 1 else ''} stayed email-less: their message
is older than the scan window, gone from the mailbox, or the document's bytes
changed at ingest (multi-document PDFs that were split can never hash-match).
Weaker matching (vendor + date similarity) is deliberately not attempted.</p>
</body></html>"""
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "EMAIL_BACKFILL.html"), "w",
                  encoding="utf-8") as f:
            f.write(doc)
    except OSError as exc:
        print(f"[EmailBackfill] COULD NOT WRITE the run report to {out_dir!r}: "
              f"{exc} — /ops-reports will keep showing the PREVIOUS run's page "
              f"(this quiet failure is how the 2026-08-07 apply went missing).")
