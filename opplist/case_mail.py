"""Which archived mails belong to a case, and what the Mail card shows
(2026-09-25). Reads data\\mail-archive\\mail.db only; Outlook
is never touched from here.

The agent thread is the union of

- the registered mail (entry_id),
- every mail in the case's conversation and in every conversation attached
  by hand (enquiry_thread, kind conversation),
- every mail whose subject's thread key is the case's or an attached one
  (kind thread_key),
- and, only when all of that is empty, whatever the archive finds under
  the cleaned source subject (MF1: only a subject with a letter and at
  least eight characters; '061' stays empty).

One tab per live vendor request that carries a thread key or conversation
id, by the same rules without the fallback. The one place that rule lives:
followup.from_archive and detect_followups.thread_of keep their own,
narrower readings for now.
"""

from __future__ import annotations

import json
import re

from . import branding, db, mail_archive, matching, parsing

#: The Mail card renders every mail of the case; past this many, the rest
#: sits behind 'show older'.
MAIL_CAP = 60
_GREETING = re.compile(
    r"^\s*(dear|hi|hello|hola|estimad[oa]s?|buen[oa]s\s+(d[ií]as|tardes|noches)|"
    r"good\s+(morning|afternoon|evening|day)|prezad[oa]s?|caro|bom\s+dia|boa\s+tarde)\b",
    re.I)
_SIGNOFF = re.compile(
    r"^\s*(best\s+regards|kind\s+regards|warm\s+regards|regards|rgds|brgds|cordialmente|"
    r"saludos(\s+cordiales)?|atte\.?-?|atentamente|sincerely|cheers|thanks\s*&\s*regards|"
    r"thank\s+you\s*&\s*regards)\s*[,.!:]*\s*(/\s*[A-Za-z ]+\s*[,.]?)?\s*$", re.I)
_THANKS = re.compile(r"^\s*(thanks|thank\s+you|gracias|obrigad[oa])\s*[,.!]*\s*$", re.I)
_ATTE = re.compile(r"^\s*atte\.?-?\b", re.I)
_OFFICE = re.compile(r"^[A-Z]{2,4}-")
_IMAGE = re.compile(r"\.(png|jpe?g|gif|bmp|webp|svg|ico)$", re.I)
_MID = re.compile(r"\[mid:\s*([^\]]+)\]")
#: a vendor request's note that names the subject it went out with: logged
#: from Outlook, or (W2a) on the team site by a colleague with Ask vendors
_LOGGED_FROM = re.compile(r"^(?:Logged from Outlook|Logged on the team site by [^:\n]*):\s*(.+)$", re.S)
#: W2a: the site's note ends so when the team address was copied
TEAM_COPIED = " (team copied)"


# ------------------------------------------------------------ the archive --
def open_search(db_path=None):
    """(ArchiveSearch or None, mail_archive.status()). The search is opened
    whenever the file is there and readable - a stale sync still shows the
    mails, with the sync time in the card's hint. The caller closes it."""
    info = mail_archive.status(db_path)
    if not info["exists"] or info["reason"].startswith("mail archive unreadable"):
        return None, info
    try:
        search = mail_archive.ArchiveSearch(db_path)
        search.__enter__()
    except mail_archive.ArchiveError as exc:
        info["reason"] = str(exc)
        return None, info
    return search, info


def mail_key(mail) -> str:
    return (getattr(mail, "message_id", "") or "").strip() or (mail.entry_id or "").strip() \
        or "{}|{}|{}".format(mail.received_at, mail.subject, mail.sender_address)


def stamp(mail) -> str:
    return mail.received_at or mail.received_on or ""


_REF_TAIL = re.compile(r"^[A-Z0-9]{3,20}$")


def key_matches(candidate: str, keys, loose: bool = False) -> bool:
    """Exact key equality; with `loose`, also a key that is one of ours
    with a reference glued on: a vendor answers 'RE: RATE CHECK // FCL
    // BALTIMORE - VALENCIA / ZPQA3087', their file number after a single
    slash, which the thread-key rules keep in the key."""
    if candidate in keys:
        return True
    if loose:
        for key in keys:
            if key and candidate.startswith(key) and _REF_TAIL.match(candidate[len(key):] or "-"):
                return True
    return False


