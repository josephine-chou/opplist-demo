"""Where things live.

Code, schema and fixtures sit in the project folder. Data - the database,
restore points, the mail archive, exports and reports - lives in one data
folder: ./data by default, or the folder named by the environment variable
OPPLIST_DATA_DIR (the demo scripts set it to ./demo). Every path is derived
from those two anchors rather than from the current directory.

One more layout exists for a PC where the app runs every day from git
worktrees of the last commit (off unless asked for, see BESIDE below): there
the data folder sits beside the project folder, so the worktrees share it.
"""

from __future__ import annotations

import os
from pathlib import Path

# .../opplist-demo/opplist/paths.py -> project = .../opplist-demo
PROJECT_DIR = Path(__file__).resolve().parent.parent
#: The folder the project folder sits in: worktrees of it (see below) and
#: nothing else of the app are looked for there.
COMPANY_DIR = PROJECT_DIR.parent

#: OP1 / OP2: the git worktrees beside the project folder that run the last
#: commit (the morning run, the app on 8765). They hold code only.
RUN_WORKTREE_NAME = "opplist-run"
SERVE_WORKTREE_NAME = "opplist-serve"
MAIN_NAME = "opplist"
#: The project folder that is edited: what the app writes under
#: the project (export-preview\) goes there even when the code runs from a
#: worktree (OP2).
MAIN_DIR = (COMPANY_DIR / MAIN_NAME
            if PROJECT_DIR.name in (RUN_WORKTREE_NAME, SERVE_WORKTREE_NAME) else PROJECT_DIR)
#: The worktrees are off unless asked for (OPPLIST_WORKTREES=1, __main__).
#: Asked for, or running from one, the layout is the one they were written
#: for: the data folder beside the project folder, shared by every worktree.
BESIDE = (os.environ.get("OPPLIST_WORKTREES", "").strip() == "1"
          or PROJECT_DIR.name in (RUN_WORKTREE_NAME, SERVE_WORKTREE_NAME))


def code_folders() -> list:
    """Every folder that holds OppList's code (OP2): this one, the main
    folder and both worktrees. A key file is never kept inside any of them."""
    out = []
    for folder in (PROJECT_DIR, MAIN_DIR, COMPANY_DIR / MAIN_NAME,
                   COMPANY_DIR / RUN_WORKTREE_NAME, COMPANY_DIR / SERVE_WORKTREE_NAME):
        if folder not in out:
            out.append(folder)
    return out


def code_folder_of(path) -> str | None:
    """The name of the code folder that holds path, or None."""
    try:
        target = Path(path).resolve()
    except (OSError, ValueError):
        return None
    for folder in code_folders():
        try:
            if target.is_relative_to(Path(folder).resolve()):
                return Path(folder).name
        except (OSError, ValueError):
            continue
    return None


SCHEMA_SQL = PROJECT_DIR / "schema.sql"
FIXTURES_DIR = PROJECT_DIR / "fixtures"

DATA_DIR = (Path(os.environ["OPPLIST_DATA_DIR"]).expanduser().resolve()
            if os.environ.get("OPPLIST_DATA_DIR") else (COMPANY_DIR if BESIDE else PROJECT_DIR) / "data")
WORKBOOKS_DIR = DATA_DIR / "workbooks"
RESTORE_POINTS_DIR = DATA_DIR / "restore-points"

# Exports land in a preview folder by default and only reach the real paths with an explicit
# --publish, so that no scheduled run can be fed app output by accident.
EXPORT_PREVIEW_DIR = (MAIN_DIR if BESIDE else DATA_DIR) / "export-preview"

DB_PATH = DATA_DIR / "opplist.db"
LOG_PATH = DATA_DIR / "opplist.log"
#: The live files, fixed (13c): tests patch DATA_DIR / DB_PATH / LOG_PATH,
#: never these - they say what must not be written by a test or a copy.
LIVE_DATA_DIR = DATA_DIR
LIVE_DB_PATH = DB_PATH
LIVE_LOG_PATH = LOG_PATH

# The legacy workbook that was the master before the app took over. Read
# only, and after the switchover it is not in workbooks\ any more: it is moved
# into restore-points\ so that nothing can mistake it for live data.
SOURCE_WORKBOOK = WORKBOOKS_DIR / "LATAM Opportunity List.xlsx"
ARCHIVED_WORKBOOK = RESTORE_POINTS_DIR / "LATAM Opportunity List.xlsx"


def source_workbook() -> Path | None:
    """The workbook to verify a migration against, wherever it now lives.

    Before the switchover that is the master in the workbooks folder;
    afterwards it is the copy in restore-points, or the dated pre-cutover
    snapshot beside it.
    None means there is nothing to compare against any more.
    """
    if SOURCE_WORKBOOK.exists():
        return SOURCE_WORKBOOK
    if ARCHIVED_WORKBOOK.exists():
        return ARCHIVED_WORKBOOK
    snapshots = sorted(RESTORE_POINTS_DIR.glob("LATAM OppList pre-cutover-*.xlsx"))
    return snapshots[-1] if snapshots else None

# What the app produces. Export-OppIndex.ps1 picks the newest
# '*Opportunity List*.xlsx' in workbooks\, so this name stays recognisable.
EXPORT_WORKBOOK_NAME = "LATAM Opportunity List (auto-export).xlsx"
OPP_INDEX_NAME = "opp-index.csv"
AGENTS_CSV_NAME = "agents.csv"

THREAD_OPP_MAP = DATA_DIR / "thread-opp-map.csv"
RATE_REQUEST_DATA = DATA_DIR / "rate-request-data.csv"
#: The agent directory import-agents reads.
AGENT_DIRECTORY_WORKBOOK = WORKBOOKS_DIR / "AGENT DIRECTORY.xlsx"
#: Where the month report files go (Report YYYY-MM.html / .xlsx).
REPORTS_DIR = DATA_DIR / "reports"


#: The settings that name a folder (FX1).
FOLDER_SETTINGS = ("export_share_dir", "offsite_backup_dir")


def folder_setting(value) -> str:
    """A folder setting as a path: whitespace and one pair of surrounding
    quotes (" or ') taken off - Windows' Copy as path puts them around it.
    Saving stores it this way; every reader cleans it again, so a value
    saved with its quotes before FX1 works as it is."""
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text
