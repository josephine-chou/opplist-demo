"""The tracking list: what needs doing today.

Nine rules, each answering one question about one row. They only read; acting
on an item goes through the ordinary repository calls, so a case changed from
here is changed exactly as it would be from its own page.

Two things every rule obeys: `legacy = 1` never appears (the previous owner's
backlog is not today's work), and anything snoozed stays away until its date.

Business days matter for the short rules - a mail sent on Friday is not
overdue on Saturday - so those are counted in Python rather than in SQL.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import dataclass, field

from . import db

#: The placeholder that means 'still looking'. 'Unknown' is a
#: decision to stop looking and is deliberately not chased.
PLACEHOLDER_SEARCHING = "(TBC)"


@dataclass
class Item:
    """One row of the list: what, which, how long, and what it needs."""

    rule: str
    kind: str                      # enquiry | party | vendor_request
    ref_id: int                    # what a snooze would name
    enquiry_id: int | None = None  # where 'Open' goes for a case row
    party_id: int | None = None
    label: str = ""                # OPP-1401, or the party key
    detail: str = ""               # the one line she needs to decide
    days: int | None = None
    since: str = ""                # the date `days` is counted from
    extra: dict = field(default_factory=dict)
    #: The detail as separate words for the page (requester, lane, mode,
    #: commodity); `detail` keeps the one-line form the CLI prints.
    parts: list = field(default_factory=list)
    #: Past the point where the row goes red (LATE_AFTER); only R1 and R3.
    late: bool = False


@dataclass
class Section:
    rule: str
    title: str
    hint: str
    action: str                    # which row form the page draws
    band: str = "today"            # today | backlog
    items: list = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.items)


def _date(value):
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def business_days_between(start, end) -> int | None:
    """Whole working days from `start` to `end`, Monday to Friday.

    Holidays are not modelled: the rules count weekdays, and a calendar of
    public holidays would be one more thing to keep right for no gain.
    """
    first, last = _date(start), _date(end)
    if first is None or last is None or last < first:
        return None
    days = 0
    step = first
    while step < last:
        step += dt.timedelta(days=1)
        if step.weekday() < 5:
            days += 1
    return days


def add_business_days(start, count: int) -> str | None:
    """The date `count` working days after `start` (Monday to Friday, no
    holidays - the same calendar as business_days_between)."""
    day = _date(start)
    if day is None:
        return None
    left = max(0, int(count or 0))
    while left > 0:
        day += dt.timedelta(days=1)
        if day.weekday() < 5:
            left -= 1
    return day.isoformat()


def ladder(conn, row, today=None, settings=None) -> dict:
    """The follow-up ladder of one case for the case page
    (2026-09-24): the quote date, each FU sent or due, the decision date, and
    which step is next. The clock is rule_quoted_no_answer's - the latest
    of the quote, the last follow-up and the last live note - and the
    thresholds are fu_settings(); nothing here re-decides the ladder, it
    only puts dates on it. Every date is ISO; None when it does not apply.

    Keys: quoted, steps [(label, date, state)] with state done / now /
    todo, next (label or ''), next_due, days_left (None when not due,
    0 when due now), clock.
    """
    settings = settings or fu_settings(conn)
    today = _today(today)
    quoted = row["quoted_on"] or row["quoted_on_any"]
    out = {"quoted": quoted, "steps": [], "next": "", "next_due": None,
           "next_seq": None, "days_left": None, "clock": None}
    if row["status"] != "Quoted" or not quoted:
        return out
    case_id = row["id"]
    # FX7: a wait's follow-ups (v_wait_followup) are never the quote's, its own day included
    fus = [r["at"] for r in conn.execute(
        "SELECT at FROM status_event s WHERE enquiry_id = ? AND kind = 'followup' "
        "AND voided = 0 AND at >= ? AND NOT EXISTS (SELECT 1 FROM v_wait_followup x WHERE x.id = s.id) "
        "ORDER BY at, id", (case_id, quoted))]
    last_note = conn.execute(
        "SELECT MAX(at) FROM status_event WHERE enquiry_id = ? AND kind = 'note' AND voided = 0",
        (case_id,)).fetchone()[0]
    live_note = conn.execute(
        "SELECT MAX(at) FROM status_event s WHERE enquiry_id = ? AND kind = 'note' AND voided = 0 "
        "AND NOT {}".format(system_event_sql("s")), (case_id,)).fetchone()[0]
    last_event = conn.execute(
        "SELECT MAX(at) FROM status_event WHERE enquiry_id = ? AND voided = 0",
        (case_id,)).fetchone()[0]
    clock = max(d for d in (quoted, fus[-1] if fus else None, last_note, last_event) if d)
    out["clock"] = clock
    count = len(fus)
    days = business_days_between(clock, today)
    answered = fu1_answered(live_note, fus[-1] if fus else None)
    step_now = fu_step(count, days, settings, answered=answered)
    whole = settings["fu1_days"] + settings["fu2_days"] + settings["fu_decide_days"]
    wait = settings.get("fu1_wait_days", FU_DEFAULTS["fu1_wait_days"])
    # L2: until they answer FU1 there is no FU2 on the ladder - FU1, then Decide
    waits = count <= 1 < settings["fu_max"] and not answered

    # the next step and when it is due, on the same clock the rule reads
    if count >= settings["fu_max"]:
        nxt, due = "Decide", add_business_days(clock, settings["fu_decide_days"])
    elif count == 0 and days is not None and days >= whole:
        nxt, due = "Decide", clock
    elif count == 0:
        nxt, due = "FU1", add_business_days(clock, settings["fu1_days"])
    elif waits:
        nxt, due = "Decide", add_business_days(clock, wait)
    else:
        nxt, due = "FU{}".format(count + 1), add_business_days(clock, settings["fu2_days"])
    if step_now:
        nxt = step_now
    out["next"], out["next_due"] = nxt, due
    out["next_seq"] = int(nxt[2:]) if nxt.startswith("FU") else None
    left = business_days_between(today, due) if due else None
    out["days_left"] = 0 if (left is None and due and due <= today) or step_now else left

    steps = [("Quoted", quoted, "done")]
    for n in range(1, (1 if waits else max(settings["fu_max"], count)) + 1):
        if n <= count:
            steps.append(("FU{}".format(n), fus[n - 1], "done"))
        elif "FU{}".format(n) == nxt:
            steps.append(("FU{} due".format(n), due, "now"))
        else:
            # a later rung: its due date follows the one before it
            steps.append(("FU{}".format(n), None, "todo"))
    if nxt == "Decide":
        steps.append(("Decide", due, "now"))
    elif waits:
        steps.append(("Decide", add_business_days(due, wait) if due else None, "todo"))
    else:
        after = due
        for _ in range(count + 1, settings["fu_max"]):
            after = add_business_days(after, settings["fu2_days"])
        steps.append(("Decide", add_business_days(after, settings["fu_decide_days"]) if after else None,
                      "todo"))
    out["steps"] = steps
    return out


def calendar_days_between(start, end) -> int | None:
    first, last = _date(start), _date(end)
    if first is None or last is None:
        return None
    return (last - first).days


#: Every rule reads the In cases only: an Out case (a direct customer's
#: own shipment) is nobody's enquiry to chase (v9). The rules
#: below spell it as (SELECT * FROM v_enquiry WHERE scope = 'In') e.
#: v31 (2026-09-25): the chasing rules - R1, R2, R3, R6, R9, R10 - also
#: leave out a case another of our offices handles (handled_by set: the home office
#: is only copied); the data rules R4, R5 and R8 list every case, and R7 is
#: per party. The New-case invariant covers the home office's cases only.


#: Batch 21: a direct customer's case counts like any other, but nobody
#: chases a direct customer: R3's follow-up ladder (FU1, FU2, Decide)
#: leaves their cases out, and the case page's Follow-ups card says
#: 'Direct customer - no follow-ups'. But a direct customer's question is
#: handled as an agent's (her choice A of 13 Nov, batch D7c): Waiting on
#: us, Pending on us and Answered as usual, counted in Tracking's Replied
#: and in the nav number. Every other Tracking rule applies.
DIRECT_CUSTOMER = "Direct Customer"


def no_followups(requester_type) -> bool:
    """True for a case whose requester is a direct customer (batch 21)."""
    return (requester_type or "") == DIRECT_CUSTOMER


def _today(today=None) -> str:
    return today or dt.date.today().isoformat()


def _snoozed(conn, today) -> set:
    return {(r["rule"], r["ref_kind"], r["ref_id"]) for r in conn.execute(
        "SELECT rule, ref_kind, ref_id FROM tracking_snooze WHERE until >= ?",
        (today,))}


def _lane(row) -> str:
    origin = row["origin_loc"] or row["origin_country"] or "?"
    dest = row["dest_loc"] or row["dest_country"] or "?"
    return "{} - {}".format(origin, dest)


def _case_parts(row) -> list:
    bits = [row["requester_key"], _lane(row)]
    if row["service_mode"]:
        bits.append(row["service_mode"])
    if row["commodity"]:
        bits.append(row["commodity"])
    return [str(b) for b in bits if b]


def _case_detail(row) -> str:
    return "  ".join(_case_parts(row))


# ------------------------------------------------------------------ rules --
#: Events a CLI backfill wrote that record nothing that happened with a
#: customer or a vendor: source 'import' (retire-sourcing's "Sourcing
#: retired, back to New", fix-closed-reasons' relabelling, fix-locations'
#: lane note, and any later backfill of that kind). They never count as
#: activity in a rule's 'newer than' or 'last activity', and never show as
#: Last activity on Tracking or the Enquiries page. The cutover's events
#: (source 'migration') are history - real quotes and status changes with
#: the workbook's dates - and count like any other (2026-09-24, 8b fix).
SYSTEM_SOURCES = ("import",)


def system_event_sql(alias: str = "s") -> str:
    return "({a}.source IN ({src}))".format(
        a=alias, src=",".join("'{}'".format(x) for x in SYSTEM_SOURCES))


def activity_on_sql(case_alias: str = "e") -> str:
    """The date of the case's newest live event that is not a backfill's -
    the rules' and the pages' 'last activity' (v_enquiry.last_event_on
    counts the backfills' events too)."""
    return ("(SELECT MAX(s.at) FROM status_event s WHERE s.enquiry_id = {e}.id "
            "AND s.voided = 0 AND NOT {sys})").format(e=case_alias, sys=system_event_sql("s"))



def round_start_sql(case_alias: str = "e") -> str:
    """For a New case in an open re-quote round (v_enquiry.in_requote), the
    date of the round's requote event (the latest live one on New or
    Sourcing); NULL for any other case. The alias must be a v_enquiry row.
    Since v30 the Re-quoted button writes a requote event on Quoted, which
    closes its own round, so only old data can open one - on the live data
    this has nothing to act on (kept so such a case still reads right)."""
    return ("(CASE WHEN {e}.status = 'New' AND {e}.in_requote = 1 THEN "
            "(SELECT MAX(r.at) FROM status_event r WHERE r.enquiry_id = {e}.id "
            "AND r.voided = 0 AND r.kind = 'requote' AND r.status IN ('New','Sourcing')) END)"
            ).format(e=case_alias)


def in_round_sql(vr_alias: str = "v", case_alias: str = "e") -> str:
    """A live vendor request that belongs to the case's current round: any
    of them outside a re-quote round; inside one, only those asked on or
    after the day the round opened - a new round needs new rates, so the
    earlier round's requests and answers count for nothing in R1, R2, R9."""
    return ("({v}.voided = 0 AND ({rs} IS NULL OR {v}.asked_on >= {rs}))").format(
        v=vr_alias, rs=round_start_sql(case_alias))


def rule_new_no_action(conn, today, days_wanted) -> list:
    """R1: registered, nothing done. Either set a status or ask a vendor.

    Nothing done means no live vendor request (in a re-quote round: none
    asked in this round, in_round_sql). Notes, a status set back to New by
    hand, retire-sourcing's event - no other event takes a New case out of
    R1: it is our turn until somebody is asked for a rate. WR1: or until
    she says we wait for the requester's details (R12 lists it then)."""
    from . import wait
    waiting = wait.live_waits(conn)
    items = []
    for row in conn.execute("""
            SELECT e.* FROM (SELECT * FROM v_enquiry WHERE scope = 'In' AND handled_by IS NULL) e
            WHERE e.status = 'New' AND e.status_confirmed = 1 AND e.legacy = 0
              AND NOT EXISTS (SELECT 1 FROM vendor_request v
                              WHERE v.enquiry_id = e.id AND {})
            ORDER BY e.received_on""".format(in_round_sql("v", "e"))):
        if row["id"] in waiting:
            continue
        days = business_days_between(row["received_on"], today)
        # no received date: it cannot be timed, but it is still our turn
        if days is not None and days <= days_wanted:
            continue
        items.append(Item("R1", "enquiry", row["id"], enquiry_id=row["id"],
                          label=row["opp_no"], detail=_case_detail(row), parts=_case_parts(row),
                          days=days, since=row["received_on"]))
    return items


def rule_waiting(conn, today, settings=None) -> list:
    """R12 (WR1): every New case with a live wait for the requester's
    details (wait.live), with its ladder's step - 'FU1' / 'FU2' / 'Decide'
    when one is due, '' while it waits - its working days since the wait
    note and the what-we-need line."""
    from . import wait
    settings = settings or fu_settings(conn)
    items = []
    waiting = wait.live_waits(conn)
    if not waiting:
        return items
    for row in conn.execute("""
            SELECT e.* FROM (SELECT * FROM v_enquiry WHERE scope = 'In' AND handled_by IS NULL) e
            WHERE e.status = 'New' AND e.legacy = 0 ORDER BY e.received_on"""):
        found = waiting.get(row["id"])
        if found is None:
            continue
        rung = wait.ladder(conn, found, today, settings)
        items.append(Item("R12", "enquiry", row["id"], enquiry_id=row["id"], label=row["opp_no"],
                          detail=_case_detail(row), parts=_case_parts(row),
                          days=business_days_between(found.since, today), since=found.since,
                          extra={"step": rung["step"], "what": found.what, "next": rung["next"],
                                 "next_due": rung["next_due"], "next_seq": rung["next_seq"],
                                 "fu_count": rung["fu_count"]}))
    return items


def rule_vendor_silent(conn, today, days_wanted) -> list:
    """R2: we asked somebody and they have not come back (in a re-quote
    round, only the requests of this round)."""
    items = []
    for row in conn.execute("""
            SELECT v.id AS vendor_request_id, v.asked_on, v.enquiry_id,
                   p.key AS vendor_key, e.opp_no, e.requester_key,
                   e.origin_loc, e.dest_loc, e.origin_country, e.dest_country,
                   e.service_mode, e.commodity
            FROM vendor_request v
            JOIN (SELECT * FROM v_enquiry WHERE scope = 'In' AND handled_by IS NULL) e ON e.id = v.enquiry_id
            JOIN party p ON p.id = v.vendor_party_id
            WHERE v.replied_on IS NULL AND v.voided = 0 AND e.legacy = 0
              AND e.status IN ('New','Quoted') AND {}
            ORDER BY v.asked_on""".format(in_round_sql("v", "e"))):
        days = business_days_between(row["asked_on"], today)
        if days is None or days < days_wanted:
            continue
        items.append(Item(
            "R2", "vendor_request", row["vendor_request_id"],
            enquiry_id=row["enquiry_id"], label=row["opp_no"],
            detail="{} has not replied   {}".format(
                row["vendor_key"], _lane(row)),
            days=days, since=row["asked_on"],
            extra={"vendor_key": row["vendor_key"]}))
    return items


#: The R3 ladder's settings and their defaults (schema v13).
#: L2 (2026-10-21): fu1_wait_days - after FU1 with no answer, Decide this
#: many working days on; no FU2 unless they answered FU1.
FU_DEFAULTS = {"fu1_days": 1, "fu2_days": 5, "fu_decide_days": 5, "fu_max": 2, "fu1_wait_days": 10}


def fu_settings(conn) -> dict:
    out = {}
    for key, default in FU_DEFAULTS.items():
        try:
            out[key] = int(db.get_setting(conn, key, default))
        except (TypeError, ValueError):
            out[key] = default
    return out


def fu1_answered(last_note_on, last_fu_on) -> bool:
    """L2: the requester answered after our last follow-up - a live note
    that is not a system event (a reply the scan wrote, or her own note of
    a call or a WhatsApp) dated on or after that follow-up's day."""
    return bool(last_note_on and last_fu_on and str(last_note_on)[:10] >= str(last_fu_on)[:10])


def fu_step(fu_count: int, days: int | None, settings: dict, answered: bool = False) -> str:
    """Where a quoted case stands on the ladder: 'FU1' (nothing sent yet and
    fu1_days are up), 'FU2' (one sent, they answered it - fu1_answered -
    and fu2_days up), 'Decide' (one sent and no answer for fu1_wait_days,
    L2; or fu_max sent and fu_decide_days up), '' when it is still time to
    wait."""
    if days is None:
        return ""
    if fu_count >= settings["fu_max"]:
        return "Decide" if days >= settings["fu_decide_days"] else ""
    if fu_count == 0:
        # an old quote nobody ever chased: past the whole ladder's length it
        # is a decision, not a first follow-up
        whole = settings["fu1_days"] + settings["fu2_days"] + settings["fu_decide_days"]
        if days >= whole:
            return "Decide"
        return "FU1" if days >= settings["fu1_days"] else ""
    if fu_count == 1:
        if answered:
            return "FU2" if days >= settings["fu2_days"] else ""
        # L2: no answer to FU1 - there is no FU2, the case waits for a decision
        return "Decide" if days >= settings.get("fu1_wait_days", FU_DEFAULTS["fu1_wait_days"]) else ""
    # more than one sent but fewer than fu_max (fu_max raised later): the
    # next one is due on the FU2 spacing
    return "FU{}".format(fu_count + 1) if days >= settings["fu2_days"] else ""


#: DD1 (2026-11-12, her answer to FX4's question): who may log or clear a
#: question - the case page (manual) and the team site (web); nothing the
#: morning scan writes does either
HAND_SOURCES = ("manual", "web")


def open_question(conn, enquiry_id):
    """DD1: the question logged by hand (Waiting on us, on the case page or
    the team site) that nobody has answered yet, as its status_event row
    (id, at, note, source), else None.

    The newest live 'Reply from' note logged by hand that asks us something
    stays open until a person records what answers it, after it (a later
    day, or the same day written later): Answered (an 'Answered by' note
    logged by hand) or Re-quoted (a requote event). A closing status takes
    the case off Quoted, so off every R3 row. Nothing the scan writes - a
    later reply written as noted, a mail of ours - closes it: those move
    the follow-up clock and leave the question open."""
    from . import detect_followups
    marks = ",".join("'{}'".format(s) for s in HAND_SOURCES)
    for q in conn.execute(
            "SELECT id, at, note, source FROM status_event WHERE enquiry_id = ? AND kind = 'note' AND voided = 0 "
            "AND source IN ({}) AND note LIKE 'Reply from %' ORDER BY at DESC, id DESC".format(marks),
            (enquiry_id,)):
        if not detect_followups.asks_us(q["note"], q["source"]):
            continue
        answered = conn.execute(
            "SELECT 1 FROM status_event WHERE enquiry_id = ? AND voided = 0 "
            "AND ((kind = 'note' AND source IN ({}) AND note LIKE ?) OR kind = 'requote') "
            "AND (substr(at, 1, 10) > ? OR (substr(at, 1, 10) = ? AND id > ?)) LIMIT 1".format(marks),
            (enquiry_id, detect_followups.ANSWER_PREFIX + "%", (q["at"] or "")[:10], (q["at"] or "")[:10],
             q["id"])).fetchone()
        return None if answered else q
    return None


def replied_note(conn, enquiry_id):
    """(at, note) of the case's open question (open_question), else None."""
    q = open_question(conn, enquiry_id)
    return ((q["at"] or "")[:10], q["note"]) if q is not None else None


def rule_quoted_no_answer(conn, today, settings=None) -> list:
    """R3: quoted, and the follow-up ladder says it is time for the next
    step. The clock starts at the latest of the quote, the last follow-up
    and the last live note, in working days. Direct customers
    are left out (batch 21) - but for their open question (D7c, her choice
    A of 13 Nov): Waiting on us works for them as for an agent; the ladder
    (FU1, FU2, Decide) stays off."""
    settings = settings or FU_DEFAULTS
    items = []
    for row in conn.execute("""
            SELECT e.*, {act} AS activity_on,
                   (SELECT MAX(s.at) FROM status_event s WHERE s.enquiry_id = e.id
                     AND s.kind = 'note' AND s.voided = 0 AND NOT {sys}) AS last_note_on,
                   (SELECT s.note FROM status_event s WHERE s.enquiry_id = e.id
                     AND s.kind = 'note' AND s.voided = 0 AND NOT {sys}
                    ORDER BY s.at DESC, s.id DESC LIMIT 1) AS last_note,
                   (SELECT s.source FROM status_event s WHERE s.enquiry_id = e.id
                     AND s.kind = 'note' AND s.voided = 0 AND NOT {sys}
                    ORDER BY s.at DESC, s.id DESC LIMIT 1) AS last_note_source
            FROM (SELECT * FROM v_enquiry WHERE scope = 'In' AND handled_by IS NULL) e
            WHERE e.status = 'Quoted' AND e.legacy = 0
            ORDER BY activity_on""".format(act=activity_on_sql("e"), sys=system_event_sql("s"))):
        # batch 21: nobody chases a direct customer; D7c: their question waits on us all the same
        direct = no_followups(row["requester_type"])
        quoted = row["quoted_on"] or row["quoted_on_any"] or row["received_on"]
        # a status event after the quote (quoted again, a corrected date)
        # is activity too: activity_on covers it, notes and follow-ups -
        # but not an event the system wrote (a backfill, the cutover)
        clock = max(d for d in (quoted, row["last_fu_on"], row["last_note_on"],
                                row["activity_on"]) if d)
        days = business_days_between(clock, today)
        fu_count = int(row["fu_count"] or 0)
        extra = {"fu_count": fu_count, "next_seq": fu_count + 1}
        from . import detect_followups          # a reply's kind, read off the note

        question = open_question(conn, row["id"])
        if question is None and direct:
            continue
        if question is not None:
            # Waiting on us (FX4): a question logged by hand on the case
            # page or on the team site, ours at once. DD1: it stays until a
            # person answers it (Answered, Re-quoted, a closing status); a
            # later reply the scan wrote or a mail of ours moves the clock
            # and leaves it open. The row keeps the question's date and words.
            clock = (question["at"] or "")[:10]
            days = business_days_between(clock, today)
            if days is None:
                continue
            step = "Replied"
            extra["reply_note"] = _reply_gist(question["note"])
            extra["reply_note_full"] = question["note"]     # the page's title
            extra["question_id"] = question["id"]

        else:
            step = fu_step(fu_count, days, settings,
                           answered=fu1_answered(row["last_note_on"], row["last_fu_on"]))
        if not step:
            continue
        extra["step"] = step
        items.append(Item("R3", "enquiry", row["id"], enquiry_id=row["id"],
                          label=row["opp_no"], detail=_case_detail(row), parts=_case_parts(row),
                          days=days, since=clock, extra=extra))
    return items


#: R3 is read in this order: what they asked us, then the ladder, then the
#: cases waiting for a decision (2026-09-17). Each group is a
#: heading of its own on the page, with its own count and its own 'Show all'.
R3_TITLES = {
    "Replied": "Waiting on us",
    "FU1": "FU1 due",
    "FU2": "FU2 due",
    "Decide": "Decide - no answer after the follow-ups",
}


def _r3_rank(step: str) -> float:
    if step == "Replied":
        return 0
    if step == "Decide":
        return 9
    if step.startswith("FU"):
        try:
            return 1 + int(step[2:]) / 100
        except ValueError:
            return 5
    return 5


def r3_groups(section) -> list:
    """[{key, step, title, rows}] for one R3 section, in reading order. An
    empty group is left out; the section's own count is unchanged.

    The rows are under 'rows', not 'items': a template reading `group.items`
    would get the dict's own method instead (the same trap as `t.clear`).
    """
    by_step = {}
    for item in section.items:
        by_step.setdefault(item.extra.get("step", ""), []).append(item)
    out = []
    for step in sorted(by_step, key=lambda s: (_r3_rank(s), s)):
        out.append({"key": "{}-{}".format(section.rule, step.lower()), "step": step,
                    "title": R3_TITLES.get(step, "{} due".format(step)),
                    "rows": by_step[step]})
    return out


_GIST = re.compile(r"^Reply from\s+(?P<who>[^:(]*?)\s*(?:\([^)]*\))?\s*:\s*(?P<body>.*)$", re.S | re.I)


def _reply_gist(note: str, limit: int = 60) -> str:
    """'Reply from Ana (needs an answer): can you redo it [mid:...]' ->
    'Ana: can you redo it'. The mark and the Message-ID are for the
    timeline; the row has room for the words themselves."""
    text = re.sub(r"\s*\[mid:[^\]]*\]", "", note or "").strip()
    found = _GIST.match(text)
    if found:
        text = "{}: {}".format(found.group("who").strip(),
                               " ".join(found.group("body").split()))
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def rule_confirm_status(conn, today) -> list:
    """R4: migrated with a blank status. Say what it really is."""
    return [Item("R4", "enquiry", row["id"], enquiry_id=row["id"],
                 label=row["opp_no"], detail=_case_detail(row), parts=_case_parts(row),
                 days=calendar_days_between(row["received_on"], today),
                 since=row["received_on"],
                 extra={"status": row["status"]})
            for row in conn.execute("""
                SELECT e.* FROM (SELECT * FROM v_enquiry WHERE scope = 'In') e
                WHERE e.status_confirmed = 0 AND e.legacy = 0
                ORDER BY e.received_on DESC""")]


def rule_identify_requester(conn, today) -> list:
    """R5: parked on '(TBC)' when it was registered. Who was it for?"""
    return [Item("R5", "enquiry", row["id"], enquiry_id=row["id"],
                 label=row["opp_no"], detail=_case_detail(row), parts=_case_parts(row),
                 days=calendar_days_between(row["received_on"], today),
                 since=row["received_on"])
            for row in conn.execute("""
                SELECT e.* FROM (SELECT * FROM v_enquiry WHERE scope = 'In') e
                JOIN party p ON p.id = e.requester_party_id
                WHERE p.placeholder = 1 AND lower(trim(p.key)) = ?
                  AND e.legacy = 0 AND e.status IN ('New','Quoted')
                ORDER BY e.received_on DESC""",
                (PLACEHOLDER_SEARCHING.lower(),))]


def rule_record_who_won(conn, today) -> list:
    """R6: won, vendors were asked, nobody recorded whose rate it was."""
    return [Item("R6", "enquiry", row["id"], enquiry_id=row["id"],
                 label=row["opp_no"], detail=_case_detail(row), parts=_case_parts(row),
                 days=calendar_days_between(row["closed_on_any"], today),
                 since=row["closed_on_any"] or "")
            for row in conn.execute("""
                SELECT e.* FROM (SELECT * FROM v_enquiry WHERE scope = 'In' AND handled_by IS NULL) e
                WHERE e.status = 'Won' AND e.legacy = 0
                  AND e.rate_used IS NULL
                  AND EXISTS (SELECT 1 FROM vendor_request v
                              WHERE v.enquiry_id = e.id AND v.voided = 0)
                ORDER BY e.closed_on_any DESC""")]


def rule_touch_base(conn, today, cadence_default) -> list:
    """R7: a party we work with that we have not spoken to in a while.

    Only parties with something on the clock already: a name imported from an
    agent list has never been contacted, and that is prospecting work, not a
    reminder that a relationship is going quiet.
    """
    items = []
    for row in conn.execute("""
            SELECT * FROM v_party_stats
            WHERE stage IN ('active','contacted') AND placeholder = 0
              AND last_activity_on IS NOT NULL
            ORDER BY last_activity_on"""):
        cadence = row["cadence_days"] or cadence_default
        days = calendar_days_between(row["last_activity_on"], today)
        if days is None or days < cadence:
            continue
        items.append(Item(
            "R7", "party", row["id"], party_id=row["id"], label=row["key"],
            detail="{}  {}  {} inquiries".format(
                row["type"], row["country"] or "?", row["total_enquiries"]),
            days=days, since=row["last_activity_on"],
            extra={"cadence": cadence}))
    return items


def lane_needs_fixing_sql(conn, alias: str = "e") -> str:
    """The R8 condition: with the master layer,
    a side whose id is NULL although the country is known (nothing resolved,
    or a roll-up cleared by migrate-master), or an id pointing at a place
    still to confirm. On the old location table: the text column instead."""
    from . import db

    if db.master_active(conn):
        return """(({a}.origin_loc_id IS NULL AND {a}.origin_country IS NOT NULL)
                OR ({a}.dest_loc_id IS NULL AND {a}.dest_country IS NOT NULL)
                OR {a}.origin_loc_id IN (SELECT id FROM master_location WHERE status = 'to_confirm')
                OR {a}.dest_loc_id IN (SELECT id FROM master_location WHERE status = 'to_confirm'))""".format(a=alias)
    return """(({a}.origin_loc IS NULL AND {a}.origin_country IS NOT NULL)
            OR ({a}.dest_loc IS NULL AND {a}.dest_country IS NOT NULL)
            OR {a}.origin_loc IN (SELECT key FROM location WHERE status = 'To Confirm')
            OR {a}.dest_loc IN (SELECT key FROM location WHERE status = 'To Confirm'))""".format(a=alias)


def rule_fix_location(conn, today) -> list:
    """R8: a case whose lane is a country, or a location nobody confirmed.
    v33: any live route of the case (route A is the case's own lane)."""
    from . import db
    condition = lane_needs_fixing_sql(conn)
    if db._has_table(conn, "enquiry_route"):
        condition = "({} OR EXISTS (SELECT 1 FROM enquiry_route r WHERE r.enquiry_id = e.id AND r.voided = 0 "                     "AND {}))".format(condition, lane_needs_fixing_sql(conn, "r"))
    return [Item("R8", "enquiry", row["id"], enquiry_id=row["id"],
                 label=row["opp_no"], detail=_case_detail(row), parts=_case_parts(row),
                 days=calendar_days_between(row["received_on"], today),
                 since=row["received_on"])
            for row in conn.execute("""
                SELECT e.* FROM (SELECT * FROM v_enquiry WHERE scope = 'In') e
                WHERE e.legacy = 0 AND e.status IN ('New','Quoted')
                  AND {}
                ORDER BY e.received_on DESC""".format(condition))]


def rule_mail_activity(conn, today) -> list:
    """R9: a vendor answered. A New case with any live answered vendor
    request is here however old the answer is - we hold a rate and owe a
    quote - except in a re-quote round, where only the answers to this
    round's requests count. A Quoted case only when the answer is newer
    than the last activity on it (backfill events do not count)."""
    return [Item("R9", "enquiry", row["id"], enquiry_id=row["id"],
                 label=row["opp_no"], detail=_case_detail(row), parts=_case_parts(row),
                 days=calendar_days_between(row["replied_on"], today),
                 since=row["replied_on"],
                 extra={"replied_on": row["replied_on"]})
            for row in conn.execute("""
                SELECT e.*, (SELECT MAX(v.replied_on) FROM vendor_request v
                             WHERE v.enquiry_id = e.id AND {rnd}) AS replied_on
                FROM (SELECT *, {act} AS activity_on FROM v_enquiry e WHERE scope = 'In' AND handled_by IS NULL) e
                WHERE e.legacy = 0 AND e.status IN ('New','Quoted')
                  AND EXISTS (SELECT 1 FROM vendor_request v
                              WHERE v.enquiry_id = e.id AND {rnd}
                                AND v.replied_on IS NOT NULL
                                AND (e.status = 'New'
                                     OR e.activity_on IS NULL
                                     OR v.replied_on > e.activity_on))
                ORDER BY replied_on DESC""".format(act=activity_on_sql("e"), rnd=in_round_sql("v", "e")))]


def rule_silent(conn, today) -> list:
    """R11 (L1): quoted cases whose requester has not written since our
    quote for silent_days or more (silent.py, the archive's threads)."""
    from . import silent
    items = []
    for c in silent.cached(conn, today):
        row = c["row"]
        items.append(Item("R11", "enquiry", row["id"], enquiry_id=row["id"], label=row["opp_no"],
                          detail=_case_detail(row), parts=_case_parts(row), days=c["days"], since=c["quoted"],
                          extra={"country": c["country"], "city": c["city"], "quoted": c["quoted"],
                                 "fu_count": int(row["fu_count"] or 0), "last_fu": (row["last_fu_on"] or "")[:10]}))
    return items


def rule_shipment_attention(conn, today) -> list:
    """R10: an active shipment with an open issue older than one working
    day, an ETA gone by with no arrival, or no event for five working
    days. The rule itself lives with the shipments."""
    from . import shipments, shipments_switch
    if not db._has_table(conn, "shipment") or not shipments_switch.enabled(conn):     # FX3: switched off
        return []
    items = []
    for shipment_id, enquiry_id, title, reasons, days, since in shipments.attention(conn, today):
        opp = conn.execute("SELECT opp_no, requester_key, handled_by FROM v_enquiry WHERE id = ?",
                           (enquiry_id,)).fetchone()
        if opp is not None and opp["handled_by"]:
            continue            # another office's shipment: left out of the chasing rules (v31)
        items.append(Item("R10", "shipment", shipment_id, enquiry_id=enquiry_id,
                          label=title, detail="; ".join(reasons),
                          parts=[opp["requester_key"] or "", opp["opp_no"]] + reasons if opp else reasons,
                          days=days, since=since,
                          extra={"shipment_id": shipment_id, "reasons": reasons}))
    items.sort(key=lambda i: -(i.days or 0))
    return items


#: rule -> (title, what the row is for, which row form, which band).
#:
#: The bands are not part of the rules; they come from what the rules actually
#: return. Three cases needed an answer this morning and 167 were the backlog
#: the migration brought with it, and one list of 211 rows hides the three.
#: 'today' is what came up because of this week's work; 'backlog' is the pile
#: that has been there since before the app existed and is worked through a
#: few at a time.
SECTIONS = (
    ("R1", "New, nothing done yet", "Set a status, or ask a vendor.",
     "status", "today"),
    ("R12", "Waiting for requester", "We asked them for details; chase them, or mark Info received.",
     "status", "today"),
    ("R2", "Vendor has not replied", "Chase them, or record what came back.",
     "vendor", "today"),
    ("R9", "A vendor answered", "Their reply is newer than anything on the "
     "case.", "status", "today"),
    ("R10", "Shipment needs attention", "An open issue, an ETA gone by, or "
     "nothing logged for a week.", "shipment", "today"),
    ("R3", "Quoted, follow up", "Send the next follow-up, or decide: won, lost, "
     "or still waiting?", "status", "today"),
    ("R6", "Won, whose rate?", "Record which vendor's rate was used.",
     "open", "today"),
    ("R5", "Requester still '(TBC)'", "Registered from a mail we sent out; "
     "say who it turned out to be for.", "open", "today"),
    ("R4", "Status never confirmed", "Migrated with a blank status.",
     "status", "backlog"),
    ("R8", "Lane needs fixing", "A country with no location, or one still "
     "'To Confirm'.", "open", "backlog"),
    ("R7", "Time to touch base", "Nothing has passed between us for a while.",
     "touch", "backlog"),
    ("R11", "No reply for a month", "Quoted, and the requester has not written since our quote.",
     "status", "today"),
)


#: The two rules that are the morning's real work (2026-09-15 layout): a
#: new case nobody has touched, and a quote nobody has answered. They sit
#: open at the top of the page; every other rule is folded underneath.
FOCUS_RULES = ("R1", "R3")
#: When a focus row turns red: R1 counts working days, R3 calendar days.
LATE_AFTER = {"R1": 3, "R3": 14}
#: A focus panel shows this many rows (after sorting) until 'Show all'.
FOCUS_LIMIT = 10


def is_focus(rule: str) -> bool:
    return rule in FOCUS_RULES


def focus(sections) -> list:
    """The focus sections, in FOCUS_RULES order, whether or not they have rows."""
    by_rule = {section.rule: section for section in sections}
    return [by_rule[rule] for rule in FOCUS_RULES if rule in by_rule]


def others(sections) -> list:
    """Everything that is not a focus rule, in SECTIONS order."""
    return [section for section in sections if not is_focus(section.rule)]


#: FX4 (her call of 11 Nov): the Today's work cards she does not work,
#: hidden until she unticks them in Settings
HIDDEN_SETTING = "tracking_hidden_cards"
HIDDEN_DEFAULT = "r9,decide,r2,r6"
#: the name a hidden card goes by in the line under Today's work
SHORT_TITLES = {"decide": "Decide", "replied": "Waiting on us"}


def hidden_cards(conn) -> set:
    """The keys of the Today's work cards Settings hides."""
    raw = db.get_setting(conn, HIDDEN_SETTING, None)
    raw = HIDDEN_DEFAULT if raw is None else str(raw)
    known = {d[0] for d in TODAY_CARDS}
    return {k.strip() for k in raw.split(",") if k.strip() in known}


def card_key_of(item) -> str:
    """The Today's work card a rule's row is drawn on ('' for none)."""
    if item.rule == "R3":
        step = item.extra.get("step", "")
        if step == "Replied":
            return "replied"
        if step == "Decide":
            return "decide"
        return "fu1" if step == "FU1" else "fu2"
    for key, rule, step, *_rest in TODAY_CARDS:
        if rule == item.rule and step is None:
            return key
    return ""


def focus_count(conn, today=None) -> int:
    """R1 + R3 rows, for the badge on the Tracking link. Only the two rules
    run, so every page can afford it. FX4: the rows of a card Settings
    hides are left out (the Decide step while decide is hidden)."""
    today = _today(today)
    found = rule_new_no_action(conn, today, int(db.get_setting(conn, "rule_new_days", 1))) + \
        rule_quoted_no_answer(conn, today, fu_settings(conn)) + \
        [i for i in rule_waiting(conn, today, fu_settings(conn)) if i.extra.get("step")]   # WR1: a chaser due
    asleep = _snoozed(conn, today)
    held = held_until_later(conn, today)
    hidden = hidden_cards(conn)
    return sum(1 for item in found
               if (item.rule, item.kind, item.ref_id) not in asleep
               and item.enquiry_id not in held
               and card_key_of(item) not in hidden)


def held_until_later(conn, today) -> set:
    """Cases parked with a hold_until still ahead: every rule skips them
    (v11). A hold with no date, or one that has passed, is a
    note on the case and nothing more."""
    return {r["id"] for r in conn.execute(
        "SELECT id FROM enquiry WHERE hold_reason IS NOT NULL AND hold_until IS NOT NULL "
        "AND hold_until <> '' AND hold_until > ?", (today,))}


def case_nudge(conn, row, today=None) -> dict:
    """What the tracking page would say about one open case that is not
    quoted yet - the case page's Next tile and rail: R1's
    line when the case is New, confirmed, with no live vendor request and
    past rule_new_days; 'Send the quote' as soon as a vendor of this round
    (in_round_sql) has sent a usable rate (quoted = 1, T3: we hold a rate
    and owe the quote, the silent ones can wait); R2's when a vendor has
    been silent for rule_vendor_no_reply_days (a reply without a rate
    changes nothing); otherwise 'Send the quote'. Handled by another office
    comes first. The thresholds and the working-day count are the rules'
    own; nothing is re-decided, and Tracking's cards are not touched.
    Keys: rule ('R1' / 'R2' / ''), text, hint (the section's hint), days."""
    today = _today(today)
    out = {"rule": "", "text": "Send the quote", "hint": "", "days": None}
    handled = row["handled_by"] if "handled_by" in row.keys() else None
    if handled:
        # another office handles it (v31): the home office has nothing to chase
        return {"rule": "", "text": "Handled by {}".format(handled), "hint": "", "days": None}
    if row["status"] != "New":
        return out
    hints = {s[0]: s[2] for s in SECTIONS}
    from . import wait
    found = wait.live(conn, row["id"])
    if found is not None:
        # WR1: we wait for their details - the wait's ladder says what is next
        from .dates import month_day
        rung = wait.ladder(conn, found, today)
        when = "due" if rung["step"] else (month_day(rung["next_due"]) if rung["next_due"] else "")
        return {"rule": "R12", "text": "Waiting for requester: {} {}".format(rung["next"], when).strip(),
                "hint": hints["R12"], "days": business_days_between(found.since, today)}
    vendors = conn.execute(
        "SELECT v.asked_on, v.replied_on, v.quoted, p.key AS vendor_key FROM vendor_request v "
        "JOIN party p ON p.id = v.vendor_party_id JOIN v_enquiry e ON e.id = v.enquiry_id "
        "WHERE v.enquiry_id = ? AND {} ORDER BY v.asked_on, v.id".format(in_round_sql("v", "e")),
        (row["id"],)).fetchall()
    if row["status_confirmed"] and not vendors:
        days = business_days_between(row["received_on"], today)
        if days is not None and days > int(db.get_setting(conn, "rule_new_days", 1)):
            return {"rule": "R1", "text": "Nothing done yet, {} days".format(days),
                    "hint": hints["R1"], "days": days}
    if any(v["quoted"] for v in vendors):
        # a rate of this round is in hand: the quote is ours to send (T3)
        return out
    wanted =int(db.get_setting(conn, "rule_vendor_no_reply_days", 2))
    silent = []
    for v in vendors:
        if v["replied_on"]:
            continue
        days = business_days_between(v["asked_on"], today)
        if days is not None and days >= wanted:
            silent.append((days, v["vendor_key"]))
    if silent:
        days, key = max(silent)
        return {"rule": "R2", "text": "Vendor {} no reply, {} days".format(key, days),
                "hint": hints["R2"], "days": days}
    return out


def new_cases_off_every_card(conn, today=None) -> list:
    """The invariant (2026-09-24): a New case is our turn, so every one of
    them - scope In, confirmed, not legacy, not held until a later date -
    is a row of R1, R2, R9 or R12 (WR1). The rules run here without their waiting
    days (a case that came in today is R1's tomorrow) and with snoozed
    rows counted (the row comes back when the snooze ends); what is left
    has fallen off Tracking. Unconfirmed cases are R4's, scope Out is no
    rule's. Returns the v_enquiry rows, oldest first; [] is the good news."""
    today = _today(today)
    covered = {i.enquiry_id for i in rule_new_no_action(conn, today, -10 ** 6)}
    covered |= {i.enquiry_id for i in rule_vendor_silent(conn, today, -10 ** 6)}
    covered |= {i.enquiry_id for i in rule_mail_activity(conn, today)}
    covered |= {i.enquiry_id for i in rule_waiting(conn, today)}          # WR1
    held = held_until_later(conn, today)
    return [row for row in conn.execute(
        "SELECT * FROM v_enquiry WHERE status = 'New' AND scope = 'In' AND legacy = 0 AND handled_by IS NULL "
        "AND status_confirmed = 1 ORDER BY received_on, opp_no")
        if row["id"] not in covered and row["id"] not in held]


def run(conn: sqlite3.Connection, today=None) -> list:
    """Every section, in rule order. Reads only."""
    today = _today(today)
    settings = {
        "new": int(db.get_setting(conn, "rule_new_days", 1)),
        "vendor": int(db.get_setting(conn, "rule_vendor_no_reply_days", 2)),
        "cadence": int(db.get_setting(conn, "cadence_days_default", 45)),
    }
    found = {
        "R1": rule_new_no_action(conn, today, settings["new"]),
        "R2": rule_vendor_silent(conn, today, settings["vendor"]),
        "R3": rule_quoted_no_answer(conn, today, fu_settings(conn)),
        "R4": rule_confirm_status(conn, today),
        "R5": rule_identify_requester(conn, today),
        "R6": rule_record_who_won(conn, today),
        "R7": rule_touch_base(conn, today, settings["cadence"]),
        "R8": rule_fix_location(conn, today),
        "R9": rule_mail_activity(conn, today),
        "R10": rule_shipment_attention(conn, today),
        "R11": rule_silent(conn, today),
        "R12": rule_waiting(conn, today, fu_settings(conn)),
    }
    asleep = _snoozed(conn, today)
    sections = []
    for rule, title, hint, action, band in SECTIONS:
        items = [item for item in found[rule]
                 if (rule, item.kind, item.ref_id) not in asleep]
        sections.append(Section(rule, title, hint, action, band, items))
    held = held_until_later(conn, today)
    if held:
        for section in sections:
            section.items = [item for item in section.items
                             if item.enquiry_id not in held]
    for section in sections:
        limit = LATE_AFTER.get(section.rule)
        if limit is not None:
            for item in section.items:
                item.late = item.days is not None and item.days >= limit
    return sections


def band(sections, name: str) -> list:
    return [section for section in sections if section.band == name]


def total(sections) -> int:
    return sum(section.count for section in sections)


# ------------------------------------------------------------------ snooze --
SNOOZE_CHOICES = (3, 7, 14)


def snooze(repo, rule: str, kind: str, ref_id: int, until: str) -> None:
    """Put one item away until a date. Replaces any earlier snooze on it."""
    row = repo.conn.execute(
        "SELECT id FROM tracking_snooze WHERE rule = ? AND ref_kind = ? "
        "AND ref_id = ?", (rule, kind, ref_id)).fetchone()
    if row is None:
        repo.insert("tracking_snooze", {
            "rule": rule, "ref_kind": kind, "ref_id": ref_id, "until": until})
    else:
        repo.update("tracking_snooze", row["id"], {"until": until})


def snooze_until(days: int, today=None) -> str:
    """A date `days` working days ahead? No: plain days, as the buttons say."""
    start = _date(_today(today))
    return (start + dt.timedelta(days=max(int(days), 1))).isoformat()


def render(sections, limit: int = 8) -> list:
    """Plain text for the CLI, the same list the page shows."""
    lines = []
    rest = others(sections)
    for name, heading in (("focus", "-- focus: R1 and R3 --"),
                          ("today", "-- needs an answer --"),
                          ("backlog", "-- backlog, a few at a time --")):
        chosen = [s for s in (focus(sections) if name == "focus" else band(rest, name))
                  if s.count]
        if chosen:
            lines.append(heading)
        for section in chosen:
            lines.append("[{}] {:<28} {}".format(
                section.rule, section.title, section.count))
            for item in section.items[:limit]:
                lines.append("      {:<10} {:<52} {}".format(
                    item.label, item.detail[:52],
                    "{} days".format(item.days) if item.days is not None else ""))
            if section.count > limit:
                lines.append("      ... and {} more".format(section.count - limit))
    if not lines:
        lines.append("Nothing to chase today.")
    else:
        lines.append("")
        lines.append("{} item(s) in {} section(s)".format(
            total(sections), sum(1 for s in sections if s.count)))
    return lines


# ============================================================ page model ====
# The Tracking page (2026-09-24): what the template needs,
# computed once from run()'s output. run() and the rules are untouched;
# this only regroups their items (R3 by step), counts them for the tiles
# and the two totals, and adds what each row shows - the step pill, the
# last activity, the filter attributes. The pending suggestions are not
# here (T1, 2026-10-02): she works them on the case page.

#: The cards of "Today's work", in order: (key, rule, R3 step or None,
#: title, hint, colour, folded, always shown). FU steps past FU2 (fu_max
#: raised) go with FU2. T1 (2026-10-02): the first three are what she works
#: through each morning - the first chasers, the untouched new inquiries,
#: the second chasers - and the rest keep their order.
TODAY_CARDS = (
    ("fu1", "R3", "FU1", "FU1 due", "send the first chaser, then Log as sent", "navy", False, True),
    ("r1", "R1", None, "New, nothing done yet", "set a status, or ask a vendor", "navy", False, True),
    # WR1: right after R1 - the New cases that wait for the requester's details
    ("wait", "R12", None, "Waiting for requester", "chase them for what we need; Info received puts the case "
     "back to work", "navy", False, False),
    ("fu2", "R3", "FU2", "FU2 due", "second chaser", "navy", False, True),
    # FX4: logged by hand only (the step keeps its key)
    ("replied", "R3", "Replied", "Waiting on us",
     "logged on the case page or on the team site; Answered clears it", "rose", False, True),
    ("decide", "R3", "Decide", "Decide, no answer after the follow-ups",
     "tick the dead ones and mark them Lost in one go", "amber", False, True),
    # L1: the month-end review, by where the agent is; its title carries silent_days
    ("silent", "R11", None, "No reply for {days}+ days",
     "by the requester's place: Lost, Cancelled, or Keep for another round", "amber", True, False),
    ("r9", "R9", None, "A vendor answered", "their reply is newer than anything on the case", "green", False, True),
    ("r10", "R10", None, "Shipment needs attention", "an open issue, an ETA gone by, or nothing logged for a week",
     "accent", False, False),
    ("r2", "R2", None, "Vendor has not replied", "chase them, or record what came back", "grey", True, False),
    ("r6", "R6", None, "Won, whose rate?", "record which vendor's rate was used", "green", True, False),
    ("r5", "R5", None, "Requester still (TBC)", "registered from a mail we sent out; say who it turned out to be for",
     "grey", True, False),
)
BACKLOG_CARDS = (
    ("r4", "R4", None, "Status never confirmed", "migrated with a blank status", "grey", True, False),
    ("r8", "R8", None, "Lane needs fixing", "a country with no location, or one still To Confirm", "grey", True, False),
    ("r7", "R7", None, "Time to touch base", "nothing has passed between us for a while", "grey", True, False),
)
#: The KPI tiles: (card key, label, hint, colour); Shipments is added when R10 has rows.
TILES = (
    ("fu1", "FU1 due", "first chaser, 1 working day after the quote", "navy"),
    ("r1", "New, untouched", "set a status or ask a vendor", "navy"),
    ("fu2", "FU2 due", "second chaser, 5 working days after FU1", "navy"),
    ("replied", "Waiting on us", "logged on the case page or the team site", "rose"),
    ("decide", "Decide", "no answer after the chasers", "amber"),
    ("r9", "Vendor answered", "their reply is newer than the case", "green"),
)
OCEAN_MODES = ("FCL", "LCL", "RORO", "Break Bulk", "Breakbulk", "Project")
GIST_LEN = 90
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")
_SIGNOFF = re.compile(r"\b(best regards|kind regards|warm regards|regards|rgds|brgds|saludos|atte\.?|atentamente|"
                      r"cordialmente|sincerely|thanks & regards|thank you & regards|sent from my)\b.*$", re.I | re.S)


def activity_gist(text, limit: int = GIST_LEN) -> str:
    """The words of a reply for the Last activity column: no Message-ID,
    no URLs, no emoji, cut before the sign-off, one line, `limit` long."""
    text = re.sub(r"\s*\[mid:[^\]]*\]", "", text or "")
    found = _GIST.match(text.strip())
    who = ""
    if found:
        who = found.group("who").strip()
        text = found.group("body")
    text = _URL.sub("", text)
    text = _EMOJI.sub("", text)
    text = _SIGNOFF.sub("", text)
    text = " ".join(text.split()).strip(" -:;,")
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return "{}: {}".format(who, text) if who and text else text


def _day_label(value) -> str:
    from .dates import month_day
    d = _date(value)
    return month_day(d) if d else ""


def last_activity(conn, enquiry_id: int) -> dict:
    """The newest live event of a case for the row, not counting the ones
    the system wrote (system_event_sql): {text, quote}. A
    status event reads 'Quoted 4 Sep' (' (mail)' when a suggestion set
    it), a follow-up 'FU1 sent 26 Aug', the other side's reply 'Reply 25
    Aug' with the gist in quotes, any other note 'Note 10 Sep: ...'."""
    row = conn.execute(
        "SELECT kind, status, at, note, seq, source FROM status_event s WHERE enquiry_id = ? AND voided = 0 "
        "AND NOT {} ORDER BY at DESC, id DESC LIMIT 1".format(system_event_sql("s")), (enquiry_id,)).fetchone()
    if row is None:
        return {"text": "", "quote": ""}
    when = _day_label(row["at"])
    if row["kind"] == "followup":
        return {"text": "FU{} sent {}".format(row["seq"] or "", when), "quote": ""}
    if row["kind"] == "note":
        note = row["note"] or ""
        if note.lower().startswith("reply from"):
            return {"text": "Reply {}".format(when), "quote": activity_gist(note)}
        return {"text": "Note {}: {}".format(when, activity_gist(note)), "quote": ""}
    label = "re-quote" if row["kind"] == "requote" else (row["status"] or "")
    return {"text": "{} {}{}".format(label, when, " (mail)" if row["source"] == "mail" else ""), "quote": ""}


def _status_pill(status) -> tuple:
    return (status or "?", "s-" + (status or "").lower())


def _step_cell(item, rule, card_key, case) -> tuple:
    """(pill text, pill class, sub-line) for the Step column."""
    extra = item.extra or {}
    status = case["status"] if case is not None else ""
    if rule == "R3":
        step = extra.get("step", "")
        sent = int(extra.get("fu_count") or 0)
        sent_text = "none sent" if sent == 0 else "{} sent".format(sent)
        if step == "Replied":
            return "Waiting on us", "p-rose", ""
        if step == "Decide":
            return "Decide", "p-amber", sent_text
        return "{} due".format(step), "p-navy", sent_text
    if rule == "R12":
        # WR1: the wait's step, and what we asked them for
        step = extra.get("step", "")
        what = extra.get("what") or ""
        line = what if len(what) <= 90 else what[:90].rstrip() + "..."
        if step == "Decide":
            return "Decide", "p-amber", line
        if step:
            return "{} due".format(step), "p-navy", line
        return "{} {}".format(extra.get("next") or "FU1", _day_label(extra.get("next_due"))), "p-grey", line
    if rule == "R2":
        text, cls = _status_pill(status)
        return text, cls, "{} · no reply".format(extra.get("vendor_key") or "vendor")
    if rule == "R9":
        text, cls = _status_pill(status)
        return text, cls, "vendor replied {}".format(_day_label(extra.get("replied_on")))
    if rule == "R4":
        text, cls = _status_pill(status)
        return text, cls, "unconfirmed"
    if rule == "R5":
        text, cls = _status_pill(status)
        return text, cls, "requester (TBC)"
    if rule == "R7":
        return "quiet", "p-grey", "cadence {} d".format(extra.get("cadence") or "?")
    if rule == "R10":
        return "shipment", "p-accent", "; ".join(extra.get("reasons") or [])
    if rule == "R11":
        sent = int(extra.get("fu_count") or 0)
        return ("quoted {}".format(_day_label(extra.get("quoted"))), "p-amber",
                "{} FU{}".format(sent, ", last {}".format(_day_label(extra.get("last_fu"))) if sent and extra.get("last_fu")
                                  else "") if sent else "no FU sent")
    return _status_pill(status) + ("",)


def _row(conn, item, card, today) -> dict:
    """One table row: what the template prints and the filters read."""
    key, rule = card["key"], card["rule"]
    extra = item.extra or {}
    case = conn.execute(
        "SELECT id, opp_no, status, scope, entry_id, requester_key, requester_type, latam, service_mode, "
        "commodity, origin_loc, dest_loc, origin_country, dest_country FROM v_enquiry WHERE id = ?",
        (item.enquiry_id,)).fetchone() if item.enquiry_id else None
    pill, pill_cls, sub = _step_cell(item, rule, key, case)
    if rule == "R7":
        party = conn.execute("SELECT type, country, region, latam, total_enquiries, won FROM v_party_stats WHERE id = ?",
                             (item.party_id,)).fetchone() if item.party_id else None
        latam = bool(party and party["latam"] == "Y")          # v36: the Settings list
        requester = item.label
        lane = "{} · {} · {} inquiries · {} won".format(
            party["type"], party["country"] or "?", party["total_enquiries"], party["won"] or 0) if party else item.detail
        last = {"text": "", "quote": ""}
        requester_type = party["type"] if party else ""
        mode = ""
    else:
        requester = (case["requester_key"] if case is not None else "") or ""
        latam = bool(case is not None and case["latam"] == "Y")
        mode = (case["service_mode"] if case is not None else "") or ""
        requester_type = (case["requester_type"] if case is not None else "") or ""
        parts = list(item.parts or [])
        lane = " · ".join(p for p in parts[1:] if p) if parts else item.detail
        if rule == "R2":
            last = {"text": "asked {} {}".format(extra.get("vendor_key") or "", _day_label(item.since)), "quote": ""}
        elif rule == "R10":
            last = {"text": "; ".join(extra.get("reasons") or []), "quote": ""}
        elif item.enquiry_id:
            last = last_activity(conn, item.enquiry_id)
        else:
            last = {"text": "", "quote": ""}
    if rule == "R3" and extra.get("step") == "Replied" and extra.get("reply_note_full"):
        last = {"text": "Reply {}".format(_day_label(item.since)), "quote": activity_gist(extra["reply_note_full"])}
    return {
        "item": item, "key": key, "rule": rule, "ref_id": item.ref_id, "kind": item.kind,
        "enquiry_id": item.enquiry_id, "party_id": item.party_id,
        "opp": item.label if rule != "R7" else "", "label": item.label,
        "shipment_id": extra.get("shipment_id"),
        "requester": requester, "latam": latam, "lane": lane,
        "pill": pill, "pill_cls": pill_cls, "sub": sub,
        "days": item.days, "since": item.since, "since_label": _day_label(item.since), "late": item.late,
        "last": last["text"], "quote": last["quote"],
        "status": case["status"] if case is not None else "", "scope": case["scope"] if case is not None else "In",
        "entry_id": case["entry_id"] if case is not None else "",
        "mode": mode, "requester_type": requester_type,
        "ocean": mode in OCEAN_MODES, "air": mode in ("Air", "Sea-Air"),
        "office": requester_type == "Office",
        # L2: the Lost reason a Decide or R11 row offers first, and the batch uses
        "no_response": _no_response(requester_type),
        "step": extra.get("step", ""), "next_seq": extra.get("next_seq"),
        "what": extra.get("what", ""),
        # WR1: an R1 row's Waiting for requester box starts with what the acknowledgment would ask
        "wait_prefill": _wait_prefill(conn, item.enquiry_id) if rule == "R1" and item.enquiry_id else "",
        # MF1: an R5 row's Who asked? candidates, as the case page shows them
        "who": _who_asked(conn, item.enquiry_id) if rule == "R5" and item.enquiry_id else [],
        # FX7: a follow-up the scan found, waiting in Suggested updates (FU1, FU2, Decide, a wait's rows)
        "found_fu": _found_fu(conn, item.enquiry_id)
        if item.enquiry_id and (rule == "R12" or (rule == "R3" and extra.get("step") != "Replied")) else None,
        "search":" ".join(filter(None, [item.label, requester, lane])).casefold(),
    }


def _found_fu(conn, enquiry_id):
    from . import suggestions
    return suggestions.pending_followup(conn, enquiry_id)


def _wait_prefill(conn, enquiry_id) -> str:
    from . import wait
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
    return wait.prefill(conn, row) if row is not None else ""


def _who_asked(conn, enquiry_id) -> list:
    from . import who_asked
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
    return who_asked.candidates(conn, row)


def _no_response(requester_type) -> str:
    from . import status
    return status.no_response_reason(requester_type)


def _card(defn, items, conn, today) -> dict:
    key, rule, step, title, hint, colour, folded, always = defn
    card = {"key": key, "rule": rule, "step": step, "title": title, "hint": hint, "colour": colour,
            "folded": folded, "always": always, "batch": key == "decide", "party": rule == "R7"}
    rows = [_row(conn, item, card, today) for item in items]
    rows.sort(key=lambda r: -(r["days"] if r["days"] is not None else -1))
    card["rows"] = rows
    card["count"] = len(rows)
    return card


def _group_by_place(card, conn) -> None:
    """L1: the silent card's rows grouped by the requester's country, then
    city ('no city' last); each row carries its group's label, the groups
    their counts; the title carries silent_days."""
    from . import silent
    card["title"] = card["title"].format(days=silent.days_setting(conn))

    def place(row):
        extra = row["item"].extra or {}
        return (extra.get("country") or "Country unknown", extra.get("city") or "")

    card["rows"].sort(key=lambda r: (place(r)[0] == "Country unknown", place(r)[0], place(r)[1] == "",
                                     place(r)[1], -(r["days"] or 0), r["opp"]))
    groups = []
    for row in card["rows"]:
        country, city = place(row)
        label = "{} \u00b7 {}".format(country, city or "no city")
        if not groups or groups[-1]["label"] != label:
            groups.append({"label": label, "count": 0})
        groups[-1]["count"] += 1
        row["group"] = label
    card["groups"] = groups
    card["grouped"] = True


def page_model(conn, today=None, sections=None) -> dict:
    """Everything the Tracking page shows, from run()'s sections."""
    today = _today(today)
    sections = sections if sections is not None else run(conn, today)
    by_rule = {section.rule: section for section in sections}
    r3 = by_rule.get("R3")
    by_step = {"Replied": [], "FU1": [], "FU2": [], "Decide": []}
    if r3 is not None:
        for item in r3.items:
            step = item.extra.get("step", "")
            if step in by_step:
                by_step[step].append(item)
            elif step.startswith("FU"):
                by_step["FU2"].append(item)
            else:
                by_step["Decide"].append(item)

    def items_of(defn):
        key, rule, step = defn[0], defn[1], defn[2]
        if rule == "R3":
            return by_step.get(step, [])
        return by_rule[rule].items if rule in by_rule else []

    today_cards, hidden_rows = [], []
    hidden = hidden_cards(conn)
    for defn in TODAY_CARDS:
        if defn[0] in hidden:
            # FX4: drawn in no view, nor its tile, nor counted; the line
            # under Today's work names it when it has rows
            count = len(items_of(defn))
            if count:
                hidden_rows.append({"key": defn[0], "title": SHORT_TITLES.get(defn[0], defn[3]), "count": count})
            continue
        card = _card(defn, items_of(defn), conn, today)
        if card["count"] or card["always"]:
            today_cards.append(card)
    backlog_cards = [c for c in (_card(d, items_of(d), conn, today) for d in BACKLOG_CARDS) if c["count"]]
    for card in today_cards:
        if card["key"] == "silent":
            _group_by_place(card, conn)
    counts = {c["key"]: c["count"] for c in today_cards + backlog_cards}
    tiles = [{"key": k, "label": label, "hint": hint, "colour": colour, "count": counts.get(k, 0)}
             for k, label, hint, colour in TILES if k not in hidden]
    if counts.get("r10"):
        tiles.append({"key": "r10", "label": "Shipments", "hint": "an issue, a passed ETA or a quiet week",
                      "colour": "accent", "count": counts["r10"]})
    if counts.get("wait"):
        tiles.insert(2, {"key": "wait", "label": "Waiting for requester", "hint": "we asked them for details",
                         "colour": "navy", "count": counts["wait"]})
    if counts.get("silent"):
        from . import silent
        tiles.append({"key": "silent", "label": "No reply {}+ days".format(silent.days_setting(conn)),
                      "hint": "the month-end review, by place", "colour": "amber", "count": counts["silent"]})
    return {
        "today": today,
        "tiles": tiles,
        "today_cards": today_cards,
        "backlog_cards": backlog_cards,
        "today_total": sum(c["count"] for c in today_cards),
        "hidden": hidden_rows,
        "backlog_total": sum(c["count"] for c in backlog_cards),
        "sections": sections,
        "total": total(sections),
    }


# ------------------------------------------ the Enquiries list's Last event --
#: The label's tone for each kind of action (2026-09-25).
ACTION_TONES = {"New": "grey", "Dropped": "grey", "Cancelled": "grey", "Quoted": "accent", "Re-quoted": "accent",
                "Won": "green", "Lost": "rose", "Sourcing": "amber", "Rate request": "amber",
                "Note": "grey"}


def our_last_actions(conn) -> dict:
    """{enquiry_id: {label, at, tone}}: our own last action on each case,
    in one pass - the newest live status change (a re-quote and a close
    included), follow-up sent, rate request sent, or note typed by hand.
    The requester's replies ('Reply from ...' notes), vendors' answers and
    the events a backfill wrote do not count. On one day a status change
    comes before a follow-up, a follow-up before a rate request, a rate
    request before a note."""
    out = {}
    rows = conn.execute(
        "SELECT enquiry_id, at, 3 AS prio, id,"
        " CASE WHEN kind = 'requote' THEN 'Re-quoted' ELSE status END AS label"
        " FROM status_event WHERE voided = 0 AND kind IN ('status','requote') AND NOT {sys}"
        " UNION ALL SELECT enquiry_id, at, 2, id, 'FU' || COALESCE(seq, '')"
        " FROM status_event WHERE voided = 0 AND kind = 'followup'"
        " UNION ALL SELECT enquiry_id, at, 1, id, 'Acknowledged'"           # AL1: our acknowledgement
        " FROM status_event WHERE voided = 0 AND kind = 'ack'"
        " UNION ALL SELECT enquiry_id, asked_on, 1, id, 'Rate request'"
        " FROM vendor_request WHERE voided = 0 AND asked_on IS NOT NULL AND asked_on <> ''"
        " UNION ALL SELECT enquiry_id, at, 0, id, 'Note'"
        " FROM status_event WHERE voided = 0 AND kind = 'note' AND source = 'manual'"
        " AND lower(COALESCE(note, '')) NOT LIKE 'reply from%'"
        " ORDER BY enquiry_id, at DESC, prio DESC, id DESC".format(sys=system_event_sql("status_event")))
    for row in rows:
        if row["enquiry_id"] in out:
            continue
        label = row["label"] or ""
        tone = "navy" if label.startswith("FU") else ACTION_TONES.get(label, "grey")
        out[row["enquiry_id"]] = {"label": label, "at": row["at"], "tone": tone}
    return out
