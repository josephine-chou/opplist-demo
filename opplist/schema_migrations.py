"""Schema upgrades for databases created by an earlier version.

schema.sql always describes the current shape. A database that was created
before a change is brought forward here, one step at a time, and
setting.schema_version records how far it has come. Upgrades run inside a
transaction and never drop a table or a column: business data is not deleted.
"""

from __future__ import annotations

import json
import sqlite3

from . import branding

CURRENT_VERSION = 58

# --------------------------------------------------------------------- v2 --
# v_enquiry gained exact / approximate date pairs. quoted_on and closed_on now
# only see events recorded with approx = 0, so an export can no longer hand a
# close date to a case that never had one; quoted_on_any and closed_on_any
# keep the old, inclusive behaviour for screens and tracking rules.
_V2_VIEW = """
DROP VIEW IF EXISTS v_enquiry;
CREATE VIEW v_enquiry AS
SELECT e.*,
       substr(e.received_on, 1, 7)                                   AS month,
       rp.key                                                        AS requester_key,
       rp.type                                                       AS requester_type,
       rp.country                                                    AS requester_country,
       rc.region                                                     AS requester_region,
       CASE rp.type WHEN 'Office' THEN 'Office' ELSE 'Agent' END AS source_type_legacy,
       oc.region                                                     AS origin_region,
       dc.region                                                     AS dest_region,
       CASE WHEN oc.region = 'Latin America' OR dc.region = 'Latin America' OR rc.region = 'Latin America' THEN 'Y' ELSE 'N' END AS latam,
       CASE
         WHEN e.origin_country IS NULL OR e.dest_country IS NULL THEN NULL
         WHEN e.origin_country = 'Unknown' OR e.dest_country = 'Unknown' THEN 'To Confirm'
         WHEN e.origin_country = 'USA' AND e.dest_country = 'USA' THEN 'US Domestic'
         WHEN e.origin_country = 'USA' THEN 'US Export'
         WHEN e.dest_country = 'USA' THEN 'US Import'
         ELSE 'Cross-trade' END                                      AS direction,
       CASE e.container_type
         WHEN '20GP' THEN 1 WHEN '20OT' THEN 1 WHEN '20FR' THEN 1 WHEN '20RF' THEN 1
         WHEN '40GP' THEN 2 WHEN '40HQ' THEN 2 WHEN '45HQ' THEN 2 WHEN '40OT' THEN 2 WHEN '40FR' THEN 2 WHEN '40RF' THEN 2 WHEN '40NOR' THEN 2
         ELSE NULL END * e.container_qty                             AS teu,
       (SELECT MIN(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.status = 'Quoted' AND s.approx = 0)                    AS quoted_on,
       (SELECT MIN(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.status IN ('Won','Lost','Dropped') AND s.approx = 0)   AS closed_on,
       (SELECT MIN(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.status = 'Quoted')                                     AS quoted_on_any,
       (SELECT MIN(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.status IN ('Won','Lost','Dropped'))                    AS closed_on_any,
       (SELECT MAX(at) FROM status_event s WHERE s.enquiry_id = e.id)                                                             AS last_event_on,
       COALESCE(e.group_id, e.id)                                    AS request_group,
       (SELECT COUNT(*) FROM vendor_request v WHERE v.enquiry_id = e.id)                     AS vendor_count,
       (SELECT COUNT(*) FROM vendor_request v WHERE v.enquiry_id = e.id AND v.quoted = 1)    AS vendor_quoted_count
FROM enquiry e
JOIN party rp ON rp.id = e.requester_party_id
LEFT JOIN country rc ON rc.name = rp.country
LEFT JOIN country oc ON oc.name = e.origin_country
LEFT JOIN country dc ON dc.name = e.dest_country;
"""


def _to_v2(conn: sqlite3.Connection) -> list:
    """Rebuild v_enquiry, and clear the 'Other' the old migration invented.

    Before v2 a Lost or Dropped case whose workbook row had no Loss Reason was
    given closed_reason = 'Other'. That is a placeholder, not a reason, so it
    goes back to NULL where the migration is the only thing that wrote it.
    """
    conn.executescript(_V2_VIEW)
    notes = []
    cur = conn.execute(
        "UPDATE enquiry SET closed_reason = NULL "
        "WHERE closed_reason = 'Other' AND created_by = 'migration' "
        "AND EXISTS (SELECT 1 FROM change_log c WHERE c.table_name = 'enquiry' "
        "            AND c.row_id = enquiry.id AND c.source = 'migration')"
    )
    if cur.rowcount:
        notes.append("cleared the 'Other' placeholder on {} migrated closed "
                     "cases".format(cur.rowcount))
    conn.execute(
        "UPDATE enquiry SET system_notes = "
        "  trim(replace(system_notes, 'no Loss Reason in the workbook', '')) "
        "WHERE system_notes LIKE '%no Loss Reason in the workbook%'"
    )
    return notes


# --------------------------------------------------------------------- v3 --
# Export target and auto export became settings rather than a CLI flag alone,
# so that starting to publish exports is one deliberate change in one place.
_V3_SETTINGS = (
    ("export_target", "preview"),
    ("auto_export", "1"),
)


def _add_settings(conn: sqlite3.Connection, wanted) -> list:
    """Add settings this version introduced, never overwrite an existing one."""
    notes = []
    for key, value in wanted:
        row = conn.execute(
            "SELECT 1 FROM setting WHERE key = ?", (key,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO setting (key, value) VALUES (?, ?)",
                         (key, value))
            notes.append("setting {} added, default {!r}".format(key, value))
    return notes


def _to_v3(conn: sqlite3.Connection) -> list:
    return _add_settings(conn, _V3_SETTINGS)


# --------------------------------------------------------------------- v4 --
# Registering from Outlook needs to know which addresses are our own: a mail
# from us to somebody outside is us asking a vendor for a rate, not an enquiry
# arriving.
_V4_SETTINGS = (
    ("internal_domains", json.dumps([branding.INTERNAL_DOMAIN])),
)


def _to_v4(conn: sqlite3.Connection) -> list:
    return _add_settings(conn, _V4_SETTINGS)


# --------------------------------------------------------------------- v5 --
# The tracking list can add a note to a case. A note is recorded as
# an event at the status the case is already at, which made it look like a
# status change with a real date: a note on a Won case would have handed the
# export a closing date the workbook never had. status_event.kind separates
# the two. Notes still count as activity, so last_event_on sees them.
_V5_VIEW = """
DROP VIEW IF EXISTS v_enquiry;
CREATE VIEW v_enquiry AS
SELECT e.*,
       substr(e.received_on, 1, 7)                                   AS month,
       rp.key                                                        AS requester_key,
       rp.type                                                       AS requester_type,
       rp.country                                                    AS requester_country,
       rc.region                                                     AS requester_region,
       CASE rp.type WHEN 'Office' THEN 'Office' ELSE 'Agent' END AS source_type_legacy,
       oc.region                                                     AS origin_region,
       dc.region                                                     AS dest_region,
       CASE WHEN oc.region = 'Latin America' OR dc.region = 'Latin America' OR rc.region = 'Latin America' THEN 'Y' ELSE 'N' END AS latam,
       CASE
         WHEN e.origin_country IS NULL OR e.dest_country IS NULL THEN NULL
         WHEN e.origin_country = 'Unknown' OR e.dest_country = 'Unknown' THEN 'To Confirm'
         WHEN e.origin_country = 'USA' AND e.dest_country = 'USA' THEN 'US Domestic'
         WHEN e.origin_country = 'USA' THEN 'US Export'
         WHEN e.dest_country = 'USA' THEN 'US Import'
         ELSE 'Cross-trade' END                                      AS direction,
       CASE e.container_type
         WHEN '20GP' THEN 1 WHEN '20OT' THEN 1 WHEN '20FR' THEN 1 WHEN '20RF' THEN 1
         WHEN '40GP' THEN 2 WHEN '40HQ' THEN 2 WHEN '45HQ' THEN 2 WHEN '40OT' THEN 2 WHEN '40FR' THEN 2 WHEN '40RF' THEN 2 WHEN '40NOR' THEN 2
         ELSE NULL END * e.container_qty                             AS teu,
       -- Dates the record really carries. A migrated case that never had a quote
       -- or close date still gets an event so the timeline reads in order, but
       -- that date is a guess (approx = 1) and must not leave the app: exports
       -- and the monthly report read quoted_on / closed_on, never the _any pair.
       (SELECT MIN(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.status = 'Quoted' AND s.approx = 0 AND s.kind = 'status')                    AS quoted_on,
       (SELECT MIN(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.status IN ('Won','Lost','Dropped') AND s.approx = 0 AND s.kind = 'status')   AS closed_on,
       -- The same dates including the approximate ones, for screens and rules
       -- that need to place a case on the timeline at all.
       (SELECT MIN(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.status = 'Quoted' AND s.kind = 'status')                                     AS quoted_on_any,
       (SELECT MIN(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.status IN ('Won','Lost','Dropped') AND s.kind = 'status')                    AS closed_on_any,
       (SELECT MAX(at) FROM status_event s WHERE s.enquiry_id = e.id)                                                             AS last_event_on,
       COALESCE(e.group_id, e.id)                                    AS request_group,
       (SELECT COUNT(*) FROM vendor_request v WHERE v.enquiry_id = e.id)                     AS vendor_count,
       (SELECT COUNT(*) FROM vendor_request v WHERE v.enquiry_id = e.id AND v.quoted = 1)    AS vendor_quoted_count
FROM enquiry e
JOIN party rp ON rp.id = e.requester_party_id
LEFT JOIN country rc ON rc.name = rp.country
LEFT JOIN country oc ON oc.name = e.origin_country
LEFT JOIN country dc ON dc.name = e.dest_country;
"""


def _to_v5(conn: sqlite3.Connection) -> list:
    columns = [r[1] for r in conn.execute("PRAGMA table_info(status_event)")]
    notes = []
    if "kind" not in columns:
        conn.execute("ALTER TABLE status_event ADD COLUMN kind TEXT NOT NULL "
                     "DEFAULT 'status' CHECK (kind IN ('status','note'))")
        notes.append("status_event.kind added; every existing row is a status")
    conn.executescript(_V5_VIEW)
    notes.append("v_enquiry rebuilt: notes no longer count as quote or "
                 "closing dates")
    return notes


# --------------------------------------------------------------------- v6 --
# The timeline became editable. An event that turns out to be wrong
# is struck out rather than deleted - business data is never deleted
# - so every derived date and last_event_on skips it.
# 'Duplicate' and 'Registered in error' join the closed reasons at the same
# time, because that is what the two new buttons write.
_V6_VIEW = """
DROP VIEW IF EXISTS v_enquiry;
CREATE VIEW v_enquiry AS
SELECT e.*,
       substr(e.received_on, 1, 7)                                   AS month,
       rp.key                                                        AS requester_key,
       rp.type                                                       AS requester_type,
       rp.country                                                    AS requester_country,
       rc.region                                                     AS requester_region,
       CASE rp.type WHEN 'Office' THEN 'Office' ELSE 'Agent' END AS source_type_legacy,
       oc.region                                                     AS origin_region,
       dc.region                                                     AS dest_region,
       CASE WHEN oc.region = 'Latin America' OR dc.region = 'Latin America' OR rc.region = 'Latin America' THEN 'Y' ELSE 'N' END AS latam,
       CASE
         WHEN e.origin_country IS NULL OR e.dest_country IS NULL THEN NULL
         WHEN e.origin_country = 'Unknown' OR e.dest_country = 'Unknown' THEN 'To Confirm'
         WHEN e.origin_country = 'USA' AND e.dest_country = 'USA' THEN 'US Domestic'
         WHEN e.origin_country = 'USA' THEN 'US Export'
         WHEN e.dest_country = 'USA' THEN 'US Import'
         ELSE 'Cross-trade' END                                      AS direction,
       CASE e.container_type
         WHEN '20GP' THEN 1 WHEN '20OT' THEN 1 WHEN '20FR' THEN 1 WHEN '20RF' THEN 1
         WHEN '40GP' THEN 2 WHEN '40HQ' THEN 2 WHEN '45HQ' THEN 2 WHEN '40OT' THEN 2 WHEN '40FR' THEN 2 WHEN '40RF' THEN 2 WHEN '40NOR' THEN 2
         ELSE NULL END * e.container_qty                             AS teu,
       -- Dates the record really carries. A migrated case that never had a quote
       -- or close date still gets an event so the timeline reads in order, but
       -- that date is a guess (approx = 1) and must not leave the app: exports
       -- and the monthly report read quoted_on / closed_on, never the _any pair.
       (SELECT MIN(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.status = 'Quoted' AND s.approx = 0 AND s.kind = 'status' AND s.voided = 0)                    AS quoted_on,
       (SELECT MIN(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.status IN ('Won','Lost','Dropped') AND s.approx = 0 AND s.kind = 'status' AND s.voided = 0)   AS closed_on,
       -- The same dates including the approximate ones, for screens and rules
       -- that need to place a case on the timeline at all.
       (SELECT MIN(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.status = 'Quoted' AND s.kind = 'status' AND s.voided = 0)                                     AS quoted_on_any,
       (SELECT MIN(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.status IN ('Won','Lost','Dropped') AND s.kind = 'status' AND s.voided = 0)                    AS closed_on_any,
       (SELECT MAX(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.voided = 0)                                                             AS last_event_on,
       COALESCE(e.group_id, e.id)                                    AS request_group,
       (SELECT COUNT(*) FROM vendor_request v WHERE v.enquiry_id = e.id)                     AS vendor_count,
       (SELECT COUNT(*) FROM vendor_request v WHERE v.enquiry_id = e.id AND v.quoted = 1)    AS vendor_quoted_count
FROM enquiry e
JOIN party rp ON rp.id = e.requester_party_id
LEFT JOIN country rc ON rc.name = rp.country
LEFT JOIN country oc ON oc.name = e.origin_country
LEFT JOIN country dc ON dc.name = e.dest_country;
"""

_V6_CLOSED_REASONS = ("Duplicate", "Registered in error")


def _to_v6(conn: sqlite3.Connection) -> list:
    notes = []
    columns = [r[1] for r in conn.execute("PRAGMA table_info(status_event)")]
    if "voided" not in columns:
        conn.execute("ALTER TABLE status_event ADD COLUMN voided INTEGER "
                     "NOT NULL DEFAULT 0")
        notes.append("status_event.voided added; every existing event counts")
    conn.executescript(_V6_VIEW)
    notes.append("v_enquiry rebuilt: a voided event counts for nothing")

    row = conn.execute(
        "SELECT value FROM setting WHERE key = 'closed_reasons'").fetchone()
    if row is not None:
        import json

        try:
            reasons = json.loads(row[0])
        except ValueError:
            reasons = []
        added = [r for r in _V6_CLOSED_REASONS if r not in reasons]
        if added:
            # 'Other' stays last: it is the fallback, not a reason.
            rest = [r for r in reasons if r != "Other"]
            tail = ["Other"] if "Other" in reasons else []
            conn.execute("UPDATE setting SET value = ? WHERE key = ?",
                         (json.dumps(rest + added + tail), "closed_reasons"))
            notes.append("closed reasons added: " + ", ".join(added))
    return notes


# --------------------------------------------------------------------- v7 --
# A case now derives its thread key from its subject whenever it is saved.
# The cases already on file get the same treatment once: a key is
# how Sort-Inbox and propose-status find the mail again, and a case with a
# subject but no key is invisible to both.
def _to_v7(conn: sqlite3.Connection) -> list:
    from . import parsing

    rows = conn.execute(
        "SELECT id, source_subject FROM enquiry "
        "WHERE source_subject IS NOT NULL AND source_subject <> '' "
        "  AND (thread_key IS NULL OR thread_key = '')").fetchall()
    filled = 0
    for row in rows:
        key = parsing.subject_thread_key(row[1])
        if key:
            conn.execute("UPDATE enquiry SET thread_key = ? WHERE id = ?",
                         (key, row[0]))
            filled += 1
    return ["thread_key filled in from the subject on {} case(s)".format(filled)] \
        if filled else []


# --------------------------------------------------------------------- v8 --
# Three things a person can now say that the tables could not hold: whose
# rate a won case used (four answers, not a free note), a vendor request
# recorded by mistake (struck out, never deleted), and one party merged
# into another (the row stays, points at the survivor, its name kept as an
# alias). The views are rebuilt from schema.sql so a struck-out vendor
# request counts for nothing.
_V8_COLUMNS = (
    ("enquiry", "rate_used",
     "TEXT CHECK (rate_used IN ('agent','office','own','not_recorded'))"),
    ("vendor_request", "voided", "INTEGER NOT NULL DEFAULT 0"),
    ("party", "aliases", "TEXT"),
    ("party", "merged_into", "INTEGER REFERENCES party(id)"),
)


def _columns(conn, table) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info({})".format(table))}


def _rebuild_views(conn) -> list:
    """Every CREATE VIEW in schema.sql, dropped and created again, so the
    views of an upgraded database are the views of a new one."""
    import re

    from . import paths

    sql = paths.SCHEMA_SQL.read_text(encoding="utf-8")
    names = []
    for block in re.findall(r"CREATE VIEW\s+(\w+)\s+AS.*?;", sql, re.S):
        names.append(block)
    for statement in re.findall(r"CREATE VIEW\s+\w+\s+AS.*?;", sql, re.S):
        name = re.match(r"CREATE VIEW\s+(\w+)", statement).group(1)
        conn.execute("DROP VIEW IF EXISTS {}".format(name))
        conn.execute(statement)
    return names


def _to_v8(conn: sqlite3.Connection) -> list:
    notes = []
    for table, column, definition in _V8_COLUMNS:
        if column not in _columns(conn, table):
            conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(
                table, column, definition))
            notes.append("{}.{} added".format(table, column))
    # What the won cases already on file said, read into the four answers.
    filled = conn.execute("""
        UPDATE enquiry SET rate_used = CASE
            WHEN awarded_vendor_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM party p WHERE p.id = enquiry.awarded_vendor_id
                  AND p.type = 'Office') THEN 'office'
            WHEN awarded_vendor_id IS NOT NULL THEN 'agent'
            WHEN lower(COALESCE(awarded_note, '')) LIKE 'own rate%' THEN 'own'
            WHEN COALESCE(awarded_note, '') <> '' THEN 'not_recorded'
            ELSE NULL END
        WHERE status = 'Won' AND rate_used IS NULL""").rowcount
    if filled:
        notes.append("rate_used read off {} won case(s)".format(filled))
    views = _rebuild_views(conn)
    notes.append("views rebuilt: " + ", ".join(views))
    return notes


