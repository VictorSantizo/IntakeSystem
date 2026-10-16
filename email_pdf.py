"""email_pdf.py — the stored source email as PDF pages behind the bill.

Karen pays a bill by printing it with its email behind it (training session
2026-08-03; reports #55 + #57, with #8/#26 as the on-screen half). The email
itself lives in intake_log columns (email_from/to/account/subject/date/body)
— there is no .eml on disk — so this module typesets those fields into PDF
pages and appends them to the bill document, giving one printable "packet".

Works on both a logged record and a queued review item: each is a dict with
the same email_* keys. Records logged before the email columns existed
(pre 2026-07-07) have nothing stored, so there is nothing to print — callers
get None and should fall back to the bare document.

Fail-soft throughout: PyMuPDF missing, an unreadable/unsupported document
(.docx), or a record with no stored email all return None, never raise.
"""
import io

try:
    import fitz          # PyMuPDF — also used by indexer.py / ocr.py
except ImportError:      # pragma: no cover - declared in requirements.txt
    fitz = None

# US Letter, 1" side margins, generous top/bottom.
_PAGE = (612, 792)
_MARGIN = 54
_BODY_SIZE = 10
_MAX_PAGES = 30          # safety valve; email_body is capped at 20k chars anyway


def has_email(rec: dict) -> bool:
    """Whether this record/queue item carries a stored source email worth
    printing. The account name alone doesn't count — that's which mailbox
    polled, not email content."""
    return bool(rec.get("email_from") or rec.get("email_subject")
                or rec.get("email_body"))


def _latin(text: str) -> str:
    """PyMuPDF's built-in Helvetica covers Latin-1 only; emails are almost
    always within that. Replace anything outside it rather than failing the
    whole packet over one exotic character."""
    return (text or "").encode("latin-1", "replace").decode("latin-1")


def _fmt_size(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n // 1024} KB"
    return f"{n} B"


def attachments_lines(attachments) -> list:
    """The 'Attachments:' block between the header and the body (Nancy's print
    spec, 2026-08-10): the honest full list, or the literal 'Attachments: None'
    so a reader is never left wondering whether files rode along. Each item:
    {filename, size?, note?} — note is the caller's provenance line (e.g.
    'filed as 260807-001 — \\\\LACKSSERVER2021\\...'). attachments=None means
    the list is UNKNOWN (a pre-archive record), which prints nothing: unknown
    is not the same fact as none."""
    if attachments is None:
        return []
    if not attachments:
        return ["Attachments: None"]
    lines = ["Attachments:"]
    for i, a in enumerate(attachments, 1):
        size = _fmt_size(a.get("size"))
        lines.append(f"  {i}. {a.get('filename') or '(unnamed)'}"
                     + (f"  ({size})" if size else ""))
        if a.get("note"):
            lines.append(f"     {a['note']}")
    return lines


def text_pdf_bytes(text: str, max_pages: int = _MAX_PAGES):
    """Plain text typeset as US-Letter PDF page(s), or None when nothing could
    be set (blank text, PyMuPDF missing). The one text→PDF path in the app —
    the email pages use it, and email_append's Word-document fallback rendering
    does too (2026-08-10: non-PDF documents convert instead of being skipped)."""
    if fitz is None:
        return None
    lines = _latin(text or "")
    doc = fitz.open()
    rect = fitz.Rect(_MARGIN, _MARGIN, _PAGE[0] - _MARGIN, _PAGE[1] - _MARGIN)
    while lines and doc.page_count < max_pages:
        page = doc.new_page(width=_PAGE[0], height=_PAGE[1])
        tw = fitz.TextWriter(page.rect)
        rest = tw.fill_textbox(rect, lines, fontsize=_BODY_SIZE)
        tw.write_text(page)
        # The leftover comes back as (line, width) tuples — turn it back into
        # text for the next page's fill.
        lines = "\n".join(t[0] if isinstance(t, (tuple, list)) else str(t)
                          for t in rest) if rest else ""
    if doc.page_count == 0:                      # nothing typeset at all
        return None
    return doc.tobytes()


def email_pdf_bytes(rec: dict, attachments=None):
    """The stored source email typeset as PDF page(s), or None when the record
    has no stored email (or PyMuPDF is unavailable). `attachments` (see
    attachments_lines) prints the attachment list between header and body."""
    if fitz is None or not has_email(rec):
        return None
    head = ["SOURCE EMAIL  (stored copy, printed with this document)", ""]
    for label, key in (("From", "email_from"), ("To", "email_to"),
                       ("Date", "email_date"), ("Subject", "email_subject"),
                       ("Mailbox", "email_account")):
        if rec.get(key):
            head.append(f"{label}:  {rec[key]}")
    att_block = attachments_lines(attachments)
    if att_block:
        head.append("")
        head.extend(att_block)
    text = "\n".join(head) + "\n" + ("_" * 78) + "\n\n" + (rec.get("email_body") or "")
    return text_pdf_bytes(text)


def packet_pdf_bytes(doc_path: str, rec: dict, attachments=None):
    """One printable PDF: the document first, its source email behind it —
    the order Karen staples paper. None when there's no stored email or the
    document can't be opened (caller falls back to serving the bare file)."""
    email_bytes = email_pdf_bytes(rec, attachments=attachments)
    if email_bytes is None:
        return None
    try:
        src = fitz.open(doc_path)
        if not src.is_pdf:                       # image scan → PDF page(s)
            converted = src.convert_to_pdf()
            src.close()
            src = fitz.open("pdf", converted)
    except Exception:
        return None                              # .docx, missing file, etc.
    out = fitz.open()
    out.insert_pdf(src)
    out.insert_pdf(fitz.open("pdf", email_bytes))
    return out.tobytes()
