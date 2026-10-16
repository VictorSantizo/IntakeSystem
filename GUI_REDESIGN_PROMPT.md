# Prompt for a fresh session — "One question, one task" landing screen

> **⚠ PARTIALLY SUPERSEDED (2026-07-30):** a concurrent session already built a
> version of this — commit `8a3b5fd` adds a **/home two-card landing** (bare "/"
> bounces Karen there; `?full=1` shows the full dashboard; question order
> karen-q → paid-twin → flagged-queue → important-log; task = catchup head).
> A fresh session should START by reviewing /home against the spec below and
> extend/refine it — do NOT build a second landing screen from scratch.

Copy everything below the line into a new Claude session opened in `T:\IntakeSystem`.

---

## The ask

Redesign the first screen Karen sees when she opens Klick Klack so it is radically
simpler than today's dashboard. The target model, in Joshua's words: **"here's 1
question I need answered and here's 1 thing to get done."** One glance, two cards,
zero navigation required:

1. **One question** — the single most important thing the system needs a human
   answer on right now. Sources to draw from, best-first: an open `/karen`
   questionnaire item (karen_questions.py — these now submit one-at-a-time and
   disappear when answered), a held-for-human ambiguity (twin-bill warning, held
   harvest item, date-sanity keep), or a review-queue item the AI flagged as
   needing eyes (`important` + `important_reason`).
2. **One thing to get done** — the single next action, drawn from what the app
   already knows: the next bill in Karen's pay-in-received-order stack
   (`/catchup` default order — KAREN PAYS BY RECEIVED DATE, this is locked), or
   the oldest review-queue item if the queue is what's blocking.

Everything else (full dashboard, queues, lists) stays reachable but must not
compete for attention on this screen. "Show me everything" should be one obvious
click away, not the default.

## Who this is for

Karen (60s, owner, not technical, low patience for busy screens). Her direct
feedback on the current UI: **"too complicated."** Real usage data has justified
simplifying before. She responds well to plain language, big targets, and
paper-office metaphors. Nancy and DeeAnna use the same landing but Karen is the
design target.

## Current state (verify before building — do not trust this doc blindly)

- The landing page is the priority-list + action-menu dashboard
  (`templates/dashboard.html`, the `/` route in `app.py`), with an
  important-correspondence surface and a from-email attention box.
- `/karen` (templates/karen.html) already renders one-question-at-a-time with
  per-question Submit — reuse its data, don't duplicate it.
- `/catchup` (received-order pay stack), `/bills`, the review queue, and the
  paper views (`/desk` etc.) all exist and stay unchanged.
- There is a locked style spec for the paper views — this redesign is for the
  MAIN app chrome, not the paper views; don't touch those.

## Hard constraints

- **Check for concurrent sessions before editing `app.py` or shared templates**:
  read `T:\_OPS INDEX\PROJECT_STATUS.md` and `git log --oneline -5` FIRST. A
  blueprint-split refactor of app.py may be in flight or recently landed
  (2026-07-29 night shift) — if app.py's routes have moved into `blueprints/`,
  edit there. If the split is mid-flight (uncommitted app.py churn), STOP and
  coordinate rather than editing app.py.
- Never commit without being asked; batch for review.
- Full UNC paths (`\\LACKSSERVER2021\File-Cabinet\...`) in anything
  user-facing/persistent — never drive letters.
- `T:\IntakeSystem` IS production. Tests must stay green: run
  `./python/python.exe -m pytest -q` (492+ tests) after changes. CI runs on push.
- Don't remove existing capabilities — this is a re-prioritization of the
  landing surface, not a feature deletion. The current dashboard content should
  survive somewhere (secondary page or below-the-fold).
- New POST forms in standalone (non-base.html) templates need a manual
  `csrf_token` hidden input — base.html's auto-inject only covers pages that
  extend it.

## Definition of done

- Opening the app shows the two-card screen with real data (one question, one
  task), each with a single obvious action button.
- Empty states handled: no open questions → celebrate/hide the card; nothing to
  do → say so plainly.
- A one-click path to the full dashboard/queues.
- Tests green; a new test file covering the landing route's selection logic
  (which question wins, which task wins, empty states).
- Update `T:\_OPS INDEX\PROJECT_STATUS.md` when done.
