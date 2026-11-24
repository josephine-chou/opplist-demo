"""The team site's vendor list (W2a, 2026-10-29): lists/vendors, what the
site's Ask vendors card picks from, as ..\\OppListWeb\\INTERFACE.md section 5
says. One row per vendor party (not merged away) with at least one live
vendor request, sorted by key: what we asked them and where, and the
address to write to.

The addresses come from the mail archive, read only, in one pass: the mails
we sent (sender_internal = 1) to addresses at the party's domains. `to` is
the address most often on To (a tie: the one written to last); none on To,
the most often on CC; none sent, the address that wrote to us most often
from those domains; none, the party's contact (is_primary first). `cc` is
up to three other addresses at its domains that were on at least two of the
same sent mails as `to`, most often first. One of our offices has no domain
of its own: only its contacts give it an address. No archive: contacts
only, and the note says why.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

#: the full name on the row, for the site's search box
FULL_MAX = 120
CC_MAX = 3
#: an address that never counts: a robot's
_ROBOTS = ("noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster")
#: the ends of a case that are not a vendor's country
_HOME = ("", "USA", "Unknown")


def is_robot(address: str) -> bool:
    return address.split("@", 1)[0].startswith(_ROBOTS)


def _address(value) -> str:
    """An SMTP address as the archive keeps it, lower case; '' for anything
    that is not one or is a robot's."""
    text = str(value or "").strip().strip("<>").strip().lower()
    if text.count("@") != 1 or " " in text or not text.split("@")[0] or "." not in text.split("@")[1]:
        return ""
    return "" if is_robot(text) else text


def far_countries(origin, dest) -> list:
    """The countries of a case a vendor works in: its ends outside the US
    (a US import: the origin; a US export: the destination; a cross-trade:
    both), never USA or Unknown."""
    out = []
    for country in (origin, dest):
        country = (country or "").strip()
        if country not in _HOME and country not in out:
            out.append(country)
    return out


#: Q2c: the roles that put a party on the list before it is asked
VENDOR_ROLES = ("carrier_air", "carrier_ocean", "coloader", "trucker")


def _roles(text) -> list:
    """'agent|coloader' -> ['agent', 'coloader'] in the party page's order."""
    from .migrate_master import ROLES
    found = {t.strip() for t in (text or "").split("|") if t.strip()}
    return [r for r in ROLES if r in found]


def _row_of(r) -> dict:
    from .firebase_sync import cut
    return {"key": r["key"], "name": r["key"], "full": cut(r["full_name"] or "", FULL_MAX),
            "type": r["type"] or "", "roles": _roles(r["roles"]),
            "country": "" if (r["country"] or "") == "Unknown" else (r["country"] or ""),
            "_asked": set(), "_quoted": set(), "_in": set(), "_qin": set(), "_modes": set(), "_parts": set(),
            "lastAsked": ""}


def _vendors(conn) -> dict:
    """{party id: the row without addresses} for every listed vendor: each
    party with a live vendor request, and (Q2b) every Carrier, Coloader and
    Trucker not merged away and not a placeholder, asked or not; each row
    with the parts it was asked for on its live requests."""
    from . import db, parts as parts_mod
    rows = {}
    for r in conn.execute(
            "SELECT p.id, p.key, p.full_name, p.type, p.roles, p.country, v.enquiry_id, v.asked_on, v.quoted, v.parts, "
            "e.origin_country, e.dest_country, e.service_mode "
            "FROM vendor_request v JOIN party p ON p.id = v.vendor_party_id JOIN enquiry e ON e.id = v.enquiry_id "
            "WHERE v.voided = 0 AND p.merged_into IS NULL ORDER BY p.id, v.id"):
        row = rows.get(r["id"])
        if row is None:
            row = rows[r["id"]] = _row_of(r)
        row["_parts"].update(parts_mod.split(r["parts"]))
        countries = far_countries(r["origin_country"], r["dest_country"])
        row["_asked"].add(r["enquiry_id"])
        row["_in"].update(countries)
        if r["service_mode"]:
            row["_modes"].add(r["service_mode"])
        if r["quoted"]:
            row["_quoted"].add(r["enquiry_id"])
            row["_qin"].update(countries)
        row["lastAsked"] = max(row["lastAsked"], (r["asked_on"] or "")[:10])
    # Q2b: a carrier, a coloader or a trucker is on the list before it is
    # asked; Q2c: so is a party whose roles say it is one
    for r in conn.execute(
            "SELECT id, key, full_name, type, roles, country FROM party WHERE merged_into IS NULL "
            "AND placeholder = 0 ORDER BY id"):
        if r["id"] not in rows and (r["type"] in db.CARRIER_TYPES or set(_roles(r["roles"])) & set(VENDOR_ROLES)):
            rows[r["id"]] = _row_of(r)
    for row in rows.values():
        row.update({"asked": len(row.pop("_asked")), "quoted": len(row.pop("_quoted")),
                    "askedIn": sorted(row.pop("_in")), "quotedIn": sorted(row.pop("_qin")),
                    "modes": sorted(row.pop("_modes")), "parts": parts_mod.split(parts_mod.SEP.join(row.pop("_parts")))})
    return rows


