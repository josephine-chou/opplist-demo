"""What the mail suggests for a case, waiting for a yes or a no.

Two things fill the `suggestion` table: the scheduled `propose-status
--open --write-suggestions` run (source 'scan') and 'Update from Outlook'
on the case page (source 'mail', one selected mail and what followed it in
its conversation). W2b (2026-10-29): a Won or Lost a colleague sent on the
team site is one too (source 'web', written by site_close; Won's rate used
comes from the vendor they named). FX5 (2026-11-13): a later mail from the
other side saying the cargo cannot be stacked suggests the Non-stackable
tag, from both (kind 'tag' since v50, D7a). FX7 (v58): a chaser of ours
the morning scan found is kind 'followup'; Accept writes the follow-up.

Nothing here changes a case until `accept` is called,
and that goes through the same repository calls the case page uses, with
source 'mail' on the row it writes. A dismissed suggestion is remembered:
the same mail is not suggested again.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import dataclass, field

from . import db, firebase_pull, intake, matching, parsing, propose, site_close

KINDS = ("status", "vendor", "note", "shipment_milestone", "shipment_issue", "shipment_note", "case_number",
         "tag", "edit", "followup")
SHIPMENT_KINDS = ("shipment_milestone", "shipment_issue", "shipment_note")
STATES = ("pending", "accepted", "dismissed")


# ----------------------------------------------------------- recording --
def _same(conn, enquiry_id, values) -> bool:
    """A suggestion saying the same thing is already there, whatever its
    state: the scan says it again tomorrow, the answer stands."""
    return conn.execute(
        "SELECT 1 FROM suggestion WHERE enquiry_id = ? AND kind = ? "
        "AND COALESCE(proposed_status, '') = ? AND COALESCE(vendor_key, '') = ? "
        "AND COALESCE(evidence_date, '') = ? AND COALESCE(evidence_quote, '') = ?",
        (enquiry_id, values["kind"], values.get("proposed_status") or "",
         values.get("vendor_key") or "", values.get("evidence_date") or "",
         values.get("evidence_quote") or "")).fetchone() is not None


def _dismissed_mail(conn, entry_id, message_id=None) -> bool:
    if entry_id and conn.execute(
            "SELECT 1 FROM suggestion WHERE entry_id = ? AND state = 'dismissed'",
            (entry_id,)).fetchone() is not None:
        return True
    if message_id and conn.execute(
            "SELECT 1 FROM suggestion WHERE message_id = ? AND state = 'dismissed'",
            (message_id,)).fetchone() is not None:
        return True
    return False


def _blocking(conn, enquiry_id, values):
    """FX6: the row that keeps `values` from being suggested again - the
    newest dismissed suggestion of the same mail, else the newest one saying
    the same thing - or None."""
    for column in ("entry_id", "message_id"):
        if values.get(column):
            row = conn.execute("SELECT * FROM suggestion WHERE {} = ? AND state = 'dismissed' "
                               "ORDER BY COALESCE(decided_at, created_at) DESC, id DESC".format(column),
                               (values[column],)).fetchone()
            if row is not None:
                return row
    return conn.execute(
        "SELECT * FROM suggestion WHERE enquiry_id = ? AND kind = ? "
        "AND COALESCE(proposed_status, '') = ? AND COALESCE(vendor_key, '') = ? "
        "AND COALESCE(evidence_date, '') = ? AND COALESCE(evidence_quote, '') = ? "
        "ORDER BY COALESCE(decided_at, created_at) DESC, id DESC",
        (enquiry_id, values["kind"], values.get("proposed_status") or "",
         values.get("vendor_key") or "", values.get("evidence_date") or "",
         values.get("evidence_quote") or "")).fetchone()


def _day(text) -> str:
    """'2026-11-13...' -> '13 Nov'."""
    import datetime as dt
    try:
        return dt.date.fromisoformat(str(text or "")[:10]).strftime("%d %b").lstrip("0")
    except ValueError:
        return str(text or "")[:10]


def nothing_new(blocked) -> str:
    """FX6: Update from Outlook's 'Nothing new', naming what stopped it -
    the newest of the rows that did: its kind and status or note, its
    date, when it was dismissed (or that it is waiting, or was accepted),
    its quote cut at 60."""
    plain = "Nothing new to suggest from that mail (already suggested, or dismissed before)."
    rows = [r for r in blocked or [] if r is not None]
    if not rows:
        return plain
    row = max(rows, key=lambda r: (r["decided_at"] or r["created_at"] or "", r["id"]))
    if row["kind"] == "status":
        what = row["proposed_status"] or "A status"
    elif row["kind"] == "vendor":
        what = "Vendor {}".format(row["vendor_key"] or "")
    elif row["kind"] == "tag":
        what = row["note"] or "A tag"
    else:
        what = "A {}".format(row["kind"])
    when = row["proposed_date"] or row["evidence_date"]
    state = {"dismissed": "was dismissed on {}".format(_day(row["decided_at"] or row["created_at"])),
             "accepted": "was accepted on {}".format(_day(row["decided_at"] or row["created_at"])),
             "pending": "is already waiting below"}.get(row["state"], row["state"])
    quote = " ".join((row["evidence_quote"] or row["note"] or "").split())
    if len(quote) > 60:
        quote = quote[:57].rstrip() + "..."
    return "Nothing new: {}{} from that thread {}{}. If that reading was wrong, record it on the case page.".format(
        what, " on {}".format(_day(when)) if when else "", state, " ('{}')".format(quote) if quote else "")


def record(repo, enquiry_id: int, opp_no: str, values: dict, blocked: list | None = None) -> int | None:
    """One pending suggestion, unless the same one exists or the mail it
    came from was dismissed. Returns the row id, or None when skipped
    (FX6: `blocked` gets the row that stopped it)."""
    conn = repo.conn
    if _dismissed_mail(conn, values.get("entry_id"), values.get("message_id")) or _same(conn, enquiry_id, values):
        if blocked is not None:
            blocked.append(_blocking(conn, enquiry_id, values))
        return None
    row = {"enquiry_id": enquiry_id, "opp_no": opp_no, "state": "pending"}
    row.update({k: v for k, v in values.items() if v not in (None, "")})
    return repo.insert("suggestion", row)


def from_proposals(repo, proposals) -> dict:
    """The scan's proposals that would change a case, as pending rows.
    Returns counts: written / same / not a change."""
    counts = {"written": 0, "same": 0, "unchanged": 0}
    for p in proposals:
        if p.verdict != "change" or not p.proposed_date:
            counts["unchanged"] += 1
            continue
        case = repo.conn.execute("SELECT id FROM enquiry WHERE opp_no = ?", (p.opp_no,)).fetchone()
        if case is None:
            continue
        if p.proposed_status == "Quoted" and firebase_pull.logged_on_site(
                repo.conn, case["id"], p.proposed_date, "quote"):
            # W1b: a colleague logged this quote on the team site
            counts["same"] += 1
            continue
        if p.proposed_status in ("Won", "Lost", "Cancelled") and site_close.waiting(repo.conn, case["id"]):
            # W2b: a colleague's Won or Lost waits for her already
            counts["same"] += 1
            continue
        made = record(repo, case["id"], p.opp_no, {
            "kind": "status", "proposed_status": p.proposed_status,
            "closed_reason": p.closed_reason or None, "proposed_date": p.proposed_date,
            "evidence_sender": p.evidence_sender, "evidence_date": p.evidence_date,
            "evidence_quote": (p.evidence_quote or "")[:400], "confidence": p.confidence,
            "note": p.note or None, "source": "scan",
            # SG1: the evidence mail, as requoted_values and from_mail keep it
            "message_id": getattr(p, "message_id", "") or None, "entry_id": getattr(p, "entry_id", "") or None,
        })
        counts["written" if made else "same"] += 1
    repo.commit()
    return counts


# ------------------------------------------------- one mail on the case --
@dataclass
class Membership:
    """Does the selected mail belong to this case?"""
    belongs: bool = False
    how: str = ""
    others: list = field(default_factory=list)      # [(enquiry_id, opp_no)] it may belong to


def membership(conn, case, mail) -> Membership:
    """The mail is the case's when its conversation id or its subject's
    thread key is the case's. Otherwise the cases that do match are listed
    so the user can go to the right one. AL1: a case number in the subject
    decides first (case_numbers) - its case only, and two numbers or one
    that does not fit belong to none."""
    from . import case_numbers, dates
    link = case_numbers.link_of(conn, mail)
    if link.kind in ("registered", "attached", "number") and (link.kind != "registered" or link.numbered):
        if link.case_id == case["id"]:
            return Membership(True, {"registered": "the mail it was registered from",
                                     "attached": "attached to this case",
                                     "number": "case number {} in the subject".format(link.opp_nos[0])}[link.kind])
        return Membership(False, "the subject carries case number {}".format(link.opp_nos[0]),
                          [(link.case_id, link.opp_nos[0])])
    if link.kind == "numbers":
        return Membership(False, "the subject carries two case numbers ({})".format(", ".join(link.opp_nos)),
                          list(zip(link.case_ids, link.opp_nos)))
    if link.kind == "mismatch":
        return Membership(False, "the subject carries case number {}, but the mail is not from or to its "
                                 "requester or vendors".format(link.opp_nos[0]), [])
    key = parsing.subject_thread_key(getattr(mail, "subject", "") or "")
    conversation = getattr(mail, "conversation_id", "") or ""
    # the case's own ids, and the threads attached to it by hand
    mine = db.thread_values(conn, case["id"])
    how = ""
    if conversation and conversation in mine["conversation"]:
        how = "same Outlook conversation"
    elif key and key in mine["thread_key"]:
        how = "same subject"
    if how:
        if link.kind == "registered" and link.case_id == case["id"]:
            return Membership(True, "the mail it was registered from")
        # N1: a thread several cases share is split by date (5.0 item 8)
        owners = case_numbers.thread_owners(conn, mail)
        if len(owners) > 1 and case["id"] in {o["id"] for o in owners}:
            chosen = case_numbers.split_owners(owners, case_numbers.mail_day(mail))
            if case["id"] not in chosen:
                names = [(o["id"], o["opp_no"]) for o in owners if o["id"] in chosen]
                label = ", ".join(n for _, n in names)
                return Membership(False, "this thread is also {}'s: a mail of {} is {}'s".format(
                    label, dates.month_day(case_numbers.mail_day(mail)), label), names)
            others = case_numbers.shared_with(case["id"], owners)
            if all(o["same_day"] for o in others):
                how += " (shared with {}, registered the same day)".format(", ".join(o["opp_no"] for o in others))
            else:
                how += " (shared with {}: {})".format(", ".join(o["opp_no"] for o in others), others[0]["mine"])
        return Membership(True, how)
    listed = [(row["id"], row["opp_no"]) for row in
              db.thread_cases(conn, conversation=conversation, key=key)
              if row["id"] != case["id"]]
    return Membership(False, "neither the conversation nor the subject is this case's", listed)


def _later_mails(search, mail) -> list:
    """The selected mail and what followed it in its conversation."""
    expand = getattr(search, "conversation_of", None) if search is not None else None
    found = list(expand(mail)) if expand else []
    since = getattr(mail, "received_at", "") or getattr(mail, "received_on", "") or ""
    out, seen = [], set()
    for m in [mail] + found:
        key = getattr(m, "entry_id", "") or id(m)
        if key in seen:
            continue
        at = getattr(m, "received_at", "") or getattr(m, "received_on", "") or ""
        if m is mail or at >= since:
            seen.add(key)
            out.append(m)
    hydrate = getattr(search, "hydrate", None) if search is not None else None
    return hydrate(out) if hydrate else out


def from_mail(repo, case, mail, search=None, blocked: list | None = None) -> list:
    """Suggestions for `case` read out of `mail` and the mails after it in
    the conversation, recorded as pending rows with source 'mail'.

    The status comes from propose.classify - the same rules as the
    scheduled scan. A mail we sent out to vendors suggests logging them.
    A mail that decides nothing is offered as a dated note, so the thread
    still leaves a trace on the case if she wants one. FX6: `blocked`
    gets each row that kept a suggestion from being made again.
    """
    import functools
    record_ = functools.partial(record, blocked=blocked)
    conn = repo.conn
    internal_domains = {str(d).strip().lower() for d in db.get_setting_list(conn, "internal_domains")}
    if getattr(mail, "sender_internal", False):
        own = matching.domain_of(getattr(mail, "sender_address", ""))
        if own:
            internal_domains.add(own)
    requester_domains = propose.requester_domains_of(conn, case["requester_party_id"])
    office = propose.office_prefix(case["requester_key"], conn)
    mails = _later_mails(search, mail)
    threads = [propose.to_thread_mail(m, internal_domains, requester_domains, office, propose.handled_by_of(case))
               for m in mails]
    proposal = propose.classify(threads, case["requester_key"],
                                requester_known=bool(requester_domains or office))
    made = []
    decided = False              # something was read, written now or before
    entry_id = getattr(mail, "entry_id", "") or ""
    if proposal.proposed_status not in ("", propose.STATUS_UNKNOWN) \
            and proposal.proposed_status != case["status"] and proposal.proposed_date:
        decided = True
        made.append(record_(repo, case["id"], case["opp_no"], {
            "kind": "status", "proposed_status": proposal.proposed_status,
            "closed_reason": proposal.closed_reason or None,
            "proposed_date": proposal.proposed_date,
            "evidence_sender": proposal.evidence_sender, "evidence_date": proposal.evidence_date,
            "evidence_quote": (proposal.evidence_quote or "")[:400],
            "confidence": proposal.confidence, "note": proposal.note or None,
            "entry_id": entry_id, "source": "mail",
        }))
    # RQ1: on a Quoted case our revised quote is a Re-quoted suggestion, read
    # by the morning scan's test (detect_followups.revised_quote); a mail that
    # passes it decides something even when it was suggested before
    if case["status"] == "Quoted":
        from . import detect_followups
        ours, _theirs = detect_followups._sides(case, internal_domains, requester_domains)
        seen = []
        for r in detect_followups.revised_quotes(conn, case, mails, requester_domains, ours, seen):
            made.append(record_(repo, case["id"], case["opp_no"], detect_followups.requoted_values(r, "mail")))
        decided = decided or bool(seen)
    # Vendors we wrote to in any of these mails: offered for logging. The
    # requester's own domains are never vendors, whatever the mail looks like.
    for m in mails:
        draft = intake.build(conn, m)
        if not draft.outbound:
            continue
        for vendor in draft.vendors:
            if not vendor.get("checked", True) or vendor["domain"] in requester_domains:
                continue
            decided = True
            exists = conn.execute(
                "SELECT 1 FROM vendor_request v JOIN party p ON p.id = v.vendor_party_id "
                "WHERE v.enquiry_id = ? AND (lower(p.key) = ? OR p.id IN "
                "(SELECT party_id FROM party_domain WHERE domain = ?))",
                (case["id"], (vendor["key"] or "").lower(), vendor["domain"])).fetchone()
            if exists:
                continue
            made.append(record_(repo, case["id"], case["opp_no"], {
                "kind": "vendor", "vendor_key": vendor["key"], "vendor_domain": vendor["domain"],
                "proposed_date": (getattr(m, "received_on", "") or "")[:10],
                "evidence_sender": "{} <{}>".format(getattr(m, "sender_name", ""),
                                                    getattr(m, "sender_address", "")),
                "evidence_date": (getattr(m, "received_on", "") or "")[:10],
                "evidence_quote": (getattr(m, "subject", "") or "")[:200],
                "confidence": "medium", "entry_id": getattr(m, "entry_id", "") or "",
                "source": "mail",
            }))
    # FX5: the other side says the cargo cannot be stacked
    tagged = non_stackable_values(conn, case, mails, internal_domains | detect_internal(conn), "mail")
    if tagged:
        made.append(record_(repo, case["id"], case["opp_no"], tagged))
    if not decided:
        # Nothing decided: the mail as a dated note, so the case shows it.
        first = next((t for t in threads if t.body), None)
        lines = propose._message_lines(first.body) if first and first.body else []
        line = (lines[0] if lines else getattr(mail, "subject", "") or "")[:200]
        made.append(record_(repo, case["id"], case["opp_no"], {
            "kind": "note", "note": line,
            "proposed_date": (getattr(mail, "received_on", "") or "")[:10],
            "evidence_sender": "{} <{}>".format(getattr(mail, "sender_name", ""),
                                                getattr(mail, "sender_address", "")),
            "evidence_date": (getattr(mail, "received_on", "") or "")[:10],
            "evidence_quote": line, "confidence": "low",
            "entry_id": entry_id, "message_id": getattr(mail, "message_id", "") or None, "source": "mail",
        }))
    repo.commit()
    return [m for m in made if m]


# ------------------------------------------- Non-stackable from a mail --
#: FX5, v50 (D7a): a tag suggestion is kind 'tag', its note the special
#: handling tag to add as Settings writes it ('Non-stackable'). It names no
#: entry_id or message_id, so its mail stays free for the other
#: suggestions and the follow-up reader; the sentence, the sender and the
#: date are its evidence.
def tag_of(row) -> str:
    """The tag a tag suggestion adds, '' for any other row."""
    return (row["note"] or "") if row["kind"] == "tag" else ""


def _has_tag(value, tag) -> bool:
    return tag.casefold() in {t.strip().casefold() for t in (value or "").split(";") if t.strip()}


def detect_internal(conn) -> set:
    from . import detect_followups
    return detect_followups.internal_domains(conn)


def non_stackable_wanted(conn, case):
    """The tag to suggest for the case, None when there is none to make:
    Non-stackable is not on Settings' special_handling list, the case has
    it, or it was suggested before (pending, dismissed or accepted - one per
    case, and her answer stands; Put back brings a dismissed one back)."""
    tag = intake.non_stackable_tag(conn)
    if tag is None or _has_tag(case["special_handling"], tag):
        return None
    if conn.execute("SELECT 1 FROM suggestion WHERE enquiry_id = ? AND kind = 'tag' AND lower(note) = lower(?)",
                    (case["id"], tag)).fetchone() is not None:
        return None
    return tag


def _day_of(mail) -> str:
    return (getattr(mail, "sent_at", "") or getattr(mail, "received_at", "")
            or getattr(mail, "received_on", "") or "")[:10]


def non_stackable_values(conn, case, mails, internal, source) -> dict | None:
    """FX5 item 2c: a pending row's values from the first of `mails` (oldest
    first) from the other side - not from JoJo, no internal domain -
    whose own words say the cargo cannot be stacked
    (parsing.non_stackable_line: never a question); None when there is
    none or nothing is wanted (non_stackable_wanted). The mail the case was
    registered from, and a mail before its received day, are not later
    mails: registration read those (DD1)."""
    tag = non_stackable_wanted(conn, case)
    if tag is None:
        return None
    registered = (case["entry_id"] or "").strip()
    since = (case["received_on"] or "")[:10]
    for m in mails:
        if registered and (getattr(m, "entry_id", "") or "").strip() == registered:
            continue
        day = _day_of(m)
        if since and day and day < since:
            continue
        address = (getattr(m, "sender_address", "") or "").strip()
        domain = matching.domain_of(address.lower())
        if getattr(m, "sender_internal", False) or (domain and domain in internal):
            continue
        sentence = parsing.non_stackable_line(parsing.body_lines(getattr(m, "body_excerpt", "") or ""))
        if not sentence:
            continue
        return {"kind": "tag", "note": tag, "proposed_date": day or None,
                "evidence_sender": "{} <{}>".format(getattr(m, "sender_name", "") or "", address),
                "evidence_date": day or None, "evidence_quote": sentence[:400], "confidence": "medium",
                "source": source}
    return None


def scan_non_stackable(repo, search, opp_nos=(), dry_run=False) -> list:
    """FX5 item 2c, in the morning run: the open cases (New and Quoted, in
    scope) whose thread (detect_followups.thread_of, the Mail card's rule)
    has a later mail from the other side saying the cargo cannot be
    stacked, one pending suggestion each. Returns [(opp_no, sentence,
    sender, date)] of the ones written (or, dry, that would be)."""
    from . import detect_followups
    conn = repo.conn
    out = []
    if intake.non_stackable_tag(conn) is None:
        return out
    internal = detect_followups.internal_domains(conn)
    sql = "SELECT * FROM v_enquiry WHERE status IN ('New', 'Quoted') AND scope = 'In' AND legacy = 0"
    args = ()
    if opp_nos:
        sql += " AND opp_no IN ({})".format(",".join("?" for _ in opp_nos))
        args = tuple(o.strip().upper() for o in opp_nos)
    for row in conn.execute(sql + " ORDER BY opp_no", args).fetchall():
        if non_stackable_wanted(conn, row) is None:
            continue
        values = non_stackable_values(conn, row, detect_followups.thread_of(search, row, conn), internal, "scan")
        if values is None:
            continue
        if dry_run or record(repo, row["id"], row["opp_no"], values):
            out.append((row["opp_no"], values["evidence_quote"], values["evidence_sender"], values["evidence_date"]))
    if not dry_run:
        repo.commit()
    return out


def _add_tag(repo, row) -> str:
    """Accept on a tag suggestion: the tag added to the case's Special
    handling as the case page's boxes write it (';'-joined, save_enquiry,
    change_log), while it is on Settings' list."""
    conn = repo.conn
    tag = tag_of(row)
    if not any(str(t).casefold() == tag.casefold() for t in (db.get_setting_list(conn, "special_handling") or [])):
        raise ValueError("{} is no longer on Settings' Special handling list".format(tag))
    case = conn.execute("SELECT special_handling FROM enquiry WHERE id = ?", (row["enquiry_id"],)).fetchone()
    tags = [t for t in ((case["special_handling"] if case is not None else "") or "").split(";") if t]
    if not _has_tag(";".join(tags), tag):
        repo.save_enquiry(row["enquiry_id"], {"special_handling": ";".join(tags + [tag])})
    _record(repo, row["id"], None, {"tag": tag}, False, "")
    repo.commit()
    return "{}: {} added to Special handling".format(row["opp_no"], tag)


