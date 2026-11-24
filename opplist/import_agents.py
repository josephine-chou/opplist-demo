"""import-agents: the agent directory.

One sheet per country, one row per agent approached: who they are, who was
written to, when, whether they answered, whether there was a call. The
agents become parties (MFN members, stage 'prospect' when new), their
people become contacts, and the outreach becomes the party timeline.

Matching an agent to a party already on file is deliberately narrow: a
domain that is already theirs, or a name that normalises to exactly the
same words. A name that merely contains the other's words goes to
data\\agent-directory-review.csv for a person to look at; it is never merged by
itself. Re-running adds nothing twice: events are keyed on (party, type,
date, source='directory'), contacts on their address, domains on the domain.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

from . import db, matching, paths

SKIP_SHEETS = ("outreach summary report",)
YEAR = 2026
#: When the outreach text carries no date at all.
UNDATED = "{}-01-01".format(YEAR)

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_DAY_MONTH = re.compile(
    r"(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:of\s+)?([A-Za-z]{3,9})\b")
_MONTHS = {m[:3]: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"), start=1)}
#: Sheet names that are not spelt like the country table.
COUNTRY_FIXES = {"paraquay": "Paraguay"}


@dataclass
class DirectoryReport:
    sheets: int = 0
    agents: int = 0
    matched_by_domain: int = 0
    matched_by_name: int = 0
    created: int = 0
    review: list = field(default_factory=list)   # (country, directory name, party key, why)
    review_path: str = ""
    mfn_set: int = 0
    domains_learned: int = 0
    contacts_added: int = 0
    events_added: int = 0
    events_skipped: int = 0                       # already there
    unknown_countries: list = field(default_factory=list)
    undated: int = 0

    def lines(self) -> list:
        out = [
            "sheets read            {:5d}".format(self.sheets),
            "agents read            {:5d}".format(self.agents),
            "matched by domain      {:5d}".format(self.matched_by_domain),
            "matched by name        {:5d}".format(self.matched_by_name),
            "party created          {:5d}".format(self.created),
            "for review (not merged){:5d}  -> {}".format(len(self.review),
                                                        self.review_path or "-"),
            "mfn_member set         {:5d}".format(self.mfn_set),
            "party_domain learned   {:5d}".format(self.domains_learned),
            "contact added          {:5d}".format(self.contacts_added),
            "party_event added      {:5d}  (already there {})".format(
                self.events_added, self.events_skipped),
            "outreach with no date  {:5d}  (dated {})".format(self.undated, UNDATED),
        ]
        if self.unknown_countries:
            out.append("sheets whose country is not in the country table: "
                       + ", ".join(sorted(set(self.unknown_countries))))
        return out


@dataclass
class Agent:
    country: str
    name: str
    contact_names: list
    titles: list
    emails: list
    outreach: str
    connected: bool
    zoom: bool
    remarks: str
    interests: str


# ------------------------------------------------------------ the workbook --
def _text(value) -> str:
    return str(value).replace("\xa0", " ").strip() if value is not None else ""


def _yes(value) -> bool:
    return _text(value).lower().startswith("y")


def _lines(value) -> list:
    """Cells hold several people, one per line or separated by ' / '."""
    out = []
    for line in re.split(r"[\n\r]+", _text(value)):
        for part in re.split(r"\s+/\s+", line):
            part = part.strip(" ,;-")
            if part and not _EMAIL.fullmatch(part):
                out.append(part)
    return out


def country_of_sheet(conn, title: str, report: DirectoryReport) -> str | None:
    wanted = _text(title).casefold()
    wanted = COUNTRY_FIXES.get(wanted, wanted)
    row = conn.execute("SELECT name FROM country WHERE lower(name) = lower(?)",
                       (wanted,)).fetchone()
    if row is None:
        report.unknown_countries.append(_text(title))
        return None
    return row["name"]


def _header_index(cells) -> dict | None:
    lowered = [c.casefold() for c in cells]
    if not any(c.startswith("agent name") for c in lowered):
        return None
    index = {}
    for position, name in enumerate(lowered):
        for wanted, key in (("agent name", "name"), ("agent contact name", "contact"),
                            ("agent contact", "contact"), ("contact title", "title"),
                            ("contact email", "email"), ("outreach date", "outreach"),
                            ("successful connection", "connected"),
                            ("zoom meeting", "zoom"), ("areas of interest", "interests"),
                            ("remarks", "remarks")):
            if name.startswith(wanted) and key not in index:
                index[key] = position
    return index


def read_workbook(path, conn, report: DirectoryReport) -> list:
    """Every agent row on every country sheet."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    agents = []
    try:
        for ws in wb.worksheets:
            if ws.title.strip().casefold() in SKIP_SHEETS:
                continue
            report.sheets += 1
            country = country_of_sheet(conn, ws.title, report)
            index = None
            for row in ws.iter_rows(values_only=True):
                cells = [_text(c) for c in row]
                if index is None:
                    index = _header_index(cells)
                    continue
                name = re.sub(r"\s+", " ", cells[index["name"]]) if index.get("name") is not None \
                    and index["name"] < len(cells) else ""
                if not name:
                    continue

                def cell(key):
                    position = index.get(key)
                    return cells[position] if position is not None and position < len(cells) else ""

                emails = []
                for value in cells:
                    for found in _EMAIL.findall(value):
                        found = found.lower()
                        if found not in emails:
                            emails.append(found)
                agents.append(Agent(
                    country=country, name=name,
                    contact_names=_lines(cell("contact")),
                    titles=_lines(cell("title")), emails=emails,
                    outreach=cell("outreach"), connected=_yes(cell("connected")),
                    zoom=_yes(cell("zoom")), remarks=cell("remarks"),
                    interests=cell("interests")))
    finally:
        wb.close()
    report.agents = len(agents)
    return agents


