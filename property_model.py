"""
property_model.py — Relational core for the property-management domain.

The first slice of the app's real data model (see DESIGN.md §"Data model"): entities,
properties, units, and vendors as related tables, shaped to map cleanly onto ERPNext
later (each table carries a nullable erpnext_* link column). These tables are ADDITIVE:
they live alongside config.json, which stays the app's runtime source of truth until
readers are migrated over table-by-table. Importing or initializing this module does
NOT change any existing behavior.

Seeding is idempotent (INSERT OR IGNORE by natural key), so it can run on every startup
and will never duplicate rows or overwrite Karen's corrections:
  - entities / properties  ← config.json
  - properties.unit_count  ← PROXY_UNIT_COUNTS (a one-time filename-derived best guess)
  - vendors                ← distinct payees already in the intake log
"""
import json
import os
from database import get_conn

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# One-time best-guess unit counts, derived 2026-06-30 from the distinct "<root>-<unit>"
# codes mined across 175k filenames in the ops index (T:\_OPS INDEX\ops_index.sqlite).
# SEED ONLY — it over-counts where units were renumbered over the years, and any property
# not listed defaults to 1 (single-family / office / personal residence). Karen reviews
# and corrects these in the Properties & Units editor; the rent rolls are the authoritative
# source if exact current counts are ever needed.
PROXY_UNIT_COUNTS = {
    "TL222": 42, "T27": 34, "TB201": 20, "T35": 17, "PP16": 15, "PP18": 14,
    "PP24": 12, "PP40": 4, "T43": 3, "T42": 3, "T40-641": 3, "T40-832": 3,
    "PP37": 3, "PP51": 2, "PP32": 2, "TB002": 2,
}

# Real unit LABELS, mined 2026-08-03 from the same 175k-filename ops index that produced
# PROXY_UNIT_COUNTS above -- but resolved down to actual units instead of just counted.
# The archive has encoded units as "<PROPCODE>-<UNIT>" in filenames for 13 years
# (e.g. "06-24-26 A-1 APPLIANCE REPAIR, INC. - INV# 115117 - TL222-5.pdf"), so the unit
# vocabulary already existed as a naming convention; this is it promoted to data.
#
# Baked in as a literal on purpose, exactly as PROXY_UNIT_COUNTS was: the ops index lives
# on a workstation share the app server can't count on, so seeding must not read it at
# runtime. Re-mine with scratchpad/mine_units.py if the archive is ever re-scanned.
#
# The value is the number of archive filings that mention that unit -- kept because it
# separates the real units from the residue at a glance. Two kinds of junk shared this
# field and were filtered out: 4+ digit account/check numbers (TL222-0573, T20-1212) and
# zero-padded spellings of a unit that also appears bare (T27-05 vs T27-5). What survives
# is mostly a clean contiguous run (TL222 1-21, T27 1-30), which is the tell that the
# filter is cutting in the right place. A handful of one-sighting stragglers still get
# through (T27-43, TB201-217, PP16-105) -- seeded anyway rather than guessed away, with
# their hit count shown in the Units editor so Karen can prune them on sight.
#
# NOTE: these counts contradict PROXY_UNIT_COUNTS, which counted junk tokens as units --
# TL222 is 21 units, not 42; T27 is 30, not 34; TB201 18, not 20. PROXY still drives
# properties.unit_count (and therefore how shared bills split across entities) until
# that's deliberately reconciled -- see recompute_unit_counts_from_units().
SEED_UNITS = {
    "PP16": {"2": 1, "5": 1, "9": 1, "12": 2, "18": 11, "19": 10, "24": 1, "42": 2, "44": 4, "47": 4, "49": 11, "58": 57, "105": 2},
    "PP18": {"1": 10, "2": 24, "3": 26, "4": 39, "5": 33, "6": 19, "7": 49, "8": 32, "9": 61, "10": 19, "11": 33, "12": 41, "19": 1, "20": 1},
    "PP24": {"1": 40, "2": 24, "3": 1, "4": 37, "5": 57, "6": 38, "7": 2, "8": 14, "9": 11},
    "PP32": {"102": 1},
    "PP37": {"102": 6, "103": 16, "104": 3},
    "PP40": {"41": 87, "42": 357, "44": 1, "49": 34},
    "PP51": {"57": 2, "58": 173},
    "PP53": {"58": 1},
    "PP59": {"60": 5},
    "T27": {"1": 57, "2": 114, "3": 82, "4": 51, "5": 160, "6": 100, "7": 82, "8": 174, "9": 91, "10": 169, "11": 100, "12": 52, "13": 45, "14": 91, "15": 167, "16": 33, "17": 88, "18": 71, "19": 53, "20": 66, "21": 26, "22": 27, "23": 155, "24": 80, "25": 26, "26": 45, "27": 27, "28": 51, "29": 52, "30": 58, "43": 1},
    "T35": {"1": 110, "2": 112, "3": 117, "4": 121, "5": 115, "6": 86, "7": 149, "8": 117, "9": 158, "10": 79, "11": 49, "12": 93, "20": 2, "29": 3, "42": 2, "43": 10, "44": 2},
    "T42": {"11": 1, "311": 94, "313": 50},
    "T43": {"1": 38, "12": 2, "207": 5},
    "TB002": {"11": 4},
    "TB201": {"1": 93, "2": 19, "3": 67, "4": 95, "5": 46, "6": 33, "7": 99, "8": 43, "9": 35, "10": 107, "11": 62, "12": 72, "13": 65, "14": 40, "15": 198, "16": 32, "17": 64, "18": 48, "217": 3},
    "TL222": {"1": 37, "2": 79, "3": 41, "4": 84, "5": 111, "6": 77, "7": 40, "8": 47, "9": 38, "10": 41, "11": 22, "12": 80, "13": 54, "14": 23, "15": 39, "16": 104, "17": 144, "18": 54, "19": 108, "20": 91, "21": 69},
}


