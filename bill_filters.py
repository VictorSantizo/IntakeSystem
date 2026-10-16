"""Applying the Bills filter bar to rows already in hand (report #93, Joshua).

The /bills page has two GET forms — the search box and the filter bar — and
they used to ignore each other: searching threw the filters away, and a search
took the page over completely, so "show me the Paid ones, now find Forster"
answered with every Forster bill, paid or not. The template now carries both
halves of the state, and the route runs the search results through this module
so the filters mean the same thing on a search as they do on All Bills.

The predicate deliberately mirrors database.get_all_entries' SQL — equality on
the code columns, a comma-separated status list allowed, and date_from/date_to
bounding `date_received`. Pure functions over dicts, so the behaviour is
testable without a database, and one test pins the two against each other.
"""

# Columns the filter bar can pin, matched exactly (get_all_entries uses `=`).
_EQ_KEYS = ("status", "entity_code", "property_code", "mail_type", "doc_type")


def active(filters):
    """The filters actually set — {} when the bar is untouched. Drives the
    "these filters are still on" note the search results carry."""
    return {k: v for k, v in (filters or {}).items() if (v or "").strip()}


def matches(row, filters):
    """Does one logged record satisfy the filter bar?"""
    for k in _EQ_KEYS:
        want = (filters.get(k) or "").strip()
        if not want:
            continue
        got = (row.get(k) or "").strip()
        if "," in want:      # e.g. "Processed,Paid" — same IN () as the SQL
            if got not in [s.strip() for s in want.split(",") if s.strip()]:
                return False
        elif got != want:
            return False
    got_date = (row.get("date_received") or "").strip()
    if (filters.get("date_from") or "").strip():
        if not got_date or got_date < filters["date_from"].strip():
            return False
    if (filters.get("date_to") or "").strip():
        if not got_date or got_date > filters["date_to"].strip():
            return False
    return True


def apply_filters(rows, filters):
    """Filter a list of logged records. No filters set -> the rows unchanged
    (identity, not a copy-shaped no-op that quietly reorders anything)."""
    if not active(filters):
        return rows
    return [r for r in (rows or []) if matches(r, filters)]
