r"""Propose a status for cases nobody ever confirmed.

The migration brought in cases whose Status cell was blank. The answer is
usually sitting in Outlook: the agent wrote 'please proceed', or said the rate
was too high, or nothing came back after we quoted. This reads those threads
and writes a CSV of proposals.

It never writes to the database and never touches Outlook beyond reading.
Applying a proposal is a separate command over the CSV, so the list can be
edited - deleting a row is how you say no.

Finding the thread, in order:

1. `conversation_id`, then `thread_key`, when the case carries one
2. the case's own mail folder, named 'OPP-1287 ...' or '-0327 ...'
3. the rarest words of the case (requester, lane, commodity) against subjects,
   with document frequency counted over every enquiry, never over a shortlist

What it decides is a suggestion with the sentence it came from, so that the
answer can be checked in one glance rather than trusted.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import branding, db, dedupe, intake, matching, parsing

#: What the agent says when the business is ours.
WON_PHRASES = (
    "please proceed",
    # 'proceed with the booking', not 'the address to proceed with quotation';
    # T5b: nor their own next step, 'to proceed with customer submission'
    re.compile(r"proceed with (?!(the |a |our |your |this )?"
               r"(quot|rate|pric|cost|customer|client|submission|approval|review))"),
    "go ahead", "please arrange",
    "quote is approved", "quotation is approved", "quote approved",
    "quotation approved", "rate is approved", "rates are approved",
    "approved the quote", "approved your quote", "approved your rate",
    "we approve", "aprobado", "aprobada",
    # the customer sending the paperwork is the customer going ahead
    "ready to go", "attached are the documents", "attached are the shipping documents",
    "documents for this shipment", "please find attached the documents",
    "shipping documents attached",
    "please book", "booking confirm", "confirm the booking", "confirmed the booking",
    "we confirm", "confirmamos", "puede proceder", "adelante con",
    "please issue the bl", "please pick up", "please pickup",
)
#: What they say when it went elsewhere.
LOST_PRICE_PHRASES = (
    "too high", "not competitive", "better rate", "cheaper", "más económico",
    "mas economico", "price is high", "high price", "out of budget",
    "over budget", "above budget",
)
LOST_OTHER_PHRASES = (
    "awarded to", "another forwarder", "went with", "going with another",
    "chose another", "otro agente", "otra naviera",
)
#: The customer arranging it themselves: the shipment goes ahead without us,
#: so it is Lost, and it is read before any cancel phrase (a customer who
#: will 'proceed on their own' and asks us to cancel what we had started).
OWN_ARRANGEMENT_PHRASES = (
    "proceed on their own", "ship it ourselves", "our own forwarder", "handle it themselves",
)
def is_requote_note(note) -> bool:
    """A proposal whose note says re-quote reopens the quote round."""
    return (note or "").strip().casefold().startswith("re-quote")


#: What they say when the shipment is not going ahead at all: Cancelled
#: (C1, 2026-10-02). 'On hold' and 'postponed' are not here: a postponement
#: is not a closing (detect-followups keeps the reply as a note, and On hold
#: needs her Until date).
CANCELLED_PHRASES = (
    "cancel", "cancelled", "canceled",
    # 'no longer' on its own also says 'Dana no longer works here'
    "no longer need", "no longer require", "no longer going", "no longer ship",
    "no longer interested", "no longer proceed", "no longer moving",
    "not shipping", "se cancela", "anulado",
)
#: The order never confirmed: Cancelled, Order not confirmed.
NOT_CONFIRMED_PHRASES = (
    "did not move on", "not confirm the order", "order was not placed",
)
#: The note the retired 'they asked for the rate again' proposal carried
#: (2026-09-17 to 2026-09-25). Nothing proposes it any more - that mail is
#: R3's Replied step, which already offers the Re-quoted button - but a
#: suggestion or a CSV row still carrying it is recognised: withdraw-
#: requote-suggestions takes the pending ones back, and is_requote_note()
#: reads the 'Re-quote' mark on the Re-quoted suggestions that replaced it
#: (detect_followups.REQUOTED_NOTE).
REQUOTE_NOTE = "Re-quote: they asked for the rate again"
#: Our own side booking the job: the mail Victor sends the office when
#: the customer has said yes, and the note asking for it to be put on the
#: list. Read from internal mail only.
BOOKING_PHRASES = (
    "new booking", "booking confirmed", "inform overseas to proceed",
    "inform origin to proceed", "proceed with the booking",
    "keep this on record", "record this on our list", "add this to the list",
    "add this to our list", "please add to the list", "shipment confirmed",
)
#: What our own quote looks like going out: the sentence it opens with, never
#: a number. A mail quoting the customer's own figures back at them is full
#: of USD and per-kg and is not a quote.
QUOTE_PHRASES = (
    "please see below rate", "please see below rates",
    "please find below rate", "please find below rates",
    "please see below dest charges", "please see below destination charges",
    "rates for your reference", "rate for your reference",
    "rates for your review", "rate for your review",
    "please find attached quotation", "please find attached our quotation",
    "please see attached quotation",
    "our rate as below", "our rates as below",
    # the same sentences with a word or two in the middle: 'please see
    # below our new rates', 'see below revised rates', 'find attached
    # our updated quotation'
    # ...and a few words of its own on the way: 'see below our lowest
    # FCL rates on FOB terms', 'our air freight Door - Airport rates'
    re.compile(r"\b(see|find) (below|attached)(:)?( [a-z0-9/&\-\u2013\u2014]+){0,7}?"
               r" (rates?|quotation|quotes?|charges|costs?|pricing|offer)\b"),
    re.compile(r"\b(rates?|quotation|quotes?|costs?) (as )?(per )?(below|attached)\b"),
    # 'Below rates, as promised:', 'below is the revised quotation'
    re.compile(r"\bbelow (are |is )?(our |the )?(best |updated |revised |all-in )?"
               r"(rates?|quotation|quotes?|charges|costs?)\b"),
)
#: Us chasing after a quote went out - the quote itself may be in a mail
#: that never reached this mailbox, the chase says it happened.
QUOTE_FOLLOW_UP_PHRASES = (
    "secure this business", "capture this business", "win this business",
    "following up on the rates", "following up on our rates",
    "following up on the quotation", "following up on our quotation",
    "feedback on the rates", "feedback on our rates", "feedback on our quotation",
    "rates provided", "rate provided", "quotation provided", "quote provided",
    "rates we sent", "rates we provided", "quotation we sent",
)
#: The agent acknowledging our quote ('Thanks a lot for the quotation', 'Thnks
#: fr yr costs'): as sure a sign of a quote as the quote itself.
QUOTE_ACK_PHRASES = (
    re.compile(r"\b(thank(s| you)|thx|tnks|thnks|thanks a lot|many thanks)\b"
               r"[^.\n!]{0,15}\b(for|fr)\b[^.\n!]{0,12}\b(your|yr|ur|the|this)\b"
               r"[^.\n!]{0,25}\b(rates?|quotation|quotes?|costs?|offer|pricing"
               r"|tariff|proposal)\b"),
    "noted your rate", "noted your quotation", "received your rate",
    "received your quotation", "we have received the rate", "gracias por la cotiz",
    "gracias por las tarifas", "gracias por su cotiz",
    # ...or passing it on: 'we sent the proposal to our customer today'
    "sent the proposal", "sent your proposal", "sent the rates to",
    "sent your rates to", "sent your quotation to", "sent the quotation to",
    "forwarded your rates", "forwarded your quotation", "forwarded the rates",
    "shared your rates", "passed your rates", "presented your rates",
    "will check with the client", "check them with cnee", "revert to you results",
)
#: Us asking them for something, with nothing back.
ASKING_PHRASES = (
    "please advise", "please confirm", "could you send", "please provide",
    "kindly advise", "waiting for", "any update", "please share",
    "kindly provide", "please send", "we still need", "we need the",
    "we would need", "need the following", "please let us know",
    "please let me know", "can you advise", "could you advise",
    "can you confirm", "could you confirm", "can you please",
)

STATUS_UNKNOWN = "Unknown"

#: A sentence is quoted back as evidence, so it has to be short enough to read.
EVIDENCE_LEN = 160
BODY_LEN = 300
LAST_MAILS = 3


@dataclass
class ThreadMail:
    """One mail of a case thread, as far as this needs to know."""

    at: str
    sender_name: str
    address: str
    internal: bool
    subject: str
    body: str
    from_requester: bool = False
    #: Addressed to the requester (a domain on the To/CC line, or the
    #: office prefix when one of our offices is the requester).
    to_requester: bool = False
    #: FX6: at least one recipient outside the internal domains (what tells
    #: our quote from a forward among ourselves when the requester has no
    #: domain on file)
    to_outside: bool = False
    folder_path: str = ""
    #: T6: written on our domain by another of our offices (office_side):
    #: a vendor answering us, neither ours nor the requester's
    other_office: bool = False
    #: SG1: the mail's own ids, carried to a suggestion read from it
    message_id: str = ""
    entry_id: str = ""

    @property
    def external(self) -> bool:
        """Outside mail that can say something about the case: never
        another office's (T6)."""
        return not self.internal and not self.other_office


