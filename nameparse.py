r"""
nameparse.py – Karen's filename convention + folder-path priors, app-importable.

The office's 13-year archive encodes most of a document's facts in its FILENAME
(date / entity / method+ref / payee / amount / properties) and the rest in its
FOLDER (paid vs incoming, entity tree, year/month). This module turns both into
structured priors so the live cabinet watcher (cabinet_watch.py) can register a
hand-filed document the way the nightly harvest does — but within seconds, and
cross-checked against the OCR read.

Provenance: the parsing core is a port of harvest_daily_filing.py's parse_name
(T:\_OPS INDEX), which is the battle-tested "compact subset" of the formal
grammar in T:\_OPS INDEX\KAREN_FILENAME_SPEC.md. Every regex below carries a
scar comment from that lineage — do not "simplify" them without reading it.
Spec rules that matter here: §0 never silently drop (unparseable → the caller
holds for a human, never guesses), §2 parse by recognizing tokens anywhere
(fields are optional and order varies), §3.5 amounts are the largest $ value
and never a bare number.

Entity tokens come from the SHARED table T:\_OPS INDEX\entity_tokens.py (code
review P1-7: hand-copied entity lists drift; harvest and reconcile disagreed on
TRUST4/KLR-LRC/BRITT/PERSONAL until they were driven from one file). We load
that file at runtime and fall back to a pinned copy only if it's unreachable.
"""
import datetime
import importlib.util
import os
import re

# ── entity tokens (shared with the data-side scripts) ─────────────────────────

# Fallback copy of _OPS INDEX\entity_tokens.py ENTITY_TOKENS — used only when
# the shared file can't be loaded (e.g. tests on a machine without the share).
# If you're editing this list, you're editing the wrong file: change
# _OPS INDEX\entity_tokens.py and let _load_entity_tokens() pick it up.
_FALLBACK_ENTITY_TOKENS = [
    ("TRUST4",   None),
    ("TRUST3",   "TRUST3"),
    ("TRUST2",   "TRUST2"),
    ("TRUST1",   "TRUST1"),
    ("TRUST",    "TRUST1"),
    ("KLR-LRC",  None),
    ("BRITT",    None),
    ("KLR",      "KLR"),
    ("LRC",      "LRC"),
    ("PERSONAL", None),
]


