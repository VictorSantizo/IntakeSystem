r"""
payment_rules.py – mine the business's operating rules from the paid archive.

Joshua (2026-08-06): "if we do a lot more analysis on this side of the ledger
... we can get a much better picture of the rules and general operations of
the business." Input: the PAID_BACKFILL_<year>.csv datasets the dry runs
produce (parsed date / entity / method / CK# / payee / amount / properties per
packet). Output: T:\_OPS INDEX\PAYMENT_RULES.html — one page of discovered
rules, each section ending in what the app can DO with it:

  1. Vendor payment cadence — monthly/quarterly/annual/ad-hoc, typical amount,
     and when the NEXT payment is due → feeds Expected Bills, so a missing
     bill becomes visible, not just a late one.
  2. Who pays whom — the dominant paying entity per vendor and how
     concentrated it is → auto-fill + a wrong-entity alarm at review time.
  3. Method rules — vendors that are always check / always ACH → an odd
     payment method becomes a flag, and pay-time defaults get smarter.
  4. Check-number series per entity — ranges, gaps, and reuse per book →
     cross-checks the register; a gap is a missing record or a missing check.
  5. Property attribution — which properties each vendor serves → property
     auto-fill and misfile detection.
  6. Receipt-to-payment lag — how long mail waits before it's paid (2026
     matches only; the archive alone doesn't know when a bill arrived).
  7. Name variants — spellings that are almost surely the same vendor →
     seed rows for the vendor-alias table.

Read-only over the CSVs (movements and summary sheets excluded); no DB, no
files touched. Usage:
    python payment_rules.py [--years 2025 2026] [--ops "T:\_OPS INDEX"]
"""
import argparse
import csv
import datetime
import html
import os
import re
import statistics
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nameparse  # noqa: E402

_EXCLUDE_DISPOSITIONS = {"skip-movement", "skip-summary"}

# Cadence classification from the MEDIAN gap between a vendor's payments.
# Bands are generous: real bills wobble around their cycle.
_CADENCES = (
    ((21, 45), "monthly"),
    ((46, 75), "every 2 months"),
    ((76, 120), "quarterly"),
    ((150, 240), "twice a year"),
    ((300, 450), "annual"),
)
_MIN_PAYMENTS_FOR_CADENCE = 3


