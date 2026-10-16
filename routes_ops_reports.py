"""Ops Reports — admin-only viewer for the scheduled data-side report
artifacts that live next to the app on the file-cabinet share
(\\\\LACKSSERVER2021\\File-Cabinet\\_OPS INDEX; server-local F:\\File-Cabinet\\_OPS INDEX).
Until now they were invisible unless someone browsed the share — and a failed
6 AM run was completely silent.

Same pattern as routes_karen.py: register() adds views with add_url_rule under
plain endpoint names; the app module comes in as _M for helpers/config.

Serving is allow-list only: a request name must be an exact key of REGISTRY,
and the path handed to the filesystem is built from the registry's OWN key —
never from the request string — so traversal is structurally impossible, not
merely filtered. Everything is wrapped in admin_required (these pages hold
real financial data).
"""
import os
import re
from datetime import datetime

from flask import Response, abort, render_template

_M = None   # the app module (composition root); set by register()

# The ops index sits beside the app folder in the file cabinet:
#   server  F:\File-Cabinet\IntakeSystem  ->  F:\File-Cabinet\_OPS INDEX
#   a desk  <drive>:\IntakeSystem         ->  <drive>:\_OPS INDEX
# config.json may override with "ops_index_dir" (a server-local F:\ path,
# per the config-paths convention). Tests re-point DEFAULT_OPS_DIR.
DEFAULT_OPS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_OPS INDEX")

# What people see when we name the folder (drive letters differ per desk).
OPS_DIR_UNC = r"\\LACKSSERVER2021\File-Cabinet\_OPS INDEX"

# One missed 6 AM run, with slack for a slow one. BIG_PICTURE, HARVEST and
# RECONCILIATION are all written by the same scheduled task
# (daily_bigpicture.bat), so any of them going stale means the task failed.
STALE_AFTER_HOURS = 26

# Curated registry — the ONLY files these routes will ever serve, in display
# order. daily=True marks artifacts rewritten by the 6 AM task (stale-flagged);
# one-shot review artifacts keep daily=False. New artifacts get a row here
# (plus a Helper mention in assistant.py) — never a hardcoded route.
REGISTRY = {
    "HARVEST.html": dict(
        title="Harvest — manual filing \u2192 intake",
        desc="The 6 AM pass over the file cabinet: documents staff filed by "
             "hand, linked to existing bills, registered as new records, or "
             "held for a human judgment call (those reappear every run until "
             "someone settles them).",
        daily=True),
    "RECONCILIATION.html": dict(
        title="Payment Reconciliation — app vs paper archive",
        desc="Paper payment records matched against bills: what Reconcile-Bot "
             "auto-marked Paid this run, near-matches needing a human confirm, "
             "duplicate-record flags (section A2), and section D's "
             "blank-amount \u201cinvisible\u201d bills.",
        daily=True),
    "BIG_PICTURE.html": dict(
        title="The Big Picture",
        desc="Daily overview: 13 years of operations mined from the archive "
             "(documents, dollars, payees) plus a snapshot of the live queue "
             "and unpaid bills at generation time.",
        daily=True),
    "bigpicture_daily.log": dict(
        title="6 AM task log",
        desc="Run-by-run log of the scheduled task that writes the three "
             "reports above — the first place to look when they go stale.",
        daily=False),
    "EMAIL_BACKFILL.html": dict(
        title="Email backfill — source emails retro-attached",
        desc="Reports #55/#8/#26: emails re-read from the office mailboxes "
             "(read-only — IMAP PEEK / POP3 TOP, mail is never marked read) "
             "and matched to email-less records and queue items by exact "
             "file hash. Dry runs and applies both rewrite this page.",
        daily=False),
    "EMAIL_APPEND.html": dict(
        title="Email append — emails onto the back of filed PDFs",
        desc="Reports #55/#106/#114: the source email(s) — with the honest "
             "attachment list — appended permanently to the END of each filed "
             "PDF, matched by exact file hash against the mail archive. Dry "
             "run first (python app.py append-emails), apply on the server.",
        daily=False),
    "HARVEST_BACKFILL.html": dict(
        title="Harvest backfill (one-shot)",
        desc="The July 26 one-time backfill that registered pre-baseline "
             "hand-filed documents into the intake log — kept for reference.",
        daily=False),
    "ROLLBACK_RENAMES.html": dict(
        title="Rollback review — pay-time renames",
        desc="Review artifact for undoing pay-time renames of staff-placed "
             "archive files (report #90): which files roll back and which "
             "need a hand look. Pairs with ROLLBACK_RENAMES.csv in the same "
             "folder.",
        daily=False),
    "REGISTER_VALIDATION.html": dict(
        title="Register validation (one-shot)",
        desc="Balance-continuity check of the consolidation registers "
             "(July 14): per-entity match rates, breaks and month gaps.",
        daily=False),
    "PAID_BACKFILL_2026.html": dict(
        title="Paid-bills backfill 2026 — dry run",
        desc="Phase-1 review for pulling the 2026 PAID BILLS trees into the "
             "app (Joshua, Aug 6): per packet, whether it matches an existing "
             "bill (and would mark it Paid), needs a person, or would create "
             "an already-Paid record. Nothing is applied until this is "
             "approved; pairs with PAID_BACKFILL_2026.csv.",
        daily=False, tool=("/paid-backfill", "Review visually, side by side")),
    "PAYMENT_RULES.html": dict(
        title="Payment rules — mined from the paid archive",
        desc="The business's operating rules read out of the paid-bills "
             "datasets: vendor payment rhythms (feeds Expected Bills), which "
             "entity pays which vendor, always-one-method vendors, check-book "
             "series and gaps, vendor-property ties, receipt-to-payment lag, "
             "and same-vendor spelling clusters (alias seeds). Re-run "
             "payment_rules.py after each backfill pass to refresh.",
        daily=False),
}

