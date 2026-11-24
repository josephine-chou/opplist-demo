"""Case numbers in mail subjects (AL1, 2026-10-01).

From October Iris registers an inquiry, then acknowledges it to the
agent with the case number in the subject ('[OPP-1504] ' in front, or right
after 'RE: '); the agent's replies carry it. This module says which case a
mail belongs to - the one rule the Mail card, propose-status,
detect-followups, followup.from_archive, the shipment detection and the
register page all read - and writes what the numbers tell:

    (a) the mail a case was registered from belongs to that case; a mail
        attached to a case by hand from a suggestion (enquiry_thread kind
        'message') belongs to that one;
    (b) a subject with exactly one valid number belongs to that case only,
        and never joins another case through its thread key, its
        conversation or the subject fallback - when it fits the case (the
        sender, or for our own mail an outside recipient, is the requester
        or one of the case's vendors); a number that does not fit is
        matched to no case and Suggested updates asks 'Check the case
        number' (a pasted old number, the clipboard still holding the
        previous case);
    (c) two or more valid numbers: no case; Suggested updates asks 'Two
        case numbers in this subject' with a button per case;
    (d) no valid number: today's rules (conversation, thread key, the
        subject fallback) decide.

A valid number is a case that exists and was not voided (Registered in
error, or Duplicate of another case). The parser (parsing.case_numbers)
only reads; validity is checked here.

The acknowledgement: the first mail we send to the requester that carries
the case's number, before any Quoted event and without a price, is recorded
once as an 'ack' event - our action, no status change, never a quote, a
re-quote or a follow-up. The scan (detect-followups) writes it, as it
writes follow-ups.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field

from . import branding, db, matching, parsing

VOID_REASONS = ("Registered in error",)
TWO_NUMBERS = "Two case numbers in this subject"
CHECK_NUMBER = "Check the case number"
ACK_NOTE = "Acknowledged"


# ------------------------------------------------------------ the numbers --
def valid_cases(conn, subject) -> list:
    """The cases a subject's numbers name that exist and are not void, in
    the subject's order: rows (id, opp_no, requester_party_id, status)."""
    out = []
    for opp_no in parsing.case_numbers(subject):
        row = conn.execute(
            "SELECT e.id, e.opp_no, e.requester_party_id, e.status, p.key AS requester_key, "
            "p.type AS requester_type FROM enquiry e JOIN party p ON p.id = e.requester_party_id "
            "WHERE e.opp_no = ? AND COALESCE(e.closed_reason, '') NOT IN ({}) "
            "AND COALESCE(e.closed_reason, '') NOT LIKE 'Duplicate%'".format(
                ",".join("?" for _ in VOID_REASONS)), (opp_no,) + VOID_REASONS).fetchone()
        if row is not None:
            out.append(row)
    return out


def mail_ref(mail) -> str:
    """How one mail is named in enquiry_thread / suggestion: its
    Message-ID, else its entry id."""
    return (getattr(mail, "message_id", "") or "").strip() or (getattr(mail, "entry_id", "") or "").strip()


def internal_domains(conn) -> set:
    return {d.lower() for d in (db.get_setting_list(conn, "internal_domains") or [branding.INTERNAL_DOMAIN])}


# --------------------------------------------------------------- the fit --
@dataclass
class Party:
    """Who a party is in a mail: its domains, its contacts' addresses, and
    for one of our offices the prefix its people sign with ('JJZ-')."""
    domains: set = field(default_factory=set)
    addresses: set = field(default_factory=set)
    prefix: str = ""

    def sent(self, mail) -> bool:
        address = (getattr(mail, "sender_address", "") or "").strip().lower()
        if self.prefix and (getattr(mail, "sender_name", "") or "").strip().upper().startswith(self.prefix):
            return True
        return bool(address) and (address in self.addresses or matching.domain_of(address) in self.domains)

    def is_address(self, address) -> bool:
        address = (address or "").strip().lower()
        return bool(address) and (address in self.addresses or matching.domain_of(address) in self.domains)

    def named(self, names) -> bool:
        return bool(self.prefix) and any((n or "").strip().upper().startswith(self.prefix) for n in names or ())


