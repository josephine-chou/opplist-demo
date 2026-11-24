"""The team site (W1a, 2026-10-27): OppList pushes its lists to the
document store the colleagues' site reads. The documents are the ones the
two halves agreed on, field by field; this is the push half, and
firebase_pull (W1b) the pull half, which every run does first.

In this edition the store is a folder of JSON documents under the data
folder (local_store): the same three calls, no service, no key, no network.
The names (firebase_*, FirebaseError, the typed values) are kept from the
hosted store the code was written against.

Every value comes from OppList's own functions - assign_list.listed and
pending_listed, tracking.ladder, rule_quoted_no_answer, case_nudge,
last_activity - never a second copy of a rule. Nothing from the mail
bodies beyond what Tracking shows, nothing from the older Read by Claude
notes, no prices.

Off by default: setting team_site_store turns the pushes on, and only the
app over its own database pushes (never a test, never a copy).

A run, in order:
  (0) W1b: firebase_pull.pull - the actions people took on the site,
      applied and answered (CX1: their notes stay on the site);
  (1) build lists/assign, lists/inquiries, one cases/{OPP} per case in
      scope, reports/{yyyy-mm} for each month report exported (MR1: and
      the default month live while it is not), and (W2a)
      lists/vendors, the Ask vendors card's list (firebase_vendors), and
      (WA1) lists/parties, the Who asked? picker's list (its `at` is the
      server's time of the write; the hash leaves it out, so the document
      is rewritten only when the parties change);
  (2) write the ones whose hash (of the JSON, keys sorted) differs from the
      last run's, in commits of at most 400 writes; a case that left the
      list is written once as {opp, gone: true}; nothing is ever deleted;
      a document over 1,000,000 bytes is not written and is reported;
  (3) meta/sync last, every run, with pushedAt the server's time and
      pulledAt the readTime of (0) once every action up to it is answered,
      and (W2a) teamCc, Settings rate_request_cc, and (W2b) lostReasons,
      Settings closed_reasons_lost (inquiry rows and cases carry
      pendingClose while a Won or Lost from the site waits, site_close),
      and (W3) quoteRules, the quote card's markup (cases/{OPP}.reply,
      the original mail, is firebase_reply's);
  (4) the hashes, the time, the counts and the error in
      ..\\data\\firebase-sync.json.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import db, firebase_pull, firebase_reply, firebase_vendors, local_store, logs, paths, site_close
from . import status as status_mod

DEFAULT_PROJECT = "demo-opplist"
#: the sync's own version, in meta/sync.version for problem reports
SYNC_VERSION = "W3"
BATCH = 400
MAX_BYTES = 1_000_000
#: when the app runs it: this long after a write, every PERIOD while it is
#: open, and FIRST_DELAY after it starts
DEBOUNCE = 30.0
PERIOD = 300.0
CHECK_EVERY = 60.0              # RQ2: the minute check for read requests
FIRST_DELAY = 5.0
#: FX3: the most documents the pushes may write in one local day (Settings,
#: "Team site: most writes a day"); a normal day is a few hundred, and
#: Firestore's free limit is 20,000
CAP_SETTING = "firebase_daily_cap"
CAP_DEFAULT = 5000
#: the 08:15 step leaves the push to the app when it pushed this recently
APP_RECENT_MINUTES = 10
#: how many days of write counts firebase-sync.json keeps
WRITES_KEPT_DAYS = 14
PAUSED_LINE = ("Team site paused: {n:,} writes today (cap {cap:,}). Look for a second OppList window; "
               "it goes again tomorrow, or raise the cap in Settings.")
QUOTA_LINE = ("Team site: Firebase's free writes for today are used up; it takes updates again after "
              "midnight Pacific (2 AM Chicago). Nothing is lost.")
#: the text lengths of INTERFACE.md
SUBJ_MAX, SUBJ_FULL_MAX, WHAT_MAX, QTY_MAX = 160, 300, 200, 40
LAST_TEXT_MAX, SUB_MAX = 60, 160
COM_MAX = 80                    # FX5: a list row's commodity
TIMELINE_MAX = 30
CLOSED = ("Won", "Lost", "Cancelled", "Dropped")
#: the notes scan_claude wrote before CX1 begin so: none of their words go
#: to the site
CLAUDE_NOTE = "Read by Claude"
#: W3: the quote card's markup rules (Settings, under Team site)
MARKUP_SETTING, MARKUP_DEFAULT = "quote_markup_pct", 10
FLOOR_SETTING, FLOOR_DEFAULT = "quote_margin_floor_pct", 5
#: W3: the quote action's mark on its event (Q3 reads it); not shown on the site
_QUOTE_TAG = re.compile(r"\s*\[quote:[^\]]*\]")
#: the Unix epoch: meta/sync.pulledAt until the first pull, so the site keeps
#: showing every action made there as not taken in yet
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


class FirebaseError(Exception):
    """Anything that stops a run: no key, the wrong project, the network,
    the API."""


# ------------------------------------------------------------- settings --
#: This edition pushes to a folder of JSON documents (local_store), never to
#: a service. Setting team_site_store 'on' turns the pushes on (off by
#: default; the demo turns it on); the folder is <data folder>/team-site.
STORE_SETTING = "team_site_store"
STORE_FOLDER = "team-site"
OFF_LINE = "the team site's local store is off (Settings, Team site)"


def store_dir(conn=None) -> Path:
    """Where the team site's documents are kept: one JSON file each."""
    return paths.DATA_DIR / STORE_FOLDER


def project(conn) -> str:
    return (db.get_setting(conn, "firebase_project", DEFAULT_PROJECT) or "").strip() or DEFAULT_PROJECT


#: Where people open the team site: setting team_site_url. The default is a
#: reserved name that resolves nowhere.
SITE_URL_SETTING, SITE_URL_DEFAULT = "team_site_url", "https://team-site.example"


def site_url(conn) -> str:
    """The team site's address without a trailing slash, for links to it."""
    return ((db.get_setting(conn, SITE_URL_SETTING, SITE_URL_DEFAULT) or "").strip() or SITE_URL_DEFAULT).rstrip("/")


def service_account(conn) -> str:
    """What the pushes write to, as Settings shows it: the store's folder;
    '' while it is off."""
    return "local store in {}".format(store_dir(conn)) if configured(conn)[0] else ""


def configured(conn) -> tuple:
    """(True, '') when a run can go, else (False, why)."""
    on = (db.get_setting(conn, STORE_SETTING, "") or "").strip().lower() in ("on", "1", "yes", "true")
    return (True, "") if on else (False, OFF_LINE)


