"""Multi-account email polling: config migration, IMAP/POP3 pollers (mocked
imaplib/poplib — no network), per-account error isolation, and the Settings
mailbox add/update/delete routes.
"""
import email
import imaplib
import json
import os
import poplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime

import app as klr
import email_poller


# ── Test doubles ───────────────────────────────────────────────────────────────

def _email_bytes(msgid, days_ago=0, subject="Water bill", pdf_name="bill.pdf"):
    """A real RFC-5322 message with a PDF attachment, CRLF line endings."""
    m = EmailMessage()
    m["Message-ID"] = msgid
    m["From"] = "City Water <billing@water.example>"
    m["Subject"] = subject
    m["Date"] = format_datetime(datetime.now(timezone.utc) - timedelta(days=days_ago))
    m.set_content("Your bill is attached.")
    m.add_attachment(b"%PDF-1.4 fake bill", maintype="application",
                     subtype="pdf", filename=pdf_name)
    return m.as_bytes(policy=SMTP)


def _fake_imap_cls(messages, fail_login=False, seen=None):
    """imaplib.IMAP4_SSL stand-in serving `messages` (list of raw bytes)."""
    log = seen if seen is not None else {}
    log.setdefault("hosts", [])

    class _FakeIMAP:
        def __init__(self, host, port=993, timeout=None):
            log["hosts"].append((host, port))
            log["timeout"] = timeout

        def login(self, user, pw):
            if fail_login:
                raise imaplib.IMAP4.error("AUTHENTICATIONFAILED bad credentials")

        def select(self, box, readonly=False):
            log["readonly"] = readonly

        def search(self, charset, criteria):
            nums = b" ".join(str(i + 1).encode() for i in range(len(messages)))
            return "OK", [nums]

        def fetch(self, num, spec):
            raw = messages[int(num) - 1]
            if "HEADER.FIELDS" in spec:
                mid = email.message_from_bytes(raw)["Message-ID"]
                return "OK", [(b"1 (BODY[HEADER])", f"Message-ID: {mid}\r\n\r\n".encode())]
            return "OK", [(b"1 (BODY[])", raw), b")"]

        def logout(self):
            pass

    return _FakeIMAP


def _fake_pop3_cls(messages, rec=None, fail_login=False, no_top=False):
    """poplib.POP3_SSL stand-in. `rec` collects the commands issued so tests
    can assert everything goes through TOP (RETR marks mail read on many real
    servers) and that DELE is never sent. `no_top` simulates the rare server
    without TOP support, where RETR is the only option."""
    log = rec if rec is not None else {}
    log.setdefault("top", [])
    log.setdefault("retr", [])
    log.setdefault("dele", [])

    class _FakePOP3:
        def __init__(self, host, port=995, timeout=None):
            log["host"] = (host, port)

        def user(self, u):
            pass

        def pass_(self, p):
            if fail_login:
                raise poplib.error_proto("-ERR authentication failed")

        def stat(self):
            return (len(messages), sum(len(m) for m in messages))

        def top(self, num, nlines):
            if no_top:
                raise poplib.error_proto("-ERR TOP not supported")
            log["top"].append((num, nlines))
            headers, _, body = messages[num - 1].partition(b"\r\n\r\n")
            lines = headers.split(b"\r\n")
            if nlines:   # TOP n returns headers + first n body lines
                lines += [b""] + body.split(b"\r\n")[:nlines]
            return (b"+OK", lines, sum(len(l) for l in lines))

        def retr(self, num):
            log["retr"].append(num)
            raw = messages[num - 1]
            return (b"+OK", raw.split(b"\r\n"), len(raw))

        def dele(self, num):
            log["dele"].append(num)
            raise AssertionError("DELE must never be issued — mailbox is read-only")

        def quit(self):
            pass

    return _FakePOP3


