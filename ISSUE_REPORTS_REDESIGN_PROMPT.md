# Prompt for a fresh session — Issue Reports page redesign (reports #66–#70)

Copy everything below the line into a new Claude session opened in `T:\IntakeSystem`.

---

## START HERE — plan first, then wait

Before touching anything: read this whole prompt, do the orientation checks
(PROJECT_STATUS, git log, concurrent-session check), then give Joshua a
**list of brief bullet points of what you are going to do** — one bullet per
deliverable, plain language, no code — and **wait for his go-ahead** before
starting work.

## The ask

Joshua filed five reports on 2026-08-04 (#66–#70), all about the **Issue
Reports page (/reports)**. Together they describe a redesign of the
issue-report lifecycle so his "validation phase" (checking whether reported
issues are actually fixed) is fast. Implement them as one coherent feature,
not five patches. His words, condensed:

- **#66:** default sort oldest → newest; show whether any development has
  been done on an issue; filter by user; "anything else to make the
  validation phase faster."
- **#67:** be able to add **notes** to an issue; and/or a button like
  **"checked and not yet resolved"** (a verification bounce-back).
- **#68:** an **AI description** per issue; **associate similar issues**
  (e.g. #66–#70 all belong together); visible **dev status** per issue ("if
  no dev work has been done on it, I don't check it") with filtering — and
  the **default view = oldest→newest issues dev believes are resolved**
  (the validation queue). Also: **any page that has an open issue about it
  should show an indicator** — clickable to read the issue, add notes, or
  mark it resolved.
- **#69:** a way to **bump an issue to higher priority**.
- **#70:** "I want to know **what Dev believes resolved looks like**" — when
  something is marked resolved, capture WHAT was done and what to check.

## Also in scope (Joshua, 2026-08-04): the workflow must be WRITTEN DOWN and the Night Shift must USE it

1. **Create `T:\IntakeSystem\VALIDATION_WORKFLOW.md`** — the canonical,
   human-readable definition of the lifecycle you build. One page: the states
   (Open → In progress → Dev-resolved/awaiting-verification → Verified, plus
   the "checked, not yet resolved" bounce-back), what each means, who may
   move what (dev sessions move things TO Dev-resolved and MUST attach a
   what-I-did/what-to-check note; only Joshua/staff Verify or bounce), and
   how priority, notes, grouping, and the validation-queue default view fit.
   This doc is the spec; the /reports page is its implementation. Keep them
   in sync forever after.

2. **Update the Night Shift scheduled task to follow that workflow.** The
   task is `night-shift-problem-reports` (cron 30 1 * * 2-6, enabled) — use
   the `mcp__scheduled-tasks__list_scheduled_tasks` /
   `update_scheduled_task` tools; read its current prompt first at
   `C:\Users\LacksUser\.claude\scheduled-tasks\night-shift-problem-reports\SKILL.md`.
   Preserve everything it already does (orientation, triage,
   fix-instance-AND-root-cause rule, hard constraints, morning report) and
   change its report handling to be workflow-native:
   - Work selection from the workflow states: bounced-back ("checked, not
     yet resolved") first, then priority-bumped, then Open — oldest first.
   - Every report it works on gets a **dev note**; finished fixes move to
     **Dev-resolved with the required resolution note** (that queue IS
     Joshua's morning validation list). It must NEVER set Verified.
   - **Graceful fallback:** the new columns/states only exist after the
     schema migration runs at the server's next boot. If the mechanism
     isn't live yet when a night run fires, the task behaves exactly as
     today (markdown report + queued server-console lines). Detect, don't
     assume.

3. **The night shift's write path — build it deliberately.** Night-shift
   sessions must NEVER write intake.db from a workstation (WAL over SMB),
   and they have no staff login (policy: no passwords in automation). The
   approved approach: writes go **through the running app** via a
   token-authed dev API, same pattern as `/api/connector/heartbeat`
   (token in config.json; see scanflow.py). Scope it tightly: endpoints for
   add-note and set-dev-status ONLY (never Verify, never Resolved-final,
   never record mutations), token compared with hmac.compare_digest,
   every call activity-logged with a distinct actor label (e.g. "Night
   Shift"), and a config token distinct from connector_token. This is a
   deliberate, contained trust-surface expansion — document it in
   VALIDATION_WORKFLOW.md and DESIGN.md §8.

## Suggested shape (use judgment, but cover every ask)

- Lifecycle beyond Open/Resolved: something like Open → In progress →
  **Dev-resolved (awaiting verification)** → Verified/Closed, plus the #67
  bounce-back ("checked, not yet resolved" → back to In progress with a
  note). Keep the existing `resolve-report` server CLI meaningful — it
  should map onto "Dev-resolved" (see Compatibility below).
- A `report_notes` mechanism (who/when/text) covering #67's notes, #70's
  resolution description (require a note when marking Dev-resolved), and
  the dev-status trail #66/#68 want. Timestamped notes by dev sessions ARE
  the "has dev touched this" signal.
- Priority flag (#69) — sort bumped issues first within the chosen view.
- Filters: by user, by status/dev-status, by priority; default view per
  #68 = validation queue (Dev-resolved, oldest first).
- Grouping (#68): a simple `topic`/`group` label (free text or picked from
  existing) is enough — don't over-engineer; AI-suggested grouping is a
  nice-to-have on top.
- AI description (#68): one-line summary per report. Follow the existing
  Anthropic client patterns (see classifier.py/assistant.py — always pass
  `timeout=`, degrade gracefully when the API is unavailable; never let AI
  failure break the page). Batch/lazy, not on every page load.
- Page indicator (#68): reports store `page_url` — on any page whose path
  has open issues, show a small indicator (likely in base.html) linking to
  those reports, with the add-note / mark-resolved actions. Match on URL
  path, ignore host/query. Keep it subtle; Karen's screens must not get
  noisier (her "too complicated" feedback is standing).

## Where things live (verify before editing — concurrent sessions are active)

- Routes: **`routes_reports.py`** (extracted from app.py 2026-07-29; the
  register(app, appmod) pattern — read its docstring and follow it; original
  endpoint names via add_url_rule, NOT Blueprints).
- Template: `templates/reports.html` (extends base.html, so CSRF token
  auto-injects into POST forms).
- Data: `bug_reports` table + helpers in `database.py` (`add_bug_report`,
  `get_bug_reports`, `set_bug_report_status`, `get_open_report_count`).
  Schema changes must be **additive** via init_db's existing
  add-column-if-upgrading pattern (see the paid_date block) — the DB is
  production, on LACKSSERVER2021.
- The floating "Report a problem" widget lives in base.html.
- The Helper's APP_GUIDE (assistant.py) is HAND-MAINTAINED — update it if
  the /reports workflow changes (standing rule).

## Compatibility / don't break

- Server console CLI: `python app.py list-reports` / `resolve-report <id>...`
  (also invoked from `server_next_visit.bat`) — keep both working; map
  resolve-report to the new Dev-resolved state, and note in its output that
  verification is still pending.
- ~30 pre-existing Open reports (#8 onward) must migrate sanely: everything
  Open stays Open; nothing auto-flips to a new state.
- Existing tests reference /reports lightly; run the FULL suite
  (`./python/python.exe -m pytest` — gate on pytest's own exit code, never
  `pytest | tail`). 587+ tests, all must stay green.

## Hard constraints (standing house rules)

- Check `T:\_OPS INDEX\PROJECT_STATUS.md` + `git log --oneline -5` FIRST —
  multiple sessions share this tree. Re-read files before editing; commit
  with explicit file lists (never `git add -A`); don't commit other
  sessions' in-progress files (watch for `queue_state.py`).
- NEVER write production intake.db from a workstation session (read-only
  queries are fine; schema migration code runs at the server's next boot).
- Full UNC paths in anything user-facing; never drive letters.
- Add regression tests for each behavior; verify each new test FAILS
  against the pre-change code before trusting it.
- Update `T:\_OPS INDEX\PROJECT_STATUS.md` when done.

## Definition of done

- Joshua saw the bullet-point plan and approved it before work started.
- All five asks covered; suite green; committed in logical chunks with
  explicit file lists (commit as you go — Joshua's standing preference now
  is commit+push per green chunk so CI verifies).
- `VALIDATION_WORKFLOW.md` exists and matches what was built; DESIGN.md §8
  documents the token-authed dev API.
- The `night-shift-problem-reports` scheduled task is updated to follow the
  workflow, with the pre-migration fallback intact.
- Reports #66–#70 get a dev note describing what was built (using the new
  mechanism itself) and are marked Dev-resolved for Joshua's verification
  pass after the next restart.
