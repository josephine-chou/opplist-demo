"""Read quote (batch Q2d, INTERFACE.md section 11): a vendor's
reply read with the Anthropic API on her computer, turned into drafts of
that vendor's rates for the team site's rates drawer.

Iris presses Read quote on a Rate requests row on 8765 (or runs
read-quote-trial). OppList takes the mail she picks from Outlook, read only
(its text, the quoted thread under it, its PDFs and pictures as files, the
text of its sheets and documents), or a message she pastes, and sends it
with what OppList knows about the shipment to the Messages API through her
own key (requests, no SDK). The answer is checked here; OppList's rules
(never the model's) make one draft per route read, with warnings. The
drafts go to the site as rateDrafts/{id} in the sync run that follows;
nothing is a version of the vendor's rates until someone saves a draft in
the drawer. Every reading is kept in quote_read (the mail's text, what the
API returned, the tokens and the cost); what the person changed on saving
is kept on the draft, for the weekly report. Nothing here is ever deleted.

The key is read from its file when a call is made and goes only in the
request's x-api-key header: never printed, logged, stored, shown on a page
or put in an environment variable.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import branding, db, logs, paths

# ---------------------------------------------------------------- settings --
ON_SETTING = "read_quote_on"
RECORDED_SETTING = "read_quote_recorded"
KEY_SETTING = "read_quote_key"
MODEL_SETTING = "read_quote_model"
CAP_SETTING = "read_quote_cap_usd"
ERROR_SETTING = "read_quote_last_error"
CAP_DEFAULT = 20.0
#: the current Sonnet on Anthropic's models page (16 Nov 2026) and the Haiku
#: Settings also offers; never switched on our own
MODELS = ("claude-sonnet-5-5", "claude-haiku-5-5")
DEFAULT_MODEL = MODELS[0]
#: Read quote's prompt (READ_QUOTE_PROMPT); a change to it is a new number
READ_QUOTE_PROMPT = 4           # RQ1c (16 Nov): -1 and 0 for none, no union types
READER = "Iris"            # only she reads (16 Nov)

# -------------------------------------------------------------- the prices --
#: USD per million tokens, from https://platform.claude.com/docs/en/about-claude/pricing
#: as it stood on PRICES_TAKEN. Haiku 5.5 costs more for a prompt over
#: 100,000 tokens (input, cache writes and cache reads counted together).
PRICES_TAKEN = "2026-11-16"
PRICES = {
    "claude-sonnet-5-5": {"input": 2.00, "write_5m": 2.50, "write_1h": 4.00, "read": 0.10, "output": 10.00},
    "claude-haiku-5-5": {"input": 0.10, "write_5m": 0.125, "write_1h": 0.20, "read": 0.01, "output": 0.50,
                         "over": 100_000,
                         "long": {"input": 0.50, "write_5m": 0.625, "write_1h": 1.00, "read": 0.05, "output": 2.50}},
}

# ------------------------------------------------------------- the call --
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
MAX_TOKENS = 16_000
TIMEOUT = 120
RETRY_AFTER = 5
RETRY_STATUSES = (429, 500, 529)
#: tests and the 8799 probe hand a fake transport in here; None = the real one
TRANSPORT = None
SLEEP = time.sleep

# ------------------------------------------------------------- the limits --
TEXT_MAX = 60_000               # the mail's text and the files' text together
PASTE_MAX = 20_000
PDF_MAX = 5
PDF_PAGES = 30
IMAGE_MAX = 5
IMAGE_BYTES = 5 * 1024 * 1024   # the API's limit for one picture
OPTIONS_MAX = 3
LINES_READ_MAX = 60             # an option, in the answer
LINES_MAX = 40                  # a draft's option (the drawer's)
WARN_MAX = 8
WARN_LEN = 200
NOTE_MAX = 300
QUOTE_MAX = 200
CHARGE_MAX = 200
SHORT_MAX = 60
LABEL_MAX = 60
FILES_MAX = 10                  # file names on a draft
EXPIRE_DAYS = 14
OPEN = ("New", "Quoted")

SEGS = ("Freight", "Inland", "Origin", "Destination", "Customs", "Other")
UNITS = ("20'", "40'", "40'HQ", "45'HQ", "CNTR", "KG", "CBM", "W/M", "SHIPMENT", "BL", "HBL", "HAWB", "AWB",
         "ENTRY", "FILING", "TRIP", "PICK UP", "PALLET", "DAY")
WORDS = ("AT COST", "INCLUDED", "IF APPLICABLE", "N/A")
SAID = ("incl", "excl", "none", "notsaid")
LEGS = ("pickup", "delivery", "none")
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
TEXT_KINDS = (".xlsx", ".xlsm", ".xls", ".csv", ".docx", ".txt")
#: a currency as vendors write it -> its three letters (the value is never changed)
#: RQ1: "$" alone is USD (her decision of 16 Nov; a mail naming its currency
#: is read as that one by the prompt); R$, MX$, C$ and the others stay theirs
CURRENCY_MARKS = {"US$": "USD", "U$S": "USD", "USD$": "USD", "USD $": "USD", "US $": "USD", "US DOLLARS": "USD",
                  "$": "USD", "R$": "BRL", "€": "EUR", "EURO": "EUR", "EUROS": "EUR", "MX$": "MXN", "C$": "CAD",
                  "CA$": "CAD", "RMB": "CNY"}
#: RQ1: a carrier the vendor has not named yet
NOT_A_CARRIER = ("pending", "tba", "tbc", "tbd", "to be confirmed", "to be advised")
#: RQ1: a price the vendor calls approximate (beside the reading's approx)
APPROX = re.compile(r"(?<![A-Za-z])(approx\.?|approximately|aprox\.?|aproximad[oa]s?|estimated|est\.|around)(?![A-Za-z])|~",
                    re.I)

# -------------------------------------------------------------- sentences --
KEY_REFUSED = ("The API key was refused: make a new key in the Console's OppList workspace and save it again "
               "in {path}.")
NO_CREDITS = "The Anthropic account has no credits left: add credits in the Console, then read again."
LIMIT_REACHED = ("The OppList workspace's spend limit in the Console is reached: raise it there, or wait for "
                 "next month.")
MODEL_GONE = "The model {model} is no longer offered: pick another in Settings (Read quote)."
BUSY = "Anthropic is busy right now: try again in a few minutes."
TIMED_OUT = "Anthropic did not answer within 120 seconds: try again."
NO_CONNECTION = "No connection to Anthropic: check the internet connection, then read again."
REFUSAL = "Claude declined to read this mail{}: type the rates in the drawer."
CUT_OFF = "The answer was cut off at 16,000 tokens: read again, or paste only the part with the prices."
NOT_JSON = "The answer was not the rate form's JSON: read again."
NOT_LIVE = "Read quote calls the API only from the app over the live database (8765)."
OUTLOOK_CLOSED = "Outlook is not open: open it, then press Read again."


class ReadQuoteError(Exception):
    """A reading that cannot go; the message is for her."""


# ===================================================================== key
def key_path(conn=None) -> Path:
    """Setting read_quote_key, else ..\\data\\secrets\\anthropic-key.txt; a
    path inside the repo is refused."""
    configured = (db.get_setting(conn, KEY_SETTING, "") or "").strip().strip('"') if conn is not None else ""
    path = Path(configured) if configured else paths.DATA_DIR / "secrets" / "anthropic-key.txt"
    # OP2: inside none of the code folders (opplist, opplist-run, opplist-serve)
    inside = paths.code_folder_of(path)
    if inside:
        raise ReadQuoteError("the key must not be inside the repo ({}); keep it in ..\\data\\secrets".format(
            inside))
    return path


def recorded(conn) -> bool:
    """This edition: the answers come from recordings (read_quote_recorded),
    so no key is needed and nothing is called. The demo turns it on."""
    return str(db.get_setting(conn, RECORDED_SETTING, "0") or "0").strip() == "1"


def key_state(conn) -> tuple:
    """(True, 'there') when the key file is there, else (False, why)."""
    if recorded(conn):
        return True, "not needed: recorded answers"
    try:
        path = key_path(conn)
    except ReadQuoteError as exc:
        return False, str(exc)
    if not path.is_file():
        return False, "no key at {}".format(path)
    return True, "there"


def _key(conn) -> str:
    """The key itself: the file's first line. Read only when a call is made."""
    path = key_path(conn)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        raise ReadQuoteError("no key at {}".format(path)) from None
    key = lines[0].strip() if lines else ""
    if not key:
        raise ReadQuoteError("the key file {} is empty".format(path))
    return key


def is_on(conn) -> bool:
    return str(db.get_setting(conn, ON_SETTING, "0") or "0").strip() == "1"


def model(conn) -> str:
    return (db.get_setting(conn, MODEL_SETTING, DEFAULT_MODEL) or "").strip() or DEFAULT_MODEL


def cap(conn) -> float:
    try:
        value = float(str(db.get_setting(conn, CAP_SETTING, CAP_DEFAULT)).replace(",", "").strip())
    except (TypeError, ValueError):
        return CAP_DEFAULT
    return value if value > 0 else CAP_DEFAULT


def _now() -> dt.datetime:
    return dt.datetime.now()


def _stamp(moment=None) -> str:
    return (moment or _now()).strftime("%Y-%m-%d %H:%M:%S")


def note_error(conn, sentence: str) -> None:
    db.set_setting(conn, ERROR_SETTING, "{}: {}".format(_now().strftime("%Y-%m-%d %H:%M"), sentence))
    conn.commit()


# ================================================================== the cost
def cost_of(model_id: str, usage: dict) -> float:
    """USD of one call from its usage (input, output, cache writes and
    reads) at the table's prices. A model not in the table: refused before
    the call, so never here."""
    table = PRICES[model_id]
    usage = usage or {}
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    write = int(usage.get("cache_creation_input_tokens") or 0)
    read = int(usage.get("cache_read_input_tokens") or 0)
    split = usage.get("cache_creation") or {}
    write_1h = int(split.get("ephemeral_1h_input_tokens") or 0) if isinstance(split, dict) else 0
    write_5m = max(write - write_1h, 0)
    if "over" in table and inp + write + read > table["over"]:
        table = table["long"]
    total = (inp * table["input"] + out * table["output"] + write_5m * table["write_5m"]
             + write_1h * table["write_1h"] + read * table["read"]) / 1_000_000
    return round(total, 6)


def month_spend(conn, month=None) -> tuple:
    """(USD, readings) this local month: every call made, the trial's too."""
    month = month or _now().strftime("%Y-%m")
    if not db._has_table(conn, "quote_read"):
        return 0.0, 0
    row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM quote_read WHERE substr(at, 1, 7) = ? "
                       "AND model IS NOT NULL", (month,)).fetchone()
    return float(row[0] or 0), int(row[1] or 0)


def under_cap(conn) -> tuple:
    spent, _n = month_spend(conn)
    limit = cap(conn)
    if spent >= limit:
        return False, ("This month's readings have cost USD {:.2f}, the cap of USD {:.2f}: Read quote waits for "
                       "next month, or raise the cap in Settings.".format(spent, limit))
    return True, ""


