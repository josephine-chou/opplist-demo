"""The team site's Won or Lost (W2b, 2026-10-29): a colleague says how a
case ended and adds the mail that says so; OppList takes it in as one
pending status suggestion, source 'web', and the case changes only when
she accepts it in Suggested updates (..\\OppListWeb\\INTERFACE.md section 6).

  (1) take(): the action 'close', refused with a message for the colleague
      or written as the suggestion; the mail scan's pending closing
      suggestion for the same case is dismissed (the colleague's record
      wins, as in W1b and W2a);
  (2) refresh(): every pull, for each close still waiting - the pasted
      mail (evidence/{id}) saved under data\\site-evidence\\<id>\\, the
      forwarded one looked for in the mail archive until it is there;
  (3) answers(): her decision as actionResults/{id} again - confirmed, not
      confirmed, waiting again after Put back - once per decision;
  (4) pending_closes(): what lists/inquiries and cases/* push as
      pendingClose.

The suggestion and its action are tied by '[site:<action id>]' in the
suggestion's note (the way [mid:] ties an event to its mail); no column.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
from pathlib import Path

from . import db, paths

RESULTS = ("Won", "Lost")
CLOSED = ("Won", "Lost", "Cancelled", "Dropped")
FOLDER = "site-evidence"
FORWARDED = "Forwarded to Iris"
UNREADABLE = "The pasted mail could not be read."
QUOTE_MAX = 400
REASON_MAX = 120
MAX_IMAGES = 3
#: a party's type -> whose rate it was (status.RATE_USED)
RATE_OF_TYPE = {"Office": "office", "Carrier": "carrier", "Coloader": "carrier", "Trucker": "carrier"}
_MARK = re.compile(r"\[site:([^\]]+)\]")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_JPEG = re.compile(r"^data:image/jpeg;base64,(.+)$", re.S)
IMAGE_NAME = re.compile(r"^[1-9]\.jpg$")


def mark(action_id: str) -> str:
    return "[site:{}]".format(action_id)


def action_id_of(row) -> str:
    """The site close a suggestion came from, '' for any other (D7b: an
    edit's suggestion is site_edit's, not a close)."""
    if row is None or (row["source"] or "") != "web" or row["kind"] != "status":
        return ""
    found = _MARK.search(row["note"] or "")
    return found.group(1) if found else ""


def evidence_dir(action_id: str) -> Path:
    """data\\site-evidence\\<id> (data\\ is never in git)."""
    if not _ID.match(action_id or ""):
        raise ValueError("not a site action id: {!r}".format(action_id))
    return Path(paths.DATA_DIR) / FOLDER / action_id


def _web_closes(conn, where="", args=()) -> list:
    return conn.execute(
        "SELECT * FROM suggestion WHERE source = 'web' AND kind = 'status' AND instr(note, '[site:') > 0 "
        "{} ORDER BY id".format(where), args).fetchall()


def waiting(conn, enquiry_id):
    """The close from the site still pending on this case, or None."""
    rows = _web_closes(conn, "AND enquiry_id = ? AND state = 'pending'", (enquiry_id,))
    return rows[0] if rows else None


def suggestion_of(conn, action_id):
    rows = _web_closes(conn, "AND instr(note, ?) > 0", (mark(action_id),))
    return rows[-1] if rows else None


# ------------------------------------------------- FX3: on the Timeline --
#: the event's note when the colleague forwarded the mail
FORWARDED_NOTE = "Mail forwarded to Iris"
#: the once-only repair of the closes accepted before FX3
REPAIR_SETTING = "fx3_site_events_repaired"


def _bare(text) -> str:
    return re.sub(r"[^0-9a-z]+", "", str(text or "").casefold())


def event_note(row) -> str:
    """The note of the event a site close writes: the pasted text when it
    says more than the reason (cut at QUOTE_MAX), FORWARDED_NOTE when the
    mail was forwarded, '' otherwise - never the sender and the quote."""
    quote = (row["evidence_quote"] or "").strip()
    if quote == FORWARDED:
        return FORWARDED_NOTE
    if not quote or quote == UNREADABLE:
        return ""
    bare = _bare(quote)
    if not bare or bare in _bare(row["closed_reason"]):
        return ""
    return quote[:QUOTE_MAX]


def add_event(repo, action_id: str, event_id: int) -> None:
    """Record a status event in the site action's event_ids (so the case
    shows "team site · <who>" on it, firebase_pull.site_by)."""
    action = _action(repo.conn, action_id)
    if action is None:
        return
    try:
        ids = [int(i) for i in json.loads(action["event_ids"] or "[]")]
    except (TypeError, ValueError):
        ids = []
    if event_id not in ids:
        repo.update("site_action", action_id, {"event_ids": json.dumps(ids + [event_id])})


def repair_once(conn) -> list:
    """FX3, once: the site closes she accepted before FX3 went on the
    Timeline as source mail with "<sender> <quote>" and outside the
    action's event_ids. Each such live event is made the site's: source
    web, the note event_note() gives (when it still holds what the old
    Accept wrote; one she edited stays), and its id in event_ids - through
    the repository, source migration, one action. The startup lines; []
    when it ran before. Commits."""
    if db.get_setting(conn, REPAIR_SETTING):
        return []
    repo = db.Repository(conn, source="migration")
    repo.pin_action()
    done = []
    for s in _web_closes(conn, "AND state = 'accepted' AND event_ref LIKE 'status_event:%'"):
        action_id = action_id_of(s)
        action = _action(conn, action_id)
        event = conn.execute("SELECT * FROM status_event WHERE id = ?",
                             (int(s["event_ref"].split(":")[1]),)).fetchone()
        if action is None or event is None or event["voided"]:
            continue
        ids = json.loads(action["event_ids"] or "[]")
        if event["id"] in ids and event["source"] == "web":
            continue
        values = {"source": "web"}
        old_note = " ".join(p for p in ((s["evidence_sender"] or "").strip(), (s["evidence_quote"] or "").strip())
                            if p)[:QUOTE_MAX]
        if (event["note"] or "") == old_note:
            values["note"] = event_note(s) or None
        repo.update("status_event", event["id"], values)
        add_event(repo, action_id, event["id"])
        done.append("{} {} (status_event {})".format(s["opp_no"], event["status"], event["id"]))
    db.set_setting(conn, REPAIR_SETTING, dt.date.today().isoformat())
    conn.commit()
    if not done:
        return ["Team site closes on the Timeline: nothing to repair."]
    return ["Team site closes on the Timeline repaired: {}.".format(", ".join(done))]


def _action(conn, action_id):
    if not action_id or not db._has_table(conn, "site_action"):
        return None
    return conn.execute("SELECT * FROM site_action WHERE id = ?", (action_id,)).fetchone()


def _fields(action_row) -> dict:
    try:
        return json.loads(action_row["fields"] or "{}") if action_row is not None else {}
    except ValueError:
        return {}


def _evidence_kind(value) -> str:
    return "forwarded" if str(value or "").strip() == "forwarded" else "pasted"


# ----------------------------------------------------------- take in --
def take(repo, case, action, who, today, action_id, refused) -> str:
    """The action 'close': refused (raises `refused` with the message for
    the colleague) or one pending status suggestion, source 'web'."""
    from . import suggestions
    conn = repo.conn
    opp = case["opp_no"]
    result = str(action.get("result") or "").strip()
    if result not in RESULTS:
        raise refused("{}: '{}' is not Won or Lost, so nothing was sent.".format(opp, result))
    not_done = "the {} was not sent".format(result)
    if case["status"] in CLOSED:
        raise refused("{} is closed ({}) already, so {}.".format(opp, case["status"], not_done))
    if case["handled_by"]:
        raise refused("{} is handled by {}, so {}.".format(opp, case["handled_by"], not_done))
    day = str(action.get("date") or "").strip() or today
    try:
        if not _DATE.match(day):
            raise ValueError(day)
        dt.date.fromisoformat(day)
    except ValueError:
        raise refused("{}: '{}' is not a date, so {}.".format(opp, day, not_done)) from None
    if day > today:
        raise refused("{}: {} is after today, so {}.".format(opp, day, not_done))
    before = waiting(conn, case["id"])
    if before is not None:
        raise refused("{} already has a {} waiting for Iris.".format(opp, before["proposed_status"]))
    reason = " ".join(str(action.get("reason") or "").split())[:REASON_MAX] if result == "Lost" else ""
    forwarded = _evidence_kind(action.get("evidence")) == "forwarded"
    repo.insert("suggestion", {
        "enquiry_id": case["id"], "opp_no": opp, "kind": "status", "state": "pending", "source": "web",
        "proposed_status": result, "closed_reason": reason or None, "proposed_date": day,
        "evidence_sender": who, "evidence_date": day,
        # the pasted text comes with refresh(), straight after
        "evidence_quote": FORWARDED if forwarded else None, "confidence": "high",
        "note": "Sent on the team site by {} {}".format(who, mark(action_id)),
    })
    # the mail scan's closing for the same case: the colleague's record wins
    said = "{} sent {} on the team site on {}".format(who, result, day)
    for s in conn.execute("SELECT id, note FROM suggestion WHERE enquiry_id = ? AND kind = 'status' "
                          "AND state = 'pending' AND source != 'web' AND proposed_status IN ({}) ORDER BY id".format(
                              ",".join("?" for _ in CLOSED[:3])), (case["id"],) + CLOSED[:3]).fetchall():
        suggestions._record(repo, s["id"], "dismissed", None, False, "")
        repo.update("suggestion", s["id"], {"note": "{} · {}".format(s["note"], said) if s["note"] else said})
    return pending_message(opp, result)


def pending_message(opp, result) -> str:
    return "{} {} is waiting for Iris's confirmation.".format(opp, result)


# ---------------------------------------------------- evidence, mail --
def _save_pasted(action_id, doc) -> str:
    """The pictures and the text of evidence/{id} into its folder; the text
    ('' when none). Raises ValueError when nothing in it can be saved."""
    images, text = [], ""
    if isinstance(doc, dict):
        for item in (doc.get("images") or [])[:MAX_IMAGES]:
            found = _JPEG.match(str(item or ""))
            if found:
                try:
                    images.append(base64.b64decode(found.group(1), validate=False))
                except (ValueError, TypeError):
                    pass
        text = str(doc.get("text") or "").strip()
    if not images and not text:
        raise ValueError("nothing readable")
    folder = evidence_dir(action_id)
    folder.mkdir(parents=True, exist_ok=True)
    for n, data in enumerate(images, start=1):
        (folder / "{}.jpg".format(n)).write_bytes(data)
    if text:
        (folder / "text.txt").write_text(text, encoding="utf-8")
    return text


def _fetch_pasted(repo, client, s, action_id) -> None:
    try:
        doc = client.get("evidence/" + action_id)
        text = _save_pasted(action_id, doc)
    except Exception:                                   # missing or unreadable: never a refusal
        quote = UNREADABLE
    else:
        quote = " ".join(text.split())[:QUOTE_MAX] if text else "Pictures of the mail"
    if (s["evidence_quote"] or "") != quote:
        repo.update("suggestion", s["id"], {"evidence_quote": quote})


def _forwarded_mail(search, opp, address, day):
    """The colleague's mail with the case number in its subject, sent on or
    after the day before `day`; the first one, or None."""
    if search is None or not address:
        return None
    since = (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat() if _DATE.match(day or "") else ""
    for mail in search.by_subject(opp):
        if (mail.sender_address or "").strip().casefold() != address.strip().casefold():
            continue
        if (mail.received_on or "") >= since:
            return mail
    return None


def _look_for_forwarded(repo, search, s, action) -> None:
    mail = _forwarded_mail(search, s["opp_no"], action["by_email"] or "", s["proposed_date"] or "")
    if mail is None:
        return
    quote = "{} · {} · {}".format(FORWARDED, (mail.received_at or mail.received_on or "")[:16], mail.subject or "")
    repo.update("suggestion", s["id"], {"entry_id": mail.entry_id or None, "message_id": mail.message_id or None,
                                        "evidence_quote": quote[:QUOTE_MAX]})


def _open_archive():
    from . import assign_list
    return assign_list.archive_search()


def refresh(repo, client, open_search=_open_archive) -> None:
    """Every pull, for each close still waiting: the pasted mail fetched
    when its folder is not there yet, the forwarded one looked for in the
    mail archive (opened only then, read only) until it is found. A failure
    here never stops the pull."""
    conn = repo.conn
    search, opened = None, False
    try:
        for s in _web_closes(conn, "AND state = 'pending'"):
            action_id = action_id_of(s)
            action = _action(conn, action_id)
            if action is None:
                continue
            try:
                if _evidence_kind(_fields(action).get("evidence")) == "forwarded":
                    if not s["entry_id"]:
                        if not opened:
                            search, opened = open_search(), True
                        _look_for_forwarded(repo, search, s, action)
                elif not evidence_dir(action_id).is_dir():
                    _fetch_pasted(repo, client, s, action_id)
            except Exception:
                continue
    finally:
        if search is not None:
            search.__exit__(None, None, None)
    repo.commit()


# ------------------------------------------------------------ answers --
def answer_of(s) -> tuple:
    """(ok, msg) for the colleague, from where the suggestion stands."""
    opp, result = s["opp_no"], s["proposed_status"]
    if s["state"] == "pending":
        return True, pending_message(opp, result)
    if s["state"] == "dismissed":
        return False, "Iris did not confirm {} on {}.".format(result, opp)
    decided = (s["decided_as"] or "").capitalize()
    if decided in RESULTS:
        return True, "Iris confirmed {} on {}.".format(decided, opp)
    return True, "Iris recorded {} as {}.".format(opp, decided or "seen")


def answers(repo) -> int:
    """Each close whose answer changed since it was last written: the new
    message, and actionResults/{id} to write again (answered_at cleared).
    Returns how many."""
    conn = repo.conn
    if not db._has_table(conn, "site_action"):
        return 0
    changed = 0
    for a in conn.execute("SELECT id, message FROM site_action WHERE type = 'close' AND result = 'applied'").fetchall():
        s = suggestion_of(conn, a["id"])
        if s is None:
            continue
        msg = answer_of(s)[1]
        if msg != (a["message"] or ""):
            repo.update("site_action", a["id"], {"message": msg, "answered_at": None})
            changed += 1
    if changed:
        repo.commit()
    return changed


def answer_ok(conn, action_row) -> bool:
    """actionResults' ok: False for a refusal and for a close she did not
    confirm."""
    if action_row["result"] != "applied":
        return False
    if action_row["type"] == "edit":
        from . import site_edit
        return site_edit.answer_ok(conn, action_row)
    if action_row["type"] == "read":
        from . import site_read                       # RQ2: a reading that found no prices is not ok
        return site_read.answer_ok(conn, action_row)
    if action_row["type"] != "close":
        return True
    s = suggestion_of(conn, action_row["id"])
    return s is None or answer_of(s)[0]


# --------------------------------------------------------------- push --
def pending_closes(conn) -> dict:
    """{opp: pendingClose} for every close from the site still waiting."""
    out = {}
    for s in _web_closes(conn, "AND state = 'pending'"):
        action_id = action_id_of(s)
        action = _action(conn, action_id)
        if action is None:
            continue
        out[s["opp_no"]] = {"result": s["proposed_status"], "by": s["evidence_sender"] or "",
                            "at": _fields(action).get("date") or s["proposed_date"] or "", "id": action_id,
                            "evidence": _evidence_kind(_fields(action).get("evidence"))}
    return out


# ------------------------------------------------------------- the page --
def rate_for(conn, row):
    """Won's rate used from the action's rateFrom: (rate_used, party_id,
    vendor_request_id, note), or None when not sure (R6 asks later)."""
    from . import firebase_pull
    action = _action(conn, action_id_of(row))
    rate_from = str(_fields(action).get("rateFrom") or "").strip()
    if not rate_from:
        return None
    if rate_from.casefold() == "own":
        return ("own", None, None, None)
    for r in conn.execute(
            "SELECT v.id, v.vendor_party_id, p.key FROM vendor_request v JOIN party p ON p.id = v.vendor_party_id "
            "WHERE v.enquiry_id = ? AND v.voided = 0 ORDER BY v.asked_on, v.id", (row["enquiry_id"],)):
        if (r["key"] or "").strip().casefold() != rate_from.casefold():
            continue
        kept = firebase_pull._kept(conn, r["vendor_party_id"])
        if kept is None:
            continue
        ptype = conn.execute("SELECT type FROM party WHERE id = ?", (kept["id"],)).fetchone()["type"]
        rate = RATE_OF_TYPE.get(ptype, "agent")
        return (rate, kept["id"], r["id"] if rate == "agent" else None, None)
    return None


def view_of(conn, row):
    """What Suggested updates shows for a close from the site, None for any
    other suggestion."""
    from . import status as status_mod
    action_id = action_id_of(row)
    if not action_id:
        return None
    action = _action(conn, action_id)
    evidence = _evidence_kind(_fields(action).get("evidence"))
    images, text = [], ""
    try:
        folder = evidence_dir(action_id)
        if folder.is_dir():
            images = sorted(p.name for p in folder.iterdir() if IMAGE_NAME.match(p.name))
            if (folder / "text.txt").is_file():
                text = (folder / "text.txt").read_text(encoding="utf-8")
    except (OSError, ValueError):
        pass
    reason = row["closed_reason"] or ""
    return {"id": action_id, "by": row["evidence_sender"] or "", "evidence": evidence, "images": images,
            "text": text, "found": bool(row["entry_id"]), "mail": row["evidence_quote"] or "",
            "unreadable": (row["evidence_quote"] or "") == UNREADABLE,
            "reason_gone": bool(row["proposed_status"] == "Lost" and reason
                                and not status_mod.reason_belongs(conn, "Lost", reason))}
