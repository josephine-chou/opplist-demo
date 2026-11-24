r"""A local archive of the mailbox, so that nothing has to scan Outlook twice.

Why: propose-status used to run one DASL query per case over the whole
mailbox. That held the COM server for nine minutes and more, and the first
hiccup in Outlook ('The RPC server is unavailable') failed every case at
once - on 2026-09-14 all 93 open cases came back 'no thread found' and no
suggestion was written. The same mail had been read by the PowerShell board
ten minutes earlier.

So the mail is read once, into data\mail-archive\mail.db (its own file, its
own schema version, never inside opplist.db), and everything that used to
query Outlook queries this instead:

  * sync()          - one pass over the folders, incremental after the first
                      run; the only thing here that touches Outlook. Read
                      only: nothing is created, moved, marked or deleted.
  * ArchiveSearch   - the same interface as outlook_bridge.Search, answered
                      from SQLite, so propose.propose() runs unchanged.
  * search_terms()  - the keyword search of scripts\Search-Mail.ps1, with the
                      same output files (a daily AWB report uses it).

The archive never deletes a message. A mail deleted or moved in Outlook keeps
its row; a move only updates folder_path and entry_id, because the Message-ID
header (PR_INTERNET_MESSAGE_ID) is the key and it survives Sort-Inbox.
"""

from __future__ import annotations

import csv
import datetime as dt
import fnmatch
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import logs, outlook_bridge, parsing, paths
from .outlook_bridge import FolderInfo, MailInfo, OutlookError

ARCHIVE_DIR = paths.DATA_DIR / "mail-archive"
ARCHIVE_DB = ARCHIVE_DIR / "mail.db"
SYNC_LOG = ARCHIVE_DIR / "sync-log.txt"
SEARCH_DIR = paths.DATA_DIR / "mail-search"

#: The archive's own schema version (meta.schema_version).
SCHEMA_VERSION = 2
#: A body is stored whole up to this, like the live read (outlook_bridge).
BODY_LIMIT = outlook_bridge.BODY_LIMIT
#: An archive older than this is not trusted for a scan: propose-status
#: falls back to Outlook and the Tracking page shows the line in red.
STALE_HOURS = 36
#: MF1: what the case page's Mail card and Tracking say when the last sync
#: is more than one working day old (status()['behind_line'])
BEHIND_LINE = "Mail archive last synced {at}. Run python -m opplist mail-sync with Outlook open."
#: A folder synced for the first time without --days / --full reads this far
#: back (decided 2026-09-14).
FIRST_SYNC_DAYS = 60
#: The incremental window starts this long before the folder's last sync,
#: so a mail delivered late, or filed after the last pass, is still picked
#: up (decided 2026-09-14: a normal day's sync in about a minute).
OVERLAP_HOURS = 24
#: --relocate without --days looks this far back.
RELOCATE_DAYS = 30
#: Outlook connection attempts and the pause between them (seconds).
RETRIES = 3
RETRY_WAIT = 30
#: This many folders failing in a row means Outlook is gone, not the folder.
GONE_AFTER = 5
#: Conversations without an id are matched on topic within this many days.
TOPIC_WINDOW_DAYS = 90

PR_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x39FE001F"
PR_SENDER_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x5D01001F"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS message (
    id                 INTEGER PRIMARY KEY,
    message_id         TEXT NOT NULL DEFAULT '',
    entry_id           TEXT NOT NULL DEFAULT '',
    store              TEXT NOT NULL DEFAULT '',
    folder_path        TEXT NOT NULL DEFAULT '',
    conversation_id    TEXT NOT NULL DEFAULT '',
    conversation_topic TEXT NOT NULL DEFAULT '',
    received_at        TEXT NOT NULL DEFAULT '',
    sent_at            TEXT NOT NULL DEFAULT '',
    sender_name        TEXT NOT NULL DEFAULT '',
    sender_address     TEXT NOT NULL DEFAULT '',
    sender_internal    INTEGER NOT NULL DEFAULT 0,
    to_addresses       TEXT NOT NULL DEFAULT '[]',
    cc_addresses       TEXT NOT NULL DEFAULT '[]',
    display_to         TEXT NOT NULL DEFAULT '',
    subject            TEXT NOT NULL DEFAULT '',
    body               TEXT NOT NULL DEFAULT '',
    body_top           TEXT NOT NULL DEFAULT '',
    has_attachments    INTEGER NOT NULL DEFAULT 0,
    attachment_names   TEXT NOT NULL DEFAULT '[]',
    first_seen_at      TEXT NOT NULL DEFAULT '',
    last_seen_at       TEXT NOT NULL DEFAULT ''
);
-- The key is the Message-ID header; a mail without one is keyed on its
-- entry id instead (and is then lost track of when Sort-Inbox moves it).
CREATE UNIQUE INDEX IF NOT EXISTS ux_message_message_id
    ON message(message_id) WHERE message_id <> '';
CREATE UNIQUE INDEX IF NOT EXISTS ux_message_entry_id
    ON message(entry_id) WHERE message_id = '';
CREATE INDEX IF NOT EXISTS ix_message_received ON message(received_at);
CREATE INDEX IF NOT EXISTS ix_message_conversation ON message(conversation_id);
CREATE INDEX IF NOT EXISTS ix_message_topic ON message(conversation_topic);
CREATE INDEX IF NOT EXISTS ix_message_folder ON message(folder_path);
CREATE INDEX IF NOT EXISTS ix_message_entry ON message(entry_id);
CREATE INDEX IF NOT EXISTS ix_message_sender ON message(sender_address);
-- F2c (2026-10-09): by_subject scans this covering index, not the table
CREATE INDEX IF NOT EXISTS ix_message_subject ON message(subject);

-- Every folder a mail has been seen in (a copy in Sent Items and one in
-- the case folder are the same Message-ID). message.folder_path is the
-- primary one: the case folder if any, else the first seen. message_id here
-- is the archive key: the Message-ID header, or the entry id for a mail
-- without one.
CREATE TABLE IF NOT EXISTS message_location (
    message_id    TEXT NOT NULL,
    folder_path   TEXT NOT NULL,
    entry_id      TEXT NOT NULL DEFAULT '',
    store         TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL DEFAULT '',
    last_seen_at  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (message_id, folder_path)
);
CREATE INDEX IF NOT EXISTS ix_location_folder ON message_location(folder_path);
-- F2c: by_entry_id's fallback finds a moved mail's old entry id here
CREATE INDEX IF NOT EXISTS ix_location_entry ON message_location(entry_id);