# ============================================================ the prompt
SYSTEM_PROMPT = """You read a freight vendor's reply to @@OFFICE@@, a freight forwarder, and copy the vendor's prices and terms for one shipment into a rate form. The shipment, the routes and what was asked are given as context: use them to know which route a price is for, never copy them into the answer.

Copy, never work out. Copy numbers, currencies and units exactly as the vendor wrote them. Never compute, convert, total, split or guess anything. When the text does not say something, leave that field empty: "" for text, -1 for a number. A number the mail does not give is -1, never 0.

Lines. One line per charge as the vendor wrote it. In "quote" put the whole line as written, the charge's name included, copied word for word. "charge" is the charge's name as written.
- "seg", the part the charge belongs to: Freight (the main carriage: ocean, air or truck freight and the surcharges on it); Inland (from the port to an inland point, IPI, rail or truck beyond the port); Origin (pickup and origin charges); Destination (destination charges and delivery); Customs (clearance, duties and taxes); Other (anything else).
- "unit": one of 20', 40', 40'HQ, 45'HQ, CNTR, KG, CBM, W/M, SHIPMENT, BL, HBL, HAWB, AWB, ENTRY, FILING, TRIP, PICK UP, PALLET, DAY when the vendor's unit is one of them (per 40HC is 40'HQ, per kilo is KG, per container of any size not named is CNTR, per shipment or per file is SHIPMENT). Otherwise "" and the unit as written in "unit_as_written". When the unit is one of the list, "unit_as_written" is "". When the vendor writes no unit at all, both are "": never supply one.
- "currency": the line's currency as written (USD, EUR, BRL, CLP, US$ ...). A price marked with "$" alone is "$", unless the mail says which currency its prices are in (pesos, M.N., MXN, CLP, COP ...): then that currency. R$, MX$, C$ and the like as written.
- "rate": the price per unit. When the vendor writes AT COST, INCLUDED, IF APPLICABLE or N/A in place of a price, put that word in "word" and "rate" is -1; otherwise "word" is "".
- A charge given as a percentage of other charges ("4% of air freight + FSC") with no amount of its own: the percentage in "percent", what it is a percentage of in "percent_of" as written, and "rate", "min", "qty" and "amount" -1. Otherwise "percent" is -1 and "percent_of" is "".
- "approx": true when the vendor calls the price approximate (approx., approximately, estimated, around, ~), else false.
- "min": the minimum, only when stated, else -1. "qty" and "amount": only when the vendor states a quantity or a total for the line, else -1; never work them out.
- Weight breaks (air: -45, +45, +100, +300, +500, +1000 kg and so on): put every break on the line in "breaks" (from_kg, to_kg, rate as written; -45 is from -1 to 45, +45 is from 45 to -1), never pick one, and "rate" is -1.
- "door_leg": "pickup" for a pickup or collection at the shipper's door, "delivery" for a delivery to the consignee's door, else "none".
- A charge due only if something happens is not a line: overweight, waiting time, detention, demurrage, storage, inspection, "if required", "subject to", anything per day or per hour beyond the free time. Put it in "conditions" as written.

Options and routes. Say which route each price is for by the route numbers given, or 0 when it does not say and you cannot tell. Several options from this vendor for one route (different carriers, routings or services) are separate options, at most three, each with a "label" of a few words ("MSC via Balboa", "Direct, weekly"). One option per route when there is only one.

A price list by place or variant (rail ramps, ports, airports, terminals, zones, truck types) gives alternatives, never charges to add up: one option per place or variant, in the vendor's order, labelled with it. A charge that differs by place goes in that place's option; a charge for every place goes in each option. At most three options: the places past the third go in the first option's "notes", each with its prices as written. Example, in invented words: "Drayage from Brookfield: Ramp North / Ramp East $410; Ramp South $520. Pre-pull: Ramp North, Ramp East $90; Ramp South $140. Doc fee $25 per shipment." is two options: "Ramp North / Ramp East" with the lines Drayage 410, Pre-pull 90 and Doc fee 25 per SHIPMENT; "Ramp South" with Drayage 520, Pre-pull 140 and Doc fee 25 per SHIPMENT.

What was said about three things, each "incl" (the rates include it: "all-in", "including FSC"), "excl" (it comes on top: "subject to", "plus", "excluding"), "none" (stated not to apply: "no minimum") or "notsaid", with in "quote" the words it came from ("" when notsaid):
- "fuel": fuel surcharge (FSC, BAF, bunker);
- "local": destination or local charges;
- "min": a minimum charge.

Terms. "valid_until": a date YYYY-MM-DD only when the vendor states a date; a day and month without a year (Dec/31, 31 Dec, 31/12) is the first such day on or after the day the mail was sent; "" when it is not clear whether the day or the month comes first (05/06). "valid_text": the validity words as written ("valid till ETD", "subject to space", "valid 15 days", "Valid: Dec/31"). "transit" as written. "carrier": the carrier's name as written, or ""; PENDING, TBA, TBC, TBD and "to be confirmed" are not names. "free_time": the free days or hours for the container or the cargo at the port, terminal or warehouse, as written; time to load or to wait at the door is not free time: put it in "notes". "quoted_origin" and "quoted_dest": the place and country the vendor says it quotes from and to, as written, with the words in "quote"; all three "" when it does not say. "notes": anything else about the price worth knowing, in the vendor's words, short.

Files. "files": one entry for each file named under "Files sent with it", in that order, "name" exactly as given there; "used" true when any price in your answer came from that file; otherwise false, with "why" in a few words ("Air rates; the route is LCL", "a booking form", "the same prices as the mail"). "why" is "" when "used" is true. "files" is [] when no file was sent.

No prices. When the reply gives no prices at all (we will revert, checking with the carrier, a question back), "priced" is false, "routes" is empty and "why_not" says why in a few words. Otherwise "priced" is true and "why_not" is "".""".replace("@@OFFICE@@", branding.HOME_OFFICE_NAME)


def _enum(values) -> dict:
    return {"type": "string", "enum": list(values)}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def answer_schema() -> dict:
    """Point 4: every field required, additionalProperties false everywhere.
    RQ1c: no union type anywhere (anyOf or a type array makes the grammar
    grow fast and the API refused RQ1b's as too large): a number the mail
    does not give is -1, a route not named 0, made None by nulls()."""
    text = {"type": "string"}
    number = {"type": "number"}
    said_item = _obj({"value": _enum(SAID), "quote": text})
    place = _obj({"place": text, "country": text, "quote": text})
    line = _obj({
        "seg": _enum(SEGS), "charge": text, "unit": _enum(UNITS + ("",)), "unit_as_written": text,
        "currency": text, "rate": number, "word": _enum(WORDS + ("",)), "min": number, "qty": number,
        "amount": number,
        "breaks": {"type": "array", "items": _obj({"from_kg": number, "to_kg": number, "rate": number})},
        "door_leg": _enum(LEGS), "quote": text,
        "percent": number, "percent_of": text, "approx": {"type": "boolean"}})
    option = _obj({
        "label": text, "currency": text, "lines": {"type": "array", "items": line},
        "valid_until": text, "valid_text": text, "transit": text, "carrier": text, "free_time": text,
        "said": _obj({"fuel": said_item, "local": said_item, "min": said_item}),
        "conditions": {"type": "array", "items": text},
        "quoted_origin": place, "quoted_dest": place, "notes": text})
    route = _obj({"route": {"type": "integer"}, "options": {"type": "array", "items": option}})
    used = _obj({"name": text, "used": {"type": "boolean"}, "why": text})
    return _obj({"priced": {"type": "boolean"}, "why_not": text, "routes": {"type": "array", "items": route},
                 "files": {"type": "array", "items": used}})


# ========================================================= the answer, checked
LINE_NUMBERS = ("rate", "min", "qty", "amount", "percent")
BREAK_NUMBERS = ("from_kg", "to_kg", "rate")


def nulls(answer):
    """RQ1c: the answer as the schema has it (-1 for a number not given, 0
    for a route not named) made into the answer with None, as check_answer
    and the rest take it; a qty of 0 is None too (a vendor never quotes a
    quantity of nought). Leaves anything not in that shape as it is."""
    if not isinstance(answer, dict):
        return answer
    for route in answer.get("routes") or []:
        if not isinstance(route, dict):
            continue
        if route.get("route") == 0 and not isinstance(route.get("route"), bool):
            route["route"] = None
        for opt in route.get("options") or []:
            for line in (opt.get("lines") or []) if isinstance(opt, dict) else []:
                if not isinstance(line, dict):
                    continue
                for name in LINE_NUMBERS:
                    if _is_minus_one(line.get(name)):
                        line[name] = None
                if line.get("qty") == 0 and not isinstance(line.get("qty"), bool):
                    line["qty"] = None
                for b in line.get("breaks") or []:
                    if isinstance(b, dict):
                        for name in BREAK_NUMBERS:
                            if _is_minus_one(b.get(name)):
                                b[name] = None
    return answer


def _is_minus_one(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == -1


class BadAnswer(Exception):
    pass


_LONG = 4000


def _need(cond, why):
    if not cond:
        raise BadAnswer(why)


def _check_text(value, what, limit=_LONG):
    _need(isinstance(value, str), "{} is not text".format(what))
    _need(len(value) <= limit, "{} is longer than {} characters".format(what, limit))


def _check_number(value, what):
    _need(value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)),
          "{} is not a number".format(what))


def _check_keys(value, keys, what):
    _need(isinstance(value, dict), "{} is not an object".format(what))
    _need(set(value) == set(keys), "{} has the fields {}".format(what, ", ".join(sorted(value))))


def check_answer(answer) -> dict:
    """The answer as point 4 describes it (types, the lists, the lengths, at
    most 3 options and 60 lines an option), as site_rates.check does for the
    site; raises BadAnswer."""
    _check_keys(answer, ("priced", "why_not", "routes", "files"), "the answer")
    _need(isinstance(answer["priced"], bool), "priced is not true or false")
    _need(isinstance(answer["files"], list) and len(answer["files"]) <= 20, "files is not a list of at most 20")
    for f in answer["files"]:
        _check_keys(f, ("name", "used", "why"), "a file")
        _check_text(f["name"], "a file's name")
        _need(isinstance(f["used"], bool), "a file's used is not true or false")
        _check_text(f["why"], "a file's why")
    _check_text(answer["why_not"], "why_not")
    _need(isinstance(answer["routes"], list), "routes is not a list")
    _need(len(answer["routes"]) <= 20, "more than 20 routes")
    for r_n, route in enumerate(answer["routes"], start=1):
        where = "route {}".format(r_n)
        _check_keys(route, ("route", "options"), where)
        _need(route["route"] is None or (isinstance(route["route"], int) and not isinstance(route["route"], bool)),
              "{}: the route number is not a whole number".format(where))
        _need(isinstance(route["options"], list), "{}: options is not a list".format(where))
        _need(len(route["options"]) <= OPTIONS_MAX, "{}: more than {} options".format(where, OPTIONS_MAX))
        for o_n, opt in enumerate(route["options"], start=1):
            w = "{} option {}".format(where, o_n)
            _check_keys(opt, ("label", "currency", "lines", "valid_until", "valid_text", "transit", "carrier",
                              "free_time", "said", "conditions", "quoted_origin", "quoted_dest", "notes"), w)
            for name in ("label", "currency", "valid_until", "valid_text", "transit", "carrier", "free_time",
                         "notes"):
                _check_text(opt[name], "{} {}".format(w, name))
            _need(isinstance(opt["lines"], list), "{}: lines is not a list".format(w))
            _need(len(opt["lines"]) <= LINES_READ_MAX, "{}: more than {} lines".format(w, LINES_READ_MAX))
            _check_keys(opt["said"], ("fuel", "local", "min"), w + " said")
            for name in ("fuel", "local", "min"):
                item = opt["said"][name]
                _check_keys(item, ("value", "quote"), "{} said {}".format(w, name))
                _need(item["value"] in SAID, "{} said {}: '{}' is not one of {}".format(w, name, item["value"],
                                                                                       ", ".join(SAID)))
                _check_text(item["quote"], "{} said {} quote".format(w, name))
            _need(isinstance(opt["conditions"], list) and len(opt["conditions"]) <= 40,
                  "{}: conditions is not a list of at most 40".format(w))
            for c in opt["conditions"]:
                _check_text(c, w + " condition")
            for side in ("quoted_origin", "quoted_dest"):
                _check_keys(opt[side], ("place", "country", "quote"), "{} {}".format(w, side))
                for name in ("place", "country", "quote"):
                    _check_text(opt[side][name], "{} {} {}".format(w, side, name))
            for l_n, line in enumerate(opt["lines"], start=1):
                lw = "{} line {}".format(w, l_n)
                _check_keys(line, ("seg", "charge", "unit", "unit_as_written", "currency", "rate", "word", "min",
                                   "qty", "amount", "breaks", "door_leg", "quote", "percent", "percent_of",
                                   "approx"), lw)
                _need(isinstance(line["approx"], bool), "{}: approx is not true or false".format(lw))
                _check_text(line["percent_of"], lw + " percent_of")
                _check_number(line["percent"], lw + " percent")
                _need(line["seg"] in SEGS, "{}: '{}' is not a part".format(lw, line["seg"]))
                _need(line["unit"] in UNITS + ("",), "{}: '{}' is not a unit of the list".format(lw, line["unit"]))
                _need(line["word"] in WORDS + ("",), "{}: '{}' is not a word of the list".format(lw, line["word"]))
                _need(line["door_leg"] in LEGS, "{}: '{}' is not a door leg".format(lw, line["door_leg"]))
                for name in ("charge", "unit_as_written", "currency", "quote"):
                    _check_text(line[name], "{} {}".format(lw, name))
                for name in ("rate", "min", "qty", "amount"):
                    _check_number(line[name], "{} {}".format(lw, name))
                _need(isinstance(line["breaks"], list) and len(line["breaks"]) <= 20,
                      "{}: breaks is not a list of at most 20".format(lw))
                for b in line["breaks"]:
                    _check_keys(b, ("from_kg", "to_kg", "rate"), lw + " break")
                    for name in ("from_kg", "to_kg", "rate"):
                        _check_number(b[name], "{} break {}".format(lw, name))
    return answer


# ================================================================= the API
def _requests_transport(url, headers, body, timeout):
    """(status, the answer as JSON or {'_text': ...}); raises TimeoutError
    or ConnectionError.

    The one place in the package that opens a connection, with the standard
    library only. call() reaches it only with a key read from the key file,
    which this edition does not ship; the tests and the demo hand call() a
    transport of their own (recorded answers), so it never runs there."""
    import socket
    import urllib.error
    import urllib.request
    if not (headers or {}).get("x-api-key"):
        raise ConnectionError()
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (socket.timeout, TimeoutError):
        raise TimeoutError() from None
    except (urllib.error.URLError, OSError):
        raise ConnectionError() from None
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, {"_text": text[:500]}


@dataclass
class Called:
    answer: dict | None = None
    usage: dict = field(default_factory=dict)
    raw: str = ""
    error: str = ""


def _api_error(status, body, model_id, key_file) -> str:
    error = (body or {}).get("error") or {} if isinstance(body, dict) else {}
    message = str(error.get("message") or (body or {}).get("_text") or "") if isinstance(body, dict) else ""
    kind = str(error.get("type") or "")
    low = message.lower()
    if status in (401, 403) or kind in ("authentication_error", "permission_error"):
        return KEY_REFUSED.format(path=key_file)
    if "credit balance" in low or "credits" in low and "low" in low:
        return NO_CREDITS
    if "usage limit" in low or "spend limit" in low or "spending limit" in low:
        return LIMIT_REACHED
    if status == 404 or kind == "not_found_error" or ("model" in low and any(
            w in low for w in ("not found", "not supported", "does not exist", "no longer", "deprecated"))):
        return MODEL_GONE.format(model=model_id)
    if status in RETRY_STATUSES or kind in ("overloaded_error", "rate_limit_error", "api_error"):
        return BUSY
    return "Anthropic refused the reading ({}: {}).".format(status, " ".join(message.split())[:160] or kind or "-")


