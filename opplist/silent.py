"""Quoted cases with no reply for a month (batch L1).

A JJC case is silent when its status is Quoted, the requester is not a
direct customer, no mail from the requester's side is on the case's agent
thread dated after our quote mail (the quote date from the Quoted event
when the archive holds no mail of ours), and at least silent_days days
have passed since the quote date (L1b: a follow-up of ours does not move
it); a case on hold with an Until date still ahead is not silent.
Tracking's R11 lists them and the Enquiries page has them as a view (the
month report's section 12 of L1 went in 17a-2).

A case closed Lost with a No response reason (customer; since L2 also
agent and other office / sales) in the last
REOPEN_DAYS days is still read by the morning scan: a requester mail after
the closing becomes the suggestion REOPEN_NOTE (proposed Quoted).
"""

from __future__ import annotations

import datetime as dt

from . import db

SETTING = "silent_days"
DEFAULT_DAYS = 30
NO_RESPONSE = "No response from customer"
#: L2: the Decide card closes an agent's case 'No response from agent' and
#: an office's 'No response from other office / sales' - all three reopen
NO_RESPONSE_ALL = ("No response from customer", "No response from agent",
                   "No response from other office / sales")
REOPEN_DAYS = 90
REOPEN_NOTE = "They wrote after the case was closed: reopen as Quoted?"


def days_setting(conn) -> int:
    try:
        return max(int(str(db.get_setting(conn, SETTING, DEFAULT_DAYS)).strip()), 1)
    except (TypeError, ValueError):
        return DEFAULT_DAYS


def _day(value):
    try:
        return dt.date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _stamp(mail) -> str:
    from . import detect_followups
    return detect_followups._sent_stamp(mail)


def reading(conn, search, row, internal) -> dict:
    """What the thread says about a quoted case: {'quoted' (the quote
    date), 'quote_stamp' (our quote mail, else the quote date), 'reply_days'
    (the days of their mails after our quote mail)}."""
    from . import detect_followups
    quoted = (row["quoted_on"] or row["quoted_on_any"] or "")[:10]
    thread = detect_followups.thread_of(search, row, conn) if search is not None else []
    domains = detect_followups.requester_domains(conn, search, row) if search is not None else set()
    ours, theirs = detect_followups._sides(row, internal, domains)
    mine = [m for m in thread if ours(m) and quoted and _stamp(m)[:10] >= quoted]
    quote_stamp = _stamp(mine[0]) if mine else (quoted + " 00:00" if quoted else "")
    replies = [_stamp(m)[:10] for m in thread if theirs(m) and quote_stamp and _stamp(m) > quote_stamp]
    return {"quoted": quoted, "quote_stamp": quote_stamp, "reply_days": replies}


def _cases(conn, today):
    from . import tracking
    held = tracking.held_until_later(conn, today)
    for row in conn.execute(
            "SELECT e.*, p.country AS party_country, p.city AS party_city FROM v_enquiry e "
            "JOIN party p ON p.id = e.requester_party_id WHERE e.status = 'Quoted' AND e.scope = 'In' "
            "AND e.legacy = 0 AND e.handled_by IS NULL AND COALESCE(e.requester_type, '') <> 'Direct Customer' "
            "ORDER BY e.opp_no").fetchall():
        if row["id"] not in held:
            yield row


# ---------------------------------------------- each case's reading, kept --
#: L1b: {case id: (key, reading)}, like F2b's fu_reply_cache. The key holds
#: what reading() depends on: the case's updated_at, the archive's last
#: sync, and - read fresh from the row each time, since not every write
#: that changes them touches updated_at - the quote date, the requester and
#: the thread's identities; plus the newest change to the attached threads,
#: the domains and the contacts (add_thread and the party edits do not
#: touch the case), and the internal domains (a setting).
_READINGS = {}


def _aux_version(conn) -> int:
    row = conn.execute("SELECT MAX(id) FROM change_log WHERE table_name IN "
                       "('enquiry_thread', 'party_domain', 'contact', 'party')").fetchone()
    return row[0] or 0


def _reading_key(row, last_sync, aux, internal) -> tuple:
    return (row["updated_at"], last_sync, (row["quoted_on"] or row["quoted_on_any"] or "")[:10],
            row["requester_party_id"], row["entry_id"], row["conversation_id"], row["thread_key"],
            row["source_subject"], aux, tuple(sorted(internal)))