# --------------------------------------------------------------- reading --
def _shipments_off_sql(conn) -> str:
    """FX3: the shipment kinds left out while shipments are switched off."""
    from . import shipments_switch
    if shipments_switch.enabled(conn):
        return ""
    return " AND s.kind NOT IN ({})".format(", ".join("'{}'".format(k) for k in SHIPMENT_KINDS))


def pending(conn, enquiry_id=None, shipment_id=None) -> list:
    """Pending rows: every one (Tracking), a case's own (its page - the
    shipment kinds belong to the shipment page), or a shipment's."""
    sql = ("SELECT s.*, e.status AS current_status FROM suggestion s "
           "JOIN enquiry e ON e.id = s.enquiry_id WHERE s.state = 'pending'") + _shipments_off_sql(conn)
    args = []
    if enquiry_id:
        sql += " AND s.enquiry_id = ? AND s.shipment_id IS NULL"
        args.append(enquiry_id)
    if shipment_id:
        sql += " AND s.shipment_id = ?"
        args.append(shipment_id)
    sql += " ORDER BY s.enquiry_id, s.created_at, s.id"
    return conn.execute(sql, args).fetchall()


def describe(row) -> str:
    """One line saying what a suggestion would do."""
    if row["kind"] == "status":
        text = "Set {}".format(row["proposed_status"])
        if row["closed_reason"]:
            text += " ({})".format(row["closed_reason"])
        return text + " on {}".format(row["proposed_date"] or "?")
    if row["kind"] == "vendor":
        return "Log vendor request to {} on {}".format(row["vendor_key"], row["proposed_date"] or "?")
    if row["kind"] == "shipment_milestone" and not row["shipment_id"]:
        from . import shipments
        return shipments.start_label(row)
    if row["kind"] == "shipment_milestone":
        return "Shipment milestone {} on {}".format(row["proposed_status"], row["proposed_date"] or "?")
    if row["kind"] == "shipment_issue":
        return "Shipment issue '{}' on {}".format(row["proposed_status"], row["proposed_date"] or "?")
    if row["kind"] == "shipment_note":
        return "Shipment note '{}' on {}".format(row["proposed_status"], row["proposed_date"] or "?")
    if row["kind"] == "case_number":
        return "{}: {}".format(row["proposed_status"], row["named_cases"] or row["opp_no"])
    if row["kind"] == "tag":
        return "Add {}".format(row["note"])
    if row["kind"] == "edit":
        return "Team site change: {}".format(row["evidence_quote"] or "")
    if row["kind"] == "followup":
        return "Follow-up found in the mail on {}".format(row["proposed_date"] or "?")
    return "Note only on {}".format(row["proposed_date"] or "?")


