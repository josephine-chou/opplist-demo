"""Batch OP2 (2026-11-20): `python -m opplist serve` over the live database
runs the last commit from its own worktree, JoJo\\opplist-serve. Each git
test builds its own small repository under tmp_path (a probe package that
says where it ran and what it saw); every value here is invented."""

from __future__ import annotations

import json
import logging
import socket
import sqlite3
import subprocess
from pathlib import Path

import pytest

from conftest import seed_reference
from opplist import __main__ as cli
from opplist import db, firebase_sync, paths, ratecard_sync, read_quote, running
from opplist.web import create_app

SERVE_ENVS = (cli.SERVE_WORKTREE_ENV, cli.SERVE_COMMIT_ENV, cli.SERVE_SUBJECT_ENV, cli.SERVE_UNCOMMITTED_ENV,
              cli.SERVE_FALLBACK_ENV, cli.MAIN_DIR_ENV, cli.IN_WORKTREE_ENV, cli.COMMIT_ENV, cli.UNCOMMITTED_ENV)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.test",
                           *args], check=True, capture_output=True, text=True).stdout


PROBE_MAIN = '''import json, os, sys
from pathlib import Path
from opplist import app, paths
paths.EXPORT_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
(paths.EXPORT_PREVIEW_DIR / "probe-preview.csv").write_text("written", encoding="utf-8")
Path(os.environ["OP2_PROBE"]).write_text(json.dumps({
    "cwd": os.getcwd(), "argv": sys.argv[1:], "value": app.VALUE,
    "worktree": os.environ.get("OPPLIST_SERVE_WORKTREE"), "commit": os.environ.get("OPPLIST_SERVE_COMMIT"),
    "subject": os.environ.get("OPPLIST_SERVE_SUBJECT"), "left": os.environ.get("OPPLIST_SERVE_UNCOMMITTED"),
    "main": os.environ.get("OPPLIST_MAIN_DIR"), "fallback": os.environ.get("OPPLIST_SERVE_FALLBACK"),
    "run": os.environ.get("OPPLIST_RUN_WORKTREE"),
    "project": str(paths.PROJECT_DIR), "preview": str(paths.EXPORT_PREVIEW_DIR)}))
sys.exit(int(os.environ.get("OP2_EXIT", "0")))
'''


def clear_envs(monkeypatch):
    """Every hand-over variable gone, and put back as it was afterwards
    (the hand-over itself sets the fallback's in os.environ)."""
    for name in SERVE_ENVS:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def code(tmp_path, monkeypatch):
    """A repository shaped like opplist in a JoJo-like folder: a probe
    package (with the real paths.py, so the worktree's paths are the real
    rule), schema.sql, committed; and a 'live' database beside it, in
    JoJo\\data, whose port nothing answers on."""
    root = tmp_path / "JoJo" / "opplist"
    real_paths = (Path(paths.__file__).read_text(encoding="utf-8"))
    for name, text in (("opplist/__init__.py", ""), ("opplist/__main__.py", PROBE_MAIN),
                       ("opplist/app.py", "VALUE = 1\n"), ("opplist/paths.py", real_paths),
                       ("schema.sql", "-- schema\n"), (".gitignore", "__pycache__/\nexport-preview/\n")):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "Add the probe")
    live = tmp_path / "JoJo" / "data" / "opplist.db"
    live.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(live))
    conn.execute("CREATE TABLE setting (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO setting VALUES ('port', ?)", (str(free_port()),))
    conn.commit()
    conn.close()
    probe = tmp_path / "probe.json"
    monkeypatch.setenv("OP2_PROBE", str(probe))
    monkeypatch.setenv("OP1_PROBE", str(tmp_path / "op1-probe.json"))
    monkeypatch.delenv("OP2_EXIT", raising=False)
    clear_envs(monkeypatch)
    monkeypatch.setattr(cli, "HANDOVER_SKIP", None)
    return root, probe, live


def seen(probe):
    return json.loads(probe.read_text(encoding="utf-8"))


def head(root):
    return git(root, "rev-parse", "HEAD").strip()


