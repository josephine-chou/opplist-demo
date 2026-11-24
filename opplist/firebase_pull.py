"""The team site, the pull half (W1b, 2026-10-28): what people did on the
site comes back into OppList, as ..\\OppListWeb\\INTERFACE.md sections 2
and 3 say. firebase_sync.sync_once calls pull() first in every run, then
pushes the lists, which then already show what was taken in.

  (1) actions/{id} with at > the last pulledAt, in the order of at, each
      applied once through the Repository (change_log source 'web', the
      events it writes source 'web') and recorded in site_action, applied
      or refused with a message for the person;
  (2) actionResults/{same id} for every action recorded and not answered
      yet: {ok, msg, by, type, opp, at (server time)};
  (3) retired 2026-11-04 (CX1, with the quick notes): the site's notes
      stay on the site; nothing reads notes/ any more, and the site_note
      rows from before stay as they are.

W2a (2026-10-29): the action 'ask' - Log as sent on the Ask vendors card
(INTERFACE.md section 5) - is a vendor request per party asked, source
'web', taken in like the others.

WA1 (2026-11-06): the action 'requester' - Who asked? on a case still
parked on (TBC) - sets the requester to a party from lists/parties, or with
partyId 0 writes the colleague's text as a note and leaves (TBC) for her.

W2b (2026-10-29): the action 'close' - Won or Lost with the mail that says
so (section 6) - is a pending status suggestion, source 'web' (site_close);
each pull also fetches its pasted mail, looks for its forwarded one and
writes actionResults again when she has accepted or dismissed it.

An action that throws stops the pull there: the ones before it stay applied
and answered, pulledAt does not move past it, the next run tries again.
pulledAt becomes the query's readTime only when every action up to it is
answered; firebase_sync writes it into meta/sync last.

Duplicates (her decision, 27 Oct): a quote, a follow-up or a question
logged on the site and the same one found in the mail are one event, and
the colleague's record wins. Site second: the mail's event of that kind on
that case within a day is voided (never deleted) and the site's carries its
[mid:]. Mail second: logged_on_site() tells the scan it is already there.

Q3 (2026-11-19, v56): after the actions, every run takes in the quote
card's quotes/{id} (and quoteCosts/{id}) of the live events carrying a
[quote:<id>] mark (site_quotes.take_quotes); nothing is written back.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field

from . import db, logs, site_close

SOURCE = "web"
TYPES = ("assign", "quote", "followup", "waiting", "answered", "ask", "close", "requester", "edit", "rates", "discard",
         "read", "waitreq", "inforec")
#: the site's 'how' -> status_event.channel
CHANNELS = {"Email": "mail", "Phone": "call", "WhatsApp": "whatsapp", "Teams": "teams", "Other": "other"}
#: the fields every action has; the rest are its type's
COMMON = ("type", "opp", "by", "name", "owner", "app", "at")
#: days apart within which a site's event and a mail's are the same one
WITHIN = 1
BATCH = 400
_OPP = re.compile(r"^OPP-\d{4,6}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MID = re.compile(r"\[mid:([^\]]+)\]")
#: W3: a Firestore document id, as the quote card writes quotes/{id}
_QUOTE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
#: the event of each kind a site action and a mail can both write
_TWIN_SQL = {
    "quote": "kind IN ('status','requote') AND status = 'Quoted'",
    "followup": "kind = 'followup'",
    "reply": "kind = 'note' AND note LIKE 'Reply from %'",
}
#: what the refusal says was not done, per type
_NOT_DONE = {"assign": "it was not assigned", "quote": "the quote was not logged",
             "followup": "the follow-up was not logged", "waiting": "the question was not logged",
             "answered": "the answer was not logged", "ask": "the rate requests were not logged",
             "close": "the Won or Lost was not sent", "requester": "the requester was not set",
             "edit": "nothing was changed", "rates": "the rates were not kept",
             "discard": "nothing was set aside", "read": "nothing was read",
             "waitreq": "it was not set to wait for the requester", "inforec": "nothing was changed"}


class Refused(Exception):
    """The action makes no sense for OppList; the message is for the person."""


@dataclass
class PullResult:
    found: int = 0                  # actions the query returned
    applied: int = 0
    refused: int = 0
    skipped: int = 0                # already taken in before
    answered: int = 0               # actionResults written
    deleted: int = 0                # RQ2: uploads/ parts deleted
    quotes: int = 0                 # Q3: the quote card's quotes taken in
    quotes_waiting: int = 0         # Q3: marks whose quote could not be taken in yet
    pulled_at: str = ""             # what meta/sync.pulledAt gets ('' = never pulled)
    error: str = ""
    lines: list = field(default_factory=list)

    def line(self) -> str:
        out = "team site: {} action{} taken in ({} applied, {} refused), {} answered".format(
            self.applied + self.refused, "" if self.applied + self.refused == 1 else "s", self.applied,
            self.refused, self.answered)
        if self.skipped:
            out += "; {} already taken in".format(self.skipped)
        if self.quotes or self.quotes_waiting:
            out += "; {} quote{} taken in".format(self.quotes, "" if self.quotes == 1 else "s") + (
                " ({} tried again at the next run)".format(self.quotes_waiting) if self.quotes_waiting else "")
        return out + ("; stopped: " + self.error if self.error else "")


# -------------------------------------------------------------- values --
def iso(value) -> str:
    """A Firestore moment as UTC text 'YYYY-MM-DDTHH:MM:SS.ffffffZ' (what
    site_action.at holds, and what a query compares with)."""
    if isinstance(value, dt.datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
        return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return str(value or "")


def who_of(action: dict) -> str:
    """The person as the colleagues know them: the name on the site, else
    their Handled by name, else their login before the @."""
    return (str(action.get("name") or "").strip() or str(action.get("owner") or "").strip()
            or str(action.get("by") or "").split("@")[0].strip() or "someone")


def site_by(conn) -> dict:
    """{status_event id: who logged it} for the events site actions wrote."""
    out = {}
    if not db._has_table(conn, "site_action"):
        return out
    for row in conn.execute("SELECT by_name, by_owner, by_email, event_ids FROM site_action "
                            "WHERE result = 'applied' AND event_ids IS NOT NULL"):
        name = who_of({"name": row["by_name"], "owner": row["by_owner"], "by": row["by_email"]})
        for event_id in json.loads(row["event_ids"] or "[]"):
            out[int(event_id)] = name
    return out


def logged_on_site(conn, enquiry_id, day, kind, sources=(SOURCE,)) -> bool:
    """Mail second (the duplicates rule): a live event of this kind
    ('quote', 'followup', 'reply') logged on the team site on this case
    within a day of `day` - the mail's one is already on the case. FX4:
    `sources` ('web', and 'manual' for a question logged on the case page)."""
    if not day:
        return False
    return conn.execute(
        "SELECT 1 FROM status_event WHERE enquiry_id = ? AND voided = 0 AND source IN ({}) AND {} "
        "AND ABS(julianday(substr(at, 1, 10)) - julianday(?)) <= ? LIMIT 1".format(
            ",".join("?" for _ in sources), _TWIN_SQL[kind]),
        (enquiry_id, *sources, str(day)[:10], WITHIN)).fetchone() is not None


def _void_mail_twins(repo, enquiry_id, day, kind) -> list:
    """Site second: the mail's live events of this kind within a day are
    struck out (kept, counting for nothing) and the case follows what is
    left. Returns their Message-IDs, for the site's event to carry."""
    conn = repo.conn
    mids = []
    for row in conn.execute(
            "SELECT id, note FROM status_event WHERE enquiry_id = ? AND voided = 0 AND source IN ('mail','scan') "
            "AND {} AND ABS(julianday(substr(at, 1, 10)) - julianday(?)) <= ? ORDER BY id".format(_TWIN_SQL[kind]),
            (enquiry_id, day, WITHIN)).fetchall():
        found = [m.strip() for m in _MID.findall(row["note"] or "")]
        if not found:
            # an accepted suggestion keeps its mail's Message-ID on the row
            made = conn.execute("SELECT message_id FROM suggestion WHERE event_ref = ? AND message_id IS NOT NULL",
                                ("status_event:{}".format(row["id"]),)).fetchone()
            found = [made["message_id"]] if made else []
        mids.extend(m for m in found if m not in mids)
        repo.update("status_event", row["id"], {"voided": 1})
        repo._restate(row["id"])
    return mids


