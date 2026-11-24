"""Cargo checks (schema v34, 2026-09-29).

Computed on every page view from the case's cargo lines, the totals the
mail states (enquiry_cargo_stated) and the case itself; nothing here is
stored except the answers (enquiry_cargo.decided, enquiry.cargo_decided).
A check is 'ok' (green, passed or answered) or 'ask' (amber, to decide);
the Cargo card counts the asks next to its title.

    total       a total the mail states against the lines of its block
    repeat      a line repeated word for word in one mail (not counted)
    each        a weight or volume per piece with no pieces given
    reading     '1.940 kgs': thousands or decimals
    unit        a size with no unit: both readings, cm and in, with their
                density; the register page refuses until one is chosen (15c)
    size_unit   a size whose written unit gives a density outside the range
                while the other of cm / in gives one inside it (15c)
    stated_cbm  a volume the mail states against the size
    chargeable  a chargeable weight the mail states against ours (Air)
    density     kg per cbm outside the plausible range
    fcl         cargo per container over the limit for its size
    air / lcl   an Air or LCL case too big for its mode
    qty_mode    a quantity text that disagrees with the mode
    missing     a counted line without a number it needs (15b)
    per_month   a weight a month the mail states against the line's (15b)
    us_road     an FCL container on US roads over a standard chassis (15b)
    mail_size   a size in the mail against a line's stated volume (15b)

The limits live in Settings (setting 'cargo_limits'); the tolerance is the
percentage two figures may differ before a check turns amber.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import cargo, parsing

DEFAULT_LIMITS = {"density_min": 30, "density_max": 3000, "air_max_cbm": 30, "air_max_kg": 20000,
                  "lcl_max_cbm": 25, "lcl_max_kg": 15000, "fcl_max_kg_20": 20000, "fcl_max_kg_40": 25000,
                  "tolerance_pct": 10}
LIMIT_LABELS = {
    "density_min": "Density at least (kg per cbm)", "density_max": "Density at most (kg per cbm)",
    "air_max_cbm": "Air: at most (cbm)", "air_max_kg": "Air: at most (kg)",
    "lcl_max_cbm": "LCL: at most (cbm)", "lcl_max_kg": "LCL: at most (kg)",
    "fcl_max_kg_20": "FCL 20': cargo per container at most (kg)",
    "fcl_max_kg_40": "FCL 40': cargo per container at most (kg)",
    "tolerance_pct": "Stated and computed may differ by (%)",
}
#: a total is an exact sum: the lines match it within this share (or half a unit)
TOTAL_TOLERANCE = 0.01
_MODE_PREFIX = re.compile(r"^\s*(FCL|LCL|Air)\s*:", re.IGNORECASE)
_AIR_WORDS = re.compile(r"\b(?:volumetric|chargeable)\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d[\d.,]*")


@dataclass
class Check:
    code: str
    kind: str                         # 'ok' or 'ask'
    text: str
    line_id: int | None = None        # the line it is about (None: the case)
    row: int | None = None            # the line's row number on the card
    actions: list = field(default_factory=list)   # [(label, action, value)]
    short: str = ""                   # a few words for the rail ('weight missing', 15b)

    @property
    def key(self) -> str:
        return "{}:{}".format(self.code, self.line_id if self.line_id is not None else "case")


def limits(conn) -> dict:
    """The limits from Settings, the defaults filling any gap."""
    out = dict(DEFAULT_LIMITS)
    row = conn.execute("SELECT value FROM setting WHERE key = 'cargo_limits'").fetchone() if conn else None
    if row and row[0]:
        try:
            for key, value in json.loads(row[0]).items():
                if key in out and value not in (None, ""):
                    out[key] = float(value)
        except (ValueError, TypeError, AttributeError):
            pass
    return out


def decided_codes(text) -> set:
    return {c.strip() for c in (text or "").split(",") if c.strip()}


def add_code(text, code) -> str:
    codes = decided_codes(text)
    codes.add(code)
    return ",".join(sorted(codes))


def _n(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _kg(value) -> str:
    return "{:,.2f}".format(value) if abs(value) < 10 else "{:,.1f}".format(value)


def _plain(value, places=3) -> str:
    text = "{:,.{}f}".format(value, places)
    return text.rstrip("0").rstrip(".") if "." in text else text


def _pct(a, b) -> float:
    return abs(a - b) / b * 100.0 if b else 0.0


def _container_limit(ctype, lim) -> float | None:
    size = re.match(r"\s*(\d{2})", ctype or "")
    if not size:
        return None
    return lim["fcl_max_kg_20"] if size.group(1) == "20" else lim["fcl_max_kg_40"]


def _reading_of(line):
    """(plain, other, unit) of the line's weight as written, when it reads
    two ways; None otherwise."""
    written = line.get("weight_written") or ""
    found = _NUMBER.search(written)
    if not found:
        return None
    plain, other = parsing.readings(found.group(0).rstrip(".,"))
    if other is None or plain is None:
        return None
    unit = line.get("weight_unit") or "kg"
    return plain, other, unit


def _kg_of(line, value) -> float | None:
    """The line's kg had its weight been `value` (in its own unit)."""
    trial = dict(line)
    trial["weight_value"] = value
    return cargo.normalise({k: trial.get(k) for k in ("weight_value", "weight_unit", "weight_basis", "pieces")})["weight_kg"]