def party_of(conn, party_id) -> Party:
    from . import propose
    row = conn.execute("SELECT key, type FROM party WHERE id = ?", (party_id,)).fetchone()
    if row is None:
        return Party()
    addresses = {r[0].strip().lower() for r in conn.execute(
        "SELECT email FROM contact WHERE party_id = ? AND email LIKE '%@%'", (party_id,))}
    prefix = propose.office_prefix(row["key"]) if row["type"] == "Office" else ""
    return Party(propose.requester_domains_of(conn, party_id), addresses, prefix)


def case_parties(conn, case) -> list:
    """The requester first, then the vendor of every live vendor request."""
    out = [party_of(conn, case["requester_party_id"])]
    for row in conn.execute("SELECT DISTINCT vendor_party_id FROM vendor_request "
                            "WHERE enquiry_id = ? AND voided = 0", (case["id"],)):
        out.append(party_of(conn, row[0]))
    return out


def _recipients(mail) -> list:
    return list(getattr(mail, "to_addresses", None) or []) + list(getattr(mail, "cc_addresses", None) or [])


def _recipient_names(mail) -> list:
    return list(getattr(mail, "to_names", None) or []) + list(getattr(mail, "cc_names", None) or [])


def is_ours(mail, internal, parties=()) -> bool:
    """Sent from our own domain - unless it is one of our offices that is a
    party of the case, signing with its prefix."""
    if any(p.prefix and p.sent(mail) for p in parties):
        return False
    domain = matching.domain_of(getattr(mail, "sender_address", "") or "")
    return bool(getattr(mail, "sender_internal", False)) or bool(domain and domain in internal)


def fits(conn, case, mail, internal=None) -> bool:
    """Item 5: does the number fit its mail? From outside, the sender is
    the requester or a vendor of the case; our own mail fits when it has no
    outside recipient, or one of them is the requester or a vendor (or,
    for one of our offices, its prefix is on the To or Cc line)."""
    internal = internal if internal is not None else internal_domains(conn)
    parties = case_parties(conn, case)
    if not is_ours(mail, internal, parties):
        return any(p.sent(mail) for p in parties)
    outside = [a for a in _recipients(mail) if matching.domain_of(a) and matching.domain_of(a) not in internal]
    names = _recipient_names(mail)
    if any(p.named(names) for p in parties):
        return True
    if not outside:
        return True
    return any(p.is_address(a) for p in parties for a in outside)


def to_requester(conn, case, mail) -> bool:
    requester = party_of(conn, case["requester_party_id"])
    return any(requester.is_address(a) for a in _recipients(mail)) or requester.named(_recipient_names(mail))


# ---------------------------------------------------------- which case --
@dataclass
class Link:
    """Which case a mail belongs to. kind: registered / attached / number
    (case_id) - numbers / mismatch (case_ids; matched to none) - none
    (today's rules decide)."""
    kind: str = "none"
    case_id: int | None = None
    case_ids: list = field(default_factory=list)
    opp_nos: list = field(default_factory=list)
    numbered: bool = False


def _entry_ids(mail, search=None) -> set:
    if search is not None and hasattr(search, "entry_ids_of"):
        return search.entry_ids_of(mail)
    return {e for e in (getattr(mail, "entry_id", "") or "",) if e}


def link_of(conn, mail, search=None, internal=None, cache=None) -> Link:
    """The one rule (see the module note). `cache` is a dict the caller
    keeps for a run; the link of a mail is computed once."""
    ref = mail_ref(mail)
    if cache is not None and ref and ref in cache:
        return cache[ref]
    link = _link_of(conn, mail, search, internal)
    if cache is not None and ref:
        cache[ref] = link
    return link


