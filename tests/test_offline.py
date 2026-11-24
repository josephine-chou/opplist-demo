"""This edition runs offline: no service is contacted, no key is shipped.

The team site and the Rate Card are folders of JSON documents (local_store),
Read quote's one HTTP function is never reached without a key file, the
git worktree hand-overs are off unless asked for, and nothing in the
repository names a hosted project, a key or a service account. Needs no
forbidden list, so it runs on CI too.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from conftest import seed_reference
from opplist import __main__ as cli
from opplist import db, firebase_sync, paths, ratecard_sync, read_quote

ROOT = paths.PROJECT_DIR
#: Client libraries of the services this edition does not talk to, and
#: general HTTP clients: none is imported anywhere in the package.
NO_IMPORT = {"requests", "google", "firebase_admin", "anthropic", "httpx", "urllib3", "aiohttp", "grpc"}
#: Marks of a key, a service account or a hosted project, written in two
#: halves so that this file does not carry them itself.
MARKS = ["AI" + "za", "sk-" + "ant-", "-----" + "BEGIN", "private" + "_key", "client" + "_email",
         "firebase" + "app.com", "firebase" + "io.com", "web" + ".app", "google" + "apis.com"]
TEXT = {".py", ".json", ".txt", ".csv", ".md", ".html", ".sql", ".js", ".css", ".cmd", ".sh", ".yml", ".toml", ""}


def repo_files() -> list:
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT,
                             capture_output=True, check=True).stdout.decode()
        names = [n for n in out.split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = [str(p.relative_to(ROOT)).replace("\\", "/") for p in ROOT.rglob("*") if p.is_file()
                 and ".git" not in p.parts and "__pycache__" not in p.parts]
    return [ROOT / n for n in names if (ROOT / n).is_file() and Path(n).suffix in TEXT]


def test_no_key_account_or_hosted_project_is_named_anywhere():
    found = []
    for path in repo_files():
        text = path.read_bytes().decode("utf-8", "replace")
        found += ["{}: {}".format(path.relative_to(ROOT).as_posix(), mark) for mark in MARKS if mark in text]
    assert not found, "\n".join(found[:40])


def test_the_package_imports_no_service_or_http_client():
    found = []
    for path in sorted((ROOT / "opplist").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            found += ["{}:{}: {}".format(path.name, node.lineno, n) for n in names if n.split(".")[0] in NO_IMPORT]
    assert not found, "\n".join(found)
    listed = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for name in ("google-auth", "requests", "firebase", "anthropic", "httpx"):
        assert name not in listed, name


def test_only_read_quotes_one_function_can_open_a_connection():
    """urllib.request and socket are named in one function that could reach
    outside (and in the check of this machine's own port)."""
    found = []
    for path in sorted((ROOT / "opplist").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.Module))]:
            for node in ast.iter_child_nodes(func) if isinstance(func, ast.Module) else ast.walk(func):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                for name in names:
                    if name in ("urllib.request", "http.client", "socket", "ftplib", "smtplib"):
                        found.append((path.name, getattr(func, "name", "<module>"), name))
    # __main__.answers_on asks 127.0.0.1 whether an OppList already listens there
    assert sorted(set(found)) == [("__main__.py", "answers_on", "socket"),
                                  ("read_quote.py", "_requests_transport", "socket"),
                                  ("read_quote.py", "_requests_transport", "urllib.request")]


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(tmp_path / "offline.db")
    seed_reference(db.Repository(connection, source="test"))
    connection.commit()
    yield connection
    connection.close()


def test_both_stores_are_off_until_settings_turns_them_on(conn):
    assert firebase_sync.configured(conn)[0] is False and ratecard_sync.configured(conn)[0] is False
    with pytest.raises(firebase_sync.FirebaseError):
        firebase_sync.client_for(conn)
    with pytest.raises(ratecard_sync.RateCardError):
        ratecard_sync.client_for(conn)
    db.set_setting(conn, firebase_sync.STORE_SETTING, "on")
    db.set_setting(conn, ratecard_sync.STORE_SETTING, "on")
    site, card = firebase_sync.client_for(conn), ratecard_sync.client_for(conn)
    assert type(site).__name__ == type(card).__name__ == "LocalStore"
    assert site.folder == paths.DATA_DIR / "team-site" and card.folder == paths.DATA_DIR / "rate-card"
    # a link to the site is a setting's, a reserved name by default
    assert firebase_sync.site_url(conn) == "https://team-site.example"


def test_read_quote_never_calls_without_a_key(conn, monkeypatch):
    reached = []
    monkeypatch.setattr(read_quote, "_requests_transport", lambda *a, **k: reached.append(a) or (200, {}))
    monkeypatch.setattr(read_quote, "TRANSPORT", None)
    assert read_quote.key_state(conn)[0] is False                  # no key file ships with the repo
    with pytest.raises(read_quote.ReadQuoteError, match="no key at"):
        read_quote.call(conn, read_quote.DEFAULT_MODEL, [{"type": "text", "text": "x"}])
    assert reached == []
    monkeypatch.undo()
    # and the function itself refuses a request that carries no key
    with pytest.raises(ConnectionError):
        read_quote._requests_transport(read_quote.API_URL, {"x-api-key": ""}, {}, 1)


def test_no_worktree_and_no_git_unless_asked_for(tmp_path, monkeypatch):
    monkeypatch.delenv(cli.WORKTREES_ENV, raising=False)
    monkeypatch.delenv("OPPLIST_DATA_DIR", raising=False)
    for name in (cli.SERVE_WORKTREE_ENV, cli.IN_WORKTREE_ENV):
        monkeypatch.delenv(name, raising=False)

    def no_git(*args, **kwargs):
        raise AssertionError("git was looked for")
    monkeypatch.setattr(cli, "_git_exe", no_git)
    monkeypatch.setattr(cli, "answers_on", lambda port: False)
    root = tmp_path / "clone"
    (root / "data").mkdir(parents=True)
    # a visitor's clone, its own database: served from the folder it is in
    assert cli.serve_hand_over(["serve"], repo_root=root) is None
    assert cli.hand_over(["firebase-sync", "--scheduled"], repo_root=root) is None
    assert cli.serve_where(["serve", "--where"], root) == 0
    assert not (tmp_path / "opplist-serve").exists() and not (tmp_path / "opplist-run").exists()
    assert cli._live_db(root) == root / "data" / "opplist.db"