def _with_mids(note, mids) -> str:
    return " ".join([(note or "").strip()] + ["[mid:{}]".format(m) for m in mids]).strip()


# ------------------------------------------------------------- actions --
def _day_of(action, today, opp, kind) -> str:
    """The action's date, today when it has none; a malformed one refused."""
    value = str(action.get("date") or "").strip()
    if not value:
        return today
    if not _DATE.match(value):
        raise Refused("{}: '{}' is not a date, so {}.".format(opp, value, _NOT_DONE[kind]))
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        raise Refused("{}: '{}' is not a date, so {}.".format(opp, value, _NOT_DONE[kind])) from None
    return value


def _owners(conn) -> list:
    from . import assign_list
    return assign_list.owners(conn)


def _open_quoted(case, kind) -> None:
    """Follow-up and question: on a quoted case only."""
    status = case["status"]
    if status in ("Won", "Lost", "Cancelled", "Dropped"):
        raise Refused("{} is closed ({}), so {}.".format(case["opp_no"], status, _NOT_DONE[kind]))
    if status != "Quoted":
        raise Refused("{} is {}, not Quoted, so {}.".format(case["opp_no"], status, _NOT_DONE[kind]))


def _assign(repo, case, action, who, today) -> str:
    to = str(action.get("to") or "").strip()
    if to and to not in _owners(repo.conn):
        raise Refused("'{}' is not a Handled by name in OppList, so {} was not assigned.".format(to, case["opp_no"]))
    if to == (case["owner"] or "").strip():
        return "{} was already {}.".format(case["opp_no"], "handled by " + to if to else "not assigned")
    # the way the Assign List's sheet set it (AL2, retired in CX1)
    repo.update("enquiry", case["id"], {"owner": to or None})
    repo.touch("enquiry", case["id"])
    return "{} is now {}.".format(case["opp_no"], "handled by " + to if to else "not assigned")


