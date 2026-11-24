"""Packing from a list (batch 23, schema v39).

A cargo line's Packing is a choice from the Settings list packing_units
(Pallets, Skids, Crates, ...), stored in enquiry_cargo.pack_unit. The word
the mail (or a person) wrote stays in pack_word as written, so nothing read
is lost: 'plt' stays 'plt' and its Packing is Pallets.

    unit_for(word, units)  the list entry a written word means, or None
    singular(unit)         'Boxes' -> 'box', 'Big bags' -> 'big bag'
    label(pieces, unit)    '1 pallet', '25 big bags'

A word that does not map ('machinery', 'carton on skid') leaves Packing
empty: she picks it.
"""

from __future__ import annotations

import re

#: The default list (setting packing_units; the Settings page edits it).
DEFAULT_UNITS = ["Pallets", "Skids", "Crates", "Cases", "Boxes", "Cartons", "Packages", "Pieces",
                 "Drums", "Bags", "Big bags", "Rolls", "Bundles", "Units"]

#: What a mail writes for each default unit, besides the unit's own
#: singular and plural (casefolded, spaces collapsed). Only whole words:
#: 'bulk piece' or 'carton on skid' are not on it and do not map.
WORDS = {
    "Pallets": ("plt", "plts", "ptl", "ptls", "pallete", "palletes", "euro pallet", "euro pallets",
                "europallet", "europallets", "wooden pallet", "wooden pallets", "palet", "palets",
                "paleta", "paletas", "palete", "paletes"),
    "Skids": ("skd", "skds", "wooden skid", "wooden skids"),
    "Crates": ("crt", "crts", "wooden crate", "wooden crates"),
    "Cases": ("wooden case", "wooden cases", "cs"),
    "Boxes": ("bx", "bxs", "caja", "cajas", "caixa", "caixas"),
    "Cartons": ("ctn", "ctns", "carton box", "carton boxes", "cartoon", "cartoons"),
    "Packages": ("pkg", "pkgs", "pk", "pks", "pack", "packs", "bulto", "bultos", "volume", "volumes",
                 "volumen", "volumenes", "colli", "coli", "collo"),
    "Pieces": ("pc", "pcs", "pz", "pza", "pzas", "pieza", "piezas", "peca", "pecas"),
    "Drums": ("barrel", "barrels", "tambor", "tambores"),
    "Bags": ("sack", "sacks", "saco", "sacos", "bolsa", "bolsas"),
    "Big bags": ("bigbag", "bigbags", "jumbo bag", "jumbo bags", "bulk bag", "bulk bags", "fibc", "fibcs",
                 "super sack", "super sacks"),
    "Rolls": ("rollo", "rollos", "bobina", "bobinas"),
    "Bundles": ("bdl", "bdls", "fardo", "fardos"),
    "Units": ("unidad", "unidades"),
}

#: '... in 5 x 40' containers' after the word says how the pieces travel,
#: not what they are: 'pallets in 5 x 40' containers' is Pallets.
_CONTAINER_TAIL = re.compile(r"\s+in\s+\d+\s*x\s*\d+.*$", re.IGNORECASE)


def _norm(text) -> str:
    return " ".join(str(text or "").casefold().replace("’", "'").strip(" .,;:-").split())


def singular(unit) -> str:
    """'Boxes' -> 'box', 'Cases' -> 'case', 'Big bags' -> 'big bag',
    'Pallets' -> 'pallet': the list entry for one piece, lower case."""
    text = " ".join(str(unit or "").split()).lower()
    if not text:
        return ""
    for tail in ("xes", "ches", "shes", "sses", "zzes"):
        if text.endswith(tail):
            return text[:-2]
    if text.endswith("ies") and len(text) > 4:
        return text[:-3] + "y"
    if text.endswith("s") and not text.endswith("ss"):
        return text[:-1]
    return text


def _alias_table() -> dict:
    out = {}
    for unit, words in WORDS.items():
        for word in (unit.casefold(), singular(unit)) + tuple(words):
            out.setdefault(_norm(word), unit)
    return out


_ALIASES = _alias_table()


def unit_for(word, units=None) -> str | None:
    """The entry of `units` (her Settings list; the default list when not
    given) that a written word means: 'plt' -> 'Pallets', 'wooden case' ->
    'Cases', 'bultos' -> 'Packages'. None when the word does not map, or
    maps to a unit that is not on her list."""
    text = _CONTAINER_TAIL.sub("", _norm(word))
    if not text:
        return None
    units = [u for u in (units if units is not None else DEFAULT_UNITS) if str(u or "").strip()]
    # one of her own entries, written as it is or for one piece
    for unit in units:
        if text in (_norm(unit), _norm(singular(unit))):
            return unit
    canonical = _ALIASES.get(text)
    if canonical is None:
        return None
    for unit in units:
        if _norm(unit) == _norm(canonical) or _norm(singular(unit)) == _norm(singular(canonical)):
            return unit
    return None


def label(pieces, unit) -> str:
    """'1 pallet', '25 big bags', '3 boxes': the unit in lower case, its
    singular for one piece; the unit alone when no pieces are given."""
    unit = " ".join(str(unit or "").split())
    if pieces in (None, ""):
        return unit.lower()
    try:
        number = float(pieces)
    except (TypeError, ValueError):
        return "{} {}".format(pieces, unit.lower()).strip()
    shown = int(number) if number == int(number) else number
    if not unit:
        return str(shown)
    return "{} {}".format(shown, singular(unit) if number == 1 else unit.lower())


def units_of(conn) -> list:
    """Her list (setting packing_units), the default one when it is not set."""
    from . import db
    return db.get_setting_list(conn, "packing_units") or list(DEFAULT_UNITS)