def named_cases(row) -> list:
    """The case numbers a case_number suggestion offers (AL1)."""
    return [n for n in (row["named_cases"] or row["opp_no"] or "").split(",") if n]


# --------------------------------------------------------------- deciding --
#: E1 (v40): what she recorded, beside what the app suggested
DECIDED = ("quoted", "requote", "won", "lost", "cancelled", "note", "history", "milestone", "start",
           "vendor", "attach", "dismissed", "followup")
#: what the case page's Status actions offer for each status (item 3)
ALLOWED = {"New": ("quoted", "won", "lost", "cancelled", "note"),
           "Quoted": ("requote", "won", "lost", "cancelled", "note"),
           "Won": ("cancelled", "note"),
           # L1: a reply after a 'No response from customer' closing reopens it
           "Lost": ("quoted", "note")}
_WANTED = {"Quoted": "quoted", "Won": "won", "Lost": "lost", "Cancelled": "cancelled"}
TONES = {"quoted": "s-quoted", "requote": "s-quoted", "won": "s-won", "lost": "s-lost",
         "cancelled": "s-cancelled", "note": "tone-grey", "history_quoted": "s-quoted",
         "history_requote": "s-quoted"}


@dataclass
class Plan:
    """What Accept writes for one pending status or note suggestion, and
    how its row says so (E1 item 1): the one place both are decided."""
    action: str                       # quoted requote won lost cancelled note history_quoted history_requote
    at: str
    note: str = ""
    reason: str | None = None
    round: int | None = None          # requote: the round it would be
    label: str = ""
    tone: str = ""
    already: str = ""                 # 'already quoted Sep 29'
    before: str = ""                  # history: 'Won Sep 18'
    rate: tuple | None = None         # W2b, Won from the site: set_rate_used's arguments