def _load_entity_tokens():
    r"""ENTITY_TOKENS from the shared _OPS INDEX file, wherever this copy of the
    app lives (server: F:\File-Cabinet\IntakeSystem, desks: T:\IntakeSystem —
    both have _OPS INDEX as a sibling of the app folder)."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(os.path.dirname(here), "_OPS INDEX", "entity_tokens.py"),
        r"F:\File-Cabinet\_OPS INDEX\entity_tokens.py",
    ]
    for path in candidates:
        try:
            if not os.path.isfile(path):
                continue
            spec = importlib.util.spec_from_file_location("entity_tokens", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return list(mod.ENTITY_TOKENS)
        except Exception as e:
            print(f"[NameParse] Could not load shared entity tokens from {path}: {e}")
    return list(_FALLBACK_ENTITY_TOKENS)


ENTITY_TOKENS = _load_entity_tokens()

# ── filename regexes (ported from harvest_daily_filing.py, scars intact) ──────

RE_DATE = re.compile(r"^\s*(\d{1,2})[-._](\d{1,2})[-._]\s?(\d{2,4})(?!\d)")
# Ops-index form "YYYY_MM_DD ..." (6,022 archive files, spec §3.1).
RE_DATE_ISO = re.compile(r"^\s*(\d{4})_(\d{1,2})_(\d{1,2})(?!\d)")
# Bare 8-digit scanner names "MMDDYYYY" (spec §3.1's scanner form) — only when
# the whole stem is the date, so an account number can't read as one.
RE_DATE_SCANNER = re.compile(r"^(\d{2})(\d{2})(20\d{2})$")
# Comma-grouped form must REQUIRE a comma, else plain digits — with the old
# `(?:,\d{3})*` the first branch matched just "123" of "$1230.00" and won the
# alternation (~10% of the archive parsed wrong; Swan CK#3012 test, 2026-07-24).
RE_AMT = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
# Boundaries: "DECK 2026"/"TRUCK 4512"/"PAYCHECK 123" are not check numbers,
# and 8+ digit confirmation numbers must not truncate to a fabricated check #.
RE_CK = re.compile(r"(?<![A-Z])CK\s?#?\s?(\d{3,7})(?!\d)|(?<![A-Z])CHECK\s?#?\s?(\d{3,7})(?!\d)", re.I)
# (?<![A-Z&]) keeps "AT&T 805-123-4567" from minting property "T805-123".
RE_PROP = re.compile(r"\b(?<![A-Z&])(?:TL|TB|T|PP)\s?\d{1,3}(?:-\d{1,4})?\b")
# Real bill-payment methods vs money movements that are NOT vendor bills.
RE_PAY_METHOD = re.compile(r"\bACH\b|ONLINE\s+BILL\s+PAY|\bBILL\s*PAY\b|AUTO\s*-?\s*PAY|\bWIRE\b|\bEFT", re.I)
RE_NONBILL = re.compile(r"\bDEPOSIT(?:S|ED)?\b|OWNER'?S?\s+DRAW|\bTRANSFER(?:S|RED)?\b", re.I)
# "PAID BILLS" as a segment of the DIRECTORY path — the lookbehind rejects
# UNPAID BILLS / PREPAID BILLS.
RE_PAID_TREE = re.compile(r"(?<![A-Z])PAID BILLS")
# Summary/recap sheets must not become one giant fictitious paid bill.
RE_SUMMARY = re.compile(r"\bSUMMARY\b|\bRECAP\b|\bREGISTER\b|\bLIST\b", re.I)

# Vendor-matching stopwords. Superset of database.py's _TWIN_STOP_WORDS (P1-8);
# re-sync if that set changes again.
STOP = {"THE", "INC", "LLC", "CO", "COMPANY", "OF", "AND", "FOR", "CORP", "INTL",
        "INTERNATIONAL", "SERVICES", "SERVICE", "BILL", "BILLS", "PAID",
        "STATEMENT", "INVOICE", "ASSOCIATION", "ASSOCIATES"}

AP_INCOMING = "ACCOUNTS PAYABLE - INCOMING BILLS"
# Month folders come zero-padded ("07. JULY", the AP tree) AND bare ("1.
# JANUARY", the statement trees) — both are Karen's, both must parse.
_MONTH_SEG_RE = re.compile(r"^(0?[1-9]|1[0-2])\.\s+[A-Z]+$")
_YEAR_SEG_RE = re.compile(r"^(19|20)\d{2}$")


def norm_prop(tok: str) -> str:
    t = tok.replace(" ", "").upper()
    m = re.fullmatch(r"TB(\d{1,2})", t)
    if m:
        return "TB" + m.group(1).zfill(3)
    m = re.fullmatch(r"T0*(\d+)", t)
    if m:
        return "T" + m.group(1)
    return t


def vtokens(v) -> set:
    """Vendor-name tokens for overlap matching (same rule as harvest's)."""
    return {w for w in re.sub(r"[^A-Z0-9 ]", " ", (v or "").upper()).split()
            if len(w) > 3 and not w.isdigit() and w not in STOP}


def _leading_date(stem: str):
    """(date, matched_text) from the stem's leading date in any supported form.
    (None, None) when nothing date-shaped leads the stem — ~10% of archive files
    legitimately have no date (spec §1). (None, matched_text) when something
    date-SHAPED leads the name but cannot be a real date: a typo'd year like
    "06-30-256" (record #941 — parsed as the year 256 and filed seventeen
    centuries early) or an impossible month/day. Spec §3.1: such names are a
    person's call, never a guess — parse_name surfaces this as bad_date."""
    m = RE_DATE.match(stem)
    if m:
        mo, dy, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        yr = yr + 2000 if yr < 100 else yr
        try:
            if not 1900 <= yr <= 2099:   # 3-digit / unreal year survives the 20xx clamp
                raise ValueError(yr)
            return datetime.date(yr, mo, dy), m.group(0)
        except ValueError:
            return None, m.group(0)
    m = RE_DATE_ISO.match(stem)
    if m:
        yr, mo, dy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            if not 1900 <= yr <= 2099:
                raise ValueError(yr)
            return datetime.date(yr, mo, dy), m.group(0)
        except ValueError:
            return None, m.group(0)
    m = RE_DATE_SCANNER.match(stem.strip())
    if m:
        mo, dy, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime.date(yr, mo, dy), m.group(0)
        except ValueError:
            # 8 bare digits ending in 20xx is as likely an account number as a
            # scanner date, and scanners don't typo — not flagged.
            return None, None
    return None, None


def parse_name(path: str) -> dict:
    """Structured read of a filename in Karen's convention (tokens recognized
    anywhere, spec §2). Returns the same shape harvest's parse_name does, plus
    `is_summary`."""
    stem = os.path.splitext(os.path.basename(path))[0]
    up = " " + stem.upper() + " "
    d, date_text = _leading_date(stem)
    amts = [float(a.replace(",", "")) for a in RE_AMT.findall(stem)]
    ent = None
    for tok, canon in ENTITY_TOKENS:
        # Both-sided boundary: TRUSTWORTHY must not read as TRUST1, nor
        # BRITTANY as BRITT (which then blanked a real KLR token via the break).
        if re.search(rf"(?<![A-Z0-9]){re.escape(tok)}(?![A-Z0-9])", up):
            ent = canon
            break
    if ent is None and re.search(r"(?<![A-Z])PP(?!\s?\d)(?![A-Z])", up):
        ent = "PP"
    props = [norm_prop(t) for t in RE_PROP.findall(stem)]
    # No explicit entity token: infer from the property prefix (schema §6.4 —
    # T=Trust1, TL=Trust2, TB=Trust3, PP=PP; consistent across 13 years).
    if ent is None and props:
        pfx = re.match(r"(TL|TB|T|PP)", props[0]).group(1)
        ent = {"T": "TRUST1", "TL": "TRUST2", "TB": "TRUST3", "PP": "PP"}[pfx]
    toks = {w for w in re.sub(r"[^A-Z0-9 ]", " ", up).split()
            if len(w) > 3 and not w.isdigit() and w not in STOP}
    # Payee guess: payment markers stripped FIRST so a dash-less name like
    # "SWAN CABINETS CK#3012" yields "SWAN CABINETS", not a per-check vendor.
    payee = None
    body = RE_AMT.sub("", stem)
    body = RE_CK.sub("", body)
    body = RE_PAY_METHOD.sub("", body)
    body = re.sub(r"\s*\(\d+\)\s*$", "", body)   # "(1)" copy suffixes aren't payee
    if date_text:
        body = body.replace(date_text, "", 1)
    for seg in re.split(r"\s-\s|-\s|\s-", body):
        seg = RE_PROP.sub("", seg).strip(" .-")
        seg = re.sub(r"^(TRUST\s?\d?|KLR-LRC|KLR|LRC|BRITT|PERSONAL|PP)\b", "", seg,
                     flags=re.I).strip(" .-")
        if len(re.sub(r"[^A-Za-z]", "", seg)) >= 3:
            payee = seg[:80]
            break
    ck = RE_CK.search(stem)
    check_no = (ck.group(1) or ck.group(2)) if ck else None
    # Only claim a method the app's pay form actually uses: Check / ACH.
    method = "Check" if ck else ("ACH" if re.search(r"\bACH\b", stem, re.I) else None)
    return {"path": path, "stem": stem, "date": d, "amounts": amts, "entity": ent,
            "props": props, "tokens": toks, "payee": payee,
            # A date-shaped prefix that parsed to nothing real (record #941's
            # "06-30-256"): the filer MEANT a date, so nobody may guess one.
            "bad_date": bool(date_text and d is None),
            "check": check_no, "method": method,
            "is_payment": bool(ck or RE_PAY_METHOD.search(stem) or RE_NONBILL.search(stem)),
            "is_nonbill": bool(RE_NONBILL.search(stem)),
            "is_summary": bool(RE_SUMMARY.search(stem))}


# Month-name + standalone-year tokens ("... CASH FLOW JAN 2024", "MARCH 2024
# STATEMENT"). Karen's statement filenames carry a month WORD, not a full date,
# so _leading_date finds nothing there — this is the coarser fallback for
# period-dated documents (archive backfill registration).
RE_MONTH_WORD = re.compile(
    r"(?<![A-Z])(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)"
    r"(?:UARY|RUARY|CH|IL|E|Y|UST|TEMBER|OBER|EMBER)?(?![A-Z])")
RE_YEAR_WORD = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_MONTH_NUM = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
              "JUL": 7, "AUG": 8, "SEP": 9, "SEPT": 9, "OCT": 10, "NOV": 11,
              "DEC": 12}


