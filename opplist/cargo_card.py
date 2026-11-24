"""The Cargo card of the case page, v3 part 2 (batch 15b; sample marks 3
and 4). Cargo is lines only: what the card shows is worked out here from
the lines, so the page and the tests read the same thing.

    rows(...)        one display row per line, by mode: FCL shows What,
                     Weight (per container), Containers, How often, Routes;
                     Air and LCL show What, Size of each, Weight, Volume
                     (and Routes when the case has more than one route).
                     A number the line needs and lacks reads 'missing'.
    strip(...)       the totals strip, each total with its working under it.
    source_text(...) what 'Show the mail text' shows: the lines' stored text
                     (13b); for a case registered before 13b the registered
                     mail from the archive; otherwise qty_raw.
    read_lines(text) the registration reader run on that text: the lines
                     it finds, as line dicts (nothing is saved).
"""

from __future__ import annotations

import re

from . import cargo, parsing, routes

FCL_LIKE = ("FCL",)


def _plain(value, places=3) -> str:
    if value is None:
        return ""
    text = "{:,.{}f}".format(float(value), places)
    return text.rstrip("0").rstrip(".") if "." in text else text


def _kg(value) -> str:
    return "{:,.1f} kg".format(value) if value is not None else ""


def _t(value) -> str:
    return "{:,.1f} t".format(value / 1000.0) if value is not None else ""


def is_fcl(mode) -> bool:
    return (mode or "") in FCL_LIKE


# ------------------------------------------------------------------ rows --
def what_of(line) -> str:
    """'1 pallet', '25 big bags' from the Packing chosen (v39, its singular
    for one piece); without one, the word as written ('3 crates'); '' when
    the line gives neither pieces nor a packing."""
    from . import packing

    pieces, pack = line.get("pieces"), (line.get("pack_word") or "").strip()
    if (line.get("pack_unit") or "").strip():
        return packing.label(pieces, line["pack_unit"])
    if pieces:
        return "{} {}".format(pieces, pack or ("piece" if pieces == 1 else "pieces")).strip()
    return pack


def row(line, n, mode, live_routes) -> dict:
    """What one line shows on the card."""
    d = cargo.derive(line)
    fcl = is_fcl(mode)
    pieces, pack = line.get("pieces"), (line.get("pack_word") or "").strip()
    what = what_of(line)
    out = {"id": line.get("id"), "n": n, "counted": bool(int(line.get("counted", 1) or 0)),
           "what": what or ("" if fcl else "totals only"), "commodity": line.get("commodity") or "",
           # D7a (v50): under the commodity, 'HS 2507.00.10 · USD 12,500'
           "hs": line.get("hs_code") or "", "value": cargo.value_text(line.get("cargo_value"), line.get("cargo_value_cur")),
           "routes": routes.letters_of(line.get("route_ids"), live_routes) if live_routes else "All",
           "source": line.get("source") or ""}
    # the weight, as written, and what it comes to
    value, unit, basis = line.get("weight_value"), line.get("weight_unit") or "kg", line.get("weight_basis") or "total"
    if value is None:
        out["weight"], out["weight_missing"] = "missing", True
        out["weight_sub"] = "per container" if fcl and basis == "container" else ("per shipment" if fcl else "")
    else:
        out["weight"], out["weight_missing"] = "{} {}".format(_plain(value), unit), False
        if fcl:
            if basis == "container":
                each = value * cargo.WEIGHT_UNITS.get(unit, 1.0)
                out["weight_sub"] = "= {} per container".format(_kg(each)) if unit != "kg" else "per container"
            else:
                out["weight_sub"] = "= {} per shipment".format(_kg(d["weight_n"])) if unit != "kg" and d["weight_n"] is not None \
                    else "per shipment, all containers"
        elif basis == "each":
            out["weight_sub"] = ("{} x {} {} = {}".format(pieces, _plain(value), unit, _kg(d["weight_n"]))
                                 if d["weight_n"] is not None else "each piece - how many pieces?")
        elif unit != "kg":
            out["weight_sub"] = "= {}".format(_kg(d["weight_n"]))
        else:
            out["weight_sub"] = "in total"
    if fcl:
        qty, ctype = line.get("container_qty"), line.get("container_type")
        out["containers"] = "{} x {}".format(qty, ctype or "?") if qty else (ctype or "")
        out["containers_sub"] = "per shipment, each route" if qty else ""
        out["containers_missing"] = not qty
        pm, months = line.get("per_month"), line.get("months")
        out["often"] = "{} a month".format(_plain(pm, 2)) if pm else ""
        out["often_sub"] = "for {} months".format(_plain(months, 1)) if months else ""
        return out
    # the size of each piece, as written, and in cm
    dims = [line.get(k) for k in ("dim_l", "dim_w", "dim_h")]
    dunit = line.get("dims_unit")
    if all(v is not None for v in dims):
        out["size"] = "{} x {} x {}{}".format(*(_plain(v, 2) for v in dims), " " + dunit if dunit else "")
        if not dunit:
            out["size_sub"], out["size_warn"] = "no unit: cm or in?", True
        elif dunit != "cm":
            out["size_sub"] = "= {} x {} x {} cm".format(*(_plain(line.get(k), 1) for k in ("length_cm", "width_cm", "height_cm")))
    elif line.get("dims_text"):
        out["size"] = line["dims_text"]
    else:
        out["size"], out["size_none"] = "not given", True
    # the volume
    if d["volume_cbm"] is None:
        out["volume"], out["volume_missing"] = "missing", True
    else:
        out["volume"] = "{:,.3f} cbm".format(d["volume_cbm"])
        written = line.get("cbm_written") or ""
        if d["cbm_from"] == "stated":
            out["volume_sub"] = written if "cft" in written.lower() else (
                "{} x {} each".format(pieces, _plain(line.get("cbm_value"))) if line.get("cbm_basis") == "each" else "as stated")
        else:
            each = (line.get("length_cm") or 0) * (line.get("width_cm") or 0) * (line.get("height_cm") or 0) / 1e6
            out["volume_sub"] = "{} x {}".format(pieces or 1, "{:.3f}".format(each))
    return out