# ------------------------------------------------------------- the dates --
def outreach_dates(text: str) -> list:
    """'Email sent 5 Jan / 24 Feb' -> ['2026-01-05', '2026-02-24'].

    'First email outreach on the 28 january / 10 of March' works the same
    way; a part with a month but no day ('/ March') is left out. An empty
    list means the text carried no date.
    """
    out = []
    for day, month in _DAY_MONTH.findall(text or ""):
        number = _MONTHS.get(month[:3].lower())
        if not number:
            continue
        try:
            when = dt.date(YEAR, number, int(day)).isoformat()
        except ValueError:
            continue
        if when not in out:
            out.append(when)
    return out


# --------------------------------------------------------------- matching --
def _domains(emails) -> list:
    out = []
    for email in emails:
        domain = matching.domain_of(email)
        if domain and domain not in matching.FREE_MAIL_DOMAINS and domain not in out:
            out.append(domain)
    return out


def find_party(conn, agent: Agent) -> tuple:
    """(party_id, how) - how is 'domain', 'name', or '' with the review
    candidates as a third item when a name only contains the other's words."""
    for domain in _domains(agent.emails):
        found = matching.by_domain(conn, domain)
        if found:
            return found[0].party_id, "domain", []
    wanted = set(matching.normalise_name(agent.name))
    if not wanted:
        return None, "", []
    review = []
    for row in conn.execute("SELECT id, key, full_name FROM party WHERE placeholder = 0"):
        for field_name in ("key", "full_name"):
            tokens = set(matching.normalise_name(row[field_name]))
            if not tokens:
                continue
            if tokens == wanted:
                return row["id"], "name", []
            if tokens <= wanted and any(len(t) >= 4 for t in tokens):
                review.append((row["key"], "party '{}' is contained in '{}'".format(
                    row[field_name], agent.name)))
                break
    return None, "", review


# ----------------------------------------------------------------- import --
def _note(existing, line) -> str:
    existing = (existing or "").rstrip()
    if line in existing:
        return existing
    return (existing + "\n" + line).strip() if existing else line


def _learn_domain(repo, party_id, domain, report) -> None:
    owner = repo.conn.execute("SELECT party_id FROM party_domain WHERE domain = ?",
                              (domain,)).fetchone()
    if owner is None and repo.attach_domain(party_id, domain):
        report.domains_learned += 1


def _add_contacts(repo, party_id, agent: Agent, report) -> None:
    names = agent.contact_names
    for index, email in enumerate(agent.emails):
        if repo.conn.execute("SELECT 1 FROM contact WHERE lower(email) = ?",
                             (email,)).fetchone():
            continue
        name = names[index] if index < len(names) else None
        title = agent.titles[index] if index < len(agent.titles) else None
        repo.insert("contact", {"party_id": party_id, "name": name, "email": email,
                                "title": title, "is_primary": 0})
        report.contacts_added += 1


def _add_event(repo, party_id, at, kind, summary, report) -> None:
    exists = repo.conn.execute(
        "SELECT 1 FROM party_event WHERE party_id = ? AND type = ? AND at = ? "
        "AND source = 'directory'", (party_id, kind, at)).fetchone()
    if exists:
        report.events_skipped += 1
        return
    repo.insert("party_event", {"party_id": party_id, "at": at, "type": kind,
                                "summary": summary, "source": "directory"})
    report.events_added += 1


