"""Status vocabulary and the two mappings around it.

The app has six statuses. The workbook and the PowerShell pipeline have nine
strings between them, so every value crossing that boundary goes through here
and nowhere else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Whose rate a won case used (v8). The order is the order on
#: the form.
RATE_USED = (
    ("agent", "Agent rate"),
    ("office", "Office rate"),
    ("own", "Own rate"),
    ("carrier", "Carrier direct"),
    ("not_recorded", "Not recorded"),
)
#: The answers that name a party (awarded_vendor_id).
RATE_USED_WITH_PARTY = ("agent", "office", "carrier")
RATE_USED_LABELS = dict(RATE_USED)

#: Sourcing is retired (2026-09-24): a case is New until a quote goes out,
#: then Quoted. Nothing writes it any more and no picker offers it, but old
#: status_event rows still carry it and the CHECK constraints still allow
#: it, so it keeps a rank (STAGE_RANK) and an export string (to_legacy).
OPEN_STATUSES = ("New", "Quoted")
CLOSED_STATUSES = ("Won", "Lost", "Cancelled", "Dropped")
#: The closings that carry a reason from their own list.
REASON_STATUSES = ("Lost", "Cancelled", "Dropped")
ALL_STATUSES = OPEN_STATUSES + CLOSED_STATUSES
#: The retired status, for reading history only.
RETIRED_STATUSES = ("Sourcing",)
#: The note on the event retire-sourcing writes.
SOURCING_RETIRED_NOTE = "Sourcing retired, back to New"

#: The kinds of status_event that say what the case is. 'requote' (v19) is
#: a quote sent again - since v30 (Re-quote means re-quoted) it carries
#: status Quoted and ranks with Quoted. Older ones carry Sourcing (v19 to
#: v28) or New (batch 8, never used on the live data).
STATUS_KINDS = ("status", "requote")
STATUS_KIND_SQL = "kind IN ('status','requote')"

#: How far along a status is. Two events on the same day are ordered by
#: this before the row id: a case with New and Quoted both dated today is
#: Quoted. The three closed statuses rank equal (the later row wins), and
#: so do Quoted and a re-quote (v19): a re-quote written after today's
#: quote makes the case New again, a quote written after today's re-quote
#: makes it Quoted again - the newest of the two says where the round is.
#: Sourcing is retired but keeps its rank so old events still sort. The
#: same rule is written out in v_enquiry (schema.sql) and verify.py - keep
#: the three together.
STAGE_RANK = {"New": 1, "Sourcing": 2, "Quoted": 3, "Won": 4, "Lost": 4, "Cancelled": 4, "Dropped": 4}
REQUOTE_RANK = 3
STAGE_RANK_SQL = ("CASE WHEN kind = 'requote' THEN 3 WHEN status = 'New' THEN 1 "
                  "WHEN status = 'Sourcing' THEN 2 WHEN status = 'Quoted' THEN 3 ELSE 4 END")
#: ORDER BY for 'the event that says what the case is now'.
LATEST_EVENT_ORDER = "at DESC, {} DESC, id DESC".format(STAGE_RANK_SQL)


#: The vocabularies (2026-09-18; Cancelled 2026-10-02, v38).
#: They answer different questions, so they are separate lists:
#:   Won       = we got it.
#:   Lost      = the shipment went ahead, or will, without us.
#:   Cancelled = the shipment did not go ahead at all (also after it was
#:               awarded to us). A postponement is not a closing.
#:   Dropped   = this row should not exist.
REASON_SETTING = {"Lost": "closed_reasons_lost", "Cancelled": "closed_reasons_cancelled",
                  "Dropped": "closed_reasons_dropped"}
DEFAULT_REASONS_LOST = (
    "Price too high", "Rate too slow", "No response from customer",
    "Unable to handle", "No vendor rate",
    "Went with another forwarder", "Customer chose the other mode", "Other",
)
#: SC1 (2026-11-10): the losing route of a quote-both case (one route per
#: mode); such a route is never a lost case anywhere
OTHER_MODE_REASON = "Customer chose the other mode"
DEFAULT_REASONS_CANCELLED = ("Shipment cancelled", "Order not confirmed",
                             "Postponed with no new date", "Other")
DEFAULT_REASONS_DROPPED = ("Duplicate of OPP-xxxx", "Registered in error")
DEFAULT_REASONS = {"Lost": DEFAULT_REASONS_LOST, "Cancelled": DEFAULT_REASONS_CANCELLED,
                   "Dropped": DEFAULT_REASONS_DROPPED}
#: L2 (2026-10-21): the Lost reason for a case nobody answered, by who
#: asked - an agent, one of our offices, anyone else (a customer).
NO_RESPONSE_AGENT = "No response from agent"
NO_RESPONSE_OFFICE = "No response from other office / sales"
NO_RESPONSE_CUSTOMER = "No response from customer"
NO_RESPONSE_REASONS = (NO_RESPONSE_AGENT, NO_RESPONSE_OFFICE, NO_RESPONSE_CUSTOMER)


def no_response_reason(requester_type) -> str:
    """The No response reason for a case whose requester is of this party
    type (the Decide card's batch button and its rows' Lost...)."""
    if (requester_type or "") == "Agent":
        return NO_RESPONSE_AGENT
    if (requester_type or "") == "Office":
        return NO_RESPONSE_OFFICE
    return NO_RESPONSE_CUSTOMER


#: A Lost case's Their price / Our price (v21): enquiry columns, their
#: labels, and the longest entry kept (FX1: 200, was 80).
LOST_PRICES = (("lost_their_price", "Their price"), ("lost_our_price", "Our price"))
PRICE_MAX = 200


def lost_prices(get, keep_blank: bool = False) -> tuple:
    """The two prices from a form or a decision (`get` is its .get), each
    stripped and cut at PRICE_MAX: ({column: value}, [labels cut]). A blank
    one is left out, or kept as None (it clears the price) with keep_blank."""
    prices, cut = {}, []
    for name, label in LOST_PRICES:
        value = (get(name) or "").strip()
        if len(value) > PRICE_MAX:
            value = value[:PRICE_MAX].rstrip()
            cut.append(label)
        if value or keep_blank:
            prices[name] = value or None
    return prices, cut


def prices_cut_text(cut) -> str:
    """The message line for prices cut at PRICE_MAX, or ''."""
    return "{} cut at {} characters.".format(" and ".join(cut), PRICE_MAX) if cut else ""


#: The reason a shipment cancelled before v38 carried as a Lost reason.
SHIPMENT_CANCELLED = "Shipment cancelled"
#: 'Duplicate of OPP-1412' is the entry 'Duplicate of OPP-xxxx' with a real
#: number in it: one shape, many values. 'Duplicate of (TBC)' is the same
#: shape with the number still to be found.
_OPP_NUMBER = re.compile(r"OPP-\d{3,}", re.I)
_DUPLICATE_OF = re.compile(r"duplicate of\b.*", re.I)


def reason_shape(reason: str) -> str:
    """The reason as the settings list spells it: a duplicate folded back to
    'Duplicate of OPP-xxxx', any other case number to 'OPP-xxxx'."""
    text = (reason or "").strip()
    if _DUPLICATE_OF.match(text):
        return DEFAULT_REASONS_DROPPED[0]
    return _OPP_NUMBER.sub("OPP-xxxx", text)


def closed_reasons(conn, status: str | None = None) -> list:
    """The reasons a status may carry; every list when no status is given."""
    from . import db

    if status in REASON_SETTING:
        stored = db.get_setting_list(conn, REASON_SETTING[status])
        return [str(r) for r in stored] or list(DEFAULT_REASONS[status])
    out = []
    for key in ("Lost", "Cancelled", "Dropped"):
        for reason in closed_reasons(conn, key):
            if reason not in out:
                out.append(reason)
    return out


def reason_belongs(conn, status: str, reason: str | None) -> bool:
    """False when the reason comes from the other status's list. A blank
    reason is somebody else's problem (the form makes it required)."""
    if not (reason or "").strip():
        return True
    if status not in REASON_SETTING:
        return True
    wanted = reason_shape(reason).casefold()
    return wanted in {reason_shape(r).casefold() for r in closed_reasons(conn, status)}


