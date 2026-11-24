"""Read quote from the team site (batch RQ2, INTERFACE.md
section 12): a vendor's reply sent in from a Rate requests row on the site
- their mail dragged in from Outlook (.msg, or an .eml), the files they
sent, or a pasted message - read on her computer with her key, as section
11 reads one she picks on 8765.

  (1) taking in (firebase_pull, every cycle, the 08:15 run too): the
      action checked as section 12 says, the parts of each file fetched
      from uploads/ and put together in order, the files kept in
      data\\site-reads\\<action id>\\ (never deleted); answered "queued";
  (2) reading (the app on 8765 only, on its background thread, oldest
      first, one at a time): through section 11's gate (on, the key, the
      cap), a .msg opened through Outlook (Session.OpenSharedItem, read
      only), an .eml with Python's email module, files as a mail's
      attachments, a paste as a pasted message; the drafts of section 11
      with readBy the action's name; the answer rewritten "done";
  (3) the uploads of an action deleted once it is read, whatever came of
      it, and parts with no action after a day (checked once a day):
      deleting them is the only change OppList makes there;
  (4) meta/sync.readQuote {on, capReached, appAt} and the app's minute
      check for new read actions.

What OppList writes for the site to show never names her computer or
OppList (her rule of 27 Oct, again on 16 Nov): a why is a phrase in lower
case, shown after "Could not read it: ".
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import re
from pathlib import Path

from . import db, logs, paths

TYPE = "read"
KINDS = ("mail", "files", "paste")
MAIL_FILES = (".msg", ".eml")
FILES_MAX = 6
PARTS_MAX = 30
BYTES_MAX = 15 * 1024 * 1024            # in all (the site's READ_BYTES)
TEXT_MAX = 20_000
NAME_MAX = 120
TYPE_MAX = 100
WHY_MAX = 200
QUEUED = "Waiting to be read."
#: a part of uploads/ older than this with no action waiting is deleted
ORPHAN_AFTER = dt.timedelta(days=1)
DELETED_KEPT = 2000                     # the action ids kept in the state as done with
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: section 11's sentences -> the phrase the site shows after "Could not read it: "
OFF = "Read quote is off"
CAP = "this month's limit is reached"


class Refused(Exception):
    """A read action that cannot be taken in; the message is the site's phrase."""


# ------------------------------------------------------------- the place --
def root() -> Path:
    return paths.DATA_DIR / "site-reads"


def folder(action_id: str) -> Path:
    return root() / action_id


def _safe(name: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name or "file").strip(" .") or "file"
    return stem[:NAME_MAX]


def part_id(action_id: str, file_index: int, part_index: int) -> str:
    return "{}-{}-{}".format(action_id, file_index, part_index)


def _bridge():
    from . import outlook_bridge, read_quote
    return read_quote.OUTLOOK or outlook_bridge


# -------------------------------------------------------- the site's words --
def phrase(error: str) -> str:
    """A reading's error as the site shows it after 'Could not read it: ':
    lower case, never naming her computer or OppList, at most 200."""
    from . import read_quote as rq
    text = " ".join(str(error or "").split())
    known = {
        rq.BUSY: "the reading service was busy; send it again in a few minutes",
        rq.TIMED_OUT: "the reading took too long; send it again",
        rq.NO_CONNECTION: "there was no connection to the reading service; send it again",
        rq.NO_CREDITS: "Read quote is not available right now",
        rq.LIMIT_REACHED: CAP,
        rq.CUT_OFF: "it was too long to read; send only the part with the prices",
        rq.NOT_JSON: "the reading went wrong; send it again",
        rq.LIST_LINE: "this is the line from Outlook's message list, not the mail",
    }
    if text in known:
        return known[text]
    if text in (OFF, CAP):
        return text
    if text.startswith(("The API key was refused", "The model ", "OppList has no price", "Read quote needs the key",
                        "Read quote calls the API")):
        return "Read quote is not available right now"
    if text.startswith("This month's readings have cost"):
        return CAP
    if text.startswith("Claude declined"):
        return "it could not be read; type the rates in"
    if text.startswith("The answer did not pass"):
        return "the reading went wrong; send it again"
    if text.startswith("The message is ") and "characters" in text:
        return "the message is too long; paste the part with the prices"
    if not text:
        return "it could not be read"
    return _lower(text.rstrip("."))[:WHY_MAX]