def silent_cases(conn, search, today=None, days=None, cache=None, last_sync="") -> list:
    """[{'row', 'quoted', 'days', 'country', 'city'}] - the silent cases
    today: no requester mail after our quote mail, and silent_days or more
    since the quote date (L1b: a follow-up of ours does not move it). With
    `cache` each case's reading is kept and read again only when its key
    changed; `search` may then be a callable that opens the archive when a
    case has to be read."""
    from . import detect_followups
    today = (today if isinstance(today, str) else (today or dt.date.today()).isoformat())[:10]
    days = days or days_setting(conn)
    internal = detect_followups.internal_domains(conn)
    aux = _aux_version(conn) if cache is not None else 0
    out, seen = [], set()
    for row in _cases(conn, today):
        seen.add(row["id"])
        read = None
        if cache is not None:
            key = _reading_key(row, last_sync, aux, internal)
            held = cache.get(row["id"])
            if held is not None and held[0] == key:
                read = held[1]
        if read is None:
            source = search() if callable(search) else search
            read = reading(conn, source, row, internal)
            if cache is not None:
                cache[row["id"]] = (_reading_key(row, last_sync, aux, internal), read)
        if read["reply_days"] or not read["quoted"]:
            continue
        quiet = (_day(today) - _day(read["quoted"])).days
        if quiet < days:
            continue
        out.append({"row": row, "quoted": read["quoted"], "days": quiet,
                    "country": (row["party_country"] or "").strip() if (row["party_country"] or "") != "Unknown" else "",
                    "city": (row["party_city"] or "").strip()})
    if cache is not None:
        for stale in [k for k in cache if k not in seen]:
            cache.pop(stale, None)
    return out


def cached(conn, today=None, days=None) -> list:
    """silent_cases with each case's reading kept (_READINGS); the archive
    is opened only when a case has to be read. With no archive at all
    nothing is listed: whether they replied cannot be known."""
    from . import mail_archive
    info = mail_archive.status()
    if not info.get("exists"):
        return []
    opened = []

    def search():
        if not opened:
            try:
                handle = mail_archive.ArchiveSearch()
                handle.__enter__()
            except mail_archive.ArchiveError:
                handle = None
            opened.append(handle)
        return opened[0]

    try:
        return silent_cases(conn, search, today, days, cache=_READINGS, last_sync=info.get("last_sync_at") or "")
    finally:
        if opened and opened[0] is not None:
            opened[0].__exit__(None, None, None)


def silent_ids(conn, today=None) -> list:
    return [c["row"]["id"] for c in cached(conn, today)]


# ------------------------------------------------------------- reopen --
def reopen_candidates(conn, today=None) -> list:
    today = (today if isinstance(today, str) else (today or dt.date.today()).isoformat())[:10]
    since = (_day(today) - dt.timedelta(days=REOPEN_DAYS)).isoformat()
    out = []
    for row in conn.execute("SELECT * FROM v_enquiry WHERE status = 'Lost' AND closed_reason IN (?, ?, ?) "
                            "AND legacy = 0", NO_RESPONSE_ALL).fetchall():
        closed = conn.execute("SELECT MAX(at) FROM status_event WHERE enquiry_id = ? AND kind = 'status' "
                              "AND status = 'Lost' AND voided = 0", (row["id"],)).fetchone()[0]
        if closed and closed[:10] >= since:
            out.append((row, closed[:10]))
    return out


def reopen_suggestions(conn, search, today=None, repo=None) -> list:
    """For each case closed 'No response from customer' in the last
    REOPEN_DAYS days, the requester's newest mail after the closing day
    becomes REOPEN_NOTE (kind status, proposed Quoted); with `repo` it is
    recorded through suggestions.record. Returns the lines for the run."""
    from . import detect_followups, suggestions
    internal = detect_followups.internal_domains(conn)
    lines = []
    for row, closed in reopen_candidates(conn, today):
        thread = detect_followups.thread_of(search, row, conn)
        domains = detect_followups.requester_domains(conn, search, row)
        _ours, theirs = detect_followups._sides(row, internal, domains)
        later = [m for m in thread if theirs(m) and _stamp(m)[:10] > closed]
        if not later:
            continue
        mail = later[-1]
        from . import mail_archive
        quote = " ".join(mail_archive.top_part(mail.body_excerpt or "").split())[:400]
        values = {"kind": "status", "proposed_status": "Quoted", "proposed_date": _stamp(mail)[:10],
                  "evidence_sender": mail.sender_name or mail.sender_address, "evidence_date": _stamp(mail),
                  "evidence_quote": quote, "confidence": "medium", "note": REOPEN_NOTE, "source": "scan",
                  "entry_id": getattr(mail, "entry_id", "") or None,
                  "message_id": (getattr(mail, "message_id", "") or "").strip() or None}
        made = suggestions.record(repo, row["id"], row["opp_no"], values) if repo is not None else None
        lines.append("{} closed {}, they wrote {}{}".format(row["opp_no"], closed, _stamp(mail)[:10],
                                                           "" if repo is None else (" - suggested" if made else " - already there")))
    if repo is not None:
        repo.commit()
    return lines