def size_readings(line, lim=None) -> dict | None:
    """A line's size read as cm and as in (15c): {unit: (cbm, kg per cbm or
    None, fits)} for both, from the numbers as written times the pieces;
    None when the line has no full size. `fits` is True when there is no
    weight to judge by."""
    dims = [_n(line.get(k)) for k in ("dim_l", "dim_w", "dim_h")]
    if not all(dims):
        return None
    lim = lim or DEFAULT_LIMITS
    d = cargo.derive(line)
    pieces, kg = d["pieces_n"] or 1, d["weight_n"]
    out = {}
    for unit in ("cm", "in"):
        f = cargo.DIMS_UNITS[unit]
        cbm = pieces * dims[0] * f * dims[1] * f * dims[2] * f / 1e6
        density = kg / cbm if kg and cbm else None
        out[unit] = (cbm, density, density is None or lim["density_min"] <= density <= lim["density_max"])
    return out


def _reading_words(unit, reading, line, lim) -> str:
    """'As cm: 0.111 cbm, 5,332 kg per cbm, far too dense for one pallet'."""
    cbm, density, fits = reading
    text = "As {}: {} cbm".format(unit, _plain(cbm))
    if density is not None:
        text += ", {} kg per cbm".format(_plain(density, 0 if density >= 10 else 2))
        if not fits:
            pieces = cargo.derive(line)["pieces_n"]
            word = (line.get("pack_word") or "").strip()
            if not word and (line.get("pack_unit") or "").strip():
                # v39: a line given its Packing from the list ('one pallet')
                from . import packing
                word = packing.label(pieces or 1, line["pack_unit"]).split(" ", 1)[-1]
            what = ""
            if word:
                what = " for {} {}".format("one" if (pieces or 1) == 1 else pieces, word)
            text += ", far too {}{}".format("dense" if density > lim["density_max"] else "light", what)
    return text


def _unit_actions(readings, current=None) -> list:
    """Use in / Use cm, the reading that fits first; choosing the unit the
    size already has is an answer ('keep'), not a change."""
    order = sorted(("cm", "in"), key=lambda u: (not readings[u][2], u != "cm"))
    return [("Use {}".format(u), "keep" if u == current else "unit", "size_unit" if u == current else u)
            for u in order]


