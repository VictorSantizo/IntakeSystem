# Morning notes — overnight run under Joshua's blanket go-ahead

*(Written by the long-running punch-list session. Everything below is pushed
and CI-verified green unless marked otherwise.)*

## Done overnight (after you went to sleep)

| Commit | What | Notes |
|---|---|---|
| `a722ac4` | Design q2: bulk-confirm skip-not-block date guard | mixed-batch test revert-verified |
| `8591f75` | Design q5: manual-entry non-blocking similarity banner | banner + activity log; save never blocked |
| `8a74cf9` + `640f4d3` | Design q8: both renames ("Check for new items", "Needs Review") | templates + APP_GUIDE + docstrings; ids/endpoints untouched. `640f4d3` corrects my `git add -A` accidentally sweeping in `queue_state.py` (another session's WIP) — untracked again, file untouched on disk |
| `6fac42f` | Design q4 FIRST HALF: staff password minimum 4 → 8 | new passwords/resets only; boundary test; CI green on fresh runner |
| `3db607c` | CI: checkout v5 / setup-python v6 | clears the Node-20 deprecation annotation |

## Two process slips, disclosed

1. **`git add -A` swept a contested file** (`queue_state.py`) into `8a74cf9`.
   Corrected in `640f4d3`; back to explicit file lists.
2. **One push went out before the suite was confirmed green**: I gated on
   `pytest | tail`'s exit code, which is tail's, not pytest's. The failure
   turned out to be a stale-bytecode artifact of my own revert-verify dance
   (same-size edit + preserved mtime → Python reused the old .pyc), so the
   pushed code was actually correct and CI independently confirmed green —
   but the gate was broken and is fixed (pytest runs unpiped now).

## Deferred to avoid collisions (needs whoever owns it, or me in the morning)

- **Design q4 SECOND HALF** (re-validate a removed staff member's live session
  per request, in `require_login`): app.py was being actively restructured by
  the split-6 session all night — not touched. Small change once app.py is
  quiet.
- **⚠ CI trap for the split-6 session:** the `ui-redesign-sample` branch's CI
  FAILS with `ModuleNotFoundError: queue_state` — its app.py imports the
  module, but `queue_state.py` exists only on disk, not in git. **Main will
  break the same way if app.py's `import queue_state` is committed without
  committing queue_state.py in the same commit.**

## Still awaiting Joshua's answers (design questions)

1. Split 6+ state ownership (though queue_state.py suggests a session is
   already executing option (b) — confirm that was your instruction).
2. /home `?three=1` — kill or keep after you've looked.
3. Design q6 — report #22's "Open in Explorer" ask (recommend honest
   Copy-path now; connector command channel only if demand persists).
4. Design q7 — green-light the properties/units relational step (unblocks
   reports #20/#54).

## Resolve-clicks queued (after next restart + a quick look)

- #17, #47 (the renames, live at next restart) · #8, #26, #27 (verified
  Jul-24) · #42, #48, #46, #36, #25, #37 (the other session's fixes, live
  since the Jul-30 restart — its own notes say verify-then-resolve).

## Restart state

Running code predates tonight's commits — next restart activates: both
design-decision behaviors (q2, q5), the renames (q8), and the password
minimum (q4a), plus whatever the other sessions landed after `8836225`.