@dataclass
class Proposal:
    opp_no: str
    requester: str
    current_status: str
    proposed_status: str = STATUS_UNKNOWN
    proposed_date: str = ""
    closed_reason: str = ""
    evidence_sender: str = ""
    evidence_date: str = ""
    evidence_quote: str = ""
    confidence: str = "low"
    awarded_vendor: str = ""
    found_by: str = ""
    mails_seen: int = 0
    note: str = ""
    current_since: str = ""
    mails: list = field(default_factory=list)
    #: SG1: the evidence mail's Message-ID and entry id
    message_id: str = ""
    entry_id: str = ""

    @property
    def verdict(self) -> str:
        """change / keep / no thread - what the row means for the case."""
        if self.proposed_status == STATUS_UNKNOWN:
            return "no thread" if not self.mails_seen else "undecided"
        return "keep" if self.proposed_status == self.current_status else "change"


# ------------------------------------------------------------ the reading --
def _quote_free(body: str) -> str:
    """The writer's own words, with the quoted history cut off.

    Line breaks are kept: they are where one sentence ends and the next
    begins in a mail, and the evidence is quoted a sentence at a time.
    """
    lines = [" ".join(line.split()) for line in parsing.body_lines(body)]
    return "\n".join(line for line in lines if line)[:BODY_LEN]


def office_prefix(requester_key: str, conn=None) -> str:
    """'JoJo JJT' -> 'JJT-': the way that office signs its mail."""
    code = branding.office_code((requester_key or "").strip(), conn)
    return code + "-" if code else ""


#: T6: who wrote a mail on our own domain (office_side)
SIDE_OURS, SIDE_REQUESTER, SIDE_OTHER_OFFICE = "ours", "requester", "other office"


def office_side(sender_name: str, office: str = "", handled_by: str = "") -> str:
    """T6, the one reading of a mail on our own domain, for propose and
    detect_followups: the requester's when the requester is a JoJo
    office and the sender's name carries its prefix (`office`, 'JJT-');
    ours when the name carries no office code (JJC's own people:
    intake._office_names leaves HOME_OFFICE and '#' aliases out) or the
    code of the office handling the case (handled_by, OPP-1347); otherwise
    another office's - a vendor answering us."""
    name = (sender_name or "").strip()
    if office and name.upper().startswith(office):
        return SIDE_REQUESTER
    codes = intake._office_names([name])
    if not codes:
        return SIDE_OURS
    if (handled_by or "").strip().upper() in codes:
        return SIDE_OURS
    return SIDE_OTHER_OFFICE


def handled_by_of(row) -> str:
    """The case's handled_by, or '' when the row does not carry it."""
    return (row["handled_by"] or "") if row is not None and "handled_by" in row.keys() else ""


def to_thread_mail(mail, internal_domains, requester_domains,
                   office: str = "", handled_by: str = "") -> ThreadMail:
    """One of our offices asking us for a rate writes from jojofreight.example like
    we do; the prefix on the sender's name ('JJT-FRANK ZHOU') is theirs,
    and their mail is read as the customer's, not as ours. T6: any other
    office on our domain (not JJC, not the case's handled_by) is a vendor
    answering us: neither ours nor the requester's (office_side)."""
    address = (getattr(mail, "sender_address", "") or "").lower()
    domain = matching.domain_of(address)
    sender_name = getattr(mail, "sender_name", "") or ""
    internal = bool(getattr(mail, "sender_internal", False)) or \
        (domain and domain in internal_domains)
    from_requester = bool(domain and domain in requester_domains)
    other_office = False
    if internal:
        side = office_side(sender_name, office, handled_by)
        if side == SIDE_REQUESTER:
            internal, from_requester = False, True
        elif side == SIDE_OTHER_OFFICE:
            internal, from_requester, other_office = False, False, True
    display_to = (getattr(mail, "display_to", "") or "").lower()
    to_requester = any(d in display_to for d in requester_domains) or any(
        matching.domain_of(str(r).lower()) in requester_domains
        for r in (getattr(mail, "recipients", []) or [])) or bool(
        office and office.lower() in display_to)
    # FX6b: a mail of ours whose recipients could not be resolved: an
    # address on its display line with one of the requester's domains
    # counts (names alone never do)
    if internal and not (getattr(mail, "recipients", []) or []):
        shown = re.findall(r"[\w.+'-]+@[\w.-]+", display_to)
        to_requester = to_requester or any(matching.domain_of(a) in requester_domains for a in shown)
    # FX6: a light read carries no recipients; Outlook found our own mail by
    # the requester's domain on a header, which for our mail is To or Cc
    to_requester = to_requester or bool(internal and getattr(mail, "header_match", False) and requester_domains)
    # FX6: a recipient outside our own domains (To and Cc as resolved; the
    # addresses on the display line when they were not)
    recipients = [str(r).lower() for r in (getattr(mail, "recipients", []) or [])] or \
        re.findall(r"[\w.+'-]+@[\w.-]+", display_to)
    to_outside = any(matching.domain_of(r) and matching.domain_of(r) not in internal_domains for r in recipients)
    return ThreadMail(
        at=getattr(mail, "received_on", "") or "",
        sender_name=sender_name,
        address=address,
        internal=bool(internal),
        subject=getattr(mail, "subject", "") or "",
        body=_quote_free(getattr(mail, "body_excerpt", "") or ""),
        from_requester=from_requester,
        to_requester=to_requester,
        to_outside=to_outside,
        folder_path=getattr(mail, "folder_path", "") or "",
        other_office=other_office,
        message_id=(getattr(mail, "message_id", "") or "").strip(),
        entry_id=getattr(mail, "entry_id", "") or "",
    )