CREATE TABLE IF NOT EXISTS folder_sync (
    folder_path      TEXT PRIMARY KEY,
    position         INTEGER NOT NULL DEFAULT 0,
    last_received_at TEXT,
    last_synced_at   TEXT,
    last_count       INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    subject, body, content='message', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS message_ai AFTER INSERT ON message BEGIN
    INSERT INTO message_fts(rowid, subject, body)
        VALUES (new.id, new.subject, new.body);
END;
CREATE TRIGGER IF NOT EXISTS message_ad AFTER DELETE ON message BEGIN
    INSERT INTO message_fts(message_fts, rowid, subject, body)
        VALUES ('delete', old.id, old.subject, old.body);
END;
CREATE TRIGGER IF NOT EXISTS message_au AFTER UPDATE OF subject, body ON message BEGIN
    INSERT INTO message_fts(message_fts, rowid, subject, body)
        VALUES ('delete', old.id, old.subject, old.body);
    INSERT INTO message_fts(rowid, subject, body)
        VALUES (new.id, new.subject, new.body);
END;
"""

#: version -> list of SQL statements that bring the archive from version-1
#: to version. Bump SCHEMA_VERSION and add an entry; open_archive applies
#: them in order (the tables themselves are CREATE IF NOT EXISTS in SCHEMA,
#: so a migration only has to fill or alter).
MIGRATIONS: dict = {
    # 2 (2026-09-14): message_location, seeded with each mail's one known place.
    2: [
        "INSERT OR IGNORE INTO message_location(message_id, folder_path, entry_id, "
        "store, first_seen_at, last_seen_at) "
        "SELECT CASE WHEN message_id <> '' THEN message_id ELSE entry_id END, "
        "folder_path, entry_id, store, first_seen_at, last_seen_at FROM message "
        "WHERE folder_path <> ''",
    ],
}


class ArchiveError(RuntimeError):
    """The archive is missing or not usable. The message is for the user."""


# ------------------------------------------------------------- the file --
def open_archive(db_path=None, create: bool = True) -> sqlite3.Connection:
    """Open (and create or upgrade) the archive. WAL, so a sync can write
    while the web app reads."""
    path = Path(db_path or ARCHIVE_DB)
    if not create and not path.exists():
        raise ArchiveError(
            "No mail archive yet at {}. Run: python -m opplist mail-sync --days 60"
            .format(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    version = int(get_meta(conn, "schema_version", "0") or 0)
    if version == 0:
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    else:
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS.get(target, ()):
                conn.execute(statement)
            set_meta(conn, "schema_version", str(target))
    conn.commit()
    return conn


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def set_meta(conn, key, value) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def _now(now=None) -> dt.datetime:
    return now or dt.datetime.now()


def _stamp(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def status(db_path=None, now=None) -> dict:
    """What the Tracking page shows and propose-status checks: whether the
    archive is there, how many mails, when it was last synced, and whether
    that is too long ago to trust."""
    path = Path(db_path or ARCHIVE_DB)
    out = {"path": str(path), "exists": path.exists(), "count": 0,
           "last_sync_at": "", "hours_since": None, "stale": True,
           "ok": False, "reason": "", "behind": False, "behind_line": ""}
    if not path.exists():
        out["reason"] = "no mail archive yet (run: python -m opplist mail-sync --days 60)"
        return out
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            out["count"] = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
            out["last_sync_at"] = get_meta(conn, "last_sync_at", "") or ""
        finally:
            conn.close()
    except sqlite3.Error as exc:
        out["reason"] = "mail archive unreadable: {}".format(exc)
        return out
    if not out["last_sync_at"]:
        out["reason"] = "the mail archive has never completed a sync"
        return out
    try:
        last = dt.datetime.strptime(out["last_sync_at"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        out["reason"] = "the mail archive's last sync time is unreadable"
        return out
    hours = (_now(now) - last).total_seconds() / 3600.0
    out["hours_since"] = round(hours, 1)
    # MF1: more than one working day behind - the case page's Mail card and
    # Tracking say so in one line (the 7 to 9 Nov runs were skipped unseen)
    from .tracking import business_days_between
    behind = business_days_between(last.date(), _now(now).date())
    if behind is not None and behind > 1:
        out["behind"] = True
        out["behind_line"] = BEHIND_LINE.format(at=out["last_sync_at"][:16])
    out["stale"] = hours > STALE_HOURS
    if out["stale"]:
        out["reason"] = "the mail archive was last synced {} ({:.0f} h ago, more than {} h)".format(
            out["last_sync_at"], hours, STALE_HOURS)
    elif not out["count"]:
        out["reason"] = "the mail archive is empty"
    else:
        out["ok"] = True
    return out


def usable(db_path=None, now=None) -> tuple:
    """(True, '') when a scan may read the archive instead of Outlook."""
    info = status(db_path, now)
    return info["ok"], info["reason"]


# ------------------------------------------------------------ the record --
@dataclass
class Record:
    """One row of message, before it is stored."""

    message_id: str = ""
    entry_id: str = ""
    store: str = ""
    folder_path: str = ""
    conversation_id: str = ""
    conversation_topic: str = ""
    received_at: str = ""
    sent_at: str = ""
    sender_name: str = ""
    sender_address: str = ""
    sender_internal: bool = False
    to_addresses: list = field(default_factory=list)
    cc_addresses: list = field(default_factory=list)
    display_to: str = ""
    subject: str = ""
    body: str = ""
    has_attachments: bool = False
    attachment_names: list = field(default_factory=list)

    @property
    def key(self) -> tuple:
        """('message_id', value) or ('entry_id', value)."""
        if self.message_id:
            return "message_id", self.message_id
        return "entry_id", self.entry_id


#: Where the writer's own words end and the quoted chain begins: the quote
#: headers parsing already knows, plus the 'On ... wrote:' family.
_WROTE = re.compile(r"^\s*(?:On .{5,160} wrote:|El .{5,160} escribi|Em .{5,160} escreveu:"
                    # AL6a: Vietnamese Gmail, 'Vao Thu 5, 17 thg 9, 2026 vao luc 05:58 <name> <addr> da viet:'
                    r"|V\u00e0o .{5,400} \u0111\u00e3 vi\u1ebft:)",
                    re.IGNORECASE)


def recompute_body_top(conn, dry_run: bool = False) -> tuple:
    """AL6a: body_top as top_part reads it today, for the mails whose top
    part changed since they were synced. (changed, total)."""
    changed, total = 0, 0
    for row in conn.execute("SELECT id, body, body_top FROM message").fetchall():
        total += 1
        top = top_part(row["body"] or "")
        if top != (row["body_top"] or ""):
            changed += 1
            if not dry_run:
                conn.execute("UPDATE message SET body_top = ? WHERE id = ?", (top, row["id"]))
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return changed, total


def top_part(body: str) -> str:
    """The new part of a mail: everything above the first quoted header.

    Lines are kept as written (blank ones included, leading ones dropped),
    unlike propose._quote_free which packs them for an evidence sentence.
    """
    keep, started = [], False
    for raw in (body or "").splitlines():
        line = raw.rstrip()
        if not line.strip() and not started:
            continue
        if parsing._QUOTE_START.match(line) or _WROTE.match(line):
            break
        started = True
        keep.append(line)
    return "\n".join(keep).strip()


def record_from_info(info: MailInfo, store: str = "", sent_at: str = "",
                     has_attachments: bool = False, attachment_names=()) -> Record:
    """A Record out of a MailInfo (the shape the tests feed in; the sync
    reads Outlook items straight into Records with the same fields)."""
    received = info.received_at or (info.received_on + " 00:00" if info.received_on else "")
    if received and len(received) == 16:
        received += ":00"
    return Record(
        message_id=getattr(info, "message_id", "") or "",
        entry_id=info.entry_id or "",
        store=store or _store_of(info.folder_path),
        folder_path=info.folder_path or "",
        conversation_id=info.conversation_id or "",
        conversation_topic=info.conversation_topic or "",
        received_at=received,
        sent_at=sent_at,
        sender_name=info.sender_name or "",
        sender_address=info.sender_address or "",
        sender_internal=bool(info.sender_internal),
        to_addresses=list(info.to_addresses or []),
        cc_addresses=list(info.cc_addresses or []),
        display_to=info.display_to or "",
        subject=info.subject or "",
        body=(info.body_excerpt or "")[:BODY_LIMIT],
        has_attachments=bool(has_attachments),
        attachment_names=list(attachment_names or []),
    )


def _store_of(folder_path: str) -> str:
    parts = [p for p in (folder_path or "").split("\\") if p]
    return parts[0] if parts else ""


def _timestamp_full(value) -> str:
    try:
        return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
            value.year, value.month, value.day, value.hour, value.minute,
            getattr(value, "second", 0) or 0)
    except Exception:
        return ""


def _recipients_of(item) -> tuple:
    """(to, cc) SMTP addresses. PR_SMTP_ADDRESS on the recipient is one
    property read; resolving the Exchange user is the fallback."""
    _text = outlook_bridge._text
    to_list, cc_list = [], []
    try:
        recipients = item.Recipients
        count = recipients.Count
    except Exception:
        return to_list, cc_list
    for index in range(1, count + 1):
        try:
            recipient = recipients.Item(index)
            address = ""
            try:
                address = _text(recipient.PropertyAccessor.GetProperty(PR_SMTP_ADDRESS))
            except Exception:
                address = ""
            if "@" not in address:
                address, _ = outlook_bridge._smtp_from_address_entry(recipient.AddressEntry)
            if "@" not in address:
                address = _text(getattr(recipient, "Address", ""))
            if "@" not in address:
                continue
            kind = getattr(recipient, "Type", 1)          # olTo 1, olCC 2, olBCC 3
            (cc_list if kind == 2 else to_list).append(address)
        except Exception:
            continue
    return to_list, cc_list


def _sender_of(item) -> tuple:
    """(name, smtp, internal): the sender's SMTP property first, then the
    slower Exchange lookup outlook_bridge does."""
    _text = outlook_bridge._text
    name, address, internal = outlook_bridge._sender(item)
    if "@" not in address:
        try:
            smtp = _text(item.PropertyAccessor.GetProperty(PR_SENDER_SMTP_ADDRESS))
            if "@" in smtp:
                address = smtp
        except Exception:
            pass
    return name, address, internal


def record_from_item(item, folder_path: str) -> Record:
    """Read one Outlook MailItem whole. Reads only."""
    _text = outlook_bridge._text
    record = Record(folder_path=folder_path, store=_store_of(folder_path))
    record.subject = _text(getattr(item, "Subject", ""))
    record.received_at = _timestamp_full(getattr(item, "ReceivedTime", None))
    record.sent_at = _timestamp_full(getattr(item, "SentOn", None))
    if not record.received_at:
        record.received_at = record.sent_at
    record.entry_id = _text(getattr(item, "EntryID", ""))
    record.message_id = outlook_bridge.message_id_of(item)
    record.conversation_id = _text(getattr(item, "ConversationID", ""))
    record.conversation_topic = _text(getattr(item, "ConversationTopic", ""))
    record.sender_name, record.sender_address, record.sender_internal = _sender_of(item)
    record.display_to = "; ".join(
        part for part in (_text(getattr(item, "To", "")),
                          _text(getattr(item, "CC", ""))) if part)
    record.to_addresses, record.cc_addresses = _recipients_of(item)
    record.body = _text(getattr(item, "Body", ""))[:BODY_LIMIT]
    try:
        attachments = item.Attachments
        count = attachments.Count
    except Exception:
        count = 0
    record.has_attachments = count > 0
    names = []
    for index in range(1, min(count, 30) + 1):
        try:
            names.append(_text(attachments.Item(index).FileName))
        except Exception:
            continue
    record.attachment_names = names
    return record


# --------------------------------------------------------------- storing --
_COLUMNS = ("message_id", "entry_id", "store", "folder_path", "conversation_id",
            "conversation_topic", "received_at", "sent_at", "sender_name",
            "sender_address", "sender_internal", "to_addresses", "cc_addresses",
            "display_to", "subject", "body", "body_top", "has_attachments",
            "attachment_names", "first_seen_at", "last_seen_at")


def is_case_path(folder_path: str) -> bool:
    """Any component of the path is a case folder ('OPP-1287 (PET Flakes)',
    '-0327 (Industrial Fans)') - the spellings case_folders() accepts."""
    return any(outlook_bridge.CASE_FOLDER.search(part)
               for part in (folder_path or "").split("\\") if part)


def _note_location(conn, key: str, record: Record, stamp: str) -> None:
    conn.execute(
        "INSERT INTO message_location(message_id, folder_path, entry_id, store, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(message_id, folder_path) DO UPDATE SET entry_id = excluded.entry_id, "
        "store = excluded.store, last_seen_at = excluded.last_seen_at",
        (key, record.folder_path, record.entry_id, record.store, stamp, stamp))


def primary_location(conn, key: str):
    """The row of message_location that message.folder_path should point
    at: the case folder seen most recently (a mail re-filed from one case
    to another follows the move), else the first-seen place."""
    rows = conn.execute(
        "SELECT rowid, * FROM message_location WHERE message_id = ? "
        "ORDER BY first_seen_at, rowid", (key,)).fetchall()
    cases = [row for row in rows if is_case_path(row["folder_path"])]
    if cases:
        return max(cases, key=lambda row: (row["last_seen_at"], row["rowid"]))
    return rows[0] if rows else None


def _set_primary(conn, message_row_id: int, key: str, stamp: str) -> bool:
    """Point message at its primary location. True when the place changed."""
    primary = primary_location(conn, key)
    if primary is None:
        return False
    before = conn.execute("SELECT folder_path FROM message WHERE id = ?",
                          (message_row_id,)).fetchone()["folder_path"]
    conn.execute(
        "UPDATE message SET folder_path = ?, entry_id = ?, store = ?, "
        "last_seen_at = ? WHERE id = ?",
        (primary["folder_path"], primary["entry_id"], primary["store"], stamp,
         message_row_id))
    return before != primary["folder_path"]


def store(conn, records, now=None) -> tuple:
    """Insert new records, refresh the ones already there. (new, updated).

    An existing row keeps everything it has. Every place the mail is seen
    goes into message_location; message.folder_path / entry_id / store are
    the primary place (primary_location) and last_seen_at moves. Nothing
    is ever deleted here.
    """
    stamp = _stamp(_now(now))
    new = updated = 0
    for record in records:
        column, value = record.key
        if not value:
            continue                         # nothing to key on: not stored
        if column == "message_id":
            row = conn.execute("SELECT id FROM message WHERE message_id = ? AND message_id <> ''",
                               (value,)).fetchone()
        else:
            row = conn.execute("SELECT id FROM message WHERE message_id = '' "
                               "AND entry_id = ?", (value,)).fetchone()
        if row is not None:
            _note_location(conn, value, record, stamp)
            _set_primary(conn, row["id"], value, stamp)
            updated += 1
            continue
        values = {
            "message_id": record.message_id, "entry_id": record.entry_id,
            "store": record.store, "folder_path": record.folder_path,
            "conversation_id": record.conversation_id,
            "conversation_topic": record.conversation_topic,
            "received_at": record.received_at, "sent_at": record.sent_at,
            "sender_name": record.sender_name, "sender_address": record.sender_address,
            "sender_internal": 1 if record.sender_internal else 0,
            "to_addresses": json.dumps(list(record.to_addresses)),
            "cc_addresses": json.dumps(list(record.cc_addresses)),
            "display_to": record.display_to, "subject": record.subject,
            "body": record.body, "body_top": top_part(record.body),
            "has_attachments": 1 if record.has_attachments else 0,
            "attachment_names": json.dumps(list(record.attachment_names)),
            "first_seen_at": stamp, "last_seen_at": stamp,
        }
        conn.execute(
            "INSERT INTO message ({}) VALUES ({})".format(
                ", ".join(_COLUMNS), ", ".join(":" + c for c in _COLUMNS)),
            values)
        _note_location(conn, value, record, stamp)
        new += 1
    return new, updated


def light_record_from_item(item, folder_path: str) -> Record:
    """Only what --relocate needs: the key and where the item is. No body,
    no recipients - a fraction of the cost of record_from_item."""
    _text = outlook_bridge._text
    record = Record(folder_path=folder_path, store=_store_of(folder_path))
    record.entry_id = _text(getattr(item, "EntryID", ""))
    record.message_id = outlook_bridge.message_id_of(item)
    record.received_at = _timestamp_full(getattr(item, "ReceivedTime", None))
    return record


def store_locations(conn, records, now=None) -> tuple:
    """--relocate: record where each already-archived mail was seen and
    re-point its primary place. (added, refreshed, skipped, moved):
    locations new / seen again, mails the archive does not have (left
    alone: nothing to key their content on), primaries that changed."""
    stamp = _stamp(_now(now))
    added = refreshed = skipped = moved = 0
    for record in records:
        column, value = record.key
        if not value:
            continue
        if column == "message_id":
            row = conn.execute("SELECT id FROM message WHERE message_id = ? AND message_id <> ''",
                               (value,)).fetchone()
        else:
            row = conn.execute("SELECT id FROM message WHERE message_id = '' "
                               "AND entry_id = ?", (value,)).fetchone()
        if row is None:
            skipped += 1
            continue
        known = conn.execute(
            "SELECT 1 FROM message_location WHERE message_id = ? AND folder_path = ?",
            (value, record.folder_path)).fetchone()
        _note_location(conn, value, record, stamp)
        if known:
            refreshed += 1
        else:
            added += 1
        if _set_primary(conn, row["id"], value, stamp):
            moved += 1
    return added, refreshed, skipped, moved


def location_stats(conn) -> dict:
    """{'messages': n, 'locations': n, 'multi': mails seen in more than one
    folder, 'max': the most places one mail has}."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(c > 1), 0) AS multi, COALESCE(MAX(c), 0) AS most "
        "FROM (SELECT message_id, COUNT(*) AS c FROM message_location GROUP BY message_id)"
    ).fetchone()
    return {"messages": conn.execute("SELECT COUNT(*) FROM message").fetchone()[0],
            "locations": conn.execute("SELECT COUNT(*) FROM message_location").fetchone()[0],
            "multi": row["multi"], "max": row["most"]}


