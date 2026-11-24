"""The snapshot the offline team site (site/) runs on.

The team site reads documents that OppList pushes: the lists, one document
per case, the month report. This module makes those documents for the demo
database with the same code that pushes them (firebase_sync.sync_once) into
a local store in a temporary folder, adds the few documents that only the
site itself writes (site_snapshot_extras), and saves everything as one JSON
file:

    {"made": "<seed>-<date>", "now": "<date>T09:30:00", "docs": {path: fields}}

A time is {"__time": "2026-11-24T09:25:00.000"}: the wall clock of the demo
day with no zone, which the site reads in the visitor's own zone so that
"today" is the demo's day everywhere. Same seed and date, same bytes.

The push runs on a copy of the database: taking in and expiring are part of
a sync, and the demo database itself must stay as generate() made it.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import tempfile
from pathlib import Path

from . import db

SNAPSHOT_NAME = "snapshot.json"
FOLDER = "site"
#: the push happens at 09:25 of the demo day and the page opens at 09:30
PUSH_AT = dt.time(9, 25)
OPEN_AT = dt.time(9, 30)


def _wall(value: dt.datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}".format(value.microsecond // 1000)


def encode(value):
    """A document's Python values as the snapshot keeps them."""
    if isinstance(value, dt.datetime):
        return {"__time": _wall(value)}
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return value


def _store_docs(folder: Path) -> dict:
    """Every document of a local store folder: {path: fields as Python values}."""
    from .firebase_sync import from_fields
    import urllib.parse
    out = {}
    for file in sorted(folder.rglob("*.json")):
        parts = [urllib.parse.unquote(p) for p in file.relative_to(folder).with_suffix("").parts]
        raw = json.loads(file.read_text(encoding="utf-8"))
        out["/".join(parts)] = from_fields(raw.get("fields") or {})
    return out


def pushed_docs(db_path, archive_path, today: dt.date) -> dict:
    """What OppList pushes for this database on `today`, by the push code
    itself. {path: fields}; times are datetimes."""
    from . import firebase_sync, mail_archive
    from .local_store import LocalStore
    moment = dt.datetime.combine(today, PUSH_AT, tzinfo=dt.timezone.utc)
    with tempfile.TemporaryDirectory(prefix="opplist-site-") as tmp:
        tmp = Path(tmp)
        copy = tmp / "opplist.db"
        shutil.copyfile(db_path, copy)
        store = LocalStore(firebase_sync.DEFAULT_PROJECT, tmp / "store", clock=lambda: moment)

        # the archive is read from a copy too: opening it must leave no file beside the demo's own
        archive_copy = None
        if archive_path is not None and Path(archive_path).exists():
            archive_copy = tmp / "mail.db"
            shutil.copyfile(archive_path, archive_copy)

        def search():
            if archive_copy is None:
                return None
            found = mail_archive.ArchiveSearch(archive_copy)
            found.__enter__()
            return found

        conn = db.open_db(copy)
        kept = firebase_sync._archive_search
        firebase_sync._archive_search = search
        try:
            result = firebase_sync.sync_once(
                conn, store, {}, today=today.isoformat(), reports_dir=tmp / "reports",
                record=tmp / "report-exports.json", now=moment.replace(tzinfo=None), by_app=True)
        finally:
            firebase_sync._archive_search = kept
            conn.close()
        if not result.ok:
            raise RuntimeError("the demo push did not go through: {}".format(result.reason or "no reason given"))
        docs = _store_docs(tmp / "store")
    # Read quote stamps a reading with the computer's local time, which the
    # push turns into UTC: the one value here that would differ from one time
    # zone to the next. The demo's reading happened at the push.
    for path, doc in docs.items():
        if path.startswith("rateDrafts/") and "readAt" in doc:
            doc["readAt"] = moment
    return docs


def build(db_path, archive_path, today: dt.date, seed: int) -> dict:
    """The whole snapshot as a dict (see the module's text)."""
    from . import site_snapshot_extras
    docs = pushed_docs(db_path, archive_path, today)
    docs.update(site_snapshot_extras.docs(docs, today, seed, dt.datetime.combine(today, PUSH_AT)))
    return {
        "made": "{}-{}".format(seed, today.isoformat()),
        "now": dt.datetime.combine(today, OPEN_AT).strftime("%Y-%m-%dT%H:%M:%S"),
        "docs": dict(sorted(dict({path: encode(fields) for path, fields in docs.items()}, **site_docs(db_path)).items())),
    }


def count(file) -> int:
    """How many documents a snapshot file holds."""
    return len(json.loads(Path(file).read_text(encoding="utf-8")).get("docs") or {})


SITE_DOCS_NAME = "site-docs.json"


def write_site_docs(out_dir, docs: dict) -> Path:
    """<out_dir>/site/site-docs.json: what people did on the site in the demo
    world (demo_showcase), the documents as the site wrote them. The
    snapshot adds them to what OppList pushes."""
    file = Path(out_dir) / FOLDER / SITE_DOCS_NAME
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps({p: encode(d) for p, d in sorted(docs.items())}, ensure_ascii=False, sort_keys=True,
                               indent=1) + "\n", encoding="utf-8", newline="\n")
    return file


def site_docs(db_path) -> dict:
    """The documents write_site_docs kept beside this demo database, still
    in the snapshot's form ({} when there are none)."""
    file = Path(db_path).parent / FOLDER / SITE_DOCS_NAME
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write(out_dir, db_path, archive_path, today: dt.date, seed: int) -> Path:
    """<out_dir>/site/snapshot.json. Returns the file."""
    snapshot = build(db_path, archive_path, today, seed)
    file = Path(out_dir) / FOLDER / SNAPSHOT_NAME
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_name(file.name + ".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                   encoding="utf-8", newline="\n")
    tmp.replace(file)
    return file
