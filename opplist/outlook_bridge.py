r"""Reading the mail selected in Outlook.

Read only. Version 1 does not create, move, delete or send anything; the only
call that touches Outlook's own UI is Display(), which opens a mail the user
already has.

Everything here can fail in ways that are not bugs: Outlook is not running,
nothing is selected, the mail was filed somewhere else, the COM server is
busy. All of that comes back as OutlookError with a sentence the user can act
on. The server must never fall over because of it.

Threading: Flask handles each request on its own thread, so every entry point
calls CoInitialize first and CoUninitialize at the end.

Without pywin32 (any machine that is not Windows, or a Windows machine
without it) every entry point raises OutlookUnavailable, an OutlookError,
and the rest of the app - the forms, the mail archive, the exports - works
as before.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from . import logs, parsing

#: MailItem. Meeting requests, tasks and reports have other class numbers.
OL_MAIL_ITEM = 43
#: A long thread is read whole: the first enquiry sits at the bottom of
#: the quoted chain, often well past the first few thousand characters.
BODY_LIMIT = 60000

NOT_RUNNING = "Open Outlook and select a mail first."
NOTHING_SELECTED = "Open Outlook and select a mail first."
NOT_A_MAIL = "That Outlook item is not a mail. Select the inquiry mail itself."
ITEM_GONE = "That mail has been moved or deleted; the case still has its details."
#: M2: every way of finding a mail failed (its id, each store, its
#: Message-ID in the candidate folders)
MOVED = ("That mail is not where this morning's mail sync saw it, nor in the folders this case's "
         "mails are filed in; the next sync finds it.")


class OutlookError(RuntimeError):
    """Something Outlook-shaped went wrong. The message is for the user."""

    def __init__(self, message, detail=""):
        super().__init__(message)
        self.message = message
        self.detail = detail


class OutlookUnavailable(OutlookError):
    """pywin32 is missing, so there is no Outlook on this machine at all.
    Retrying cannot help, unlike an Outlook that is merely not running."""


def _no_pywin32_message() -> str:
    if sys.platform == "win32":
        return ("pywin32 is not installed, so Outlook cannot be read. "
                "Run: pip install pywin32")
    return ("Outlook cannot be read on this machine: pywin32 and classic Outlook "
            "are Windows only. Everything else works; register the mail by hand.")


def available() -> bool:
    try:
        import win32com.client  # noqa: F401
        import pythoncom        # noqa: F401
    except Exception:           # ImportError, or a pywin32 whose DLLs do not load
        return False
    return True


def _require_pywin32() -> None:
    if not available():
        raise OutlookUnavailable(_no_pywin32_message())


@contextmanager
def _com():
    """One COM apartment per call, released whatever happens."""
    _require_pywin32()
    import pythoncom
    pythoncom.CoInitialize()
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


def _application():
    import win32com.client
    try:
        return win32com.client.Dispatch("Outlook.Application")
    except Exception as exc:                      # pywin32 raises com_error
        logs.get("outlook").warning("cannot reach Outlook: %s", exc)
        raise OutlookError(NOT_RUNNING, str(exc))


# ------------------------------------------------------------- the reading --
@dataclass
class MailInfo:
    """What is read off a mail. Nothing here is written back to Outlook."""

    subject: str = ""
    received_on: str = ""
    received_at: str = ""
    entry_id: str = ""
    conversation_id: str = ""
    conversation_topic: str = ""
    sender_name: str = ""
    sender_address: str = ""
    sender_internal: bool = False
    to_addresses: list = field(default_factory=list)
    cc_addresses: list = field(default_factory=list)
    #: The recipients' display names, one per address in the same order
    #: ('' when unknown). 'JJH-DANIEL YIP' says which own office a
    #: colleague sits in, which the address does not (2026-09-25).
    to_names: list = field(default_factory=list)
    cc_names: list = field(default_factory=list)
    body_excerpt: str = ""
    selection_count: int = 1
    folder_path: str = ""
    notes: list = field(default_factory=list)
    #: The To and CC lines as Outlook shows them (names, sometimes
    #: addresses) - cheap, unlike resolving each recipient.
    display_to: str = ""
    #: Headers only - no body, recipients not resolved (see Search.hydrate).
    light: bool = False
    #: Outlook matched the domain on a From/To/CC header (a DASL search).
    header_match: bool = False
    #: The Internet Message-ID header (PR_INTERNET_MESSAGE_ID). Stable across
    #: folders, unlike entry_id; the mail archive keys on it.
    message_id: str = ""
    #: When it was sent ('YYYY-MM-DD HH:MM'), from the archive; '' when unknown.
    sent_at: str = ""
    #: batch 14: the attachments' file names (inline pictures included)
    attachment_names: list = field(default_factory=list)

    @property
    def recipients(self) -> list:
        return list(self.to_addresses) + list(self.cc_addresses)


def read_fixture(path) -> MailInfo:
    """A MailInfo out of one of the JSON mail fixtures (a mail as the
    archive stores it), for the selftest and the tests."""
    import json
    from pathlib import Path

    with Path(path).open(encoding="utf-8") as handle:
        data = json.load(handle)
    return MailInfo(
        subject=data.get("subject", ""), sender_name=data.get("sender_name", ""),
        sender_address=data.get("sender_address", ""),
        sender_internal=bool(data.get("sender_internal")),
        received_on=data.get("received_on", ""), received_at=data.get("received_at", ""),
        to_addresses=list(data.get("to_addresses") or []),
        cc_addresses=list(data.get("cc_addresses") or []),
        to_names=list(data.get("to_names") or []), cc_names=list(data.get("cc_names") or []),
        display_to=data.get("display_to", ""), conversation_id=data.get("conversation_id", ""),
        entry_id=data.get("entry_id", ""), body_excerpt=data.get("body", ""),
        message_id=data.get("message_id", ""))


def _text(value) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""


def _date(value):
    """Outlook hands back a pywintypes datetime; take the local date."""
    try:
        return dt.date(value.year, value.month, value.day).isoformat()
    except Exception:
        return ""


def _timestamp(value):
    try:
        return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}".format(
            value.year, value.month, value.day, value.hour, value.minute)
    except Exception:
        return ""


def _smtp_from_address_entry(entry) -> tuple:
    """(address, internal). An Exchange entry needs an extra hop for its SMTP."""
    if entry is None:
        return "", False
    try:
        entry_type = _text(entry.Type).upper()
    except Exception:
        entry_type = ""
    if entry_type == "EX":
        try:
            user = entry.GetExchangeUser()
            if user is not None:
                address = _text(user.PrimarySmtpAddress)
                if address:
                    return address, True
        except Exception:
            pass
        # A distribution list or a stale DN: internal, address unknown.
        return "", True
    try:
        return _text(entry.Address), False
    except Exception:
        return "", False


def _sender(item) -> tuple:
    """(name, smtp address, internal)."""
    name = _text(getattr(item, "SenderName", ""))
    try:
        kind = _text(item.SenderEmailType).upper()
    except Exception:
        kind = ""
    if kind == "EX":
        try:
            address, _ = _smtp_from_address_entry(item.Sender)
        except Exception:
            address = ""
        return name, address, True
    try:
        return name, _text(item.SenderEmailAddress), False
    except Exception:
        return name, "", False


def _recipients(item) -> tuple:
    """(to_addresses, cc_addresses, to_names, cc_names): the names ride
    along with the addresses, same order, '' when Outlook has none."""
    to_list, cc_list, to_names, cc_names = [], [], [], []
    try:
        recipients = item.Recipients
        count = recipients.Count
    except Exception:
        return to_list, cc_list, to_names, cc_names
    for index in range(1, count + 1):
        try:
            recipient = recipients.Item(index)
            address, _ = _smtp_from_address_entry(recipient.AddressEntry)
            if not address:
                address = _text(getattr(recipient, "Address", ""))
            if not address or "@" not in address:
                continue
            name = _text(getattr(recipient, "Name", ""))
            # olTo = 1, olCC = 2, olBCC = 3
            kind = getattr(recipient, "Type", 1)
            if kind == 2:
                cc_list.append(address)
                cc_names.append(name)
            else:
                to_list.append(address)
                to_names.append(name)
        except Exception:
            continue
    return to_list, cc_list, to_names, cc_names


def _add_recipients(info, item) -> None:
    """FX6b: To and Cc resolved onto a mail read light - for our own mails,
    whose recipients say whether a quote went to the requester."""
    info.to_addresses, info.cc_addresses, info.to_names, info.cc_names = _recipients(item)


#: MAPI property tag of the Internet Message-ID header, as a string.
PR_INTERNET_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"


def message_id_of(item) -> str:
    """The Message-ID header of a MailItem, or '' when it has none (a
    draft, a report, an item Outlook made itself)."""
    try:
        return _text(item.PropertyAccessor.GetProperty(PR_INTERNET_MESSAGE_ID))
    except Exception:
        return ""


def _read_item(item, selection_count=1, light=False) -> MailInfo:
    """`light` reads the headers only: resolving each recipient through
    Exchange and pulling the body cost about a second a mail, which a
    search over a whole window cannot afford. Search.hydrate adds the
    body later, to the mails that matter."""
    info = MailInfo(selection_count=selection_count, light=light)
    info.subject = _text(getattr(item, "Subject", ""))
    received = getattr(item, "ReceivedTime", None)
    info.received_on = _date(received)
    info.received_at = _timestamp(received)
    info.entry_id = _text(getattr(item, "EntryID", ""))
    info.conversation_id = _text(getattr(item, "ConversationID", ""))
    info.conversation_topic = _text(getattr(item, "ConversationTopic", ""))
    info.sender_name, info.sender_address, info.sender_internal = _sender(item)
    info.display_to = "; ".join(
        part for part in (_text(getattr(item, "To", "")),
                          _text(getattr(item, "CC", ""))) if part)
    if light:
        return info
    info.to_addresses, info.cc_addresses, info.to_names, info.cc_names = _recipients(item)
    info.message_id = message_id_of(item)
    body = _text(getattr(item, "Body", ""))
    info.body_excerpt = body[:BODY_LIMIT]
    try:
        attached = item.Attachments
        info.attachment_names = [_text(attached.Item(i).FileName) for i in range(1, attached.Count + 1)]
    except Exception:
        info.attachment_names = []
    if info.sender_internal and not info.sender_address:
        info.notes.append(
            "The sender is an internal Exchange address, so the requester was "
            "looked for on To and CC instead.")
    if not info.received_on:
        info.notes.append("Outlook did not give a received date for this mail.")
    return info


def _selection(app):
    """(Selection, count) for the active explorer, or an OutlookError."""
    try:
        explorer = app.ActiveExplorer()
    except Exception as exc:
        raise OutlookError(NOT_RUNNING, str(exc))
    if explorer is None:
        raise OutlookError(NOT_RUNNING)
    try:
        selection = explorer.Selection
        count = selection.Count
    except Exception as exc:
        raise OutlookError(NOTHING_SELECTED, str(exc))
    if not count:
        raise OutlookError(NOTHING_SELECTED)
    return selection, count


def _mail_at(selection, index, count):
    """The MailInfo at this position, or None if it is not a mail."""
    try:
        item = selection.Item(index)
        if getattr(item, "Class", None) != OL_MAIL_ITEM:
            return None
    except Exception:
        return None
    try:
        return _read_item(item, selection_count=count)
    except Exception as exc:
        logs.get("outlook").exception("could not read the selected mail")
        raise OutlookError(
            "Outlook would not hand over that mail. Try selecting it "
            "again.", str(exc))


def read_selection() -> MailInfo:
    """The first mail in the current Outlook selection.

    Raises OutlookError with a sentence for the user if Outlook is not there,
    nothing is selected, or the selected item is not a mail.
    """
    with _com():
        app = _application()
        selection, count = _selection(app)
        for index in range(1, count + 1):
            info = _mail_at(selection, index, count)
            if info is None:
                continue
            if count > 1:
                info.notes.append(
                    "{} mails were selected; the first one was used.".format(count))
            return info
        raise OutlookError(NOT_A_MAIL)


def _folder_at(session, path: str):
    """The Outlook folder at an archive path ('\\Mailbox\\Inbox\\AGENTS'),
    or None."""
    parts = [p for p in (path or "").split("\\") if p]
    if not parts:
        return None
    try:
        roots = session.Folders
        folder = None
        for index in range(1, roots.Count + 1):
            root = roots.Item(index)
            if root.Name == parts[0]:
                folder = root
                break
        if folder is None:
            return None
        for name in parts[1:]:
            folder = folder.Folders.Item(name)
        return folder
    except Exception:
        return None


def _find_item(app, entry_id: str, message_id: str = "", folders=()):
    """M2: a mail wherever it is now. In order: its entry id; its entry id
    in each store (a mail filed into the PST is in another store); with a
    Message-ID, Items.Find on PR_INTERNET_MESSAGE_ID in each candidate
    folder (a move the archive has not seen yet). The log says which step
    found it; every step failing is OutlookError(MOVED)."""
    log = logs.get("outlook")
    if not entry_id and not message_id:
        raise OutlookError(ITEM_GONE)
    session = app.Session
    if entry_id:
        try:
            item = session.GetItemFromID(entry_id)
            if item is not None:
                log.info("found the mail by its entry id")
                return item
        except Exception as exc:
            log.info("GetItemFromID failed: %s", exc)
        try:
            stores = session.Stores
            count = stores.Count
        except Exception:
            count = 0
        for index in range(1, count + 1):
            try:
                store = stores.Item(index)
                item = session.GetItemFromID(entry_id, store.StoreID)
            except Exception:
                continue
            if item is not None:
                log.info("found the mail by its entry id in store %s", _text(getattr(store, "DisplayName", "")) or index)
                return item
    if message_id:
        criteria = "@SQL=\"{}\" = '{}'".format(PR_INTERNET_MESSAGE_ID, message_id.replace("'", "''"))
        for path in folders or ():
            folder = _folder_at(session, path)
            if folder is None:
                continue
            try:
                item = folder.Items.Find(criteria)
            except Exception as exc:
                log.info("Find in %s failed: %s", path, exc)
                continue
            if item is not None:
                log.info("found the mail by its Message-ID in %s", path)
                return item
    log.info("the mail was not found (entry id, %s store(s), %s folder(s))",
             "every" if entry_id else "no", len(folders or ()))
    raise OutlookError(MOVED)


def read_item(entry_id: str, message_id: str = "", folders=()) -> MailInfo:
    """One mail by its entry id (the registered mail for Acknowledge,
    whatever is selected now); M2: also after a move, through its
    Message-ID and the candidate folders."""
    if not entry_id and not message_id:
        raise OutlookError(ITEM_GONE)
    with _com():
        app = _application()
        item = _find_item(app, entry_id, message_id, folders)
        if item is None or getattr(item, "Class", None) != OL_MAIL_ITEM:
            raise OutlookError(ITEM_GONE)
        try:
            return _read_item(item)
        except Exception as exc:
            logs.get("outlook").exception("could not read the mail")
            raise OutlookError("Outlook would not hand over that mail.", str(exc))


def read_selection_all() -> list:
    """Every mail in the current Outlook selection, in Outlook's own order.

    Only `outlook-selftest --all` uses this: registering still takes one mail
    at a time. Items that are not mails are skipped silently; the
    caller compares len(result) with selection_count to see how many.
    """
    with _com():
        app = _application()
        selection, count = _selection(app)
        mails = []
        for index in range(1, count + 1):
            info = _mail_at(selection, index, count)
            if info is not None:
                mails.append(info)
        if not mails:
            raise OutlookError(NOT_A_MAIL)
        return mails


# ------------------------------------------------------------- searching --
#: Case mail is filed under a folder named after the case: 'OPP-1287 (PET
#: Flakes)' or just '-0327 (Industrial Fans and Parts)'. The rule lives with
#: the subject's case number in parsing (AL1).
CASE_FOLDER = parsing.CASE_FOLDER

#: How many folders a search will walk before giving up. A mailbox this size
#: has a few hundred; the cap is only there so a mistake cannot run for ever.
MAX_FOLDERS = 2000

#: Folder.DefaultItemType: 0 is mail. Calendars, contacts, tasks and notes
#: hold nothing this reads, and one of them - an online archive, a sync issues
#: folder - can take minutes to answer.
OL_MAIL_FOLDER = 0

#: Seconds one search term may spend walking the mailbox. Whatever it has
#: found by then is what it returns, and the caller is told it was cut short:
#: a partial answer beats a command that never comes back.
SEARCH_BUDGET = 45.0


@dataclass
class FolderInfo:
    path: str
    folder: object = None
    mail: bool = True

    @property
    def name(self) -> str:
        return self.path.rsplit("\\", 1)[-1]


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Where a case thread usually is when it has no folder of its own.
PRIORITY_FOLDERS = ("inbox", "- inbox", "sent items", "sent", "quotes")

#: Folders a query can sit on for minutes without answering. None of them
#: holds a case mail, so they are not walked at all.
SKIP_FOLDERS = ("sync issues", "conflicts", "local failures", "server failures",
                "junk email", "deleted items", "rss feeds", "yammer root",
                "conversation action settings", "quick step settings")


def _walk(folder, path, found, depth=0):
    if depth > 8 or len(found) >= MAX_FOLDERS:
        return
    try:
        full = path + "\\" + folder.Name
    except Exception:
        return
    if folder.Name.strip().lower() in SKIP_FOLDERS:
        return
    try:
        mail = folder.DefaultItemType == OL_MAIL_FOLDER
    except Exception:
        mail = True
    found.append(FolderInfo(full, folder, mail))
    try:
        children = folder.Folders
        count = children.Count
    except Exception:
        return
    for index in range(1, count + 1):
        try:
            child = children.Item(index)
        except Exception:
            continue
        _walk(child, full, found, depth + 1)


class FolderIndex:
    """The folder-ranking half of a search, shared by the live Outlook
    search and the mail archive's. Subclasses provide folders()."""

    def folders(self, mail_only: bool = True) -> list:      # pragma: no cover
        raise NotImplementedError

    def case_folders(self, opp_no: str) -> list:
        """Folders whose name carries this case number, in either spelling."""
        digits = "".join(ch for ch in str(opp_no) if ch.isdigit()).lstrip("0")
        if not digits:
            return []
        out = []
        for info in self.folders():
            for found in CASE_FOLDER.finditer(info.name):
                if found.group(1).lstrip("0") == digits:
                    out.append(info)
                    break
        return out

    def folders_for(self, hints=(), opp_no: str = "") -> list:
        """The mail folders, most likely first.

        A query is run folder by folder and the budget is 45 s for ~180
        folders, so the order decides what a cut-short search has seen:
        the case's own folder, then folders named after the requester
        (`\\Mailbox\\Clients\\<requester>\\...`), then the inboxes and sent
        items, then everything else in mailbox order.
        """
        wanted = {str(h).strip().casefold() for h in hints
                  if h and len(str(h).strip()) >= 4}
        case = {info.path for info in self.case_folders(opp_no)} if opp_no else set()

        def rank(info):
            if info.path in case:
                return 0
            path = info.path.casefold()
            if any(h in path for h in wanted):
                return 1
            name = info.name.strip().casefold()
            if name in PRIORITY_FOLDERS or "inbox" in name or "sent" in name \
                    or "quote" in path:
                return 2
            return 3
        return sorted(self.folders(), key=rank)      # stable: keeps mailbox order

    def folders_named(self, name: str) -> list:
        wanted = (name or "").strip().casefold()
        if not wanted:
            return []
        return [info for info in self.folders()
                if info.name.strip().casefold() == wanted]