def _quote(repo, case, action, who, today) -> str:
    opp = case["opp_no"]
    if case["status"] in ("Won", "Lost", "Cancelled", "Dropped"):
        raise Refused("{} is closed ({}), so {}.".format(opp, case["status"], _NOT_DONE["quote"]))
    day = _day_of(action, today, opp, "quote")
    what = " ".join(str(action.get("what") or "").split())[:1000]
    sent_by = str(action.get("sentBy") or "").strip()
    note = "{} (sent by {})".format(what, sent_by) if what and sent_by else (what or (
        "Sent by {}".format(sent_by) if sent_by else ""))
    mids = _void_mail_twins(repo, case["id"], day, "quote")
    note = _with_mids(note, mids)
    # W3: the quote card's saved quotes/{id}, marked at the end for Q3
    # (rateFrom stays in the site_action fields only)
    quote_id = str(action.get("quoteId") or "").strip()
    if _QUOTE_ID.match(quote_id):
        note = "{} [quote:{}]".format(note, quote_id).strip()
    note = note or None
    status = repo.conn.execute("SELECT status FROM enquiry WHERE id = ?", (case["id"],)).fetchone()["status"]
    if status == "Quoted":
        # her Re-quoted button
        repo.requote(case["id"], day, note=note, source=SOURCE)
        said = "{} re-quoted on {}.".format(opp, day)
    else:
        # her Quoted button
        repo.set_status(case["id"], "Quoted", day, note=note, source=SOURCE)
        said = "{} is Quoted on {}.".format(opp, day)
    return said + (" The same quote found in the mail was replaced by this one." if mids else "")


def _followup(repo, case, action, who, today) -> str:
    opp = case["opp_no"]
    _open_quoted(case, "followup")
    day = _day_of(action, today, opp, "followup")
    how = str(action.get("how") or "Email").strip()
    channel = CHANNELS.get(how)
    if channel is None:
        raise Refused("{}: '{}' is not a way OppList knows a follow-up went out, so {}.".format(
            opp, how, _NOT_DONE["followup"]))
    mids = _void_mail_twins(repo, case["id"], day, "followup")
    note = " ".join(str(action.get("note") or "").split())[:1000]
    event = repo.add_followup(case["id"], day, channel=channel, note=_with_mids(note, mids) or None, source=SOURCE)
    seq = repo.conn.execute("SELECT seq FROM status_event WHERE id = ?", (event,)).fetchone()["seq"]
    said = "FU{} on {} logged ({}, {}).".format(seq, opp, how, day)
    # FX7: the mail's follow-up suggestion for that day is closed - the site's stands
    from . import suggestions
    if suggestions.close_logged_followups(repo, case["id"]):
        said += " The follow-up found in the mail that day was closed."
    if action.get("answered") is True:
        # they answered: a reply that does not ask, so the ladder reads it
        from . import detect_followups
        repo.add_note(case["id"], day, detect_followups.reply_note(
            case["requester_key"] or "?", "answered the follow-up", kind="noted"), source=SOURCE)
        said += " Their answer was logged too."
    return said + (" The same follow-up found in the mail was replaced by this one." if mids else "")


def _waiting(repo, case, action, who, today, source=SOURCE) -> str:
    """A question to us (Pending on us). FX4: the case page's Waiting on us
    writes the same, with source 'manual'."""
    from . import detect_followups
    opp = case["opp_no"]
    _open_quoted(case, "waiting")
    day = _day_of(action, today, opp, "waiting")
    what = " ".join(str(action.get("what") or "").split())
    if not what:
        raise Refused("{}: the question is empty, so {}.".format(opp, _NOT_DONE["waiting"]))
    owner = str(action.get("who") or "").strip()
    if owner and owner not in _owners(repo.conn):
        raise Refused("'{}' is not a Handled by name in OppList, so the question on {} was not logged.".format(
            owner, opp))
    mids = _void_mail_twins(repo, case["id"], day, "reply")
    # a reply that needs an answer, the requester as the sender: on
    # Pending on us at once (tracking reads the source 'web')
    note = detect_followups.reply_note(case["requester_key"] or "?", what, kind="asks")
    repo.add_note(case["id"], day, _with_mids(note, mids), source=source)
    said = "{} is on Pending on us: the question of {} was logged.".format(opp, day)
    if owner and owner != (case["owner"] or "").strip():
        repo.update("enquiry", case["id"], {"owner": owner})
        repo.touch("enquiry", case["id"])
        said += " Handled by is now {}.".format(owner)
    return said + (" The same reply found in the mail was replaced by this one." if mids else "")


def _answered(repo, case, action, who, today, source=SOURCE) -> str:
    """The question answered: off Pending on us. FX4: the case page's
    Answered writes the same, with source 'manual'."""
    from . import detect_followups, tracking
    opp = case["opp_no"]
    day = _day_of(action, today, opp, "answered")
    asked = tracking.replied_note(repo.conn, case["id"]) if case["status"] == "Quoted" else None
    if asked is None:
        return "{} had no question waiting, so nothing was written.".format(opp)
    # after the question, on its day too (a later id on the same date)
    at = max(day, (asked[0] or "")[:10])
    note = " ".join(str(action.get("note") or "").split())[:500]
    repo.add_note(case["id"], at, detect_followups.answer_note(who, note), source=source)
    return "{} left Pending on us: answered by {} on {}.".format(opp, who, at)


