r"""
paid_backfill.py – phase-1 DRY RUN: match a year's PAID BILLS trees against the
intake log, and report what a backfill would do — without doing any of it.

Joshua's ask (2026-08-06): pull the archive's payment documents into Klick
Klack so paid-vs-unpaid is answerable, matching to existing records first and
never minting a duplicate. This tool is the look-before-you-leap step: it walks
the seven per-entity "… PAID BILLS" trees for one year, parses each filename
with the SAME grammar the live cabinet watcher uses (nameparse.py), applies the
same match / near-match / hold rules (ported from cabinet_watch.py — keep them
in sync), and writes a review artifact into T:\_OPS INDEX:

    PAID_BACKFILL_<year>.html   — human summary, grouped by disposition
    PAID_BACKFILL_<year>.csv    — one row per file, the phase-2 apply input

Dispositions (what apply WOULD do, per file):
    known          already registered (a record's filed/pdf path or attachment)
    linked-by-hash byte-identical to a logged document → attach as copy
    match          matches a logged record (amount ±$0.01 + vendor tokens +
                   date window) → attach; if the record is Pending, also mark
                   Paid with the CK#/method the filename carries
    held           needs a person: near-match twin risk, CK# conflicts with a
                   record already Paid under a different check, two packets
                   claiming one record, no parseable date on a payment, or a
                   date outside the target year
    create-paid    clean payment filing, nothing to match → new record,
                   already Paid (OCR cross-check happens at apply time)
    needs-ocr      in a paid tree but no payment marker in the name — only an
                   OCR read can classify it (apply-time work, per-file AI)
    skip-movement  deposits / owner draws / transfers — not vendor bills
    skip-summary   recap/summary/register sheets — not documents to register

STRICTLY READ-ONLY: the intake DB is opened mode=ro&immutable=1 (a snapshot —
never creates -wal/-shm on the share, per the never-write-from-a-workstation
rule), no file is moved, renamed, or hashed into any database. `apply` is
phase 2 and deliberately refuses to run here — it must execute inside the
app's process on the server.

Usage:
    python paid_backfill.py dry-run [--year 2026] [--archive T:\Files]
        [--db T:\IntakeSystem\intake.db] [--out "T:\_OPS INDEX"] [--no-hash]
"""
import argparse
import csv
import datetime
import hashlib
import html
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict

# The bundled interpreter (python\python314._pth) pins sys.path and drops the
# usual script-directory entry, so a CLI run can't see its sibling modules
# without this. Harmless under a normal Python.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nameparse  # noqa: E402

UNC_FILES_ROOT = r"\\LACKSSERVER2021\File-Cabinet\Files"
_DOC_EXTS = (".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".docx")

# Same thresholds as cabinet_watch._match_entry / _near_match (ported 2026-08-06
# — if those rules change, change these WITH them; the dry run's whole value is
# predicting what the live rules would do).
_AMOUNT_TOL = 0.01
_DATE_WINDOW_DAYS = 60


def load_entries(db_path: str):
    """The intake log + attachment paths, as plain dicts — read from a PRIVATE
    temp copy of the database (plus its -wal, so rows the server hasn't
    checkpointed yet still appear; WAL frame checksums make a mid-write copy
    recover to a consistent prefix). The live intake.db is never opened at all
    by this tool — not even read-only, which on WAL would need the shared -shm
    — honoring the never-touch-the-DB-from-a-workstation rule."""
    import shutil
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="paid_backfill_snap_")
    try:
        snap = os.path.join(tmpdir, "snapshot.db")
        shutil.copy2(db_path, snap)
        wal = db_path + "-wal"
        if os.path.exists(wal):
            shutil.copy2(wal, snap + "-wal")
        conn = sqlite3.connect(snap)
        conn.row_factory = sqlite3.Row
        try:
            entries = [dict(r) for r in conn.execute(
                "SELECT id, vendor, amount, date_received, status, check_number, "
                "       payment_method, property_code, file_hash, filed_path, "
                "       pdf_path, doc_type, paid_date FROM intake_log")]
            att_paths = [r[0] for r in conn.execute(
                "SELECT file_path FROM attachments WHERE file_path IS NOT NULL")]
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return entries, att_paths