# --------------------------------------------------------------------- v9 --
# Two CHECK constraints have to widen - party.type gains 'Carrier',
# enquiry.rate_used gains 'carrier' - and SQLite cannot alter a CHECK, so
# the two tables are rebuilt from schema.sql (every row copied, foreign
# keys kept: they reference the table by name). enquiry.scope arrives the
# same way: In for everybody, Out for the cases a direct customer asked.
_V9_REBUILD = ("party", "enquiry")


def _statements(sql: str, pattern: str) -> list:
    import re

    return re.findall(pattern, sql, re.S | re.I)


def _rebuild_table(conn, name: str, schema_sql: str, keep_domestic: bool = True) -> list:
    """Recreate `name` from schema.sql and copy every row across.

    Columns are matched by name: an old column that is not in the new
    definition is dropped (there are none), a new column takes its default.
    Views are dropped before and rebuilt after, because a rename is refused
    while a view still points at the old table.

    TK1 (v46): while a table's service_mode still allows 'Domestic' (an
    upgrade from before v46), a rebuild by an earlier step keeps allowing
    it beside 'Truck', so its rows copy; v46 turns them into Truck and
    rebuilds without it (keep_domestic=False).
    """
    import re

    match = re.search(r"CREATE TABLE {}\s*\((.*?)\);".format(name), schema_sql, re.S)
    if match is None:
        raise RuntimeError("no CREATE TABLE {} in schema.sql".format(name))
    body = match.group(1)
    old_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    if keep_domestic and old_sql is not None and "'Domestic'" in old_sql[0] and "'Domestic'" not in body:
        body = body.replace("'Truck'", "'Truck','Domestic'")
    old_columns = [r[1] for r in conn.execute("PRAGMA table_info({})".format(name))]
    conn.execute("CREATE TABLE {}_new ({})".format(name, body))
    new_columns = [r[1] for r in conn.execute("PRAGMA table_info({}_new)".format(name))]
    shared = [c for c in new_columns if c in old_columns]
    columns = ", ".join(shared)
    conn.execute("INSERT INTO {n}_new ({c}) SELECT {c} FROM {n}".format(n=name, c=columns))
    conn.execute("DROP TABLE {}".format(name))
    conn.execute("ALTER TABLE {n}_new RENAME TO {n}".format(n=name))
    made = []
    # A partial index ('... WHERE row_key IS NOT NULL;') ends after its
    # WHERE, not at the first ');' in the file.
    for statement in _statements(
            schema_sql, r"CREATE (?:UNIQUE )?INDEX \w+ ON {}\s*\([^;]*?\)(?:\s+WHERE[^;]*)?;".format(name)):
        conn.execute(statement)
        made.append(statement.split()[2] if statement.upper().startswith("CREATE INDEX")
                    else statement.split()[3])
    return made


def _check_values(table_sql: str, column: str) -> str:
    """The CHECK (column IN (...)) list of a column, from the stored CREATE
    TABLE - the comments beside it name the values too, so only the
    constraint itself is read."""
    import re

    match = re.search(r"CHECK\s*\(\s*{}\s+IN\s*\((.*?)\)\s*\)".format(column),
                      table_sql, re.S | re.I)
    return match.group(1) if match else ""


def _to_v9(conn: sqlite3.Connection) -> list:
    import re

    from . import paths

    notes = []
    # The comments are dropped first: a ';' inside one would cut a CREATE
    # TABLE short.
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    if "scope" not in _columns(conn, "enquiry"):
        conn.execute("ALTER TABLE enquiry ADD COLUMN scope TEXT NOT NULL DEFAULT 'In' "
                     "CHECK (scope IN ('In','Out'))")
        notes.append("enquiry.scope added: In for every case")
    # A direct customer's case is our own shipment to buy for: Out.
    moved = [r[0] for r in conn.execute(
        "SELECT e.opp_no FROM enquiry e JOIN party p ON p.id = e.requester_party_id "
        "WHERE p.type = 'Direct Customer' AND e.scope = 'In' "
        "ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER)")]
    if moved:
        conn.execute("UPDATE enquiry SET scope = 'Out' WHERE requester_party_id IN "
                     "(SELECT id FROM party WHERE type = 'Direct Customer') AND scope = 'In'")
        notes.append("scope set to Out on {} case(s) asked by a direct customer: {}".format(
            len(moved), ", ".join(moved)))

    # The CHECK constraints: rebuild when they are still the old ones.
    party_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'party'").fetchone()[0]
    enquiry_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'enquiry'").fetchone()[0]
    wanted = []
    if "'Carrier'" not in _check_values(party_sql, "type"):
        wanted.append("party")
    if "'carrier'" not in _check_values(enquiry_sql, "rate_used"):
        wanted.append("enquiry")
    if wanted:
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for name in _statements(schema_sql, r"CREATE VIEW\s+(\w+)\s+AS"):
                conn.execute("DROP VIEW IF EXISTS {}".format(name))
            for name in wanted:
                indexes = _rebuild_table(conn, name, schema_sql)
                notes.append("{} rebuilt (CHECK widened), indexes: {}".format(
                    name, ", ".join(indexes)))
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    # A won case whose 'agent' is a carrier was really booked direct.
    changed = conn.execute(
        "UPDATE enquiry SET rate_used = 'carrier' WHERE rate_used = 'agent' AND "
        "awarded_vendor_id IN (SELECT id FROM party WHERE type = 'Carrier')").rowcount
    if changed:
        notes.append("rate_used 'agent' -> 'carrier' on {} case(s) won with a carrier".format(changed))
    views = _rebuild_views(conn)
    notes.append("views rebuilt: " + ", ".join(views))
    return notes


# -------------------------------------------------------------------- v10 --
# A place written in words ('Miami port', 'Barcelona, Spain') is matched to
# a location through its aliases. The first two are seeded; the
# rest are added as mails turn them up.
_V10_ALIASES = (
    ("MIA", "Miami, Miami port", None),
    ("BCN", "Barcelona, Barcelona port",
     {"official_name": "Barcelona", "type": "Seaport", "country": "Spain",
      "code_system": "UN/LOCODE", "code_official": "ESBCN", "is_seaport": 1}),
)


def _to_v10(conn: sqlite3.Connection) -> list:
    notes = []
    if "aliases" not in _columns(conn, "location"):
        conn.execute("ALTER TABLE location ADD COLUMN aliases TEXT")
        notes.append("location.aliases added")
    for key, aliases, create in _V10_ALIASES:
        row = conn.execute("SELECT key, aliases FROM location WHERE key = ?", (key,)).fetchone()
        if row is None and create:
            if conn.execute("SELECT 1 FROM country WHERE name = ?",
                            (create["country"],)).fetchone() is None:
                continue
            conn.execute(
                "INSERT INTO location (key, official_name, type, country, status, "
                "code_system, code_official, is_seaport, aliases) "
                "VALUES (?, ?, ?, ?, 'OK', ?, ?, ?, ?)",
                (key, create["official_name"], create["type"], create["country"],
                 create["code_system"], create["code_official"], create["is_seaport"], aliases))
            notes.append("location {} created with aliases '{}'".format(key, aliases))
        elif row is not None and not row[1]:
            conn.execute("UPDATE location SET aliases = ? WHERE key = ?", (aliases, key))
            notes.append("location {} aliases '{}'".format(key, aliases))
    return notes


# -------------------------------------------------------------------- v11 --
# A case can be parked: 'On hold' in the Set status box writes why and
# until when, without touching the status. Tracking skips it
# until the date; the report never looks at it.
def _to_v11(conn: sqlite3.Connection) -> list:
    notes = []
    for column in ("hold_reason", "hold_until"):
        if column not in _columns(conn, "enquiry"):
            conn.execute("ALTER TABLE enquiry ADD COLUMN {} TEXT".format(column))
            notes.append("enquiry.{} added".format(column))
    views = _rebuild_views(conn)
    notes.append("views rebuilt: " + ", ".join(views))
    return notes


# -------------------------------------------------------------------- v12 --
# What the mail suggests for a case waits in `suggestion` until somebody
# says yes or no. A yes writes a status_event or vendor_request
# with source 'mail', so the two CHECK lists are widened.
_V12_TABLE = """
CREATE TABLE IF NOT EXISTS suggestion (
    id              INTEGER PRIMARY KEY,
    enquiry_id      INTEGER NOT NULL REFERENCES enquiry(id) ON DELETE CASCADE,
    opp_no          TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('status','vendor','note')),
    proposed_status TEXT,
    closed_reason   TEXT,
    proposed_date   TEXT,
    vendor_key      TEXT,
    vendor_domain   TEXT,
    note            TEXT,
    evidence_sender TEXT,
    evidence_date   TEXT,
    evidence_quote  TEXT,
    confidence      TEXT,
    entry_id        TEXT,
    source          TEXT NOT NULL DEFAULT 'scan' CHECK (source IN ('scan','mail')),
    state           TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','accepted','dismissed')),
    decided_at      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_suggestion_case ON suggestion (enquiry_id, state);
"""


def _to_v12(conn: sqlite3.Connection) -> list:
    import re

    from . import paths

    notes = []
    if "suggestion" not in _tables(conn):
        conn.executescript(_V12_TABLE)
        notes.append("suggestion table created")
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    wanted = []
    for table in ("status_event", "vendor_request"):
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,)).fetchone()[0]
        if "'mail'" not in _check_values(table_sql, "source"):
            wanted.append(table)
    if wanted:
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for name in _statements(schema_sql, r"CREATE VIEW\s+(\w+)\s+AS"):
                conn.execute("DROP VIEW IF EXISTS {}".format(name))
            for name in wanted:
                indexes = _rebuild_table(conn, name, schema_sql)
                notes.append("{} rebuilt (source may be 'mail'), indexes: {}".format(
                    name, ", ".join(indexes)))
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    views = _rebuild_views(conn)
    notes.append("views rebuilt: " + ", ".join(views))
    return notes