def ensure_closed_reason(conn, wanted: str, status: str = "Lost") -> str:
    """The closed reason as the settings spell it (matched without regard
    to case), added to that status's list when it is not there yet."""
    from . import db

    key = REASON_SETTING.get(status, REASON_SETTING["Lost"])
    reasons = closed_reasons(conn, status if status in REASON_SETTING else "Lost")
    for reason in reasons:
        if str(reason).strip().casefold() == wanted.strip().casefold():
            return str(reason)
    db.set_setting(conn, key, reasons + [wanted])
    return wanted


#: Why a quote was done again (v22): its own vocabulary,
#: on the Quoted event that reopens a round or on the requote event that
#: opened it. Not a closed reason: closed_reasons() never returns these.
REQUOTE_SETTING = "requote_reasons"
DEFAULT_REQUOTE_REASONS = ("Lower rate", "Request changed", "Rates expired",
                           "Different routing or carrier", "Extra option asked",
                           "Correction", "Other")
NOT_RECORDED = "Not recorded"


def requote_reasons(conn) -> list:
    from . import db

    stored = db.get_setting_list(conn, REQUOTE_SETTING)
    return [str(r) for r in stored] or list(DEFAULT_REQUOTE_REASONS)


def requote_reason_belongs(conn, reason: str | None) -> bool:
    """False when the reason is not on the requote list; a blank is fine
    (the reason is optional)."""
    if not (reason or "").strip():
        return True
    wanted = reason.strip().casefold()
    return wanted in {r.casefold() for r in requote_reasons(conn)}