def unit_sort_key(label: str):
    """Order units the way a person reads a mailbox bank: 1, 2, ... 10, 21, then 'A'.
    Plain string sort puts "10" before "2", which looks broken on a 30-unit property."""
    s = (label or "").strip()
    return (0, int(s), "") if s.isdigit() else (1, 0, s.upper())


def _entity_kind(code: str) -> str:
    """Classify an entity code into a coarse kind (trust / personal / office / llc)."""
    c = (code or "").upper()
    if c.startswith("TRUST"):
        return "trust"
    if c.startswith("PERS"):
        return "personal"
    if c.endswith("-OF") or "OFFICE" in c:
        return "office"
    return "llc"


def init_property_schema():
    """Create the property-model tables if they don't exist. Additive + idempotent.
    Every table carries a nullable erpnext_* column so records can later be linked to
    ERPNext documents without a schema change (see DESIGN.md §"ERPNext boundary")."""
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                code            TEXT UNIQUE NOT NULL,
                name            TEXT,
                kind            TEXT,                 -- trust | llc | personal | office
                erpnext_company TEXT,                 -- future ERPNext link (nullable)
                notes           TEXT,
                active          INTEGER DEFAULT 1,
                created_at      TEXT DEFAULT (datetime('now','localtime'))
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS properties (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                code          TEXT UNIQUE NOT NULL,
                address       TEXT,
                entity_id     INTEGER REFERENCES entities(id),
                cluster       TEXT,                   -- grouping (e.g. Oak Ave, Offices)
                unit_count    INTEGER DEFAULT 1,      -- #2 split weight; = COUNT(units) once units exist
                property_type TEXT,                   -- single_family | multi | office | commercial
                status        TEXT DEFAULT 'active',  -- active | inactive
                erpnext_asset TEXT,
                notes         TEXT,
                created_at    TEXT DEFAULT (datetime('now','localtime'))
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS units (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                unit_code     TEXT UNIQUE,            -- e.g. T027-12
                property_id   INTEGER REFERENCES properties(id),
                label         TEXT,                   -- apt/unit number or street #
                beds          REAL,
                baths         REAL,
                sqft          INTEGER,
                status        TEXT DEFAULT 'unknown', -- occupied | vacant | unknown
                erpnext_asset TEXT,
                notes         TEXT,
                created_at    TEXT DEFAULT (datetime('now','localtime'))
            )""")
        # Additive upgrade for deployments that already created `units` without these
        # (the table shipped empty, so this only ever adds columns -- never migrates rows).
        for col, typedef in [
            ("archive_hits", "INTEGER DEFAULT 0"),  # filings mentioning this unit; 0 = staff-added
            ("source",       "TEXT"),               # archive | staff
        ]:
            try:
                conn.execute(f"ALTER TABLE units ADD COLUMN {col} {typedef}")
            except Exception:
                pass  # column already exists
        conn.execute("CREATE INDEX IF NOT EXISTS idx_units_property ON units(property_id)")
        # Which units a record is about (report #20). A link table rather than a
        # unit_id column because one invoice routinely covers several units -- a
        # roofer bills 3, 7 and 12 on one page -- mirroring how property_code
        # already lets a record name more than one property.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entry_units (
                entry_id INTEGER NOT NULL,
                unit_id  INTEGER NOT NULL REFERENCES units(id),
                PRIMARY KEY (entry_id, unit_id)
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entry_units_unit ON entry_units(unit_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vendors (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                name              TEXT UNIQUE NOT NULL,   -- canonical spelling
                category          TEXT,                   -- utility|maintenance|insurance|legal|card|other
                default_bill_type TEXT,
                erpnext_supplier  TEXT,
                notes             TEXT,
                created_at        TEXT DEFAULT (datetime('now','localtime'))
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vendor_aliases (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                alias     TEXT UNIQUE NOT NULL,           -- one of the 3,493 archive spellings
                vendor_id INTEGER REFERENCES vendors(id)
            )""")
        conn.commit()


