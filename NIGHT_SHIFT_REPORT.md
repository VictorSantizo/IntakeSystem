# Night shift — 2026-08-11

Two commits, both pushed to `origin/main`. Full suite green (1,017 tests,
pytest exit 0) before each commit.

| | |
|---|---|
| Code running on the server when this run started | `12e8b2d` (8/10 18:13) |
| Now on main | `9e3944bb` |
| **Restart needed** | **Yes** — Settings → Restart |

---

## 1. Report #22 — "Copy Path still not working" (Joshua's bounce, 8/10 17:19)

**Commit `5009b893`. → Dev-resolved.**

Joshua was right twice over, and the reason he never saw the button change is
that **the button was never wired to anything.**

The file path was passed to the click handler as an inline `onclick` argument
built by Jinja's `|tojson` filter. `|tojson` escapes `<`, `>`, `&` and the
single quote — but **not** the double quote. So the browser received:

```
onclick="copyDocPath(this, "\\LACKSSERVER2021\File-Cabinet\...pdf")"
```

The attribute **ended at the path's own opening quote**. The handler was the
fragment `copyDocPath(this, ` — a syntax error — and the rest of the path
became a garbage attribute name. Verified by feeding the old markup through a
real HTML parser: the path does not reach the button at all.

That killed the Copy path button on the record view, the compare view and the
paid-backfill view — and it had also silently killed the **"Open in Explorer"
button that #22 was originally closed with on 8/5**, which is why that never
worked either.

Fixed by removing the whole class of bug: paths no longer ride event
attributes anywhere in the app. A button carries `data-copy-path` (normal
autoescaping, which *does* escape quotes) and one delegated listener in the
page chrome handles all of them.

**Second, separate fault in the same report:** on the new `/files` page the
handler *did* run and copied the wrong thing — `f.path` raw, i.e. the
server's own `F:\File-Cabinet\...`, the one drive letter no other desk has. It
landed on the clipboard and File Explorer rejected it. That button was the
only staff-facing path in the app that had never been through the `|unc`
filter. (The APP_GUIDE had been promising full UNC there since the page
shipped.)

**And the label Joshua asked for:** one shared `copyPathButton` — every Copy
path button in the app now reads **"Path Copied"**, restores its original
label afterwards (kept in a data attribute, so a double-click can't strand
it), and hands the path over in a box if the clipboard refuses, instead of
doing nothing visible.

**What to check after the restart:**
1. `/entry/273/view` — Copy path should say "Path Copied" and paste into the
   File Explorer address bar.
2. The **Open in Explorer** button on that same row (if this desk's connector
   is running) — expect it to work for the first time.
3. `/files` — open any folder, Copy path on a **file** row, confirm you get
   the `\\LACKSSERVER2021\...` path and not `F:\`.

Tests pin the class, not just the instance: no `|tojson` inside any `on*`
handler anywhere in templates, and the record page's button is read back
through a real HTML parser using a path containing `'` and `&`.

## 2. Report #55 — the email at the back of the bill (Karen; Joshua's bounce)

**→ Dev-resolved. No new code — this shipped 8/10 and is already LIVE.**

The report was never moved when the work landed. All three things Joshua asked
for in his bounce now exist:

1. **A sample record**, verified tonight in the actual file rather than
   inferred: **record #809 / `260721-003`**, Callender's House of Glass,
   $9,750.00, T131. That filed PDF is 2 pages — page 1 the invoice, page 2
   headed *"SOURCE EMAIL (stored copy, printed with this document)"* with
   From / To / Date / Subject / Mailbox and the attachment list.
2. **Merging the related issues** — shipped 8/8: "Group with another issue" on
   each `/reports` card. Put #55's number on #57, #8 and #26.