def month_year(stem: str):
    """(year, month) hints from a filename's tokens — either may be None.
    Coarser than _leading_date and only consulted when that finds nothing:
    a month word anywhere, a standalone 4-digit year anywhere."""
    up = (stem or "").upper()
    m = RE_MONTH_WORD.search(up)
    y = RE_YEAR_WORD.search(up)
    return (int(y.group(1)) if y else None,
            _MONTH_NUM[m.group(1)] if m else None)


# ── folder-path priors ────────────────────────────────────────────────────────

def path_priors(path: str, cfg: dict = None) -> dict:
    """What the document's FOLDER says about it. All keys are priors, not
    verdicts — notably a file under INCOMING BILLS is often but not always a
    bill (statements get filed there too), so doc_type stays the OCR's call.

    Returns {"in_paid_tree", "in_ap_incoming", "year", "month",
             "entity", "category"}."""
    dirname = os.path.dirname(path or "")
    segs = [s.strip() for s in re.split(r"[\\/]+", dirname) if s.strip()]
    up_segs = [s.upper() for s in segs]

    # PAID BILLS must be a real directory segment (harvest matches dirname too);
    # AP_INCOMING likewise — harvest's whole-path test let a FILENAME containing
    # the words register a Pending bill, fixed here by testing segments only.
    in_paid_tree = any(RE_PAID_TREE.search(s) for s in up_segs)
    in_ap_incoming = any(AP_INCOMING in s for s in up_segs)

    year = month = None
    for s in segs:
        st = s.strip()
        if _YEAR_SEG_RE.match(st):
            year = int(st)
        else:
            m = _MONTH_SEG_RE.match(st.upper())
            if m:
                month = int(m.group(1))

    # Entity from folder names, most-specific (deepest) segment first. Karen's
    # trees use both token forms ("KLR PAID BILLS") and spaced names
    # ("TRUST 3", "TRUST  COMBINED..."), so collapse runs of spaces before
    # matching and treat "TRUST <N>" as "TRUST<N>".
    entity = None
    for s in reversed(up_segs):
        flat = re.sub(r"TRUST\s+(\d)", r"TRUST\1", re.sub(r"\s+", " ", s))
        for tok, canon in ENTITY_TOKENS:
            if re.search(rf"(?<![A-Z0-9]){re.escape(tok)}(?![A-Z0-9])", f" {flat} "):
                entity = canon
                break
        else:
            if re.fullmatch(r"PP(\s+PAID.*)?", flat):
                entity = "PP"
        if entity is not None or flat.startswith("PP "):
            entity = entity or "PP"
            break

    # The most meaningful non-structural segment (for notes/holding messages):
    # first segment under a year folder that isn't a month, e.g.
    # "ACCOUNTS PAYABLE - INCOMING BILLS" or "VENDORS - INSURANCE".
    category = None
    for i, s in enumerate(segs):
        if _YEAR_SEG_RE.match(s.strip()) and i + 1 < len(segs):
            nxt = segs[i + 1].strip()
            if not _MONTH_SEG_RE.match(nxt.upper()):
                category = nxt
            break

    return {"in_paid_tree": in_paid_tree, "in_ap_incoming": in_ap_incoming,
            "year": year, "month": month, "entity": entity, "category": category}


