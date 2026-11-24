"""The document store of this edition: a folder of JSON files.

The team site and the Rate Card are document stores in the cloud. This
edition talks to neither: everything that builds the documents, takes in
what people did on the site and guards the pushes is the same code, and the
one object it calls - commit, get, run_query - is this class, which keeps
each document as one JSON file under the data folder. Nothing here opens a
network connection.

A document 'cases/OPP-7101' is the file <folder>/cases/OPP-7101.json:

    {"fields": {<name>: <typed value>, ...}, "updateTime": "<UTC time>"}

The values are stored typed, as the document stores of that kind do it
({"stringValue": ...}, {"timestampValue": ...}), so a time comes back as a
time and an integer as an integer.

run_query answers what the code asks: one root collection, `where` with a
field compared to a value (or several joined by AND), `orderBy`, `limit`
and `select`. Anything else is refused with a sentence, never guessed.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.parse
from pathlib import Path

_OPS = {
    "EQUAL": lambda a, b: a == b,
    "NOT_EQUAL": lambda a, b: a != b,
    "LESS_THAN": lambda a, b: a < b,
    "LESS_THAN_OR_EQUAL": lambda a, b: a <= b,
    "GREATER_THAN": lambda a, b: a > b,
    "GREATER_THAN_OR_EQUAL": lambda a, b: a >= b,
}


class StoreError(Exception):
    """The folder cannot be read or written, or a query asks for something
    this store does not answer."""


def _segment(part: str) -> str:
    """One part of a document path as a file name any system takes."""
    return urllib.parse.quote(part, safe="-_.()@ ")


def _stamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class LocalStore:
    """commit, get and run_query over a folder. `project` is the store's
    name (the state files remember which store they pushed to); `clock`
    gives the time a commit and a query happen at (UTC now by default);
    `error` is the exception a caller wants raised (StoreError)."""

    def __init__(self, project: str, folder, clock=None, error=StoreError):
        self.project = project
        self.error = error
        self.folder = Path(folder)
        self.email = "local store"
        self._clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    # ------------------------------------------------------------ files --
    def _file(self, path: str) -> Path:
        parts = [p for p in path.strip("/").split("/") if p]
        if len(parts) < 2 or len(parts) % 2:
            raise self.error("'{}' is not a document path".format(path))
        return self.folder.joinpath(*[_segment(p) for p in parts[:-1]]) / (_segment(parts[-1]) + ".json")

    def _now(self) -> dt.datetime:
        moment = self._clock()
        return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)

    def _read(self, file: Path):
        from .firebase_sync import from_fields
        try:
            raw = json.loads(file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise self.error("the local store could not read {} ({})".format(file.name, type(exc).__name__)) from None
        return from_fields(raw.get("fields") or {})

    # ------------------------------------------------------- the three --
    def commit(self, writes: list) -> dict:
        """Write whole documents (and delete the ones asked for). Every
        document is built first, so a value the store cannot hold stops the
        commit before a file is touched."""
        from .firebase_sync import Delete, to_fields
        if not writes:
            return {}
        now = self._now()
        todo = []
        for w in writes:
            if isinstance(w, Delete):
                todo.append((self._file(w.path), None))
                continue
            doc = dict(w.doc)
            for name in w.server_time:
                # 'readQuote.appAt' is the field appAt inside the map readQuote
                *outer, last = name.split(".")
                target = doc
                for part in outer:
                    target[part] = dict(target[part]) if isinstance(target.get(part), dict) else {}
                    target = target[part]
                target[last] = now
            todo.append((self._file(w.path), json.dumps(
                {"fields": to_fields(doc), "updateTime": _stamp(now)}, ensure_ascii=False, sort_keys=True, indent=1)))
        try:
            for file, text in todo:
                if text is None:
                    if file.exists():
                        file.unlink()
                    continue
                file.parent.mkdir(parents=True, exist_ok=True)
                tmp = file.with_name(file.name + ".tmp")
                tmp.write_text(text + "\n", encoding="utf-8", newline="\n")
                os.replace(tmp, file)
        except OSError as exc:
            raise self.error("the local store could not write ({})".format(type(exc).__name__)) from None
        return {"commitTime": _stamp(now)}

    def get(self, path: str) -> dict | None:
        """One document's fields as Python values, None when there is none."""
        return self._read(self._file(path))

    def run_query(self, query: dict) -> dict:
        """{'docs': [(path, fields)], 'readTime': the time of the reading}."""
        sources = query.get("from") or []
        if len(sources) != 1 or not sources[0].get("collectionId") or sources[0].get("allDescendants"):
            raise self.error("the local store answers a query over one root collection only")
        unknown = set(query) - {"from", "where", "orderBy", "limit", "select"}
        if unknown:
            raise self.error("the local store does not answer a query with {}".format(", ".join(sorted(unknown))))
        collection = sources[0]["collectionId"]
        test = self._where(query.get("where"))
        found = []
        folder = self.folder / _segment(collection)
        for file in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            fields = self._read(file)
            if fields is None:
                continue
            name = "{}/{}".format(collection, urllib.parse.unquote(file.stem))
            if test(name, fields):
                found.append((name, fields))
        for order in reversed(query.get("orderBy") or []):
            field = order["field"]["fieldPath"]
            if field != "__name__":
                found = [d for d in found if _field(d[1], field) is not None]
            found.sort(key=lambda d, f=field: d[0] if f == "__name__" else _sortable(_field(d[1], f)),
                       reverse=order.get("direction") == "DESCENDING")
        limit = query.get("limit")
        if isinstance(limit, dict):
            limit = limit.get("value")
        if limit is not None:
            found = found[:int(limit)]
        select = query.get("select")
        if select is not None:
            names = [f["fieldPath"] for f in select.get("fields") or []]
            found = [(name, {k: v for k, v in fields.items() if k in names}) for name, fields in found]
        return {"docs": found, "readTime": _stamp(self._now())}

    def _where(self, where):
        from .firebase_sync import from_value
        if not where:
            return lambda name, fields: True
        if "compositeFilter" in where:
            if where["compositeFilter"].get("op") != "AND":
                raise self.error("the local store joins filters with AND only")
            tests = [self._where(f) for f in where["compositeFilter"].get("filters") or []]
            return lambda name, fields: all(t(name, fields) for t in tests)
        if "fieldFilter" not in where:
            raise self.error("the local store does not answer the filter {}".format(", ".join(sorted(where))))
        one = where["fieldFilter"]
        op = _OPS.get(one.get("op"))
        if op is None:
            raise self.error("the local store does not answer the comparison {}".format(one.get("op")))
        field, wanted = one["field"]["fieldPath"], from_value(one["value"])

        def test(name, fields):
            value = name if field == "__name__" else _field(fields, field)
            if value is None:
                return False
            try:
                return op(_sortable(value), _sortable(wanted))
            except TypeError:
                return False
        return test


def _field(fields: dict, path: str):
    value = fields
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _sortable(value):
    """A time without a zone is UTC, so stored and asked times compare."""
    if isinstance(value, dt.datetime) and value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value
