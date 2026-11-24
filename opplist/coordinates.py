"""Coordinates for master_location (2026-09-24, schema v27):
lat / lon filled from two reference files Iris downloaded by hand -
the UN/LOCODE release (code list, cp1252 / latin-1, no header) and
OurAirports' airports.csv (UTF-8). The app never fetches anything.

Matching, in order, first hit wins:
- manual: data\\coordinates-manual.csv (key, lat, lon, note), exact key;
  never over a coordinate already set unless --manual-wins; a key that is
  not on file is listed, not created;
- unlocode: the location's own UN/LOCODE against the code list;
- iata: an airport's IATA code against airports.csv (large / medium /
  small airports only);
- name: NameWoDiacritics + country code in the code list, taken only when
  exactly one candidate fits - a seaport needs Function '1', an airport
  Function '4', a city takes any entry with coordinates; when several fit,
  the one whose code is the location's IATA, else the only one that is a
  port or airport (Function 1 or 4), else the only approved one (Status
  AA / AC / AF / AI / AS) - still several, and it is ambiguous.
- city-airport: the UN/LOCODE release carries no coordinates for many
  major entries (USCHI, GBLON, PABLB...), so a city or port still without
  them takes the one large / medium airport OurAirports lists under that
  municipality and country - close enough for a map of the world, and
  marked as such so a person can tighten it.
A dry run writes data\\coordinates-proposed.csv; --apply writes only the
coordinates that are still empty, through the repository.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import db

#: The file's real order: Function comes before Status ('--34-6--' then
#: 'AI'), whatever the documentation lists.
UNLOCODE_COLUMNS = ("change", "country", "location", "name", "name_wo_diacritics", "subdivision",
                    "function", "status", "date", "iata", "coordinates", "remarks")
AIRPORT_TYPES = ("large_airport", "medium_airport", "small_airport")
_DMS = re.compile(r"^\s*(\d{2})(\d{2})([NS])\s+(\d{3})(\d{2})([EW])\s*$")


def dms_to_decimal(text: str):
    """'4047N 07400W' -> (40.7833, -74.0). None when it does not read."""
    found = _DMS.match(text or "")
    if not found:
        return None
    lat = int(found.group(1)) + int(found.group(2)) / 60.0
    lon = int(found.group(4)) + int(found.group(5)) / 60.0
    if found.group(3) == "S":
        lat = -lat
    if found.group(6) == "W":
        lon = -lon
    return round(lat, 4), round(lon, 4)


# --------------------------------------------------------------- readers --
def read_manual(path) -> list:
    """data\\coordinates-manual.csv: [{key, lat, lon, note}]; a row whose
    numbers do not read is kept with lat None so it is listed."""
    out = []
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = (row.get("key") or "").strip()
            if not key:
                continue
            try:
                lat, lon = float(row.get("lat") or ""), float(row.get("lon") or "")
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError
            except ValueError:
                lat = lon = None
            out.append({"key": key, "lat": lat, "lon": lon, "note": (row.get("note") or "").strip()})
    return out


def _decode(raw: bytes) -> str:
    """D7a (v50): UTF-8 first (strict), then cp1252, then latin-1 - a UTF-8
    release read as cp1252 gave 'SÃ£o Paulo' for 'São Paulo'. A cp1252
    file with any letter beyond ASCII is never valid UTF-8, so it still
    reads as cp1252."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def read_unlocode(source) -> list:
    """The code list rows as dicts, from a zip, a folder, or one CSV.
    Rows without coordinates are kept (the name match wants to know they
    exist) with lat / lon None."""
    source = Path(source)
    texts = []
    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as z:
            for info in sorted(z.infolist(), key=lambda i: i.filename):
                name = info.filename.lower()
                if name.endswith(".csv") and "codelist" in name.replace(" ", "").replace("-", ""):
                    texts.append(_decode(z.read(info)))
    elif source.is_dir():
        for path in sorted(source.rglob("*.csv")):
            if "codelist" in path.name.lower().replace(" ", "").replace("-", ""):
                texts.append(_decode(path.read_bytes()))
    elif source.is_file():
        texts.append(_decode(source.read_bytes()))
    if not texts:
        raise FileNotFoundError("no UN/LOCODE code list CSV under {}".format(source))
    rows = []
    for text in texts:
        for cells in csv.reader(io.StringIO(text)):
            if len(cells) < 11 or not cells[2].strip():
                continue                                    # a country header line, or short
            row = dict(zip(UNLOCODE_COLUMNS, [c.strip() for c in cells] + [""] * 12))
            coords = dms_to_decimal(row["coordinates"])
            row["lat"], row["lon"] = coords if coords else (None, None)
            row["code"] = row["country"].upper() + row["location"].upper()
            rows.append(row)
    return rows


