"""mail_health — the "mail stopped flowing" watchdog and its every-page red
banner (Joshua 2026-08-10). Three signals: repeated poll failures (debounced),
a silent poll loop, and a mailbox gone quiet in the archive."""
from datetime import datetime

import email.message

import app as klr
import mail_archive
import mail_health

NOW = datetime(2026, 8, 10, 12, 0)
CFG = {"mail_accounts": [
    {"id": 2, "name": "Larry's Inbox", "protocol": "imap", "host": "x",
     "username": "u", "password": "p", "enabled": True}]}


def _archive(date_str):
    m = email.message.EmailMessage()
    m["From"] = "billing@entrusted.com"
    m["To"] = "larry@silcom.com"
    m["Subject"] = "a bill"
    m["Date"] = f"{datetime.strptime(date_str, '%Y-%m-%d'):%a, %d %b %Y} 09:00:00 -0700"
    m["Message-ID"] = f"<{date_str}@x>"
    m.set_content("hello")
    mail_archive.store_message({"id": 2, "name": "Larry's Inbox"}, m)


def _mark_synced(name="Larry's Inbox", acct_id=2):
    """A completed sync for the box — the quiet-mailbox signal's gate."""
    with mail_archive._conn() as conn:
        conn.execute("INSERT OR REPLACE INTO sync_state (account_id, "
                     "account_name, protocol, last_synced, last_result) "
                     "VALUES (?,?,?,datetime('now','localtime'),?)",
                     (acct_id, name, "imap", "ok"))
        conn.commit()


def test_healthy_mail_is_quiet(env):
    _archive("2026-08-09")     # mail seen yesterday
    _mark_synced()
    status = {"Larry's Inbox": {"when": "2026-08-10 11:58", "saved": 1,
                                "error": None, "fails": 0}}
    assert mail_health.check(CFG, status, app_started="2026-08-10 08:00",
                             now=NOW) == []


def test_no_accounts_configured_is_not_a_problem(env):
    assert mail_health.check({"mail_accounts": []}, {}, "2026-08-10 08:00",
                             now=NOW) == []


def test_repeated_poll_failure_flags_but_one_blip_does_not(env):
    st = {"Larry's Inbox": {"when": "2026-08-10 11:58", "saved": 0,
                            "error": "IMAP login error: bad password",
                            "fails": 1}}
    assert mail_health.check(CFG, st, "2026-08-10 08:00", now=NOW) == []
    st["Larry's Inbox"]["fails"] = 2
    probs = mail_health.check(CFG, st, "2026-08-10 08:00", now=NOW)
    assert len(probs) == 1
    assert probs[0]["account"] == "Larry's Inbox"
    assert "bad password" in probs[0]["text"]


def test_silent_poll_loop_flags_after_grace(env):
    # Fresh start: no poll yet, app up 10 minutes -> quiet (grace period)
    assert mail_health.check(CFG, {}, "2026-08-10 11:50", now=NOW) == []
    # Up for hours with no completed poll -> loud
    probs = mail_health.check(CFG, {}, "2026-08-10 08:00", now=NOW)
    assert any("not completed a single poll" in p["text"] for p in probs)
    # Polls happened, but the last one is an hour old -> loud
    st = {"Larry's Inbox": {"when": "2026-08-10 11:00", "saved": 0,
                            "error": None, "fails": 0}}
    probs = mail_health.check(CFG, st, "2026-08-10 08:00", now=NOW)
    assert any("last ran 2026-08-10 11:00" in p["text"] for p in probs)


def test_quiet_mailbox_flags_only_after_a_completed_sync(env):
    """The false banner Joshua caught mid-initial-fill (2026-08-10): the sync
    walks oldest-first, so 'newest archived' is weeks old while 1,000+
    messages pour in. A box is judged quiet only once it has a COMPLETED
    sync on record — and never while a sync is actively running."""
    ok = {"Larry's Inbox": {"when": "2026-08-10 11:58", "saved": 0,
                            "error": None, "fails": 0}}
    # never archived from this box -> not a stall, just no initial pull yet
    assert mail_health.check(CFG, ok, "2026-08-10 08:00", now=NOW) == []
    _archive("2026-07-30")     # newest mail 11 days old...
    # ...but NO completed sync yet (mid-initial-fill) -> still quiet
    assert mail_health.check(CFG, ok, "2026-08-10 08:00", now=NOW) == []
    _mark_synced()             # the box has finished a full sync -> judge it
    probs = mail_health.check(CFG, ok, "2026-08-10 08:00", now=NOW)
    assert len(probs) == 1
    assert probs[0]["account"] == "Larry's Inbox"
    assert "no new mail seen since 2026-07-30" in probs[0]["text"]
    assert "Settings" in probs[0]["text"]
    # an actively running sync suspends the judgement entirely
    mail_archive._sync_running = True
    try:
        assert mail_health.check(CFG, ok, "2026-08-10 08:00", now=NOW) == []
    finally:
        mail_archive._sync_running = False