#: A word has to be this rare across the whole book to be worth searching on.
RARE_AT_MOST = 3
#: Four letters is not a needle: '%hand%' and '%long%' match half the mailbox
#: ('Long Beach', 'handling', 'shorthand').
MIN_TERM_LEN = 5
#: A term that brings back this many mails is not identifying one case, so
#: the next term is tried instead of trusting the pile.
TOO_BROAD = 60


def requester_frequency(conn) -> Counter:
    """How many cases each word of a requester's name appears on.

    'Tolvex' is on forty cases and identifies nothing; 'Morvel' is on two and
    identifies the thread. Counted over every enquiry, like every other
    document frequency here.
    """
    counter = Counter()
    for row in conn.execute(
            "SELECT rp.key AS requester_key FROM enquiry e "
            "JOIN party rp ON rp.id = e.requester_party_id"):
        for token in set(dedupe.name_tokens(row["requester_key"])):
            counter[token] += 1
    return counter


def rare_terms(row, df: Counter, limit: int = 3,
               requester_df: Counter = None) -> list:
    """The words of this case that hardly any other case uses.

    The commodity and the lane come from the case; the requester's name is
    added because a case often has neither ('RATE REQUEST // ATL - GYE' with
    no commodity), and the agent's own name still narrows the mailbox down.

    Document frequency is counted over every enquiry: a
    word can look unique inside a handful of candidates and be worthless
    across the whole book.
    """
    terms = []
    for token in dedupe._row_tokens(row):
        if len(token) < MIN_TERM_LEN:
            continue
        frequency = df.get(token, 0)
        if frequency and frequency <= RARE_AT_MOST:
            terms.append((frequency, -len(token), token))
    if requester_df is not None:
        requester = row["requester_key"] if "requester_key" in row.keys() else ""
        for token in set(dedupe.name_tokens(requester)):
            if len(token) < MIN_TERM_LEN:
                continue
            frequency = requester_df.get(token, 0)
            if frequency and frequency <= RARE_AT_MOST:
                # Ranked after the cargo words: a commodity is a better
                # needle than a company that has a handful of cases.
                terms.append((frequency + 0.5, -len(token), token))
    terms.sort()
    return [token for _, _, token in terms[:limit]]


#: How far either side of the received date the requester's own mail is
#: taken as belonging to this case.
WINDOW_BEFORE = 3
WINDOW_AFTER = 45


def case_window(received_on: str) -> tuple:
    day = dt.date.fromisoformat(received_on[:10])
    return ((day - dt.timedelta(days=WINDOW_BEFORE)).isoformat(),
            (day + dt.timedelta(days=WINDOW_AFTER)).isoformat())


def _touches(mail, domains) -> bool:
    """Did one of these domains send the mail, or receive it?"""
    if getattr(mail, "header_match", False):
        return True                     # Outlook matched it on a header
    addresses = [getattr(mail, "sender_address", "") or ""]
    addresses.extend(getattr(mail, "recipients", []) or [])
    return any(matching.domain_of(a) in domains for a in addresses)


def _narrow_to_case(found, row, df, requester_df, own_window=False,
                    siblings=None, hydrate=None) -> tuple:
    """The requester is on many threads; keep the ones that are this case.

    Whatever identifies the case is tried in turn - conversation id, thread
    key, a rare word in the subject - and the first that matches anything
    narrows the pool. When nothing identifies it, the whole window stands.

    Inside the requester's own window (`own_window`) its name is no needle:
    every mail there is theirs, so only the cargo and lane words count.
    """
    conversations, keys = _identities(row)
    if conversations:
        hits = [m for m in found if m.conversation_id in conversations]
        if hits:
            return hits, "conversation id"
    if keys:
        hits = [m for m in found
                if parsing.subject_thread_key(m.subject) in keys]
        if hits:
            return hits, "thread key"
    for term in rare_terms(row, df,
                           requester_df=None if own_window else requester_df):
        hits = [m for m in found if term in (m.subject or "").lower()]
        if hits:
            return hits, "rare word '{}'".format(term)
    if own_window and not keys:
        hits, how = _started_with_the_case(found, row, siblings or (), hydrate)
        if hits:
            return hits, how
    return found, "whole window"


#: A thread that opened this many days either side of the received date
#: is taken to have opened with the case.
STARTED_WITHIN = 1


#: How many opening mails are read in full to look for the commodity.
OPENERS_AT_MOST = 8


def _started_with_the_case(found, row, siblings, hydrate=None) -> tuple:
    """(mails, how): the thread of theirs that opened on the case's
    received date.

    A case migrated from the workbook has no subject and no thread key;
    its commodity ('ceramic floor tiles') need not be in the subject
    line ('Pedido - Ceramicas Norte Chile'). What it does have is a date.
    The agent's one thread that starts on that date is the case. When
    several start then - or another dateless case of theirs was received
    the same day - the opening mails are read in full, and the one that
    names the commodity in its body is the case; failing that, nothing
    can tell them apart.
    """
    received = row["received_on"] or ""
    if not received:
        return [], ""
    threads = {}
    for mail in found:
        key = parsing.subject_thread_key(mail.subject or "") or mail.conversation_id
        threads.setdefault(key, []).append(mail)
    started = [sorted(mails, key=lambda m: m.received_on or "")
               for mails in threads.values()
               if _days_apart(min(m.received_on or "" for m in mails), received)
               <= STARTED_WITHIN]
    if not started:
        return [], ""
    # Five Tolvex threads opened that day; four of them carry the thread
    # keys of their other cases. What is left is this one.
    claimed = {other["thread_key"] for other in siblings if other["thread_key"]}
    unclaimed = [mails for mails in started
                 if parsing.subject_thread_key(mails[0].subject or "") not in claimed]
    if len(unclaimed) == 1 and len(started) > 1:
        return unclaimed[0], ("thread started on the received date, the others "
                              "are their other cases")
    started = unclaimed or started
    ambiguous = len(started) > 1 or any(
        not other["thread_key"]
        and _days_apart(other["received_on"], received) <= STARTED_WITHIN
        for other in siblings)
    if not ambiguous:
        return started[0], "thread started on the received date"
    words = {t for t in dedupe.name_tokens(row["commodity"] or "")
             if len(t) >= 4 and t not in SUBJECT_STOCK_WORDS}
    if not words or hydrate is None:
        return [], ""
    openers = [mails[0] for mails in started[:OPENERS_AT_MOST]]
    named = []
    for opener, mails in zip(hydrate(openers), started):
        body = (getattr(opener, "body_excerpt", "") or "").lower()
        if any(w in body for w in words):
            named.append(mails)
    if len(named) == 1:
        return named[0], "thread started on the received date, commodity in the mail"
    return [], ""


def _days_apart(a: str, b: str) -> int:
    try:
        return abs((dt.date.fromisoformat(a[:10]) - dt.date.fromisoformat(b[:10])).days)
    except (TypeError, ValueError):
        return 10 ** 6


def _folder_hints(row) -> list:
    """Words a folder named after the requester would carry."""
    requester = row["requester_key"] if "requester_key" in row.keys() else ""
    return [t for t in dedupe.name_tokens(requester or "") if len(t) >= 4]