def read_airports(path) -> list:
    """OurAirports rows (large / medium / small) as dicts with lat / lon."""
    out = []
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("type") not in AIRPORT_TYPES:
                continue
            try:
                lat, lon = float(row["latitude_deg"]), float(row["longitude_deg"])
            except (TypeError, ValueError):
                continue
            out.append({"name": row.get("name", ""), "iata": (row.get("iata_code") or "").strip().upper(),
                        "country": (row.get("iso_country") or "").strip().upper(), "type": row["type"],
                        "municipality": (row.get("municipality") or "").strip(),
                        "region": (row.get("iso_region") or "").strip().upper(),
                        "lat": round(lat, 4), "lon": round(lon, 4)})
    return out


# -------------------------------------------------------------- matching --
@dataclass
class Proposal:
    location_id: int
    key: str
    source: str                    # manual | unlocode | iata | name | city-airport | ambiguous | unmatched | unknown-key | retired-key | kept
    matched: str = ""
    lat: float | None = None
    lon: float | None = None
    candidates: list = field(default_factory=list)
    note: str = ""


def _fits(location_type: str, function: str) -> bool:
    if location_type == "seaport":
        return "1" in function
    if location_type == "airport":
        return "4" in function
    return True


_APPROVED = ("AA", "AC", "AF", "AI", "AS")


def _narrow(candidates: list, iata: str) -> list:
    """Several entries of one name: the tie-breakers, each applied only
    when it leaves exactly one."""
    if len(candidates) <= 1:
        return candidates
    if iata:
        hit = [r for r in candidates if r["location"].upper() == iata or r["iata"].upper() == iata]
        if len(hit) == 1:
            return hit
    hit = [r for r in candidates if "1" in r["function"] or "4" in r["function"]]
    if len(hit) == 1:
        return hit
    approved = [r for r in candidates if r["status"].upper() in _APPROVED]
    if len(approved) == 1:
        return approved
    return candidates


