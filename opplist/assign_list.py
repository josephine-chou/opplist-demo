"""The team site's Assign list (lists/assign, INTERFACE.md): the JJC inquiries
still waiting for our quote, and (AL6) the quoted cases whose requester
asked us something we have not answered. Each colleague picks a name in
Handled By on the site; it is enquiry.owner - a person, never handled_by
(batch 10's office field), in either direction.

These were the Google Sheet's rules (AL2 to AL7); CX1 (2026-11-04) retired
the sheet and its sync and kept the lists here for the site. A direct
customer's case stays off both lists (AL7's named customers went with the
sheet's Settings part). SC1 (2026-11-10): the waiting list lists a direct
customer's case too, as every other inquiry. D7c (2026-11-13, her choice
A): the Replied list has a direct customer's open question too; only the
follow-up ladder leaves them out.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from . import db, parsing

#: AL5: the On hold text's reason is cut to this many characters
HOLD_REASON_MAX = 200
#: AL6: What They Asked is cut to this many characters
ASKED_MAX = 160


def owners(conn) -> list:
    """The Handled By names: Settings' owners."""
    return [str(o).strip() for o in db.get_setting_list(conn, "owners") if str(o).strip()]


def plain_subject(subject) -> str:
    """The registered subject without RE / FW and without a case number."""
    text = parsing.strip_case_numbers(subject or "")
    for _ in range(8):
        before = text
        text = parsing._REPLY_PREFIX.sub("", text)
        if text == before:
            break
    return re.sub(r"\s+", " ", text).strip()


def hold_text(reason, until) -> str:
    """AL5: the On hold text - the reason as she typed it (at most
    HOLD_REASON_MAX characters), ' · until MM/DD' with an Until date; ''
    when the case is not on hold."""
    reason = " ".join((reason or "").split())[:HOLD_REASON_MAX]
    if not reason:
        return ""
    try:
        day = dt.date.fromisoformat((until or "").strip()[:10])
    except ValueError:
        return reason
    return "{} · until {:02d}/{:02d}".format(reason, day.month, day.day)


@dataclass
class Row:
    opp_no: str
    enquiry_id: int
    requester: str
    subject: str
    received_on: str
    owner: str
    on_hold: str = ""


def listed(conn, today=None) -> list:
    """The waiting inquiries, in the list's order: Not assigned first, then
    Assigned; the oldest received first, then OPP No. AL4: a case on hold
    is listed like any other - whoever has it knows what it waits for;
    `today` is kept for the callers."""
    rows = conn.execute(
        "SELECT id, opp_no, requester_key, requester_type, requester_party_id, source_subject, received_on, owner, "
        "hold_reason, hold_until FROM v_enquiry WHERE status = 'New' AND handled_by IS NULL AND scope = 'In' "
        "AND legacy = 0 "
        "AND COALESCE(closed_reason, '') <> 'Registered in error' "
        "AND COALESCE(closed_reason, '') NOT LIKE 'Duplicate%'").fetchall()
    out = [Row(r["opp_no"], r["id"], r["requester_key"] or "", plain_subject(r["source_subject"]),
               r["received_on"] or "", (r["owner"] or "").strip(), hold_text(r["hold_reason"], r["hold_until"]))
           for r in rows]
    out.sort(key=lambda r: (bool(r.owner), r.received_on or "9999", r.opp_no))
    return out


@dataclass
class PendingRow:
    opp_no: str
    enquiry_id: int
    requester: str
    subject: str
    asked_on: str
    owner: str
    note_id: int
    note: str


def pending_listed(conn, today=None) -> list:
    """AL6: the cases Tracking shows in its Replied group today -
    rule_quoted_no_answer's Replied rows, without the snoozed ones and the
    ones held until a later day (as focus_count leaves them out) - in the
    waiting list's scope (JJC's, In, not legacy, not void; D7c: a direct
    customer's question too). Not assigned first, then Assigned; the oldest Asked On
    first, then OPP No."""
    from . import tracking
    today = tracking._today(today if today is None or isinstance(today, str) else today.isoformat())
    asleep = tracking._snoozed(conn, today)
    held = tracking.held_until_later(conn, today)
    out = []
    for item in tracking.rule_quoted_no_answer(conn, today, tracking.fu_settings(conn)):
        if item.extra.get("step") != "Replied" or (item.rule, item.kind, item.ref_id) in asleep \
                or item.enquiry_id in held:
            continue
        row = conn.execute(
            "SELECT id, opp_no, requester_key, requester_type, requester_party_id, source_subject, owner, scope, "
            "legacy, handled_by, closed_reason FROM v_enquiry WHERE id = ?", (item.enquiry_id,)).fetchone()
        # D7c (her choice A of 13 Nov): a direct customer's question is listed too
        if row is None or row["scope"] != "In" or row["legacy"] or row["handled_by"] \
                or (row["closed_reason"] or "") == "Registered in error" \
                or (row["closed_reason"] or "").startswith("Duplicate"):
            continue
        # DD1: the question itself - its date and words - whatever came after it
        note = conn.execute("SELECT id, at, note FROM status_event WHERE id = ?",
                            (item.extra.get("question_id"),)).fetchone()
        if note is None:
            continue
        out.append(PendingRow(row["opp_no"], row["id"], row["requester_key"] or "",
                              plain_subject(row["source_subject"]), (note["at"] or "")[:10],
                              (row["owner"] or "").strip(), note["id"], note["note"] or ""))
    out.sort(key=lambda r: (bool(r.owner), r.asked_on or "9999", r.opp_no))
    return out


