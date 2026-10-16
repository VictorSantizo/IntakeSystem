"""
email_poller.py — Pull bill attachments from the office mailboxes.

Polls every enabled account in config's `mail_accounts` list — IMAP or POP3 —
saves their PDF/image/Word attachments into the email folder (where the folder
watcher picks them up and the AI sorts bills from junk), and records each
message so it's never pulled twice. Dedup is by Message-ID, which is globally
unique, so the same message arriving through two mailboxes (e.g. a box that
also forwards into Gmail) still only ingests once.

Read-only by intent, but only the IMAP path can guarantee it: readonly SELECT
plus BODY.PEEK means the server itself refuses flag changes (and only the
INBOX is ever looked at — Spam and Trash are left alone). The POP3 path never
issues DELE ("leave a copy on the server" is respected) and pulls whole
messages with TOP rather than RETR, but whether the server flags TOP'd or
RETR'd mail as read is the server's choice — MagicMail marks read on both
(see _POP3_TOP_ALL). Prefer IMAP for every account; POP3 is the last resort.
"""
import imaplib, poplib, email, os, re, json, threading

# poplib's default per-line cap (2048 bytes) is smaller than real-world email headers
# (oversized DKIM/References lines are common in marketing mail). One such message in a
# mailbox killed EVERY poll of that account with "line too long" — the newest-first walk
# re-hit the same message each cycle, so the box never delivered a single email
# (Karen's Inbox, 2026-07-07 → 07-14). 10 MB also covers pathological single-line
# bodies during full RETR.
poplib._MAXLINE = 10 * 1024 * 1024

from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import date, datetime, timedelta

# Only one poll may run at a time. The 5-minute background loop and the Check for new items
# button can overlap, and two concurrent sessions would each see the same
# not-yet-marked messages and save their attachments twice.
_poll_lock = threading.Lock()

# New accounts look back only a few days: any mailbox added after launch had
# its history flowing into the already-polled Gmail account (via Gmail's own
# "check mail from other accounts" fetcher), so asking a new connection for a
# month of old mail would be redundant at best and a slow first poll at worst.
# The migrated original Gmail account keeps its 30-day window (see
# legacy_gmail_account). Editable per account in Settings.
DEFAULT_LOOKBACK_NEW = 3

# POP3 can't be asked for "messages since <date>" the way IMAP SEARCH can, so
# _poll_pop3_account walks the mailbox newest-first and stops after this many
# CONSECUTIVE messages older than the lookback window — a streak, not a single
# hit, because Date headers can sit slightly out of arrival order (a message
# sent days ago but delivered today must not end the scan early).
_POP3_OLD_STREAK = 25

# Full messages are pulled with TOP <n> <lines>, not RETR: TOP with a line
# count no real body reaches returns the entire message, and on Dovecot-style
# servers only RETR flags the message as read (Larry works his inbox by unread
# status; RETR polls were marking everything read within minutes of arrival,
# 2026-07-17). RETR survives only as the fallback for servers with no TOP at
# all — same value fetchmail has used for decades.
#
# 2026-08-04: that assumption is NOT universal — MagicMail (mail.hiwaay.net)
# flags \Seen on TOP too, so POP3 polling can never be flag-safe there and
# Larry's inbox kept getting marked read despite the TOP fix. Any mailbox
# whose owner triages by unread/bold must be configured as IMAP instead: the
# IMAP path selects readonly (EXAMINE), where the protocol itself forbids
# flag changes. Keep POP3 mode only for hosts that offer nothing else.
_POP3_TOP_ALL = 99_999_999

IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
DOCX_CTYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Last poll result per account label, surfaced on the Settings page so a bad
# password or unreachable host is visible where it can be fixed. Written only
# under _poll_lock; readers get a shallow copy.
_poll_status = {}   # label -> {"when": str, "saved": int, "error": str|None}

# Mailbox storage as % of its limit, per account label (IMAP QUOTA — Gmail
# supports it; POP3 has no equivalent). A FULL box silently stops receiving:
# assistklr2@gmail.com sat at 165% and bounced every bill for 18 days before
# anyone knew (2026-08-10). mail_health turns >=100% into the red banner.
_quota_pct = {}


