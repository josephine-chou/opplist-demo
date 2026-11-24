"""Reading and validating the enquiry form.

The conditional rules live here once. The browser greys out the fields that do
not apply, but the browser is only a convenience: every rule is checked again
on the server, because a form can always arrive with JavaScript turned off or
a field re-enabled.

Quantity fields are never hidden, only relaxed. Hiding them would hide data
that is already recorded.
"""

from __future__ import annotations

import datetime as dt

from .. import branding, status as status_mod

SERVICE_MODES = ["Air", "Sea-Air", "LCL", "FCL", "RORO", "Break Bulk", "Project",
                 "Truck", "Cross-border Truck", "Multi-mode", "Not specified"]  # TK1: Truck (was Domestic); SA1
#: SA1: the modes that take the Air cargo fields (chargeable weight, no TEU).
AIR_MODES = ("Air", "Sea-Air")

INCOTERMS = ["EXW", "FCA", "FAS", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP",
             "DPU", "DDP", "DDU"]

# Which incoterms make an alternative address compulsory, when
# the route's Service does not say (batch S1: FAS no longer - the seller
# delivers alongside the vessel, so the origin is the port).
ORIGIN_ALT_INCOTERMS = {"EXW", "FCA"}
DEST_ALT_INCOTERMS = {"DAP", "DPU", "DDP", "DDU"}

#: Batch S1: the Service says which legs the requester wants from us, so it
#: decides which places the route needs - (pickup, delivery). The incoterm
#: only says who pays for which leg between buyer and seller; it decides
#: when the Service is empty or a value outside these four (Settings can
#: add others). A mismatch (EXW quoted Port to port) is not an error.
SERVICE_PLACES = {
    "Door to door": (True, True),
    "Door to port": (True, False),
    "Port to door": (False, True),
    "Port to port": (False, False),
}
_SERVICE_KEYS = {k.casefold(): k for k in SERVICE_PLACES}


def places_needed(incoterm: str | None, service: str | None) -> dict:
    """{'origin_alt': who asks or None, 'dest_alt': who asks or None}: the
    pickup and delivery places a route needs, each with the name of what
    decided ('Door to port', 'EXW'). The one rule; missing(), field_state()
    and app.js's fieldState() follow it."""
    known = _SERVICE_KEYS.get((service or "").strip().casefold())
    if known:
        pickup, delivery = SERVICE_PLACES[known]
        return {"origin_alt": known if pickup else None, "dest_alt": known if delivery else None}
    return {"origin_alt": incoterm if incoterm in ORIGIN_ALT_INCOTERMS else None,
            "dest_alt": incoterm if incoterm in DEST_ALT_INCOTERMS else None}

FREE_TEXT_QTY_MODES = {"RORO", "Break Bulk", "Project", "Multi-mode", "Truck", "Cross-border Truck"}

#: Fields the form owns. Anything else on the enquiry row is set elsewhere.
ENQUIRY_FIELDS = (
    "received_on", "requester_party_id", "end_customer", "owner",
    "service_mode", "container_type", "container_qty", "air_weight_kg",
    "lcl_cbm", "qty_raw", "origin_country", "origin_loc", "origin_alt",
    "dest_country", "dest_loc", "dest_alt", "incoterm", "commodity",
    "special_handling", "remarks", "source_subject", "thread_key",
    "conversation_id", "entry_id", "status", "scope", "handled_by",
)

#: A case can be opened at any of the open statuses: Victor often quotes
#: before anybody writes to us. Sourcing is retired (2026-09-24).
OPEN_STATUSES = list(status_mod.OPEN_STATUSES)