def load_dataset(ops_dir, years):
    """All payment rows from the given years' dry-run CSVs, parsed and
    filtered to actual payments (a date and either an amount or a CK#)."""
    rows = []
    for y in years:
        path = os.path.join(ops_dir, f"PAID_BACKFILL_{y}.csv")
        try:
            with open(path, encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("disposition") in _EXCLUDE_DISPOSITIONS:
                        continue
                    try:
                        d = datetime.date.fromisoformat(r.get("name_date") or "")
                    except ValueError:
                        continue
                    amt = None
                    if r.get("amount"):
                        try:
                            amt = float(r["amount"])
                        except ValueError:
                            amt = None
                    if amt is None and not r.get("check"):
                        continue
                    rows.append({
                        "date": d, "entity": r.get("entity") or "?",
                        "method": r.get("method") or "",
                        "check": r.get("check") or "",
                        "amount": amt, "payee": (r.get("payee") or "").strip(),
                        "props": [p for p in (r.get("props") or "").split() if p],
                        "year": y,
                    })
        except OSError:
            print(f"[PaymentRules] no dataset for {y} ({path}) — skipped")
    return rows


def _vendor_key(payee: str) -> str:
    """Canonical grouping key: the payee's significant tokens, sorted. 'SO.
    CALIF. EDISON' and 'SO CALIF EDISON' group; genuinely different names
    don't. Empty-token payees group under their raw uppercase name."""
    toks = nameparse.vtokens(payee)
    return " ".join(sorted(toks)) if toks else (payee or "?").upper()


def group_by_vendor(rows):
    groups = defaultdict(list)
    for r in rows:
        if r["payee"]:
            groups[_vendor_key(r["payee"])].append(r)
    return groups


def display_name(group) -> str:
    """The most common raw spelling, as the group's face."""
    return Counter(r["payee"].upper() for r in group).most_common(1)[0][0]


def cadence_of(dates):
    """(label, median_gap_days) from sorted payment dates; (None, None) when
    there's no stable rhythm to speak of."""
    if len(dates) < _MIN_PAYMENTS_FOR_CADENCE:
        return None, None
    gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
    if not gaps:
        return None, None
    med = statistics.median(gaps)
    for (lo, hi), label in _CADENCES:
        if lo <= med <= hi:
            return label, med
    return None, med


def vendor_cadences(groups, today=None):
    """Per-vendor rhythm: cadence, typical amount, last seen, next expected.
    Only vendors with a recognizable cadence make the list."""
    today = today or datetime.date.today()
    out = []
    for key, g in groups.items():
        dates = sorted(r["date"] for r in g)
        label, med = cadence_of(dates)
        if not label:
            continue
        amounts = [r["amount"] for r in g if r["amount"] is not None]
        next_due = dates[-1] + datetime.timedelta(days=int(med))
        out.append({
            "vendor": display_name(g), "n": len(g), "cadence": label,
            "median_gap": med,
            "typical": statistics.median(amounts) if amounts else None,
            "lo": min(amounts) if amounts else None,
            "hi": max(amounts) if amounts else None,
            "last": dates[-1], "next": next_due,
            "overdue": next_due < today - datetime.timedelta(days=14),
            "entities": Counter(r["entity"] for r in g),
        })
    out.sort(key=lambda v: -v["n"])
    return out


def entity_rules(groups, min_n=3):
    """Vendors with a DOMINANT paying entity (≥90%), and the split payees."""
    dominant, split = [], []
    for key, g in groups.items():
        if len(g) < min_n:
            continue
        ents = Counter(r["entity"] for r in g)
        (top, n), total = ents.most_common(1)[0], len(g)
        row = {"vendor": display_name(g), "n": total, "entity": top,
               "share": n / total, "entities": ents}
        (dominant if n / total >= 0.9 else split).append(row)
    dominant.sort(key=lambda v: -v["n"])
    split.sort(key=lambda v: -v["n"])
    return dominant, split


def method_rules(groups, min_n=3):
    """Vendors always paid one way (100% check or 100% ACH)."""
    out = []
    for key, g in groups.items():
        methods = [r["method"] for r in g if r["method"]]
        if len(methods) < min_n:
            continue
        c = Counter(methods)
        (top, n) = c.most_common(1)[0]
        if n == len(methods):
            out.append({"vendor": display_name(g), "n": len(methods),
                        "method": top})
    out.sort(key=lambda v: -v["n"])
    return out


def check_series(rows):
    """Per entity: the check-number books in use, with gaps and reuse.
    Consecutive checks usually differ by small steps; a fresh book jumps far.
    Numbers are clustered into 'books' (gap > 1000 starts a new one), then
    each book reports its range, coverage, and missing numbers."""
    per_ent = defaultdict(list)
    for r in rows:
        if r["check"]:
            try:
                per_ent[r["entity"]].append((int(r["check"]), r["date"]))
            except ValueError:
                continue
    out = []
    for ent, pairs in sorted(per_ent.items()):
        nums = sorted({n for n, _d in pairs})
        reuse = [n for n, c in Counter(n for n, _d in pairs).items() if c > 2]
        books = []
        start = 0
        for i in range(1, len(nums) + 1):
            if i == len(nums) or nums[i] - nums[i - 1] > 1000:
                book = nums[start:i]
                if len(book) >= 5:
                    span = book[-1] - book[0] + 1
                    missing = span - len(book)
                    books.append({"lo": book[0], "hi": book[-1], "n": len(book),
                                  "missing": missing,
                                  "coverage": len(book) / span})
                start = i
        out.append({"entity": ent, "checks": len(pairs), "books": books,
                    "reused": sorted(reuse)[:12]})
    return out


def property_rules(groups, min_n=3):
    """Vendors tied to specific properties (for auto-fill / misfile alarms)."""
    out = []
    for key, g in groups.items():
        props = Counter(p for r in g for p in r["props"])
        if not props or len(g) < min_n:
            continue
        out.append({"vendor": display_name(g), "n": len(g),
                    "props": props.most_common(6)})
    out.sort(key=lambda v: -v["n"])
    return out


def name_variants(groups):
    """Spelling clusters: distinct raw payee spellings inside one token-key
    group — ready-made vendor-alias seed rows."""
    out = []
    for key, g in groups.items():
        spellings = Counter(r["payee"].upper() for r in g)
        if len(spellings) > 1:
            out.append({"canonical": display_name(g),
                        "variants": spellings.most_common(), "n": len(g)})
    out.sort(key=lambda v: -len(v["variants"]))
    return out


def receipt_to_payment_lag(db_path):
    """Days from received to paid, from the app's own Paid records (a private
    snapshot read via paid_backfill.load_entries — the live DB is never
    opened). App-era only: the archive alone doesn't know when a bill
    arrived. Returns a sorted list of lag days (negatives dropped — a paid
    date before the received date is a data typo, not a rule)."""
    if not (db_path and os.path.exists(db_path)):
        return []
    import paid_backfill
    lags = []
    entries, _att = paid_backfill.load_entries(db_path)
    for e in entries:
        if e.get("status") != "Paid":
            continue
        try:
            rec = datetime.date.fromisoformat((e.get("date_received") or "")[:10])
            paid = datetime.date.fromisoformat((e.get("paid_date") or "")[:10])
        except ValueError:
            continue
        d = (paid - rec).days
        if 0 <= d <= 400:
            lags.append(d)
    return sorted(lags)


def analyze(ops_dir, years, today=None, db_path=None):
    rows = load_dataset(ops_dir, years)
    groups = group_by_vendor(rows)
    dominant, split = entity_rules(groups)
    return {
        "rows": rows, "groups": groups,
        "cadences": vendor_cadences(groups, today=today),
        "dominant": dominant, "split": split,
        "methods": method_rules(groups),
        "series": check_series(rows),
        "props": property_rules(groups),
        "variants": name_variants(groups),
        "lags": receipt_to_payment_lag(db_path),
        "years": years,
    }


# ── the artifact ──────────────────────────────────────────────────────────────

def write_html(res, out_path, generated=None):
    esc = lambda s: html.escape(str(s if s is not None else ""))
    money = lambda v: f"${v:,.2f}" if v is not None else "—"
    when = generated or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    rows, years = res["rows"], res["years"]
    ent_totals = defaultdict(float)
    for r in rows:
        if r["amount"]:
            ent_totals[r["entity"]] += r["amount"]

    p = [f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Payment Rules — mined from the paid archive</title><style>
 body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#222;max-width:1200px}}
 h1{{font-size:1.4rem}} h2{{font-size:1.1rem;margin-top:30px}}
 table{{border-collapse:collapse;width:100%;font-size:.84rem;margin-top:6px}}
 th,td{{border:1px solid #ccc;padding:4px 8px;text-align:left;vertical-align:top}}
 th{{background:#f0f0f0}} tr:nth-child(even){{background:#fafafa}}
 .num{{text-align:right}} .why{{background:#E8F1FB;border:1px solid #9DBEE3;
 padding:8px 12px;border-radius:6px;margin:8px 0;font-size:.86rem}}
 .warn{{color:#C62828;font-weight:600}}
</style></head><body>
<h1>&#129504; Payment Rules — mined from the paid archive ({" + ".join(map(str, years))})</h1>
<p>Generated {esc(when)} · {len(rows)} payment documents ·
{len(res['groups'])} distinct payees · parsed from the PAID BILLS filenames
(the dry-run datasets). Every rule below is descriptive — what the business
already does — with the app feature it can power.</p>"""]

    # 1. Cadence
    cad = res["cadences"]
    p.append(f"""<h2>1. Vendor payment rhythms ({len(cad)} vendors with a beat)</h2>
<div class="why">&#8594; Feeds <b>Expected Bills</b>: a vendor with a rhythm whose next
payment is overdue means a bill went missing — the opposite failure of a duplicate.</div>
<table><tr><th>Vendor</th><th class="num">Payments</th><th>Cadence</th>
<th class="num">Typical $</th><th class="num">Range</th><th>Last paid</th>
<th>Next expected</th></tr>""")
    for v in cad[:60]:
        nxt = esc(v["next"].isoformat()) + (" <span class='warn'>overdue?</span>"
                                            if v["overdue"] else "")
        p.append(f"<tr><td>{esc(v['vendor'])}</td><td class='num'>{v['n']}</td>"
                 f"<td>{esc(v['cadence'])}</td><td class='num'>{money(v['typical'])}</td>"
                 f"<td class='num'>{money(v['lo'])} – {money(v['hi'])}</td>"
                 f"<td>{v['last'].isoformat()}</td><td>{nxt}</td></tr>")
    p.append("</table>")

    # 2. Entity rules
    dom, split = res["dominant"], res["split"]
    p.append(f"""<h2>2. Who pays whom ({len(dom)} vendors with one clear payer)</h2>
<div class="why">&#8594; Auto-fills the entity at review time and flags a bill
about to be paid from the WRONG account.</div>
<table><tr><th>Vendor</th><th class="num">Payments</th><th>Paying entity</th>
<th class="num">Share</th></tr>""")
    for v in dom[:50]:
        p.append(f"<tr><td>{esc(v['vendor'])}</td><td class='num'>{v['n']}</td>"
                 f"<td>{esc(v['entity'])}</td><td class='num'>{v['share']:.0%}</td></tr>")
    p.append("</table>")
    if split:
        p.append(f"""<h3 style="font-size:.95rem;">Split payees ({len(split)} vendors paid by several entities — normal for shared services, worth knowing)</h3>
<table><tr><th>Vendor</th><th class="num">Payments</th><th>Entities</th></tr>""")
        for v in split[:25]:
            ents = ", ".join(f"{e} ({n})" for e, n in v["entities"].most_common())
            p.append(f"<tr><td>{esc(v['vendor'])}</td><td class='num'>{v['n']}</td>"
                     f"<td>{esc(ents)}</td></tr>")
        p.append("</table>")

    # 3. Method rules
    meth = res["methods"]
    p.append(f"""<h2>3. Always paid the same way ({len(meth)} vendors)</h2>
<div class="why">&#8594; Pay-time defaults, and an alarm when a check shows up
for an always-ACH vendor (or the reverse).</div>
<table><tr><th>Vendor</th><th class="num">Payments</th><th>Method — every time</th></tr>""")
    for v in meth[:40]:
        p.append(f"<tr><td>{esc(v['vendor'])}</td><td class='num'>{v['n']}</td>"
                 f"<td>{esc(v['method'])}</td></tr>")
    p.append("</table>")

    # 4. Check series
    p.append("""<h2>4. Check books per entity</h2>
<div class="why">&#8594; Cross-checks the register: a gap inside a book is a
check this dataset never saw — a missing record, a voided check, or a check
that never cleared. Heavy reuse of one number is usually a typo cluster.</div>
<table><tr><th>Entity</th><th class="num">Checks</th><th>Books (lo–hi · seen · missing inside range)</th><th>Numbers used 3+ times</th></tr>""")
    for s in res["series"]:
        books = "<br>".join(
            f"CK#{b['lo']:,}–{b['hi']:,} · {b['n']} seen · "
            f"{b['missing']} missing ({b['coverage']:.0%} coverage)"
            for b in s["books"]) or "—"
        reused = ", ".join(f"CK#{n}" for n in s["reused"]) or "—"
        p.append(f"<tr><td>{esc(s['entity'])}</td><td class='num'>{s['checks']}</td>"
                 f"<td>{books}</td><td>{reused}</td></tr>")
    p.append("</table>")

    # 5. Property attribution
    pr = res["props"]
    p.append(f"""<h2>5. Vendors and their properties ({len(pr)} vendors with property ties)</h2>
<div class="why">&#8594; Property auto-fill, and a misfile alarm when a
gardener who only ever serves PP58 shows up on a TRUST3 bill.</div>
<table><tr><th>Vendor</th><th class="num">Payments</th><th>Properties (count)</th></tr>""")
    for v in pr[:40]:
        props = ", ".join(f"{c} ({n})" for c, n in v["props"])
        p.append(f"<tr><td>{esc(v['vendor'])}</td><td class='num'>{v['n']}</td>"
                 f"<td>{esc(props)}</td></tr>")
    p.append("</table>")

    # 6. Dollars per entity (context)
    p.append("""<h2>6. Dollar flow per entity (this dataset)</h2>
<table><tr><th>Entity</th><th class="num">$ total</th></tr>""")
    for ent, tot in sorted(ent_totals.items(), key=lambda kv: -kv[1]):
        p.append(f"<tr><td>{esc(ent)}</td><td class='num'>${tot:,.2f}</td></tr>")
    p.append("</table>")

    # 6b. Receipt-to-payment lag
    lags = res.get("lags") or []
    if lags:
        med = statistics.median(lags)
        p90 = lags[int(len(lags) * 0.9) - 1] if len(lags) > 1 else lags[-1]
        buckets = Counter()
        for d in lags:
            buckets["same week" if d <= 7 else
                    "within a month" if d <= 31 else
                    "1–2 months" if d <= 62 else "over 2 months"] += 1
        p.append(f"""<h2>6b. How long mail waits to be paid (app-era records, n={len(lags)})</h2>
<div class="why">&#8594; Real aging data for the pay-in-received-order stack:
median {med:.0f} days, 90% paid within {p90} days. The archive alone can't
know arrival dates, so this grows as the backfill matches more packets.</div>
<table><tr><th>Paid&hellip;</th><th class="num">Bills</th></tr>""")
        for label in ("same week", "within a month", "1–2 months", "over 2 months"):
            if buckets.get(label):
                p.append(f"<tr><td>{label}</td><td class='num'>{buckets[label]}</td></tr>")
        p.append("</table>")

    # 7. Name variants
    var = res["variants"]
    p.append(f"""<h2>7. Same vendor, several spellings ({len(var)} clusters)</h2>
<div class="why">&#8594; Seed rows for the vendor-alias table — one history per
vendor instead of one per spelling (the Southern California Edison problem).</div>
<table><tr><th>Most common spelling</th><th>Variants seen (count)</th></tr>""")
    for v in var[:40]:
        variants = " · ".join(f"{esc(s)} ({n})" for s, n in v["variants"])
        p.append(f"<tr><td>{esc(v['canonical'])}</td><td>{variants}</td></tr>")
    p.append("</table></body></html>")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(p))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--years", type=int, nargs="+", default=[2025, 2026])
    ap.add_argument("--ops", default=r"T:\_OPS INDEX")
    ap.add_argument("--db", default=r"T:\IntakeSystem\intake.db")
    args = ap.parse_args(argv)
    res = analyze(args.ops, args.years, db_path=args.db)
    if not res["rows"]:
        sys.exit("No dry-run datasets found — run paid_backfill.py first.")
    out = os.path.join(args.ops, "PAYMENT_RULES.html")
    write_html(res, out)
    print(f"[PaymentRules] {len(res['rows'])} payments, "
          f"{len(res['groups'])} payees -> {out}")
    print(f"[PaymentRules] cadences: {len(res['cadences'])} · "
          f"dominant-entity: {len(res['dominant'])} · "
          f"single-method: {len(res['methods'])} · "
          f"variant clusters: {len(res['variants'])}")


if __name__ == "__main__":
    main()
