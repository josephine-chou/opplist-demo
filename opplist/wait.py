"""Waiting for requester (batch WR1, 2026-11-06).

A New case often cannot be quoted until the requester sends what is
missing. The button writes one timeline note, 'Waiting for requester:
<what we need>' (the prefix is the marker, source manual); the status
stays New. Nothing else is stored: whether the wait is live is read off
the timeline every time.

The wait is live from that note until the first of:

- a live 'Reply from' note dated after it (the morning scan's, or hers);
- a live vendor request asked after it;
- a live status event after it (Quoted, Lost, any status, a requote);
- a live 'Info received' note after it;
- the note itself struck out (a newer wait note strikes the older out).

'After' on the same day is the order the rows were written in (the
event's id; for a vendor request, its created_at against the note's).

While it is live the case is not in R1; R12 lists it with a ladder of its
own, the quote ladder's settings counted from the wait note: FU1 fu1_days
working days after it, FU2 fu2_days after FU1 (or after her later note of
their answer), Decide fu_decide_days after the last. Only follow-ups
dated on or after the wait note count; a quote's ladder counts only those
on or after the quote, so the two never mix.

FX7 (2026-11-23, v58): a follow-up dated from the wait note's day to the
day the wait ended (v_wait: the first event that ended it, a Quoted among
them) is the wait's even when the quote is dated that same day (OPP-1728:
wait note, chaser, Quoted the same day); the quote's ladder, v_enquiry's
fu_count and the site push leave it out, and the timeline calls it
'FU<n> · our question'.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PREFIX = "Waiting for requester:"
INFO_RECEIVED = "Info received"
REPLY_PREFIX = "Reply from"
WHAT_MAX = 300
#: Our words when we chase the requester for details. The morning scan
#: reads a mail of ours with these (or the quote chaser's words) after the
#: wait note as a follow-up.
CHASER_PHRASES = re.compile(
    r"\b(?:still\s+need|we\s+need\s+the\s+following|need\s+the\s+following|"
    r"send\s+(?:us\s+)?(?:these|the|those)\s+(?:details|information|info)|"
    r"(?:missing|pending)\s+(?:details|information|info)|details\s+below|information\s+below|"
    r"following\s+up\s+on\s+the\s+details)\b", re.I)


@dataclass
class Wait:
    enquiry_id: int
    event_id: int
    since: str              # the note's date, YYYY-MM-DD
    what: str               # what we need
    start: str              # 'YYYY-MM-DD HH:MM': mails after this are after the note
    created_at: str = ""


def note_text(what: str) -> str:
    return "{} {}".format(PREFIX, " ".join((what or "").split()))


def is_wait_note(note) -> bool:
    return (note or "").startswith(PREFIX)


def what_of(note) -> str:
    return (note or "")[len(PREFIX):].strip() if is_wait_note(note) else ""


def _start(at, created_at) -> str:
    """Mails stamped after this come after the note: the moment it was
    written when it is dated that day, else the end of its day (a note
    dated back says 'since then'; a mail that day is not after it)."""
    day = (at or "")[:10]
    made = (created_at or "")[:16]
    if made[:10] == day and len(made) == 16:
        return made
    return day + " 23:59"


def _after(at, row_id, since, note_id) -> bool:
    at = (at or "")[:10]
    return at > since or (at == since and row_id > note_id)


def _ended(conn, enquiry_id, note) -> bool:
    since, note_id = (note["at"] or "")[:10], note["id"]
    for ev in conn.execute(
            "SELECT id, at, kind, note FROM status_event WHERE enquiry_id = ? AND voided = 0 AND at >= ? "
            "AND id <> ?", (enquiry_id, since, note_id)):
        if not _after(ev["at"], ev["id"], since, note_id):
            continue
        if ev["kind"] in ("status", "requote"):
            return True
        text = (ev["note"] or "").strip()
        if ev["kind"] == "note" and (text.lower().startswith(REPLY_PREFIX.lower())
                                     or text.lower().startswith(INFO_RECEIVED.lower())):
            return True
    for vr in conn.execute(
            "SELECT asked_on, created_at FROM vendor_request WHERE enquiry_id = ? AND voided = 0 "
            "AND asked_on IS NOT NULL AND asked_on <> '' AND asked_on >= ?", (enquiry_id, since)):
        asked = (vr["asked_on"] or "")[:10]
        if asked > since or (vr["created_at"] or "") >= (note["created_at"] or ""):
            return True
    return False


def _latest_note(conn, enquiry_id):
    return conn.execute(
        "SELECT id, at, note, created_at FROM status_event WHERE enquiry_id = ? AND kind = 'note' "
        "AND voided = 0 AND note LIKE ? ORDER BY at DESC, id DESC LIMIT 1",
        (enquiry_id, PREFIX + "%")).fetchone()


def live(conn, enquiry_id) -> Wait | None:
    """The case's live wait, or None. Only a New case waits."""
    case = conn.execute("SELECT status FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
    if case is None or case["status"] != "New":
        return None
    note = _latest_note(conn, enquiry_id)
    if note is None or not is_wait_note(note["note"]) or _ended(conn, enquiry_id, note):
        return None
    return Wait(enquiry_id, note["id"], (note["at"] or "")[:10], what_of(note["note"]),
                _start(note["at"], note["created_at"]), note["created_at"] or "")


def live_waits(conn) -> dict:
    """{enquiry_id: Wait} for every New case with a live wait."""
    out = {}
    for row in conn.execute(
            "SELECT DISTINCT s.enquiry_id FROM status_event s JOIN enquiry e ON e.id = s.enquiry_id "
            "WHERE e.status = 'New' AND s.kind = 'note' AND s.voided = 0 AND s.note LIKE ?", (PREFIX + "%",)):
        found = live(conn, row["enquiry_id"])
        if found is not None:
            out[row["enquiry_id"]] = found
    return out


# ------------------------------------------------------------------ writing --
def start(repo, enquiry_id: int, what: str, at: str, source: str = "manual") -> tuple:
    """The button: (event id, '') or (None, why not). A live wait on the
    case is replaced - its note struck out. WR2: the team site's waitreq
    writes the same, with source 'web'."""
    what = " ".join((what or "").split())
    if not what:
        return None, "Say what we need from the requester."
    if len(what) > WHAT_MAX:
        return None, "What we need is {} characters; keep it to {}.".format(len(what), WHAT_MAX)
    case = repo.conn.execute("SELECT status FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
    if case is None:
        return None, "No such inquiry."
    if case["status"] != "New":
        return None, "Waiting for requester is for a New case; this one is {}.".format(case["status"])
    older = live(repo.conn, enquiry_id)
    event_id = repo.add_note(enquiry_id, at, note_text(what), source=source)
    if older is not None:
        repo.void_status_event(older.event_id)
    return event_id, ""


def info_received(repo, enquiry_id: int, at: str, source: str = "manual") -> tuple:
    """'Info received': (event id, '') or (None, why not). WR2: the team
    site's inforec writes the same, with source 'web'."""
    found = live(repo.conn, enquiry_id)
    if found is None:
        return None, "This case is not waiting for the requester."
    if (at or "")[:10] < found.since:
        at = found.since
    return repo.add_note(enquiry_id, at, INFO_RECEIVED, source=source), ""


def prefill(conn, row) -> str:
    """What the acknowledgment would ask (ack.questions), as one line."""
    from . import ack
    try:
        asks = ack.questions(conn, row)
    except Exception:
        return ""
    text = "; ".join(asks)
    return text if len(text) <= WHAT_MAX else text[:WHAT_MAX].rsplit(";", 1)[0]


# ------------------------------------------------------------------- ladder --
def followups(conn, found: Wait) -> list:
    """The live follow-ups dated on or after the wait note, oldest first."""
    return conn.execute(
        "SELECT id, at, seq FROM status_event WHERE enquiry_id = ? AND kind = 'followup' AND voided = 0 "
        "AND at >= ? ORDER BY at, id", (found.enquiry_id, found.since)).fetchall()


def ladder(conn, found: Wait, today=None, settings=None) -> dict:
    """The wait's ladder, with tracking.ladder's keys (the case page reads
    either) and 'step': the step due today, or ''."""
    from . import tracking
    settings = settings or tracking.fu_settings(conn)
    today = tracking._today(today)
    fus = [r["at"][:10] for r in followups(conn, found)]
    # her later note (a call, their partial answer) restarts the clock
    last_note = conn.execute(
        "SELECT MAX(s.at) FROM status_event s WHERE s.enquiry_id = ? AND s.kind = 'note' AND s.voided = 0 "
        "AND s.at >= ? AND s.id <> ? AND NOT {}".format(tracking.system_event_sql("s")),
        (found.enquiry_id, found.since, found.event_id)).fetchone()[0]
    clock = max(d for d in (found.since, fus[-1] if fus else None, (last_note or "")[:10] or None) if d)
    count = len(fus)
    days = tracking.business_days_between(clock, today)
    top = max(int(settings["fu_max"]), 1)
    if count >= top:
        nxt, wanted = "Decide", settings["fu_decide_days"]
    elif count == 0:
        nxt, wanted = "FU1", settings["fu1_days"]
    else:
        nxt, wanted = "FU{}".format(count + 1), settings["fu2_days"]
    due = tracking.add_business_days(clock, wanted)
    step = nxt if days is not None and days >= wanted else ""
    left = tracking.business_days_between(today, due) if due else None
    steps = [("Waiting", found.since, "done")]
    after = due
    for n in range(1, max(top, count) + 1):
        label = "FU{}".format(n)
        if n <= count:
            steps.append((label, fus[n - 1], "done"))
        elif label == nxt:
            steps.append(("{} due".format(label), due, "now"))
        else:
            after = tracking.add_business_days(after, settings["fu2_days"]) if after else None
            steps.append((label, after, "todo"))
    if nxt == "Decide":
        steps.append(("Decide", due, "now"))
    else:
        steps.append(("Decide", tracking.add_business_days(after, settings["fu_decide_days"]) if after else None,
                      "todo"))
    return {"quoted": None, "since": found.since, "what": found.what, "steps": steps, "next": nxt,
            "next_due": due, "next_seq": int(nxt[2:]) if nxt.startswith("FU") else None,
            "days_left": 0 if step or (left is None and due and due <= today) else left,
            "clock": clock, "step": step, "fu_count": count}


def of_waits(conn, enquiry_id) -> dict:
    """FX7: {follow-up event id: wait note id} - the case's live follow-ups
    dated within a wait (v_wait_followup: from the wait note's day to the
    day the wait ended, the quote's own day included). They are the wait's,
    on our question; a quote's ladder never counts them."""
    return {r["id"]: r["wait_event_id"] for r in conn.execute(
        "SELECT id, wait_event_id FROM v_wait_followup WHERE enquiry_id = ? ORDER BY wait_since",
        (enquiry_id,))}


def wait_on(conn, enquiry_id, day):
    """FX7: the wait (a v_wait row: id, since, until) whose days hold `day`,
    the latest one when two do; None when no wait does."""
    day = (day or "")[:10]
    if not day:
        return None
    return conn.execute("SELECT * FROM v_wait WHERE enquiry_id = ? AND since <= ? AND until >= ? "
                        "ORDER BY since DESC, id DESC LIMIT 1", (enquiry_id, day, day)).fetchone()


def wait_seq(conn, wait_id) -> int:
    """FX7: the number the next follow-up of this wait takes."""
    return conn.execute("SELECT COUNT(*) FROM v_wait_followup WHERE wait_event_id = ?",
                        (wait_id,)).fetchone()[0] + 1


def next_seq(conn, enquiry_id) -> int | None:
    """The number Log as sent gives a chaser on a waiting case: the
    follow-ups since the wait note, plus one. None when not waiting."""
    found = live(conn, enquiry_id)
    if found is None:
        return None
    return len(followups(conn, found)) + 1


def doc(conn, enquiry_id, today=None, settings=None) -> dict | None:
    """The team site's case_doc 'wait' (WR1 item 7), or None."""
    found = live(conn, enquiry_id)
    if found is None:
        return None
    rung = ladder(conn, found, today, settings)
    step = rung["step"]
    return {"since": found.since, "what": found.what,
            "next": "{} due".format(step) if step.startswith("FU") else step}
