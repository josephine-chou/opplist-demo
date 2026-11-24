"""export-analysis: three flat CSV files for analysis outside the app.

    data\\analysis\\enquiries.csv        one row per case (no duplicates, no
                                        mis-registrations)
    data\\analysis\\vendor_requests.csv  one row per vendor request
    data\\analysis\\parties.csv          one row per party

Everything derived is read the way the app reads it - v_enquiry, the
report's group and outcome rules, the same rate-used labels - so a number
in a pivot table is the number on the screen. UTF-8 with BOM, so Excel
opens the files without asking.
"""

from __future__ import annotations

import csv
import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import matching, paths, report, words
from .status import RATE_USED_LABELS

ENQUIRY_COLUMNS = (
    "opp_no", "received_on", "quoted_on", "closed_on", "days_to_quote", "status",
    "outcome", "closed_reason", "requester", "requester_type", "requester_country",
    "end_customer", "origin_country", "origin_detail", "dest_country", "dest_detail",
    "lane_latam", "direction", "mode", "commodity", "rate_used", "awarded_vendor",
    "vendor_requests_sent", "vendor_replied", "scope", "hold_reason", "hold_until",
)
VENDOR_COLUMNS = (
    "opp_no", "vendor", "vendor_type", "vendor_country", "vendor_region",
    "vendor_mfn", "sent_on", "replied_on", "status", "awarded", "voided",
)
PARTY_COLUMNS = (
    "party", "type", "country", "region", "mfn", "on_agent_directory",
    "inbound_requests", "inbound_last_on", "inbound_won",
    "outbound_requests", "outbound_last_on", "outbound_replied", "outbound_awarded",
    "directory_outreach_events", "directory_last_event_on", "first_seen_on",
)

#: The report's six groups, in the words the analysis file uses.
REQUESTER_TYPES = {
    "MFN agents": "LATAM agent MFN",
    "LATAM agents": "LATAM agent not MFN",
    "Our offices": "own office",
    "Direct customers": "Direct customer",
    "Other agents": "Other agent",
    "Unidentified": "TBC",
}
#: The report's seven outcome buckets folded into the six asked for:
#: a stale case is still in progress, it has just gone quiet.
OUTCOMES = {
    "Won": "Won",
    "Lost": "Lost",
    "Cancelled": "Cancelled",
    "Quoted, awaiting customer": "Awaiting",
    "In progress (not quoted)": "In progress",
    "Stale": "In progress",
    "No response": "No response",
    "Unknown": "Unknown",
}
RATE_USED = {"agent": "Agent", "office": "own office", "own": "Own",
             "carrier": "Carrier direct", "not_recorded": "Not recorded"}


def _detail(location, alt) -> str:
    """'MIA' / 'MIA / 100 Example Way, Doral' - the place, then the address."""
    parts = [p for p in (location, alt) if p]
    return " / ".join(parts)


@dataclass
class AnalysisReport:
    out_dir: Path
    enquiries: int = 0
    vendor_requests: int = 0
    parties: int = 0

    def lines(self) -> list:
        return [
            "enquiries.csv        {:5d} rows".format(self.enquiries),
            "vendor_requests.csv  {:5d} rows".format(self.vendor_requests),
            "parties.csv          {:5d} rows".format(self.parties),
            "folder: {}".format(self.out_dir),
        ]


def _yn(value) -> str:
    return "Y" if value else "N"


def _days(later, earlier):
    try:
        return (dt.date.fromisoformat(str(later)[:10])
                - dt.date.fromisoformat(str(earlier)[:10])).days
    except (TypeError, ValueError):
        return ""


# ------------------------------------------------------------- enquiries --
def enquiry_rows(conn: sqlite3.Connection, today=None) -> list:
    today = today or dt.date.today().isoformat()
    sql = """
        SELECT e.*, p.key AS requester_key2, ap.key AS awarded_vendor,
               {grp} AS grp, {outcome} AS outcome_bucket,
               (SELECT COUNT(*) FROM vendor_request v
                 WHERE v.enquiry_id = e.id AND v.voided = 0) AS sent,
               (SELECT COUNT(*) FROM vendor_request v
                 WHERE v.enquiry_id = e.id AND v.voided = 0
                   AND v.replied_on IS NOT NULL) AS replied
        FROM v_enquiry e
        JOIN party p ON p.id = e.requester_party_id
        LEFT JOIN party ap ON ap.id = e.awarded_vendor_id
        WHERE {real}
        ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER)
    """.format(grp=report.group_of_requester(), outcome=report.outcome_sql(today),
               real=report.real_cases())
    out = []
    for r in conn.execute(sql):
        latam = r["latam_lane"] == "Y"                  # v36: the Settings list
        out.append({
            "opp_no": r["opp_no"],
            "received_on": r["received_on"] or "",
            "quoted_on": r["quoted_on"] or "",
            "closed_on": r["closed_on"] or "",
            "days_to_quote": _days(r["quoted_on"], r["received_on"])
            if r["quoted_on"] and r["received_on"] else "",
            "status": r["status"],
            "outcome": OUTCOMES.get(r["outcome_bucket"], r["outcome_bucket"]),
            "closed_reason": r["closed_reason"] or "",
            "requester": r["requester_key"],
            "requester_type": REQUESTER_TYPES.get(r["grp"], r["grp"]),
            "requester_country": r["requester_country"] or "",
            "end_customer": r["end_customer"] or "",
            "origin_country": r["origin_country"] or "",
            "origin_detail": _detail(r["origin_loc"], r["origin_alt"]),
            "dest_country": r["dest_country"] or "",
            "dest_detail": _detail(r["dest_loc"], r["dest_alt"]),
            "lane_latam": _yn(latam),
            "direction": words.direction_label(r["direction"]),
            "mode": r["service_mode"] or "",
            "commodity": r["commodity"] or "",
            "rate_used": RATE_USED.get(r["rate_used"] or "", ""),
            "awarded_vendor": r["awarded_vendor"] or "",
            "vendor_requests_sent": r["sent"],
            "vendor_replied": r["replied"],
            "scope": r["scope"],
            "hold_reason": r["hold_reason"] or "",
            "hold_until": r["hold_until"] or "",
        })
    return out


