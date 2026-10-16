"""
filing.py – Suggests where a confirmed document should be filed, in Karen's
13-year folder system (F:\\File-Cabinet\\Files\\<YEAR>\\<CATEGORY>\\...), and
validates any user-chosen destination before a file is moved there.

Used by the review form's "File as" panel (reports #23/#18): the app proposes a
filename in Karen's naming convention plus a folder in her archive, both
overridable. Suggestions come from two sources, in order:

  1. HISTORY — where this vendor (ideally with the same entity) was filed
     before, with the year and "NN. MONTH" segments of that path bumped to the
     document's received date. Karen's tree repeats its category structure
     every year, so last year's folder maps cleanly onto this year's.
  2. RULE — incoming bills/receipts go to
     <archive>\\<YYYY>\\ACCOUNTS PAYABLE - INCOMING BILLS\\<MM. MONTH>
     (that is where the paper flow drops them today, confirmed by the July
     harvest backfill).

Anything else falls back to the app's own Filed tree (existing behavior,
computed here only for display). The suggestion NEVER moves a file itself —
_finalize_entry in app.py stays the single place that files documents.

June-30 lesson (filed-path drift): destinations are validated against two
allowed roots only, nothing is ever overwritten, and records keep their
file_hash so the hash self-heal can re-link a moved document later.
"""
import os
import re
from datetime import date

from database import get_recent_vendor_filings

# Karen's month folders: "01. JANUARY" ... "12. DECEMBER" (data-derived).
_MONTH_NAMES = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
                "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"]
_MONTH_SEG_RE = re.compile(r"^(0[1-9]|1[0-2])\.\s+[A-Z]+$")
_YEAR_SEG_RE = re.compile(r"^(19|20)\d{2}$")

# Same character set _make_filename strips — a custom filename must obey the
# same Windows rules, and additionally may not try to smuggle in a path.
_ILLEGAL_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')

AP_INCOMING = "ACCOUNTS PAYABLE - INCOMING BILLS"


def _received_date(entry: dict) -> date:
    raw = (entry.get("date_received") or "").strip()
    try:
        return date.fromisoformat(raw)
    except (ValueError, TypeError):
        return date.today()


def month_folder(d: date) -> str:
    return f"{d.month:02d}. {_MONTH_NAMES[d.month - 1]}"


def _is_under(path: str, root: str) -> bool:
    """True if `path` is inside `root` (case-insensitive, separator-safe)."""
    if not (path and root):
        return False
    p = os.path.normcase(os.path.normpath(path))
    r = os.path.normcase(os.path.normpath(root))
    return p == r or p.startswith(r + os.sep)


def _bump_path_to_date(dir_path: str, root: str, d: date) -> str:
    """Rewrite the year segment (and a 'NN. MONTH' segment, if present) of a
    historical folder path to the document's received date. Segments that
    don't match the known patterns are left untouched."""
    rel = os.path.relpath(dir_path, root)
    if rel.startswith(".."):
        return dir_path
    parts = [] if rel == "." else rel.split(os.sep)
    out = []
    for seg in parts:
        s = seg.strip()
        if _YEAR_SEG_RE.match(s):
            out.append(str(d.year))
        elif _MONTH_SEG_RE.match(s):
            out.append(month_folder(d))
        else:
            out.append(seg)
    return os.path.join(root, *out) if out else root


def default_filed_dir(entry: dict, cfg: dict) -> str:
    """The app's legacy destination — Filed\\ENTITY\\PROP\\YYYY. Computed only
    so the panel can show where the document would otherwise go; the actual
    legacy move in _finalize_entry is unchanged."""
    filed_base = cfg.get("filed_folder", "")
    prop = (entry.get("property_code") or "").split(",")[0].strip() or "GENERAL"
    return os.path.join(filed_base, entry.get("entity_code") or "UNKNOWN",
                        prop, date.today().strftime("%Y"))