def _lower(text: str) -> str:
    """'No prices in it' -> 'no prices in it'; 'MSC said ...' stays."""
    if len(text) > 1 and text[0].isupper() and not text[1].isupper():
        return text[0].lower() + text[1:]
    return text


def done_message(conn, reading) -> tuple:
    """(ok, msg) of a reading for actionResults: 'Read: 2 options for
    route A.'; no prices: its why_not; failed: why."""
    from . import routes as routes_mod
    if reading["status"] == "read":
        bits = []
        for d in conn.execute("SELECT draft FROM quote_read_draft WHERE quote_read_id = ? ORDER BY id",
                              (reading["id"],)):
            doc = json.loads(d["draft"])
            n = len(doc.get("options") or [])
            words = "{} option{}".format(n, "" if n == 1 else "s")
            if doc.get("route"):
                words += " for route {}".format(routes_mod.letter(doc["route"]))
            bits.append(words)
        if bits:
            return True, "Read: {}.".format("; ".join(bits))
        return False, "no prices in it"
    if reading["status"] == "no_prices":
        from . import read_quote as rq
        why = " ".join(((rq.answer_of(reading["response"]) or {}).get("why_not") or "").split()).rstrip(".")
        return False, (_lower(why) or "no prices in it")[:WHY_MAX]
    return False, phrase(reading["error"])


# ------------------------------------------------------------ the action --
def _request_of(conn, case, vendor):
    """The vendor's newest live rate request on the case, by its key."""
    return conn.execute(
        "SELECT v.*, p.key AS vendor_key FROM vendor_request v JOIN party p ON p.id = v.vendor_party_id "
        "WHERE v.enquiry_id = ? AND v.voided = 0 AND lower(trim(p.key)) = lower(trim(?)) ORDER BY v.id DESC",
        (case["id"], vendor)).fetchone()


def _case_why(conn, case) -> str:
    """'' when the case takes rates, else the site's phrase."""
    from . import read_quote as rq
    takes, why = rq.case_takes_rates(conn, case)
    if takes:
        return ""
    if case["status"] in ("Won", "Lost", "Cancelled", "Dropped"):
        return "the case is closed"
    return why


def _int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value if isinstance(value, int) else None