def rows(lines, mode, live_routes) -> list:
    return [row(line, n, mode, live_routes) for n, line in enumerate(lines, start=1)]


def _size_waits_for_unit(line) -> bool:
    return all(line.get(k) is not None for k in ("dim_l", "dim_w", "dim_h")) and not line.get("dims_unit")


def missing(lines, mode) -> list:
    """[(row number, what)] for the counted lines lacking a number they need."""
    out = []
    for n, line in enumerate(lines, start=1):
        if not int(line.get("counted", 1) or 0):
            continue
        d = cargo.derive(line)
        if line.get("weight_value") is None and d["weight_n"] is None:
            out.append((n, "weight"))
        if is_fcl(mode):
            if not line.get("container_qty"):
                out.append((n, "containers"))
        elif d["volume_cbm"] is None and not _size_waits_for_unit(line):
            # 15c: a size whose unit is still to choose is asked on its own (cargo_checks 'unit')
            out.append((n, "volume or size"))
    return out


# ---------------------------------------------------------------- totals --
def strip(t, lines, mode, live_routes) -> list:
    """[{label, value, formula, key}] - the totals strip by mode. An empty
    total reads '-'."""
    counted = [l for l in lines if int(l.get("counted", 1) or 0)]
    letters = [r.get("letter") or "A" for r in (live_routes or [])] or ["A"]
    n_routes = len(letters)
    if is_fcl(mode):
        types = sorted({l.get("container_type") for l in counted if l.get("container_type")})
        ctype = types[0] if len(types) == 1 else ("mixed" if types else "")
        per_ship = t.get("container_qty")
        each_route = [r["containers"] for r in t.get("per_route", [])]
        if per_ship and n_routes > 1 and len(set(each_route)) == 1:
            ship_f = "{} on each of {}".format(each_route[0], ", ".join(letters))
        elif per_ship:
            ship_f = "one route" if n_routes == 1 else " + ".join(str(x) for x in each_route)
        else:
            ship_f = "type the containers in a line"
        # weight per container: from the lines that give both
        per_box, first_missing = set(), None
        for n, l in enumerate(lines, start=1):
            if not int(l.get("counted", 1) or 0):
                continue
            qty, kg = l.get("container_qty"), cargo.derive(l)["weight_n"]
            if qty and kg is not None:
                per_box.add(round(kg / int(qty), 1))
            elif first_missing is None:
                first_missing = n
        if len(per_box) == 1:
            wpc, wpc_f = _kg(per_box.pop()), "line 1" if len(counted) == 1 else "every line"
        elif per_box:
            wpc, wpc_f = "varies", "see the lines"
        else:
            wpc, wpc_f = "-", "type the weight in line {}".format(first_missing or 1)
        pm, project = t.get("per_month"), t.get("project")
        line_pm = next((l.get("per_month") for l in counted if l.get("per_month")), None)
        months = t.get("months")
        return [
            {"label": "Per shipment", "value": "{} x {}".format(per_ship, ctype).strip() if per_ship else "-", "formula": ship_f},
            {"label": "Weight per container", "value": wpc, "formula": wpc_f},
            {"label": "Gross per shipment", "value": _kg(t.get("gross_kg")) or "-",
             "formula": "all routes" if n_routes > 1 else "one route"},
            {"label": "Per month", "value": "{} x {}".format(_plain(pm, 2), ctype).strip() if pm else "-",
             "formula": "{} a month x {} route{}".format(_plain(line_pm, 2), n_routes, "s" if n_routes != 1 else "")
             if pm and line_pm else ""},
            {"label": "Whole project", "value": "{} x {}".format(_plain(project, 1), ctype).strip() if project else "-",
             "formula": "{} a month x {} months".format(_plain(pm, 2), _plain(months, 1)) if project and months else "",
             "key": True},
        ]
    pieces = [int(l["pieces"]) for l in counted if l.get("pieces")]
    gross, volume = t.get("gross_kg"), t.get("volume_cbm")
    froms = {cargo.derive(l)["cbm_from"] for l in counted if cargo.derive(l)["volume_cbm"] is not None}
    vol_f = "as stated" if froms == {"stated"} else ("from the size" if froms == {"dims"} else
                                                     ("sum of the lines" if froms else ""))
    if volume is None and any(_size_waits_for_unit(l) for l in counted):
        vol_f = "choose the size unit"
    out = [
        {"label": "Pieces", "value": str(sum(pieces)) if pieces else "-", "formula": "counted lines" if pieces else "not given"},
        {"label": "Gross weight", "value": _kg(gross) or "-", "formula": "sum of the lines" if gross is not None else ""},
        {"label": "Volume", "value": "{:,.3f} cbm".format(volume) if volume is not None else "-", "formula": vol_f},
    ]
    if mode in cargo.AIR_MODES:
        vk = volume * cargo.CBM_TO_KG if volume is not None else None
        ch = routes.chargeable(gross, volume)
        why = ("volumetric is higher" if vk > gross else "gross is higher") if vk is not None and gross is not None else ""
        out.append({"label": "Volumetric", "value": _kg(vk) or "-",
                    "formula": "{:,.3f} cbm x 166.67".format(volume) if vk is not None else ""})
        out.append({"label": "Chargeable", "value": _kg(ch) or "-", "formula": why, "key": True})
    elif mode == "LCL":
        if gross is not None or volume is not None:
            wm = max(volume or 0, (gross or 0) / 1000.0)
            out.append({"label": "W/M", "value": "{:,.3f}".format(wm),
                        "formula": "max({:,.3f} cbm, {:,.3f} t)".format(volume or 0, (gross or 0) / 1000.0), "key": True})
        else:
            out.append({"label": "W/M", "value": "-", "formula": "", "key": True})
    return out