# ------------------------------------------------------------- the sync --
@dataclass
class FolderResult:
    path: str
    new: int = 0            # mails inserted (or, with --relocate, places added)
    updated: int = 0        # mails seen again (or places refreshed)
    seen: int = 0
    seconds: float = 0.0
    error: str = ""
    skipped: int = 0        # --relocate: mails the archive does not hold
    moved: int = 0          # --relocate: primary places that changed


@dataclass
class SyncReport:
    started_at: str = ""
    finished_at: str = ""
    days_back: object = None
    folders: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    attempts: int = 0
    ok: bool = False
    seconds: float = 0.0
    relocate: bool = False

    @property
    def new(self) -> int:
        return sum(f.new for f in self.folders)

    @property
    def seen(self) -> int:
        return sum(f.seen for f in self.folders)

    @property
    def skipped(self) -> int:
        return sum(f.skipped for f in self.folders)

    @property
    def moved(self) -> int:
        return sum(f.moved for f in self.folders)

    @property
    def updated(self) -> int:
        return sum(f.updated for f in self.folders)

    @property
    def failed(self) -> int:
        return sum(1 for f in self.folders if f.error)

    def summary(self) -> str:
        window = ("all mail" if self.days_back == 0 else
                  "{} days".format(self.days_back) if self.days_back else "incremental")
        if self.relocate:
            return ("mail-sync --relocate {}: {} folder(s), {} mails looked at, "
                    "{} place(s) added, {} refreshed, {} primary moved, {} not in "
                    "the archive, {} failed, {:.0f} s, {}".format(
                        "ok" if self.ok else "FAILED", len(self.folders), self.seen,
                        self.new, self.updated, self.moved, self.skipped, self.failed,
                        self.seconds, window))
        return ("mail-sync {}: {} folder(s), {} mails read, {} new, {} updated, "
                "{} failed, {:.0f} s, {}".format(
                    "ok" if self.ok else "FAILED", len(self.folders), self.seen,
                    self.new, self.updated, self.failed, self.seconds, window))

    def lines(self) -> list:
        out = [self.summary()]
        for folder in self.folders:
            if folder.error:
                out.append("  failed  {}: {}".format(folder.path, folder.error))
        for error in self.errors:
            out.append("  {}".format(error))
        return out


