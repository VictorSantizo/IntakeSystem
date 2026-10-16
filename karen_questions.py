r"""karen_questions.py — Karen's one-time migration questionnaire (web form).

The 2026-07 data audit (T:\_OPS INDEX\AUDIT_REPORT.md) left a short list of
questions only Karen can answer before the new accounting system's masters are
locked. This module is the single source of truth for the questions; answers
land in the karen_answers table (database.py) so they survive restarts and are
visible to whoever does the migration. The printable twin of this form is
T:\_OPS INDEX\KAREN ONE-PAGER - JULY 2026.docx.

Questions for Syed are included too — Karen relays them and reports back
(mark "ASK SYED" until she has his answer).
"""

SECTIONS = [
    ("A", "Quick confirmations",
     "Pick the answer that matches reality. A note beats a shrug — anything you want to add, add."),
    ("B", "Who are these?",
     "These three receive large payments from Trust 1 and we can't tell whether they're property owners or vendors."),
    ("C", "Ten date typos — already fixed; just glance",
     "Each of these sat in a run of correctly-dated rows, so the year is clearly a slip of the pen. "
     "We corrected them in the NEW system's copy only — your workbooks are untouched."),
    ("D", "Just checking",
     "Things we noticed while auditing — tell us if we've read them wrong."),
    ("E", "Syed's account list",
     "Answer what you know. If it needs Syed, pick ASK SYED, get his answer when you can, and update this page."),
    ("F", "QuickBooks",
     "The one thing that needs scheduling."),
    ("G", "Management fees",
     "We reconstructed how your management fees work from 13 years of checks — it reconciles "
     "to the penny (per-property % of collected rent, checks on the 15th and 30th). "
     "Four loose ends only you can settle:"),
]

