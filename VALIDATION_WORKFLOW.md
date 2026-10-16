# Issue-Report Validation Workflow

*The canonical definition of the issue-report lifecycle. The `/reports` page is
this document's implementation — **if one changes, the other must change in the
same commit** (standing rule, like the Helper's APP_GUIDE). Born 2026-08-04
from Joshua's reports #66–#70.*

## Why

Joshua's mornings have a **validation phase**: checking whether issues dev
believes it fixed are actually fixed. That phase is fast only when the system
answers three questions at a glance: *what does dev think is done?* (check
those), *what has dev not touched?* (skip those), and *what exactly did dev do
and how do I check it?* (report #70).

## The states

| State | Meaning |
|---|---|
| **Open** | Filed; no dev work recorded yet. |
| **In progress** | Dev has started (or the issue bounced back from a failed check). |
| **Dev-resolved** | Dev believes it is fixed and has written a **resolution note** (required): what was done + what to check. Awaiting human verification. |
| **Verified** | A person checked and it really works. Closed. |
| **Resolved** *(legacy)* | Closed before this workflow existed (reports ≤ Aug-2026). Read as closed history; never assigned anymore. |

**The bounce-back** (report #67): from Dev-resolved, the button **"Checked,
not yet resolved"** returns the issue to *In progress*, records the checker's
note (kind `bounce`), and stamps `bounced_at` — bounced issues are the night
shift's first pick next run.

## Who may move what

- **Dev** (night shift, dev sessions, the server CLI) moves issues to
  *In progress* and *Dev-resolved* — and marking Dev-resolved **requires** the
  what-I-did/what-to-check note. Dev can **never** set Verified.
- **Joshua/staff** (in the app) Verify, bounce, reopen, add notes, bump
  priority, set topics. Verification is human-only, always.
- Nothing ever auto-flips: migration kept every pre-existing report exactly
  where it was.

## Notes

Every report carries a note trail (`report_notes`): who, when, text, kind —
`note` (human comment), `dev` (dev work happened — the "has dev touched this"
signal), `resolution` (the required Dev-resolved note, shown in a green
**What dev did & what to check** box), `bounce` (a failed check), `status`
(state-change breadcrumbs). "No dev work yet" on a card literally means no
`dev`/`resolution` note exists — skip it when validating (report #66/#68).

## Priority, topics, AI summaries

- **Priority bump** (#69): one click; bumped issues sort first in every view.
- **Topic** (#68): a free-text group label (e.g. "Issue Reports page") so
  related issues travel together; filterable. AI suggests one when blank.
- **AI summary** (#68): a one-line description per report, written in batch by
  the ✨ button (or in the background for fresh reports) — never on page load,
  and the page renders identically when the AI is unavailable.

## Families: split and group (2026-08-08)

One report can hold several asks, and several reports can hold one. Both are
the same relationship — a **parent** and its **sub-issues** (`parent_id`) —
and both were asked for in a bounce:

> **#68** (Joshua): *"reports like this should be separated into their own
> issues. 68a, 68b, etc."* — **#55** (Joshua): *"There is 3 or so issues open
> for this. we should merge them."*

- **Split into sub-issues** — one ask per line; each line becomes its own
  report, lettered **#68a, #68b, …** (Joshua's own notation), inheriting the
  parent's reporter, page, record reference, screenshot, topic and priority,
  and landing **Open**. The parent's own status never changes — its status is
  a fact about it, not about its children — and it gains a note listing what
  came out of it. **✨ Suggest** proposes the lines from the report's text;
  nothing is created until a person presses Split, and an unavailable AI just
  means an empty suggestion.
- **Group with another issue** — point a report at the one that keeps the
  thread. **Nothing is closed, merged away or rewritten**: they stay separate
  records with separate statuses, because verification is per-issue and
  human-only. Empty the box to take a report back out on its own.
- **One level only.** A parent cannot itself have a parent, and a report with
  sub-issues cannot become someone's child. A tree of trees would put back the
  reading these features exist to remove.
- **A family sorts as one thread**: it takes the *parent's* place in every
  view, children following in order, indented, each chipped *"↳ part of #N"*.
  A sub-issue keeps its label and its parent link even when a filter hides the
  rest of the family.
- Sub-issues are ordinary reports everywhere else: the night shift picks them
  up by the same work order, they Dev-resolve and Verify one at a time, and
  they count once each in the badge.

## The views (`/reports`)

- **To verify** — *the default*: Dev-resolved issues, **oldest first**,
  priority-bumped on top. This IS the morning validation queue (#68).
- **Open** — Open + In progress, oldest first.
- **All** — everything, including closed.
- Filters on each: **search** (#73), reporter, topic, priority-only. Old
  `?show=` links still work. Search (`?q=`) matches every word against the
  report's own text, its topic and AI summary, the reporter, the page it was
  filed from, its record reference, its status, **and its note trail** — so
  "all the issues about email" finds the ones whose notes, not their titles,
  say so. It is how a set of related issues gets found and given one topic.
- **Page indicator** (#68): any page with unverified issues filed about it
  shows a small admin-only chip ("N open issues about this page") linking to
  exactly those reports, with the full note/verify/bounce actions.

## How dev writes state (the trust surface)

Night-shift and dev sessions must never write intake.db from a workstation
(WAL over SMB) and have no staff login (no passwords in automation). They
write **through the running app** via the token-authed dev API
(`routes_reports.py`, documented in DESIGN.md §8):

- `POST /api/dev/report_new` `{token, description, page_url?, actor?}` →
  files a new report, always **Open** (the same verb as the staff widget —
  Joshua 2026-08-04: issues asked for in chat get tracked like any other).
- `POST /api/dev/report_note` `{token, report_id, text, actor?}` → adds a
  `dev` note.
- `POST /api/dev/report_status` `{token, report_id, status, note?, actor?}` →
  `In progress` or `Dev-resolved` **only** (`note` required for Dev-resolved).
- Auth: `night_shift_token` in config.json (distinct from `connector_token`),
  compared with `hmac.compare_digest`; unset token = API off. Redacted from
  config backups like every other secret.
- Every call is activity-logged under its actor label (default **"Night
  Shift"**). The API cannot Verify, cannot close, cannot touch records.
- **Always call it through `night_api.py`** (`./python/python.exe night_api.py
  probe | note | status | new`, from `T:\IntakeSystem`) — the one stable
  command shape, on the project permission allowlist
  (`.claude/settings.json`). Ad-hoc token-posting scripts trip permission
  prompts that strand unattended runs (2026-08-05 1:30 AM lesson: the first
  night run stalled on exactly that and shipped nothing).

The server console equivalent (humans at the keyboard, F:\ on
LACKSSERVER2021): `python app.py resolve-report <id...> [--note "what was
done + what to check"]` → sets **Dev-resolved** (verification still pending);
`list-reports` shows the new states (`!` = bumped, `B` = bounced).

## Night-shift work order

1. **Bounced-back** issues (`bounced_at` set) — a human already said "still
   broken"; oldest first.
2. **Priority-bumped** issues, oldest first.
3. **Open** issues, oldest first.

Every report worked gets a dev note; every finished fix goes to Dev-resolved
with its resolution note — that queue is Joshua's morning list. If the schema
migration hasn't run yet (pre-restart) or the API answers 404/403, the night
shift falls back to its old behavior: markdown report + queued server-console
lines. Detect, don't assume.