def _imap_account(**over):
    a = {"id": 1, "name": "Gmail intake", "protocol": "imap", "host": "imap.gmail.com",
         "port": 993, "ssl": True, "username": "gm@example.com", "password": "pw",
         "enabled": True, "lookback_days": 30}
    a.update(over)
    return a


def _pop3_account(**over):
    a = {"id": 2, "name": "Larry POP3", "protocol": "pop3", "host": "mail.hiwaay.net",
         "port": 995, "ssl": True, "username": "larryram@silcom.com", "password": "pw",
         "enabled": True, "lookback_days": 3}
    a.update(over)
    return a


def _dedup():
    """(is_processed, mark_processed, the-set) backed by a plain set."""
    done = set()
    return (lambda mid: mid in done), done.add, done


# ── Config migration ───────────────────────────────────────────────────────────

def test_migration_converts_legacy_gmail(env):
    cfg = klr.load_config()
    del cfg["mail_accounts"]
    cfg["gmail_user"] = "assistklr2@gmail.com"
    cfg["gmail_app_password"] = "apppassword16chr"
    klr.save_config(cfg)

    klr.migrate_mail_accounts()
    cfg = klr.load_config()
    assert "gmail_user" not in cfg and "gmail_app_password" not in cfg
    (acct,) = cfg["mail_accounts"]
    # The already-clean production account keeps its exact old behavior.
    assert acct["protocol"] == "imap" and acct["host"] == "imap.gmail.com"
    assert acct["port"] == 993 and acct["ssl"] is True
    assert acct["lookback_days"] == 30
    assert acct["username"] == "assistklr2@gmail.com"
    assert acct["password"] == "apppassword16chr"
    assert acct["enabled"] is True

    # Idempotent: a second run must not duplicate or reset anything.
    klr.migrate_mail_accounts()
    assert klr.load_config()["mail_accounts"] == [acct]


def test_migration_without_legacy_keys_yields_empty_list(env):
    cfg = klr.load_config()
    del cfg["mail_accounts"]
    klr.save_config(cfg)
    klr.migrate_mail_accounts()
    assert klr.load_config()["mail_accounts"] == []


def test_migration_never_touches_existing_list(env):
    cfg = klr.load_config()
    cfg["mail_accounts"] = [_pop3_account()]
    cfg["gmail_user"] = "stray@gmail.com"   # leftover key must not re-migrate
    klr.save_config(cfg)
    klr.migrate_mail_accounts()
    assert klr.load_config()["mail_accounts"] == [_pop3_account()]


def test_unmigrated_cfg_still_polls_gmail(env, monkeypatch):
    """Belt and braces: a pre-migration config (e.g. an old backup restored at
    runtime) must keep polling via the synthesized legacy account."""
    seen = {}
    monkeypatch.setattr(email_poller.imaplib, "IMAP4_SSL",
                        _fake_imap_cls([_email_bytes("<a@x>")], seen=seen))
    cfg = {"gmail_user": "assistklr2@gmail.com", "gmail_app_password": "pw"}
    is_done, mark, _ = _dedup()
    saved, err = email_poller.poll_email_once(cfg, env.email_folder, is_done, mark)
    assert (saved, err) == (1, None)
    assert seen["hosts"] == [("imap.gmail.com", 993)]


# ── IMAP path (behavior preserved from the single-account poller) ──────────────

def test_imap_saves_attachment_meta_and_dedups(env, monkeypatch):
    msgs = [_email_bytes("<seen@x>", pdf_name="old.pdf"),
            _email_bytes("<new@x>", pdf_name="water.pdf")]
    seen = {}
    monkeypatch.setattr(email_poller.imaplib, "IMAP4_SSL", _fake_imap_cls(msgs, seen=seen))
    cfg = {"mail_accounts": [_imap_account()]}
    is_done, mark, done = _dedup()
    done.add("<seen@x>")

    saved, err = email_poller.poll_email_once(cfg, env.email_folder, is_done, mark)
    assert (saved, err) == (1, None)
    assert seen["readonly"] is True            # never alters the mailbox
    assert "<new@x>" in done
    dest = os.path.join(env.email_folder, "water.pdf")
    assert os.path.exists(dest)
    assert not os.path.exists(os.path.join(env.email_folder, "old.pdf"))
    with open(dest + ".meta.json", encoding="utf-8") as f:
        meta = json.load(f)
    assert meta["from"].startswith("City Water")
    assert meta["subject"] == "Water bill"
    assert meta["received_date"] == datetime.now().date().isoformat()


