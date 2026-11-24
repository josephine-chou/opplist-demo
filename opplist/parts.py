"""The parts a vendor is asked to quote (batch Q2b, her design of 9 and 11
Nov; INTERFACE.md section 5): one list, in its fixed order.

vendor_request.parts keeps them '|'-separated in this order; NULL = not
said. Inland is the leg between the port and an inland point, by rail or
truck (IPI, her word); Door to door is the whole move. The lines of a
vendor's rates (section 9) use the same names, with Other instead of Door
to door.
"""

from __future__ import annotations

PARTS = ("Freight", "Inland", "Origin", "Destination", "Customs", "Door to door")
SEP = "|"


def split(text) -> list:
    """'Freight|Inland' -> ['Freight', 'Inland'] (known ones, in order)."""
    found = {p.strip() for p in str(text or "").split(SEP) if p.strip()}
    return [p for p in PARTS if p in found]


def join(parts) -> str | None:
    """The known parts in their fixed order, '|'-separated; None when none."""
    found = set(parts or [])
    out = [p for p in PARTS if p in found]
    return SEP.join(out) or None


def union(text, more) -> str | None:
    return join(split(text) + list(more or []))


def read(values) -> tuple:
    """(known parts in order, the names not known) out of what was sent."""
    sent = [str(v or "").strip() for v in (values or []) if str(v or "").strip()]
    known = [p for p in PARTS if p in sent]
    unknown = [v for v in sent if v not in PARTS]
    return known, unknown
