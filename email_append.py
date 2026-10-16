r"""email_append.py — the source email PERMANENTLY at the back of the document.

Reports #55/#106/#114 and Karen's 2026-08-10 ask. The email-packet print
(email_pdf.packet_pdf_bytes, commit 8c6900c1) answered "print the bill with its
email" — but only as a byte stream served to the browser. The FILED file on
disk stayed the bare attachment, so anyone opening it from the cabinet tree
(Nancy, report #114: "there is no email attached to the back of this invoice")
saw no email at all. This module closes that gap: it typesets the source
email(s) — header, the honest Attachments list, body — and appends those pages
to the END of the filed PDF itself, once, idempotently.

"Email(s)", plural, is deliberate: the same message often lands in two office
inboxes (Larry's AND Karen's), and a re-sent invoice arrives as a genuinely new
message. Every distinct (mailbox, message) pair carrying this document's exact
bytes gets its own page set at the back, tracked per-key in the new
intake_log.email_appended column so nothing is ever appended twice.

THE DEDUP CONTRACT this must not break: intake_log.file_hash stays the sha256
of the ORIGINAL document bytes (ingest dedup and the mail archive both match on
it). Appending changes the bytes on disk, so the new filed_sha256 column
records the packet's own hash — get_entries_by_hash and the heal-by-hash paths
accept either, keeping self-healing alive for appended files.

Non-PDF documents CONVERT instead of being skipped (Joshua 2026-08-10: "if
they are printable, print them to PDF. ESPECIALLY if they are Word doc"):
images through PyMuPDF's native conversion, .docx through LibreOffice headless
when it's installed (the approved converter, 2026-07-29) or a text-faithful
PyMuPDF rendering when it isn't — the rendering says so on its first line.
The converted PDF becomes the filed primary and the ORIGINAL Word/image file
stays on the record as an attachment copy; nothing is ever deleted. Only a
file that can't be converted at all is skipped, with the reason reported.

RESEND ATTENTION (Karen, 2026-08-10: "this resend of invoices should also be a
signal that this bill is trying to get attention"): when the same document
bytes arrive under a second distinct message — or ingest byte-dedups a fresh
email copy onto an existing record — flag_resend() raises the record's
`important` flag in the _year_net shape (append to important_reason, keep the
first flagged_at, clear acknowledged_at so the board shows it again). Payables
only, via doc_types.is_payable — never a literal == "Bill".
"""
import hashlib
import json
import os
from datetime import datetime

try:
    import fitz          # PyMuPDF — same optional import as email_pdf.py
except ImportError:      # pragma: no cover - declared in requirements.txt
    fitz = None

import database
import doc_types
import email_pdf
import filing
import filing_ledger
import mail_archive

# Artifact home, derived like routes_ops_reports.DEFAULT_OPS_DIR so it lands in
# the right _OPS INDEX from the server (F:\File-Cabinet\_OPS INDEX) and from a
# desk (<drive>:\_OPS INDEX) alike.
_OPS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_OPS INDEX")

# The key an append records when the email came from the record's own email_*
# columns (no archive copy yet — pre-archive records, or archive lag at filing
# time). When the archive later supplies the same message, the matching row is
# recognized by mailbox+subject so the identical email is never appended twice.
_OWN_KEY = "record:own"


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def appended_keys(entry: dict) -> set:
    """The (mailbox, message) keys already at the back of this record's file."""
    try:
        return set(json.loads(entry.get("email_appended") or "[]"))
    except (ValueError, TypeError):
        return set()


def _email_dict(m: dict) -> dict:
    """An archived message row as the email_* dict email_pdf typesets."""
    return {"email_from": m.get("from_addr"), "email_to": m.get("to_addr"),
            "email_account": m.get("account_name"),
            "email_subject": m.get("subject"),
            "email_date": m.get("date_raw") or m.get("date_iso"),
            "email_body": m.get("body")}


