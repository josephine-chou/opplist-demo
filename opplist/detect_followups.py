"""Follow-ups we sent, and replies the requester sent, read out of the mail
archive (second batch, 2026-09-16).

For every quoted case still in scope the thread is looked up in the archive
(conversation id, else the subject's thread key against the conversation
topic). Two things are written straight to the timeline - not through
Suggested updates, because neither is a judgement:

- a mail of ours after the quote whose own words chase the quote ('just
  checking back', 'any feedback', ...) and that carries no rate is a
  follow-up: a `followup` event dated the day it was sent, channel mail,
  source mail, the Message-ID kept in the note so the next run skips it.
- a mail from the requester's domain after our last outgoing mail is a
  reply: a `note` event (source mail) with the first words of it, so the
  R3 ladder's clock restarts and nobody is chased the day after they
  answered. Whether the reply says Won or Lost is propose-status's job and
  goes through Suggested updates as before.

WR1 (2026-11-06): a New case waiting for the requester's details (wait.py)
is read too, from the wait note: our chasers to the requester after it are
follow-ups, their mails after it are replies (which end the wait). Other
New cases are not read.

FX7 (2026-11-23, v58): a follow-up is no longer written straight to the
timeline. Where the scan found one (after the quote, or in a wait) it
writes a pending suggestion, kind 'followup' (the mail's Message-ID and
entry id, the day it was sent, the sender, its first words, and whether it
is on the quote or on our question); Accept writes the event the scan used
to write. A dismissed or expired one counts as known, as a struck-out
detection does. A follow-up logged by hand or on the team site that day
still wins: no suggestion, and a pending one is closed (suggestions.
close_logged_followups). The replies stay automatic.

SG1 (2026-11-04): one mail shows once. A mail with a pending status
suggestion gets no automatic note until that suggestion is decided:
accepted, its event stands for the mail; dismissed or expired, the next
run writes the note, dated as the mail.

Runs inside `propose-status --open --write-suggestions` (the daily job) and
on its own as `python -m opplist detect-followups [--dry-run]`.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from . import branding, db, intake, mail_archive, matching, parsing, propose

#: Our own words when we chase a quote.
_FU_PHRASES = re.compile(
    r"\b(?:checking\s+back|following\s+up|follow(?:ing)?[\s-]+up|how\s+the\s+rates?\s+look|"
    r"any\s+update|any\s+feedback|workable|look(?:ing)?\s+forward\s+to\s+your\s+(?:feedback|approval))\b",
    re.I)
NOTE_LEN = 120
SUBJECT_LEN = 60
_MID = re.compile(r"\[mid:([^\]]+)\]")

#: A reply that asks us something, or pushes on the price: it is our move
#: again and the case goes on the list tomorrow (2026-09-17).
_REPLY_ASKS = re.compile(
    r"\?"
    # T5b: 'also' may stand between ('please also check', 'kindly also
    # confirm')
    r"|\b(please\s+(also\s+)?(advise|confirm|quote|send|share|check)|kindly\s+(also\s+)?(advise|confirm|check))\b"
    # T5b: a request for us to act asks as a question does ('Yes, please
    # proceed with the Friday uplift'); a bare 'proceed with' is their own next step
    r"|\b(please|kindly)\s+(also\s+)?(proceed|book|arrange|go\s+ahead)\b"
    r"|\b(can|could|would|will)\s+(you|we)\b"
    r"|\b(what|which|how\s+much|how\s+many|how\s+long|when\s+can|where\s+can|any\s+chance)\b"
    # 'competitor' is them pushing back; 'competitive' on its own is the
    # agent telling us what they will quote their own client (OPP-1421,
    # 2026-09-16), so it is deliberately not here
    r"|\b(competitor|cheaper|cheapest|too\s+high|target\s+rate|target\s+price|"
    r"better\s+rate|better\s+price|reduce|reduction|discount|match\s+the)\b"
    # their own selling rate, or a counter-offer to look at: a target we
    # have to answer (2026-09-21). 'please check the below' is already a
    # 'please check' above.
    r"|\b(selling\s+below|selling\s+rate|our\s+selling|refer\s+to\s+(the\s+)?below)\b"
    # AL6a: more ways of asking - 'please also provide', 'we would like to
    # obtain', 'we need your best rate'. Not 'let us know': 'let us know if
    # you need anything' is a courtesy.
    r"|\b(please|kindly)\s+(also\s+)?(provide|review|clarify|update|revise)\b"
    # AL6: 'also' before or after 'like to', and I'd as well as we'd
    r"|\b(would|we['\u2019]?d|i['\u2019]?d)\s+(also\s+)?like\s+to\s+(also\s+)?"
    r"(obtain|get|have|receive|know|check|request)\b"
    r"|\bneed\s+your\s+(help|support|assistance|best\s+rates?|rates?|quote|quotation|confirmation)\b",
    re.I)
#: AL6a: a link is never a question - a '?' in a URL, a signature's
#: mailto - so links are taken out before the words are read. T5b: a link
#: in angle brackets with any scheme (<blob:...>, <cid:...>, <tel:...>,
#: <file:...>) and Outlook's image placeholder [cid:...] as well.
_ANY_LINK = r"<\s*[a-z][a-z0-9+.\-]*:[^<>\s]*>|\[cid:[^\]]*\]"
_LINKS = re.compile(r"<[^<>]*(?:https?:|mailto:|www\.)[^<>]*>|" + _ANY_LINK +
                    r"|\bhttps?://\S+|\bwww\.\S+|\bmailto:\S+", re.I)
#: How a reply that needs an answer, and one that does not, are marked in
#: the note. The mark is for the reader; the rule below reads the words.
MARK_ASKS = "needs an answer"
MARK_NOTED = "noted"
#: FX4: the kind every reply the scan writes carries
MARK_NOTED_KIND = "noted"
#: FX4: who wrote a note decides whether it can ask us something - the
#: scan's (source mail) never does, old ones included
SCAN_SOURCES = ("mail", "scan")
_REPLY_PREFIX = re.compile(r"^Reply from (?P<who>[^:(]*?)\s*(?:\((?P<mark>[^)]*)\))?\s*:", re.I)


def reply_kind(text: str) -> str:
    """'asks' when the requester's words put the ball back with us (a
    question, 'please advise', a push on the price), 'noted' when they are
    an acknowledgement ('thanks', 'will revert', 'I will check with my
    client'). A thank-you is activity, not a task."""
    return "asks" if _REPLY_ASKS.search(_LINKS.sub(" ", text or "")) else "noted"


#: T5: the links taken out of the words a note or the draft warning shows -
#: '<http...>' or '<mailto:...>' in angle brackets (a security
#: scanner's rewrite, a signature) and a bare http(s)://...; T5b: any
#: scheme in angle brackets, and [cid:...]
_SHOWN_LINKS = re.compile(r"<\s*(?:https?:|mailto:)[^<>]*>|" + _ANY_LINK + r"|\bhttps?://\S+", re.I)


def without_links(text: str) -> str:
    """The words with their links taken out and the spaces closed up."""
    return " ".join(_SHOWN_LINKS.sub(" ", text or "").split())


def note_kind(note: str) -> str:
    """The kind of a 'Reply from ...' note: the mark it carries, or the
    words themselves when it was written before the marks existed."""
    found = _REPLY_PREFIX.match((note or "").strip())
    if found is None:
        return ""
    mark = (found.group("mark") or "").strip().casefold()
    if mark == MARK_ASKS:
        return "asks"
    if mark == MARK_NOTED:
        return "noted"
    return reply_kind(note)


def asks_us(note: str, source: str) -> bool:
    """FX4: a note that puts the case on Pending on us - a 'Reply from'
    note that asks, logged by hand or on the team site (never one the
    scan wrote)."""
    return (source or "") not in SCAN_SOURCES and note_kind(note) == "asks"


def reply_note(sender: str, top: str, mid: str = "", kind: str = "") -> str:
    """The note a detected reply leaves on the timeline."""
    kind = kind or reply_kind(top)
    return "Reply from {} ({}): {}{}".format(
        (sender or "?").strip(), MARK_ASKS if kind == "asks" else MARK_NOTED,
        without_links(top)[:NOTE_LEN], " [mid:{}]".format(mid) if mid else "")


@dataclass
class Report:
    cases: int = 0
    waits: int = 0                      # WR1: New cases waiting for the requester
    threads: int = 0
    followups: int = 0
    replies: int = 0
    skipped: int = 0
    closed: int = 0                     # FX7: pending follow-up suggestions a hand-logged one closed
    held: int = 0                       # SG1: a pending status suggestion has the mail
    dry_run: bool = False
    lines: list = field(default_factory=list)
    #: AL1: the case-number scan's findings (case_numbers.ScanReport)
    case_numbers: object = None

    def summary(self) -> list:
        out = [
            "quoted cases looked at : {}".format(self.cases),
            "waiting New cases looked at : {}".format(self.waits),
            "  threads found        : {}".format(self.threads),
            "follow-ups {}  : {}".format("found    " if self.dry_run else "suggested", self.followups),
            "replies {}      : {}".format("found   " if self.dry_run else "recorded", self.replies),
            "skipped (already on the case, or logged by hand that day) : {}".format(self.skipped),
            "held back (a status suggestion on the same mail is pending) : {}".format(self.held),
        ]
        if self.closed:
            out.append("follow-up suggestions closed (logged by hand or on the team site that day) : {}".format(
                self.closed))
        if self.case_numbers is not None:
            out.extend(self.case_numbers.lines()[:40])
        if self.dry_run:
            out.append("dry run: nothing was written")
        out.extend("  " + line for line in self.lines[:60])
        if len(self.lines) > 60:
            out.append("  ... and {} more".format(len(self.lines) - 60))
        return out


def is_followup_text(subject: str, top: str) -> bool:
    """Our words chase the quote, and give no rate."""
    top = top or ""
    if not _FU_PHRASES.search(top):
        return False
    if intake._CHARGES_AMOUNT.search(top) or intake._GIVE_PHRASE.search(top):
        return False
    return True


def mid_of(mail) -> str:
    return (getattr(mail, "message_id", "") or "").strip()


def recorded_mids(conn, enquiry_id) -> set:
    """Message-IDs already on the case's timeline (live or voided: a voided
    detection stays voided)."""
    out = set()
    for row in conn.execute("SELECT note FROM status_event WHERE enquiry_id = ? AND note LIKE '%[mid:%'",
                            (enquiry_id,)):
        out.update(m.strip() for m in _MID.findall(row["note"] or ""))
    return out


def suggested_mails(conn, enquiry_id) -> tuple:
    """SG1 item 3b: (pending, accepted) - the Message-IDs and entry ids of
    the case's status suggestions of either source in those states. A
    pending one holds the mail's automatic note back; an accepted one's
    event stands for the mail. A dismissed (or expired) one holds nothing."""
    pending, accepted = set(), set()
    for row in conn.execute("SELECT state, message_id, entry_id FROM suggestion WHERE enquiry_id = ? "
                            "AND kind = 'status' AND state IN ('pending', 'accepted')", (enquiry_id,)):
        ids = {v.strip() for v in (row["message_id"], row["entry_id"]) if v and v.strip()}
        (pending if row["state"] == "pending" else accepted).update(ids)
    return pending, accepted


def _taken(mail, ids) -> bool:
    return bool(ids) and bool({mid_of(mail), (getattr(mail, "entry_id", "") or "").strip()} & ids)


def _sent_on(mail) -> str:
    return ((getattr(mail, "sent_at", "") or mail.received_at or mail.received_on or "")[:10])


def _sent_stamp(mail) -> str:
    return (getattr(mail, "sent_at", "") or mail.received_at or mail.received_on or "")[:16]


def thread_of(search, row, conn=None) -> list:
    """The case's mails, oldest first, by the Mail card's union rule
    (case_mail.mails_for): the registered mail, every
    conversation of the case, every mail whose subject key is the case's
    or an attached one (with `conn`), and the plain subject only when all
    of that is empty."""
    from . import case_mail

    if conn is not None:
        found = case_mail.case_mails(conn, search, row)
    else:
        wanted = row["thread_key"] or parsing.thread_key(parsing.clean_subject(row["source_subject"] or ""))
        found = case_mail.mails_for(
            search, row["entry_id"], [row["conversation_id"]] if row["conversation_id"] else [],
            {wanted} if wanted else set(), [row["source_subject"] or ""])
    found = list(found)
    found.sort(key=_sent_stamp)
    return found


def requester_domains(conn, search, row) -> set:
    """party_domain, the contacts' domains, and the registering mail's
    sender domain."""
    domains = set(propose.requester_domains_of(conn, row["requester_party_id"]))
    if row["entry_id"]:
        registered = search.by_entry_id(row["entry_id"])
        if registered is not None and not registered.sender_internal:
            domain = matching.domain_of(registered.sender_address)
            if domain and not matching.is_free_mail(domain):
                domains.add(domain)
    return domains


def _sides(row, internal, domains):
    """(ours, theirs) for a case's thread. When the requester is a JoJo
    office ('JoJo JJZ'), its people write from jojofreight.example like we do;
    a sender named with that office's code ('JJZ-WENDY FANG') is the
    requester, never our follow-up (2026-09-30, OPP-1450). T6: the reading
    is propose.office_side's, so another office on our domain (not JJC,
    not handled_by) is a vendor answering us - neither ours nor theirs."""
    office = propose.office_prefix(row["requester_key"] or "") if "requester_key" in row.keys() else ""
    handled = propose.handled_by_of(row)

    def side(mail):
        name = mail.sender_name or ""
        if office and name.strip().upper().startswith(office):
            return propose.SIDE_REQUESTER
        domain = matching.domain_of(mail.sender_address)
        if mail.sender_internal or (domain and domain in internal):
            return propose.office_side(name, office, handled)
        return propose.SIDE_REQUESTER if domain and domain in domains else ""

    def ours(mail):
        return side(mail) == propose.SIDE_OURS

    def theirs(mail):
        return side(mail) == propose.SIDE_REQUESTER

    return ours, theirs


def logged_that_day(conn, enquiry_id, day) -> str:
    """Why the mail's follow-up is not needed: logged by hand that day, or
    on the team site within a day (W1b: the colleague's record wins, the
    mail's is already on the case); '' when neither."""
    from . import firebase_pull
    day = (day or "")[:10]
    if conn.execute(
            "SELECT 1 FROM status_event WHERE enquiry_id = ? AND kind = 'followup' AND voided = 0 "
            "AND substr(at, 1, 10) = ? AND source = 'manual'", (enquiry_id, day)).fetchone() is not None:
        return "a follow-up was logged by hand that day"
    if firebase_pull.logged_on_site(conn, enquiry_id, day, "followup"):
        return "a follow-up was logged on the team site that day"
    return ""


def _manual_followup_that_day(conn, enquiry_id, day) -> bool:
    return bool(logged_that_day(conn, enquiry_id, day))


#: FX7: what a follow-up suggestion is on (suggestion.proposed_status)
ON_QUOTE = "quote"
ON_QUESTION = "question"
FIRST_WORDS = 160


def followup_suggested(conn, enquiry_id, mail) -> bool:
    """FX7: a follow-up suggestion of this mail exists on the case, in any
    state - pending (waiting for her), accepted (its event stands),
    dismissed or expired (known: never suggested again)."""
    for value in {v for v in (mid_of(mail), (getattr(mail, "entry_id", "") or "").strip()) if v}:
        if conn.execute("SELECT 1 FROM suggestion WHERE enquiry_id = ? AND kind = 'followup' "
                        "AND (message_id = ? OR entry_id = ?)", (enquiry_id, value, value)).fetchone() is not None:
            return True
    return False


def first_words(top) -> str:
    return without_links(" ".join((top or "").split()))[:FIRST_WORDS]


def followup_values(mail, on: str, note: str, top: str) -> dict:
    """FX7: the pending suggestion row of one follow-up the scan found: the
    day it was sent, who sent it, its first words, what it is on, and the
    event note Accept writes (the scan's 'FU detected: ...' with [mid:])."""
    stamp = _sent_stamp(mail)
    return {"kind": "followup", "proposed_status": on, "proposed_date": stamp[:10],
            "evidence_sender": "{} <{}>".format(mail.sender_name or "", mail.sender_address or "").strip(),
            "evidence_date": stamp[:16], "evidence_quote": first_words(top), "note": note,
            "confidence": "high", "source": "scan",
            "message_id": mid_of(mail) or None, "entry_id": (getattr(mail, "entry_id", "") or "").strip() or None}


def scan_case(conn, repo, search, row, internal, report: Report, dry_run: bool, waiting=None) -> None:
    """One quoted case: its follow-ups and the requester's replies. Only
    our mails to the requester count (T4, 2026-10-29): a mail we send a
    vendor with the case number in the subject, or one of our offices
    asked as a vendor answering on that thread, is neither a follow-up
    nor the line after which the requester's mails are replies.

    WR1: `waiting` (a wait.Wait) reads a New case waiting for the
    requester from the wait note instead of the quote: a mail of ours to
    them after it that chases (the quote chaser's words or the details
    chaser's) is a follow-up numbered from the wait, their mails after it
    and after our last mail are replies.

    FX4: a reply is always written as noted (it moves the follow-up clock
    and ends a Waiting for requester), and our mails after a question are
    no longer read as its answer: Waiting on us and Answered are logged by
    hand, on the case page or the team site."""
    from . import firebase_pull, wait
    thread = thread_of(search, row, conn)
    if not thread:
        return
    report.threads += 1
    quoted = (row["quoted_on"] or row["quoted_on_any"] or "")[:10]
    wait_seq = len(wait.followups(conn, waiting)) if waiting is not None else 0
    known = recorded_mids(conn, row["id"])
    held, stands = suggested_mails(conn, row["id"])
    domains = requester_domains(conn, search, row)
    ours, theirs = _sides(row, internal, domains)

    def hold(mail, what) -> bool:
        """SG1 item 3b: no automatic note for a mail a status suggestion
        has - held back while it is pending, never once it is accepted."""
        if _taken(mail, stands):
            report.skipped += 1
            return True
        if _taken(mail, held):
            report.held += 1
            report.lines.append("{} {} {} held back: a status suggestion on it is pending".format(
                row["opp_no"], what, _sent_stamp(mail)[:10]))
            return True
        return False

    # 1. our follow-ups after the quote
    last_ours = ""
    for mail in thread:
        if not ours(mail) or not _to_requester(mail, row, domains):
            continue
        stamp = _sent_stamp(mail)
        last_ours = max(last_ours, stamp)
        top = mail_archive.top_part(mail.body_excerpt or "")
        if waiting is not None:
            if stamp <= waiting.start:
                continue
            if not (is_followup_text(mail.subject, top) or wait.CHASER_PHRASES.search(top)):
                continue
        elif not quoted or stamp[:10] <= quoted:
            continue
        elif not is_followup_text(mail.subject, top):
            continue
        mid = mid_of(mail)
        if mid and mid in known:
            report.skipped += 1
            continue
        if followup_suggested(conn, row["id"], mail):
            # FX7: suggested before - waiting for her, accepted, dismissed or expired
            report.skipped += 1
            continue
        if _manual_followup_that_day(conn, row["id"], stamp[:10]):
            # T4b: logged by hand or on the site, still a follow-up
            report.skipped += 1
            continue
        if hold(mail, "FU"):
            continue
        note = "FU detected: {}{}".format((mail.subject or "")[:SUBJECT_LEN],
                                          " [mid:{}]".format(mid) if mid else "")
        report.followups += 1
        if waiting is not None:
            wait_seq += 1
        report.lines.append("{} FU{} {}{} {} (suggested)".format(
            row["opp_no"], wait_seq if waiting is not None else "", "on our question " if waiting is not None else "",
            stamp[:10], (mail.subject or "")[:50]))
        if not dry_run:
            # FX7: a pending suggestion - nothing on the timeline until she accepts it
            values = followup_values(mail, ON_QUESTION if waiting is not None else ON_QUOTE, note, top)
            repo.insert("suggestion", dict(values, enquiry_id=row["id"], opp_no=row["opp_no"], state="pending"))
            if mid:
                known.add(mid)
    # 2. the requester's replies after our last mail to them
    boundary = last_ours or (quoted + " 00:00" if quoted else "")
    if waiting is not None:
        boundary = max(last_ours, waiting.start)
    for mail in thread:
        if not theirs(mail):
            continue
        stamp = _sent_stamp(mail)
        if not boundary or stamp <= boundary:
            continue
        mid = mid_of(mail)
        if mid and mid in known:
            report.skipped += 1
            continue
        if firebase_pull.logged_on_site(conn, row["id"], stamp[:10], "reply", sources=("web", "manual")):
            # W1b: a colleague logged this question on the team site; FX4: or
            # she logged it on the case page (Waiting on us) - the hand-logged
            # one stands, the scan's noted copy would take it off Pending on us
            report.skipped += 1
            continue
        top = " ".join(mail_archive.top_part(mail.body_excerpt or "").split())
        # FX4: always noted - what the scan reads never puts a case on
        # Pending on us (Waiting on us is logged by hand, her rule of 21 Oct)
        note = reply_note(mail.sender_name or mail.sender_address, top, mid, MARK_NOTED_KIND)
        if hold(mail, "reply"):
            continue
        report.replies += 1
        report.lines.append("{} reply {} {}".format(
            row["opp_no"], stamp[:10], (mail.sender_name or mail.sender_address)[:30]))
        if not dry_run:
            repo.add_note(row["id"], stamp[:10], note, source="mail")
            if mid:
                known.add(mid)



#: AL6a: the note an answer to a question leaves on the timeline (FX4: the
#: site's and the case page's Answered; the scan writes none any more)
ANSWER_PREFIX = "Answered by"


def answer_note(sender: str, top: str, mid: str = "") -> str:
    return "{} {}: {}{}".format(ANSWER_PREFIX, (sender or "?").strip(), without_links(top)[:NOTE_LEN],
                                " [mid:{}]".format(mid) if mid else "")


# ------------------------------------------------------ Re-quoted, from our mail --
#: The note a Re-quoted suggestion carries; its 'Re-quote' mark is what
#: suggestions.accept reads to write the button's event (v30).
REQUOTED_NOTE = "Re-quote: we sent revised rates"
EVIDENCE_LEN = 160


@dataclass
class Requoted:
    """One mail of ours that reads as a revised quote to the requester."""
    enquiry_id: int
    opp_no: str
    at: str                       # the mail's sent date: the event's date
    sender: str                   # 'Name <address>'
    quote: str                    # the line with the amount
    subject: str
    message_id: str = ""
    entry_id: str = ""
    last_quoted: str = ""


def _gives_an_amount(subject: str, top: str) -> bool:
    """mail_direction reads the new part as giving a rate, and that part
    carries an amount - a charge with its amount, or a currency amount on
    its own line ('Usd1640', '0.45 X KG MIN USD180', '$1980.00 including
    fuel'), as the give reading recognises it (2026-09-25). A target or
    budget is theirs, not ours."""
    return intake.mail_direction(subject, top) == "quoting" and \
        bool(intake._CHARGES_AMOUNT.search(top) or intake._CURRENCY_AMOUNT.search(top)) \
        and not intake._TARGET_RATE.search(top)


def _amount_line(top: str) -> str:
    """The evidence: the first line with a charge and its amount, else the
    first line with any currency amount."""
    lines = (top or "").splitlines()
    for pattern in (intake._CHARGES_AMOUNT, intake._CURRENCY_AMOUNT):
        for line in lines:
            if pattern.search(line):
                return " ".join(line.split())[:EVIDENCE_LEN]
    return " ".join((top or "").split())[:EVIDENCE_LEN]


def _to_requester(mail, row, domains) -> bool:
    """The To line holds the requester: one of the requester party's
    domains, or - one of our offices asking us - that office by the code in
    the display name, the way intake reads office recipients. A mail
    written by another of our offices (an office asked as a vendor,
    'JJZ-WENDY FANG' from jojofreight.example) is never one (T4)."""
    if _from_other_office(mail, row):
        return False
    to = [matching.domain_of(str(a).lower()) for a in (getattr(mail, "to_addresses", None) or [])]
    if any(d and d in domains for d in to):
        return True
    code = propose.office_prefix(row["requester_key"] or "").rstrip("-")
    return bool(code and code in intake._office_recipients(mail))


def _from_other_office(mail, row) -> bool:
    """Written on our domain by one of our offices that is neither the
    case's requester nor the office handling it (handled_by: its quote to
    the requester is ours, OPP-1347 by YVR): the office code on the
    sender's name, not JJC's own."""
    domain = matching.domain_of((mail.sender_address or "").lower())
    if not (mail.sender_internal or (domain or "").endswith(branding.INTERNAL_DOMAIN)):
        return False
    office = propose.office_prefix(row["requester_key"] or "") if "requester_key" in row.keys() else ""
    return propose.office_side(mail.sender_name or "", office, propose.handled_by_of(row)) \
        == propose.SIDE_OTHER_OFFICE


def _already_evidence(conn, enquiry_id, mail, known_mids) -> bool:
    """The mail is on the timeline already (its Message-ID in an event's
    note), or a suggestion was made of it before (any state)."""
    mid = mid_of(mail)
    if mid and mid in known_mids:
        return True
    entry = getattr(mail, "entry_id", "") or ""
    for column, value in (("message_id", mid), ("entry_id", entry)):
        if value and conn.execute(
                "SELECT 1 FROM suggestion WHERE enquiry_id = ? AND {} = ?".format(column),
                (enquiry_id, value)).fetchone() is not None:
            return True
    return False


def last_quote_on(conn, enquiry_id) -> str:
    """The day of the case's last live Quoted or requote event, '' when none."""
    last = conn.execute(
        "SELECT MAX(at) FROM status_event WHERE enquiry_id = ? AND voided = 0 "
        "AND status = 'Quoted' AND kind IN ('status','requote')", (enquiry_id,)).fetchone()[0]
    return (last or "")[:10]


def revised_quote(mail, row, domains, ours, last) -> str:
    """RQ1, the one test for 'our mail is a revised quote' (requoted_mails
    and suggestions.from_mail): ours (`ours`, _sides' reading), To the
    requester, dated after `last` (last_quote_on), not a chaser (the
    follow-up phrases, even with the old rate repeated), and giving a rate
    - an amount as _gives_an_amount reads it, or a quote phrase whose
    sentence is not us asking a vendor (propose.quote_words). Returns the
    evidence line, '' when it is not one."""
    if not last or not ours(mail):
        return ""
    sent = _sent_on(mail)
    if not sent or sent <= last:
        return ""
    top = mail_archive.top_part(mail.body_excerpt or "")
    if _FU_PHRASES.search(top) or not _to_requester(mail, row, domains):
        return ""
    if _gives_an_amount(mail.subject, top):
        return _amount_line(top)
    body = propose._quote_free(top)
    phrase = propose.quote_words(body)
    return propose._sentence_with(body, phrase)[:EVIDENCE_LEN] if phrase else ""


def revised_quotes(conn, row, mails, domains, ours, seen=None) -> list:
    """The mails (oldest first) that pass revised_quote on a Quoted case,
    one a day, none that already has a suggestion of any kind or state or
    is on the timeline (RQ1 item 3), none the team site logged. `seen`
    collects every mail that passed the test, offered or not."""
    from . import firebase_pull
    if row["status"] != "Quoted":
        return []
    last = last_quote_on(conn, row["id"])
    if not last:
        return []
    known = recorded_mids(conn, row["id"])
    out, taken_days = [], set()
    for mail in sorted(mails, key=_sent_stamp):
        quote = revised_quote(mail, row, domains, ours, last)
        if not quote:
            continue
        if seen is not None:
            seen.append(mail)
        sent = _sent_on(mail)
        # one re-quote a day: a second mail that day (the destination
        # charges after the revised freight, OPP-1318 on 18 Aug) belongs to
        # the same quote, as a mail on the day of the last quote is that
        # quote; a day with a mail already suggested or recorded is done
        if sent in taken_days:
            continue
        taken_days.add(sent)
        if _already_evidence(conn, row["id"], mail, known):
            continue
        if firebase_pull.logged_on_site(conn, row["id"], sent, "quote"):
            # W1b: the colleague logged this quote on the team site
            continue
        out.append(Requoted(
            enquiry_id=row["id"], opp_no=row["opp_no"], at=sent,
            sender="{} <{}>".format(mail.sender_name or "", mail.sender_address or "internal").strip(),
            quote=quote, subject=mail.subject or "",
            message_id=mid_of(mail), entry_id=getattr(mail, "entry_id", "") or "", last_quoted=last))
    return out


def requoted_mails(conn, search, row, internal=None) -> list:
    """For a Quoted case: our mails in its threads that are a revised quote
    (2026-09-25; the test is revised_quote, RQ1), offered once."""
    if row["status"] != "Quoted" or not last_quote_on(conn, row["id"]):
        return []
    internal = internal if internal is not None else internal_domains(conn)
    thread = thread_of(search, row, conn)
    if not thread:
        return []
    domains = requester_domains(conn, search, row)
    ours, _theirs = _sides(row, internal, domains)            # T6: not another office's, not the requester's
    return revised_quotes(conn, row, thread, domains, ours)


def requoted_suggestions(conn, search, opp_nos=()) -> list:
    """requoted_mails() for every Quoted case in scope (or the ones named)."""
    internal = internal_domains(conn)
    sql = "SELECT * FROM v_enquiry WHERE status = 'Quoted' AND scope = 'In' AND legacy = 0"
    args = ()
    if opp_nos:
        sql += " AND opp_no IN ({})".format(",".join("?" for _ in opp_nos))
        args = tuple(o.strip().upper() for o in opp_nos)
    out = []
    for row in conn.execute(sql + " ORDER BY opp_no", args).fetchall():
        out.extend(requoted_mails(conn, search, row, internal))
    return out


def record_requoted(repo, found) -> dict:
    """One pending 'Re-quoted' suggestion per mail, keyed like the others
    (its Message-ID and entry id, and the same case / status / evidence),
    so a dismissed one does not come back. Accept writes the button's
    event: Quoted, kind requote, the mail's date, no reason."""
    from . import suggestions
    counts = {"written": 0, "same": 0}
    for r in found:
        made = suggestions.record(repo, r.enquiry_id, r.opp_no, requoted_values(r, "scan"))
        counts["written" if made else "same"] += 1
    repo.commit()
    return counts


def requoted_values(r, source) -> dict:
    """The suggestion row of one Requoted: the morning scan's ('scan') and
    Update from Outlook's ('mail', RQ1) are the same row."""
    return {"kind": "status", "proposed_status": "Quoted", "proposed_date": r.at,
            "evidence_sender": r.sender, "evidence_date": r.at, "evidence_quote": r.quote,
            "confidence": "medium", "note": REQUOTED_NOTE, "source": source,
            "message_id": r.message_id or None, "entry_id": r.entry_id or None}


def internal_domains(conn) -> set:
    out = {str(d).strip().lower() for d in db.get_setting_list(conn, "internal_domains")
           if str(d).strip()}
    return out or {branding.INTERNAL_DOMAIN}


@dataclass
class ReclassifyReport:
    asks: int = 0
    noted: int = 0
    rewritten: int = 0
    kept: int = 0                       # AL6a: --from-archive could not read them
    dry_run: bool = True
    lines: list = field(default_factory=list)

    def summary(self) -> list:
        out = [
            "'Reply from' notes read : {}".format(self.asks + self.noted),
            "  needs an answer       : {}".format(self.asks),
            "  noted (no task)       : {}".format(self.noted),
            "marks written into the note : {}".format(self.rewritten),
        ]
        if self.kept:
            out.append("kept as they are (no Message-ID, mail not in the archive, or struck out) : {}".format(self.kept))
        out.extend("  " + line for line in self.lines[:40])
        if len(self.lines) > 40:
            out.append("  ... and {} more".format(len(self.lines) - 40))
        if self.dry_run:
            out.append("dry run: nothing was written")
        return out


def split_note(note: str):
    """('Ana', 'needs an answer', 'the words') for a reply note, else None."""
    found = _REPLY_PREFIX.match((note or "").strip())
    if found is None:
        return None
    return (found.group("who") or "").strip(), (found.group("mark") or "").strip(), \
        (note or "").strip()[found.end():].strip()


def reclassify(conn, dry_run: bool = True, search=None) -> ReclassifyReport:
    """Read every 'Reply from ...' note again with today's rule and write
    the mark into it (2026-09-17). The words are never changed,
    only the mark between the name and the colon, so the timeline and the
    R3 list agree on what a reply was.

    AL6a, with `search` (--from-archive): each live note is read again from
    its whole mail, found in the archive by the note's Message-ID, as the
    scan reads a reply now; a note without one, or whose mail is gone,
    keeps its mark, and so does a struck-out one."""
    repo = db.Repository(conn, source="import")
    report = ReclassifyReport(dry_run=dry_run)
    for row in conn.execute(
            "SELECT s.id, s.note, s.voided, s.at, e.opp_no FROM status_event s "
            "JOIN enquiry e ON e.id = s.enquiry_id "
            "WHERE s.kind = 'note' AND s.note LIKE 'Reply from %' ORDER BY s.id"):
        parts = split_note(row["note"])
        if parts is None:
            continue
        who, mark, body = parts
        if "[rate again]" in (row["note"] or ""):
            # S2: an accepted 'they asked for the rate again' note stays a
            # needs-an-answer reply until someone answers; never re-read
            report.kept += 1
            continue
        if search is not None:
            mids = [m.strip() for m in _MID.findall(row["note"] or "")]
            mail = search.by_message_id(mids[0]) if mids and not row["voided"] else None
            if mail is None:
                report.kept += 1
                continue
            kind = reply_kind(" ".join(mail_archive.top_part(mail.body_excerpt or "").split()))
        else:
            kind = reply_kind(body)
        if kind == "asks":
            report.asks += 1
        else:
            report.noted += 1
        wanted = "Reply from {} ({}): {}".format(
            who or "?", MARK_ASKS if kind == "asks" else MARK_NOTED, body)
        if wanted == row["note"]:
            continue
        report.rewritten += 1
        report.lines.append("{}{} {} {} -> ({})".format(
            row["opp_no"], " [voided]" if row["voided"] else "", (row["at"] or "")[:10], body[:60],
            MARK_ASKS if kind == "asks" else MARK_NOTED))
        if not dry_run:
            repo.update("status_event", row["id"], {"note": wanted})
    if dry_run:
        conn.rollback()
    else:
        repo.commit()
    return report


def run(conn, search, dry_run: bool = False, opp_nos=()) -> Report:
    """Every quoted case in scope (or the ones named)."""
    repo = db.Repository(conn, source="import")
    report = Report(dry_run=dry_run)
    internal = internal_domains(conn)
    if not dry_run:
        # FX7: a follow-up logged by hand or on the site since closes the mail's suggestion
        from . import suggestions
        report.closed = len(suggestions.close_logged_followups(repo))
    sql = "SELECT * FROM v_enquiry WHERE status = 'Quoted' AND scope = 'In' AND legacy = 0"
    args = ()
    if opp_nos:
        sql += " AND opp_no IN ({})".format(",".join("?" for _ in opp_nos))
        args = tuple(o.strip().upper() for o in opp_nos)
    for row in conn.execute(sql + " ORDER BY opp_no", args).fetchall():
        report.cases += 1
        scan_case(conn, repo, search, row, internal, report, dry_run)
    # WR1: the New cases waiting for the requester, from the wait note
    from . import wait
    waiting = wait.live_waits(conn)
    wanted = {o.strip().upper() for o in opp_nos}
    for row in conn.execute("SELECT * FROM v_enquiry WHERE status = 'New' AND scope = 'In' AND legacy = 0 "
                            "ORDER BY opp_no").fetchall():
        if row["id"] not in waiting or (wanted and row["opp_no"] not in wanted):
            continue
        report.waits += 1
        scan_case(conn, repo, search, row, internal, report, dry_run, waiting=waiting[row["id"]])
    if not opp_nos:
        # AL1: the acknowledgements, and the subjects whose case number
        # asks a question (two numbers, or one that does not fit the mail)
        from . import case_numbers
        report.case_numbers = case_numbers.scan(conn, repo, search, dry_run=dry_run)
    if dry_run:
        conn.rollback()
    else:
        repo.commit()
    return report