def known_paths(entries: list, att_paths: list) -> set:
    out = set()
    for e in entries:
        for k in ("filed_path", "pdf_path"):
            if e.get(k):
                out.add(os.path.normcase(os.path.normpath(e[k])))
    for p in att_paths:
        out.add(os.path.normcase(os.path.normpath(p)))
    return out


def _match_entry(doc, entries):
    """Ported from cabinet_watch._match_entry (minus the hash arm — hashes are
    handled by the caller): amount (±$0.01) + vendor-token overlap +
    received-date within 60 days."""
    if not doc["amounts"]:
        return None, None
    dt = doc["tokens"]
    for e in entries:
        if e.get("amount") is None or not any(
                abs(a - e["amount"]) < _AMOUNT_TOL for a in doc["amounts"]):
            continue
        if not (nameparse.vtokens(e.get("vendor")) & dt):
            continue
        try:
            rec = datetime.date.fromisoformat((e.get("date_received") or "")[:10])
        except ValueError:
            rec = None
        if doc["date"] and rec and abs((doc["date"] - rec).days) > _DATE_WINDOW_DAYS:
            continue
        return e, "amount + vendor + date window"
    return None, None


def _veto_tokens(doc) -> set:
    """Vendor tokens for the twin-risk veto: the parsed PAYEE, not the whole
    stem. Whole-stem tokens carry the entity word — every TRUST-tree packet
    contains 'TRUST' — and in a batch run that made one blank-amount
    'MONTECITO BANK & TRUST' record hold 146 unrelated packets."""
    return nameparse.vtokens(doc["payee"]) or doc["tokens"]


def _near_match(doc, entries):
    """The twin-risk veto before a fresh Paid record — cabinet_watch's rule,
    scoped for BATCH use (the live watcher judges one fresh filing, where any
    related record is worth a pause; over 1,356 files the same rule held 879).
    Batch scoping, first dry run 2026-08-06:
      * only records a payment could BE FOR may veto (Bill / Receipt / blank
        doc_type) — a bank statement or legal record is never 'paid';
      * vendor overlap must be substantive: two shared tokens, or the whole of
        one side's name (so CHASE↔CHASE still counts but a lone shared
        'ELECTRIC' between two different electricians doesn't)."""
    dt = _veto_tokens(doc)
    dprops = set(doc["props"])
    out = []
    for e in entries:
        if (e.get("doc_type") or "") not in ("Bill", "Receipt", ""):
            continue
        et = nameparse.vtokens(e.get("vendor"))
        shared = et & dt
        tok_hit = len(shared) >= 2 or (bool(shared) and shared in (et, dt))
        amt_hit = (e.get("amount") is not None and doc["amounts"]
                   and any(abs(a - e["amount"]) < _AMOUNT_TOL for a in doc["amounts"]))
        prop_hit = bool(dprops & {p.strip().upper() for p in
                                  (e.get("property_code") or "").split(",") if p.strip()})
        if (tok_hit and (amt_hit or e.get("amount") is None)) or (amt_hit and prop_hit):
            out.append((_days_apart(doc, e), e))
    out.sort(key=lambda t: t[0])
    return [e for _d, e in out]


def _days_apart(doc, e) -> int:
    try:
        rec = datetime.date.fromisoformat((e.get("date_received") or "")[:10])
    except ValueError:
        rec = None
    if doc["date"] and rec:
        return abs((doc["date"] - rec).days)
    return 9999


# A record can be the twin of at most ONE packet, so it may hold at most this
# many candidates (the closest-dated ones — a margin of two covers an off-by-a-
# cycle guess). Without the cap, one blank-amount pending Edison bill held all
# 99 Edison packets of the year (first live dry run, 2026-08-06).
_VETO_CAP = 2


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_files(archive: str, year: int) -> list:
    r"""Every document file under <archive>\<year> whose folder is a PAID BILLS
    tree (nameparse.path_priors decides — same test the cabinet watcher uses)."""
    base = os.path.join(archive, str(year))
    out = []
    for root, _dirs, files in os.walk(base):
        for name in files:
            if os.path.splitext(name)[1].lower() not in _DOC_EXTS:
                continue
            p = os.path.join(root, name)
            if nameparse.path_priors(p)["in_paid_tree"]:
                out.append(p)
    out.sort()
    return out