def _tables(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


# -------------------------------------------------------------------- v13 --
# Follow-ups on a quote: status_event.kind gains 'followup' and
# the columns seq / channel; the ladder's settings and the three mail
# templates arrive with their defaults (never overwriting a value already
# there). The CHECK widens, so status_event is rebuilt from schema.sql the
# v12 way; v_enquiry gains fu_count / last_fu_on / last_fu_seq.
FU_SETTINGS = (
    ("fu1_days", "1"), ("fu2_days", "5"), ("fu_decide_days", "5"), ("fu_max", "2"),
    # The pool: versions separated
    # by a line of ---; the blanks are {first_name} {rates} {client}
    # {valid_until_line} {open_question_line} (opplist/followup.py).
    # Same text as schema.sql.
    ("fu_template_1",
     'Hi {first_name},\n\nAre {rates} workable for this shipment? We can book as soon as you give the go-ahead, and we are glad to rework anything that does not fit.\n{valid_until_line}\n{open_question_line}\nThank you.\n---\nHi {first_name},\n\nFollowing up on {rates}: do they suit what you need? If you want to go ahead, or if another option should be priced, just say the word.\n{valid_until_line}\n{open_question_line}\nThank you.\n---\nHi {first_name},\n\nChecking back to ask how {rates} sit with you. Should a figure need another look, or should we check something else, send us a line and we will get onto it.\n{valid_until_line}\n{open_question_line}\nThank you.\n---\nHi {first_name},\n\nHoping your week is going well. We are circling back to find out whether {rates} are a fit for {client}. Any feedback is welcome, and any piece of the offer can be revisited.\n{valid_until_line}\n{open_question_line}\nThank you.\n---\nHi {first_name},\n\nFollowing up with a short reminder about {rates}. Do the numbers work for {client}? If a leg or a charge should be priced again, we can do that quickly.\n{valid_until_line}\n{open_question_line}\nThank you.'),
    ("fu_template_2",
     'Hi {first_name},\n\nFollowing up on our offer below, which we have not heard back on yet. Is it still being weighed, or has the cargo plan moved on?\nThank you.\n---\nHi {first_name},\n\nAny update on where the offer below stands? If your side is still comparing, no rush; if the plan has been dropped, a short note is enough for us to close it.\nThank you.\n---\nHi {first_name},\n\nChecking back once more on the offer below. Is the move still going ahead? If something on your end has shifted, feel free to tell us.\nThank you.\n---\nHi {first_name},\n\nIs the cargo below still on track? When the dates slip, we are glad to price the move again once the ready date is firm.\nWe look forward to hearing from you.\n---\nHi {first_name},\n\nTouching base about the offer below. Could you tell us whether this shipment is live or paused for now? We can go over the figures again nearer the time it is ready.\nThank you.\n---\nHi {first_name},\n\nFollowing up to ask whether anything has moved on the cargo below. If the timeline is different now, send us the new dates and we will bring the offer up to date.\nWe look forward to hearing from you.'),
    ("fu_template_3",
     'Hi {first_name},\n\nThanks once more for inviting us to quote. Has the business been awarded yet? If it has, a word on how our offer measured up would help us sharpen the next one.\nThank you.\n---\nHi {first_name},\n\nThe shipment below has probably been placed by now. If it went to someone else, we would value hearing how far off we were, so the next offer lands closer.\nThank you.\n---\nHi {first_name},\n\nWe appreciated the chance to bid on this one. Is there a final word on the shipment? Whichever way it went, your comments would help us serve your next requests better.\nThank you.\n---\nHi {first_name},\n\nCould you share how the bid below turned out? If another offer was chosen, was it mainly the price, the routing or something else? Knowing that lets us quote better next time.\nThank you.'),
)


def _to_v13(conn: sqlite3.Connection) -> list:
    import re

    from . import paths

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'status_event'"
    ).fetchone()[0]
    if "'followup'" not in _check_values(table_sql, "kind") or \
            "seq" not in _columns(conn, "status_event"):
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for name in _statements(schema_sql, r"CREATE VIEW\s+(\w+)\s+AS"):
                conn.execute("DROP VIEW IF EXISTS {}".format(name))
            indexes = _rebuild_table(conn, "status_event", schema_sql)
            notes.append("status_event rebuilt (kind may be 'followup'; seq, channel added), "
                         "indexes: {}".format(", ".join(indexes)))
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    notes.extend(_add_settings(conn, FU_SETTINGS))
    views = _rebuild_views(conn)
    notes.append("views rebuilt: " + ", ".join(views))
    return notes


# -------------------------------------------------------------------- v14 --
# The master data layer: master_area, master_location,
# master_location_alias, party_group; enquiry gains origin_loc_id / dest_loc_id
# and the *_loc_legacy copies; party gains group_id and roles. enquiry's
# foreign key to location(key) goes (the text columns are derived from the
# ids from now on), which SQLite can only do by rebuilding the table from
# schema.sql. The data itself moves with `migrate-master --apply`, not here.
_V14_TABLES = """
CREATE TABLE IF NOT EXISTS master_area (
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    country_code  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS master_location (
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    type          TEXT NOT NULL CHECK (type IN ('city','airport','seaport','ramp')),
    country       TEXT NOT NULL REFERENCES country(name),
    country_code  TEXT,
    subdivision   TEXT,
    parent_id     INTEGER REFERENCES master_location(id),
    area_id       INTEGER REFERENCES master_area(id),
    unlocode      TEXT,
    iata          TEXT,
    is_seaport    INTEGER NOT NULL DEFAULT 0,
    is_airport    INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','to_confirm','retired')),
    note          TEXT,
    source        TEXT NOT NULL DEFAULT 'phase1' CHECK (source IN ('phase1','manual','intake')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS master_location_alias (
    alias         TEXT NOT NULL,
    location_id   INTEGER NOT NULL REFERENCES master_location(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('code','iata_city','unlocode','legacy_key','manual')),
    PRIMARY KEY (alias, location_id)
);
CREATE INDEX IF NOT EXISTS idx_master_alias ON master_location_alias(alias);
CREATE TABLE IF NOT EXISTS party_group (
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    notes         TEXT
);
"""

_V14_COLUMNS = (
    ("enquiry", "origin_loc_id", "INTEGER REFERENCES master_location(id)"),
    ("enquiry", "dest_loc_id", "INTEGER REFERENCES master_location(id)"),
    ("enquiry", "origin_loc_legacy", "TEXT"),
    ("enquiry", "dest_loc_legacy", "TEXT"),
    ("party", "group_id", "INTEGER REFERENCES party_group(id)"),
    ("party", "roles", "TEXT NOT NULL DEFAULT ''"),
)


def _to_v14(conn: sqlite3.Connection) -> list:
    import re

    from . import paths

    notes = []
    tables = _tables(conn)
    if not {"master_area", "master_location", "master_location_alias", "party_group"} <= tables:
        conn.executescript(_V14_TABLES)
        notes.append("master_area, master_location, master_location_alias, party_group created")
    for table, column, definition in _V14_COLUMNS:
        if column not in _columns(conn, table):
            conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, column, definition))
            notes.append("{}.{} added".format(table, column))
    enquiry_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'enquiry'").fetchone()[0]
    if re.search(r"REFERENCES\s+location\s*\(", enquiry_sql, re.I):
        schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for name in _statements(schema_sql, r"CREATE VIEW\s+(\w+)\s+AS"):
                conn.execute("DROP VIEW IF EXISTS {}".format(name))
            indexes = _rebuild_table(conn, "enquiry", schema_sql)
            notes.append("enquiry rebuilt (foreign key to location(key) removed), indexes: {}".format(
                ", ".join(indexes)))
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    notes.extend(_add_settings(conn, (("master_contract_version", "1"),)))
    views = _rebuild_views(conn)
    notes.append("views rebuilt: " + ", ".join(views))
    return notes


# -------------------------------------------------------------------- v15 --
# R0 for the Rate Desk: two more contract views,
# v_master_enquiry_lane and v_master_vendor_request. Views only; the
# contract version stays 1 because nothing was removed or renamed.
def _to_v15(conn: sqlite3.Connection) -> list:
    views = _rebuild_views(conn)
    return ["views rebuilt: " + ", ".join(views)]


# -------------------------------------------------------------------- v16 --
# Follow-ups are counted per quote round: v_enquiry.fu_count,
# last_fu_on and last_fu_seq now see only the follow-ups dated on or after
# the latest live Quoted event, so a case quoted again starts at FU1 again,
# and fu_count_total keeps the whole history. Views only; no table changes.
def _to_v16(conn: sqlite3.Connection) -> list:
    views = _rebuild_views(conn)
    return ["views rebuilt: " + ", ".join(views)]


# -------------------------------------------------------------------- v17 --
# Closed reasons are two vocabularies, not one (2026-09-18): Lost
# is a shipment we did not get, Dropped is a row that should not exist. Two
# settings, seeded with the words agreed on; the old single list stays in
# the table for the workbook import and is no longer read by the app.
_V17_SETTINGS = (
    ("closed_reasons_lost",
     '["Price too high","Rate too slow","No response from customer","Shipment cancelled",'
     '"Unable to handle","No vendor rate","Went with another forwarder","Other"]'),
    ("closed_reasons_dropped", '["Duplicate of OPP-xxxx","Registered in error"]'),
)


def _to_v17(conn: sqlite3.Connection) -> list:
    notes = _add_settings(conn, _V17_SETTINGS)
    notes.append("the reasons already on the cases are not touched here: "
                 "python -m opplist fix-closed-reasons --apply does that")
    return notes


# -------------------------------------------------------------------- v18 --
# Re-quote (2026-09-21): v_enquiry gains quote_rounds and
# in_requote. Views only, no table changes, no data moved.
def _to_v18(conn: sqlite3.Connection) -> list:
    views = _rebuild_views(conn)
    return ["views rebuilt: " + ", ".join(views)]


# -------------------------------------------------------------------- v19 --
# A same-day re-quote must win over the Quoted it undoes
# (2026-09-21). status_event.kind gains 'requote' (the table is rebuilt the
# way v13 did it - SQLite cannot alter a CHECK), the events requote() wrote
# under v18 (kind status, Sourcing, note 'Re-quote...') take the new kind
# through the repository so change_log has them, and the views are rebuilt
# for the new in_requote rule. The restore point is taken by open_db.
def _to_v19(conn: sqlite3.Connection) -> list:
    import re

    from . import db, paths

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'status_event'"
    ).fetchone()[0]
    if "'requote'" not in _check_values(table_sql, "kind"):
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for name in _statements(schema_sql, r"CREATE VIEW\s+(\w+)\s+AS"):
                conn.execute("DROP VIEW IF EXISTS {}".format(name))
            indexes = _rebuild_table(conn, "status_event", schema_sql)
            notes.append("status_event rebuilt (kind may be 'requote'), indexes: {}".format(
                ", ".join(indexes)))
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    repo = db.Repository(conn, source="migration")
    moved = 0
    for row in conn.execute(
            "SELECT id FROM status_event WHERE kind = 'status' AND status = 'Sourcing' "
            "AND note LIKE 'Re-quote%' ORDER BY id").fetchall():
        repo.update("status_event", row["id"], {"kind": "requote"})
        moved += 1
    notes.append("re-quote events written under v18 given kind 'requote': {}".format(moved))
    views = _rebuild_views(conn)
    notes.append("views rebuilt: " + ", ".join(views))
    return notes


# -------------------------------------------------------------------- v20 --
# Two things about mail threads (2026-09-23). The Chinese reply prefixes
# (回复：, 答复:, 转发…) now strip like RE: and FW:, so the thread
# keys computed before them are wrong for those subjects: every case's key
# is computed again and replaced only when the old one carried a prefix and
# the new one differs - through the repository, so change_log has it. And a
# new table, enquiry_thread, for the threads attached to a case by hand
# when the ids the case carries no longer find its mail.
_OLD_KEY_PREFIX = ("RE", "RES", "FW", "FWD", "ENC", "RV", "AW", "TR")


def _to_v20(conn: sqlite3.Connection) -> list:
    import re

    from . import db, parsing, paths

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    if not db._has_table(conn, "enquiry_thread"):
        for statement in _statements(schema_sql, r"CREATE TABLE enquiry_thread\s*\(.*?\);"):
            conn.execute(statement)
        for statement in _statements(
                schema_sql, r"CREATE (?:UNIQUE )?INDEX \w+ ON enquiry_thread\s*\([^;]*?\);"):
            conn.execute(statement)
        notes.append("enquiry_thread table created")
    repo = db.Repository(conn, source="migration")
    changed = []
    for row in conn.execute(
            "SELECT id, opp_no, source_subject, thread_key FROM enquiry "
            "WHERE source_subject IS NOT NULL AND thread_key IS NOT NULL ORDER BY id").fetchall():
        old = row["thread_key"] or ""
        new = parsing.subject_thread_key(row["source_subject"])
        if new and new != old and old.startswith(_OLD_KEY_PREFIX):
            repo.update("enquiry", row["id"], {"thread_key": new})
            changed.append(row["opp_no"])
    notes.append("thread keys recomputed for the new reply prefixes: {} changed{}".format(
        len(changed), " (" + ", ".join(changed) + ")" if changed else ""))
    return notes


# -------------------------------------------------------------------- v21 --
# Report section 7, 'Why we lost' (2026-09-24): the two prices a
# lost case can carry, both free text, both optional. Columns only; the
# views are rebuilt so v_enquiry's e.* picks them up.
def _to_v21(conn: sqlite3.Connection) -> list:
    notes = []
    for column in ("lost_their_price", "lost_our_price"):
        if column not in _columns(conn, "enquiry"):
            conn.execute("ALTER TABLE enquiry ADD COLUMN {} TEXT".format(column))
            notes.append("enquiry.{} added".format(column))
    views = _rebuild_views(conn)
    notes.append("views rebuilt: " + ", ".join(views))
    return notes


# -------------------------------------------------------------------- v22 --
# The reason lives on the event that carries it (2026-09-24):
# status_event.reason, a plain added column. A Lost / Dropped event takes
# the closed reason, a Quoted event that reopens a round (or the requote
# event that opened it) takes one of setting 'requote_reasons'. The closed
# reasons already on the cases are copied onto their closing events, once,
# through the repository; enquiry.closed_reason is not touched - the
# exports and the report keep reading it.
_V22_SETTINGS = (
    ("requote_reasons",
     '["Lower rate","Request changed","Rates expired","Different routing or carrier",'
     '"Extra option asked","Correction","Other"]'),
)


def _to_v22(conn: sqlite3.Connection) -> list:
    from . import db, status as status_mod

    notes = []
    if "reason" not in _columns(conn, "status_event"):
        conn.execute("ALTER TABLE status_event ADD COLUMN reason TEXT")
        notes.append("status_event.reason added")
    notes.extend(_add_settings(conn, _V22_SETTINGS))
    repo = db.Repository(conn, source="migration")
    copied = 0
    for case in conn.execute(
            "SELECT id, status, closed_reason FROM enquiry WHERE status IN ('Lost', 'Dropped') "
            "AND closed_reason IS NOT NULL AND TRIM(closed_reason) <> '' ORDER BY id").fetchall():
        event = conn.execute(
            "SELECT id, reason FROM status_event WHERE enquiry_id = ? AND voided = 0 "
            "AND {} AND status = ? ORDER BY {} LIMIT 1".format(
                status_mod.STATUS_KIND_SQL, status_mod.LATEST_EVENT_ORDER),
            (case["id"], case["status"])).fetchone()
        if event is not None and event["reason"] is None:
            repo.update("status_event", event["id"], {"reason": case["closed_reason"]})
            copied += 1
    notes.append("closed reasons copied onto their closing events: {}".format(copied))
    return notes