# ------------------------------------------------------- handed over --
def test_clean_code_serves_the_commit_from_the_serve_worktree(code):
    root, probe, live = code
    assert cli.serve_hand_over(["serve"], repo_root=root) == 0
    got = seen(probe)
    worktree = root.parent / "opplist-serve"
    assert got["cwd"] == str(worktree) and got["value"] == 1 and got["argv"] == ["serve"]
    assert (got["commit"], got["subject"], got["left"]) == (head(root), "Add the probe", "0")
    assert (got["worktree"], got["main"], got["fallback"]) == (str(worktree), str(root), None)
    assert head(worktree) == head(root)


def test_uncommitted_code_serves_the_committed_value_and_passes_the_count(code):
    root, probe, live = code
    (root / "opplist" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")       # unfinished
    (root / "opplist" / "half.py").write_text("X = 1\n", encoding="utf-8")          # untracked code
    assert cli.serve_hand_over(["serve", "--db", str(live)], repo_root=root) == 0
    got = seen(probe)
    assert got["value"] == 1 and got["left"] == "2" and got["commit"] == head(root)


def test_a_new_commit_moves_the_worktree_and_a_change_inside_it_is_discarded(code):
    root, probe, live = code
    assert cli.serve_hand_over(["serve"], repo_root=root) == 0
    worktree = root.parent / "opplist-serve"
    # RG2b: another length than 'VALUE = 1' - Python checks the worktree's
    # cached bytecode by the source's size and its mtime in whole seconds,
    # and the checkout may fall in the first run's second
    (root / "opplist" / "app.py").write_text("VALUE = 55\n", encoding="utf-8")
    git(root, "commit", "-q", "-am", "Fifty-five")
    (worktree / "opplist" / "app.py").write_text("VALUE = 999\n", encoding="utf-8")   # changed in there
    assert cli.serve_hand_over(["serve"], repo_root=root) == 0
    got = seen(probe)
    assert got["value"] == 55 and got["subject"] == "Fifty-five" and head(worktree) == head(root)


def test_the_servers_exit_code_is_returned(code, monkeypatch):
    root, probe, live = code
    monkeypatch.setenv("OP2_EXIT", "7")
    assert cli.serve_hand_over(["serve"], repo_root=root) == 7


def test_relative_db_and_restore_dir_are_passed_on_absolute(code, monkeypatch):
    root, probe, live = code
    monkeypatch.chdir(root)
    relative = str(Path("..") / "data" / "opplist.db")        # RG2b: folders on any machine
    assert cli.serve_hand_over(["serve", "--db", relative, "--restore-dir", "points",
                                "--debug"], repo_root=root) == 0
    assert seen(probe)["argv"] == ["serve", "--db", str(live.resolve()), "--restore-dir",
                                   str((root / "points").resolve()), "--debug"]
    assert cli.serve_hand_over(["serve", "--db=../data/opplist.db"], repo_root=root) == 0
    assert seen(probe)["argv"] == ["serve", "--db={}".format(live.resolve())]


def test_export_preview_from_the_worktree_goes_to_the_main_folder(code):
    root, probe, live = code
    assert cli.serve_hand_over(["serve"], repo_root=root) == 0
    got = seen(probe)
    assert got["project"] == str(root.parent / "opplist-serve")                  # the code from there
    assert got["preview"] == str(root / "export-preview")                        # what it writes, here
    assert (root / "export-preview" / "probe-preview.csv").read_text(encoding="utf-8") == "written"
    assert not (root.parent / "opplist-serve" / "export-preview").exists()


# --------------------------------------------------- not handed over --
def test_a_copy_is_served_from_this_folder(code, tmp_path):
    root, probe, live = code
    copy = tmp_path / "copy.db"
    copy.write_bytes(live.read_bytes())
    assert cli.serve_hand_over(["serve", "--db", str(copy)], repo_root=root) is None
    assert not (root.parent / "opplist-serve").exists() and not probe.exists()
    assert cli.serve_hand_over(["check"], repo_root=root) is None                 # not serve


def test_a_serve_started_inside_a_worktree_or_a_hand_over_is_not_handed_over(code, monkeypatch):
    root, probe, live = code
    for name in ("opplist-serve", "opplist-run"):
        assert cli.serve_hand_over(["serve"], repo_root=root.parent / name) is None
    for env in (cli.SERVE_WORKTREE_ENV, cli.IN_WORKTREE_ENV):
        monkeypatch.setenv(env, "1")
        assert cli.serve_hand_over(["serve"], repo_root=root) is None
        monkeypatch.delenv(env)
    assert not probe.exists() and not (root.parent / "opplist-serve").exists()


def test_serve_never_touches_opplist_run_and_the_morning_run_never_touches_opplist_serve(code):
    root, probe, live = code
    (root / "opplist" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert cli.hand_over(["mail-sync", "--scheduled"], repo_root=root) == 0        # the probe's 0
    run = root.parent / "opplist-run"
    first = head(run)
    git(root, "commit", "-q", "-am", "Two")
    assert cli.serve_hand_over(["serve"], repo_root=root) == 0
    assert head(run) == first                                                    # opplist-run where it was
    serve = root.parent / "opplist-serve"
    at_serve = head(serve)
    (root / "opplist" / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    git(root, "commit", "-q", "-am", "Three")
    (root / "opplist" / "app.py").write_text("VALUE = 4\n", encoding="utf-8")
    assert cli.hand_over(["mail-sync", "--scheduled"], repo_root=root) == 0
    assert head(run) == head(root) and head(serve) == at_serve                   # opplist-serve where it was


def test_something_answering_on_the_port_exits_3_and_leaves_the_worktree(code):
    root, probe, live = code
    assert cli.serve_hand_over(["serve"], repo_root=root) == 0
    worktree = root.parent / "opplist-serve"
    old = head(worktree)
    (root / "opplist" / "app.py").write_text("VALUE = 8\n", encoding="utf-8")
    git(root, "commit", "-q", "-am", "Eight")
    probe.unlink()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    conn = sqlite3.connect(str(live))
    conn.execute("UPDATE setting SET value = ? WHERE key = 'port'", (str(port),))
    conn.commit()
    conn.close()
    try:
        assert cli.serve_hand_over(["serve"], repo_root=root) == cli.EXIT_ALREADY_RUNNING == 3
    finally:
        listener.close()
    assert head(worktree) == old and not probe.exists()
    log = (root.parent / "data" / "opplist.log").read_text(encoding="utf-8")
    assert "WARNING opplist.serve: serve refused: something already answers on 127.0.0.1:{}".format(port) in log


# -------------------------------------------------------- fallbacks --
def _fallback(root, probe):
    assert cli.serve_hand_over(["serve"], repo_root=root) is None
    assert not probe.exists()
    import os
    return os.environ.get(cli.SERVE_FALLBACK_ENV)


def test_git_missing_serves_from_this_folder_with_the_reason(code, monkeypatch):
    root, probe, live = code
    monkeypatch.setattr(cli, "_git_exe", lambda: None)
    assert _fallback(root, probe) == "git not found"


def test_worktree_add_failing_serves_from_this_folder(code):
    root, probe, live = code
    (root.parent / "opplist-serve").write_text("in the way", encoding="utf-8")   # a file, not a folder
    reason = _fallback(root, probe)
    assert reason and "already exists" in reason


def test_a_worktree_not_clean_after_the_checkout_serves_from_this_folder(code):
    root, probe, live = code
    assert cli.serve_hand_over(["serve"], repo_root=root) == 0
    probe.unlink()
    (root.parent / "opplist-serve" / "opplist" / "extra.py").write_text("X = 1\n", encoding="utf-8")
    assert _fallback(root, probe) == "JoJo\\opplist-serve is not clean after the checkout (opplist/extra.py)"


def test_an_unexpected_error_serves_from_this_folder(code, monkeypatch):
    root, probe, live = code

    def boom(git, root):
        raise RuntimeError("the probe broke")
    monkeypatch.setattr(cli, "_prepare_serve_worktree", boom)
    assert _fallback(root, probe) == "RuntimeError: the probe broke"


@pytest.fixture
def world(tmp_path, monkeypatch):
    clear_envs(monkeypatch)
    monkeypatch.setattr(cli, "HANDOVER_SKIP", None)
    path = tmp_path / "op2.db"
    conn = db.open_db(path)
    seed_reference(db.Repository(conn, source="test"))
    conn.commit()
    conn.close()
    return path


def client(path):
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client()


def test_the_fallback_reason_reaches_the_console_the_log_and_every_page(world, monkeypatch, capsys, caplog):
    monkeypatch.setenv(cli.SERVE_FALLBACK_ENV, "git not found")
    red = "8765 is running the code in {}, not the last commit: git not found.".format(
        running.folder(paths.PROJECT_DIR))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    conn = sqlite3.connect(str(world))
    conn.execute("UPDATE setting SET value = ? WHERE key = 'port'", (str(listener.getsockname()[1]),))
    conn.commit()
    conn.close()
    try:
        with caplog.at_level(logging.INFO):
            assert cli.main(["serve", "--db", str(world)]) == 3                 # stops at FX3, after the line
    finally:
        listener.close()
    out = capsys.readouterr().out
    assert out.index(red) < out.index("OppList is already running")
    assert any(r.getMessage() == red and r.levelno == logging.WARNING for r in caplog.records)
    c = client(world)
    for url in ("/enquiries", "/settings", "/tracking"):
        body = c.get(url).data.decode("utf-8")
        assert 'id="code-banner"' in body and red in body
    assert "Running the code in {}: not the last commit, git not found.".format(
        running.folder(paths.PROJECT_DIR)) in c.get("/settings").data.decode("utf-8")


def test_no_red_line_and_the_old_footer_when_never_handed_over(world):
    body = client(world).get("/settings").data.decode("utf-8")
    assert 'id="code-banner"' not in body
    assert " · code " in body and "Running the code in {}</p>".format(running.folder(paths.PROJECT_DIR)) in body
    assert running.start_lines() == []


# ----------------------------------------------- what she sees there --
def _as_worktree(monkeypatch, main_dir, commit="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678", left="0"):
    monkeypatch.setenv(cli.SERVE_WORKTREE_ENV, str(paths.PROJECT_DIR))
    monkeypatch.setenv(cli.SERVE_COMMIT_ENV, commit)
    monkeypatch.setenv(cli.SERVE_SUBJECT_ENV, "Batch ZZ9: the probe line")
    monkeypatch.setenv(cli.SERVE_UNCOMMITTED_ENV, left)
    monkeypatch.setenv(cli.MAIN_DIR_ENV, str(main_dir))


@pytest.fixture
def main_repo(tmp_path):
    root = tmp_path / "JoJo" / "opplist"
    root.mkdir(parents=True)
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "Batch ZZ10: the next one")
    return root


def test_the_footer_and_settings_from_the_serve_worktree(world, main_repo, monkeypatch, capsys):
    _as_worktree(monkeypatch, main_repo, commit=head(main_repo), left="3")
    c = client(world)
    body = c.get("/settings").data.decode("utf-8")
    short = head(main_repo)[:7]
    assert " · commit {}</footer>".format(short) in body and " · code " not in body
    assert "Running commit {} Batch ZZ9: the probe line, from {}, since ".format(short, paths.PROJECT_DIR) in body
    assert "3 code files in JoJo\\opplist were not committed at that start (not running)" in body
    assert "restart 8765" not in body and 'id="code-banner"' not in body
    assert running.start_lines() == [
        (logging.INFO, "Code: commit {} Batch ZZ9: the probe line, from {}".format(
            short, running.folder(paths.PROJECT_DIR))),
        (logging.INFO, "3 code files in JoJo\\opplist are not committed: not running.")]


def test_settings_says_when_the_main_folder_is_at_another_commit(world, main_repo, monkeypatch):
    _as_worktree(monkeypatch, main_repo)                                        # an older commit runs
    body = client(world).get("/settings").data.decode("utf-8")
    assert "JoJo\\opplist is at {} Batch ZZ10: the next one: restart 8765 to run it".format(
        head(main_repo)[:7]) in body
    assert "not committed at that start" not in body


def test_settings_says_nothing_of_the_head_when_git_fails(world, tmp_path, monkeypatch):
    _as_worktree(monkeypatch, tmp_path / "not-a-repo")
    body = client(world).get("/settings").data.decode("utf-8")
    assert "Running commit a1b2c3d" in body and "restart 8765" not in body


def test_a_package_imported_from_elsewhere_is_a_red_line(world, tmp_path, monkeypatch):
    _as_worktree(monkeypatch, tmp_path)
    monkeypatch.setenv(cli.SERVE_WORKTREE_ENV, str(tmp_path / "JoJo" / "opplist-serve"))
    body = client(world).get("/enquiries").data.decode("utf-8")
    red = "8765 is running the code in {f}, not the last commit: the package was loaded from {f}, " \
          "not from JoJo\\opplist-serve.".format(f=running.folder(paths.PROJECT_DIR))
    assert 'id="code-banner"' in body and red in body and " · code " in body


def test_a_long_subject_is_cut_where_shown():
    long = "Batch ZZ11: " + "a very long line " * 20
    assert len(running.shown_subject(long)) == 80 and running.shown_subject(long).endswith("...")
    assert running.shown_subject(long) == cli.shown_subject(long)
    assert running.uncommitted_line(1, "JoJo\\opplist") == cli.uncommitted_line(1, "JoJo\\opplist") == \
        "1 code file in JoJo\\opplist is not committed: not running."


# ------------------------------------------------------------- keys --
@pytest.mark.parametrize("folder", ["opplist", "opplist-run", "opplist-serve"])
def test_a_key_inside_any_code_folder_is_refused(world, tmp_path, monkeypatch, folder):
    monkeypatch.setattr(paths, "COMPANY_DIR", tmp_path / "JoJo")
    key = tmp_path / "JoJo" / folder / "secrets" / "key.json"                # never made, never read
    conn = db.open_db(world)
    try:
        # this edition has one key file left, Read quote's: the team site and
        # the Rate Card are local stores with no key at all
        for module, setting, error in ((read_quote, "read_quote_key", read_quote.ReadQuoteError),):
            db.set_setting(conn, setting, str(key))
            with pytest.raises(error, match=r"must not be inside the repo \({}\)".format(folder)):
                module.key_path(conn)
        for module in (firebase_sync, ratecard_sync):
            assert not hasattr(module, "key_path")
        db.set_setting(conn, "read_quote_key", str(tmp_path / "JoJo" / "data" / "secrets" / "key.txt"))
        assert read_quote.key_path(conn) == tmp_path / "JoJo" / "data" / "secrets" / "key.txt"
    finally:
        conn.close()


# ------------------------------------------------------------ --where --
def test_where_prints_and_changes_nothing(code, capsys):
    root, probe, live = code
    (root / "opplist" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    before = live.read_bytes()
    assert cli.serve_hand_over(["serve", "--where"], repo_root=root) == 0
    out = capsys.readouterr().out
    assert "Database: {} (the live one)".format(live) in out
    assert "Code: a start runs commit {} Add the probe, from JoJo\\opplist-serve".format(head(root)[:7]) in out
    assert "JoJo\\opplist-serve is not there yet: the first start makes it" in out
    assert "1 code file in JoJo\\opplist is not committed: not running (opplist/app.py)" in out
    assert "nothing answers" in out
    assert not (root.parent / "opplist-serve").exists() and not probe.exists() and live.read_bytes() == before


def test_where_names_the_worktrees_commit_and_leaves_it(code, capsys, tmp_path):
    root, probe, live = code
    assert cli.serve_hand_over(["serve"], repo_root=root) == 0
    worktree = root.parent / "opplist-serve"
    old = head(worktree)
    git(root, "commit", "-q", "--allow-empty", "-m", "Later")
    capsys.readouterr()
    assert cli.serve_hand_over(["serve", "--where"], repo_root=root) == 0
    out = capsys.readouterr().out
    assert "JoJo\\opplist-serve is at commit {} Add the probe (a start brings it to {})".format(
        old[:7], head(root)[:7]) in out
    assert head(worktree) == old
    copy = tmp_path / "copy.db"
    copy.write_bytes(live.read_bytes())
    assert cli.serve_hand_over(["serve", "--where", "--db", str(copy)], repo_root=root) == 0
    out = capsys.readouterr().out
    assert "(a copy: served from this folder)" in out and "Code: a start runs the code in JoJo\\opplist" in out


def test_where_through_main_reads_only(world, capsys):
    before = world.read_bytes()
    assert cli.main(["serve", "--where", "--db", str(world)]) == 0
    out = capsys.readouterr().out
    assert "a copy: served from this folder" in out and "Port 8765" in out
    assert world.read_bytes() == before


@pytest.fixture(autouse=True)
def _worktrees_asked_for(monkeypatch):
    """This edition hands over to a worktree only when asked to
    (OPPLIST_WORKTREES=1); these tests are about the hand-over."""
    monkeypatch.setenv(cli.WORKTREES_ENV, "1")