def check(conn, case, action, today) -> dict:
    """The action's own fields as section 12 has them (no file fetched
    yet): {request, route, kind, text, sent, files}; Refused with the
    site's phrase."""
    why = _case_why(conn, case)
    if why:
        raise Refused(why)
    vendor = action.get("vendor")
    vendor = vendor.strip() if isinstance(vendor, str) else ""
    request = _request_of(conn, case, vendor) if vendor else None
    if request is None:
        raise Refused("{} is not on this case's rate requests".format(vendor or "the vendor"))
    route = action.get("route")
    if route is not None:
        route = _int(route)
        seqs = {r[0] for r in conn.execute("SELECT seq FROM enquiry_route WHERE enquiry_id = ? AND voided = 0",
                                           (case["id"],))}
        if route not in seqs:
            raise Refused("route {} is not a route of this case".format(action.get("route")))
    kind = action.get("kind")
    if kind not in KINDS:
        raise Refused("the request's kind is not mail, files or paste")
    text = action.get("text") if action.get("text") is not None else ""
    if not isinstance(text, str):
        raise Refused("the message is not text")
    if len(text) > TEXT_MAX:
        raise Refused("the message is longer than 20,000 characters")
    sent = action.get("sent")
    sent = sent.strip() if isinstance(sent, str) else ""
    try:
        if not _DATE.match(sent):
            raise ValueError
        dt.date.fromisoformat(sent)
    except ValueError:
        raise Refused("'{}' is not the day they sent it".format(action.get("sent"))) from None
    latest = (dt.date.fromisoformat(today) + dt.timedelta(days=1)).isoformat()   # a colleague a day ahead
    if sent > latest:
        raise Refused("{} is after today".format(sent))
    files = action.get("files") if action.get("files") is not None else []
    if not isinstance(files, list) or len(files) > FILES_MAX:
        raise Refused("at most 6 files can be sent")
    clean, total = [], 0
    for f in files:
        f = f if isinstance(f, dict) else {}
        name, kind_of = f.get("name"), f.get("type") if f.get("type") is not None else ""
        size, parts = _int(f.get("size")), _int(f.get("parts"))
        if not isinstance(name, str) or not name.strip() or len(name) > NAME_MAX or not isinstance(kind_of, str) \
                or len(kind_of) > TYPE_MAX or size is None or size <= 0 or parts is None or not 1 <= parts <= PARTS_MAX:
            raise Refused("a file of the request is not described as the site sends it")
        total += size
        clean.append({"name": name.strip(), "type": kind_of, "size": size, "parts": parts})
    if total > BYTES_MAX:
        raise Refused("the files are more than 15 MB in all")
    if kind == "paste":
        if clean:
            raise Refused("a pasted message comes with no files")
        if not text.strip():
            raise Refused("the pasted message is empty")
    elif not clean:
        raise Refused("no file was sent")
    if kind == "mail" and not any(Path(f["name"]).suffix.lower() in MAIL_FILES for f in clean):
        raise Refused("no mail file (.msg or .eml) among the files")
    return {"request": request, "route": route, "kind": kind, "text": text, "sent": sent, "files": clean}


def fetch(client, action_id, files) -> list:
    """Each file's parts from uploads/, in order, put together and decoded:
    [bytes]. A missing part, a part that is not base64, or sizes that do
    not add up: Refused with the file's name."""
    out = []
    for i, f in enumerate(files):
        chunks = []
        for j in range(f["parts"]):
            doc = client.get("uploads/" + part_id(action_id, i, j))
            data = (doc or {}).get("data")
            if not isinstance(data, str) or not data:
                raise Refused("a part of {} is missing".format(f["name"]))
            chunks.append(data)
        try:
            raw = base64.b64decode("".join(chunks), validate=True)
        except (binascii.Error, ValueError):
            raise Refused("{} did not arrive whole".format(f["name"])) from None
        if len(raw) != f["size"]:
            raise Refused("{} did not arrive whole ({:,} of {:,} bytes)".format(f["name"], len(raw), f["size"]))
        out.append(raw)
    return out


def keep(action_id, files, datas) -> list:
    """The files written into data\\site-reads\\<action id>\\ (two with
    the same name get their index in front); their paths."""
    where = folder(action_id)
    where.mkdir(parents=True, exist_ok=True)
    used, out = set(), []
    for i, (f, data) in enumerate(zip(files, datas)):
        name = _safe(f["name"])
        if name.casefold() in used:
            name = "{}_{}".format(i, name)
        used.add(name.casefold())
        path = where / name
        path.write_bytes(data)
        out.append(str(path))
    return out


def take(repo, case, action, who, today, action_id, client) -> str:
    """firebase_pull's handler: checked, the files fetched and kept;
    'Waiting to be read.'. The scheduled run takes them in too; only the
    app reads."""
    got = check(repo.conn, case, action, today)
    if got["files"]:
        if client is None:
            raise Refused("the files could not be fetched")
        paths_kept = keep(action_id, got["files"], fetch(client, action_id, got["files"]))
        logs.get("readquote").info("site read %s %s taken in: %s file%s in %s", case["opp_no"], action_id,
                                   len(paths_kept), "" if len(paths_kept) == 1 else "s", folder(action_id))
    return QUEUED