class _OutlookGone(RuntimeError):
    """Folder after folder failed with a COM error: Outlook is not there."""


def _since_for(row, days_back, now) -> tuple:
    """(datetime or None, why). None means the whole folder.

    --days / --full apply to every folder. Otherwise a folder is read from
    OVERLAP_HOURS before its last sync, and a folder never synced from
    FIRST_SYNC_DAYS back. last_received_at is statistics only.
    """
    if days_back is not None:
        if int(days_back) <= 0:
            return None, "all mail"
        return now - dt.timedelta(days=int(days_back)), "{} days".format(days_back)
    if row is not None and row["last_synced_at"]:
        try:
            last = dt.datetime.strptime(row["last_synced_at"], "%Y-%m-%d %H:%M:%S")
            since = last - dt.timedelta(hours=OVERLAP_HOURS)
            return since, "since {}".format(since.strftime("%Y-%m-%d %H:%M"))
        except ValueError:
            pass
    return now - dt.timedelta(days=FIRST_SYNC_DAYS), "first sync, {} days".format(
        FIRST_SYNC_DAYS)


def _restriction(since) -> str:
    if since is None:
        return ""
    return "@SQL=\"urn:schemas:httpmail:datereceived\" >= '{}'".format(
        since.strftime("%Y-%m-%d %H:%M"))


def _iter_items(items):
    """Walk an Items collection with GetFirst/GetNext (cheaper than
    Item(index) on a big folder). Stops at the first unreadable step."""
    try:
        item = items.GetFirst()
    except Exception:
        return
    while item is not None:
        yield item
        try:
            item = items.GetNext()
        except Exception:
            return


def _is_com_error(exc) -> bool:
    return type(exc).__name__ == "com_error" or "RPC server" in str(exc) \
        or "Outlook" in str(exc)


def _sync_folder(conn, info, since, now, limit=None, relocate=False) -> FolderResult:
    """Read one folder's mail since `since` into the archive. Raises on a
    folder that cannot be opened; the caller decides what that means.

    `relocate` reads only keys and places (light) and touches nothing but
    message_location and the primary place; folder_sync is left alone, so
    the next ordinary sync still starts from its own last pass.
    """
    if relocate:
        return _relocate_folder(conn, info, since, now, limit)
    result = FolderResult(path=info.path)
    started = time.monotonic()
    items = info.folder.Items
    restriction = _restriction(since)
    if restriction:
        items = items.Restrict(restriction)
    newest = ""
    batch, consecutive_errors = [], 0
    for item in _iter_items(items):
        try:
            if getattr(item, "Class", None) != outlook_bridge.OL_MAIL_ITEM:
                continue
            record = record_from_item(item, info.path)
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            logs.get("archive").info("cannot read an item in %s: %s", info.path, exc)
            if consecutive_errors >= 10:
                raise
            continue
        result.seen += 1
        if record.received_at > newest:
            newest = record.received_at
        batch.append(record)
        if len(batch) >= 50:
            new, updated = store(conn, batch, now)
            result.new += new
            result.updated += updated
            conn.commit()
            batch = []
        if limit and result.seen >= limit:
            break
    if batch:
        new, updated = store(conn, batch, now)
        result.new += new
        result.updated += updated
    result.seconds = time.monotonic() - started
    row = conn.execute("SELECT last_received_at FROM folder_sync WHERE folder_path = ?",
                       (info.path,)).fetchone()
    previous = (row["last_received_at"] if row is not None else "") or ""
    conn.execute(
        "INSERT INTO folder_sync(folder_path, last_received_at, last_synced_at, "
        "last_count, last_error) VALUES (?, ?, ?, ?, NULL) "
        "ON CONFLICT(folder_path) DO UPDATE SET last_received_at = excluded.last_received_at, "
        "last_synced_at = excluded.last_synced_at, last_count = excluded.last_count, "
        "last_error = NULL",
        (info.path, max(newest, previous) or None, _stamp(_now(now)), result.seen))
    conn.commit()
    return result


def _relocate_folder(conn, info, since, now, limit=None) -> FolderResult:
    result = FolderResult(path=info.path)
    started = time.monotonic()
    items = info.folder.Items
    restriction = _restriction(since)
    if restriction:
        items = items.Restrict(restriction)
    batch, consecutive_errors = [], 0

    def flush(batch):
        added, refreshed, skipped, moved = store_locations(conn, batch, now)
        result.new += added
        result.updated += refreshed
        result.skipped += skipped
        result.moved += moved
        conn.commit()

    for item in _iter_items(items):
        try:
            if getattr(item, "Class", None) != outlook_bridge.OL_MAIL_ITEM:
                continue
            record = light_record_from_item(item, info.path)
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            logs.get("archive").info("cannot read an item in %s: %s", info.path, exc)
            if consecutive_errors >= 10:
                raise
            continue
        result.seen += 1
        batch.append(record)
        if len(batch) >= 100:
            flush(batch)
            batch = []
        if limit and result.seen >= limit:
            break
    if batch:
        flush(batch)
    result.seconds = time.monotonic() - started
    return result