def _case_row(conn, enquiry_id):
    return conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()


def _evidence_note(row) -> str:
    if row["kind"] == "note":
        return (row["note"] or row["evidence_quote"] or "")[:400]
    return " ".join(part for part in ((row["evidence_sender"] or "").strip(),
                                      (row["evidence_quote"] or "").strip()) if part)[:400]


def _latest_quote_on(conn, enquiry_id) -> str:
    row = conn.execute("SELECT MAX(at) FROM status_event WHERE enquiry_id = ? AND voided = 0 "
                       "AND status = 'Quoted' AND kind IN ('status', 'requote')", (enquiry_id,)).fetchone()
    return row[0] or ""


def _closing_event(repo_or_conn, enquiry_id):
    conn = getattr(repo_or_conn, "conn", repo_or_conn)
    return db.Repository(conn).latest_event(enquiry_id)


def label_of(action, round_=None, reason=None, before="") -> str:
    from . import dates
    if action == "requote":
        return "Re-quoted · round {}".format(round_ or 2)
    if action in ("history_quoted", "history_requote"):
        return "{}, history before {}".format("Quoted" if action == "history_quoted" else "Re-quoted", before)
    if action in ("lost", "cancelled"):
        return action.capitalize() + (" · {}".format(reason) if reason else "")
    return {"quoted": "Quoted", "won": "Won", "note": "Note"}.get(action, action)


_SENDER = re.compile(r"^(?P<name>.*?)\s*<(?P<address>[^<>]*)>\s*$")


def quote_note(conn, row, case) -> bool:
    """RQ1 item 4: a note suggestion whose evidence is our own mail (a
    sender on our domain whom office_side reads as ours) and whose words
    are a quote going out (propose.quote_words)."""
    from . import detect_followups
    if row["kind"] != "note" or case is None:
        return False
    sender = _SENDER.match(row["evidence_sender"] or "")
    if sender is None:
        return False
    domain = matching.domain_of(sender.group("address").strip().lower())
    if not domain or domain not in detect_followups.internal_domains(conn):
        return False
    if propose.office_side(sender.group("name"), propose.office_prefix(case["requester_key"] or ""),
                           propose.handled_by_of(case)) != propose.SIDE_OURS:
        return False
    words = row["note"] or row["evidence_quote"] or ""
    return not detect_followups._FU_PHRASES.search(words) and bool(propose.quote_words(words))


def quote_notes_accepted(conn) -> list:
    """RQ1 item 5, read only: accepted note suggestions from the mail path
    that are our own quote (quote_note) and left no live Quoted or requote
    event on their day - [(opp_no, day, suggestion id, event_ref, words)]."""
    out = []
    for row in conn.execute("SELECT * FROM suggestion WHERE kind = 'note' AND state = 'accepted' "
                            "AND source = 'mail' AND COALESCE(decided_as, 'note') = 'note' "
                            "ORDER BY opp_no, proposed_date, id").fetchall():
        case = _case_row(conn, row["enquiry_id"])
        if not quote_note(conn, row, case):
            continue
        day = (row["proposed_date"] or row["evidence_date"] or "")[:10]
        if conn.execute("SELECT 1 FROM status_event WHERE enquiry_id = ? AND voided = 0 AND status = 'Quoted' "
                        "AND kind IN ('status', 'requote') AND substr(at, 1, 10) = ?",
                        (row["enquiry_id"], day)).fetchone() is not None:
            continue
        out.append((row["opp_no"], day, row["id"], row["event_ref"] or "", (row["note"] or "")[:80]))
    return out


def _history_option(conn, row, case, at, proposed=None):
    """('history_quoted' | 'history_requote', 'Won Sep 18') when a quote
    suggestion on a closed case is dated before the event the case is
    closed by - what set_status's history rule writes; else None."""
    from . import dates
    if case["status"] not in ("Won", "Lost", "Cancelled", "Dropped"):
        return None
    if (proposed or row["proposed_status"] or "") not in ("Quoted", "New", "Sourcing"):
        return None
    closing = _closing_event(conn, case["id"])
    if closing is None or not at or at >= closing["at"]:
        return None
    before = "{} {}".format(closing["status"], dates.month_day(closing["at"]))
    earlier = conn.execute("SELECT 1 FROM status_event WHERE enquiry_id = ? AND voided = 0 AND status = 'Quoted' "
                           "AND kind IN ('status', 'requote') AND at < ? LIMIT 1", (case["id"], at)).fetchone()
    return ("history_requote" if earlier else "history_quoted"), before