# ── the filename's date, checked against the tree it was filed in ─────────────
#
# Report #107 (Karen, 2026-08-07): "07-01-23 PEYTON_SCAPES - JUNE - INV# 621 -
# TL222-16.pdf" was hand-filed into Files\2026\ACCOUNTS PAYABLE - INCOMING
# BILLS\07. JULY, and the "23" in its name was taken at face value. Record
# #1194 was logged as received 2023-07-01: three years past due the moment it
# appeared, at the top of a pay-by-received-date stack, wearing a permanent
# record number (230701-001) that says 2023 forever. Four older records carry
# the same wound — #279, #314, #587 (all nightly harvest) and #941, whose
# filename year was "256".
#
# The rule below is cabinet_watch._archive_period's, generalized: the TREE
# decides the year — someone deliberately put this file in 2026\07. JULY —
# and the FILENAME decides month and day. They only disagree when a year was
# mistyped, and the month folder is the witness: if it agrees with the month in
# the name, the year is the typo and the folder wins. If the months disagree
# too, nothing is corrected (a December bill filed into January's folder is
# ordinary) — the disagreement is reported instead, because saying so is worth
# more than a guess. §0 of the filename spec: never silently drop, never guess.

# Anything outside this window is not a year, it is a typo ("256", "2062").
EARLIEST_FILING_YEAR = 1990