def call(conn, model_id, content, transport=None) -> Called:
    """One reading: one request, one retry after 5 seconds on 429, 500, 529
    or a dropped connection, 120 seconds each. The key goes in the header
    and nowhere else."""
    transport = transport or TRANSPORT
    if transport is None and recorded(conn):
        # this edition: answered from a recording, no key read, nothing sent
        from . import read_quote_recorded
        transport, key_file, key = read_quote_recorded.transport, "recorded answers", read_quote_recorded.NO_KEY
    else:
        transport = transport or _requests_transport
        key_file = key_path(conn)
        key = _key(conn)
    headers = {"x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json"}
    body = {"model": model_id, "max_tokens": MAX_TOKENS, "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": content}],
            "output_config": {"format": {"type": "json_schema", "schema": answer_schema()}}}
    out = Called()
    tried = 0
    while True:
        try:
            status, answer = transport(API_URL, headers, body, TIMEOUT)
        except TimeoutError:
            out.error = TIMED_OUT
            return out
        except ConnectionError:
            if tried == 0:
                tried += 1
                SLEEP(RETRY_AFTER)
                continue
            out.error = NO_CONNECTION
            return out
        if status == 200:
            break
        sentence = _api_error(status, answer, model_id, key_file)
        if status in RETRY_STATUSES and tried == 0 and sentence == BUSY:
            tried += 1
            SLEEP(RETRY_AFTER)
            continue
        out.raw = json.dumps(answer, ensure_ascii=False)[:20000]
        out.error = sentence
        return out
    out.raw = json.dumps(answer, ensure_ascii=False)
    out.usage = dict((answer or {}).get("usage") or {})
    stop = (answer or {}).get("stop_reason")
    if stop == "refusal":
        details = (answer or {}).get("stop_details") or {}
        category = details.get("category") if isinstance(details, dict) else None
        out.error = REFUSAL.format(" ({})".format(category) if category else "")
        return out
    if stop == "max_tokens":
        out.error = CUT_OFF
        return out
    text = "".join(b.get("text") or "" for b in (answer or {}).get("content") or []
                   if isinstance(b, dict) and b.get("type") == "text")
    try:
        parsed = json.loads(text)
    except ValueError:
        out.error = NOT_JSON
        return out
    try:
        out.answer = check_answer(nulls(parsed))
    except BadAnswer as exc:
        out.error = "The answer did not pass OppList's check ({}): read again.".format(str(exc)[:120])
    return out


# ================================================================ the gate
def _request(conn, request_id):
    return conn.execute("SELECT v.*, p.key AS vendor_key FROM vendor_request v JOIN party p ON p.id = v.vendor_party_id "
                        "WHERE v.id = ?", (request_id,)).fetchone()


def case_takes_rates(conn, case) -> tuple:
    """(True, '') when the site's drawer takes this case's rates: New or
    Quoted, in the lists, not another office's."""
    from . import site_rates
    if case["status"] not in OPEN:
        return False, "the case is {}".format(case["status"])
    if (case["handled_by"] or "").strip():
        return False, "the case is handled by {}".format(case["handled_by"])
    if not site_rates.in_lists(conn, case):
        return False, "the case is not in the lists"
    return True, ""


def live_app(db_path) -> bool:
    from . import firebase_sync
    return TRANSPORT is not None or firebase_sync.is_live(db_path)


def gate(conn, case, request, db_path=None, assume=False) -> dict:
    """Point 2: {'show', 'enabled', 'why'} for a Rate requests row. Shown
    when read_quote_on, the key file is there and the month is under the
    cap; only the cap stopping it: shown disabled with the reason; else
    nothing. `assume`: as if on, with the key, on the live app (the
    report's look at real cases)."""
    takes, why = case_takes_rates(conn, case)
    if not takes:
        return {"show": False, "enabled": False, "why": why}
    if request is None or request["voided"]:
        return {"show": False, "enabled": False, "why": "the rate request is struck out"}
    if not assume:
        if not is_on(conn):
            return {"show": False, "enabled": False, "why": "Read quote is off in Settings"}
        has_key, why = key_state(conn)
        if not has_key:
            return {"show": False, "enabled": False, "why": why}
        if db_path is not None and not live_app(db_path):
            return {"show": False, "enabled": False, "why": "this app is not over the live database"}
    ok, why = under_cap(conn)
    if not ok:
        return {"show": True, "enabled": False, "why": why}
    return {"show": True, "enabled": True, "why": ""}


# ============================================================ the panel
def vendor_domains(conn, party_id) -> set:
    return {(r[0] or "").strip().lower() for r in conn.execute(
        "SELECT domain FROM party_domain WHERE party_id = ?", (party_id,)) if r[0]}


def mail_id(mail) -> str:
    return (getattr(mail, "message_id", "") or "").strip() or (getattr(mail, "entry_id", "") or "").strip()


def sent_of(mail) -> str:
    """'YYYY-MM-DD HH:MM', her local time."""
    return ((getattr(mail, "sent_at", "") or "") or (getattr(mail, "received_at", "") or "")
            or (getattr(mail, "received_on", "") or ""))[:16]


def from_vendor(mail, domains) -> bool:
    from . import matching
    if getattr(mail, "sender_internal", False):
        return False
    if not domains:
        return True                 # RD2: a vendor with no domain - any outside sender
    return (matching.domain_of(getattr(mail, "sender_address", "") or "") or "").lower() in domains


def panel_mails(conn, search, request) -> list:
    """The mails of the rate request's tab on the Mail card, newest first:
    [{id, entry_id, message_id, sent, sender, address, subject, files,
    vendor, picked, mail}]; the newest from the vendor picked."""
    from . import attachments as att, case_mail
    if search is None or not (request["thread_key"] or request["conversation_id"]):
        return []
    domains = vendor_domains(conn, request["vendor_party_id"])
    mails = sorted(case_mail.vendor_mails(search, request, conn), key=sent_of, reverse=True)
    out = []
    for mail in mails:
        names = [n for n in (getattr(mail, "attachment_names", []) or []) if n and not att.is_inline_image(n)]
        out.append({"id": mail_id(mail), "entry_id": mail.entry_id or "", "message_id": mail.message_id or "",
                    "sent": sent_of(mail), "sender": case_mail.short_name(mail.sender_name) or mail.sender_address,
                    "address": mail.sender_address or "", "subject": mail.subject or "", "files": names,
                    "vendor": from_vendor(mail, domains), "picked": False, "mail": mail})
    for item in out:
        if item["vendor"] and item["id"]:
            item["picked"] = True
            break
    return out


# ======================================================== what goes with it
def clean_text(text: str) -> str:
    """The mail's text with the quoted thread under it: line ends made
    plain, trailing spaces and runs of blank lines taken out, picture
    references ([cid:...]) dropped."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = re.sub(r"\[cid:[^\]]*\]", "", text)
    lines = [l.rstrip() for l in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _plain(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return "{:,}".format(value) if isinstance(value, int) else "{:g}".format(value)


def route_context(conn, case) -> list:
    """Each live route as the prompt and the rules need it."""
    from . import ratecard_sync, routes as routes_mod
    out = []
    for r in routes_mod.routes_of(conn, case["id"]):
        cargo = ratecard_sync._cargo(conn, case, r)
        pod = ratecard_sync._first_pod(conn, r["id"])
        pod_name = conn.execute("SELECT key FROM master_location WHERE id = ?", (pod,)).fetchone()[0] \
            if pod else (r.get("pod") or "")
        out.append({
            "id": r["id"], "seq": r["seq"], "letter": r["letter"], "mode": r.get("service_mode") or case["service_mode"] or "",
            "origin": r.get("origin_loc") or r.get("origin_alt") or "", "origin_country": r.get("origin_country") or "",
            "dest": r.get("dest_loc") or r.get("dest_alt") or "", "dest_country": r.get("dest_country") or "",
            "pod": pod_name or "", "incoterm": r.get("incoterm") or case["incoterm"] or "",
            "scope": r.get("service_scope") or "", "cargo": cargo,
            "chargeable": cargo.get("chargeableKg")})
    return out


def _cargo_words(c: dict) -> str:
    bits = []
    for box in c.get("containers") or []:
        bits.append("{} x {}".format(box["qty"], box["eq"]))
    for key, unit in (("pieces", "pieces"), ("kg", "kg"), ("cbm", "cbm"), ("chargeableKg", "kg chargeable")):
        if c.get(key) is not None:
            bits.append("{} {}".format(_plain(c[key]), unit))
    if c.get("commodity"):
        bits.append("commodity " + c["commodity"])
    bits.append("DG" if c.get("dg") else "not DG")
    return ", ".join(bits)


def _place_words(name, country) -> str:
    if name and country and country != "Unknown":
        return "{} ({})".format(name, country)
    return name or (country if country and country != "Unknown" else "not given")


def context_text(conn, case, request, routes) -> str:
    """What OppList knows, as context only: the case number, each live
    route, its cargo, the vendor and what was asked. Never the requester,
    our rates, our quotes or another vendor's rates."""
    from . import parts as parts_mod, routes as routes_mod
    asked = parts_mod.split(request["parts"]) if request is not None else []
    ids = routes_mod.parse_ids(request["route_ids"]) if request is not None else None
    covered = [r["seq"] for r in routes if not ids or r["id"] in ids]
    lines = ["What OppList knows about this shipment (context for reading the mail; never copy it into the answer):",
             "Case: {}".format(case["opp_no"]),
             "Vendor: {}".format(request["vendor_key"] if request is not None else ""),
             "Asked of the vendor: {}; routes {}".format(", ".join(asked) or "the parts were not noted",
                                                          ", ".join(str(s) for s in covered) or "all")]
    for r in routes:
        lines.append("Route {} ({}): {}; from {} to {}; port of discharge {}; incoterm {}; service scope {}; "
                     "cargo: {}".format(r["seq"], r["letter"], r["mode"] or "mode not given",
                                        _place_words(r["origin"], r["origin_country"]),
                                        _place_words(r["dest"], r["dest_country"]), r["pod"] or "not given",
                                        r["incoterm"] or "not given", r["scope"] or "not given",
                                        _cargo_words(r["cargo"])))
    return "\n".join(lines)


def _kind(name: str) -> str:
    ext = Path(name or "").suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in IMAGE_TYPES:
        return "image"
    if ext in TEXT_KINDS:
        return "text"
    return "other"


def _pages(data: bytes):
    """A PDF's page count, or None when it cannot be opened."""
    import io

    import pypdf
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            return None
        return len(reader.pages)
    except Exception:
        return None


@dataclass
class Gathered:
    blocks: list = field(default_factory=list)     # the files, then the text
    files: list = field(default_factory=list)      # {name, kind, size, sent, why}
    text_sent: str = ""
    record_text: str = ""


def gather(mail_text: str, header: str, files, context: str) -> Gathered:
    """Point 2's limits: PDFs (at most 5, 30 pages in all) and pictures (at
    most 5, no inline ones) as files, before the text; sheets, documents
    and text files as their text within the 60,000 characters the mail's
    text starts; anything else, or past a limit, not sent - and why."""
    out = Gathered()
    body = mail_text[:TEXT_MAX]
    left = TEXT_MAX - len(body)
    texts, record = [], []
    pdfs = pages = images = 0
    for f in files or []:
        kind = _kind(f.name)
        item = {"name": f.name, "kind": kind, "size": int(f.size or 0), "sent": False, "why": ""}
        if getattr(f, "path", ""):
            item["path"] = f.path               # RQ2: the file as kept in data/site-reads/<action id>
        text = getattr(f, "text", None)
        if text is not None and getattr(text, "read", False):
            record.append("--- {} (text, for the record) ---\n{}".format(f.name, text.text))
        if f.inline:
            item["why"] = "a picture in the mail's body or signature"
        elif kind == "other":
            item["why"] = "a {} file: not a kind Read quote sends".format(Path(f.name).suffix.lower() or "nameless")
        elif f.data is None:
            item["why"] = f.reason or ("over 10 MB" if int(f.size or 0) > 10 * 1024 * 1024 else "Outlook did not hand it over")
        elif kind == "pdf":
            n = _pages(f.data)
            if n is None:
                item["why"] = "the PDF could not be opened (encrypted or broken)"
            elif pdfs >= PDF_MAX:
                item["why"] = "past the {} PDFs a reading sends".format(PDF_MAX)
            elif pages + n > PDF_PAGES:
                item["why"] = "{} pages: past the {} pages a reading sends".format(n, PDF_PAGES)
            else:
                pdfs += 1
                pages += n
                item["sent"], item["pages"] = True, n
                out.blocks.append({"type": "document", "title": f.name, "source": {
                    "type": "base64", "media_type": "application/pdf",
                    "data": base64.standard_b64encode(f.data).decode("ascii")}})
        elif kind == "image":
            if images >= IMAGE_MAX:
                item["why"] = "past the {} pictures a reading sends".format(IMAGE_MAX)
            elif len(f.data) > IMAGE_BYTES:
                item["why"] = "over 5 MB, the API's limit for a picture"
            else:
                images += 1
                item["sent"] = True
                out.blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": IMAGE_TYPES[Path(f.name).suffix.lower()],
                    "data": base64.standard_b64encode(f.data).decode("ascii")}})
        else:
            if text is None or not getattr(text, "read", False):
                item["why"] = getattr(text, "reason", "") or "no text in it"
            else:
                part = "\n\n=== Attachment: {} ===\n{}".format(f.name, text.text)
                if left <= 200:
                    item["why"] = "past the 60,000 characters a reading sends"
                else:
                    if len(part) > left:
                        part = part[:left]
                        item["why"] = "cut at the 60,000 characters"
                    texts.append(part)
                    left -= len(part)
                    item["sent"] = True
        out.files.append(item)
    out.text_sent = body + "".join(texts)
    out.record_text = out.text_sent + ("\n\n" + "\n\n".join(record) if record else "")
    kinds = {"pdf": "a PDF", "image": "a picture", "text": "its text below"}
    names = ["{} ({})".format(f["name"], kinds.get(f["kind"], f["kind"])) for f in out.files if f["sent"]]
    files_line = "Files sent with it, in this order: {}".format("; ".join(names)) if names else "No files sent with it."
    out.blocks.append({"type": "text", "text": "{}\n\n=== The vendor's reply ===\n{}\n{}\n\n{}".format(
        context, header, files_line, out.text_sent)})
    return out