_HELD_RE = re.compile(r'Held for a human</div>\s*<div class="kv">(\d+)')


def register(app, appmod):
    global _M
    _M = appmod
    adm = appmod.admin_required
    r = app.add_url_rule
    r("/ops-reports", "ops_reports", adm(ops_reports))
    r("/ops-reports/view/<name>", "ops_report_view", adm(ops_report_view))
    r("/ops-reports/raw/<name>", "ops_report_raw", adm(ops_report_raw))


def _ops_dir():
    try:
        override = _M.load_config().get("ops_index_dir")
    except Exception:
        override = None
    return override or DEFAULT_OPS_DIR


def _ago(dt, now=None):
    """'23 minutes ago' / '5 hours ago' / '3 days ago' — plain language."""
    secs = ((now or datetime.now()) - dt).total_seconds()
    if secs < 90:
        return "just now"
    mins = secs / 60
    if mins < 90:
        return f"{round(mins)} minutes ago"
    hours = mins / 60
    if hours < 36:
        return f"{round(hours)} hours ago"
    return f"{round(hours / 24)} days ago"


def _held_count(path):
    """The 'Held for a human' KPI from HARVEST.html, or None if unreadable.
    A cheap regex over a ~4 KB file — not worth a parser."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            m = _HELD_RE.search(f.read())
        return int(m.group(1)) if m else None
    except OSError:
        return None


def ops_reports():
    now = datetime.now()
    d = _ops_dir()
    rows, stale = [], []
    for name, meta in REGISTRY.items():
        path = os.path.join(d, name)
        row = dict(name=name, title=meta["title"], desc=meta["desc"],
                   daily=meta["daily"], exists=os.path.isfile(path),
                   tool=meta.get("tool"), stamp=None, ago=None, stale=False)
        if row["exists"]:
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
            row["stamp"] = mtime.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")
            row["ago"] = _ago(mtime, now)
            row["stale"] = (meta["daily"] and
                            (now - mtime).total_seconds() > STALE_AFTER_HOURS * 3600)
        elif meta["daily"]:
            row["stale"] = True   # a daily that was never written is a failure too
        if row["stale"]:
            stale.append(row)
        rows.append(row)
    held = _held_count(os.path.join(d, "HARVEST.html"))
    return render_template("ops_reports.html", rows=rows, stale=stale,
                           held=held, ops_unc=OPS_DIR_UNC)


def _resolve(name):
    """Registry gate shared by view + raw: the name must be an exact REGISTRY
    key (so it can't contain separators or dots-up); unknown or missing -> 404."""
    if name not in REGISTRY:
        abort(404)
    path = os.path.join(_ops_dir(), name)
    if not os.path.isfile(path):
        abort(404)
    return name, path


def ops_report_view(name):
    known, path = _resolve(name)
    meta = REGISTRY[known]
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    is_html = known.lower().endswith((".html", ".htm"))
    text = None
    if not is_html:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    return render_template("ops_report_view.html", name=known, meta=meta,
                           is_html=is_html, text=text,
                           stamp=mtime.strftime("%b %d, %Y %I:%M %p").replace(" 0", " "),
                           ago=_ago(mtime))


def ops_report_raw(name):
    """The artifact bytes, exactly as the scheduled task wrote them. HTML is
    iframed by the view page (the artifacts are self-contained, inline-styled
    documents); .txt/.log serve as text/plain."""
    known, path = _resolve(name)
    is_html = known.lower().endswith((".html", ".htm"))
    with open(path, "rb") as f:
        data = f.read()
    return Response(data, mimetype="text/html" if is_html else "text/plain",
                    headers={"Cache-Control": "no-store"})
