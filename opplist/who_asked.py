"""Who asked? from mail, for a case parked on the (TBC) requester (batch
MF1). Read only: a candidate is a suggestion she takes
with a click (Use as requester, Duplicate of OPP-nnnn); nothing here
writes.

Two readings, nothing wider:

a. the case's own mails (case_mail.case_mails: its conversations, its
   numbered mails, its attached threads) - every outside address on them
   (From, To, Cc) that is not ours and not one of the case's vendors, and
   for a forward the original sender as intake reads it
   (intake.forward_original). Its party is found by domain, else by the
   contact's address; an address with no party is shown as it is, with
   Add party.
b. the same shipment - another case with a known requester, received
   within WINDOW_DAYS days before or after, with the same origin and
   destination countries (and the same destination place when both have
   one) and at least one vendor asked on both.

No search of the whole archive by place words: OPP-1693's lane words also
hit an unrelated RO-RO thread.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from . import branding, db, matching, parsing

#: b: how far apart the two cases' received dates may be
WINDOW_DAYS = 10
#: a: the mails read per case at most (the Mail card's own cap is larger)
MAX_MAILS = 300


@dataclass
class Candidate:
    """One answer to Who asked?, with what it rests on."""

    kind: str                       # 'mail' | 'shipment'
    party_id: int | None = None
    party_key: str = ""
    address: str = ""               # a: the address read; b: ''
    domain: str = ""
    evidence: str = ""              # the one line she reads
    other_id: int | None = None     # b: the other case
    other_opp: str = ""
    mails: int = 0                  # a: how many of the case's mails carry it
    suggested_key: str = ""         # a, no party: the key Add party starts with
    suggested_country: str = ""
    extra: dict = field(default_factory=dict)


def _day(mail) -> str:
    return (getattr(mail, "received_at", "") or getattr(mail, "received_on", "") or "")[:10]


def _evidence(mail) -> str:
    """'2026-11-04, ana@x.test: RE: RATE REQUEST ...'"""
    return "{}, {}: {}".format(_day(mail) or "?", (mail.sender_address or mail.sender_name or "?").strip(),
                               " ".join((mail.subject or "").split()))


def vendor_marks(conn, enquiry_id) -> tuple:
    """(party ids, domains, addresses) of the case's vendors (live vendor
    requests): their party domains and their contacts' addresses."""
    ids = {r[0] for r in conn.execute("SELECT DISTINCT vendor_party_id FROM vendor_request "
                                      "WHERE enquiry_id = ? AND voided = 0", (enquiry_id,))}
    if not ids:
        return set(), set(), set()
    marks = ",".join("?" * len(ids))
    domains = {(r[0] or "").strip().lower() for r in conn.execute(
        "SELECT domain FROM party_domain WHERE party_id IN ({})".format(marks), tuple(ids))}
    addresses = {(r[0] or "").strip().lower() for r in conn.execute(
        "SELECT email FROM contact WHERE email IS NOT NULL AND party_id IN ({})".format(marks), tuple(ids))}
    return ids, domains - {""}, addresses - {""}


def party_of_address(conn, address) -> tuple:
    """(party id, key) of the one live, real party this address belongs to:
    by its domain, else by a contact with this address; (None, '')."""
    domain = matching.domain_of(address)
    found = {}
    for match in matching.by_domain(conn, domain):
        found[match.party_id] = match.key
    if not found:
        for row in conn.execute("SELECT DISTINCT p.id, p.key FROM contact c JOIN party p ON p.id = c.party_id "
                                "WHERE lower(trim(c.email)) = ?", (address.strip().lower(),)):
            found[row["id"]] = row["key"]
    live = []
    for party_id, key in found.items():
        row = conn.execute("SELECT id, key, merged_into, placeholder FROM party WHERE id = ?", (party_id,)).fetchone()
        while row is not None and row["merged_into"] is not None:
            row = conn.execute("SELECT id, key, merged_into, placeholder FROM party WHERE id = ?",
                               (row["merged_into"],)).fetchone()
        if row is not None and not row["placeholder"] and row["id"] not in [p for p, _ in live]:
            live.append((row["id"], row["key"]))
    return live[0] if len(live) == 1 else (None, "")


