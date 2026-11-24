"""The two CSV files the PowerShell pipeline reads.

The byte format is a contract, not a preference: UTF-8 with BOM, CRLF line
endings, every field wrapped in double quotes, embedded quotes doubled. That
is what Windows PowerShell's Export-Csv emits and what Sort-Inbox.ps1 and
Map-AgentEmails.ps1 have been reading all along. Changing it breaks them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import status as status_mod

BOM = "﻿"
CRLF = "\r\n"

OPP_INDEX_COLUMNS = (
    "OppNo",
    "DateReceived",
    "Agent",
    "Mode",
    "OriginDetail",
    "DestDetail",
    "Commodity",
    "Status",
)

AGENTS_COLUMNS = ("Agent", "FullName", "Country", "TotalEnquiries", "KnownEmail")


def quote(value) -> str:
    """PowerShell Export-Csv quotes everything and doubles inner quotes."""
    if value is None:
        text = ""
    else:
        text = str(value)
    return '"' + text.replace('"', '""') + '"'


def render(columns, rows) -> str:
    """Build the whole file as one string, header included."""
    lines = [",".join(quote(c) for c in columns)]
    lines.extend(",".join(quote(cell) for cell in row) for row in rows)
    return BOM + CRLF.join(lines) + CRLF


def write(path: str | Path, columns, rows) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline='' so that the CRLF above is written through untouched.
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(render(columns, rows))
    return path


# ---------------------------------------------------------------------------
def opp_index_rows(conn: sqlite3.Connection) -> list:
    """One row per case, in case-number order, as the workbook had them."""
    rows = conn.execute(
        "SELECT * FROM v_enquiry ORDER BY CAST(substr(opp_no, 5) AS INTEGER)"
    ).fetchall()
    out = []
    for row in rows:
        out.append((
            row["opp_no"],
            row["received_on"] or "",
            row["requester_key"] or "",
            row["service_mode"] or "",
            row["origin_loc"] or "",
            row["dest_loc"] or "",
            row["commodity"] or "",
            status_mod.row_to_legacy(row),
        ))
    return out


def agents_rows(conn: sqlite3.Connection) -> list:
    """KnownEmail keeps the old one-cell-many-values shape; Map-AgentEmails splits it."""
    rows = conn.execute(
        "SELECT id, key, full_name, country, total_enquiries FROM v_party_stats "
        "ORDER BY total_enquiries DESC, key COLLATE NOCASE"
    ).fetchall()
    out = []
    for row in rows:
        emails = [
            r["email"]
            for r in conn.execute(
                "SELECT email FROM contact WHERE party_id = ? AND email IS NOT NULL "
                "AND trim(email) <> '' ORDER BY is_primary DESC, id",
                (row["id"],),
            )
        ]
        seen = []
        for email in emails:
            if email.lower() not in [e.lower() for e in seen]:
                seen.append(email)
        out.append((
            row["key"],
            row["full_name"] or row["key"],
            row["country"] or "",
            row["total_enquiries"],
            "; ".join(seen),
        ))
    return out


def export_opp_index(conn: sqlite3.Connection, path: str | Path) -> Path:
    return write(path, OPP_INDEX_COLUMNS, opp_index_rows(conn))


def export_agents(conn: sqlite3.Connection, path: str | Path) -> Path:
    return write(path, AGENTS_COLUMNS, agents_rows(conn))