# -------------------------------------------------------------------- v23 --
# The case page's Mail card (2026-09-25): a vendor request
# remembers the mail it was logged from - its conversation id and its
# subject's thread key - so the vendor's thread can be shown beside the
# agent's. Two nullable columns. The rows already logged from Outlook keep
# the subject in their note ('Logged from Outlook: <subject>'): the key is
# computed from it and set when the mail archive knows a mail with that
# key, the conversation id from the first such mail. Read only from the
# archive; nothing without one.
def _to_v23(conn: sqlite3.Connection) -> list:
    from . import db, mail_archive, parsing

    notes = []
    for column in ("conversation_id", "thread_key"):
        if column not in _columns(conn, "vendor_request"):
            conn.execute("ALTER TABLE vendor_request ADD COLUMN {} TEXT".format(column))
            notes.append("vendor_request.{} added".format(column))
    rows = conn.execute(
        "SELECT id, notes FROM vendor_request WHERE notes LIKE 'Logged from Outlook:%' "
        "AND COALESCE(thread_key, '') = '' ORDER BY id").fetchall()
    keyed = 0
    if rows:
        search = None
        try:
            search = mail_archive.ArchiveSearch()
            search.__enter__()
        except mail_archive.ArchiveError:
            search = None
        if search is None:
            notes.append("vendor threads: no mail archive to read, {} row(s) left for later".format(len(rows)))
        else:
            repo = db.Repository(conn, source="migration")
            try:
                for row in rows:
                    subject = row["notes"].split(":", 1)[1].strip()
                    clean = parsing.clean_subject(subject)
                    key = parsing.thread_key(clean)
                    if not key:
                        continue
                    hits = [m for m in search.by_subject(clean, limit=300)
                            if parsing.subject_thread_key(m.subject) == key]
                    if not hits:
                        continue
                    hits.sort(key=lambda m: m.received_at or m.received_on or "")
                    values = {"thread_key": key}
                    if hits[0].conversation_id:
                        values["conversation_id"] = hits[0].conversation_id
                    repo.update("vendor_request", row["id"], values)
                    keyed += 1
            finally:
                search.__exit__(None, None, None)
            notes.append("vendor requests given a thread key from the archive: {} of {}".format(
                keyed, len(rows)))
    else:
        notes.append("vendor requests given a thread key from the archive: 0 of 0")
    views = _rebuild_views(conn)
    notes.append("views rebuilt: " + ", ".join(views))
    return notes


# -------------------------------------------------------------------- v24 --
# Shipments (2026-09-25): two new tables from schema.sql, nothing
# else touched. No backfill: a shipment starts from its won case by hand.
def _to_v24(conn: sqlite3.Connection) -> list:
    import re

    from . import db, paths

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    for table in ("shipment", "shipment_event"):
        if db._has_table(conn, table):
            notes.append("{} table already there".format(table))
            continue
        for statement in _statements(schema_sql, r"CREATE TABLE {}\s*\(.*?\);".format(table)):
            conn.execute(statement)
        for statement in _statements(
                schema_sql, r"CREATE (?:UNIQUE )?INDEX \w+ ON {}\s*\([^;]*?\);".format(table)):
            conn.execute(statement)
        notes.append("{} table created".format(table))
    # R10 snoozes a shipment: the CHECK on tracking_snooze.ref_kind grows
    # by one value, which in SQLite means rebuilding the table (rows kept)
    current = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tracking_snooze'").fetchone()
    if current is not None and "'shipment'" not in (current["sql"] if hasattr(current, "keys") else current[0]):
        # only the views schema.sql owns are dropped and rebuilt; the
        # 'location' compatibility view migrate-master created is not one
        # of them, and dropping every view took it with it (hotfix v25)
        for view in _schema_view_names():
            conn.execute("DROP VIEW IF EXISTS {}".format(view))
        _rebuild_table(conn, "tracking_snooze", schema_sql)
        notes.append("tracking_snooze rebuilt: ref_kind accepts 'shipment'")
        notes.append("views rebuilt: " + ", ".join(_rebuild_views(conn)))
    return notes


def _schema_view_names() -> list:
    import re

    from . import paths

    sql = paths.SCHEMA_SQL.read_text(encoding="utf-8")
    return re.findall(r"CREATE VIEW\s+(\w+)\s+AS", sql)


def _object_type(conn, name: str):
    row = conn.execute("SELECT type FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


# -------------------------------------------------------------------- v25 --
# Hotfix (2026-09-25 17:07 to 17:40): v24 dropped every view before
# rebuilding tracking_snooze and recreated only the seven in schema.sql,
# so the 'location' compatibility view that migrate-master (v14) left
# over master_location was gone - every page reading `location` failed.
# This puts it back with migrate-master's own definition, and only on a
# master-migrated file (location_old there, location missing); a fresh
# file still has the location table from schema.sql.
def _to_v25(conn: sqlite3.Connection) -> list:
    from . import db, migrate_master

    if _object_type(conn, "location") is not None:
        return ["location {} already there".format(_object_type(conn, "location"))]
    if not db._has_table(conn, "location_old"):
        return ["location: neither the view nor location_old on file (never master-migrated), nothing to do"]
    conn.execute(migrate_master.LOCATION_VIEW_SQL.replace("CREATE VIEW location", "CREATE VIEW IF NOT EXISTS location", 1))
    return ["location view recreated (migrate-master's definition)"]


# -------------------------------------------------------------------- v26 --
# Shipments 5b: suggestions for shipments reuse the suggestion
# table - three more kinds (a CHECK, so the table is rebuilt from schema.sql
# with every row copied) and three columns (shipment_id, message_id,
# proposed_eta); shipment gets closed_reason. No view reads suggestion, so
# none is dropped.
def _to_v26(conn: sqlite3.Connection) -> list:
    import re

    from . import db, paths

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    if db._has_table(conn, "shipment") and "closed_reason" not in _columns(conn, "shipment"):
        conn.execute("ALTER TABLE shipment ADD COLUMN closed_reason TEXT "
                     "CHECK (closed_reason IN ('Delivered','Not shipped','Registered in error'))")
        notes.append("shipment.closed_reason added")
    current = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'suggestion'").fetchone()
    if current is not None and "shipment_milestone" not in current[0]:
        indexes = _rebuild_table(conn, "suggestion", schema_sql)
        notes.append("suggestion rebuilt for the shipment kinds (shipment_id, message_id, proposed_eta added; "
                     "indexes {})".format(", ".join(indexes)))
    else:
        notes.append("suggestion already carries the shipment kinds")
    return notes


# -------------------------------------------------------------------- v27 --
# Shipments 5c: master_location.lat / lon (filled by the
# fill-coordinates command, never fetched) and shipment.via_points. Three
# ALTER TABLEs, nothing rebuilt, no view touched.
def _to_v27(conn: sqlite3.Connection) -> list:
    from . import db

    notes = []
    for table, column, definition in (("master_location", "lat", "REAL"), ("master_location", "lon", "REAL"),
                                      ("shipment", "via_points", "TEXT")):
        if not db._has_table(conn, table):
            notes.append("{} not on file, {} skipped".format(table, column))
            continue
        if column not in _columns(conn, table):
            conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, column, definition))
            notes.append("{}.{} added".format(table, column))
    return notes


# -------------------------------------------------------------------- v28 --
# Cargo lines (2026-09-24): the enquiry_cargo table and its
# index, from schema.sql. Nothing is backfilled - a case without lines is
# the single implicit line its own fields describe. No view changes.
def _to_v28(conn: sqlite3.Connection) -> list:
    import re

    from . import db, paths

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    if db._has_table(conn, "enquiry_cargo"):
        notes.append("enquiry_cargo already on file")
        return notes
    for statement in _statements(schema_sql, r"CREATE TABLE enquiry_cargo\s*\(.*?\);"):
        conn.execute(statement)
    for statement in _statements(schema_sql, r"CREATE (?:UNIQUE )?INDEX \w+ ON enquiry_cargo\s*\([^;]*?\);"):
        conn.execute(statement)
    notes.append("enquiry_cargo table created (no rows: nothing backfilled)")
    return notes


# -------------------------------------------------------------------- v29 --
# Sourcing retired (2026-09-24): a re-quote now writes a requote event with
# status New, so v_enquiry.in_requote can no longer ask whether the winning
# event is a Sourcing. It now reads 'an open round': a live requote event
# with no live Quoted after it, on a case that is not closed. Only v_enquiry
# is dropped and created again, by name, from schema.sql; every other view -
# the 'location' compatibility view migrate-master left above all (the v24
# incident) - stays as it is. open_db has taken the restore point already.
def _to_v29(conn: sqlite3.Connection) -> list:
    import re

    from . import paths

    sql = paths.SCHEMA_SQL.read_text(encoding="utf-8")
    found = re.findall(r"CREATE VIEW\s+v_enquiry\s+AS.*?;", sql, re.S)
    if len(found) != 1:
        raise RuntimeError("schema.sql should hold one CREATE VIEW v_enquiry, found {}".format(len(found)))
    conn.execute("DROP VIEW IF EXISTS v_enquiry")
    conn.execute(found[0])
    return ["v_enquiry rebuilt: in_requote reads an open re-quote round (other views untouched)"]


# -------------------------------------------------------------------- v30 --
# Re-quote means re-quoted (2026-09-25): the Re-quoted button writes a
# requote event with status Quoted, and every live Quoted event counts as a
# quote whatever its kind (quoted_on, the follow-up round, quote_rounds);
# in_requote is left for an old requote event on New or Sourcing. Only
# v_enquiry is rebuilt, by name, from schema.sql - the same step as v29, so
# every other view (the 'location' compatibility view above all) stays.
# open_db has taken the restore point already.
def _to_v30(conn: sqlite3.Connection) -> list:
    _to_v29(conn)
    return ["v_enquiry rebuilt: every Quoted event is a quote, a requote event on Quoted "
            "closes its own round (other views untouched)"]


# -------------------------------------------------------------------- v31 --
# Cases handled by another of our offices (2026-09-25): enquiry.handled_by,
# the office's code (JQM, JJH, ...); NULL is the home office. One ALTER TABLE, nothing
# backfilled - Iris sets it on the cases she knows - then v_enquiry is
# rebuilt by name so it carries the column; every other view stays (the
# v24 lesson). open_db has taken the restore point already.
def _to_v31(conn: sqlite3.Connection) -> list:
    notes = []
    if "handled_by" not in _columns(conn, "enquiry"):
        conn.execute("ALTER TABLE enquiry ADD COLUMN handled_by TEXT")
        notes.append("enquiry.handled_by added (NULL = {})".format(branding.HOME_OFFICE))
    else:
        notes.append("enquiry.handled_by already on file")
    _to_v29(conn)
    notes.append("v_enquiry rebuilt to carry handled_by (other views untouched)")
    return notes


# -------------------------------------------------------------------- v32 --
# Quick notes on the case page (2026-09-25): the case_note table, created
# from schema.sql as v28 did for enquiry_cargo. Nothing backfilled, no view
# touched. open_db has taken the restore point already.
def _to_v32(conn: sqlite3.Connection) -> list:
    import re

    from . import db, paths

    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    if db._has_table(conn, "case_note"):
        return ["case_note already on file"]
    for statement in _statements(schema_sql, r"CREATE TABLE case_note\s*\(.*?\);"):
        conn.execute(statement)
    for statement in _statements(schema_sql, r"CREATE (?:UNIQUE )?INDEX \w+ ON case_note\s*\([^;]*?\);"):
        conn.execute(statement)
    return ["case_note table created (quick notes; nothing backfilled)"]


# -------------------------------------------------------------------- v33 --
# One mail is one OPP, with routes and cargo inside it (2026-09-28):
# enquiry_route (every case gets route A from its own lane, its
# result from the status), cargo lines as written with how often, how long,
# counted and routes, vendor requests with their routes, one shipment per
# won route (the shipment table is rebuilt: enquiry_id is no longer unique),
# the case's gross_kg and volume_cbm, v_route. A cargo line whose Deliver to
# differs from its case's destination becomes a route of its own. Request
# groups stay as they are. open_db has taken the restore point already.
V33_REPORT = []          # the Deliver to lines turned into routes, for the upgrade notes


def _to_v33(conn: sqlite3.Connection) -> list:
    import re

    from . import db, paths, routes

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    added = []
    for table, column, decl in (
            ("enquiry", "gross_kg", "REAL"), ("enquiry", "volume_cbm", "REAL"),
            ("enquiry_cargo", "dim_l", "REAL"), ("enquiry_cargo", "dim_w", "REAL"), ("enquiry_cargo", "dim_h", "REAL"),
            ("enquiry_cargo", "dims_unit", "TEXT CHECK (dims_unit IS NULL OR dims_unit IN ('cm','in','mm','m'))"),
            ("enquiry_cargo", "weight_value", "REAL"),
            ("enquiry_cargo", "weight_unit", "TEXT CHECK (weight_unit IS NULL OR weight_unit IN ('kg','lbs','t'))"),
            ("enquiry_cargo", "weight_basis", "TEXT NOT NULL DEFAULT 'total' CHECK (weight_basis IN ('total','each'))"),
            ("enquiry_cargo", "container_type", "TEXT"), ("enquiry_cargo", "container_qty", "INTEGER"),
            ("enquiry_cargo", "per_month", "REAL"), ("enquiry_cargo", "months", "REAL"),
            ("enquiry_cargo", "counted", "INTEGER NOT NULL DEFAULT 1"), ("enquiry_cargo", "route_ids", "TEXT"),
            ("vendor_request", "route_ids", "TEXT"),
            ("suggestion", "route_id", "INTEGER REFERENCES enquiry_route(id)")):
        if column not in _columns(conn, table):
            conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, column, decl))
            added.append("{}.{}".format(table, column))
    if added:
        notes.append("columns added: " + ", ".join(added))
    if not db._has_table(conn, "enquiry_route"):
        for statement in _statements(schema_sql, r"CREATE TABLE enquiry_route\s*\(.*?\);"):
            conn.execute(statement)
        for statement in _statements(schema_sql, r"CREATE (?:UNIQUE )?INDEX \w+ ON enquiry_route\s*\([^;]*?\);"):
            conn.execute(statement)
        notes.append("enquiry_route table created")
    # one shipment per won route: enquiry_id stops being unique (rebuild);
    # a file without the table (never past v23) gets it from schema.sql
    if not db._has_table(conn, "shipment"):
        for statement in _statements(schema_sql, r"CREATE TABLE shipment\s*\(.*?\);"):
            conn.execute(statement)
        notes.append("shipment table created")
    elif "route_id" not in _columns(conn, "shipment"):
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for view in _schema_view_names():
                conn.execute("DROP VIEW IF EXISTS {}".format(view))
            _rebuild_table(conn, "shipment", schema_sql)
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        notes.append("shipment rebuilt: route_id added, one shipment per won route")
    notes.extend(_add_settings(conn, (("service_scopes", json.dumps(routes.DEFAULT_SCOPES)),)))
    notes.append("views rebuilt: " + ", ".join(_rebuild_views(conn)))

    # ---- the data: route A for every case, results from the status
    repo = db.Repository(conn, source="migration")
    made = 0
    results = {"Won": 0, "Lost": 0}
    for case in conn.execute("SELECT * FROM v_enquiry ORDER BY id").fetchall():
        if conn.execute("SELECT 1 FROM enquiry_route WHERE enquiry_id = ?", (case["id"],)).fetchone():
            continue
        values = {k: case[k] for k in routes.LANE_FIELDS}
        if case["status"] in ("Won", "Lost"):
            values["result"] = case["status"]
            values["result_on"] = case["closed_on_any"]
            if case["status"] == "Lost":
                values["lost_reason"] = case["closed_reason"]
            results[case["status"]] += 1
        repo.add_route(case["id"], values, source="migration", sync=False)
        made += 1
    notes.append("route A added to {} case(s): {} Won, {} Lost from the status, the rest without a result".format(
        made, results["Won"], results["Lost"]))
    # ---- a cargo line delivering somewhere else is a route of its own
    del V33_REPORT[:]
    for line in conn.execute("SELECT l.*, e.opp_no, e.dest_alt, e.dest_loc FROM enquiry_cargo l "
                             "JOIN enquiry e ON e.id = l.enquiry_id WHERE l.voided = 0 AND l.route_ids IS NULL "
                             "AND COALESCE(TRIM(l.deliver_to), '') <> '' ORDER BY l.enquiry_id, l.seq").fetchall():
        deliver = " ".join(line["deliver_to"].split()).casefold()
        same = {" ".join((line[k] or "").split()).casefold() for k in ("dest_alt", "dest_loc")} - {""}
        # no destination on the case (OPP-1480: Vietnam, no place): the line's
        # Deliver to is where the case goes, not another route
        if not same or deliver in same:
            continue
        first = conn.execute("SELECT * FROM enquiry_route WHERE enquiry_id = ? AND voided = 0 ORDER BY seq LIMIT 1",
                             (line["enquiry_id"],)).fetchone()
        values = {k: first[k] for k in ("origin_country", "origin_loc", "origin_loc_id", "origin_alt",
                                        "dest_country", "service_mode", "incoterm")}
        values.update({"dest_alt": line["deliver_to"], "dest_loc": None, "dest_loc_id": None,
                       "notes": "from cargo line {} (Deliver to), migration v33".format(line["label"] or line["seq"])})
        route_id = repo.add_route(line["enquiry_id"], values, source="migration", sync=False, follow_case=False)
        repo.update("enquiry_cargo", line["id"], {"route_ids": str(route_id)})
        seq = conn.execute("SELECT seq FROM enquiry_route WHERE id = ?", (route_id,)).fetchone()["seq"]
        V33_REPORT.append("{} line {} -> route {}: {}".format(line["opp_no"], line["label"] or line["seq"],
                                                              routes.letter(seq), line["deliver_to"]))
    notes.append("{} cargo line(s) with their own Deliver to became routes{}".format(
        len(V33_REPORT), (": " + "; ".join(V33_REPORT)) if V33_REPORT else ""))
    # ---- cargo lines as written; shipments on route A
    for line in conn.execute("SELECT * FROM enquiry_cargo WHERE dims_unit IS NULL AND weight_value IS NULL").fetchall():
        repo.update("enquiry_cargo", line["id"], {
            "dim_l": line["length_cm"], "dim_w": line["width_cm"], "dim_h": line["height_cm"],
            "dims_unit": "cm" if line["length_cm"] is not None else None,
            "weight_value": line["weight_kg"], "weight_unit": "kg" if line["weight_kg"] is not None else None})
    shipped = 0
    for ship in conn.execute("SELECT id, enquiry_id FROM shipment WHERE route_id IS NULL").fetchall():
        first = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? ORDER BY seq LIMIT 1",
                             (ship["enquiry_id"],)).fetchone()
        if first is not None:
            repo.update("shipment", ship["id"], {"route_id": first["id"]})
            shipped += 1
    notes.append("{} shipment(s) set on route A".format(shipped))
    # ---- the totals: gross and volume as they stand, the rest recomputed
    filled = recomputed = 0
    for case in conn.execute("SELECT id, air_weight_kg, lcl_cbm, gross_kg, volume_cbm FROM enquiry "
                             "ORDER BY id").fetchall():
        if case["gross_kg"] is None and case["volume_cbm"] is None and \
                (case["air_weight_kg"] is not None or case["lcl_cbm"] is not None):
            repo.update("enquiry", case["id"], {"gross_kg": case["air_weight_kg"], "volume_cbm": case["lcl_cbm"]})
            filled += 1
        if repo.sync_cargo_totals(case["id"], typed=True):
            recomputed += 1
        repo.sync_route_a(case["id"])
    notes.append("gross_kg / volume_cbm filled on {} case(s) from air_weight_kg / lcl_cbm; totals "
                 "recomputed on {} case(s) (Air: chargeable weight; LCL: volume; other modes: both empty)".format(
                     filled, recomputed))
    return notes