def _link_of(conn, mail, search, internal) -> Link:
    named = valid_cases(conn, getattr(mail, "subject", "") or "")
    numbered = bool(named)
    entries = _entry_ids(mail, search)
    if entries:
        row = conn.execute("SELECT id, opp_no FROM enquiry WHERE entry_id IN ({}) ORDER BY id LIMIT 1".format(
            ",".join("?" for _ in entries)), tuple(entries)).fetchone()
        if row is not None:
            return Link("registered", row["id"], [row["id"]], [row["opp_no"]], numbered)
    refs = entries | {r for r in (mail_ref(mail),) if r}
    if refs and db._has_table(conn, "enquiry_thread"):
        row = conn.execute(
            "SELECT t.enquiry_id, e.opp_no FROM enquiry_thread t JOIN enquiry e ON e.id = t.enquiry_id "
            "WHERE t.kind = 'message' AND t.voided = 0 AND t.value IN ({}) ORDER BY t.id LIMIT 1".format(
                ",".join("?" for _ in refs)), tuple(refs)).fetchone()
        if row is not None:
            return Link("attached", row["enquiry_id"], [row["enquiry_id"]], [row["opp_no"]], True)
    if len(named) >= 2:
        return Link("numbers", None, [r["id"] for r in named], [r["opp_no"] for r in named], True)
    if len(named) == 1:
        case = named[0]
        if fits(conn, case, mail, internal):
            return Link("number", case["id"], [case["id"]], [case["opp_no"]], True)
        return Link("mismatch", None, [case["id"]], [case["opp_no"]], True)
    return Link()


def belongs(conn, case_id, mail, search=None, internal=None, cache=None) -> bool:
    """May a reader that found this mail for `case_id` (by its conversation,
    its thread key, its subject, or its number) keep it? N1 (e): a thread
    several cases share is split by date for the mails without a number."""
    link = link_of(conn, mail, search, internal, cache)
    if link.kind in ("number", "attached"):
        return link.case_id == case_id
    if link.kind == "registered":
        # the registered mail is its case's; carrying a number it is that case's only
        if link.case_id == case_id:
            return True
        if link.numbered:
            return False
    if link.kind in ("numbers", "mismatch"):
        return False
    owners = thread_owners(conn, mail, cache)
    if case_id not in {o["id"] for o in owners} or len(owners) < 2:
        return True
    return case_id in split_owners(owners, mail_day(mail))


# ---------------------------------------------------- a shared thread (N1) --
def mail_day(mail) -> str:
    """A mail's date as YYYY-MM-DD: received_on, else sent_at, else received_at."""
    for name in ("received_on", "sent_at", "received_at"):
        value = (getattr(mail, name, "") or "").strip()
        if value:
            return value[:10]
    return ""


def thread_owners(conn, mail, cache=None) -> list:
    """The cases whose own ids or attached threads include this mail's
    Outlook conversation or its subject's thread key: rows (id, opp_no,
    status, received_on) in case order."""
    conversation = (getattr(mail, "conversation_id", "") or "").strip()
    key = parsing.subject_thread_key(getattr(mail, "subject", "") or "") or ""
    ref = ("owners", conversation, key)
    if cache is not None and ref in cache:
        return cache[ref]
    rows = db.thread_cases(conn, conversation=conversation or None, key=key or None)
    out = owners_of(conn, [r["id"] for r in rows])
    if cache is not None:
        cache[ref] = out
    return out


def owners_of(conn, ids) -> list:
    ids = list(dict.fromkeys(ids))
    if not ids:
        return []
    return conn.execute("SELECT id, opp_no, status, received_on FROM enquiry WHERE id IN ({}) ORDER BY id".format(
        ",".join("?" for _ in ids)), tuple(ids)).fetchall()