def find_thread(search, conn, row, df: Counter,
                requester_df: Counter = None, requester_domains=(),
                span=None, subject_df: Counter = None) -> tuple:
    """(mails, how it was found), with their bodies read. Empty when
    nothing plausible turned up.

    AL1: the mails whose subject carries the case's number come first (and
    their conversations); whatever is found is kept only when
    case_numbers says it is this case's - a mail numbered for another case,
    two numbers, or a number that does not fit never reads as this one."""
    from . import case_numbers
    numbered = []
    if "id" in row.keys() and row["opp_no"] and hasattr(search, "entry_ids_of"):      # the mail archive
        numbered = case_numbers.keep(conn, row["id"], case_numbers.numbered_mails(search, row["opp_no"]), search)
    if numbered:
        found, how = _whole_conversations(search, numbered), "case number {} in the subject".format(row["opp_no"])
    else:
        found, how = _find_thread(search, conn, row, df, requester_df,
                                  requester_domains, span, subject_df)
    if found and "id" in row.keys():
        found = case_numbers.keep(conn, row["id"], found, search)
    hydrate = getattr(search, "hydrate", None)
    if found and hydrate is not None:
        found = hydrate(found)
    return found, how


def _find_thread(search, conn, row, df: Counter,
                 requester_df: Counter = None, requester_domains=(),
                 span=None, subject_df: Counter = None) -> tuple:
    """(mails, how it was found). Empty when nothing plausible turned up.

    The requester's own domains come first: every mail they sent or were
    sent in the case's window is a candidate, narrowed to this case where
    something identifies it. Only a requester with no known domain falls
    back to the case folder and the rare words.

    `span` widens the dates asked of Outlook (one search covering all of a
    requester's cases in the run); the case window is applied here anyway.
    """
    domains = {d for d in requester_domains if d}
    hints = _folder_hints(row)
    opp_no = row["opp_no"]
    pool = []
    if domains and row["received_on"]:       # no date, no window (OPP-1337)
        start, end = case_window(row["received_on"])
        ask_start, ask_end = span or (start, end)
        pool = [m for m in search.by_domains(sorted(domains), ask_start, ask_end,
                                             hints=hints, opp_no=opp_no)
                if start <= (m.received_on or "") <= end and _touches(m, domains)]
        label = "/".join(sorted(domains))
        if pool:
            siblings = _other_cases_in_window(conn, row)
            hits, how = _narrow_to_case(pool, row, df, requester_df,
                                        own_window=True, siblings=siblings,
                                        hydrate=getattr(search, "hydrate", None))
            if how != "whole window":
                return _whole_conversations(search, hits), \
                    "requester domain {} ({}, {} of {} mails)".format(
                        label, how, len(hits), len(pool))

    found, how = _by_identity(search, row, df, requester_df, hints, opp_no,
                              subject_df if subject_df is not None else df,
                              domains)
    if found:
        return found, how

    if pool:
        # Nothing names this case in their mail. Theirs alone in these
        # weeks, the whole window is the thread; with other cases of
        # theirs alongside it, reading it would read another case.
        if not siblings:
            return _whole_conversations(search, pool), \
                "requester domain {} (whole window, {} mails)".format(
                    label, len(pool))
        others = [r["opp_no"] for r in siblings]
        return [], ("requester domain {}: {} mails in the window, none "
                    "identifiable as this case ({} overlap: {})".format(
                        label, len(pool), len(others), ", ".join(others[:4])))
    return [], how


#: How many of the found conversations are completed from Outlook.
EXPAND_AT_MOST = 6


def _whole_conversations(search, hits) -> list:
    """The hits plus every mail Outlook keeps in their conversations.

    The domain search only returns what the other side sent; the quote we
    sent back sits in the same conversation.
    """
    expand = getattr(search, "conversation_of", None)
    if expand is None:
        return hits
    out, seen, done = [], set(), set()
    for mail in hits:
        key = mail.entry_id or id(mail)
        if key not in seen:
            seen.add(key)
            out.append(mail)
    for mail in hits:
        conversation = mail.conversation_id
        if not conversation or conversation in done or len(done) >= EXPAND_AT_MOST:
            continue
        done.add(conversation)
        for other in expand(mail):
            key = other.entry_id or id(other)
            if key not in seen:
                seen.add(key)
                out.append(other)
    return out


def _other_cases_in_window(conn, row) -> list:
    """The requester's other live cases whose windows overlap this one
    (rows with opp_no, received_on, thread_key)."""
    start, end = case_window(row["received_on"])
    return list(conn.execute(
        "SELECT opp_no, received_on, thread_key FROM enquiry "
        "WHERE requester_party_id = ? AND opp_no != ? "
        "  AND legacy = 0 AND received_on >= date(?, '-{} days') "
        "  AND received_on <= date(?, '+{} days') ORDER BY opp_no".format(
            WINDOW_AFTER, WINDOW_BEFORE),
        (row["requester_party_id"], row["opp_no"], start, end)))


def _by_identity(search, row, df, requester_df, hints, opp_no,
                 subject_df, domains=()) -> tuple:
    """The thread by what names the case: conversation id, thread key, its
    folder, a rare word. (mails, how) - or ([], why not)."""
    conversations, keys = _identities(row)
    for conversation in sorted(conversations):
        found = search.by_conversation(conversation)
        if found:
            return found, "conversation id"

    thread_key = row["thread_key"] if "thread_key" in row.keys() else None
    subject = row["source_subject"] if "source_subject" in row.keys() else None
    commodity = row["commodity"] if "commodity" in row.keys() else None
    if thread_key and not subject and commodity and \
            parsing.subject_thread_key(commodity) == thread_key:
        # The workbook kept some subject lines in the commodity column
        # ('RE: Pricing needed for sea and air // PO# ...').
        subject = commodity
    if keys and subject:
        # The key itself is one long word with the spaces taken out, so it
        # never appears in a subject line. The search term has to be a word
        # the subject really contains - and one few other cases use, or
        # the whole mailbox answers; the key is what confirms the match.
        # An attached thread is searched by its own subject.
        subjects = [subject]
        if isinstance(row, dict):
            subjects += [s for s in (row.get("extra_subjects") or ()) if s]
        for text in subjects:
            term = _search_word(text, subject_df)
            if term:
                found = [m for m in search.by_subject(term, hints=hints, opp_no=opp_no)
                         if parsing.subject_thread_key(m.subject) in keys]
                if found:
                    return found, "thread key"

    for info in search.case_folders(row["opp_no"]):
        found = search.in_folder(info)
        if found:
            return found, "case folder {}".format(info.path)

    broad = []
    for term in rare_terms(row, df, requester_df=requester_df):
        found = search.by_subject(term, hints=hints, opp_no=opp_no)
        if not found:
            continue
        if len(found) > TOO_BROAD:
            broad.append((term, len(found)))
            continue
        if domains and not any(_touches(m, domains) for m in found):
            # 'voice' found Cargoways' thread for a Keel Cargo case:
            # a word in a subject line is not the requester on the mail.
            continue
        return found, "rare word '{}'".format(term)
    if broad:
        term, count = broad[0]
        return [], "'{}' matched {} mails - too many to be this case".format(
            term, count)
    if getattr(search, "cut_short", False):
        return [], "the search ran out of time before it finished"
    return [], ""


