"""The official code lists for the team site's place picker (batch D7a, INTERFACE.md section 8 `official/`).

`python -m opplist official-places --unlocode <release> --airports
<airports.csv>` reads the same two files fill-coordinates reads (the
UN/LOCODE release, OurAirports' airports.csv) and builds one file per
country and an index - public reference data only, nothing from the
database. The dry run prints the countries, the rows and the file sizes;
--apply writes them into ..\\OppListWeb\\app\\public\\official\\ (only
{CC}.json and index.json are replaced; nothing is deleted) and keeps the
two paths in Settings (official_unlocode_path, official_airports_path) so
D7b checks a pick against the same files.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import coordinates, db, paths

OUT_DIR = paths.DATA_DIR / "site" / "official"
SETTINGS = ("official_unlocode_path", "official_airports_path")
#: the Function column's eight places: 1 port, 2 rail, 3 road terminal, 4 airport,
#: 5 postal exchange, 6 multimodal, 7 fixed transport, B border crossing
_FLAGS = (("1", "p"), ("2", "r"), ("3", "t"), ("4", "a"), ("5", "o"), ("6", "m"), ("7", "f"), ("B", "b"))
_LARGE_MEDIUM = ("large_airport", "medium_airport")
_VERSION = re.compile(r"Version\s+(\d{4}-\d+)")


def flags_of(function: str) -> str:
    """'--34-6--' -> 'tam'; '' when none."""
    function = (function or "").upper()
    return "".join(letter for digit, letter in _FLAGS if digit in function)


def _coord(value):
    return round(value, 4) if value is not None else None


def _name_key(name: str):
    """Sorted as read: 'Abaeté' before 'Abaetetuba' (accents set aside, then as written)."""
    plain = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    return plain.casefold(), (name or "").casefold()


def release_of(source) -> str:
    """'2025-1': the release's version, read from its UNLOCODE.ttl (or any
    .ttl / .txt beside the CSVs); '' when it does not say."""
    source = Path(source)
    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as z:
            for info in z.infolist():
                if info.filename.lower().endswith(".ttl"):
                    with z.open(info) as f:
                        found = _VERSION.search(f.read(20000).decode("utf-8", "replace"))
                    if found:
                        return found.group(1)
        return ""
    folder = source if source.is_dir() else source.parent
    for base in (folder, folder.parent):
        for path in sorted(base.rglob("*.ttl")):
            with path.open("rb") as f:
                found = _VERSION.search(f.read(20000).decode("utf-8", "replace"))
            if found:
                return found.group(1)
    return ""


@dataclass
class Built:
    version: str
    built: str
    files: dict = field(default_factory=dict)        # {CC: {"c", "loc", "air"}}

    def index(self) -> dict:
        return {"v": self.version, "built": self.built,
                "countries": {cc: len(doc["loc"]) + len(doc["air"]) for cc, doc in sorted(self.files.items())}}


def build(unlocode_rows, airport_rows, version: str, built: str) -> Built:
    """{CC}.json for every country with at least one row: `loc` every
    UN/LOCODE entry but those marked for deletion (change X, status XX),
    `air` the large and medium airports with an IATA code, each sorted by
    name (INTERFACE.md section 8)."""
    loc, air = {}, {}
    for row in unlocode_rows:
        if row.get("change", "").strip().upper() == "X" or row.get("status", "").strip().upper() == "XX":
            continue
        cc = row["country"].strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", cc):
            continue
        loc.setdefault(cc, []).append([row["location"].strip().upper(), row["name"].strip(),
                                       row.get("subdivision", "").strip(), flags_of(row.get("function")),
                                       _coord(row.get("lat")), _coord(row.get("lon"))])
    for row in airport_rows:
        if row.get("type") not in _LARGE_MEDIUM or not row.get("iata"):
            continue
        cc = (row.get("country") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", cc):
            continue
        air.setdefault(cc, []).append([row["iata"], row.get("name", "").strip(),
                                       (row.get("municipality") or "").strip(),
                                       _coord(row.get("lat")), _coord(row.get("lon"))])
    out = Built(version, built)
    for cc in sorted(set(loc) | set(air)):
        out.files[cc] = {"c": cc,
                         "loc": sorted(loc.get(cc, []), key=lambda r: (_name_key(r[1]), r[0])),
                         "air": sorted(air.get(cc, []), key=lambda r: (_name_key(r[1]), r[0]))}
    return out


def dumps(doc) -> bytes:
    """Compact JSON, UTF-8, accents as they are."""
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def write(built: Built, out_dir=None) -> dict:
    """Replace {CC}.json and index.json in `out_dir`; nothing is deleted.
    Returns {file name: bytes written}."""
    out_dir = Path(out_dir or OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    sizes = {}
    for cc, doc in built.files.items():
        data = dumps(doc)
        (out_dir / "{}.json".format(cc)).write_bytes(data)
        sizes["{}.json".format(cc)] = len(data)
    data = dumps(built.index())
    (out_dir / "index.json").write_bytes(data)
    sizes["index.json"] = len(data)
    return sizes


def sizes_of(built: Built) -> dict:
    out = {"{}.json".format(cc): len(dumps(doc)) for cc, doc in built.files.items()}
    out["index.json"] = len(dumps(built.index()))
    return out


def remember(conn, unlocode, airports) -> None:
    """Settings: the two files a pick is checked against (D7b)."""
    db.set_setting(conn, SETTINGS[0], str(Path(unlocode).resolve()))
    db.set_setting(conn, SETTINGS[1], str(Path(airports).resolve()))
    conn.commit()


def lines(built: Built, sizes: dict, written: bool, out_dir=None) -> list:
    biggest = sorted(((n, s) for n, s in sizes.items() if n != "index.json"), key=lambda x: -x[1])[:5]
    out = ["UN/LOCODE release: {}".format(built.version or "(not found)"),
           "countries: {}".format(len(built.files)),
           "rows: {:,} UN/LOCODE, {:,} airports".format(sum(len(d["loc"]) for d in built.files.values()),
                                                      sum(len(d["air"]) for d in built.files.values())),
           "files: {} ({:,} bytes in all)".format(len(sizes), sum(sizes.values())),
           "biggest: " + ", ".join("{} {:,}".format(n, s) for n, s in biggest)]
    out.append(("written to " if written else "would be written to ") + str(Path(out_dir or OUT_DIR)))
    return out


# ------------------------------------- RG2a (F): a place read from a mail --
#: (unlocode path, airports path, their mtimes) -> Lookup; the files are read
#: once per process while they stay as they are
_LOOKUPS = {}
#: the Function digits of a UN/LOCODE row that make it a port, an airport
_PORT, _AIRPORT = "1", "4"


def _fold(text) -> str:
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    return " ".join(plain.split()).casefold()


@dataclass
class Lookup:
    """The two official files as the register reader asks them: an IATA
    code (large and medium airports), a UN/LOCODE, or a name in a country."""

    airports: dict = field(default_factory=dict)        # IATA -> row
    locodes: dict = field(default_factory=dict)         # 'CNNGB' -> row
    names: dict = field(default_factory=dict)           # (cc, folded name) -> [row]
    air_names: dict = field(default_factory=dict)       # (cc, folded name or municipality) -> [row]

    def __bool__(self) -> bool:
        return bool(self.airports or self.locodes)


def lookup(conn) -> Lookup:
    """The files Settings names (official_unlocode_path, official_airports_path);
    an empty Lookup when they are not set or not there."""
    paths_ = [(db.get_setting(conn, name, "") or "").strip() for name in SETTINGS]
    stamp = []
    for p in paths_:
        try:
            stamp.append(Path(p).stat().st_mtime if p else None)
        except OSError:
            stamp.append(None)
    key = (tuple(paths_), tuple(stamp))
    if key in _LOOKUPS:
        return _LOOKUPS[key]
    out = Lookup()
    if paths_[0] and stamp[0] is not None:
        try:
            for row in coordinates.read_unlocode(paths_[0]):
                if row.get("change", "").strip().upper() == "X" or row.get("status", "").strip().upper() == "XX":
                    continue
                cc = row["country"].strip().upper()
                out.locodes.setdefault(row["code"], row)
                for name in {row.get("name_wo_diacritics") or "", row.get("name") or ""}:
                    if name.strip():
                        out.names.setdefault((cc, _fold(name)), []).append(row)
        except (OSError, FileNotFoundError, ValueError):
            pass
    if paths_[1] and stamp[1] is not None:
        try:
            for row in coordinates.read_airports(paths_[1]):
                if row.get("type") not in _LARGE_MEDIUM or not row.get("iata"):
                    continue
                out.airports.setdefault(row["iata"], row)
                for name in {row.get("municipality") or "", row.get("name") or ""}:
                    if name.strip():
                        out.air_names.setdefault((row["country"], _fold(name)), []).append(row)
        except (OSError, ValueError):
            pass
    _LOOKUPS.clear()
    _LOOKUPS[key] = out
    return out


def _country(conn, cc):
    row = conn.execute("SELECT name FROM country WHERE iso2 = ?", (cc,)).fetchone()
    return row["name"] if row else None


def _iso2(conn, country):
    row = conn.execute("SELECT iso2 FROM country WHERE name = ?", (country or "",)).fetchone()
    return (row["iso2"] or "").upper() if row and row["iso2"] else ""


def _from_airport(conn, row) -> dict | None:
    country = _country(conn, row["country"])
    if not country:
        return None
    return {"text": row["iata"], "name": row.get("name") or row["iata"], "type": "airport", "country": country,
            "iata": row["iata"], "unlocode": None, "is_airport": 1, "is_seaport": 0,
            "town": row.get("municipality") or "", "official": "IATA {}".format(row["iata"])}


def _from_locode(conn, row, ocean: bool) -> dict | None:
    country = _country(conn, row["country"])
    if not country:
        return None
    function = row.get("function") or ""
    port, air = _PORT in function, _AIRPORT in function
    name = row.get("name") or row.get("name_wo_diacritics") or row["code"]
    text = row.get("name_wo_diacritics") or name
    sub = (row.get("subdivision") or "").strip().upper()
    if country in db.STATE_CODES and sub in db.STATE_CODES[country]:
        text = "{}, {}".format(text, sub)                 # a US or Canadian city is NAME, ST
    return {"text": text, "name": name, "type": "seaport" if (port and ocean) else "city", "country": country,
            "iata": None, "unlocode": row["code"], "is_airport": 1 if air else 0, "is_seaport": 1 if port else 0,
            "official": "UN/LOCODE {}".format(row["code"])}


def place_for(conn, text, country=None, mode=None) -> dict | None:
    """RG2a (F): the new master place a place read from a mail would be,
    built from the official row that names it - an IATA code of a large or
    medium airport, a UN/LOCODE ('CNNGB'), or the name of exactly one
    entry in the mail's `country` (an airport's name or town for Air).
    {'text', 'name', 'type', 'country', 'iata', 'unlocode', 'is_airport',
    'is_seaport', 'official'}; None when the files do not name it, or name
    it more than once."""
    found = lookup(conn)
    if not found:
        return None
    written = " ".join((text or "").split())
    if not written:
        return None
    ocean = mode in ("FCL", "LCL", "RORO")
    code = written.upper()
    if re.fullmatch(r"[A-Z]{3}", written) and code in found.airports:
        return _from_airport(conn, found.airports[code])
    if re.fullmatch(r"[A-Z]{2}\s?[A-Z2-9]{3}", written) and code.replace(" ", "") in found.locodes:
        return _from_locode(conn, found.locodes[code.replace(" ", "")], ocean)
    cc = _iso2(conn, country)
    if not cc or len(written) < 3:
        return None
    name = _fold(written.split(",")[0])
    if mode == "Air":
        rows = {r["iata"]: r for r in found.air_names.get((cc, name), [])}
        if len(rows) == 1:
            return _from_airport(conn, next(iter(rows.values())))
    rows = {r["code"]: r for r in found.names.get((cc, name), [])}
    if len(rows) > 1:
        # one name, several entries: the one a port (ocean) or the one approved
        fits = {c: r for c, r in rows.items() if _PORT in (r.get("function") or "")} if ocean else {}
        rows = fits if len(fits) == 1 else {c: r for c, r in rows.items()
                                            if (r.get("status") or "").upper() in coordinates._APPROVED}
    if len(rows) == 1:
        return _from_locode(conn, next(iter(rows.values())), ocean)
    return None


def run(unlocode, airports, today=None) -> Built:
    unlocode_rows = coordinates.read_unlocode(unlocode)
    airport_rows = coordinates.read_airports(airports)
    return build(unlocode_rows, airport_rows, release_of(unlocode),
                 (today or dt.date.today().isoformat()))
