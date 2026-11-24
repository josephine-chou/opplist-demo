"""The standard questions of an acknowledgment, by mode and by Service
(batch A1). One place: the template's {missing_paragraph} reads
them from here.

Each question has a key, the words that go into "could you please let us
know: ...", and a test of whether the case already answers it. A question
the case answers is not asked; the case's own gaps (forms.missing and the
route's Still missing) map onto the same keys, so nothing is asked twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Question:
    key: str
    text: str
    answered: Callable                       # (facts) -> bool


def _has(*names):
    return lambda f: any(f.get(n) not in (None, "", 0) for n in names)


def _never(_facts) -> bool:
    return False


def _dg_known(f) -> bool:
    return f.get("dg") is not None


PIECES = Question("pieces", "the number of pieces", _has("pieces"))
DIMENSIONS = Question("dimensions", "the dimensions of each piece", _has("dimensions"))
GROSS_WEIGHT = Question("weight", "the gross weight", _has("weight"))
CBM_WEIGHT = Question("cbm", "the volume (cbm) and the gross weight", lambda f: bool(f.get("cbm") and f.get("weight")))
COMMODITY = Question("commodity", "the commodity", _has("commodity"))
DG = Question("dg", "whether the cargo is dangerous goods (DG) or not", _dg_known)
READY = Question("ready", "the cargo ready date", _never)
PICKUP = Question("pickup", "the pickup address", _has("origin_alt"))
DELIVERY = Question("delivery", "the delivery address", _has("dest_alt"))
CONTAINERS = Question("containers", "the container type and quantity",
                      lambda f: bool(f.get("container_type") and f.get("container_qty")))
WEIGHT_PER_CONTAINER = Question("weight", "the cargo weight per container", _has("weight"))
WEIGHT_PER_UNIT = Question("weight", "the weight per container or pallet", _has("weight"))
STRIPPED = Question("stripped", "whether the containers stay loaded or are stripped into the warehouse", _never)
DURATION = Question("duration", "the storage duration", _never)
SCHEDULE = Question("schedule", "the inbound and outbound schedule", _never)
CUSTOMS_STATUS = Question("customs_status", "the customs status of the cargo", _never)
FINAL_DELIVERY = Question("final_delivery", "the final delivery locations", _has("dest_alt"))
HS_CODES = Question("commodity", "the commodity and its HS codes", _never)
VALUE = Question("value", "the cargo value", _never)
DOCUMENTS = Question("documents", "the documents: commercial invoice, packing list, AWB or B/L", _never)
POA = Question("poa", "a POA", _never)
ISF_POA = Question("poa", "the ISF details and a POA", _never)

#: By Service first (Warehousing and Customs only replace the mode's list).
BY_SERVICE = {
    "warehousing": (COMMODITY, DG, WEIGHT_PER_UNIT, STRIPPED, DURATION, SCHEDULE, CUSTOMS_STATUS, FINAL_DELIVERY),
    "customs only": (HS_CODES, VALUE, DOCUMENTS),
}
BY_MODE = {
    "Air": (PIECES, DIMENSIONS, GROSS_WEIGHT, COMMODITY, DG, READY),
    "LCL": (CBM_WEIGHT, DIMENSIONS, COMMODITY),
    "FCL": (CONTAINERS, COMMODITY, WEIGHT_PER_CONTAINER),
}
#: SA1: a mode that asks what another mode asks.
ASKS_AS = {"Sea-Air": "Air", "Cross-border Truck": "Truck"}
#: forms.missing's fields, as the question that asks for them.
GAP_QUESTIONS = {
    "air_weight_kg": GROSS_WEIGHT,
    "lcl_cbm": CBM_WEIGHT,
    "container_type": CONTAINERS,
    "container_qty": CONTAINERS,
    "qty_raw": Question("pieces", "the number of pieces and their dimensions", _has("pieces", "dimensions")),
    "origin_alt": PICKUP,
    "dest_alt": DELIVERY,
}


def service_kind(service) -> str:
    """'warehousing', 'customs only' or '' for a route's Service."""
    text = (service or "").strip().casefold()
    return text if text in BY_SERVICE else ""


def standard(mode, service, incoterm) -> tuple:
    """The standard questions for a case's mode and Service, in order; the
    pickup or delivery address as the Service (or, without one, the
    incoterm) needs, for the modes that ask it."""
    from .web import forms
    mode = ASKS_AS.get(mode or "", mode)
    kind = service_kind(service)
    if kind == "warehousing":
        return BY_SERVICE[kind]
    if kind == "customs only":
        return BY_SERVICE[kind] + ((ISF_POA,) if mode in ("FCL", "LCL") else (POA,))
    out = list(BY_MODE.get(mode or "", (COMMODITY,)))
    needed = forms.places_needed(incoterm, service)
    if needed["origin_alt"] and mode in ("Air", "LCL", "FCL"):
        out.append(PICKUP)
    if needed["dest_alt"] and mode == "FCL":
        out.append(DELIVERY)
    return tuple(out)


def to_ask(facts: dict, gaps, mode, service, incoterm) -> list:
    """The words to ask, in order: the case's gaps first (forms.missing's
    fields), then the standard questions, each key once, none the case
    already answers."""
    out, keys = [], set()
    candidates = [GAP_QUESTIONS[g] for g in gaps if g in GAP_QUESTIONS]
    candidates += list(standard(mode, service, incoterm))
    for q in candidates:
        if q.key in keys or q.answered(facts):
            continue
        keys.add(q.key)
        out.append(q.text)
    return out
