"""Verify the migration against the workbook.

Every check prints numbers rather than a verdict, because 'looks the same' is
not a result. Where a difference is expected by the spec it is still counted
and listed, marked as expected, so nothing is silently swallowed.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

from . import export_csv, export_xlsx, paths

#: The status counts a demo database is checked against are not constants:
#: they are computed from the demo data itself (demo_reference), by building
#: the same seed and date again.

# Columns the app no longer computes, so they are not compared.
SKIP_COLUMNS = {
    "Overdue Flag",
    "Location Check",
    "Country/Location Check",
    "Mode/Location Check",
}

QTY_COLUMNS = ("Container Type", "Container Qty", "Air Weight (kgs)",
               "LCL Volume (cbm)")


def expected_reason(header, left, right, opp_no, source, context):
    """Why this cell is allowed to differ, or None if it is a real difference.

    Each rule is narrow on purpose: 'the whole column may differ' would hide a
    genuine mistake in that column, so every rule also checks the shape of the
    change and, where it matters, the row it happened on.
    """
    if header == "Special Handling" and left:
        return ("Special Handling: tags re-joined with '; ', non-tag text moved "
                "to Remarks or End customer")

    if header == "Loss Reason" and left == "" and right:
        return ("Loss Reason: Dropped cases carry the reason the old Status "
                "string encoded")

    if header in ("System Notes", "Remarks") and right.startswith(left):
        return ("{}: the migration appends a provenance line where it filled "
                "a blank".format(header))

    if header in QTY_COLUMNS and left == "" and opp_no in context["qty_parsed"]:
        return ("quantity columns: filled from Source Qty (raw) by the "
                "quantity parser")

    if header == "LATAM" and left == "" and right in ("Y", "N"):
        return ("LATAM: the workbook formula gave up when both lane countries "
                "were blank; v_enquiry still answers from the requester region")

    if header == "Direction" and left == "" and right:
        return "Direction: as above, derived where the workbook formula was blank"

    if (header == "Status" and left == "In Progress / Shipped"
            and right == "Secured"):
        return ("Status: 'In Progress / Shipped' is a Won case and exports as "
                "'Secured'")

    if header == "Source Type" and left == "" and right in ("Agent", "Office"):
        return ("Source Type: the party now has a type where Agent_Master left "
                "the cell blank")

    return None


@dataclass
class Check:
    name: str
    ok: bool
    lines: list = field(default_factory=list)

    def render(self) -> list:
        mark = "PASS" if self.ok else "FAIL"
        out = ["[{}] {}".format(mark, self.name)]
        out.extend("       " + line for line in self.lines)
        return out


@dataclass
class VerifyResult:
    checks: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def render(self) -> list:
        out = []
        for check in self.checks:
            out.extend(check.render())
        passed = sum(1 for c in self.checks if c.ok)
        out.append("")
        out.append("{} of {} checks passed".format(passed, len(self.checks)))
        return out


# ------------------------------------------------------------------ helpers --
def _norm(value):
    """One canonical form for a cell so that types do not create false diffs."""
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return "{:.6f}".format(value).rstrip("0")
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


def _sheet_rows(ws, key_col=0):
    return [r for r in ws.iter_rows(min_row=2, values_only=True)
            if r[key_col] not in (None, "")]


def _opportunity_rows(ws):
    """Every row with anything in it, numbered or not.

    A row with an agent, a date and a lane but no Enquiry No. is a case
    Iris half-registered; the migration numbers it, so the counts here have to see it too.
    """
    return [r for r in ws.iter_rows(min_row=2, values_only=True)
            if any(v not in (None, "") for v in r)]


def edited_in_the_app(conn) -> set:
    """Cases somebody has changed since the migration, by opp number.

    After the switchover the workbook is a frozen copy in restore-points and
    the app is where the work happens, so a case she has answered no longer
    matches the sheet - and should not. Those cases are named and left out of
    the comparison rather than counted as migration faults.
    """
    return {r["opp_no"] for r in conn.execute(
        "SELECT DISTINCT e.opp_no FROM enquiry e "
        "JOIN change_log c ON c.table_name = 'enquiry' AND c.row_id = e.id "
        "WHERE c.source <> 'migration' "
        "UNION "
        "SELECT DISTINCT e.opp_no FROM enquiry e "
        "JOIN status_event s ON s.enquiry_id = e.id "
        "JOIN change_log c ON c.table_name = 'status_event' "
        "                 AND c.row_id = s.id "
        "WHERE c.source <> 'migration'")}


def numbered_at_migration(conn) -> set:
    """The opp numbers the migration allocated, which the workbook has not."""
    return {r["opp_no"] for r in conn.execute(
        "SELECT opp_no FROM enquiry "
        "WHERE system_notes LIKE '%numbered at migration%'")}


# ------------------------------------------------------------------- checks --
def _numbered_extras(conn) -> tuple:
    """The parties and locations only the newly numbered rows needed.

    A half-registered row can name an agent that never reached Agent_Master
    and a port that never reached Lookup_Location, so migrating it has to
    create them. They are counted here rather than waved through, so that any
    other new party or location still fails the check.
    """
    parties = [r["key"] for r in conn.execute(
        "SELECT p.key FROM party p JOIN enquiry e ON e.requester_party_id = p.id "
        "WHERE p.placeholder = 1 GROUP BY p.key "
        "HAVING COUNT(*) = SUM(CASE WHEN e.system_notes "
        "                           LIKE '%numbered at migration%' "
        "                           THEN 1 ELSE 0 END)")]
    locations = [r["key"] for r in conn.execute(
        "SELECT l.key FROM location l "
        "WHERE l.status = 'To Confirm' "
        "  AND l.notes LIKE 'Created during migration%' "
        "  AND l.key IN (SELECT origin_loc FROM enquiry "
        "                WHERE system_notes LIKE '%numbered at migration%' "
        "                UNION "
        "                SELECT dest_loc FROM enquiry "
        "                WHERE system_notes LIKE '%numbered at migration%')")]
    return parties, locations


def _parties_from_elsewhere(conn) -> dict:
    """Parties that did not come out of Agent_Master, by where they came from.

    The tracker, the agent-development list and the app itself all add parties after the
    migration. They are supposed to be there, and they are not the workbook's
    to answer for, so the count names them instead of failing on them.
    """
    return {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(*) AS n FROM party "
        "WHERE source <> 'xlsx' GROUP BY source")}


def check_counts(conn, wb) -> Check:
    extra_parties, extra_locations = _numbered_extras(conn)
    elsewhere = _parties_from_elsewhere(conn)
    expected = {
        "enquiry": len(_opportunity_rows(wb["Opportunities"])),
        "party": len(_sheet_rows(wb["Agent_Master"])) + len(extra_parties)
                 + sum(elsewhere.values()),
        "location": len(_sheet_rows(wb["Lookup_Location"])) + len(extra_locations),
        "country": len(_sheet_rows(wb["Country_List"])),
    }
    lines = []
    ok = True
    for table, want in expected.items():
        got = conn.execute("SELECT COUNT(*) AS n FROM {}".format(table)).fetchone()["n"]
        lines.append("{:<10} db {:5d}   workbook {:5d}   diff {:+d}".format(
            table, got, want, got - want))
        if got != want:
            ok = False
    numbered = numbered_at_migration(conn)
    if numbered:
        lines.append("of the enquiries, {} were rows with no Enquiry No. that "
                     "the migration numbered: {}".format(
                         len(numbered), ", ".join(sorted(numbered))))
    if extra_parties:
        lines.append("party expected includes {} agent(s) those rows name "
                     "that Agent_Master does not: {}".format(
                         len(extra_parties), ", ".join(sorted(extra_parties))))
    if extra_locations:
        lines.append("location expected includes {} place(s) those rows name "
                     "that Lookup_Location does not: {}".format(
                         len(extra_locations),
                         ", ".join(sorted(extra_locations))))
    for source, count in sorted(elsewhere.items()):
        lines.append("party expected includes {} added by {} since the "
                     "migration".format(count, source))
    return Check("1. row counts match the workbook", ok, lines)


def check_status_distribution(conn, wb) -> Check:
    """The distribution the workbook implies, for the cases nobody has touched.

    What is checked is the workbook itself: every case answered in the app
    since the migration is named and left out, because it is supposed to
    differ now.
    """
    from . import status as status_mod

    edited = edited_in_the_app(conn)
    numbered = numbered_at_migration(conn)
    skip = edited | numbered

    expected = {}
    unconfirmed_want = legacy_want = 0
    for row in _sheet_rows(wb["Opportunities"]):
        opp_no = str(row[0]).strip()
        if opp_no in skip:
            continue
        mapped = status_mod.from_legacy(
            str(row[27]).strip() if row[27] not in (None, "") else "")
        expected[mapped.status] = expected.get(mapped.status, 0) + 1
        unconfirmed_want += 0 if mapped.confirmed else 1
        legacy_want += 1 if mapped.legacy else 0

    marks = ",".join("?" for _ in skip) or "''"
    args = tuple(skip)
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM enquiry "
        "WHERE opp_no NOT IN ({}) GROUP BY status".format(marks), args).fetchall()
    got = {r["status"]: r["n"] for r in rows}
    unconfirmed = conn.execute(
        "SELECT COUNT(*) AS n FROM enquiry WHERE status_confirmed = 0 "
        "AND opp_no NOT IN ({})".format(marks), args).fetchone()["n"]
    legacy = conn.execute(
        "SELECT COUNT(*) AS n FROM enquiry WHERE legacy = 1 "
        "AND opp_no NOT IN ({})".format(marks), args).fetchone()["n"]

    lines = []
    ok = True
    for status in sorted(set(expected) | set(got)):
        have, want = got.get(status, 0), expected.get(status, 0)
        lines.append("{:<9} {:4d}   workbook {:4d}   diff {:+d}".format(
                         status, have, want, have - want))
        if have != want:
            ok = False
    lines.append("status_confirmed=0 {:4d}   workbook {:4d}   diff {:+d}".format(
        unconfirmed, unconfirmed_want, unconfirmed - unconfirmed_want))
    lines.append("legacy=1           {:4d}   workbook {:4d}   diff {:+d}".format(
        legacy, legacy_want, legacy - legacy_want))
    if unconfirmed != unconfirmed_want or legacy != legacy_want:
        ok = False
    if edited:
        lines.append("not counted: {} case(s) answered in the app since the "
                     "migration".format(len(edited)))
    if numbered:
        lines.append("not counted: {} case(s) the migration numbered itself"
                     .format(len(numbered)))
    return Check("2. status distribution", ok, lines)


def _distribution(conn) -> dict:
    """{status: n} plus 'status_confirmed=0' and 'legacy=1' for one database."""
    out = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM enquiry GROUP BY status")}
    out["status_confirmed=0"] = conn.execute(
        "SELECT COUNT(*) FROM enquiry WHERE status_confirmed = 0").fetchone()[0]
    out["legacy=1"] = conn.execute("SELECT COUNT(*) FROM enquiry WHERE legacy = 1").fetchone()[0]
    return out


def demo_reference(conn) -> dict | None:
    """The distribution the demo generator produces for this database's
    seed and date (settings demo_seed / demo_today), built again in a
    temporary folder. None when the database is not a demo database."""
    from . import db, demo

    seed = db.get_setting(conn, demo.SEED_SETTING)
    day = db.get_setting(conn, demo.DATE_SETTING)
    if seed in (None, "") or not day:
        return None
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        demo.generate(Path(tmp) / "demo", seed=int(seed), today=str(day), leak_list=None)
        ref = sqlite3.connect(str(Path(tmp) / "demo" / demo.DB_NAME))
        try:
            return _distribution(ref)
        finally:
            ref.close()


def check_demo_distribution(conn, reference=None) -> Check:
    """A demo database against the demo data it was generated as: the counts
    move only when someone works the cases in the app."""
    reference = reference if reference is not None else demo_reference(conn)
    got = _distribution(conn)
    lines, ok = [], True
    for key in sorted(set(reference) | set(got)):
        have, want = got.get(key, 0), reference.get(key, 0)
        lines.append("{:<18} {:4d}   demo data {:4d}   diff {:+d}".format(key, have, want, have - want))
        ok = ok and have == want
    return Check("2. status distribution (against the demo data)", ok, lines)


def check_events(conn) -> Check:
    without = conn.execute(
        "SELECT COUNT(*) AS n FROM enquiry e "
        "WHERE NOT EXISTS (SELECT 1 FROM status_event s WHERE s.enquiry_id = e.id)"
    ).fetchone()["n"]
    mismatched = conn.execute(
        "SELECT e.opp_no, e.status, "
        # The same rule the app applies when it re-states a case: a struck-out
        # event and a note do not say what the case is (schema v5, v6).
        # (v19: a re-quote counts as a status event and outranks a same-day
        # Quoted - the rule of status.STAGE_RANK, written out here too.)
        "(SELECT s.status FROM status_event s WHERE s.enquiry_id = e.id "
        " AND s.voided = 0 AND s.kind IN ('status','requote') "
        " ORDER BY s.at DESC, CASE WHEN s.kind = 'requote' THEN 3 WHEN s.status = 'New' THEN 1 "
        " WHEN s.status = 'Sourcing' THEN 2 WHEN s.status = 'Quoted' THEN 3 ELSE 4 END DESC, "
        " s.id DESC LIMIT 1) AS last_status "
        "FROM enquiry e"
    ).fetchall()
    bad = [r for r in mismatched if r["last_status"] != r["status"]]
    total_events = conn.execute(
        "SELECT COUNT(*) AS n FROM status_event").fetchone()["n"]
    # Several events on the case's latest day: fine (the furthest stage
    # is the case, the same-day rule), just worth a line.
    crowded = conn.execute(
        "SELECT e.opp_no, COUNT(*) AS n FROM enquiry e "
        "JOIN status_event s ON s.enquiry_id = e.id AND s.voided = 0 AND s.kind IN ('status','requote') "
        "WHERE s.at = (SELECT MAX(t.at) FROM status_event t WHERE t.enquiry_id = e.id "
        "              AND t.voided = 0 AND t.kind IN ('status','requote')) "
        "GROUP BY e.id HAVING n > 1 ORDER BY e.opp_no").fetchall()
    lines = [
        "status_event rows           {}".format(total_events),
        "enquiries with no event     {}".format(without),
        "last event != enquiry.status {}".format(len(bad)),
    ]
    for row in bad[:10]:
        lines.append("   {} status {} but last event {}".format(
            row["opp_no"], row["status"], row["last_status"]))
    lines.append("latest day has several events {} (info, not a failure)".format(len(crowded)))
    if crowded:
        lines.append("   " + ", ".join(
            "{} ({})".format(r["opp_no"], r["n"]) for r in crowded[:10]))
    return Check("3. every enquiry has events and ends on its own status",
                 without == 0 and not bad, lines)


def check_placeholders(conn) -> Check:
    allowed = ("(tbc)", "unknown", "colin")
    # A row the workbook never numbered is a case that was never finished, so
    # its agent may not be in Agent_Master and its ports may not be in
    # Lookup_Location. Those are expected; anything else is not.
    created = conn.execute(
        "SELECT p.key, COUNT(e.id) AS n, "
        "       SUM(CASE WHEN e.system_notes LIKE '%numbered at migration%' "
        "                THEN 1 ELSE 0 END) AS numbered "
        "FROM party p JOIN enquiry e ON e.requester_party_id = p.id "
        "WHERE p.placeholder = 1 AND lower(trim(p.key)) NOT IN (?, ?, ?) "
        "GROUP BY p.key", allowed
    ).fetchall()
    unexpected_parties = [r for r in created if r["numbered"] != r["n"]]
    expected_parties = [r for r in created if r["numbered"] == r["n"]]

    numbered_locations = {
        r["key"] for r in conn.execute(
            "SELECT origin_loc AS key FROM enquiry "
            "WHERE system_notes LIKE '%numbered at migration%' "
            "  AND origin_loc IS NOT NULL "
            "UNION "
            "SELECT dest_loc FROM enquiry "
            "WHERE system_notes LIKE '%numbered at migration%' "
            "  AND dest_loc IS NOT NULL")}
    to_confirm = conn.execute(
        "SELECT key FROM location WHERE status = 'To Confirm' "
        "AND notes LIKE 'Created during migration%'"
    ).fetchall()
    unexpected_locations = [r["key"] for r in to_confirm
                            if r["key"] not in numbered_locations]
    expected_locations = [r["key"] for r in to_confirm
                          if r["key"] in numbered_locations]

    lines = [
        "unexpected placeholder requesters {}".format(len(unexpected_parties)),
        "unexpected locations created      {}".format(len(unexpected_locations)),
    ]
    for row in unexpected_parties:
        lines.append("   {} ({} enquiries)".format(row["key"], row["n"]))
    for key in unexpected_locations[:10]:
        lines.append("   {}".format(key))
    if expected_parties:
        lines.append("expected: {} placeholder requester(s) for rows the "
                     "migration numbered: {}".format(
                         len(expected_parties),
                         ", ".join(r["key"] for r in expected_parties)))
    if expected_locations:
        lines.append("expected: {} location(s) created for those rows: "
                     "{}".format(len(expected_locations),
                                 ", ".join(sorted(expected_locations))))
    return Check("4. no requester or location invented by the migration",
                 not unexpected_parties and not unexpected_locations, lines)


def check_workbook_cells(conn, wb, export_path) -> Check:
    """Compare the exported Opportunities sheet with the original, cell by cell.

    The workbook is a frozen copy now, so a case she has answered in the app
    is meant to differ from it: those are named and skipped rather than
    counted as migration faults.
    """
    edited = edited_in_the_app(conn)
    original = {}
    ws = wb["Opportunities"]
    headers = [str(c).strip() if c else "" for c in
               next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
    for row in _sheet_rows(ws):
        original[str(row[0]).strip()] = row

    exported_wb = openpyxl.load_workbook(export_path, read_only=True, data_only=False)
    try:
        ews = exported_wb["Opportunities"]
        eheaders = [str(c).strip() if c else "" for c in
                    next(ews.iter_rows(min_row=1, max_row=1, values_only=True))]
        exported = {str(r[0]).strip(): r for r in _sheet_rows(ews)}
    finally:
        exported_wb.close()

    lines = []
    ok = True
    wanted = export_xlsx.OPPORTUNITY_COLUMNS
    if eheaders[:len(wanted)] != list(wanted):
        ok = False
        lines.append("header mismatch in the export")
        for index, (want, got) in enumerate(zip(wanted, eheaders), start=1):
            if want != got:
                lines.append("   col {}: expected {!r}, got {!r}".format(index, want, got))

    numbered = numbered_at_migration(conn)
    missing = set(original) - set(exported)
    extra = set(exported) - set(original) - numbered
    if missing or extra:
        ok = False
    lines.append("cases in workbook {}   in export {}   missing {}   extra {}".format(
        len(original), len(exported), len(missing), len(extra)))
    if numbered:
        lines.append("not compared: {} case(s) the migration numbered, which "
                     "the workbook has no row for by number ({})".format(
                         len(numbered), ", ".join(sorted(numbered))))
    if edited:
        lines.append("not compared: {} case(s) answered in the app since the "
                     "migration - the workbook is a frozen copy and they are "
                     "meant to differ".format(len(edited)))

    context = {
        "qty_parsed": {
            r["opp_no"] for r in conn.execute(
                "SELECT opp_no FROM enquiry "
                "WHERE system_notes LIKE '%qty parsed from raw (migration)%'")
        }
    }

    compared = 0
    diffs = []
    expected_diffs = {}
    for opp_no, source_row in original.items():
        target_row = exported.get(opp_no)
        if target_row is None or opp_no in edited:
            continue
        for index, header in enumerate(headers[:len(wanted)]):
            if header in SKIP_COLUMNS:
                continue
            compared += 1
            left = _norm(source_row[index])
            right = _norm(target_row[index]) if index < len(target_row) else ""
            if left == right:
                continue
            reason = expected_reason(header, left, right, opp_no, source_row, context)
            if reason:
                expected_diffs.setdefault(reason, []).append((opp_no, header, left, right))
            else:
                diffs.append((opp_no, header, left, right))

    lines.append("cells compared {}   differences {}   expected {}   unexpected {}".format(
        compared,
        len(diffs) + sum(len(v) for v in expected_diffs.values()),
        sum(len(v) for v in expected_diffs.values()),
        len(diffs)))
    for reason, items in sorted(expected_diffs.items()):
        lines.append("expected: {} cells - {}".format(len(items), reason))
        for opp_no, header, left, right in items[:3]:
            lines.append("   {} {}: {!r} -> {!r}".format(opp_no, header, left, right))
        if len(items) > 3:
            lines.append("   ... and {} more".format(len(items) - 3))
    for opp_no, header, left, right in diffs[:25]:
        lines.append("UNEXPECTED {} {}: {!r} -> {!r}".format(
            opp_no, header, left, right))
    if diffs:
        ok = False
    return Check("5. exported Opportunities sheet matches the workbook", ok, lines)


def _powershell_equivalent_csv(export_path) -> str:
    """What Export-OppIndex.ps1 would write, read from the exported workbook.

    It maps workbook headers to CSV columns, formats Date Received as
    yyyy-MM-dd, trims everything else, and skips rows without a case number.
    """
    wanted = [
        ("Enquiry No.", "OppNo"),
        ("Date Received", "DateReceived"),
        ("Agent", "Agent"),
        ("Service Mode", "Mode"),
        ("Origin Detail", "OriginDetail"),
        ("Dest. Detail", "DestDetail"),
        ("Commodity", "Commodity"),
        ("Status", "Status"),
    ]
    wb = openpyxl.load_workbook(export_path, read_only=True, data_only=True)
    try:
        ws = wb["Opportunities"]
        headers = [str(c).strip() if c else "" for c in
                   next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
        index = {h: i for i, h in enumerate(headers)}
        rows = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row[index["Enquiry No."]] in (None, ""):
                continue
            record = []
            for header, _ in wanted:
                value = row[index[header]]
                if header == "Date Received":
                    if isinstance(value, dt.datetime):
                        value = value.date().isoformat()
                    elif isinstance(value, dt.date):
                        value = value.isoformat()
                    else:
                        value = str(value).strip() if value is not None else ""
                else:
                    value = str(value).strip() if value is not None else ""
                record.append(value)
            rows.append(record)
    finally:
        wb.close()
    return export_csv.render([c for _, c in wanted], rows)


def check_opp_index(conn, export_path, csv_path) -> Check:
    ours = Path(csv_path).read_bytes()
    theirs = _powershell_equivalent_csv(export_path).encode("utf-8")
    lines = [
        "opp-index.csv {} bytes; Export-OppIndex equivalent {} bytes".format(
            len(ours), len(theirs)),
    ]
    ok = ours == theirs
    if not ok:
        a = ours.decode("utf-8").split("\r\n")
        b = theirs.decode("utf-8").split("\r\n")
        differing = [i for i in range(max(len(a), len(b)))
                     if (a[i] if i < len(a) else None) != (b[i] if i < len(b) else None)]
        lines.append("differing lines: {}".format(len(differing)))
        for i in differing[:10]:
            lines.append("   line {}".format(i + 1))
            lines.append("     app: {}".format(a[i] if i < len(a) else "<missing>"))
            lines.append("     ps : {}".format(b[i] if i < len(b) else "<missing>"))
    else:
        lines.append("identical, byte for byte ({} data rows)".format(
            ours.decode("utf-8").count("\r\n") - 1))
    starts_with_bom = ours.startswith(b"\xef\xbb\xbf")
    all_quoted = all(field.startswith('"') for field in
                     ours.decode("utf-8-sig").split("\r\n")[0].split(","))
    lines.append("BOM {}   CRLF {}   all fields quoted {}".format(
        starts_with_bom, b"\r\n" in ours, all_quoted))
    return Check("6. opp-index.csv equals the Export-OppIndex output",
                 ok and starts_with_bom and all_quoted, lines)


def _contacts_added_since(conn) -> set:
    """Contact ids inserted by anything but the migration (per change_log)."""
    return {r["row_id"] for r in conn.execute(
        "SELECT row_id FROM change_log WHERE table_name = 'contact' "
        "AND field = '(insert)' AND source <> 'migration'")}


def check_contact_emails(conn, wb) -> Check:
    workbook_emails = set()
    for row in _sheet_rows(wb["Agent_Master"]):
        for part in str(row[8] or "").split(";"):
            part = part.strip()
            if part:
                workbook_emails.add(part.lower())
    # Contacts the tracker, the agent-development list or the app added are not the
    # workbook's to answer for: named, and left out of the comparison.
    added = _contacts_added_since(conn)
    db_emails = {
        r["e"] for r in conn.execute(
            "SELECT id, lower(trim(email)) AS e FROM contact "
            "WHERE email IS NOT NULL AND trim(email) <> ''")
        if r["id"] not in added
    }
    lines = [
        "distinct emails: workbook {}   db {}   diff {:+d}".format(
            len(workbook_emails), len(db_emails),
            len(db_emails) - len(workbook_emails)),
        "contact rows {}".format(conn.execute(
            "SELECT COUNT(*) AS n FROM contact").fetchone()["n"]),
    ]
    if added:
        lines.append("not compared: {} contact(s) added since the migration "
                     "(tracker, agent directory, the app)".format(len(added)))
    for email in sorted(workbook_emails - db_emails)[:10]:
        lines.append("   missing from db: {}".format(email))
    for email in sorted(db_emails - workbook_emails)[:10]:
        lines.append("   only in db: {}".format(email))
    return Check("7. contact emails match the workbook",
                 workbook_emails == db_emails, lines)


# -------------------------------------------------------------------- entry --
def run(conn: sqlite3.Connection, workbook=None, export_path=None,
        csv_path=None) -> VerifyResult:
    result = VerifyResult()
    reference = demo_reference(conn)
    if reference is not None and not workbook:
        # a demo database has no workbook behind it: its counts are checked
        # against the demo data, the rest as usual
        result.checks.append(check_demo_distribution(conn, reference))
        result.checks.append(check_events(conn))
        result.checks.append(check_placeholders(conn))
        return result
    workbook = Path(workbook or paths.SOURCE_WORKBOOK)
    wb = openpyxl.load_workbook(workbook, read_only=True, data_only=True)
    try:
        result.checks.append(check_counts(conn, wb))
        result.checks.append(check_status_distribution(conn, wb))
        result.checks.append(check_events(conn))
        result.checks.append(check_placeholders(conn))
        if export_path and Path(export_path).exists():
            result.checks.append(check_workbook_cells(conn, wb, export_path))
            if csv_path and Path(csv_path).exists():
                result.checks.append(check_opp_index(conn, export_path, csv_path))
        result.checks.append(check_contact_emails(conn, wb))
    finally:
        wb.close()
    return result
