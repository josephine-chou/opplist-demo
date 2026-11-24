r"""Suspected duplicate detection.

Scoring a candidate is cheap; getting the rarity right is the whole trick.
Document frequency is counted over EVERY enquiry, not over the candidate set:
inside a window of sixty days a word like 'batteries' looks unique and would
pull unrelated cases together. This was got wrong once already, so the corpus
count lives in one place and the candidate list never touches it.

Tokenisation is a port of Get-Tokens + Get-NameTokens in
scripts\Sort-Inbox.ps1, so the app and the mail pipeline agree on what counts
as a word.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

# Sort-Inbox.ps1 Get-Tokens. Process vocabulary only: commodity nouns and
# place names must stay out, because they are what actually discriminates.
STOP_WORDS = {
    "the", "and", "for", "from", "with", "rate", "rates", "request", "quote",
    "quotation", "inquiry", "enquiry", "port", "city", "usa", "air", "ocean",
    "fcl", "lcl", "door", "cargo", "freight", "shipment", "new", "via",
}

PROBABLE = 70
POSSIBLE = 40
MAX_RESULTS = 5
#: A reference both subjects (or remarks) carry - a PO or booking number.
SHARED_REFERENCE_POINTS = 40
#: DD1 (2026-11-12): the same subject or the same Outlook conversation is the
#: same case only while that case is open and came in at most this many days
#: before the mail (Settings, dedupe_same_subject_days). The same request at
#: another time is a new case - the quote is different (her rule, agreed
#: with Victor).
SAME_SUBJECT_SETTING = "dedupe_same_subject_days"
SAME_SUBJECT_DAYS = 14
OPEN_STATUSES = ("New", "Quoted")


def same_subject_days(conn) -> int:
    try:
        return max(int(_setting(conn, SAME_SUBJECT_SETTING, SAME_SUBJECT_DAYS)), 0)
    except (TypeError, ValueError):
        return SAME_SUBJECT_DAYS


def link_holds(other, day, days=SAME_SUBJECT_DAYS) -> tuple:
    """(True, '') when a same-subject or same-conversation link to `other`
    holds for a mail of `day`: the case is open (New or Quoted, not legacy)
    and came in at most `days` days before it. Else (False, why): 'a Lost
    case', '23 days earlier'."""
    status = _get(other, "status") or ""
    if status not in OPEN_STATUSES or _get(other, "legacy"):
        return False, "a {} case".format(status or "closed") if status else "a closed case"
    apart = _days_apart(_get(other, "received_on"), day) if day else None
    received, mail = _date(_get(other, "received_on")), _date(day)
    if received and mail and received < mail and apart is not None and apart > days:
        return False, "{} days earlier".format(apart)
    return True, ""


def tokens(text) -> list:
    """Get-Tokens: split on non-alphanumerics, lower case, keep length >= 3."""
    if not text:
        return []
    clean = re.sub(r"[^A-Za-z0-9]", " ", str(text)).lower()
    return [t for t in clean.split(" ") if len(t) >= 3 and t not in STOP_WORDS]


def name_tokens(text) -> list:
    """Get-NameTokens: drop bare numbers, fold a trailing 's' on long words."""
    out = []
    for token in tokens(text):
        if token.isdigit():
            continue
        if len(token) > 3 and token.endswith("s"):
            token = token[:-1]
        out.append(token)
    return out


#: A case's words come from the commodity and both location keys.
TOKEN_FIELDS = ("commodity", "origin_loc", "dest_loc")


def _get(row, key):
    """Read a field from either a sqlite3.Row or the form's plain dict."""
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _row_tokens(row) -> set:
    """Every word of the commodity and both places - what propose.py
    searches the mail for. The duplicate score uses case_tokens()."""
    words = set()
    for field_name in TOKEN_FIELDS:
        words.update(name_tokens(_get(row, field_name)))
    return words


#: A place's token is marked so it never meets a commodity word.
PLACE_MARK = "@"


def _place_token(value):
    text = " ".join(str(value or "").split()).casefold()
    return PLACE_MARK + text if text else None