def test_imap_connection_has_a_timeout(env, monkeypatch):
    # Regression for the 2026-07-25 review: an unbounded IMAP connection
    # could hang forever and, via the single non-reentrant poll lock, silently
    # stop email intake for every account until the app restarts. POP3 already
    # had a timeout; IMAP now matches it.
    seen = {}
    monkeypatch.setattr(email_poller.imaplib, "IMAP4_SSL",
                        _fake_imap_cls([], seen=seen))
    cfg = {"mail_accounts": [_imap_account()]}
    is_done, mark, _ = _dedup()
    email_poller.poll_email_once(cfg, env.email_folder, is_done, mark)
    assert seen["timeout"] == 60


# ── POP3 path ──────────────────────────────────────────────────────────────────

def test_pop3_lookback_dedup_and_readonly(env, monkeypatch):
    # msg nums 1..3, oldest first: too old / already processed / new.
    msgs = [_email_bytes("<old@x>", days_ago=10, pdf_name="ancient.pdf"),
            _email_bytes("<seen@x>", pdf_name="dupe.pdf"),
            _email_bytes("<new@x>", pdf_name="power.pdf")]
    rec = {}
    monkeypatch.setattr(email_poller.poplib, "POP3_SSL", _fake_pop3_cls(msgs, rec=rec))
    cfg = {"mail_accounts": [_pop3_account(lookback_days=3)]}
    is_done, mark, done = _dedup()
    done.add("<seen@x>")

    saved, err = email_poller.poll_email_once(cfg, env.email_folder, is_done, mark)
    assert (saved, err) == (1, None)
    # RETR is never issued when TOP works — RETR flags mail read on many
    # servers, which destroyed Larry's unread-based triage (2026-07-17).
    assert rec["retr"] == []
    # Newest first: header peek each message, full TOP pull only for the new one.
    assert rec["top"] == [(3, 0), (3, email_poller._POP3_TOP_ALL), (2, 0), (1, 0)]
    assert rec["dele"] == []                   # mailbox left untouched
    assert "<new@x>" in done and "<old@x>" not in done
    assert os.path.exists(os.path.join(env.email_folder, "power.pdf"))
    assert os.path.exists(os.path.join(env.email_folder, "power.pdf.meta.json"))
    assert not os.path.exists(os.path.join(env.email_folder, "ancient.pdf"))


def test_pop3_no_top_server_still_ingests_via_retr(env, monkeypatch):
    """A server without TOP (rare) still gets polled — RETR is the only fetch
    POP3 offers there, accepting that such a server may mark the mail read."""
    rec = {}
    monkeypatch.setattr(email_poller.poplib, "POP3_SSL",
                        _fake_pop3_cls([_email_bytes("<nt@x>", pdf_name="gas.pdf")],
                                       rec=rec, no_top=True))
    cfg = {"mail_accounts": [_pop3_account()]}
    is_done, mark, _ = _dedup()
    saved, err = email_poller.poll_email_once(cfg, env.email_folder, is_done, mark)
    assert (saved, err) == (1, None)
    assert rec["retr"] == [1, 1]               # header peek + full pull
    assert os.path.exists(os.path.join(env.email_folder, "gas.pdf"))