def _wanted(info, patterns) -> bool:
    if not patterns:
        return True
    path = info.path.casefold().lstrip("\\")
    return any(fnmatch.fnmatchcase(path, str(p).casefold().lstrip("\\"))
               for p in patterns)


def _sync_once(conn, report, days_back, patterns, progress, now, done, relocate=False):
    """One connected pass over the folders. Raises _OutlookGone when the
    connection dies part way, OutlookError when it never came up."""
    with outlook_bridge.Search() as search:
        infos = [info for info in search.folders() if _wanted(info, patterns)]
        if not infos:
            raise OutlookError("Outlook listed no mail folders.",
                               "folders() came back empty")
        for position, info in enumerate(infos):
            conn.execute(
                "INSERT INTO folder_sync(folder_path, position) VALUES (?, ?) "
                "ON CONFLICT(folder_path) DO UPDATE SET position = excluded.position",
                (info.path, position))
        conn.commit()
        consecutive = 0
        for info in infos:
            if info.path in done:
                continue
            row = conn.execute("SELECT * FROM folder_sync WHERE folder_path = ?",
                               (info.path,)).fetchone()
            since, why = _since_for(row, days_back, now)
            try:
                result = _sync_folder(conn, info, since, now, relocate=relocate)
                consecutive = 0
            except Exception as exc:
                detail = str(exc)
                result = FolderResult(path=info.path, error=detail)
                conn.execute(
                    "UPDATE folder_sync SET last_error = ?, last_synced_at = ? "
                    "WHERE folder_path = ?", (detail, _stamp(_now(now)), info.path))
                conn.commit()
                logs.get("archive").warning("folder %s failed: %s", info.path, detail)
                if _is_com_error(exc):
                    consecutive += 1
                    if consecutive >= GONE_AFTER:
                        _note(report, result)
                        raise _OutlookGone(detail)
            _note(report, result)
            if not result.error:
                done.add(info.path)          # a failed folder is tried again
            if progress:
                progress(result, why)


def _note(report, result) -> None:
    """Record a folder's result, replacing an earlier attempt's."""
    report.folders[:] = [f for f in report.folders if f.path != result.path]
    report.folders.append(result)


def sync(days_back=None, folders=None, progress=None, db_path=None, now=None,
         retries: int = RETRIES, retry_wait: float = RETRY_WAIT,
         relocate: bool = False) -> SyncReport:
    """Bring the archive up to date from Outlook.

    days_back None: incremental (each folder from a day before the newest
    mail it has; a folder never seen gets FIRST_SYNC_DAYS). An integer:
    that many days for every folder; 0: everything.

    Outlook not answering is retried `retries` times, `retry_wait` seconds
    apart. The report says whether it succeeded; the CLI turns that into
    the exit code and sync-log.txt gets a line either way.
    """
    started = time.monotonic()
    now = _now(now)
    if relocate and days_back is None:
        days_back = RELOCATE_DAYS
    report = SyncReport(started_at=_stamp(now), days_back=days_back, relocate=relocate)
    conn = open_archive(db_path)
    done = set()
    try:
        for attempt in range(1, max(1, int(retries)) + 1):
            report.attempts = attempt
            try:
                _sync_once(conn, report, days_back, folders, progress, now, done,
                           relocate=relocate)
                report.ok = True
                break
            except (OutlookError, _OutlookGone) as exc:
                message = getattr(exc, "message", None) or str(exc)
                detail = getattr(exc, "detail", "")
                report.errors.append("attempt {} of {}: {}{}".format(
                    attempt, retries, message, " ({})".format(detail) if detail else ""))
                logs.get("archive").warning("sync attempt %s failed: %s %s",
                                            attempt, message, detail)
                if isinstance(exc, outlook_bridge.OutlookUnavailable):
                    break                   # no pywin32: waiting will not bring it
                if attempt < retries:
                    time.sleep(retry_wait)
        report.seconds = time.monotonic() - started
        report.finished_at = _stamp(_now())
        if report.ok and not relocate and not folders:
            # A partial sync (--folders) does not count as "the archive is
            # up to date": Tracking and propose-status go by last_sync_at.
            set_meta(conn, "last_sync_at", report.finished_at)
        if relocate:
            set_meta(conn, "last_relocate_at", report.finished_at)
            set_meta(conn, "last_relocate_summary", report.summary())
        else:
            set_meta(conn, "last_sync_summary", report.summary())
        conn.commit()
    finally:
        conn.close()
    _append_sync_log(report)
    return report


def _append_sync_log(report, path=None) -> None:
    if not (path or SYNC_LOG):
        return
    target = Path(path or SYNC_LOG)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write("{}  {}\n".format(report.finished_at or report.started_at,
                                           report.summary()))
            for line in report.lines()[1:]:
                handle.write("{}  {}\n".format(" " * 19, line.strip()))
    except OSError as exc:                      # a log we cannot write is not a failure
        logs.get("archive").warning("cannot write %s: %s", target, exc)


# ------------------------------------------------------------ searching --
def _mail_from_row(row) -> MailInfo:
    received = row["received_at"] or ""
    info = MailInfo(
        subject=row["subject"] or "",
        received_on=received[:10],
        received_at=received[:16],
        entry_id=row["entry_id"] or "",
        conversation_id=row["conversation_id"] or "",
        conversation_topic=row["conversation_topic"] or "",
        sender_name=row["sender_name"] or "",
        sender_address=row["sender_address"] or "",
        sender_internal=bool(row["sender_internal"]),
        to_addresses=json.loads(row["to_addresses"] or "[]"),
        cc_addresses=json.loads(row["cc_addresses"] or "[]"),
        # the archive keeps the To line as Outlook showed it, names only;
        # the Cc names are not stored (2026-09-25)
        to_names=[part.strip() for part in (row["display_to"] or "").split(";") if part.strip()],
        cc_names=[],
        body_excerpt=row["body"] or "",
        folder_path=row["folder_path"] or "",
        display_to=row["display_to"] or "",
        light=False,
        message_id=row["message_id"] or "",
        sent_at=(row["sent_at"] or received)[:16],
    )
    # the case page's Mail card reads these two (2026-09-25)
    try:
        info.attachment_names = json.loads(row["attachment_names"] or "[]")
    except (KeyError, IndexError, ValueError, TypeError):
        info.attachment_names = []
    try:
        info.body_top = row["body_top"] or ""
    except (KeyError, IndexError):
        info.body_top = ""
    return info


_SELECT = "SELECT * FROM message"


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