def _history_dir(entry: dict, archive: str, d: date) -> str | None:
    """Most recent archive folder this vendor's documents were filed into,
    preferring records that also share the entity. Only archive-tree paths
    count — the app's own Filed tree teaches nothing about Karen's system.

    2026-07-27 code review: used to pull get_all_entries() (the WHOLE intake_log)
    and scan it in Python on every single suggestion; the vendor filter (and the
    recency bound this suggestion actually wants) is now pushed into SQL via
    get_recent_vendor_filings, which already returns rows newest-first."""
    vendor = (entry.get("vendor") or "").strip()
    if not vendor:
        return None
    entity = (entry.get("entity_code") or "").strip()
    same_entity_dir = any_entity_dir = None
    for fp, entity_code in get_recent_vendor_filings(vendor):
        if not _is_under(fp, archive):
            continue
        if entity and entity_code == entity:
            same_entity_dir = same_entity_dir or os.path.dirname(fp)
            break                       # newest same-entity match wins outright
        any_entity_dir = any_entity_dir or os.path.dirname(fp)
    winner = same_entity_dir or any_entity_dir
    return _bump_path_to_date(winner, archive, d) if winner else None


def suggest_filed_dir(entry: dict, cfg: dict) -> dict:
    """Ranked destination suggestion for a document about to be confirmed.

    Returns {"dir": str, "source": "history"|"rule"|"default",
             "candidates": [str, ...]} — candidates are the distinct options
    for the folder picker, best first."""
    archive = (cfg.get("archive_folder") or "").strip()
    d = _received_date(entry)
    fallback = default_filed_dir(entry, cfg)
    if not archive:
        return {"dir": fallback, "source": "default", "candidates": [fallback]}

    history = _history_dir(entry, archive, d)
    rule = None
    if (entry.get("doc_type") or "") in ("Bill", "Receipt"):
        rule = os.path.join(archive, str(d.year), AP_INCOMING, month_folder(d))

    candidates, seen = [], set()
    for c in (history, rule, fallback):
        if c and os.path.normcase(c) not in seen:
            seen.add(os.path.normcase(c))
            candidates.append(c)
    best = candidates[0]
    source = "history" if best == history else ("rule" if best == rule else "default")
    return {"dir": best, "source": source, "candidates": candidates}


def cabinet_root(cfg: dict) -> str:
    """The whole file server share, as the server sees it. Anywhere under here
    is a legal destination (Joshua, 2026-07-29: 'move any file to any folder in
    the file server') EXCEPT the app's own machinery (cabinet_excludes)."""
    return (cfg.get("cabinet_root") or cfg.get("server_root")
            or "F:\\File-Cabinet").rstrip("\\/")


def cabinet_excludes(cfg: dict) -> list:
    """Subtrees of the cabinet root that documents must never be filed into by
    hand: the app's own folder (watched inboxes, staging, Scan Archive, code)
    and the data-side workbench. The app's Filed tree lives INSIDE IntakeSystem
    but stays allowed — it is checked as an allowed root before these apply."""
    root = cabinet_root(cfg)
    ex = cfg.get("cabinet_exclude")
    if not ex:
        ex = [os.path.join(root, "IntakeSystem"), os.path.join(root, "_OPS INDEX")]
    return ex


def display_path(path: str, cfg: dict) -> str:
    r"""User-facing form of a server-local path. House rule (reports #33/#36):
    staff see full UNC (\\LACKSSERVER2021\File-Cabinet\...) — drive letters
    differ per desk and F:\ exists only on the server itself. Paths outside the
    server root (temp dirs in tests, legacy oddities) pass through unchanged."""
    p = os.path.normpath(path) if path else ""
    root = (cfg.get("server_root") or "F:\\File-Cabinet").rstrip("\\/")
    unc = (cfg.get("server_root_unc") or "\\\\LACKSSERVER2021\\File-Cabinet").rstrip("\\/")
    nc_p, nc_root = os.path.normcase(p), os.path.normcase(root)
    if nc_p == nc_root or nc_p.startswith(nc_root + os.sep):
        return unc + p[len(root):]
    return path or ""