def _days(owners) -> tuple:
    """(the registration days in order, {case id: its day}); a case with no
    received_on counts with the earliest day."""
    real = sorted({(o["received_on"] or "")[:10] for o in owners if (o["received_on"] or "")[:10]})
    earliest = real[0] if real else ""
    return real, {o["id"]: ((o["received_on"] or "")[:10] or earliest) for o in owners}


def split_owners(owners, day: str) -> set:
    """5.0 item 8: the cases a mail without a number belongs to - those
    registered on the latest day on or before the mail's date; a mail dated
    before all of them is the earliest day's; same-day cases share."""
    real, day_of = _days(owners)
    if not real or not day:
        return {o["id"] for o in owners}
    before = [d for d in real if d <= day]
    chosen = before[-1] if before else real[0]
    return {i for i, d in day_of.items() if d == chosen}


def ranges(owners) -> dict:
    """{case id: (first day or '', last day or '')} - the days of a shared
    thread's unnumbered mails each case holds; '' is open."""
    import datetime as dt
    real, day_of = _days(owners)
    out = {}
    for case_id, day in day_of.items():
        later = [d for d in real if d > day]
        end = ""
        if later:
            end = (dt.date.fromisoformat(later[0]) - dt.timedelta(days=1)).isoformat()
        out[case_id] = ("" if day == (real[0] if real else "") else day, end)
    return out


def _span_words(first, last, whose) -> str:
    from . import dates
    if first and last:
        return "{} {} to {}".format(whose, dates.month_day(first), dates.month_day(last))
    if first:
        return "{} from {}".format(whose, dates.month_day(first))
    if last:
        return "{} up to {}".format(whose, dates.month_day(last))
    return whose


def shared_with(case_id, owners) -> list:
    """The other cases of a shared thread, each as {id, opp_no, status,
    same_day, theirs ('its mails up to Oct 4'), mine ("this case's from
    Oct 5"), up_to ('up to Oct 4')}; [] when nobody else has it."""
    span = ranges(owners)
    mine = span.get(case_id, ("", ""))
    out = []
    for o in owners:
        if o["id"] == case_id:
            continue
        theirs = span.get(o["id"], ("", ""))
        out.append({"id": o["id"], "opp_no": o["opp_no"], "status": o["status"], "same_day": theirs == mine,
                    "theirs": _span_words(theirs[0], theirs[1], "its mails"),
                    "mine": _span_words(mine[0], mine[1], "this case's"),
                    "up_to": _span_words(theirs[0], theirs[1], "").strip()})
    return out


def case_owners(conn, case_id, key=None, conversation=None) -> list:
    """The owners of a key or conversation, as thread_owners gives them."""
    rows = db.thread_cases(conn, conversation=conversation or None, key=key or None)
    return owners_of(conn, [r["id"] for r in rows])


def keep(conn, case_id, mails, search=None, internal=None, cache=None) -> list:
    cache = {} if cache is None else cache
    internal = internal if internal is not None else internal_domains(conn)
    return [m for m in mails if belongs(conn, case_id, m, search, internal, cache)]


def numbered_mails(search, opp_no) -> list:
    """The archived mails whose subject carries this case number, in any
    of the spellings the parser reads."""
    try:
        n = int(opp_no.split("-", 1)[1])
    except (IndexError, ValueError):
        return []
    found, seen = [], set()
    for term in {"OPP-{:04d}".format(n), "OPP {:04d}".format(n), "OPP{:04d}".format(n),
                 "OPP-{}".format(n), "OPP {}".format(n)}:
        for mail in search.by_subject(term, limit=100000):
            key = mail_ref(mail) or id(mail)
            if key in seen or opp_no not in parsing.case_numbers(mail.subject):
                continue
            seen.add(key)
            found.append(mail)
    return found


# --------------------------------------------------- the acknowledgement --
def _stamp(mail) -> str:
    return (getattr(mail, "sent_at", "") or getattr(mail, "received_at", "") or getattr(mail, "received_on", "") or "")