def is_live(db_path) -> bool:
    """Only the app over the live database pushes: never a test, never a
    probe server on 8799 over a copy."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    try:
        return Path(db_path).resolve() == Path(paths.LIVE_DB_PATH).resolve()
    except OSError:
        return False


# --------------------------------------------------- Firestore's values --
class Timestamp(str):
    """A moment exactly as Firestore wrote it ('...T15:00:00.123456789Z',
    a query's readTime), sent back as a timestamp without losing a digit."""


def to_value(value) -> dict:
    """A Python value as a Firestore typed value."""
    if isinstance(value, Timestamp):
        return {"timestampValue": str(value)}
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, dt.datetime):
        moment = value if value.tzinfo else value.astimezone()
        return {"timestampValue": moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, dict):
        return {"mapValue": {"fields": to_fields(value)}}
    if isinstance(value, (list, tuple)):
        # D7c: Firestore refuses an array directly inside an array ("Cannot
        # convert an array value in an array value"); refuse it here, before
        # anything is sent, so a document that carries one is never pushed
        for v in value:
            if isinstance(v, (list, tuple)):
                raise TypeError("Firestore takes no array inside an array: {!r}".format(list(v)[:3]))
        return {"arrayValue": {"values": [to_value(v) for v in value]}}
    raise TypeError("no Firestore value for {}".format(type(value).__name__))


def to_fields(doc: dict) -> dict:
    return {str(k): to_value(v) for k, v in doc.items()}


def from_value(value: dict):
    """A Firestore typed value as a Python value (timestamps as aware UTC)."""
    if "nullValue" in value:
        return None
    if "booleanValue" in value:
        return bool(value["booleanValue"])
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "timestampValue" in value:
        text = value["timestampValue"].replace("Z", "+00:00")
        found = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", text)
        if found:                                # Python takes at most 6 decimals
            text = found.group(1) + (found.group(2) or "")[:7] + found.group(3)
        return dt.datetime.fromisoformat(text)
    if "stringValue" in value:
        return value["stringValue"]
    if "mapValue" in value:
        return from_fields(value["mapValue"].get("fields") or {})
    if "arrayValue" in value:
        return [from_value(v) for v in value["arrayValue"].get("values") or []]
    if "bytesValue" in value:
        return base64.b64decode(value["bytesValue"])
    if "referenceValue" in value:
        return value["referenceValue"]
    if "geoPointValue" in value:
        return dict(value["geoPointValue"])
    raise ValueError("unknown Firestore value {}".format(sorted(value)))


def from_fields(fields: dict) -> dict:
    return {k: from_value(v) for k, v in (fields or {}).items()}


@dataclass
class Write:
    """One document to write whole; `server_time` names the fields set to
    the server's time (updateTransforms REQUEST_TIME)."""
    path: str
    doc: dict
    server_time: tuple = ()


@dataclass
class Delete:
    """RQ2: one document deleted (only uploads/ parts, INTERFACE.md section 12)."""
    path: str


# ----------------------------------------------------------- the client --
def client_for(conn) -> object:
    """The store the pushes write to and the pulls read from: the local
    folder, with the three calls every run makes (commit, get, run_query).
    The fake in the tests has the same three."""
    ok, why = configured(conn)
    if not ok:
        raise FirebaseError(why)
    return local_store.LocalStore(project(conn), store_dir(conn), error=FirebaseError)


# ---------------------------------------------------------- the values --
#: an amount of money: a currency mark or code before or after a number
#: ('USD 2300', '$480.00', 'US$ 8.74', '5100 USD', '€ 90')
_MONEY_MARK = r"(?:USD|US\$|U\$S|EUR|RMB|CNY|HKD|GBP|MXN|CLP|BRL|R\$|NT\$|\$|€|£|¥)"
_AMOUNT = r"\d[\d,.]*(?:\s?[kK]\b)?"
_MONEY = re.compile(r"(?<![A-Za-z]){m}\s?{a}|\b{a}\s?{m}(?![A-Za-z])".format(m=_MONEY_MARK, a=_AMOUNT))
#: an address in angle brackets, '<mailto:a@b.example>' or '<a@b.example>'
_ADDRESS = re.compile(r"<(?:mailto:)?[^<>\s]+@[^<>\s]+>", re.I)
PRICE = "(price)"


def clean(text) -> str:
    """No prices on the site: every amount of money becomes '(price)';
    addresses in angle brackets out; one line."""
    text = _ADDRESS.sub("", str(text or ""))
    text = _MONEY.sub(PRICE, text)
    return " ".join(text.split())


def cut(text, limit: int) -> str:
    """One line of text, cleaned, at most `limit` characters, '…' when cut."""
    text = clean(text)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "\u2026"


def _day(value) -> str:
    return str(value or "")[:10]


def _country(value) -> str:
    """cases/*.oc and .dc: the country as stored, '' for none or Unknown."""
    value = (value or "").strip()
    return "" if value == "Unknown" else value


def team_cc(conn) -> str:
    """meta/sync.teamCc (W2a): Settings rate_request_cc; '' = no team CC."""
    from .schema_migrations import RATE_REQUEST_CC
    return (db.get_setting(conn, "rate_request_cc", RATE_REQUEST_CC) or "").strip()


def percent(conn, key, default):
    """A Settings percentage, 0 to 100 (7.5 kept, 10 as 10); the default
    when it is missing or not a number."""
    try:
        value = float(str(db.get_setting(conn, key, default)).replace("%", "").strip())
    except (TypeError, ValueError):
        return default
    if not 0 <= value <= 100:
        return default
    return int(value) if value == int(value) else value


def quote_rules(conn) -> dict:
    """meta/sync.quoteRules (W3): the quote card's markup on a vendor's
    rate and the margin it warns under; a requester that is a JoJo
    Office gets no markup (Rate Card's rule of 23 Oct)."""
    return {"markupPct": percent(conn, MARKUP_SETTING, MARKUP_DEFAULT),
            "marginFloorPct": percent(conn, FLOOR_SETTING, FLOOR_DEFAULT), "officesNoMarkup": True}


#: FX4: the folder of the Make Outlook drafts helper, as she types it
HELPER_SETTING = "helper_folder"


def helper_folder(conn) -> str:
    """meta/sync.helperFolder (FX4): where the Make Outlook drafts helper
    lives, '' when not set. Only in meta/sync, never in a site file (the
    site's files are public)."""
    return str(db.get_setting(conn, HELPER_SETTING, "") or "").strip()


def _scope_sql(alias="e") -> str:
    """The Inquiries scope: In, not registered in error, not a duplicate;
    legacy cases included (INTERFACE.md, lists/inquiries)."""
    return ("{a}.scope = 'In' AND COALESCE({a}.closed_reason, '') <> 'Registered in error' "
            "AND COALESCE({a}.closed_reason, '') NOT LIKE 'Duplicate%'").format(a=alias)