def plan(conn, row, today=None) -> Plan:
    """What Accept writes for a pending status or note suggestion: Quoted on
    a New case is Quoted; Quoted on a Quoted case is Re-quoted (a requote
    event, its round); Won, Lost, Cancelled are themselves with the
    suggestion's reason; anything the case's status does not allow is Note
    only. The note is the sender and the quoted words (a note
    suggestion's own note). RQ1: a note that is our own quote (quote_note)
    is Quoted on a New case, Re-quoted on a Quoted one, and on a closed
    case the history option when dated before the closing; else Note only."""
    from . import dates
    case = _case_row(conn, row["enquiry_id"])
    status = case["status"] if case is not None else "New"
    at = row["proposed_date"] or today or dt.date.today().isoformat()
    # FX3: a site close's note is what the pasted mail adds, never "<sender> <quote>"
    note = site_close.event_note(row) if site_close.action_id_of(row) else _evidence_note(row)
    proposed = "New" if row["proposed_status"] == "Sourcing" else (row["proposed_status"] or "")
    action = "note"
    before = ""
    if row["kind"] == "status" and not (row["note"] or "").startswith(propose.REQUOTE_NOTE):
        wanted = _WANTED.get(proposed)
        if wanted == "quoted" and status == "Quoted":
            wanted = "requote"
        if wanted in ALLOWED.get(status, ("note",)):
            action = wanted
    elif quote_note(conn, row, case):
        proposed = "Quoted"
        if status in ("New", "Quoted"):
            action = "requote" if status == "Quoted" else "quoted"
        else:
            hist = _history_option(conn, row, case, at, proposed)
            if hist:
                action, before = hist
    reason = None
    if action in ("lost", "cancelled"):
        # 'Other' belongs to the Lost and the Cancelled lists
        reason = row["closed_reason"] or "Other"
    out = Plan(action=action, at=at, note=note, reason=reason, before=before)
    if action == "requote":
        out.round = int(case["quote_rounds"] or 0) + 1
    out.label = label_of(action, out.round, reason, before)
    out.tone = TONES[action]
    if action == "won" and row["source"] == "web":
        out.rate = site_close.rate_for(conn, row)
    if proposed == "Quoted" and action in ("quoted", "requote"):
        last = _latest_quote_on(conn, row["enquiry_id"])
        if last and at <= last:
            out.already = "already quoted {}".format(dates.month_day(last))
    return out


def options(conn, row, today=None) -> list:
    """Record as, for Edit: [(value, label, suggested)] - what the case
    page's Status actions offer for the case's status, and on a closed case
    a quote dated before its closing as history."""
    p = plan(conn, row, today)
    case = _case_row(conn, row["enquiry_id"])
    status = case["status"] if case is not None else "New"
    out = []
    for action in ALLOWED.get(status, ("note",)):
        label = "Note only" if action == "note" else label_of(action, int(case["quote_rounds"] or 0) + 1)
        out.append((action, label, action == p.action))
    hist = _history_option(conn, row, case, p.at, "Quoted" if quote_note(conn, row, case) else None) \
        if case is not None else None
    if hist:
        out.insert(0, (hist[0], label_of(hist[0], before=hist[1]), hist[0] == p.action))
    return out


def prefill(conn, row, today=None) -> dict:
    """The Edit form's values: what Accept would write; Rate used
    preselected as the Won form does."""
    p = plan(conn, row, today)
    vendors = conn.execute("SELECT 1 FROM vendor_request WHERE enquiry_id = ? AND voided = 0 LIMIT 1",
                           (row["enquiry_id"],)).fetchone()
    out = {"action": p.action, "at": p.at, "note": p.note, "reason": p.reason or "",
           "rate_used": "agent" if vendors else "own"}
    if p.rate:
        # W2b: whose rate the colleague said
        out.update(rate_used=p.rate[0], rate_party=p.rate[1], rate_request=p.rate[2])
    else:
        # Q3: as the case page's Won form, from the last quote when no request is awarded
        from . import site_quotes
        out.update(site_quotes.won_start(conn, row["enquiry_id"]) or {})
    return out


def _same_rate(a, b) -> bool:
    """Two Rate used answers name the same thing (the request for an
    agent's, else the party)."""
    if not a or not b:
        return not a and not b
    if a[0] != b[0]:
        return False
    return (a[2] or a[1]) == (b[2] or b[1]) if a[0] == "agent" else a[1] == b[1]


def _event_after(conn, enquiry_id, before_id, action) -> str:
    """'status_event:<id>' of the row a decision wrote, '' when it only
    confirmed what was there."""
    rows = conn.execute("SELECT id, kind, status FROM status_event WHERE enquiry_id = ? AND id > ? ORDER BY id",
                        (enquiry_id, before_id)).fetchall()
    if not rows:
        return ""
    if action == "note":
        found = [r for r in rows if r["kind"] == "note"]
    else:
        target = {"won": "Won", "lost": "Lost", "cancelled": "Cancelled"}.get(action, "Quoted")
        found = [r for r in rows if r["kind"] in ("status", "requote") and r["status"] == target] or \
            [r for r in rows if r["kind"] in ("status", "requote")]
    pick = found[-1] if found and action == "won" else (found[0] if found else rows[0])
    return "status_event:{}".format(pick["id"])


def decide(repo, row, decision: dict, today=None, edited_from: Plan | None = None) -> str:
    """Write one decision on a status or note suggestion through the
    repository calls the case page's forms use, source mail, and record it
    on the suggestion (decided_as, decided_values, edited, event_ref).
    `decision`: action, at, note, reason, their_price, our_price, rate
    (rate_used, party, request, note) and routes (won ids, lost, cancelled).
    Raises ValueError, the suggestion left pending, when it cannot be."""
    from . import status as status_mod
    conn = repo.conn
    eid = row["enquiry_id"]
    action = decision["action"]
    at = (decision.get("at") or "").strip() or today or dt.date.today().isoformat()
    note = (decision.get("note") or "").strip()
    reason = (decision.get("reason") or "").strip() or None
    if action in ("lost", "cancelled"):
        if not reason:
            raise ValueError("{} needs a reason.".format(action.capitalize()))
        if not status_mod.reason_belongs(conn, action.capitalize(), reason):
            raise ValueError("'{}' is not a {} reason.".format(reason, action.capitalize()))
    if action in ("requote", "history_requote") and not status_mod.requote_reason_belongs(conn, reason):
        raise ValueError("'{}' is not a re-quote reason.".format(reason))
    if action in ("won", "lost", "cancelled"):
        refusal = repo.closing_refusal(eid, action.capitalize(), at)
        if refusal:
            raise ValueError(refusal)
    if action == "note" and not note and edited_from is None:
        raise ValueError("A note needs some text.")
    p = edited_from or plan(conn, row, today)          # what Accept would write, before anything is
    prices, cut = status_mod.lost_prices(decision.get)      # FX1: cut at 200
    before = conn.execute("SELECT COALESCE(MAX(id), 0) FROM status_event WHERE enquiry_id = ?", (eid,)).fetchone()[0]
    opp = row["opp_no"]
    # FX3: a Won or Lost sent on the team site is the site's on the Timeline
    # (source web, the action's event_ids), whatever she records it as
    site_action = site_close.action_id_of(row)
    src = firebase_pull.SOURCE if site_action else "mail"
    repo.last_status_outcome = None
    if action in ("quoted", "history_quoted"):
        repo.set_status(eid, "Quoted", at, note=note or None, source=src)
        done = "{} set to Quoted on {}".format(opp, at)
    elif action in ("requote", "history_requote"):
        repo.requote(eid, at, note=note or None, source=src, reason=reason)
        done = "{} re-quoted on {}".format(opp, at)
    elif action == "won":
        routes = decision.get("routes")
        if routes:
            won_ids, lost, cancelled = routes
            repo.record_results(eid, at, won_ids, lost, note=note or None, source=src, cancelled=cancelled)
        else:
            repo.set_status(eid, "Won", at, note=note or None, source=src)
        if decision.get("rate"):
            repo.set_rate_used(eid, *decision["rate"])
        done = "{} set to Won on {}".format(opp, at)
    elif action in ("lost", "cancelled"):
        status = action.capitalize()
        repo.set_status(eid, status, at, note=note or None, closed_reason=reason, source=src)
        if action == "lost" and prices:
            repo.update("enquiry", eid, prices)
        done = "{} set to {} on {}".format(opp, status, at)
    elif action == "note":
        repo.add_note(eid, at, (note or "(no text)")[:400], source=src)
        done = "{}: note added on {}".format(opp, at)
    else:
        raise ValueError("Not something this suggestion can be recorded as.")
    if repo.last_status_outcome in ("confirmed", "repeat") and action != "note":
        done = "{} was already {} on {}; nothing new was recorded".format(
            opp, "Quoted" if action in ("quoted", "requote", "history_quoted", "history_requote") else action.capitalize(), at)
    # Accepting anything about a case means somebody looked at it.
    repo.confirm_status(eid)
    values = {"date": at, "reason": reason, "note": note or None}
    if decision.get("rate"):
        rate = decision["rate"]
        values.update(rate_used=rate[0], rate_party=rate[1], rate_request=rate[2], rate_note=rate[3])
    if decision.get("routes"):
        won_ids, lost, cancelled = decision["routes"]
        values["routes"] = {"won": list(won_ids), "lost": {str(k): v for k, v in lost.items()},
                            "cancelled": {str(k): v for k, v in (cancelled or {}).items()}}
    for key, value in prices.items():
        values[key.replace("lost_", "")] = value
    edited = (action != p.action or at != p.at or (note or "") != (p.note or "") or
              (reason or None) != (p.reason or None) or not _same_rate(decision.get("rate"), p.rate) or
              bool(decision.get("routes")) or any(k in values for k in ("their_price", "our_price")))
    ref = _event_after(conn, eid, before, action)
    _record(repo, row["id"], "history" if action.startswith("history_") else action, values, edited, ref)
    if site_action and ref:
        site_close.add_event(repo, site_action, int(ref.split(":")[1]))
    same = same_mail_note(conn, row, before)
    if same is not None:
        # SG1 item 3c: the mail is on the timeline once - the older automatic
        # note is struck out, an action of its own (Put back on the timeline)
        repo.void_status_event(same["id"])
        done += "; the timeline note of {} on the same mail was struck out".format(same["at"][:10])
    repo.commit()
    if site_close.action_id_of(row):
        done += "; {} sees it on the team site at the next sync".format(row["evidence_sender"] or "the colleague")
    if action == "lost" and cut:
        done += "; " + status_mod.prices_cut_text(cut)[:-1]
    return done + (" (edited)" if edited else "")