def first_quote_on(conn, case_id) -> str:
    row = conn.execute("SELECT MIN(at) FROM status_event WHERE enquiry_id = ? AND voided = 0 "
                       "AND status = 'Quoted' AND kind IN ('status','requote')", (case_id,)).fetchone()
    return row[0] or ""


def ack_mail(conn, case, mails, search=None, internal=None):
    """The acknowledgement among `mails`: the first mail we sent to the
    requester that carries this case's number, before any Quoted event and
    without a price. None when there is none."""
    from . import detect_followups, mail_archive
    internal = internal if internal is not None else internal_domains(conn)
    parties = case_parties(conn, case)
    quoted = first_quote_on(conn, case["id"])
    for mail in sorted(mails, key=_stamp):
        if case["opp_no"] not in parsing.case_numbers(mail.subject):
            continue
        if not is_ours(mail, internal, parties) or not to_requester(conn, case, mail):
            continue
        if quoted and _stamp(mail)[:10] > quoted[:10]:
            return None
        top = mail_archive.top_part(getattr(mail, "body_excerpt", "") or "")
        if detect_followups._gives_an_amount(mail.subject or "", top):
            return None                      # a quote, not an acknowledgement
        return mail
    return None


def has_ack(conn, case_id) -> bool:
    return conn.execute("SELECT 1 FROM status_event WHERE enquiry_id = ? AND kind = 'ack' AND voided = 0",
                        (case_id,)).fetchone() is not None


# ------------------------------------------------------------- the scan --
@dataclass
class ScanReport:
    acknowledged: list = field(default_factory=list)       # 'OPP-1504 2026-10-02'
    suggested: list = field(default_factory=list)          # 'OPP-1504: Check the case number'
    skipped: int = 0
    remembered: int = 0                                     # M2: registered mails' Message-IDs kept

    def lines(self) -> list:
        out = ["case numbers: {} acknowledgement(s) recorded, {} suggestion(s)".format(
            len(self.acknowledged), len(self.suggested))]
        if self.remembered:
            out.append("registered mails' Message-IDs kept: {}".format(self.remembered))
        out += ["  ack " + a for a in self.acknowledged]
        out += ["  " + s for s in self.suggested]
        return out


def _suggested_before(conn, mail) -> bool:
    refs = {r for r in (getattr(mail, "message_id", "") or "", getattr(mail, "entry_id", "") or "") if r}
    for ref in refs:
        if conn.execute("SELECT 1 FROM suggestion WHERE kind = 'case_number' AND (message_id = ? OR entry_id = ?)",
                        (ref, ref)).fetchone() is not None:
            return True
    return False


def _who(mail, internal) -> str:
    if getattr(mail, "sender_internal", False) or matching.domain_of(mail.sender_address or "") in internal:
        outside = [a for a in _recipients(mail) if matching.domain_of(a) not in internal]
        return "to " + ", ".join(outside or _recipients(mail))[:200]
    return "{} <{}>".format(mail.sender_name or "", mail.sender_address or "").strip()


def remember_registered(conn, repo, search, dry_run=False) -> int:
    """M2: a case with a registered mail and no Message-ID row gets one when
    the archive finds that mail by its entry id (or a move it recorded) -
    never from the subject guess."""
    from . import case_mail
    if not db._has_table(conn, "enquiry_thread"):
        return 0
    rows = conn.execute(
        "SELECT id, entry_id FROM enquiry WHERE COALESCE(entry_id, '') <> '' AND id NOT IN "
        "(SELECT enquiry_id FROM enquiry_thread WHERE kind = 'message' AND source = ?) ORDER BY id",
        (case_mail.REGISTER_SOURCE,)).fetchall()
    kept = 0
    today = dt.date.today().isoformat()
    for row in rows:
        mail = search.by_entry_id(row["entry_id"])
        mid = (getattr(mail, "message_id", "") or "").strip() if mail is not None else ""
        if not mid:
            continue
        if dry_run:
            if not conn.execute("SELECT 1 FROM enquiry_thread WHERE kind = 'message' AND value = ?", (mid,)).fetchone():
                kept += 1
        elif case_mail.remember_registered(repo, row["id"], mid, today):
            kept += 1
    return kept