def _addresses(conn, mail, internal) -> list:
    """[(address, how)] the outside people on one mail: its From, To and Cc,
    and for a forward the original sender (intake.forward_original)."""
    from . import intake
    out = []
    for address in [mail.sender_address] + list(getattr(mail, "to_addresses", []) or []) \
            + list(getattr(mail, "cc_addresses", []) or []):
        out.append(((address or "").strip().lower(), "on"))
    body = getattr(mail, "body_excerpt", "") or ""
    if parsing.is_forward(mail.subject, body):
        quoted = parsing.quoted_mails(body)
        original, address = intake.forward_original(quoted, internal | intake._internal_domains(conn, mail))
        if original is not None and address:
            out.append((address.strip().lower(), "forwarded from"))
    return [(a, how) for a, how in out if "@" in a]


def office_party(conn, code) -> tuple:
    """(party id, key) of the office party 'JoJo <CODE>', or
    (None, 'JoJo <CODE>') when it is not on file (13g's party)."""
    key = branding.office_key(code, conn)
    row = conn.execute("SELECT id, key FROM party WHERE type = 'Office' AND lower(trim(key)) = ? "
                       "AND merged_into IS NULL", (key.lower(),)).fetchone()
    return (row["id"], row["key"]) if row is not None else (None, key)


def _office_of(name) -> str:
    """RG2b (3): the office code a display name starts with ('JJZ-ANA LI' ->
    'SZX'), as 13g reads it at Register; '' for ours (JJC) and for none."""
    from . import intake
    found = parsing._OFFICE_PREFIX.match(name or "")
    return found.group(1) if found and found.group(1) != intake.HOME_OFFICE else ""


def _offices(conn, mail, internal) -> list:
    """RG2b (3): [(code, how)] the Our offices that wrote: the sender's
    display name, and for a forward the original sender as intake reads it."""
    from . import intake
    out = []
    code = _office_of(getattr(mail, "sender_name", ""))
    if code:
        out.append((code, "on"))
    body = getattr(mail, "body_excerpt", "") or ""
    if parsing.is_forward(mail.subject, body):
        original, _address = intake.forward_original(parsing.quoted_mails(body),
                                                     internal | intake._internal_domains(conn, mail))
        if original is not None and original.office and original.office != intake.HOME_OFFICE:
            out.append((original.office, "forwarded from"))
    return out


def _office_candidate(conn, code, evidence) -> Candidate:
    party_id, key = office_party(conn, code)
    cand = Candidate("mail", party_id, key if party_id is not None else "", "", "", evidence)
    cand.extra = {"office": code}
    if party_id is None:
        cand.suggested_key = key
    return cand