def _by_of(source, logged_by="") -> str:
    """Who logged the asking note: a colleague on the team site (W1b), the
    mail scan, or her own."""
    if source == "web":
        return logged_by or "team site"
    return "Iris (Outlook)" if source in ("mail", "scan") else "Iris"


def assign_doc(conn, today, search=None, by_site=None) -> tuple:
    """lists/assign, both tabs in the sheet's order, and the pend map
    {opp: {on, what, by}} the inquiries rows carry."""
    from . import assign_list
    by_site = firebase_pull.site_by(conn) if by_site is None else by_site
    waiting = [{"opp": r.opp_no, "req": r.requester, "subj": cut(r.subject, SUBJ_MAX), "rcv": r.received_on,
                "owner": r.owner, "hold": r.on_hold} for r in assign_list.listed(conn, today)]
    pending, pend = [], {}
    for r in assign_list.pending_listed(conn, today):
        source = conn.execute("SELECT source FROM status_event WHERE id = ?", (r.note_id,)).fetchone()
        p = {"on": r.asked_on, "what": cut(assign_list.what_they_asked(r.note, search), WHAT_MAX),
             "by": _by_of(source["source"] if source else "", by_site.get(r.note_id, ""))}
        pend[r.opp_no] = p
        pending.append({"opp": r.opp_no, "req": r.requester, "subj": cut(r.subject, SUBJ_MAX), "on": p["on"],
                        "what": p["what"], "owner": r.owner, "by": p["by"]})
    return {"waiting": waiting, "pending": pending}, pend


def _fu_steps(conn, today) -> dict:
    """{enquiry id: step} of R3 today - FU1, FU2 or Decide - with the
    snoozed and held cases left out, as Tracking does. Replied is pend's.
    A later FU (fu_max raised) goes with FU2, as on Tracking's cards."""
    from . import tracking
    asleep = tracking._snoozed(conn, today)
    held = tracking.held_until_later(conn, today)
    out = {}
    for item in tracking.rule_quoted_no_answer(conn, today, tracking.fu_settings(conn)):
        step = item.extra.get("step", "")
        if (item.rule, item.kind, item.ref_id) in asleep or item.enquiry_id in held or step == "Replied":
            continue
        if step.startswith("FU") and step not in ("FU1", "FU2"):
            step = "FU2"
        if step in ("FU1", "FU2", "Decide"):
            out[item.enquiry_id] = step
    return out


def tbc_ids(conn) -> set:
    """WA1: the ids of the (TBC) placeholder party (one, normally)."""
    from .tracking import PLACEHOLDER_SEARCHING
    return {r[0] for r in conn.execute("SELECT id FROM party WHERE placeholder = 1 AND lower(trim(key)) = ?",
                                       (PLACEHOLDER_SEARCHING.lower(),))}


PARTY_TYPES = ("Agent", "Office", "Direct Customer")


def parties_doc(conn) -> dict:
    """WA1: lists/parties without its `at`: every live party (not merged,
    not a placeholder) that can be a requester, ordered by key."""
    rows = conn.execute(
        "SELECT id, key, type, country FROM party WHERE merged_into IS NULL AND COALESCE(placeholder, 0) = 0 "
        "AND type IN ({})".format(",".join("?" * len(PARTY_TYPES))), PARTY_TYPES).fetchall()
    rows = sorted(rows, key=lambda r: ((r["key"] or "").strip().casefold(), r["id"]))
    return {"parties": [{"id": r["id"], "key": (r["key"] or "").strip(), "type": r["type"],
                         "country": (r["country"] or "").strip()} for r in rows]}


def inquiry_row(conn, row, today, settings, fu_steps, pend, closes=None, waits=None, tbc=None, mfn=None) -> dict:
    """One row of lists/inquiries (INTERFACE.md's table). WR1: 'wait' is
    the live wait's date, '' when the case does not wait for the requester
    (`waits` is wait.live_waits, read once by build). WA1: 'tbc' is True
    while the requester is the (TBC) placeholder, absent otherwise (`tbc`
    is tbc_ids, read once by build). SC1: 'latam' and 'legacy' the same
    way. FX4: 'mfn' True when the requester is a MFN member agent
    (db.mfn_party_ids, `mfn` read once by build), absent otherwise."""
    from . import wait as wait_mod
    if waits is None:
        found = wait_mod.live(conn, row["id"])
    else:
        found = waits.get(row["id"])
    from . import assign_list, tracking
    from .web import qty_text
    status = row["status"] or ""
    nxt = due = fu = since = ""
    if row["handled_by"]:
        # another office's case, closed or not (the site's seed reads so)
        nxt = tracking.case_nudge(conn, row, today)["text"]            # 'Handled by JJH'
    elif status in CLOSED:
        pass
    elif status == "Quoted" and tracking.no_followups(row["requester_type"]):
        nxt = "No follow-ups (direct customer)"
    elif status == "Quoted":
        ladder = tracking.ladder(conn, row, today, settings)
        nxt, due, since = ladder["next"] or "", ladder["next_due"] or "", ladder["clock"] or ""
        fu = fu_steps.get(row["id"], "")
    else:
        nxt = tracking.case_nudge(conn, row, today)["text"]
    # the Enquiries page's lane, '?' for a side not known
    origin = row["origin_loc"] or row["origin_country"] or "?"
    dest = row["dest_loc"] or row["dest_country"] or "?"
    out = {
        "opp": row["opp_no"], "rcv": row["received_on"] or "", "req": row["requester_key"] or "",
        "rtype": row["requester_type"] or "", "subj": cut(assign_list.plain_subject(row["source_subject"]), SUBJ_MAX),
        "lane": "{} - {}".format(origin, dest),
        "mode": row["service_mode"] or "", "qty": cut(qty_text(row), QTY_MAX),
        "st": status, "next": nxt, "due": _day(due), "fu": fu, "since": _day(since),
        "owner": (row["owner"] or "").strip(), "office": row["handled_by"] or "",
        "hold": assign_list.hold_text(row["hold_reason"], row["hold_until"]),
        "last": _day(row["last_activity_on"]), "lastText": cut(_last_text(conn, row["id"]), LAST_TEXT_MAX),
        "quoted": _day(row["quoted_on_any"]), "closed": _day(row["closed_on_any"]) if status in CLOSED else "",
        "pend": pend.get(row["opp_no"]),
        # W2b: a Won or Lost from the site waiting for her, else null
        "pendingClose": (closes or {}).get(row["opp_no"]),
        "wait": found.since if found is not None else "",
        # W3: the countries cases/{OPP} carries, for the site's search
        "oc": _country(row["origin_country"]), "dc": _country(row["dest_country"]),
    }
    # FX5: what it ships, for the site's search and the Inquiries filter
    commodity = cut(row["commodity"], COM_MAX)
    if commodity:
        out["com"] = commodity
    if row["requester_party_id"] in (tbc_ids(conn) if tbc is None else tbc):
        out["tbc"] = True
    # SC1: LATAM (her choice when she made one, v_enquiry.latam) and a
    # legacy row (st stays Dropped), each absent otherwise
    if row["latam"] == "Y":
        out["latam"] = True
    if row["legacy"]:
        out["legacy"] = True
    if row["requester_party_id"] in (db.mfn_party_ids(conn) if mfn is None else mfn):
        out["mfn"] = True
    return out


