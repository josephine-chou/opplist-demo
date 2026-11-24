"""The original mail on the team site (W3, 2026-11-11): cases/{OPP}.reply,
what the site's quote card answers, as ..\\OppListWeb\\INTERFACE.md says.

Every open case (New or Quoted, not another office's) gets
{on, subject, mid, name, first, to, cc}, found the way the
acknowledgment's Reply All finds the registered mail
(case_mail.resolve_registered): on its date, its subject as
ack.numbered_subject writes it, its Message-ID, the sender's name and
first word (followup.first_name_of), the sender's address, and the other
addresses on its To and CC without hers. A colleague's forward (A2b) is
written the acknowledgment's way (ack.forward_target): no mid, to the
original sender, cc the colleague who forwarded it. A mail not found: what
the case knows (its subject, the requester's primary contact as to), mid
empty.

The archive is searched once per case and the answer kept in
firebase-sync.json under `replies`, against a fingerprint of what the
search reads from the case; a push where nothing changed searches nothing.
A mail not found is looked for again once a day (the archive may not have
it yet). No archive at all: what the case knows, nothing kept.

W3b (2026-11-11): the cache keeps the mail's facts, and the reply is made
from them on every push (reply_of) with three more rules: our own mail is
never answered (a new mail to the requester on it, via 'colleague'); a
vendor of the case on the mail takes the mid and every vendor address
away; one of our offices signing with its prefix gives the word after it
as first. RULES is in the fingerprint, so a new version looks every open
case up once more.
"""

from __future__ import annotations

import hashlib
import json

STATE_KEY = "replies"
#: W3b: the rules' version, in every fingerprint, so a new version looks
#: every open case up once more
RULES = "W3b"
OPEN = ("New", "Quoted")
CC_MAX = 20


def is_open(row) -> bool:
    """New or Quoted, not another office's."""
    return (row["status"] or "") in OPEN and not (row["handled_by"] or "")


def fingerprint(conn, row) -> str:
    """What resolve_registered reads from the case, hashed: a change of any
    of it searches again."""
    from . import case_mail
    parts = [RULES, row["entry_id"] or "", case_mail.registered_message_id(conn, row["id"]),
             row["conversation_id"] or "", row["source_subject"] or "", (row["received_on"] or "")[:10]]
    return hashlib.sha256(json.dumps(parts).encode("utf-8")).hexdigest()[:16]


def _name(value) -> str:
    """A person's name, '' for none or an address."""
    value = " ".join(str(value or "").split()).strip("'\"")
    return "" if "@" in value else value


def _subject(subject, opp_no) -> str:
    from . import ack
    from .firebase_sync import SUBJ_FULL_MAX, cut
    return cut(ack.numbered_subject(subject or "", opp_no), SUBJ_FULL_MAX)


def own_addresses(search) -> set:
    """Her own addresses (the mailbox's), lower case; set() when the search
    cannot say."""
    found = getattr(search, "own_addresses", None)
    try:
        return {a.strip().lower() for a in (found() if found else ()) if a and a.strip()}
    except Exception:
        return set()


def _addresses(values) -> list:
    return [str(a or "").strip() for a in (values or [])]