# ============================================================ the drafts
def _cut(text, limit) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def _currency(text) -> str:
    """Three letters, or '' (US$ is USD, R$ BRL, the euro sign EUR)."""
    raw = " ".join(str(text or "").split()).upper()
    if raw in CURRENCY_MARKS:
        return CURRENCY_MARKS[raw]
    return raw if re.fullmatch(r"[A-Z]{3}", raw) else ""


def legs(route) -> set:
    """RD2b's door legs: the service scope, else the incoterm."""
    scope = (route.get("scope") or "").strip().casefold()
    by_scope = {"door to door": {"pickup", "delivery"}, "door to port": {"pickup"}, "port to door": {"delivery"},
                "port to port": set()}
    if scope in by_scope:
        return by_scope[scope]
    term = (route.get("incoterm") or "").strip().upper()
    if term in ("EXW", "FCA"):
        return {"pickup"}
    if term in ("DAP", "DPU", "DDP", "DDU"):
        return {"delivery"}
    return set()


_COUNTRY_ALIASES = {"united states": "USA", "united states of america": "USA", "us": "USA", "u.s.": "USA",
                    "u.s.a.": "USA", "usa": "USA", "estados unidos": "USA", "eeuu": "USA", "ee.uu.": "USA",
                    "brasil": "Brazil", "mexico": "Mexico", "méxico": "Mexico", "perú": "Peru",
                    "uk": "United Kingdom", "great britain": "United Kingdom", "prc": "China"}


def country_names(conn) -> dict:
    out = dict(_COUNTRY_ALIASES)
    for r in conn.execute("SELECT name, iso2 FROM country"):
        if r["name"]:
            out[r["name"].casefold()] = r["name"]
        if r["iso2"]:
            out[r["iso2"].casefold()] = r["name"]
    return out


def _same_country(written, ours, names) -> bool:
    a = names.get(" ".join(written.split()).casefold(), " ".join(written.split()).casefold())
    b = names.get(" ".join(ours.split()).casefold(), " ".join(ours.split()).casefold())
    return str(a).casefold() == str(b).casefold()


def _break_label(b) -> str:
    lo, hi = b.get("from_kg"), b.get("to_kg")
    if lo is None and hi is not None:
        span = "-{} kg".format(_plain(hi))
    elif hi is None and lo is not None:
        span = "+{} kg".format(_plain(lo))
    else:
        span = "{}-{} kg".format(_plain(lo), _plain(hi))
    return "{} {}".format(span, _plain(b.get("rate")) or "-")


def _pick_break(breaks, kg):
    holding = [b for b in breaks if b.get("rate") is not None
               and (b.get("from_kg") is None or kg >= b["from_kg"]) and (b.get("to_kg") is None or kg < b["to_kg"])]
    if not holding:
        return None
    return max(holding, key=lambda b: (b.get("from_kg") is not None, b.get("from_kg") or 0))


_MONTHS = {"jan": 1, "january": 1, "ene": 1, "enero": 1, "janeiro": 1, "feb": 2, "february": 2, "febrero": 2,
           "fev": 2, "fevereiro": 2, "mar": 3, "march": 3, "marzo": 3, "marco": 3, "março": 3, "apr": 4, "april": 4,
           "abr": 4, "abril": 4, "may": 5, "mayo": 5, "mai": 5, "maio": 5, "jun": 6, "june": 6, "junio": 6,
           "junho": 6, "jul": 7, "july": 7, "julio": 7, "julho": 7, "aug": 8, "august": 8, "ago": 8, "agosto": 8,
           "sep": 9, "sept": 9, "september": 9, "set": 9, "septiembre": 9, "setembro": 9, "oct": 10, "october": 10,
           "out": 10, "octubre": 10, "outubro": 10, "nov": 11, "november": 11, "noviembre": 11, "novembro": 11,
           "dec": 12, "december": 12, "dic": 12, "diciembre": 12, "dez": 12, "dezembro": 12}
_MON = r"(?P<mon>[A-Za-zçÇ]{3,10})\.?"
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
_YEAR = r"(?:[\s,/.-]+(?:de\s+)?(?P<year>\d{4}))?"
_DATE_WORDS = (re.compile(r"(?<![A-Za-z0-9])" + _MON + r"[\s/.-]*" + _DAY + _YEAR + r"(?![0-9])"),
               re.compile(r"(?<![A-Za-z0-9])" + _DAY + r"[\s/.-]*(?:de\s+)?" + _MON + _YEAR + r"(?![A-Za-z0-9])"))
_DATE_NUMBERS = re.compile(r"(?<![\d/.-])(?P<a>\d{1,2})[/.-](?P<b>\d{1,2})(?:[/.-](?P<year>\d{4}|\d{2}))?(?![\d/.-])")


def _on_or_after(month, day, year, sent) -> str:
    """YYYY-MM-DD: with no year, the first such day on or after `sent`."""
    try:
        if year:
            year = int(year)
            return dt.date(year + 2000 if year < 100 else year, month, day).isoformat()
        start = dt.date.fromisoformat(sent[:10])
        for y in (start.year, start.year + 1, start.year + 2, start.year + 3, start.year + 4):
            try:
                found = dt.date(y, month, day)
            except ValueError:
                continue                # 29 Feb in a year without it
            if found >= start:
                return found.isoformat()
    except (ValueError, TypeError):
        return ""
    return ""


def valid_from(text, sent) -> tuple:
    """RQ1: (the valid-until day, '') from the validity words when they hold
    one date (a day and a month, the year when written; Dec/31, 31 Dec,
    31/12, 12/31), or ('', the date as written) when day and month could be
    either way round (05/06), or ('', '')."""
    text = " ".join(str(text or "").split())
    iso = re.findall(r"(?<![\d-])\d{4}-\d{2}-\d{2}(?![\d-])", text)
    if len(iso) == 1:
        try:
            return dt.date.fromisoformat(iso[0]).isoformat(), ""
        except ValueError:
            return "", ""
    found = []
    for pattern in _DATE_WORDS:
        for m in pattern.finditer(text):
            month = _MONTHS.get(m.group("mon").casefold())
            if month:
                found.append((m.start(), month, int(m.group("day")), m.group("year"), m.group(0)))
    for m in _DATE_NUMBERS.finditer(text):
        if any(s <= m.start() < s + len(w) for s, _mo, _d, _y, w in found):
            continue
        a, b = int(m.group("a")), int(m.group("b"))
        if a == b and 1 <= a <= 12:
            found.append((m.start(), a, a, m.group("year"), m.group(0)))
        elif 1 <= b <= 12 < a <= 31:
            found.append((m.start(), b, a, m.group("year"), m.group(0)))
        elif 1 <= a <= 12 < b <= 31:
            found.append((m.start(), a, b, m.group("year"), m.group(0)))
        elif 1 <= a <= 12 and 1 <= b <= 12:
            found.append((m.start(), None, None, m.group("year"), m.group(0)))
    if len(found) != 1:
        return "", ""
    _s, month, day, year, written = found[0]
    if month is None:
        return "", written
    day_found = _on_or_after(month, day, year, sent) if sent else ""
    return day_found, ""


def _percent(l) -> tuple:
    """RQ1: (the percentage, what it is of) when the line is a percentage of
    other charges - from the reading's percent and percent_of, or a rate
    whose unit as written is a percentage ('% of USD AIR FREIGHT + FSC')."""
    of = " ".join(str(l.get("percent_of") or "").split())
    if l.get("percent") is not None or of:
        return l.get("percent"), of
    written = " ".join(str(l.get("unit_as_written") or "").split())
    if written.startswith("%") and l.get("rate") is not None:
        return l["rate"], re.sub(r"^%\s*(?:of\s+)?", "", written, flags=re.I)
    return None, ""


def _containers(route) -> float:
    return sum(float(b.get("qty") or 0) for b in ((route or {}).get("cargo") or {}).get("containers") or [])


def _option(opt, route, names, sent="") -> tuple:
    """(the drawer's option, its warnings) by point 5's rules b to f and
    RQ1's (a percentage, approximate, no unit on more than one container,
    a carrier not named yet, a valid-until day without its year)."""
    warnings = []
    lines = [dict(l) for l in opt["lines"]]
    # RQ1: a percentage of other charges keeps its words and no number (RQ1b: one sentence an option)
    pcts = []
    for l in lines:
        pct, of = _percent(l)
        if pct is None and not of:
            continue
        name = l["charge"] or "a line"
        words = "{}% of {}".format(_plain(pct), of) if pct is not None and of else (
            "{}%".format(_plain(pct)) if pct is not None else of)
        l["charge"] = "{} ({} as written)".format(l["charge"], words).strip()
        l["rate"] = l["min"] = l["qty"] = l["amount"] = None
        l["breaks"], l["unit"], l["unit_as_written"], l["_pct"] = [], "", "", True
        pcts.append("{} ({})".format(name, words))
    if pcts:
        warnings.append(_cut("percentages of other charges: {}; work out the amounts or leave them out".format(
            ", ".join(pcts)), WARN_LEN))
    # RQ1: what the vendor calls approximate (RQ1b: one sentence)
    approx = [l["charge"] or "a line" for l in lines if l.get("approx") is True or APPROX.search(l.get("quote") or "")]
    if approx:
        warnings.append(_cut("the vendor says approximate: {}".format(", ".join(approx)), WARN_LEN))
    # b. the currency: the lines' when they agree; a line in another one keeps its words, no number
    curs = [_currency(l["currency"]) or _currency(opt["currency"]) for l in lines if not l["word"] and not l.get("_pct")]
    seen = [c for c in curs if c]
    cur = _currency(opt["currency"])
    if seen:
        counts = {c: seen.count(c) for c in seen}
        if len(counts) == 1:
            cur = seen[0]
        elif cur not in counts:
            cur = max(counts, key=lambda c: (counts[c], -seen.index(c)))
    if not cur and (opt["currency"] or "").strip():
        warnings.append(_cut("Currency as written: '{}'; pick it in the drawer".format(opt["currency"]), WARN_LEN))
    others = []
    notes = []
    for l in lines:
        line_cur = _currency(l["currency"]) or cur
        if not l["word"] and not l.get("_pct") and cur and line_cur and line_cur != cur:
            number = next((v for v in (l["rate"], l["amount"], l["min"]) if v is not None), None)
            l["charge"] = "{} ({} {} as written)".format(l["charge"], line_cur, _plain(number)).replace("  ", " ") \
                if number is not None else "{} ({} as written)".format(l["charge"], line_cur)
            l["rate"] = l["min"] = l["qty"] = l["amount"] = None
            l["breaks"] = []
            if line_cur not in others:
                others.append(line_cur)
    for c in others:
        warnings.append("Lines in {}: the drawer keeps one currency; type them or leave them out".format(c))
    # c. weight breaks: the break that holds the route's chargeable kg
    kg = route.get("chargeable") if route else None
    no_kg = []
    for l in lines:
        if not l["breaks"]:
            continue
        notes.append("{} breaks: {}".format(l["charge"] or "Freight", ", ".join(_break_label(b) for b in l["breaks"])))
        picked = _pick_break(l["breaks"], kg) if kg is not None else None
        if picked is not None:
            l["rate"] = picked["rate"]
        else:
            l["rate"] = None
            no_kg.append(l["charge"] or "a line")
    if no_kg:
        warnings.append(_cut("Weight breaks on {} and no chargeable weight{}: pick the rate in the drawer".format(
            ", ".join(no_kg), " on route " + route["letter"] if route else ""), WARN_LEN))
    # d. a country the vendor wrote that differs from the route's; a door leg asked and not quoted
    if route:
        for side, ours_place, ours_country, verb in (("quoted_origin", route["origin"], route["origin_country"],
                                                      "starts at"),
                                                     ("quoted_dest", route["dest"], route["dest_country"], "goes to")):
            written = opt[side]
            if written["country"].strip() and ours_country and ours_country != "Unknown" \
                    and not _same_country(written["country"], ours_country, names):
                warnings.append(_cut("The vendor wrote {}; route {} {} {}".format(
                    ", ".join(x for x in (written["place"].strip(), written["country"].strip()) if x),
                    route["letter"], verb, ", ".join(x for x in (ours_place, ours_country) if x)), WARN_LEN))
        wanted = legs(route)
        have = {l["door_leg"] for l in lines}
        if "pickup" in wanted and "pickup" not in have:
            warnings.append("Door pickup asked; no pickup charge in the mail")
        if "delivery" in wanted and "delivery" not in have:
            warnings.append("Door delivery asked; no delivery charge in the mail")
    # e. units not in the list; a line read twice; more than 40 lines
    odd = []
    for n, l in enumerate(lines, start=1):
        if l["unit"] not in UNITS:
            l["unit"] = ""
        if not l["unit"] and (l["unit_as_written"] or "").strip():
            odd.append("'{}' ({})".format(_cut(l["unit_as_written"], 30), _cut(l["charge"], 30) or "line {}".format(n)))
    if odd:
        warnings.append(_cut("Unit not in the list: {}; pick one in the drawer".format(", ".join(odd)), WARN_LEN))
    kept, twice, seen_keys = [], [], set()
    for l in lines:
        key = (" ".join((l["quote"] or "").split()).casefold(), l["amount"])
        if key[0] and key in seen_keys:
            twice.append(l["charge"] or "a line")
            continue
        seen_keys.add(key)
        kept.append(l)
    if twice:
        warnings.append(_cut("Read twice, one kept: {}".format(", ".join(twice)), WARN_LEN))
    if len(kept) > LINES_MAX:
        warnings.append("{} lines read; the drawer takes {}, so the last {} are left out".format(
            len(kept), LINES_MAX, len(kept) - LINES_MAX))
        kept = kept[:LINES_MAX]
    # RQ1: a price with no unit counts once in the drawer; on more than one container say so
    boxes = _containers(route)
    if boxes > 1:
        bare = [l["charge"] or "a line" for l in kept
                if not l["unit"] and not (l["unit_as_written"] or "").strip() and l["rate"] is not None
                and l["qty"] is None and l["amount"] is None and not l["word"]]
        if bare:
            warnings.append(_cut("no unit in the mail for {}; the drawer counts each once (per container it would be "
                                 "{} times)".format(", ".join(bare), _plain(boxes)), WARN_LEN))
    # f. the note: the conditions, the valid-until words when there is no date, the breaks, the vendor's notes
    valid = opt["valid_until"].strip()
    try:
        dt.date.fromisoformat(valid)
    except ValueError:
        # RQ1: a day and month without a year, from the reading's words or the validity text
        written = valid
        valid, unclear = valid_from(written, sent)
        if not valid and not unclear:
            valid, unclear = valid_from(opt["valid_text"], sent)
        if unclear:
            warnings.append(_cut("valid until {}: day or month first?".format(unclear), WARN_LEN))
    carrier = opt["carrier"].strip()
    if carrier.strip(" .:-").casefold() in NOT_A_CARRIER:
        carrier = ""
    parts = [c.strip() for c in opt["conditions"] if c.strip()]
    if not valid and opt["valid_text"].strip():
        parts.append("Valid: " + opt["valid_text"].strip())
    parts += notes
    if opt["notes"].strip():
        parts.append(opt["notes"].strip())
    said = {k: (opt["said"][k]["value"] if opt["said"][k]["value"] in SAID else "notsaid") for k in ("fuel", "local", "min")}
    out = {"label": _cut(opt["label"], LABEL_MAX), "cur": cur,
           "lines": [{"seg": l["seg"], "charge": _cut(l["charge"], CHARGE_MAX), "unit": l["unit"],
                      "rate": None if l["word"] else l["rate"], "min": l["min"], "qty": l["qty"],
                      "amount": None if l["word"] else l["amount"], "word": l["word"],
                      "quote": _cut(l["quote"], QUOTE_MAX),
                      "asWritten": "" if l["unit"] else _cut(l["unit_as_written"], 60)} for l in kept],
           "valid": valid, "transit": _cut(opt["transit"], SHORT_MAX), "carrier": _cut(carrier, SHORT_MAX),
           "free": _cut(opt["free_time"], SHORT_MAX), "said": said, "note": _cut("; ".join(parts), NOTE_MAX)}
    return out, warnings


