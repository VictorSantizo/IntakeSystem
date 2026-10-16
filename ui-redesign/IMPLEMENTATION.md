# Klick Klack — UI Redesign Implementation Map

**For:** the coding agent implementing the redesign in the IntakeSystem repo
**Visual source of truth:** the static mockups in `klick-klack-redesign/` (6 pages + `site.css`)
**Companion doc:** `IntakeSystem-Code-Review-2026-07-27.md` (house rules in §7 apply throughout)
**Date:** 2026-07-31

---

## 1. Goal & non-goals

**Goal:** make the app feel calm and obvious to non-technical staff. One dominant moment per page, one filled button per page, reference data collapsed, severity color only when action is required.

**Non-goals (do not touch):**
- No route, query, or business-logic changes. Every page renders the same data from the same view functions.
- No changes to the ingest pipeline, DB layer, classifier, or auth.
- The paper views (`desk.html`, `cabinet.html`, `rolodex.html`, `board.html`, `tray.html`, `calendar.html`, `intray.html`) are already the "cozy" side — leave them for a later pass.

## 2. Design language

Derived from the mockups (`klick-klack-redesign/site.css` is the reference implementation):

| Token | Value | Role |
|---|---|---|
| `--ink` | `#3c3835` | all primary text (charcoal brown) |
| `--ink-soft` | `#7a726b` | secondary text, metadata |
| `--paper` | `#faf6f3` | page background |
| `--cream` | `#f5ece5` | tertiary surface (stat panels, hover) |
| `--line` | `#e7dcd2` | hairline dividers (replace card shadows) |
| `--accent` | `#bc773f` | terracotta — the ONE action color |
| `--accent-deep` | `#9c4a2a` | needs-action severity only (overdue, urgent) |
| `--sage` | `#7a8b6f` | all-clear / "sure" confidence |
| Type | Georgia serif for headlines & display numbers; system sans for UI | warmth without a webfont |

**The page contract (every page):**
1. Eyebrow label + a **plain-language sentence** as `<h1>` ("Six documents to check. Three look sure.")
2. At most 3 work rows visible; the rest behind `<details>`
3. **One filled button** — the very next concrete action, with a small line of reassurance under it
4. Secondary actions as plain word links, never buttons
5. Severity color appears only on items that need action

## 3. `base.html` — the global pass (do this first)

**Strategy: remap the existing CSS variables, don't rename them.** All 40 templates consume `--navy`, `--blue`, `--pale`, etc. Rename values, keep names — the redesign lands everywhere at once:

```css
:root{
  --navy:#3c3835;      /* was #1F3864 — now ink (nav bg, headings) */
  --blue:#bc773f;      /* was #2E75B6 — now terracotta accent */
  --light-blue:#e7dcd2;/* hairlines */
  --pale:#f5ece5;      /* cream surfaces */
  --red:#9c4a2a;       /* severity, quieter */
  --orange:#bc773f;
  /* keep --green/--yellow/--gray/--dark/--mid; add: */
  --sage:#7a8b6f; --paper:#faf6f3; --cream:#f5ece5; --ink-soft:#7a726b;
}
body{ background:var(--paper); }
```