def _last_text(conn, enquiry_id) -> str:
    """tracking.last_activity's line, without the words of a Read with
    Claude note ('Note Oct 25')."""
    from . import tracking
    text = tracking.last_activity(conn, enquiry_id)["text"]
    found = re.match(r"^(Note [^:]*): {}".format(re.escape(CLAUDE_NOTE)), text)
    return found.group(1) if found else text


def _number(value) -> str:
    """7000 -> '7000', 3.25 -> '3.25' (no thousands mark, as written)."""
    return "{:g}".format(value) if value is not None else ""


def _cargo(conn, row) -> list:
    from . import cargo, cargo_card
    out = []
    for n, line in enumerate(cargo.lines_of(conn, row["id"]), start=1):
        if line.get("container_qty"):
            what = "{} x {}".format(line["container_qty"], line.get("container_type") or "?")
        else:
            what = cargo_card.what_of(line)
        value = line.get("weight_value")
        weight = "{} {}".format(_number(value), line.get("weight_unit") or "kg") if value is not None else ""
        # the volume stated or typed, else what the dims come to
        volume = line.get("cbm")
        if volume is None:
            volume = cargo.derive(line).get("volume_cbm")
            volume = round(volume, 3) if volume is not None else None
        # D7c: the size as the Cargo card shows it when the three numbers are
        # there (a size changed on the site shows the new one), else the words
        size = cargo.written(line)["size"] if all(line.get(k) is not None for k in ("dim_l", "dim_w", "dim_h")) \
            else line.get("dims_text")
        item = {"n": n, "what": cut(what, 80), "commodity": cut(line.get("commodity"), 80), "weight": weight,
                "cbm": "{} cbm".format(_number(volume)) if volume is not None else "",
                "dims": cut(size, 80)}
        # D7a (v50): the HS codes as stored and the value of the goods, each absent when empty
        if line.get("hs_code"):
            item["hs"] = line["hs_code"]
        if line.get("cargo_value") is not None:
            item["value"] = cargo.value_text(line["cargo_value"], line.get("cargo_value_cur"))
        out.append(item)
    return out


def _routes(conn, row) -> list:
    if not db._has_table(conn, "enquiry_route"):
        return []
    return [{"n": r["seq"], "from": r["origin_loc"] or r["origin_country"] or "",
             "to": r["dest_loc"] or r["dest_country"] or "", "mode": r["service_mode"] or "",
             "incoterm": r["incoterm"] or "", "scope": r["service_scope"] or "", "result": r["result"] or ""}
            for r in conn.execute("SELECT * FROM enquiry_route WHERE enquiry_id = ? AND voided = 0 ORDER BY seq",
                                  (row["id"],))]


def _rate_requests(conn, row) -> list:
    """cases/{OPP}.rr[]. Q2b: parts (what it was asked for, [] = not said)
    and routes (the route numbers, enquiry_route.seq, of the live routes it
    covers; [] = every route)."""
    from . import parts as parts_mod, routes as routes_mod
    seq_of = {r["id"]: r["seq"] for r in conn.execute(
        "SELECT id, seq FROM enquiry_route WHERE enquiry_id = ? AND voided = 0", (row["id"],))} \
        if db._has_table(conn, "enquiry_route") else {}
    out = []
    for r in conn.execute(
            "SELECT p.key AS vendor_key, v.asked_on, v.replied_on, v.quoted, v.parts, v.route_ids "
            "FROM vendor_request v JOIN party p ON p.id = v.vendor_party_id "
            "WHERE v.enquiry_id = ? AND v.voided = 0 ORDER BY v.asked_on, v.id", (row["id"],)):
        ids = routes_mod.parse_ids(r["route_ids"]) or []
        out.append({"vendor": r["vendor_key"] or "", "asked": _day(r["asked_on"]), "replied": _day(r["replied_on"]),
                    "quoted": bool(r["quoted"]), "parts": parts_mod.split(r["parts"]),
                    "routes": sorted(seq_of[i] for i in ids if i in seq_of)})
    return out


def _event(e, logged_by="") -> dict | None:
    """One timeline line: what happened, and its words under it; None for
    a Read with Claude note. A mail's words only as Tracking shows them
    (its gist); a status a mail set shows its reason, never the mail. An
    event a team-site action wrote says who in its sub (W1b)."""
    from . import detect_followups, parsing, tracking
    note = _QUOTE_TAG.sub("", parsing.hide_mid(e["note"] or "")).strip()
    from_mail = e["source"] in ("mail", "scan")
    kind, text = e["kind"], e["status"] or ""
    sub = e["reason"] or ("" if from_mail else note)
    if kind == "followup":
        text, sub = "FU{} sent".format(e["seq"] or ""), e["channel"] or ""
    elif kind == "ack":
        text, sub = "Acknowledged", ""
    elif kind == "requote":
        text = "Re-quoted"
    elif kind == "note":
        reply = detect_followups.split_note(note)
        if note.startswith(CLAUDE_NOTE):
            return None
        if reply is not None:
            # the reply's words as Tracking shows them (its gist)
            text, sub = "Reply from {}".format(reply[0] or "?"), tracking.activity_gist(reply[2], tracking.GIST_LEN)
        elif note.startswith(detect_followups.ANSWER_PREFIX):
            # our own answer: who answered, not the words of our mail
            text, sub = note.split(":", 1)[0].strip(), ""
        elif from_mail:
            text, sub = "Note", tracking.activity_gist(note, tracking.GIST_LEN)
        else:
            text, sub = "Note", note
    if logged_by:
        # Q2c: people read it on the site - "logged by", not "on the team site"
        tail = "logged by {}".format(logged_by)
        sub = cut(sub, SUB_MAX - len(tail) - 3) + " \u00b7 " + tail if clean(sub) else tail
    return {"at": _day(e["at"]), "text": cut(text, 80), "sub": cut(sub, SUB_MAX), "src": e["source"] or ""}