def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def seed_property_model():
    """Populate entities / properties / vendors from what we already have. Idempotent:
    INSERT OR IGNORE by natural key, so re-running never duplicates rows or clobbers
    Karen's edits (e.g. corrected unit counts)."""
    cfg = _load_config()
    with get_conn() as conn:
        # Entities from config.
        for e in cfg.get("entities", []):
            conn.execute(
                "INSERT OR IGNORE INTO entities (code, name, kind) VALUES (?,?,?)",
                (e["code"], e.get("name", ""), _entity_kind(e["code"])))
        ent_id = {r["code"]: r["id"] for r in conn.execute("SELECT id, code FROM entities")}

        # Properties from config; unit_count seeded from the proxy, status from the note.
        for p in cfg.get("properties", []):
            note = p.get("note") or ""
            status = "inactive" if "inactive" in note.lower() else "active"
            conn.execute(
                """INSERT OR IGNORE INTO properties
                       (code, address, entity_id, unit_count, status, notes)
                   VALUES (?,?,?,?,?,?)""",
                (p["code"], p.get("address", ""), ent_id.get(p.get("entity_code")),
                 PROXY_UNIT_COUNTS.get(p["code"], 1), status, note))

        # Units from the archive's 13-year "<PROPCODE>-<UNIT>" filename convention.
        prop_id = {r["code"]: r["id"] for r in conn.execute("SELECT id, code FROM properties")}
        for pcode, units in SEED_UNITS.items():
            pid = prop_id.get(pcode)
            if not pid:
                continue  # property was renamed or removed from config; skip quietly
            for label, hits in units.items():
                conn.execute(
                    """INSERT OR IGNORE INTO units
                           (unit_code, property_id, label, archive_hits, source)
                       VALUES (?,?,?,?,'archive')""",
                    (f"{pcode}-{label}", pid, label, hits))

        # Vendors from payees the intake log already knows (best-effort dedup by name).
        seen = {r["name"].lower() for r in conn.execute("SELECT name FROM vendors")}
        for r in conn.execute(
                "SELECT DISTINCT vendor FROM intake_log WHERE vendor IS NOT NULL AND vendor!=''"):
            v = (r["vendor"] or "").strip()
            if v and v.lower() not in seen:
                conn.execute("INSERT OR IGNORE INTO vendors (name) VALUES (?)", (v,))
                seen.add(v.lower())
        conn.commit()

    # Now that units exist, correct the unit counts they contradict (never Karen's own).
    reconcile_unit_counts()