# ----------------------------------------------------------- W2a: ask --
#: mailboxes, not companies: such an address is a party of its own, keyed
#: by the address, and its domain is never remembered
FREE_MAIL = ("gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com", "icloud.com",
             "aol.com", "qq.com", "163.com", "126.com", "sina.com")
_EMAIL = re.compile(r"^[^@\s<>()\[\],;:\"]+@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$",
                    re.I)


def _is_free(domain) -> bool:
    from . import matching
    return domain in FREE_MAIL or matching.is_free_mail(domain)


def _names(items) -> str:
    """'A', 'A and B', 'A, B and C'."""
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _kept(conn, party_id):
    """The party a merged one went into (merged_into followed to the end)."""
    seen = set()
    while party_id is not None and party_id not in seen:
        seen.add(party_id)
        row = conn.execute("SELECT id, key, merged_into FROM party WHERE id = ?", (party_id,)).fetchone()
        if row is None:
            return None
        if row["merged_into"] is None:
            return row
        party_id = row["merged_into"]
    return None


def _key_taken(conn, key) -> bool:
    return conn.execute("SELECT 1 FROM party WHERE lower(trim(key)) = lower(trim(?))", (key,)).fetchone() is not None


def _has_contact(conn, party_id, address) -> bool:
    return conn.execute("SELECT 1 FROM contact WHERE party_id = ? AND lower(trim(email)) = ?",
                        (party_id, address.lower())).fetchone() is not None


def _add_contact(repo, party_id, address, who) -> bool:
    if _has_contact(repo.conn, party_id, address):
        return False
    repo.insert("contact", {"party_id": party_id, "email": address,
                            "notes": "Added from a rate request {} logged on the team site.".format(who)})
    return True


def _new_party(repo, key, who) -> int:
    return repo.add_party(key, type="Unknown", country="Unknown", source="manual",
                          notes="Added from a rate request {} logged on the team site.".format(who))


def _adhoc_party(repo, address, who) -> tuple:
    """(party id, new?) for a typed address that is not ours: the party of
    its domain; a mailbox's own party (found again by its contact); else a
    new party of type Unknown with the domain remembered and the address as
    its contact."""
    from . import intake, matching
    conn = repo.conn
    domain = matching.domain_of(address)
    if _is_free(domain):
        for r in conn.execute("SELECT party_id FROM contact WHERE lower(trim(email)) = ? ORDER BY id",
                              (address.lower(),)):
            kept = _kept(conn, r["party_id"])
            if kept is not None:
                return kept["id"], False
        new = not _key_taken(conn, address)
        party = _new_party(repo, address, who)
        _add_contact(repo, party, address, who)
        return party, new
    for match in matching.by_domain(conn, domain):
        kept = _kept(conn, match.party_id)
        if kept is not None:
            return kept["id"], False
    key = intake.suggested_party_name(domain) or domain
    if _key_taken(conn, key):
        key = domain
    new = not _key_taken(conn, key)
    party = _new_party(repo, key, who)
    matching.remember_domain(repo, party, domain)
    _add_contact(repo, party, address, who)
    return party, new


