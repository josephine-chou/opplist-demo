"""Command line entry point: python -m opplist <command>.

Every command of the app: the server, the imports, the checks, the exports
and the one-off repairs. `python -m opplist --help` lists them.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
import datetime as dt
import sys
from pathlib import Path


# ------------------------------------------- the morning run's hand-over --
# OP1 (2026-11-10): a --scheduled run that finds uncommitted code runs the
# commit HEAD points at instead of skipping. Everything here uses the
# standard library only and runs before any other opplist module is
# imported, so unfinished code in the package cannot break it. The worktree
# JoJo\opplist-run sits beside opplist, so paths.DATA_DIR there is still
# JoJo\data; it holds code only and nothing else is written into it.
#
# This edition: both hand-overs (this one and serve's, below) are for the PC
# the app lives on and are off unless asked for with OPPLIST_WORKTREES=1.
# Without it nothing here makes a worktree or runs git: a clone is served
# from the folder it is in.
WORKTREES_ENV = "OPPLIST_WORKTREES"
RUN_DIR_NAME = "opplist-run"
IN_WORKTREE_ENV = "OPPLIST_RUN_WORKTREE"
COMMIT_ENV = "OPPLIST_RUN_COMMIT"             # scheduled.COMMIT_ENV
UNCOMMITTED_ENV = "OPPLIST_RUN_UNCOMMITTED"   # scheduled.UNCOMMITTED_ENV
_CODE_PATHS = ("opplist", "schema.sql")       # scheduled.CODE_PATHS
#: why the hand-over could not happen (git failed); main() records the skip
HANDOVER_SKIP = None


def worktrees_on() -> bool:
    return os.environ.get(WORKTREES_ENV, "").strip() == "1"


def _git_exe():
    import shutil
    found = shutil.which("git")
    if found:
        return found
    fallback = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "cmd" / "git.exe"
    return str(fallback) if fallback.exists() else None


def _git_out(git, root, *args, env=None) -> str:
    result = subprocess.run([git, "-C", str(root), *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=120, env=env)
    if result.returncode != 0:
        raise OSError((result.stderr or result.stdout or "git {} failed".format(args[0])).strip().splitlines()[0])
    return result.stdout


def _code_changes(git, root, env=None) -> list:
    """scheduled.uncommitted, in the standard library only: modified, added,
    deleted or renamed files under the package folder or schema.sql, and
    untracked .py files there."""
    out = []
    for line in _git_out(git, root, "status", "--porcelain", "--untracked-files=all", "--", *_CODE_PATHS,
                         env=env).splitlines():
        if len(line) < 4:
            continue
        code, name = line[:2], line[3:].strip().strip('"')
        if code == "??" and not name.endswith(".py"):
            continue
        out.append(name)
    return out


def hand_over(argv=None, repo_root=None):
    """The exit code of the same command run from the worktree at HEAD, or
    None when this process runs it itself: not --scheduled, already inside
    the worktree (a run there never starts another), clean code, or git
    failed (then HANDOVER_SKIP says why and main() records the skip)."""
    global HANDOVER_SKIP
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parent.parent
    if "--scheduled" not in argv or os.environ.get(IN_WORKTREE_ENV) or root.name == RUN_DIR_NAME:
        return None
    if not worktrees_on():
        return None
    git = _git_exe()
    try:
        if git is None:
            raise OSError("git not found")
        changed = _code_changes(git, root)
        if not changed:
            return None
        commit = _git_out(git, root, "rev-parse", "HEAD").strip()
        worktree = root.parent / RUN_DIR_NAME
        if (worktree / ".git").exists():
            # that commit, detached and forced: a change in there is discarded
            _git_out(git, worktree, "checkout", "--quiet", "--detach", "--force", commit)
        else:
            _git_out(git, root, "worktree", "prune")          # a folder removed by hand
            _git_out(git, root, "worktree", "add", "--quiet", "--detach", str(worktree), commit)
    except (OSError, subprocess.SubprocessError) as exc:
        HANDOVER_SKIP = "git cannot be run: {}".format(exc)
        return None
    env = dict(os.environ)
    env.update({IN_WORKTREE_ENV: "1", COMMIT_ENV: commit, UNCOMMITTED_ENV: str(len(changed)),
                "PYTHONDONTWRITEBYTECODE": "1"})
    return subprocess.run([sys.executable, "-m", "opplist", *argv], cwd=str(worktree), env=env).returncode


# --------------------------------------------------- serve's hand-over --
# OP2 (2026-11-20): `python -m opplist serve` over the live database, started
# from JoJo\opplist, always runs the commit HEAD points at, from the
# worktree JoJo\opplist-serve (its own: the morning run moves
# opplist-run while 8765 may be running). A running server reads templates
# and static files from its folder, so serving from the folder every batch
# edits let a batch's unfinished files (and a restart, its unfinished
# schema) reach her. Standard library only, before any other import, as
# OP1's. Any failure serves from this folder as before, with the reason on
# every page; only FX3's exit 3 keeps OppList from starting.
SERVE_DIR_NAME = "opplist-serve"              # paths.SERVE_WORKTREE_NAME
SERVE_WORKTREE_ENV = "OPPLIST_SERVE_WORKTREE"   # running.WORKTREE_ENV: the worktree's folder
SERVE_COMMIT_ENV = "OPPLIST_SERVE_COMMIT"
SERVE_SUBJECT_ENV = "OPPLIST_SERVE_SUBJECT"
SERVE_UNCOMMITTED_ENV = "OPPLIST_SERVE_UNCOMMITTED"
SERVE_FALLBACK_ENV = "OPPLIST_SERVE_FALLBACK"   # why it serves from this folder
MAIN_DIR_ENV = "OPPLIST_MAIN_DIR"
_PATH_OPTIONS = ("--db", "--restore-dir")

#: FX3: serve's exit code when an OppList (or anything) already answers on
#: its port - OppList.cmd shows the line and pauses
EXIT_ALREADY_RUNNING = 3


def _serve_port(db_path) -> int:
    """The port setting read without opening the database (no upgrade,
    nothing written); 8765 when it cannot be read."""
    import sqlite3
    path = Path(db_path)
    if path.exists():
        try:
            conn = sqlite3.connect("file:{}?mode=ro".format(path.as_posix()), uri=True)
            try:
                row = conn.execute("SELECT value FROM setting WHERE key = 'port'").fetchone()
            finally:
                conn.close()
            if row and str(row[0]).strip().isdigit():
                return int(str(row[0]).strip())
        except sqlite3.Error:
            pass
    return 8765


def answers_on(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """FX3: something already accepts connections on host:port."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def already_running_line(port) -> str:
    return "OppList is already running on http://127.0.0.1:{} - close that window first, or use it.".format(port)


def uncommitted_line(count, folder) -> str:
    """'3 code files in JoJo\\opplist are not committed: not running.'"""
    return "{} code file{} in {} {} not committed: not running.".format(
        count, "" if count == 1 else "s", folder, "is" if count == 1 else "are")


#: a commit's subject as shown (her subjects run to a paragraph)
SUBJECT_SHOWN = 80


def shown_subject(subject) -> str:
    subject = " ".join((subject or "").split())
    return subject if len(subject) <= SUBJECT_SHOWN else subject[:SUBJECT_SHOWN - 3].rstrip() + "..."


def _folder(path) -> str:
    """JoJo\\opplist-serve: a folder named with its parent."""
    path = Path(path)
    return "{}\\{}".format(path.parent.name, path.name)


def _option_value(argv, name):
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(name + "="):
            return arg[len(name) + 1:]
    return None


def _absolute_options(argv) -> list:
    """argv with --db and --restore-dir made absolute (the worktree is
    another current folder)."""
    out, take = [], None
    for arg in argv:
        if take:
            out.append(str(Path(arg).resolve()))
            take = None
            continue
        name = arg.split("=", 1)[0]
        if name in _PATH_OPTIONS and "=" in arg:
            out.append("{}={}".format(name, Path(arg.split("=", 1)[1]).resolve()))
            continue
        if arg in _PATH_OPTIONS:
            take = arg
        out.append(arg)
    return out


def _live_db(root) -> Path:
    """paths.LIVE_DB_PATH, from the repository's folder, by paths' rule: the
    folder OPPLIST_DATA_DIR names, else data beside the project folder with
    the worktrees asked for, else data inside it."""
    named = os.environ.get("OPPLIST_DATA_DIR")
    if named:
        return Path(named).expanduser().resolve() / "opplist.db"
    return (Path(root).parent if worktrees_on() else Path(root)) / "data" / "opplist.db"


def _serve_db(argv, root) -> Path:
    given = _option_value(argv, "--db")
    return Path(given).resolve() if given else _live_db(root)


def _is_live(db_path, root) -> bool:
    try:
        return Path(db_path).resolve() == _live_db(root).resolve()
    except OSError:
        return False


def _log_line(root, level, text) -> None:
    """One line in data\\opplist.log in logs' format (the hand-over runs
    before logs can be imported); a log that cannot be written is no reason
    to stop."""
    try:
        target = _live_db(root).with_name("opplist.log")
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:23]
        with target.open("a", encoding="utf-8") as handle:
            handle.write("{} {:<7} opplist.serve: {}\n".format(stamp, level, text))
    except OSError:
        pass


def _head(git, root, env=None) -> tuple:
    """(commit, subject) of HEAD."""
    out = _git_out(git, root, "log", "-1", "--format=%H%x00%s", "HEAD", env=env).strip()
    commit, _, subject = out.partition("\x00")
    if not commit:
        raise OSError("no commit at HEAD")
    return commit, subject


def _prepare_serve_worktree(git, root) -> tuple:
    """JoJo\\opplist-serve at the commit HEAD points at, checked:
    (worktree, commit, subject, uncommitted files here). OSError when it
    cannot be done or is not right afterwards."""
    changed = _code_changes(git, root)
    commit, subject = _head(git, root)
    worktree = Path(root).parent / SERVE_DIR_NAME
    if (worktree / ".git").exists():
        # that commit, detached and forced: a change made in there is discarded
        _git_out(git, worktree, "checkout", "--quiet", "--detach", "--force", commit)
    else:
        _git_out(git, root, "worktree", "prune")          # a folder removed by hand
        _git_out(git, root, "worktree", "add", "--quiet", "--detach", str(worktree), commit)
    at = _git_out(git, worktree, "rev-parse", "HEAD").strip()
    if at != commit:
        raise OSError("{} is at {}, not at {}".format(_folder(worktree), at[:7], commit[:7]))
    left = _code_changes(git, worktree)
    if left:
        raise OSError("{} is not clean after the checkout ({})".format(_folder(worktree), ", ".join(left[:3])))
    return worktree, commit, subject, changed


def _wait(proc) -> int:
    """The server's exit code. Ctrl+C reaches the server too (one console);
    this waits for it to end rather than killing it."""
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue


def serve_hand_over(argv=None, repo_root=None):
    """The exit code of serve run from JoJo\\opplist-serve at HEAD (or
    FX3's 3, or --where's 0), or None when this process serves itself: not
    serve, a copy (--db elsewhere), started inside a worktree or a
    hand-over, or the hand-over failed (then SERVE_FALLBACK_ENV says why)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parent.parent
    if not argv or argv[0] != "serve":
        return None
    if "--where" in argv:
        return serve_where(argv, root)
    if (os.environ.get(SERVE_WORKTREE_ENV) or os.environ.get(IN_WORKTREE_ENV)
            or root.name in (RUN_DIR_NAME, SERVE_DIR_NAME)):
        return None
    db_path = _serve_db(argv, root)
    if not _is_live(db_path, root):
        return None
    # FX3's check first, the worktree untouched: a running 8765 may be serving from it
    port = _serve_port(db_path)
    if answers_on(port):
        print(already_running_line(port))
        _log_line(root, "WARNING", "serve refused: something already answers on 127.0.0.1:{}".format(port))
        return EXIT_ALREADY_RUNNING
    if not worktrees_on():
        return None                              # this edition: served from this folder, no git
    try:
        git = _git_exe()
        if git is None:
            raise OSError("git not found")
        worktree, commit, subject, changed = _prepare_serve_worktree(git, root)
        env = dict(os.environ)
        env.pop(SERVE_FALLBACK_ENV, None)
        env.update({SERVE_WORKTREE_ENV: str(worktree), SERVE_COMMIT_ENV: commit, SERVE_SUBJECT_ENV: subject,
                    SERVE_UNCOMMITTED_ENV: str(len(changed)), MAIN_DIR_ENV: str(root)})
        proc = subprocess.Popen([sys.executable, "-m", "opplist", *_absolute_options(argv)],
                                cwd=str(worktree), env=env)
    except Exception as exc:                     # never keeps OppList from starting
        if isinstance(exc, (OSError, subprocess.SubprocessError)):
            reason = str(exc).strip() or type(exc).__name__
        else:
            reason = "{}: {}".format(type(exc).__name__, exc) if str(exc) else type(exc).__name__
        os.environ[SERVE_FALLBACK_ENV] = reason.rstrip(".")
        return None
    return _wait(proc)


def serve_where(argv, root) -> int:
    """serve --where: what a start would do. Nothing made, moved or
    written: git reads only (no optional locks), the database read only."""
    for stream in (sys.stdout, sys.stderr):      # main() is not reached: the same as it does
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    root = Path(root)
    if not worktrees_on():
        print("Worktrees are off ({}=1 turns them on): serve runs the code in this folder.".format(WORKTREES_ENV))
        return 0
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    db_path = _serve_db(argv, root)
    live = _is_live(db_path, root)
    worktree = root.parent / SERVE_DIR_NAME
    print("Database: {} ({})".format(db_path, "the live one" if live else "a copy: served from this folder"))
    inside = root.name in (RUN_DIR_NAME, SERVE_DIR_NAME) or os.environ.get(SERVE_WORKTREE_ENV) \
        or os.environ.get(IN_WORKTREE_ENV)
    git = _git_exe()
    try:
        if git is None:
            raise OSError("git not found")
        changed = _code_changes(git, root, env=env)
        commit, subject = _head(git, root, env=env)
        if inside:
            print("Code: {} is a worktree: a serve started here runs its own code".format(_folder(root)))
        elif live:
            print("Code: a start runs commit {} {}, from {}".format(commit[:7], shown_subject(subject),
                                                                    _folder(worktree)))
        else:
            print("Code: a start runs the code in {}".format(_folder(root)))
        if (worktree / ".git").exists():
            at, at_subject = _head(git, worktree, env=env)
            print("{} is at commit {} {}{}".format(_folder(worktree), at[:7], shown_subject(at_subject),
                                                   "" if at == commit else " (a start brings it to {})".format(commit[:7])))
        else:
            print("{} is not there yet: the first start makes it".format(_folder(worktree)))
        if changed:
            print("{} ({})".format(uncommitted_line(len(changed), _folder(root)).rstrip("."), ", ".join(changed)))
        else:
            print("No uncommitted code in {}".format(_folder(root)))
    except (OSError, subprocess.SubprocessError) as exc:
        print("git cannot be run ({}): a start serves the code in {}".format(exc, _folder(root)))
    port = _serve_port(db_path)
    print("Port {}: {}".format(port, "something answers - a start stops there (exit 3)" if answers_on(port)
                               else "nothing answers"))
    return 0


if __name__ == "__main__":
    _handed = hand_over()
    if _handed is None:
        _handed = serve_hand_over()
    if _handed is not None:
        sys.exit(_handed)

from . import (autoexport, backup, checks, db, export_csv, export_xlsx, intake,
               import_tracker, logs, mail_archive, master_info, migrate_master,
               migrate_xlsx, outlook_bridge, paths, propose, tracking, verify)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=str(paths.DB_PATH),
                        help="database file (default: {})".format(paths.DB_PATH))


def _add_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workbook", default=None,
                        help="source workbook, opened read only (default: the "
                             "master, or its copy in restore-points)")
    parser.add_argument("--publish", action="store_true",
                        help="write the real paths (workbooks\\ and data\\) for "
                             "this run, whatever setting export_target says. "
                             "Before the switchover this overwrites the "
                             "files the PowerShell pipeline reads.")
    parser.add_argument("--preview", action="store_true",
                        help="write the preview folder for this run")
    parser.add_argument("--out-dir", default=None,
                        help="override where the xlsx goes")
    parser.add_argument("--data-dir", default=None,
                        help="override where the CSV files go")


def _target(conn, args) -> str:
    """setting export_target, unless this run overrides it."""
    if getattr(args, "publish", False):
        return "publish"
    if getattr(args, "preview", False):
        return "preview"
    return autoexport.target(conn)


def _out_dirs(conn, args):
    out_dir, data_dir = autoexport.directories(_target(conn, args))
    if args.out_dir:
        out_dir = Path(args.out_dir)
    if args.data_dir:
        data_dir = Path(args.data_dir)
    return out_dir, data_dir


def _print(lines) -> None:
    for line in lines:
        print(line)


# ------------------------------------------------------------------ commands --
def cmd_init_db(args) -> int:
    notes = []
    conn = db.open_db(args.db, upgrade_notes=notes)
    version = db.get_setting(conn, "schema_version")
    print("database : {}".format(Path(args.db).resolve()))
    print("schema   : version {}".format(version))
    _print(notes)
    conn.close()
    return 0


def _require_workbook(path):
    """The workbook a command needs, or None with a sentence already printed."""
    workbook = Path(path) if path else paths.source_workbook()
    if workbook is None or not workbook.exists():
        print("Workbook not found: {}\nThe master was moved into "
              "restore-points\\ at the switchover (2026-09-09); pass "
              "--workbook to point at a copy.".format(workbook or "(none)"))
        return None
    return workbook


def cmd_migrate(args) -> int:
    workbook = _require_workbook(args.workbook)
    if workbook is None:
        return 1
    db_path = Path(args.db)
    existed = db_path.exists()
    notes = []
    conn = db.open_db(db_path, upgrade_notes=notes)
    _print(notes)
    if existed and not args.no_backup:
        target = backup.before_task(db_path, "migrate",
                                    restore_dir=args.restore_dir)
        print("restore point: {}".format(target))

    report = migrate_xlsx.migrate(conn, workbook=workbook,
                                  thread_map=args.thread_map,
                                  dedupe_report=args.dedupe_report)
    print("workbook : {}".format(workbook.resolve()))
    print("database : {}".format(db_path.resolve()))
    print("")
    _print(report.lines())
    conn.close()
    return 0


def cmd_export(args) -> int:
    conn = db.open_db(args.db)
    mode = _target(conn, args)
    out_dir, data_dir = _out_dirs(conn, args)
    months = args.month or None

    xlsx_path = export_xlsx.export(
        conn, out_dir / paths.EXPORT_WORKBOOK_NAME,
        db_path=Path(args.db).resolve(), months=months)
    opp_index = export_csv.export_opp_index(conn, data_dir / paths.OPP_INDEX_NAME)
    agents = export_csv.export_agents(conn, data_dir / paths.AGENTS_CSV_NAME)

    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("target   : {}".format(
        "PUBLISH - the live paths the PowerShell pipeline reads" if mode == "publish"
        else "preview only (setting export_target = preview; --publish overrides)"))
    for path in (xlsx_path, opp_index, agents):
        print("{}  {}  {} bytes".format(stamp, path, path.stat().st_size))

    share = paths.folder_setting(db.get_setting(conn, "export_share_dir", "")) if mode == "publish" else ""
    if share:
        try:
            import shutil
            target = Path(share) / xlsx_path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(xlsx_path, target)
            print("copied to share: {}".format(target))
        except OSError as exc:          # a missing share must not stop an export
            print("share copy failed (ignored): {}".format(exc))
    conn.close()
    return 0


def cmd_cargo_check(args) -> int:
    """The cases whose cargo numbers fail a check, or
    whose stored air weight differs from their lines or quantity text, to
    data\\cargo-check.csv. Read only."""
    from . import cargo_report

    conn = db.open_db(args.db)
    out = Path(args.out) if args.out else paths.DATA_DIR / "cargo-check.csv"
    found, by_check = cargo_report.write(conn, out)
    conn.close()
    print("database : {}".format(Path(args.db).resolve()))
    print("written  : {}".format(out))
    print("rows     : {} ({} case(s))".format(len(found), len({r["opp"] for r in found})))
    for check, n in by_check.most_common():
        print("  {:<12} {}".format(check, n))
    print("Nothing was changed; fix a case on its page.")
    return 0


def cmd_check(args) -> int:
    conn = db.open_db(args.db)
    findings = checks.run(conn)
    print("database : {}".format(Path(args.db).resolve()))
    print("")
    _print(checks.render(findings))
    conn.close()
    return 0


def cmd_verify(args) -> int:
    workbook = Path(args.workbook) if args.workbook else paths.source_workbook()
    if not args.workbook and (workbook is None or not workbook.exists()):
        # a demo database is verified against the demo data instead
        conn = db.open_db(args.db)
        try:
            if verify.demo_reference(conn) is not None:
                print("demo database: counts checked against the demo data (same seed and date)")
                result = verify.run(conn)
                _print(result.render())
                return 0 if result.ok else 1
        finally:
            conn.close()
    if workbook is None or not workbook.exists():
        print("No workbook to verify against; pass --workbook, or run it on a "
              "demo database (python -m opplist demo).")
        return 1
    conn = db.open_db(args.db)
    out_dir, data_dir = _out_dirs(conn, args)
    print("workbook : {}".format(workbook))
    result = verify.run(
        conn,
        workbook=workbook,
        export_path=out_dir / paths.EXPORT_WORKBOOK_NAME,
        csv_path=data_dir / paths.OPP_INDEX_NAME,
    )
    _print(result.render())
    conn.close()
    return 0 if result.ok else 1


def cmd_backup(args) -> int:
    db_path = Path(args.db)
    conn = db.open_db(db_path)
    keep = int(db.get_setting(conn, "backup_keep", "30"))
    conn.close()
    made = backup.daily(db_path, restore_dir=args.restore_dir)
    print("daily restore point: {}".format(made or "already exists for today"))
    removed = backup.prune(keep, restore_dir=args.restore_dir, db_path=db_path)
    print("pruned {} old restore point(s), keeping {}".format(len(removed), keep))
    return 0


def cmd_serve(args) -> int:
    from . import running
    from .web import create_app

    if getattr(args, "where", False):            # OP2: read only, changes nothing
        return serve_where(["serve", "--db", str(args.db)], paths.PROJECT_DIR)

    # OP2: which code this is, before the server's own lines (a fallback's
    # reason, or the commit run from JoJo\opplist-serve)
    logs.setup()
    for level, line in running.start_lines():
        print(line)
        logs.get("serve").log(level, line)

    # FX3: one OppList at a time - Windows lets a second server bind the same
    # 127.0.0.1 port beside the first, and both then push the team site.
    # Checked before the database is opened: nothing upgraded, backed up,
    # synced or pushed. (OP2: the hand-over makes the same check first.)
    port = _serve_port(args.db)
    if answers_on(port):
        print(already_running_line(port))
        logs.get("serve").warning("serve refused: something already answers on 127.0.0.1:%s", port)
        return EXIT_ALREADY_RUNNING

    notes = []
    conn = db.open_db(args.db, upgrade_notes=notes)
    _print(notes)
    port = int(db.get_setting(conn, "port", "8765"))
    keep = int(db.get_setting(conn, "backup_keep", "30"))
    mode = autoexport.target(conn)
    conn.close()

    # One restore point a day, taken at startup.
    if Path(args.db).exists():
        try:
            made = backup.daily(args.db, restore_dir=args.restore_dir)
            if made:
                print("restore point: {}".format(made))
            removed = backup.prune(keep, restore_dir=args.restore_dir, db_path=args.db)
            if removed:
                print("pruned {} old restore point(s)".format(len(removed)))
        except OSError as exc:
            print("backup skipped: {}".format(exc))

    # OP1: the Sea-Air cases' totals, once, after today's restore point
    try:
        from . import seaair_resync
        conn = db.open_db(args.db)
        try:
            for line in seaair_resync.run_once(conn):
                print(line)
                logs.get("serve").info(line)
        finally:
            conn.close()
    except Exception as exc:                  # never stop the start over it
        logs.get("serve").exception("Sea-Air re-sync")
        print("Sea-Air re-sync failed: {}".format(exc))

    # FX3: once, after today's restore point - the team site's closes
    # accepted before FX3 made the site's on the Timeline; shipments
    # switched off (their pending suggestions dismissed, a CSV copy)
    from . import shipments_switch, site_close
    for name, once in (("team site closes", site_close.repair_once),
                       ("shipments switched off", shipments_switch.switch_off_once)):
        try:
            conn = db.open_db(args.db)
            try:
                for line in once(conn):
                    print(line)
                    logs.get("serve").info(line)
            finally:
                conn.close()
        except Exception as exc:              # never stop the start over it
            logs.get("serve").exception(name)
            print("{} failed: {}".format(name, exc))

    app = create_app(args.db)
    # W1a: the team site - a push a few seconds after the start, then every
    # five minutes (only over the live database with the key in place)
    app.firebase_syncer.start()
    print("export target: {}".format(mode))
    print("OppList on http://127.0.0.1:{}   (Ctrl+C to stop)".format(port))
    # 127.0.0.1 only: single user, no login, nothing on the network.
    app.run(host="127.0.0.1", port=port, debug=args.debug, use_reloader=False)
    return 0


def cmd_firebase_sync(args) -> int:
    """W1a: python -m opplist firebase-sync - one sync with the team site now
    (W1b: what people did there taken in first, then the push)
    (or --status: what it would push, and the last run). --scheduled (the
    08:15 task's fourth action) goes through scheduled.guard as every
    scheduled command does; not set up yet is one line and exit 0."""
    from . import firebase_sync
    logs.setup()
    conn = db.open_db(args.db)
    try:
        ok, why = firebase_sync.configured(conn)
        if not ok and not args.status:
            # not set up yet: one line, and nothing failed (the 08:15 run says so and goes on)
            print("team site: not set up - {}".format(why))
            return 0
        print("project  : {}".format(firebase_sync.project(conn)))
        print("store    : {}".format(firebase_sync.store_dir(conn)))
        print("set up   : {}".format("yes" if ok else "no, " + why))
        last = firebase_sync.status()
        print("last     : {}".format("{} {}, {} written{}".format(
            last["at"], "ok" if last["ok"] else "failed", last["written"],
            "" if last["ok"] else " - " + last["error"]) if last else "never"))
        if last.get("pull"):
            print("taken in : {}".format(last["pull"].replace("team site: ", "")))
        # Q2c: the Rate Card's line, as Settings shows it
        from . import ratecard_sync
        print("rate card: {}".format(ratecard_sync.status_line(conn)))
        if args.status:
            built = firebase_sync.build(conn)
            print("would push: {} inquiries, {} waiting, {} pending on us, {} cases".format(
                len(built.inquiries), len(built.assign["waiting"]), len(built.assign["pending"]), len(built.cases)))
            return 0
    finally:
        conn.close()
    if getattr(args, "scheduled", False):
        # FX3: the open app pushes every five minutes; the 08:15 step does
        # not push again right after it (done, with the reason)
        recent = firebase_sync.app_pushed_within()
        if recent:
            args.done_reason = "the app pushed at {}, within {} minutes; push left to it".format(
                recent[11:16], firebase_sync.APP_RECENT_MINUTES)
            print("team site: " + args.done_reason)
            return 0
    result = firebase_sync.sync(args.db)
    _print(result.lines())
    if result.paused:
        # FX3: not sent today; the banner says so, the next day goes again
        args.done_reason = result.reason
        return 0
    return 0 if result.ok else 1


def cmd_read_quote(args) -> int:
    """Q2d: python -m opplist read-quote --status - Read quote's lines as
    Settings shows them (on or off, the key there or not, the model, this
    month's spend against the cap, the last error, the report)."""
    from . import read_quote
    conn = db.open_db(args.db)
    try:
        _print(read_quote.status_lines(conn))
    finally:
        conn.close()
    return 0


def cmd_read_quote_report(args) -> int:
    """Q2d: python -m opplist read-quote-report [--days 7] - the week's
    readings and what people changed on saving, to ..\\data\\reports."""
    from . import read_quote
    logs.setup()
    conn = db.open_db(args.db)
    try:
        path = read_quote.write_report(conn, args.days, args.today)
    finally:
        conn.close()
    print("read quote report: {}".format(path))
    return 0


def cmd_read_quote_trial(args) -> int:
    """Q2d: python -m opplist read-quote-trial [--limit 10] [--since
    2026-10-01] - the newest replies with prices read as a trial (counted
    in the month's spend, never pushed), written to ..\\data\\reports.
    --pick-only lists the mails it would read and calls nothing. RQ1:
    --again <reading ids> reads those readings' mails again with today's
    prompt, each beside the earlier reading."""
    from . import case_mail, read_quote
    logs.setup()
    conn = db.open_db(args.db)
    search, info = case_mail.open_search()
    try:
        if search is None:
            print("read quote trial: no mail archive ({})".format(info.get("reason") or "not there"))
            return 1
        if args.pick_only:
            for m in read_quote.trial_picks(conn, search, args.limit, args.since):
                print("{} | {} | {} | {} | {} | {}".format(
                    m["case"]["opp_no"], m["request"]["vendor_key"], m["mode"] or "-", m["sent"], m["subject"],
                    ", ".join(m["files"]) or "no files"))
            return 0
        has_key, why = read_quote.key_state(conn)
        if not has_key:
            print("read quote trial: {}".format(why))
            return 1
        if args.again:
            path = read_quote.again(conn, search, args.again, db_path=args.db)
        else:
            path = read_quote.trial(conn, search, args.limit, args.since, db_path=args.db)
    finally:
        if search is not None:
            search.__exit__(None, None, None)
        conn.close()
    print("read quote trial: {}".format(path))
    return 0


RETIRED_DRAFTS = "draft-followups: retired 2026-11-04 (batch CX1, no Claude)"


def cmd_draft_followups(args) -> int:
    """F2's morning drafts, retired with every call to Claude (CX1): one line
    printed and logged, exit 0 - the scheduled task still calls it."""
    logs.get("scheduled").info(RETIRED_DRAFTS)
    print(RETIRED_DRAFTS)
    return 0


def cmd_tracking(args) -> int:
    """Print the tracking list. Reads only."""
    logs.setup()
    conn = db.open_db(args.db)
    try:
        sections = tracking.run(conn, today=args.today)
    finally:
        conn.close()
    _print(tracking.render(sections, limit=args.limit))
    return 0


def cmd_propose_status(args) -> int:
    """Read Outlook and suggest a status for cases nobody confirmed. No writes."""
    logs.setup()
    conn = db.open_db(args.db)
    try:
        opp_nos = [o.strip().upper() for o in (args.opp or "").split(",")
                   if o.strip()]
        month = None if opp_nos else args.month
        scope = "open" if args.open else "unconfirmed"
        if not opp_nos and not month and not args.open:
            print("Say which month (--month 2026-08), --open for every open "
                  "case, or which case (--opp OPP-1307).")
            return 2
        started = [time.time()]

        def tick(proposal):
            now = time.time()
            print("  {:<10} {:<9} {:<7} {:>5.1f}s  {}".format(
                proposal.opp_no, proposal.proposed_status, proposal.confidence,
                now - started[0], proposal.found_by or proposal.note),
                flush=True)
            started[0] = now

        source = getattr(args, "source", "archive") or "archive"
        if source == "archive":
            ok, why = mail_archive.usable()
            if not ok:
                # Not a fallback to Outlook: a live scan over the whole
                # mailbox is what failed on 2026-09-14. Sync first.
                print("WARNING: {}.".format(why))
                print("Nothing was read. Run: python -m opplist mail-sync   "
                      "(or pass --source outlook to read Outlook directly)")
                return 2
            info = mail_archive.status()
            print("source   : mail archive ({} mails, synced {})".format(
                info["count"], info["last_sync_at"]))
        try:
            if source == "archive":
                with mail_archive.ArchiveSearch() as search:
                    proposals = propose.propose(
                        conn, search, month=month, opp_nos=opp_nos,
                        scope=scope, after_current=args.open,
                        progress=tick if args.progress else None)
            else:
                print("source   : Outlook (live)")
                with outlook_bridge.Search() as search:
                    proposals = propose.propose(
                        conn, search, month=month, opp_nos=opp_nos,
                        scope=scope, after_current=args.open,
                        progress=tick if args.progress else None)
        except outlook_bridge.OutlookError as exc:
            print(exc.message)
            return 1
        except mail_archive.ArchiveError as exc:
            print(str(exc))
            return 1
        if not proposals:
            print("No cases to look at.")
            return 0
        _print(propose.render(proposals, limit=args.limit))
        requoted = []
        if args.open and source == "archive":
            # our revised quotes to the requester, after the last quote
            # (2026-09-25): listed on a dry run, written below
            from . import detect_followups
            with mail_archive.ArchiveSearch() as search:
                requoted = detect_followups.requoted_suggestions(conn, search, opp_nos=opp_nos)
            print("")
            print("re-quoted (our revised quote to the requester, after the last quote): {}".format(len(requoted)))
            for r in requoted:
                print("  {:<10} Re-quoted {}  (last quote {})  {}  \"{}\"".format(
                    r.opp_no, r.at, r.last_quoted, r.sender[:40], r.quote[:70]))
        if getattr(args, "write_suggestions", False):
            from . import suggestions
            if args.open and not opp_nos:
                # SG1: what nobody decided in suggestion_expire_days goes
                # first, so its mail's held-back note is written below
                gone = suggestions.expire(db.Repository(conn, source="scan"))
                print("suggestions expired (pending {}+ days): {}".format(suggestions.expire_days(conn), len(gone)))
            counts = suggestions.from_proposals(db.Repository(conn, source="scan"), proposals)
            print("")
            print("suggestions: {} written, {} already there, {} keep the status"
                  .format(counts["written"], counts["same"], counts["unchanged"]))
            if requoted:
                from . import detect_followups
                made = detect_followups.record_requoted(db.Repository(conn, source="scan"), requoted)
                print("re-quoted suggestions: {} written, {} already there".format(made["written"], made["same"]))
            if args.open and source == "archive":
                # L1: a reply after a 'No response from customer' closing
                # (the last 90 days) suggests reopening the case as Quoted
                from . import silent
                with mail_archive.ArchiveSearch() as search:
                    reopened = silent.reopen_suggestions(conn, search, repo=db.Repository(conn, source="scan"))
                print("")
                print("reopen after a No-response closing: {}".format(len(reopened)))
                for line in reopened:
                    print("  " + line)
            if args.open and source == "archive":
                # the same daily run also records the follow-ups we sent and
                # the replies that came back
                from . import detect_followups
                with mail_archive.ArchiveSearch() as search:
                    report = detect_followups.run(conn, search)
                print("")
                print("follow-ups / replies from the archive:")
                _print(report.summary())
            if args.open and source == "archive":
                # FX5: a later mail from the other side saying the cargo
                # cannot be stacked suggests the Non-stackable tag
                with mail_archive.ArchiveSearch() as search:
                    tagged = suggestions.scan_non_stackable(db.Repository(conn, source="scan"), search,
                                                            opp_nos=opp_nos)
                print("")
                print("Non-stackable suggested (a later mail says so): {}".format(len(tagged)))
                for opp, sentence, sender, day in tagged:
                    print("  {:<10} {}  {}  \"{}\"".format(opp, day, sender[:40], sentence[:70]))
        target = args.out or (paths.DATA_DIR / "proposed-status-{}.csv".format(
            "open" if args.open else (month or "cases")))
        written = propose.write_csv(proposals, target)
        print("")
        print("wrote {}".format(written))
        if Path(written) != Path(target):
            print("({} was locked - close it in Excel and the next run will "
                  "use it again)".format(target))
        print("Nothing was written to the database. Delete the rows you "
              "disagree with, then: python -m opplist apply-status "
              "\"{}\"".format(written))
    finally:
        conn.close()
    return 0


def cmd_apply_status(args) -> int:
    """Apply the rows still in a proposal CSV."""
    logs.setup()
    rows = propose.read_csv(args.csv)
    if not rows:
        print("Nothing in {}.".format(args.csv))
        return 1
    db_path = Path(args.db)
    if db_path.exists():
        target = backup.before_task(db_path, "apply-status",
                                    restore_dir=args.restore_dir)
        print("restore point: {}".format(target))
    conn = db.open_db(db_path)
    repo = db.Repository(conn, source="scan")
    try:
        applied, skipped = propose.apply(repo, rows)
        repo.commit()
    finally:
        conn.close()
    for opp_no, status, at in applied:
        print("  {} -> {} ({})".format(opp_no, status, at))
    for opp_no, why in skipped:
        print("  {} skipped: {}".format(opp_no or "(no case)", why))
    print("applied {} of {} row(s)".format(len(applied), len(rows)))
    return 0


def cmd_reader_check(args) -> int:
    """RG1: the rule-based reader on the base cases' registered mails from
    the mail archive, against the cases today; read-only on both databases."""
    import sqlite3
    from . import mail_archive, reader_check
    logs.setup()
    months = [m.strip() for m in (args.months or "").split(",") if m.strip()] or list(reader_check.MONTHS)
    conn = sqlite3.connect("file:{}?mode=ro".format(Path(args.db).resolve().as_posix()), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        with mail_archive.ArchiveSearch() as search:
            result = reader_check.check(conn, search, months)
    except mail_archive.ArchiveError as exc:
        print(str(exc))
        return 1
    finally:
        conn.close()
    before = reader_check.load(args.compare) if args.compare else None
    path = reader_check.write_report(result, before, compared_with=Path(args.compare).name if args.compare else "")
    for month, fields in reader_check.counts(result).items():
        r = fields["requester"]
        print("{}: requester right {} of {}".format(month, r["right"], r["value"]))
    if before is not None:
        worse, better = reader_check.changes(before, result)
        print("right -> not right: {}   not right -> right: {}".format(len(worse), len(better)))
    print("report: {}".format(path))
    return 0


def cmd_case_number_mismatch(args) -> int:
    """FX7 item 7, measuring only: the archived mails whose one case number
    fits while their conversation and subject are none of that case's;
    read-only on both databases, the list in docs\\reports."""
    import datetime as _dt
    import sqlite3
    from collections import Counter
    from . import case_numbers, mail_archive, paths
    logs.setup()
    conn = sqlite3.connect("file:{}?mode=ro".format(Path(args.db).resolve().as_posix()), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        with mail_archive.ArchiveSearch() as search:
            rows = case_numbers.mismatches(conn, search)
    except mail_archive.ArchiveError as exc:
        print(str(exc))
        return 1
    finally:
        conn.close()
    out = Path(args.out) if args.out else \
        paths.REPORTS_DIR / "case-number-mismatch-{}.csv".format(_dt.date.today().isoformat())
    case_numbers.write_mismatches(rows, out)
    per_case = Counter(r["case"] for r in rows)
    print("mails: {} on {} case(s)".format(len(rows), len(per_case)))
    for case, n in sorted(per_case.items()):
        print("  {}: {}".format(case, n))
    print("list: {}".format(out))
    return 0


def cmd_outlook_selftest(args) -> int:
    """Print what Register from Outlook would pre-fill. Writes nothing."""
    logs.setup()
    try:
        if getattr(args, "fixture", None):
            mails = [outlook_bridge.read_fixture(args.fixture)]
        else:
            mails = (outlook_bridge.read_selection_all() if args.all
                     else [outlook_bridge.read_selection()])
    except outlook_bridge.OutlookError as exc:
        print(exc.message)
        if exc.detail and args.verbose:
            print("  ({})".format(exc.detail))
        return 1

    conn = db.open_db(args.db)
    try:
        drafts = [intake.build(conn, mail) for mail in mails]
    finally:
        conn.close()
    if args.all:
        _print(intake.summarise(drafts, selection_count=mails[0].selection_count))
    else:
        _print(intake.describe(drafts[0]))
    return 0


def cmd_import_tracker(args) -> int:
    """Load rate-request-data.csv into vendor_request."""
    logs.setup()
    db_path = Path(args.db)
    if db_path.exists() and not args.no_backup:
        target = backup.before_task(db_path, "import-tracker",
                                    restore_dir=args.restore_dir)
        print("restore point: {}".format(target))
    conn = db.open_db(db_path)
    try:
        report = import_tracker.run(conn, csv_path=args.csv,
                                    thread_map=args.thread_map)
    finally:
        conn.close()
    print("tracker  : {}".format(Path(args.csv or paths.RATE_REQUEST_DATA)))
    print("database : {}".format(db_path.resolve()))
    print("")
    _print(report.lines())
    return 0


def cmd_agent_domains(args) -> int:
    """Attach the agent directory's e-mail domains to the parties they belong
    to (party_domain rows only; nothing else changes). Restore point first."""
    from . import import_agents
    logs.setup()
    db_path = Path(args.db)
    if db_path.exists() and not args.no_backup:
        target = backup.before_task(db_path, "agent-domains",
                                    restore_dir=args.restore_dir)
        print("restore point: {}".format(target))
    conn = db.open_db(db_path)
    try:
        report = import_agents.add_domains(conn, workbook=args.workbook)
    finally:
        conn.close()
    print("workbook : {}".format(Path(args.workbook or paths.AGENT_DIRECTORY_WORKBOOK)))
    print("database : {}".format(db_path.resolve()))
    print("")
    _print(report.lines())
    return 0


def cmd_agent_rate_requests(args) -> int:
    """FX4: the Agent platform's rate-request counts, written to
    ..\\data\\outreach-import\\rate-requests.json (the file replaced is copied
    into _backup first). Read only on the database; not on the schedule."""
    from . import agent_rate_requests
    result = agent_rate_requests.run(Path(args.db), today=args.today, folder=args.out)
    if result.backup is not None:
        print("kept      : {}".format(result.backup))
    print("written   : {}".format(result.path))
    print("database  : {} (read only)".format(Path(args.db).resolve()))
    print("")
    _print(result.lines)
    return 0


def cmd_followup(args) -> int:
    """Print the follow-up text for one case, the same text the
    case page shows under Follow-ups."""
    from . import followup
    conn = db.open_db(args.db)
    try:
        text = followup.build_for_opp(conn, args.opp_no, seq=args.step, archive_path=args.archive,
                                      variant=args.variant)
    except ValueError as exc:
        print(str(exc))
        return 1
    finally:
        conn.close()
    print(text, end="")
    return 0


def cmd_detect_followups(args) -> int:
    """Record the follow-ups we sent and the requester's replies, read out
    of the mail archive. --dry-run only lists them."""
    from . import detect_followups
    logs.setup()
    if getattr(args, "reclassify", False):
        # read the replies already on the timeline again, with today's rule
        db_path = Path(args.db)
        if not args.dry_run and db_path.exists():
            print("restore point: {}".format(
                backup.before_task(db_path, "reclassify-replies")))
        conn = db.open_db(db_path)
        try:
            if getattr(args, "from_archive", False):
                # AL6a: each reply read again from its whole mail
                with mail_archive.ArchiveSearch(args.archive) as search:
                    report = detect_followups.reclassify(conn, dry_run=args.dry_run, search=search)
            else:
                report = detect_followups.reclassify(conn, dry_run=args.dry_run)
        except mail_archive.ArchiveError as exc:
            print(str(exc))
            return 1
        finally:
            conn.close()
        _print(report.summary())
        return 0
    conn = db.open_db(args.db)
    try:
        opp_nos = [o.strip() for o in (args.opp or "").split(",") if o.strip()]
        with mail_archive.ArchiveSearch(args.archive) as search:
            report = detect_followups.run(conn, search, dry_run=args.dry_run, opp_nos=opp_nos)
    except mail_archive.ArchiveError as exc:
        print(str(exc))
        return 1
    finally:
        conn.close()
    _print(report.summary())
    return 0


def cmd_fix_locations(args) -> int:
    """Add the gateways and ports the master list is missing, and repair the
    rows a mail subject created by accident."""
    from . import fix_locations
    logs.setup()
    db_path = Path(args.db)
    if args.apply and db_path.exists() and not args.no_backup:
        target = backup.before_task(db_path, "fix-locations", restore_dir=args.restore_dir)
        print("restore point: {}".format(target))
    conn = db.open_db(db_path)
    try:
        report = fix_locations.run(conn, dry_run=not args.apply)
    except ValueError as exc:
        print(str(exc))
        return 1
    finally:
        conn.close()
    print("database : {}".format(db_path.resolve()))
    print("")
    _print(report.lines())
    return 0


def cmd_fix_person_places(args) -> int:
    """Retire the person read as a place and complete Hamilton (2026-09-23)."""
    from . import fix_person_places
    logs.setup()
    db_path = Path(args.db)
    if args.apply and db_path.exists() and not args.no_backup:
        target = backup.before_task(db_path, "fix-person-places", restore_dir=args.restore_dir)
        print("restore point: {}".format(target))
    conn = db.open_db(db_path)
    try:
        report = fix_person_places.run(conn, dry_run=not args.apply)
    except ValueError as exc:
        print(str(exc))
        return 1
    finally:
        conn.close()
    print("database : {}".format(db_path.resolve()))
    print("")
    _print(report.lines())
    return 0


def cmd_fix_closed_reasons(args) -> int:
    """Put the closed reasons on the two vocabularies - Lost (a shipment we
    did not get) and Dropped (the row should not exist)."""
    from . import fix_closed_reasons
    logs.setup()
    db_path = Path(args.db)
    if args.apply and db_path.exists() and not args.no_backup:
        target = backup.before_task(db_path, "fix-closed-reasons",
                                    restore_dir=args.restore_dir)
        print("restore point: {}".format(target))
    conn = db.open_db(db_path)
    try:
        report = fix_closed_reasons.run(conn, dry_run=not args.apply)
    except ValueError as exc:
        print(str(exc))
        return 1
    finally:
        conn.close()
    print("database : {}".format(db_path.resolve()))
    print("")
    _print(report.lines())
    return 0


def cmd_fill_subjects(args) -> int:
    """Batch M1: cases with a thread key and no registered subject get the
    subject of the earliest archived mail with that key (fill_subjects).
    Dry run by default; --apply takes a restore point first."""
    from . import fill_subjects, mail_archive
    logs.setup()
    db_path = Path(args.db)
    if args.apply and db_path.exists():
        target = backup.before_task(db_path, "fill-subjects", restore_dir=args.restore_dir)
        print("restore point: {}".format(target))
    conn = db.open_db(db_path)
    try:
        try:
            found = fill_subjects.propose(conn, archive_path=args.archive)
        except mail_archive.ArchiveError as exc:
            print(str(exc))
            return 1
        ready = [f for f in found if not f.why]
        print("cases with a thread key and no subject: {}; a subject found for {}, left alone {}".format(
            len(found), len(ready), len(found) - len(ready)))
        for line in fill_subjects.lines(found):
            print(line)
        if args.apply:
            repo = db.Repository(conn, source=fill_subjects.SOURCE)
            written = fill_subjects.apply(repo, found)
            repo.commit()
            print("subjects written: {}".format(written))
        else:
            print("dry run: nothing written (--apply writes them, restore point first)")
    finally:
        conn.close()
    return 0


def cmd_fill_coordinates(args) -> int:
    """master_location.lat / lon from the UN/LOCODE code list and
    OurAirports. Dry run writes data\\coordinates-proposed.csv;
    --apply takes a restore point and writes only empty coordinates."""
    from . import coordinates
    logs.setup()
    db_path = Path(args.db)
    unlocode_rows = coordinates.read_unlocode(args.unlocode)
    airport_rows = coordinates.read_airports(args.airports)
    print("UN/LOCODE rows: {} ({} with coordinates); airports: {}".format(
        len(unlocode_rows), sum(1 for r in unlocode_rows if r["lat"] is not None), len(airport_rows)))
    manual_path = Path(args.manual) if args.manual else paths.DATA_DIR / "coordinates-manual.csv"
    manual = coordinates.read_manual(manual_path) if manual_path.exists() else []
    print("manual rows: {} ({})".format(len(manual), manual_path if manual else "no file"))
    if args.apply and db_path.exists() and not args.no_backup:
        target = backup.before_task(db_path, "fill-coordinates", restore_dir=args.restore_dir)
        print("restore point: {}".format(target))
    conn = db.open_db(db_path)
    try:
        proposals = coordinates.propose(conn, unlocode_rows, airport_rows, manual=manual,
                                        manual_wins=args.manual_wins)
        found = coordinates.counts(proposals)
        print("locations without coordinates: {}".format(
            sum(1 for p in proposals if p.source not in ("unknown-key", "retired-key", "kept"))))
        print("manual {manual} (kept as set {kept}, unknown key {uk}, retired key {rk}); matched by unlocode "
              "{unlocode}, by iata {iata}, by name {name}, by the town's airport {ca}; ambiguous {ambiguous}; "
              "unmatched {unmatched}".format(ca=found["city-airport"], uk=found["unknown-key"],
                                             rk=found["retired-key"], **found))
        for p in proposals:
            if p.source in ("unknown-key", "retired-key", "kept"):
                print("  {:<12} {:<24} {}".format(p.source, p.key, p.matched or p.note))
        for p in proposals:
            if p.source == "ambiguous":
                print("  ambiguous  {:<24} {}".format(p.key, " | ".join(p.candidates)[:140]))
        unmatched = [p.key for p in proposals if p.source == "unmatched"]
        if unmatched:
            print("  unmatched  " + ", ".join(unmatched))
        if args.apply:
            repo = db.Repository(conn, source="fill-coordinates")
            written = coordinates.apply(repo, proposals)
            repo.commit()
            print("coordinates written: {}".format(written))
        else:
            out = coordinates.write_csv(proposals, Path(args.out) if args.out else paths.DATA_DIR / "coordinates-proposed.csv")
            print("proposals written to {}".format(out))
    finally:
        conn.close()
    return 0


def cmd_propose_shipments(args) -> int:
    """The fourth step of the 08:15 chain: for every active
    shipment, the archive mails of its case's threads newer than the
    shipment's last event, one pending suggestion per mail at most; and
    (5d) a Start shipment suggestion for a won JJC case with no shipment
    whose threads show the cargo moving."""
    from . import shipments, shipments_switch
    logs.setup()
    conn = db.open_db(args.db)
    try:
        if not shipments_switch.enabled(conn):
            # FX3: switched off - one line, and the morning run records it as done
            print(shipments_switch.OFF_LINE)
            args.done_reason = shipments_switch.OFF_LINE
            return 0
    finally:
        conn.close()
    ok, why = mail_archive.usable()
    if not ok:
        print("WARNING: {}. Nothing was read. Run: python -m opplist mail-sync".format(why))
        return 2
    conn = db.open_db(args.db)
    try:
        with mail_archive.ArchiveSearch() as search:
            report = shipments.propose(conn, search, dry_run=args.dry_run)
    finally:
        conn.close()
    print("{}: {} active shipment(s), {} mail(s) newer than the last event, {} already proposed".format(
        "dry run" if args.dry_run else "run", report["shipments"], report["mails"], report["skipped"]))
    for shipment_id, title, proposal in report["proposals"]:
        print("  #{:<4} {:<32} {:<9} {:<28} {}  {:<6} {}".format(
            shipment_id, title[:32], proposal.kind, proposal.name[:28], proposal.at, proposal.confidence,
            proposal.quote[:70]))
    starts = report.get("starts") or {}
    print("Start shipment: {} won JJC case(s) with no shipment, {} already suggested or dismissed, "
          "{} with no milestone mail since the Won date, {} with no Won date".format(
              starts.get("cases", 0), starts.get("asked", 0), starts.get("no_mail", 0), starts.get("no_date", 0)))
    for case, won, mail, proposal in starts.get("proposals", []):
        print("  {}  won {}  {:<10} {}  {:<6} {} | {}".format(
            case["opp_no"], won, proposal.name, proposal.at, proposal.confidence,
            (mail.subject or "")[:50], proposal.quote[:60]))
    if not args.dry_run:
        print("suggestions written: {} (Start shipment: {})".format(report["written"], starts.get("written", 0)))
    return 0


def cmd_hs_codes(args) -> int:
    """D7a (v50): the HS codes in cargo lines' commodity text moved into
    hs_code. Dry run writes data\\proposed-hs-codes.csv; --apply takes a
    restore point and moves them through the repository."""
    from . import hs_codes
    logs.setup()
    db_path = Path(args.db)
    if args.apply and db_path.exists() and not args.no_backup:
        target = backup.before_task(db_path, "hs-codes", restore_dir=args.restore_dir)
        print("restore point: {}".format(target))
    conn = db.open_db(db_path)
    try:
        report = hs_codes.find(conn)
        if args.apply:
            repo = db.Repository(conn, source="hs-codes")
            hs_codes.apply(repo, report)
            repo.commit()
        else:
            out = hs_codes.write_csv(report, Path(args.out) if args.out else paths.DATA_DIR / "proposed-hs-codes.csv")
            print("proposals written to {}".format(out))
    finally:
        conn.close()
    print("database : {}".format(db_path.resolve()))
    _print(hs_codes.lines(report, dry_run=not args.apply))
    return 0


def cmd_official_places(args) -> int:
    """D7a: the official code lists for the team site's place picker
    (INTERFACE.md section 8, official/), from the UN/LOCODE release and
    OurAirports' airports.csv. Dry run prints; --apply writes the files and
    keeps the two paths in Settings."""
    from . import official_places
    logs.setup()
    built = official_places.run(args.unlocode, args.airports)
    if args.release:
        built.version = args.release
    out_dir = Path(args.out_dir) if args.out_dir else official_places.OUT_DIR
    if args.apply:
        sizes = official_places.write(built, out_dir)
        conn = db.open_db(args.db)
        try:
            official_places.remember(conn, args.unlocode, args.airports)
        finally:
            conn.close()
    else:
        sizes = official_places.sizes_of(built)
    _print(official_places.lines(built, sizes, written=args.apply, out_dir=out_dir))
    return 0


def cmd_fix_vendor_quoted(args) -> int:
    """One vendor status, not two (2026-09-25): line the rows
    on file up with the derived quoted flag."""
    from . import fix_vendor_quoted
    logs.setup()
    db_path = Path(args.db)
    if args.apply and db_path.exists() and not args.no_backup:
        target = backup.before_task(db_path, "fix-vendor-quoted",
                                    restore_dir=args.restore_dir)
        print("restore point: {}".format(target))
    conn = db.open_db(db_path)
    try:
        report = fix_vendor_quoted.run(conn, dry_run=not args.apply)
    finally:
        conn.close()
    print("database : {}".format(db_path.resolve()))
    print("")
    _print(report.lines())
    return 0


def cmd_repair_fu_templates(args) -> int:
    """The FU templates back on the pool (2026-09-25): an old default saved
    back with CRLF is set to empty. Dry run by default."""
    from . import repair_fu_templates
    logs.setup()
    db_path = Path(args.db)
    if args.apply and db_path.exists() and not args.no_backup:
        target = backup.before_task(db_path, "repair-fu-templates",
                                    restore_dir=args.restore_dir)
        print("restore point: {}".format(target))
    conn = db.open_db(db_path)
    try:
        report = repair_fu_templates.run(conn, dry_run=not args.apply)
    finally:
        conn.close()
    print("database : {}".format(db_path.resolve()))
    print("")
    _print(report.lines())
    return 0


def cmd_retire_sourcing(args) -> int:
    """Sourcing retired (2026-09-24): list the cases still on it and the
    pending suggestions proposing it; --apply moves the cases to New and
    dismisses the suggestions, after a restore point."""
    from . import retire_sourcing
    logs.setup()
    db_path = Path(args.db)
    if args.apply and db_path.exists() and not args.no_backup:
        target = backup.before_task(db_path, "retire-sourcing",
                                    restore_dir=args.restore_dir)
        print("restore point: {}".format(target))
    conn = db.open_db(db_path)
    try:
        report = retire_sourcing.run(conn, dry_run=not args.apply)
    finally:
        conn.close()
    print("database : {}".format(db_path.resolve()))
    print("")
    _print(report.lines())
    return 0


def cmd_direct_customers_out(args) -> int:
    """SC1: read only - the cases the reports leave out and why (a direct
    customer before October; from October a direct customer's or an
    office's case that is not LATAM). --apply is refused: scope is not
    the report's any more."""
    from . import direct_customers_out
    logs.setup()
    if args.apply:
        print(direct_customers_out.NO_APPLY)
        return 2
    db_path = Path(args.db)
    conn = db.open_db(db_path)
    try:
        report = direct_customers_out.run(conn)
    finally:
        conn.close()
    print("database : {}".format(db_path.resolve()))
    print("")
    _print(report.lines())
    return 0


def cmd_scope_back(args) -> int:
    """SC1: every Out case the rules set (not her) back to In; dry run by
    default, --apply writes after a restore point."""
    from . import scope_back
    logs.setup()
    db_path = Path(args.db)
    if args.apply and db_path.exists() and not args.no_backup:
        target = backup.before_task(db_path, "scope-back", restore_dir=args.restore_dir)
        print("restore point: {}".format(target))
    conn = db.open_db(db_path)
    try:
        report = scope_back.run(conn, dry_run=not args.apply)
    finally:
        conn.close()
    print("database : {}".format(db_path.resolve()))
    print("")
    _print(report.lines())
    return 0


def cmd_rekey_threads(args) -> int:
    """MF1, TK2: the stored thread keys the subject rules change (a leading
    reference that leaves no letter, or common words only, stays). Listed
    by default, --apply writes after a restore point."""
    from . import rekey_threads
    logs.setup()
    db_path = Path(args.db)
    if args.apply and db_path.exists() and not args.no_backup:
        target = backup.before_task(db_path, "rekey-threads", restore_dir=args.restore_dir)
        print("restore point: {}".format(target))
    conn = db.open_db(db_path)
    try:
        report = rekey_threads.run(conn, dry_run=not args.apply)
    finally:
        conn.close()
    print("database : {}".format(db_path.resolve()))
    print("")
    _print(report.lines())
    return 0


def cmd_withdraw_requote_suggestions(args) -> int:
    """The retired 'they asked for the rate again' suggestions still
    pending: listed; --apply dismisses them with a note after a restore
    point (2026-09-25)."""
    from . import withdraw_requote
    logs.setup()
    db_path = Path(args.db)
    if args.apply and db_path.exists() and not args.no_backup:
        target = backup.before_task(db_path, "withdraw-requote-suggestions",
                                    restore_dir=args.restore_dir)
        print("restore point: {}".format(target))
    conn = db.open_db(db_path)
    try:
        report = withdraw_requote.run(conn, dry_run=not args.apply)
    finally:
        conn.close()
    print("database : {}".format(db_path.resolve()))
    print("")
    _print(report.lines())
    return 0


def cmd_migrate_cargo_lines(args) -> int:
    """Cargo is lines only (batch 15b): every case with typed numbers and no
    line gets one. Tried on a copy first and compared (numbers and exports);
    --apply writes only when nothing differs, after a restore point."""
    from . import cargo_lines_migration
    logs.setup()
    db_path = Path(args.db)
    report = cargo_lines_migration.run(db_path, write=args.apply, restore_dir=args.restore_dir)
    print("database : {}".format(db_path.resolve()))
    print("")
    _print(report.lines())
    if not report.ok:
        print("")
        print("Nothing was written: the numbers or the exports would change.")
        return 1
    return 0


def cmd_mfn_link(args) -> int:
    """WC1: Agent parties against the MFN directory. The dry run writes the
    proposal and the summary; --apply writes after a restore point."""
    from . import mfn_link
    logs.setup()
    db_path = Path(args.db)
    conn = db.open_db(db_path)
    try:
        try:
            plan = mfn_link.run(conn, directory_dir=args.dir, out_dir=args.out_dir,
                                reports_dir=args.reports_dir)
        except FileNotFoundError as exc:
            print(str(exc))
            return 1
        print("directory : {} ({} offices in {} countries, files of {})".format(
            plan.directory.folder, len(plan.directory.offices), len(plan.directory.codes),
            plan.directory.date))
        print("database  : {}".format(db_path.resolve()))
        print("proposed  : {}".format(plan.proposed_csv))
        print("decisions : {}{}".format(plan.decisions_csv, " (created, empty)" if plan.created_decisions else ""))
        print("summary   : {}".format(plan.summary_path))
        print("")
        counts = plan.counts()
        for kind in mfn_link.KINDS:
            print("{:<11}: {}".format(kind, counts.get(kind, 0)))
        print("merge?     : {}".format(sum(1 for r in plan.rows if r.merge_with)))
        print("groups     : {} proposed, {} parties to group".format(
            len(plan.groups["groups"]), len(plan.group_writes())))
        if not args.apply:
            print("")
            print("dry run   : nothing in the database changed; review the proposal, "
                  "fill in the decisions file, then --apply")
            return 0
        target = backup.before_task(db_path, "mfn-link", restore_dir=args.restore_dir)
        print("restore point: {}".format(target))
        done = mfn_link.apply(conn, plan)
    finally:
        conn.close()
    print("")
    _print(done.lines())
    logs.get("mfn_link").info("mfn-link --apply: %s", "; ".join(done.lines()))
    return 0


def cmd_party_domains(args) -> int:
    """Find mail domains for the parties that have none (dry run writes the
    CSV); --apply reads the CSV back after a restore point."""
    from . import party_domains
    logs.setup()
    db_path = Path(args.db)
    if args.apply:
        if db_path.exists() and not args.no_backup:
            target = backup.before_task(db_path, "party-domains",
                                        restore_dir=args.restore_dir)
            print("restore point: {}".format(target))
        conn = db.open_db(db_path)
        try:
            report = party_domains.apply(conn, csv_path=args.csv)
        finally:
            conn.close()
        print("csv      : {}".format(Path(args.csv or party_domains.PROPOSED_CSV)))
        print("database : {}".format(db_path.resolve()))
        print("")
        _print(report.lines())
        return 0
    conn = db.open_db(db_path)
    try:
        report = party_domains.run(conn, archive_path=args.archive, csv_path=args.csv)
    except mail_archive.ArchiveError as exc:
        print(str(exc))
        return 1
    finally:
        conn.close()
    print("dry run  : nothing in the database changed; review the CSV, then --apply")
    print("")
    _print(report.lines())
    return 0


def cmd_report(args) -> int:
    """Print the month report; --html / --pdf / --xlsx write the files (7.3)."""
    from . import monthly, monthly_export
    conn = db.open_db(args.db)
    try:
        month = args.month or monthly.default_month()
        built = monthly.build(conn, month, dt.date.today())        # 17j: the running month closes on today
        kinds = [k for k, on in (("html", args.html), ("pdf", args.pdf),
                                 ("xlsx", args.xlsx)) if on]
        written = {}
        try:
            written = monthly_export.write(conn, month, kinds=kinds, out_dir=args.out_dir)
        except monthly_export.EdgeNotFound as exc:
            print(str(exc))
            return 1
    finally:
        conn.close()
    if not written or args.print:
        _print(monthly.render_text(built))
    for path in written.values():
        print("wrote {}".format(path))
    return 0


def cmd_import_agents(args) -> int:
    """Load the agent directory into party / contact / party_event
. A restore point first, like every import."""
    from . import import_agents
    logs.setup()
    db_path = Path(args.db)
    if db_path.exists() and not args.no_backup:
        target = backup.before_task(db_path, "import-agents",
                                    restore_dir=args.restore_dir)
        print("restore point: {}".format(target))
    conn = db.open_db(db_path)
    try:
        report = import_agents.run(conn, workbook=args.workbook,
                                 review_path=args.review)
    finally:
        conn.close()
    print("workbook : {}".format(Path(args.workbook or paths.AGENT_DIRECTORY_WORKBOOK)))
    print("database : {}".format(db_path.resolve()))
    print("")
    _print(report.lines())
    return 0


def cmd_export_analysis(args) -> int:
    """Three flat CSV files for analysis outside the app (data/analysis)."""
    from . import export_analysis
    conn = db.open_db(args.db)
    try:
        report = export_analysis.run(conn, out_dir=args.out_dir)
    finally:
        conn.close()
    _print(report.lines())
    return 0


def cmd_recompute_body_top(args) -> int:
    """AL6a: body_top again, for the archived mails whose top part changed
    (new quote headers). mail.db is copied beside itself first."""
    import shutil
    logs.setup()
    path = Path(args.archive or mail_archive.ARCHIVE_DB)
    if not path.exists():
        print("No mail archive at {}".format(path))
        return 1
    if not args.dry_run:
        copy = path.with_name("mail_before_body-top_{}.db".format(dt.datetime.now().strftime("%Y%m%d_%H%M%S")))
        probe = mail_archive.open_archive(path, create=False)
        probe.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        probe.close()
        shutil.copy2(path, copy)
        print("copy first: {}".format(copy))
    conn = mail_archive.open_archive(path, create=False)
    try:
        changed, total = mail_archive.recompute_body_top(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    print("mails whose top part changes: {} of {}{}".format(changed, total, " (dry run: nothing written)"
                                                           if args.dry_run else ""))
    return 0


def cmd_mail_sync(args) -> int:
    """Bring data\\mail-archive\\mail.db up to date from Outlook. Reads only."""
    logs.setup()
    days = 0 if args.full else args.days
    if args.relocate and days is None:
        days = mail_archive.RELOCATE_DAYS
    folders = _folder_patterns(args)
    started = time.time()
    print("mail archive : {}".format(mail_archive.ARCHIVE_DB))
    print("mode         : {}".format(
        "relocate (keys and places only; nothing else changes)" if args.relocate
        else "sync"))
    print("window       : {}".format(
        "everything (--full)" if days == 0 else
        "{} days".format(days) if days else
        "incremental ({} h before each folder's last sync; a folder never seen "
        "before: {} days)".format(mail_archive.OVERLAP_HOURS,
                                  mail_archive.FIRST_SYNC_DAYS)))
    if folders:
        print("folders      : {}".format(", ".join(folders)))

    def tick(result, why):
        if result.error:
            print("  FAILED  {}  {}".format(result.path, result.error[:120]), flush=True)
        elif args.relocate and (args.progress or result.new or result.moved):
            print("  {:>5} seen {:>4} added {:>4} moved {:>4} unknown {:>6.1f}s  {}  ({})".format(
                result.seen, result.new, result.moved, result.skipped, result.seconds,
                result.path, why), flush=True)
        elif not args.relocate and (args.progress or result.new or result.updated):
            print("  {:>5} new {:>5} upd {:>6.1f}s  {}  ({})".format(
                result.new, result.updated, result.seconds, result.path, why), flush=True)

    report = mail_archive.sync(days_back=days, folders=folders, progress=tick,
                               relocate=args.relocate)
    print("")
    for line in report.lines():
        print(line)
    print("elapsed      : {:.0f} s".format(time.time() - started))
    try:
        conn = mail_archive.open_archive()
        try:
            stats = mail_archive.location_stats(conn)
        finally:
            conn.close()
        print("archive      : {} mails in {} places; {} mail(s) in more than one "
              "folder (up to {})".format(stats["messages"], stats["locations"],
                                         stats["multi"], stats["max"]))
    except Exception as exc:                      # a report line, not the sync
        print("archive      : stats unavailable ({})".format(exc))
    print("sync log     : {}".format(mail_archive.SYNC_LOG))
    return 0 if report.ok else 1


def _folder_patterns(args) -> list:
    """--folder (repeatable) and --folders (comma separated) as one list of
    wildcards on the folder path; [] means every folder."""
    out = []
    for value in list(getattr(args, "folder", None) or []) + \
            (getattr(args, "folders", "") or "").split(","):
        value = (value or "").strip()
        if value:
            out.append(value)
    return out


def cmd_mail_search(args) -> int:
    """Search the archive the way scripts\\Search-Mail.ps1 searches Outlook,
    and write the same three files to data\\mail-search."""
    logs.setup()
    terms = list(args.terms or [])
    if args.term_file:
        for line in Path(args.term_file).read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                terms.append(line)
    terms = [t for t in terms if t.strip()]
    if not terms:
        print("No terms given. Pass them on the command line or with --term-file.")
        return 1
    ok, why = mail_archive.usable()
    if not ok:
        print("WARNING: {}".format(why))
    started = time.time()
    try:
        with mail_archive.ArchiveSearch() as search:
            result = mail_archive.search_terms(
                search._conn, terms, days_back=args.days, full_body=args.full_body,
                max_body_chars=args.max_body_chars, folders=_folder_patterns(args) or None)
    except mail_archive.ArchiveError as exc:
        print(str(exc))
        return 1
    written = mail_archive.write_search(result, tag=args.tag, out_dir=args.out_dir,
                                        full_body=args.full_body)
    for line in result.log:
        print(line)
    print("Files written to: {}".format(written["hits"].parent))
    for key in ("hits", "bodies", "full", "log"):
        if written[key]:
            print("  {}".format(written[key]))
    print("elapsed: {:.1f} s".format(time.time() - started))
    return 0


def cmd_migrate_master(args) -> int:
    """Master Phase 2: the dry run writes
    the report only; --apply takes a restore point first; --verify prints
    the section 8 checks."""
    if args.verify:
        checks = migrate_master.verify(args.db, args.prep_dir, args.restore_dir)
        _print(migrate_master.check_lines(checks))
        return 0 if all(c.ok for c in checks) else 1
    if not args.apply:
        plan, report = migrate_master.dry_run(args.db, args.prep_dir, args.report)
        _print(migrate_master.summary_lines(plan, report))
        print("nothing written (dry run); --apply does it")
        return 1 if plan.blocked else 0
    logs.setup()
    plan, report, result, checks = migrate_master.apply(
        args.db, args.prep_dir, restore_dir=args.restore_dir, report_path=args.report,
        export=not args.no_export)
    _print(migrate_master.summary_lines(plan, report))
    for key, value in result.items():
        print("{:<12}: {}".format(key[:12], value))
    if checks:
        print("")
        _print(migrate_master.check_lines(checks))
    return 1 if plan.blocked or not all(c.ok for c in checks) else 0


def cmd_master_info(args) -> int:
    """The master contract as another app sees it: the three v_master_*
    views, their columns and row counts, read through a read-only ATTACH
."""
    if not Path(args.db).exists():
        print("database not found: {}".format(args.db))
        return 1
    conn = master_info.open_attached(args.db)
    try:
        info = master_info.describe(conn, "opp")
    finally:
        conn.close()
    _print(master_info.lines(info, Path(args.db).resolve()))
    missing = [name for name, view in info["views"].items() if view is None]
    if missing or info["version"] is None:
        print("not on this database yet: {}".format(", ".join(missing) or "master_contract_version"))
        return 1
    return 0


def cmd_not_yet(args) -> int:
    print("'{}' arrives with a later milestone.".format(
        args.command))
    return 2


# --------------------------------------------------------------------- main --
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m opplist",
        description="OppList: inquiry registration and tracking.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db", help="create the database and schema")
    _add_common(p)
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("migrate", help="load the Excel workbook into the database")
    _add_common(p)
    p.add_argument("--workbook", default=None,
                   help="default: the master, or its copy in restore-points")
    p.add_argument("--thread-map", default=str(paths.THREAD_OPP_MAP))
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--dedupe-report", default=None,
                   help="where the probable-duplicate list goes "
                        "(default: export-preview\\migration-dedupe-report.csv)")
    p.add_argument("--no-backup", action="store_true",
                   help="skip the restore point (only for a fresh database)")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("export", help="write the xlsx and the two CSV files")
    _add_common(p)
    _add_output(p)
    p.add_argument("--month", action="append",
                   help="report month YYYY-MM; repeatable (default: last and this)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("report", help="the month report; --html / --xlsx write "
                                      "Report YYYY-MM files to docs\\reports")
    _add_common(p)
    p.add_argument("--month", default=None, help="YYYY-MM (default: last month)")
    p.add_argument("--html", action="store_true", help="write {MONTH} REPORT.html")
    p.add_argument("--pdf", action="store_true", help="write {MONTH} REPORT.pdf (Edge headless)")
    p.add_argument("--xlsx", action="store_true", help="write {MONTH} REPORT.xlsx")
    p.add_argument("--out-dir", default=None, help="folder (default: docs\\reports)")
    p.add_argument("--print", action="store_true", help="print the text as well")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("export-analysis",
                       help="the inquiries (enquiries.csv), vendor requests and parties CSV files for "
                            "analysis (default folder: data/analysis)")
    _add_common(p)
    p.add_argument("--out-dir", default=None)
    p.set_defaults(func=cmd_export_analysis)

    p = sub.add_parser("verify", help="check the migration against the workbook")
    _add_common(p)
    _add_output(p)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("backup", help="take today's restore point and prune old ones")
    _add_common(p)
    p.add_argument("--restore-dir", default=None)
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("serve", help="run the local web app on 127.0.0.1")
    _add_common(p)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--where", action="store_true",
                   help="print what a start would do (the database, the commit and folder it would run, "
                        "the uncommitted code, the port) and change nothing")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("check", help="consistency check; lists, never fixes")
    _add_common(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("cargo-check", help="cases whose cargo numbers fail a check -> data\\cargo-check.csv (read only)")
    _add_common(p)
    p.add_argument("--out", help="csv to write (default: data\\cargo-check.csv)")
    p.set_defaults(func=cmd_cargo_check)

    p = sub.add_parser("tracking", help="what needs chasing today")
    _add_common(p)
    p.add_argument("--today", default=None, help="pretend it is this date")
    p.add_argument("--limit", type=int, default=8,
                   help="rows to show per rule (default 8)")
    p.set_defaults(func=cmd_tracking)

    p = sub.add_parser("propose-status",
                       help="read Outlook and suggest a status; writes a CSV, "
                            "never the database")
    _add_common(p)
    p.add_argument("--month", default=None, help="YYYY-MM, e.g. 2026-08")
    p.add_argument("--open", action="store_true",
                   help="every open case (New / Quoted), reading "
                        "only the mail since its current status")
    p.add_argument("--opp", default=None,
                   help="one case or a comma separated list, e.g. OPP-1307")
    p.add_argument("--out", default=None, help="where the CSV goes")
    p.add_argument("--limit", type=int, default=40,
                   help="rows to print (the CSV always has them all)")
    p.add_argument("--write-suggestions", action="store_true",
                   help="record the proposed changes as pending suggestions "
                        "(the Tracking page's 'Suggested updates')")
    p.add_argument("--progress", action="store_true",
                   help="print each case as it is read, with its timing")
    p.add_argument("--source", choices=("archive", "outlook"), default="archive",
                   help="read the mail archive (default; when it is missing or "
                        "older than {} h the command stops with exit code 2 and "
                        "says to run mail-sync) or Outlook itself".format(
                            mail_archive.STALE_HOURS))
    p.set_defaults(func=cmd_propose_status)

    p = sub.add_parser("draft-followups",
                       help="retired 2026-11-04 (CX1): prints one line; the scheduled task still calls it")
    _add_common(p)
    p.set_defaults(func=cmd_draft_followups)

    p = sub.add_parser("recompute-body-top",
                       help="AL6a: the mail archive's body_top again where the top part changed "
                            "(copies mail.db first)")
    p.add_argument("--dry-run", action="store_true", help="count them, write nothing")
    p.add_argument("--archive", default=None)
    p.set_defaults(func=cmd_recompute_body_top)

    p = sub.add_parser("mail-sync",
                       help="copy new mail from Outlook into data\\mail-archive\\mail.db "
                            "(read only; Outlook must be open)")
    p.add_argument("--days", type=int, default=None,
                   help="read this many days back in every folder (default: "
                        "incremental, from the newest mail each folder already has)")
    p.add_argument("--full", action="store_true",
                   help="read every folder from the beginning (the first time)")
    p.add_argument("--folder", action="append", default=None,
                   help="only folders whose path matches this wildcard; repeatable")
    p.add_argument("--folders", default="",
                   help="the same as a comma separated list, e.g. "
                        "\"*PROJECTS*,*INBOX*,*Sent Items*\"")
    p.add_argument("--relocate", action="store_true",
                   help="read only each mail's Message-ID / EntryID / folder (no "
                        "bodies) and bring message_location and the primary folder "
                        "up to date; default window {} days".format(
                            mail_archive.RELOCATE_DAYS))
    p.add_argument("--progress", action="store_true",
                   help="print every folder, not only the ones with changes")
    p.set_defaults(func=cmd_mail_sync)

    p = sub.add_parser("mail-search",
                       help="search the mail archive (subject and body) and write "
                            "data\\mail-search\\<tag>-hits.csv / -bodies.txt like "
                            "Search-Mail.ps1; AWB numbers match loosely")
    p.add_argument("terms", nargs="*", help="AWB / HBL / PO numbers, names, words")
    p.add_argument("--term-file", default=None,
                   help="one term per line, # comments (e.g. data\\mail-search\\awb-list.txt)")
    p.add_argument("--days", type=int, default=60, help="0 = no date limit (default 60)")
    p.add_argument("--tag", default="search", help="output file prefix (default: search)")
    p.add_argument("--full-body", action="store_true",
                   help="also write <tag>-full.txt with the complete text of every hit")
    p.add_argument("--folder", action="append", default=None,
                   help="only folders whose path matches this wildcard; repeatable")
    p.add_argument("--folders", default="", help="the same, comma separated")
    p.add_argument("--max-body-chars", type=int, default=2000,
                   help="cap per mail in -bodies.txt (default 2000)")
    p.add_argument("--out-dir", default=None, help="default: data\\mail-search")
    p.set_defaults(func=cmd_mail_search)

    p = sub.add_parser("apply-status",
                       help="apply the rows left in a proposal CSV")
    _add_common(p)
    p.add_argument("csv", help="the CSV from propose-status")
    p.add_argument("--restore-dir", default=None)
    p.set_defaults(func=cmd_apply_status)

    p = sub.add_parser("reader-check",
                       help="RG1: the rule-based reader on the report base's registered mails from the mail "
                            "archive, against the cases today; no Outlook; read-only")
    _add_common(p)
    p.add_argument("--months", default="", help="YYYY-MM,YYYY-MM (default: 2026-09,2026-10,2026-11)")
    p.add_argument("--compare", default="", help="an earlier reader-check report: its counts beside, and what changed")
    p.set_defaults(func=cmd_reader_check)

    p = sub.add_parser("case-number-mismatch",
                       help="FX7: the mails whose one case number fits while their conversation and subject are "
                            "none of that case's; measuring only, read-only")
    _add_common(p)
    p.add_argument("--out", default="", help="the CSV (default: ..\\docs\\reports\\case-number-mismatch-<today>.csv)")
    p.set_defaults(func=cmd_case_number_mismatch)

    p = sub.add_parser("outlook-selftest",
                       help="parse the mail selected in Outlook; writes nothing")
    _add_common(p)
    p.add_argument("--verbose", action="store_true",
                   help="also print the underlying COM error")
    p.add_argument("--all", action="store_true",
                   help="one line per selected mail plus a tally, for checking "
                        "a batch of real inquiries at once")
    p.add_argument("--fixture", default=None,
                   help="parse a JSON fixture from fixtures\\ instead of the Outlook selection")
    p.set_defaults(func=cmd_outlook_selftest)

    p = sub.add_parser("import-tracker",
                       help="load rate-request-data.csv into vendor_request")
    _add_common(p)
    p.add_argument("--csv", default=None,
                   help="default: ..\\data\\rate-request-data.csv")
    p.add_argument("--thread-map", default=None,
                   help="default: ..\\data\\thread-opp-map.csv")
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_import_tracker)

    p = sub.add_parser("import-agents", help="load the agent directory (agents, contacts, outreach)")
    _add_common(p)
    p.add_argument("--workbook", default=None,
                   help="the agent directory workbook (default: workbooks\\AGENT DIRECTORY.xlsx)")
    p.add_argument("--review", default=None,
                   help="where the match review CSV goes (default: data\\agent-directory-review.csv)")
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_import_agents)

    p = sub.add_parser("followup", help="print the follow-up mail text for a case "
                                        "(python -m opplist followup OPP-1407 --step 2)")
    _add_common(p)
    p.add_argument("opp_no", help="the case number, e.g. OPP-1407")
    p.add_argument("--step", type=int, default=None,
                   help="FU number (1-3); default: the next one on the case")
    p.add_argument("--variant", type=int, default=None,
                   help="which version of the step's template (1-based, wraps); "
                        "default: the requester's turn in the rotation")
    p.add_argument("--archive", default=None,
                   help="the mail archive to read names and validity from")
    p.set_defaults(func=cmd_followup)

    p = sub.add_parser("detect-followups",
                       help="record the follow-ups we sent and the requester's replies "
                            "from the mail archive (also runs inside propose-status --open "
                            "--write-suggestions)")
    _add_common(p)
    p.add_argument("--dry-run", action="store_true", help="list them, write nothing")
    p.add_argument("--reclassify", action="store_true",
                   help="do not read the archive: read the 'Reply from ...' notes already on "
                        "the timeline again and write today's mark into them")
    p.add_argument("--opp", default="", help="only these cases, comma separated")
    p.add_argument("--from-archive", action="store_true",
                   help="with --reclassify: read each reply again from its mail in the archive (AL6a)")
    p.add_argument("--archive", default=None)
    p.set_defaults(func=cmd_detect_followups)

    p = sub.add_parser("fix-locations",
                       help="add the ports and gateways the master list is missing and repair "
                            "the rows a subject line created by accident; dry run by default")
    _add_common(p)
    p.add_argument("--apply", action="store_true",
                   help="write the changes (restore point first)")
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_fix_locations)

    p = sub.add_parser("fix-person-places",
                       help="retire TOMAS IBANEZ (a person read as a place) and complete "
                            "HAMILTON, Bermuda (2026-09-23); dry run by default")
    _add_common(p)
    p.add_argument("--apply", action="store_true",
                   help="write the changes (restore point first)")
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_fix_person_places)

    p = sub.add_parser("fix-closed-reasons",
                       help="put the closed reasons on the two lists (Lost = a shipment we "
                            "did not get, Dropped = the row should not exist); dry run by "
                            "default")
    _add_common(p)
    p.add_argument("--apply", action="store_true",
                   help="write the changes (restore point first)")
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_fix_closed_reasons)

    p = sub.add_parser("fill-subjects",
                       help="cases with a thread key and no registered subject: the subject of the earliest "
                            "archived mail with that key; dry run by default")
    _add_common(p)
    p.add_argument("--archive", default=None, help="the mail archive (default: data\\mail-archive\\mail.db)")
    p.add_argument("--apply", action="store_true", help="write the subjects (restore point first)")
    p.add_argument("--restore-dir", default=None, help="where the restore point goes")
    p.set_defaults(func=cmd_fill_subjects)

    p = sub.add_parser("fill-coordinates",
                       help="fill master_location.lat / lon from the UN/LOCODE code list and OurAirports; "
                            "dry run by default (data\\coordinates-proposed.csv)")
    _add_common(p)
    p.add_argument("--unlocode", required=True, help="the UN/LOCODE zip, its folder, or one code list CSV")
    p.add_argument("--airports", required=True, help="OurAirports airports.csv")
    p.add_argument("--manual", default=None,
                   help="a CSV of hand-set coordinates (key, lat, lon, note); default data\\coordinates-manual.csv when it exists")
    p.add_argument("--manual-wins", action="store_true",
                   help="let a manual row replace a coordinate that is already set")
    p.add_argument("--apply", action="store_true", help="write the coordinates (restore point first)")
    p.add_argument("--out", default=None, help="where the dry run writes its CSV")
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_fill_coordinates)

    p = sub.add_parser("propose-shipments",
                       help="read the archive mails of every active shipment's threads newer than its "
                            "last event and propose milestones / issues (pending suggestions)")
    _add_common(p)
    p.add_argument("--dry-run", action="store_true", help="print the proposals, write nothing")
    p.set_defaults(func=cmd_propose_shipments)

    p = sub.add_parser("repair-fu-templates",
                       help="set fu_template_1/2/3 that hold an old default (any line breaks) to empty, "
                            "so the current pool is used; dry run by default")
    _add_common(p)
    p.add_argument("--apply", action="store_true", help="write the changes (restore point first)")
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_repair_fu_templates)

    p = sub.add_parser("official-places",
                       help="the official code lists for the team site's place picker (UN/LOCODE and "
                            "OurAirports, one file per country); dry run by default")
    _add_common(p)
    p.add_argument("--unlocode", required=True, help="the UN/LOCODE zip, its folder, or one code list CSV")
    p.add_argument("--airports", required=True, help="OurAirports airports.csv")
    p.add_argument("--apply", action="store_true",
                   help="write {CC}.json and index.json and keep the two paths in Settings")
    p.add_argument("--out-dir", default=None, help="default ..\\OppListWeb\\app\\public\\official")
    p.add_argument("--release", default=None, help="the release's version when its files do not say (2025-1)")
    p.set_defaults(func=cmd_official_places)

    p = sub.add_parser("hs-codes",
                       help="move the HS codes written in cargo lines' commodity into hs_code (v50); "
                            "dry run by default (data\\proposed-hs-codes.csv)")
    _add_common(p)
    p.add_argument("--apply", action="store_true", help="move them (restore point first)")
    p.add_argument("--out", default=None, help="the dry run's CSV (default data\\proposed-hs-codes.csv)")
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_hs_codes)

    p = sub.add_parser("fix-vendor-quoted",
                       help="one vendor status, not two: quoted = 1 where the status is Quoted, "
                            "status Quoted where only the flag was set; dry run by default")
    _add_common(p)
    p.add_argument("--apply", action="store_true",
                   help="write the changes (restore point first)")
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_fix_vendor_quoted)

    p = sub.add_parser("retire-sourcing",
                       help="Sourcing is retired: list the cases still on it and the pending "
                            "suggestions proposing it; --apply moves them to New; dry run by default")
    _add_common(p)
    p.add_argument("--apply", action="store_true",
                   help="write the changes (restore point first)")
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_retire_sourcing)

    # WA1: the clearer name; the old one still works
    p = sub.add_parser("not-latam-out", aliases=["direct-customers-out"],
                       help="SC1: read only - the cases the reports leave out and why (direct customers "
                            "before 1 Oct 2026; from October direct customer and own office cases that "
                            "are not LATAM); scope is not changed")
    _add_common(p)
    p.add_argument("--apply", action="store_true", help="refused since SC1: the command only lists")
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_direct_customers_out)

    p = sub.add_parser("scope-back",
                       help="SC1: the Out cases the old rules set (not by hand) back to In; dry run by default")
    _add_common(p)
    p.add_argument("--apply", action="store_true", help="write the changes (restore point first)")
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_scope_back)

    p = sub.add_parser("withdraw-requote-suggestions",
                       help="the retired 'they asked for the rate again' suggestions still pending: "
                            "listed; --apply dismisses them with a note; dry run by default")
    _add_common(p)
    p.add_argument("--apply", action="store_true", help="write the changes (restore point first)")
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_withdraw_requote_suggestions)

    p = sub.add_parser("rekey-threads",
                       help="MF1, TK2: the stored thread keys the subject rules change (a leading "
                            "reference that leaves no letter, BRN-QT-207, or common words only, "
                            "KLX-552018 - IMPORT AIR, stays); dry run by default")
    _add_common(p)
    p.add_argument("--apply", action="store_true", help="write the keys (restore point first)")
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_rekey_threads)

    p = sub.add_parser("migrate-cargo-lines",
                       help="cargo is lines only (15b): one line for every case with typed numbers and no "
                            "line; tried on a copy and compared first; dry run by default")
    _add_common(p)
    p.add_argument("--apply", action="store_true", help="write the lines (restore point first)")
    p.add_argument("--restore-dir", default=None)
    p.set_defaults(func=cmd_migrate_cargo_lines)

    p = sub.add_parser("party-domains",
                       help="propose mail domains (from the archive) for parties that have "
                            "none; dry run writes data\\party-domains-proposed.csv, --apply "
                            "reads it back")
    _add_common(p)
    p.add_argument("--apply", action="store_true",
                   help="apply the CSV's add-domain / merge-into rows (restore point first)")
    p.add_argument("--csv", default=None,
                   help="the proposal CSV (default: data\\party-domains-proposed.csv)")
    p.add_argument("--archive", default=None,
                   help="the mail archive to read (default: data\\mail-archive\\mail.db)")
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_party_domains)

    p = sub.add_parser("mfn-link",
                       help="WC1: Agent parties against the MFN directory; dry run writes "
                            "data\\mfn-link\\mfn-link-proposed.csv and docs\\reports\\mfn-link-"
                            "YYYY-MM-DD.md, --apply writes the links and groups")
    _add_common(p)
    p.add_argument("--apply", action="store_true",
                   help="write the link rows and her decisions (restore point first)")
    p.add_argument("--dir", default=None,
                   help="the directory files (default: data\\mfn\\directory)")
    p.add_argument("--out-dir", default=None, help="proposal and decisions (default: data\\mfn-link)")
    p.add_argument("--reports-dir", default=None, help="summary folder (default: docs\\reports)")
    p.add_argument("--restore-dir", default=None)
    p.set_defaults(func=cmd_mfn_link)

    p = sub.add_parser("migrate-master",
                       help="master Phase 2: location v2 into master_* tables, inquiry "
                            "location ids, party roles / groups / merges "
                            ". Dry run by default")
    _add_common(p)
    p.add_argument("--dry-run", action="store_true",
                   help="plan and write data\\master-prep\\migrate-master-report.md (the default)")
    p.add_argument("--apply", action="store_true",
                   help="restore point, schema v14, then the plan in one transaction")
    p.add_argument("--verify", action="store_true",
                   help="print the section 8 acceptance checks (read only)")
    p.add_argument("--prep-dir", default=None,
                   help="the Phase 1 CSV folder (default: data\\master-prep)")
    p.add_argument("--report", default=None, help="where the report goes")
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--no-export", action="store_true",
                   help="apply: skip rewriting the three export files")
    p.set_defaults(func=cmd_migrate_master)

    p = sub.add_parser("master-info",
                       help="the master contract views (v_master_location / v_master_alias / "
                            "v_master_party): columns and row counts, read through a "
                            "read-only ATTACH the way the Rate Desk does")
    _add_common(p)
    p.set_defaults(func=cmd_master_info)

    p = sub.add_parser("firebase-sync",
                       help="push the lists to the team site (Firebase) now; --status shows what it would push")
    _add_common(p)
    p.add_argument("--status", action="store_true")
    p.set_defaults(func=cmd_firebase_sync)

    p = sub.add_parser("read-quote", help="Read quote's settings and this month's spend (--status)")
    _add_common(p)
    p.add_argument("--status", action="store_true")
    p.set_defaults(func=cmd_read_quote)

    p = sub.add_parser("read-quote-report", help="the week's Read quote readings -> ..\\data\\reports")
    _add_common(p)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--today", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_read_quote_report)

    p = sub.add_parser("read-quote-trial", help="read the newest vendor replies with prices as a trial (never pushed)")
    _add_common(p)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--since", default="2026-10-01")
    p.add_argument("--pick-only", action="store_true", help="list the mails it would read; no call")
    p.add_argument("--again", type=int, nargs="+", metavar="READING",
                   help="read these readings' mails again with today's prompt, beside the earlier reading")
    p.set_defaults(func=cmd_read_quote_trial)

    p = sub.add_parser("agent-rate-requests",
                       help="write the Agent platform's rate-request counts (outreach-import\\rate-requests.json)")
    _add_common(p)
    p.add_argument("--out", default=None, help="the folder to write into (default: ..\\data\\outreach-import)")
    p.add_argument("--today", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_agent_rate_requests)

    p = sub.add_parser("agent-domains",
                       help="attach the agent directory's e-mail domains to the matched parties")
    _add_common(p)
    p.add_argument("--workbook", default=None)
    p.add_argument("--restore-dir", default=None)
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_agent_domains)

    p = sub.add_parser("leakcheck",
                       help="list every string from a forbidden list that appears in "
                            "the repository (or, with --all, in any file under --root)")
    p.add_argument("--list", required=True, help="the forbidden list (kept outside the repo)")
    p.add_argument("--root", default=".", help="folder to scan (default: current)")
    p.add_argument("--all", action="store_true",
                   help="scan every file under --root, not only what git would commit")
    p.set_defaults(func=cmd_leakcheck)

    p = sub.add_parser("demo",
                       help="build synthetic demo data: <out>/opplist.db and <out>/mail-archive/mail.db "
                            "(same seed and date -> the same bytes)")
    p.add_argument("--out", default="demo", help="folder to write (default: demo)")
    p.add_argument("--seed", type=int, default=42, help="random seed (default: 42)")
    p.add_argument("--today", default=None,
                   help="the date the data is relative to, YYYY-MM-DD (default: today)")
    p.add_argument("--force", action="store_true",
                   help="replace a database that is not a demo database (it is copied aside first)")
    p.add_argument("--site-data", default=None, metavar="FOLDER",
                   help="also copy the team site's snapshot there (site/data, to serve site/ as plain files)")
    p.set_defaults(func=cmd_demo)

    return parser