# ── Sync from config edits ─────────────────────────────────────────────────────
# Settings edits entities/properties in config.json; these keep the relational
# tables in step so entity_unit_weights (payment splits) never computes from
# stale codes. Seeding is INSERT OR IGNORE and deliberately never updates, so
# without these the tables drifted apart the first time anything was renamed.
# All callers treat sync as best-effort — a sync failure must never block the
# config change itself.

def sync_entity_upsert(code: str, name: str) -> None:
    with get_conn() as conn:
        conn.execute("""INSERT INTO entities (code, name, kind) VALUES (?,?,?)
                        ON CONFLICT(code) DO UPDATE SET name=excluded.name, active=1""",
                     (code, name or "", _entity_kind(code)))
        conn.commit()

def sync_entity_rename(old_code: str, new_code: str, name: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE entities SET code=?, name=?, kind=? WHERE code=?",
                     (new_code, name or "", _entity_kind(new_code), old_code))
        conn.commit()

def sync_entity_deactivate(code: str) -> None:
    """Config deletion only happens when no properties reference the entity;
    the relational row is kept (history) but marked inactive."""
    with get_conn() as conn:
        conn.execute("UPDATE entities SET active=0 WHERE code=?", (code,))
        conn.commit()

def sync_property_upsert(code: str, address: str, entity_code: str, note: str = "") -> None:
    """Insert or update a property from its config values. unit_count is NOT
    touched on update — that's Karen's curated number from the Units editor."""
    with get_conn() as conn:
        ent = conn.execute("SELECT id FROM entities WHERE code=?",
                           (entity_code or "",)).fetchone()
        status = "inactive" if "inactive" in (note or "").lower() else "active"
        conn.execute("""INSERT INTO properties (code, address, entity_id, unit_count, status, notes)
                        VALUES (?,?,?,1,?,?)
                        ON CONFLICT(code) DO UPDATE SET
                            address=excluded.address, entity_id=excluded.entity_id,
                            status=excluded.status, notes=excluded.notes""",
                     (code, address or "", ent["id"] if ent else None, status, note or ""))
        conn.commit()

def property_reference_count(code: str) -> int:
    """How many live records still point at property `code` -- the reference-integrity
    check delete_property (app.py) was missing (2026-07-25 code review, P1-10:
    "delete_property has no reference-integrity check, unlike its sibling
    delete_entity"; deleting a still-referenced property silently orphans historical
    bills' property_code, breaking cabinet drawers, unit-weight splitting, dropdowns).

    property_code is a comma-separated string of codes (e.g. "T27, PP16"), so a plain
    substring LIKE match would wrongly count code "T2" as a hit inside "T27". Matched
    the same way rename_property_code (database.py) already matches this identical
    field shape: a cheap LIKE '%code%' to find candidates, then split-on-comma +
    exact-token comparison to confirm.

    Deliberately does NOT count vendor_hints.property_codes / account_hints.property_codes
    (best-effort learned autofill caches that re-learn on the next confirm, not
    historical records) or expected_bills.property_code (the recurring-bill radar,
    forward-looking, not history) -- rename_property_code doesn't cascade into
    either of those two for the same reason (see review Priority 2 notes), so this
    guard mirrors that same scope rather than inventing a stricter one."""
    with get_conn() as conn:
        total = 0
        rows = conn.execute(
            "SELECT property_code AS v FROM intake_log WHERE property_code LIKE ?",
            (f"%{code}%",)).fetchall()
        for r in rows:
            tokens = [p.strip() for p in (r["v"] or "").split(",") if p.strip()]
            if code in tokens:
                total += 1
        # Structural children in this module's own schema (units.property_id).
        # The `units` table is currently unpopulated everywhere in the app (see
        # DESIGN.md / P3 review note), so this is always 0 today -- kept for when
        # it isn't.
        total += conn.execute(
            """SELECT COUNT(*) AS n FROM units u JOIN properties p ON p.id = u.property_id
               WHERE p.code = ?""", (code,)).fetchone()["n"]
        return total


