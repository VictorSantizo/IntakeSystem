"""The document-type vocabulary — one list, used everywhere.

Before this module the same literal list lived in six places (the entry form,
the Intake Log filter, the review form, the classifier's JSON contract, the
Helper's guide, and install.py — since deleted, report #82). Adding a type
meant six edits, and
reports #51/#57/#58/#59 sat queued for a week because of it. Import from here
instead; the app publishes DOC_TYPES to Jinja as a global.

Order matters: it is the order staff see in every dropdown, so the everyday
types come first and Other stays last.

Added 2026-08-05 from Karen's and Joshua's reports:
  Statement (#57) · Insurance (#59) · Tax (#51) · Audit (#58)
"""

# What a person may choose. Existing records may hold a legacy value not in
# this list (nothing is ever rewritten behind a person's back) — the forms cope
# by adding the record's own value as an extra option.
DOC_TYPES = [
    "Bill",
    "Receipt",
    "Income",
    "Bank Statement",
    "Statement",
    "Insurance",
    "Tax",
    "Audit",
    "Legal",
    "Correspondence",
    "Other",
]

# One line each, for the classifier prompt and the Helper. Kept short on
# purpose: these are the distinctions people actually get wrong.
DOC_TYPE_GUIDE = {
    "Bill": "money is owed and a payment is expected",
    "Receipt": "proof a payment already happened",
    "Income": "money coming in (rent, deposits)",
    "Bank Statement": "a bank's own periodic account statement",
    "Statement": "a running account statement from a vendor or service that is "
                 "NOT a bank — a summary of activity, not a demand for payment",
    "Insurance": "a policy, declarations page, certificate of insurance, or a "
                 "renewal/cancellation notice (an insurance PREMIUM invoice is "
                 "a Bill)",
    "Tax": "anything from a taxing authority or the tax preparer — assessments, "
           "returns, notices, 1099s (a property-tax bill with an amount due and "
           "a due date is a Bill)",
    "Audit": "an audit report, audit correspondence, or an examination notice",
    "Legal": "carries real legal weight or needs someone to act (executed "
             "leases, notices, court filings, demand letters)",
    "Correspondence": "a letter or message with no money attached",
    "Other": "anything else, including blank/unexecuted forms",
}


# TAUGHT TO THE AI 2026-08-05 — as a pair, and here is the trap this guards.
#
# The classifier prompt offers every type in DOC_TYPES, and
# app._EMAIL_RELEVANT_DOCTYPES — the allow-list of types that make an EMAIL
# worth keeping (anything outside it is filed aside as junk) — is built from
# the set below. Add a type to the prompt without adding it here and the
# office silently stops seeing that type of email (e.g. every emailed
# insurance policy or tax notice). tests/test_doc_types.py fails the day the
# two halves drift apart.
# Receipt + Correspondence joined 2026-08-10 (Nancy's report, Joshua's call:
# "even though the second attachment is not an invoice, we still want to
# capture it. In this case its a receipt which we definitely want to capture.
# and even if it's just correspondance."). They land in the review queue's
# housekeeping section (the #105 split), so bills stay unburied. What still
# files aside as junk: Other (the marketing/job-application flood the filter
# was built for, 431 filtered to date), unknown AI labels, and regex-fallback
# guesses during an AI outage.
EMAIL_RELEVANT_WHEN_AI_LEARNS_THESE = {"Bill", "Income", "Bank Statement",
                                       "Statement", "Insurance", "Tax",
                                       "Audit", "Legal", "Receipt",
                                       "Correspondence"}


# What "something to pay" means, in ONE place. Two features had grown their own
# answer: the review queue's bills-vs-housekeeping split (#105) tested
# doc_type == "Bill" inline, and the past-due flag (app._scan_is_overdue) tested
# nothing at all — its docstring said "a bill", its code took any document with
# a due date. That is report #111: a QuickBooks payroll-error EMAIL sat in the
# queue wearing a red "past due" badge, because a sentence in it ("you should
# receive this payment by 08/28/2025") had been read as a due date.
#
# A Receipt is proof a payment already happened and a Statement is a summary,
# not a demand — neither is money owed. If a type is ever added here, check
# every caller: this set decides what can be called late.
PAYABLE_DOC_TYPES = {"Bill"}


def normalize(value):
    """The stored form of a doc type someone typed or an AI returned. Matching
    is case-insensitive so 'insurance' and 'INSURANCE' both land on Insurance;
    an unrecognized value passes through untouched rather than being silently
    dropped (a wrong label a person can see and fix beats a blank one)."""
    v = (value or "").strip()
    if not v:
        return ""
    for known in DOC_TYPES:
        if v.lower() == known.lower():
            return known
    return v


def is_payable(value) -> bool:
    """True when this document type is money someone still owes — the only
    kind that can be past due, or belong in the review queue's bills section."""
    return normalize(value) in PAYABLE_DOC_TYPES


def choices_for(current):
    """The options to render in a dropdown, including a record's own legacy
    value so editing a record never silently retypes it."""
    cur = (current or "").strip()
    if cur and cur not in DOC_TYPES:
        return DOC_TYPES + [cur]
    return list(DOC_TYPES)