def _imap_quota_pct(M):
    """This connection's mailbox storage as a rounded percentage of its
    limit, or None when the server has no QUOTA support. Metadata-only."""
    try:
        typ, data = M.getquotaroot("INBOX")
        if typ != "OK":
            return None
        leaves = []
        for part in (data or []):
            leaves.extend(part if isinstance(part, list) else [part])
        for item in leaves:
            if item is None:
                continue
            s = (item.decode("utf-8", "ignore")
                 if isinstance(item, (bytes, bytearray)) else str(item))
            m = re.search(r"STORAGE (\d+) (\d+)", s)
            if m and int(m.group(2)):
                return round(100 * int(m.group(1)) / int(m.group(2)))
        return None
    except Exception:
        return None


def get_poll_status():
    """Last poll outcome per account label (for the Settings page)."""
    return dict(_poll_status)


def legacy_gmail_account(user, pw):
    """The single Gmail account every install had before `mail_accounts`
    existed, expressed as a mail_accounts entry. Keeps the historical 30-day
    lookback: that account's mail is already ingested, and Message-ID dedup
    makes re-checking the window cheap."""
    return {"id": 1, "name": user or "Gmail intake", "protocol": "imap",
            "host": "imap.gmail.com", "port": 993, "ssl": True,
            "username": user, "password": pw, "enabled": True,
            "lookback_days": 30}


def _accounts_from_cfg(cfg):
    """Enabled accounts from config. When `mail_accounts` doesn't exist at all
    (a config written before the migration, or an old backup restored while the
    app is running), synthesize the classic gmail_user/gmail_app_password pair
    so polling never silently stops; migrate_mail_accounts() in app.py persists
    the same conversion at startup."""
    accounts = cfg.get("mail_accounts")
    if accounts is None:
        user = (cfg.get("gmail_user") or "").strip()
        pw = (cfg.get("gmail_app_password") or "").strip()
        accounts = [legacy_gmail_account(user, pw)] if user else []
    return [a for a in accounts if a.get("enabled", True)]


def configured_accounts(cfg):
    """Enabled accounts that actually have credentials — the ones a poll will
    talk to. Empty means email intake isn't set up (callers treat as no-op)."""
    return [a for a in _accounts_from_cfg(cfg)
            if (a.get("username") or "").strip() and (a.get("password") or "").strip()]


def _safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name or "").strip()
    return name or "email_attachment.pdf"


def _decode_filename(raw):
    if not raw:
        return ""
    try:
        return "".join(
            (b.decode(enc or "utf-8", "ignore") if isinstance(b, bytes) else b)
            for b, enc in decode_header(raw))
    except Exception:
        return raw


def _extract_body(msg, limit=20000):
    """Best-effort plain-text body of an email (for storing/showing the source —
    Tier 4 #14/#17). Prefers text/plain; falls back to a crude HTML strip. Skips
    attachment parts. Truncated to `limit` chars."""
    def _decode(part):
        payload = part.get_payload(decode=True)
        if not payload:
            return ""
        return payload.decode(part.get_content_charset() or "utf-8", "ignore")
    body = ""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and not part.get_filename():
                    body = _decode(part)
                    if body:
                        break
            if not body:
                for part in msg.walk():
                    if part.get_content_type() == "text/html" and not part.get_filename():
                        body = re.sub(r"<[^>]+>", " ", _decode(part))   # crude tag strip
                        if body:
                            break
        else:
            body = _decode(msg)
            if msg.get_content_type() == "text/html":
                body = re.sub(r"<[^>]+>", " ", body)
    except Exception:
        body = ""
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()[:limit]


def _msg_local_date(msg):
    """The message's own Date header as a local calendar date, or None. This is
    the mail-received date Karen actually operates off of (Tier 1 #1) — the
    email's own date, not when we happened to pull it."""
    try:
        dt = parsedate_to_datetime(msg.get("Date"))
        if dt is None:
            return None
        try:
            dt = dt.astimezone()   # express in our local calendar date
        except Exception:
            pass
        return dt.date()
    except Exception:
        return None