def classify_file(path, doc, year, entries, by_hash, known, fhash):
    """One file's disposition: (disposition, reason, matched_entry_or_None).
    Mirrors cabinet_watch._process's decision ladder for a paid-tree file."""
    if os.path.normcase(os.path.normpath(path)) in known:
        return "known", "already registered to a record or attachment", None
    if fhash and fhash in by_hash:
        e = by_hash[fhash]
        return "linked-by-hash", f"byte-identical to record #{e['id']}", e
    if doc["is_nonbill"]:
        return "skip-movement", "deposit / owner draw / transfer — not a vendor bill", None
    if doc["is_summary"]:
        return "skip-summary", "summary / recap / register sheet", None

    matched, why = _match_entry(doc, entries)
    if matched:
        if (doc["is_payment"] and matched.get("status") == "Paid" and doc["check"]
                and (matched.get("check_number") or "") not in ("", doc["check"])):
            return ("held", f"CK#{doc['check']} vs record #{matched['id']} already "
                    f"Paid under CK#{matched.get('check_number')} — likely a "
                    f"different cycle of a recurring bill", matched)
        action = ("attach as the payment document" if doc["is_payment"]
                  else "attach as a related document")
        if matched.get("status") != "Paid" and doc["is_payment"]:
            action = "attach + mark Paid" + (f" (CK#{doc['check']})" if doc["check"] else "")
        return "match", f"{why} → {action}", matched

    if not doc["is_payment"]:
        return ("needs-ocr", "no CK#/ACH/bill-pay marker in the name — needs the "
                "OCR read to classify (apply-time)", None)
    if not doc["date"]:
        return "held", "payment filing with no parseable date — never guess a paid date", None
    if doc["date"].year != year:
        return ("held", f"filename date {doc['date'].isoformat()} is outside "
                f"{year} — misfiled or a typo", None)
    cands = _near_match(doc, entries)
    if cands:
        near = cands[0]
        return ("near-veto", f"near-match to record #{near['id']} "
                f"({near.get('vendor') or '?'}, {near.get('status')}) — a fresh "
                f"Paid record risks a twin", cands)
    return "create-paid", "clean payment filing, nothing on file to match", None


def run_dryrun(archive: str, year: int, db_path: str, out_dir: str,
               do_hash: bool = True, unc_root: str = UNC_FILES_ROOT) -> dict:
    entries, att_paths = load_entries(db_path)
    known = known_paths(entries, att_paths)
    by_hash = {e["file_hash"]: e for e in entries if e.get("file_hash")}
    files = collect_files(archive, year)

    rows = []
    for i, path in enumerate(files, 1):
        doc = nameparse.parse_name(path)
        fhash = ""
        if do_hash:
            try:
                fhash = _sha256(path)
            except OSError:
                fhash = ""
        disp, reason, matched = classify_file(path, doc, year, entries, by_hash,
                                              known, fhash)
        cands = None
        if disp == "near-veto":
            cands, matched = matched, None
        rows.append({"path": path, "doc": doc, "disposition": disp,
                     "reason": reason, "matched": matched, "cands": cands})
        if i % 200 == 0:
            print(f"  ...{i}/{len(files)}")

    # Twin-risk capacity pass: a record can be the twin of at most one packet,
    # so each may hold only its _VETO_CAP closest-dated candidates; every other
    # same-vendor packet is a different payment cycle and becomes create-paid.
    # Closest packets claim capacity first, then try their next candidate.
    capacity = Counter()
    vetoed = [r for r in rows if r["disposition"] == "near-veto"]
    vetoed.sort(key=lambda r: _days_apart(r["doc"], r["cands"][0]))
    for r in vetoed:
        holder = next((e for e in r["cands"] if capacity[e["id"]] < _VETO_CAP), None)
        if holder is not None:
            capacity[holder["id"]] += 1
            r["disposition"], r["matched"] = "held", holder
            r["reason"] = (f"near-match to record #{holder['id']} "
                           f"({holder.get('vendor') or '?'}, {holder.get('status')})"
                           f" — a fresh Paid record risks a twin")
        else:
            r["disposition"] = "create-paid"
            r["reason"] = ("same-vendor records exist but their closest-dated "
                           "packets are already held — this one is a different "
                           "payment cycle")

    # Two payment packets claiming the SAME record is a decision, not a match:
    # keep the packet whose date sits closest to the record's received date,
    # hold the rest. (A monthly bill logged once but paid twelve times in the
    # archive would otherwise all pile onto one record.)
    claims = defaultdict(list)
    for r in rows:
        if r["disposition"] == "match" and r["matched"] is not None:
            claims[r["matched"]["id"]].append(r)
    for eid, claimants in claims.items():
        if len(claimants) < 2:
            continue

        def closeness(r):
            try:
                rec = datetime.date.fromisoformat(
                    (r["matched"].get("date_received") or "")[:10])
            except ValueError:
                return 9999
            return abs((r["doc"]["date"] - rec).days) if r["doc"]["date"] else 9999

        claimants.sort(key=closeness)
        for r in claimants[1:]:
            r["disposition"] = "held"
            r["reason"] = (f"record #{eid} is already claimed by a closer-dated "
                           f"packet this run — likely a recurring bill logged once")

    summary = Counter(r["disposition"] for r in rows)
    dollars = defaultdict(float)
    for r in rows:
        if r["doc"]["amounts"]:
            dollars[r["disposition"]] += max(r["doc"]["amounts"])

    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, f"PAID_BACKFILL_{year}.csv")
    html_path = os.path.join(out_dir, f"PAID_BACKFILL_{year}.html")
    _write_csv(csv_path, rows, archive, unc_root)
    _write_html(html_path, rows, summary, dollars, year, archive, unc_root,
                db_path, len(entries))
    print(f"[PaidBackfill] {len(files)} files -> {dict(summary)}")
    print(f"[PaidBackfill] wrote {html_path} and {csv_path}")
    return {"files": len(files), "summary": dict(summary),
            "html": html_path, "csv": csv_path}