# ------------------------------------------------------------- the state --
def reading_of(conn, action_id):
    if not db._has_table(conn, "quote_read") or "site_action" not in {
            r[1] for r in conn.execute("PRAGMA table_info(quote_read)")}:
        return None
    return conn.execute("SELECT * FROM quote_read WHERE site_action = ? ORDER BY id DESC", (action_id,)).fetchone()


def state_of(conn, row) -> str:
    """actionResults.state of a read action: refused or read is 'done',
    else 'queued'."""
    if row["result"] != "applied":
        return "done"
    return "done" if reading_of(conn, row["id"]) is not None else "queued"


def answer_ok(conn, row) -> bool:
    if row["result"] != "applied":
        return False
    reading = reading_of(conn, row["id"])
    return True if reading is None else done_message(conn, reading)[0]


def queued(conn) -> list:
    """The read actions taken in and not read yet, oldest first."""
    if not db._has_table(conn, "site_action"):
        return []
    return [r for r in conn.execute("SELECT * FROM site_action WHERE type = ? AND result = 'applied' ORDER BY at, id",
                                    (TYPE,)).fetchall() if reading_of(conn, r["id"]) is None]


# ----------------------------------------------------------- the sources --
def _file(path, name, size, data):
    from . import attachments as att, outlook_bridge
    found = outlook_bridge.AttachedFile(name=name, size=size, path=str(path))
    if size > att.MAX_BYTES:
        found.reason = "over 10 MB"
        return found
    found.data = data
    if not att.is_image(name):
        try:
            found.text = att.read_file(path, name, size)
        except Exception as exc:                          # a file that cannot be read sends no text
            logs.get("readquote").info("site read: text of %s: %s", name, exc)
    return found


def _html_text(html: str) -> str:
    import html as html_mod
    text = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", text)
    text = re.sub(r"(?i)</td>|</th>", "\t", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html_mod.unescape(text)


def read_eml(path):
    """An .eml with Python's email module: (MailInfo, [AttachedFile]).
    Its text (plain, else its HTML as text), subject, sender and sent time;
    its attachments (a picture in the body or signature, one with a
    Content-ID shown inline, marked inline and not sent; an attached mail
    not opened). Raises ValueError when it is not a mail."""
    import email
    import email.policy
    import email.utils
    import tempfile

    from . import attachments as att, outlook_bridge
    raw = Path(path).read_bytes()
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    if not (msg.get("From") or msg.get("Subject") or msg.get("Date")):
        raise ValueError("no From, Subject or Date in it")
    info = outlook_bridge.MailInfo()
    info.subject = " ".join(str(msg.get("Subject") or "").split())
    name, address = email.utils.parseaddr(str(msg.get("From") or ""))
    info.sender_name, info.sender_address = name, address
    try:
        when = email.utils.parsedate_to_datetime(str(msg.get("Date")))
        if when.tzinfo is not None:
            when = when.astimezone()                     # her local time
        info.sent_at = when.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, IndexError):
        info.sent_at = ""
    info.message_id = str(msg.get("Message-ID") or "").strip()
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        try:
            text = body.get_content()
        except (LookupError, ValueError):
            text = body.get_payload(decode=True).decode("utf-8", "replace") if body.get_payload(decode=True) else ""
        if body.get_content_type() == "text/html":
            text = _html_text(text)
    info.body_excerpt = text[:outlook_bridge.BODY_LIMIT]
    files = []
    with tempfile.TemporaryDirectory(prefix="opplist-eml-") as tmp:
        for index, part in enumerate(msg.iter_attachments(), start=1):
            fname = part.get_filename() or "attachment {}".format(index)
            if part.get_content_type() == "message/rfc822" or fname.lower().endswith((".msg", ".eml")):
                files.append(outlook_bridge.AttachedFile(name=fname, size=len(part.as_bytes())))
                continue
            data = part.get_payload(decode=True) or b""
            inline = att.is_image(fname) and (att.is_inline_image(fname) or (
                bool(part.get("Content-ID")) and (part.get_content_disposition() or "inline") == "inline"))
            if inline:
                files.append(outlook_bridge.AttachedFile(name=fname, size=len(data), inline=True))
                continue
            copy = Path(tmp) / "{:02d}_{}".format(index, _safe(fname))
            copy.write_bytes(data)
            found = _file(copy, fname, len(data), data)
            found.path = ""
            files.append(found)
    info.attachment_names = [f.name for f in files]
    return info, files


