"""Who the reports count (batch SC1, 2026-11-10; before it batch 21,
2026-10-05; DC1, 2026-11-03; WA1, 2026-11-06).

Until SC1 these rules set scope Out, which hid the cases from Tracking, the
Assign List and the team site as well as from the reports. SC1 splits the
two layers. Work: every inquiry stays In, so the work pages show it; scope
is Out only when Iris sets it by hand under More (nothing sets it at
registration, and nothing when a requester changes from (TBC)). Report: the
same rule decides who the reports count, without touching scope:

    a case is counted when its scope is In, it is not Dropped (section 3's
    volume keeps the legacy rows, VOLUME), its requester is an Agent, a
    Office or a Direct Customer, and `out_reason` gives '' for it.

`out_reason` is the rule as batch 21, DC1 and WA1 wrote it, its LATAM test
reading the case's own latam flag (v_enquiry.latam, which since v49 is her
choice when she made one):

    BEFORE      a Direct Customer's case received before CUTOFF (or with no
                date): they were not registered systematically then
    NOT_LATAM   a Direct Customer's case from CUTOFF that is not LATAM
    OWN_OFFICE  a Office's case from CUTOFF that is not LATAM

A case from CUTOFF with a country missing (or Unknown) is not decided: it
counts, unless she set LATAM to no. LATAM set to yes counts it whatever its
countries. Cases another of our offices handles (handled_by) count like any
other. `out_sql` is the same rule in SQL, `counted_sql` the whole test; a
test checks the two agree on every invented case.

`run` (the not-latam-out command, alias direct-customers-out) only lists,
read only, the cases the report leaves out and why. scope_back puts back In
what the old rules set Out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The first day direct customers were registered systematically.
CUTOFF = "2026-10-01"
BEFORE = "before October"
NOT_LATAM = "not LATAM"
OFFICE = "Office"
OWN_OFFICE = "own office, not LATAM"
#: the requesters the rule looks at
TYPES = ("Direct Customer", OFFICE)
#: the requesters a report can name a group for
COUNTED_TYPES = ("Agent", OFFICE, "Direct Customer")
#: the refusal of not-latam-out --apply
NO_APPLY = ("not-latam-out only lists since SC1 (10 Nov 2026): the report leaves these cases out by "
            "itself, and their scope stays In; scope-back puts back In what it set Out.")


def latam_countries(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM v_latam_country")}


def _country(value) -> str:
    value = (value or "").strip()
    return "" if value == "Unknown" else value


def out_reason(party_type, received_on, origin, dest, latam, chosen="") -> str:
    """BEFORE, NOT_LATAM, OWN_OFFICE, or '' when the report counts the case
    (or the rule does not decide it: a country missing). `latam` is the
    case's flag ('Y' / 'N', v_enquiry.latam), `chosen` her choice (latam_choice:
    '', 'Y' or 'N')."""
    if party_type not in TYPES:
        return ""
    received = (received_on or "").strip()[:10]
    if not received or received < CUTOFF:
        return BEFORE if party_type == "Direct Customer" else ""
    chosen = (chosen or "").strip().upper()
    if chosen == "Y":
        return ""
    if chosen != "N":
        if not _country(origin) or not _country(dest):
            return ""
        if latam == "Y":
            return ""
    return NOT_LATAM if party_type == "Direct Customer" else OWN_OFFICE


def counted(scope, status, party_type, received_on, origin, dest, latam, chosen="",
            legacy=0, closed_reason="", volume=False) -> bool:
    """The whole test in Python (counted_sql is the same in SQL)."""
    if scope != "In" or party_type not in COUNTED_TYPES:
        return False
    if volume:
        reason = (closed_reason or "")
        if (status == "Dropped" and not legacy) or reason.casefold().startswith("duplicate") \
                or reason == "Registered in error":
            return False
    elif status == "Dropped":
        return False
    return out_reason(party_type, received_on, origin, dest, latam, chosen) == ""


def _sql_country(column) -> str:
    return "COALESCE(NULLIF(TRIM({}), 'Unknown'), '')".format(column)


def out_sql(e="e", p="p") -> str:
    """SQL: out_reason of a v_enquiry row `e` with its requester `p`."""
    received = "substr(TRIM(COALESCE({e}.received_on, '')), 1, 10)".format(e=e)
    reason = "CASE WHEN {p}.type = 'Direct Customer' THEN '{nl}' ELSE '{oo}' END".format(
        p=p, nl=NOT_LATAM, oo=OWN_OFFICE)
    return ("(CASE WHEN COALESCE({p}.type, '') NOT IN ('Direct Customer', '{office}') THEN '' "
            "WHEN {rec} = '' OR {rec} < '{cut}' "
            "THEN CASE WHEN {p}.type = 'Direct Customer' THEN '{before}' ELSE '' END "
            "WHEN UPPER(TRIM(COALESCE({e}.latam_choice, ''))) = 'Y' THEN '' "
            "WHEN UPPER(TRIM(COALESCE({e}.latam_choice, ''))) = 'N' THEN {reason} "
            "WHEN {o} = '' OR {d} = '' THEN '' "
            "WHEN {e}.latam = 'Y' THEN '' "
            "ELSE {reason} END)").format(
        p=p, e=e, office=OFFICE, rec=received, cut=CUTOFF, before=BEFORE, reason=reason,
        o=_sql_country(e + ".origin_country"), d=_sql_country(e + ".dest_country"))


def counted_sql(e="e", p="p", volume=False, with_dropped=False) -> str:
    """SQL: the cases a report counts - the one definition every report
    reads (monthly.CASES, report.in_month, the dashboard, the cheat sheet).
    `e` is a v_enquiry row, `p` its requester. volume=True is section 3's
    reading: the legacy rows stay, a duplicate or a mis-registration does not.
    with_dropped=True keeps the Dropped rows for a figure that counts them
    as such (the dashboard's Dropped slice); the rest of the rule holds."""
    if with_dropped:
        dropped = "1 = 1"
    else:
        dropped = ("({e}.status <> 'Dropped' OR {e}.legacy = 1) "
                   "AND COALESCE({e}.closed_reason, '') NOT LIKE 'Duplicate%' "
                   "AND COALESCE({e}.closed_reason, '') <> 'Registered in error'" if volume
                   else "{e}.status <> 'Dropped'").format(e=e)
    return ("({e}.scope = 'In' AND {dropped} AND {p}.type IN ({types}) AND {out} = '')".format(
        e=e, p=p, dropped=dropped, types=", ".join("'{}'".format(t) for t in COUNTED_TYPES),
        out=out_sql(e, p)))


def counted_ids_sql(volume=False, with_dropped=False) -> str:
    """SQL: the ids of the counted cases, for a query that reads routes
    (v_route carries the route's countries, not the case's)."""
    return ("(SELECT c.id FROM v_enquiry c JOIN party cp ON cp.id = c.requester_party_id WHERE {})"
            .format(counted_sql("c", "cp", volume, with_dropped)))


def is_tbc(conn, party_id) -> bool:
    """The (TBC) placeholder a case is parked on until we know who asked."""
    from .tracking import PLACEHOLDER_SEARCHING
    row = conn.execute("SELECT placeholder, key FROM party WHERE id = ?", (party_id,)).fetchone()
    return row is not None and bool(row["placeholder"]) and \
        (row["key"] or "").strip().casefold() == PLACEHOLDER_SEARCHING.casefold()


def reason_text(reason) -> str:
    """For a message: 'direct customer, not LATAM'."""
    return {BEFORE: "direct customer, received before Oct 1", NOT_LATAM: "direct customer, not LATAM",
            OWN_OFFICE: OWN_OFFICE}.get(reason, reason)


def reason_of(conn, enquiry_id) -> str:
    """out_reason of one case as it is now."""
    row = conn.execute(
        "SELECT {} AS reason FROM v_enquiry e JOIN party p ON p.id = e.requester_party_id WHERE e.id = ?".format(
            out_sql()), (enquiry_id,)).fetchone()
    return row["reason"] if row is not None else ""


@dataclass
class Report:
    rows: list = field(default_factory=list)     # (opp_no, received_on, requester, status, reason, lane, type, scope)

    def lines(self) -> list:
        out = ["read only: the cases the reports leave out, and why (scope is not changed)"]
        for reason in (BEFORE, NOT_LATAM, OWN_OFFICE):
            mine = [t for t in self.rows if t[4] == reason]
            out.append("{:<50}: {}".format(reason_text(reason), len(mine)))
            for opp, received, key, status, _, lane, _, scope in mine:
                out.append("  {}  {}  {}  {}  {}{}".format(opp, received or "(no date)", key, lane or "-", status,
                                                           "  (scope Out)" if scope == "Out" else ""))
        return out


def _lane(row) -> str:
    return "{} to {}".format(_country(row["origin_country"]) or "?", _country(row["dest_country"]) or "?")


def run(conn) -> Report:
    """Every case of a direct customer or one of our offices that the report
    leaves out, oldest first; legacy rows too. Writes nothing."""
    report = Report()
    for row in conn.execute(
            "SELECT e.opp_no, e.received_on, e.scope, e.status, e.origin_country, e.dest_country, "
            "p.key, p.type, {} AS reason FROM v_enquiry e JOIN party p ON p.id = e.requester_party_id "
            "WHERE p.type IN ('Direct Customer', 'Office') "
            "ORDER BY COALESCE(e.received_on, ''), e.opp_no".format(out_sql())):
        if row["reason"]:
            report.rows.append((row["opp_no"], row["received_on"], row["key"], row["status"], row["reason"],
                                _lane(row), row["type"], row["scope"]))
    return report