def _save_attachments(msg, email_folder, account=None):
    """Save every bill-shaped attachment (PDF / photo / .docx) of a parsed
    message into email_folder, each with a .meta.json sidecar (received date,
    from, to, subject, body, which mailbox pulled it) so enqueue_scan can stamp
    the record. Returns the number of files saved."""
    received = _msg_local_date(msg)
    meta = {
        "received_date": received.isoformat() if received else None,
        "from": _decode_filename(msg.get("From")),
        # Who the mail was addressed to, and which configured mailbox fetched
        # it — with several boxes polled, staff need to see whose mail this was.
        "to": _decode_filename(msg.get("To")),
        "account": (account or {}).get("name") or "",
        "subject": _decode_filename(msg.get("Subject")),
        "email_date_raw": msg.get("Date"),
        "body": _extract_body(msg),
    }
    saved = 0
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
        # Skip tiny images — they're almost always signature logos / inline
        # graphics, not a photographed bill (which is hundreds of KB+).
        if is_image and not (is_pdf or is_docx) and len(payload) < 25000:
            continue
        base = _safe_name(fname or "email_attachment.pdf")
        if "." not in base:
            base += ".pdf"
        dest = os.path.join(email_folder, base)
        root, ext = os.path.splitext(dest)
        c = 1
        while os.path.exists(dest):
            dest = f"{root} ({c}){ext}"
            c += 1
        # Write the sidecar FIRST so it's already present when the folder
        # watcher fires on the document file (the watcher ignores .meta.json).
        try:
            with open(dest + ".meta.json", "w", encoding="utf-8") as mf:
                json.dump(meta, mf)
        except Exception as e:
            print(f"[Email] could not write meta for {dest}: {e}")
        with open(dest, "wb") as f:
            f.write(payload)
        saved += 1
    return saved


