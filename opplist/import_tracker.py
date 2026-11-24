r"""Load the rate-request tracker into vendor_request.

`data\rate-request-data.csv` is what the PowerShell scan produces: one row per
vendor per thread, with who was asked, when, and what came back. That is the
buy side of a case, and it is the half the app has never had.

Two rules shape everything here:

* re-running changes nothing it has already done. Rows are matched on RowKey,
  and a second run updates what the tracker knows (replied_on, machine status,
  the gist, quoted) and leaves alone what the app knows (awarded, notes).
* a row that cannot be tied to a case is counted and skipped, never guessed
  into one. The tracker has 221 rows and 111 of them carry a case number; the
  rest are threads nobody has registered.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import intake, matching, parsing, paths

#: 'seavio.example' in a Vendor column is a domain, not a company name.
_DOMAIN_LIKE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,}$")

_FREE_MAIL_KEYS = {intake.suggested_party_key(d).lower()
                   for d in matching.FREE_MAIL_DOMAINS}


def _looks_like_domain(value: str) -> bool:
    return bool(_DOMAIN_LIKE.match((value or "").strip()))

#: MachineStatus values that mean a usable rate came back.
QUOTED_STATUSES = ("quoted",)

_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


@dataclass
class TrackerReport:
    rows_read: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    no_opp_no: int = 0
    unknown_opp_no: list = field(default_factory=list)
    no_vendor: int = 0
    duplicate_row_keys: int = 0
    #: (opp_no, vendor label) for rows whose vendor is the case's requester:
    #: the customer side of the thread, which the tracker scan cannot tell
    #: from a vendor.
    requester_side: list = field(default_factory=list)
    awarded_cleared: int = 0
    parties_created: list = field(default_factory=list)
    domains_learned: int = 0
    contacts_added: int = 0
    thread_keys_set: int = 0
    thread_map_skipped: int = 0
    #: Rows with no OppNo that a case was found for anyway.
    matched_by_thread: int = 0
    matched_by_requester: int = 0
    #: (subject, asked_on, vendor) for the rows still without a case.
    unmatched: list = field(default_factory=list)

    def lines(self) -> list:
        out = [
            "rows read              {:5d}".format(self.rows_read),
            "vendor_request insert  {:5d}  updated {:5d}  unchanged {:5d}".format(
                self.inserted, self.updated, self.unchanged),
            "no case number         {:5d}  (case found by thread key {}, "
            "by requester domain {}, still without a case {})".format(
                self.no_opp_no + self.matched_by_thread + self.matched_by_requester,
                self.matched_by_thread, self.matched_by_requester, self.no_opp_no),
            "skipped, no vendor     {:5d}".format(self.no_vendor),
            "same RowKey twice      {:5d}  (merged)".format(
                self.duplicate_row_keys),
            "skipped, requester side{:5d}".format(len(self.requester_side)),
            "awarded cleared        {:5d}".format(self.awarded_cleared),
            "party created          {:5d}".format(len(self.parties_created)),
            "party_domain learned   {:5d}".format(self.domains_learned),
            "contact added          {:5d}".format(self.contacts_added),
            "thread_key set         {:5d}  (skipped {})".format(
                self.thread_keys_set, self.thread_map_skipped),
        ]
        if self.unknown_opp_no:
            out.append("case numbers not in the database: {}".format(
                ", ".join(sorted(set(self.unknown_opp_no)))))
        for opp_no, vendor in self.requester_side:
            out.append("   requester side: {} {}".format(opp_no, vendor))
        if self.parties_created:
            out.append("vendors created: " + ", ".join(self.parties_created))
        if self.unmatched:
            out.append("rows still without a case ({}):".format(len(self.unmatched)))
            for subject, asked_on, vendor in self.unmatched:
                out.append("   {:<10} {:<28} {}".format(
                    asked_on or "-", (vendor or "")[:28], (subject or "")[:70]))
        return out


def _text(value):
    value = (value or "").strip()
    return value or None


def _date(value):
    """'2026-08-25 09:35' -> '2026-08-25'. Anything else is no date."""
    found = _DATE.search(str(value or ""))
    if not found:
        return None
    try:
        return dt.date(*(int(part) for part in found.groups())).isoformat()
    except ValueError:
        return None


def _split(value) -> list:
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def _pairs(names, emails) -> list:
    """Names and addresses line up when the counts agree, and not otherwise."""
    if names and emails and len(names) == len(emails):
        return list(zip(names, emails))
    return [(None, email) for email in emails] + [(name, None) for name in names]


# ------------------------------------------------------------- the vendor --
def resolve_vendor(repo, row, report) -> int | None:
    """The party this row was asked of, creating it if this is the first time.

    Domain first, because that is a fact; the tracker's Vendor column is a
    label somebody typed. A vendor that is new here is created as an Agent,
    which is what the tracker's vendors are.
    """
    conn = repo.conn
    domain = (row.get("VendorDomain") or "").strip().lower().lstrip("@")
    key = (row.get("Vendor") or "").strip()

    if domain and domain not in matching.FREE_MAIL_DOMAINS:
        found = conn.execute(
            "SELECT party_id FROM party_domain WHERE domain = ?",
            (domain,)).fetchone()
        if found is not None:
            _add_contacts(repo, found["party_id"], row, report)
            return found["party_id"]

    if key:
        found = conn.execute(
            "SELECT id FROM party WHERE lower(trim(key)) = ?",
            (key.lower(),)).fetchone()
        if found is not None:
            _learn_domain(repo, found["id"], domain, report)
            _add_contacts(repo, found["id"], row, report)
            return found["id"]

    # The tracker writes the domain into the Vendor column when it does not
    # know a name, so 'seavio.example' becomes 'Seavio' rather than a party
    # called after a web address.
    if key and _looks_like_domain(key):
        key = intake.suggested_party_key(key)
    if not key and domain:
        key = intake.suggested_party_key(domain)
    if not key:
        return None
    if (domain and domain in matching.FREE_MAIL_DOMAINS) or \
            key.lower() in _FREE_MAIL_KEYS:
        # A thread with a gmail address and no company name identifies
        # nobody. Counted, not invented into the party list.
        return None

    known = conn.execute("SELECT id FROM party WHERE lower(trim(key)) = ?",
                         (key.lower(),)).fetchone()
    party_id = repo.add_party(
        key, type="Agent", country="Unknown", source="tracker",
        stage="active", notes="Added from the rate request tracker.")
    if known is None:
        report.parties_created.append(key)
    _learn_domain(repo, party_id, domain, report)
    _add_contacts(repo, party_id, row, report)
    return party_id


def _learn_domain(repo, party_id, domain, report) -> None:
    domain = (domain or "").strip().lower().lstrip("@")
    if not domain or domain in matching.FREE_MAIL_DOMAINS:
        return
    owner = repo.conn.execute(
        "SELECT party_id FROM party_domain WHERE domain = ?",
        (domain,)).fetchone()
    if owner is None and repo.attach_domain(party_id, domain):
        report.domains_learned += 1


def _add_contacts(repo, party_id, row, report) -> None:
    """The people on the thread, once each."""
    names = _split(row.get("Contacts"))
    emails = [e for e in _split(row.get("Emails")) if "@" in e]
    for index, (name, email) in enumerate(_pairs(names, emails)):
        if not email and not name:
            continue
        existing = repo.conn.execute(
            "SELECT id FROM contact WHERE party_id = ? "
            "AND COALESCE(lower(email), '') = COALESCE(lower(?), '') "
            "AND COALESCE(name, '') = COALESCE(?, '')",
            (party_id, email, name)).fetchone()
        if existing is not None:
            continue
        repo.insert("contact", {"party_id": party_id, "name": name,
                                "email": email, "is_primary": 0})
        report.contacts_added += 1
        if email:
            _learn_domain(repo, party_id, email.split("@", 1)[1], report)


# --------------------------------------------------------------- the rows --
#: What the tracker owns. Everything else on a vendor_request row belongs to
#: the app and a re-import must not touch it. 'awarded' in particular is
#: never written here: only a person, closing a case as Won, says whose rate
#: was used.
TRACKER_FIELDS = ("asked_on", "replied_on", "quoted", "their_ref",
                  "machine_status", "gist")
assert "awarded" not in TRACKER_FIELDS


def _values_from(row) -> dict:
    machine_status = _text(row.get("MachineStatus"))
    return {
        "asked_on": _date(row.get("DateAsked")),
        "replied_on": _date(row.get("DateReplied")),
        "quoted": 1 if (machine_status or "").strip().lower()
        in QUOTED_STATUSES else 0,
        "their_ref": _text(row.get("TheirRef")),
        "machine_status": machine_status,
        "gist": _text(row.get("ReplyGist")),
    }


def merge_duplicate_keys(rows, report) -> list:
    """One row per RowKey. The file sometimes carries the same ask twice.

    RowKey is case + vendor domain, so two rows with the same key are two
    mails on one ask: the earliest date it went out, the latest answer, and
    whatever the later row knows.
    """
    merged = {}
    order = []
    for row in rows:
        key = (row.get("RowKey") or "").strip()
        if not key or key not in merged:
            if key:
                merged[key] = dict(row)
                order.append(key)
            else:
                order.append(row)
            continue
        report.duplicate_row_keys += 1
        first = merged[key]
        asked = sorted(filter(None, (_date(first.get("DateAsked")),
                                     _date(row.get("DateAsked")))))
        replied = sorted(filter(None, (_date(first.get("DateReplied")),
                                       _date(row.get("DateReplied")))))
        for name in ("Subject", "TheirRef", "MachineStatus", "ReplyGist",
                     "Contacts", "Emails"):
            if (row.get(name) or "").strip():
                first[name] = row[name]
        if (row.get("MachineStatus") or "").strip().lower() in QUOTED_STATUSES:
            first["MachineStatus"] = row["MachineStatus"]
        first["DateAsked"] = asked[0] if asked else first.get("DateAsked")
        first["DateReplied"] = replied[-1] if replied else first.get("DateReplied")
    return [merged[item] if isinstance(item, str) else item for item in order]


#: The case window a vendor request falls in (propose uses the same).
WINDOW_BEFORE = 3
WINDOW_AFTER = 45
#: A RowKey prefix shorter than this matches too many keys to be trusted.
MIN_PREFIX = 16


def find_case_for(conn, row) -> tuple:
    """(enquiry_id, how) for a row with no case number, or (None, '').

    Two ways, both accepted only when exactly one case answers:
    1. the thread: the subject's thread key, or the RowKey's
       key prefix, against enquiry.thread_key / conversation_id;
    2. the requester: an outside domain on the row other than the vendor's
       that is some case's requester domain, with the row's asked_on
       inside that case's window (received - 3 days .. + 45 days).
    """
    subject = (row.get("Subject") or "").strip()
    key = parsing.subject_thread_key(subject) if subject else ""
    if key:
        hits = conn.execute(
            "SELECT id FROM enquiry WHERE legacy = 0 "
            "  AND (thread_key = ? OR conversation_id = ?)", (key, key)).fetchall()
        if len(hits) == 1:
            return hits[0]["id"], "thread key"
    prefix = (row.get("RowKey") or "").split("|", 1)[0].strip().upper()
    if len(prefix) >= MIN_PREFIX and "'" not in prefix:
        hits = conn.execute(
            "SELECT id FROM enquiry WHERE legacy = 0 AND thread_key IS NOT NULL "
            "  AND substr(thread_key, 1, ?) = ?", (len(prefix), prefix)).fetchall()
        if len(hits) == 1:
            return hits[0]["id"], "thread key"

    asked_on = _date(row.get("DateAsked")) or _date(row.get("DateReplied"))
    vendor_domain = (row.get("VendorDomain") or "").strip().lower().lstrip("@")
    others = set()
    for address in _split(row.get("Emails")):
        domain = matching.domain_of(address.strip().lower())
        if domain and domain != vendor_domain and \
                domain not in matching.FREE_MAIL_DOMAINS:
            others.add(domain)
    if others and asked_on:
        marks = ",".join("?" for _ in others)
        hits = conn.execute(
            "SELECT DISTINCT e.id FROM enquiry e "
            "WHERE e.legacy = 0 "
            "  AND e.received_on >= date(?, '-{after} days') "
            "  AND e.received_on <= date(?, '+{before} days') "
            "  AND (e.requester_party_id IN (SELECT party_id FROM party_domain "
            "                               WHERE domain IN ({marks})) "
            "       OR e.requester_party_id IN (SELECT party_id FROM contact "
            "                                  WHERE email LIKE '%@%' AND lower(substr(email, instr(email, '@') + 1)) IN ({marks})))"
            .format(after=WINDOW_AFTER, before=WINDOW_BEFORE, marks=marks),
            [asked_on, asked_on] + sorted(others) + sorted(others)).fetchall()
        if len(hits) == 1:
            return hits[0]["id"], "requester domain"
    return None, ""


def import_rows(repo, rows, report) -> None:
    conn = repo.conn
    rows = merge_duplicate_keys(rows, report)
    for row in rows:
        report.rows_read += 1
        # AL1: OppTag - the one case number the thread's subjects carry -
        # wins over the folder vote in OppNo
        opp_no = (row.get("OppTag") or row.get("OppNo") or "").strip().upper()
        if not opp_no:
            case_id, how = find_case_for(conn, row)
            if case_id is None:
                report.no_opp_no += 1
                report.unmatched.append((
                    (row.get("Subject") or "").strip(),
                    _date(row.get("DateAsked")) or _date(row.get("DateReplied")),
                    (row.get("Vendor") or row.get("VendorDomain") or "").strip()))
                continue
            if how == "thread key":
                report.matched_by_thread += 1
            else:
                report.matched_by_requester += 1
            case = {"id": case_id}
            opp_no = conn.execute("SELECT opp_no FROM enquiry WHERE id = ?",
                                  (case_id,)).fetchone()["opp_no"]
        else:
            case = conn.execute("SELECT id FROM enquiry WHERE opp_no = ?",
                                (opp_no,)).fetchone()
            if case is None:
                report.unknown_opp_no.append(opp_no)
                continue

        domain = (row.get("VendorDomain") or "").strip().lower().lstrip("@")
        if domain and domain in requester_domains(conn, case["id"]):
            # The scan lists every outside party on the thread. The one who
            # asked us is on it too, and is not somebody we asked.
            report.requester_side.append(
                (opp_no, (row.get("Vendor") or domain).strip()))
            continue

        row_key = (row.get("RowKey") or "").strip()
        values = _values_from(row)
        existing = None
        if row_key:
            existing = conn.execute(
                "SELECT * FROM vendor_request WHERE row_key = ?",
                (row_key,)).fetchone()
        if existing is None:
            vendor_party_id = resolve_vendor(repo, row, report)
            if vendor_party_id is None:
                report.no_vendor += 1
                continue
            repo.insert("vendor_request", dict(
                values, enquiry_id=case["id"], vendor_party_id=vendor_party_id,
                row_key=row_key or None, source="tracker"))
            report.inserted += 1
            continue

        changed = repo.update("vendor_request", existing["id"],
                              {k: values[k] for k in TRACKER_FIELDS})
        if changed:
            repo.touch("vendor_request", existing["id"])
            report.updated += 1
        else:
            report.unchanged += 1


def requester_domains(conn, enquiry_id: int) -> set:
    """Every domain the case's requester is known by: party_domain and the
    domains of their contacts' addresses."""
    row = conn.execute("SELECT requester_party_id FROM enquiry WHERE id = ?",
                       (enquiry_id,)).fetchone()
    if row is None:
        return set()
    party_id = row["requester_party_id"]
    domains = {r["domain"].lower() for r in conn.execute(
        "SELECT domain FROM party_domain WHERE party_id = ?", (party_id,))}
    for r in conn.execute(
            "SELECT email FROM contact WHERE party_id = ? AND email LIKE '%@%'",
            (party_id,)):
        domains.add(r["email"].rsplit("@", 1)[1].strip().lower())
    return domains