def _name_key(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def file_use(answer, sent_names) -> dict:
    """RQ1b: {file name: (used, why)} for each file sent, from the answer's
    files; (None, '') for a file the answer does not name."""
    said = {_name_key(f.get("name")): f for f in (answer or {}).get("files") or [] if isinstance(f, dict)}
    out = {}
    for name in sent_names:
        f = said.get(_name_key(name))
        out[name] = (None, "") if f is None else (bool(f.get("used")), " ".join(str(f.get("why") or "").split()))
    return out


def file_warnings(answer, sent_names) -> list:
    out = []
    for name, (used, why) in file_use(answer, sent_names).items():
        if used is None:
            out.append(_cut("{}: the reading did not say whether any price came from it".format(name), WARN_LEN))
        elif not used:
            out.append(_cut("{}: no prices taken from it{}".format(name, " ({})".format(why) if why else ""), WARN_LEN))
    return out


def capped(warnings) -> list:
    """RQ1b: at most WARN_MAX; past it the last place says how many more."""
    if len(warnings) <= WARN_MAX:
        return list(warnings)
    return list(warnings[:WARN_MAX - 1]) + ["{} more warnings: check each line against the mail".format(
        len(warnings) - (WARN_MAX - 1))]


def drafts_of(answer, routes, names, sent="", sent_files=(), force=None) -> tuple:
    """Point 5 a: one draft per route read - [{'route', 'options',
    'warnings'}] and the readings' notes. A route null on a case with one
    live route is that route; a route number the case does not have is
    no route, with a warning. `sent`: the day the mail was sent (a valid
    until day without its year is the first one on or after it);
    `sent_files`: the names of the files sent, each one no price came
    from a warning on every draft (RQ1b). RQ2: `force`, a route number
    the site's request named: everything read is for that route (a route
    the vendor named that differs is a warning)."""
    if not answer["priced"]:
        return [], []
    by_seq = {r["seq"]: r for r in routes}
    grouped, order = {}, []
    for entry in answer["routes"]:
        warn = []
        number = entry["route"]
        if force is not None and force in by_seq:
            target = by_seq[force]
            if number is not None and number != force:
                warn.append("The vendor named route {}; read as route {}, as the request said".format(number, force))
        elif number is None:
            target = routes[0] if len(routes) == 1 else None
        elif number in by_seq:
            target = by_seq[number]
        else:
            target = None
            warn.append("The vendor's route {} is not a route of this case: pick the route in the drawer".format(number))
        key = target["seq"] if target else 0
        if key not in grouped:
            grouped[key] = {"route": target, "options": [], "warnings": []}
            order.append(key)
        grouped[key]["warnings"] += warn
        for opt in entry["options"]:
            grouped[key]["options"].append(opt)
    out = []
    for key in order:
        g = grouped[key]
        options, warnings = [], file_warnings(answer, sent_files) + list(g["warnings"])
        if len(g["options"]) > OPTIONS_MAX:
            warnings.append("{} options read; the first {} are here".format(len(g["options"]), OPTIONS_MAX))
        made = [_option(o, g["route"], names, sent) for o in g["options"][:OPTIONS_MAX]]
        made = [(o, w) for o, w in made if o["lines"]]
        for o, w in made:
            options.append(o)
            prefix = "{}: ".format(o["label"]) if len(made) > 1 and o["label"] else ""
            warnings += [_cut(prefix + x, WARN_LEN) for x in w]
        if not options:
            continue
        unique = []
        for w in warnings:
            if w not in unique:
                unique.append(w)
        out.append({"route": g["route"], "options": options, "warnings": capped(unique)})
    return out, []


# ============================================================ the reading
@dataclass
class Source:
    kind: str                       # mail, paste or files (RQ2: files sent from the site, no mail among them)
    text: str = ""
    subject: str = ""
    sender: str = ""
    sent: str = ""                  # 'YYYY-MM-DD HH:MM' or the paste's day
    entry_id: str = ""
    message_id: str = ""
    files: list = field(default_factory=list)       # outlook_bridge.AttachedFile
    note: str = ""                  # RQ2: the note sent with a mail file from the site


@dataclass
class Reading:
    id: int = 0
    status: str = ""
    error: str = ""
    why_not: str = ""
    cost: float = 0.0
    drafts: list = field(default_factory=list)      # quote_read_draft ids
    files: list = field(default_factory=list)
    docs: list = field(default_factory=list)        # the drafts' documents (the trial's, never pushed)


def _outlook():
    from . import outlook_bridge
    return outlook_bridge


#: tests hand a fake Outlook here (read_item, read_files)
OUTLOOK = None


def from_mail(mail, search=None) -> Source:
    """The picked mail from Outlook, read only: its text (the quoted thread
    under it included) and its attachments."""
    from . import outlook_bridge
    bridge = OUTLOOK or _outlook()
    folders = ()
    if search is not None:
        try:
            folders = tuple(search.folders_seen([getattr(mail, "message_id", "")]) + search.standard_folders())
        except Exception:
            folders = ()
    entry_id, message_id = getattr(mail, "entry_id", "") or "", getattr(mail, "message_id", "") or ""
    try:
        item = bridge.read_item(entry_id, message_id, folders)
        names = [n for n in (getattr(item, "attachment_names", []) or getattr(mail, "attachment_names", []) or []) if n]
        files = bridge.read_files(entry_id, message_id, folders) if names else []
    except outlook_bridge.OutlookError as exc:
        raise ReadQuoteError(OUTLOOK_CLOSED if exc.message == outlook_bridge.NOT_RUNNING else exc.message) from None
    return Source(kind="mail", text=clean_text(getattr(item, "body_excerpt", "") or ""),
                  subject=getattr(item, "subject", "") or getattr(mail, "subject", "") or "",
                  sender="{} <{}>".format(getattr(mail, "sender_name", "") or getattr(item, "sender_name", ""),
                                          getattr(mail, "sender_address", "") or getattr(item, "sender_address", "")),
                  sent=sent_of(mail) or sent_of(item), entry_id=getattr(item, "entry_id", "") or entry_id,
                  message_id=getattr(item, "message_id", "") or message_id, files=files)


LIST_LINE = ("This is the line from Outlook's message list, not the mail. Open the mail, click in its text, press "
             "Ctrl+A and Ctrl+C, and paste again.")


def outlook_list_line(text: str) -> bool:
    """RQ1c: a paste copied from Outlook's message list (its header line
    with From, Subject and Received among the tab-separated columns, and a
    row or two), not from the mail."""
    lines = [l for l in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n") if l.strip()]
    if not lines or len(lines) > 3:
        return False
    columns = {c.strip().casefold() for c in lines[0].split("\t")}
    return {"from", "subject", "received"} <= columns


def from_paste(text: str, day: str, today: str) -> Source:
    text = clean_text(text)
    if not text:
        raise ReadQuoteError("Paste the vendor's message first.")
    if outlook_list_line(text):
        raise ReadQuoteError(LIST_LINE)
    if len(text) > PASTE_MAX:
        raise ReadQuoteError("The message is {:,} characters, more than {:,}: paste the part with the prices.".format(
            len(text), PASTE_MAX))
    day = (day or today).strip()
    try:
        dt.date.fromisoformat(day)
    except ValueError:
        raise ReadQuoteError("'{}' is not a date (the day they sent it).".format(day)) from None
    if day > today:
        raise ReadQuoteError("{} is after today (the day they sent it).".format(day))
    return Source(kind="paste", text=text, sent=day)


def _utc(local_stamp: str) -> str:
    try:
        moment = dt.datetime.strptime(local_stamp[:19], "%Y-%m-%d %H:%M:%S").astimezone()
    except ValueError:
        return ""
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def read(conn, request_id: int, source: Source, today=None, db_path=None, transport=None, trial=False,
         repo=None, by_name=None, site_action=None, route=None) -> Reading:
    """One reading (points 2 to 5 and 7). The gate is the caller's (the
    page and the trial check it); the cap and the live app are checked here
    again. Raises ReadQuoteError before anything is sent. RQ2: `by_name`
    who asked (the site's read action's name; else Iris),
    `site_action` that action's id, `route` the route number it named."""
    from . import firebase_sync
    today = today or dt.date.today().isoformat()
    repo = repo or db.Repository(conn, source="readquote")
    request = _request(conn, request_id)
    if request is None:
        raise ReadQuoteError("That rate request is not on file.")
    case = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (request["enquiry_id"],)).fetchone()
    model_id = model(conn)
    if model_id not in PRICES:
        raise ReadQuoteError("OppList has no price for the model {}: pick one Settings offers.".format(model_id))
    # a reading answered from a recording sends nothing, so it needs no live database
    if transport is None and TRANSPORT is None and not recorded(conn)             and not firebase_sync.is_live(db_path or paths.DB_PATH):
        raise ReadQuoteError(NOT_LIVE)
    ok, why = under_cap(conn)
    if not ok:
        raise ReadQuoteError(why)
    has_key, why = key_state(conn)
    if not has_key:
        raise ReadQuoteError("Read quote needs the key: {}.".format(why))
    routes = route_context(conn, case)
    header = ("Pasted message, sent {}".format(source.sent) if source.kind == "paste" else
              "Files the vendor sent, on {}".format(source.sent) if source.kind == "files" else
              "From: {}\nSent: {}\nSubject: {}".format(source.sender, source.sent, source.subject))
    by_seq = {r["seq"]: r for r in routes}
    if route is not None and route in by_seq:
        header += "\nThe person who sent it in says it is for route {} ({}).".format(route, by_seq[route]["letter"])
    else:
        route = None
    if source.note:
        header += "\nTheir note for the reading: {}".format(source.note)
    got = gather(source.text, header, source.files, context_text(conn, case, request, routes))
    called = call(conn, model_id, got.blocks, transport=transport)
    cost = cost_of(model_id, called.usage) if called.usage else 0.0
    answer = called.answer
    status = "failed" if called.error else ("no_prices" if not answer["priced"] else "read")
    names = country_names(conn)
    drafts = []
    why_not = ""
    if answer is not None:
        sent_names = [f["name"] for f in got.files if f["sent"]]
        drafts, _ = drafts_of(answer, routes, names, (source.sent or today)[:10], sent_names, force=route)
        for f in got.files:
            if f["sent"]:
                f["used"], f["used_why"] = file_use(answer, [f["name"]])[f["name"]]
        why_not = answer["why_not"].strip()
        if answer["priced"] and not drafts:
            status, why_not = "no_prices", why_not or "no lines read"
    at = _stamp()
    reading_id = repo.insert("quote_read", {
        "enquiry_id": case["id"], "vendor_request_id": request["id"], "vendor_party_id": request["vendor_party_id"],
        "source": source.kind, "entry_id": source.entry_id or None, "message_id": source.message_id or None,
        "subject": source.subject or None, "sender": source.sender or None, "sent_at": source.sent or None,
        "files": json.dumps(got.files, ensure_ascii=False), "text_sent": got.record_text, "model": model_id,
        "prompt_version": READ_QUOTE_PROMPT, "input_tokens": called.usage.get("input_tokens"),
        "output_tokens": called.usage.get("output_tokens"),
        "cache_write_tokens": called.usage.get("cache_creation_input_tokens"),
        "cache_read_tokens": called.usage.get("cache_read_input_tokens"), "cost_usd": cost,
        "response": called.raw or None, "status": status, "error": called.error or None, "trial": 1 if trial else 0,
        "by_name": by_name or READER, "site_action": site_action, "at": at})
    out = Reading(id=reading_id, status=status, error=called.error, why_not=why_not, cost=cost, files=got.files)
    sent_files = [f["name"] for f in got.files if f["sent"]][:FILES_MAX]
    for d in drafts:
        seq = d["route"]["seq"] if d["route"] else None
        doc = {"schema": 1, "opp": case["opp_no"], "vendor": request["vendor_key"], "route": seq,
               "on": (source.sent or today)[:10], "options": d["options"], "warnings": d["warnings"],
               "source": {"kind": source.kind, "subject": _cut(source.subject, 200) if source.kind == "mail" else "",
                          "sent": source.sent, "files": sent_files},
               "readBy": by_name or READER, "readAt": _utc(at)}
        out.docs.append(dict(doc, id="rq{}-{}".format(reading_id, seq or 0)))
        if trial:
            continue
        route_id = d["route"]["id"] if d["route"] else None
        for old in conn.execute(
                "SELECT d.id FROM quote_read_draft d JOIN quote_read q ON q.id = d.quote_read_id "
                "WHERE d.status = 'open' AND q.enquiry_id = ? AND q.vendor_party_id = ? AND d.route_id IS ? "
                "AND q.id <> ?", (case["id"], request["vendor_party_id"], route_id, reading_id)).fetchall():
            repo.update("quote_read_draft", old["id"], {"status": "replaced", "closed_at": at})
        out.drafts.append(repo.insert("quote_read_draft", {
            "quote_read_id": reading_id, "site_id": "rq{}-{}".format(reading_id, seq or 0), "route_id": route_id,
            "draft": json.dumps(doc, ensure_ascii=False, sort_keys=True), "status": "open",
            "taken": json.dumps([""] * len(doc["options"]))}))
    repo.commit()
    if called.error:
        note_error(conn, called.error)
    logs.get("readquote").info("read quote %s %s: %s, %s draft%s, USD %.4f%s", case["opp_no"], request["vendor_key"],
                               status, len(drafts), "" if len(drafts) == 1 else "s", cost,
                               " (trial)" if trial else "")
    return out


# ======================================================= the site's drafts
def draft_docs(conn) -> dict:
    """{'rateDrafts/<id>': the document} for every draft (never deleted),
    with its status and savedAs."""
    from .firebase_sync import Timestamp
    out = {}
    if not db._has_table(conn, "quote_read_draft"):
        return out
    for r in conn.execute("SELECT d.*, v.site_id AS saved_as FROM quote_read_draft d "
                          "LEFT JOIN vendor_rate v ON v.id = d.vendor_rate_id ORDER BY d.id"):
        doc = json.loads(r["draft"])
        if doc.get("readAt"):
            doc["readAt"] = Timestamp(doc["readAt"])
        doc["status"] = r["status"]
        doc["savedAs"] = r["saved_as"] or ""
        doc["taken"] = taken_of(r)                    # Q2e: per option '', saved or skipped
        out["rateDrafts/" + r["site_id"]] = doc
    return out


#: Q2e: what each option of a draft can be
TAKEN = ("", "saved", "skipped")


def taken_of(row) -> list:
    """Q2e: the draft's options' states, one per option, in order ('' not
    yet, 'saved', 'skipped'); a row without them (never, after v55) reads
    as its status says."""
    try:
        n = len(json.loads(row["draft"]).get("options") or []) or 1
    except (ValueError, AttributeError, TypeError):
        n = 1
    try:
        kept = json.loads(row["taken"]) if "taken" in row.keys() and row["taken"] else None
    except ValueError:
        kept = None
    if isinstance(kept, list):
        out = [x if x in TAKEN else "" for x in kept[:n]]
        return out + [""] * (n - len(out))
    out = [""] * n
    if row["status"] == "saved":
        out[min(int(row["option"] or 0), n - 1)] = "saved"
    elif row["status"] == "discarded":
        out = ["skipped"] * n
    return out


def _close_if_taken(repo, row, taken, who) -> str:
    """Q2e: the draft's taken written; once every option is taken it is
    saved (one or more saved) or discarded (none). Returns its status."""
    values = {"taken": json.dumps(taken)}
    status = row["status"]
    if all(taken):
        status = "saved" if "saved" in taken else "discarded"
        values.update({"status": status, "closed_at": _stamp()})
        if status == "discarded" and not row["confirmed_by"]:
            values["confirmed_by"] = who
    repo.update("quote_read_draft", row["id"], values)
    return status


def expire(repo, today=None) -> int:
    """Point 6c: an open draft whose case is closed, or 14 days after its
    reading, is expired."""
    conn = repo.conn
    if not db._has_table(conn, "quote_read_draft"):
        return 0
    today = today or dt.date.today().isoformat()
    oldest = (dt.date.fromisoformat(today) - dt.timedelta(days=EXPIRE_DAYS)).isoformat()
    rows = conn.execute("SELECT d.id FROM quote_read_draft d JOIN quote_read q ON q.id = d.quote_read_id "
                        "JOIN enquiry e ON e.id = q.enquiry_id WHERE d.status = 'open' "
                        "AND (e.status NOT IN ('New', 'Quoted') OR substr(q.at, 1, 10) <= ?)", (oldest,)).fetchall()
    for r in rows:
        repo.update("quote_read_draft", r["id"], {"status": "expired", "closed_at": _stamp()})
    if rows:
        repo.commit()
    return len(rows)


def site_link(conn, opp, vendor_key, route_seq, site_id) -> str:
    from urllib.parse import quote

    from . import firebase_sync
    link = "{}/#/case?opp={}&rates={}".format(firebase_sync.site_url(conn), quote(opp),
                                              quote(vendor_key or "", safe=""))
    if route_seq:
        link += "&route={}".format(route_seq)
    return link + "&draft={}".format(quote(site_id))


def answer_of(response) -> dict | None:
    """The rate form's answer inside a reading's kept API response, or None."""
    try:
        whole = json.loads(response or "{}")
        text = "".join(b.get("text") or "" for b in whole.get("content") or [] if b.get("type") == "text")
        answer = json.loads(text) if text else None
    except (ValueError, AttributeError, TypeError):
        return None
    return nulls(answer) if isinstance(answer, dict) else None


def view(conn, reading_id) -> dict | None:
    """What the panel shows after a reading: per route and option the lines
    as read, the warnings, the cost, and the link to check and save."""
    from . import routes as routes_mod
    q = conn.execute("SELECT q.*, e.opp_no, p.key AS vendor_key FROM quote_read q JOIN enquiry e ON e.id = q.enquiry_id "
                     "JOIN party p ON p.id = q.vendor_party_id WHERE q.id = ?", (reading_id,)).fetchone()
    if q is None:
        return None
    why_not = (answer_of(q["response"]) or {}).get("why_not") or ""
    drafts = []
    for d in conn.execute("SELECT * FROM quote_read_draft WHERE quote_read_id = ? ORDER BY id", (reading_id,)):
        doc = json.loads(d["draft"])
        drafts.append({"id": d["site_id"], "status": d["status"], "route": doc.get("route"),
                       "letter": routes_mod.letter(doc["route"]) if doc.get("route") else "",
                       "options": doc["options"], "warnings": doc["warnings"],
                       "link": site_link(conn, q["opp_no"], q["vendor_key"], doc.get("route"), d["site_id"])})
    return {"id": q["id"], "status": q["status"], "error": q["error"] or "", "why_not": why_not,
            "cost": q["cost_usd"] or 0.0, "tokens": (q["input_tokens"] or 0, q["output_tokens"] or 0),
            "subject": q["subject"] or "", "sent": q["sent_at"] or "", "source": q["source"],
            "files": json.loads(q["files"] or "[]"), "drafts": drafts, "at": q["at"], "model": q["model"] or ""}


# ===================================================== a save from a draft
def resolve(conn, case, party, doc) -> tuple:
    """Point 6a, before the version is kept: (how, the draft row or None,
    first save?, a sentence for the result or ''). A draft that is not an
    open one of that case and vendor: 'web' and 'not linked'; one already
    saved from: a correction ('read', not the first save)."""
    draft_id, option, how = doc.get("draft"), doc.get("option"), doc.get("how")
    if not draft_id and how is None:
        return "web", None, False, ""
    if not isinstance(draft_id, str) or not draft_id or how != "read" or option not in (0, 1, 2) \
            or isinstance(option, bool) or not db._has_table(conn, "quote_read_draft"):
        return "web", None, False, " The reading was not linked (the draft's fields are not complete)."
    row = conn.execute("SELECT d.* FROM quote_read_draft d JOIN quote_read q ON q.id = d.quote_read_id "
                       "WHERE d.site_id = ? AND q.enquiry_id = ? AND q.vendor_party_id = ?",
                       (draft_id, case["id"], party["id"])).fetchone()
    # Q2e: per option - the first save of an option not yet saved takes it;
    # another save of a saved option is a correction
    if row is not None and row["status"] == "open":
        taken = taken_of(row)
        return "read", row, not (option < len(taken) and taken[option] == "saved"), ""
    if row is not None and row["status"] == "saved":
        return "read", row, False, ""
    return "web", None, False, " The reading {} was not linked: it is not an open draft of this case and vendor.".format(
        draft_id)


def _same(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    if isinstance(a, str) or isinstance(b, str):
        return " ".join(str(a or "").split()).casefold() == " ".join(str(b or "").split()).casefold()
    return a == b


LINE_FIELDS = ("seg", "charge", "unit", "rate", "word", "min", "qty", "amount")


def changes(option: dict, saved: dict, read_route, saved_route, read_on, saved_on) -> dict:
    """What was changed from the option on saving: each line (matched by
    position when the charge is the same, then by charge, then by
    position) added, removed or changed; and the currency, valid until,
    transit, carrier, free time, what was said, the note, the route and
    the day. A qty or an amount the reading left to the drawer (null) is
    not a change."""
    read_lines = list(option.get("lines") or [])
    saved_lines = list(saved.get("lines") or [])
    pairs, free_read, free_saved = [], set(range(len(read_lines))), set(range(len(saved_lines)))
    for i in range(min(len(read_lines), len(saved_lines))):
        if _same(read_lines[i].get("charge"), saved_lines[i].get("charge")):
            pairs.append((i, i))
            free_read.discard(i)
            free_saved.discard(i)
    for j in sorted(free_saved):
        match = next((i for i in sorted(free_read) if _same(read_lines[i].get("charge"), saved_lines[j].get("charge"))),
                     None)
        if match is not None:
            pairs.append((match, j))
            free_read.discard(match)
            free_saved.discard(j)
    for i, j in zip(sorted(free_read), sorted(free_saved)):
        pairs.append((i, j))
    free_read -= {i for i, _ in pairs}
    free_saved -= {j for _, j in pairs}
    lines = []
    for i, j in sorted(pairs, key=lambda p: p[1]):
        r, s = read_lines[i], saved_lines[j]
        diff = {}
        for name in LINE_FIELDS:
            rv, sv = r.get(name), s.get(name)
            if name in ("qty", "amount") and rv is None:
                continue
            if name == "word":
                rv, sv = rv or None, sv or None
            if not _same(rv, sv) and not (rv in (None, "") and sv in (None, "")):
                diff[name] = {"read": rv, "saved": sv}
        if diff:
            lines.append({"what": "changed", "n": j + 1, "charge": s.get("charge") or r.get("charge") or "",
                          "quote": r.get("quote") or "", "fields": diff})
    for j in sorted(free_saved):
        lines.append({"what": "added", "n": j + 1, "charge": saved_lines[j].get("charge") or "", "quote": "",
                      "fields": {name: {"read": None, "saved": saved_lines[j].get(name)} for name in LINE_FIELDS
                                 if saved_lines[j].get(name) not in (None, "")}})
    for i in sorted(free_read):
        lines.append({"what": "removed", "n": None, "charge": read_lines[i].get("charge") or "",
                      "quote": read_lines[i].get("quote") or "",
                      "fields": {name: {"read": read_lines[i].get(name), "saved": None} for name in LINE_FIELDS
                                 if read_lines[i].get(name) not in (None, "")}})
    fields = {}
    said = option.get("said") or {}
    for name, read_value, saved_value in (
            ("cur", option.get("cur"), saved.get("cur")), ("valid", option.get("valid"), saved.get("valid_until")),
            ("transit", option.get("transit"), saved.get("transit")),
            ("carrier", option.get("carrier"), saved.get("carrier")),
            ("free", option.get("free"), saved.get("free_time")),
            ("said.fuel", said.get("fuel"), saved.get("said_fuel")),
            ("said.local", said.get("local"), saved.get("said_local")),
            ("said.min", said.get("min"), saved.get("said_min")),
            ("note", option.get("note"), saved.get("note")), ("route", read_route, saved_route),
            ("on", read_on, saved_on)):
        if not _same(read_value or None, saved_value or None) and not (not read_value and not saved_value):
            fields[name] = {"read": read_value, "saved": saved_value}
    return {"lines": lines, "fields": fields}


def mark_saved(repo, row, rate_id, doc, values, route_seq, day, who, at) -> None:
    """Q2e: the option becomes saved; the draft stays open until every
    option is taken. The draft's first save keeps the version (savedAs),
    the option, who and when, and what was changed from that option."""
    draft = json.loads(row["draft"])
    option = int(doc.get("option") or 0)
    taken = taken_of(row)
    if option < len(taken):
        taken[option] = "saved"
    if row["vendor_rate_id"] is None:
        chosen = draft["options"][option] if option < len(draft["options"]) else (draft["options"] or [{}])[0]
        diff = changes(chosen, values, draft.get("route"), route_seq, draft.get("on"), day)
        repo.update("quote_read_draft", row["id"], {
            "vendor_rate_id": rate_id, "option": option,
            "changes": json.dumps(diff, ensure_ascii=False, sort_keys=True), "confirmed_by": who,
            "confirmed_at": at})
        row = repo.conn.execute("SELECT * FROM quote_read_draft WHERE id = ?", (row["id"],)).fetchone()
    _close_if_taken(repo, row, taken, who)


# ================================================================ discard
class Refused(Exception):
    pass


def take_discard(repo, case, action, who, today) -> str:
    """Point 6b: an open draft of that case is set aside."""
    conn = repo.conn
    opp = case["opp_no"]
    draft_id = action.get("draft")
    if not isinstance(draft_id, str) or not draft_id.strip() or len(draft_id) > 40:
        raise Refused("{}: the action names no reading, so nothing was set aside.".format(opp))
    row = conn.execute("SELECT d.*, q.enquiry_id, p.key AS vendor_key FROM quote_read_draft d "
                       "JOIN quote_read q ON q.id = d.quote_read_id JOIN party p ON p.id = q.vendor_party_id "
                       "WHERE d.site_id = ?", (draft_id.strip(),)).fetchone() \
        if db._has_table(conn, "quote_read_draft") else None
    if row is None or row["enquiry_id"] != case["id"]:
        raise Refused("{}: {} is not a reading of this case, so nothing was set aside.".format(opp, draft_id))
    if row["status"] in ("saved", "discarded") and action.get("option") is not None:
        raise Refused("That option was already saved or skipped.")         # Q2e: every option is taken
    if row["status"] != "open":
        state = {"saved": "already saved", "discarded": "already set aside", "replaced": "replaced by a newer reading",
                 "expired": "expired"}.get(row["status"], row["status"])
        raise Refused("{}: the reading of {}'s mail is {}, so nothing was set aside.".format(opp, row["vendor_key"], state))
    # Q2e: with option, that option skipped; without, every option not taken
    taken = taken_of(row)
    option = action.get("option")
    if option is not None:
        if isinstance(option, bool) or not isinstance(option, int) or not 0 <= option < len(taken):
            raise Refused("{}: option {} is not an option of the reading of {}'s mail, so nothing was set "
                          "aside.".format(opp, option, row["vendor_key"]))
        if taken[option]:
            raise Refused("That option was already saved or skipped.")
        taken[option] = "skipped"
    else:
        taken = [t or "skipped" for t in taken]
    status = _close_if_taken(repo, row, taken, who)
    if option is not None:
        said = "{}: option {} of the reading of {}'s mail was skipped".format(opp, option + 1, row["vendor_key"])
        return said + ("." if status == "open" else "; the reading is {}.".format(
            "done" if status == "saved" else "set aside"))
    if status == "saved":
        return "{}: the rest of the reading of {}'s mail was set aside.".format(opp, row["vendor_key"])
    return "{}: the reading of {}'s mail was set aside.".format(opp, row["vendor_key"])


# ============================================================== Settings
def status_lines(conn) -> list:
    """Settings' block and read-quote --status: on or off, the key, the
    model, the cap and this month's spend, the last error, the report."""
    spent, n = month_spend(conn)
    has_key, why = key_state(conn)
    try:
        where = str(key_path(conn))
    except ReadQuoteError as exc:
        where = str(exc)
    report = latest_report()
    return ["Read quote: {}".format("on" if is_on(conn) else "off"),
            "key: {}".format("there ({})".format(where) if has_key else why),
            "model: {}".format(model(conn)),
            "this month: USD {:.2f} of {:.2f}, {} reading{}".format(spent, cap(conn), n, "" if n == 1 else "s"),
            "last error: {}".format(db.get_setting(conn, ERROR_SETTING, "") or "none"),
            "report: {}".format(report or "not written yet")]


def info(conn, db_path=None) -> dict:
    spent, n = month_spend(conn)
    has_key, why = key_state(conn)
    try:
        where = str(key_path(conn))
    except ReadQuoteError:
        where = ""
    return {"on": is_on(conn), "recorded": recorded(conn),
            "has_key": has_key, "key_line": (why if recorded(conn) else "there") if has_key else why, "key_path": where,
            "model": model(conn), "models": MODELS, "cap": cap(conn), "spent": spent, "readings": n,
            "spend_line": "USD {:.2f} of {:.2f}, {} reading{}".format(spent, cap(conn), n, "" if n == 1 else "s"),
            "error": db.get_setting(conn, ERROR_SETTING, "") or "", "configured_key": db.get_setting(conn, KEY_SETTING, "") or "",
            "prices_taken": PRICES_TAKEN}


# ========================================================== the weekly report
def reports_dir() -> Path:
    return paths.DATA_DIR / "reports"


def latest_report() -> str:
    found = sorted(reports_dir().glob("read-quote-2*.md")) if reports_dir().exists() else []
    return str(found[-1]) if found else ""


def _fmt(value) -> str:
    if value is None or value == "":
        return "(empty)"
    if isinstance(value, float):
        return _plain(value)
    return str(value)


def report(conn, days: int = 7, today=None) -> str:
    """Point 9: the week's readings and what people changed, as Markdown."""
    today = today or dt.date.today().isoformat()
    start = (dt.date.fromisoformat(today) - dt.timedelta(days=days - 1)).isoformat()
    month = today[:7]
    reads = conn.execute("SELECT q.*, e.opp_no, p.key AS vendor_key FROM quote_read q JOIN enquiry e ON e.id = q.enquiry_id "
                         "JOIN party p ON p.id = q.vendor_party_id WHERE substr(q.at, 1, 10) BETWEEN ? AND ? "
                         "ORDER BY q.at, q.id", (start, today)).fetchall()
    ids = [r["id"] for r in reads] or [0]
    drafts = conn.execute("SELECT d.*, q.subject, q.sent_at, q.source, e.opp_no, p.key AS vendor_key FROM quote_read_draft d "
                          "JOIN quote_read q ON q.id = d.quote_read_id JOIN enquiry e ON e.id = q.enquiry_id "
                          "JOIN party p ON p.id = q.vendor_party_id WHERE d.quote_read_id IN ({}) ORDER BY d.id".format(
                              ",".join("?" for _ in ids)), ids).fetchall()
    count = lambda s: sum(1 for r in reads if r["status"] == s)       # noqa: E731
    saved = [d for d in drafts if d["status"] == "saved"]
    changed = [d for d in saved if _has_changes(d["changes"])]
    lines = ["# Read quote: {} to {}".format(start, today), "",
             "Readings {}: {} read, {} no prices, {} failed{}.".format(
                 len(reads), count("read"), count("no_prices"), count("failed"),
                 " ({} in the trial)".format(sum(1 for r in reads if r["trial"])) if any(r["trial"] for r in reads) else ""),
             "", "Drafts {}: {} saved unchanged, {} saved with changes, {} set aside, {} expired, {} replaced, {} still open.".format(
                 len(drafts), len(saved) - len(changed), len(changed), sum(1 for d in drafts if d["status"] == "discarded"),
                 sum(1 for d in drafts if d["status"] == "expired"), sum(1 for d in drafts if d["status"] == "replaced"),
                 sum(1 for d in drafts if d["status"] == "open")), ""]
    # RQ2: the readings asked for on the team site, counted apart by who asked
    site = {}
    for r in reads:
        if _site_action(r):
            site[r["by_name"] or "?"] = site.get(r["by_name"] or "?", 0) + 1
    lines += ["From the site: {}.".format(", ".join("{} {}".format(k, v) for k, v in sorted(
        site.items(), key=lambda kv: (-kv[1], kv[0].casefold()))) if site else "none"), "", "## Readings"]
    for r in reads:
        made = sum(1 for d in drafts if d["quote_read_id"] == r["id"])
        lines.append("- {} {} {}: {}{}, asked by {}{}".format(
            r["at"][:16], r["opp_no"], r["vendor_key"], r["status"].replace("_", " "),
            ", {} draft{}".format(made, "" if made == 1 else "s") if made else "", r["by_name"] or READER,
            " (team site)" if _site_action(r) else " (trial)" if r["trial"] else ""))
    if not reads:
        lines.append("None.")
    lines.append("")
    tally = {}
    for d in changed:
        diff = json.loads(d["changes"])
        for name in diff.get("fields") or {}:
            tally[name] = tally.get(name, 0) + 1
        for line in diff.get("lines") or []:
            if line["what"] in ("added", "removed"):
                tally["line " + line["what"]] = tally.get("line " + line["what"], 0) + 1
            else:
                for name in line["fields"]:
                    tally["line " + name] = tally.get("line " + name, 0) + 1
    lines.append("## The fields changed")
    lines += ["- {}: {}".format(k, v) for k, v in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))] or ["- none"]
    lines += ["", "## Per vendor"]
    vendors = {}
    for r in reads:
        vendors.setdefault(r["vendor_key"], [0, 0])[0] += 1
    for d in changed:
        vendors.setdefault(d["vendor_key"], [0, 0])[1] += 1
    lines += ["- {}: {} reading{}, {} saved with changes".format(k, v[0], "" if v[0] == 1 else "s", v[1])
              for k, v in sorted(vendors.items(), key=lambda kv: kv[0].casefold())] or ["- none"]
    lines += ["", "## Drafts saved with changes"]
    for d in changed:
        diff = json.loads(d["changes"])
        lines.append("### {} {} ({})".format(d["opp_no"], d["vendor_key"], d["site_id"]))
        lines.append("Mail: {} (sent {})".format(d["subject"] or "(a pasted message)", (d["sent_at"] or "")[:16]))
        for name, pair in sorted((diff.get("fields") or {}).items()):
            lines.append("- {}: read {} / saved {}".format(name, _fmt(pair["read"]), _fmt(pair["saved"])))
        for line in diff.get("lines") or []:
            lines.append("- line {} {}{}: {}".format(line.get("n") or "-", line["what"],
                                                     " ({})".format(line["charge"]) if line["charge"] else "",
                                                     "; ".join("{} read {} / saved {}".format(k, _fmt(v["read"]), _fmt(v["saved"]))
                                                               for k, v in line["fields"].items()) or "-"))
            if line.get("quote"):
                lines.append("  - quote: \"{}\"".format(_cut(line["quote"], 200)))
    if not changed:
        lines.append("None.")
    lines += ["", "## Failed readings"]
    failed = [r for r in reads if r["status"] == "failed"]
    lines += ["- {} {} {}: {}".format(r["at"][:16], r["opp_no"], r["vendor_key"], r["error"] or "-") for r in failed] \
        or ["None."]
    week = conn.execute("SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
                        "COALESCE(SUM(cache_write_tokens),0), COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(cost_usd),0) "
                        "FROM quote_read WHERE substr(at, 1, 10) BETWEEN ? AND ?", (start, today)).fetchone()
    spent, n = month_spend(conn, month)
    lines += ["", "## Tokens and cost",
              "- the week: {:,} input, {:,} output, {:,} cache written, {:,} cache read; USD {:.2f}".format(
                  week[0], week[1], week[2], week[3], week[4]),
              "- {}: USD {:.2f} in {} reading{} (cap USD {:.2f})".format(month, spent, n, "" if n == 1 else "s", cap(conn)),
              "", "Prices from Anthropic's pricing page on {}; prompt version {}.".format(PRICES_TAKEN, READ_QUOTE_PROMPT)]
    return "\n".join(lines) + "\n"