def _unc(path: str, archive: str, unc_root: str) -> str:
    """Artifact paths are full UNC (house rule: drive letters differ per desk)."""
    p = os.path.normpath(path)
    a = os.path.normpath(archive)
    if os.path.normcase(p).startswith(os.path.normcase(a)):
        return unc_root + p[len(a):]
    return p


_CSV_COLS = ("disposition", "reason", "unc_path", "name_date", "entity",
             "method", "check", "amount", "payee", "props",
             "matched_id", "matched_status", "matched_vendor", "matched_amount",
             "matched_check")


def _write_csv(path, rows, archive, unc_root):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(_CSV_COLS)
        for r in rows:
            d, m = r["doc"], r["matched"]
            w.writerow([
                r["disposition"], r["reason"], _unc(r["path"], archive, unc_root),
                d["date"].isoformat() if d["date"] else "",
                d["entity"] or "", d["method"] or "", d["check"] or "",
                f"{max(d['amounts']):.2f}" if d["amounts"] else "",
                d["payee"] or "", " ".join(d["props"]),
                m["id"] if m else "", (m.get("status") or "") if m else "",
                (m.get("vendor") or "") if m else "",
                (f"{m['amount']:.2f}" if m and m.get("amount") is not None else ""),
                (m.get("check_number") or "") if m else "",
            ])


_ORDER = ("match", "held", "create-paid", "needs-ocr", "linked-by-hash",
          "known", "skip-movement", "skip-summary")

_BLURB = {
    "match": "Would attach to the existing record — and mark it Paid if it is "
             "still Pending. This is the payoff bucket: each one flips an "
             "\u201cowed\u201d bill to Paid with its check number and proof document.",
    "held": "A person decides these — twin risks, check-number conflicts, "
            "recurring-bill ambiguity, missing or out-of-year dates.",
    "create-paid": "No existing record matches: apply would create a record "
                   "already marked Paid (with an OCR cross-check first).",
    "needs-ocr": "In a paid tree but the filename has no payment marker — only "
                 "an OCR read can say what these are.",
    "linked-by-hash": "Byte-identical to a document already logged — attach as "
                      "a copy, nothing new to create.",
    "known": "Already registered in Klick Klack (the reconciler, the cabinet "
             "watcher, or an earlier link) — nothing to do.",
    "skip-movement": "Deposits, owner draws, transfers — money movements, not "
                     "vendor bills; the backfill leaves them alone.",
    "skip-summary": "Recap/summary/register sheets — paper about payments, not "
                    "payment documents.",
}


