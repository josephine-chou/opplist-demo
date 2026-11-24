"""The morning run never uses unfinished code and never upgrades the
database (batch 13f, 2026-09-30; batch OP1, 2026-11-10).

The scheduled tasks ("JoJo Mail Refresh", "JoJo Mail Relocate")
call `python -m opplist <command> --scheduled`. Before the command runs:

  1. OP1: when the package folder (opplist\\opplist\\) or schema.sql has an
     uncommitted change, the run is not skipped any more: the top of
     __main__.py (standard library only) brings the worktree
     JoJo\\opplist-run to the commit HEAD points at and runs the same
     command there. Inside the worktree the code is clean, so the check
     below passes. Changes under tests\\, docs\\ and export-preview\\ never
     counted. A git failure is a skip, recorded with its reason.
  2. the database must already be at the code's schema version: a
     scheduled run never upgrades it. The app on 8765 is the only place
     that does, with its restore point, when Iris restarts it.

A skipped run does nothing else, writes one line to data\\opplist.log and
exits with its own code. data\\scheduled-run.json keeps every command's
latest outcome (OP1): the Tracking page's run line shows the newest, the
banner on every page reads today's. Manual runs (no --scheduled) behave as
before; a manual run of one of the morning steps is remembered apart, so
that the banner goes away once she has run the fix it names.

Before the guard decides, the first scheduled command of a day may copy
the newest daily restore point off the PC (OP1, setting
offsite_backup_dir): see offsite_copy.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

from . import logs, paths, schema_migrations

#: exit codes of a skipped scheduled run
EXIT_NOT_COMMITTED = 75
EXIT_NEEDS_UPGRADE = 76
REPO_ROOT = Path(__file__).resolve().parent.parent
#: what counts as code: the package folder and the schema (paths in the repo)
CODE_PATHS = ("opplist", "schema.sql")
STATE_NAME = "scheduled-run.json"
#: OP1: set by __main__ for the run it hands to the worktree
COMMIT_ENV = "OPPLIST_RUN_COMMIT"
UNCOMMITTED_ENV = "OPPLIST_RUN_UNCOMMITTED"
#: the scheduled task's steps, in its order (Register-MailRefresh.ps1)
MORNING_STEPS = ("mail-sync", "propose-status --open --write-suggestions",
                 "propose-shipments", "firebase-sync")
#: the app on 8765 pushes the team site itself within minutes of a start,
#: so the banner's fix never asks her to run this one
APP_DOES = ("firebase-sync",)
#: OP1: the off-PC copy
OFFSITE_SETTING = "offsite_backup_dir"
OFFSITE_EVERY_DAYS = 7
OFFSITE_LATE_DAYS = 8
OFFSITE_PATTERN = re.compile(r"^OppList_(\d{8})\.db$")
DAILY_PATTERN = re.compile(r"^opplist_(\d{8})\.db$")


def _git() -> str | None:
    found = shutil.which("git")
    if found:
        return found
    fallback = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "cmd" / "git.exe"
    return str(fallback) if fallback.exists() else None


def uncommitted(repo_root=None) -> list:
    """The code files with uncommitted changes: modified, added, deleted or
    renamed files under the package folder or schema.sql, and untracked
    .py files there. Raises OSError when git cannot be run.
    (__main__._code_changes is the same check in the standard library
    only, for the hand-over that runs before any other import.)"""
    git = _git()
    if git is None:
        raise OSError("git not found")
    result = subprocess.run([git, "-C", str(repo_root or REPO_ROOT), "status", "--porcelain",
                             "--untracked-files=all", "--", *CODE_PATHS],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if result.returncode != 0:
        raise OSError((result.stderr or "git status failed").strip().splitlines()[0])
    out = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        code, name = line[:2], line[3:].strip().strip('"')
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        if code == "??" and not name.endswith(".py"):
            continue
        out.append(name)
    return out


def head_commit(repo_root=None) -> str:
    """The short hash of the commit the code was checked out at; '' when
    git cannot say."""
    git = _git()
    if git is None:
        return ""
    try:
        result = subprocess.run([git, "-C", str(repo_root or REPO_ROOT), "rev-parse", "--short=7", "HEAD"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def db_version(db_path) -> int | None:
    """The database's schema_version, read without opening it for writing;
    None when there is no database or no version."""
    return _read_setting(db_path, "schema_version", int)


def _read_setting(db_path, key, kind=str):
    path = Path(db_path)
    if not path.exists():
        return None
    conn = sqlite3.connect("file:{}?mode=ro".format(path.as_posix()), uri=True)
    try:
        row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
        return kind(row[0]) if row and row[0] is not None else None
    except (sqlite3.Error, ValueError):
        return None
    finally:
        conn.close()


# --------------------------------------------------------------- the record --
def _state_path() -> Path:
    return Path(paths.DATA_DIR) / STATE_NAME


def step_of(command: str) -> str:
    """The command's name, the key of its entry: 'propose-status' for
    'propose-status --open --write-suggestions'."""
    return (command or "").split(" ", 1)[0]


def load() -> dict:
    """{'runs': {step: entry}, 'manual': {step: entry}, 'offsite': entry}.
    The one-entry file from before OP1 reads as that command's entry."""
    try:
        state = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    if "command" in state and "runs" not in state:
        state = {"runs": {step_of(state.get("command") or ""): state}}
    state.setdefault("runs", {})
    state.setdefault("manual", {})
    return state