def facts_of(conn, mail, own=()) -> dict:
    """W3b: what a reply is made from, read once from the registered mail
    and kept in the cache; the reply is made from it again on every push,
    so a vendor or a contact added later counts. Her own addresses (`own`)
    on the mail are noted, nothing is dropped here."""
    from . import ack, case_numbers
    to, cc = _addresses(getattr(mail, "to_addresses", None)), _addresses(getattr(mail, "cc_addresses", None))
    sender = (getattr(mail, "sender_address", "") or "").strip()
    mine = {a.lower() for a in own}
    out = {"on": case_numbers.mail_day(mail) or "", "subject": getattr(mail, "subject", "") or "",
           "mid": (getattr(mail, "message_id", "") or "").strip(),
           "sender_name": getattr(mail, "sender_name", "") or "", "sender_address": sender,
           "sender_internal": bool(getattr(mail, "sender_internal", False)),
           "to": to, "to_names": [str(n or "") for n in getattr(mail, "to_names", None) or []],
           "cc": cc, "cc_names": [str(n or "") for n in getattr(mail, "cc_names", None) or []],
           "own": sorted({a.lower() for a in [sender] + to + cc if a and a.lower() in mine}),
           "forward": None}
    if ack.is_colleague_forward(mail):
        target = ack.forward_target(conn, mail)
        out["forward"] = {"name": target["name"] or "", "address": (target["address"] or "").strip(),
                          "cc": (target["cc"] or "").strip(), "subject": target["subject"] or ""}
    return out


def _first(name, prefix="") -> str:
    """followup.first_name_of, and W3b item 3: 'JJZ-JACKY LI' of the
    office JJZ is Jacky."""
    from . import followup
    if prefix and name.strip().upper().startswith(prefix):
        return followup.first_name_of(name.strip()[len(prefix):])
    return followup.first_name_of(name)


def _named(facts) -> list:
    """(address, display name) of every To and CC address, in order. The
    archive keeps one line of names for To and CC together (display_to);
    Outlook gives each line its own. A name is used only when the names
    and the addresses count the same, else every name is ''."""
    to, cc = facts.get("to") or [], facts.get("cc") or []
    to_names, cc_names = facts.get("to_names") or [], facts.get("cc_names") or []
    if len(to_names) == len(to) and len(cc_names) == len(cc):
        names = to_names + cc_names
    elif not cc_names and len(to_names) == len(to) + len(cc):
        names = to_names
    else:
        names = [""] * (len(to) + len(cc))
    return list(zip(to + cc, names))


def _contact_name(conn, party_id, address) -> str:
    if not party_id:
        return ""
    row = conn.execute("SELECT name FROM contact WHERE party_id = ? AND lower(trim(email)) = ? "
                       "AND trim(COALESCE(name, '')) <> '' ORDER BY is_primary DESC, id LIMIT 1",
                       (party_id, address.lower())).fetchone()
    return _name(row["name"]) if row is not None else ""


def reply_of(conn, row, facts) -> dict:
    """The case's reply from the kept facts of its registered mail (W3b):
    a colleague's forward as the acknowledgment answers it (via
    'forward'); our own mail (case_numbers.is_ours) a new mail to the
    requester on it, cc the other requester addresses and our colleague
    (via 'colleague'); else a reply to the sender (via ''). A vendor of
    the case anywhere on the mail: no mid and no vendor address."""
    from types import SimpleNamespace
    from . import case_numbers
    parties = case_numbers.case_parties(conn, row)
    requester, vendors = parties[0], parties[1:]
    own = set(facts.get("own") or [])
    sender = facts.get("sender_address") or ""
    to_line, cc_line = facts.get("to") or [], facts.get("cc") or []

    def vendor(address) -> bool:
        return any(v.is_address(address) for v in vendors)

    known = from_case(conn, row)
    forward = facts.get("forward")
    mail = SimpleNamespace(sender_address=sender, sender_name=facts.get("sender_name") or "",
                           sender_internal=facts.get("sender_internal", False))
    if forward:
        via, mid, subject = "forward", "", forward["subject"]
        name, to = _name(forward["name"]), forward["address"]
        cc = [forward["cc"]] if forward["cc"] else []
    elif case_numbers.is_ours(mail, case_numbers.internal_domains(conn), parties):
        via, mid, subject = "colleague", "", facts.get("subject")
        theirs = [(a, n) for a, n in _named(facts) if "@" in a and a.lower() not in own and not vendor(a)
                  and (requester.is_address(a) or requester.named([n]))]
        if theirs:
            to = theirs[0][0]
            name = _name(theirs[0][1]) or _contact_name(conn, row["requester_party_id"], to)
        else:
            to, name = known["to"], known["name"]
        cc = [a for a, _n in theirs[1:]] + [sender]
    else:
        via, mid, subject = "", facts.get("mid") or "", facts.get("subject")
        name, to = _name(facts.get("sender_name")), sender
        cc = to_line + cc_line
    if any(vendor(a) for a in [sender] + to_line + cc_line):
        mid = ""
        if vendor(to):
            to, name = known["to"], known["name"]
    taken = own | {to.lower()}
    out = []
    for address in cc:
        address = str(address or "").strip()
        if "@" in address and address.lower() not in taken and not vendor(address):
            taken.add(address.lower())
            out.append(address)
    return {"on": facts.get("on") or (row["received_on"] or "")[:10],
            "subject": _subject(subject, row["opp_no"]), "mid": mid, "name": name,
            "first": _first(name, requester.prefix), "to": to, "cc": out[:CC_MAX], "via": via}