def _text(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _number(value, field, errors):
    value = _text(value)
    if value is None:
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        errors[field] = "must be a number"
        return None


def _integer(value, field, errors):
    number = _number(value, field, errors)
    if number is None:
        return None
    if number != int(number):
        errors[field] = "must be a whole number"
        return None
    return int(number)


def _date(value, field, errors, required=False):
    value = _text(value)
    if value is None:
        if required:
            errors[field] = "required"
        return None
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        errors[field] = "use YYYY-MM-DD"
        return None


def read(form, tags=(), only_posted: bool = False) -> dict:
    """Turn the raw form into enquiry column values. No validation yet.

    `only_posted` (15a, the case page): each card posts only its own
    fields, so a field the form does not carry is left out instead of
    blanked - received_on, end_customer and the requester included, and
    special_handling only when the form says it carries the boxes
    (special_handling_present: unticked boxes are not posted)."""
    errors: dict = {}
    values = {
        "received_on": _date(form.get("received_on"), "received_on", errors),
        "end_customer": _text(form.get("end_customer")),
        "service_mode": _text(form.get("service_mode")),
        "container_type": _text(form.get("container_type")),
        "container_qty": _integer(form.get("container_qty"), "container_qty", errors),
        "air_weight_kg": _number(form.get("air_weight_kg"), "air_weight_kg", errors),
        "lcl_cbm": _number(form.get("lcl_cbm"), "lcl_cbm", errors),
        "qty_raw": _text(form.get("qty_raw")),
        "origin_country": _text(form.get("origin_country")),
        "origin_loc": _text(form.get("origin_loc")),
        "origin_alt": _text(form.get("origin_alt")),
        "dest_country": _text(form.get("dest_country")),
        "dest_loc": _text(form.get("dest_loc")),
        "dest_alt": _text(form.get("dest_alt")),
        "incoterm": _text(form.get("incoterm")),
        "commodity": _text(form.get("commodity")),
        "special_handling": ";".join(tags) if tags else None,
        "remarks": _text(form.get("remarks")),
        "source_subject": _text(form.get("source_subject")),
    }
    # v33: the lane, mode and incoterm are route A's and the case page's main
    # form no longer posts them (each route saves through its own form);
    # the quantity fields a form does not carry are not blanked either
    for key in ("service_mode", "incoterm", "origin_country", "origin_loc", "origin_alt",
                "dest_country", "dest_loc", "dest_alt", "air_weight_kg", "lcl_cbm",
                "container_type", "container_qty", "qty_raw", "remarks", "source_subject", "commodity"):
        if key not in form:
            values.pop(key, None)
    # 13c: the typed gross weight and volume in any unit - '2,200 lbs',
    # '25 t', '200 cft', or a number in the unit picked beside the box;
    # stored as kg and cbm
    from .. import cargo
    if "gross_kg" in form:
        text = (form.get("gross_kg") or "").strip()
        if text:
            number, unit = cargo.read_weight(text)
            unit = unit or (form.get("gross_unit") or "kg")
            if number is None:
                errors["gross_kg"] = cargo.NEEDS_NUMBER
                values["gross_kg"] = None
            else:
                values["gross_kg"] = round(number * cargo.WEIGHT_UNITS.get(unit, 1.0), 3)
        else:
            values["gross_kg"] = None
    if "volume_cbm" in form:
        text = (form.get("volume_cbm") or "").strip()
        if text:
            number = cargo.read_volume_cbm(text, form.get("volume_unit") or "cbm")
            if number is None:
                errors["volume_cbm"] = cargo.NEEDS_NUMBER
            values["volume_cbm"] = number
        else:
            values["volume_cbm"] = None
    requester = _text(form.get("requester_party_id"))
    values["requester_party_id"] = int(requester) if requester else None
    if only_posted:
        for key in ("received_on", "end_customer", "requester_party_id"):
            if key not in form:
                values.pop(key, None)
        if "special_handling_present" not in form:
            values.pop("special_handling", None)
    # Owner and scope are only written when the form carries them: the
    # form no longer shows either, and a field left out must not be blanked.
    if "owner" in form:
        values["owner"] = _text(form.get("owner"))
    if "scope" in form:
        values["scope"] = "Out" if _text(form.get("scope")) == "Out" else "In"
    # Handled by another of our offices (v31): the home office, or nothing,
    # is NULL; anything else is an office's three-letter code ('JQM').
    if "handled_by" in form:
        office = (_text(form.get("handled_by")) or "").upper()
        if office in ("", branding.HOME_OFFICE):
            values["handled_by"] = None
        elif len(office) == 3 and office.isalpha():
            values["handled_by"] = office
        else:
            errors["handled_by"] = "not one of our office codes"
    # Only the new-case form posts a status. The case page changes it through
    # the status dialogue, which writes an event, so it must not arrive here.
    status = _text(form.get("status"))
    if status:
        if status in OPEN_STATUSES:
            values["status"] = status
        else:
            errors["status"] = "not a status a new case can start at"
    if values.get("origin_loc"):
        values["origin_loc"] = values["origin_loc"].strip().upper()
    if values.get("dest_loc"):
        values["dest_loc"] = values["dest_loc"].strip().upper()
    return values, errors


def missing(values: dict) -> dict:
    """The completeness rules of the conditional-field table: what a case still
    lacks (requester, received_on, the quantity the mode needs, the pickup
    or delivery place the route's Service needs - or its incoterm, with no
    Service; values['service_scope']), as {field: message}. The
    register form refuses on them; the case page saves and names them
    (2026-09-28: an old gap must not block every later save)."""
    out = {}
    if not values.get("requester_party_id"):
        out["requester_party_id"] = "pick who is asking"
    if not values.get("received_on"):
        out["received_on"] = "required"
    mode = values.get("service_mode")
    if mode in AIR_MODES and values.get("air_weight_kg") is None:
        out["air_weight_kg"] = "required for {}".format(mode)
    if mode == "LCL" and values.get("lcl_cbm") is None:
        out["lcl_cbm"] = "required for LCL"
    if mode == "FCL":
        if not values.get("container_type"):
            out["container_type"] = "required for FCL"
        if values.get("container_qty") is None:
            out["container_qty"] = "required for FCL"
    if mode in FREE_TEXT_QTY_MODES and not values.get("qty_raw"):
        out["qty_raw"] = "describe the pieces or dimensions"
    needed = places_needed(values.get("incoterm"), values.get("service_scope"))
    if needed["origin_alt"] and not values.get("origin_alt"):
        out["origin_alt"] = "{} needs the pickup place".format(needed["origin_alt"])
    if needed["dest_alt"] and not values.get("dest_alt"):
        out["dest_alt"] = "{} needs the delivery place".format(needed["dest_alt"])
    return out


def validate(values: dict, errors: dict, complete: bool = True) -> dict:
    """Apply the conditional-field table. Adds to `errors` and returns it. With
    complete=False (the case page) only what can never be saved is added
    (container qty below 1); the gaps are missing()'s, named after the save."""
    # setdefault: a value that failed to parse already has the more useful
    # message ('must be a number'), and 'required' would bury it.
    if complete:
        for field, message in missing(values).items():
            errors.setdefault(field, message)
    qty = values.get("container_qty")
    if values.get("service_mode") == "FCL" and qty is not None and qty < 1:
        errors["container_qty"] = "at least 1"
    return errors


#: The field names as the form shows them, for the Not saved banner and the
#: 'Still missing' line (2026-09-28).
FIELD_LABELS = {
    "requester_party_id": "Requester", "received_on": "Received on", "end_customer": "End customer",
    "handled_by": "Handled by", "status": "Status", "service_mode": "Service mode", "incoterm": "Incoterm",
    "container_type": "Container type", "container_qty": "Container qty", "commodity": "Commodity",
    "air_weight_kg": "Air weight (kg)", "lcl_cbm": "LCL volume (cbm)", "qty_raw": "Quantity as received",
    "origin_country": "Origin country", "origin_loc": "Origin location", "origin_alt": "Origin alt (pickup)",
    "dest_country": "Dest. country", "dest_loc": "Dest. location", "dest_alt": "Dest. alt (delivery)",
    "remarks": "Remarks", "source_subject": "Source subject",
    "gross_kg": "Gross weight (kg)", "volume_cbm": "Volume (cbm)",
    "service_scope": "Service", "pod": "Port of discharge", "result": "Result", "lost_reason": "Lost reason",
}
#: The element each field's banner link jumps to: its own id where it had
#: one, fld-<name> where _fields.html gave it one for this.
FIELD_ANCHORS = {"requester_party_id": "requester", "end_customer": "end_customer", "handled_by": "handled_by",
                 "service_mode": "service_mode", "incoterm": "incoterm", "qty_raw": "qty_raw",
                 "origin_country": "origin_country", "origin_loc": "origin_loc",
                 "dest_country": "dest_country", "dest_loc": "dest_loc"}


def error_items(errors: dict) -> list:
    """[{field, label, anchor, message}] in the form's order."""
    order = list(FIELD_LABELS)
    fields = sorted(errors, key=lambda f: order.index(f) if f in order else len(order))
    return [{"field": f, "label": FIELD_LABELS.get(f, f.replace("_", " ").capitalize()),
             "anchor": FIELD_ANCHORS.get(f, "fld-" + f), "message": errors[f]} for f in fields]


def missing_line(gaps: dict) -> str:
    """'Dest. alt (delivery), DAP needs the delivery place; ...'"""
    return "; ".join("{}, {}".format(i["label"], i["message"]) for i in error_items(gaps))


def field_state(mode: str | None, incoterm: str | None, service: str | None = None) -> dict:
    """What the form should look like: 'required', 'normal' or 'dim'.

    The template and app.js share this so that the greying agrees with the
    server rules instead of drifting from them.
    """
    state = {name: "dim" for name in
             ("air_weight_kg", "lcl_cbm", "container_type", "container_qty",
              "qty_raw", "origin_alt", "dest_alt")}
    if mode in AIR_MODES:
        state["air_weight_kg"] = "required"
        state["lcl_cbm"] = "normal"
    elif mode == "LCL":
        state["lcl_cbm"] = "required"
        state["air_weight_kg"] = "normal"
    elif mode == "FCL":
        state["container_type"] = "required"
        state["container_qty"] = "required"
    elif mode in FREE_TEXT_QTY_MODES:
        state["qty_raw"] = "required"
    elif mode == "Not specified":
        for name in ("air_weight_kg", "lcl_cbm", "container_type",
                     "container_qty", "qty_raw"):
            state[name] = "normal"
    needed = places_needed(incoterm, service)
    if needed["origin_alt"]:
        state["origin_alt"] = "required"
    if needed["dest_alt"]:
        state["dest_alt"] = "required"
    return state


LABELS = {
    "Air": {"lcl_cbm": "Volume (cbm), for chargeable weight"},
    "Sea-Air": {"lcl_cbm": "Volume (cbm), for chargeable weight"},
    "LCL": {"air_weight_kg": "Weight (kg)"},
}


def labels_for(mode: str | None) -> dict:
    return LABELS.get(mode or "", {})
