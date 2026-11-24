"""The HS codes written into cargo lines' commodity text, moved into hs_code
(batch D7a, v50).

Before v50 the mail reader wrote a block's HS code into the commodity
('White clay (HS 2507.00.10)'). `python -m opplist hs-codes` lists, for
each live line whose hs_code is empty and whose commodity carries a code,
the code found and the commodity without it; the dry run writes
data\\proposed-hs-codes.csv. --apply moves them through the repository
(change_log; the case's commodity follows its lines). A commodity whose HS
part says more than the codes read (another number beside them) is left,
listed. The cases whose own commodity still carries an HS code are counted,
never changed.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import parsing

HEADERS = ("opp", "line", "commodity now", "HS code found", "commodity after")
#: the HS part of a commodity: in brackets (half or full width), or at the end
_HEADING = r"(?:H\.?\s?S\.?|HTS(?:US)?|NCM)(?:\s*codes?)?"
_IN_BRACKETS = re.compile(r"\s*[(（]\s*" + _HEADING + r"(?![A-Za-z])[^)）]*[)）]", re.IGNORECASE)
_AT_END = re.compile(r"\s*[-–,;|]?\s*(?<![A-Za-z])" + _HEADING + r"(?![A-Za-z])\s*[:：]?\s*[\d.\s,]+$",
                     re.IGNORECASE)


def without_hs(text: str, codes: str) -> str | None:
    """The commodity without its HS part, '' when nothing else is left;
    None when the part removed carries a number that is not one of `codes`
    (then it is left for her)."""
    removed = []

    def cut(m):
        removed.append(m.group(0))
        return ""

    after = _IN_BRACKETS.sub(cut, text or "")
    after = _AT_END.sub(cut, after)
    digits = {re.sub(r"\D", "", c) for c in codes.split(", ") if c}
    for part in removed:
        for run in re.findall(r"\d[\d.\s]*\d|\d", part):
            if re.sub(r"\D", "", run) not in digits:
                return None
    return " ".join(after.split()).strip(" -–,;|")


@dataclass
class Proposal:
    opp: str
    line_id: int
    line: str
    commodity: str
    hs: str
    after: str | None             # None: left for her


@dataclass
class Report:
    proposals: list = field(default_factory=list)
    left: list = field(default_factory=list)          # [(opp, line, commodity, why)]
    cases_with_hs: list = field(default_factory=list)  # [(opp, commodity)]
    moved: int = 0


def find(conn) -> Report:
    report = Report()
    for row in conn.execute(
            "SELECT c.id, c.seq, c.label, c.commodity, e.opp_no FROM enquiry_cargo c "
            "JOIN enquiry e ON e.id = c.enquiry_id WHERE c.voided = 0 AND COALESCE(c.hs_code, '') = '' "
            "AND COALESCE(c.commodity, '') <> '' ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER), c.seq"):
        codes, _where = parsing.hs_codes_in([row["commodity"]])
        if not codes:
            if re.search(r"(?<![A-Za-z])" + _HEADING + r"(?![A-Za-z])\s*[:：]?\s*\d", row["commodity"], re.I):
                report.left.append((row["opp_no"], _line_name(row), row["commodity"], "a code not 4 to 10 digits"))
            continue
        after = without_hs(row["commodity"], codes)
        proposal = Proposal(row["opp_no"], row["id"], _line_name(row), row["commodity"], codes, after)
        if after is None:
            report.left.append((row["opp_no"], proposal.line, row["commodity"], "the HS part names more than the codes read"))
        else:
            report.proposals.append(proposal)
    for row in conn.execute("SELECT opp_no, commodity FROM enquiry WHERE COALESCE(commodity, '') <> '' "
                            "ORDER BY CAST(substr(opp_no, 5) AS INTEGER)"):
        if parsing.hs_codes_in([row["commodity"]])[0]:
            report.cases_with_hs.append((row["opp_no"], row["commodity"]))
    return report


def _line_name(row) -> str:
    return str(row["label"] or row["seq"])


def write_csv(report: Report, path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        out = csv.writer(f)
        out.writerow(HEADERS)
        for p in report.proposals:
            out.writerow((p.opp, p.line, p.commodity, p.hs, p.after))
    return path


def apply(repo, report: Report) -> int:
    """hs_code set and the HS part taken out of the commodity, through the
    repository, only where hs_code is still empty."""
    moved = 0
    for p in report.proposals:
        row = repo.conn.execute("SELECT hs_code FROM enquiry_cargo WHERE id = ?", (p.line_id,)).fetchone()
        if row is None or (row["hs_code"] or "").strip():
            continue
        repo.edit_cargo_line(p.line_id, {"hs_code": p.hs, "commodity": p.after or None})
        moved += 1
    report.moved = moved
    return moved


def lines(report: Report, dry_run: bool) -> list:
    out = ["cargo lines with an HS code in the commodity: {}".format(len(report.proposals) + len(report.left)),
           "  to move: {}{}".format(len(report.proposals), "" if dry_run else " (moved {})".format(report.moved)),
           "  left for her: {}".format(len(report.left))]
    for opp, line, commodity, why in report.left:
        out.append("    {} line {}: {!r} - {}".format(opp, line, commodity, why))
    out.append("cases whose own commodity still carries an HS code (counted, not changed): {}".format(
        len(report.cases_with_hs)))
    return out