class Search(FolderIndex):
    """One COM session for a whole run of searches.

    Opening Outlook per case would be slow and would hold the apartment open
    and closed repeatedly; propose-status opens this once instead. Nothing
    here writes: it reads folders and items and hands back MailInfo.
    """

    def __init__(self, budget: float = SEARCH_BUDGET):
        self._app = None
        self._folders = None
        self._budget = budget
        #: True when the last search ran out of time before finishing.
        self.cut_short = False
        #: term -> mails. Two cases often share a rare word, and one walk of
        #: the mailbox per term is enough.
        self._by_subject = {}

    def __enter__(self):
        _require_pywin32()
        import pythoncom
        pythoncom.CoInitialize()
        try:
            self._app = _application()
        except BaseException:
            # __exit__ does not run when __enter__ raises: release it here
            pythoncom.CoUninitialize()
            raise
        return self

    def __exit__(self, *exc):
        self._app = None
        self._folders = None
        import pythoncom
        pythoncom.CoUninitialize()
        return False

    # ----------------------------------------------------------- folders --
    def folders(self, mail_only: bool = True) -> list:
        if self._folders is None:
            found = []
            try:
                roots = self._app.GetNamespace("MAPI").Folders
                for index in range(1, roots.Count + 1):
                    _walk(roots.Item(index), "", found)
            except Exception as exc:
                logs.get("outlook").warning("cannot list folders: %s", exc)
            self._folders = found
        if mail_only:
            return [info for info in self._folders if info.mail]
        return self._folders

    # ------------------------------------------------------------- items --
    def _items(self, info, restriction=None, limit=200, light=True) -> list:
        try:
            items = info.folder.Items
            if restriction:
                items = items.Restrict(restriction)
            count = items.Count
        except Exception as exc:
            logs.get("outlook").info("cannot read %s: %s", info.path, exc)
            return []
        out = []
        for index in range(1, min(count, limit) + 1):
            try:
                item = items.Item(index)
                if getattr(item, "Class", None) != OL_MAIL_ITEM:
                    continue
                mail = _read_item(item, light=light)
            except Exception:
                continue
            mail.folder_path = info.path
            out.append(mail)
        return out

    def hydrate(self, mails) -> list:
        """The same mails with their bodies read (the light ones fetched
        again by entry id). FX6b: a light mail of ours also gets its To and
        Cc resolved - propose reads our quote only when it went to the
        requester; the others' recipients stay unresolved (nothing needs
        them, and each costs Exchange a lookup). Read only."""
        out = []
        for mail in mails:
            if getattr(mail, "light", False) and mail.entry_id:
                try:
                    item = self._app.Session.GetItemFromID(mail.entry_id)
                    if getattr(mail, "sender_internal", False):
                        _add_recipients(mail, item)
                    mail.body_excerpt = _text(getattr(item, "Body", ""))[:BODY_LIMIT]
                    mail.light = False
                except Exception as exc:
                    logs.get("outlook").info("cannot read the body of %r: %s",
                                             mail.subject, exc)
            out.append(mail)
        return out

    def in_folder(self, info, limit=200) -> list:
        return self._items(info, None, limit)

    def by_subject(self, term: str, limit=200, hints=(), opp_no="") -> list:
        """Every mail whose subject contains `term`, anywhere in the mailbox.

        Outlook does the filtering (a DASL restriction), so this is a query
        rather than a walk over every item.
        """
        term = (term or "").strip()
        if not term or "'" in term:
            return []
        cached = self._by_subject.get(term.casefold())
        if cached is not None:
            return cached
        restriction = ("@SQL=\"urn:schemas:httpmail:subject\" LIKE '%{}%'"
                       .format(term))
        out = []
        deadline = time.monotonic() + self._budget
        self.cut_short = False
        for info in self.folders_for(hints, opp_no):
            out.extend(self._items(info, restriction, limit))
            if len(out) >= limit:
                break
            if time.monotonic() > deadline:
                # Some folders - an online archive, a sync issues folder -
                # can take minutes to answer a query. Whatever has been found
                # by now is the answer, and the caller says so.
                self.cut_short = True
                logs.get("outlook").info(
                    "search for %r cut short at %s", term, info.path)
                break
        self._by_subject[term.casefold()] = out
        return out

    def by_domains(self, domains, start=None, end=None, limit=2000,
                   hints=(), opp_no="") -> list:
        """Every mail one of these domains sent, or was sent to, between
        `start` and `end` (YYYY-MM-DD, both inclusive).

        Outlook filters on the From, To and CC headers and on the received
        date (DASL); the caller narrows by case, because the same agent is
        on many threads and the mailbox does not know which one this is.
        The dates go into the query rather than being filtered afterwards:
        an agent with years of mail would otherwise fill the limit with
        old threads before the window was reached.
        """
        wanted = sorted({str(d).strip().lower().lstrip("@") for d in domains
                         if d and "'" not in str(d)})
        if not wanted:
            return []
        cache_key = ("domains",) + tuple(wanted) + (start or "", end or "")
        cached = self._by_subject.get(cache_key)
        if cached is not None:
            return cached
        clauses = []
        for domain in wanted:
            # 'to' and 'cc' are display names; they carry the address only
            # when Outlook shows it ('renata.vidal@tolvex.example'), which a
            # reply to an outside address often does.
            for header in ("fromemail", "to", "cc", "displayto", "displaycc"):
                clauses.append("\"urn:schemas:httpmail:{}\" LIKE '%{}%'"
                               .format(header, domain))
        restriction = "(" + " OR ".join(clauses) + ")"
        for bound, op, when in ((start, ">=", "00:00"), (end, "<=", "23:59")):
            if bound and _DATE_ONLY.match(str(bound)):
                restriction += (" AND \"urn:schemas:httpmail:datereceived\" "
                                "{} '{} {}'".format(op, bound, when))
        restriction = "@SQL=" + restriction
        out = []
        deadline = time.monotonic() + self._budget
        self.cut_short = False
        for info in self.folders_for(hints, opp_no):
            found = self._items(info, restriction, limit)
            for mail in found:
                mail.header_match = True
            out.extend(found)
            if len(out) >= limit:
                break
            if time.monotonic() > deadline:
                self.cut_short = True
                logs.get("outlook").info(
                    "domain search for %s cut short at %s", wanted, info.path)
                break
        self._by_subject[cache_key] = out
        return out

    def conversation_of(self, mail, limit=100) -> list:
        """Every mail Outlook files under this mail's conversation, in
        whatever folder of the store it sits - the way the reading pane's
        conversation view finds them. One call, no folder walk.

        The domain search only sees mail the other side sent (Outlook's
        To/CC headers are display names, not addresses); this is how our
        own replies and quotes on the same thread come back.
        """
        key = ("conv", mail.conversation_id or mail.entry_id)
        cached = self._by_subject.get(key)
        if cached is not None:
            return cached
        out, seen = [], set()
        try:
            session = self._app.Session
            item = session.GetItemFromID(mail.entry_id)
            conversation = item.GetConversation()
            table = conversation.GetTable() if conversation is not None else None
            while table is not None and not table.EndOfTable and len(out) < limit:
                row = table.GetNextRow()
                entry_id = _text(row.Item("EntryID"))
                if not entry_id or entry_id in seen:
                    continue
                seen.add(entry_id)
                try:
                    found = session.GetItemFromID(entry_id)
                    if getattr(found, "Class", None) != OL_MAIL_ITEM:
                        continue
                    info = _read_item(found, light=True)
                    if info.sender_internal:
                        # FX6b: whom our own mails went to (our quote is one
                        # that went to the requester); the others' stay unread
                        _add_recipients(info, found)
                    info.body_excerpt = _text(getattr(found, "Body", ""))[:BODY_LIMIT]
                    info.light = False
                    try:
                        info.folder_path = _text(found.Parent.FolderPath)
                    except Exception:
                        pass
                    out.append(info)
                except Exception:
                    continue
        except Exception as exc:
            logs.get("outlook").info("conversation of %r not readable: %s",
                                     mail.subject, exc)
        if mail.entry_id and mail.entry_id not in seen:
            out.append(mail)
        self._by_subject[key] = out
        return out

    def by_conversation(self, conversation_id: str, limit=200) -> list:
        wanted = (conversation_id or "").strip()
        if not wanted:
            return []
        out = []
        deadline = time.monotonic() + self._budget
        for info in self.folders():
            for mail in self._items(info, None, limit):
                if mail.conversation_id == wanted:
                    out.append(mail)
            if time.monotonic() > deadline:
                self.cut_short = True
                break
        return out


