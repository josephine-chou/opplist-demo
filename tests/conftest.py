"""Shared fixtures. Tests never touch the real database or the real workbook."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opplist import db, mail_archive, paths  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _new_database_from_a_template(tmp_path_factory):
    """A new database is the same file every time: schema.sql brought
    forward by every migration, which takes seconds - and nearly every test
    makes one. The session builds it once, and a test's new database starts
    as a copy of it, with the notes the long way would have given.

    Only for the default schema on a file that is not there yet; a database
    that exists, another schema file or ':memory:' goes the long way, so the
    tests of the migrations themselves are untouched."""
    import shutil

    real = db.open_db
    template = tmp_path_factory.mktemp("template") / "new.db"
    notes = []
    real(template, upgrade_notes=notes).close()
    schema = Path(paths.SCHEMA_SQL)
    stamp = schema.stat().st_mtime_ns

    def open_db(db_path, schema_sql=None, upgrade_notes=None):
        try:
            usable = (schema_sql is None and str(db_path) != ":memory:" and not Path(db_path).exists()
                      and Path(db_path).parent.is_dir() and Path(paths.SCHEMA_SQL) == schema
                      and schema.stat().st_mtime_ns == stamp)
        except (OSError, TypeError, ValueError):
            usable = False
        if not usable:
            return real(db_path, schema_sql, upgrade_notes)
        shutil.copyfile(template, db_path)
        conn = real(db_path, schema_sql, None)
        if upgrade_notes is not None:
            upgrade_notes.extend(notes)
        return conn

    db.open_db = open_db
    yield
    db.open_db = real


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No test opens a network connection: a socket may only connect to
    this machine (the test servers on 127.0.0.1). Anything else fails the
    test that tried."""
    import socket

    real = socket.socket.connect

    def connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, (bytes, bytearray)):
            host = host.decode("ascii", "replace")
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost") and self.family in (
                socket.AF_INET, socket.AF_INET6):
            raise AssertionError("a test tried to connect to {}".format(host))
        return real(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture(autouse=True)
def _no_real_mail_archive(tmp_path, monkeypatch):
    r"""Tests never read or write data\mail-archive: point the archive at an
    empty temporary folder so 'no archive yet' is what every test sees."""
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", tmp_path / "no-archive" / "mail.db")
    monkeypatch.setattr(mail_archive, "SYNC_LOG", tmp_path / "no-archive" / "sync-log.txt")
    monkeypatch.setattr(mail_archive, "SEARCH_DIR", tmp_path / "no-archive" / "mail-search")


@pytest.fixture(autouse=True)
def _no_real_data_dir(tmp_path, monkeypatch):
    """Tests never write into the live data folder: restore points, the
    log, the default database and the CSV drop folder all go under
    tmp_path. backup._dir / logs.setup / paths.DB_PATH read these at call
    time, so patching the module attributes is enough (2026-09-16: a run
    used to leave opplist_before_apply-status_*.db and log lines behind)."""
    scratch = tmp_path / "data"
    monkeypatch.setattr(paths, "DATA_DIR", scratch)
    monkeypatch.setattr(paths, "RESTORE_POINTS_DIR", scratch / "restore-points")
    monkeypatch.setattr(paths, "LOG_PATH", scratch / "opplist.log")
    monkeypatch.setattr(paths, "DB_PATH", scratch / "opplist.db")
    # the log handler is attached once per process; make sure that once
    # happens under a tmp path, never the live one
    from opplist import logs
    if not logs._configured:
        logs.setup(scratch / "opplist.log")

# ----------------------------------------- the live data folder (13c) --
# A test run never writes into the live data folder (JoJo\data): on
# 2026-09-28 a debug script run outside pytest left two 'register: created
# OPP-1453' lines in the live log. At the end of the run: no log handler of
# this process may point there, and no file may have appeared there (the
# mail archive and SQLite's -wal / -shm files aside - the live app and the
# scheduled task write those while tests run). Either fails the run.
_LIVE_FILES = set()


def _live_files():
    live = Path(paths.LIVE_DATA_DIR)
    out = set()
    if not live.exists():
        return out
    for path in live.rglob("*"):
        rel = path.relative_to(live).as_posix()
        if rel.startswith("mail-archive/") or rel.endswith(("-wal", "-shm", "-journal")) or not path.is_file():
            continue
        out.add(rel)
    return out


def pytest_sessionstart(session):
    _LIVE_FILES.clear()
    _LIVE_FILES.update(_live_files())


def pytest_sessionfinish(session, exitstatus):
    import logging

    live = Path(paths.LIVE_DATA_DIR).resolve()
    problems = []
    for handler in logging.getLogger("opplist").handlers:
        target = getattr(handler, "baseFilename", None)
        if target and live in Path(target).resolve().parents:
            problems.append("a log handler writes {}".format(target))
    problems.extend("new file in the live data folder: {}".format(rel) for rel in sorted(_live_files() - _LIVE_FILES))
    if problems:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        for line in problems:
            if reporter:
                reporter.write_line("LIVE DATA WRITTEN BY THE TEST RUN: " + line, red=True)
        session.exitstatus = 1


FIXTURES = paths.FIXTURES_DIR


def form_nesting(html_text):
    """(deepest <form> nesting, the ids in the order they open).

    A form inside a form is dropped by the browser, and a button pointing
    at the dropped one does nothing (the 'Update OPP-xxxx' banner, fixed
    2026-09-17), so every page is checked for it.
    """
    import html.parser

    class Forms(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = 0
            self.deepest = 0
            self.ids = []

        def handle_starttag(self, tag, attrs):
            if tag == "form":
                self.depth += 1
                self.deepest = max(self.deepest, self.depth)
                self.ids.append(dict(attrs).get("id", ""))

        def handle_endtag(self, tag):
            if tag == "form":
                self.depth = max(0, self.depth - 1)

    parser = Forms()
    parser.feed(html_text)
    return parser.deepest, parser.ids


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def repo(conn):
    return db.Repository(conn, source="test")


def seed_reference(repo):
    """The smallest reference data an enquiry needs."""
    for name, region in (("USA", "North America"), ("Chile", "Latin America"),
                         ("Brazil", "Latin America"), ("China", "Asia"),
                         ("Unknown", "Undefined")):
        repo.insert("country", {"name": name, "region": region})
    for key, country in (("MIA", "USA"), ("LAX", "USA"), ("SAI", "Chile"),
                         ("MEM", "USA"), ("KUL", "Unknown"), ("NGB", "China")):
        repo.insert("location", {"key": key, "country": country, "type": "City"})


def add_party(repo, key, ptype="Agent", country="USA", **extra):
    values = {"key": key, "full_name": key, "type": ptype, "country": country}
    values.update(extra)
    return repo.insert("party", values)


def add_enquiry(repo, opp_no, party_id, status="New", received=None, **extra):
    values = {
        "opp_no": opp_no,
        "requester_party_id": party_id,
        "received_on": received or dt.date.today().isoformat(),
        "status": status,
    }
    values.update(extra)
    enquiry_id = repo.insert("enquiry", values)
    repo.insert("status_event", {
        "enquiry_id": enquiry_id,
        "at": values["received_on"],
        "status": status,
    })
    return enquiry_id