class Tally:
    """What the archive says about one party's addresses."""

    def __init__(self):
        # per line (to, cc, came = wrote to us): address -> [count, latest moment]
        self.lines = {"to": {}, "cc": {}, "came": {}}
        self.mails = []                               # the sets of its addresses on each sent mail

    def note(self, line, address, moment):
        found = self.lines[line].setdefault(address, [0, ""])
        found[0] += 1
        found[1] = max(found[1], moment)

    def best(self, line) -> str:
        """The most often; a tie: the latest, then the first by name."""
        counts = self.lines[line]
        if not counts:
            return ""
        order = sorted(counts)
        order.sort(key=lambda a: counts[a][1], reverse=True)           # stable: the latest first
        order.sort(key=lambda a: counts[a][0], reverse=True)           # then the most often
        return order[0]

    def pick(self) -> tuple:
        """(to, cc) from the archive alone; ('', []) when it knows nobody."""
        to = self.best("to") or self.best("cc") or self.best("came")
        if not to:
            return "", []
        together = {}
        for found in self.mails:
            if to in found:
                for other in found - {to}:
                    together[other] = together.get(other, 0) + 1
        cc = sorted((a for a, n in together.items() if n >= 2), key=lambda a: (-together[a], a))
        return to, cc[:CC_MAX]


def tally(archive_path, by_domain: dict) -> dict:
    """{party id: Tally} from one pass over the archive's mails. Raises
    sqlite3.Error / OSError when it cannot be read."""
    path = Path(archive_path)
    if not path.exists():
        raise FileNotFoundError("no mail archive at {}".format(path))
    out = {}
    conn = sqlite3.connect("file:{}?mode=ro".format(path.as_posix()), uri=True, timeout=10)
    try:
        for sender, internal, to_list, cc_list, sent_at, received_at in conn.execute(
                "SELECT sender_address, sender_internal, to_addresses, cc_addresses, sent_at, received_at "
                "FROM message"):
            moment = sent_at or received_at or ""
            if internal:
                seen = {}
                for line, raw in (("to", to_list), ("cc", cc_list)):
                    try:
                        addresses = json.loads(raw or "[]")
                    except ValueError:
                        continue
                    for value in addresses if isinstance(addresses, list) else []:
                        address = _address(value)
                        party = by_domain.get(address.rsplit("@", 1)[-1]) if address else None
                        if party is None:
                            continue
                        found = out.setdefault(party, Tally())
                        found.note(line, address, moment)
                        seen.setdefault(party, set()).add(address)
                for party, addresses in seen.items():
                    out[party].mails.append(addresses)
            else:
                address = _address(sender)
                party = by_domain.get(address.rsplit("@", 1)[-1]) if address else None
                if party is not None:
                    found = out.setdefault(party, Tally())
                    found.note("came", address, moment)
    finally:
        conn.close()
    return out


def _contacts(conn, party_ids) -> dict:
    """{party id: its contact address} - is_primary first, then the lowest id."""
    out = {}
    for r in conn.execute("SELECT party_id, email FROM contact WHERE email LIKE '%@%' "
                          "ORDER BY is_primary DESC, id"):
        if r["party_id"] in party_ids and r["party_id"] not in out:
            address = _address(r["email"])
            if address:
                out[r["party_id"]] = address
    return out


def rows(conn, archive_path=None) -> tuple:
    """(lists/vendors rows, note): the note is '' when the archive was read,
    else why the addresses are the contacts' only."""
    from . import mail_archive
    vendors = _vendors(conn)
    offices = {pid for pid, row in vendors.items() if row["type"] == "Office"}
    by_domain = {}
    for r in conn.execute("SELECT domain, party_id FROM party_domain"):
        if r["party_id"] in vendors and r["party_id"] not in offices:
            by_domain[(r["domain"] or "").strip().lower()] = r["party_id"]
    note = ""
    try:
        found = tally(archive_path or mail_archive.ARCHIVE_DB, by_domain)
    except (OSError, sqlite3.Error) as exc:
        found = {}
        note = str(exc) if isinstance(exc, FileNotFoundError) else "the mail archive could not be read ({}: {})".format(
            type(exc).__name__, exc)
    contacts = _contacts(conn, set(vendors))
    out = []
    for pid, row in vendors.items():
        to, cc = found[pid].pick() if pid in found else ("", [])
        row["to"] = to or contacts.get(pid, "")
        row["cc"] = cc if to else []
        out.append(row)
    out.sort(key=lambda r: (r["key"].casefold(), r["key"]))
    return out, note