def open_item(entry_id: str, message_id: str = "", folders=()) -> None:
    """Show an existing mail in Outlook. Opens a window, changes nothing.
    M2: found after a move too (_find_item)."""
    if not entry_id and not message_id:
        raise OutlookError(ITEM_GONE)
    with _com():
        app = _application()
        item = _find_item(app, entry_id, message_id, folders)
        try:
            item.Display()
        except Exception as exc:
            raise OutlookError(
                "Outlook would not open that mail.", str(exc))


#: FX4: draft_reply_all and draft_new (the acknowledgment, follow-up and
#: feedback drafts) went with the mail templates. FX5 (2026-11-13): the
#: acknowledgment is back, blank - blank_reply_all and blank_new below.

#: Application.CreateItem: a new mail (olMailItem)
OL_CREATE_MAIL = 0


def blank_reply_all(entry_id: str, make_subject, message_id: str = "", folders=()) -> None:
    """FX5: Acknowledge, blank - a Reply All of the registered mail, its
    subject `make_subject(subject)`, shown for her to write and send. The
    body is not touched: what Outlook puts in a reply (her signature, the
    mail replied to) is all there is. Displayed only: never sent, never
    saved."""
    if not entry_id and not message_id:
        raise OutlookError(ITEM_GONE)
    with _com():
        app = _application()
        item = _find_item(app, entry_id, message_id, folders)
        if item is None or getattr(item, "Class", None) != OL_MAIL_ITEM:
            raise OutlookError(ITEM_GONE)
        try:
            reply = item.ReplyAll()
            reply.Subject = make_subject(_text(reply.Subject) or _text(item.Subject))
            reply.Display()                       # Outlook inserts her signature
        except OutlookError:
            raise
        except Exception as exc:
            logs.get("outlook").exception("could not open the reply")
            raise OutlookError("Outlook would not open a reply to that mail.", str(exc))