def sync_property_deactivate(code: str) -> None:
    """Mirrors sync_entity_deactivate's intent: a property that's still referenced
    should not silently go inactive. 2026-07-25 code review, P1-10. The
    delete_property route (routes_settings.py) now calls property_reference_count
    itself and refuses *before* touching config.json, mirroring delete_entity's
    inline pre-check -- so in the normal delete flow this raise never fires. It
    stays as the relational layer's own backstop for any other caller (the route
    reaches us through _sync_model, which treats sync as best-effort and swallows
    exceptions), keeping the properties row (and anything reading it: unit-weight
    splitting, the Properties & Units admin page) from being marked inactive out
    from under records that still reference it."""
    if property_reference_count(code):
        raise ValueError(
            f"Can't deactivate property {code}: still referenced by existing records.")
    with get_conn() as conn:
        conn.execute("UPDATE properties SET status='inactive' WHERE code=?", (code,))
        conn.commit()


# ── Read / update helpers (for the Properties & Units admin page and the #2 split) ──

def get_entities() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM entities ORDER BY code")]


def get_properties() -> list[dict]:
    """Properties joined to their owning entity, for the admin review page."""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT p.*, e.code AS entity_code, e.name AS entity_name
            FROM properties p LEFT JOIN entities e ON e.id = p.entity_id
            ORDER BY p.entity_id, p.code""")]


def update_property_units(code: str, unit_count) -> None:
    """Karen's correction of a seeded unit count."""
    try:
        n = max(0, int(unit_count))
    except (TypeError, ValueError):
        return
    with get_conn() as conn:
        conn.execute("UPDATE properties SET unit_count=? WHERE code=?", (n, code))
        conn.commit()


# ── Units ──────────────────────────────────────────────────────────────────────

def get_units_for_property(code: str) -> list[dict]:
    """This property's units, in mailbox order. Empty list for a property that has
    none -- single-family homes and offices legitimately never get unit rows."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT u.* FROM units u JOIN properties p ON p.id = u.property_id
            WHERE p.code = ?""", (code,)).fetchall()
    return sorted((dict(r) for r in rows), key=lambda u: unit_sort_key(u["label"]))


def get_units_by_property() -> dict:
    """{property_code: [unit, ...]} for the whole portfolio in one query -- the Units
    editor renders every property, so per-property lookups would be 40 round trips."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT u.*, p.code AS property_code
            FROM units u JOIN properties p ON p.id = u.property_id""").fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["property_code"], []).append(dict(r))
    for units in out.values():
        units.sort(key=lambda u: unit_sort_key(u["label"]))
    return out


def _recount_property(conn, property_id: int) -> None:
    """Keep properties.unit_count in step with the actual unit rows.

    Once a property has units, its count is DERIVED -- unit_count exists to weight how a
    shared bill splits across entities, and a hand-typed number that disagrees with the
    unit list would silently skew that money math. Properties with no units (single-family
    homes, offices) keep their manually-entered number, which is why this only ever writes
    when there's something to count."""
    n = conn.execute("SELECT COUNT(*) AS n FROM units WHERE property_id=?",
                     (property_id,)).fetchone()["n"]
    if n:
        conn.execute("UPDATE properties SET unit_count=? WHERE id=?", (n, property_id))