def _longest_word(text: str) -> str:
    words = re.findall(r"[A-Za-z]{4,}", text or "")
    return max(words, key=len) if words else ""


#: A subject word used by more cases than this is no search term:
#: 'Request', 'Inquiry' and 'shipment' each walk the whole mailbox for 45 s
#: and come back cut short.
SUBJECT_TOO_COMMON = 15


#: Words every second subject line carries. Never a search term, whatever
#: the counts say.
SUBJECT_STOCK_WORDS = frozenset((
    "request", "requests", "quotation", "quotations", "quote", "quotes",
    "rates", "rate", "shipment", "shipments", "inquiry", "enquiry", "urgent",
    "cargo", "freight", "export", "import", "ocean", "container", "containers",
    "booking", "pricing", "please", "kindly", "offer", "costs", "charges",
    "delivery", "order", "orders", "consol", "consolidate", "consolidated",
    "shipping", "logistics", branding.COMPANY_SHORT.lower(), "transport", "service", "services",
    "airfreight", "seafreight", "pallet", "pallets", "update", "urgente",
    "cotizacion", "cotización", "tarifa", "tarifas", "embarque", "solicitud",
))


def subject_frequency(conn) -> Counter:
    """How many cases carry each word in their subject line - the count a
    subject search term is judged by. Over every enquiry."""
    counter = Counter()
    for row in conn.execute("SELECT source_subject FROM enquiry "
                            "WHERE source_subject IS NOT NULL"):
        for word in {w.lower() for w in
                     re.findall(r"[A-Za-z][A-Za-z0-9]{4,}", row["source_subject"])}:
            counter[word] += 1
    return counter


def _search_word(subject: str, df: Counter) -> str:
    """The subject word the fewest other cases use (longest on a tie).

    `df` is the subject frequency; stock words are out whatever it says."""
    words = {w for w in re.findall(r"[A-Za-z][A-Za-z0-9]{4,}", subject or "")
             if w.lower() not in SUBJECT_STOCK_WORDS}
    if not words:
        return ""
    best = min(words, key=lambda w: (df.get(w.lower(), 0), -len(w), w))
    return best if df.get(best.lower(), 0) <= SUBJECT_TOO_COMMON else ""


# --------------------------------------------------------- the evidence --
#: 'Dear @JJC-AVA TRAN <mailto:AvaT@jojofreight.example>' is how the mail opens, not
#: what it says.
_SALUTATION = re.compile(
    r"^\s*(dear|hi|hello|hola|estimad[oa]s?|buenos d[ií]as)\b.*$",
    re.IGNORECASE)

#: How a mail ends. Everything from here down is the signature block.
#: The whole line has to be made of these words, so 'Thanks / Regards' and
#: 'Thanks & best regards,' are sign-offs while 'Thanks for the rates' is not.
_SIGN_OFF = re.compile(
    r"^\s*(?:(?:thanks?|thank you|best|kind|warm|many|regards?|saludos?|"
    r"cordial|un|atentamente|br|tks|rgds)[\s/,&.!-]*)+$", re.IGNORECASE)

#: Signature and disclaimer lines, which can also sit above the sign-off.
_SIGNATURE_LINE = re.compile(
    r"^\s*(tel|fax|cell|mobile|email|e-mail|website|mfn id|address)\s*[:.]"
    r"|^\s*-{2,}\s*$"
    r"|" + re.escape(branding.INTERNAL_DOMAIN) +
    r"|if you would like to no longer receive"
    r"|this email transmission",
    re.IGNORECASE)


def _message_lines(body: str) -> list:
    """The lines that are the message, with the signature block cut off.

    Quoting 'Email: someone@jojofreight.example | Website: www.jojofreight.example' as the
    evidence for a status is worse than quoting nothing at all.
    """
    kept = []
    for line in re.split(r"\n+", body or ""):
        line = line.strip()
        if not line:
            continue
        if _SIGN_OFF.match(line):
            break
        if _SALUTATION.match(line) or _SIGNATURE_LINE.search(line):
            continue
        kept.append(line)
    return kept


def _sentence_with(body: str, phrase: str) -> str:
    """The sentence the phrase sits in, so the evidence reads like English.

    The phrase is looked for in the whole body - a decision can be written
    under a sign-off - but when nothing matched, the line quoted comes from
    the message rather than from the signature.
    """
    for part in re.split(r"(?<=[.!?])\s+|\n+", body or ""):
        part = part.strip()
        if not part or _SALUTATION.match(part):
            continue
        if phrase and phrase in part.lower():
            return part if len(part) <= EVIDENCE_LEN else \
                part[:EVIDENCE_LEN - 1] + "\u2026"
    parts = _message_lines(body)
    if parts:
        return max(parts, key=len)[:EVIDENCE_LEN]
    return ""


def _match(mail: ThreadMail, phrases) -> str:
    """The phrase (or the text a pattern matched) found in the message.

    Only the message itself is read - the quoted mail below a reply is cut
    off first - so 'thank you for your rates' quoted back inside our own
    reminder is not read as the agent saying it again.
    """
    return _match_text(mail.body, phrases)


def _match_text(body: str, phrases) -> str:
    text = " {} ".format((body or "").lower())
    for phrase in phrases:
        if isinstance(phrase, re.Pattern):
            found = phrase.search(text)
            if found:
                return found.group(0).strip()
        elif phrase in text:
            return phrase
    return ""


#: T5b: Outlook's recall notice - the subject's recall prefix (parsing's
#: RECALL, CANCELAR) or the notice's own words in the languages the
#: mailbox sees. It is evidence of nothing ('deseja cancelar a mensagem'
#: is not a cancelled shipment).
_RECALL_SUBJECT = re.compile(r"^[\s　]*(RECALL|CANCELAR)[\s　]*[:：]", re.I)
_RECALL_WORDS = re.compile(
    r"would like to recall the message|deseja cancelar a mensagem|gostaria de cancelar a mensagem"
    r"|desea recuperar el mensaje", re.I)


def is_recall(mail: ThreadMail) -> bool:
    return bool(_RECALL_SUBJECT.match(mail.subject or "") or _RECALL_WORDS.search(mail.body or ""))


def _is_placeholder(requester_key: str) -> bool:
    return (requester_key or "").strip().casefold() in (
        intake.PLACEHOLDER_SEARCHING.casefold(),
        intake.PLACEHOLDER_ABANDONED.casefold())


#: The same words inside a request for a quote: 'kindly quote as
#: below', 'please send your ocean rates per the details below'.
_ASKING_FOR_A_QUOTE = re.compile(
    r"\b(please|pls|kindly)\b[^.\n]{0,12}\b(quote|provide|send|advise|share|"
    r"check|confirm|give|forward|offer)\b"
    r"|\byour (best |updated |revised |all-in |lowest )?(rates?|quot|costs?|charges|pric)"
    # FX6: asking a vendor for help with it: 'Would you please assist for
    # this quote below?' (OPP-1702)
    r"|\bassist (us |me )?(with|for|on) (this|the|our) quote"
    r"|\bhelp (us |me )?with (this|the) quote")


def went_out(mail, requester_known: bool) -> bool:
    """FX6: a mail of ours that went to the requester (To or Cc; an office
    requester by its code) - with no domain on file for the requester, to
    anybody outside our own domains. A forward of a vendor's cost among
    ourselves never did (OPP-1722's 'Seavio cost attached')."""
    return mail.internal and (mail.to_requester if requester_known else mail.to_outside)