def cmd_demo(args) -> int:
    from . import demo
    try:
        result = demo.generate(args.out, seed=args.seed, today=args.today, force=args.force,
                               site_data=args.site_data)
    except demo.DemoError as exc:
        print(str(exc))
        return 1
    _print(demo.report_lines(result, args.out))
    return 0


def cmd_leakcheck(args) -> int:
    from . import leakcheck
    hits, forbidden, scanned = leakcheck.run(args.list, args.root, every_file=args.all)
    for hit in hits:
        print("{}:{}: [{}] {}".format(hit.path, hit.line, hit.term, hit.text))
    print("{} hit(s) in {} file(s) scanned; forbidden list has {} entries".format(
        len(hits), scanned, forbidden.size))
    return 1 if hits else 0


def main(argv=None) -> int:
    # Folder names carry characters a cp1252 console cannot show; a report
    # line must not be the thing that crashes a command.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    # --scheduled (13f): the scheduled tasks' flag, taken out before parsing
    # so every command accepts it. Such a run first checks that the code is
    # committed and that the database needs no upgrade (scheduled.guard).
    argv = list(sys.argv[1:] if argv is None else argv)
    is_scheduled = "--scheduled" in argv
    argv = [a for a in argv if a != "--scheduled"]
    args = build_parser().parse_args(argv)
    # FX3: a command may say why a done run did less (args.done_reason)
    args.scheduled, args.done_reason = is_scheduled, ""
    from . import scheduled
    command = " ".join(argv)
    if not is_scheduled:
        # OP1: a morning step run by hand clears its line in the banner
        result = args.func(args)
        try:
            scheduled.record_manual(command, "done" if not result else "failed")
        except Exception:
            pass
        return result
    db_path = getattr(args, "db", None) or paths.DB_PATH
    # OP1: the off-PC copy first, so a skipped run still makes it
    scheduled.offsite_copy(db_path)
    if HANDOVER_SKIP:
        scheduled.record(command, "skipped", HANDOVER_SKIP)
        return scheduled.EXIT_NOT_COMMITTED
    skipped = scheduled.guard(db_path, command)
    if skipped is not None:
        return skipped
    try:
        result = args.func(args)
    except BaseException as exc:
        # OP1: the error's first line, before the exception goes on
        first = (str(exc).strip().splitlines() or [""])[0]
        scheduled.record(command, "failed", "{}: {}".format(type(exc).__name__, first) if first else type(exc).__name__)
        raise
    scheduled.record(command, "done" if not result else "failed",
                     (getattr(args, "done_reason", "") or "") if not result else "exit {}".format(result))
    # RG2b (layer 3): on the 1st of a month (or the first run after it, once)
    # a morning step checks the reader on the month before; never fails the step
    if scheduled.step_of(command) in [scheduled.step_of(s) for s in scheduled.MORNING_STEPS]:
        from . import reader_month
        for line in reader_month.scheduled_check(db_path):
            print(line)
            logs.get("reader-month").info(line)
    return result


if __name__ == "__main__":
    sys.exit(main())