# ------------------------------------------------------ vendor requests --
def vendor_rows(conn: sqlite3.Connection) -> list:
    out = []
    for r in conn.execute("""
        SELECT e.opp_no, p.key AS vendor, p.type, p.country, c.region, p.mfn_member,
               v.asked_on, v.replied_on, v.machine_status, v.awarded, v.voided
        FROM vendor_request v
        JOIN enquiry e ON e.id = v.enquiry_id
        JOIN party p ON p.id = v.vendor_party_id
        LEFT JOIN country c ON c.name = p.country
        ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER), v.asked_on, v.id"""):
        out.append({
            "opp_no": r["opp_no"], "vendor": r["vendor"], "vendor_type": r["type"],
            "vendor_country": r["country"] or "", "vendor_region": r["region"] or "",
            "vendor_mfn": _yn(r["mfn_member"] == 1),
            "sent_on": r["asked_on"] or "", "replied_on": r["replied_on"] or "",
            "status": r["machine_status"] or "",
            "awarded": _yn(r["awarded"]), "voided": _yn(r["voided"]),
        })
    return out


# --------------------------------------------------------------- parties --
def party_rows(conn: sqlite3.Connection) -> list:
    """Every live party (a merged one has nothing left to count)."""
    out = []
    for r in conn.execute("""
        SELECT p.id, p.key, p.type, p.country, c.region, p.mfn_member, p.source,
               p.notes, p.created_at,
               (SELECT COUNT(*) FROM enquiry e WHERE e.requester_party_id = p.id) AS inbound,
               (SELECT MAX(e.received_on) FROM enquiry e WHERE e.requester_party_id = p.id) AS inbound_last,
               (SELECT MIN(e.received_on) FROM enquiry e WHERE e.requester_party_id = p.id) AS inbound_first,
               (SELECT COUNT(*) FROM enquiry e WHERE e.requester_party_id = p.id
                  AND e.status = 'Won') AS inbound_won,
               (SELECT COUNT(*) FROM vendor_request v WHERE v.vendor_party_id = p.id
                  AND v.voided = 0) AS outbound,
               (SELECT MAX(v.asked_on) FROM vendor_request v WHERE v.vendor_party_id = p.id
                  AND v.voided = 0) AS outbound_last,
               (SELECT MIN(v.asked_on) FROM vendor_request v WHERE v.vendor_party_id = p.id
                  AND v.voided = 0) AS outbound_first,
               (SELECT COUNT(*) FROM vendor_request v WHERE v.vendor_party_id = p.id
                  AND v.voided = 0 AND v.replied_on IS NOT NULL) AS outbound_replied,
               (SELECT COUNT(*) FROM vendor_request v WHERE v.vendor_party_id = p.id
                  AND v.voided = 0 AND v.awarded = 1) AS outbound_awarded,
               (SELECT COUNT(*) FROM party_event pe WHERE pe.party_id = p.id
                  AND pe.source = 'directory') AS directory_events,
               (SELECT MAX(pe.at) FROM party_event pe WHERE pe.party_id = p.id
                  AND pe.source = 'directory') AS directory_last,
               (SELECT MIN(pe.at) FROM party_event pe WHERE pe.party_id = p.id) AS event_first
        FROM party p
        LEFT JOIN country c ON c.name = p.country
        WHERE p.merged_into IS NULL
        ORDER BY p.key COLLATE NOCASE"""):
        on_directory = (r["source"] == "directory" or r["directory_events"] > 0
                   or "Agent directory" in (r["notes"] or ""))
        firsts = [d for d in (r["inbound_first"], r["outbound_first"], r["event_first"],
                              (r["created_at"] or "")[:10]) if d]
        out.append({
            "party": r["key"], "type": r["type"], "country": r["country"] or "",
            "region": r["region"] or "", "mfn": _yn(r["mfn_member"] == 1),
            "on_agent_directory": _yn(on_directory),
            "inbound_requests": r["inbound"], "inbound_last_on": r["inbound_last"] or "",
            "inbound_won": r["inbound_won"],
            "outbound_requests": r["outbound"], "outbound_last_on": r["outbound_last"] or "",
            "outbound_replied": r["outbound_replied"],
            "outbound_awarded": r["outbound_awarded"],
            "directory_outreach_events": r["directory_events"],
            "directory_last_event_on": r["directory_last"] or "",
            "first_seen_on": min(firsts) if firsts else "",
        })
    return out


# ------------------------------------------------------------------ entry --
def _write(path: Path, columns, rows) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)


def run(conn: sqlite3.Connection, out_dir=None, today=None) -> AnalysisReport:
    out_dir = Path(out_dir or (paths.DATA_DIR / "analysis"))
    out_dir.mkdir(parents=True, exist_ok=True)
    report_ = AnalysisReport(out_dir=out_dir)
    rows = enquiry_rows(conn, today=today)
    _write(out_dir / "enquiries.csv", ENQUIRY_COLUMNS, rows)
    report_.enquiries = len(rows)
    rows = vendor_rows(conn)
    _write(out_dir / "vendor_requests.csv", VENDOR_COLUMNS, rows)
    report_.vendor_requests = len(rows)
    rows = party_rows(conn)
    _write(out_dir / "parties.csv", PARTY_COLUMNS, rows)
    report_.parties = len(rows)
    return report_