def attachment_notes(atts, cfg=None) -> list:
    """The archive's attachment list dressed for printing: each file with its
    size; when those exact bytes are filed in the system, the record number and
    the full UNC path (house rule #33/#36 — never a drive letter); and when the
    bytes have arrived under more than one distinct email, that fact — on paper,
    where it means "the sender keeps sending this"."""
    cfg = cfg or {}
    out = []
    for a in (atts or []):
        bits = []
        sha = a.get("sha256") or ""
        ents = database.get_entries_by_hash(sha)
        if ents:
            e = ents[0]
            label = e.get("record_no") or f"#{e['id']}"
            unc = filing.display_path(
                e.get("filed_path") or e.get("pdf_path") or "", cfg)
            bits.append(f"filed as {label}" + (f" — {unc}" if unc else ""))
        sends = mail_archive.distinct_sends_for_hash(sha)
        if sends > 1:
            bits.append(f"this exact file has arrived under {sends} separate emails")
        out.append({"filename": a.get("filename"), "size": a.get("size"),
                    "note": "; ".join(bits)})
    return out


def emails_for_entry(entry: dict, cfg=None) -> list:
    """Every email that belongs at the back of this record's document:
    [{key, email, attachments}], oldest first. Archive copies (all mailboxes)
    win; the record's own email_* columns are the fallback when the archive
    holds nothing for these bytes (attachments unknown → None, which prints
    no attachment section rather than a false 'None')."""
    items = [{"key": f"{m['account_id']}:{m['message_key']}",
              "email": _email_dict(m),
              "attachments": attachment_notes(m.get("attachments"), cfg)}
             for m in mail_archive.messages_for_hash(entry.get("file_hash") or "")]
    if not items and email_pdf.has_email(entry):
        items.append({"key": _OWN_KEY, "email": entry, "attachments": None})
    return items


# ── Converting a non-PDF primary so the email can ride at its back ────────────

_IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def _soffice_path(cfg=None):
    """LibreOffice's soffice executable if it's anywhere we'd expect —
    config.json "soffice_path" first, then PATH, then the standard installs."""
    import shutil
    cands = [(cfg or {}).get("soffice_path") or "", shutil.which("soffice") or "",
             r"C:\Program Files\LibreOffice\program\soffice.exe",
             r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def _docx_pdf_bytes(path, cfg=None):
    """.docx → (pdf_bytes, how) — LibreOffice headless when installed (true
    layout), else the document's text typeset via PyMuPDF: faithful to the
    words, not the formatting, and its first line says exactly that.
    (None, '') when even the text can't be read."""
    soffice = _soffice_path(cfg)
    if soffice:
        import subprocess
        import tempfile
        try:
            with tempfile.TemporaryDirectory() as td:
                subprocess.run([soffice, "--headless", "--convert-to", "pdf",
                                "--outdir", td, path],
                               timeout=120, capture_output=True, check=True)
                out = os.path.join(td, os.path.splitext(
                    os.path.basename(path))[0] + ".pdf")
                if os.path.exists(out):
                    with open(out, "rb") as f:
                        return f.read(), "LibreOffice"
        except Exception as e:
            print(f"[EmailAppend] LibreOffice convert failed for "
                  f"{os.path.basename(path)}: {type(e).__name__}: {e} — "
                  f"falling back to the text rendering")
    from classifier import extract_text_from_docx
    text = extract_text_from_docx(path)
    if not text or text.startswith("[DOCX extraction error"):
        return None, ""
    header = (f"CONVERTED FROM {os.path.basename(path)}\n"
              f"(text rendering — the original Word file is kept on the "
              f"record as a copy)\n" + "_" * 78 + "\n\n")
    b = email_pdf.text_pdf_bytes(header + text, max_pages=80)
    return (b, "text rendering") if b else (None, "")


def _convert_to_pdf(path, cfg=None):
    """Any printable non-PDF document → (pdf_bytes, how) or (None, '')."""
    if fitz is None:
        return None, ""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return _docx_pdf_bytes(path, cfg)
    if ext in _IMG_EXTS:
        try:
            src = fitz.open(path)
            data = src.convert_to_pdf()
            src.close()
            return data, "image"
        except Exception as e:
            print(f"[EmailAppend] image convert failed for "
                  f"{os.path.basename(path)}: {e}")
    return None, ""


def _promote_to_pdf(entry, path, by, cfg=None):
    """Convert a non-PDF primary to PDF beside it, make the PDF the filed
    primary, and keep the original file on the record as a copy. Returns the
    new PDF path, or None (with the skip reason) when conversion failed."""
    pdf_bytes, how = _convert_to_pdf(path, cfg)
    if pdf_bytes is None:
        return None
    base = os.path.splitext(path)[0]
    pdf_path = base + ".pdf"
    c = 1
    while os.path.exists(pdf_path):
        pdf_path = f"{base} ({c}).pdf"
        c += 1
    filing_ledger.announce(pdf_path)
    with open(pdf_path, "wb") as f:
        f.write(pdf_bytes)
    orig_name = os.path.basename(path)
    database.add_attachment(entry["id"], path, "Original file", orig_name, by)
    database.update_entry(entry["id"], {"filed_path": pdf_path})
    database.log_activity(entry["id"], "Converted to PDF", by,
                          f"{orig_name} → PDF ({how}) so the source email can "
                          f"ride at the back; the original file stays on the "
                          f"record as a copy")
    return pdf_path


def append_pages_to_file(path: str, items: list, dry: bool = False) -> int:
    """Typeset each item's email and append its pages to the END of the PDF at
    `path` (temp-write + atomic replace, destination announced to the filing
    ledger first so the cabinet watcher knows the rewrite was us). Returns how
    many email page-sets were appended (0 = nothing typeset / not a PDF)."""
    if fitz is None or not items:
        return 0
    page_sets = []
    for it in items:
        b = email_pdf.email_pdf_bytes(it["email"], attachments=it.get("attachments"))
        if b:
            page_sets.append(b)
    if not page_sets:
        return 0
    if dry:
        return len(page_sets)
    doc = fitz.open(path)
    try:
        if not doc.is_pdf:
            return 0
        for b in page_sets:
            doc.insert_pdf(fitz.open("pdf", b))
        tmp = path + ".appending.tmp"
        doc.save(tmp)
    finally:
        doc.close()
    try:
        filing_ledger.announce(path)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return len(page_sets)


def append_to_entry(entry_id: int, by: str = "System", dry: bool = False,
                    cfg: dict = None) -> dict:
    """Append every not-yet-appended source email to the back of a record's
    filed PDF. Idempotent: keys land in intake_log.email_appended, and a second
    call is a no-op. Returns {"appended": n} or {"skipped": reason}."""
    if fitz is None:
        return {"skipped": "PyMuPDF unavailable"}
    entry = database.get_entry(entry_id)
    if not entry:
        return {"skipped": "no such record"}
    path = entry.get("filed_path") or entry.get("pdf_path") or ""
    if not path or not os.path.exists(path):
        return {"skipped": "document file missing"}
    is_pdf = path.lower().endswith(".pdf")
    done = appended_keys(entry)
    items = emails_for_entry(entry, cfg)
    if _OWN_KEY in done:
        # The own-columns copy is already at the back; its archive twin (same
        # mailbox, same subject) is the same pages — never append it again.
        for it in items:
            if (it["key"] != _OWN_KEY
                    and (it["email"].get("email_account") or "") == (entry.get("email_account") or "")
                    and (it["email"].get("email_subject") or "") == (entry.get("email_subject") or "")):
                done.add(it["key"])
    new = [it for it in items if it["key"] not in done]
    if not new:
        return {"appended": 0}
    if dry:
        res = {"appended": 0, "would_append": [it["key"] for it in new],
               "accounts": [it["email"].get("email_account") or "?" for it in new]}
        if not is_pdf:
            res["would_convert"] = os.path.basename(path)
        return res
    if not is_pdf:
        promoted = _promote_to_pdf(entry, path, by, cfg)
        if promoted is None:
            ext = os.path.splitext(path)[1] or "this file type"
            return {"skipped": f"could not convert {ext} to PDF"}
        path = promoted
    n = append_pages_to_file(path, new)
    if n == 0:
        return {"skipped": "could not typeset the email pages"}
    done |= {it["key"] for it in new}
    database.update_entry(entry_id, {"email_appended": json.dumps(sorted(done)),
                                     "filed_sha256": _sha256_file(path)})
    boxes = ", ".join(it["email"].get("email_account") or "?" for it in new)
    database.log_activity(entry_id, "Email appended to document", by,
                          f"{n} source email(s) added to the back of the filed "
                          f"PDF (from: {boxes})")
    return {"appended": n}


def append_meta_to_file(path: str, meta: dict) -> int:
    """Sidecar-meta variant for files that are NOT a record's primary document
    (the auto-attached copy of a re-sent email): put the arriving email's pages
    at the back of that copy file. Attachment list unknown → None (no false
    'Attachments: None')."""
    email = {"email_from": meta.get("from"), "email_to": meta.get("to"),
             "email_account": meta.get("account"),
             "email_subject": meta.get("subject"),
             "email_date": meta.get("email_date_raw"),
             "email_body": meta.get("body")}
    if not email_pdf.has_email(email) or not path.lower().endswith(".pdf"):
        return 0
    return append_pages_to_file(path, [{"key": "meta", "email": email,
                                        "attachments": None}])


def flag_resend(entry: dict, note_how: str, by: str = "System") -> bool:
    """A vendor sending the same document again is a bill trying to get
    attention (Karen, 2026-08-10). Same shape as database._year_net: append to
    important_reason (never clobber a human's or the AI's earlier reason), keep
    the first flagged_at, clear acknowledged_at so the board resurfaces it.
    Payables only — doc_types.is_payable, the one 'is this money owed' test."""
    if not entry or not doc_types.is_payable(entry.get("doc_type") or ""):
        return False
    vendor = (entry.get("vendor") or "").strip() or "The vendor"
    add = f"{vendor} re-sent this bill — {note_how}"
    if (entry.get("status") or "") == "Paid":
        add += " (marked Paid here — worth checking the payment went through)"
    reason = (entry.get("important_reason") or "").strip()
    if add in reason:
        return False
    database.update_entry(entry["id"], {
        "important": 1,
        "flagged_at": entry.get("flagged_at")
                      or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "acknowledged_at": "",
        "important_reason": "; ".join(filter(None, [reason, add])),
    })
    database.log_activity(entry["id"], "Re-send flagged", by, add)
    return True


def process_new_messages(msg_ids: list, cfg: dict = None, progress=print) -> dict:
    """The after-sync pass: for each newly archived message, find the records
    its attachments belong to (exact bytes only), put the new mailbox copy at
    the back of their filed PDFs, and raise the attention flag when the bytes
    have now arrived under more than one distinct message. Runs in the app (or
    a server CLI) — it writes intake.db and filed files."""
    out = {"appended": 0, "flagged": 0}
    for mid in msg_ids:
        m = mail_archive.get_message(mid)
        if not m:
            continue
        for a in m.get("attachments") or []:
            sha = a.get("sha256") or ""
            if not sha:
                continue
            for e in database.get_entries_by_hash(sha):
                try:
                    res = append_to_entry(e["id"], by="Mail Archive", cfg=cfg)
                    if res.get("appended"):
                        out["appended"] += res["appended"]
                        progress(f"[EmailAppend] #{e['id']}: +{res['appended']} "
                                 f"email page-set(s) from {m.get('account_name')}")
                except Exception as ex:
                    progress(f"[EmailAppend] #{e['id']}: {type(ex).__name__}: {ex}")
                if mail_archive.distinct_sends_for_hash(sha) > 1:
                    fresh = database.get_entry(e["id"]) or e
                    if flag_resend(fresh,
                                   f"another copy arrived by email in "
                                   f"{m.get('account_name') or '?'} on "
                                   f"{m.get('date_iso') or '?'}",
                                   by="Mail Archive"):
                        out["flagged"] += 1
    return out


# ── The retro pass (CLI: python app.py append-emails [apply]) ─────────────────

def retro_append(apply: bool = False, cfg: dict = None, progress=print,
                 out_dir: str = _OPS_DIR) -> dict:
    """Walk every logged record and put its source email(s) at the back of its
    filed PDF — the catch-up for everything filed before this shipped. Dry run
    by default; writes EMAIL_APPEND.html into the ops index either way."""
    cfg = cfg or {}
    entries = database.get_all_entries()
    summary = {"apply": apply, "appended": [], "skipped": [], "total": len(entries),
               "no_email": 0, "flagged": 0}
    for i, e in enumerate(entries, 1):
        # A visible heartbeat: this walks every record against the archive and
        # used to print nothing until the final summary — minutes of silence
        # that read as "hung" at a console (the 2026-07-18 reindex lesson;
        # Joshua interrupted exactly such a run on 2026-08-11).
        if i % 100 == 0 or i == len(entries):
            progress(f"[EmailAppend] {i}/{len(entries)} records looked at — "
                     f"{len(summary['appended'])} "
                     f"{'appended' if apply else 'would append'}, "
                     f"{summary['flagged']} flagged so far")
        has_archive = bool(e.get("file_hash")) and \
            bool(mail_archive.messages_for_hash(e["file_hash"]))
        if not has_archive and not email_pdf.has_email(e):
            summary["no_email"] += 1
            continue
        # Re-send flags belong to the retro too: the live after-sync pass
        # flags as messages arrive, but a message archived while the app was
        # mid-restart never got that pass (2026-08-10 evening — the initial
        # fill was killed twice by deploy restarts, so the whole backlog's
        # flags ride on this pass instead).
        if apply and has_archive and \
                mail_archive.distinct_sends_for_hash(e["file_hash"]) > 1:
            if flag_resend(e, "the same document has arrived under more than "
                              "one separate email (see it on /mail)",
                           by="Email Append (retro)"):
                summary["flagged"] += 1
        res = append_to_entry(e["id"], by="Email Append (retro)",
                              dry=not apply, cfg=cfg)
        if res.get("skipped"):
            summary["skipped"].append(dict(entry_id=e["id"],
                                           record_no=e.get("record_no"),
                                           vendor=e.get("vendor"),
                                           reason=res["skipped"]))
        elif apply and res.get("appended"):
            summary["appended"].append(dict(entry_id=e["id"],
                                            record_no=e.get("record_no"),
                                            vendor=e.get("vendor"),
                                            n=res["appended"]))
        elif not apply and res.get("would_append"):
            summary["appended"].append(dict(entry_id=e["id"],
                                            record_no=e.get("record_no"),
                                            vendor=e.get("vendor"),
                                            n=len(res["would_append"]),
                                            accounts=res.get("accounts")))
    progress(f"[EmailAppend] {'APPLIED' if apply else 'DRY RUN'}: "
             f"{len(summary['appended'])} record(s) "
             f"{'got' if apply else 'would get'} email pages, "
             f"{summary['flagged']} flagged as re-sends, "
             f"{len(summary['skipped'])} skipped, "
             f"{summary['no_email']} have no known source email.")
    _write_artifact(summary, out_dir)
    return summary


def _write_artifact(summary, out_dir):
    """EMAIL_APPEND.html — what got (or would get) its email at the back, and
    the skips a person may want to look at. Registered on /ops-reports."""
    import html as _html
    e = _html.escape
    ran = datetime.now().strftime("%Y-%m-%d %H:%M")
    mode = "APPLIED" if summary["apply"] else "DRY RUN — nothing was written"
    verb = "got" if summary["apply"] else "would get"
    rows_a = "".join(
        f"<tr><td><a href='/entry/{m['entry_id']}/view'>"
        f"{e(str(m.get('record_no') or '#' + str(m['entry_id'])))}</a></td>"
        f"<td>{e(m.get('vendor') or '')}</td><td>{m['n']}"
        f"{(' — ' + e(', '.join(m['accounts']))) if m.get('accounts') else ''}</td></tr>"
        for m in summary["appended"]) or "<tr><td colspan=3>none</td></tr>"
    rows_s = "".join(
        f"<tr><td><a href='/entry/{m['entry_id']}/view'>"
        f"{e(str(m.get('record_no') or '#' + str(m['entry_id'])))}</a></td>"
        f"<td>{e(m.get('vendor') or '')}</td><td>{e(m['reason'])}</td></tr>"
        for m in summary["skipped"]) or "<tr><td colspan=3>none</td></tr>"
    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Email append — {e(mode)}</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#222}}
table{{border-collapse:collapse;margin:8px 0 18px}}td,th{{border:1px solid #ccc;
padding:4px 10px;font-size:.9rem}}th{{background:#f0f4f8;text-align:left}}</style>
</head><body>
<h1>Email append — {e(mode)}</h1>
<p>Run {e(ran)}. Reports #55/#106/#114: the source email(s) — with the
attachment list — appended permanently to the back of each filed PDF. Matched
by exact file hash against the mail archive, falling back to the email stored
on the record itself.</p>
<h2>Records that {verb} email pages — {len(summary['appended'])}</h2>
<table><tr><th>Record</th><th>Vendor</th><th>Emails at the back</th></tr>{rows_a}</table>
<h2>Skipped — {len(summary['skipped'])}</h2>
<table><tr><th>Record</th><th>Vendor</th><th>Why</th></tr>{rows_s}</table>
<p>{summary['no_email']} record(s) have no known source email (physical mail,
or the message predates the archive window) — nothing to append.</p>
</body></html>"""
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "EMAIL_APPEND.html"), "w",
                  encoding="utf-8") as f:
            f.write(doc)
    except OSError as exc:
        print(f"[EmailAppend] could not write artifact: {exc}")