# -------------------------------------------------------------------- v34 --
# Cargo lines keep what was read (batch 13b, 2026-09-29): the mail
# and its lines of text, the weight and volume as written, how an ambiguous
# weight was read, the checks answered; the totals a mail states get a table
# of their own (enquiry_cargo_stated), a check on the lines, never counted
# with them. Existing lines are not re-read: they have no source text, and
# their card shows the case's qty_raw instead.
def _to_v34(conn: sqlite3.Connection) -> list:
    import re

    from . import db, paths

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    if not db._has_table(conn, "enquiry_cargo_stated"):
        for statement in _statements(schema_sql, r"CREATE TABLE enquiry_cargo_stated\s*\(.*?\);"):
            conn.execute(statement)
        for statement in _statements(schema_sql, r"CREATE (?:UNIQUE )?INDEX \w+ ON enquiry_cargo_stated\s*\([^;]*?\);"):
            conn.execute(statement)
        notes.append("enquiry_cargo_stated table created")
    added = []
    for table, column, decl in (
            ("enquiry", "cargo_decided", "TEXT"),
            ("enquiry_cargo", "source_mail", "TEXT"), ("enquiry_cargo", "source_text", "TEXT"),
            ("enquiry_cargo", "weight_written", "TEXT"),
            ("enquiry_cargo", "weight_reading",
             "TEXT CHECK (weight_reading IS NULL OR weight_reading IN ('auto','chosen'))"),
            ("enquiry_cargo", "cbm_value", "REAL"),
            ("enquiry_cargo", "cbm_basis", "TEXT NOT NULL DEFAULT 'total' CHECK (cbm_basis IN ('total','each'))"),
            ("enquiry_cargo", "cbm_written", "TEXT"),
            ("enquiry_cargo", "stated_id", "INTEGER REFERENCES enquiry_cargo_stated(id)"),
            ("enquiry_cargo", "decided", "TEXT")):
        if column not in _columns(conn, table):
            conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, column, decl))
            added.append("{}.{}".format(table, column))
    if added:
        notes.append("columns added: " + ", ".join(added))
    notes.extend(_add_settings(conn, (("cargo_limits", json.dumps(_V34_LIMITS)),)))
    notes.append("views rebuilt: " + ", ".join(_rebuild_views(conn)))
    return notes


_V34_LIMITS = {"density_min": 30, "density_max": 3000, "air_max_cbm": 30, "air_max_kg": 20000,
               "lcl_max_cbm": 25, "lcl_max_kg": 15000, "fcl_max_kg_20": 20000, "fcl_max_kg_40": 25000,
               "tolerance_pct": 10}


# -------------------------------------------------------------------- v35 --
# Cargo is lines only (batch 15b, 2026-10-01). enquiry_cargo is rebuilt so a
# line may come from the migration (source 'migration') and carry a weight
# per container (weight_basis 'container'); the US chassis limit is a new
# setting. No data moves here: migrate-cargo-lines makes the lines, after
# checking that every number stays the same.
def _to_v35(conn: sqlite3.Connection) -> list:
    import re

    from . import paths

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'enquiry_cargo'").fetchone()
    if table_sql is not None and ("'migration'" not in _check_values(table_sql[0], "source")
                                  or "'container'" not in _check_values(table_sql[0], "weight_basis")):
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for name in _statements(schema_sql, r"CREATE VIEW\s+(\w+)\s+AS"):
                conn.execute("DROP VIEW IF EXISTS {}".format(name))
            indexes = _rebuild_table(conn, "enquiry_cargo", schema_sql)
            notes.append("enquiry_cargo rebuilt (source may be 'migration', weight_basis 'container'), "
                         "indexes: {}".format(", ".join(indexes)))
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    notes.extend(_add_settings(conn, (("us_chassis_max_kg", "17200"),)))
    notes.append("views rebuilt: " + ", ".join(_rebuild_views(conn)))
    return notes


def _to_v36(conn: sqlite3.Connection) -> list:
    """Batch 20: LATAM is the latam_countries list of Settings (view
    v_latam_country), not the country table's region - v_enquiry and v_route
    get latam_lane and latam_agent, v_party_stats latam and latam_agent."""
    return ["views rebuilt: " + ", ".join(_rebuild_views(conn))]


def _to_v37(conn: sqlite3.Connection) -> list:
    """AL1, case numbers in mail subjects: status_event may be kind 'ack'
    (the acknowledgement), suggestion kind 'case_number' with named_cases,
    enquiry_thread kind 'message' (one mail attached to a case). And a case
    whose source subject carries a case number gets the thread key the new
    5.1 rule gives (the number is no longer part of it), through the
    repository so change_log has it."""
    import re

    from . import db, parsing, paths

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    wanted = (("status_event", "kind", "'ack'"), ("suggestion", "kind", "'case_number'"),
              ("enquiry_thread", "kind", "'message'"))
    rebuild = []
    for table, column, value in wanted:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
        if row is not None and (value not in _check_values(row[0], column)
                                or (table == "suggestion" and "named_cases" not in _columns(conn, table))):
            rebuild.append(table)
    if rebuild:
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for name in _statements(schema_sql, r"CREATE VIEW\s+(\w+)\s+AS"):
                conn.execute("DROP VIEW IF EXISTS {}".format(name))
            for table in rebuild:
                indexes = _rebuild_table(conn, table, schema_sql)
                notes.append("{} rebuilt, indexes: {}".format(table, ", ".join(indexes) or "-"))
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    repo = db.Repository(conn, source="migration")
    changed = []
    for row in conn.execute("SELECT id, opp_no, source_subject, thread_key FROM enquiry "
                            "WHERE source_subject IS NOT NULL ORDER BY id").fetchall():
        if not parsing.case_numbers(row["source_subject"]):
            continue
        new = parsing.subject_thread_key(row["source_subject"]) or None
        if row["thread_key"] and new and new != row["thread_key"]:
            repo.update("enquiry", row["id"], {"thread_key": new})
            changed.append(row["opp_no"])
    notes.append("thread keys recomputed for a case number in the subject: {}{}".format(
        len(changed), " ({})".format(", ".join(changed)) if changed else ""))
    notes.append("views rebuilt: " + ", ".join(_rebuild_views(conn)))
    return notes


_V38_SETTINGS = (
    ("closed_reasons_cancelled",
     '["Shipment cancelled","Order not confirmed","Postponed with no new date","Other"]'),
)


def _to_v38(conn: sqlite3.Connection) -> list:
    """C1, Cancelled: a closing status of its own - the shipment
    did not go ahead. enquiry.status, status_event.status and
    enquiry_route.result accept it (the three tables rebuilt, only the views
    schema.sql owns dropped and made again, so the location view stays);
    the new list closed_reasons_cancelled. Every case Lost with the reason
    Shipment cancelled becomes Cancelled - the case, the Lost event it is
    closed by and its routes lost for that reason - through the repository,
    so change_log has each value. Then Shipment cancelled leaves the Lost
    list, unless a Lost case or route still carries it."""
    import json
    import re

    from . import db, paths, status as status_mod

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    rebuild = []
    for table in ("enquiry", "status_event", "enquiry_route"):
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
        # none of the three names Cancelled anywhere else in its definition
        if row is not None and "'Cancelled'" not in row[0]:
            rebuild.append(table)
    if rebuild:
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for name in _statements(schema_sql, r"CREATE VIEW\s+(\w+)\s+AS"):
                conn.execute("DROP VIEW IF EXISTS {}".format(name))
            for table in rebuild:
                indexes = _rebuild_table(conn, table, schema_sql)
                notes.append("{} rebuilt (Cancelled allowed), indexes: {}".format(table, ", ".join(indexes) or "-"))
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    notes.extend(_add_settings(conn, _V38_SETTINGS))
    notes.append("views rebuilt: " + ", ".join(_rebuild_views(conn)))

    repo = db.Repository(conn, source="migration")
    cancelled = status_mod.SHIPMENT_CANCELLED
    converted = []
    for case in conn.execute(
            "SELECT id, opp_no FROM enquiry WHERE status = 'Lost' "
            "AND lower(trim(COALESCE(closed_reason, ''))) = ? ORDER BY opp_no",
            (cancelled.casefold(),)).fetchall():
        latest = repo.latest_event(case["id"])
        if latest is not None and latest["status"] == "Lost" and latest["kind"] == "status":
            repo.update("status_event", latest["id"], {"status": "Cancelled"})
        repo.update("enquiry", case["id"], {"status": "Cancelled"})
        if db._has_table(conn, "enquiry_route"):
            for route in conn.execute(
                    "SELECT id FROM enquiry_route WHERE enquiry_id = ? AND result = 'Lost' "
                    "AND lower(trim(COALESCE(lost_reason, ''))) = ?", (case["id"], cancelled.casefold())).fetchall():
                repo.update("enquiry_route", route["id"], {"result": "Cancelled"})
        converted.append(case["opp_no"])
    notes.append("Lost with '{}' made Cancelled: {}{}".format(
        cancelled, len(converted), " ({})".format(", ".join(converted)) if converted else ""))

    # the reason leaves the Lost list - unless something Lost still carries it
    still = [r["opp_no"] for r in conn.execute(
        "SELECT DISTINCT e.opp_no FROM enquiry e WHERE (e.status = 'Lost' AND lower(trim(COALESCE(e.closed_reason, ''))) = ?) "
        "OR EXISTS (SELECT 1 FROM enquiry_route r WHERE r.enquiry_id = e.id AND r.voided = 0 AND r.result = 'Lost' "
        "AND lower(trim(COALESCE(r.lost_reason, ''))) = ?) ORDER BY e.opp_no",
        (cancelled.casefold(), cancelled.casefold()))] if db._has_table(conn, "enquiry_route") else []
    row = conn.execute("SELECT value FROM setting WHERE key = 'closed_reasons_lost'").fetchone()
    lost = json.loads(row[0]) if row is not None and row[0] else []
    if any(str(r).strip().casefold() == cancelled.casefold() for r in lost):
        if still:
            notes.append("'{}' kept on the Lost reasons: still on Lost {}".format(cancelled, ", ".join(still)))
        else:
            db.set_setting(conn, "closed_reasons_lost",
                           [r for r in lost if str(r).strip().casefold() != cancelled.casefold()])
            notes.append("'{}' taken off the Lost reasons".format(cancelled))
    return notes


# -------------------------------------------------------------------- v39 --
# Batch 23 (2026-10-02): place pickers, a Dest. location and a Port of
# discharge of up to four places each (enquiry_route_place), Packing from a
# Settings list (enquiry_cargo.pack_unit, setting packing_units), and a
# route added to a closed case following the case. The data, through the
# repository (change_log source migration): packing words mapped to the
# list; a pod naming exactly one master place becomes that place; every
# dest_loc_id becomes the first destination; the open routes of Lost and
# Cancelled cases take the case's result. What it did and what it only
# lists is in V39_REPORT and the upgrade notes. open_db has taken the
# restore point already.
V39_REPORT = {}


def _v39_route_label(row) -> str:
    from . import routes
    return "{} {}".format(row["opp_no"], routes.letter(row["seq"]))