def import_agent(repo, agent: Agent, report: DirectoryReport) -> None:
    conn = repo.conn
    party_id, how, review = find_party(conn, agent)
    label = "Agent directory ({})".format(agent.country or "country unknown")
    if party_id is not None:
        if how == "domain":
            report.matched_by_domain += 1
        else:
            report.matched_by_name += 1
        row = conn.execute("SELECT mfn_member, full_name, notes FROM party WHERE id = ?",
                           (party_id,)).fetchone()
        values = {"notes": _note(row["notes"], label)}
        if row["mfn_member"] != 1:
            values["mfn_member"] = 1
            report.mfn_set += 1
        if not row["full_name"]:
            values["full_name"] = agent.name
        repo.update("party", party_id, values)
    else:
        for key, why in review:
            report.review.append((agent.country or "", agent.name, key, why))
        party_id = repo.add_party(
            agent.name, full_name=agent.name, type="Agent", stage="prospect",
            source="directory", country=agent.country, mfn_member=1, notes=label)
        report.created += 1
        report.mfn_set += 1
    for domain in _domains(agent.emails):
        _learn_domain(repo, party_id, domain, report)
    _add_contacts(repo, party_id, agent, report)

    dates = outreach_dates(agent.outreach)
    if agent.outreach and not dates:
        # No date in the text: first of the year, the text kept as the summary.
        report.undated += 1
        dates = [UNDATED]
    for when in dates:
        _add_event(repo, party_id, when, "outreach",
                   agent.outreach if when == UNDATED
                   else "outreach (agent directory): {}".format(agent.outreach)[:200], report)
    last = dates[-1] if dates else UNDATED
    if agent.connected:
        _add_event(repo, party_id, last, "reply", "replied (agent directory)", report)
    if agent.zoom:
        _add_event(repo, party_id, last, "meeting", "Zoom meeting (agent directory)", report)
    if agent.remarks:
        summary = agent.remarks
        if agent.interests:
            summary = "{} | interests: {}".format(summary, agent.interests)
        _add_event(repo, party_id, last, "note", summary[:500], report)


def write_review(rows, path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Country", "Directory name", "Existing party", "Why"])
        writer.writerows(rows)
    return path


def run(conn: sqlite3.Connection, workbook=None, review_path=None,
        repo=None) -> DirectoryReport:
    from . import db
    repo = repo or db.Repository(conn, source="import")
    report = DirectoryReport()
    agents = read_workbook(workbook or paths.AGENT_DIRECTORY_WORKBOOK, conn, report)
    for agent in agents:
        import_agent(repo, agent, report)
    review_path = Path(review_path or (paths.DATA_DIR / "agent-directory-review.csv"))
    write_review(report.review, review_path)
    report.review_path = str(review_path)
    repo.commit()
    return report


# ------------------------------------------------- domains only --
@dataclass
class DomainReport:
    agents: int = 0
    with_domains: int = 0
    matched: int = 0
    unmatched: int = 0
    added: int = 0
    already: int = 0
    taken: int = 0
    lines_added: list = field(default_factory=list)
    lines_unmatched: list = field(default_factory=list)

    def lines(self) -> list:
        out = [
            "agents in the workbook     : {}".format(self.agents),
            "  with an e-mail domain    : {}".format(self.with_domains),
            "  matched to a party       : {}".format(self.matched),
            "  not matched (skipped)    : {}".format(self.unmatched),
            "domains added              : {}".format(self.added),
            "  already on the party     : {}".format(self.already),
            "  owned by another party   : {}".format(self.taken),
        ]
        if self.lines_added:
            out.append("")
            out.extend("  + {}".format(line) for line in self.lines_added)
        if self.lines_unmatched:
            out.append("")
            out.append("not matched:")
            out.extend("  ? {}".format(line) for line in self.lines_unmatched[:40])
            if len(self.lines_unmatched) > 40:
                out.append("  ... {} more".format(len(self.lines_unmatched) - 40))
        return out


def add_domains(conn: sqlite3.Connection, workbook=None) -> DomainReport:
    """Every e-mail domain in the agent-development workbook onto the party its agent is
    matched to (by domain, then by name). Adds only; a domain another party
    already owns is left where it is. Rerunnable: the second run adds none.
    """
    repo = db.Repository(conn)
    report = DomainReport()
    directory = DirectoryReport()
    agents = read_workbook(workbook or paths.AGENT_DIRECTORY_WORKBOOK, conn, directory)
    report.agents = len(agents)
    for agent in agents:
        domains = _domains(agent.emails)
        if not domains:
            continue
        report.with_domains += 1
        party_id, how, _ = find_party(conn, agent)
        if not party_id:
            report.unmatched += 1
            report.lines_unmatched.append("{} ({})".format(agent.name, ", ".join(domains)))
            continue
        report.matched += 1
        for domain in domains:
            owner = conn.execute("SELECT party_id FROM party_domain WHERE domain = ?",
                                 (domain,)).fetchone()
            if owner is None:
                if not repo.attach_domain(party_id, domain):
                    continue                  # our own domain (13g)
                report.added += 1
                key = conn.execute("SELECT key FROM party WHERE id = ?", (party_id,)).fetchone()["key"]
                report.lines_added.append("{} -> {}".format(domain, key))
            elif owner["party_id"] == party_id:
                report.already += 1
            else:
                report.taken += 1
    repo.commit()
    return report