def mail_source(path, note: str = "", others=()):
    """A mail file (.msg through Outlook, .eml with the email module) as a
    section 11 mail: its text with the quoted thread, its subject, sender
    and sent time, its attachments; `others` the other files sent beside
    it. Raises read_quote.ReadQuoteError '<name> could not be opened
    (why)'."""
    from . import outlook_bridge, read_quote as rq
    path = Path(path)
    try:
        if path.suffix.lower() == ".msg":
            info, files = _bridge().open_msg(str(path))
        else:
            info, files = read_eml(path)
    except outlook_bridge.OutlookError as exc:
        why = ("Outlook was not open to read it" if exc.message == outlook_bridge.NOT_RUNNING
               else _lower(exc.message.rstrip(".")))
        raise rq.ReadQuoteError("{} could not be opened ({})".format(path.name, why)) from None
    except Exception as exc:                              # a broken file
        logs.get("readquote").info("site read: %s: %s: %s", path, type(exc).__name__, exc)
        raise rq.ReadQuoteError("{} could not be opened (it is not a mail file Read quote can read)".format(
            path.name)) from None
    sender = "{} <{}>".format(info.sender_name or "", info.sender_address or "").strip()
    return rq.Source(kind="mail", text=rq.clean_text(info.body_excerpt or ""), subject=info.subject or "",
                     sender=sender, sent=(info.sent_at or "")[:16], message_id=info.message_id or "",
                     files=list(files) + list(others), note=" ".join((note or "").split()))


def source_of(fields, kept, today):
    """The site's request as a section 11 source."""
    from . import read_quote as rq
    kind, note = fields["kind"], fields.get("text") or ""
    if kind == "paste":
        return rq.from_paste(note, fields["sent"], max(today, fields["sent"]))
    files = []
    for f, path in zip(fields["files"], kept):
        files.append((f, Path(path)))
    if kind == "mail":
        mail = next(p for f, p in files if p.suffix.lower() in MAIL_FILES)
        others = [_file(p, f["name"], f["size"], p.read_bytes()) for f, p in files if p != mail]
        source = mail_source(mail, note, others)
        if not source.sent:
            source.sent = fields["sent"]
        return source
    return rq.Source(kind="files", text=rq.clean_text(note), sent=fields["sent"],
                     files=[_file(p, f["name"], f["size"], p.read_bytes()) for f, p in files])


# ------------------------------------------------------------ the reading --
def _fields(row) -> dict:
    try:
        return json.loads(row["fields"] or "{}")
    except ValueError:
        return {}


def _kept_paths(row, fields) -> list:
    where = folder(row["id"])
    used, out = set(), []
    for i, f in enumerate(fields.get("files") or []):
        name = _safe(f["name"])
        if name.casefold() in used:
            name = "{}_{}".format(i, name)
        used.add(name.casefold())
        out.append(str(where / name))
    return out


def _failed(repo, row, case, request, fields, error, kept=()) -> int:
    """A reading that went nowhere (no call): kept as failed, with why."""
    names = [{"name": f["name"], "kind": Path(f["name"]).suffix.lower().lstrip(".") or "file", "size": f["size"],
              "sent": False, "why": "not read", "path": p} for f, p in zip(fields.get("files") or [], kept)]
    return repo.insert("quote_read", {
        "enquiry_id": case["id"], "vendor_request_id": request["id"] if request is not None else None,
        "vendor_party_id": request["vendor_party_id"] if request is not None else _vendor_party(repo.conn, fields),
        "source": fields.get("kind") if fields.get("kind") in KINDS else "paste", "files": json.dumps(
            names, ensure_ascii=False), "text_sent": fields.get("text") or None, "status": "failed",
        "error": error, "by_name": row["by_name"] or None, "site_action": row["id"],
        "sent_at": fields.get("sent") or None, "at": _stamp()})