def _our_quote(ordered, requester_known: bool = False) -> tuple:
    """(mail, phrase) of our newest quote going out, or (None, '').

    A quote is told from a request for one by the sentence it sits in:
    'Please see below rates for your reference' is ours going out,
    'Please kindly provide your rates as per below' is us asking a vendor.
    FX6: only a mail that went to the requester (went_out).
    """
    for mail in reversed(ordered):
        if not went_out(mail, requester_known):
            continue
        phrase = quote_words(mail.body)
        if phrase:
            return mail, phrase
    return None, ""


def quote_words(body: str) -> str:
    """The quote phrase our words open with ('please see below rates'), or
    '' when there is none or its sentence asks somebody for a quote (RQ1:
    _our_quote, the revised-quote test and a note suggestion's default
    read it the same way)."""
    phrase = _match_text(body, QUOTE_PHRASES)
    if not phrase or _ASKING_FOR_A_QUOTE.search(_sentence_with(body, phrase).lower()):
        return ""
    return phrase


def _cite(proposal, mail) -> None:
    """SG1: the proposal names the mail it was read from."""
    proposal.message_id = getattr(mail, "message_id", "") or ""
    proposal.entry_id = getattr(mail, "entry_id", "") or ""


def _quoted(proposal, mail, phrase, confidence, note) -> Proposal:
    proposal.proposed_status = "Quoted"
    proposal.proposed_date = mail.at
    proposal.evidence_sender = "{} <{}>".format(
        mail.sender_name, mail.address or ("internal" if mail.internal else "?"))
    proposal.evidence_date = mail.at
    _cite(proposal, mail)
    proposal.evidence_quote = _sentence_with(mail.body, phrase)
    proposal.confidence = confidence
    proposal.note = note
    return proposal


def classify(mails: list, requester_key: str,
             requester_known: bool = False) -> Proposal:
    """Read the thread newest first and take the first thing it decides.

    The common shape is that the last mail is the agent saying
    yes - but a won case often carries on with pickup and paperwork for days
    afterwards, so the whole thread is read rather than only its last mail.
    The date is always the mail the decision was read from, never today.

    A case parked on '(TBC)' or 'Unknown' has no requester yet: the outside
    mail on its thread is vendors answering our rate requests, and nothing
    a vendor says wins or loses the case. With the requester known by
    domain (`requester_known`), the same goes for anybody else on the
    thread - a vendor's 'no longer works here', a shipper's 'please book'.
    Only a requester with no domain on file leaves strangers a low-
    confidence say.
    """
    proposal = Proposal(opp_no="", requester=requester_key,
                        current_status="", mails_seen=len(mails))
    if not mails:
        proposal.note = "no thread found in Outlook"
        return proposal

    # T5b: a recall notice says nothing about the case
    ordered = sorted((m for m in mails if not is_recall(m)), key=lambda m: (m.at, m.sender_name))
    proposal.mails = ordered[-LAST_MAILS:]
    last_external = next((m for m in reversed(ordered) if m.external), None)
    placeholder = _is_placeholder(requester_key)

    for mail in reversed(ordered):
        if mail.internal or mail.other_office or placeholder:     # T6: an office answering us decides nothing
            continue
        if requester_known and not mail.from_requester:
            continue
        for phrases, status, reason in (
                (WON_PHRASES, "Won", ""),
                # The reasons are the lists as Settings spells them.
                # The customer arranging it themselves is Lost and is
                # read before any cancel phrase; a shipment that does not go
                # ahead at all is Cancelled (C1, 2026-10-02).
                (LOST_PRICE_PHRASES, "Lost", "Price too high"),
                (LOST_OTHER_PHRASES, "Lost", "Went with another forwarder"),
                (OWN_ARRANGEMENT_PHRASES, "Lost", "Went with another forwarder"),
                (CANCELLED_PHRASES, "Cancelled", "Shipment cancelled"),
                (NOT_CONFIRMED_PHRASES, "Cancelled", "Order not confirmed")):
            phrase = _match(mail, phrases)
            if not phrase:
                continue
            if reason == "Price too high":
                # 'Out of budget, please quote general cargo instead' and
                # a fresh quote from us the same day: it is quoted again.
                quote, quote_phrase = _our_quote(ordered, requester_known)
                if quote is not None and quote.at >= mail.at:
                    return _quoted(proposal, quote, quote_phrase, "medium",
                                   "re-quoted after '{}' on {}".format(
                                       phrase, mail.at))
            proposal.proposed_status = status
            proposal.closed_reason = reason
            proposal.proposed_date = mail.at
            proposal.evidence_sender = "{} <{}>".format(
                mail.sender_name, mail.address or "?")
            proposal.evidence_date = mail.at
            _cite(proposal, mail)
            proposal.evidence_quote = _sentence_with(mail.body, phrase)
            proposal.confidence = _confidence(mail, ordered, last_external)
            return proposal

    if placeholder:
        proposal.note = "requester not known yet - vendor mail decides nothing"
        return proposal

    # The customer's yes may never have reached this mailbox; our own
    # booking of the job did.
    for mail in reversed(ordered):
        if not mail.internal:
            continue
        phrase = _match(mail, BOOKING_PHRASES) or (
            "new booking" if "new booking" in (mail.subject or "").lower() else "")
        if phrase:
            proposal.proposed_status = "Won"
            proposal.proposed_date = mail.at
            proposal.evidence_sender = "{} <{}>".format(
                mail.sender_name, mail.address or "internal")
            proposal.evidence_date = mail.at
            _cite(proposal, mail)
            proposal.evidence_quote = _sentence_with(mail.body, phrase) \
                if phrase in (mail.body or "").lower() else (mail.subject or "")[:EVIDENCE_LEN]
            proposal.confidence = "medium"
            proposal.note = "our own booking mail; the customer's yes was not found"
            return proposal

    # (The 'they asked for the rate again' proposal was retired on
    # 2026-09-25: that mail is R3's Replied step. A revised quote is read
    # from our own mail instead - detect_followups.requoted_mails.)

    # Nothing decided. Did we quote? A quote that went out is a quote,
    # whatever the customer has or has not said since.
    quote, phrase = _our_quote(ordered, requester_known)
    if quote is not None:
        return _quoted(proposal, quote, phrase, "medium",
                       "we quoted and nothing came back")

    # The quote itself may never have reached this mailbox; the agent
    # thanking us for it, or us chasing them about it, says it went out.
    for mail in reversed(ordered):
        if mail.external:
            phrase = _match(mail, QUOTE_ACK_PHRASES)
            if phrase:
                return _quoted(proposal, mail, phrase,
                               "high" if mail.from_requester else "medium",
                               "the agent acknowledged our quote")
    for mail in reversed(ordered):
        if went_out(mail, requester_known):             # FX6: a chase sent to a colleague is not one
            phrase = _match(mail, QUOTE_FOLLOW_UP_PHRASES)
            if phrase:
                return _quoted(proposal, mail, phrase, "medium",
                               "our follow-up after a quote; the quote "
                               "itself was not found - date is the follow-up")

    if last_external is None:
        # Only us on the thread. Waiting on the customer means we asked
        # them something; a thread of our own sourcing mail (Victor
        # asking the SIN office for destination charges, NGB sending him
        # the local charges) says nothing about the case.
        if not requester_known:
            proposal.note = ("only our own mail on the thread; the requester "
                             "has no domain on file, so whom we asked cannot "
                             "be told")
            return proposal
        asked = next((m for m in reversed(ordered)
                      if m.internal and m.to_requester and _match(m, ASKING_PHRASES)), None)
        if asked is None:
            proposal.note = ("only our own mail on the thread, and none of "
                             "it asks the customer for anything")
            return proposal
        latest = ordered[-1]
        proposal.proposed_status = "New"
        proposal.proposed_date = ""
        proposal.evidence_sender = "{} <{}>".format(
            asked.sender_name, asked.address or "internal")
        proposal.evidence_date = asked.at
        _cite(proposal, asked)
        proposal.evidence_quote = _sentence_with(
            asked.body, _match(asked, ASKING_PHRASES))
        proposal.confidence = "medium"
        proposal.note = "waiting on customer"
        return proposal

    proposal.note = "thread found, nothing in it decides the case"
    return proposal