def _site_action(row) -> str:
    """RQ2: the team site's read action of a reading, '' for one of 8765."""
    try:
        return row["site_action"] or ""
    except (IndexError, KeyError):
        return ""


def _has_changes(text) -> bool:
    try:
        diff = json.loads(text or "{}")
    except ValueError:
        return False
    return bool(diff.get("lines") or diff.get("fields"))


def write_report(conn, days: int = 7, today=None) -> Path:
    today = today or dt.date.today().isoformat()
    path = reports_dir() / "read-quote-{}.md".format(today.replace("-", ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report(conn, days, today), encoding="utf-8")
    return path


# ================================================================ the trial
_MONEY = re.compile(r"\d.*?(?:USD|US\$|EUR|BRL|MXN|CLP|PEN|COP|ARS|CAD|CNY|[$€£¥])|"
                    r"(?:USD|US\$|EUR|BRL|MXN|CLP|PEN|COP|ARS|CAD|CNY|[$€£¥])\s?\d", re.S)
TRIAL_FILES = (".pdf", ".xls", ".xlsx")
TRIAL_MODES = ("FCL", "LCL", "Air", "Truck")


def _trial_mode(conn, request, case) -> str:
    from . import routes as routes_mod
    live = routes_mod.routes_of(conn, case["id"])
    ids = routes_mod.parse_ids(request["route_ids"])
    modes = [r.get("service_mode") or case["service_mode"] or "" for r in live if not ids or r["id"] in ids]
    mode = (modes or [case["service_mode"] or ""])[0]
    return "Truck" if mode in ("Truck", "Cross-border Truck") else mode


#: FX6: a shipping document, not a quote, by a whole word of its subject or
#: of an attachment's name (KLN's 'DRAFT : ...' with its HBL and invoice)
SHIPPING_DOC = re.compile(r"(?<![A-Za-z0-9])(DRAFT|HBL|MBL|B/L|AWB|HAWB|MAWB|INVOICE|ARRIVAL NOTICE|PRE-ALERT|"
                          r"BOOKING CONFIRMATION|ISF|PACKING LIST)(?![A-Za-z0-9])", re.I)
#: FX6: the vendor's reply is looked for from the day it was asked to this
#: many days after the day it was replied (no replied day: after asked)
AFTER_REPLIED = 3
AFTER_ASKED = 14
TRIAL_PER_VENDOR = 2
TRIAL_PER_CASE = 3


def _window(request) -> tuple:
    """(first day, last day) a rate request's quote is looked for in."""
    asked = request["asked_on"] or ""
    try:
        if request["replied_on"]:
            last = dt.date.fromisoformat(request["replied_on"][:10]) + dt.timedelta(days=AFTER_REPLIED)
        else:
            last = dt.date.fromisoformat(asked[:10]) + dt.timedelta(days=AFTER_ASKED)
    except ValueError:
        return asked[:10], "9999-12-31"
    return asked[:10], last.isoformat()


def shipping_doc(subject, files) -> str:
    """The shipping document's word in a subject or a file name, else ''."""
    for text in [subject or ""] + list(files or []):
        found = SHIPPING_DOC.search(text)
        if found:
            return found.group(1)
    return ""


def trial_picks(conn, search, limit: int = 10, since: str = "2026-10-01") -> list:
    """Point 10, FX6: the replies that quoted - per rate request replied or
    quoted since `since`, the vendor's mails in its tab sent from the day it
    was asked to three days after the day it was replied (no replied day:
    14 days after asked), with a digit and a currency or a pdf, xls or xlsx
    attached, none whose subject or file names carry a shipping document's
    word; of those the earliest, the reply that quoted. At most one a rate
    request, two a vendor and three a case; FCL, LCL, Air and a truck among
    them when there are such; the newest first otherwise."""
    if search is None:
        return []
    rows = conn.execute(
        "SELECT v.*, p.key AS vendor_key FROM vendor_request v JOIN party p ON p.id = v.vendor_party_id "
        "WHERE v.voided = 0 AND (COALESCE(v.thread_key, '') <> '' OR COALESCE(v.conversation_id, '') <> '') "
        "AND (COALESCE(v.replied_on, '') >= ? OR (v.machine_status = 'Quoted' AND COALESCE(v.replied_on, v.asked_on, '') >= ?)) "
        "ORDER BY v.id", (since, since)).fetchall()
    found = []
    for request in rows:
        case = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (request["enquiry_id"],)).fetchone()
        if case is None:
            continue
        mode = _trial_mode(conn, request, case)
        first, last = _window(request)
        replies = []
        for m in panel_mails(conn, search, request):
            day = m["sent"][:10]
            if not m["vendor"] or not m["id"] or day < since or day < first or day > last:
                continue
            if shipping_doc(m["subject"], m["files"]):
                continue
            body = getattr(m["mail"], "body_excerpt", "") or ""
            files = [n for n in m["files"] if Path(n).suffix.lower() in TRIAL_FILES]
            if not (_MONEY.search(body) or files):
                continue
            replies.append(m)
        if replies:
            earliest = min(replies, key=lambda m: m["sent"])
            found.append({"request": request, "case": case, "mode": mode, **earliest})
    found.sort(key=lambda m: m["sent"], reverse=True)
    picks, per_vendor, per_case, seen = [], {}, {}, set()

    def take(m):
        vendor, case = m["request"]["vendor_party_id"], m["case"]["id"]
        if m["id"] in seen or per_vendor.get(vendor, 0) >= TRIAL_PER_VENDOR \
                or per_case.get(case, 0) >= TRIAL_PER_CASE or len(picks) >= limit:
            return False
        picks.append(m)
        seen.add(m["id"])
        per_vendor[vendor] = per_vendor.get(vendor, 0) + 1
        per_case[case] = per_case.get(case, 0) + 1
        return True

    for mode in TRIAL_MODES:
        for m in found:
            if m["mode"] == mode and take(m):
                break
    for m in found:
        take(m)
    picks.sort(key=lambda m: m["sent"], reverse=True)
    return picks