def case_tokens(row) -> set:
    """What the duplicate score compares (13g): the commodity's words, and
    each place as ONE token - its key, or its name when there is no key.
    'Rio de Janeiro' is one place, not the two rare words 'rio' and
    'janeiro' it once counted as (20 points each, 2026-09-30)."""
    words = set(name_tokens(_get(row, "commodity")))
    for side in ("origin", "dest"):
        token = _place_token(_place(row, side))
        if token:
            words.add(token)
    return words


def document_frequency(conn: sqlite3.Connection) -> Counter:
    """How many enquiries contain each token, over the whole table: the
    words (_row_tokens) and the place tokens (case_tokens) alike."""
    from . import db as db_mod

    counts = Counter()
    for row in conn.execute(
        "SELECT e.commodity, {} AS origin_loc, {} AS dest_loc FROM enquiry e {}".format(
            db_mod.ORIGIN_KEY, db_mod.DEST_KEY, db_mod.LOCATION_JOIN)
    ):
        counts.update(_row_tokens(row) | case_tokens(row))
    return counts


# ---------------------------------------------------------- references --
#: A run of letters and digits; a dot or a hyphen joins two runs, a space
#: only between two digits ('101 694').
_REF_RUN = re.compile(r"[A-Za-z0-9]+(?:(?:[.\-]|(?<=\d) (?=\d))[A-Za-z0-9]+)*")
#: Not references: sizes and counts ('1X40HQ', '120X80X100'), a number with
#: its unit ('12400KG'), a date written as eight digits.
_NOT_REF = re.compile(r".*\dX\d.*|\d+(?:KGS?|LBS?|CBM|M3|CMS?|MM|TONS?|MT|PCS|PLTS?|CTNS?)")


def references(text) -> set:
    """The reference-like tokens of a subject or remark (13g): 5 or more
    characters with at least 3 digits, compared with spaces, hyphens and
    dots dropped, so '101.694' and '101694' are the same."""
    out = set()
    for run in _REF_RUN.findall(str(text or "")):
        token = re.sub(r"[.\- ]", "", run).upper()
        if len(token) < 5 or sum(c.isdigit() for c in token) < 3:
            continue
        if _NOT_REF.fullmatch(token) or _is_date(token):
            continue
        out.add(token)
    return out


def _is_date(token) -> bool:
    if len(token) != 8 or not token.isdigit():
        return False
    for fmt in ("%Y%m%d", "%d%m%Y", "%m%d%Y"):
        try:
            dt.datetime.strptime(token, fmt)
            return True
        except ValueError:
            pass
    return False


def _case_references(row) -> set:
    return references(_get(row, "source_subject")) | references(_get(row, "remarks"))


# ---------------------------------------------------------------- comparing --
def _norm(value):
    return (value or "").strip().casefold()


def _date(value):
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _days_apart(a, b):
    left, right = _date(a), _date(b)
    return abs((left - right).days) if left and right else None


def _endpoint_match(loc_a, loc_b, country_a, country_b):
    """'exact' when both location keys agree, 'partial' when only the country
    could be compared because one side has no location, None when they differ.

    An endpoint nobody has filled in is not evidence of a different lane, but
    it is not evidence of the same one either, so a partial match still counts
    while a full one is needed before the date bonus applies.
    """
    a, b = _norm(loc_a), _norm(loc_b)
    if a and b:
        return "exact" if a == b else None
    ca, cb = _norm(country_a), _norm(country_b)
    if ca and cb:
        return "partial" if ca == cb else None
    return "partial"


def _place(row, side):
    """The stored case's place: the master key its id names (candidates()
    selects it as *_key), else the text column."""
    return _get(row, side + "_key") or _get(row, side + "_loc")


def _lane_match(new, other):
    """(matched, exact) for the origin -> destination pair."""
    origin = _endpoint_match(new.get("origin_loc"), _place(other, "origin"),
                             new.get("origin_country"), other["origin_country"])
    dest = _endpoint_match(new.get("dest_loc"), _place(other, "dest"),
                           new.get("dest_country"), other["dest_country"])
    if origin is None or dest is None:
        return False, False
    return True, origin == "exact" and dest == "exact"