def from_mails(conn, row, mails) -> list:
    """a: the candidates the case's own mails give, the most mails first."""
    internal = db.internal_domains(conn)
    vendor_ids, vendor_domains, vendor_addresses = vendor_marks(conn, row["id"])
    by = {}
    for mail in sorted(mails, key=_day)[:MAX_MAILS]:
        # RG2b (3): one of our offices that wrote is that office's party
        for code, how in _offices(conn, mail, internal):
            party_id, _key = office_party(conn, code)
            if party_id is not None and party_id in vendor_ids:
                continue                      # an office we asked
            slot = ("p", party_id) if party_id is not None else ("o", code)
            cand = by.get(slot)
            if cand is None:
                evidence = _evidence(mail)
                if how == "forwarded from":
                    evidence = "{} (forwarded from {})".format(evidence, code)
                cand = by[slot] = _office_candidate(conn, code, evidence)
            cand.mails += 1
        for address, how in _addresses(conn, mail, internal):
            domain = matching.domain_of(address)
            if not domain or db.is_internal_domain(conn, domain):
                continue
            if domain in vendor_domains or address in vendor_addresses:
                continue
            party_id, key = party_of_address(conn, address)
            if party_id is not None and party_id in vendor_ids:
                continue
            slot = ("p", party_id) if party_id is not None else ("a", address)
            cand = by.get(slot)
            if cand is None:
                evidence = _evidence(mail)
                if how == "forwarded from":
                    evidence = "{} (forwarded from {})".format(evidence, address)
                elif address != (mail.sender_address or "").strip().lower():
                    evidence = "{} (copied: {})".format(evidence, address)
                cand = by[slot] = Candidate("mail", party_id, key, address, domain, evidence)
                if party_id is None:
                    from . import intake
                    cand.suggested_key = intake._domain_label(domain) if not matching.is_free_mail(domain) else ""
                    cand.suggested_country = matching.country_for_tld(conn, domain)
            cand.mails += 1
    return sorted(by.values(), key=lambda c: (c.party_id is None, -c.mails, c.party_key or c.address))


def _address_for(mail, quoted, domain) -> str:
    """The first address with this domain on the mail or in its quoted chain."""
    for address in [mail.sender_address] + list(getattr(mail, "to_addresses", []) or []) \
            + list(getattr(mail, "cc_addresses", []) or []) + [q.address for q in quoted]:
        if address and matching.domain_of(address) == domain:
            return address.strip().lower()
    return ""


def from_outlook(conn, row, mail) -> list:
    """RG2b (2): the mail she selected in Outlook read by the register
    reader (intake.build, as Register from Outlook reads it): its requester
    as a candidate - a party on file (Use), an address with no party (Add
    party) or one of our offices. Read only; [] when the reader finds nobody
    (or only the (TBC) placeholder)."""
    from . import direct_customers_out as dco, intake
    draft = intake.build(conn, mail)
    evidence = "From Outlook: {}".format(_evidence(mail))
    vendor_ids, _domains, _addresses = vendor_marks(conn, row["id"])
    party_id = draft.values.get("requester_party_id")
    if party_id and not dco.is_tbc(conn, party_id):
        party = conn.execute("SELECT id, key, merged_into, placeholder FROM party WHERE id = ?",
                             (party_id,)).fetchone()
        if party is not None and party["merged_into"] is None and not party["placeholder"] \
                and party["id"] not in vendor_ids:
            cand = Candidate("outlook", party["id"], party["key"], "", "", evidence, mails=1)
            how = draft.sources.get("requester_party_id") or ""
            cand.extra = {"how": how}
            return [cand]
    new = draft.new_party or {}
    if new.get("type") == "Office" and branding.office_short(new.get("key", ""), conn):
        cand = _office_candidate(conn, branding.office_short(new["key"], conn), evidence)
        cand.kind, cand.mails = "outlook", 1
        return [cand]
    domain = (new.get("domain") or "").strip().lower()
    if domain and not db.is_internal_domain(conn, domain):
        address = _address_for(mail, draft.quoted, domain)
        cand = Candidate("outlook", None, "", address or "@" + domain, domain, evidence, mails=1)
        cand.suggested_key = new.get("key") or (intake._domain_label(domain) if not matching.is_free_mail(domain)
                                                else "")
        cand.suggested_country = new.get("country") or matching.country_for_tld(conn, domain)
        return [cand]
    return []


def candidate_state(cand) -> dict:
    """A candidate as the session keeps it between From Outlook and Use."""
    return {"kind": cand.kind, "party_id": cand.party_id, "party_key": cand.party_key, "address": cand.address,
            "domain": cand.domain, "evidence": cand.evidence[:300], "suggested_key": cand.suggested_key,
            "suggested_country": cand.suggested_country, "office": cand.extra.get("office", "")}