def event_reason_list(conn, status: str, kind: str = "status") -> list:
    """The vocabulary an event's reason comes from: the closed lists for
    Lost / Cancelled / Dropped, the requote list for a Quoted or requote event, none
    for the rest."""
    if status in REASON_SETTING:
        return closed_reasons(conn, status)
    if status == "Quoted" or kind == "requote":
        return requote_reasons(conn)
    return []


def quote_rounds(conn, enquiry_id: int) -> list:
    """The case's quote rounds, oldest first: one dict per live Quoted
    event whatever its kind (v30: the Re-quoted button writes a requote
    event on Quoted, which is its own round) - {event_id, at, reason,
    opener_id, opener_reason, requote}.
    `requote` is True from the second round on. The reason of a round is
    the one on its Quoted event; when that is NULL it is the reason of the
    nearest earlier live requote event (the one that opened the round). The
    one place that rule lives (v22)."""
    rows = conn.execute(
        "SELECT id, at, kind, status, reason FROM status_event WHERE enquiry_id = ? "
        "AND voided = 0 AND {} AND (status = 'Quoted' OR kind = 'requote') "
        "ORDER BY at, id".format(STATUS_KIND_SQL), (enquiry_id,)).fetchall()
    out, opener = [], None
    for row in rows:
        if row["status"] != "Quoted":
            # an old requote event on New / Sourcing: it opened the round
            # the next Quoted closes (v18 to v29 data)
            opener = row
            continue
        reason = (row["reason"] or "").strip() or None
        opener_reason = (opener["reason"] or "").strip() if opener is not None else None
        out.append({
            "event_id": row["id"], "at": row["at"],
            "reason": reason or opener_reason or None,
            "own_reason": reason,
            "opener_id": opener["id"] if opener is not None else None,
            "opener_reason": opener_reason or None,
            "requote": bool(out),
        })
        opener = None
    return out