#: MF1: the shortest clean subject the plain subject search may run on
FALLBACK_MIN = 8


def fallback_subject(clean: str) -> bool:
    """A clean subject the plain subject search may run on: strong
    (strong_subject) and not only common words (parsing.only_common_words)."""
    return strong_subject(clean) and not parsing.only_common_words(clean)


def strong_subject(clean: str) -> bool:
    """A clean subject the plain subject search may run on: a letter in it
    and at least FALLBACK_MIN characters."""
    clean = (clean or "").strip()
    return len(clean) >= FALLBACK_MIN and parsing.has_letter(clean)


def mails_for(search, entry_id, conversations, keys, subjects, fallback=True, loose=False,
              conn=None, case=None) -> list:
    """The union rule, oldest first. `subjects` are the texts whose thread
    key may be one of `keys` (the source subject, the attached subjects):
    the archive is searched under each and the hits kept when their key
    matches. With `fallback`, an empty result falls back to the plain
    subject search; with `loose`, a key with a reference on the end still
    counts (the vendor tabs).

    AL1: with `conn` and `case` (a row with id and opp_no) the mails whose
    subject carries the case's number come in too, and every mail found is
    kept only when case_numbers says it is this case's - a numbered mail is
    its number's case only, two numbers or a number that does not fit are
    no case's, the registered mail is its case's."""
    found = {}

    def add(mails):
        for mail in mails:
            found.setdefault(mail_key(mail), mail)

    if entry_id:
        registered = search.by_entry_id(entry_id)
        if registered is not None:
            add([registered])
    for conversation in sorted(c for c in conversations if c):
        add(search.by_conversation(conversation, limit=300))
    keys = {k for k in keys if k}
    cleaned = []
    for text in subjects:
        clean = parsing.clean_subject(text or "")
        if clean and clean not in cleaned:
            cleaned.append(clean)
    if keys:
        for clean in cleaned:
            add(m for m in search.by_subject(clean, limit=300)
                if key_matches(parsing.subject_thread_key(m.subject), keys, loose))
    if case is not None and conn is not None:
        from . import case_numbers
        add(case_numbers.numbered_mails(search, case["opp_no"]))
        for message in sorted(_attached_messages(conn, case["id"])):
            mail = search.by_message_id(message) or search.by_entry_id(message)
            if mail is not None:
                add([mail])
    if not found and fallback:
        # MF1: never on a weak key - '061' found 215 mails of other threads.
        # D7b: nor on common words alone - 'IMPORT AIR' found 8 mails of
        # other KLX requests before OPP-1730's own mail reached the archive
        for clean in cleaned:
            if fallback_subject(clean):
                add(search.by_subject(clean, limit=300))
    mails = sorted(found.values(), key=stamp)
    if case is not None and conn is not None:
        from . import case_numbers
        mails = case_numbers.keep(conn, case["id"], mails, search)
    return mails


def _attached_messages(conn, case_id) -> set:
    """The single mails attached to the case from a case-number suggestion."""
    if not db._has_table(conn, "enquiry_thread"):
        return set()
    return {r[0] for r in conn.execute("SELECT value FROM enquiry_thread WHERE enquiry_id = ? "
                                       "AND kind = 'message' AND voided = 0", (case_id,))}


def case_mails(conn, search, row) -> list:
    """The agent thread of a case (a v_enquiry row)."""
    values = db.thread_values(conn, row["id"])
    subjects = [row["source_subject"] or ""]
    if db._has_table(conn, "enquiry_thread"):
        # M2: the registered mail's own row carries no subject
        subjects += [r["note"] for r in conn.execute(
            "SELECT note FROM enquiry_thread WHERE enquiry_id = ? AND voided = 0 AND note IS NOT NULL "
            "AND source <> ?", (row["id"], REGISTER_SOURCE))]
    return mails_for(search, row["entry_id"], values["conversation"], values["thread_key"], subjects,
                     conn=conn, case=row)


# ------------------------------------------------- the registered mail --
#: enquiry_thread rows of the registered mail's Message-ID (kind 'message')
REGISTER_SOURCE = "register"
REGISTER_NOTE = "registered mail"
#: candidate folders Outlook searches for a moved mail
MAX_FOLDERS = 12
REQUESTER_DAYS = 90


