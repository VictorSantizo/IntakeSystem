r"""mail_health.py — is mail still flowing? A LOUD answer when it isn't.

Joshua, 2026-08-10: "if mail all of a sudden stopped flowing, how would we
know?" Until now: only by opening Settings (the per-account poll status lives
there) or by reading the server console. A dead mailbox password could sit
unnoticed for days while bills pile up unseen — the exact silent failure this
office can't afford.

check() turns three signals into a list of problems, and the shared chrome
(_chrome_shared.html) renders any problem as a red banner at the top of EVERY
page, both looks:

1. A mailbox whose polls keep failing (bad password after a credential
   update, unreachable host). Debounced on the poller's consecutive-failure
   counter — one blip of network weather is not a banner, a streak is.
2. The poll loop itself gone silent (thread died, app wedged): no completed
   poll for far longer than its 5-minute cadence.
3. A mailbox that has gone QUIET — polls succeed but the newest message the
   mail archive has seen from it is days old. This is the "credentials were
   changed and the old ones still 'work' but see nothing" / "mail is landing
   in a different box now" case that per-poll errors can never catch.

What to DO about a banner is in the banner: check the mailbox password under
Settings → Intake. All reads here are cheap (in-memory poll status + one
indexed query on the archive sidecar); app.py caches the result briefly per
process so page renders never pay for it twice in a row.
"""
from datetime import datetime, timedelta

import mail_archive
from email_poller import configured_accounts

QUIET_DAYS = 4          # newest archived mail from a box older than this -> notice
POLL_OVERDUE_MIN = 30   # the 5-minute poll loop silent this long -> notice
MIN_FAILS = 2           # consecutive poll failures before the banner (debounce)
QUOTA_FULL_PCT = 100    # at/over the storage limit: the server REJECTS new mail
QUOTA_WARN_PCT = 95     # close enough that bouncing is days away


def _parse_minute(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def check(cfg, poll_status=None, app_started="", now=None) -> list:
    """Problems as [{account, text}] — empty means mail is flowing. Never
    raises: a health check that can take a page down is worse than no check."""
    try:
        return _check(cfg, poll_status or {}, app_started, now or datetime.now())
    except Exception as e:      # pragma: no cover - belt and suspenders
        print(f"[MailHealth] check failed: {type(e).__name__}: {e}")
        return []


def _check(cfg, poll_status, app_started, now):
    accounts = configured_accounts(cfg)
    if not accounts:
        return []                # email intake isn't set up; nothing to watch
    problems = []

    # 1 — repeated poll failures, per mailbox
    full_boxes = set()
    for label, st in sorted(poll_status.items()):
        if st.get("error") and st.get("fails", 0) >= MIN_FAILS:
            problems.append({"account": label,
                             "text": f"every poll is failing — {st['error']}"})
        # 1b — a full (or nearly full) mailbox. The one failure mode where
        # polls succeed while SENDERS get bounces: assistklr2@gmail.com sat at
        # 165% of its 15 GB and silently rejected every bill for 18 days
        # (found 2026-08-10). The poller reads the quota on each IMAP poll.
        pct = st.get("quota_pct")
        if pct is not None and pct >= QUOTA_FULL_PCT:
            full_boxes.add(label)
            problems.append({"account": label,
                             "text": f"this mailbox is FULL ({pct}% of its "
                                     f"storage limit) — the mail server is "
                                     f"REJECTING new mail, so senders get "
                                     f"bounce-backs and bills never arrive. "
                                     f"Empty out old mail (and the trash) or "
                                     f"upgrade its storage plan"})
        elif pct is not None and pct >= QUOTA_WARN_PCT:
            problems.append({"account": label,
                             "text": f"this mailbox is nearly full ({pct}% of "
                                     f"its storage limit) — mail will start "
                                     f"bouncing within days; clean it out or "
                                     f"upgrade its storage now"})

    # 2 — the poll loop itself has gone silent (only judged once the app has
    # been up long enough that "no poll yet" can't just be a fresh start)
    started = _parse_minute(app_started)
    if started and (now - started) > timedelta(minutes=POLL_OVERDUE_MIN):
        polls = [d for d in (_parse_minute(st.get("when"))
                             for st in poll_status.values()) if d]
        newest = max(polls, default=None)
        if newest is None:
            problems.append({"account": "all mailboxes",
                             "text": "the background mail poller has not "
                                     "completed a single poll since the app "
                                     "started"})
        elif (now - newest) > timedelta(minutes=POLL_OVERDUE_MIN):
            problems.append({"account": "all mailboxes",
                             "text": f"the background mail poller last ran "
                                     f"{newest:%Y-%m-%d %H:%M} — it should "
                                     f"run every 5 minutes"})

    # 3 — a mailbox gone quiet: the archive has history for it, but nothing
    # new for days. Judged ONLY for a mailbox with at least one COMPLETED
    # sync: mid-initial-fill the archive fills oldest-first, so its "newest"
    # message is weeks old while mail flows fine — Joshua hit exactly this
    # false banner during the first fill (2026-08-10, "47 days" at 1,000+
    # messages archived). A box mid-sync, or never fully synced, is unjudged.
    if mail_archive.sync_running():
        return problems
    completed = {(s.get("account_name") or "").strip()
                 for s in mail_archive.status().get("sync", [])}
    newest_by = mail_archive.newest_by_account()
    for a in accounts:
        name = a.get("name") or a.get("username")
        if name not in completed or name in full_boxes:
            continue          # a full box already explains its own quiet
        latest = (newest_by.get(name) or "").strip()
        if not latest:
            continue
        try:
            age = (now.date() - datetime.strptime(latest, "%Y-%m-%d").date()).days
        except ValueError:
            continue
        if age > QUIET_DAYS:
            problems.append({"account": name,
                             "text": f"no new mail seen since {latest} "
                                     f"({age} days) — the password may have "
                                     f"changed (update it in Settings), or "
                                     f"the mailbox may be full and bouncing "
                                     f"mail back to senders"})
    return problems
