"""Cargo lines (schema v28, 2026-09-24): one consignment per
row of enquiry_cargo, each charged on its own, and the numbers derived
from a line in one place - like shipments.summary for a shipment.

    volume_cbm     stated cbm when the line carries one, else
                   pieces x L x W x H / 1e6 when the dims are known
    volumetric_kg  cbm x 1e6 / 6000 (IATA)
    chargeable_kg  max(weight_kg, volumetric_kg)              - Air
    revenue_ton    max(cbm, weight_kg / 1000)                 - ocean LCL (W/M)

The case's own three quantity fields keep their meaning and the export
contract does not change: with lines on file the repository writes the
totals into the enquiry row on every line change (air_weight_kg = the
lines' chargeable_kg summed for Air, weight_kg otherwise; lcl_cbm = the
cbm summed; qty_raw = the lines joined with ' / '). A case without lines
works exactly as before: its fields are the single implicit line.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Mapping

#: SA1: the modes charged on air weight (Sea-Air flies its last leg).
AIR_MODES = ("Air", "Sea-Air")
#: IATA: one cubic metre of air cargo is 1e6 / 6000 = 166.67 kg.
VOLUMETRIC_DIVISOR = 6000
CBM_TO_KG = 1e6 / VOLUMETRIC_DIVISOR
#: A stated cbm and the dims' cbm further apart than this get a note.
CBM_TOLERANCE = 0.10

FIELDS = ("label", "pieces", "pack_word", "dims_text", "length_cm", "width_cm", "height_cm",
          "weight_kg", "cbm", "deliver_to", "commodity", "notes",
          # v33 (2026-09-28): the numbers as written, how the weight was given,
          # FCL containers per shipment, how often and how long, counted, routes
          "dim_l", "dim_w", "dim_h", "dims_unit", "weight_value", "weight_unit", "weight_basis",
          "container_type", "container_qty", "per_month", "months", "counted", "route_ids",
          # v34 (2026-09-29): what was read - the mail and its lines, the
          # weight and volume as written, how an ambiguous weight was read,
          # the total it belongs to, the checks answered
          "source_mail", "source_text", "weight_written", "weight_reading", "cbm_value", "cbm_basis",
          "cbm_written", "stated_id", "decided",
          # v39 (batch 23): the Packing chosen from setting packing_units;
          # pack_word stays the word as written
          "pack_unit",
          # v50 (D7a): the HS codes as written, the value of the goods
          "hs_code", "cargo_value", "cargo_value_cur")
#: to cm and to kg
DIMS_UNITS = {"cm": 1.0, "in": 2.54, "mm": 0.1, "m": 100.0}
#: how a line's weight is given: in total, each piece, or (15b, FCL) per container
WEIGHT_BASES = ("total", "each", "container")
WEIGHT_UNITS = {"kg": 1.0, "lbs": 0.45359237, "t": 1000.0}
_FLOATS = ("length_cm", "width_cm", "height_cm", "weight_kg", "cbm", "dim_l", "dim_w", "dim_h",
           "weight_value", "per_month", "months", "cbm_value", "cargo_value")
_INTS = ("pieces", "container_qty", "counted", "stated_id")
#: the fields of a total the mail states (enquiry_cargo_stated)
STATED_FIELDS = ("source_mail", "source_text", "pieces", "pack_word", "weight_value", "weight_unit",
                 "weight_written", "weight_kg", "cbm", "cbm_written", "chargeable_kg", "chargeable_written")


#: a number with words after it (13c, 2026-09-30): '2 a month', '24 months',
#: '25 big bags', '7605 lbs', '25,000 kg'
_LEADING = re.compile(r"^\s*(?P<n>\d[\d.,]*)\s*(?:x\b|x(?=\s|$))?\s*(?P<rest>.*?)\s*$", re.IGNORECASE)
NEEDS_NUMBER = "needs a number"
#: one cubic foot in cbm
CFT_TO_CBM = 0.0283168
_WEIGHT_WORDS = (("lbs", ("lb", "lbs", "pound", "pounds")),
                 ("t", ("t", "ton", "tons", "tonne", "tonnes", "mt")),
                 ("kg", ("kg", "kgs", "kilo", "kilos", "kilogram", "kilograms")))
_VOLUME_WORDS = (("cft", ("cft", "cuft", "cu ft", "cu. ft.", "ft3", "cubic feet", "cubic foot")),
                 ("cbm", ("cbm", "m3", "cubic meters", "cubic metres", "cubic meter", "cubic metre")))


def leading_number(text):
    """(number, the words after it) for '2 a month', '24 months', '25 big
    bags', '7605 lbs'; (None, text) when the text does not begin with a
    number. A comma before three digits separates thousands, before one or
    two it is a decimal comma (parsing._number)."""
    from . import parsing

    text = (str(text) if text is not None else "").strip()
    found = _LEADING.match(text)
    if not found:
        return None, text
    try:
        return float(parsing._number(found.group("n").rstrip(".,"))), found.group("rest")
    except ValueError:
        return None, text


def _unit_word(rest, table):
    words = " ".join((rest or "").lower().replace(".", " ").split())
    for unit, spellings in table:
        if any(words == w or words.startswith(w + " ") for w in spellings):
            return unit
    return None


def read_months(text):
    """'24 months' -> 24, '2 years' -> 24; None when there is no number."""
    number, rest = leading_number(text)
    if number is None:
        return None
    if (rest or "").lower().startswith(("year", "yr", "yrs")):
        number *= 12
    return number


def read_weight(text):
    """(value, unit) of '25 t', '25,000 kg', '7605 lbs'; unit None when no
    unit is written; value None when there is no number."""
    number, rest = leading_number(text)
    return number, (_unit_word(rest, _WEIGHT_WORDS) if number is not None else None)


def read_volume_cbm(text, unit=None):
    """A volume in cbm from '6.05 cbm', '200 cft' or a bare number in
    `unit` (cbm or cft); None when there is no number."""
    number, rest = leading_number(text)
    if number is None:
        return None
    unit = _unit_word(rest, _VOLUME_WORDS) or unit or "cbm"
    return round(number * CFT_TO_CBM, 4) if unit == "cft" else number


def _fmt(value, places=2) -> str:
    if value is None:
        return ""
    text = "{:.{}f}".format(value, places).rstrip("0").rstrip(".")
    return text or "0"


def _num(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def derive(line: Mapping, mode: str | None = None) -> dict:
    """The derived numbers of one line, with a label for each that says
    how it was arrived at ('8 x 120 x 100 x 100 cm = 9.6 cbm')."""
    pieces = line.get("pieces")
    try:
        pieces = int(pieces) if pieces not in (None, "") else None
    except (TypeError, ValueError):
        pieces = None
    length, width, height = (_num(line.get(k)) for k in ("length_cm", "width_cm", "height_cm"))
    weight = _num(line.get("weight_kg"))
    stated = _num(line.get("cbm"))
    dims_known = all(v is not None and v > 0 for v in (length, width, height))
    from_dims = None
    if dims_known:
        from_dims = (pieces or 1) * length * width * height / 1e6
        dims_formula = "{}{} x {} x {} cm = {} cbm".format(
            "{} x ".format(pieces) if pieces and pieces != 1 else "",
            _fmt(length), _fmt(width), _fmt(height), _fmt(from_dims))
    else:
        dims_formula = ""
    if stated is not None:
        cbm, cbm_from, cbm_formula = stated, "stated", "as stated"
    elif from_dims is not None:
        cbm, cbm_from, cbm_formula = from_dims, "dims", dims_formula
    else:
        cbm, cbm_from, cbm_formula = None, "", ""
    cbm_note = ""
    if stated is not None and from_dims is not None and stated > 0 \
            and abs(from_dims - stated) / stated > CBM_TOLERANCE:
        cbm_note = "stated {} cbm, dims give {} cbm".format(_fmt(stated), _fmt(from_dims))

    volumetric = cbm * CBM_TO_KG if cbm is not None else None
    if weight is not None and volumetric is not None:
        chargeable = max(weight, volumetric)
        chargeable_formula = "max({} kg, {} cbm x {} = {} kg) = {} kg".format(
            _fmt(weight, 1), _fmt(cbm), _fmt(CBM_TO_KG), _fmt(volumetric, 1), _fmt(chargeable, 1))
    elif weight is not None or volumetric is not None:
        chargeable = weight if weight is not None else volumetric
        chargeable_formula = ("actual {} kg".format(_fmt(weight, 1)) if weight is not None
                              else "volumetric {} cbm x {} = {} kg".format(
                                  _fmt(cbm), _fmt(CBM_TO_KG), _fmt(volumetric, 1)))
    else:
        chargeable, chargeable_formula = None, ""
    tons = weight / 1000.0 if weight is not None else None
    if cbm is not None and tons is not None:
        revenue = max(cbm, tons)
        revenue_formula = "max({} cbm, {} kg / 1000 = {} t) = {}".format(
            _fmt(cbm), _fmt(weight, 1), _fmt(tons, 3), _fmt(revenue))
    elif cbm is not None or tons is not None:
        revenue = cbm if cbm is not None else tons
        revenue_formula = "{} cbm".format(_fmt(cbm)) if cbm is not None else "{} t".format(_fmt(tons, 3))
    else:
        revenue, revenue_formula = None, ""
    return {
        "pieces_n": pieces, "weight_n": weight,
        "volume_cbm": cbm, "cbm_from": cbm_from, "cbm_formula": cbm_formula, "cbm_note": cbm_note,
        "volumetric_kg": volumetric, "chargeable_kg": chargeable,
        "chargeable_formula": chargeable_formula,
        "revenue_ton": revenue, "revenue_formula": revenue_formula,
        "is_air": (mode or "") in AIR_MODES,
    }


def line_text(line: Mapping, derived: Mapping | None = None) -> str:
    """'8 pallets 120 x 100 x 100 cm, 4500 kg, 9.6 cbm' - one line's share
    of qty_raw."""
    derived = derived or derive(line)
    parts = []
    head = ""
    if derived["pieces_n"] is not None:
        if line.get("pack_unit"):
            from . import packing
            head = packing.label(derived["pieces_n"], line["pack_unit"])
        else:
            head = "{} {}".format(derived["pieces_n"], (line.get("pack_word") or "pcs").strip())
    dims = (line.get("dims_text") or "").strip()
    if not dims and derived["cbm_from"] == "dims":
        dims = "{} x {} x {} cm".format(_fmt(_num(line.get("length_cm"))), _fmt(_num(line.get("width_cm"))),
                                        _fmt(_num(line.get("height_cm"))))
    if head or dims:
        parts.append((head + " " + dims).strip())
    if derived["weight_n"] is not None:
        parts.append("{} kg".format(_fmt(derived["weight_n"], 1)))
    if derived["volume_cbm"] is not None:
        parts.append("{} cbm".format(_fmt(derived["volume_cbm"])))
    return ", ".join(parts)


def summarise(lines, mode: str | None = None) -> dict:
    """{'lines': [line + derived + 'text'], 'totals': {...}, 'mode'} for a
    list of line mappings (rows on file, or the draft's)."""
    out, totals = [], {"count": 0, "pieces": None, "weight_kg": None, "cbm": None,
                       "volumetric_kg": None, "chargeable_kg": None, "revenue_ton": None}

    def add(key, value):
        if value is None:
            return
        totals[key] = (totals[key] or 0) + value

    for line in lines:
        d = derive(line, mode)
        row = dict(line)
        row.update(d)
        row["text"] = line_text(line, d)
        row["show"] = {
            "pieces": "" if d["pieces_n"] is None else str(d["pieces_n"]),
            "weight": _fmt(d["weight_n"], 1), "cbm": _fmt(d["volume_cbm"]),
            "volumetric": _fmt(d["volumetric_kg"], 1), "chargeable": _fmt(d["chargeable_kg"], 1),
            "revenue": _fmt(d["revenue_ton"]),
        }
        out.append(row)
        if not int(line.get("counted", 1) if line.get("counted") is not None else 1):
            continue                     # listed, struck through, not in the totals (v33 counted)
        totals["count"] += 1
        add("pieces", d["pieces_n"])
        add("weight_kg", d["weight_n"])
        add("cbm", d["volume_cbm"])
        add("volumetric_kg", d["volumetric_kg"])
        add("chargeable_kg", d["chargeable_kg"])
        add("revenue_ton", d["revenue_ton"])
    totals["show"] = {
        "pieces": "" if totals["pieces"] is None else str(totals["pieces"]),
        "weight": _fmt(totals["weight_kg"], 1), "cbm": _fmt(totals["cbm"]),
        "volumetric": _fmt(totals["volumetric_kg"], 1), "chargeable": _fmt(totals["chargeable_kg"], 1),
        "revenue": _fmt(totals["revenue_ton"]),
    }
    return {"lines": out, "totals": totals, "mode": mode or "", "is_air": (mode or "") in AIR_MODES}


def commodity_of(lines) -> str | None:
    """The case's commodity (15b): the distinct commodities of the counted
    lines, in line order, joined with '; '; None when no line names one."""
    seen = []
    for line in lines:
        if line.get("voided") or not int(line.get("counted", 1) if line.get("counted") is not None else 1):
            continue
        text = str(line.get("commodity") or "").strip()
        if text and text not in seen:
            seen.append(text)
    return "; ".join(seen) or None


def enquiry_fields(summary: Mapping) -> dict:
    """What the case's own three fields become while lines are on file."""
    totals = summary["totals"]
    if not totals["count"]:
        return {}
    # the case's chargeable weight is max(total gross, total volumetric) -
    # not the lines' own chargeable weights added up (13b, 2026-09-30)
    if summary["is_air"]:
        options = [v for v in (totals["weight_kg"], totals["volumetric_kg"]) if v is not None]
        weight = max(options) if options else None
    else:
        weight = totals["weight_kg"]
    # v33: qty_raw is the text as received - never computed from the lines
    return {
        "air_weight_kg": round(weight, 1) if weight is not None else None,
        "lcl_cbm": round(totals["cbm"], 2) if totals["cbm"] is not None else None,
    }


def lines_of(conn: sqlite3.Connection, enquiry_id: int, voided: bool = False) -> list:
    """The case's lines in seq order, live ones unless `voided`."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'enquiry_cargo'").fetchone() is None:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM enquiry_cargo WHERE enquiry_id = ? AND voided = ? ORDER BY seq, id",
        (enquiry_id, 1 if voided else 0))]


def summary(conn: sqlite3.Connection, enquiry_id: int, mode: str | None = None) -> dict:
    """The derived state of a case's live lines (mode read off the case
    when not given), plus its struck-out lines under 'voided'."""
    if mode is None:
        row = conn.execute("SELECT service_mode FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        mode = row["service_mode"] if row else None
    out = summarise(lines_of(conn, enquiry_id), mode)
    out["voided"] = lines_of(conn, enquiry_id, voided=True)
    return out


# ------------------------------------------------------- from the mail ----
# ------------------------------------------- v50 (D7a): HS code and value --
#: one HS code as written: 4 to 10 digits, dots or spaces between them
_HS_ONE = re.compile(r"^\d[\d. ]*\d$")
HS_REFUSED = "4 to 10 digits each, dots or spaces as written (2507.00.10); several with commas"
VALUE_REFUSED = "an amount and a currency (USD 12,500); USD when no currency is typed"
#: a currency written as a sign or a word, and the three letters it is
_CURRENCY_SIGNS = {"$": "USD", "us$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP",
                   "rmb": "CNY", "cny": "CNY", "r$": "BRL", "brl": "BRL"}
_MULTIPLIERS = {"k": 1e3, "mil": 1e3, "mm": 1e6, "million": 1e6, "millions": 1e6}
_VALUE_TEXT = re.compile(
    r"^\s*(?:(?P<cur>r\$|us\$|[a-z]{3}|\$|€|£)\s*)?(?P<n>\d[\d,.\s]*?)\s*(?P<mult>millions?|mil|mm|k)?"
    r"\s*(?P<cur2>[a-z]{3}|\$|€|£)?\s*$", re.IGNORECASE)


def hs_code_of(text) -> tuple:
    """(codes, error): the HS codes typed, ', ' between them, each 4 to 10
    digits with dots or spaces as written; ('', None) when nothing is typed."""
    parts = [" ".join(p.split()) for p in re.split(r"[,;/]", text or "")]
    parts = [p for p in parts if p]
    if not parts:
        return "", None
    for part in parts:
        if not _HS_ONE.match(part) or not 4 <= len(re.sub(r"\D", "", part)) <= 10:
            return "", HS_REFUSED
    return ", ".join(parts), None


def amount_of(text) -> float | None:
    """'12,500' / '12.500,00' / '12480.5' as a number: with both marks the
    last is the decimal; one mark between groups of three is thousands."""
    text = re.sub(r"\s", "", text or "")
    if not text or not re.fullmatch(r"\d[\d,.]*", text):
        return None
    if "," in text and "." in text:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        text = text.replace("." if decimal == "," else ",", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", "") if re.fullmatch(r"\d{1,3}(?:,\d{3})+", text) else text.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", text):
        text = text.replace(".", "")
    try:
        return float(text)
    except ValueError:
        return None


def currency_of(word) -> str | None:
    """'$' -> USD, 'rmb' -> CNY, 'eur' -> EUR; three letters as they are."""
    word = (word or "").strip().lower()
    if word in _CURRENCY_SIGNS:
        return _CURRENCY_SIGNS[word]
    return word.upper() if re.fullmatch(r"[a-z]{3}", word) else None


def value_of(text, default_currency="USD") -> tuple:
    """(amount, currency, error) out of what was typed: 'USD 12,500',
    '12480 eur', '$ 1.3 million', '12480' (USD); (None, None, None) when
    nothing is typed."""
    text = (text or "").strip()
    if not text:
        return None, None, None
    m = _VALUE_TEXT.match(text)
    if not m or (m.group("cur") and m.group("cur2")):
        return None, None, VALUE_REFUSED
    amount = amount_of(m.group("n"))
    word = m.group("cur") or m.group("cur2")
    currency = currency_of(word) if word else default_currency
    if amount is None or currency is None:
        return None, None, VALUE_REFUSED
    if m.group("mult"):
        amount *= _MULTIPLIERS[m.group("mult").lower()]
    return round(amount, 2), currency, None


def value_text(amount, currency) -> str:
    """'USD 12,500' (cents only when there are any); '' without an amount."""
    if amount is None:
        return ""
    number = "{:,.2f}".format(amount)
    if number.endswith(".00"):
        number = number[:-3]
    return "{} {}".format(currency or "USD", number)


def _norm_text(text) -> str:
    return " ".join((text or "").split()).casefold()


def lines_from_block(block, mail_id: str | None = None, source: str = "mail") -> tuple:
    """(lines, stated) out of a parsing.CargoBlock (v34,
    2026-09-29): one cargo line per item the mail lists, each with the
    lines of text it came from and its numbers as written; the block's
    total (and its stated volume or chargeable weight) as one 'stated'
    dict, a check on the lines and never counted with them (rule 2). A
    quantity the lines do not give at all (a weight stated only as the
    block's total) is carried by one more line, labelled 'Total', so it
    is counted once. A line repeated word for word is not counted (rule 3).
    stated is None when the mail states nothing for the block as a whole."""
    from . import packing, parsing

    pk = block.packing
    items = list(pk.items) if pk else []
    # v50 (D7a): the HS code goes to hs_code (intake._hs_and_value), no
    # longer into the commodity as '(HS 4910)'
    commodity = block.commodity or ""
    lines, seen = [], set()
    for n, item in enumerate(items):
        text = "\n".join(item.lines)
        word = item.word or ("pcs" if item.pieces_stated else "")
        if item.pieces_stated and item.pieces != 1 and word and not word.endswith("s") \
                and word not in ("pcs", "colli", "coli"):
            word += "s"
        row = {
            "label": block.label or "",
            "pieces": item.pieces if item.pieces_stated else None, "pack_word": word or None,
            # v39: the Packing the word means (the reader of a mail maps it again on her own list)
            "pack_unit": packing.unit_for(word) if word else None,
            "dims_text": item.dims_text or None,
            "dim_l": item.dims_nums[0] if item.dims_nums else None,
            "dim_w": item.dims_nums[1] if item.dims_nums else None,
            "dim_h": item.dims_nums[2] if item.dims_nums else None,
            "dims_unit": item.dims_unit,
            "weight_value": item.weight_value, "weight_unit": item.weight_unit or ("kg" if item.weight_value is not None else None),
            "weight_basis": "each" if item.weight_each else "total",
            "weight_written": item.weight_written or None, "weight_reading": item.weight_reading or None,
            "cbm_value": item.cbm_value, "cbm_basis": "each" if item.cbm_each else "total",
            "cbm_written": item.cbm_written or None, "cbm": None, "weight_kg": None,
            "length_cm": None, "width_cm": None, "height_cm": None,
            "deliver_to": block.deliver_to or "", "commodity": commodity if n == 0 else "",
            "notes": "; ".join(block.notes) if n == 0 else "",
            "source": source, "source_mail": mail_id, "source_text": text,
            "counted": 1, "evidence": item.lines[0] if item.lines else "",
        }
        key = _norm_text(text)
        if key and key in seen:
            row["counted"] = 0                       # word for word again: the card asks (rule 3)
        seen.add(key)
        lines.append(normalise(row))
    stated = None
    if pk is not None and (pk.total_pieces is not None or pk.total_weight_value is not None
                           or pk.total_cbm is not None or pk.stated_chargeable_kg is not None):
        text = list(pk.total_lines)
        if pk.stated_chargeable_text and pk.stated_chargeable_text not in text:
            text.append(pk.stated_chargeable_text)
        stated = {
            "source_mail": mail_id, "source_text": "\n".join(text),
            "pieces": pk.total_pieces, "pack_word": _plural(pk.total_word, pk.total_pieces) or None,
            "weight_value": pk.total_weight_value,
            "weight_unit": pk.total_weight_unit or None, "weight_written": pk.total_weight_written or None,
            "weight_kg": round(pk.total_kg, 3) if pk.total_kg is not None else None,
            "cbm": pk.total_cbm, "cbm_written": pk.total_cbm_written or None,
            "chargeable_kg": pk.stated_chargeable_kg, "chargeable_written": pk.stated_chargeable_text or None,
        }
        # a quantity no line gives: the total carries it, once (rule 2)
        no_weight = stated["weight_kg"] is not None and all(l.get("weight_value") is None for l in lines)
        no_volume = stated["cbm"] is not None and all(l.get("cbm") is None and l.get("length_cm") is None for l in lines)
        if lines and (no_weight or no_volume):
            carrier = {"label": "Total", "source": source, "source_mail": mail_id,
                       "source_text": "\n".join(pk.total_lines), "counted": 1, "deliver_to": block.deliver_to or "",
                       "evidence": pk.total_lines[0] if pk.total_lines else ""}
            if no_weight:
                carrier.update({"weight_value": stated["weight_value"], "weight_unit": stated["weight_unit"] or "kg",
                                "weight_basis": "total", "weight_written": stated["weight_written"]})
                stated["weight_value"] = stated["weight_kg"] = None
                stated["weight_written"] = stated["weight_unit"] = None
            if no_volume:
                carrier.update({"cbm_value": stated["cbm"], "cbm_basis": "total", "cbm_written": stated["cbm_written"]})
                stated["cbm"], stated["cbm_written"] = None, None
            lines.append(normalise(carrier))
        if all(stated[k] is None for k in ("pieces", "weight_kg", "cbm", "chargeable_kg")):
            stated = None
    return lines, stated


def _plural(word, pieces) -> str:
    if word and pieces and pieces != 1 and not word.endswith("s") and word not in ("pcs", "colli", "coli"):
        return word + "s"
    return word or ""


def from_block(block, source: str = "mail") -> dict:
    """The block's lines folded into one (older callers and the draft's
    'evidence'); lines_from_block is the reader since v34."""
    lines, _stated = lines_from_block(block, source=source)
    return lines[0] if lines else {}


def _grouped_dims(items) -> list:
    out = []
    for item in items:
        if out and out[-1][0] == item.dims_text:
            out[-1][1] += item.pieces
        else:
            out.append([item.dims_text, item.pieces])
    return ["{}{}".format("{} x ".format(n) if n != 1 else "", text) for text, n in out]


def clean(values: Mapping, units=None) -> tuple:
    """Typed line values out of a form: (values, errors). Dims typed as
    text are read into cm when they carry a unit. `units` is her Packing
    list (setting packing_units; the default list when not given)."""
    from . import packing, parsing

    out, errors = {}, {}
    out["label"] = (values.get("label") or "").strip()[:60] or None
    pieces = (str(values.get("pieces") or "")).strip()
    out["pack_word"] = (values.get("pack_word") or "").strip()[:30] or None
    if "pack_unit" in values:
        # v39: the Packing dropdown (its value as chosen, or nothing)
        out["pack_unit"] = (values.get("pack_unit") or "").strip()[:30] or None
    if pieces:
        # '25 big bags': 25, and the words are the packing when none is given
        # (13c); v39: they choose the Packing too when the dropdown is empty
        number, rest = leading_number(pieces)
        if number is None:
            errors["pieces"] = NEEDS_NUMBER
        else:
            out["pieces"] = int(number)
            if rest and not rest.lower().startswith(("x ", "x")):
                if not out["pack_word"]:
                    out["pack_word"] = rest[:30]
                if "pack_unit" in values and not out["pack_unit"]:
                    out["pack_unit"] = packing.unit_for(rest, units)
    else:
        out["pieces"] = None
    dims_text = (values.get("dims_text") or "").strip()
    dims, as_written = parsing.dims_from_text(dims_text) if dims_text else (None, "")
    out["dims_text"] = as_written[:80] or None
    if dims:
        out["length_cm"], out["width_cm"], out["height_cm"] = (round(v, 2) for v in dims)
    else:
        out["length_cm"] = out["width_cm"] = out["height_cm"] = None
        for key in ("length_cm", "width_cm", "height_cm"):
            typed = (str(values.get(key) or "")).strip()
            if typed:
                number = _num(typed.replace(",", ""))
                if number is None:
                    errors[key] = "must be a number"
                out[key] = number
    for key in ("weight_kg", "cbm"):
        typed = (str(values.get(key) or "")).strip()
        if typed:
            number = _num(typed.replace(",", ""))
            if number is None:
                errors[key] = "must be a number"
            out[key] = number
        else:
            out[key] = None
    for key in ("deliver_to", "commodity", "notes"):
        out[key] = (values.get(key) or "").strip()[:400] or None
    # v50 (D7a): HS codes and the value of the goods, refused when malformed
    if "hs_code" in values:
        codes, why = hs_code_of(values.get("hs_code"))
        if why:
            errors["hs_code"] = why
        out["hs_code"] = codes or None
    if "cargo_value" in values:
        typed_value = values.get("cargo_value")
        if isinstance(typed_value, (int, float)):
            amount, currency, why = float(typed_value), currency_of(values.get("cargo_value_cur")) or "USD", None
        else:
            amount, currency, why = value_of(typed_value, currency_of(values.get("cargo_value_cur")) or "USD")
        if why:
            errors["cargo_value"] = why
        out["cargo_value"], out["cargo_value_cur"] = amount, currency
    # v33: the numbers as written, with units; each or in total
    for key, allowed in (("dims_unit", DIMS_UNITS), ("weight_unit", WEIGHT_UNITS)):
        if key in values:
            unit = (values.get(key) or "").strip()
            out[key] = unit if unit in allowed else None
    # 13c: numbers the way people write them - '2 a month', '24 months',
    # '2 years', '25 t' (the unit follows), '52 x 42 x 47 in' in the first
    # size box (all three and the unit); a box with no number is refused
    first = (str(values.get("dim_l") or "")).strip()
    triple = parsing._DIMS.search(first) if first else None
    if triple:
        out["dim_l"], out["dim_w"], out["dim_h"] = (float(parsing._number(triple.group(k))) for k in ("l", "w", "h"))
        unit = parsing._unit_of_dims(triple)
        if unit:
            out["dims_unit"] = unit
    for key in ("dim_l", "dim_w", "dim_h", "weight_value", "per_month", "months"):
        if key not in values or (triple and key in ("dim_l", "dim_w", "dim_h")):
            continue
        typed_value = (str(values.get(key) or "")).strip()
        if not typed_value:
            out[key] = None
            continue
        if key == "months":
            number = read_months(typed_value)
        elif key == "weight_value":
            number, unit = read_weight(typed_value)
            if unit:
                out["weight_unit"] = unit
            if number is not None and re.search(r"\beach\b|/\s*(?:pc|piece)\b", typed_value, re.IGNORECASE):
                out["weight_basis"] = "each"
        else:
            number, _rest = leading_number(typed_value)
        if number is None:
            errors[key] = NEEDS_NUMBER
        out[key] = number
    if "weight_basis" in values:
        basis = values.get("weight_basis")
        out["weight_basis"] = basis if basis in WEIGHT_BASES else "total"
    # v34: the volume as written, per piece or in total; what was read.
    # 15b: in cbm or cft (the picker beside it, or the word typed); a volume
    # typed in cft is kept as written beside the cbm it becomes
    if "cbm_value" in values:
        typed_value = (str(values.get("cbm_value") or "")).strip()
        picked = values.get("cbm_unit") if values.get("cbm_unit") in ("cbm", "cft") else None
        number = read_volume_cbm(typed_value, picked) if typed_value else None
        if typed_value and number is None:
            errors["cbm_value"] = NEEDS_NUMBER
        out["cbm_value"] = number
        if number is None:
            out["cbm"] = None
        elif "cbm_unit" in values:
            amount, rest = leading_number(typed_value)
            unit = _unit_word(rest, _VOLUME_WORDS) or picked or "cbm"
            out["cbm_written"] = "{} cft".format(_fmt(amount, 3)) if unit == "cft" else None
    if "cbm_basis" in values:
        out["cbm_basis"] = "each" if values.get("cbm_basis") == "each" else "total"
    for key in ("source_mail", "source_text", "weight_written", "cbm_written", "decided"):
        if key in values:
            out[key] = (values.get(key) or "").strip() or None
    if "weight_reading" in values:
        out["weight_reading"] = values.get("weight_reading") if values.get("weight_reading") in ("auto", "chosen") else None
    if "stated_id" in values:
        typed_value = (str(values.get("stated_id") or "")).strip()
        out["stated_id"] = int(typed_value) if typed_value.isdigit() else None
    if "dims_unit_present" in values and "dims_unit" in values:
        unit = (values.get("dims_unit") or "").strip()
        out["dims_unit"] = unit if unit in DIMS_UNITS else None
    if "container_qty" in values:
        typed_value = (str(values.get("container_qty") or "")).strip()
        if typed_value:
            number, _rest = leading_number(typed_value)          # '1 x 40GP' -> 1 (13c)
            if number is None:
                errors["container_qty"] = NEEDS_NUMBER
            else:
                out["container_qty"] = int(number)
                if out["container_qty"] < 1:
                    errors["container_qty"] = "at least 1"
        else:
            out["container_qty"] = None
    if "container_type" in values:
        out["container_type"] = (values.get("container_type") or "").strip() or None
    # only the Cargo card's own checkbox says 'not counted' (it posts
    # counted_present); a form without it leaves the line counted
    if "counted_present" in values:
        out["counted"] = 1 if values.get("counted") in ("1", "on", 1, True) else 0
    if hasattr(values, "getlist") and "routes_present" in values:
        chosen = [v for v in values.getlist("route") if str(v).isdigit()]
        every = values.get("routes_total")
        out["route_ids"] = None if not chosen or (every and len(chosen) >= int(every)) else ",".join(sorted(set(chosen), key=int))
    return out, errors


def typed(values: Mapping) -> dict:
    """The line's fields as the table stores them (ints and floats), so a
    repository write compares like with like and logs only real changes;
    then normalise() fills the cm and kg columns from what was written."""
    out = {}
    for key in FIELDS:
        if key not in values:
            continue
        value = values[key]
        if key in _INTS:
            out[key] = int(float(value)) if value not in (None, "") else None
        elif key in _FLOATS:
            out[key] = float(value) if value not in (None, "") else None
        else:
            out[key] = (str(value).strip() or None) if value is not None else None
    if "counted" in out and out["counted"] is None:
        out["counted"] = 1
    if "weight_basis" in out and out["weight_basis"] not in WEIGHT_BASES:
        out["weight_basis"] = "total"
    if "cbm_basis" in out and out["cbm_basis"] not in ("total", "each"):
        out["cbm_basis"] = "total"
    if "weight_reading" in out and out["weight_reading"] not in ("auto", "chosen"):
        out["weight_reading"] = None
    return normalise(out)


def normalise(line: dict) -> dict:
    """The size per piece in cm and the line's total kg, from the numbers as
    written (dim_l x dim_w x dim_h in dims_unit; weight_value in weight_unit,
    in total or each). A line given only in cm and kg (older callers, the
    mail reader) gets its as-written copy in cm and kg."""
    out = dict(line)
    dims = [out.get(k) for k in ("dim_l", "dim_w", "dim_h")]
    if any(v is not None for v in dims):
        # v34: a size the mail gave without a unit stays without one - the
        # Cargo card asks cm or in; nothing is converted until then
        unit = out.get("dims_unit") if "dims_unit" in out else "cm"
        out["dims_unit"] = unit if unit in DIMS_UNITS else None
        factor = DIMS_UNITS.get(out["dims_unit"])
        for key, value in zip(("length_cm", "width_cm", "height_cm"), dims):
            out[key] = round(value * factor, 3) if value is not None and factor else None
    elif any(out.get(k) is not None for k in ("length_cm", "width_cm", "height_cm")):
        out["dim_l"], out["dim_w"], out["dim_h"] = (out.get(k) for k in ("length_cm", "width_cm", "height_cm"))
        out["dims_unit"] = "cm"
    pieces = out.get("pieces")
    if out.get("weight_value") is not None:
        unit = out.get("weight_unit") or "kg"
        out["weight_unit"] = unit
        each = out.get("weight_basis") == "each"
        boxes = out.get("container_qty")
        if each and not pieces:
            out["weight_kg"] = None               # per piece, pieces unknown: kept, not counted (v34)
        elif out.get("weight_basis") == "container":
            # 15b: per container, times the containers of a shipment (FCL)
            out["weight_kg"] = round(out["weight_value"] * WEIGHT_UNITS.get(unit, 1.0) * int(boxes), 3) if boxes else None
        else:
            out["weight_kg"] = round(out["weight_value"] * WEIGHT_UNITS.get(unit, 1.0) * (pieces if each else 1), 3)
    elif out.get("weight_kg") is not None and "weight_value" in out:
        out["weight_value"], out["weight_unit"] = out["weight_kg"], "kg"
        out["weight_basis"] = out.get("weight_basis") or "total"
    if out.get("cbm_value") is not None:
        each = out.get("cbm_basis") == "each"
        out["cbm"] = None if each and not pieces else round(out["cbm_value"] * (pieces if each else 1), 4)
    elif "cbm_value" in out and out.get("cbm") is not None:
        out["cbm_value"], out["cbm_basis"] = out["cbm"], out.get("cbm_basis") or "total"
    return out


def written(line: Mapping) -> dict:
    """What the Cargo card shows under each number (the CargoCheck sample):
    the size as written with its cm under it, the weight as written with
    the 'each' multiplication, the volume's basis."""
    d = derive(line)
    unit = line.get("dims_unit")
    dims = [line.get(k) for k in ("dim_l", "dim_w", "dim_h")]
    size = size_cm = ""
    if all(v is not None for v in dims):
        size = "{} x {} x {}{}".format(*(_fmt(v, 2) for v in dims), " " + unit if unit else "")
        if not unit:
            size_cm = "no unit: cm or in?"
        elif unit != "cm":
            size_cm = "= {} x {} x {} cm".format(*(_fmt(line.get(k), 1) for k in ("length_cm", "width_cm", "height_cm")))
    elif line.get("dims_text"):
        size = line["dims_text"]
    weight_note = ""
    value, wunit = line.get("weight_value"), line.get("weight_unit") or "kg"
    if value is not None:
        if line.get("weight_basis") == "each" and d["pieces_n"]:
            weight_note = "{} {} each x {}".format(_fmt(value, 3), wunit, d["pieces_n"])
            if wunit != "kg":
                weight_note += " = {} {}".format(_fmt(value * d["pieces_n"], 1), wunit)
        elif line.get("weight_basis") == "each":
            weight_note = "{} {} each - pieces?".format(_fmt(value, 3), wunit)
        elif line.get("weight_basis") == "container":
            weight_note = "{} {} per container".format(_fmt(value, 3), wunit)
        elif wunit != "kg":
            weight_note = "{} {}".format(_fmt(value, 3), wunit)
        written_as = line.get("weight_written")
        if written_as and line.get("weight_reading"):
            weight_note = "as written: {}".format(written_as) + (" - " + weight_note if weight_note else "")
    each_cbm = None
    if all(line.get(k) for k in ("length_cm", "width_cm", "height_cm")):
        each_cbm = line["length_cm"] * line["width_cm"] * line["height_cm"] / 1e6
    if d["cbm_from"] == "stated" and line.get("cbm_basis") == "each" and line.get("cbm_value") is not None:
        volume_note = "{} x {} each".format(d["pieces_n"], _fmt(line["cbm_value"], 3))
    elif d["cbm_from"] == "stated":
        volume_note = "as stated" + (" · " + d["cbm_note"] if d["cbm_note"] else "")
    elif each_cbm is not None:
        volume_note = "{} x {}".format(d["pieces_n"], _fmt(each_cbm, 3)) if (d["pieces_n"] or 1) > 1 else "from size"
    else:
        volume_note = ""
    # the card's numbers as the CargoCheck sample prints them: 1,730.0 kg, 9.600 cbm
    return {"size": size, "size_cm": size_cm,
            "weight": "{:,.1f}".format(d["weight_n"]) if d["weight_n"] is not None else "",
            "weight_note": weight_note,
            "volume": "{:,.3f}".format(d["volume_cbm"]) if d["volume_cbm"] is not None else "",
            "volume_note": volume_note, "each_cbm": each_cbm}


def is_blank(values: Mapping) -> bool:
    """A form row nobody filled in: no pieces, no size, no weight, no cbm."""
    return not any((str(values.get(k) or "")).strip() for k in
                   ("pieces", "dims_text", "length_cm", "weight_kg", "cbm", "deliver_to", "commodity",
                    "dim_l", "weight_value", "container_qty", "cbm_value"))