def _to_v39(conn: sqlite3.Connection) -> list:
    import re

    from . import db, packing, paths, places, routes

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    if not db._has_table(conn, "enquiry_route_place"):
        for statement in _statements(schema_sql, r"CREATE TABLE enquiry_route_place\s*\(.*?\);"):
            conn.execute(statement)
        for statement in _statements(schema_sql, r"CREATE (?:UNIQUE )?INDEX \w+ ON enquiry_route_place\s*\([^;]*?\);"):
            conn.execute(statement)
        notes.append("enquiry_route_place table created")
    if db._has_table(conn, "enquiry_cargo") and "pack_unit" not in _columns(conn, "enquiry_cargo"):
        conn.execute("ALTER TABLE enquiry_cargo ADD COLUMN pack_unit TEXT")
        notes.append("columns added: enquiry_cargo.pack_unit")
    notes.extend(_add_settings(conn, (("packing_units", json.dumps(packing.DEFAULT_UNITS)),)))

    repo = db.Repository(conn, source="migration")
    report = {"packing_mapped": {}, "packing_unmapped": {}, "pod_places": [], "pod_text": [],
              "wrong": {"not_a_port": [], "or": [], "same_as_dest": [], "dest_is_port": []},
              "dest_first": 0, "followed": []}

    # ---- a. Packing: the words on file mapped to the list, kept as written
    units = db.get_setting_list(conn, "packing_units") or list(packing.DEFAULT_UNITS)
    if db._has_table(conn, "enquiry_cargo"):
        for line in conn.execute("SELECT id, pack_word, pack_unit FROM enquiry_cargo "
                                 "WHERE TRIM(COALESCE(pack_word, '')) <> '' ORDER BY id").fetchall():
            word = line["pack_word"].strip()
            unit = line["pack_unit"] or packing.unit_for(word, units)
            if unit:
                if not line["pack_unit"]:
                    repo.update("enquiry_cargo", line["id"], {"pack_unit": unit})
                mapped = report["packing_mapped"].setdefault(word, [unit, 0])
                mapped[1] += 1
            else:
                report["packing_unmapped"][word] = report["packing_unmapped"].get(word, 0) + 1

    if db._has_table(conn, "enquiry_route"):
        # ---- b. a Port of discharge naming exactly one master place becomes it
        for route in conn.execute(
                "SELECT r.id, r.seq, r.pod, r.voided, e.opp_no FROM enquiry_route r JOIN enquiry e ON e.id = r.enquiry_id "
                "WHERE TRIM(COALESCE(r.pod, '')) <> '' ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER), r.seq").fetchall():
            if conn.execute("SELECT 1 FROM enquiry_route_place WHERE route_id = ? AND side = 'pod' AND voided = 0",
                            (route["id"],)).fetchone():
                continue                                  # a second run: done already
            found = places.resolve_one(conn, route["pod"])
            if found is not None:
                repo.update("enquiry_route", route["id"], {"pod_places": [found["id"]]})
                report["pod_places"].append("{} '{}' -> {}".format(_v39_route_label(route), route["pod"], found["key"]))
            else:
                report["pod_text"].append("{} '{}'".format(_v39_route_label(route), route["pod"]))
        # ---- c. every route's dest_loc_id is the first destination
        for route in conn.execute(
                "SELECT r.id, r.dest_loc_id FROM enquiry_route r WHERE r.dest_loc_id IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM enquiry_route_place p WHERE p.route_id = r.id AND p.side = 'dest' AND p.voided = 0) "
                "ORDER BY r.id").fetchall():
            if repo._set_route_places(route["id"], "dest", [route["dest_loc_id"]]):
                report["dest_first"] += 1
        # ---- the routes that look wrong under the new rule: listed, not changed
        for case in conn.execute("SELECT DISTINCT e.id, e.opp_no FROM enquiry e JOIN enquiry_route r ON r.enquiry_id = e.id "
                                 "WHERE r.voided = 0 ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER)").fetchall():
            for route in routes.routes_of(conn, case["id"]):
                route["opp_no"] = case["opp_no"]
                for rule, text in _v39_wrong(route):
                    report["wrong"][rule].append(text)
        # ---- 7. a live route with no result on a Lost or Cancelled case follows it
        for route in conn.execute(
                "SELECT r.id, r.seq, e.id AS enquiry_id, e.opp_no, e.status, e.closed_reason FROM enquiry_route r "
                "JOIN enquiry e ON e.id = r.enquiry_id WHERE r.voided = 0 AND TRIM(COALESCE(r.result, '')) = '' "
                "AND e.status IN ('Lost', 'Cancelled') ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER), r.seq").fetchall():
            on = repo.closing_date(route["enquiry_id"], route["status"])
            repo.update("enquiry_route", route["id"], {"result": route["status"], "result_on": on,
                                                       "lost_reason": route["closed_reason"]})
            report["followed"].append("{}: {}{}, {}".format(
                _v39_route_label(route), route["status"],
                ", " + route["closed_reason"] if route["closed_reason"] else "", on or "no date"))

    V39_REPORT.clear()
    V39_REPORT.update(report)
    notes.extend(_v39_notes(report))
    return notes


def _v39_wrong(route) -> list:
    """[(rule, text)] for a live route that looks wrong under batch 23's
    rule: a POD that is not a port (an airport on Air), a POD with OR in
    it, a POD that is the Dest. location itself, a Dest. location that is
    a port (an airport on Air) with a POD as well."""
    import re

    from . import places

    ports, text = route.get("pod_places") or [], (route.get("pod") or "").strip()
    if not ports and not text:
        return []
    air = route.get("service_mode") == "Air"
    fits = places.is_airport if air else places.is_port
    word = "an airport" if air else "a port"
    label = "{} {}".format(route["opp_no"], route["letter"])
    shown = " / ".join(p["key"] for p in ports) if ports else text
    out = []
    if not ports or not all(fits(p) for p in ports):
        out.append(("not_a_port", "{} ({}: not {})".format(label, shown, word)))
    if re.search(r"\sor\s", text, re.IGNORECASE) and not ports:
        out.append(("or", "{} ({})".format(label, shown)))
    dests = route.get("dest_places") or []
    dest_ids = {d["id"] for d in dests}
    dest_keys = {(route.get("dest_loc") or "").strip().casefold()} - {""}
    if any(p["id"] in dest_ids for p in ports) or (not ports and text.casefold() in dest_keys):
        out.append(("same_as_dest", "{} ({})".format(label, shown)))
    if dests and fits(dests[0]):
        out.append(("dest_is_port", "{} ({} and POD {})".format(label, dests[0]["key"], shown)))
    return out


def _v39_notes(report) -> list:
    def words(table):
        return "; ".join("'{}' {}{}".format(word, value[1], " -> " + value[0]) if isinstance(value, list)
                         else "'{}' {}".format(word, value)
                         for word, value in sorted(table.items(), key=lambda kv: (
                             -(kv[1][1] if isinstance(kv[1], list) else kv[1]), kv[0].casefold())))
    wrong = report["wrong"]
    lines = [
        "Packing mapped to the list: {} word(s), {} line(s){}".format(
            len(report["packing_mapped"]), sum(v[1] for v in report["packing_mapped"].values()),
            ": " + words(report["packing_mapped"]) if report["packing_mapped"] else ""),
        "Packing not mapped (the word kept, the dropdown empty): {} word(s), {} line(s){}".format(
            len(report["packing_unmapped"]), sum(report["packing_unmapped"].values()),
            ": " + words(report["packing_unmapped"]) if report["packing_unmapped"] else ""),
        "Port of discharge turned into a place: {}{}".format(
            len(report["pod_places"]), " ({})".format("; ".join(report["pod_places"])) if report["pod_places"] else ""),
        "Port of discharge kept as text: {}{}".format(
            len(report["pod_text"]), " ({})".format("; ".join(report["pod_text"])) if report["pod_text"] else ""),
        "Dest. location: dest_loc_id made the first place on {} route(s); a dest_loc without a master place "
        "stays as it is".format(report["dest_first"]),
    ]
    for rule, name in (("not_a_port", "POD not a port"), ("or", "POD with OR"),
                       ("same_as_dest", "POD the same place as the Dest. location"),
                       ("dest_is_port", "Dest. location a port with a POD as well")):
        lines.append("Looks wrong, not changed - {}: {}{}".format(
            name, len(wrong[rule]), " ({})".format("; ".join(wrong[rule])) if wrong[rule] else ""))
    lines.append("Open routes of Lost / Cancelled cases given the case's result: {}{}".format(
        len(report["followed"]), " ({})".format("; ".join(report["followed"])) if report["followed"] else ""))
    return lines


# -------------------------------------------------------------------- v40 --
def _to_v40(conn: sqlite3.Connection) -> list:
    """E1, edit a suggestion before accepting it: the suggestion keeps what
    the app suggested and gains what she recorded - decided_as,
    decided_values (JSON), edited, event_ref. Added columns only; the rows
    already decided keep them empty (nothing to say what was written)."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(suggestion)")}
    notes = []
    added = []
    for name, ddl in (
            ("decided_as", "decided_as TEXT CHECK (decided_as IS NULL OR decided_as IN ({}))".format(
                ",".join("'{}'".format(v) for v in ("quoted", "requote", "won", "lost", "cancelled", "note",
                                                    "history", "milestone", "start", "vendor", "attach",
                                                    "dismissed")))),
            ("decided_values", "decided_values TEXT"),
            ("edited", "edited INTEGER NOT NULL DEFAULT 0"),
            ("event_ref", "event_ref TEXT")):
        if name not in have:
            conn.execute("ALTER TABLE suggestion ADD COLUMN " + ddl)
            added.append(name)
    if added:
        notes.append("suggestion: {} added".format(", ".join(added)))
    return notes



# -------------------------------------------------------------------- v41 --
def _to_v41(conn: sqlite3.Connection) -> list:
    """E2, Strike out undoes the action that wrote the event: change_log
    gains action_id (every row one user action writes carries the same
    id) and an index on it. The rows already there keep it empty: nothing
    says which of them went together."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(change_log)")}
    notes = []
    if "action_id" not in have:
        conn.execute("ALTER TABLE change_log ADD COLUMN action_id INTEGER")
        notes.append("change_log: action_id added (rows before v41 keep it empty)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_change_log_action ON change_log (action_id)")
    return notes


# -------------------------------------------------------------------- v42 --
def _to_v42(conn: sqlite3.Connection) -> list:
    """RD1, the Rate Desk reads routes: master_location gains no_parent (a
    gateway with no parent city on purpose), and the master contract views
    are rebuilt from schema.sql - v_master_route_lane, v_master_route_place
    and v_master_enquiry_cargo are new, v_master_location gains no_parent
    and v_master_vendor_request route_ids at the end. Contract version stays
    1: nothing is removed or moved. Every row keeps no_parent 0, so the
    gateways without a parent still show in check until she decides."""
    import re

    from . import paths

    notes = []
    if "no_parent" not in _columns(conn, "master_location"):
        conn.execute("ALTER TABLE master_location ADD COLUMN no_parent INTEGER NOT NULL DEFAULT 0")
        notes.append("master_location: no_parent added (0 on every row)")
    sql = paths.SCHEMA_SQL.read_text(encoding="utf-8")
    rebuilt = []
    for statement in re.findall(r"CREATE VIEW\s+v_master_\w+\s+AS.*?;", sql, re.S):
        name = re.match(r"CREATE VIEW\s+(\w+)", statement).group(1)
        conn.execute("DROP VIEW IF EXISTS {}".format(name))
        conn.execute(statement)
        rebuilt.append(name)
    notes.append("contract views rebuilt: " + ", ".join(rebuilt))
    return notes


# -------------------------------------------------------------------- v43 --
def _to_v43(conn: sqlite3.Connection) -> list:
    """W1b, the team site's actions taken in: site_action (one row per
    action, applied or refused, never applied twice) and site_note (a site
    note with a case and the quick note it became). status_event is rebuilt
    so a follow-up may go out by whatsapp, teams or other, and an event
    may come from the 'web' (logged on the team site). Every row is kept."""
    import re

    from . import paths

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'status_event'").fetchone()
    if row is not None and ("'whatsapp'" not in _check_values(row[0], "channel")
                            or "'web'" not in _check_values(row[0], "source")):
        before = conn.execute("SELECT COUNT(*) FROM status_event").fetchone()[0]
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for name in _statements(schema_sql, r"CREATE VIEW\s+(\w+)\s+AS"):
                conn.execute("DROP VIEW IF EXISTS {}".format(name))
            indexes = _rebuild_table(conn, "status_event", schema_sql)
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        after = conn.execute("SELECT COUNT(*) FROM status_event").fetchone()[0]
        if after != before:
            raise RuntimeError("status_event rebuild kept {} of {} rows".format(after, before))
        notes.append("status_event rebuilt ({} rows; channel whatsapp / teams / other, source web), indexes: {}".format(
            after, ", ".join(indexes) or "-"))
        notes.append("views rebuilt: " + ", ".join(_rebuild_views(conn)))
    for table in ("site_action", "site_note"):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
            conn.execute(re.search(r"CREATE TABLE {}\s*\(.*?\);".format(table), schema_sql, re.S).group(0))
            for statement in _statements(schema_sql, r"CREATE INDEX \w+ ON {}\s*\([^;]*?\);".format(table)):
                conn.execute(statement)
            notes.append("{} table created".format(table))
    return notes


#: W2a: who copies the team on a rate request mail from the team site
RATE_REQUEST_CC = branding.TEAM_CC


def _to_v44(conn: sqlite3.Connection) -> list:
    """W2a, Ask vendors on the team site: vendor_request.source and
    suggestion.source accept 'web' (a rate request logged on the site; the
    site's Won / Lost suggestions of phase 2b). Both tables are rebuilt as
    v43 rebuilt status_event - every row kept, the views rebuilt - and the
    setting rate_request_cc is added."""
    import re

    from . import paths

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    for table in ("vendor_request", "suggestion"):
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
        if row is None or "'web'" in _check_values(row[0], "source"):
            continue
        before = conn.execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0]
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for name in _statements(schema_sql, r"CREATE VIEW\s+(\w+)\s+AS"):
                conn.execute("DROP VIEW IF EXISTS {}".format(name))
            indexes = _rebuild_table(conn, table, schema_sql)
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        after = conn.execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0]
        if after != before:
            raise RuntimeError("{} rebuild kept {} of {} rows".format(table, after, before))
        notes.append("{} rebuilt ({} rows; source web), indexes: {}".format(table, after, ", ".join(indexes) or "-"))
    if notes:
        notes.append("views rebuilt: " + ", ".join(_rebuild_views(conn)))
    notes.extend(_add_settings(conn, (("rate_request_cc", RATE_REQUEST_CC),)))
    return notes


def _to_v45(conn: sqlite3.Connection) -> list:
    """Batch 17m: v_enquiry.teu from the case's FCL cargo lines (the helper
    view v_cargo_teu_line), so a case with two container types, or a type
    written as the mail had it, has its TEU. Views only."""
    return ["views rebuilt: " + ", ".join(_rebuild_views(conn))]