#: What would argue with a decision already read further up the thread.
_CONTRADICTIONS = (LOST_PRICE_PHRASES + LOST_OTHER_PHRASES + OWN_ARRANGEMENT_PHRASES
                   + CANCELLED_PHRASES + NOT_CONFIRMED_PHRASES)


def _confidence(mail, ordered, last_external) -> str:
    """How much the sentence is worth.

    The agent who asked for the rate saying 'please proceed' is the strongest
    thing there is. The same words from somebody else on the thread - a
    shipper's automated reminder, say - is worth looking at but not trusting.
    A won case usually carries on for days with pickup and paperwork, so what
    matters is not whether the mail was the last one but whether anything
    after it says otherwise.
    """
    if not mail.from_requester:
        return "low"
    later = ordered[ordered.index(mail) + 1:]
    for other in later:
        if other.external and _match(other, _CONTRADICTIONS):
            return "medium"
    return "high"


def guess_awarded_vendor(mails: list, party_keys, requester_key: str) -> str:
    """A vendor named in our own mails after the decision, if any."""
    text = " ".join(m.body.lower() for m in mails if m.internal)
    if not text:
        return ""
    requester = (requester_key or "").casefold()
    for key in party_keys:
        folded = key.casefold()
        if folded == requester or len(folded) < 4:
            continue
        if re.search(r"\b{}\b".format(re.escape(folded)), text):
            return key
    return ""


# ------------------------------------------------------------------- run --
CSV_COLUMNS = (
    "opp_no", "requester", "current_status", "proposed_status",
    "proposed_date", "closed_reason", "evidence_sender", "evidence_date",
    "evidence_quote", "confidence", "awarded_vendor_guess", "found_by",
    "mails_seen", "note", "current_since",
)


#: The date a case's current status is from: its latest event that counts.
#: A migrated status event is the snapshot taken on registration day, so a
#: migrated case is 'current since' the day it came in.
CURRENT_SINCE_SQL = """
COALESCE((SELECT MAX(s.at) FROM status_event s
          WHERE s.enquiry_id = e.id AND s.voided = 0 AND s.kind IN ('status','requote')),
         e.received_on)"""


def cases(conn: sqlite3.Connection, month=None, opp_nos=(),
          scope="unconfirmed") -> list:
    """The cases to look at.

    'unconfirmed' is a month's cases nobody confirmed; 'open' is every case
    still New or Quoted, confirmed or not, because a case answered
    a week ago can have had mail since.
    """
    select = "SELECT e.*, {} AS current_since FROM v_enquiry e ".format(
        CURRENT_SINCE_SQL)
    if opp_nos:
        marks = ",".join("?" for _ in opp_nos)
        rows = conn.execute(
            select + "WHERE e.opp_no IN ({}) ORDER BY e.opp_no".format(marks),
            tuple(opp_nos)).fetchall()
    elif scope == "open":
        rows = conn.execute(
            select + "WHERE e.status IN ('New', 'Quoted') "
            "AND e.legacy = 0 ORDER BY e.received_on, e.opp_no").fetchall()
    else:
        rows = conn.execute(
            select + "WHERE e.status_confirmed = 0 AND e.legacy = 0 "
            "AND (? IS NULL OR e.month = ?) ORDER BY e.received_on, e.opp_no",
            (month, month)).fetchall()
    return [_with_threads(conn, row) for row in rows]


def _with_threads(conn, row) -> dict:
    """The case as a dict, plus the threads attached to it by hand
    (enquiry_thread): what names the case is the case's own ids
    and those."""
    out = dict(row)
    values = db.thread_values(conn, row["id"])
    out["extra_conversations"] = sorted(values["conversation"] - {row["conversation_id"]})
    out["extra_thread_keys"] = sorted(values["thread_key"] - {row["thread_key"]})
    out["extra_subjects"] = [] if not db._has_table(conn, "enquiry_thread") else [
        r["note"] for r in conn.execute(
            "SELECT note FROM enquiry_thread WHERE enquiry_id = ? AND voided = 0 "
            "AND note IS NOT NULL AND source <> 'register' ORDER BY id", (row["id"],))]
    return out


def _identities(row) -> tuple:
    """(conversation ids, thread keys) that name the case: its own, and the
    attached ones when the row came through cases()."""
    conversations, keys = set(), set()
    if "conversation_id" in row.keys() and row["conversation_id"]:
        conversations.add(row["conversation_id"])
    if "thread_key" in row.keys() and row["thread_key"]:
        keys.add(row["thread_key"])
    if isinstance(row, dict):
        conversations.update(row.get("extra_conversations") or ())
        keys.update(row.get("extra_thread_keys") or ())
    return conversations, keys


#: Second-level labels that make a two-label "domain" a public suffix
#: (`com.ar`, `co.uk`) rather than anybody's mail domain.
_GENERIC_LABELS = {"com", "co", "net", "org", "gov", "edu", "ac", "mil",
                   "or", "ne", "go", "biz", "info"}


def is_public_suffix(domain: str) -> bool:
    """`com.ar` matches every Argentine address; it identifies nobody."""
    labels = (domain or "").lower().strip().split(".")
    if len(labels) < 2:
        return True
    return len(labels) == 2 and labels[0] in _GENERIC_LABELS


def requester_domains_of(conn, party_id) -> set:
    """party_domain plus the domains of the party's contacts' addresses.

    Free-mail domains and public suffixes are left out: a seed row that
    read `pampa-demo.com.ar/com.ar` gave one party the "domain" `com.ar`,
    which would have pulled in every Argentine mail of the month.
    """
    domains = {r["domain"].lower() for r in conn.execute(
        "SELECT domain FROM party_domain WHERE party_id = ?", (party_id,))}
    for r in conn.execute(
            "SELECT email FROM contact WHERE party_id = ? AND email LIKE '%@%'",
            (party_id,)):
        domains.add(r["email"].rsplit("@", 1)[1].strip().lower())
    return {d for d in domains
            if d and d not in matching.FREE_MAIL_DOMAINS
            and not is_public_suffix(d)}