def _same_day_in(year, d):
    """d moved to `year`, keeping month and day (Feb 29 → Feb 28 off-leap)."""
    try:
        return datetime.date(year, d.month, d.day)
    except ValueError:
        return datetime.date(year, d.month, 28)


def received_from_filing(doc_date, priors, today=None):
    """(date, note) — the received date to record for a document filed by hand
    into the year tree, plus a one-line note when the filename's own date could
    not be taken at face value (None when it could, which is the normal case).

    Returns (None, note) only when there is no defensible date at all; the
    caller decides what to do with that (hold for review, or fall back)."""
    today = today or datetime.date.today()
    if doc_date is None:
        return None, None
    tree_year = (priors or {}).get("year")
    tree_month = (priors or {}).get("month")
    named = doc_date.isoformat()
    real_year = EARLIEST_FILING_YEAR <= doc_date.year <= today.year + 1

    if not tree_year:
        if real_year:
            return doc_date, None
        return None, (f"The filename reads {named}, which is not a real date, "
                      f"and it is not filed under a year folder to correct it from")
    if doc_date.year == tree_year:
        return doc_date, None

    where = f"filed under {tree_year}" + (f"\\{tree_month:02d}" if tree_month else "")
    if tree_month and doc_date.month == tree_month:
        fixed = _same_day_in(tree_year, doc_date)
        return fixed, (f"Filename reads {named} but the document is {where} — "
                       f"same month, so the year is a typo: recorded as "
                       f"{fixed.isoformat()}")
    if not real_year:
        fixed = _same_day_in(tree_year, doc_date)
        return fixed, (f"Filename reads {named}, which is not a real year — "
                       f"{where}, so recorded as {fixed.isoformat()}")
    return doc_date, (f"Filename reads {named} but the document is {where} — "
                      f"different month, so it was left as named; check the year")