def _to_v46(conn: sqlite3.Connection) -> list:
    """TK1: the mode Domestic becomes Truck (trucking inside the US and
    across a border; whether a move is domestic is its direction, from the
    two countries). Every case and route whose mode is Domestic is set to
    Truck through the repository - change_log has each field, old Domestic
    new Truck, source migration, all under one action - with the CHECKs
    off for that write only; then enquiry and enquiry_route are rebuilt
    from schema.sql as v43 and v44 rebuilt theirs: every row kept (counted
    before and after), indexes and views rebuilt, foreign keys checked."""
    import re

    from . import db, paths

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    tables = [t for t in ("enquiry", "enquiry_route") if db._has_table(conn, t)]
    stale = [t for t in tables if "'Domestic'" in (conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (t,)).fetchone()[0] or "")]
    if not stale:
        return notes
    before = {t: conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in tables}
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA ignore_check_constraints = ON")
    try:
        repo = db.Repository(conn, source="migration")
        changed = []
        for table in tables:
            label = "e.opp_no" if table == "enquiry" else "e.opp_no || ' route ' || r.seq"
            source = "enquiry e" if table == "enquiry" else "enquiry_route r JOIN enquiry e ON e.id = r.enquiry_id"
            key = "e.id" if table == "enquiry" else "r.id"
            for row in conn.execute("SELECT {k} AS id, {l} AS label FROM {s} WHERE {a}.service_mode = 'Domestic' "
                                    "ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER), {k}".format(
                                        k=key, l=label, s=source, a=key.split(".")[0])).fetchall():
                if conn.execute("SELECT service_mode FROM {} WHERE id = ?".format(table),
                                (row["id"],)).fetchone()[0] != "Domestic":
                    continue                               # route A followed its case already
                repo.update(table, row["id"], {"service_mode": "Truck"})
                changed.append(row["label"])
        repo.commit()
        for name in _statements(schema_sql, r"CREATE VIEW\s+(\w+)\s+AS"):
            conn.execute("DROP VIEW IF EXISTS {}".format(name))
        indexes = []
        for table in tables:
            indexes += _rebuild_table(conn, table, schema_sql, keep_domestic=False)
        conn.commit()
    finally:
        conn.execute("PRAGMA ignore_check_constraints = OFF")
        conn.execute("PRAGMA foreign_keys = ON")
    after = {t: conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in tables}
    if after != before:
        raise RuntimeError("the v46 rebuild kept {} of {} rows".format(after, before))
    broken = conn.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        raise RuntimeError("the v46 rebuild left {} foreign key problem(s)".format(len(broken)))
    notes.append("Domestic -> Truck: {}".format(", ".join(changed) or "none"))
    notes.append("rebuilt ({}), indexes: {}".format(
        ", ".join("{} {} rows".format(t, after[t]) for t in tables), ", ".join(indexes) or "-"))
    notes.append("views rebuilt: " + ", ".join(_rebuild_views(conn)))
    return notes


def _to_v47(conn: sqlite3.Connection) -> list:
    """DC1: v_enquiry.latam (and v_route's) is also 'Y' when the requester
    is a Direct Customer whose country is in v_latam_country, as it is for
    an Agent there; latam_agent unchanged. Views only, as v45; no row
    changes. open_db has taken the restore point already."""
    return ["views rebuilt: " + ", ".join(_rebuild_views(conn))]


def _to_v48(conn: sqlite3.Connection) -> list:
    """SA1: two modes of their own. 'Sea-Air' (a real sea-air move; Multi-mode
    keeps meaning 'quote air and ocean side by side') and 'Cross-border
    Truck' (a truck that itself crosses a border; Truck stays trucking inside
    one country) join the service_mode CHECK of enquiry and enquiry_route,
    and the shipment kinds 'sea_air' and 'cross_border_truck' join
    shipment.mode's. The three tables are rebuilt from schema.sql as v46
    rebuilt two (every row counted before and after, indexes and views
    rebuilt, foreign keys checked); a table that already allows them is left
    alone, so the step can run twice. Then the five cases Iris
    confirmed on 6 and 9 Nov get their mode through the repository (source
    migration, one action; route A follows the case), each only while it
    still has the mode she corrected - a case not on file, or changed since,
    is left as it is and named in the notes."""
    import re

    from . import db, paths

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    marks = (("enquiry", "'Sea-Air'"), ("enquiry_route", "'Sea-Air'"), ("shipment", "'sea_air'"))
    tables = [t for t, _ in marks if db._has_table(conn, t)]
    stale = [t for t, mark in marks if t in tables and mark not in (conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (t,)).fetchone()[0] or "")]
    if stale:
        before = {t: conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in stale}
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for name in _statements(schema_sql, r"CREATE VIEW\s+(\w+)\s+AS"):
                conn.execute("DROP VIEW IF EXISTS {}".format(name))
            indexes = []
            for table in stale:
                indexes += _rebuild_table(conn, table, schema_sql)
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        after = {t: conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in stale}
        if after != before:
            raise RuntimeError("the v48 rebuild kept {} of {} rows".format(after, before))
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError("the v48 rebuild left {} foreign key problem(s)".format(len(broken)))
        notes.append("rebuilt ({}), indexes: {}".format(
            ", ".join("{} {} rows".format(t, after[t]) for t in stale), ", ".join(indexes) or "-"))
        notes.append("views rebuilt: " + ", ".join(_rebuild_views(conn)))
    else:
        notes.append("the mode CHECKs already allow Sea-Air and Cross-border Truck")
    if "enquiry" in tables:
        repo = db.Repository(conn, source="migration")
        done, already, left = [], [], []
        for opp_no, was, now in _V48_CASES:
            row = conn.execute("SELECT id, service_mode FROM enquiry WHERE opp_no = ?", (opp_no,)).fetchone()
            if row is None:
                continue
            if row["service_mode"] == now:
                already.append(opp_no)                     # by hand, or a second run
                continue
            if row["service_mode"] != was:
                left.append("{} ({}, not {})".format(opp_no, row["service_mode"] or "no mode", was))
                continue
            repo.update("enquiry", row["id"], {"service_mode": now})
            done.append("{} {} -> {}".format(opp_no, was, now))
        repo.commit()
        if done:
            notes.append("modes set: " + "; ".join(done))
        if already:
            notes.append("modes already right: " + ", ".join(already))
        if left:
            notes.append("left as they are (changed since 9 Nov): " + "; ".join(left))
    return notes


#: SA1: (case, the mode it has, the mode it gets), confirmed by Iris
#: on 6 and 9 Nov 2026.
_V48_CASES = (
    ("OPP-1554", "Multi-mode", "Sea-Air"),
    ("OPP-1258", "Not specified", "Cross-border Truck"),
    ("OPP-1410", "Truck", "Cross-border Truck"),
    ("OPP-1508", "Truck", "RORO"),                 # a dump truck shipped RORO, Tacoma to Alexandria
    ("OPP-1562", "Truck", "Air"),                  # bonded trucking LAX to GDL airport: air cargo in transit
)


def _to_v49(conn: sqlite3.Connection) -> list:
    """SC1: enquiry.latam_choice (LATAM set by hand under More: NULL
    automatic, 'Y' or 'N'), added when it is not there, and the views
    rebuilt so v_enquiry.latam and v_route.latam give her choice first.
    Then, through the repository (change_log, source migration, one
    action), the three parties Iris named on 10 Nov go from Agent to
    Direct Customer - nothing else about them - each only while it is
    still an Agent (a second run finds them done). Settings' Lost reasons
    gain "Customer chose the other mode" (before Other) when it is not
    there. open_db has taken the restore point already."""
    from . import db, status

    notes = []
    if "latam_choice" not in _columns(conn, "enquiry"):
        conn.execute("ALTER TABLE enquiry ADD COLUMN latam_choice TEXT "
                     "CHECK (latam_choice IS NULL OR latam_choice IN ('Y','N'))")
        notes.append("enquiry.latam_choice added")
    notes.append("views rebuilt: " + ", ".join(_rebuild_views(conn)))
    repo = db.Repository(conn, source="migration")
    done, already = [], []
    for key in _V49_DIRECT_CUSTOMERS:
        for row in conn.execute("SELECT id, key, type FROM party WHERE lower(trim(key)) = ? "
                                "AND merged_into IS NULL", (key.casefold(),)).fetchall():
            if row["type"] == "Agent":
                repo.update("party", row["id"], {"type": "Direct Customer"})
                done.append(row["key"])
            else:
                already.append("{} ({})".format(row["key"], row["type"]))
    repo.commit()
    if done:
        notes.append("party type Agent -> Direct Customer: " + ", ".join(done))
    if already:
        notes.append("left as they are: " + ", ".join(already))
    stored = db.get_setting(conn, status.REASON_SETTING["Lost"], None)
    if stored:
        try:
            reasons = json.loads(stored)
        except ValueError:
            reasons = None
        if isinstance(reasons, list) and status.OTHER_MODE_REASON.casefold() not in {
                str(r).casefold() for r in reasons}:
            at = next((i for i, r in enumerate(reasons) if str(r).casefold() == "other"), len(reasons))
            reasons.insert(at, status.OTHER_MODE_REASON)
            db.set_setting(conn, status.REASON_SETTING["Lost"], reasons)
            notes.append("Lost reasons: {} added".format(status.OTHER_MODE_REASON))
    return notes


#: SC1: the parties Iris moved from Agent to Direct Customer on 10 Nov 2026
_V49_DIRECT_CUSTOMERS = ("Briskline Cargo", "Summit Cargo", "Nova Worldwide")

#: FX5's tag suggestions were note rows whose note began with this
_V50_TAG_NOTE = "Add special handling: "


def _to_v50(conn: sqlite3.Connection) -> list:
    """D7a: enquiry_cargo gains hs_code, cargo_value and cargo_value_cur
    (added when not there). suggestion is rebuilt as v43 rebuilt
    status_event - every row kept, counted before and after, the views
    rebuilt - so kind may be 'tag' (FX5's Non-stackable suggestion) and
    'edit' (D7b). Then every row FX5 wrote as a note whose note starts with
    'Add special handling: ', in any state, becomes kind 'tag' with note =
    the tag, its state, dates and evidence kept, through the repository
    (change_log, source migration). open_db has taken the restore point."""
    import re

    from . import db, paths

    notes = []
    for column, kind in (("hs_code", "TEXT"), ("cargo_value", "REAL"), ("cargo_value_cur", "TEXT")):
        if column not in _columns(conn, "enquiry_cargo"):
            conn.execute("ALTER TABLE enquiry_cargo ADD COLUMN {} {}".format(column, kind))
            notes.append("enquiry_cargo.{} added".format(column))
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'suggestion'").fetchone()
    if row is not None and "'tag'" not in _check_values(row[0], "kind"):
        before = conn.execute("SELECT COUNT(*) FROM suggestion").fetchone()[0]
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for name in _statements(schema_sql, r"CREATE VIEW\s+(\w+)\s+AS"):
                conn.execute("DROP VIEW IF EXISTS {}".format(name))
            indexes = _rebuild_table(conn, "suggestion", schema_sql)
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        after = conn.execute("SELECT COUNT(*) FROM suggestion").fetchone()[0]
        if after != before:
            raise RuntimeError("suggestion rebuild kept {} of {} rows".format(after, before))
        notes.append("suggestion rebuilt ({} rows before, {} after; kind tag and edit), indexes: {}".format(
            before, after, ", ".join(indexes) or "-"))
        notes.append("views rebuilt: " + ", ".join(_rebuild_views(conn)))
    repo = db.Repository(conn, source="migration")
    moved = []
    for s in conn.execute("SELECT id, opp_no, state, note FROM suggestion WHERE kind = 'note' AND substr(note, 1, ?) = ? "
                          "ORDER BY id", (len(_V50_TAG_NOTE), _V50_TAG_NOTE)).fetchall():
        repo.update("suggestion", s["id"], {"kind": "tag", "note": s["note"][len(_V50_TAG_NOTE):]})
        moved.append("{} {} ({})".format(s["opp_no"], s["note"][len(_V50_TAG_NOTE):], s["state"]))
    repo.commit()
    notes.append("tag suggestions moved from note rows: {}{}".format(len(moved), ": " + ", ".join(moved) if moved else ""))
    # the files official-places reads (and D7b checks a pick against): the
    # ones fill-coordinates read, when they are there; never overwritten
    unlocode, airports = paths.DATA_DIR / "raw" / "unlocode", paths.DATA_DIR / "raw" / "airports.csv"
    notes.extend(_add_settings(conn, (("official_unlocode_path", str(unlocode) if unlocode.exists() else ""),
                                      ("official_airports_path", str(airports) if airports.exists() else ""))))
    return notes


def _to_v51(conn: sqlite3.Connection) -> list:
    """Q2a: vendor_rate and vendor_rate_line (a vendor's rates for a case,
    typed on the team site, every version kept), made from schema.sql when
    they are not there. Nothing else changes. open_db has taken the
    restore point."""
    import re

    from . import db, paths

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    for table in ("vendor_rate", "vendor_rate_line"):
        if db._has_table(conn, table):
            notes.append("{} already on file".format(table))
            continue
        for statement in _statements(schema_sql, r"CREATE TABLE {}\s*\(.*?\);".format(table)):
            conn.execute(statement)
        for statement in _statements(schema_sql, r"CREATE (?:UNIQUE )?INDEX \w+ ON {}\s*\([^;]*?\);".format(table)):
            conn.execute(statement)
        notes.append("{} table created".format(table))
    return notes


def _to_v52(conn: sqlite3.Connection) -> list:
    """Q2b: party.type also Coloader and Trucker (party rebuilt as v9 did:
    foreign keys off, the views dropped and rebuilt; the rows of party and
    of every table that references it counted before and after, an error
    when one differs, then PRAGMA foreign_key_check);
    vendor_request.parts; vendor_rate.route_id and received_on, the rows
    already there filled (received_on the local day of at; route_id the
    case's only live route). No party's type is changed. open_db has taken
    the restore point."""
    import re

    from . import paths

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'party'").fetchone()
    if row is not None and "'Coloader'" not in _check_values(row[0], "type"):
        children = sorted({t for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                           for fk in conn.execute("PRAGMA foreign_key_list({})".format(t)) if fk[2] == "party"})

        def counts():
            return {t: conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in ["party"] + children}

        before = counts()
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for name in _statements(schema_sql, r"CREATE VIEW\s+(\w+)\s+AS"):
                conn.execute("DROP VIEW IF EXISTS {}".format(name))
            indexes = _rebuild_table(conn, "party", schema_sql)
            after = counts()
            if after != before:
                raise RuntimeError("party rebuild changed the counts: {} before, {} after".format(before, after))
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError("foreign_key_check after the party rebuild: {}".format([tuple(p) for p in problems][:5]))
        notes.append("party rebuilt (type Coloader and Trucker), indexes: {}".format(", ".join(indexes) or "-"))
        notes.append("rows before and after: " + ", ".join("{} {}".format(t, n) for t, n in before.items()))
        notes.append("foreign_key_check: nothing")
        notes.append("views rebuilt: " + ", ".join(_rebuild_views(conn)))
    if "parts" not in _columns(conn, "vendor_request"):
        conn.execute("ALTER TABLE vendor_request ADD COLUMN parts TEXT")
        notes.append("vendor_request.parts added")
    if _columns(conn, "vendor_rate"):
        for column, kind in (("route_id", "INTEGER REFERENCES enquiry_route(id)"), ("received_on", "TEXT")):
            if column not in _columns(conn, "vendor_rate"):
                conn.execute("ALTER TABLE vendor_rate ADD COLUMN {} {}".format(column, kind))
                notes.append("vendor_rate.{} added".format(column))
        filled = 0
        for r in conn.execute("SELECT id, enquiry_id, at, route_id, received_on FROM vendor_rate").fetchall():
            values = {}
            if not r["received_on"] and r["at"]:
                from . import site_rates
                values["received_on"] = site_rates._local_day(r["at"]) or None
            if r["route_id"] is None:
                live = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? AND voided = 0",
                                    (r["enquiry_id"],)).fetchall()
                if len(live) == 1:
                    values["route_id"] = live[0]["id"]
            values = {k: v for k, v in values.items() if v is not None}
            if values:
                conn.execute("UPDATE vendor_rate SET {} WHERE id = ?".format(", ".join("{} = ?".format(k) for k in values)),
                             list(values.values()) + [r["id"]])
                filled += 1
        notes.append("vendor_rate rows filled (received_on, route_id): {}".format(filled))
    return notes