def _ask(repo, case, action, who, today) -> str:
    """Ask vendors, Log as sent (INTERFACE.md section 5): one vendor request
    per party asked - the listed keys, the typed addresses - asked_on the
    action's date, source 'web', the subject's thread key and a note the
    Mail card reads. A live request for the same party within a day is
    already there. The scan's pending vendor suggestion for the same party
    is dismissed: the colleague's record wins."""
    from . import case_mail, matching, parsing, parts as parts_mod, suggestions
    conn = repo.conn
    opp = case["opp_no"]
    if case["status"] in ("Won", "Lost", "Cancelled", "Dropped"):
        raise Refused("{} is closed ({}), so {}.".format(opp, case["status"], _NOT_DONE["ask"]))
    if case["handled_by"]:
        raise Refused("{} is handled by {}, so {}.".format(opp, case["handled_by"], _NOT_DONE["ask"]))
    day = _day_of(action, today, opp, "ask")
    subject = " ".join(str(action.get("subject") or "").split())[:300]
    vendors = [str(k or "").strip() for k in (action.get("vendors") or []) if str(k or "").strip()]
    parties, new, unknown, bad, ours = {}, [], [], [], []      # parties: id -> key, in order

    def take(party_id):
        row = conn.execute("SELECT key FROM party WHERE id = ?", (party_id,)).fetchone()
        parties.setdefault(party_id, row["key"])

    listed = {}                                                  # the key sent (casefold) -> party id
    for key in vendors:
        found = conn.execute("SELECT id FROM party WHERE lower(trim(key)) = lower(trim(?)) ORDER BY id",
                             (key,)).fetchone()
        kept = _kept(conn, found["id"]) if found else None
        if kept is None:
            if key not in unknown:
                unknown.append(key)
            continue
        listed[key.casefold()] = kept["id"]
        take(kept["id"])

    def well_formed(value):
        address = str(value or "").strip().strip("<>").strip()
        if not _EMAIL.match(address):
            if address and address not in bad:
                bad.append(address)
            return ""
        if db.is_internal_domain(conn, matching.domain_of(address)):
            if address not in ours:
                ours.append(address)
            return ""
        return address

    for item in action.get("adhoc") or []:
        address = well_formed(item.get("email") if isinstance(item, dict) else item)
        if not address:
            continue
        party, made = _adhoc_party(repo, address, who)
        if made:
            new.append(party)
        take(party)
    # an address typed for a listed vendor that had none: its contact now
    for key, value in (action.get("to") or {}).items() if isinstance(action.get("to"), dict) else []:
        party = listed.get(str(key or "").strip().casefold())
        address = well_formed(value) if party else ""
        if not address:
            continue
        _add_contact(repo, party, address, who)
        domain = matching.domain_of(address)
        if not _is_free(domain) and not conn.execute("SELECT 1 FROM party_domain WHERE party_id = ?",
                                                     (party,)).fetchone():
            matching.remember_domain(repo, party, domain)
    left = ["{} is not a vendor in OppList".format(k) for k in unknown] + \
           ["{} is not a mail address".format(a) for a in bad] + ["{} is one of ours".format(a) for a in ours]
    # Q2b: what they were asked to quote, and for which routes
    asked_parts, asked_routes, also_left = _ask_parts_routes(conn, case, action)
    left += also_left
    if not parties:
        raise Refused("{}: nothing to log, so {}{}.".format(
            opp, _NOT_DONE["ask"], " (" + "; ".join(left) + ")" if left else ""))
    note = "Logged on the team site by {}: {}{}".format(who, subject, case_mail.TEAM_COPIED if action.get(
        "teamCc") is True else "")
    thread_key = parsing.subject_thread_key(subject) or None
    logged, already = [], []
    for party, key in parties.items():
        there = conn.execute(
                "SELECT * FROM vendor_request WHERE enquiry_id = ? AND vendor_party_id = ? AND voided = 0 "
                "AND ABS(julianday(substr(asked_on, 1, 10)) - julianday(?)) <= ? ORDER BY id DESC",
                (case["id"], party, day, WITHIN)).fetchone()
        if there is not None:
            added = _widen_request(repo, case, there, asked_parts, asked_routes)
            already.append("{}, now also {}".format(key, added) if added else key)
            continue
        repo.insert("vendor_request", {"enquiry_id": case["id"], "vendor_party_id": party, "asked_on": day,
                                       "source": SOURCE, "thread_key": thread_key, "notes": note,
                                       "parts": parts_mod.join(asked_parts),
                                       "route_ids": _route_ids_of(conn, case, asked_routes)})
        logged.append(key)
    if logged:
        repo.confirm_status(case["id"])
    # the scan's suggestion of the same request: the colleague's record wins
    dismissed = []
    for s in conn.execute("SELECT id, vendor_key, vendor_domain, note FROM suggestion WHERE enquiry_id = ? "
                          "AND kind = 'vendor' AND state = 'pending' ORDER BY id", (case["id"],)).fetchall():
        for party, key in parties.items():
            domains = {r[0] for r in conn.execute("SELECT domain FROM party_domain WHERE party_id = ?", (party,))}
            if (s["vendor_key"] or "").strip().casefold() == key.casefold() or \
                    (s["vendor_domain"] or "").strip().lower() in domains:
                suggestions._record(repo, s["id"], "dismissed", None, False, "")
                said = "Logged on the team site by {} on {}".format(who, day)
                repo.update("suggestion", s["id"], {"note": "{} · {}".format(s["note"], said) if s["note"] else said})
                dismissed.append(key)
                break
    out = ["{}: {} {} logged on {}.".format(opp, "rate requests to" if len(logged) > 1 else "a rate request to",
                                            _names(logged), day) if logged
           else "{}: no new rate request logged.".format(opp)]
    if already:
        out.append("Already on the case: {}.".format("; ".join(already) if any(", now also" in a for a in already)
                                                     else _names(already)))
    made = [parties[p] for p in new if p in parties]
    if made:
        out.append("{} {} new in OppList (Unknown).".format(_names(made), "is" if len(made) == 1 else "are"))
    if left:
        out.append("Left out: {}.".format("; ".join(left)))
    if dismissed:
        out.append("The mail's suggestion for {} was dismissed.".format(_names(dismissed)))
    return " ".join(out)


# ------------------------------------------------- Q2b: parts and routes --
def _live_routes(conn, case_id) -> list:
    if not db._has_table(conn, "enquiry_route"):
        return []
    return conn.execute("SELECT id, seq FROM enquiry_route WHERE enquiry_id = ? AND voided = 0 ORDER BY seq",
                        (case_id,)).fetchall()


