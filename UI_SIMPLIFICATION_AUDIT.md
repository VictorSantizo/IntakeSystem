# UI Simplification Audit — 2026-07-07

Read-only audit of objective UI complexity, prompted by Karen saying the app feels too complicated.
No code was changed. Working tree was clean at audit time; no concurrent edits detected
(newest template write was 7/6 9:05 PM, matching last night's commits).

**Legend:** ✅ = safe cleanup, no sign-off needed · ❓ = guess, validate with Karen first

---

## The numbers (what "complicated" looks like objectively)

| Page | Controls visible at once (approx) | Verdict |
|---|---|---|
| Top nav (non-admin) | 7 page links + theme picker + report link + account + sign out ≈ 11 | Crowded |
| Dashboard | ~29 click targets: 3 period buttons, 2 banner CTAs, 4 shortcut cards, 7 KPI tiles, 2 clickable tables, activity table | **Overloaded** |
| Review Queue | ~17 chrome controls *before* the first document: 4–5 header buttons, 8 filter controls, 4 fast-lane buttons; then 2 controls + ~9 metadata chips per card | **Overloaded** |
| Review item (confirm) | 4 header buttons + **13 form fields** + 2 submit-row controls + dup banner + batch-date modal | Heavy — flagship page |
| Intake Log | 9 filter controls + **14 table columns**, 2 interactive controls per row | Column-heavy |
| Bills | 2 view toggles + table | Fine |
| Catch Up | 2 top buttons + 3 controls per row across 5 sections | Fine |
| Vendor bills | 2 header buttons + accounts table + bills table w/ status dropdown + Edit per row + pay-all modal | Moderate |
| Entry view | 5 header buttons + 6-field inline pay form + ~13 detail rows + always-visible receipt-upload form + email/copies sections | Moderate-heavy |
| Entry edit | ~15 fields + Save/Mark-Paid/Cancel/**Delete** in one row + pay modal | Heavy; Delete adjacency risky |
| Expected | Staff see a plain checklist (admin extras properly gated) | Good |
| Settings (admin) | 7 config fields + 4 sub-pages | Fine for admin |

---

## Prioritized simplification candidates
(highest impact ÷ lowest effort first)

### 1. Consolidate "mark as paid" — currently ~7 surfaces, one of which loses data
**What:** Paid can be recorded via: Catch Up per-row ✓ Paid, Catch Up bulk pay, entry view inline form, entry edit modal, vendor page "Mark All as Paid", **and** the quick status dropdowns on Intake Log and vendor-bills pages.
**Why complex:** Staff can't form one habit; and the status dropdowns ([log.html:29](templates/log.html:29), [vendor_bills.html:66](templates/vendor_bills.html:66) → [app.py:2022](app.py:2022)) set `Paid` with **no paid_date/amount/method/by** — silently inconsistent records depending on which path was used.
**Change:** Keep two paths: the entry-view inline pay form (single) and Catch Up bulk pay. Make the status dropdowns either drop the "Paid" option or open the pay form.
- ✅ The data hole (dropdown-Paid records nothing) is an objectively-bad inconsistency; fixing it needs no sign-off on the *problem*, only on the chosen UX.
- ❓ Which surfaces to remove — ask Karen which ones staff actually use.

### 2. Review/confirm form: progressive disclosure (13 fields → ~7 at first glance)
**What:** [review.html](templates/review.html) shows Date Received, Scanned Date, Mail Type, Doc Type, Vendor, Invoice #, Account #, Bill Type (+inline add), Amount, Due Date, Entity, Properties (multiselect + its own filter box), Notes — all at once, all equal weight.
**Why complex:** This is the page staff touch most. The real job is "verify what the AI extracted" — Vendor, Amount, Due Date, Entity, Property, Doc Type, Bill Type. The rest is metadata that's prefilled and almost never edited: Scanned Date (machine-known), Mail Type (derivable from source folder), Received Date (batch cover sheet already sets it), Invoice/Account #s, Notes.
**Change:** Show the 7 verify-me fields; fold the rest behind a "More details" disclosure (the page already has a show/hide mechanism — `updateFormVisibility()` — so the pattern exists). Consider collapsing the Properties box to chips + "change" when the AI already picked one.
- ❓ Validate the exact field split with Karen/staff — e.g. maybe they edit Invoice # often.

### 3. Merge the three bill views — Bills / Catch Up / Owed are near-duplicates
**What:**
- `/bills?view=due` and `/catchup` are the same list (unpaid bills by due date), flat vs. bucketed.
- `/bills?view=vendor` and `/owed/vendor` are the same data (vendors with unpaid totals), table vs. cards.
- `/owed/entity` and `/owed/property` are two more groupings of the same unpaid set, reachable only from dashboard cards.
**Why complex:** Five ways to look at "what do we owe," across 2 nav items + 4 dashboard cards. Staff have to learn which one to use; they behave subtly differently.
**Change:** One "Bills" page with grouping tabs (Due date / Vendor / Entity / Property), keeping Catch Up's bucketed layout as the due-date tab. Nav drops from Bills + Catch Up + Expected to one or two items.
- ❓ Validate which views Karen actually uses before deleting any — "Catch Up" is emotionally central to her workflow (it was built for her catch-up push); the *label* may matter more than the URL.

### 4. Nav trim (11 interactive elements → ~7)
- **"+ Manual Entry"** in the nav duplicates the "+ New Entry" button on Intake Log ([log.html:4](templates/log.html:4)) and the empty-queue button. Manual entry is the exception path; it doesn't need a permanent top-level slot. ❓ (quick nod — muscle memory)
- **"Expected"** is a periodic checklist, not a daily driver. Move it under Bills or leave only the dashboard banner ("N expected bills haven't arrived → See which"), which already links there. ❓
- **Theme picker** (4 themes) sits in the nav for everyone. Move it to the /account page. It's fun, but it's a permanent piece of chrome answering a question nobody asks daily. ❓ (staff may love it — cheap to ask)
- Daily drivers that stay: Dashboard, Review Queue (badge), Bills/Catch Up, Intake Log.
- Admin-only items (Settings, Reports) are already correctly gated. ✅ no change needed.

### 5. Dashboard de-clutter — three visible paths to the same page
**What:** When there are overdue bills, the dashboard shows the red banner CTA "Catch Up Now →", the "Catch Up" shortcut card, *and* the nav "Catch Up" — simultaneously. The 4 shortcut cards duplicate nav + banners; the 7 KPI tiles overlap the "By Document Type" table right below them.
**Change:** Drop the 4 shortcut cards (banners + nav cover them); consider trimming KPIs to 4 (Review Queue, Pending, Total Bills, Total Income).
- ❓ Ask Karen which tiles she actually glances at before cutting.
- ✅ Trivia: the dashboard runs two overlapping pending-count pollers (base.html every 10s + dashboard every 8s) — one can go, invisible to users.

### 6. Review Queue chrome: 8 filter controls + duplicate "Clear" labels
**What:** 7 filter dropdowns/inputs + Clear, above a fast-lane bar with 4 more buttons. Two different buttons on this page are labeled "Clear" (filter clear at [review_queue.html:65](templates/review_queue.html:65), selection clear at line 75), plus admin "Clear Queue" — three "clear"s on one screen.
**Change:** Collapse the filter bar behind a single Search box + "Filters" toggle (especially when the queue is small — filters matter at 400 items, not 8). Rename selection "Clear" → "Untick all". ✅ the rename; ❓ the collapse threshold.

### 7. Intake Log: 14 columns → ~9
**What:** #, Date Rcvd, Type (mail), Entity, Property, Doc Type, Bill Code, Vendor, Amount, Due, Status, By, Notes, Edit.
**Change:** Drop Mail Type, Bill Code, By, Notes from the table (all visible on the row-click detail view); keeps the scan-line readable.
- ❓ Validate which columns Karen scans — she built her filing life around codes, so Bill Code may be load-bearing for her specifically.

### 8. Six statuses everywhere
**What:** Pending / In Progress / Processed / Paid / Filed / On Hold appear in every filter, dropdown, and badge. Is "Processed" vs "Filed" a distinction staff can state? Is "In Progress" ever used?
**Change:** If usage data (status counts in the DB) shows 2 of the 6 are rare, fold them. Touches many surfaces, so medium effort.
- ❓ Definitely validate — and check actual status distribution in intake.db (read-only query from the server) before proposing.

### 9. Small safe cleanups ✅ (no sign-off needed)
- [review.html](templates/review.html) has both "☰ Queue" (top, line 23) and "← Back to Queue" (form bottom, line 141) — same action twice on one page. Keep one.
- [entry_form.html:80](templates/entry_form.html:80): **Delete** (permanent, red) renders in the same button row as Save/Cancel. Move it to a separated "danger" corner. Standard hygiene.
- [entry_view.html:247](templates/entry_view.html:247): the "Attach a receipt" file-input form is always expanded on every record. Collapse to a link that reveals it.

### Explicitly *not* recommended for removal
- **Duplicate-handling layers** (ingest byte-dedup → Triage page → per-item dup banner → post-confirm warning): these look redundant but each catches a different case at a different moment; the layering is why the queue went 425→74. Leave alone.
- **Filtered email area**: only appears when non-empty; it's the safety net for AI misfiling. Leave alone.
- **Admin gating** is already clean — Settings, Reports, Clear Queue, Expected-bill management, and config sub-pages are all behind `is_admin` / `@admin_required`. No admin complexity is leaking into staff view. The only "leak" is cosmetic: the theme picker (see #4).

---

## Suggested next step
Before implementing anything beyond the ✅ items: a 15-minute walkthrough with Karen on
(a) which of the five "what do we owe" views she actually opens,
(b) which review-form fields staff genuinely edit,
(c) which statuses mean something to her.
Her "too complicated" may also be about *concepts* (batches, confidence, entities-vs-properties), not just control counts — the walkthrough should ask for her top three annoyances open-endedly first.