def _to_v53(conn: sqlite3.Connection) -> list:
    """Q2d: quote_read and quote_read_draft (Read quote's readings and the
    drafts made of them), made from schema.sql when they are not there;
    vendor_rate.how ('web' for every version already on file) and
    vendor_rate.draft_id. Nothing else changes. open_db has taken the
    restore point."""
    import re

    from . import db, paths

    notes = []
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    for table in ("quote_read", "quote_read_draft"):
        if db._has_table(conn, table):
            notes.append("{} already on file".format(table))
            continue
        for statement in _statements(schema_sql, r"CREATE TABLE {}\s*\(.*?\);".format(table)):
            conn.execute(statement)
        for statement in _statements(schema_sql, r"CREATE (?:UNIQUE )?INDEX \w+ ON {}\s*\([^;]*?\);".format(table)):
            conn.execute(statement)
        notes.append("{} table created".format(table))
    if _columns(conn, "vendor_rate"):
        before = conn.execute("SELECT COUNT(*) FROM vendor_rate").fetchone()[0]
        if "how" not in _columns(conn, "vendor_rate"):
            conn.execute("ALTER TABLE vendor_rate ADD COLUMN how TEXT NOT NULL DEFAULT 'web' "
                         "CHECK (how IN ('web','read'))")
            notes.append("vendor_rate.how added ({} versions on file, each 'web')".format(before))
        if "draft_id" not in _columns(conn, "vendor_rate"):
            conn.execute("ALTER TABLE vendor_rate ADD COLUMN draft_id TEXT")
            notes.append("vendor_rate.draft_id added")
        after = conn.execute("SELECT COUNT(*) FROM vendor_rate").fetchone()[0]
        if after != before:
            raise RuntimeError("vendor_rate had {} rows before, {} after".format(before, after))
    return notes


def _to_v54(conn: sqlite3.Connection) -> list:
    """RQ2: Read quote from the team site. quote_read.source also 'files'
    (files sent from the site with no mail among them) and
    quote_read.site_action (the site's read action; NULL for a reading
    started on 8765). quote_read is rebuilt as v43 rebuilt status_event:
    foreign keys off, the views dropped and rebuilt, the rows of
    quote_read and of every table that references it counted before and
    after (an error when one differs), then PRAGMA foreign_key_check.
    by_name keeps what it had. open_db has taken the restore point."""
    import re

    from . import db, paths

    notes = []
    if not db._has_table(conn, "quote_read"):
        return ["quote_read not on file (made by v53 from schema.sql)"]
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'quote_read'").fetchone()
    if "'files'" in _check_values(row[0], "source") and "site_action" in _columns(conn, "quote_read"):
        return ["quote_read already takes files and site_action"]
    children = sorted({t for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                       for fk in conn.execute("PRAGMA foreign_key_list({})".format(t)) if fk[2] == "quote_read"})

    def counts():
        return {t: conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in ["quote_read"] + children}

    before = counts()
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for name in _statements(schema_sql, r"CREATE VIEW\s+(\w+)\s+AS"):
            conn.execute("DROP VIEW IF EXISTS {}".format(name))
        indexes = _rebuild_table(conn, "quote_read", schema_sql)
        after = counts()
        if after != before:
            raise RuntimeError("quote_read rebuild changed the counts: {} before, {} after".format(before, after))
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise RuntimeError("foreign_key_check after the quote_read rebuild: {}".format([tuple(p) for p in problems][:5]))
    notes.append("quote_read rebuilt (source mail / paste / files, site_action), indexes: {}".format(
        ", ".join(indexes) or "-"))
    notes.append("rows before and after: " + ", ".join("{} {}".format(t, n) for t, n in before.items()))
    notes.append("foreign_key_check: nothing")
    notes.append("views rebuilt: " + ", ".join(_rebuild_views(conn)))
    return notes


def _to_v55(conn: sqlite3.Connection) -> list:
    """Q2e: a vendor's options. vendor_rate.label ('' for every version
    already on file: a vendor's one set) and quote_read_draft.taken (JSON,
    per option '', 'saved' or 'skipped'), both added in place (SQLite adds
    a NOT NULL column with a default without a rebuild); the counts before
    and after. A draft on file: saved - its saved option 'saved', the
    others ''; discarded - every option 'skipped'; open, replaced or
    expired - every option ''. open_db has taken the restore point."""
    import json

    notes = []
    if _columns(conn, "vendor_rate"):
        before = conn.execute("SELECT COUNT(*) FROM vendor_rate").fetchone()[0]
        if "label" not in _columns(conn, "vendor_rate"):
            conn.execute("ALTER TABLE vendor_rate ADD COLUMN label TEXT NOT NULL DEFAULT ''")
            notes.append("vendor_rate.label added ({} versions on file, each '')".format(before))
        else:
            notes.append("vendor_rate.label already on file")
        after = conn.execute("SELECT COUNT(*) FROM vendor_rate").fetchone()[0]
        if after != before:
            raise RuntimeError("vendor_rate had {} rows before, {} after".format(before, after))
        notes.append("vendor_rate rows before and after: {} / {}".format(before, after))
    if _columns(conn, "quote_read_draft"):
        before = conn.execute("SELECT COUNT(*) FROM quote_read_draft").fetchone()[0]
        if "taken" not in _columns(conn, "quote_read_draft"):
            conn.execute("ALTER TABLE quote_read_draft ADD COLUMN taken TEXT")
            notes.append("quote_read_draft.taken added")
        filled = {}
        for r in conn.execute("SELECT id, draft, status, option FROM quote_read_draft WHERE taken IS NULL").fetchall():
            try:
                n = len(json.loads(r["draft"]).get("options") or []) or 1
            except (ValueError, AttributeError):
                n = 1
            taken = [""] * n
            if r["status"] == "saved":
                taken[min(int(r["option"] or 0), n - 1)] = "saved"
            elif r["status"] == "discarded":
                taken = ["skipped"] * n
            conn.execute("UPDATE quote_read_draft SET taken = ? WHERE id = ?", (json.dumps(taken), r["id"]))
            filled[r["status"]] = filled.get(r["status"], 0) + 1
        after = conn.execute("SELECT COUNT(*) FROM quote_read_draft").fetchone()[0]
        if after != before:
            raise RuntimeError("quote_read_draft had {} rows before, {} after".format(before, after))
        notes.append("quote_read_draft rows before and after: {} / {}; taken filled: {}".format(
            before, after, ", ".join("{} {}".format(k, v) for k, v in sorted(filled.items())) or "none"))
    problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise RuntimeError("foreign_key_check after v55: {}".format([tuple(p) for p in problems][:5]))
    notes.append("foreign_key_check: nothing")
    return notes


#: v56: the tables whose rows are counted before and after
_V56_COUNTED = ("enquiry", "status_event", "site_action", "vendor_request", "vendor_rate", "change_log")


def _to_v56(conn: sqlite3.Connection) -> list:
    """Q3: quote and quote_line (the team site's quote card's quotes,
    taken in after their quote actions), made from schema.sql when they
    are not there. Nothing else changes; the counts of the tables around
    them before and after, then foreign_key_check. open_db has taken the
    restore point."""
    import re

    from . import db, paths

    notes = []
    counted = [t for t in _V56_COUNTED if db._has_table(conn, t)]
    before = {t: conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in counted}
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    for table in ("quote", "quote_line"):
        if db._has_table(conn, table):
            notes.append("{} already on file".format(table))
            continue
        for statement in _statements(schema_sql, r"CREATE TABLE {}\s*\(.*?\);".format(table)):
            conn.execute(statement)
        for statement in _statements(schema_sql, r"CREATE (?:UNIQUE )?INDEX \w+ ON {}\s*\([^;]*?\);".format(table)):
            conn.execute(statement)
        notes.append("{} table created".format(table))
    after = {t: conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in counted}
    changed = [t for t in counted if before[t] != after[t]]
    if changed:
        raise RuntimeError("v56 changed the rows of {}".format(", ".join(changed)))
    notes.append("rows before and after: " + ", ".join("{} {} / {}".format(t, before[t], after[t]) for t in counted))
    problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise RuntimeError("foreign_key_check after v56: {}".format([tuple(p) for p in problems][:5]))
    notes.append("foreign_key_check: nothing")
    return notes


#: v57: the tables whose rows are counted before and after
_V57_COUNTED = ("enquiry", "enquiry_route", "master_location", "master_location_alias", "status_event", "change_log")


def _to_v57(conn: sqlite3.Connection) -> list:
    """RG2a: register_read (what the register reader filled on a
    registration, beside what was saved), made from schema.sql when it is
    not there. Nothing else changes; the counts of the tables around it
    before and after, then foreign_key_check. open_db has taken the restore
    point."""
    import re

    from . import db, paths

    notes = []
    counted = [t for t in _V57_COUNTED if db._has_table(conn, t)]
    before = {t: conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in counted}
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    if db._has_table(conn, "register_read"):
        notes.append("register_read already on file")
    else:
        for statement in _statements(schema_sql, r"CREATE TABLE register_read\s*\(.*?\);"):
            conn.execute(statement)
        for statement in _statements(schema_sql, r"CREATE (?:UNIQUE )?INDEX \w+ ON register_read\s*\([^;]*?\);"):
            conn.execute(statement)
        notes.append("register_read table created")
    after = {t: conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in counted}
    changed = [t for t in counted if before[t] != after[t]]
    if changed:
        raise RuntimeError("v57 changed the rows of {}".format(", ".join(changed)))
    notes.append("rows before and after: " + ", ".join("{} {} / {}".format(t, before[t], after[t]) for t in counted))
    problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise RuntimeError("foreign_key_check after v57: {}".format([tuple(p) for p in problems][:5]))
    notes.append("foreign_key_check: nothing")
    return notes


#: v58: the tables whose rows are counted before and after
_V58_COUNTED = ("suggestion", "enquiry", "status_event", "change_log")


def _to_v58(conn: sqlite3.Connection) -> list:
    """FX7: suggestion is rebuilt as v50 rebuilt it - every row and its id
    kept, counted before and after, the views rebuilt - so kind and
    decided_as may be 'followup' (a chaser the morning scan found, written
    only when she accepts it). The views are rebuilt in any case: v_wait
    and v_wait_followup are new, and v_enquiry's quote follow-up counts
    leave a wait's follow-ups out. Then foreign_key_check. open_db has
    taken the restore point."""
    import re

    from . import db, paths

    notes = []
    counted = [t for t in _V58_COUNTED if db._has_table(conn, t)]
    before = {t: conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in counted}
    ids_before = [r[0] for r in conn.execute("SELECT id FROM suggestion ORDER BY id")]
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'suggestion'").fetchone()
    # both CHECK lists (kind's, and decided_as's 'IS NULL OR ... IN') name it
    if row is not None and re.sub(r"--[^\n]*", "", row[0]).count("'followup'") < 2:
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for name in _statements(schema_sql, r"CREATE VIEW\s+(\w+)\s+AS"):
                conn.execute("DROP VIEW IF EXISTS {}".format(name))
            indexes = _rebuild_table(conn, "suggestion", schema_sql)
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        notes.append("suggestion rebuilt (kind and decided_as take followup), indexes: {}".format(
            ", ".join(indexes) or "-"))
    else:
        notes.append("suggestion already takes followup")
    notes.append("views rebuilt: " + ", ".join(_rebuild_views(conn)))
    after = {t: conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in counted}
    changed = [t for t in counted if before[t] != after[t]]
    if changed:
        raise RuntimeError("v58 changed the rows of {}".format(", ".join(changed)))
    if [r[0] for r in conn.execute("SELECT id FROM suggestion ORDER BY id")] != ids_before:
        raise RuntimeError("v58 changed the ids of suggestion")
    notes.append("rows before and after: " + ", ".join("{} {} / {}".format(t, before[t], after[t]) for t in counted))
    problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise RuntimeError("foreign_key_check after v58: {}".format([tuple(p) for p in problems][:5]))
    notes.append("foreign_key_check: nothing")
    return notes


MIGRATIONS = {2: _to_v2, 3: _to_v3, 4: _to_v4, 5: _to_v5, 6: _to_v6, 7: _to_v7,
              8: _to_v8, 9: _to_v9, 10: _to_v10, 11: _to_v11, 12: _to_v12, 13: _to_v13,
              14: _to_v14, 15: _to_v15, 16: _to_v16, 17: _to_v17, 18: _to_v18,
              19: _to_v19, 20: _to_v20, 21: _to_v21, 22: _to_v22, 23: _to_v23, 24: _to_v24, 25: _to_v25, 26: _to_v26, 27: _to_v27,
              28: _to_v28, 29: _to_v29, 30: _to_v30, 31: _to_v31, 32: _to_v32, 33: _to_v33,
              34: _to_v34, 35: _to_v35, 36: _to_v36, 37: _to_v37, 38: _to_v38, 39: _to_v39, 40: _to_v40,
              41: _to_v41, 42: _to_v42, 43: _to_v43, 44: _to_v44, 45: _to_v45, 46: _to_v46,
              47: _to_v47, 48: _to_v48, 49: _to_v49, 50: _to_v50, 51: _to_v51, 52: _to_v52, 53: _to_v53, 54: _to_v54,
              55: _to_v55, 56: _to_v56, 57: _to_v57, 58: _to_v58}


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM setting WHERE key = 'schema_version'").fetchone()
    return int(row[0]) if row else 1


def upgrade(conn: sqlite3.Connection) -> list:
    """Bring the database up to CURRENT_VERSION. Returns what was done."""
    version = current_version(conn)
    notes = []
    while version < CURRENT_VERSION:
        version += 1
        step = MIGRATIONS.get(version)
        if step is None:
            raise RuntimeError("no upgrade step to schema version {}".format(version))
        notes.append("schema upgraded to version {}".format(version))
        notes.extend(step(conn) or [])
        conn.execute("UPDATE setting SET value = ? WHERE key = 'schema_version'",
                     (str(version),))
        conn.commit()
    return notes