def _stamp() -> str:
    from . import read_quote as rq
    return rq._stamp()


def _vendor_party(conn, fields):
    found = conn.execute("SELECT id FROM party WHERE lower(trim(key)) = lower(trim(?))",
                         (str(fields.get("vendor") or ""),)).fetchone()
    return found["id"] if found else None


def _answered_again(repo, row, ok_msg) -> None:
    """The done message on the action; actionResults written again."""
    repo.update("site_action", row["id"], {"message": ok_msg, "answered_at": None})


def read_one(conn, row, today=None, transport=None, db_path=None) -> int:
    """One queued request read; the quote_read id. Every outcome is a
    reading on file (a failed one when nothing was sent), so it is never
    read twice."""
    from . import read_quote as rq
    today = today or dt.date.today().isoformat()
    repo = db.Repository(conn, source="readquote")
    fields = _fields(row)
    case = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (row["enquiry_id"],)).fetchone()
    request = _request_of(conn, case, str(fields.get("vendor") or "")) if case is not None else None
    kept = _kept_paths(row, fields)
    why = ""
    if not rq.is_on(conn) or not rq.key_state(conn)[0]:
        why = OFF
    elif not rq.under_cap(conn)[0]:
        why = CAP
    elif _case_why(conn, case):
        why = _case_why(conn, case)
    elif request is None:
        why = "{} is not on this case's rate requests".format(fields.get("vendor") or "the vendor")
    if why:
        reading_id = _failed(repo, row, case, request, fields, why, kept)
    else:
        try:
            source = source_of(fields, kept, today)
            got = rq.read(conn, request["id"], source, today=today, db_path=db_path, transport=transport, repo=repo,
                          by_name=row["by_name"] or None, site_action=row["id"],
                          route=fields.get("route") if isinstance(fields.get("route"), int) else None)
            reading_id = got.id
        except rq.ReadQuoteError as exc:
            conn.rollback()
            reading_id = _failed(repo, row, case, request, fields, str(exc), kept)
    reading = conn.execute("SELECT * FROM quote_read WHERE id = ?", (reading_id,)).fetchone()
    ok, msg = done_message(conn, reading)
    _answered_again(repo, row, msg)
    repo.commit()
    logs.get("readquote").info("site read %s %s by %s: %s", row["opp_no"], row["id"], row["by_name"] or "?", msg)
    return reading_id


def read_next(db_path, today=None, transport=None):
    """The app's reader: the oldest queued request read, or None when
    there is none."""
    conn = db.open_db(db_path)
    try:
        waiting = queued(conn)
        if not waiting:
            return None
        return read_one(conn, waiting[0], today=today, transport=transport, db_path=db_path)
    finally:
        conn.close()


# ----------------------------------------------------------- the uploads --
def _parts_of(row) -> list:
    out = []
    for i, f in enumerate(_fields(row).get("files") or []):
        n = _int(f.get("parts")) if isinstance(f, dict) else None
        if n is not None and 1 <= n <= PARTS_MAX:
            out += ["uploads/" + part_id(row["id"], i, j) for j in range(n)]
    return out


def _delete(client, paths_) -> int:
    from .firebase_sync import BATCH, Delete
    for start in range(0, len(paths_), BATCH):
        client.commit([Delete(p) for p in paths_[start:start + BATCH]])
    return len(paths_)