def _timeline(conn, row, by_site=None) -> list:
    from . import site_edit, tracking
    by_site = by_site or {}
    out = []
    for e in conn.execute(
            "SELECT * FROM status_event s WHERE enquiry_id = ? AND voided = 0 AND NOT {} "
            "ORDER BY at DESC, id DESC".format(tracking.system_event_sql("s")), (row["id"],)):
        line = _event(e, by_site.get(e["id"], ""))
        if line is not None:
            out.append(line)
            if len(out) == TIMELINE_MAX:
                break
    # D7b: each change made on the site, read from change_log
    edits = [{"at": i["at"], "text": cut(site_edit.headline(i, on_site=True), 80), "sub": cut(i["text"], SUB_MAX),
              "src": "web"}
             for i in site_edit.timeline(conn, row["id"])]
    if edits:
        out = sorted(out + edits, key=lambda l: l["at"], reverse=True)[:TIMELINE_MAX]
    return out


def case_doc(conn, row, inquiry: dict, by_site=None, today=None, settings=None, reply=None) -> dict:
    """cases/{OPP}: the row plus the case page's parts. WR1: 'wait'
    ({since, what, next}) only while the case waits for the requester. W3:
    'reply' (the original mail, firebase_reply) only on an open case."""
    from . import wait
    out = dict(inquiry)
    out.pop("wait", None)
    out.pop("com", None)            # FX5: a list row's; the case carries 'commodity'
    found = wait.doc(conn, row["id"], today, settings) if inquiry.get("wait") else None
    if found is not None:
        out["wait"] = found
    out.update({
        "subjFull": cut(row["source_subject"], SUBJ_FULL_MAX), "incoterm": row["incoterm"] or "",
        "commodity": cut(row["commodity"], 120), "direction": row["direction"] or "",
        "closedReason": row["closed_reason"] or "",
        # W2a: the countries, for the Ask vendors card's far ends
        "oc": _country(row["origin_country"]), "dc": _country(row["dest_country"]),
        "cargo": _cargo(conn, row), "routes": _routes(conn, row), "rr": _rate_requests(conn, row),
        "tl": _timeline(conn, row, by_site),
    })
    if reply is not None:
        out["reply"] = reply
    # Q2c: the vendorRates struck out in OppList (section 9)
    if db._has_table(conn, "vendor_rate"):
        struck = [r[0] for r in conn.execute("SELECT site_id FROM vendor_rate WHERE enquiry_id = ? AND voided = 1 "
                                             "ORDER BY id", (row["id"],))]
        if struck:
            out["rx"] = struck
    # D7b: what the edit forms need, on a case the site may change only
    from . import site_edit
    ed = site_edit.ed_of(conn, row)
    if ed is not None:
        out["ed"] = ed
    return out


@dataclass
class Built:
    assign: dict
    inquiries: list
    cases: dict                     # {opp: doc}


def build(conn, today=None, search=None, replies=None) -> Built:
    """Every document but meta/sync and the reports, from the database.
    W3: `replies` is the push's firebase_reply.Replies (one over no cache
    and `search` without it)."""
    from . import tracking
    today = tracking._today(today)
    settings = tracking.fu_settings(conn)
    by_site = firebase_pull.site_by(conn)
    assign, pend = assign_doc(conn, today, search, by_site)
    steps = _fu_steps(conn, today)
    closes = site_close.pending_closes(conn)
    rows = conn.execute(
        "SELECT e.*, {} AS last_activity_on FROM v_enquiry e WHERE {} "
        "ORDER BY e.received_on DESC, CAST(substr(e.opp_no, 5) AS INTEGER) DESC".format(
            tracking.activity_on_sql("e"), _scope_sql("e"))).fetchall()
    inquiries, cases = [], {}
    from . import wait
    waits = wait.live_waits(conn)
    tbc = tbc_ids(conn)
    mfn = db.mfn_party_ids(conn)
    replies = replies if replies is not None else firebase_reply.Replies(conn, search, {}, today)
    for row in rows:
        item = inquiry_row(conn, row, today, settings, steps, pend, closes, waits, tbc, mfn)
        inquiries.append(item)
        cases[row["opp_no"]] = case_doc(conn, row, item, by_site, today, settings, replies.of(row))
    replies.prune()
    return Built(assign, inquiries, cases)


# ------------------------------------------------------- month reports --
def exports_path() -> Path:
    return paths.DATA_DIR / "report-exports.json"