def clear_unearned_awards(repo, report) -> None:
    """The tracker never awards anything; only the app does, on a Won case.

    An awarded flag on a tracker row stays only where the case has a Won event
    and names that vendor as the one whose rate was used. Everything else is
    put back to 0 - it was never set by a person.
    """
    conn = repo.conn
    rows = conn.execute(
        "SELECT v.id FROM vendor_request v JOIN enquiry e ON e.id = v.enquiry_id "
        "WHERE v.source = 'tracker' AND v.awarded = 1 "
        "  AND NOT (e.awarded_vendor_id = v.vendor_party_id "
        "           AND EXISTS (SELECT 1 FROM status_event s "
        "                       WHERE s.enquiry_id = e.id AND s.status = 'Won' "
        "                         AND s.voided = 0 AND s.kind = 'status'))"
    ).fetchall()
    for row in rows:
        repo.update("vendor_request", row["id"], {"awarded": 0})
        report.awarded_cleared += 1


# ---------------------------------------------------------- the thread map --
def apply_thread_map(repo, rows, report) -> None:
    """Give a case its thread key when the map knows exactly one.

    Only where the case has no key of its own and the
    map ties exactly one thread to it. Two threads on one case is a question,
    not an answer.
    """
    by_case = {}
    for row in rows:
        opp_no = (row.get("OppNo") or "").strip().upper()
        key = (row.get("ThreadKey") or "").strip()
        if opp_no and key:
            by_case.setdefault(opp_no, set()).add(key)

    for opp_no, keys in sorted(by_case.items()):
        if len(keys) != 1:
            report.thread_map_skipped += 1
            continue
        case = repo.conn.execute(
            "SELECT id, thread_key FROM enquiry WHERE opp_no = ?",
            (opp_no,)).fetchone()
        if case is None:
            report.thread_map_skipped += 1
            continue
        if case["thread_key"]:
            continue
        repo.update("enquiry", case["id"], {"thread_key": next(iter(keys))})
        report.thread_keys_set += 1


# ------------------------------------------------------------------- entry --
def read_csv(path) -> list:
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def run(conn: sqlite3.Connection, csv_path=None, thread_map=None,
        repo=None) -> TrackerReport:
    """Import the tracker. Everything it writes goes through the repository."""
    from . import db

    repo = repo or db.Repository(conn, source="import")
    report = TrackerReport()
    import_rows(repo, read_csv(csv_path or paths.RATE_REQUEST_DATA), report)
    clear_unearned_awards(repo, report)
    apply_thread_map(repo, read_csv(thread_map or paths.THREAD_OPP_MAP), report)
    repo.commit()
    return report