def test_full_mailbox_flags_and_explains(env):
    """The assistklr2 lesson (2026-08-10, 165% of 15 GB, 18 days of bounced
    bills): a full box's polls SUCCEED while senders get bounces — only the
    quota says why. Full → loud with the fix; nearly full → early warning;
    and a full box's quiet is explained by fullness, not double-reported."""
    st = {"Larry's Inbox": {"when": "2026-08-10 11:58", "saved": 0,
                            "error": None, "fails": 0, "quota_pct": 165}}
    probs = mail_health.check(CFG, st, "2026-08-10 08:00", now=NOW)
    assert len(probs) == 1
    assert "FULL (165%" in probs[0]["text"]
    assert "REJECTING" in probs[0]["text"]

    st["Larry's Inbox"]["quota_pct"] = 96
    probs = mail_health.check(CFG, st, "2026-08-10 08:00", now=NOW)
    assert len(probs) == 1 and "nearly full (96%" in probs[0]["text"]

    st["Larry's Inbox"]["quota_pct"] = 40
    assert mail_health.check(CFG, st, "2026-08-10 08:00", now=NOW) == []

    # quiet + full -> ONE problem (the full box explains its own quiet)
    _archive("2026-07-24")
    _mark_synced()
    st["Larry's Inbox"]["quota_pct"] = 165
    probs = mail_health.check(CFG, st, "2026-08-10 08:00", now=NOW)
    assert len(probs) == 1 and "FULL" in probs[0]["text"]
    # quiet without quota knowledge names both causes
    st["Larry's Inbox"]["quota_pct"] = None
    probs = mail_health.check(CFG, st, "2026-08-10 08:00", now=NOW)
    assert len(probs) == 1
    assert "password" in probs[0]["text"] and "full" in probs[0]["text"]


def test_poller_quota_parse():
    import email_poller

    class FakeM:
        def getquotaroot(self, box):
            return ("OK", [[b'"INBOX" ""'],
                           [b'"" (STORAGE 25952256 15728640)']])

    class NoQuotaM:
        def getquotaroot(self, box):
            raise Exception("QUOTA not supported")

    assert email_poller._imap_quota_pct(FakeM()) == 165
    assert email_poller._imap_quota_pct(NoQuotaM()) is None


def test_banner_renders_on_every_page(staff_client, monkeypatch):
    c, _h = staff_client
    monkeypatch.setattr(klr, "_mail_alert_problems", lambda: [
        {"account": "Larry's Inbox", "text": "every poll is failing — bad password"}])
    page = c.get("/bills").data.decode()
    assert "MAIL INTAKE NEEDS ATTENTION" in page
    assert "Larry&#39;s Inbox" in page

    monkeypatch.setattr(klr, "_mail_alert_problems", lambda: [])
    assert "MAIL INTAKE NEEDS ATTENTION" not in c.get("/bills").data.decode()


def test_poller_records_consecutive_fail_streak(env, monkeypatch):
    import email_poller
    calls = {"n": 0}

    def flaky(account, folder, is_p, mark_p):
        calls["n"] += 1
        return 0, ("boom" if calls["n"] < 3 else None)

    monkeypatch.setattr(email_poller, "_poll_imap_account", flaky)
    cfg = dict(CFG, email_folder="")
    for expect in (1, 2, 0):        # two failures, then a success resets
        email_poller.poll_email_once(cfg, str(env.tmp), lambda m: False,
                                     lambda m: None)
        assert email_poller.get_poll_status()["Larry's Inbox"]["fails"] == expect
