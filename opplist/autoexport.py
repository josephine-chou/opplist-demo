"""Re-export shortly after a change.

Debounced: a burst of edits produces one export, sixty seconds after the last
of them, on a background thread with its own connection.

It only runs when export_target is 'publish'. While the export is a preview
nobody reads, rewriting it every minute would be work for nothing - and it
would also mean the app quietly rewrites files on a timer, which is the last
thing that should happen before anyone relies on the published files.

OP1 (2026-11-10): the debounced export writes data\\opp-index.csv and
data\\agents.csv only. The workbook was rewritten 674 times in October and
nobody reads it (Export-OppIndex.ps1 is its one reader and Daily-Scan.cmd
has not run since August); the Export page and `python -m opplist export`
still write it.
"""

from __future__ import annotations

import threading

from . import db, export_csv, export_xlsx, logs, paths

DEFAULT_DELAY = 60.0


def target(conn) -> str:
    """'preview' or 'publish' (setting export_target)."""
    value = (db.get_setting(conn, "export_target", "preview") or "").strip().lower()
    return "publish" if value == "publish" else "preview"


def directories(mode: str):
    """(xlsx directory, csv directory) for an export target."""
    if mode == "publish":
        return paths.WORKBOOKS_DIR, paths.DATA_DIR
    return paths.EXPORT_PREVIEW_DIR, paths.EXPORT_PREVIEW_DIR


def export_now(db_path, mode=None, months=None, workbook=True) -> dict:
    """Write all three files (workbook=False: the two CSV files only, the
    auto export since OP1). Returns what was written, for the screen."""
    conn = db.open_db(db_path)
    try:
        mode = mode or target(conn)
        out_dir, data_dir = directories(mode)
        written = {
            "mode": mode,
            "workbook": export_xlsx.export(
                conn, out_dir / paths.EXPORT_WORKBOOK_NAME,
                db_path=str(db_path), months=months) if workbook else None,
            "opp_index": export_csv.export_opp_index(
                conn, data_dir / paths.OPP_INDEX_NAME),
            "agents": export_csv.export_agents(
                conn, data_dir / paths.AGENTS_CSV_NAME),
        }
        share = paths.folder_setting(db.get_setting(conn, "export_share_dir", "")) \
            if mode == "publish" and workbook else ""
    finally:
        conn.close()

    written["share"] = None
    if share:
        # A share that is offline must not fail an export.
        try:
            import shutil
            from pathlib import Path
            copy = Path(share) / written["workbook"].name
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(written["workbook"], copy)
            written["share"] = copy
        except OSError as exc:
            logs.get("autoexport").warning("share copy failed: %s", exc)
            written["share_error"] = str(exc)
    return written


class AutoExporter:
    """One pending export at a time, restarted by every write."""

    def __init__(self, db_path, delay: float = DEFAULT_DELAY):
        self.db_path = str(db_path)
        self.delay = delay
        self._timer = None
        self._lock = threading.Lock()
        self.last_result = None
        self.last_error = None

    # -- state ------------------------------------------------------------
    def enabled(self) -> bool:
        conn = db.open_db(self.db_path)
        try:
            if target(conn) != "publish":
                return False
            return (db.get_setting(conn, "auto_export", "1") or "1").strip() == "1"
        finally:
            conn.close()

    @property
    def pending(self) -> bool:
        with self._lock:
            return self._timer is not None and self._timer.is_alive()

    # -- driving ----------------------------------------------------------
    def schedule(self) -> bool:
        """Call after a write. Returns whether an export is now pending."""
        if not self.enabled():
            return False
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay, self._run)
            self._timer.daemon = True
            self._timer.start()
        return True

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _run(self) -> None:
        try:
            self.last_result = export_now(self.db_path, workbook=False)
            self.last_error = None
            logs.get("autoexport").info("auto export wrote %s and %s",
                                        self.last_result["opp_index"],
                                        self.last_result["agents"])
        except Exception as exc:                  # a failed export must not
            self.last_error = str(exc)            # take the server down
            logs.get("autoexport").exception("auto export failed")
        finally:
            with self._lock:
                self._timer = None