def from_mail(conn, row, mail, own=()) -> tuple:
    """(reply, forward) of the registered mail as the archive has it."""
    facts = facts_of(conn, mail, own)
    return reply_of(conn, row, facts), bool(facts["forward"])


def from_case(conn, row) -> dict:
    """The mail not found: the case's subject, the requester's primary
    contact (else its first with an address) as to."""
    from . import case_numbers
    contact = conn.execute(
        "SELECT name, email FROM contact WHERE party_id = ? AND trim(COALESCE(email, '')) <> '' "
        "ORDER BY is_primary DESC, id LIMIT 1", (row["requester_party_id"],)).fetchone() \
        if row["requester_party_id"] else None
    name = _name(contact["name"]) if contact is not None else ""
    return {"on": (row["received_on"] or "")[:10], "subject": _subject(row["source_subject"], row["opp_no"]),
            "mid": "", "name": name,
            "first": _first(name, case_numbers.party_of(conn, row["requester_party_id"]).prefix
                            if row["requester_party_id"] else ""),
            "to": (contact["email"] or "").strip() if contact is not None else "", "cc": [], "via": ""}


class Replies:
    """The replies of one push: `cache` is firebase-sync.json's `replies`
    ({opp: {fp, found, forward, tried, reply}}), `search` the push's
    archive search (None without one). `searched` counts the cases looked
    for in the archive this push."""

    def __init__(self, conn, search, cache: dict, today: str):
        self.conn, self.search, self.cache, self.today = conn, search, cache, today
        self.searched = 0
        self._own = None
        self.seen = set()

    def own(self) -> set:
        if self._own is None:
            self._own = own_addresses(self.search)
        return self._own

    def of(self, row) -> dict | None:
        """The case's reply, None for a case that is not open."""
        if not is_open(row):
            return None
        from . import case_mail
        opp = row["opp_no"]
        self.seen.add(opp)
        fp = fingerprint(self.conn, row)
        kept = self.cache.get(opp) or {}
        fresh = kept.get("fp") == fp and (kept.get("found") or kept.get("tried") == self.today)
        if not fresh and self.search is not None:
            self.searched += 1
            try:
                mail, _step = case_mail.resolve_registered(self.conn, row, self.search)
            except Exception:
                mail = None
            if mail is not None:
                facts = facts_of(self.conn, mail, self.own())
                kept = {"fp": fp, "found": True, "forward": bool(facts["forward"]), "mail": facts}
            else:
                kept = {"fp": fp, "found": False, "tried": self.today}
            self.cache[opp] = kept
            fresh = True
        if fresh and kept.get("found") and kept.get("mail"):
            return reply_of(self.conn, row, kept["mail"])
        return from_case(self.conn, row)

    def prune(self) -> None:
        """Cases no longer open drop out of the cache (found again when they
        come back)."""
        for opp in [o for o in self.cache if o not in self.seen]:
            del self.cache[opp]