def _poll_imap_account(account, email_folder, is_processed, mark_processed):
    """One IMAP mailbox — the same behavior the poller always had for Gmail,
    with host/port/ssl/lookback read from the account entry."""
    use_ssl = account.get("ssl", True)
    lookback = int(account.get("lookback_days") or DEFAULT_LOOKBACK_NEW)
    saved = 0
    M = None
    try:
        opener = imaplib.IMAP4_SSL if use_ssl else imaplib.IMAP4
        M = opener(account["host"], int(account.get("port") or (993 if use_ssl else 143)),
                   timeout=60)
        M.login(account["username"], account["password"])
        M.select("INBOX", readonly=True)   # read-only: never alters the mailbox
        _quota_pct[account.get("name") or account.get("username")
                   or account.get("host") or "account"] = _imap_quota_pct(M)
        since = (date.today() - timedelta(days=lookback)).strftime("%d-%b-%Y")
        typ, data = M.search(None, f'(SINCE {since})')
        nums = data[0].split() if (data and data[0]) else []
        for num in nums:
            # Peek just the Message-ID first (cheap, doesn't mark as read)
            typ, hdr = M.fetch(num, '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
            msgid = ""
            if hdr and hdr[0] and isinstance(hdr[0], tuple):
                m = re.search(r'Message-ID:\s*(\S+)', hdr[0][1].decode("utf-8", "ignore"),
                              re.IGNORECASE)
                if m:
                    msgid = m.group(1).strip()
            if not msgid or is_processed(msgid):
                continue
            typ, full = M.fetch(num, '(BODY.PEEK[])')
            if not full or not full[0] or not isinstance(full[0], tuple):
                continue
            msg = email.message_from_bytes(full[0][1])
            saved += _save_attachments(msg, email_folder, account)
            mark_processed(msgid)   # mark handled even if it had no attachments
        return saved, None
    except imaplib.IMAP4.error as e:
        return saved, f"IMAP login error: {e}. Check the address and password."
    except Exception as e:
        return saved, f"{type(e).__name__}: {e}"
    finally:
        try:
            if M is not None:
                M.logout()
        except Exception:
            pass


def _pop3_fetch(conn, num, nlines):
    """TOP-first fetch (see _POP3_TOP_ALL — RETR marks mail read on many
    servers); RETR only when the server has no TOP at all."""
    try:
        return conn.top(num, nlines)[1]
    except poplib.error_proto:
        return conn.retr(num)[1]


def _poll_pop3_account(account, email_folder, is_processed, mark_processed):
    """One POP3 mailbox. POP3 has no server-side search, so headers are fetched
    first (TOP <n> 0 returns headers without the body) to read Date and
    Message-ID, and the full message is only pulled for new mail inside the
    lookback window — via TOP, not RETR, so the messages stay unread (see
    _POP3_TOP_ALL). Never issues DELE — the mailbox is left exactly as found,
    like the IMAP path's readonly=True."""
    use_ssl = account.get("ssl", True)
    lookback = int(account.get("lookback_days") or DEFAULT_LOOKBACK_NEW)
    cutoff = date.today() - timedelta(days=lookback)
    saved = 0
    conn = None
    try:
        opener = poplib.POP3_SSL if use_ssl else poplib.POP3
        conn = opener(account["host"], int(account.get("port") or (995 if use_ssl else 110)),
                      timeout=60)
        conn.user(account["username"])
        conn.pass_(account["password"])
        count = conn.stat()[0]
        old_streak = 0
        for num in range(count, 0, -1):   # newest first — see _POP3_OLD_STREAK
            hdr = email.message_from_bytes(b"\r\n".join(_pop3_fetch(conn, num, 0)))
            msg_date = _msg_local_date(hdr)
            if msg_date is not None:
                if msg_date < cutoff:
                    old_streak += 1
                    if old_streak >= _POP3_OLD_STREAK:
                        break
                    continue
                old_streak = 0
            # A message with no parseable Date is treated as recent, never skipped.
            msgid = (hdr.get("Message-ID") or "").strip()
            if not msgid or is_processed(msgid):
                continue
            msg = email.message_from_bytes(
                b"\r\n".join(_pop3_fetch(conn, num, _POP3_TOP_ALL)))
            saved += _save_attachments(msg, email_folder, account)
            mark_processed(msgid)   # mark handled even if it had no attachments
        return saved, None
    except poplib.error_proto as e:
        return saved, f"POP3 error: {e}. Check the mailbox host/port/SSL settings and password."
    except Exception as e:
        return saved, f"{type(e).__name__}: {e}"
    finally:
        try:
            if conn is not None:
                conn.quit()
        except Exception:
            pass


def poll_email_once(cfg, email_folder, is_processed, mark_processed):
    """Pull new attachments from every enabled mail account into email_folder.

    is_processed(message_id)->bool and mark_processed(message_id) handle dedup so
    the same email is never pulled twice, even across mailboxes. Returns
    (total_saved, combined_error_or_None); one account failing (bad password,
    unreachable host) never stops the others — its error is reported prefixed
    with the account name, and per-account detail lands in get_poll_status().
    Silent no-op (0, None) when no mailbox is configured yet, or when another
    poll is already in progress (the overlapping caller just skips its turn)."""
    if not _poll_lock.acquire(blocking=False):
        return 0, None
    try:
        return _poll_all_locked(cfg, email_folder, is_processed, mark_processed)
    finally:
        _poll_lock.release()


def _poll_all_locked(cfg, email_folder, is_processed, mark_processed):
    accounts = configured_accounts(cfg)
    if not accounts:
        return 0, None
    if not email_folder:
        return 0, "No email folder is configured."
    try:
        os.makedirs(email_folder, exist_ok=True)
    except Exception as e:
        return 0, f"Email folder not writable: {e}"

    total, errors, seen = 0, [], set()
    for a in accounts:
        label = a.get("name") or a.get("username") or a.get("host") or "account"
        pop3 = (a.get("protocol") or "imap").strip().lower() == "pop3"
        poller = _poll_pop3_account if pop3 else _poll_imap_account
        saved, err = poller(a, email_folder, is_processed, mark_processed)
        total += saved
        seen.add(label)
        # fails = CONSECUTIVE failing polls. One blip is network weather; a
        # streak is a dead password or host, and mail_health turns a streak
        # into the every-page red banner (Joshua 2026-08-10: a mailbox that
        # stops flowing must never fail silently).
        prev_fails = (_poll_status.get(label) or {}).get("fails", 0)
        _poll_status[label] = {"when": datetime.now().strftime("%Y-%m-%d %H:%M"),
                               "saved": saved, "error": err,
                               "fails": (prev_fails + 1) if err else 0,
                               "quota_pct": _quota_pct.get(label)}
        if err:
            errors.append(f"{label}: {err}")
    # Drop status rows for renamed/removed accounts so Settings only shows live ones.
    for stale in [k for k in _poll_status if k not in seen]:
        del _poll_status[stale]
    return total, ("; ".join(errors) or None)