def _file_words(f) -> str:
    if not f.get("sent"):
        return "not sent: " + (f.get("why") or "-")
    if "used" not in f:
        return "sent"
    if f["used"] is None:
        return "sent; the reading did not say"
    return "sent; prices taken" if f["used"] else "sent; no prices taken" + (": " + f["used_why"] if f.get("used_why")
                                                                            else "")


def _files_line(files) -> str:
    return "Files: " + ("; ".join("{} ({})".format(f["name"], _file_words(f)) for f in files) or "none")


def _doc_lines(doc, heading="###") -> list:
    """A draft as the trial's file shows it: the route, its warnings, each
    option and its lines."""
    out = ["", "{} Route {}".format(heading, doc["route"] if doc["route"] is not None else "not named")]
    for w in doc["warnings"]:
        out.append("- warning: {}".format(w))
    for opt in doc["options"]:
        out.append("")
        out.append("Option {}: {}; valid until {}; transit {}; carrier {}; free time {}; fuel {}, local {}, min {}".format(
            opt["label"] or "-", opt["cur"] or "no currency", opt["valid"] or "-", opt["transit"] or "-",
            opt["carrier"] or "-", opt["free"] or "-", opt["said"]["fuel"], opt["said"]["local"], opt["said"]["min"]))
        out.append("")
        out.append("| Part | Charge | Unit | Rate or word | Min | Qty | Amount | Quote |")
        out.append("|---|---|---|---|---|---|---|---|")
        for l in opt["lines"]:
            out.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
                l["seg"], l["charge"].replace("|", "/"), l["unit"] or "({})".format(l["asWritten"] or "-"),
                l["word"] or _plain(l["rate"]), _plain(l["min"]), _plain(l["qty"]), _plain(l["amount"]),
                l["quote"].replace("|", "/")))
        if opt["note"]:
            out.append("")
            out.append("Note: " + opt["note"])
    return out