def same_mail_note(conn, row, before_id=None):
    """SG1 item 3c: the live timeline note or follow-up the scan wrote of
    this suggestion's mail (the same [mid:]), oldest first, or None. Not one
    an accepted suggestion wrote, and not one written after `before_id`
    (the event a decision just wrote)."""
    mid = (row["message_id"] or "").strip()
    if not mid:
        return None
    sql = ("SELECT id, at, kind FROM status_event WHERE enquiry_id = ? AND voided = 0 "
           "AND kind IN ('note', 'followup') AND instr(note, ?) > 0 "
           "AND 'status_event:' || id NOT IN (SELECT event_ref FROM suggestion WHERE event_ref IS NOT NULL)")
    args = [row["enquiry_id"], "[mid:{}]".format(mid)]
    if before_id is not None:
        sql += " AND id <= ?"
        args.append(before_id)
    return conn.execute(sql + " ORDER BY at, id LIMIT 1", args).fetchone()


def record_as(repo, suggestion_id: int, action: str, reason: str | None = None, today=None) -> str:
    """SG1 item 2, one of the card's buttons: what Edit writes with that
    Record as, the suggestion's date and its text (Lost and Cancelled with
    the reason picked beside the button). Won writes what Accept writes:
    several routes and the rate used stay under Edit."""
    conn = repo.conn
    row = conn.execute("SELECT * FROM suggestion WHERE id = ?", (suggestion_id,)).fetchone()
    if row is None or row["state"] != "pending":
        raise ValueError("that suggestion is not pending")
    if row["kind"] not in ("status", "note"):
        raise ValueError("this suggestion has no Record as")
    if action not in [o[0] for o in options(conn, row, today)]:
        raise ValueError("Record as: not one this case offers.")
    p = plan(conn, row, today)
    decision = {"action": action, "at": p.at, "note": p.note,
                "reason": reason if action in ("lost", "cancelled") else None}
    if action == "won":
        decision["rate"] = p.rate if p.action == "won" else None
    return decide(repo, row, decision, today)


# ----------------------------------------------------------------- expiry --
#: SG1 item 1: a pending suggestion nobody decided in this many days goes
EXPIRE_SETTING = "suggestion_expire_days"
EXPIRE_DAYS = 14


def expire_days(conn) -> int:
    try:
        return max(int(str(db.get_setting(conn, EXPIRE_SETTING, EXPIRE_DAYS)).strip()), 1)
    except (TypeError, ValueError):
        return EXPIRE_DAYS


def expiring(conn, today=None) -> list:
    """The pending suggestions created `expire_days` or more days before
    `today`, oldest first."""
    day = dt.date.fromisoformat(today) if today else dt.date.today()
    cut = (day - dt.timedelta(days=expire_days(conn))).isoformat()
    return conn.execute("SELECT * FROM suggestion WHERE state = 'pending' AND substr(created_at, 1, 10) <= ? "
                        "ORDER BY created_at, id", (cut,)).fetchall()


def expire(repo, today=None) -> list:
    """SG1 item 1, in the morning run: each pending suggestion nobody
    decided in time is dismissed, marked expired in decided_values
    (decided_as stays 'dismissed': the schema's list has no 'expired'),
    through the repository so change_log has it. Put back works as for any
    dismissed one. Returns the rows expired."""
    rows = expiring(repo.conn, today)
    days = expire_days(repo.conn)
    for row in rows:
        _record(repo, row["id"], "dismissed", {"expired": True, "days": days}, False, "")
    repo.commit()
    return rows


def is_expired(row) -> bool:
    import json
    if row["state"] != "dismissed" or not row["decided_values"]:
        return False
    try:
        return bool(json.loads(row["decided_values"]).get("expired"))
    except (ValueError, AttributeError):
        return False


def _record(repo, suggestion_id, decided_as, values, edited, event_ref) -> None:
    import json
    repo.update("suggestion", suggestion_id, {
        "state": "dismissed" if decided_as == "dismissed" else "accepted",
        "decided_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "decided_as": decided_as, "decided_values": json.dumps(values, sort_keys=True) if values else None,
        "edited": 1 if edited else 0, "event_ref": event_ref or None})


def _shipment_event_after(conn, before) -> str:
    row = conn.execute("SELECT MAX(id) FROM shipment_event").fetchone()
    return "shipment_event:{}".format(row[0]) if row[0] and row[0] > before else ""


def decide_shipment(repo, row, decision: dict | None = None) -> str:
    """Accept or Edit-and-save a shipment suggestion (a Start shipment on the
    case page, the others on the shipment page): the milestone (or title),
    the date and time, the note, and for Start shipment planned - written
    through accept_start and log_milestone as Accept is."""
    from . import shipments
    conn = repo.conn
    decision = decision or {}
    before = conn.execute("SELECT COALESCE(MAX(id), 0) FROM shipment_event").fetchone()[0]
    start = row["kind"] == "shipment_milestone" and not row["shipment_id"]
    def stamp(value):
        value = (value or "").strip()
        return value + " 00:00" if len(value) == 10 else value
    suggested = {"milestone": (row["proposed_status"] or "").replace(shipments.PLANNED, ""),
                 "at": stamp(row["proposed_date"]), "note": row["note"] or "",
                 "planned": (row["proposed_status"] or "").endswith(shipments.PLANNED)}
    chosen = dict(suggested, **{k: v for k, v in decision.items() if v is not None})
    chosen["at"] = stamp(chosen["at"])
    if start:
        done = shipments.accept_start(repo, row, milestone=chosen["milestone"], at=chosen["at"],
                                      note=chosen["note"], planned=bool(chosen["planned"]))
        ship = conn.execute("SELECT shipment_id FROM suggestion WHERE id = ?", (row["id"],)).fetchone()["shipment_id"]
        ref = _shipment_event_after(conn, before) or ("shipment:{}".format(ship) if ship else "")
        decided_as = "start"
    else:
        done = shipments.accept_suggestion(repo, row, milestone=chosen["milestone"], at=chosen["at"],
                                           note=chosen["note"])
        ref = _shipment_event_after(conn, before)
        decided_as = "milestone" if row["kind"] == "shipment_milestone" else "note"
    edited = any(str(chosen[k] or "") != str(suggested[k] or "") for k in suggested)
    values = {"milestone": chosen["milestone"], "date": chosen["at"], "note": chosen["note"] or None}
    if start:
        values["planned"] = bool(chosen["planned"])
    _record(repo, row["id"], decided_as, values, edited, ref)
    repo.commit()
    return done + (" (edited)" if edited else "")