def stage_rank(status, kind: str = "status") -> int:
    if kind == "requote":
        return REQUOTE_RANK
    return STAGE_RANK.get(status or "", 0)


@dataclass(frozen=True)
class LegacyStatus:
    """How one old Status string lands in the new model."""

    status: str
    closed_reason: str | None = None
    legacy: bool = False
    confirmed: bool = True
    event_note: str | None = None


#: The key is the Status cell as it appears in the workbook;
#: '' is a blank cell, which the old sheet treated as a live case.
LEGACY_STATUS_MAP: dict[str, LegacyStatus] = {
    "New": LegacyStatus("New"),
    # Sourcing is retired (2026-09-24): a rate asked of a vendor is still New.
    "Sent to Vendor": LegacyStatus("New"),
    "Quoted - Awaiting Reply": LegacyStatus("Quoted"),
    "Secured": LegacyStatus("Won"),
    "In Progress / Shipped": LegacyStatus("Won", event_note="shipped (migrated)"),
    "Lost": LegacyStatus("Lost"),
    "No Response": LegacyStatus("Dropped", closed_reason="No Response from Customer"),
    "Unable to Handle": LegacyStatus("Dropped", closed_reason="Unable to Handle"),
    # v38: the workbook's own word for a shipment that did not go ahead
    "Customer Cancelled": LegacyStatus("Cancelled", closed_reason=SHIPMENT_CANCELLED),
    "Out of Scope": LegacyStatus("Dropped", closed_reason="Out of Scope"),
    # No reason is recorded: the outcome is genuinely unknown, and 'Other'
    # would read as if someone had chosen it.
    "Legacy - Unknown": LegacyStatus(
        "Dropped",
        legacy=True,
        event_note="legacy, outcome unknown",
    ),
    "": LegacyStatus("New", confirmed=False),
}


def from_legacy(value: str | None) -> LegacyStatus:
    """Map a workbook Status cell onto the new model. Unknown text is a New."""
    key = (value or "").strip()
    if key in LEGACY_STATUS_MAP:
        return LEGACY_STATUS_MAP[key]
    return LegacyStatus("New", confirmed=False)


def to_legacy(status: str, *, confirmed: bool = True, legacy: bool = False,
              closed_reason: str | None = None) -> str:
    """Render a status the way Sort-Inbox.ps1 and Export-OppIndex.ps1 expect.

    Sort-Inbox treats New / Sent to Vendor / Quoted - Awaiting Reply /
    In Progress / Shipped / '' as live, so open cases must land in that set.
    """
    if legacy:
        return "Legacy - Unknown"
    if status == "New":
        return "New" if confirmed else ""
    if status == "Sourcing":            # retired; an old row can still carry it
        return "Sent to Vendor"
    if status == "Quoted":
        return "Quoted - Awaiting Reply"
    if status == "Won":
        return "Secured"
    if status == "Lost":
        return "Lost"
    if status == "Cancelled":
        return "Customer Cancelled"
    if status == "Dropped":
        reason = (closed_reason or "").strip()
        if reason.casefold() == "unable to handle":
            return "Unable to Handle"
        # A duplicate and a mis-registration say so in the export. Neither is
        # in Sort-Inbox's live set, so both read as closed and no mail is
        # proposed for them. Since 2026-09-18 a duplicate carries
        # the case it duplicates ('Duplicate of OPP-1412'); the export string
        # stays the one word the pipeline knows.
        if reason.casefold().startswith("duplicate"):
            return "Duplicate"
        if reason.casefold() == "registered in error":
            return "Registered in error"
        return "No Response"
    raise ValueError(f"unknown status {status!r}")


def row_to_legacy(row) -> str:
    """to_legacy() for a v_enquiry row (sqlite3.Row or mapping)."""
    return to_legacy(
        row["status"],
        confirmed=bool(row["status_confirmed"]),
        legacy=bool(row["legacy"]),
        closed_reason=row["closed_reason"],
    )