def _load_json(path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_json(data: dict, path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def record_export(month: str, title: str, path, when=None, record=None) -> None:
    """monthly_export.write_html: the month it wrote, its title, the file
    and when - what the next run pushes as reports/{month}."""
    record = Path(record or exports_path())
    data = _load_json(record)
    data.setdefault("months", {})[month] = {
        "title": title, "path": str(Path(path).resolve()),
        "exportedAt": (when or dt.datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")}
    _save_json(data, record)


def _seed_exports(today, reports_dir=None, record=None) -> None:
    """Once: the reports already in the folder, by title - the latest month
    up to today whose monthly.title_for is the file's name."""
    from . import monthly
    record = Path(record or exports_path())
    data = _load_json(record)
    if data.get("seeded"):
        return
    months = data.setdefault("months", {})
    folder = Path(reports_dir or paths.REPORTS_DIR)
    day = dt.date.fromisoformat(today)
    for back in range(12):
        year, mon = day.year, day.month - back
        while mon < 1:
            year, mon = year - 1, mon + 12
        month = "{:04d}-{:02d}".format(year, mon)
        file = folder / "{}.html".format(monthly.file_stem(month))
        if month in months or not file.exists() or any(
                v.get("title") == monthly.title_for(month) for v in months.values()):
            continue
        months[month] = {"title": monthly.title_for(month), "path": str(file.resolve()),
                         "exportedAt": dt.datetime.fromtimestamp(file.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%S")}
    data["seeded"] = True
    _save_json(data, record)


def _stamp(now=None) -> str:
    return (now or dt.datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")


def _export_old_live(conn, today, state, reports_dir=None, record=None) -> str:
    """MR1, the turn of the month: the month last pushed live, when it is no
    longer the default month and was never exported, is exported as HTML
    the way Export writes it (the file in the reports folder, recorded), so
    it stays on the site as it was. Its month, or ''."""
    from . import monthly, monthly_export
    live = state.get("live_report") or {}
    month = live.get("month") or ""
    if not month or month == monthly.default_month(dt.date.fromisoformat(today)):
        return ""
    state.pop("live_report", None)
    if month in _load_json(record or exports_path()).get("months", {}):
        return ""                                     # she exported it herself
    path = monthly_export.write_html(monthly.build(conn, month, dt.date.fromisoformat(today)),
                                     reports_dir, record=record)
    logs.get("firebase").info("team site: %s was live and never exported; exported it as %s", month, path)
    return month


def _live_doc(conn, today, state, now=None) -> dict:
    """MR1: the default month (8765's report page opens on it), built as
    8765's report and Export build it. updatedAt is when its HTML last
    changed: the state keeps the HTML's digest and that time, so an
    unchanged report is the same document and is not written again."""
    from . import monthly, monthly_export
    day = dt.date.fromisoformat(today)
    month = monthly.default_month(day)
    report = monthly.build(conn, month, day)
    html = monthly_export.render_html(report)
    html_digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    live = state.get("live_report") or {}
    if live.get("month") != month or live.get("digest") != html_digest or not live.get("updatedAt"):
        live = {"month": month, "digest": html_digest, "updatedAt": _stamp(now)}
        state["live_report"] = live
    return {"month": month, "title": report.title, "html": html, "exportedAt": "", "live": True,
            "updatedAt": live["updatedAt"]}


def report_docs(today, reports_dir=None, record=None, conn=None, state=None, now=None) -> dict:
    """{month: {month, title, html, exportedAt}} of every month recorded
    whose file is still there. MR1: given the connection, the default month
    too when it has no export recorded, live ({..., live: true, updatedAt});
    and at the turn of the month the old live month is exported first."""
    from . import monthly
    _seed_exports(today, reports_dir, record)
    state = state if state is not None else {}
    if conn is not None:
        _export_old_live(conn, today, state, reports_dir, record)
    recorded = _load_json(record or exports_path()).get("months", {})
    out = {}
    for month, item in sorted(recorded.items()):
        try:
            html = Path(item["path"]).read_text(encoding="utf-8")
        except (OSError, KeyError):
            continue
        out[month] = {"month": month, "title": item.get("title") or "", "html": html,
                      "exportedAt": item.get("exportedAt") or ""}
    if conn is not None:
        month = monthly.default_month(dt.date.fromisoformat(today))
        if month in recorded:
            state.pop("live_report", None)            # exported: the file in its place
        else:
            out[month] = _live_doc(conn, today, state, now)
    return dict(sorted(out.items()))


# --------------------------------------------------------------- state --
def state_path() -> Path:
    return paths.DATA_DIR / "firebase-sync.json"


def load_state(path=None) -> dict:
    return _load_json(path or state_path())


def save_state(state: dict, path=None) -> None:
    _save_json(state, path or state_path())


def digest(doc: dict) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def size_of(doc: dict) -> int:
    return len(json.dumps(doc, ensure_ascii=False).encode("utf-8"))


# ---------------------------------------------------------------- sync --
@dataclass
class SyncResult:
    ok: bool = False
    written: int = 0
    unchanged: int = 0
    gone: int = 0
    commits: int = 0
    counts: dict = field(default_factory=dict)
    too_big: list = field(default_factory=list)       # ['lists/inquiries (1,234,567 bytes)']
    reports: list = field(default_factory=list)       # the months pushed
    reason: str = ""
    pull: object = None                                # W1b: firebase_pull.PullResult
    vendors: int = 0                                   # W2a: lists/vendors rows
    parties: int = 0                                   # WA1: lists/parties rows
    vendors_to: int = 0                                # ... with a To address
    vendors_note: str = ""                             # why the addresses are the contacts' only
    paused: bool = False                               # FX3: not sent, the day's cap
    replies: object = None                             # W3: firebase_reply.Replies
    ratecard: object = None                            # Q2c: ratecard_sync.PushResult

    def vendors_line(self) -> str:
        return "vendors: {} listed, {} with an address{}".format(
            self.vendors, self.vendors_to, "; " + self.vendors_note if self.vendors_note else "")

    def lines(self) -> list:
        c = self.counts
        out = [self.pull.line()] if self.pull is not None else []
        out += ["team site: {} documents written ({} unchanged, {} gone), {} commit{}; inquiries {}, waiting {}, "
               "pending {}, cases {}".format(self.written, self.unchanged, self.gone, self.commits,
                                             "" if self.commits == 1 else "s", c.get("inquiries", 0),
                                             c.get("waiting", 0), c.get("pending", 0), c.get("cases", 0))]
        out.append("  " + self.vendors_line())
        out.append("  parties: {} listed (Who asked?)".format(self.parties))
        out += ["  not written, over 1,000,000 bytes: " + x for x in self.too_big]
        if self.reports:
            out.append("  reports: " + ", ".join(self.reports))
        if self.paused:
            out.append("  " + self.reason)
        elif not self.ok:
            out.append("  failed: " + self.reason)
        if self.ratecard is not None:
            out.append(self.ratecard.line())
        return out


# ----------------------------------------------------------- FX3 guard --
def daily_cap(conn) -> int:
    """Setting firebase_daily_cap, a whole number above 0, else CAP_DEFAULT."""
    try:
        value = int(str(db.get_setting(conn, CAP_SETTING, CAP_DEFAULT)).replace(",", "").strip())
    except (TypeError, ValueError):
        return CAP_DEFAULT
    return value if value > 0 else CAP_DEFAULT


def writes_on(state: dict, day: str) -> int:
    """The documents OppList wrote to the team site on a local date."""
    try:
        return int((state.get("writes") or {}).get(day) or 0)
    except (TypeError, ValueError):
        return 0


def _count_writes(state: dict, day: str, n: int) -> None:
    if not n:
        return
    writes = state.setdefault("writes", {})
    writes[day] = writes_on(state, day) + n
    try:
        oldest = (dt.date.fromisoformat(day) - dt.timedelta(days=WRITES_KEPT_DAYS)).isoformat()
    except ValueError:
        return
    for old in [d for d in writes if d < oldest]:
        del writes[old]


def guard_line(state=None, today=None) -> str:
    """FX3: the one line every page's banner adds while the team site is
    paused by the cap today, or after Firebase answered 429; '' otherwise."""
    state = load_state() if state is None else state
    today = today or dt.date.today().isoformat()
    if not state.get("last_ok") and " 429" in (state.get("last_error") or ""):
        return QUOTA_LINE
    paused = state.get("paused") or {}
    if paused.get("day") == today:
        return PAUSED_LINE.format(n=int(paused.get("count") or 0), cap=int(paused.get("cap") or CAP_DEFAULT))
    return ""


def app_pushed_within(minutes: float = APP_RECENT_MINUTES, state=None, now=None) -> str:
    """FX3: the time of the app's last push when it was within `minutes`,
    else ''."""
    state = load_state() if state is None else state
    last = state.get("last_app_push") or ""
    try:
        when = dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""
    now = now or dt.datetime.now()
    return last if dt.timedelta(0) <= now - when <= dt.timedelta(minutes=minutes) else ""


def _archive_search():
    from . import assign_list
    return assign_list.archive_search()


def sync_once(conn, client, state: dict, today=None, reports_dir=None, record=None, now=None,
              by_app=False) -> SyncResult:
    """One run: pull (W1b), build, write what changed, meta/sync last.
    `state` is updated as each commit lands, so a failure halfway keeps
    what was written. RQ2: `by_app`, the app's own cycle, stamps
    meta/sync.readQuote.appAt with the server time."""
    from . import assign_list, tracking
    today = tracking._today(today)
    result = SyncResult()
    result.pull = firebase_pull.pull(conn, client, state, today)
    _count_writes(state, today, result.pull.answered + result.pull.deleted)
    # Q2d: a Read quote draft of a closed case, or 14 days old, is expired
    from . import read_quote
    read_quote.expire(db.Repository(conn, source="readquote"), today)
    search = _archive_search()
    try:
        # W3: the original mails, searched once per case and kept in the state
        result.replies = firebase_reply.Replies(conn, search, state.setdefault(firebase_reply.STATE_KEY, {}), today)
        built = build(conn, today, search, result.replies)
    finally:
        if search is not None:
            search.__exit__(None, None, None)
    # W2a: the vendors and their addresses, the archive read once, read only
    vendors, result.vendors_note = firebase_vendors.rows(conn)
    result.vendors, result.vendors_to = len(vendors), sum(1 for v in vendors if v["to"])
    hashes = state.setdefault("hashes", {})
    # WA1: the hash is of the parties alone; the write adds `at` (server time)
    parties = parties_doc(conn)
    result.parties = len(parties["parties"])
    from . import site_edit
    docs = {"lists/assign": built.assign, "lists/inquiries": {"rows": built.inquiries},
            "lists/vendors": {"rows": vendors}, "lists/parties": parties,
            # D7b: the places the edit forms offer
            "lists/places": site_edit.places_doc(conn)}
    for opp, doc in built.cases.items():
        docs["cases/" + opp] = doc
    # Q2d: the Read quote drafts (INTERFACE.md section 11), each rewritten only when it changes
    docs.update(read_quote.draft_docs(conn))
    for path in list(hashes):
        if path.startswith("cases/") and path not in docs:
            docs[path] = {"opp": path[len("cases/"):], "gone": True}
    reports = report_docs(today, reports_dir, record, conn=conn, state=state, now=now)
    for month, doc in reports.items():
        docs["reports/" + month] = doc
    writes = []
    for path, doc in docs.items():
        h = digest(doc)
        if hashes.get(path) == h:
            result.unchanged += 1
            continue
        size = size_of(doc)
        if size > MAX_BYTES:
            result.too_big.append("{} ({:,} bytes)".format(path, size))
            continue
        writes.append((path, doc, h))
    # FX3: a push that would take the day past the cap is not sent (two
    # OppList windows pushing over each other used up Firestore's free day)
    cap, used = daily_cap(conn), writes_on(state, today)
    if used + len(writes) + 1 > cap:
        state["paused"] = {"day": today, "count": used, "cap": cap, "wanted": len(writes) + 1}
        result.paused = True
        result.reason = "paused: {:,} writes today, this push wants {:,} more (cap {:,})".format(used, len(writes) + 1, cap)
        result.counts = {"inquiries": len(built.inquiries), "waiting": len(built.assign["waiting"]),
                         "pending": len(built.assign["pending"]), "cases": len(built.cases)}
        return result
    for start in range(0, len(writes), BATCH):
        chunk = writes[start:start + BATCH]
        client.commit([Write(path, doc, server_time=("at",) if path == "lists/parties" else ())
                       for path, doc, _h in chunk])
        result.commits += 1
        _count_writes(state, today, len(chunk))
        for path, doc, h in chunk:
            hashes[path] = h
            result.written += 1
            if doc.get("gone") is True and path.startswith("cases/"):
                result.gone += 1
    pushed = [m for m in reports if hashes.get("reports/" + m) == digest(reports[m])]
    result.reports = [m + (" (live)" if reports[m].get("live") else "") for m in pushed]
    result.counts = {"inquiries": len(built.inquiries), "waiting": len(built.assign["waiting"]),
                     "pending": len(built.assign["pending"]), "cases": len(built.cases)}
    meta = {
        # every action up to it is taken in and answered (firebase_pull)
        "pulledAt": Timestamp(result.pull.pulled_at) if result.pull.pulled_at else EPOCH,
        "owners": assign_list.owners(conn),
        # MR1: the live month with live and updatedAt; the exported ones as before
        "reports": [dict({"month": m, "title": reports[m]["title"], "exportedAt": reports[m]["exportedAt"]},
                         **({"live": True, "updatedAt": reports[m]["updatedAt"]} if reports[m].get("live") else {}))
                    for m in pushed],
        "version": "schema {} / sync {}".format(db.get_setting(conn, "schema_version", "?"), SYNC_VERSION),
        "counts": result.counts,
        "teamCc": team_cc(conn),
        # W2b: the site offers Won or Lost once this is there
        "lostReasons": status_mod.closed_reasons(conn, "Lost"),
        # W3: the quote card's markup
        "quoteRules": quote_rules(conn),
        # FX4: the What is Make Outlook drafts? box
        "helperFolder": helper_folder(conn),
        # D7b: the lists the edit forms offer
        "editLists": site_edit.edit_lists(conn),
        # WR2: the site offers Waiting for requester and Info received once this is there
        "waitReq": True,
        # Q2e: a vendor's options kept, a draft saved option by option (INTERFACE.md section 13)
        "options": True,
    }
    # RQ2: Read quote from the site, absent while it is off
    from . import site_read
    read_meta, stamp = site_read.meta(conn, client, state, by_app)
    if read_meta is not None:
        meta["readQuote"] = read_meta
    client.commit([Write("meta/sync", meta, server_time=("pushedAt",) + ((stamp,) if stamp else ()))])
    result.commits += 1
    result.written += 1
    _count_writes(state, today, 1)
    state.pop("paused", None)
    # the push went through; a pull that stopped is this run's error
    result.ok = not result.pull.error
    result.reason = "pull stopped at " + result.pull.error if result.pull.error else ""
    return result


def sync(db_path=None, client=None, state_file=None, today=None, reports_dir=None, record=None,
         now=None, by_app=False, ratecard_client=None, ratecard_state=None) -> SyncResult:
    """A whole run with its own connection, the state file and the result
    noted for Settings. Without a client given, only over the live
    database. Never raises. `by_app`: the app's own run (FX3: the time of
    its last good push is kept for the 08:15 step)."""
    result = SyncResult()
    conn = None
    state_file = state_file or state_path()
    state = load_state(state_file)
    try:
        db_path = db_path or paths.DB_PATH
        conn = db.open_db(db_path)
        if client is None:
            if not is_live(db_path):
                # a copy (a probe on 8799, a test) leaves the live state file alone
                result.reason = "not the live database ({}); only the live app pushes".format(db_path)
                return result
            client = client_for(conn)
        name = getattr(client, "project", None) or project(conn)
        if state.get("project") != name:
            # another project: everything again (FX3: the day's writes kept)
            state = {"project": name, "writes": state.get("writes") or {}}
        result = sync_once(conn, client, state, today=today, reports_dir=reports_dir, record=record, now=now,
                           by_app=by_app)
        logs.get("firebase").info("team site pushed: %s", "; ".join(result.lines()))
    except Exception as exc:                          # a run never blocks anything
        result.ok = False
        result.reason = str(exc) if isinstance(exc, FirebaseError) else "{}: {}".format(type(exc).__name__, exc)
        logs.get("firebase").warning("team site push failed: %s", result.reason)
    finally:
        if conn is not None:
            conn.close()
    state.update({
        "last_run": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "last_ok": result.ok,
        "last_error": "" if result.ok else result.reason, "last_written": result.written,
        "last_counts": result.counts, "last_too_big": result.too_big,
        "last_pull": result.pull.line() if result.pull is not None else "",
        "last_vendors": result.vendors_line() if result.ok or result.vendors else ""})
    if result.ok:
        state["last_success"] = state["last_run"]
        if by_app:
            state["last_app_push"] = state["last_run"]
    if result.paused:
        logs.get("firebase").warning("team site paused: %s", result.reason)
    try:
        save_state(state, state_file)
    except OSError:
        pass
    # Q2c: the vendors' rates to the Rate Card, after meta/sync - only when
    # the run is live (or a client is handed in, the tests); its failure is
    # its own, and the team site's never stops it
    result.ratecard = _ratecard(db_path or paths.DB_PATH, ratecard_client, ratecard_state)
    return result


def _ratecard(db_path, client=None, state_file=None):
    from . import ratecard_sync
    if client is None and not is_live(db_path):
        return None
    conn = None
    try:
        conn = db.open_db(db_path)
        return ratecard_sync.run(conn, client=client, db_path=db_path, state_file=state_file)
    except Exception as exc:                          # never blocks anything
        logs.get("firebase").warning("rate card push failed: %s", exc)
        return ratecard_sync.PushResult(error="{}: {}".format(type(exc).__name__, exc))
    finally:
        if conn is not None:
            conn.close()


def status(state_file=None) -> dict:
    """For Settings: the last run's time, documents written, counts, the
    error; {} before any run."""
    state = load_state(state_file)
    if not state.get("last_run"):
        return {}
    return {"at": state["last_run"], "ok": bool(state.get("last_ok")), "written": state.get("last_written", 0),
            "counts": state.get("last_counts") or {}, "error": state.get("last_error") or "",
            "too_big": state.get("last_too_big") or [], "success": state.get("last_success") or "",
            "pull": state.get("last_pull") or "", "vendors": state.get("last_vendors") or ""}


class Syncer:
    """The app's runs, on background threads: about DEBOUNCE seconds after
    a write (one pending at a time), every PERIOD while the app is open and
    once FIRST_DELAY seconds after it starts. Only over the live database
    with a key in place; never in the tests. No page waits for it."""

    def __init__(self, db_path, delay: float = DEBOUNCE, period: float = PERIOD):
        self.db_path = str(db_path)
        self.delay = delay
        self.period = period
        self._timer = None
        self._lock = threading.Lock()
        self._running = threading.Lock()
        self._stop = threading.Event()
        self._loop = None
        self.last = None
        self._again = False             # RQ2: a run asked for while one was going
        self._reading = threading.Lock()
        self._check_client = None
        self._seen = ""                 # the newest action time the minute check has seen

    def enabled(self) -> bool:
        if not is_live(self.db_path):
            return False
        try:
            conn = db.open_db(self.db_path)
        except Exception:
            return False
        try:
            return configured(conn)[0]
        finally:
            conn.close()

    def schedule(self, delay=None) -> None:
        """A run `delay` seconds from now, unless one is pending already."""
        if not self.enabled():
            return
        with self._lock:
            if self._timer is not None:
                return
            self._timer = threading.Timer(self.delay if delay is None else delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
        self.run()

    def run(self) -> None:
        """One run now on this thread; a run already going is not doubled
        (RQ2: it runs once more after it, so a reading's drafts never wait
        for the next five minutes)."""
        if not self._running.acquire(blocking=False):
            self._again = True
            return
        try:
            while True:
                self._again = False
                self.last = sync(self.db_path, by_app=True)
                if getattr(self.last, "ok", False):
                    # OP1: the app's own push is the morning step's fix
                    from . import scheduled
                    scheduled.record_manual("firebase-sync", "done")
                if not self._again:
                    break
        except Exception:                             # never take the server down
            logs.get("firebase").exception("team site push")
        finally:
            self._running.release()
        self.read_waiting()

    # ------------------------------------------------ RQ2: the site's reads --
    def _reads_on(self) -> bool:
        from . import read_quote
        conn = db.open_db(self.db_path)
        try:
            return read_quote.is_on(conn) and read_quote.key_state(conn)[0] and configured(conn)[0]
        finally:
            conn.close()

    def check_reads(self) -> bool:
        """The minute check: one runQuery for actions after the last pull
        (or the newest this check has seen); a read action not taken in yet
        starts a run at once. Nothing is written when there is none."""
        if not is_live(self.db_path) or not self._reads_on():
            return False
        from . import site_read
        conn = db.open_db(self.db_path)
        try:
            if self._check_client is None:
                self._check_client = client_for(conn)
            after = max(load_state().get("pulled_at") or firebase_pull.EPOCH_TEXT, self._seen)
            wanted, self._seen = site_read.look(conn, self._check_client, after)
        finally:
            conn.close()
        if wanted:
            self.run()
        else:
            self.read_waiting()
        return wanted

    def read_waiting(self) -> None:
        """The queued requests read on a thread of their own, oldest first,
        one at a time; a run after each pushes its drafts and its answer."""
        if not is_live(self.db_path) or not self._reading.acquire(blocking=False):
            return

        def reader():
            from . import site_read
            try:
                while not self._stop.is_set() and self._reads_on():
                    if site_read.read_next(self.db_path) is None:
                        break
                    self.run()
            except Exception:                         # never take the server down
                logs.get("readquote").exception("reading the site's requests")
            finally:
                self._reading.release()

        threading.Thread(target=reader, name="read-quote-site", daemon=True).start()

    def start(self, first_delay: float = FIRST_DELAY) -> None:
        """The start run and the PERIOD loop (cmd_serve)."""
        if self._loop is not None or not is_live(self.db_path):
            return

        def loop():
            wait = first_delay
            while not self._stop.wait(wait):
                if self.enabled():
                    self.run()
                wait = self.period

        self._loop = threading.Thread(target=loop, name="firebase-sync", daemon=True)
        self._loop.start()

        def minute():
            # RQ2: new read actions looked for every minute while the app runs
            while not self._stop.wait(CHECK_EVERY):
                try:
                    self.check_reads()
                except Exception:                     # never take the server down
                    logs.get("firebase").exception("the minute check for read requests")
                    self._check_client = None

        threading.Thread(target=minute, name="read-quote-check", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