@dataclass
class Match:
    """One suspected duplicate. DD1: `seen_before` - the same subject as an
    old or closed case, shown as one line and asked nothing."""
    enquiry_id: int
    opp_no: str
    score: int
    reasons: list = field(default_factory=list)
    row: object = None

    @property
    def band(self) -> str:
        if self.score >= PROBABLE:
            return "probable"
        return "possible" if self.score >= POSSIBLE else "unlikely"

    @property
    def same_thread(self) -> bool:
        # 'same subject' is the thread key, worded for the page (2026-09-25)
        return any(r.startswith(("same thread", "same subject", "same conversation", "same mail"))
                   and not r.startswith(SEEN_BEFORE_MARKS) for r in self.reasons)

    @property
    def seen_before(self) -> bool:
        """DD1: linked by subject or conversation only outside the rule
        (closed, or older than the setting), and nothing else to go on."""
        return self.score < POSSIBLE and any(r.startswith(SEEN_BEFORE_MARKS) for r in self.reasons)

    @property
    def same_mail(self) -> bool:
        """Registered from the very mail being registered now (13g): the
        same Outlook entry id or conversation."""
        return any(r in ("same mail", "same conversation") for r in self.reasons)


#: DD1: the reasons a same-subject or same-conversation link outside the rule gives
SEEN_BEFORE_MARKS = ("same subject as", "same subject,", "same conversation as", "same conversation,")


def score_pair(new, other, df: Counter, link_days=SAME_SUBJECT_DAYS) -> Match:
    """Score one candidate against the enquiry being registered. DD1: the
    same subject or conversation scores only within link_holds; outside it
    adds no points and is no link (the reason says why)."""
    score = 0
    reasons = []

    new_thread = _norm(new.get("thread_key"))
    new_entry = _norm(new.get("entry_id"))
    if new_entry and new_entry == _norm(_get(other, "entry_id")):
        score += 100
        reasons.append("same mail")
    else:
        new_conv = _norm(new.get("conversation_id"))
        how = "subject" if new_thread and new_thread == _norm(other["thread_key"]) else (
            "conversation" if new_conv and new_conv == _norm(other["conversation_id"]) else "")
        if how:
            holds, why = link_holds(other, new.get("received_on"), link_days)
            if holds:
                score += 100
                reasons.append("same " + how)
            elif why.startswith("a "):
                reasons.append("same {} as {}".format(how, why))       # 'same subject as a Lost case'
            else:
                reasons.append("same {}, {}".format(how, why))         # 'same subject, 23 days earlier'

    lane_ok, lane_exact = _lane_match(new, other)
    same_mode = _norm(new.get("service_mode")) == _norm(other["service_mode"])

    if (lane_ok and same_mode
            and new.get("requester_party_id") == other["requester_party_id"]):
        score += 60
        reasons.append("same requester, lane and mode"
                       + ("" if lane_exact else " (lane matched on country)"))
        apart = _days_apart(new.get("received_on"), other["received_on"])
        if lane_exact and apart is not None and apart <= 7:
            score += 15
            reasons.append("{} day(s) apart".format(apart))

    new_customer = _norm(new.get("end_customer"))
    same_customer = bool(new_customer) and new_customer == _norm(other["end_customer"])
    if lane_ok and same_customer:
        score += 50
        reasons.append("same end customer and lane")

    shared_refs = sorted(_case_references(new) & _case_references(other))
    if shared_refs:
        score += SHARED_REFERENCE_POINTS
        reasons.append("same reference: " + ", ".join(shared_refs))

    shared = case_tokens(new) & case_tokens(other)
    commodity_points = 0
    rare = []
    for token in sorted(shared):
        frequency = df.get(token, 0)
        label = token[1:].upper() if token.startswith(PLACE_MARK) else token
        if frequency <= 1:
            commodity_points += 35
            rare.append("{} (unique)".format(label))
        elif frequency <= 3:
            commodity_points += 20
            rare.append("{} (seen {}x)".format(label, frequency))
    commodity_points = min(commodity_points, 50)
    if commodity_points:
        score += commodity_points
        reasons.append("rare words: " + ", ".join(rare))

    # 13g: two different requesters are not the same enquiry on place and
    # commodity words alone - it also takes the same thread, the same end
    # customer or a shared reference. The same requester scores as before.
    requester = new.get("requester_party_id")
    same_requester = requester not in (None, "") and str(requester) == str(other["requester_party_id"])
    # DD1: a same subject or conversation outside the rule is no link
    linked = any(r in ("same mail", "same subject", "same conversation") for r in reasons) \
        or same_customer or bool(shared_refs)
    if not same_requester and not linked and score >= POSSIBLE:
        score = POSSIBLE - 1
        reasons.append("different requesters, nothing shared but place and commodity words")

    return Match(other["id"], other["opp_no"], score, reasons, other)


