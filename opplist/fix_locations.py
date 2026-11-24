"""The places the master list was missing, and the rows that should never
have been created (2026-09-17).

Three jobs, all of them idempotent, none of them deleting anything:

- **add** the gateways and ports the lanes we quote actually use, with
  their codes, flags and aliases (source 'manual': typed in here, not
  guessed by the intake);
- **check** the ports already on file and say when one of them carries a
  flag that looks wrong, without touching it, and list every place that
  has a UN/LOCODE but is not marked a seaport (a list to read: most of
  them are inland cities, and only a person can tell);
- **correct** the handful of flags asked for by name (FLAGS);
- **repair** the three rows a mail subject created by accident: two are
  retired (they are not places) and Mundra is completed.

A retired place keeps its row and its history; the one
case pointing at a retired row has its lane side cleared, the words that
were read as a place kept as free text, and a note saying so.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from . import db

#: What a place needs to be worth a row. `parent` is the key of the city a
#: gateway serves; `aliases` are the other spellings a mail may use.
WANTED = (
    # Saudi Arabia: the gateway Victor quotes, and the city behind it
    dict(key="RIYADH", name="Riyadh", type="city", country="Saudi Arabia", country_code="SA",
         unlocode="SARUH", is_airport=1, aliases=(("SARUH", "unlocode"),)),
    dict(key="RUH", name="Riyadh airport", type="airport", country="Saudi Arabia", country_code="SA",
         iata="RUH", is_airport=1, parent="RIYADH"),
    # South Korea
    dict(key="BUSAN", name="Busan", type="city", country="South Korea", country_code="KR",
         unlocode="KRPUS", is_seaport=1, is_airport=1,
         aliases=(("KRPUS", "unlocode"), ("PUSAN", "manual"))),
    dict(key="PUS", name="Busan airport", type="airport", country="South Korea", country_code="KR",
         iata="PUS", is_airport=1, parent="BUSAN"),
    # the ports the LATAM and Asia lanes keep landing on
    dict(key="SHEKOU", name="Shekou", type="seaport", country="China", country_code="CN",
         unlocode="CNSHK", is_seaport=1, parent="SHENZHEN", aliases=(("CNSHK", "unlocode"),)),
    dict(key="MANZANILLO, MX", name="Manzanillo", type="city", country="Mexico", country_code="MX",
         unlocode="MXZLO", is_seaport=1,
         aliases=(("MXZLO", "unlocode"), ("MANZANILLO MEXICO", "manual"))),
    dict(key="MANZANILLO, PA", name="Manzanillo (Panama)", type="seaport", country="Panama",
         country_code="PA", unlocode="PAMIT", is_seaport=1,
         aliases=(("PAMIT", "unlocode"), ("MANZANILLO PANAMA", "manual")),
         note="Manzanillo International Terminal, Colon. 'Manzanillo' on its own is "
              "ambiguous (Mexico has one too) and is deliberately not an alias of either."),
    dict(key="PUERTO QUETZAL", name="Puerto Quetzal", type="seaport", country="Guatemala",
         country_code="GT", unlocode="GTPRQ", is_seaport=1,
         aliases=(("GTPRQ", "unlocode"), ("QUETZAL", "manual"))),
    dict(key="ITAJAI", name="Itajai", type="city", country="Brazil", country_code="BR",
         unlocode="BRITJ", is_seaport=1, aliases=(("BRITJ", "unlocode"), ("ITAJAÍ", "manual"))),
    dict(key="RIO GRANDE", name="Rio Grande", type="city", country="Brazil", country_code="BR",
         unlocode="BRRIG", is_seaport=1, aliases=(("BRRIG", "unlocode"),)),
    dict(key="VERACRUZ", name="Veracruz", type="city", country="Mexico", country_code="MX",
         unlocode="MXVER", is_seaport=1, aliases=(("MXVER", "unlocode"),)),
    dict(key="ALTAMIRA", name="Altamira", type="city", country="Mexico", country_code="MX",
         unlocode="MXATM", is_seaport=1, aliases=(("MXATM", "unlocode"),)),
)

#: Ports that are on file already and are worth a second look, with what
#: the row should say. Nothing here is changed: a flag somebody set on
#: purpose is not this command's business.
CHECK_SEAPORT = ("NINGBO", "QINGDAO", "XIAMEN", "SHEKOU", "BALBOA", "IQUIQUE", "ARICA",
                 "PARANAGUA", "ITAJAI", "RIO GRANDE", "VERACRUZ", "ALTAMIRA", "JEBEL ALI",
                 "PUERTO QUETZAL", "MANZANILLO, MX", "MANZANILLO, PA", "BUSAN", "MUNDRA")

#: Rows a mail subject created by accident. 'retire' keeps the row and
#: takes it out of every list; 'complete' fills in what a real place has.
RETIRE = (
    ("RFQ 73105 USA-GHANA", "a mail subject, not a place (RFQ numbers and "
                              "country-to-country lanes are not locations)"),
    ("TERRALUX", "a company name read out of a subject line, not a place"),
)
COMPLETE = (
    dict(key="MUNDRA", name="Mundra", type="seaport", country="India", country_code="IN",
         unlocode="INMUN", is_seaport=1, status="active", source="manual",
         aliases=(("INMUN", "unlocode"),)),
)
#: Flags on a place that is otherwise right. Both of these are container
#: ports the phase 1 file left unflagged (2026-09-17, asked for by name).
FLAGS = (
    dict(key="XIAMEN", is_seaport=1, why="one of China's biggest container ports"),
    dict(key="IQUIQUE", is_seaport=1, why="a Chilean port, and a free zone we quote"),
    # 2026-09-17: the rest of the 'UN/LOCODE but no port flag' review, read
    # and picked by name. Fort Lauderdale is deliberately not here: its port
    # is PORT EVERGLADES, which is a row of its own.
    dict(key="SHENZHEN", is_seaport=1,
         why="the city a mail names for Yantian and Shekou, which are rows of their own"),
    dict(key="DALIAN", is_seaport=1, why="a north China container port"),
    dict(key="GUANGZHOU", is_seaport=1, why="Nansha is this city's container port"),
    dict(key="FUZHOU", is_seaport=1, why="a Fujian container port"),
    dict(key="MUMBAI", is_seaport=1, why="the port city behind Nhava Sheva"),
    dict(key="DAMMAM", is_seaport=1, why="King Abdul Aziz port, the Gulf gateway we quote"),
    dict(key="MEJILLONES", is_seaport=1, why="a Chilean port north of Antofagasta"),
    dict(key="PENANG", is_seaport=1, why="a Malaysian container port"),
    dict(key="TOKYO", is_seaport=1, why="a main Japanese container port"),
    dict(key="TAMPA, FL", is_seaport=1, why="a Gulf coast port"),
    dict(key="WILMINGTON, NC", is_seaport=1, why="an east coast port"),
)


@dataclass
class Report:
    added: list = field(default_factory=list)
    already: list = field(default_factory=list)
    aliases: list = field(default_factory=list)
    retired: list = field(default_factory=list)
    completed: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    cases: list = field(default_factory=list)
    odd: list = field(default_factory=list)
    review: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    dry_run: bool = True

    def lines(self) -> list:
        out = [
            "places added        : {}".format(len(self.added)),
            "  already on file   : {}".format(len(self.already)),
            "aliases added       : {}".format(len(self.aliases)),
            "places retired      : {}".format(len(self.retired)),
            "places completed    : {}".format(len(self.completed)),
            "flags corrected     : {}".format(len(self.flags)),
            "cases tidied        : {}".format(len(self.cases)),
        ]
        for label, rows in (("+ ", self.added), ("~ ", self.aliases), ("- ", self.retired),
                            ("* ", self.completed), ("= ", self.flags), ("> ", self.cases),
                            ("? ", self.odd), ("! ", self.skipped)):
            out.extend(label + line for line in rows)
        if self.review:
            out.append("")
            out.append("a UN/LOCODE but is_seaport = 0 - {} row(s), nothing changed; most of "
                       "these are inland cities and gateways, so this is a list to read, "
                       "not a list to fix:".format(len(self.review)))
            out.extend("  " + line for line in self.review)
        if self.dry_run:
            out.append("")
            out.append("dry run: nothing was written. Run again with --apply.")
        return out


def _find(conn, key: str):
    return conn.execute("SELECT * FROM master_location WHERE key = ?", (key,)).fetchone()


def _add_one(repo, wanted: dict, report: Report, dry_run: bool, planned=()) -> None:
    conn = repo.conn
    key = wanted["key"]
    found = _find(conn, key)
    if found is not None:
        report.already.append("{} ({})".format(key, found["country"]))
        return
    if conn.execute("SELECT 1 FROM country WHERE name = ?", (wanted["country"],)).fetchone() is None:
        report.skipped.append("{}: country '{}' is not on file".format(key, wanted["country"]))
        return
    parent = _find(conn, wanted["parent"]) if wanted.get("parent") else None
    if wanted.get("parent") and parent is None and wanted["parent"] not in planned:
        report.skipped.append("{}: parent '{}' is not on file".format(key, wanted["parent"]))
        return
    columns = {name: wanted[name] for name in
               ("country_code", "unlocode", "iata", "is_seaport", "is_airport") if name in wanted}
    if parent is not None:
        columns["parent_id"] = parent["id"]
    report.added.append("{:<16} {:<22} {:<14} {}{}".format(
        key, wanted["name"], wanted["type"], wanted.get("unlocode") or wanted.get("iata") or "",
        "  under " + wanted["parent"] if wanted.get("parent") else ""))
    if dry_run:
        report.aliases.extend("{} -> {}".format(alias, key)
                              for alias, _ in wanted.get("aliases", ()))
        return
    row = repo.add_master_location(
        key, wanted["country"], type=wanted["type"], name=wanted["name"],
        source="manual", note=wanted.get("note"), **columns)
    for alias, kind in wanted.get("aliases", ()):
        if repo.add_master_alias(alias, row["id"], kind):
            report.aliases.append("{} -> {}".format(alias, key))


def _alias_only(repo, wanted: dict, report: Report, dry_run: bool) -> None:
    """A place that is already there may still be missing a spelling."""
    row = _find(repo.conn, wanted["key"])
    if row is None:
        return
    for alias, kind in wanted.get("aliases", ()):
        exists = repo.conn.execute(
            "SELECT 1 FROM master_location_alias WHERE alias = ? AND location_id = ?",
            (alias, row["id"])).fetchone()
        if exists:
            continue
        report.aliases.append("{} -> {}".format(alias, wanted["key"]))
        if not dry_run:
            repo.add_master_alias(alias, row["id"], kind)


def _retire(repo, key: str, why: str, report: Report, dry_run: bool, today: str = "",
            touch_cases: bool = True) -> None:
    conn = repo.conn
    row = _find(conn, key)
    if row is None or row["status"] == "retired":
        return
    report.retired.append("{} - {}".format(key, why))
    cases = conn.execute(
        "SELECT id, opp_no, origin_loc_id, dest_loc_id, origin_alt, dest_alt "
        "FROM enquiry WHERE origin_loc_id = ? OR dest_loc_id = ?", (row["id"], row["id"])).fetchall()
    if not touch_cases:
        for case in cases:
            report.cases.append("{}: still points at '{}' - left for Iris to change by hand"
                                .format(case["opp_no"], row["name"]))
        cases = []
    for case in cases:
        for side in ("origin", "dest"):
            if case["{}_loc_id".format(side)] != row["id"]:
                continue
            values = {"{}_loc_id".format(side): None}
            kept = case["{}_alt".format(side)]
            if not (kept or "").strip():
                values["{}_alt".format(side)] = row["name"]
                report.cases.append("{}: {} cleared, '{}' kept as free text".format(
                    case["opp_no"], side, row["name"]))
            else:
                report.cases.append("{}: {} cleared ('{}' was read as a place); the address "
                                    "already in {}_alt is left as it is".format(
                                        case["opp_no"], side, row["name"], side))
            if not dry_run:
                repo.update("enquiry", case["id"], values)
        if not dry_run:
            repo.add_note(case["id"], today or dt.date.today().isoformat(),
                          "'{}' was read out of the subject as a place and is not one ({}). "
                          "The lane side is empty again; the words are in the free-text field."
                          .format(row["name"], why), source="import")
    if not dry_run:
        note = "{}Retired {}: {}.".format(
            (row["note"].rstrip() + " ") if row["note"] else "",
            today or dt.date.today().isoformat(), why)
        repo.update("master_location", row["id"], {"status": "retired", "note": note})
        repo.touch("master_location", row["id"])


def _complete(repo, wanted: dict, report: Report, dry_run: bool) -> None:
    row = _find(repo.conn, wanted["key"])
    if row is None:
        report.skipped.append("{}: not on file, nothing to complete".format(wanted["key"]))
        return
    values = {name: wanted[name] for name in
              ("name", "type", "country", "country_code", "unlocode", "iata",
               "is_seaport", "is_airport", "status", "source")
              if name in wanted and row[name] != wanted[name]}
    if values:
        report.completed.append("{}: {}".format(
            wanted["key"], ", ".join("{} {!r} -> {!r}".format(k, row[k], v) for k, v in values.items())))
        if not dry_run:
            repo.update("master_location", row["id"], values)
            repo.touch("master_location", row["id"])
    _alias_only(repo, wanted, report, dry_run)


def _set_flags(repo, wanted: dict, report: Report, dry_run: bool) -> None:
    """One flag on a row that is otherwise right."""
    row = _find(repo.conn, wanted["key"])
    if row is None:
        report.skipped.append("{}: not on file, no flag to set".format(wanted["key"]))
        return
    values = {name: wanted[name] for name in ("is_seaport", "is_airport")
              if name in wanted and row[name] != wanted[name]}
    if not values:
        return
    report.flags.append("{}: {} ({})".format(
        wanted["key"], ", ".join("{} {} -> {}".format(k, row[k], v) for k, v in values.items()),
        wanted.get("why", "")))
    if not dry_run:
        repo.update("master_location", row["id"], values)
        repo.touch("master_location", row["id"])


def _review_codes(conn, report: Report) -> None:
    """Every place that carries a UN/LOCODE and is not marked a seaport.
    Read only: a city with a code is perfectly normal, and only a person
    can tell an inland one from a port nobody flagged."""
    for row in conn.execute(
            "SELECT key, name, type, country, unlocode FROM master_location "
            "WHERE COALESCE(unlocode, '') <> '' AND is_seaport = 0 AND status <> 'retired' "
            "ORDER BY key"):
        report.review.append("{:<22} {:<22} {:<8} {:<18} {}".format(
            row["key"], row["name"], row["type"], row["country"], row["unlocode"]))


def _look_odd(conn, report: Report, handled=()) -> None:
    for key in CHECK_SEAPORT:
        if key in handled:                     # this run sets its flags itself
            continue
        row = _find(conn, key)
        if row is None:
            continue
        if not row["is_seaport"]:
            report.odd.append("{} is on file but is_seaport = 0 (left as it is; the Locations "
                              "page can change it)".format(key))


#: This module was a one-off and it has been spent: it ran on 2026-09-17 and
#: its places are on file. Afterwards two of them were renamed by hand -
#: MANZANILLO, MX became MANZANILLO and MANZANILLO, PA became MANZANILLO MIT
#: (D4: a port that is its own town is one city row; a named terminal is a
#: seaport under the city it serves). WANTED still names the old keys and
#: `_find` matches on the key alone, so a second run over the live database
#: would not recognise them and would create duplicate places.
#:
#: The module is kept, never deleted: it is the record of
#: what was done. It simply refuses to touch the real database again. Its
#: tests, which build their own copy, still exercise the whole body; new
#: places go in through the Locations page.
APPLIED_ON = "2026-09-17"
SPENT = ("fix_locations is a one-off, applied {}: it must not run again on the "
         "live database. Its WANTED list still names MANZANILLO, MX and "
         "MANZANILLO, PA, renamed by hand afterwards, so a second run would "
         "create duplicate places. The module is kept as the record of what it "
         "did; add places through the Locations page.")


def _database_files(conn) -> list:
    """Every file this connection has open, resolved."""
    out = []
    for row in conn.execute("PRAGMA database_list"):
        name = row[2] if not isinstance(row, dict) else row["file"]
        if name:
            try:
                out.append(Path(name).resolve())
            except OSError:
                pass
    return out


def is_spent_for(conn) -> bool:
    """True when this connection is the real opplist.db (SPENT applies)."""
    from . import paths

    try:
        live = Path(paths.DB_PATH).resolve()
    except OSError:
        return False
    return live in _database_files(conn)


def run(conn, dry_run: bool = True, today: str = "") -> Report:
    """Add, check and repair. Writes nothing when `dry_run`.

    Spent on the live database: see SPENT. The check comes first, so nothing
    is read or written there; the CLI prints the sentence and stops.
    """
    if is_spent_for(conn):
        raise ValueError(SPENT.format(APPLIED_ON))
    if not db.master_active(conn):
        raise ValueError("the master location tables are not active on this database yet "
                         "(run migrate-master --apply first)")
    repo = db.Repository(conn, source="import")
    report = Report(dry_run=dry_run)
    planned = {wanted["key"] for wanted in WANTED}
    for wanted in WANTED:
        _add_one(repo, wanted, report, dry_run, planned)
        _alias_only(repo, wanted, report, dry_run)
    for key, why in RETIRE:
        _retire(repo, key, why, report, dry_run, today)
    for wanted in COMPLETE:
        _complete(repo, wanted, report, dry_run)
    for wanted in FLAGS:
        _set_flags(repo, wanted, report, dry_run)
    _look_odd(conn, report, planned | {wanted["key"] for wanted in COMPLETE}
              | {wanted["key"] for wanted in FLAGS})
    _review_codes(conn, report)
    if dry_run:
        conn.rollback()
    else:
        repo.commit()
    return report