def registered_message_id(conn, case_id) -> str:
    if not db._has_table(conn, "enquiry_thread"):
        return ""
    row = conn.execute("SELECT value FROM enquiry_thread WHERE enquiry_id = ? AND kind = 'message' "
                       "AND source = ? AND voided = 0 ORDER BY id LIMIT 1", (case_id, REGISTER_SOURCE)).fetchone()
    return row[0] if row else ""


def remember_registered(repo, case_id, message_id, added_on) -> bool:
    """The registered mail's Message-ID kept on the case: one
    enquiry_thread row, kind 'message', source 'register'. Nothing when the
    case has one or the Message-ID is another row's already."""
    message_id = (message_id or "").strip()
    if not message_id or registered_message_id(repo.conn, case_id):
        return False
    if repo.conn.execute("SELECT 1 FROM enquiry_thread WHERE kind = 'message' AND value = ?",
                         (message_id,)).fetchone():
        return False
    repo.insert("enquiry_thread", {"enquiry_id": case_id, "kind": "message", "value": message_id,
                                   "added_on": added_on, "source": REGISTER_SOURCE, "note": REGISTER_NOTE})
    return True


def _same_subject(a, b) -> bool:
    return " ".join((a or "").split()).casefold() == " ".join((b or "").split()).casefold()


def resolve_registered(conn, row, search) -> tuple:
    """(mail, step): the archive's current copy of the case's registered
    mail and which step found it - 'a' the entry id (or a move the archive
    recorded), 'b' the Message-ID the case keeps, 'c' the registered
    subject in the case's conversation on its received day (the only one
    that day, else the earliest on or after it); (None, '') otherwise."""
    if row["entry_id"]:
        mail = search.by_entry_id(row["entry_id"])
        if mail is not None:
            return mail, "a"
    mid = registered_message_id(conn, row["id"])
    if mid:
        mail = search.by_message_id(mid)
        if mail is not None:
            return mail, "b"
    subject = row["source_subject"] or ""
    if row["conversation_id"] and subject.strip():
        from . import case_numbers
        day = (row["received_on"] or "")[:10]
        same = [m for m in search.by_conversation(row["conversation_id"], limit=500)
                if _same_subject(m.subject, subject)]
        on_day = [m for m in same if case_numbers.mail_day(m) == day]
        if len(on_day) == 1:
            return on_day[0], "c"
        later = sorted((m for m in same if not day or case_numbers.mail_day(m) >= day), key=stamp)
        if later:
            return later[0], "c"
    return None, ""


def has_registered(conn, row) -> bool:
    """MF1: the case was registered from a mail - it keeps the mail's entry
    id or its Message-ID - whether or not the archive has the mail yet."""
    return bool((row["entry_id"] or "").strip() or registered_message_id(conn, row["id"]))


def registered_mail(conn, row, search):
    """The archive's current copy of the case's registered mail, or None."""
    return resolve_registered(conn, row, search)[0]


def registered_entry_id(conn, row, search) -> str:
    """The entry id to open the registered mail with: the resolved mail's,
    else the case's own."""
    mail = registered_mail(conn, row, search)
    return (mail.entry_id if mail is not None and mail.entry_id else "") or row["entry_id"] or ""


def candidate_folders(conn, row, search, mails=None, today=None) -> list:
    """Where Outlook looks for a mail that moved after the last sync, most
    recent first and at most MAX_FOLDERS: the folders the archive has seen
    this case's agent thread in, the folders it has seen the requester's
    mails in over the last REQUESTER_DAYS days, then the Inbox and Sent
    Items (kept in, whatever comes before)."""
    import datetime as dt
    from . import case_numbers
    standard = search.standard_folders()
    room = max(MAX_FOLDERS - len(standard), 0)
    out = []

    def add(paths):
        for path in paths:
            if path and path not in out and path not in standard and len(out) < room:
                out.append(path)

    mails = mails if mails is not None else case_mails(conn, search, row)
    add(search.folders_seen([getattr(m, "message_id", "") for m in mails]))
    internal = case_numbers.internal_domains(conn)
    domains = [r[0] for r in conn.execute("SELECT domain FROM party_domain WHERE party_id = ?",
                                          (row["requester_party_id"],)) if r[0] and r[0].lower() not in internal]
    addresses = [r[0] for r in conn.execute("SELECT email FROM contact WHERE party_id = ? AND email IS NOT NULL",
                                            (row["requester_party_id"],))
                 if matching.domain_of(r[0]) not in internal]
    since = ((today or dt.date.today()) - dt.timedelta(days=REQUESTER_DAYS)).isoformat()
    add(search.folders_of_senders(domains, addresses, since))
    return (out + standard)[:MAX_FOLDERS]