def test_pop3_plain_uses_pop3_class(env, monkeypatch):
    rec = {}
    monkeypatch.setattr(email_poller.poplib, "POP3",
                        _fake_pop3_cls([_email_bytes("<p@x>")], rec=rec))
    cfg = {"mail_accounts": [_pop3_account(ssl=False, port=110)]}
    is_done, mark, _ = _dedup()
    saved, err = email_poller.poll_email_once(cfg, env.email_folder, is_done, mark)
    assert (saved, err) == (1, None)
    assert rec["host"] == ("mail.hiwaay.net", 110)


# ── Multi-account: one failure never stops the others ──────────────────────────

def test_one_account_failing_does_not_stop_the_rest(env, monkeypatch):
    email_poller._poll_status.clear()
    monkeypatch.setattr(email_poller.imaplib, "IMAP4_SSL",
                        _fake_imap_cls([], fail_login=True))
    monkeypatch.setattr(email_poller.poplib, "POP3_SSL",
                        _fake_pop3_cls([_email_bytes("<ok@x>")]))
    cfg = {"mail_accounts": [_imap_account(), _pop3_account()]}
    is_done, mark, _ = _dedup()

    saved, err = email_poller.poll_email_once(cfg, env.email_folder, is_done, mark)
    assert saved == 1                          # the healthy mailbox still polled
    assert "Gmail intake" in err and "IMAP login error" in err

    status = email_poller.get_poll_status()    # per-account detail for Settings
    assert status["Gmail intake"]["error"]
    assert status["Larry POP3"]["error"] is None
    assert status["Larry POP3"]["saved"] == 1


def test_disabled_and_unconfigured_accounts_are_skipped(env):
    cfg = {"mail_accounts": [_imap_account(enabled=False),
                             _pop3_account(password="")]}
    assert email_poller.configured_accounts(cfg) == []
    # No network mocks installed: this would explode if it tried to connect.
    is_done, mark, _ = _dedup()
    assert email_poller.poll_email_once(cfg, env.email_folder, is_done, mark) == (0, None)


# ── Settings routes ────────────────────────────────────────────────────────────

def test_add_mailbox_short_default_lookback(admin_client):
    c, headers = admin_client
    assert email_poller.DEFAULT_LOOKBACK_NEW == 3   # new-account scoping decision
    r = c.post("/settings/mail/add", headers=headers, data={
        "name": "", "protocol": "pop3", "host": "mail.hiwaay.net", "port": "",
        "username": "larryram@silcom.com", "password": "se cret",
        "lookback_days": ""})
    assert r.status_code == 302
    (acct,) = klr.load_config()["mail_accounts"]
    assert acct["id"] == 1 and acct["enabled"] is True
    assert acct["name"] == "larryram@silcom.com"    # falls back to username
    assert acct["port"] == 110 and acct["ssl"] is False   # plain POP3 default
    assert acct["password"] == "secret"             # spaces stripped
    assert acct["lookback_days"] == 3               # short default for new boxes


def test_add_mailbox_requires_password(admin_client):
    c, headers = admin_client
    c.post("/settings/mail/add", headers=headers, data={
        "protocol": "imap", "host": "imap.x.com", "ssl": "on",
        "username": "u@x.com", "password": ""})
    assert klr.load_config()["mail_accounts"] == []


def test_update_blank_password_keeps_saved_one(admin_client):
    c, headers = admin_client
    c.post("/settings/mail/add", headers=headers, data={
        "name": "Karen", "protocol": "pop3", "host": "secure.emailsrvr.com",
        "ssl": "on", "username": "karen@klacks.com", "password": "orig",
        "lookback_days": "3"})
    c.post("/settings/mail/1/update", headers=headers, data={
        "name": "Karen", "protocol": "pop3", "host": "secure.emailsrvr.com",
        "port": "995", "ssl": "on", "username": "karen@klacks.com",
        "password": "", "lookback_days": "7"})     # enabled checkbox unticked
    (acct,) = klr.load_config()["mail_accounts"]
    assert acct["password"] == "orig"               # blank = keep the saved one
    assert acct["lookback_days"] == 7
    assert acct["enabled"] is False                 # unticked box disables it