# ------------------------------------------------------------ mail text --
def source_text(lines, stated, qty_raw=None, archived=None) -> dict:
    """{'head', 'label', 'lines': [(marker, text)], 'text'}: what 'Show the
    mail text' shows. 13b's stored text first; for a case registered
    before 13b, the registered mail (`archived`: (head, text)) from the
    archive; otherwise qty_raw, labelled Quantity as received."""
    from . import cargo_checks
    stored = cargo_checks.mail_text(lines, stated, None)
    if stored:
        flat = [(marker, one) for marker, block in stored for one in (block or "").split("\n") if one.strip()]
        return {"head": "", "label": "", "lines": flat, "text": "\n".join(t for _, t in flat)}
    if archived and archived[1]:
        body = [one for one in archived[1].split("\n") if one.strip()]
        return {"head": archived[0], "label": "", "lines": [("", one) for one in body], "text": "\n".join(body)}
    if qty_raw:
        return {"head": "", "label": "Quantity as received", "lines": [("", qty_raw)], "text": qty_raw}
    return {"head": "", "label": "", "lines": [], "text": ""}


def archived_text(search, row, conn=None) -> tuple | None:
    """(head, text) of the registered mail's cargo part, from the archive -
    found the way the Mail card finds it: by its Outlook id, else the
    earliest mail of the case's thread (the registered subject first). The
    writer's own lines, or the first quoted mail with cargo in it."""
    if search is None:
        return None
    mail = search.by_entry_id(row["entry_id"]) if row["entry_id"] else None
    if mail is None and conn is not None:
        from . import case_mail
        mails = sorted(case_mail.case_mails(conn, search, row), key=lambda m: getattr(m, "received_at", "") or "")
        same = [m for m in mails if (getattr(m, "subject", "") or "").strip() == (row["source_subject"] or "").strip()]
        mail = (same or mails or [None])[0]
    if mail is None:
        return None
    body = getattr(mail, "body_excerpt", "") or ""
    head = "{} · {}".format(getattr(mail, "sender_name", "") or getattr(mail, "sender_address", ""),
                           (getattr(mail, "received_on", "") or getattr(mail, "received_at", "") or "")[:10]).strip(" ·")
    return head, cargo_text_of(getattr(mail, "body_excerpt", "") or "")