def _answer_lines(answer) -> list:
    """A reading's answer as the model returned it (before OppList's rules),
    for setting two readings side by side."""
    if answer is None:
        return ["(no answer kept)"]
    if not answer.get("priced"):
        return ["No prices: {}".format(answer.get("why_not") or "-")]
    out = []
    for r in answer.get("routes") or []:
        for opt in r.get("options") or []:
            out.append("")
            out.append("Route {}, option {}: currency {}; valid until {}; valid words {}; carrier {}; free time {}".format(
                r.get("route") if r.get("route") is not None else "not named", opt.get("label") or "-",
                opt.get("currency") or "-", opt.get("valid_until") or "-", opt.get("valid_text") or "-",
                opt.get("carrier") or "-", opt.get("free_time") or "-"))
            out.append("")
            out.append("| Charge | Unit | As written | Cur | Rate or word | % of | Approx | Quote |")
            out.append("|---|---|---|---|---|---|---|---|")
            for l in opt.get("lines") or []:
                pct = "{}% of {}".format(_plain(l.get("percent")), l.get("percent_of") or "-") \
                    if l.get("percent") is not None or l.get("percent_of") else ""
                out.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
                    (l.get("charge") or "").replace("|", "/"), l.get("unit") or "", (l.get("unit_as_written") or "").replace("|", "/"),
                    l.get("currency") or "", l.get("word") or (_plain(l.get("rate")) if l.get("rate") is not None else (
                        "breaks" if l.get("breaks") else "")), pct.replace("|", "/"),
                    "yes" if l.get("approx") else "", _cut(l.get("quote") or "", 120).replace("|", "/")))
            if opt.get("notes"):
                out.append("")
                out.append("Notes: " + _cut(opt["notes"], 400))
    return out or ["(no lines)"]


def again(conn, search, reading_ids, today=None, transport=None, db_path=None) -> Path:
    """RQ1 point 10: the mails of earlier readings read again with today's
    prompt (trial 1, counted in the month's spend; nothing pushed, no
    draft; read_quote_on not needed), each beside the earlier reading."""
    from . import outlook_bridge
    today = today or dt.date.today().isoformat()
    out = ["# Read quote, read again, {}".format(today), "",
           "Readings {} read again with prompt version {}. Nothing here was pushed to the team site.".format(
               ", ".join(str(i) for i in reading_ids), READ_QUOTE_PROMPT), ""]
    totals = {"cost": 0.0, "in": 0, "out": 0, "read": 0, "no_prices": 0, "failed": 0}
    for n, old_id in enumerate(reading_ids, start=1):
        old = conn.execute("SELECT q.*, e.opp_no, p.key AS vendor_key FROM quote_read q JOIN enquiry e ON e.id = q.enquiry_id "
                           "JOIN party p ON p.id = q.vendor_party_id WHERE q.id = ?", (old_id,)).fetchone()
        if old is None:
            out += ["## {}. Reading {}".format(n, old_id), "Not on file.", ""]
            totals["failed"] += 1
            continue
        out.append("## {}. {} {}: reading {} again".format(n, old["opp_no"], old["vendor_key"], old_id))
        out.append("Mail: {} (sent {})".format(old["subject"] or "(a pasted message)", old["sent_at"] or "-"))
        try:
            if old["source"] == "paste":
                source = Source(kind="paste", text=old["text_sent"] or "", sent=old["sent_at"] or today)
            else:
                name, _, address = (old["sender"] or "").rpartition(" <")
                mail = outlook_bridge.MailInfo(subject=old["subject"] or "", entry_id=old["entry_id"] or "",
                                               message_id=old["message_id"] or "", sender_name=name,
                                               sender_address=address.rstrip(">"), sent_at=old["sent_at"] or "")
                source = from_mail(mail, search)
            reading = read(conn, old["vendor_request_id"], source, today=today, db_path=db_path, transport=transport,
                           trial=True)
        except ReadQuoteError as exc:
            out += ["Not read: {}".format(exc), ""]
            totals["failed"] += 1
            continue
        row = conn.execute("SELECT * FROM quote_read WHERE id = ?", (reading.id,)).fetchone()
        totals[reading.status] += 1
        totals["cost"] += reading.cost
        totals["in"] += row["input_tokens"] or 0
        totals["out"] += row["output_tokens"] or 0
        out.append("")
        out.append("### Earlier: reading {} (prompt version {}, {}), {}; USD {:.4f}".format(
            old_id, old["prompt_version"], (old["at"] or "")[:16], old["status"], old["cost_usd"] or 0))
        out.append(_files_line(json.loads(old["files"] or "[]")))
        out += _answer_lines(answer_of(old["response"]))
        out.append("")
        out.append("### Now: reading {} (prompt version {}), {}{}; {:,} input and {:,} output tokens, USD {:.4f}".format(
            reading.id, READ_QUOTE_PROMPT, reading.status,
            " - " + (reading.error or reading.why_not) if reading.error or reading.why_not else "",
            row["input_tokens"] or 0, row["output_tokens"] or 0, reading.cost))
        out.append(_files_line(reading.files))
        out += _answer_lines(answer_of(row["response"]))
        out.append("")
        out.append("### Now: what the drawer would get")
        for doc in reading.docs:
            out += _doc_lines(doc, "####")
        out.append("")
    spent, readings = month_spend(conn, today[:7])
    out += ["## Totals", "{} read, {} no prices, {} failed; {:,} input and {:,} output tokens; USD {:.4f}.".format(
        totals["read"], totals["no_prices"], totals["failed"], totals["in"], totals["out"], totals["cost"]),
        "{}: USD {:.2f} in {} reading{} (cap USD {:.2f}).".format(today[:7], spent, readings,
                                                                 "" if readings == 1 else "s", cap(conn))]
    stem = "read-quote-again-{}".format(today.replace("-", ""))
    path = reports_dir() / "{}.md".format(stem)
    k = 2
    while path.exists():
        path = reports_dir() / "{}-{}.md".format(stem, k)
        k += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def trial(conn, search, limit=10, since="2026-10-01", today=None, transport=None, db_path=None) -> Path:
    """Point 10: each pick read (trial 1, counted in the month's spend),
    made into what its drafts would be, never pushed; the file lists them."""
    today = today or dt.date.today().isoformat()
    picks = trial_picks(conn, search, limit, since)
    out = ["# Read quote trial, {}".format(today), "",
           "{} mail{} picked (since {}, at most two per vendor). Nothing here was pushed to the team site.".format(
               len(picks), "" if len(picks) == 1 else "s", since), ""]
    totals = {"cost": 0.0, "in": 0, "out": 0, "read": 0, "no_prices": 0, "failed": 0}
    for n, m in enumerate(picks, start=1):
        out.append("## {}. {} {} ({})".format(n, m["case"]["opp_no"], m["request"]["vendor_key"], m["mode"] or "-"))
        out.append("Mail: {} (sent {})".format(m["subject"], m["sent"]))
        try:
            source = from_mail(m["mail"], search)
            reading = read(conn, m["request"]["id"], source, today=today, db_path=db_path, transport=transport, trial=True)
        except ReadQuoteError as exc:
            out += ["Not read: {}".format(exc), ""]
            totals["failed"] += 1
            continue
        row = conn.execute("SELECT * FROM quote_read WHERE id = ?", (reading.id,)).fetchone()
        totals[reading.status] += 1
        totals["cost"] += reading.cost
        totals["in"] += row["input_tokens"] or 0
        totals["out"] += row["output_tokens"] or 0
        out.append(_files_line(reading.files))
        out.append("Reading {}: {}{}; {:,} input and {:,} output tokens, USD {:.4f}".format(
            reading.id, reading.status, " - " + (reading.error or reading.why_not) if reading.error or reading.why_not else "",
            row["input_tokens"] or 0, row["output_tokens"] or 0, reading.cost))
        for doc in reading.docs:
            out += _doc_lines(doc)
        out.append("")
    out += ["## Totals", "{} read, {} no prices, {} failed; {:,} input and {:,} output tokens; USD {:.4f}.".format(
        totals["read"], totals["no_prices"], totals["failed"], totals["in"], totals["out"], totals["cost"])]
    path = reports_dir() / "read-quote-trial-{}.md".format(today.replace("-", ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path