def where(conn, row, search, entry_id=None) -> dict:
    """{'entry_id', 'message_id', 'folders'} to find a mail with in Outlook:
    the registered mail (entry_id None) or an archive mail by its id."""
    if entry_id:
        mail = search.by_entry_id(entry_id)
        eid = (mail.entry_id if mail is not None and mail.entry_id else "") or entry_id
        mid = getattr(mail, "message_id", "") if mail is not None else ""
    else:
        mail = registered_mail(conn, row, search)
        eid = (mail.entry_id if mail is not None and mail.entry_id else "") or row["entry_id"] or ""
        mid = (getattr(mail, "message_id", "") if mail is not None else "") or registered_message_id(conn, row["id"])
    return {"entry_id": eid, "message_id": mid or "", "folders": tuple(candidate_folders(conn, row, search))}


def newest_mail(mails):
    """The newest mail with an entry id, or None (FX4: reply_target, the
    follow-up drafts' target, went with them)."""
    for mail in sorted(mails, key=stamp, reverse=True):
        if getattr(mail, "entry_id", ""):
            return mail
    return None


def vendor_subject(vendor) -> str:
    """The subject a vendor request was logged from, out of its note."""
    found = _LOGGED_FROM.match((vendor["notes"] or "").strip())
    if not found:
        return ""
    subject = found.group(1).strip()
    return subject[:-len(TEAM_COPIED)].rstrip() if subject.endswith(TEAM_COPIED) else subject


def vendor_mails(search, vendor, conn=None) -> list:
    """A vendor request's thread; with `conn`, a mail carrying another
    case's number is left out (AL1)."""
    conversations = [vendor["conversation_id"]] if vendor["conversation_id"] else []
    keys = {vendor["thread_key"]} if vendor["thread_key"] else set()
    mails = mails_for(search, "", conversations, keys, [vendor_subject(vendor)], fallback=False, loose=True)
    if conn is not None:
        from . import case_numbers
        mails = case_numbers.keep(conn, vendor["enquiry_id"], mails, search)
    return mails


def tabs(conn, search, row) -> list:
    """[{key, label, mails}] - the agent thread first, then one tab per
    live vendor request that has a thread key or a conversation id."""
    out = [{"key": "case", "label": "Agent thread", "mails": case_mails(conn, search, row)}]
    for vendor in conn.execute(
            "SELECT v.*, p.key AS vendor_key FROM vendor_request v JOIN party p ON p.id = v.vendor_party_id "
            "WHERE v.enquiry_id = ? AND v.voided = 0 AND (COALESCE(v.thread_key, '') <> '' "
            "OR COALESCE(v.conversation_id, '') <> '') ORDER BY v.asked_on, v.id", (row["id"],)):
        out.append({"key": "vendor-{}".format(vendor["id"]), "label": vendor["vendor_key"],
                    "mails": vendor_mails(search, vendor, conn)})
    return out