class ArchiveSearch(outlook_bridge.FolderIndex):
    """outlook_bridge.Search, answered from the archive.

    Same methods, same MailInfo, bodies already there (hydrate is a no-op)
    and no time budget (cut_short stays False). propose.propose() and
    suggestions.from_mail() take either.
    """

    def __init__(self, db_path=None):
        self._path = db_path
        self._conn = None
        self._folders = None
        self._cache = {}
        self.cut_short = False

    def __enter__(self):
        self._conn = open_archive(self._path, create=False)
        return self

    def __exit__(self, *exc):
        if self._conn is not None:
            self._conn.close()
        self._conn = None
        self._folders = None
        return False

    # ----------------------------------------------------------- folders --
    def folders(self, mail_only: bool = True) -> list:
        if self._folders is None:
            rows = self._conn.execute(
                "SELECT folder_path, position FROM folder_sync "
                "UNION SELECT DISTINCT folder_path, 100000 FROM message_location "
                "WHERE folder_path NOT IN (SELECT folder_path FROM folder_sync) "
                "ORDER BY 2, 1").fetchall()
            self._folders = [FolderInfo(row["folder_path"], None, True) for row in rows
                             if row["folder_path"]]
        return list(self._folders)

    # ------------------------------------------------------------- items --
    def _rows(self, where: str, params, order="received_at", limit=None) -> list:
        sql = "{} WHERE {} ORDER BY {}".format(_SELECT, where, order)
        if limit:
            sql += " LIMIT {}".format(int(limit))
        return [_mail_from_row(row) for row in self._conn.execute(sql, params)]

    def hydrate(self, mails) -> list:
        return list(mails)

    def in_folder(self, info, limit=200) -> list:
        """Every mail with a copy in this folder, whichever folder is its
        primary one; entry_id and folder_path are this copy's."""
        sql = ("SELECT m.*, l.entry_id AS here_entry_id FROM message_location l "
               "JOIN message m ON m.id = (SELECT id FROM message WHERE "
               "  (message_id = l.message_id) OR (message_id = '' AND entry_id = l.message_id) "
               "  LIMIT 1) "
               "WHERE l.folder_path = ? ORDER BY m.received_at")
        if limit:
            sql += " LIMIT {}".format(int(limit))
        out = []
        for row in self._conn.execute(sql, (info.path,)):
            mail = _mail_from_row(row)
            mail.folder_path = info.path
            mail.entry_id = row["here_entry_id"] or mail.entry_id
            out.append(mail)
        return out

    def by_subject(self, term: str, limit=200, hints=(), opp_no="") -> list:
        # The term is a bound parameter, so an apostrophe in it is fine: the
        # old guard against it sent OPP-1407's subject ("... 40' EXW / ...")
        # away empty, and its follow-up opened with 'Hi,' (2026-09-25).
        term = (term or "").strip()
        if not term:
            return []
        key = ("subject", term.casefold())
        if key in self._cache:
            return self._cache[key]
        wanted = term.casefold()
        out = [m for m in self._subject_rows(term) if wanted in m.subject.casefold()][:limit]
        self._cache[key] = out
        return out

    #: SQLite's parameter limit is 999 on old builds
    ID_CHUNK = 500

    def _subject_rows(self, term: str) -> list:
        """F2c: the mails whose subject is LIKE the term, oldest first (then
        by id): the ids first, from the covering index ix_message_subject,
        then the mails by id - not a scan of the table, bodies and all."""
        ids = [r[0] for r in self._conn.execute(
            "SELECT id FROM message WHERE subject LIKE ? ESCAPE '\\'", (_like(term),))]
        rows = []
        for start in range(0, len(ids), self.ID_CHUNK):
            chunk = ids[start:start + self.ID_CHUNK]
            rows += self._conn.execute("{} WHERE id IN ({})".format(_SELECT, ",".join("?" for _ in chunk)),
                                       chunk).fetchall()
        rows.sort(key=lambda r: (r["received_at"] or "", r["id"]))
        return [_mail_from_row(r) for r in rows]

    def by_domains(self, domains, start=None, end=None, limit=2000,
                   hints=(), opp_no="") -> list:
        wanted = sorted({str(d).strip().lower().lstrip("@") for d in domains
                         if d and "'" not in str(d)})
        if not wanted:
            return []
        key = ("domains",) + tuple(wanted) + (start or "", end or "")
        if key in self._cache:
            return self._cache[key]
        clauses, params = [], []
        for domain in wanted:
            for column in ("sender_address", "to_addresses", "cc_addresses", "display_to"):
                clauses.append("{} LIKE ? ESCAPE '\\'".format(column))
                params.append(_like(domain))
        where = "(" + " OR ".join(clauses) + ")"
        if start and outlook_bridge._DATE_ONLY.match(str(start)):
            where += " AND received_at >= ?"
            params.append(start + " 00:00:00")
        if end and outlook_bridge._DATE_ONLY.match(str(end)):
            where += " AND received_at <= ?"
            params.append(end + " 23:59:59")
        out = self._rows(where, params, limit=limit)
        for mail in out:
            mail.header_match = True
        self._cache[key] = out
        return out

    def _same_mail(self, a, b) -> bool:
        if a.entry_id and a.entry_id == b.entry_id:
            return True
        if getattr(a, "message_id", "") and a.message_id == getattr(b, "message_id", ""):
            return True
        return bool(a.received_at) and a.received_at[:16] == (b.received_at or "")[:16] \
            and a.subject == b.subject and a.sender_address == b.sender_address

    def conversation_of(self, mail, limit=100) -> list:
        """The mails in this mail's conversation, wherever they are filed:
        by conversation id, or by topic within TOPIC_WINDOW_DAYS when the
        mail has no id. The mail itself is in the list exactly once."""
        key = ("conv", mail.conversation_id, mail.entry_id or id(mail))
        if key in self._cache:
            return self._cache[key]
        found = []
        if mail.conversation_id:
            found = self._rows("conversation_id = ?", (mail.conversation_id,), limit=limit)
        if not found and (mail.conversation_topic or mail.subject):
            topic = (mail.conversation_topic or parsing.clean_subject(mail.subject)
                     or mail.subject).strip()
            anchor = (mail.received_at or mail.received_on or "")[:10]
            if topic and anchor:
                try:
                    day = dt.date.fromisoformat(anchor)
                    low = (day - dt.timedelta(days=TOPIC_WINDOW_DAYS)).isoformat()
                    high = (day + dt.timedelta(days=TOPIC_WINDOW_DAYS)).isoformat()
                    found = [m for m in self._rows(
                        "conversation_topic <> '' AND conversation_topic = ? COLLATE NOCASE "
                        "AND received_at >= ? AND received_at <= ?",
                        (topic, low + " 00:00:00", high + " 23:59:59"), limit=limit)]
                except ValueError:
                    found = []
        out = [m for m in found if not self._same_mail(m, mail)]
        out.append(mail)
        out.sort(key=lambda m: (m.received_at or m.received_on or ""))
        self._cache[key] = out
        return out

    def by_entry_id(self, entry_id: str):
        """The archived mail with this Outlook entry id, or None."""
        wanted = (entry_id or "").strip()
        if not wanted:
            return None
        found = self._rows("entry_id = ?", (wanted,), limit=1)
        if not found:
            row = self._conn.execute(
                "SELECT m.* FROM message_location l JOIN message m ON m.message_id = l.message_id "
                "AND m.message_id <> '' WHERE l.entry_id = ? LIMIT 1", (wanted,)).fetchone()
            found = [_mail_from_row(row)] if row is not None else []
        return found[0] if found else None

    def entry_ids_of(self, mail) -> set:
        """Every Outlook entry id this mail has had (one per folder it was
        seen in): a case remembers the one it was registered from, and the
        mail may have been filed since (AL1)."""
        out = {e for e in (getattr(mail, "entry_id", "") or "",) if e}
        mid = (getattr(mail, "message_id", "") or "").strip()
        if mid:
            out.update(r[0] for r in self._conn.execute(
                "SELECT entry_id FROM message_location WHERE message_id = ? AND entry_id <> ''", (mid,)))
            row = self._conn.execute("SELECT entry_id FROM message WHERE message_id = ? AND message_id <> ''",
                                     (mid,)).fetchone()
            if row is not None and row[0]:
                out.add(row[0])
        return out

    # ------------------------------------------------- where mails live --
    def folders_seen(self, message_ids) -> list:
        """The folders these mails were seen in, most recently seen first."""
        ids = [m for m in dict.fromkeys(message_ids or ()) if m]
        out = []
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            out += [tuple(r) for r in self._conn.execute(
                "SELECT folder_path, MAX(last_seen_at) FROM message_location WHERE message_id IN ({}) "
                "AND folder_path <> '' GROUP BY folder_path".format(",".join("?" for _ in chunk)), chunk)]
        return [path for path, _ in sorted(out, key=lambda r: r[1] or "", reverse=True)]

    def folders_of_senders(self, domains, addresses, since: str) -> list:
        """The folders mails from these domains or addresses received since
        `since` were seen in, most recently seen first."""
        terms, params = [], [since]
        for address in sorted({a.strip().lower() for a in addresses or () if a and a.strip()}):
            terms.append("lower(m.sender_address) = ?")
            params.append(address)
        for domain in sorted({d.strip().lower() for d in domains or () if d and d.strip()}):
            terms.append("lower(m.sender_address) LIKE ?")
            params.append("%@" + domain)
        if not terms:
            return []
        rows = self._conn.execute(
            "SELECT l.folder_path, MAX(l.last_seen_at) AS seen FROM message m JOIN message_location l "
            "ON l.message_id = m.message_id WHERE m.received_at >= ? AND ({}) AND l.folder_path <> '' "
            "GROUP BY l.folder_path ORDER BY seen DESC".format(" OR ".join(terms)), params).fetchall()
        return [r[0] for r in rows]

    def standard_folders(self) -> list:
        """Every Inbox, then every Sent Items, in the archive's folder order."""
        names = [info.path for info in self.folders()]
        return [p for p in names if p.rsplit("\\", 1)[-1].strip().lower() == "inbox"] + \
            [p for p in names if p.rsplit("\\", 1)[-1].strip().lower() == "sent items"]

    def own_addresses(self) -> set:
        """W3: the mailbox's own addresses - the stores named as one
        ('IrisChen@jojofreight.example'), lower case."""
        if "own" not in self._cache:
            self._cache["own"] = {r[0] for r in self._conn.execute(
                "SELECT DISTINCT lower(store) FROM message WHERE store LIKE '%@%'") if r[0]}
        return set(self._cache["own"])

    def by_message_id(self, message_id: str):
        """The archived mail with this Message-ID header, or None."""
        wanted = (message_id or "").strip()
        if not wanted:
            return None
        found = self._rows("message_id = ? AND message_id <> ''", (wanted,), limit=1)
        return found[0] if found else None

    def by_conversation(self, conversation_id: str, limit=200) -> list:
        wanted = (conversation_id or "").strip()
        if not wanted:
            return []
        return self._rows("conversation_id = ?", (wanted,), limit=limit)

    def later_in_conversation(self, mail) -> list:
        """Archived mails in `mail`'s conversation other than itself."""
        return [m for m in self.conversation_of(mail) if m is not mail]