def candidate_from_state(conn, state) -> Candidate:
    """The kept candidate, its party looked up again (Add party may have
    made it since)."""
    cand = Candidate(state.get("kind") or "outlook", state.get("party_id"), state.get("party_key") or "",
                     state.get("address") or "", state.get("domain") or "", state.get("evidence") or "", mails=1)
    cand.suggested_key, cand.suggested_country = state.get("suggested_key") or "", state.get("suggested_country") or ""
    if state.get("office"):
        cand.extra = {"office": state["office"]}
        if cand.party_id is None:
            cand.party_id, key = office_party(conn, state["office"])
            cand.party_key = key if cand.party_id is not None else ""
    elif cand.party_id is None and cand.address:
        address = cand.address if not cand.address.startswith("@") else "x" + cand.address
        cand.party_id, cand.party_key = party_of_address(conn, address)
    return cand


def _date(value):
    try:
        return dt.date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _same_text(a, b) -> bool:
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def same_shipment(conn, row) -> list:
    """b: the cases with a known requester that look like the same shipment."""
    received = _date(row["received_on"])
    if received is None or not (row["origin_country"] or "").strip() or not (row["dest_country"] or "").strip():
        return []
    vendors = {r[0] for r in conn.execute("SELECT DISTINCT vendor_party_id FROM vendor_request "
                                          "WHERE enquiry_id = ? AND voided = 0", (row["id"],))}
    if not vendors:
        return []
    start = (received - dt.timedelta(days=WINDOW_DAYS)).isoformat()
    end = (received + dt.timedelta(days=WINDOW_DAYS)).isoformat()
    out = []
    for other in conn.execute(
            "SELECT e.id, e.opp_no, e.received_on, e.origin_country, e.dest_country, e.dest_loc, "
            "e.requester_party_id, p.key AS requester_key FROM v_enquiry e "
            "JOIN party p ON p.id = e.requester_party_id "
            "WHERE e.id <> ? AND COALESCE(p.placeholder, 0) = 0 AND e.status <> 'Dropped' "
            "AND e.received_on BETWEEN ? AND ? ORDER BY e.received_on, e.id", (row["id"], start, end)):
        if not (_same_text(other["origin_country"], row["origin_country"])
                and _same_text(other["dest_country"], row["dest_country"])):
            continue
        if (row["dest_loc"] or "").strip() and (other["dest_loc"] or "").strip() \
                and not _same_text(row["dest_loc"], other["dest_loc"]):
            continue
        shared = sorted(r[0] for r in conn.execute(
            "SELECT DISTINCT p.key FROM vendor_request v JOIN party p ON p.id = v.vendor_party_id "
            "WHERE v.enquiry_id = ? AND v.voided = 0 AND v.vendor_party_id IN ({})".format(
                ",".join("?" * len(vendors))), (other["id"],) + tuple(vendors)))
        if not shared:
            continue
        cand = Candidate("shipment", other["requester_party_id"], other["requester_key"],
                         other_id=other["id"], other_opp=other["opp_no"])
        cand.evidence = "same shipment as {}, requester {}".format(other["opp_no"], other["requester_key"])
        cand.extra = {"received_on": other["received_on"], "vendors": shared}
        out.append(cand)
    return out


def candidates(conn, row, search=None, mails=None) -> list:
    """a then b for a (TBC) case; [] for any other case. With neither
    `search` nor `mails`, the archive is opened here; a missing archive
    leaves a out. Never raises past a logged line: it is a convenience."""
    from . import case_mail, direct_customers_out as dco, logs
    if row is None or not dco.is_tbc(conn, row["requester_party_id"]):
        return []
    out = []
    try:
        if mails is None:
            opened = None
            if search is None:
                opened, _info = case_mail.open_search()
                search = opened
            try:
                mails = case_mail.case_mails(conn, search, row) if search is not None else []
            finally:
                if opened is not None:
                    opened.__exit__(None, None, None)
        out += from_mails(conn, row, mails)
        out += same_shipment(conn, row)
    except Exception:                                       # a box, never a crash
        logs.get("web").exception("who asked")
    return out