def scan(conn, repo, search, dry_run=False, report=None) -> ScanReport:
    """Every archived mail whose subject carries a case number: a
    suggestion for two numbers or a number that does not fit (once per
    mail, whatever became of it), and each case's acknowledgement (once)."""
    report = report or ScanReport()
    internal = internal_domains(conn)
    cache = {}
    acked = set()
    for mail in search.by_subject("OPP", limit=100000):
        if not parsing.case_numbers(mail.subject):
            continue
        link = link_of(conn, mail, search, internal, cache)
        if link.kind in ("numbers", "mismatch"):
            if _suggested_before(conn, mail):
                report.skipped += 1
                continue
            title = TWO_NUMBERS if link.kind == "numbers" else CHECK_NUMBER
            report.suggested.append("{}: {} - {}".format(", ".join(link.opp_nos), title, (mail.subject or "")[:70]))
            if not dry_run:
                repo.insert("suggestion", {
                    "enquiry_id": link.case_ids[0], "opp_no": link.opp_nos[0], "kind": "case_number",
                    "proposed_status": title, "named_cases": ",".join(link.opp_nos),
                    "proposed_date": _stamp(mail)[:10] or None, "note": title,
                    "evidence_sender": _who(mail, internal), "evidence_date": _stamp(mail)[:16] or None,
                    "evidence_quote": mail.subject or "", "message_id": getattr(mail, "message_id", "") or None,
                    "entry_id": getattr(mail, "entry_id", "") or None, "source": "scan"})
        elif link.kind == "number" and link.case_id not in acked:
            acked.add(link.case_id)
            case = conn.execute("SELECT e.*, p.key AS requester_key FROM enquiry e JOIN party p "
                                "ON p.id = e.requester_party_id WHERE e.id = ?", (link.case_id,)).fetchone()
            if case is None or has_ack(conn, case["id"]):
                continue
            hydrate = getattr(search, "hydrate", None)
            mails = [m for m in numbered_mails(search, case["opp_no"])
                     if belongs(conn, case["id"], m, search, internal, cache)]
            if hydrate is not None:
                mails = hydrate(mails)
            found = ack_mail(conn, case, mails, search, internal)
            if found is None:
                continue
            at = _stamp(found)[:10]
            report.acknowledged.append("{} {} {}".format(case["opp_no"], at, (found.subject or "")[:60]))
            if not dry_run:
                mid = (getattr(found, "message_id", "") or "").strip()
                repo.add_ack(case["id"], at, "{}: {}{}".format(
                    ACK_NOTE, (found.subject or "")[:120], " [mid:{}]".format(mid) if mid else ""))
    report.remembered = remember_registered(conn, repo, search, dry_run)
    return report


# ------------------------------------------------- mixed-up numbers (FX7) --
#: FX7 item 7: the report's columns
MISMATCH_COLUMNS = ("case", "date", "sender", "subject", "joins today", "fits through",
                    "earlier in its conversation", "conversation")


def _fits_through(conn, case, mail, internal) -> str:
    """'requester' when the requester sent it or is on its To or Cc line,
    else 'vendor' (one of the case's vendors made it fit)."""
    requester = party_of(conn, case["requester_party_id"])
    if requester.sent(mail) or to_requester(conn, case, mail):
        return "requester"
    return "vendor"