def _km(a, b) -> float:
    import math
    lat1, lon1, lat2, lon2 = (math.radians(v) for v in (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def town_airport(town: list):
    """The town's one large airport; when it has several (London, Chicago)
    and they sit within 60 km of each other, their mean point; else the
    one medium airport. None when nothing fits."""
    large = [a for a in town if a["type"] == "large_airport"]
    pick = large or [a for a in town if a["type"] == "medium_airport"]
    if not pick:
        return None
    if len(pick) == 1:
        a = pick[0]
        return {"matched": "{} ({})".format(a["name"], a["iata"] or "-"), "lat": a["lat"], "lon": a["lon"]}
    if max(_km(x, y) for x in pick for y in pick) > 60:
        return None
    return {"matched": "mean of {} airports: {}".format(len(pick), ", ".join(a["iata"] or a["name"] for a in pick)),
            "lat": round(sum(a["lat"] for a in pick) / len(pick), 4),
            "lon": round(sum(a["lon"] for a in pick) / len(pick), 4)}


_STATE = re.compile(r",\s*([A-Z]{2})$")


def state_of(key: str, country: str):
    """'BEAUFORT, NC' -> 'NC' for a US or Canadian key; None otherwise."""
    if country not in ("US", "CA"):
        return None
    found = _STATE.search((key or "").strip().upper())
    return found.group(1) if found else None


def propose(conn, unlocode_rows, airport_rows, only_empty: bool = True, manual=None,
            manual_wins: bool = False) -> list:
    """One Proposal per live location (empty coordinates only by default),
    the manual rows first. A manual row for a location whose coordinates
    are set is 'kept' (the file loses) unless manual_wins; one for a key
    not on file is 'unknown-key', for a retired row 'retired-key'."""
    out = []
    done = set()
    for row in manual or []:
        loc = conn.execute("SELECT id, key, status, lat, lon FROM master_location WHERE key = ?",
                           (row["key"],)).fetchone()
        if loc is None:
            out.append(Proposal(0, row["key"], "unknown-key", note=row["note"]))
            continue
        if loc["status"] == "retired":
            out.append(Proposal(loc["id"], loc["key"], "retired-key", note=row["note"]))
            continue
        done.add(loc["id"])
        if row["lat"] is None:
            out.append(Proposal(loc["id"], loc["key"], "unmatched", "manual row does not read", note=row["note"]))
        elif loc["lat"] is not None and loc["lon"] is not None and not manual_wins:
            out.append(Proposal(loc["id"], loc["key"], "kept", "already {}, {}".format(loc["lat"], loc["lon"]),
                                note=row["note"]))
        else:
            out.append(Proposal(loc["id"], loc["key"], "manual", row["note"] or "manual", row["lat"], row["lon"],
                                note=row["note"]))
    by_code = {}
    by_name = {}
    for row in unlocode_rows:
        if row["lat"] is not None:
            by_code.setdefault(row["code"], row)
        by_name.setdefault((row["country"].upper(), row["name_wo_diacritics"].casefold()), []).append(row)
    by_iata = {}
    by_town = {}
    for row in airport_rows:
        if row["iata"]:
            by_iata.setdefault(row["iata"], row)
        if row.get("municipality") and row["type"] in ("large_airport", "medium_airport"):
            by_town.setdefault((row["country"], row["municipality"].casefold()), []).append(row)
    sql = "SELECT id, key, name, type, country_code, unlocode, iata, lat, lon FROM master_location WHERE status <> 'retired'"
    if only_empty:
        sql += " AND (lat IS NULL OR lon IS NULL)"
    for loc in conn.execute(sql + " ORDER BY key").fetchall():
        if loc["id"] in done:
            continue
        code = (loc["unlocode"] or "").replace(" ", "").upper()
        hit = by_code.get(code) if code else None
        if hit is not None:
            out.append(Proposal(loc["id"], loc["key"], "unlocode", hit["name"], hit["lat"], hit["lon"]))
            continue
        iata = (loc["iata"] or "").strip().upper()
        if loc["type"] == "airport" and iata and iata in by_iata:
            hit = by_iata[iata]
            out.append(Proposal(loc["id"], loc["key"], "iata", hit["name"], hit["lat"], hit["lon"]))
            continue
        country = (loc["country_code"] or "").upper()
        wanted = (loc["name"] or loc["key"] or "").casefold()
        state = state_of(loc["key"], country)
        candidates = [r for r in by_name.get((country, wanted), []) if r["lat"] is not None
                      and _fits(loc["type"], r["function"])
                      and (not state or not r["subdivision"] or r["subdivision"].upper() == state)]
        candidates = _narrow(candidates, iata)
        if len(candidates) == 1:
            hit = candidates[0]
            out.append(Proposal(loc["id"], loc["key"], "name", "{} ({})".format(hit["name"], hit["code"]),
                                hit["lat"], hit["lon"]))
        else:
            # no unique name entry (none, or several road-only ones): the
            # town's airport stands in before anything is called ambiguous
            town = [a for a in by_town.get((country, wanted), [])
                    if not state or a["region"] == "{}-{}".format(country, state)]
            pick = town_airport(town) if loc["type"] != "airport" else None
            if pick:
                out.append(Proposal(loc["id"], loc["key"], "city-airport", pick["matched"], pick["lat"], pick["lon"]))
            elif candidates:
                out.append(Proposal(loc["id"], loc["key"], "ambiguous",
                                    candidates=["{} {} {}".format(r["code"], r["name"], r["function"]) for r in candidates]))
            else:
                out.append(Proposal(loc["id"], loc["key"], "unmatched"))
    return out


def counts(proposals) -> dict:
    out = {"manual": 0, "unlocode": 0, "iata": 0, "name": 0, "city-airport": 0, "ambiguous": 0,
           "unmatched": 0, "unknown-key": 0, "retired-key": 0, "kept": 0}
    for p in proposals:
        out[p.source] += 1
    return out


def write_csv(proposals, path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(["key", "source", "matched", "lat", "lon", "candidates", "note"])
        for p in proposals:
            writer.writerow([p.key, p.source, p.matched, "" if p.lat is None else p.lat,
                             "" if p.lon is None else p.lon, " | ".join(p.candidates), p.note])
    return path


def apply(repo, proposals) -> int:
    """Write the matched coordinates onto locations whose coordinates are
    still empty - a 'manual' proposal also over a set one (that is what
    manual_wins produced). A manual row leaves a change_log line of its
    own with the note. Returns how many rows changed."""
    written = 0
    for p in proposals:
        if p.lat is None or p.lon is None or not p.location_id:
            continue
        row = repo.conn.execute("SELECT lat, lon FROM master_location WHERE id = ?", (p.location_id,)).fetchone()
        if row is None:
            continue
        if row["lat"] is not None and row["lon"] is not None and p.source != "manual":
            continue
        if repo.update("master_location", p.location_id, {"lat": p.lat, "lon": p.lon}):
            written += 1
            if p.source == "manual":
                repo._log("master_location", p.location_id, "coordinates", None,
                          "fill-coordinates manual: {}".format(p.note or "-"))
    return written