def blank_new(to: str, cc: str, subject: str) -> None:
    """FX5: Acknowledge, blank, for a colleague's forward - a new mail to the
    original sender, the colleague on Cc; the body is what Outlook puts in a
    new mail (her signature). Displayed only: never sent, never saved."""
    with _com():
        app = _application()
        try:
            item = app.CreateItem(OL_CREATE_MAIL)
            item.To = to or ""
            item.CC = cc or ""
            item.Subject = subject or ""
            item.Display()                        # Outlook inserts her signature
        except OutlookError:
            raise
        except Exception as exc:
            logs.get("outlook").exception("could not open a new mail")
            raise OutlookError("Outlook would not open a new mail.", str(exc))


# ----------------------------------------------------------- attachments --
#: Attachment.Type: an attached Outlook item (a mail) - saved as .msg.
OL_EMBEDDED_ITEM = 5
#: PR_ATTACH_CONTENT_ID: a picture pasted into the body carries one.
PR_ATTACH_CONTENT_ID = "http://schemas.microsoft.com/mapi/proptag/0x3712001F"
#: Item.Close: throw away any change (there is none - the item is only read).
OL_DISCARD = 1


def _safe_name(index: int, name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", name or "attachment").strip(" .") or "attachment"
    return "{:02d}_{}".format(index, stem[:80])


def _inline(attachment, name: str) -> bool:
    from . import attachments as att
    if att.is_inline_image(name):
        return True
    try:
        return bool(_text(attachment.PropertyAccessor.GetProperty(PR_ATTACH_CONTENT_ID))) and att.is_image(name)
    except Exception:
        return False


def _read_item_attachments(app, item, folder, depth=0) -> list:
    """[FileText] for one item's attachments: each saved as a copy in
    `folder` (Attachment.SaveAsFile), read, and left for the caller to
    delete with the folder. An attached mail is opened from its copy
    (Session.OpenSharedItem) for its body and, one level down, its own
    attachments. Inline pictures are skipped without a word."""
    from . import attachments as att
    out = []
    try:
        items = item.Attachments
        count = items.Count
    except Exception:
        return out
    for index in range(1, count + 1):
        try:
            a = items.Item(index)
            name = _text(a.FileName) or _text(a.DisplayName) or "attachment {}".format(index)
            kind = getattr(a, "Type", None)
            size = int(getattr(a, "Size", 0) or 0)
        except Exception as exc:
            out.append(att.FileText(name="attachment {}".format(index),
                                    reason=att.NOT_READ_BROKEN.format(str(exc)[:60])))
            continue
        is_mail = kind == OL_EMBEDDED_ITEM or name.lower().endswith(".msg")
        if is_mail and not name.lower().endswith(".msg"):
            name += ".msg"
        if att.is_image(name):
            if not _inline(a, name):
                out.append(att.FileText(name=name, reason=att.NOT_READ_IMAGE))
            continue
        if size > att.MAX_BYTES:
            out.append(att.FileText(name=name, reason=att.NOT_READ_SIZE))
            continue
        if is_mail and depth >= 1:
            out.append(att.FileText(name=name, reason=att.NOT_READ_DEEP))
            continue
        ext = (name.rsplit(".", 1)[-1].lower() if "." in name else "")
        if not is_mail and "." + ext not in att.READABLE:
            out.append(att.FileText(name=name, reason=att.NOT_READ_KIND.format("." + ext if ext else "nameless")))
            continue
        path = folder / _safe_name(index, name)
        try:
            a.SaveAsFile(str(path))
        except Exception as exc:
            logs.get("outlook").info("SaveAsFile failed for %s: %s", name, exc)
            out.append(att.FileText(name=name, reason=att.NOT_READ_BROKEN.format("Outlook would not save a copy")))
            continue
        if not is_mail:
            out.append(att.read_file(path, name, size))
            continue
        out.append(_read_attached_mail(app, path, name, folder, depth))
    return out


def _read_attached_mail(app, path, name, folder, depth):
    from . import attachments as att
    found = att.FileText(name=name)
    inner = None
    try:
        inner = app.Session.OpenSharedItem(str(path))
        body = _text(getattr(inner, "Body", ""))[:BODY_LIMIT]
        found.lines = [" ".join(l.split()) for l in body.splitlines() if l.strip()]
        sub = folder / (path.stem + "_files")
        sub.mkdir(exist_ok=True)
        found.children = [att.FileText(name="{} / {}".format(name, c.name), lines=c.lines, read=c.read,
                                       reason=c.reason)
                          for c in _read_item_attachments(app, inner, sub, depth + 1)]
        found.read = bool(found.lines)
        if not found.read:
            found.reason = att.NOT_READ_EMPTY
    except Exception as exc:
        logs.get("outlook").info("OpenSharedItem failed for %s: %s", name, exc)
        found.reason = att.NOT_READ_BROKEN.format("Outlook would not open the attached mail")
    finally:
        if inner is not None:
            try:
                inner.Close(OL_DISCARD)
            except Exception:
                pass
    return found


def read_attachments(entry_id: str, message_id: str = "", folders=()) -> list:
    """The attachments of one mail as [FileText] (batch 14). Copies are
    saved into a temporary folder of the app's own and deleted before this
    returns, whatever happens; the mail itself is only read. RD2: with a
    Message-ID and candidate folders, a mail that moved since the archive
    saw it is found the way read_item finds it (_find_item)."""
    import shutil
    import tempfile
    from pathlib import Path

    if not entry_id and not message_id:
        raise OutlookError(ITEM_GONE)
    with _com():
        app = _application()
        if message_id:
            item = _find_item(app, entry_id, message_id, folders)
        else:
            try:
                item = app.Session.GetItemFromID(entry_id)
            except Exception as exc:
                logs.get("outlook").info("GetItemFromID failed: %s", exc)
                raise OutlookError(ITEM_GONE, str(exc))
        folder = Path(tempfile.mkdtemp(prefix="opplist-attachments-"))
        try:
            return _read_item_attachments(app, item, folder)
        except Exception as exc:
            logs.get("outlook").exception("reading attachments failed")
            raise OutlookError("Outlook would not hand over the attachments.", str(exc))
        finally:
            shutil.rmtree(folder, ignore_errors=True)


@dataclass
class AttachedFile:
    """Q2d: one attachment as Read quote sends it - its bytes (None when
    not copied), its text when attachments.read_file has one, and whether
    it is a picture pasted into the body."""

    name: str
    size: int = 0
    inline: bool = False
    data: bytes | None = None
    text: object = None              # attachments.FileText or None
    reason: str = ""
    path: str = ""                   # RQ2: a file sent from the team site, as kept on disk


def read_files(entry_id: str, message_id: str = "", folders=()) -> list:
    """Q2d: the attachments of one mail as [AttachedFile]: each copied into
    a temporary folder of the app's own (Attachment.SaveAsFile), its bytes
    and text read, the copies deleted before this returns. The mail is only
    read: nothing is moved, marked read or changed. An attached mail is not
    opened (Read quote does not send one)."""
    import shutil
    import tempfile
    from pathlib import Path

    from . import attachments as att

    if not entry_id and not message_id:
        raise OutlookError(ITEM_GONE)
    with _com():
        app = _application()
        item = _find_item(app, entry_id, message_id, folders)
        if item is None or getattr(item, "Class", None) != OL_MAIL_ITEM:
            raise OutlookError(ITEM_GONE)
        folder = Path(tempfile.mkdtemp(prefix="opplist-readquote-"))
        out = []
        try:
            items = item.Attachments
            for index in range(1, items.Count + 1):
                a = items.Item(index)
                name = _text(a.FileName) or _text(a.DisplayName) or "attachment {}".format(index)
                size = int(getattr(a, "Size", 0) or 0)
                found = AttachedFile(name=name, size=size, inline=att.is_image(name) and _inline(a, name))
                is_mail = getattr(a, "Type", None) == OL_EMBEDDED_ITEM or name.lower().endswith(".msg")
                if found.inline or is_mail or size > att.MAX_BYTES:
                    out.append(found)
                    continue
                path = folder / _safe_name(index, name)
                try:
                    a.SaveAsFile(str(path))
                    found.data = path.read_bytes()
                    if not att.is_image(name):
                        found.text = att.read_file(path, name, size)
                except Exception as exc:
                    logs.get("outlook").info("SaveAsFile failed for %s: %s", name, exc)
                    found.reason = "Outlook would not save a copy"
                out.append(found)
            return out
        except OutlookError:
            raise
        except Exception as exc:
            logs.get("outlook").exception("reading attachments failed")
            raise OutlookError("Outlook would not hand over the attachments.", str(exc))
        finally:
            shutil.rmtree(folder, ignore_errors=True)


def _attached_files(item, folder) -> list:
    """[AttachedFile] of one item's attachments, copied into `folder`
    (the caller deletes it), as read_files reads them."""
    from . import attachments as att
    out = []
    items = item.Attachments
    for index in range(1, items.Count + 1):
        a = items.Item(index)
        name = _text(a.FileName) or _text(a.DisplayName) or "attachment {}".format(index)
        size = int(getattr(a, "Size", 0) or 0)
        found = AttachedFile(name=name, size=size, inline=att.is_image(name) and _inline(a, name))
        is_mail = getattr(a, "Type", None) == OL_EMBEDDED_ITEM or name.lower().endswith(".msg")
        if found.inline or is_mail or size > att.MAX_BYTES:
            out.append(found)
            continue
        path = folder / _safe_name(index, name)
        try:
            a.SaveAsFile(str(path))
            found.data = path.read_bytes()
            if not att.is_image(name):
                found.text = att.read_file(path, name, size)
        except Exception as exc:
            logs.get("outlook").info("SaveAsFile failed for %s: %s", name, exc)
            found.reason = "Outlook would not save a copy"
        out.append(found)
    return out


MSG_NOT_OPENED = "Outlook would not open it"


def open_msg(path) -> tuple:
    """RQ2: a .msg file sent from the team site, opened through Outlook
    (Session.OpenSharedItem) and only read: (MailInfo with its body,
    subject, sender and sent time, [AttachedFile]). Nothing is saved into
    a mailbox; the item is closed with its changes thrown away (there are
    none), and the attachments' copies are deleted before this returns."""
    import shutil
    import tempfile
    from pathlib import Path

    with _com():
        app = _application()
        item = None
        folder = Path(tempfile.mkdtemp(prefix="opplist-siteread-"))
        try:
            try:
                item = app.Session.OpenSharedItem(str(path))
            except Exception as exc:
                logs.get("outlook").info("OpenSharedItem failed for %s: %s", path, exc)
                raise OutlookError(MSG_NOT_OPENED, str(exc))
            if getattr(item, "Class", None) != OL_MAIL_ITEM:
                raise OutlookError("it is not a mail")
            info = _read_item(item)
            sent = getattr(item, "SentOn", None)
            info.sent_at = (_timestamp(sent) or "")[:16]
            try:
                files = _attached_files(item, folder)
            except Exception as exc:
                logs.get("outlook").info("the attachments of %s: %s", path, exc)
                files = []
            return info, files
        finally:
            if item is not None:
                try:
                    item.Close(OL_DISCARD)
                except Exception:
                    pass
            shutil.rmtree(folder, ignore_errors=True)


def attachment_names(entry_id: str) -> list:
    """The names of a mail's attachments, inline pictures left out - for the
    case page's 'Read attachments' line. [] when Outlook cannot say."""
    from . import attachments as att
    if not entry_id or not available():
        return []
    try:
        with _com():
            item = _application().Session.GetItemFromID(entry_id)
            names = []
            for index in range(1, item.Attachments.Count + 1):
                a = item.Attachments.Item(index)
                name = _text(a.FileName) or _text(a.DisplayName)
                if name and not (att.is_image(name) and _inline(a, name)):
                    names.append(name)
            return names
    except Exception as exc:
        logs.get("outlook").info("attachment names failed: %s", exc)
        return []