def _orphans_query(before: str) -> dict:
    return {"select": {"fields": [{"fieldPath": "__name__"}]}, "from": [{"collectionId": "uploads"}],
            "where": {"fieldFilter": {"field": {"fieldPath": "at"}, "op": "LESS_THAN",
                                      "value": {"timestampValue": before}}}}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def clean_uploads(conn, client, state: dict, today: str, now=None) -> int:
    """Point 6: the parts of every read action done (refused or read, its
    done answer written) deleted; once a day, parts older than a day whose
    action is not waiting (done, or not on the site at all) too. Returns
    how many were deleted."""
    if not db._has_table(conn, "site_action"):
        return 0
    done = state.setdefault("read_uploads_deleted", [])
    deleted = 0
    for row in conn.execute("SELECT * FROM site_action WHERE type = ? AND answered_at IS NOT NULL ORDER BY at",
                            (TYPE,)).fetchall():
        if row["id"] in done or state_of(conn, row) != "done":
            continue
        deleted += _delete(client, _parts_of(row))
        done.append(row["id"])
    del done[:-DELETED_KEPT]
    if state.get("read_uploads_checked") != today and _any_read(conn, state):
        now = now or _utcnow()
        before = (now - ORPHAN_AFTER).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        found = client.run_query(_orphans_query(before))
        by_action = {}
        for path, _fields_ in found["docs"]:
            if not path.startswith("uploads/"):
                continue
            pid = path[len("uploads/"):]
            bits = pid.rsplit("-", 2)
            if len(bits) == 3:
                by_action.setdefault(bits[0], []).append(path)
        for action_id, parts in by_action.items():
            row = conn.execute("SELECT * FROM site_action WHERE id = ?", (action_id,)).fetchone()
            if row is not None:
                if state_of(conn, row) == "queued":
                    continue                                    # still to be read
            elif client.get("actions/" + action_id) is not None:
                continue                                        # on the site, not taken in yet
            deleted += _delete(client, parts)
        state["read_uploads_checked"] = today
    if deleted:
        logs.get("firebase").info("team site: %s upload part%s deleted", deleted, "" if deleted == 1 else "s")
    return deleted


def _any_read(conn, state) -> bool:
    from . import read_quote as rq
    return rq.is_on(conn) or conn.execute("SELECT 1 FROM site_action WHERE type = ? LIMIT 1",
                                          (TYPE,)).fetchone() is not None


# ------------------------------------------------------------ meta/sync --
def meta(conn, client, state: dict, by_app: bool):
    """meta/sync.readQuote {on, capReached, appAt}, or None while Read
    quote is off (the site then offers nothing). appAt: the app's cycles
    set it to the server time (returned as a field to stamp); the
    scheduled run keeps the value it finds on the site. Returns (map,
    server-time field or None)."""
    from . import read_quote as rq
    if not (rq.is_on(conn) and rq.key_state(conn)[0]):
        return None, None
    out = {"on": True, "capReached": not rq.under_cap(conn)[0]}
    if by_app:
        return out, "readQuote.appAt"
    try:
        found = (client.get("meta/sync") or {}).get("readQuote") or {}
    except Exception as exc:                           # the push goes on without it
        logs.get("firebase").info("meta/sync.readQuote.appAt not read: %s", exc)
        found = {}
    if isinstance(found, dict) and isinstance(found.get("appAt"), dt.datetime):
        out["appAt"] = found["appAt"]
    return out, None


# ----------------------------------------------------- the minute check --
def new_query(after: str, limit: int = 50) -> dict:
    """The actions after `after`, oldest first. An equality on type with a
    range on at would need a composite index the site does not have, so
    the type is looked at here."""
    return {"from": [{"collectionId": "actions"}],
            "where": {"fieldFilter": {"field": {"fieldPath": "at"}, "op": "GREATER_THAN",
                                      "value": {"timestampValue": after}}},
            "orderBy": [{"field": {"fieldPath": "at"}, "direction": "ASCENDING"}], "limit": limit}


def look(conn, client, after: str) -> tuple:
    """One runQuery: (a read action not taken in yet?, the newest at seen
    or `after`). Writes nothing."""
    from .firebase_pull import iso
    found = client.run_query(new_query(after))
    newest, wanted = after, False
    for path, action in found["docs"]:
        at = iso(action.get("at"))
        newest = max(newest, at)
        if action.get("type") == TYPE and conn.execute("SELECT 1 FROM site_action WHERE id = ?", (
                path.rsplit("/", 1)[-1],)).fetchone() is None:
            wanted = True
    return wanted, newest