3. **The retro for older records** — built, and it is the one piece still
   owed. It needs the server console (see below). 185 records hold a stored
   source email; exactly **one** (#809) has it appended so far, because the
   append only fires at filing time for documents filed since the code went
   live. The retro is what covers the rest (~188 records).

## 3. Report #12 — Britt / TRUST3 / KLR-LRC entities (dev note only)

Still blocked on the same single question, and it is now the **oldest
priority-1 item open**. Nothing was guessed. It reduces to one yes/no for
Karen:

> At **5210 Carpinteria Avenue**, should ALL bills belong to one new
> **KLR-LRC** entity (her words on report #110) — or does the configuration
> live today stand: **PP37 takes the bills EXCEPT utilities, and utilities
> split 50/50 between KLR and LRC**?

Three property codes sit at that address (PP37, KLR-OF, LRC-OF), so the two
readings pay different people. That is a money question, so it stays with
Karen and Joshua.

Her other two rulings are **not** in dispute and can be built the moment this
is settled: "Personal-KR" becomes plain "Personal" (#109), and BRITT is an
entity for her personal bills at 123 La Vereda — utilities, medical,
insurance, legal — with her rentals staying under TRUST3.

## 4. The nightly Classy page — `/files`

**Commit `9e3944bb`.** Third page into the Classy set, after the dashboard and
Mail. `templates/new/files.html` + the view routed through `render_skinned`;
the classic template is untouched, so Colorful users see zero difference
(pinned by a test).

Two things the calm palette doesn't own, handled rather than imported:
`newlook.css` has no `.badge-status`, so a record's status rides the quiet
`.t` tag idiom the new look already uses; and `.panel` carries a 44px top
margin that would have spaced the folder tiles apart down the grid, so those
are cream cards instead.

The functional contract is pinned: both Copy path buttons keep
`#copy-path-btn` / `.js-copy-file` and their `data-copy-path`, `#unc-footer`
keeps feeding the Open-in-Explorer call, and the **roots** page (a separate
branch of the template) is fetched too — `render_skinned` raises on a broken
new template rather than falling back, so a half-built page would 500.

**Chose `/files` over the review queue deliberately:** `review.html` is 30KB
and the rule is all-or-nothing, so it deserves a night of its own rather than
a rushed half. **The review queue and the intake log are still the two big
ones** and should each get a dedicated night.

---

## Server-console lines still queued (a human must run these)

```
cd /d F:\File-Cabinet\IntakeSystem
```

**The #55 retro** — Karen approved this ("yes with the retros"):
```
python app.py append-emails
```
review **"Email append"** under Ops Reports, then:
```
python app.py append-emails apply
```

**Report #112 — the four remaining date edits** (unchanged from 8/10):
`#1194 → 2026-07-01`, `#279 → 2026-07-03`, `#314 → 2025-03-03`,
`#587 → 2026-06-13`. Record numbers stay, by design.

## Restart

**Needed.** It activates: the whole #22 copy-path fix (all four pages plus the
Open-in-Explorer button), the Classy `/files` page, and the two commits from
8/10 evening that never got a restart (`4eaa1a09` the skin-switch OFF track,
`8baf85ee` the retro pass carrying re-send flags).

## Open questions

- **#12 / 5210 Carpinteria** — the yes/no above. Karen and Joshua only.
- **gmail INBOX has been quiet since Jul 24** (noted 8/10) — still unconfirmed
  whether that box is retired or broken upstream.

## Nothing left half-built

No unfinished work in the tree. Untracked files present at start and left
alone (they belong to other sessions): `ISSUE_REPORTS_REDESIGN_PROMPT.md`,
`MORNING_NOTES_2026-08-04.md`, `Mail Attachments/`.

## Housekeeping

- No new permission shapes were needed; `.claude/settings.json` unchanged.
- Two durable lessons added to the night-shift SKILL.md: multi-line commit
  messages go through a scratchpad file + `git commit -F` (`git commit -m
  @'...'@` is PowerShell syntax and shatters into pathspecs in the Bash tool),
  and a plain `sqlite3` scan of a big table can blow the 120s Bash timeout
  over the share — `night_db.read()` is the bounded way in.
- No new reports filed: both faults found tonight were already covered by #22.
