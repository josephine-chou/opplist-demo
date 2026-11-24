"""Restore points.

Copying the database is cheap and it is the only safety net there is, so it
happens before every import and once a day at startup. Pruning old copies is
the single delete this app performs, and it only ever touches files it made
itself.
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
from pathlib import Path

from . import paths

DAILY_PATTERN = re.compile(r"^opplist_\d{8}\.db$")
TASK_PATTERN = re.compile(r"^opplist_before_[a-z0-9\-]+_\d{8}_\d{6}\.db$")


def folder_for(db_path=None) -> Path:
    """Where a database's restore points live: a restore-points folder beside
    it. For the live data\\opplist.db that is data\\restore-points; a run on a
    copy elsewhere can never write into the live folder (15b)."""
    if db_path is None:
        return Path(paths.RESTORE_POINTS_DIR)
    return Path(db_path).resolve().parent / "restore-points"


def _dir(restore_dir=None, db_path=None) -> Path:
    target = Path(restore_dir) if restore_dir else folder_for(db_path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _copy(db_path, target: Path) -> Path:
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    shutil.copy2(db_path, target)
    return target


def daily(db_path, restore_dir=None, today=None) -> Path | None:
    """One copy per calendar day. Returns None if today's copy already exists."""
    today = today or dt.date.today()
    target = _dir(restore_dir, db_path) / "opplist_{}.db".format(today.strftime("%Y%m%d"))
    if target.exists():
        return None
    return _copy(db_path, target)


def before_task(db_path, task: str, restore_dir=None, now=None) -> Path:
    """A named copy taken before a migration or import, which is never pruned."""
    now = now or dt.datetime.now()
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-") or "task"
    target = _dir(restore_dir, db_path) / "opplist_before_{}_{}.db".format(
        slug, now.strftime("%Y%m%d_%H%M%S"))
    return _copy(db_path, target)


def prune(keep: int, restore_dir=None, db_path=None) -> list:
    """Delete the oldest daily copies beyond `keep`. Named copies stay."""
    directory = _dir(restore_dir, db_path)
    daily_files = sorted(
        (p for p in directory.glob("opplist_*.db") if DAILY_PATTERN.match(p.name)),
        key=lambda p: p.name,
        reverse=True,
    )
    removed = []
    for path in daily_files[max(keep, 0):]:
        path.unlink()
        removed.append(path)
    return removed