# Every question gets a notes box in the form; "options" are the quick answers.
QUESTIONS = [
    # ---- A ----
    dict(id="q1", section="A",
         text="BRITT bank account = your mom's (Britten's) personal account that you manage. "
              "Keep it separate from Trust 3 in the new system?",
         options=["YES", "NO"]),
    dict(id="q2", section="A",
         text="“PERSONAL” files = you & Lawrence's personal spending (not your mom's)?",
         options=["YES", "NO"]),
    dict(id="q2b", section="A",
         text="Should personal accounts (yours and your mom's) go into the new system at all?",
         options=["INCLUDE THEM", "BUSINESS ONLY"]),
    dict(id="q3", section="A",
         text="The 5210 Carpinteria office: are its costs split 50/50 between KLR and LRC?",
         options=["YES", "NO — different split"],
         note_hint="If different — what's the real split?"),
    dict(id="q4", section="A",
         text="Trust 4 = “Lacks Family Properties” (new EIN 93-4735835), still being set up. "
              "We'll create it in the new system, empty and ready.",
         options=["OK", "NOT OK"]),
    dict(id="q5", section="A",
         text="Sold/former properties (966 Concha Loma, T20, T40, …): include their old history "
              "in the new system, or current properties only?",
         options=["INCLUDE HISTORY", "CURRENT ONLY"]),
    dict(id="q22", section="A",
         text="When a bill just says one of these — TRUST4, KLR-LRC, BRITT, or PERSONAL — with "
              "nothing more specific to identify which account it's for, should the system:",
         options=[
             dict(value="LEAVE IT BLANK — ALWAYS FLAG FOR A PERSON TO SORT OUT", recommended=True),
             dict(value="AUTO-FILL — IT'S ALWAYS ONE OBVIOUS ACCOUNT"),
         ],
         note_hint="Our two filing scripts currently disagree on this — one leaves it blank, the "
                    "other guesses. We recommend the safer, blank-and-flag option for financial "
                    "records, but it's your call — say why here if you'd rather auto-fill, or if "
                    "it's different for one of the four."),
    # ---- B ----
    dict(id="q6", section="B", text="Cosinus Properties",
         options=["OWNER", "VENDOR", "OTHER"], note_hint="If other — who are they?"),
    dict(id="q7", section="B", text="Coast Village Road LLC",
         options=["OWNER", "VENDOR", "OTHER"], note_hint="If other — who are they?"),
    dict(id="q8", section="B", text="Bob & Gail Young",
         options=["OWNER", "VENDOR", "OTHER"], note_hint="If other — who are they?"),
    # ---- C ----
    dict(id="q9", section="C",
         text="Do all ten corrected dates look right?",
         options=["ALL GOOD", "SOMETHING'S WRONG"],
         note_hint="If something's wrong — which # and what's the real date?"),
    dict(id="q10", section="C",
         text="One entry we did NOT touch — you tell us the year. In workbook "
              "PP CONSOLIDATION – MAR 2024.xlsx (the March 2024 register), row 22, there is a "
              "−$1,600 entry dated 3/09/2025 that says “Void check from 1/28/25 (re-issued)”. "
              "The rows around it are dated 3/08/2024–3/10/2024, but the entry itself mentions "
              "January 2025 — so it may truly belong to 2025. Which is right?",
         options=["MARCH 2024", "MARCH 2025", "NOT SURE — LET'S LOOK TOGETHER"]),
    # ---- D ----
    dict(id="q11", section="D",
         text="Dec 2020 (Trust 1 & 2) and Oct 2023 (PP) each have TWO versions of the consolidation "
              "workbook (“Practice”/“Copy”). The balances show which is the finished "
              "book — we use that one. Sound right?",
         options=["YES", "NO"]),
    dict(id="q12", section="D",
         text="PP cash-flow workbooks stop after Dec 2024 (the check registers keep going). "
              "Did you stop making them on purpose?",
         options=["YES — ON PURPOSE", "NO"]),
    dict(id="q13", section="D",
         text="Old Accupay payroll records — needed in the new system?",
         options=["YES", "NO", "DON'T KNOW"]),
    dict(id="q13b", section="D",
         text="California FAIR Plan, your mom's 123 La Vereda Rd: record #50 in Klick Klack "
              "is a $5,674.00 FAIR Plan document from the 6/27 email batch, still marked "
              "unpaid. The $1,026 invoice you paid 6/28 says a $5,674.00 payment was "
              "already applied to the previous balance — so we think #50 shows a payment "
              "that already happened, not money still owed. Close it?",
         options=["CLOSE IT — ALREADY PAID", "STILL OWED", "NOT SURE — PULL THE FILE"]),
    dict(id="q23", section="D",
         text="When Klick Klack re-files a bill after it's paid, the new filename starts "
              "with a date — like “06-14-26 TRUST2 CK#13386 - FRONTIER - $147.48 - TL222.pdf”. "
              "Which date should that be — the day you PAID it, or the day the bill ARRIVED?",
         options=[
             dict(value="DAY I PAID IT (the check date)", recommended=True),
             dict(value="DAY THE BILL ARRIVED"),
             dict(value="NOT SURE — LET'S LOOK AT A FOLDER TOGETHER"),
         ],
         note_hint="Your paper archive looks like it uses the payment date (paid files match "
                    "the check register by date), but the app currently uses the arrived date — "
                    "we'll match whichever you say. If it depends (say, bills paid long after "
                    "they arrive), describe it here."),
    # ---- E ----
    dict(id="q14", section="E",
         text="Account number 6600 is used for three different accounts (Bank Fees, Payroll Expense, "
              "Mortgage Principal). OK to give two of them their own numbers?",
         options=["YES — RENUMBER", "NO", "ASK SYED"]),
    dict(id="q15", section="E",
         text="“Postage” appears twice in the account list (numbers 6260 and 6320). "
              "Are these two different things?",
         options=["TWO DIFFERENT THINGS", "SAME THING", "ASK SYED"],
         note_hint="If different — what's each one for?"),
    dict(id="q16", section="E",
         text="Syed's files include two near-identical exports, “LRC Invoices” and "
              "“LRC Invoicess”. Same export saved twice, or two different reports?",
         options=["SAME TWICE", "DIFFERENT", "ASK SYED"]),
    # ---- F ----
    dict(id="q17", section="F",
         text="To bring KLR & LRC into the new system we need exports from QuickBooks "
              "(the checklist is ready). Who can open QuickBooks, and when?",
         options=[],
         note_hint="Who, and roughly when?"),
    # ---- G ----
    dict(id="q18", section="G",
         text="The Shima properties (109 & 207 W Anapamu, 641 W Canon Perdido, 311-313 "
              "W Ortega): your fee went 8% → 10% starting January 2025 — is that right? "
              "(AppFolio's directory still says 8%.)",
         options=["YES — 10% NOW", "NO — STILL 8%"]),
    dict(id="q19", section="G",
         text="Cosinus Properties (1105 Arbolado): management ended January 2025, but bills "
              "kept being paid after and a 1099 went out for 2025. What's the arrangement "
              "now — and where does its old fee income get booked in QuickBooks?",
         options=[],
         note_hint="Current arrangement + where its fees live in QB"),
    dict(id="q20", section="G",
         text="Fee checks and deposits sometimes carry a recurring $50 or $100 item marked "
              "“L.D.” — what is that?",
         options=[],
         note_hint="What L.D. stands for, and which account it belongs to"),
    dict(id="q21", section="G",
         text="140 Evans (T131), late 2025: small fee checks ($73.93, $67.57, $36.81) in "
              "months with no rent showing on the cash flow — fees on what?",
         options=[],
         note_hint="What those fees were charged on"),
]

# Section C's glance table: (workbook/row, entry, was written, now reads)
DATE_FIXES = [
    ("KLR – DEC 2023 (row 55)", "Laverne Shelton-Hill  $609.30", "12-29-2024", "12-29-2023"),
    ("KLR – DEC 2023 (row 56)", "Franchise Tax Board  $40.00", "12-29-2024", "12-29-2023"),
    ("PP – DEC 2023 (row 42)", "Deposit  $295,053.97", "12-18-2024", "12-18-2023"),
    ("TRUST 2 – DEC 2021 (row 47)", "Karen Lacks & Co.  $508.90", "12-15-2022", "12-15-2021"),
    ("TRUST 2 – MAR 2021 (row 55)", "Deposit  $2,000.00", "3-30-2020", "3-30-2021"),
    ("TRUST 2 – JAN 2022 (row 27)", "So.Calif Gas  $16.89", "1-10-2021", "1-10-2022"),
    ("TRUST 2 – MAY 2024 (row 37)", "SoCal Pest Control  $110.00", "5-31-0254", "5-31-2024"),
    ("TRUST 3 – OCT 2020 (row 38)", "Britten Beauvoix owner draw  $3,000", "10-31-2021", "10-31-2020"),
    ("TRUST 1 – MAR 2021 (row 65)", "KL&Co mgmt comm.  $8.97", "3-25-2201", "3-25-2021"),
    ("TRUST 1 – JAN 2022 (row 15)", "So.Calif. Edison  $1,501.04", "1-10-2021", "1-10-2022"),
]