def _case_threads(conn, search, case, cache) -> tuple:
    """(conversations, subject keys) a case holds without its number: the
    registered mail's conversation, the case's own and attached ones, the
    conversations of its mails that carry no number of it; its thread keys,
    its subject's and the attached subjects'."""
    from . import case_mail
    if case["id"] in cache:
        return cache[case["id"]]
    values = db.thread_values(conn, case["id"])
    conversations = set(values["conversation"])
    keys = {k for k in values["thread_key"] if k}
    key = parsing.subject_thread_key(case["source_subject"] or "")
    if key:
        keys.add(key)
    if db._has_table(conn, "enquiry_thread"):
        for r in conn.execute("SELECT note FROM enquiry_thread WHERE enquiry_id = ? AND voided = 0 AND note IS NOT NULL",
                              (case["id"],)):
            key = parsing.subject_thread_key(r["note"] or "")
            if key:
                keys.add(key)
    if case["entry_id"]:
        registered = search.by_entry_id(case["entry_id"])
        if registered is not None and registered.conversation_id:
            conversations.add(registered.conversation_id)
    try:
        own = case_mail.case_mails(conn, search, case)
    except Exception:                       # measuring only: a case the reader cannot read adds nothing
        own = []
    for mail in own:
        if case["opp_no"] not in parsing.case_numbers(mail.subject or "") and mail.conversation_id:
            conversations.add(mail.conversation_id)
    cache[case["id"]] = (conversations, keys)
    return cache[case["id"]]


def _earlier_numbers(search, mail, opp_no) -> str:
    """What the earlier mails of the mail's conversation carry: 'no number'
    when one carries none, the other numbers when one carries another,
    'the same number' when all carry it, 'first in its conversation'."""
    conversation = (mail.conversation_id or "").strip()
    if not conversation:
        return "no conversation id"
    stamp = _stamp(mail)
    earlier = [m for m in search.by_conversation(conversation, limit=500) if _stamp(m) < stamp]
    if not earlier:
        return "first in its conversation"
    plain = [m for m in earlier if not parsing.case_numbers(m.subject or "")]
    others = sorted({n for m in earlier for n in parsing.case_numbers(m.subject or "") if n != opp_no})
    parts = []
    if plain:
        parts.append("{} earlier with no number".format(len(plain)))
    if others:
        parts.append("earlier with {}".format(", ".join(others)))
    return "; ".join(parts) or "every earlier one carries {}".format(opp_no)


def mismatches(conn, search) -> list:
    """FX7 item 7, read only: the archived mails whose subject carries one
    valid case number that fits (rule (b), link kind 'number') while their
    conversation is none of that case's (_case_threads) and their subject
    without the number is none of the case's subjects or thread keys - a
    number put into another thread with the same people (OPP-1728's
    Zimbabwe thread, 17 Nov). Rows (dicts of MISMATCH_COLUMNS), by case and
    date. Nothing is written."""
    internal = internal_domains(conn)
    cache, threads = {}, {}
    out = []
    seen = set()
    for mail in search.by_subject("OPP", limit=100000):
        ref = mail_ref(mail) or id(mail)
        if ref in seen or not parsing.case_numbers(mail.subject or ""):
            continue
        seen.add(ref)
        link = link_of(conn, mail, search, internal, cache)
        if link.kind != "number":
            continue
        case = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (link.case_id,)).fetchone()
        if case is None:
            continue
        conversations, keys = _case_threads(conn, search, case, threads)
        if mail.conversation_id and mail.conversation_id in conversations:
            continue
        if parsing.subject_thread_key(mail.subject or "") in keys:
            continue
        sender = "{} <{}>".format(mail.sender_name or "", mail.sender_address or "").strip()
        out.append({"case": case["opp_no"], "date": _stamp(mail)[:16], "sender": sender,
                    "subject": mail.subject or "", "joins today": case["opp_no"],
                    "fits through": _fits_through(conn, case, mail, internal),
                    "earlier in its conversation": _earlier_numbers(search, mail, case["opp_no"]),
                    "conversation": mail.conversation_id or ""})
    out.sort(key=lambda r: (r["case"], r["date"]))
    return out


def write_mismatches(rows, path) -> None:
    """The list as a UTF-8 CSV (with BOM, for Excel), every field quoted."""
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=MISMATCH_COLUMNS, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)