# ------------------------------------------------------------------- tags --
def tag_mails(conn, row, mails, registered=None, by_date=True) -> dict:
    """{mail_key: [tag, ...]} - facts the case already recorded about each
    mail, never guesses: 'registered from', the status an accepted
    suggestion set, 'FUn' from a follow-up event, 'reply' from a detected
    reply note. `by_date` allows the same-day rules for
    events without a Message-ID; a vendor tab turns it off, since a
    follow-up or a reply is the requester's thread's business."""
    tags = {mail_key(m): [] for m in mails}
    if registered is not None and mail_key(registered) in tags:
        tags[mail_key(registered)].append("registered from")
    elif row["entry_id"]:
        for mail in mails:
            if mail.entry_id and mail.entry_id == row["entry_id"]:
                tags[mail_key(mail)].append("registered from")
    events = conn.execute(
        "SELECT at, status, kind, note, source FROM status_event WHERE enquiry_id = ? "
        "AND voided = 0 ORDER BY at, id", (row["id"],)).fetchall()
    for mail in mails:
        key = mail_key(mail)
        mid = (getattr(mail, "message_id", "") or "").strip()
        day = (stamp(mail) or "")[:10]
        # the mail's own words only: the quoted chain below them repeats the
        # earlier mails, and would pin their tags on every later reply
        body = " ".join(mail_archive.top_part(mail.body_excerpt or "").split()).casefold()
        address = (mail.sender_address or "").strip().casefold()
        for event in events:
            note = event["note"] or ""
            mids = {m.strip() for m in _MID.findall(note)}
            if event["kind"] in ("status", "requote") and event["source"] == "mail":
                if address and address in note.casefold():
                    quote = note.split(">", 1)[1].strip() if ">" in note else note
                    quote = " ".join(quote.split()).casefold()
                    if quote and quote[:50] in body:
                        tags[key].append(event["status"])
            elif event["kind"] == "followup":
                label = "FU{}".format(conn.execute(
                    "SELECT seq FROM status_event WHERE enquiry_id = ? AND kind = 'followup' "
                    "AND at = ? AND note = ? LIMIT 1", (row["id"], event["at"], event["note"])
                ).fetchone()["seq"] or "")
                if mids:
                    if mid and mid in mids:
                        tags[key].append(label)
                elif by_date and mail.sender_internal and day == event["at"] and _to_outside(mail):
                    tags[key].append(label)
            elif event["kind"] == "note" and note.startswith("Reply from"):
                if mids:
                    if mid and mid in mids:
                        tags[key].append("reply")
                elif by_date and not mail.sender_internal and day == event["at"]:
                    tags[key].append("reply")
    return {k: _unique(v) for k, v in tags.items()}


def _unique(items) -> list:
    out = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _to_outside(mail) -> bool:
    for address in getattr(mail, "recipients", []) or []:
        domain = matching.domain_of(address)
        if domain and not domain.endswith(branding.INTERNAL_DOMAIN):
            return True
    return False


# ------------------------------------------------------------ the rows ----
def short_name(name: str) -> str:
    """'JJC-VICTOR LANG' -> 'Victor Lang'; 'Martin Fuentes' as it is."""
    text = (name or "").strip().strip("'\"")
    text = _OFFICE.sub("", text)
    if text and text == text.upper() and any(ch.isalpha() for ch in text):
        text = text.title()
    return text


def snippet(body_top: str) -> str:
    """The first line that is not a greeting."""
    for raw in (body_top or "").splitlines():
        line = " ".join(raw.split())
        if not line or _GREETING.match(line) or len(line) < 3:
            continue
        return line
    return ""


def signature_cut(body_top: str) -> str:
    """The new part of the mail up to its sign-off: the first line that is
    Best Regards / Regards / Kind regards / Atte.- / Saludos, or a bare
    Thanks followed by an Atte line."""
    lines = (body_top or "").splitlines()
    keep = []
    for index, raw in enumerate(lines):
        line = raw.rstrip()
        if _SIGNOFF.match(line):
            break
        if _THANKS.match(line):
            after = [l for l in lines[index + 1:index + 4] if l.strip()]
            if after and _ATTE.match(after[0]):
                break
        keep.append(line)
    return "\n".join(keep).strip()


def attachments(names) -> list:
    """The attachment names worth a mention: no inline images."""
    if isinstance(names, str):
        try:
            names = json.loads(names or "[]")
        except ValueError:
            names = [names]
    return [n for n in (names or []) if n and not _IMAGE.search(str(n))]


def row_view(mail, tags) -> dict:
    """What the list row and the reading pane need, computed once."""
    top = mail_archive.top_part(mail.body_excerpt or "")
    return {
        "key": mail_key(mail), "mail": mail, "ours": bool(mail.sender_internal),
        "name": short_name(mail.sender_name) or mail.sender_address, "tags": tags,
        "when": stamp(mail)[:16], "snippet": snippet(top), "body": signature_cut(top),
        "attachments": attachments(getattr(mail, "attachment_names", []) or []),
        "cc": ", ".join(mail.cc_addresses or []),
    }