# --------------------------------------------------------- keyword search --
#: Search-Mail.ps1, line for line: which lines of a mail are worth a glance
#: (dates, pieces, ETA, LFD, pickup words) and which are signature noise.
_KEY_LINE = re.compile(
    r"\b(ETD|ETA|ATD|ATA|LFD|depart|departed|arriv|flight|FLT|deliver|delivered|"
    r"delivery|pick ?up|picked|on ?hand|received|recover|recovered|customs|clear|"
    r"cleared|releas|D/O|delivery order|MAWB|HAWB|AWB|pcs?|pieces?|kgs?|kg|ctns?|"
    r"cartons?|skids?|plts?|pallets?|transit|schedule|scheduled|booking|booked|"
    r"confirm|pending|hold|exam|CBP|ISF|entry|manifest|pre-?alert|POD|delay|"
    r"delayed|cancel|transfer|CFS|warehouse|truck|uplift|on ?board|dest|"
    r"destination|origin|ready|short|missing|missed|damage|recount|re-count)",
    re.IGNORECASE)
_NOISE_LINE = re.compile(
    r"confidential|privileged|addressee|permanently destroy|unsubscribe|"
    r"Standard Trading Conditions|^Tel|^Fax|^Cell|^Mobile|^Website|www\.|^http|"
    r"^Sent from my", re.IGNORECASE)
_SLASH_DATE = re.compile(r"\d{1,2}/\d{1,2}")
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def is_awb(term: str) -> bool:
    """An air waybill: 3 + 8 digits, with or without the dash and spaces."""
    digits = re.sub(r"[^0-9]", "", term or "")
    return len(digits) == 11 and re.sub(r"[0-9\-\s]", "", term or "") == ""


def term_regex(term: str):
    """The regex that confirms a hit and says which term a mail belongs to
    (Get-TermRegex): loose for an AWB, whole word for a short term."""
    if is_awb(term):
        digits = re.sub(r"[^0-9]", "", term)
        a, b, c = digits[:3], digits[3:7], digits[7:]
        return re.compile(r"(?<![0-9]){}[-\s]*{}[-\s]*{}(?![0-9])".format(a, b, c),
                          re.IGNORECASE)
    if len(term) <= 4:
        return re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
    return re.compile(re.escape(term), re.IGNORECASE)


def fts_query(term: str) -> str:
    """The FTS5 MATCH expression that finds every candidate for a term.

    An AWB's spellings tokenise three ways ('001 48213375', '001 4821 3375',
    '00148213375'); the regex above then confirms. Any other term is its
    words as a phrase, the last one as a prefix, so 'Zenith Cargo' also
    finds 'Zenith Cargos'. '' when the term has no searchable token.
    """
    if is_awb(term):
        digits = re.sub(r"[^0-9]", "", term)
        a, b, c = digits[:3], digits[3:7], digits[7:]
        phrases = {"{} {}{}".format(a, b, c), "{} {} {}".format(a, b, c),
                   "{}{}{}".format(a, b, c), "{}{} {}".format(a, b, c)}
        return " OR ".join('"{}"'.format(p) for p in sorted(phrases))
    tokens = _TOKEN.findall(term or "")
    if not tokens:
        return ""
    phrase = '"{}"'.format(" ".join(tokens))
    return phrase if len(term) <= 4 else phrase + "*"


def key_lines(top: str, limit: int = 25) -> list:
    out = []
    for line in (top or "").splitlines():
        text = " ".join(line.split())
        if not text or len(text) < 4 or _NOISE_LINE.search(text):
            continue
        if _KEY_LINE.search(text) or _SLASH_DATE.search(text):
            out.append(text)
            if len(out) >= limit:
                break
    return out


def top_for_term(top: str, regex, max_chars: int) -> str:
    """The whole new part when short; otherwise the opening plus a window
    around the term (Get-TopForTerm)."""
    if not top:
        return ""
    if len(top) <= max_chars:
        return top
    found = regex.search(top)
    if not found:
        return top[:max_chars] + " ...[cut]"
    start = max(0, found.start() - 700)
    length = min(len(top) - start, max_chars)
    if start <= 500:
        return top[:start + length] + " ...[cut]"
    return top[:500] + "\n...[cut]...\n" + top[start:start + length] + " ...[cut]"


@dataclass
class Hit:
    term: str
    date: str
    folder: str
    sender_name: str
    sender_email: str
    to: str
    subject: str
    matched_in: str
    key_lines: str
    top: str


@dataclass
class FullRecord:
    terms: str
    date: str
    folder: str
    sender: str
    to: str
    subject: str
    folder_path: str
    body: str


@dataclass
class SearchResult:
    terms: list
    days_back: int
    hits: list = field(default_factory=list)
    fulls: list = field(default_factory=list)
    candidates: int = 0
    folders: int = 0
    seconds: float = 0.0
    log: list = field(default_factory=list)

    def by_term(self, term) -> list:
        return sorted((h for h in self.hits if h.term == term), key=lambda h: h.date)

    def summary_lines(self) -> list:
        out = []
        for term in self.terms:
            rows = self.by_term(term)
            span = "{} .. {}".format(rows[0].date, rows[-1].date) if rows else ""
            out.append("{:<16} {:>4} mail(s)  {}".format(term, len(rows), span))
        return out


def _display_folder(path: str) -> str:
    return (path or "").lstrip("\\")