def validate_filed_dir(dir_str: str, cfg: dict) -> str | None:
    """Normalize a user-chosen destination folder; None if it isn't a legal
    destination: Karen's archive, the app's Filed tree, or (2026-07-29,
    move-anywhere) anywhere else under the cabinet root except the excluded
    app/system subtrees. The caller falls back to the legacy destination and
    says so in the activity log — a typo must never file a document into an
    arbitrary location outside the file server."""
    d = (dir_str or "").strip().strip('"')
    if not d or not os.path.isabs(d):
        return None
    norm = os.path.normpath(d)
    for root in (cfg.get("archive_folder", ""), cfg.get("filed_folder", "")):
        if root and _is_under(norm, root):
            return norm
    croot = cabinet_root(cfg)
    if croot and _is_under(norm, croot):
        if any(_is_under(norm, ex) for ex in cabinet_excludes(cfg)):
            return None
        return norm
    return None


def resolve_client_dir(dir_str: str, cfg: dict) -> str | None:
    """validate_filed_dir, extended for folders typed on OTHER machines.

    Every desk reaches the file server under a different drive letter (T:, Z:,
    even F:) or as a UNC share, so a folder pasted from a workstation's
    Explorer almost never starts with the server's own root — and used to be
    rejected as 'outside the archive' (report #36). If the path doesn't
    validate as-is, re-root it: find the path segment matching an allowed
    root's last folder name, graft the remainder onto that root, and accept
    only if the resulting folder actually exists server-side (a re-rooted
    guess must never invent a directory tree)."""
    ok = validate_filed_dir(dir_str, cfg)
    if ok:
        return ok
    d = (dir_str or "").strip().strip('"')
    if not d:
        return None
    segs = [s for s in re.split(r"[\\/]+", d) if s and not s.endswith(":")]
    for root in (cfg.get("archive_folder", ""), cfg.get("filed_folder", ""),
                 cabinet_root(cfg)):
        if not root:
            continue
        base = os.path.basename(os.path.normpath(root)).lower()
        for i, seg in enumerate(segs):
            if seg.lower() != base:
                continue
            cand = os.path.join(root, *segs[i + 1:]) if segs[i + 1:] else root
            # The re-rooted guess must exist server-side AND still be a legal
            # destination (the cabinet excludes apply to re-rooted paths too).
            if os.path.isdir(cand) and validate_filed_dir(cand, cfg):
                return os.path.normpath(cand)
    return None


def resolve_client_file(path_str: str, cfg: dict) -> str | None:
    """A FILE typed, pasted or picked from any desk, resolved to its real path
    on the server — the file counterpart of resolve_client_dir, and the only
    way the New Entry form's "start from a document" pick (#50) is allowed to
    reach a file. The folder must validate (so the drive-letter re-rooting and
    the cabinet excludes both apply) and the file itself must exist: a hidden
    form field is not a permission, and a path outside the file server has no
    meaning to the server anyway."""
    p = (path_str or "").strip().strip('"')
    if not p:
        return None
    folder, name = os.path.split(p.rstrip("\\/"))
    if not folder or not name:
        return None
    resolved_dir = resolve_client_dir(folder, cfg)
    if not resolved_dir:
        return None
    cand = os.path.join(resolved_dir, name)
    return os.path.normpath(cand) if os.path.isfile(cand) else None


def scan_archive_dir(cfg: dict, user: str) -> str | None:
    r"""Per-person Scan Archive folder — confirm-time copies of physical-mail
    documents, mirrored down to that person's Desktop\SCANS by the workstation
    connector (Nancy's historical-reference ask, 2026-07-29). Lives beside the
    Filed tree inside IntakeSystem, which keeps it deliberately OUTSIDE the
    cabinet watcher's pickup roots and the move-destination policy."""
    filed = (cfg.get("filed_folder") or "").strip()
    u = _ILLEGAL_NAME_CHARS.sub("", (user or "").strip())
    if not (filed and u):
        return None
    return os.path.join(os.path.dirname(filed), "Scan Archive", u)


def sanitize_filename(name: str) -> str:
    """A custom filename from the form, reduced to a safe base name (no
    directories, no illegal characters, no extension). Empty result means
    'nothing usable' — the caller then uses the convention name."""
    base = os.path.basename((name or "").strip())
    base = os.path.splitext(base)[0] if re.search(r"\.[A-Za-z0-9]{1,5}$", base) else base
    base = _ILLEGAL_NAME_CHARS.sub("", base)
    base = re.sub(r"\s+", " ", base).strip(" .")
    return base[:150]