# ------------------------------------------------------------------- entry --
def candidates(conn: sqlite3.Connection, received_on, window_days: int,
               exclude_id=None) -> list:
    """Every non-legacy case within the window, closed ones included."""
    day = _date(received_on) or dt.date.today()
    low = (day - dt.timedelta(days=window_days)).isoformat()
    high = (day + dt.timedelta(days=window_days)).isoformat()
    from . import db as db_mod

    sql = ("SELECT e.*, {} AS origin_key, {} AS dest_key FROM v_enquiry e {} "
           "WHERE e.legacy = 0 AND e.received_on IS NOT NULL "
           "AND e.received_on BETWEEN ? AND ?".format(
               db_mod.ORIGIN_KEY, db_mod.DEST_KEY, db_mod.LOCATION_JOIN))
    args = [low, high]
    if exclude_id is not None:
        sql += " AND e.id <> ?"
        args.append(exclude_id)
    return conn.execute(sql, args).fetchall()


class Found(list):
    """find()'s matches, best first, with `seen_before`: the cases linked
    by subject or conversation outside the rule (DD1), oldest last."""
    seen_before: list = []


def find(conn: sqlite3.Connection, new, window_days=None, limit=MAX_RESULTS,
         df: Counter | None = None) -> Found:
    """The suspected duplicates for a case being registered, best first.

    `new` is a plain mapping of the form values, not a stored row: the panel
    has to appear before anything is saved. DD1: a case linked only by a
    subject or conversation outside the rule is not a match; it comes back
    in `.seen_before`.
    """
    from . import db as db_mod

    if window_days is None:
        window_days = int(db_mod.get_setting(conn, "dedupe_window_days", "60"))
    if df is None:
        df = document_frequency(conn)
    link_days = same_subject_days(conn)

    scored = [score_pair(new, other, df, link_days)
              for other in candidates(conn, new.get("received_on"), window_days,
                                      new.get("id"))]
    matches = [m for m in scored if m.score >= POSSIBLE]
    matches.sort(key=lambda m: (-m.score, m.opp_no))
    found = Found(matches[:limit])
    found.seen_before = sorted((m for m in scored if m.seen_before),
                               key=lambda m: (_get(m.row, "received_on") or ""), reverse=True)
    return found


def report_rows(conn: sqlite3.Connection, threshold: int = PROBABLE) -> list:
    """Every probable pair in the database, for migration-dedupe-report.csv.

    The migration does not group anything by itself;
    it leaves this list for Iris to decide on in the app.
    """
    df = document_frequency(conn)
    rows = conn.execute(
        "SELECT * FROM v_enquiry WHERE legacy = 0 ORDER BY "
        "CAST(substr(opp_no, 5) AS INTEGER)"
    ).fetchall()
    window = int(_setting(conn, "dedupe_window_days", "60"))

    seen = set()
    out = []
    by_id = {r["id"]: r for r in rows}
    for row in rows:
        new = dict(row)
        for other in candidates(conn, row["received_on"], window, row["id"]):
            if other["id"] not in by_id:
                continue
            pair = tuple(sorted((row["id"], other["id"])))
            if pair in seen:
                continue
            match = score_pair(new, other, df)
            if match.score < threshold:
                continue
            seen.add(pair)
            out.append({
                "OppNoA": row["opp_no"],
                "OppNoB": other["opp_no"],
                "Score": match.score,
                "Band": match.band,
                "RequesterA": row["requester_key"],
                "RequesterB": other["requester_key"],
                "LaneA": _lane_text(row),
                "LaneB": _lane_text(other),
                "Reasons": "; ".join(match.reasons),
            })
    out.sort(key=lambda r: (-r["Score"], r["OppNoA"], r["OppNoB"]))
    return out


def _setting(conn, key, default):
    from . import db as db_mod
    return db_mod.get_setting(conn, key, default)


def _lane_text(row) -> str:
    origin = row["origin_loc"] or row["origin_country"] or "?"
    dest = row["dest_loc"] or row["dest_country"] or "?"
    return "{} - {}".format(origin, dest)