def _ask_parts_routes(conn, case, action) -> tuple:
    """(parts in their order, the route ids named, what was left out): a
    part name that is not one of the six, or a route number that is not a
    live route of the case, is left out and named."""
    from . import parts as parts_mod
    raw_parts = action.get("parts") if isinstance(action.get("parts"), list) else []
    known, unknown = parts_mod.read(raw_parts)
    left = ["'{}' is not a part OppList knows".format(u) for u in unknown]
    by_seq = {r["seq"]: r["id"] for r in _live_routes(conn, case["id"])}
    ids = []
    raw_routes = action.get("routes") if isinstance(action.get("routes"), list) else []
    for n in raw_routes[:10]:
        try:
            seq = int(n)
        except (TypeError, ValueError):
            seq = None
        if seq in by_seq:
            if by_seq[seq] not in ids:
                ids.append(by_seq[seq])
        else:
            left.append("route {} is not a route of {}".format(n, case["opp_no"]))
    return known, ids, left


def _route_ids_of(conn, case, ids) -> str | None:
    """routes.format_ids of the routes named; None when none is named or
    they are every live route (the request covers every route)."""
    from . import routes as routes_mod
    live = {r["id"] for r in _live_routes(conn, case["id"])}
    chosen = set(ids or []) & live
    if not chosen or chosen == live:
        return None
    return routes_mod.format_ids(chosen)


def _widen_request(repo, case, request, asked_parts, asked_routes) -> str:
    """A live request already there within a day takes the new parts and
    routes too (both lists together; one covering every route still does),
    through the repository. Returns what was added ('Inland, route B'),
    '' when nothing."""
    from . import parts as parts_mod, routes as routes_mod
    conn = repo.conn
    values, added = {}, []
    if asked_parts:
        merged = parts_mod.union(request["parts"], asked_parts)
        new = [p for p in parts_mod.split(merged) if p not in parts_mod.split(request["parts"])]
        if new:
            values["parts"], added = merged, added + new
    if (asked_parts or asked_routes) and request["route_ids"]:
        # an ask that names no route is for every route (an ask with neither
        # parts nor routes leaves the request as it is)
        had = set(routes_mod.parse_ids(request["route_ids"]) or [])
        merged = _route_ids_of(conn, case, had | set(asked_routes)) if asked_routes else None
        if merged != request["route_ids"]:
            values["route_ids"] = merged
            letters = {r["id"]: routes_mod.letter(r["seq"]) for r in _live_routes(conn, case["id"])}
            gained = [letters[i] for i in sorted(set(asked_routes) - had, key=lambda i: letters.get(i, "")) if i in letters]
            if merged is None:
                added.append("every route")
            elif gained:
                added.append("route{} {}".format("s" if len(gained) > 1 else "", ", ".join(gained)))
    if values:
        repo.update("vendor_request", request["id"], values)
        repo.touch("vendor_request", request["id"])
    return ", ".join(added)


# ------------------------------------------------------ WA1: requester --
TEXT_MAX = 300