def for_case(case, lines, stated=(), totals=None, lim=None) -> list:
    """The checks of one case. `case` a mapping with service_mode, qty_raw,
    cargo_decided, gross_kg, volume_cbm; `lines` the live lines in card
    order (dicts with id, or a stand-in id on the register form);
    `stated` the live stated totals; `totals` routes.totals() output."""
    lim = lim or dict(DEFAULT_LIMITS)
    tol = lim["tolerance_pct"]
    mode = (case.get("service_mode") if case else None) or ""
    case_decided = decided_codes(case.get("cargo_decided") if case else None)
    out = []
    derived = [(n + 1, line, cargo.derive(line)) for n, line in enumerate(lines)]
    reading_asked = set()

    # ---- line by line
    firsts = {}
    for row, line, d in derived:
        lid = line.get("id")
        decided = decided_codes(line.get("decided"))
        text = " ".join((line.get("source_text") or "").split())
        # repeat (rule 3)
        if text:
            key = (line.get("source_mail") or "", text.casefold())
            if key in firsts:
                first = firsts[key]
                counted = int(line.get("counted", 1) or 0)
                what = "Line {} repeats line {} word for word ({})".format(row, first, text[:90])
                if not counted and "repeat" not in decided:
                    out.append(Check("repeat", "ask", what + ". Not counted.", lid, row,
                                     [("Count it", "count", "1")]))
                elif not counted:
                    out.append(Check("repeat", "ok", what + "; not counted, as you chose.", lid, row,
                                     [("Count it", "count", "1")]))
                elif "repeat" in decided:
                    out.append(Check("repeat", "ok", what + "; counted, as you chose.", lid, row,
                                     [("Count once", "count", "0")]))
                else:
                    out.append(Check("repeat", "ask", what + " and is counted twice.", lid, row,
                                     [("Count once", "count", "0"), ("It is right", "keep", "repeat")]))
            else:
                firsts[key] = row
        # each with no pieces (rule 4)
        pieces = d["pieces_n"]
        each_weight = line.get("weight_basis") == "each" and line.get("weight_value") is not None
        each_cbm = line.get("cbm_basis") == "each" and line.get("cbm_value") is not None
        if (each_weight or each_cbm) and not pieces:
            what = []
            if each_weight:
                what.append("{} {} each".format(_plain(line["weight_value"]), line.get("weight_unit") or "kg"))
            if each_cbm:
                what.append("{} cbm each".format(_plain(line["cbm_value"])))
            out.append(Check("each", "ask", "Line {}: {} is per piece, but the number of pieces is not given, "
                                            "so it is not counted. How many pieces?".format(row, " and ".join(what)),
                             lid, row, [("Edit", "edit", "")]))
        # reading: thousands or decimals (rule 5)
        reading = _reading_of(line)
        value = _n(line.get("weight_value"))
        if reading and value is not None and value in (reading[0], reading[1]):
            plain, other, unit = reading
            now, then = (value, other if value == plain else plain)
            now_kg, then_kg = _kg_of(line, now), _kg_of(line, then)
            cbm = d["volume_cbm"]
            written = line.get("weight_written")
            per = (lambda kg: " ({} kg per cbm)".format(_plain(kg / cbm, 0 if kg / cbm >= 10 else 2))
                   if cbm and kg else "")
            use_then = ("Use {} {}".format(_plain(then), unit), "reading", _plain(then, 4).replace(",", ""))
            if line.get("weight_reading") == "chosen":
                out.append(Check("reading", "ok", 'Line {}: "{}" read as {} {}, as you chose.'.format(
                    row, written, _plain(now), unit), lid, row, [use_then]))
            elif line.get("weight_reading") == "auto" and now_kg is not None:
                out.append(Check("reading", "ok", 'Line {}: "{}" read as {} {}{}; {} {} would be{}.'.format(
                    row, written, _plain(now), unit, per(now_kg), _plain(then), unit,
                    per(then_kg) or " another reading"), lid, row, [use_then]))
            elif "reading" not in decided and now_kg is not None:
                lo, hi = lim["density_min"], lim["density_max"]
                fits = (lambda kg: cbm and kg and lo <= kg / cbm <= hi)
                if cbm and fits(now_kg):
                    pass                                  # the reading taken is plausible: nothing to ask
                elif cbm and fits(then_kg):
                    out.append(Check("reading", "ask", 'Line {}: {} {} in {} cbm is{}, not plausible. Read "{}" as {} {}{}?'.format(
                        row, _plain(now), unit, _plain(cbm), per(now_kg), written, _plain(then), unit, per(then_kg)),
                        lid, row, [use_then, ("Keep {} {}".format(_plain(now), unit), "keep", "reading")]))
                    reading_asked.add(lid)
                elif not cbm:
                    out.append(Check("reading", "ask", 'Line {}: "{}" can be read as {} {} or as {} {}. Which is it?'.format(
                        row, written, _plain(now), unit, _plain(then), unit),
                        lid, row, [use_then, ("Keep {} {}".format(_plain(now), unit), "keep", "reading")]))
                    reading_asked.add(lid)
        # a size with no unit (15c): both readings with their density, never a guess
        dims = [line.get(k) for k in ("dim_l", "dim_w", "dim_h")]
        readings = size_readings(line, lim)
        if all(v is not None for v in dims) and not line.get("dims_unit") and readings:
            out.append(Check("unit", "ask", "Line {}: the size {} x {} x {} has no unit. {}. {}.".format(
                row, *(_plain(v, 2) for v in dims), _reading_words("cm", readings["cm"], line, lim),
                _reading_words("in", readings["in"], line, lim)), lid, row, _unit_actions(readings),
                short="unit to choose"))
        # a written unit that makes the cargo implausible when the other fits (15c)
        unit_now = line.get("dims_unit")
        if readings and unit_now in ("cm", "in") and _n(line.get("cbm_value")) is None:
            other = "in" if unit_now == "cm" else "cm"
            if readings[unit_now][1] is not None and not readings[unit_now][2] and readings[other][2]:
                words = "Line {}: the size is written {} x {} x {} {}. {}. {}.".format(
                    row, *(_plain(v, 2) for v in dims), unit_now, _reading_words(unit_now, readings[unit_now], line, lim),
                    _reading_words(other, readings[other], line, lim))
                if "size_unit" in decided:
                    out.append(Check("size_unit", "ok", words + " Kept in {}, as you chose.".format(unit_now), lid, row))
                else:
                    out.append(Check("size_unit", "ask", words, lid, row, _unit_actions(readings, unit_now),
                                     short="size unit?"))
                reading_asked.add(lid)
        # a stated volume against the size
        cbm_value = _n(line.get("cbm_value"))
        if cbm_value is None and line.get("cbm") is not None and line.get("source") == "mail":
            cbm_value = _n(line.get("cbm"))
        length, width, height = (_n(line.get(k)) for k in ("length_cm", "width_cm", "height_cm"))
        if cbm_value and d["volume_cbm"] and length and width and height:
            size_cbm = (pieces or 1) * length * width * height / 1e6
            if _pct(size_cbm, d["volume_cbm"]) > tol:
                if "stated_cbm" in decided:
                    out.append(Check("stated_cbm", "ok", "Line {}: {} cbm as stated kept; the size gives {} cbm.".format(
                        row, _plain(d["volume_cbm"]), _plain(size_cbm)), lid, row))
                else:
                    out.append(Check("stated_cbm", "ask", "Line {}: the mail states {} cbm; the size gives {} cbm "
                                                          "({:.0f} % apart).".format(
                                                              row, _plain(d["volume_cbm"]), _plain(size_cbm),
                                                              _pct(size_cbm, d["volume_cbm"])),
                                     lid, row, [("Use the size", "use_size", ""),
                                                ("Keep {} cbm".format(_plain(d["volume_cbm"])), "keep", "stated_cbm")]))
        # density
        kg, cbm = d["weight_n"], d["volume_cbm"]
        if kg and cbm and lid not in reading_asked and int(line.get("counted", 1) or 0):
            density = kg / cbm
            if not lim["density_min"] <= density <= lim["density_max"]:
                label = "Line {}: {} kg in {} cbm is {} kg per cbm, outside {}-{}.".format(
                    row, _kg(kg), _plain(cbm), _plain(density, 0 if density >= 10 else 2),
                    _plain(lim["density_min"], 0), _plain(lim["density_max"], 0))
                if "density" in decided:
                    out.append(Check("density", "ok", label + " Kept as you said.", lid, row))
                else:
                    out.append(Check("density", "ask", label, lid, row, [("It is right", "keep", "density")]))
        # FCL: cargo per container
        qty = line.get("container_qty")
        if qty and kg:
            limit = _container_limit(line.get("container_type"), lim)
            per_box = kg / int(qty)
            if limit and per_box > limit:
                label = "Line {}: {} kg per {} is over {} kg.".format(row, _kg(per_box), line.get("container_type"),
                                                                     _plain(limit, 0))
                if "fcl" in decided:
                    out.append(Check("fcl", "ok", label + " Kept as you said.", lid, row))
                else:
                    out.append(Check("fcl", "ask", label, lid, row, [("It is right", "keep", "fcl")]))

    # ---- the totals the mail states (rule 2, rule 6)
    for s in stated:
        sid = s.get("id")
        mine = [(row, line, d) for row, line, d in derived
                if sid is not None and line.get("stated_id") == sid and int(line.get("counted", 1) or 0)]
        if not mine:
            mine = [(row, line, d) for row, line, d in derived if int(line.get("counted", 1) or 0)] \
                if len(stated) == 1 else []
        n = len(mine)
        parts, diffs = [], []
        stated_bits = []
        if s.get("pieces") is not None:
            stated_bits.append("{} {}".format(s["pieces"], s.get("pack_word") or "pieces"))
            got = sum(d["pieces_n"] or 0 for _, _, d in mine)
            if got != s["pieces"]:
                diffs.append("{} pieces".format(got))
        if s.get("weight_kg") is not None:
            written = s.get("weight_written") or "{} kg".format(_plain(s["weight_kg"]))
            unit_kg = (s.get("weight_unit") or "kg") == "kg"
            stated_bits.append(written if unit_kg else "{} = {} kg".format(written, _kg(s["weight_kg"])))
            got = sum(d["weight_n"] or 0 for _, _, d in mine)
            if abs(got - s["weight_kg"]) > max(0.5, s["weight_kg"] * TOTAL_TOLERANCE):
                diffs.append("{} kg".format(_kg(got)))
        if s.get("cbm") is not None:
            stated_bits.append(s.get("cbm_written") or "{} cbm".format(_plain(s["cbm"])))
            got = sum(d["volume_cbm"] or 0 for _, _, d in mine)
            if _pct(got, s["cbm"]) > tol:
                diffs.append("{} cbm".format(_plain(got)))
        if stated_bits and mine:
            code = "total:{}".format(sid)
            head = "The mail also gives a total: {}.".format(", ".join(stated_bits))
            if not diffs:
                out.append(Check("total", "ok", head + " The {} line{} add up to the same, so the total is only a "
                                                       "check, not another line.".format(n, "s" if n != 1 else "")))
            elif code in case_decided:
                out.append(Check("total", "ok", head + " The lines add up to {}; kept as you said.".format(
                    ", ".join(diffs))))
            else:
                out.append(Check("total", "ask", head + " The {} line{} add up to {}, which is different.".format(
                    n, "s" if n != 1 else "", ", ".join(diffs)), None, None, [("It is right", "keep_case", code)]))
        if s.get("chargeable_kg") is not None and mode in cargo.AIR_MODES and totals:
            ours = totals.get("air_weight_kg")
            if ours:
                code = "chargeable:{}".format(sid)
                head = "The mail states a chargeable weight of {} kg; ours is {} kg".format(
                    _kg(s["chargeable_kg"]), _kg(ours))
                if _pct(s["chargeable_kg"], ours) <= tol:
                    out.append(Check("chargeable", "ok", head + "."))
                elif code in case_decided:
                    out.append(Check("chargeable", "ok", head + "; kept as you said."))
                else:
                    out.append(Check("chargeable", "ask", head + " ({:.0f} % apart).".format(
                        _pct(s["chargeable_kg"], ours)), None, None, [("It is right", "keep_case", code)]))

    # ---- the case
    gross = _n(totals.get("gross_kg")) if totals else _n(case.get("gross_kg") if case else None)
    volume = _n(totals.get("volume_cbm")) if totals else _n(case.get("volume_cbm") if case else None)

    def ask_case(code, label):
        if code in case_decided:
            out.append(Check(code, "ok", label + " Kept as you said."))
        else:
            out.append(Check(code, "ask", label, None, None, [("It is right", "keep_case", code)]))

    def amount():
        bits = []
        if volume is not None:
            bits.append("{} cbm".format(_plain(volume)))
        if gross is not None:
            bits.append("{} kg".format(_kg(gross)))
        return " and ".join(bits)

    if mode == "Air" and ((volume or 0) > lim["air_max_cbm"] or (gross or 0) > lim["air_max_kg"]):
        ask_case("air", "Air with {}: more than {} cbm or {} kg.".format(
            amount(), _plain(lim["air_max_cbm"], 0), _plain(lim["air_max_kg"], 0)))
    if mode == "LCL" and ((volume or 0) > lim["lcl_max_cbm"] or (gross or 0) > lim["lcl_max_kg"]):
        ask_case("lcl", "LCL with {}: more than {} cbm or {} t. Is it FCL?".format(
            amount(), _plain(lim["lcl_max_cbm"], 0), _plain(lim["lcl_max_kg"] / 1000, 0)))
    if gross and volume and not any(d["weight_n"] and d["volume_cbm"] for _, _, d in derived):
        density = gross / volume
        if not lim["density_min"] <= density <= lim["density_max"]:
            ask_case("density", "{} kg in {} cbm is {} kg per cbm, outside {}-{}.".format(
                _kg(gross), _plain(volume), _plain(density, 0 if density >= 10 else 2),
                _plain(lim["density_min"], 0), _plain(lim["density_max"], 0)))
    qty_raw = (case.get("qty_raw") if case else None) or ""
    prefix = _MODE_PREFIX.match(qty_raw)
    if prefix and mode in ("Air", "LCL", "FCL") and prefix.group(1).upper() != mode.upper():
        ask_case("qty_mode", 'The quantity text "{}" is written for {}, but the case is {}.'.format(
            qty_raw[:60], prefix.group(1).upper() if prefix.group(1).upper() != "AIR" else "Air", mode))
    elif mode in ("LCL", "FCL") and _AIR_WORDS.search(qty_raw):
        ask_case("qty_mode", 'The quantity text "{}" has air words (volumetric, chargeable) on a {} case.'.format(
            qty_raw[:60], mode))
    return out


