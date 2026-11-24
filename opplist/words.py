"""The words Iris reads (batch 18, 2026-10-01): JoJo JJC writes
American English and the trade's own term. Every user-facing text says
inquiry, never enquiry, and Triangle, never Cross-trade; code names stay
(tables, views, routes and URLs, identifiers, stored values, settings keys)
and so do the header rows the scripts read.

A direction is stored and filtered by its value ('Cross-trade', from
v_enquiry / v_route); direction_label() is what a page, the month report or
an export for reading shows."""

from __future__ import annotations

DIRECTION_LABELS = {"Cross-trade": "Triangle"}


def direction_label(value) -> str:
    """'Cross-trade' -> 'Triangle'; every other direction as it is."""
    if value is None:
        return ""
    return DIRECTION_LABELS.get(value, value)