# --------------------------------------------- What They Asked (AL6) --
_GREETING = re.compile(r"^(dear|hi|hello|hey|good\s+day|good\s+morning|good\s+afternoon|hola|estimad[oa]s?|"
                       r"prezad[oa]s?|ol[aá]|buen\s+d[ií]a|bom\s+dia)\b", re.I)
_THANKS_FIRST = re.compile(r"^(many\s+thanks|thanks|thank\s+you|tks|thx|gracias|obrigad[oa])\b", re.I)
_THANKS_LAST = re.compile(r"[\s,.;:!&，-]*\b(thanks|tks|thx)\b[\s.!&]*$", re.I)
#: what may hang at the end once the sign-off is gone ('Thanks &' of
#: 'Thanks & Best Regards', a full-width comma)
_TAIL = re.compile(r"[\s,;:&，、-]+$")
_SENTENCE = re.compile(r"(?<=[.?!])\s+|\s+(?=\d{1,2}\.\s)")


def asked_words(top: str) -> str:
    """The words of a reply that ask us something: links, emoji and the
    sign-off with everything after it out; greeting lines at the top and a
    leading thank-you sentence dropped; the sentences reply_kind reads as
    asking, in order (else the first ones); at most ASKED_MAX characters,
    cut at a sentence end when one fits, else at a word, '...' when cut."""
    from . import detect_followups, tracking
    text = detect_followups._LINKS.sub(" ", top or "")
    text = tracking._URL.sub(" ", text)
    text = tracking._EMOJI.sub("", text)
    text = tracking._SIGNOFF.sub("", text)
    lines = [line.strip() for line in text.splitlines()]
    while lines and (not lines[0] or (len(lines[0].split()) <= 6 and _GREETING.match(lines[0]))):
        lines.pop(0)
    text = " ".join(" ".join(lines).split())
    text = re.sub(r"\s+([.,?!;:])", r"\1", text)          # what a link left behind
    sentences = [x.strip() for x in _SENTENCE.split(text) if x.strip()]
    if sentences and _THANKS_FIRST.match(sentences[0]):
        sentences = sentences[1:]
    asking = [x for x in sentences if detect_followups.reply_kind(x) == "asks"]
    chosen = asking or sentences
    whole = " ".join(chosen)
    whole = _TAIL.sub("", whole)
    whole = _THANKS_LAST.sub("", whole)
    whole = _TAIL.sub("", whole).strip()
    if len(whole) <= ASKED_MAX:
        return whole
    kept = ""
    for sentence in chosen:
        trial = (kept + " " + sentence).strip()
        if len(trial) + 4 > ASKED_MAX:
            break
        kept = trial
    if kept:
        return kept + " ..."
    cut_at = whole[:ASKED_MAX - 3].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut_at + "..."


def what_they_asked(note: str, search=None) -> str:
    """'<sender>: <words>' - the sender the note's, the words from the reply
    mail found by the note's Message-ID (its top part); without an archive
    or that mail, the note's own words."""
    from . import detect_followups, mail_archive, tracking
    parts = detect_followups.split_note(note)
    who = parts[0] if parts else ""
    mids = [m.strip() for m in re.findall(r"\[mid:([^\]]+)\]", note or "")]
    mail = None
    if search is not None and mids:
        try:
            mail = search.by_message_id(mids[0])
        except Exception:
            mail = None
    if mail is not None:
        words = asked_words(mail_archive.top_part(mail.body_excerpt or ""))
        if words:
            return "{}: {}".format(who, words) if who else words
    return tracking.activity_gist(note, ASKED_MAX)


def archive_search():
    """The mail archive for What They Asked, or None (then the note's words)."""
    from . import mail_archive
    try:
        search = mail_archive.ArchiveSearch()
        search.__enter__()
        return search
    except Exception:
        return None