def add_unit(property_code: str, label: str) -> bool:
    """Add a unit to a property. Returns False if the label is empty or already taken."""
    label = (label or "").strip()
    if not label:
        return False
    with get_conn() as conn:
        prop = conn.execute("SELECT id FROM properties WHERE code=?",
                            (property_code,)).fetchone()
        if not prop:
            return False
        cur = conn.execute(
            """INSERT OR IGNORE INTO units (unit_code, property_id, label, archive_hits, source)
               VALUES (?,?,?,0,'staff')""",
            (f"{property_code}-{label}", prop["id"], label))
        if not cur.rowcount:
            return False          # unit_code already exists
        _recount_property(conn, prop["id"])
        conn.commit()
        return True


def rename_unit(unit_id: int, label: str) -> bool:
    """Relabel a unit, keeping unit_code (the natural key) in step with its property."""
    label = (label or "").strip()
    if not label:
        return False
    with get_conn() as conn:
        row = conn.execute("""SELECT u.id, u.property_id, p.code AS pcode
                              FROM units u JOIN properties p ON p.id = u.property_id
                              WHERE u.id=?""", (unit_id,)).fetchone()
        if not row:
            return False
        try:
            conn.execute("UPDATE units SET unit_code=?, label=? WHERE id=?",
                         (f"{row['pcode']}-{label}", label, unit_id))
        except Exception:
            return False          # collides with a unit that already has that label
        conn.commit()
        return True


def unit_reference_count(unit_id: int) -> int:
    """How many records still name this unit -- the same reference-integrity check
    property_reference_count does for properties (2026-07-25 review, P1-10)."""
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM entry_units WHERE unit_id=?",
                            (unit_id,)).fetchone()["n"]


def delete_unit(unit_id: int) -> bool:
    """Remove a unit. Used to prune the single-sighting residue the archive mining let
    through; callers check unit_reference_count first so a unit still on a bill stays put."""
    with get_conn() as conn:
        row = conn.execute("SELECT property_id FROM units WHERE id=?", (unit_id,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM units WHERE id=?", (unit_id,))
        _recount_property(conn, row["property_id"])
        conn.commit()
        return True


def reconcile_unit_counts() -> int:
    """Bring unit_count in line with the seeded units, WITHOUT clobbering Karen's edits.

    PROXY_UNIT_COUNTS counted junk tokens as units, so the seeded numbers are inflated
    (TL222 said 42; it has 21) -- and unit_count weights how a shared bill splits across
    entities, so those numbers were quietly skewing real allocations. This corrects them
    from the mined unit list.

    A property is only rewritten while its count is still untouched -- i.e. still equal to
    the proxy value it was seeded with. The moment Karen types her own number the property
    is hers and this leaves it alone, which is the same never-overwrite-a-correction rule
    the rest of the seeding follows. Returns how many properties were corrected."""
    fixed = 0
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT p.id, p.code, p.unit_count, COUNT(u.id) AS actual
            FROM properties p JOIN units u ON u.property_id = p.id
            GROUP BY p.id, p.code, p.unit_count""").fetchall()
        for r in rows:
            seeded = PROXY_UNIT_COUNTS.get(r["code"], 1)
            if r["unit_count"] == seeded and r["actual"] != r["unit_count"]:
                conn.execute("UPDATE properties SET unit_count=? WHERE id=?",
                             (r["actual"], r["id"]))
                fixed += 1
        conn.commit()
    return fixed


def entity_unit_weights(property_codes) -> dict:
    """For the #2 split: given the property codes on a bill, return
    {entity_code: summed unit_count} — the proportional weight per entity, so a bill
    across a 42-unit and a 2-unit property splits ~95/5, not 50/50."""
    codes = [c for c in ((code or "").strip() for code in property_codes) if c]
    if not codes:
        return {}
    weights: dict = {}
    with get_conn() as conn:
        placeholders = ",".join("?" * len(codes))
        rows = conn.execute(f"""
            SELECT e.code AS ent, p.unit_count AS units
            FROM properties p LEFT JOIN entities e ON e.id = p.entity_id
            WHERE p.code IN ({placeholders})""", codes).fetchall()
        for r in rows:
            if r["ent"]:
                weights[r["ent"]] = weights.get(r["ent"], 0) + (r["units"] or 1)
    return weights