def _save(state: dict) -> None:
    try:
        _state_path().parent.mkdir(parents=True, exist_ok=True)
        _state_path().write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _newest(entries):
    """The newest entry; 'seq' orders two written in the same second."""
    return max(entries, key=lambda e: (e["at"], e.get("seq") or 0))


def _next_seq(data) -> int:
    data["seq"] = int(data.get("seq") or 0) + 1
    return data["seq"]


def record(command: str, state: str, reason: str = "") -> None:
    """The outcome of one scheduled run: its command's entry in
    data\\scheduled-run.json and one line in data\\opplist.log. OP1: the
    entry and the line name the commit that ran and, for a run handed to
    the worktree, how many files were uncommitted."""
    commit = os.environ.get(COMMIT_ENV, "")[:7] or head_commit()
    try:
        left = int(os.environ.get(UNCOMMITTED_ENV) or 0)
    except ValueError:
        left = 0
    data = load()
    entry = {"at": _now(), "command": command, "state": state, "reason": reason,
             "commit": commit, "uncommitted": left, "seq": _next_seq(data)}
    data["runs"][step_of(command)] = entry
    _save(data)
    logs.setup()
    where = ""
    if commit:
        where = " at {}".format(commit)
        if left:
            where += " from opplist-run ({} file{} uncommitted)".format(left, "s" if left != 1 else "")
    line = "scheduled run {}{} [{}]{}".format(state, ": " + reason if reason else "", command, where)
    (logs.get("scheduled").warning if state != "done" else logs.get("scheduled").info)(line)


def record_manual(command: str, state: str) -> None:
    """A manual run of a morning step (no --scheduled): kept apart from the
    scheduled entries, so the Tracking line still shows the task's run."""
    if step_of(command) not in {step_of(s) for s in MORNING_STEPS}:
        return
    data = load()
    data["manual"][step_of(command)] = {"at": _now(), "command": command, "state": state,
                                        "seq": _next_seq(data)}
    _save(data)


def last_run() -> dict | None:
    """The newest scheduled run's outcome, or None when there has been none."""
    runs = [e for e in load()["runs"].values() if isinstance(e, dict) and e.get("at")]
    return _newest(runs) if runs else None


def guard(db_path, command: str, repo_root=None) -> int | None:
    """None when the scheduled run may go ahead; otherwise the exit code
    after the skip has been recorded."""
    try:
        changed = uncommitted(repo_root)
    except (OSError, subprocess.SubprocessError) as exc:
        record(command, "skipped", "git cannot be run: {}".format(exc))
        return EXIT_NOT_COMMITTED
    if changed:
        record(command, "skipped", "code not committed, {} file{}".format(len(changed), "s" if len(changed) != 1 else ""))
        return EXIT_NOT_COMMITTED
    have, want = db_version(db_path), schema_migrations.CURRENT_VERSION
    if have is None or have < want:
        record(command, "skipped", "the app has to upgrade the database first (v{} to v{})".format(
            have if have is not None else "?", want))
        return EXIT_NEEDS_UPGRADE
    return None


