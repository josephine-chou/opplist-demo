"""Quick notes on the case page (2026-09-25, schema v32),
removed 2026-11-04 (batch CX1).

The case_note rows stay in their table, as everything does; no page, route,
registration or team-site pull writes one any more, and nothing shows
them. What is left here reads them.
"""

from __future__ import annotations


def for_case(conn, enquiry_id: int) -> list:
    """The notes not deleted, newest first."""
    return conn.execute(
        "SELECT * FROM case_note WHERE enquiry_id = ? AND deleted_at IS NULL "
        "ORDER BY created_at DESC, id DESC", (enquiry_id,)).fetchall()


def get(conn, note_id: int):
    return conn.execute("SELECT * FROM case_note WHERE id = ?", (note_id,)).fetchone()
