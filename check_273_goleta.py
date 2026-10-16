r"""check_273_goleta.py — readiness check for the #273 payment correction (report #11).

Entry #273 (GOLETA WATER BILL PAYMENT RECEIPTS, TL222) shows amount=$500 — an AI
misread of a combined payment receipt. paid_amount is already correct ($1,645.17,
set by Nancy on 2026-07-17 when she marked it paid). What's still wrong is the
$500 `amount` field, which should reflect the two separate invoices the receipt
actually covers: account 1724 ($1,507.60) and account 1764 ($137.57).

Those two invoices only exist in intake_log once the June/July harvest --backfill
(see T:\_OPS INDEX\harvest_daily_filing.py --backfill) has actually registered
them. This script only CHECKS for that and prints the fix — it does not write
anything, since which record(s) should end up linked to #273 (via the #5
link-original feature once it ships) is a judgment call, not a mechanical one.

Read-only: opens intake.db in mode=ro. Safe to run from a workstation.
Run:  T:\IntakeSystem\python\python.exe check_273_goleta.py [--db PATH]
"""
import argparse
import os
import sqlite3

ap = argparse.ArgumentParser()
ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "intake.db"))
args = ap.parse_args()

con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

entry = con.execute("SELECT * FROM intake_log WHERE id=273").fetchone()
if not entry:
    print("Record #273 not found in this database — check --db path.")
    raise SystemExit(1)
entry = dict(entry)
print(f"#273: {entry.get('vendor')} | amount=${entry.get('amount')} | "
      f"paid_amount=${entry.get('paid_amount')} | status={entry.get('status')}")

def find_invoice(acct_token):
    rows = con.execute(
        "SELECT id, vendor, amount, notes, pdf_path, filed_path FROM intake_log "
        "WHERE (notes LIKE ? OR pdf_path LIKE ? OR filed_path LIKE ?) "
        "AND UPPER(vendor) LIKE '%GOLETA%' AND id != 273",
        tuple(f"%{acct_token}%" for _ in range(3))).fetchall()
    return [dict(r) for r in rows]

found_1724 = find_invoice("1724")
found_1764 = find_invoice("1764")

print(f"\nInvoice acct #1724 ($1,507.60): {'FOUND — ' + str([r['id'] for r in found_1724]) if found_1724 else 'not registered yet'}")
print(f"Invoice acct #1764 ($137.57):  {'FOUND — ' + str([r['id'] for r in found_1764]) if found_1764 else 'not registered yet'}")

if found_1724 and found_1764:
    print(f"\nREADY. Both source invoices are registered (#{found_1724[0]['id']}, #{found_1764[0]['id']}).")
    print("Manual fix for #273 (in the app, Edit): change amount from "
          f"${entry.get('amount')} to $1,645.17 to match the already-correct paid_amount, "
          "then link #273 to both invoice records as their payment (once the #5 "
          "link-original action is available).")
else:
    print("\nNOT READY — run the harvest --backfill first (see harvest_daily_filing.py), "
          "then re-run this check.")