# ------------------------------------------------------------- off-PC copy --
def _date_of(name: str, pattern) -> dt.date | None:
    found = pattern.match(name)
    if not found:
        return None
    try:
        return dt.datetime.strptime(found.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def offsite_copy(db_path, today=None) -> dict | None:
    """OP1: once a day, before the guard decides (a skipped run still
    copies). When setting offsite_backup_dir names a folder and the newest
    OppList_YYYYMMDD.db there is 7 days old or more (or there is none), the
    newest daily restore point (opplist_YYYYMMDD.db beside the database) is
    copied in as OppList_YYYYMMDD.db. Nothing there is deleted or
    overwritten: the copy goes to a fresh .part name and is renamed, and a
    name already there is left alone. Only that one file is ever copied -
    never data\\secrets, never the mail archive. Never raises; the attempt
    and its reason are the 'offsite' entry. None when there was nothing to
    do (no folder, or already looked today)."""
    today = today or dt.date.today()
    try:
        return _offsite_copy(db_path, today)
    except Exception as exc:                       # never fail the run over it
        entry = {"at": _now(), "state": "failed", "reason": str(exc).splitlines()[0] if str(exc) else type(exc).__name__}
        _offsite_save(entry, today)
        return entry


def _offsite_save(entry: dict, today: dt.date) -> None:
    entry["day"] = today.isoformat()
    data = load()
    before = data.get("offsite") or {}
    if "last_copy" not in entry and before.get("last_copy"):
        entry["last_copy"] = before["last_copy"]
    data["offsite"] = entry
    _save(data)
    if entry.get("state") == "failed":
        logs.setup()
        logs.get("scheduled").warning("off-PC copy failed: %s", entry.get("reason"))
    elif entry.get("state") == "copied":
        logs.setup()
        logs.get("scheduled").info("off-PC copy %s", entry.get("copy"))


def _offsite_copy(db_path, today: dt.date) -> dict | None:
    folder = paths.folder_setting(_read_setting(db_path, OFFSITE_SETTING))
    if not folder:
        return None
    before = load().get("offsite") or {}
    if (before.get("day") or (before.get("at") or "")[:10]) == today.isoformat():
        return None
    target = Path(folder)
    try:
        held = [d for d in (_date_of(p.name, OFFSITE_PATTERN) for p in target.iterdir()) if d]
    except OSError as exc:
        entry = {"at": _now(), "state": "failed", "reason": "the folder cannot be read: {}".format(
            exc.strerror or exc)}
        _offsite_save(entry, today)
        return entry
    newest = max(held) if held else None
    if newest and (today - newest).days < OFFSITE_EVERY_DAYS:
        entry = {"at": _now(), "state": "not due", "reason": "", "last_copy": newest.isoformat()}
        _offsite_save(entry, today)
        return entry
    from . import backup
    source_dir = backup.folder_for(db_path)
    dailies = sorted((p for p in source_dir.glob("opplist_*.db") if _date_of(p.name, DAILY_PATTERN)),
                     key=lambda p: p.name) if source_dir.exists() else []
    if not dailies:
        entry = {"at": _now(), "state": "failed", "reason": "no daily restore point to copy",
                 "last_copy": newest.isoformat() if newest else ""}
        _offsite_save(entry, today)
        return entry
    source = dailies[-1]
    day = _date_of(source.name, DAILY_PATTERN)
    final = target / "OppList_{}.db".format(day.strftime("%Y%m%d"))
    if final.exists() or (newest and day <= newest):
        entry = {"at": _now(), "state": "not due", "last_copy": newest.isoformat() if newest else day.isoformat(),
                 "reason": "no restore point newer than the last copy"}
        _offsite_save(entry, today)
        return entry
    part = target / "{}.part-{}".format(final.name, dt.datetime.now().strftime("%H%M%S%f"))
    try:
        shutil.copy2(source, part)
        os.rename(part, final)                     # refuses an existing name
    except OSError as exc:
        entry = {"at": _now(), "state": "failed", "reason": "the copy failed: {}".format(exc.strerror or exc),
                 "last_copy": newest.isoformat() if newest else ""}
        _offsite_save(entry, today)
        return entry
    entry = {"at": _now(), "state": "copied", "reason": "", "copy": str(final), "last_copy": day.isoformat()}
    _offsite_save(entry, today)
    return entry


def offsite_status(db_path, today=None) -> dict | None:
    """For the Tracking line and Settings: the last copy's date, and 'bad'
    when it is more than 8 days old or the last attempt failed. None when
    no folder is set and nothing was ever tried."""
    today = today or dt.date.today()
    entry = load().get("offsite") or {}
    folder = paths.folder_setting(_read_setting(db_path, OFFSITE_SETTING))
    if not folder and not entry:
        return None
    last = entry.get("last_copy") or ""
    try:
        age = (today - dt.date.fromisoformat(last)).days if last else None
    except ValueError:
        age = None
    failed = entry.get("state") == "failed"
    return {"folder": folder, "last_copy": last, "at": entry.get("at") or "", "state": entry.get("state") or "",
            "reason": entry.get("reason") or "", "failed": failed,
            "bad": bool(folder) and (failed or age is None or age > OFFSITE_LATE_DAYS)}


# ------------------------------------------------------------------ banner --
def banner(now=None) -> dict | None:
    """OP1: one red line for every page when a step of today's morning run
    was skipped or failed, or when no step has run for more than one
    working day; None otherwise. A step is cleared by a later done run of
    it, scheduled or by hand. {'text', 'fix', 'steps', 'site'}. FX3: the
    team site's line (paused by the day's cap, or Firebase's 429) as 'site',
    or as the text when the morning run is fine."""
    morning = _morning_banner(now)
    try:
        from . import firebase_sync
        site = firebase_sync.guard_line(today=(now or dt.datetime.now()).date().isoformat())
    except Exception:                         # the banner never takes a page down
        site = ""
    if not site:
        return morning
    if morning is None:
        return {"text": site, "fix": "", "steps": [], "site": ""}
    return dict(morning, site=site)


def _morning_banner(now=None) -> dict | None:
    from .tracking import business_days_between

    now = now or dt.datetime.now()
    today = now.date().isoformat()
    data = load()
    runs = {k: e for k, e in data["runs"].items() if isinstance(e, dict) and e.get("at")}
    manual = {k: e for k, e in data["manual"].items() if isinstance(e, dict) and e.get("at")}
    if not runs:
        return None

    def cleared(step, entry):
        later = manual.get(step)
        return bool(later and later.get("state") == "done"
                    and (later["at"], later.get("seq") or 0) > (entry["at"], entry.get("seq") or 0))

    bad = [(step, e) for step, e in runs.items()
           if e["at"][:10] == today and e.get("state") != "done" and not cleared(step, e)]
    if bad:
        bad.sort(key=lambda item: _order(item[0]))
        reasons = []
        for _, e in bad:
            pair = (e.get("state") or "failed", e.get("reason") or "")
            if pair not in reasons:
                reasons.append(pair)
        if len(reasons) == 1:
            state, reason = reasons[0]
            text = "This morning's update {}{}.".format(
                "was skipped" if state == "skipped" else "failed", ": " + reason if reason else "")
        else:
            text = "This morning's update did not finish: {}.".format("; ".join(
                "{} {}{}".format(step, e.get("state"), " ({})".format(e["reason"]) if e.get("reason") else "")
                for step, e in bad))
        return {"text": text, "fix": _fix([e.get("command") or step for step, e in bad],
                                          [e.get("reason") or "" for _, e in bad]),
                "steps": [step for step, _ in bad]}
    newest = _newest(list(runs.values()) + list(manual.values()))
    gap = business_days_between(newest["at"][:10], today)
    if gap is not None and gap > 1:
        when = dt.datetime.strptime(newest["at"][:16], "%Y-%m-%d %H:%M")
        return {"text": "The morning update has not run since {} {}/{} {}.".format(
            when.strftime("%a"), when.month, when.day, when.strftime("%H:%M")),
            "fix": _fix(list(MORNING_STEPS), [], stale=True), "steps": [step_of(s) for s in MORNING_STEPS]}
    return None


def _order(step: str) -> int:
    names = [step_of(s) for s in MORNING_STEPS]
    return names.index(step) if step in names else len(names)


def _fix(commands, reasons, stale=False) -> str:
    """The fix in one sentence: what to do first, then the steps to run."""
    wanted = []
    for command in commands:
        step = step_of(command)
        if step in APP_DOES:
            continue
        full = next((s for s in MORNING_STEPS if step_of(s) == step), command)
        if full not in wanted:
            wanted.append(full)
    wanted.sort(key=lambda c: _order(step_of(c)))
    run = "python -m opplist " + ", ".join(wanted) if wanted else ""
    if any(r.startswith("the app has to upgrade") for r in reasons):
        first = "Restart OppList"
    elif any(r.startswith("git cannot be run") for r in reasons):
        first = "Check that git runs on this PC"
    elif any(r.startswith("code not committed") for r in reasons):
        first = "Commit the code in opplist (or put the unfinished files aside)"
    elif stale:
        first = "Check that the JoJo Mail Refresh task is still scheduled"
    else:
        first = "Look in data\\opplist.log for the error"
    if not run:
        return "{}; the app pushes the team site by itself.".format(first)
    return "{}, then run: {}.".format(first, run)
