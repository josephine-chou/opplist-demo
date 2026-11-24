r"""Which code this process runs (batch OP2, 2026-11-20).

`python -m opplist serve` over the live database, started from
JoJo\opplist, hands itself to the worktree JoJo\opplist-serve at the
commit HEAD points at (the top of __main__.py, standard library only). The
hand-over tells the server what it did through the environment; this module
reads it for the console, the footer, Settings and the red line on every
page. The names are __main__'s SERVE_*_ENV (that module cannot be imported
from here: under `python -m` it is the script).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import subprocess
from pathlib import Path

from . import paths

WORKTREE_ENV = "OPPLIST_SERVE_WORKTREE"        # the worktree's folder
COMMIT_ENV = "OPPLIST_SERVE_COMMIT"
SUBJECT_ENV = "OPPLIST_SERVE_SUBJECT"
UNCOMMITTED_ENV = "OPPLIST_SERVE_UNCOMMITTED"
FALLBACK_ENV = "OPPLIST_SERVE_FALLBACK"
MAIN_DIR_ENV = "OPPLIST_MAIN_DIR"
#: Settings' one git call for the main folder's HEAD
GIT_TIMEOUT = 3


def folder(path) -> str:
    """JoJo\\opplist-serve: a folder named with its parent."""
    path = Path(path)
    return "{}\\{}".format(path.parent.name, path.name)


#: __main__.SUBJECT_SHOWN
SUBJECT_SHOWN = 80


def shown_subject(subject) -> str:
    """__main__.shown_subject: at most 80 characters, the rest cut to '...'
    (ASCII: her console's code page)."""
    subject = " ".join((subject or "").split())
    return subject if len(subject) <= SUBJECT_SHOWN else subject[:SUBJECT_SHOWN - 3].rstrip() + "..."


def uncommitted_line(count, where) -> str:
    """__main__.uncommitted_line."""
    return "{} code file{} in {} {} not committed: not running.".format(
        count, "" if count == 1 else "s", where, "is" if count == 1 else "are")


def _same(a, b) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, ValueError):
        return False


def info() -> dict:
    """What this process runs:
    from_worktree  the commit from JoJo\\opplist-serve (the hand-over)
    commit, short, subject, uncommitted, main_dir  (from_worktree only)
    reason         why the code here is not the last commit, or ''
    red            the red line on every page, or ''"""
    env = os.environ
    project = paths.PROJECT_DIR
    worktree = (env.get(WORKTREE_ENV) or "").strip()
    out = {"from_worktree": False, "commit": "", "short": "", "subject": "", "uncommitted": 0,
           "main_dir": None, "folder": folder(project), "reason": "", "red": ""}
    if worktree:
        if not _same(worktree, project):
            # the environment says the serve worktree, the package came from elsewhere
            out["reason"] = "the package was loaded from {}, not from {}".format(folder(project), folder(worktree))
        else:
            commit = (env.get(COMMIT_ENV) or "").strip()
            try:
                uncommitted = int(env.get(UNCOMMITTED_ENV) or 0)
            except ValueError:
                uncommitted = 0
            main_dir = Path(env.get(MAIN_DIR_ENV) or paths.MAIN_DIR)
            out.update(from_worktree=True, commit=commit, short=commit[:7],
                       subject=shown_subject(env.get(SUBJECT_ENV)), uncommitted=uncommitted, main_dir=main_dir)
            return out
    else:
        out["reason"] = (env.get(FALLBACK_ENV) or "").strip()
    if out["reason"]:
        out["red"] = "8765 is running the code in {}, not the last commit: {}.".format(folder(project), out["reason"])
    return out


def start_lines() -> list:
    """(level, line) for the console and data\\opplist.log before the
    server's own lines; none for a server that was never handed over (a
    copy, 8799)."""
    got = info()
    if got["from_worktree"]:
        lines = [(logging.INFO, "Code: commit {} {}, from {}".format(got["short"], got["subject"], got["folder"]))]
        if got["uncommitted"]:
            lines.append((logging.INFO, uncommitted_line(got["uncommitted"], folder(got["main_dir"]))))
        return lines
    return [(logging.WARNING, got["red"])] if got["red"] else []


def footer_code(code_time: str) -> str:
    """The footer's last part: 'commit 1a2b3c4' from the serve worktree
    (a file's time there is only when git wrote it), else 'code HH:MM'."""
    got = info()
    return "commit {}".format(got["short"]) if got["from_worktree"] and got["short"] else "code {}".format(code_time)


def main_head(main_dir, timeout=GIT_TIMEOUT):
    """(commit, subject) the main folder's HEAD points at now, or None
    when git cannot say (one call, a short timeout)."""
    import shutil
    git = shutil.which("git")
    if git is None:
        fallback = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "cmd" / "git.exe"
        git = str(fallback) if fallback.exists() else None
    if git is None:
        return None
    try:
        result = subprocess.run([git, "-C", str(main_dir), "log", "-1", "--format=%H%x00%s", "HEAD"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    except (OSError, subprocess.SubprocessError):
        return None
    commit, _, subject = (result.stdout or "").strip().partition("\x00")
    if result.returncode != 0 or not commit:
        return None
    return commit, subject


def settings_lines(started: dt.datetime | None) -> list:
    """The lines beside the schema version on Settings."""
    got = info()
    if not got["from_worktree"]:
        line = "Running the code in {}".format(got["folder"])
        return [line + (": not the last commit, {}.".format(got["reason"]) if got["reason"] else "")]
    lines = ["Running commit {} {}, from {}, since {}".format(
        got["short"], got["subject"], paths.PROJECT_DIR, (started or dt.datetime.now()).strftime("%H:%M"))]
    if got["uncommitted"]:
        lines.append("{} code file{} in {} {} not committed at that start (not running)".format(
            got["uncommitted"], "" if got["uncommitted"] == 1 else "s", folder(got["main_dir"]),
            "was" if got["uncommitted"] == 1 else "were"))
    head = main_head(got["main_dir"])
    if head and got["commit"] and head[0] != got["commit"]:
        lines.append("{} is at {} {}: restart 8765 to run it".format(folder(got["main_dir"]), head[0][:7],
                                                                 shown_subject(head[1])))
    return lines