#: a line that says something about the cargo: a number with a unit, a
#: size, a packing word, containers, how often or how long
_CARGO_SIGNAL = re.compile(
    r"\d[\d.,]*\s*(?:kgs?|kilos?|lbs?|t|tons?|tonnes?|toneladas?|cbm|m3|m\u00b3|cft|cms?|mm|in|inch(?:es)?|"
    r"containers?|contentores?|contenedores?|cntrs?|pallets?|plts?|paletes?|skids?|cartons?|ctns?|pcs|pieces|"
    r"bags?|big\s*bags?|drums?|crates?|boxes|months?|meses|m\u00eas|years?|anos?|a\u00f1os|p\u00e9s|ft)\b"
    r"|\d\s*[x\u00d7*]\s*\d|\d\s*x\s*(?:20|40|45)|(?:20|40|45)\s*'|por\s+m\u00eas|per\s+month",
    re.IGNORECASE)
CARGO_TEXT_LINES = 25


def cargo_text_of(body) -> str:
    """The cargo lines of a whole mail, in order and once each (the
    thread below repeats them), a one-cell-per-line packing table read as
    rows: what 'Show the mail text' shows for a case registered before 13b."""
    raw = [l.strip(" \t>*\u2022-") for l in (body or "").replace("\r", "").split("\n")]
    raw = [l for l in raw if l]
    out = []
    for line in parsing.flatten_vertical_tables(raw):
        clean = " ".join(line.split())
        if _CARGO_SIGNAL.search(clean) and clean not in out:
            out.append(clean)
        if len(out) >= CARGO_TEXT_LINES:
            break
    return "\n".join(out)


# ---------------------------------------------------------- Read into lines --
def read_lines(text) -> list:
    """The cargo lines the registration reader finds in a text, as line
    dicts (the numbers as written); [] when it finds none."""
    lines = parsing.body_lines(text or "") if "\n" in (text or "") else [text or ""]
    if not any(l.strip() for l in lines):
        return []
    found = []
    for block in parsing.cargo_blocks(lines):
        if block.has_cargo:
            block_lines, _stated = cargo.lines_from_block(block, None, source="manual")
            found.extend(block_lines)
    if found:
        return found
    labels = parsing.parse_cargo_labels(lines)
    if labels.weight_kg is not None or labels.volume_cbm is not None:
        return [cargo.normalise({"pieces": labels.pieces, "pack_word": labels.piece_word or None,
                                 "weight_value": labels.weight_kg, "weight_unit": "kg" if labels.weight_kg is not None else None,
                                 "weight_basis": "total", "cbm_value": labels.volume_cbm, "cbm_basis": "total",
                                 "counted": 1, "source": "manual"})]
    qty = parsing.parse_body_qty("\n".join(lines))
    boxes = re.findall(r"(\d+)\s*x\s*(\d{2}\w*)", qty.qty_raw or "") if qty.mode == "FCL" else []
    if boxes:
        return [cargo.normalise({"container_qty": int(n), "container_type": parsing.normalise_container(*_split_box(b)),
                                 "counted": 1, "source": "manual"}) for n, b in boxes]
    return []


def _split_box(text):
    found = re.match(r"(\d{2})\s*'?\s*(\w*)", text)
    return (found.group(1), found.group(2)) if found else (text, "")