def search_terms(conn, terms, days_back: int = 60, now=None, max_body_chars: int = 2000,
                 full_body: bool = False, max_full_chars: int = 60000,
                 folders=None) -> SearchResult:
    """Search-Mail.ps1 over the archive: every mail whose subject or body
    carries one of the terms, one Hit per (term, mail)."""
    started = time.monotonic()
    terms = [t.strip() for t in terms if t and t.strip()]
    seen_terms, ordered = set(), []
    for term in terms:
        if term not in seen_terms:
            seen_terms.add(term)
            ordered.append(term)
    result = SearchResult(terms=ordered, days_back=int(days_back))
    regexes = {term: term_regex(term) for term in ordered}
    cutoff = ""
    if int(days_back) > 0:
        cutoff = _stamp(_now(now) - dt.timedelta(days=int(days_back)))

    def log(text):
        result.log.append("{}  {}".format(dt.datetime.now().strftime("%H:%M:%S"), text))

    log("mail-search starting. Terms={}  DaysBack={}  FullBody={}  Folders={}".format(
        len(ordered), days_back, bool(full_body), " | ".join(folders or ["*"])))
    for term in ordered:
        log("  term: {}".format(term))
    result.folders = conn.execute(
        "SELECT COUNT(DISTINCT folder_path) FROM message_location").fetchone()[0]

    candidate_ids = set()
    for term in ordered:
        query = fts_query(term)
        params = []
        if query:
            sql = ("SELECT m.id FROM message_fts f JOIN message m ON m.id = f.rowid "
                   "WHERE message_fts MATCH ?")
            params.append(query)
        else:
            sql = ("SELECT id FROM message m WHERE (subject LIKE ? ESCAPE '\\' "
                   "OR body LIKE ? ESCAPE '\\')")
            params.extend([_like(term), _like(term)])
        if cutoff:
            sql += " AND m.received_at >= ?"
            params.append(cutoff)
        try:
            candidate_ids.update(row[0] for row in conn.execute(sql, params))
        except sqlite3.OperationalError as exc:
            log("  query for {!r} failed ({}); scanning instead".format(term, exc))
            sql = ("SELECT id FROM message m WHERE (subject LIKE ? ESCAPE '\\' "
                   "OR body LIKE ? ESCAPE '\\')")
            params = [_like(term), _like(term)]
            if cutoff:
                sql += " AND m.received_at >= ?"
                params.append(cutoff)
            candidate_ids.update(row[0] for row in conn.execute(sql, params))

    per_folder = {}
    for message_id in sorted(candidate_ids):
        row = conn.execute("SELECT * FROM message WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            continue
        if folders:
            key = row["message_id"] or row["entry_id"]
            places = [r[0] for r in conn.execute(
                "SELECT folder_path FROM message_location WHERE message_id = ?", (key,))]
            if not any(_wanted(FolderInfo(path), folders) for path in places or [row["folder_path"]]):
                continue
        text = (row["subject"] or "") + "\n" + (row["body"] or "")
        matched = [term for term in ordered if regexes[term].search(text)]
        if not matched:
            continue
        result.candidates += 1
        per_folder[row["folder_path"]] = per_folder.get(row["folder_path"], 0) + 1
        received = row["received_at"] or row["sent_at"] or ""
        date = received[:16]
        top = row["body_top"] or top_part(row["body"] or "")
        keys = " | ".join(key_lines(top))
        sender = "{} <{}>".format(row["sender_name"] or "", row["sender_address"] or "")
        folder = _display_folder(row["folder_path"])
        to_line = row["display_to"] or ""
        if full_body:
            body = row["body"] or ""
            if len(body) > max_full_chars:
                body = body[:max_full_chars] + " ...[cut]"
            result.fulls.append(FullRecord(
                terms="; ".join(matched), date=date, folder=folder, sender=sender,
                to=to_line, subject=row["subject"] or "", folder_path=folder, body=body))
        for term in matched:
            where = "subject" if regexes[term].search(row["subject"] or "") else "body"
            result.hits.append(Hit(
                term=term, date=date, folder=folder,
                sender_name=row["sender_name"] or "", sender_email=row["sender_address"] or "",
                to=to_line, subject=row["subject"] or "", matched_in=where,
                key_lines=keys, top=top_for_term(top, regexes[term], max_body_chars)))
    for path, count in sorted(per_folder.items()):
        log("  {}: {} mail(s)".format(_display_folder(path), count))
    result.seconds = time.monotonic() - started
    log("Folders: {}   candidate mails: {}   hits: {}   ({:.1f} s)".format(
        result.folders, result.candidates, len(result.hits), result.seconds))
    log("")
    log("--- SUMMARY ---")
    for line in result.summary_lines():
        log(line)
    log("---------------")
    return result


_HITS_HEADER = ("Term", "Date", "Folder", "SenderName", "SenderEmail", "To",
                "Subject", "MatchedIn", "KeyLines")
_RULE = "-" * 80
_BAR = "#" * 80
_EQ = "=" * 80


def _safe_tag(tag: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]", "-", tag or "search") or "search"


def write_search(result: SearchResult, tag: str = "search", out_dir=None,
                 full_body: bool = False, now=None) -> dict:
    """The files Search-Mail.ps1 writes, byte for byte in shape: UTF-8 with
    BOM, CRLF, every CSV field quoted. {'hits': path, 'bodies': path,
    'full': path or None, 'log': path}."""
    target = Path(out_dir or SEARCH_DIR)
    target.mkdir(parents=True, exist_ok=True)
    safe = _safe_tag(tag)
    hits_path = target / "{}-hits.csv".format(safe)
    bodies_path = target / "{}-bodies.txt".format(safe)
    full_path = target / "{}-full.txt".format(safe)
    log_path = target / "{}-log.txt".format(safe)
    stamp = _now(now).strftime("%Y-%m-%d %H:%M")

    with hits_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        writer.writerow(_HITS_HEADER)
        for hit in sorted(result.hits, key=lambda h: (h.term.lower(), h.date)):
            writer.writerow([hit.term, hit.date, hit.folder, hit.sender_name,
                             hit.sender_email, hit.to, hit.subject, hit.matched_in,
                             hit.key_lines])

    lines = ["Search-Mail  {}  terms={}  days={}  hits={}".format(
        stamp, len(result.terms), result.days_back, len(result.hits)), ""]
    for term in result.terms:
        group = result.by_term(term)
        lines += [_BAR, "TERM: {}   ({} mail(s))".format(term, len(group)), _BAR]
        if not group:
            lines += ["  (no mail found)", ""]
            continue
        for hit in group:
            lines += [_RULE,
                      "DATE    : {}".format(hit.date),
                      "FROM    : {} <{}>".format(hit.sender_name, hit.sender_email),
                      "TO      : {}".format(hit.to),
                      "SUBJECT : {}".format(hit.subject),
                      "FOLDER  : {}".format(hit.folder),
                      "", hit.top, ""]
        lines.append("")
    _write_text(bodies_path, lines)

    written = {"hits": hits_path, "bodies": bodies_path, "full": None, "log": log_path}
    if full_body:
        lines = ["Search-Mail full bodies  {}  mails={}".format(stamp, len(result.fulls))]
        for record in sorted(result.fulls, key=lambda r: r.date):
            lines += [_EQ,
                      "TERMS   : {}".format(record.terms),
                      "DATE    : {}".format(record.date),
                      "FROM    : {}".format(record.sender),
                      "TO      : {}".format(record.to),
                      "SUBJECT : {}".format(record.subject),
                      "FOLDER  : {}".format(record.folder),
                      _RULE, record.body, ""]
        _write_text(full_path, lines)
        written["full"] = full_path
    _write_text(log_path, list(result.log) + [
        "Files written to: {}".format(target), "  {}".format(hits_path),
        "  {}".format(bodies_path)] + (["  {}".format(full_path)] if full_body else []))
    return written


def _write_text(path: Path, lines) -> None:
    text = "\r\n".join(line.replace("\r\n", "\n").replace("\n", "\r\n") for line in lines)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        handle.write(text + "\r\n")