def asks(checks) -> int:
    return sum(1 for c in checks if c.kind == "ask")


def mail_text(lines, stated, qty_raw=None) -> list:
    """What 'Show the mail text' shows: [(marker, text)] - the stated totals
    first (marker 'total'), then every line read from a mail, numbered like
    the card's rows. A case without any such line shows its qty_raw
    (marker ''). Manual lines have nothing to show."""
    out = []
    for s in stated:
        if s.get("source_text"):
            out.append(("total", s["source_text"]))
    for n, line in enumerate(lines, start=1):
        if line.get("source_text"):
            out.append((str(n), line["source_text"]))
    if not any(marker != "total" for marker, _ in out) and qty_raw:
        out.append(("", qty_raw))
    return out


def stated_of(conn, enquiry_id: int) -> list:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'enquiry_cargo_stated'").fetchone() is None:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM enquiry_cargo_stated WHERE enquiry_id = ? AND voided = 0 ORDER BY id", (enquiry_id,))]



# --------------------------------------------------------------- 15b checks --
US = ("USA", "United States", "United States of America", "US")
DOOR_DELIVERY = ("Door to door", "Port to door")
DOOR_PICKUP = ("Door to door", "Door to port")


def _box_size(ctype) -> str:
    found = re.match(r"(\d{2})", ctype or "")
    return "{}'".format(found.group(1)) if found else "container"