Then, in `base.html`:
- **Nav:** navy slab → quiet topbar (mockup `.topbar`): wordmark + word links + date/user. Keep the record-number "Go to #" box, the queue-count badge (restyle as plain text count), and the live poller script unchanged.
- **Cards:** `.card` loses shadow → `border-bottom:1px solid var(--line)` hairlines. `.card-header` navy bar → `.section-label` eyebrow style.
- **Buttons:** `.btn-primary` becomes the single `.next` style (terracotta). `.btn-ghost` → plain word links. One filled button per page is a *design rule*, not enforced by CSS.
- **Add the shared primitives** from `site.css`: `.eyebrow`, `.rows`/`.row`, `.next`, `.actions`, `.t` text tags, `table.quiet`, `.fields` dl rows.
- **Keep:** `.recno` chip (restyle to cream), CSRF injection script, theme picker machinery (themes still work — remap each theme's vars to stay tasteful; Hippie/Rock Star/Coastal keep their accent hue but adopt the same quiet structure), the `#kk=` show-me flash, status badges as text-only (drop pill fills except severity).

## 4. Page-by-page mapping

### 4.1 Dashboard — mockup `index.html` → `templates/dashboard.html`
- Replace the header block with eyebrow + sentence. Compose the sentence from existing counts (already passed to the template): "N things need you" + "<em>One is time-sensitive</em>" when `overdue_count > 0`.
- Today stack → **max 3 work rows**: overdue (if any), review queue, expected-missing (if any). Passes-to-you and important/email-eyes move to a "Worth a glance, no rush" hairline section with **no buttons**.
- The **one filled button** = oldest unpaid bill in received order ("Pay the next bill — {vendor} · ${amount}"). Needs one new query: oldest unpaid bill (`doc_type='Bill', status NOT IN ('Paid','Filed') ORDER BY date_received LIMIT 1`) — same data, one tiny addition in `database.py`.
- Action menu cards → plain `.actions` word links.
- "By Document Type" + "By Entity" tables → **one sentence + bar** ("July so far: 41 documents in · $9,218.44 billed · $12,050.00 received") with both tables inside a `<details>`. Period switcher stays, restyled as word links.
- Recent activity → quiet 5-row hairline list.
- **All-clear state stays** ("You're all caught up" — sage serif) and the "preview a quiet day" demo toggle is mockup-only, don't ship it.
- ⚠ **Keep the live-update contract exactly:** the review row keeps `id="review-tile"`, `id="review-count"`, `id="review-sub"`, is rendered hidden (not absent) at zero, and `window.styleReviewTile(n, urgent)` keeps its name/signature — `base.html`'s poller calls it. Urgent styling swaps class `danger` → whatever the new severity class is; update the JS accordingly.

### 4.2 Review queue — mockup `review.html` → `templates/review_queue.html`
- Sentence: "N documents to check. K look sure." — K = items meeting the existing auto-confirm-ish criteria (High confidence + known vendor). Group rows into **"sure" first, "need your judgment" second** with the existing confidence values; text tags (`sure` sage / `medium` / `unsure` deep) instead of colored conf classes.
- One filled button: **"Confirm the K sure ones"** → existing bulk-confirm path, scoped to the sure set. Reassurance line: "you can still undo / nothing is deleted".
- **Preserve the filter behavior contract:** `#filters-box` auto-opens when queue > 15 items or a filter is in the URL, stays collapsed otherwise (tests assert this — see §6). Filters move inside a `<details>` but keep the id and the open/closed logic.
- Keep Scan Now, Duplicate Triage, print-selected — as word links. Client-side filter mirror (`_filter_qs`/`_filtered_ids` in app.py) untouched.

### 4.3 Review item — mockup `review-item.html` → `templates/review.html`
- Two-column split: document left, **`<dl class="fields">`** right — each AI-read field a quiet row with its confidence tag (`AI · sure`).
- **Keep the highlight viewer exactly:** `.doc-viewer`, `[data-hl]` attributes, `.hl` boxes, page-image endpoints — restyle colors only (orange → terracotta).
- One filled button: **Confirm & file** (keep `id="confirm-log-btn"` — assistant show-me target; keep Ctrl+Enter).
- "File as" panel becomes the last `<dd>` ("File as" filename + destination, auto-updates on field correction — existing JS stays).
- ⚠ **Preserve the disclosure contract:** `#more-details` stays collapsed by default, force-opens on assumed received date via the existing `// assumed-date: force the details open` hook. Both branches are test-asserted.
- Discard/duplicate/edit links → word links; keep "recoverable" reassurance copy.

### 4.4 Pay stack — mockup `pay.html` → `templates/catchup.html`
- Sentence: "N unpaid bills, $X altogether." Rows in **received order (default stays Karen's rule)**; overdue rows get `.urgent` styling + "K days past due" meta — the only severity color on the page.
- One filled button: "Pay the next one — {oldest vendor} · ${amount}".
- Everything beyond ~7 rows behind `<details>` ("Show the other N — all current").
- Keep `?order=due` toggle as a word link, bulk-pay (keep `id="catchup-bulk-pay"`), and the record links.

### 4.5 Find — mockup `find.html` → `templates/log.html`
- Sentence + one large search input (pre-filled from `?search=`). Results as hairline rows with the `.recno` chip, not a dense table.
- Filters (status/entity/property/dates) inside `<details>`.
- **Keep:** universal search behavior (`search_documents`), record-number jump box (nav), column sort on the table fallback (`test_sortable_and_showme` asserts sortable headers — keep `?sort=` links), pagination.
- ⚠ Do not re-add columns the 2026-07-07 pass deliberately dropped: no Type (mail), By, or Notes columns; `<th>Bill Code</th>` stays; header/row colspan must match (test-asserted).

### 4.6 Record view — mockup `record.html` → `templates/entry_view.html`
- Sentence header: "{vendor}, {property} — ${amount} {status}." + `recno` chip in the eyebrow.
- One filled button: **Mark paid** (keep `id="mark-paid"`), payment form inside `<details>`. After payment the existing `_refile_to_convention` renames the file (CK#) — surface that as the reassurance line: "the filename gains the CK# itself".
- Fields → `<dl class="fields">`; document path + view link stays; copies-on-file as a quiet line.
- Add the **vendor history strip** (last ~3 payments from `get_bills_for_vendor`, already available) — this is new presentation of existing data.
- Activity log → quiet list. Edit/Pass/Flag/Attach-receipt → word links. Keep `id="ai-fill"` on the AI-fill action.

## 5. Assistant show-me targets — do not rename these ids

`assistant.py`'s APP_GUIDE promises clickable `#kk=` flash targets. Every id below must survive the redesign on its page (restyle freely):
`nav-dashboard, nav-review, nav-log, nav-bills, nav-catchup, nav-karen, nav-expected, nav-settings, scan-now, triage-btn, triage-clear-exact, triage-attach-selected, confirm-log-btn, mark-paid, ai-fill, catchup-bulk-pay`.

## 6. Test impact (update tests *with* the markup, same PR)

These tests assert old DOM strings and **will fail** until updated to the new markup — the behaviors they protect must be preserved:

| Test | Protects | Update to |
|---|---|---|
| `test_dashboard_landing.py` | all-clear state; review-tile hidden-at-zero + urgent styling; action menu labels; search form not nested in `<a>`; no KPI rows | new class names/sentence strings; keep all behaviors |
| `test_ui_structural.py` | nav trim (no Manual Entry, theme picker only on /account); `#more-details` collapsed/force-open; `#filters-box` open logic; log column drop | same ids, new surrounding markup |
| `test_ui_simplification.py` | related simplification assertions | review each assertion against new DOM |
| `test_render_sweep.py` | every template renders without error | should pass unchanged |
| `test_sortable_and_showme.py` | sortable columns + `#kk=` targets exist | keep targets, should pass |

Strategy: change markup and assertions together, run `pytest` per phase. Behavior contracts (hidden-at-zero, force-open, filter auto-open, no-nested-form, column drop) are **kept** — only the asserted strings change.

## 7. Rollout phases

1. **Phase 1 — base:** CSS variable remap + quiet topbar + shared primitives. All pages get calmer without markup changes. Run full suite.
2. **Phase 2 — dashboard:** §4.1 + dashboard test updates. Ship behind a Karen walkthrough.
3. **Phase 3 — review flow:** §4.2 + §4.3 + queue/review test updates.
4. **Phase 4 — money flow:** §4.4 + §4.5 + §4.6 + log test updates.
5. **Phase 5 (optional, later):** paper views pass for consistency.

## 8. House rules to preserve (from the code review)

- Nothing is ever deleted — discards go to recoverable set-asides; say so in the UI copy where the mockups do.
- Bills pay in **received-date order** by default; due-date order is a toggle, not the default.
- The AI's reading is a draft — confidence shows on every field; a person confirms everything unsure.
- The review row live-updates without refresh (poller + `styleReviewTile`).
- Undo affordances stay visible ("you can still undo", "recoverable").

---

## 9. Borrowed interactions (from `mail_review.html`, evaluated 2026-08-03)

A separate single-file prototype (`mail_review.html`) was evaluated against this redesign. Verdict: **interaction ideas adopted, layout/palette rejected.** Adopted:

1. **"Filed as: {filename}" confirm feedback** — after Confirm & file, show a sage confirmation strip with the exact convention filename + destination folder + an **undo** link, then advance to the next item (demoed in `review-item.html`). Builds trust in auto-filing.
2. **Progress counters** — "N to check · K filed today" near the page sentence (demoed in `review.html`). Turns the queue into a finishable task; the "filed today" count comes from `activity_log` (Created actions today by anyone).
3. **Set-aside with visible undo** — a collapsed "Set aside (N) — nothing is ever deleted" section on the queue page with "put back" links (demoed in `review.html`). Maps to the existing `_Skipped`/`_Filtered` folders.
4. **Per-field plain-language uncertainty** — when the AI is unsure of a specific field, that field gets the deep-terracotta border plus a one-line human sentence above the form ("We couldn't read the due date on this one") instead of only a `Low` confidence tag. The sentence comes from the classifier's `notes`; keep it to one line.

Rejected, with reasons:
- **Card-per-item with all fields inline-editable** — recreates the wall-of-inputs intimidation; keep hairline rows → one item at a time.
- **Source/type badge pills (indigo/green)** — against the one-accent rule; source stays a text tag.
- **Its filename format** (`YYYY-MM Property – Vendor – $amt`) — not Karen's convention; `_make_filename()` output is the only filename shown.
- **Paid checkbox on filed items** — marking paid without method/check # breaks the CK# refile (`_refile_to_convention`); quick-pay must still collect payment details.