def _party_id(value) -> int | None:
    """partyId as the site sends it (an integer, maybe as a float or text);
    None when it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def _requester(repo, case, action, who, today) -> str:
    """Who asked? on a (TBC) case: a party sets the requester (SC1: scope
    is not touched); a text alone is only a note, and (TBC) stays for her."""
    from . import direct_customers_out as dco
    conn, opp = repo.conn, case["opp_no"]
    if not dco.is_tbc(conn, case["requester_party_id"]):
        raise Refused("{} already has its requester ({}), so {}.".format(
            opp, case["requester_key"] or "?", _NOT_DONE["requester"]))
    raw = action.get("partyId")
    party_id = _party_id(raw) if raw not in (None, "") else 0
    if party_id is None:
        raise Refused("{}: '{}' is not a party in OppList, so {}.".format(opp, raw, _NOT_DONE["requester"]))
    text = " ".join(str(action.get("text") or "").split())[:TEXT_MAX]
    if not party_id:
        if not text:
            raise Refused("{}: no party and no text, so {}.".format(opp, _NOT_DONE["requester"]))
        repo.add_note(case["id"], today, "Who asked, from the team site, {}: {}".format(who, text), source=SOURCE)
        return "{}: your note on who asked was logged; Iris sets the requester.".format(opp)
    party = conn.execute("SELECT id, key, merged_into, placeholder FROM party WHERE id = ?",
                         (party_id,)).fetchone()
    if party is None:
        raise Refused("{}: party {} is not in OppList, so {}.".format(opp, party_id, _NOT_DONE["requester"]))
    if party["merged_into"] is not None:
        raise Refused("{}: {} was merged into another party, so {}.".format(
            opp, party["key"], _NOT_DONE["requester"]))
    if party["placeholder"]:
        raise Refused("{}: {} is a placeholder, so {}.".format(opp, party["key"], _NOT_DONE["requester"]))
    repo.save_enquiry(case["id"], {"requester_party_id": party["id"]})
    note = "Requester from the team site, {}: {}".format(who, party["key"])
    repo.add_note(case["id"], today, note + ("; " + text if text else ""), source=SOURCE)
    return "{}: the requester is now {}.".format(opp, party["key"])


# ------------------------------------------- WR2: waiting for requester --
def _short_day(day) -> str:
    """'2026-11-17' -> 'Nov 17' (the site's words)."""
    d = dt.date.fromisoformat(day)
    return "{} {}".format(d.strftime("%b"), d.day)


def _waitreq(repo, case, action, who, today) -> str:
    """Her Waiting for requester button (WR1), from the site: the same
    note, source web, the case staying New; a live wait is replaced."""
    from . import wait
    opp, not_done = case["opp_no"], _NOT_DONE["waitreq"]
    status = case["status"]
    if status in ("Won", "Lost", "Cancelled", "Dropped"):
        raise Refused("{} is closed ({}), so {}.".format(opp, status, not_done))
    if status != "New":
        raise Refused("{} is {} now, so {}.".format(opp, status, not_done))
    if (case["handled_by"] or "").strip():
        raise Refused("{} is handled by {}, so {}.".format(opp, case["handled_by"], not_done))
    pending = site_close.waiting(repo.conn, case["id"])
    if pending is not None:
        raise Refused("{} has a {} waiting to be confirmed, so {}.".format(
            opp, pending["proposed_status"] or "Won or Lost", not_done))
    day = _day_of(action, today, opp, "waitreq")
    what = " ".join(str(action.get("what") or "").split())
    if not what:
        raise Refused("{}: what we need is empty, so {}.".format(opp, not_done))
    if len(what) > wait.WHAT_MAX:
        raise Refused("{}: what we need is longer than {} characters, so {}.".format(opp, wait.WHAT_MAX, not_done))
    event_id, why = wait.start(repo, case["id"], what, day, source=SOURCE)
    if event_id is None:
        raise Refused("{}: {}".format(opp, why))
    return "Waiting for the requester since {}.".format(_short_day(day))


def _inforec(repo, case, action, who, today) -> str:
    """Her Info received button (WR1), from the site; no live wait (it
    ended meanwhile): accepted, nothing written."""
    from . import wait
    day = _day_of(action, today, case["opp_no"], "inforec")
    if wait.live(repo.conn, case["id"]) is None:
        return "It was not waiting for the requester any more."
    event_id, _why = wait.info_received(repo, case["id"], day, source=SOURCE)
    if event_id is None:
        return "It was not waiting for the requester any more."
    at = repo.conn.execute("SELECT at FROM status_event WHERE id = ?", (event_id,)).fetchone()["at"]
    return "Info received on {}; the case waits for our quote again.".format(_short_day((at or day)[:10]))


_HANDLERS = {"assign": _assign, "quote": _quote, "followup": _followup, "waiting": _waiting,
             "answered": _answered, "ask": _ask, "requester": _requester, "waitreq": _waitreq,
             "inforec": _inforec}


def take_in(repo, action_id: str, action: dict, today: str, client=None) -> tuple:
    """Apply one action and record it in site_action, in one transaction
    the caller commits. Returns (ok, message). Raises on anything that is
    not a refusal - the caller rolls back and stops. Q2a: `client` reads a
    rates action's vendorRates/{id} document."""
    conn = repo.conn
    kind = str(action.get("type") or "").strip()
    opp = str(action.get("opp") or "").strip().upper()
    who = who_of(action)
    case = conn.execute("SELECT * FROM v_enquiry WHERE opp_no = ?", (opp,)).fetchone() if _OPP.match(opp) else None
    before = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM status_event").fetchone()[0])
    try:
        if kind not in TYPES:
            raise Refused("OppList does not know the action '{}', so nothing was done on {}.".format(kind, opp or "-"))
        if case is None and kind == "read":
            # RQ2: the site shows it after "Could not read it: " (never naming OppList)
            raise Refused("the case is not in the lists")
        if case is None:
            result = str(action.get("result") or "").strip()
            raise Refused("{} is not in OppList, so {}.".format(opp or "That case", "the {} was not sent".format(
                result) if kind == "close" and result in site_close.RESULTS else _NOT_DONE[kind]))
        if kind == "close":
            message, ok = site_close.take(repo, case, action, who, today, action_id, Refused), True
        elif kind == "rates":
            # Q2a: a vendor's rates typed on the site, kept as a new version
            from . import site_rates
            try:
                message, ok = site_rates.take(repo, case, action, who, today, action_id, client), True
            except site_rates.Refused as exc:
                raise Refused(str(exc)) from None
        elif kind == "discard":
            # Q2d: a Read quote draft set aside on the site
            from . import read_quote
            try:
                message, ok = read_quote.take_discard(repo, case, action, who, today), True
            except read_quote.Refused as exc:
                raise Refused(str(exc)) from None
        elif kind == "read":
            # RQ2: a vendor's reply sent in to be read; the files fetched and kept, read by the app
            from . import site_read
            try:
                message, ok = site_read.take(repo, case, action, who, today, action_id, client), True
            except site_read.Refused as exc:
                raise Refused(str(exc)) from None
        elif kind == "edit":
            # D7b: a cargo line or a route changed, added or struck on the site
            from . import site_edit
            try:
                message, ok = site_edit.take(repo, case, action, who, today, action_id), True
            except site_edit.Refused as exc:
                raise Refused(str(exc)) from None
        else:
            message, ok = _HANDLERS[kind](repo, case, action, who, today), True
    except Refused as exc:
        message, ok = str(exc), False
    events = [r[0] for r in conn.execute("SELECT id FROM status_event WHERE id > ? ORDER BY id", (before,))] \
        if ok else []
    fields = {k: v for k, v in action.items() if k not in COMMON}
    repo.insert("site_action", {
        "id": action_id, "type": kind or "?", "opp_no": opp or None, "enquiry_id": case["id"] if case else None,
        "by_email": str(action.get("by") or "") or None, "by_name": str(action.get("name") or "") or None,
        "by_owner": str(action.get("owner") or "") or None, "at": iso(action.get("at")),
        "fields": json.dumps(fields, sort_keys=True, default=str, ensure_ascii=False),
        "result": "applied" if ok else "refused", "message": message,
        "event_ids": json.dumps(events) if events else None,
        "change_action": repo.action_id if ok else None,
    })
    return ok, message


def actions_query(after: str) -> dict:
    return {"from": [{"collectionId": "actions"}],
            "where": {"fieldFilter": {"field": {"fieldPath": "at"}, "op": "GREATER_THAN",
                                      "value": {"timestampValue": after}}},
            "orderBy": [{"field": {"fieldPath": "at"}, "direction": "ASCENDING"}]}


EPOCH_TEXT = "1970-01-01T00:00:00Z"


def _answer(client, repo, result) -> None:
    """actionResults/{id} for every action taken in and not answered yet,
    then answered_at; a failure leaves the rest for the next run."""
    from . import site_read
    from .firebase_sync import Write
    conn = repo.conn
    rows = conn.execute("SELECT * FROM site_action WHERE answered_at IS NULL ORDER BY at, id").fetchall()

    def doc(r):
        out = {"ok": site_close.answer_ok(conn, r), "msg": r["message"] or "", "by": r["by_email"] or "",
               "type": r["type"], "opp": r["opp_no"] or ""}
        if r["type"] == site_read.TYPE:
            out["state"] = site_read.state_of(conn, r)        # RQ2: queued, then done
        return out

    for start in range(0, len(rows), BATCH):
        chunk = rows[start:start + BATCH]
        client.commit([Write("actionResults/" + r["id"], doc(r), server_time=("at",)) for r in chunk])
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for r in chunk:
            repo.update("site_action", r["id"], {"answered_at": stamp})
        repo.commit()
        result.answered += len(chunk)


def pull_actions(conn, client, state: dict, today: str, result: PullResult) -> None:
    from . import site_edit
    repo = db.Repository(conn, source=SOURCE)
    site_edit.reset_official()                  # D7b: the official files, read once per pull
    after = state.get("pulled_at") or EPOCH_TEXT
    found = client.run_query(actions_query(after))
    result.found = len(found["docs"])
    done_at, stopped = after, False
    for path, action in found["docs"]:
        action_id = path.rsplit("/", 1)[-1]
        at = iso(action.get("at"))
        if conn.execute("SELECT 1 FROM site_action WHERE id = ?", (action_id,)).fetchone():
            result.skipped += 1                          # never applied twice
            done_at = at
            continue
        try:
            ok, message = take_in(repo, action_id, action, today, client=client)
            repo.commit()
        except Exception as exc:                         # stop here; the next run tries again
            conn.rollback()
            result.error = "action {} ({} {}): {}: {}".format(action_id, action.get("type"), action.get("opp"),
                                                             type(exc).__name__, exc)
            logs.get("firebase").warning("team site pull stopped: %s", result.error)
            stopped = True
            # pulledAt stays before this action (an equal time stays too)
            if done_at >= at:
                done_at = after
            break
        result.applied += ok
        result.refused += not ok
        result.lines.append("{} {} {}: {}".format(at[:19], action.get("type"), action.get("opp"), message))
        done_at = at
    # W2b: the closes still waiting, then her decisions on them
    site_close.refresh(repo, client)
    site_close.answers(repo)
    site_edit.answers(repo)                     # D7b: her Accept or Dismiss of a change that met hers
    _answer(client, repo, result)
    # RQ2: the uploads of the read actions done with, and once a day the parts left with no action
    from . import site_read
    try:
        result.deleted = site_read.clean_uploads(conn, client, state, today)
    except Exception as exc:                              # never stops the pull; the next run tries again
        logs.get("firebase").warning("team site: uploads not cleaned: %s: %s", type(exc).__name__, exc)
    # every action up to here is applied and answered
    state["pulled_at"] = found["readTime"] or after if not stopped else done_at


def pull(conn, client, state: dict, today: str) -> PullResult:
    """Actions and their answers. Never raises: what stopped it is in
    result.error and the push goes on."""
    result = PullResult()
    try:
        pull_actions(conn, client, state, today, result)
    except Exception as exc:
        conn.rollback()
        result.error = result.error or "{}: {}".format(type(exc).__name__, exc)
    # Q3: the quote card's quotes of the events taken in (this run's and before), read only
    from . import site_quotes
    try:
        result.quotes, result.quotes_waiting = site_quotes.take_quotes(conn, client, state, today)
    except Exception as exc:                              # never stops the run; the next run tries again
        conn.rollback()
        logs.get("firebase").warning("team site: quotes not taken in: %s: %s", type(exc).__name__, exc)
    result.pulled_at = state.get("pulled_at") or ""
    return result