# --------------------------------------------------------- follow-ups (FX7) --
@dataclass
class FollowupPlan:
    """What Accept writes for a pending follow-up suggestion: the follow-up
    event the morning scan used to write (FX7, v58)."""
    on: str                 # 'quote' or 'question' (a wait's, on our question)
    at: str
    seq: int
    note: str
    wait_id: int | None = None
    line: str = ""          # the card's sentence


def _sender_name(text) -> str:
    found = _SENDER.match(text or "")
    if found is None:
        return (text or "").strip()
    return found.group("name").strip() or found.group("address").strip()


def followup_plan(conn, row, at=None) -> FollowupPlan:
    """The number and kind the follow-up takes when written on `at` (the
    mail's day): on our question when the scan read it in a wait and a
    wait still holds that day (wait.wait_on) - the wait's next number - else
    on the quote, the quote round's next number (Repository.next_quote_seq,
    a wait's follow-ups left out)."""
    from . import detect_followups, wait
    at = (at or row["proposed_date"] or "")[:10]
    held = wait.wait_on(conn, row["enquiry_id"], at) if row["proposed_status"] == detect_followups.ON_QUESTION \
        else None
    if held is not None:
        on, seq, wait_id = detect_followups.ON_QUESTION, wait.wait_seq(conn, held["id"]), held["id"]
    else:
        on, seq, wait_id = detect_followups.ON_QUOTE, db.Repository(conn).next_quote_seq(row["enquiry_id"]), None
    words = " ".join((row["evidence_quote"] or "").split())
    if len(words) > 90:
        words = words[:87].rstrip() + "..."
    from . import dates
    line = "FU{}{} found in the mail: {}, {}{}".format(
        seq, " on our question" if on == detect_followups.ON_QUESTION else "", dates.month_day(at),
        _sender_name(row["evidence_sender"]) or "?", ", " + words if words else "")
    return FollowupPlan(on=on, at=at, seq=seq, note=row["note"] or "FU detected", wait_id=wait_id, line=line)


def _with_mid(note, row) -> str:
    """The event's note keeps the mail's [mid:], so the scan knows the mail."""
    mid = (row["message_id"] or "").strip()
    note = " ".join((note or "").split()) or "FU detected"
    if mid and "[mid:{}]".format(mid) not in note:
        note = "{} [mid:{}]".format(note, mid)
    return note


def close_followup(repo, row, why: str) -> None:
    """FX7: a pending follow-up suggestion closed without writing anything
    (as SG1 closes an expired one): dismissed, decided_values saying why."""
    _record(repo, row["id"], "dismissed", {"closed": why, "day": (row["proposed_date"] or "")[:10]}, False, "")


def close_logged_followups(repo, enquiry_id=None) -> list:
    """FX7 (T4b): each pending follow-up suggestion whose day has a
    follow-up logged by hand, or on the team site, is closed - that one
    stands. Returns the rows closed."""
    from . import detect_followups
    sql = "SELECT * FROM suggestion WHERE kind = 'followup' AND state = 'pending'"
    args = ()
    if enquiry_id:
        sql += " AND enquiry_id = ?"
        args = (enquiry_id,)
    closed = []
    for row in repo.conn.execute(sql + " ORDER BY id", args).fetchall():
        why = detect_followups.logged_that_day(repo.conn, row["enquiry_id"], row["proposed_date"])
        if why:
            close_followup(repo, row, why)
            closed.append(row)
    return closed                   # the caller's action commits it


def closed_why(row) -> str:
    """Why a dismissed follow-up suggestion was closed, '' when she dismissed it."""
    import json
    if row["state"] != "dismissed" or not row["decided_values"]:
        return ""
    try:
        return str(json.loads(row["decided_values"]).get("closed") or "")
    except (ValueError, AttributeError):
        return ""


def decide_followup(repo, row, decision: dict | None = None) -> str:
    """Accept (no `decision`) or Edit-and-save (date, note) a follow-up
    suggestion: the follow-up event exactly as the scan wrote it - dated as
    the mail, channel mail, source mail, the [mid:] in the note, a wait's
    number for a wait - recorded on the suggestion (decided_as followup,
    event_ref). A follow-up logged by hand or on the site that day wins:
    nothing is written and the suggestion is closed, saying why."""
    from . import detect_followups
    conn = repo.conn
    decision = decision or {}
    suggested = followup_plan(conn, row)
    at = (decision.get("at") or "").strip()[:10] or suggested.at
    try:
        dt.date.fromisoformat(at)
    except ValueError:
        raise ValueError("Give the date.")
    p = suggested if at == suggested.at else followup_plan(conn, row, at)
    why = detect_followups.logged_that_day(conn, row["enquiry_id"], at)
    if why:
        close_followup(repo, row, why)
        repo.commit()
        return "{}: nothing written - {}; that one stands".format(row["opp_no"], why)
    typed = decision.get("note")
    note = _with_mid(typed if typed is not None and typed.strip() else p.note, row)
    event_id = repo.add_followup(row["enquiry_id"], at, channel="mail", note=note, source="mail",
                                 seq=p.seq if p.on == detect_followups.ON_QUESTION else None)
    if event_id is None:
        raise ValueError("No such inquiry.")
    seq = conn.execute("SELECT seq FROM status_event WHERE id = ?", (event_id,)).fetchone()["seq"]
    edited = at != suggested.at or note != _with_mid(suggested.note, row)
    _record(repo, row["id"], "followup", {"date": at, "note": note, "seq": seq, "on": p.on}, edited,
            "status_event:{}".format(event_id))
    repo.commit()
    return "{}: FU{}{} logged on {}{}".format(row["opp_no"], seq,
                                             " on our question" if p.on == detect_followups.ON_QUESTION else "",
                                             at, " (edited)" if edited else "")


def pending_followup(conn, enquiry_id):
    """FX7: the case's oldest pending follow-up suggestion (id, proposed_date),
    or None - Tracking's 'Found in the mail on ...' line."""
    return conn.execute("SELECT id, proposed_date, proposed_status FROM suggestion WHERE enquiry_id = ? "
                        "AND kind = 'followup' AND state = 'pending' ORDER BY proposed_date, id LIMIT 1",
                        (enquiry_id,)).fetchone()


def accept(repo, suggestion_id: int, at: str | None = None, today=None, case: str | None = None) -> str:
    """Write what the suggestion says, through the repository, with source
    'mail' - for a status or note suggestion exactly what plan() returns
    (E1). Returns a sentence for the flash message. Raises ValueError
    when the row is not pending. `case` is the case a case_number
    suggestion's mail is attached to (one of the numbers it names). `at`
    is no longer read: the date box went with E1, Edit replaces it."""
    conn = repo.conn
    row = conn.execute("SELECT * FROM suggestion WHERE id = ?", (suggestion_id,)).fetchone()
    if row is None or row["state"] != "pending":
        raise ValueError("that suggestion is not pending")
    if row["kind"] == "case_number":
        return _attach(repo, row, case, today)
    if row["kind"] == "status" and (row["note"] or "").startswith(propose.REQUOTE_NOTE):
        # the retired 'they asked for the rate again' proposal: that mail is
        # R3's Replied step, not a quote that went out (2026-09-25)
        raise ValueError("that re-quote suggestion was retired - use Re-quoted once the new quote is sent")
    if row["kind"] == "tag":
        return _add_tag(repo, row)
    if row["kind"] == "edit":
        from . import site_edit
        return site_edit.accept(repo, row)
    if row["kind"] == "followup":
        return decide_followup(repo, row)
    if row["kind"] in ("status", "note"):
        p = plan(conn, row, today)
        return decide(repo, row, {"action": p.action, "at": p.at, "note": p.note, "reason": p.reason,
                                  "rate": p.rate}, today, edited_from=p).replace(" (edited)", "")
    if row["kind"] in SHIPMENT_KINDS:
        return decide_shipment(repo, row)
    when = row["proposed_date"] or today or dt.date.today().isoformat()
    # a vendor suggestion: the request we sent, logged as the Rate requests card does
    party = None
    if row["vendor_domain"]:
        found = matching.by_domain(conn, row["vendor_domain"])
        party = found[0].party_id if found else None
    if party is None and row["vendor_key"]:
        party = repo.add_party(row["vendor_key"], type="Unknown", country="Unknown",
                               source="outlook", notes="Added from a suggestion.")
        if row["vendor_domain"]:
            matching.remember_domain(repo, party, row["vendor_domain"])
    request_id = repo.insert("vendor_request", {
        "enquiry_id": row["enquiry_id"], "vendor_party_id": party,
        "asked_on": when, "source": "mail",
        "notes": "From Outlook: {}".format((row["evidence_quote"] or "")[:120]),
    })
    repo.confirm_status(row["enquiry_id"])
    _record(repo, suggestion_id, "vendor", {"date": when, "vendor": row["vendor_key"]}, False,
            "vendor_request:{}".format(request_id))
    repo.commit()
    return "{}: vendor request to {} logged on {}".format(row["opp_no"], row["vendor_key"], when)