def more_checks(lines, mode, live_routes, text="", us_max_kg=17200.0, lim=None) -> list:
    """The checks 15b adds: a missing number, the weight a month the mail
    states, US road weight, and a size the mail gives against a stated
    volume. `lines` the live lines in card order; `text` what 'Show the
    mail text' shows (for a case registered before 13b, the mail itself)."""
    from . import cargo_card
    lim = lim or dict(DEFAULT_LIMITS)
    tol = lim["tolerance_pct"]
    out = []
    counted = [(n, l) for n, l in enumerate(lines, start=1) if int(l.get("counted", 1) or 0)]
    # a number the line needs and lacks
    for n, what in cargo_card.missing(lines, mode):
        line = lines[n - 1]
        out.append(Check("missing", "ask", "Line {}: the {} is missing.".format(n, what), line.get("id"), n,
                         [("Edit", "edit", "")], short="{} missing".format(what.split(" ")[0])))
    # the weight a month the mail states (rule 10)
    stated = parsing.weight_per_month(text)
    if stated:
        kg_month, written, per_dest = stated
        for n, line in counted:
            pm = _n(line.get("per_month"))
            kg = cargo.derive(line)["weight_n"]
            if not pm or kg is None:
                continue
            ours = pm * kg
            how = "{} x {}".format(_plain(pm, 2), "{:,.1f} t".format(kg / 1000.0))
            where = " per destination" if per_dest else ""
            if _pct(ours, kg_month) <= tol:
                out.append(Check("per_month", "ok", "Matches the mail: {} a month{} ({}).".format(
                    "{:,.0f} t".format(kg_month / 1000.0) if kg_month >= 1000 else "{:,.0f} kg".format(kg_month),
                    where, how), line.get("id"), n))
            else:
                out.append(Check("per_month", "ask", "The mail states {} ({:,.1f} t a month{}); line {} gives {:,.1f} t a "
                                                     "month ({}).".format(written, kg_month / 1000.0, where, n,
                                                                          ours / 1000.0, how),
                                 line.get("id"), n, [("Edit", "edit", "")], short="month weight differs"))
            break
    # US road weight (rule 11): door delivery or pickup in the USA
    for route in live_routes or []:
        if (route.get("service_mode") or mode) != "FCL":
            continue
        scope = route.get("service_scope") or ""
        legs = []
        if scope in DOOR_DELIVERY and (route.get("dest_country") or "") in US:
            legs.append("delivered")
        if scope in DOOR_PICKUP and (route.get("origin_country") or "") in US:
            legs.append("picked up")
        if not legs:
            continue
        for n, line in counted:
            ids = cargo_card.routes.parse_ids(line.get("route_ids"))
            if ids and route.get("id") not in ids:
                continue
            qty, kg = line.get("container_qty"), cargo.derive(line)["weight_n"]
            if not qty or kg is None:
                continue
            per_box = kg / int(qty)
            if per_box > us_max_kg:
                out.append(Check("us_road", "ask", "Route {} is {} on US roads: {:,.1f} t in a {} is over a standard chassis "
                                                   "({:,.1f} t). Ask for a tri-axle chassis or an overweight permit.".format(
                                                       route.get("letter") or "A", " and ".join(legs), per_box / 1000.0,
                                                       _box_size(line.get("container_type")), us_max_kg / 1000.0),
                                 line.get("id"), n, short="over a US chassis"))
                break
    # a size in the mail against a stated volume (rule 12)
    if text:
        sized = [l for l in cargo_card.read_lines(text) if l.get("length_cm") and l.get("width_cm") and l.get("height_cm")]
        if sized:
            mail = sized[0]
            mail_cbm = (mail.get("pieces") or 1) * mail["length_cm"] * mail["width_cm"] * mail["height_cm"] / 1e6
            for n, line in counted:
                stated_cbm = _n(line.get("cbm_value"))
                if stated_cbm is None or line.get("dim_l") is not None:
                    continue
                if _pct(mail_cbm, stated_cbm) <= tol:
                    continue
                d = cargo.derive(line)
                size = "{} {} {} x {} x {}{}".format(mail.get("pieces") or 1, (mail.get("pack_word") or "").upper() or "piece",
                                                     *(_plain(mail.get(k), 2) for k in ("dim_l", "dim_w", "dim_h")),
                                                     " " + mail["dims_unit"] if mail.get("dims_unit") else "")
                text_ = "The mail also gives a size: {} = {:.3f} cbm, not the stated {:.3f}.".format(size, mail_cbm, stated_cbm)
                if mode in cargo.AIR_MODES and d["weight_n"] is not None:
                    text_ += " On the size the chargeable weight is {:,.1f} kg; on {} cbm it is {:,.1f} kg.".format(
                        max(d["weight_n"], mail_cbm * cargo.CBM_TO_KG), _plain(stated_cbm),
                        max(d["weight_n"], stated_cbm * cargo.CBM_TO_KG))
                from . import packing
                # v39: the seventh part is the Packing the mail's word means ('' when none)
                value = ",".join(str(x) for x in (mail.get("pieces") or 1, mail.get("pack_word") or "",
                                                  _plain(mail.get("dim_l"), 2), _plain(mail.get("dim_w"), 2),
                                                  _plain(mail.get("dim_h"), 2), mail.get("dims_unit") or "",
                                                  packing.unit_for(mail.get("pack_word")) or ""))
                out.append(Check("mail_size", "ask", text_, line.get("id"), n, [("Use the size", "use_mail_size", value)],
                                 short="size and volume differ"))
                break
    return out


def rail_words(checks) -> str:
    """The first amber check in a few words, for the rail's Cargo entry."""
    for c in checks:
        if c.kind == "ask":
            if c.short:
                return c.short
            words = re.sub(r"^Line \d+: ", "", c.text).split()
            return " ".join(words[:4]).rstrip(".,:;")
    return ""