def propose(conn, search, month=None, opp_nos=(), progress=None,
            scope="unconfirmed", after_current=False) -> list:
    """One proposal per case. Reads Outlook and the database, writes neither.

    `after_current` reads only the mail dated on or after the case's current
    status: what has happened since the last answer, not the whole history.
    """
    rows = cases(conn, month=month, opp_nos=opp_nos, scope=scope)
    df = dedupe.document_frequency(conn)
    requester_df = requester_frequency(conn)
    subject_df = subject_frequency(conn)
    internal_domains = {str(d).strip().lower() for d in
                        db.get_setting_list(conn, "internal_domains")}
    party_keys = [r["key"] for r in conn.execute(
        "SELECT key FROM party ORDER BY LENGTH(key) DESC")]

    # One Outlook search per requester: the dates asked for span every
    # case of theirs in this run; each case then keeps its own window.
    spans = {}
    for row in rows:
        if not row["received_on"]:
            continue
        start, end = case_window(row["received_on"])
        party = row["requester_party_id"]
        low, high = spans.get(party, (start, end))
        spans[party] = (min(low, start), max(high, end))

    out = []
    for row in rows:
        requester_domains = requester_domains_of(conn, row["requester_party_id"])
        found, how = find_thread(search, conn, row, df,
                                 requester_df=requester_df,
                                 requester_domains=requester_domains,
                                 span=spans.get(row["requester_party_id"]),
                                 subject_df=subject_df)
        current_since = row["current_since"] if "current_since" in row.keys() \
            else row["received_on"]
        if after_current and current_since:
            found = [m for m in found if (m.received_on or "") >= current_since]
            if not found and how:
                how = "{}; nothing since {}".format(how, current_since)
        office = office_prefix(row["requester_key"], conn)
        # AL1: our acknowledgement carrying the case number is not a quote
        from . import case_numbers
        ack = case_numbers.ack_mail(conn, row, found, search, internal_domains or None) if found else None
        if ack is not None:
            found = [m for m in found if m is not ack]
        mails = [to_thread_mail(m, internal_domains, requester_domains, office, handled_by_of(row))
                 for m in found]
        proposal = classify(mails, row["requester_key"],
                            requester_known=bool(requester_domains or office))
        proposal.opp_no = row["opp_no"]
        proposal.requester = row["requester_key"]
        proposal.current_status = row["status"]
        proposal.current_since = current_since or ""
        if not mails and how:
            # 'too many to be this case' is the reason, not the route.
            proposal.note = how
            how = ""
        proposal.found_by = how
        proposal.awarded_vendor = guess_awarded_vendor(
            proposal.mails, party_keys, row["requester_key"])
        out.append(proposal)
        if progress:
            progress(proposal)
    return out


def write_csv(proposals, path) -> Path:
    """Write the proposals, going somewhere else rather than losing them.

    The obvious way to read one of these files is to open it in Excel, and
    Excel holds the file open. Reading the mailbox took minutes, so a locked
    file becomes a second file with the time in its name, not an error.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _write_csv_to(proposals, path)
    except OSError:
        beside = path.with_name("{}-{}{}".format(
            path.stem, dt.datetime.now().strftime("%H%M%S"), path.suffix))
        return _write_csv_to(proposals, beside)


def _write_csv_to(proposals, path) -> Path:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        writer.writerow(CSV_COLUMNS)
        for p in proposals:
            writer.writerow([
                p.opp_no, p.requester, p.current_status, p.proposed_status,
                p.proposed_date, p.closed_reason, p.evidence_sender,
                p.evidence_date, p.evidence_quote, p.confidence,
                p.awarded_vendor, p.found_by, p.mails_seen, p.note,
                p.current_since,
            ])
    return path


def render(proposals, limit=None) -> list:
    """What the command prints: one line per case, then a tally."""
    lines = ["  {:<10} {:<18} {:<9} {:<8} {:<7} {}".format(
        "case", "requester", "proposed", "date", "conf", "evidence")]
    for p in proposals[:limit]:
        lines.append("  {:<10} {:<18} {:<9} {:<8} {:<7} {}".format(
            p.opp_no, (p.requester or "")[:18], p.proposed_status,
            p.proposed_date or "-", p.confidence,
            (p.evidence_quote or p.note or "")[:60]))
    if limit and len(proposals) > limit:
        lines.append("  ... and {} more".format(len(proposals) - limit))
    counts = Counter(p.proposed_status for p in proposals)
    lines.extend(["", "  " + "   ".join(
        "{} {}".format(status, counts[status])
        for status in sorted(counts))])
    verdicts = Counter(p.verdict for p in proposals)
    lines.append("  suggests a change {}   keeps the status {}   "
                 "no thread found {}   thread found but undecided {}".format(
                     verdicts["change"], verdicts["keep"],
                     verdicts["no thread"], verdicts["undecided"]))
    return lines


# ---------------------------------------------------------------- apply --
def read_csv(path) -> list:
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def apply(repo, rows, today=None) -> tuple:
    """Write the rows that are still in the file. Returns (applied, skipped).

    Deleting a row is how a proposal is refused, so anything still here is
    taken as agreed. Every one becomes a status_event with source 'scan' and
    the date the mail carried, never today's.
    """
    from . import status as status_mod

    applied, skipped = [], []
    for row in rows:
        opp_no = (row.get("opp_no") or "").strip()
        status = (row.get("proposed_status") or "").strip()
        at = (row.get("proposed_date") or "").strip()
        if status in status_mod.RETIRED_STATUSES:
            # a file written before Sourcing was retired: the case is New
            status = "New"
        if status not in status_mod.ALL_STATUSES:
            skipped.append((opp_no, "proposed status is {!r}".format(status)))
            continue
        case = repo.conn.execute(
            "SELECT id, status, status_confirmed FROM enquiry "
            "WHERE opp_no = ?", (opp_no,)).fetchone()
        if case is None:
            skipped.append((opp_no, "no such case"))
            continue
        # The file is a snapshot of a mailbox read minutes or days ago, and
        # she may have answered the same case by hand in the meantime. Her
        # answer wins: a proposal never overwrites a status somebody set.
        was = (row.get("current_status") or "").strip()
        if case["status_confirmed"] and was and case["status"] != was:
            skipped.append((opp_no, "set to {} by hand since this file was "
                                    "made".format(case["status"])))
            continue
        if not at:
            skipped.append((opp_no, "no date on the row"))
            continue
        reason = (row.get("closed_reason") or "").strip() or None
        # 'Other' is a Lost and a Cancelled reason. A Dropped case with no
        # reason stays without one rather than a made-up one.
        if status in ("Lost", "Cancelled") and not reason:
            reason = "Other"
        note = " ".join(part for part in (
            (row.get("evidence_sender") or "").strip(),
            (row.get("evidence_quote") or "").strip()) if part)
        if (row.get("note") or "").startswith(REQUOTE_NOTE):
            # the retired 'they asked for the rate again' proposal (2026-09-25)
            skipped.append((opp_no, "the 'they asked for the rate again' proposal is retired"))
            continue
        if status in ("Quoted", "New") and is_requote_note(row.get("note")):
            repo.requote(case["id"], at, note=note[:380] or None, source="scan")
        else:
            repo.set_status(case["id"], status, at,
                            note=note[:400] or None,
                            closed_reason=reason if status in ("Lost", "Cancelled", "Dropped")
                            else None,
                            source="scan")
        if repo.last_status_outcome in ("confirmed", "repeat"):
            # the same answer already on the case that day (13g): nothing written
            skipped.append((opp_no, "already {} on {}; nothing new was recorded".format(status, at)))
            continue
        applied.append((opp_no, status, at))
    return applied, skipped