def _write_html(path, rows, summary, dollars, year, archive, unc_root,
                db_path, n_entries):
    def esc(s):
        return html.escape(str(s or ""))

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Paid-Bills Backfill {year} — DRY RUN</title><style>
 body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#222}}
 h1{{font-size:1.4rem}} h2{{font-size:1.1rem;margin-top:28px}}
 table{{border-collapse:collapse;width:100%;font-size:.85rem}}
 th,td{{border:1px solid #ccc;padding:4px 8px;text-align:left;vertical-align:top}}
 th{{background:#f0f0f0}} tr:nth-child(even){{background:#fafafa}}
 .num{{text-align:right}} .badge{{font-weight:600}}
 .note{{background:#fff8e1;border:1px solid #e0c96b;padding:10px 14px;
        border-radius:6px;margin:14px 0}}
 .path{{font-family:Consolas,monospace;font-size:.78rem;word-break:break-all}}
</style></head><body>
<h1>&#128181; Paid-Bills Backfill {year} — DRY RUN</h1>
<p>Generated {esc(now)} · {len(rows)} files under the seven
&ldquo;&hellip; PAID BILLS&rdquo; trees of {esc(_unc(os.path.join(archive, str(year)), archive, unc_root))}
· matched against a read-only snapshot of the intake log ({n_entries} records).</p>
<div class="note"><strong>Nothing was changed.</strong> No records were created
or updated, no files moved, renamed, or touched. This report is the phase-1
review; applying it is a separate, server-side step that runs only after this
report is approved. To work through these packet by packet — each document
shown next to the record it matches — use the
<a href="/paid-backfill">visual review tool</a> in Klick Klack (admin).</div>
<h2>Summary</h2>
<table><tr><th>Disposition</th><th class="num">Files</th>
<th class="num">$ (from filenames)</th><th>Meaning</th></tr>"""]
    for d in _ORDER:
        if summary.get(d):
            parts.append(
                f"<tr><td class='badge'>{esc(d)}</td>"
                f"<td class='num'>{summary[d]}</td>"
                f"<td class='num'>${dollars.get(d, 0):,.2f}</td>"
                f"<td>{esc(_BLURB.get(d, ''))}</td></tr>")
    parts.append("</table>")

    for d in _ORDER:
        group = [r for r in rows if r["disposition"] == d]
        if not group:
            continue
        parts.append(f"<h2>{esc(d)} ({len(group)})</h2>")
        parts.append("<table><tr><th>File</th><th>Parsed</th>"
                     "<th>Matched record</th><th>Why</th></tr>")
        for r in group:
            doc, m = r["doc"], r["matched"]
            parsed = " · ".join(x for x in (
                doc["date"].isoformat() if doc["date"] else "no date",
                doc["entity"] or "", doc["method"] or "",
                f"CK#{doc['check']}" if doc["check"] else "",
                f"${max(doc['amounts']):,.2f}" if doc["amounts"] else "no $",
                doc["payee"] or "", " ".join(doc["props"])) if x)
            mtxt = ""
            if m:
                mtxt = (f"#{m['id']} {esc(m.get('vendor'))} "
                        f"({esc(m.get('status'))}"
                        + (f", CK#{esc(m['check_number'])}" if m.get("check_number") else "")
                        + (f", ${m['amount']:,.2f}" if m.get("amount") is not None else "")
                        + ")")
            parts.append(
                f"<tr><td class='path'>{esc(_unc(r['path'], archive, unc_root))}</td>"
                f"<td>{esc(parsed)}</td><td>{mtxt}</td><td>{esc(r['reason'])}</td></tr>")
        parts.append("</table>")
    parts.append("</body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("mode", choices=["dry-run", "apply"])
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--archive", default=r"T:\Files")
    ap.add_argument("--db", default=r"T:\IntakeSystem\intake.db")
    ap.add_argument("--out", default=r"T:\_OPS INDEX")
    ap.add_argument("--unc-root", default=UNC_FILES_ROOT)
    ap.add_argument("--no-hash", action="store_true",
                    help="skip byte-hash matching (faster, loses the "
                         "identical-file bucket)")
    args = ap.parse_args(argv)
    if args.mode == "apply":
        sys.exit("apply is phase 2: it writes the intake DB, so it must run "
                 "inside the app's process on the server — and only after the "
                 "dry-run report is approved. Nothing was done.")
    run_dryrun(args.archive, args.year, args.db, args.out,
               do_hash=not args.no_hash, unc_root=args.unc_root)


if __name__ == "__main__":
    main()