def _attach(repo, row, case, today=None) -> str:
    """AL1: the mail of a case-number suggestion goes to the case chosen -
    one enquiry_thread row of kind 'message' (the mail's Message-ID, else
    its entry id); from then on case_numbers reads it as that case's."""
    offered = named_cases(row)
    chosen = (case or "").strip().upper() or (offered[0] if len(offered) == 1 else "")
    if chosen not in offered:
        raise ValueError("pick one of {}".format(", ".join(offered)))
    target = repo.conn.execute("SELECT id FROM enquiry WHERE opp_no = ?", (chosen,)).fetchone()
    if target is None:
        raise ValueError("{} is not on file".format(chosen))
    ref = (row["message_id"] or row["entry_id"] or "").strip()
    if not ref:
        raise ValueError("the suggestion does not name its mail")
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    found = repo.conn.execute("SELECT id, enquiry_id, voided FROM enquiry_thread WHERE kind = 'message' AND value = ?",
                              (ref,)).fetchone()
    if found is None:
        repo.insert("enquiry_thread", {"enquiry_id": target["id"], "kind": "message", "value": ref,
                                       "added_on": (today or dt.date.today().isoformat()), "source": "suggestion",
                                       "note": (row["evidence_quote"] or "")[:300]})
    else:
        repo.update("enquiry_thread", found["id"], {"enquiry_id": target["id"], "voided": 0})
    _record(repo, row["id"], "attach", {"case": chosen}, False, "")
    repo.commit()
    return "The mail '{}' is {}'s now.".format((row["evidence_quote"] or "")[:60], chosen)


def dismiss(repo, suggestion_id: int) -> str:
    row = repo.conn.execute("SELECT * FROM suggestion WHERE id = ?", (suggestion_id,)).fetchone()
    if row is None or row["state"] != "pending":
        raise ValueError("that suggestion is not pending")
    _record(repo, suggestion_id, "dismissed", None, False, "")
    repo.commit()
    if site_close.action_id_of(row):
        return "{}: the {} from {} was not confirmed; they see it on the team site at the next sync".format(
            row["opp_no"], row["proposed_status"], row["evidence_sender"] or "the team site")
    if row["kind"] == "shipment_milestone" and not row["shipment_id"]:
        return "{}: Start shipment dismissed - this case will not be suggested again".format(row["opp_no"])
    return "{}: suggestion dismissed{}".format(
        row["opp_no"], " - that mail will not be suggested again" if row["entry_id"] else "")


def put_back(repo, suggestion_id: int) -> str:
    """A dismissed suggestion pending again (E1 item 6): what was decided is
    cleared; change_log keeps it."""
    row = repo.conn.execute("SELECT * FROM suggestion WHERE id = ?", (suggestion_id,)).fetchone()
    if row is None or row["state"] != "dismissed":
        raise ValueError("that suggestion is not dismissed")
    repo.update("suggestion", suggestion_id, {"state": "pending", "decided_at": None, "decided_as": None,
                                              "decided_values": None, "edited": 0, "event_ref": None})
    repo.commit()
    return "{}: suggestion put back.".format(row["opp_no"])


def dismissed(conn, enquiry_id=None, shipment_id=None) -> list:
    """The dismissed rows of a case's page (not the shipment kinds) or of a
    shipment, newest first."""
    sql = "SELECT s.*, e.status AS current_status FROM suggestion s JOIN enquiry e ON e.id = s.enquiry_id " \
          "WHERE s.state = 'dismissed'" + _shipments_off_sql(conn)
    args = []
    if enquiry_id:
        sql += " AND s.enquiry_id = ? AND s.shipment_id IS NULL"
        args.append(enquiry_id)
    if shipment_id:
        sql += " AND s.shipment_id = ?"
        args.append(shipment_id)
    return conn.execute(sql + " ORDER BY COALESCE(s.decided_at, '') DESC, s.id DESC", args).fetchall()


def edited_events(conn, table: str, ids) -> set:
    """The ids of `table` rows written from a suggestion she edited (E1
    item 5): the timeline's 'edited' label."""
    ids = list(ids)
    if not ids:
        return set()
    refs = {"{}:{}".format(table, i): i for i in ids}
    marks = ",".join("?" for _ in refs)
    return {refs[r["event_ref"]] for r in conn.execute(
        "SELECT event_ref FROM suggestion WHERE edited = 1 AND event_ref IN ({})".format(marks), list(refs))}


def view(conn, rows, today=None) -> list:
    """The rows as the page shows them (E1 item 2): each suggestion with,
    for a status or note one, the case's label, what Accept writes and
    the already-quoted line; for Edit its Record as options and prefill;
    for a shipment one the milestones its Edit offers."""
    from . import shipments
    out = []
    for row in rows:
        item = {"row": row, "id": row["id"], "kind": row["kind"], "plan": None, "options": [], "prefill": {},
                "same_mail": None,
                "milestones": [], "editable": row["kind"] not in ("vendor", "case_number"),
                "site": site_close.view_of(conn, row), "tag": tag_of(row)}
        if row["kind"] == "followup":
            # FX7: the card's sentence; Edit is the date and the note
            fu = followup_plan(conn, row)
            item["fu"] = fu
            item["prefill"] = {"date": fu.at, "note": parsing.hide_mid(fu.note)}
        elif row["kind"] in ("tag", "edit"):
            item["editable"] = False                 # FX5: Accept or Dismiss, nothing to edit
            if row["kind"] == "edit":
                from . import site_edit
                item["edit_lines"] = site_edit.conflict_lines(conn, row)     # D7b
        elif row["kind"] in ("status", "note"):
            item["plan"] = plan(conn, row, today)
            item["options"] = options(conn, row, today)
            item["prefill"] = prefill(conn, row, today)
            # SG1 item 3c: its mail is on the timeline already
            item["same_mail"] = same_mail_note(conn, row)
        elif row["kind"] in SHIPMENT_KINDS or (row["kind"] == "shipment_milestone"):
            mode = "ocean"
            if row["shipment_id"]:
                ship = conn.execute("SELECT mode FROM shipment WHERE id = ?", (row["shipment_id"],)).fetchone()
                mode = ship["mode"] if ship is not None else mode
                item["milestones"] = list(shipments.MILESTONES.get(mode, shipments.MILESTONES["ocean"]))
            else:
                case = _case_row(conn, row["enquiry_id"])
                mode = shipments.mode_for(case["service_mode"] if case is not None else None)
                item["milestones"] = list(shipments.start_milestones(mode))
            stamp = (row["proposed_date"] or "").strip()
            item["prefill"] = {"milestone": (row["proposed_status"] or "").replace(shipments.PLANNED, ""),
                               "date": stamp[:10], "time": stamp[11:16] if len(stamp) > 10 else "",
                               "note": row["note"] or "",
                               "planned": (row["proposed_status"] or "").endswith(shipments.PLANNED)}
        out.append(item)
    return out