def test_delete_mailbox(admin_client):
    c, headers = admin_client
    c.post("/settings/mail/add", headers=headers, data={
        "protocol": "imap", "host": "imap.x.com", "ssl": "on",
        "username": "u@x.com", "password": "pw"})
    r = c.post("/settings/mail/1/delete", headers=headers)
    assert r.status_code == 302
    assert klr.load_config()["mail_accounts"] == []
    r = c.post("/settings/mail/1/delete", headers=headers)   # gone already
    assert r.status_code == 302                     # graceful, no crash


def test_mail_routes_are_admin_gated(staff_client):
    c, headers = staff_client
    c.post("/settings/mail/add", headers=headers, data={
        "protocol": "imap", "host": "imap.x.com",
        "username": "u@x.com", "password": "pw"})
    assert klr.load_config().get("mail_accounts") == []


def test_settings_page_lists_mailboxes(admin_client):
    c, headers = admin_client
    c.post("/settings/mail/add", headers=headers, data={
        "name": "Karen", "protocol": "pop3", "host": "secure.emailsrvr.com",
        "ssl": "on", "username": "karen@klacks.com", "password": "pw"})
    page = c.get("/settings").data.decode()
    assert "secure.emailsrvr.com" in page
    assert "Add a mailbox" in page
    assert 'value="pw"' not in page                 # saved password never echoed


# ── poplib._MAXLINE guard (2026-07-27 code review, GH #9) ───────────────────────
# The monkeypatch above (email_poller.py) sets a private poplib attribute whose
# name/behavior isn't guaranteed across Python versions. These pin the assumption
# so a future Python upgrade that silently changes it fails LOUDLY at test time,
# not by quietly reintroducing the "mailbox never delivers" incident (2026-07-07).

def test_poplib_maxline_patch_is_still_in_effect():
    assert poplib._MAXLINE >= 10 * 1024 * 1024


def test_real_poplib_round_trips_an_oversized_header_line():
    """End-to-end against the REAL poplib.POP3 (not the test double above): a
    tiny hand-rolled POP3 server serves one message whose first header line is
    ~50 KB — comfortably past poplib's stock 2048-byte cap and exactly the shape
    that killed Karen's Inbox polling before this patch. Must not raise
    poplib.error_proto('line too long')."""
    import socket
    import threading

    big_line = b"References: <" + b"x" * 50_000 + b"@example.com>\r\n"
    msg = (big_line + b"Subject: test\r\n\r\nBody line.\r\n")
    msg_lines = msg.split(b"\r\n")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve_one():
        conn, _ = srv.accept()
        try:
            f = conn.makefile("rwb")
            f.write(b"+OK POP3 ready\r\n"); f.flush()
            while True:
                line = f.readline()
                if not line:
                    break
                cmd = line.strip().upper()
                if cmd.startswith(b"USER") or cmd.startswith(b"PASS"):
                    f.write(b"+OK\r\n")
                elif cmd.startswith(b"RETR") or cmd.startswith(b"TOP"):
                    f.write(b"+OK message follows\r\n")
                    for l in msg_lines:
                        f.write(l + b"\r\n")
                    f.write(b".\r\n")
                elif cmd.startswith(b"QUIT"):
                    f.write(b"+OK bye\r\n")
                    f.flush()
                    break
                else:
                    f.write(b"+OK\r\n")
                f.flush()
        finally:
            conn.close()

    t = threading.Thread(target=serve_one, daemon=True)
    t.start()
    try:
        client = poplib.POP3("127.0.0.1", port, timeout=5)
        client.user("tester")
        client.pass_("secret")
        resp, lines, octets = client.retr(1)   # would raise error_proto without the patch
        assert any(b"References:" in l for l in lines)
        client.quit()
    finally:
        t.join(timeout=5)
        srv.close()
