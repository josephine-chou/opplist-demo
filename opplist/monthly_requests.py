"""The month report's analysis page 5, "Analysis · Requests" (batch 17g, 2026-10-12; sample v16 page 5), cards as page 4's
(monthly_analysis.Card):

    15  What we win, by shipment size   the month's and the previous month's
                                        cases, a dot each, FCL by TEU, LCL by
                                        cbm, Air by weight, in fixed columns
    16  Why we lost                     section 2's reasons as a donut
    17  LATAM requests by country       17f's LATAM agent requests by the
                                        agent's country, three months, with
                                        the Settings list report_events
    18  How <Prev>'s requests moved     the previous month's cases on its last
                                        day and on this month's last day

Nothing here is typed by hand. The cases section 15 cannot draw (no
figure for their mode) are listed in the Report page's check box (17h,
left_out), not on the page.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass

from . import monthly, status as status_mod
from .monthly import BASE, CASES, Row, Table
from .monthly_analysis import (COUNTRY_COLOURS, FAINT, GREY, INK, LATAM_AGENT, OTHERS_COLOUR, Card, _abbr,
                               _by_country, _esc, _font, _months_back, _name, _pill, _plural)

WON_COLOUR = "#4f9a24"
LOST_COLOUR = "#b3405a"
OPEN_COLOUR = "#eda100"
NOT_QUOTED_COLOUR = "#c3c2b7"
#: section 16: the first reason, then greys from dark to light
DONUT_COLOURS = ("#b3405a", "#9aa3ad", "#d1d5da", "#e1e4e7", "#ebedef", "#f3f4f5")
#: section 17: the Settings list (one event a line)
EVENTS_SETTING = "report_events"
EVENT_LINES = 2
#: section 16: the cases named after 'never quoted', at most (17g)
NEVER_QUOTED_NAMES = 4
#: page 5 is tightened (class dense) past this estimate of its two rows'
#: height in px (see text_px); measured in Edge 2026-10-12: the page is the
#: estimate plus 345 to 352 px (live copy: September 426 at 773 px of 794,
#: October 438 at 790; the June test month 442 at 787); the made-up worst
#: case is 851 px untightened, 755 tightened
DENSE_PX = 442


def _month_name_of(month) -> str:
    return calendar.month_name[int(month[5:7])]


def _day(d: dt.date) -> str:
    return "{} {}".format(calendar.month_abbr[d.month], d.day)


def _month_end(month) -> dt.date:
    year, mon = int(month[:4]), int(month[5:7])
    return dt.date(year, mon, calendar.monthrange(year, mon)[1])


def _next_month(month) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    year, mon = (year, mon + 1) if mon < 12 else (year + 1, 1)
    return "{:04d}-{:02d}".format(year, mon)


def _figure(v) -> str:
    """12 -> '12', 7.3 -> '7.3', 6.25 -> '6.3'."""
    v = round(float(v), 1)
    return str(int(v)) if v == int(v) else "{:.1f}".format(v)


# ------------------------------------------------- 15. what we win, by size --
#: (mode, the title, the case's figure, [(label, upper bound or None)])
SIZES = (
    ("FCL", "FCL, TEU per request", "teu",
     [("1", 2), ("2", 3), ("3-7", 8), ("8-15", 16), ("16+", None)]),
    ("LCL", "LCL, cbm per request", "lcl_cbm",
     [("<1", 1), ("1-4.9", 5), ("5-9.9", 10), ("10+", None)]),
    ("Air", "Air, weight per request", "air_weight_kg",
     [("<100 kg", 100), ("100-499", 500), ("500-999", 1000), ("1-2.9 t", 3000), ("3 t+", None)]),
)
MODE_SQL = {"FCL": monthly.FCL, "LCL": monthly.LCL, "Air": monthly.AIR}
LARGE_TEU = 8
LARGE_KG = 3000


def _state_now(status) -> str:
    """A dot's colour: won, lost (Lost or Cancelled) or open (the rest)."""
    if status == "Won":
        return "won"
    if status in ("Lost", "Cancelled"):
        return "lost"
    return "open"


def _column(columns, value) -> int:
    for i, (_, upper) in enumerate(columns):
        if upper is None or value < upper:
            return i
    return len(columns) - 1


def _size_rows(conn, month, mode, day=None):
    """The month's and the previous month's cases of this mode in section
    15, oldest case number first, each with its figure as v and (17j) its
    status on the month's closing day as status_on."""
    figure = next(f for m, _, f, _ in SIZES if m == mode)
    return conn.execute(
        "SELECT e.*, e.{f} AS v, {on} AS status_on {base} WHERE e.month IN (?, ?) AND {cases} AND {mode} "
        "ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER)".format(
            f=figure, on=monthly.status_on_sql(day or monthly.close_day(month)), base=BASE, cases=CASES,
            mode=MODE_SQL[mode]),
        (monthly.previous_month(month), month)).fetchall()


def _no_size(r) -> bool:
    return not r["v"] or r["v"] <= 0


def sizes(conn, month, day=None) -> dict:
    """{mode: {'cases': [(value, state)], 'missing': n}} for the month and the
    previous one, the state on the month's closing day (17j); a case
    without its mode's figure is only counted."""
    out = {}
    for mode, _, _, _ in SIZES:
        rows = _size_rows(conn, month, mode, day)
        out[mode] = {"cases": [(float(r["v"]), _state_now(r["status_on"])) for r in rows if not _no_size(r)],
                     "missing": sum(1 for r in rows if _no_size(r))}
    return out


#: 17h: what the check box says a left-out case is missing
MISSING_WORDS = {"FCL": "FCL, no container count or type", "LCL": "LCL, no cbm", "Air": "air, no weight"}


def left_out(conn, month) -> list:
    """17h: [(mode, row)] of the cases section 15 leaves out (no figure for
    their mode), FCL, then LCL, then Air, oldest case number first; the
    same reading as sizes."""
    return [(mode, r) for mode, _, _, _ in SIZES for r in _size_rows(conn, month, mode) if _no_size(r)]


def what_we_win(conn, month, day=None) -> Card:
    prev = monthly.previous_month(month)
    found = sizes(conn, month, day)
    card = Card("15", "What we win, by shipment size")
    fcl = found["FCL"]["cases"]
    large = [(v, s) for v, s in fcl if v >= LARGE_TEU]
    card.big = "{} of {}".format(sum(1 for _, s in large if s == "won"), len(large))
    card.caption = "FCL requests of {} TEU or more won ({} TEU)".format(LARGE_TEU, _figure(sum(v for v, _ in large)))
    card.pill = ("{} and {}".format(_month_name_of(prev), _month_name_of(month)), "flat")
    svgs, table = [], Table(["Size", "Won", "Lost or cancelled", "Open"])
    for mode, title, _, columns in SIZES:
        cases = found[mode]["cases"]
        if not cases and not found[mode]["missing"]:
            continue
        table.rows.append(Row(title, [], "head"))
        counts = [{"won": 0, "lost": 0, "open": 0} for _ in columns]
        for v, s in cases:
            counts[_column(columns, v)][s] += 1
        for (label, _), c in zip(columns, counts):
            table.rows.append(Row(label, [str(c["won"]), str(c["lost"]), str(c["open"])]))
        if cases:
            svgs.append(dots_svg(title, [label for label, _ in columns], counts))
    card.svg = '<div class="dots">{}</div>'.format("".join(svgs)) if svgs else ""
    card.chart = {"widths": [float(re.search(r'width="([0-9.]+)"', svg).group(1)) for svg in svgs]}
    card.legend = [(WON_COLOUR, "Won"), (LOST_COLOUR, "Lost or cancelled"), (OPEN_COLOUR, "Still open")]
    card.table = table
    if not svgs:
        card.empty = "No FCL, LCL or air request with its size in {} or {}.".format(
            _month_name_of(prev), _month_name_of(month))
    # FCL
    if fcl:
        wins = sorted({v for v, s in fcl if s == "won"})
        n = sum(1 for _, s in fcl if s == "won")
        if not wins:
            b = [("No FCL win", False)]
        elif len(wins) == 1:
            b = [("FCL: every win is ", False), ("{} TEU".format(_figure(wins[0])), True)]
        elif len(wins) == 2:
            b = [("FCL: every win is ", False), ("{} or {} TEU".format(_figure(wins[0]), _figure(wins[1])), True)]
        else:
            b = [("FCL: wins from ", False), ("{} to {} TEU".format(_figure(wins[0]), _figure(wins[-1])), True)]
        if wins:
            b.append((" ({} {})".format(n, _plural(n, "win", "wins")), False))
        if large:
            b += [("; the ", False), (str(len(large)), True), (" {} of {} TEU or more: ".format(
                _plural(len(large), "request", "requests"), LARGE_TEU), False),
                (str(sum(1 for _, s in large if s == "won")), True), (" won, ", False),
                (str(sum(1 for _, s in large if s == "open")), True), (" still open", False)]
        card.bullets.append(b)
    lcl = found["LCL"]["cases"]
    if lcl:
        wins = sorted(v for v, s in lcl if s == "won")
        if not wins:
            card.bullets.append([("No LCL win", False)])
        else:
            span = _figure(wins[0]) if wins[0] == wins[-1] else "{} to {}".format(_figure(wins[0]), _figure(wins[-1]))
            card.bullets.append([("LCL: ", False), (str(len(wins)), True), (" {}, ".format(
                _plural(len(wins), "win", "wins")), False), (span, True), (" cbm", False)])
    air = found["Air"]["cases"]
    if air:
        wins = [v for v, s in air if s == "won"]
        heavy = [(v, s) for v, s in air if v >= LARGE_KG]
        b = [("Air: wins up to ", False), ("{} t".format(_tonnes(max(wins))), True)] if wins \
            else [("No air win", False)]
        if heavy:
            b += [("; ", False), (str(sum(1 for _, s in heavy if s == "won")), True), (" of the ", False),
                  (str(len(heavy)), True), (" {} of 3 t or more won".format(
                      _plural(len(heavy), "request", "requests")), False)]
        card.bullets.append(b)
    return card


def _tonnes(kg) -> str:
    t = kg / 1000.0
    if t >= 1:
        return _figure(t)
    return "{:.2f}".format(t).rstrip("0").rstrip(".")


DOT_R, DOT_PITCH, COL_GAP, PAD = 3.4, 8.6, 14, 2.9
BASELINE = 92.2
MAX_ROWS = 7


def dots_svg(title, labels, counts) -> str:
    """One group of section 15: a column per size, its dots stacked from the
    bottom - won, then lost or cancelled, then open - the count above and
    the label under."""
    totals = [c["won"] + c["lost"] + c["open"] for c in counts]
    per = max(3, -(-max(totals) // MAX_ROWS))
    col_w = per * DOT_PITCH
    width = round(PAD * 2 + len(labels) * col_w + (len(labels) - 1) * COL_GAP, 1)
    out = ['<svg viewBox="0 0 {w} 118" width="{w}" height="118" role="img">'.format(w=width),
           '<text x="0.0" y="11.0" {}>{}</text>'.format(_font(11, INK, "start", "600"), _esc(title)),
           '<line x1="0" y1="{b}" x2="{w}" y2="{b}" stroke="#E1E0D9" stroke-width="1"/>'.format(b=BASELINE, w=width)]
    for i, (label, c) in enumerate(zip(labels, counts)):
        x0 = PAD + i * (col_w + COL_GAP)
        k = 0
        for state, colour in (("won", WON_COLOUR), ("lost", LOST_COLOUR), ("open", OPEN_COLOUR)):
            for _ in range(c[state]):
                cx = x0 + DOT_R + (k % per) * DOT_PITCH
                cy = BASELINE - 6.3 - (k // per) * DOT_PITCH
                out.append('<circle cx="{:.1f}" cy="{:.1f}" r="{}" fill="{}"/>'.format(cx, cy, DOT_R, colour))
                k += 1
        rows = -(-totals[i] // per)
        top = BASELINE - 6.3 - (rows - 1) * DOT_PITCH if rows else BASELINE + 2
        mid = x0 + col_w / 2
        out.append('<text x="{:.1f}" y="{:.1f}" {}>{}</text>'.format(mid, top - 8.3, _font(10, GREY, "middle"),
                                                                     totals[i]))
        out.append('<text x="{:.1f}" y="105.2" {}>{}</text>'.format(mid, _font(10, GREY, "middle"), _esc(label)))
    out.append("</svg>")
    return "\n".join(out)


# ---------------------------------------------------------- 16. why we lost --
_MODE_WORD = {"Air": "air", "FCL": "FCL", "LCL": "LCL", "RORO": "RORO"}
#: AL7: read letter by letter, so 'an FCL import', 'an LCL export'
_SPELLED = ("FCL", "LCL")
_DIRECTION_NOUN = {"US Export": ("export", "exports"), "US Import": ("import", "imports"),
                   "Cross-trade": ("Triangle request", "Triangle requests"),
                   "US Domestic": ("domestic move", "domestic moves")}


def _kind(mode, direction, plural) -> str:
    word = _MODE_WORD.get(mode, "" if mode in (None, "", "Not specified") else mode.lower())
    noun = _DIRECTION_NOUN.get(direction, ("request", "requests"))[1 if plural else 0]
    return " ".join(w for w in (word, noun) if w)


def _article(text) -> str:
    vowel = text.startswith(_SPELLED) or (text[:1].lower() in "aeiou" and not text.startswith("RORO"))
    return ("an " if vowel else "a ") + text


def _lane(r) -> str:
    return "{} to {}".format(r["origin_country"] or "?", r["dest_country"] or "?")


def why_we_lost(conn, month, day=None) -> Card:
    name = _month_name_of(month)
    card = Card("16", "Why we lost")
    day = day or monthly.close_day(month)
    bars = monthly.lost_bars(conn, month, day)
    card.table = Table(["Reason", "Lost"], [Row(label, [str(n)]) for label, n in bars])
    if not bars:
        card.empty = "No lost cases in {}.".format(name)
        return card
    total = sum(n for _, n in bars)
    first, n = bars[0]
    card.big = "{} of {}".format(n, total)
    card.caption = "{} losses on price".format(name) if first == "Price too high" \
        else "{} losses: {}".format(name, first)
    colours = [DONUT_COLOURS[min(i, len(DONUT_COLOURS) - 1)] for i in range(len(bars))]
    card.svg = '<div class="dwrap">{}<div class="dlegend">{}</div></div>'.format(
        donut_svg(bars, colours, total),
        "".join('<div><span><i style="background:{}"></i>{}</span><b>{}</b></div>'.format(c, _esc(label), k)
                for (label, k), c in zip(bars, colours)))
    for reason, count in bars[:2]:
        if reason == "Other reasons":
            continue
        rows = conn.execute(
            "SELECT e.service_mode, e.direction, e.requester_key, e.origin_country, e.dest_country, "
            "e.quoted_on_any {base} WHERE e.month = ? AND {cases} AND {lost} "
            "AND TRIM({why}) = ? ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER)".format(
                base=BASE, cases=CASES, lost=monthly.lost_on(day), why=monthly.reason_on_sql(day)),
            (month, reason)).fetchall()
        parts = [(reason + ": ", False), (str(count), True)]
        kinds = {}
        for r in rows:
            kinds.setdefault((r["service_mode"], r["direction"]), []).append(r)
        (mode, direction), most = max(kinds.items(), key=lambda kv: len(kv[1])) if kinds else ((None, None), [])
        if rows and len(kinds) == 1:
            parts.append(("; " + ("all " + _kind(mode, direction, True) if len(rows) > 1
                                   else _article(_kind(mode, direction, False))), False))
        elif len(most) >= 2:
            parts += [("; ", False), (str(len(most)), True), (" of them " + _kind(mode, direction, True), False)]
        who = {}
        for r in rows:
            who.setdefault((r["requester_key"] or "", _lane(r)), []).append(r)
        if who:
            (requester, lane), same = max(who.items(), key=lambda kv: len(kv[1]))
            if len(same) >= 2:
                parts += [("; ", False), (str(len(same)), True), (" are {}, {}".format(requester, lane), False)]
        if rows and all(not r["quoted_on_any"] for r in rows):
            names = ["{} ({})".format(r["requester_key"] or "", _lane(r)) for r in rows]
            if len(names) > NEVER_QUOTED_NAMES:
                names = names[:NEVER_QUOTED_NAMES] + ["and {} more".format(len(names) - NEVER_QUOTED_NAMES)]
            parts.append(("; never quoted: " + ", ".join(names[:-1]) + (" " if names[-1].startswith("and ") else ", ")
                          + names[-1] if len(names) > 1 else "; never quoted: " + names[0], False))
        card.bullets.append(parts)
    return card


def donut_svg(bars, colours, total) -> str:
    """Section 16's ring: 118 px, radius 49, stroke 20, a hair between."""
    import math
    cx = cy = 59.0
    r = 49.0
    out = ['<svg viewBox="0 0 118 118" width="118" height="118" class="donut">']
    start = 0.0
    gap = 0.012 if len(bars) > 1 else 0.0
    for (label, n), colour in zip(bars, colours):
        share = n / total
        a0, a1 = start + gap, start + share - gap
        start += share
        if share >= 0.9999:
            out.append('<circle cx="59.0" cy="59.0" r="49.0" fill="none" stroke="{}" stroke-width="20">'
                       '<title>{} {}</title></circle>'.format(colour, _esc(label), n))
            continue
        x0, y0 = cx + r * math.sin(2 * math.pi * a0), cy - r * math.cos(2 * math.pi * a0)
        x1, y1 = cx + r * math.sin(2 * math.pi * a1), cy - r * math.cos(2 * math.pi * a1)
        large = 1 if (a1 - a0) > 0.5 else 0
        out.append('<path d="M{:.1f},{:.1f} A49.0,49.0 0 {} 1 {:.1f},{:.1f}" fill="none" stroke="{}" '
                   'stroke-width="20"><title>{} {}</title></path>'.format(x0, y0, large, x1, y1, colour,
                                                                          _esc(label), n))
    out.append('<text x="59.0" y="57.0" text-anchor="middle" class="dn">{}</text>'.format(total))
    out.append('<text x="59.0" y="75.0" text-anchor="middle" class="dl">lost</text>')
    out.append("</svg>")
    return "".join(out)


# ------------------------------------------------- 17. LATAM requests by country --
_DATE = r"(\d{4}-\d{2}-\d{2})"
_EVENT = re.compile(r"^\s*" + _DATE + r"(?:\s+to\s+" + _DATE + r")?\s+(\S.*?)\s*$")


@dataclass
class Event:
    start: dt.date
    end: dt.date
    label: str

    def text(self) -> str:
        if self.start == self.end:
            return "{}, {}".format(self.label, _day(self.start))
        if (self.start.year, self.start.month) == (self.end.year, self.end.month):
            return "{}, {}-{}".format(self.label, _day(self.start), self.end.day)
        return "{}, {} to {}".format(self.label, _day(self.start), _day(self.end))


def parse_event(line) -> Event | None:
    """'YYYY-MM-DD label' or 'YYYY-MM-DD to YYYY-MM-DD label'; None when the
    line is neither (or a date is not a date, or the end is before the start)."""
    m = _EVENT.match(line or "")
    if not m:
        return None
    try:
        start = dt.date.fromisoformat(m.group(1))
        end = dt.date.fromisoformat(m.group(2)) if m.group(2) else start
    except ValueError:
        return None
    if end < start:
        return None
    return Event(start, end, m.group(3))


def refused_event_lines(lines) -> list:
    """[(line number, text)] of the lines Settings cannot read."""
    return [(i, line) for i, line in enumerate(lines, start=1) if line.strip() and parse_event(line) is None]


def events(conn) -> list:
    from . import db
    found = [parse_event(line) for line in db.get_setting_list(conn, EVENTS_SETTING)]
    return sorted((e for e in found if e), key=lambda e: (e.start, e.end, e.label.casefold()))


def events_in(all_events, month) -> list:
    """The events that fall in the month (any day of theirs in it)."""
    first = dt.date(int(month[:4]), int(month[5:7]), 1)
    last = _month_end(month)
    return [e for e in all_events if e.start <= last and e.end >= first]


def event_lines(found) -> list:
    if len(found) <= EVENT_LINES:
        return [e.text() for e in found]
    return [found[0].text(), "+{} more".format(len(found) - 1)]


def _country_counts(conn, month) -> dict:
    return _by_country(conn, [month])


def by_country(conn, month) -> Card:
    months = _months_back(month, 3)
    name, prev = _month_name_of(month), months[-2]
    per = [_country_counts(conn, m) for m in months]
    three = _by_country(conn, months)
    top = sorted(three, key=lambda c: (-three[c], c.casefold()))[:3]
    total = sum(per[-1].values())
    before = sum(per[-2].values())
    card = Card("17", "LATAM requests by country", big=str(total),
                caption="LATAM agent requests in {}".format(name))
    if before:
        delta = total - before
        card.pill = _pill(delta, "{}% vs {} ({})".format(abs(round(100.0 * delta / before)), _abbr(prev), before))
    all_events = events(conn)
    found = [events_in(all_events, m) for m in months]
    stacks = []
    for counts in per:
        stack = [(c, counts.get(c, 0), COUNTRY_COLOURS[i]) for i, c in enumerate(top)]
        stack.append(("Others", sum(n for c, n in counts.items() if c not in top), OTHERS_COLOUR))
        stacks.append(stack)
    card.svg = country_svg([_abbr(m) for m in months], stacks, [event_lines(f) for f in found])
    card.legend = [(COUNTRY_COLOURS[i], c) for i, c in enumerate(top)] + [(OTHERS_COLOUR, "Others")]
    countries = sorted(set().union(*per), key=lambda c: (-per[-1].get(c, 0), -three[c], c.casefold()))
    card.table = Table(["Country"] + [_month_name_of(m) for m in months],
                       [Row(c, [str(p.get(c, 0)) for p in per]) for c in countries])
    card.table.rows.append(Row("Total", [str(sum(p.values())) for p in per], "total"))
    for m, f in zip(months, found):
        for e in f:
            card.table.rows.append(Row("Event: {}".format(e.text()), [_month_name_of(m)]))
    if not total:
        card.bullets.append([("No LATAM agent request in {}".format(name), False)])
        return card
    order = sorted(top, key=lambda c: (-per[-1].get(c, 0), top.index(c)))

    def move(c):
        return [(c + " ", False), ("{} → {}".format(per[-2].get(c, 0), per[-1].get(c, 0)), True)]

    rest = order[1:]
    if rest:
        parts = []
        for i, c in enumerate(rest):
            parts += ([(", ", False)] if i else []) + move(c)
        card.bullets.append(parts)
    lead = order[0]
    agent = conn.execute(
        "SELECT e.requester_key AS agent, COUNT(*) AS n {base} WHERE e.month = ? AND {cases} AND {who} "
        "AND COALESCE(NULLIF(TRIM(p.country), ''), 'Unknown') = ? GROUP BY e.requester_party_id "
        "ORDER BY n DESC, LOWER(e.requester_key) LIMIT 1".format(base=BASE, cases=CASES, who=LATAM_AGENT),
        (month, lead)).fetchone()
    parts = move(lead)
    if agent is not None:
        parts += [(", ", False), (str(agent["n"]), True), (" of them {}".format(agent["agent"] or ""), False)]
    card.bullets.append(parts)
    others = sorted(((c, n) for c, n in per[-1].items() if c not in top and n), key=lambda t: (-t[1], t[0].casefold()))
    if others:
        if all(n == 1 for _, n in others):
            card.bullets.append([("Others: {}, one request{}".format(
                ", ".join(c for c, _ in others), " each" if len(others) > 1 else ""), False)])
        else:
            parts = [("Others: ", False)]
            for i, (c, n) in enumerate(others):
                parts += [((", " if i else "") + c + " ", False), (str(n), True)]
            card.bullets.append(parts)
    return card


def country_svg(labels, stacks, notes) -> str:
    """Section 17's columns: a stack per month in the three colours and
    Others, 2 px white gaps, rounded top, the total above, the month and its
    events under it."""
    from .monthly_analysis import _top_rounded
    left, right, top, bottom = 28, 354, 22.0, 92.0
    totals = [sum(n for _, n, _ in s) for s in stacks]
    peak = max([1] + totals)
    step = 1
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000):
        if -(-peak // step) <= 5:
            break
    ymax = -(-peak // step) * step
    unit = (bottom - top) / ymax
    pitch = (right - left) / len(labels)
    out = ['<svg viewBox="0 0 380 138" width="380" height="138" role="img">']
    tick = 0
    while tick <= ymax:
        y = bottom - tick * unit
        out.append('<line x1="{}" y1="{:.1f}" x2="{}" y2="{:.1f}" stroke="#E1E0D9" stroke-width="1"/>'.format(
            left, y, right, y))
        out.append('<text x="22.0" y="{:.1f}" {}>{}</text>'.format(y + 3.5, _font(10, GREY, "end"), tick))
        tick += step
    for i, label in enumerate(labels):
        cx = left + pitch * (i + 0.5)
        x0, x1 = cx - 12, cx + 12
        parts = [(n, colour) for _, n, colour in stacks[i] if n]
        y = bottom
        for k, (n, colour) in enumerate(parts):
            h = n * unit
            if k == len(parts) - 1:
                out.append('<path d="{}" fill="{}"/>'.format(_top_rounded(x0, x1, y, y - h), colour))
            elif h > 2.5:
                out.append('<path d="M{:.1f},{:.1f} V{:.1f} H{:.1f} V{:.1f} Z" fill="{}"/>'.format(
                    x0, y, y - h + 2, x1, y, colour))
            y -= h
        out.append('<text x="{:.1f}" y="{:.1f}" {}>{}</text>'.format(cx, y - 6, _font(10.5, INK, "middle", "600"),
                                                                      totals[i]))
        out.append('<text x="{:.1f}" y="107.0" {}>{}</text>'.format(cx, _font(10.5, GREY, "middle"), label))
        for k, line in enumerate(notes[i]):
            out.append('<text x="{:.1f}" y="{:.1f}" {}>{}</text>'.format(cx, 120.0 + 12 * k,
                                                                         _font(9.5, FAINT, "middle"), _esc(line)))
    out.append("</svg>")
    return "\n".join(out)


# ---------------------------------------- 18. how the previous month moved --
STATES = (("won", "won", WON_COLOUR), ("lost", "lost or cancelled", LOST_COLOUR),
          ("open", "no decision yet", OPEN_COLOUR), ("new", "not quoted", NOT_QUOTED_COLOUR))
STATE_HEADS = {"won": "Won", "lost": "Lost or cancelled", "open": "No decision yet", "new": "Not quoted"}


#: 17j: a Lost reason that says nobody answered (any requester type)
NO_RESPONSE_PREFIX = "no response from"


def win_rate_on(conn, month, day, agents_offices=False) -> tuple:
    """(won, lost, rate or None) of the month's cases on the day, as
    section 4 counts them: agents' and offices' cases only when section 4
    shows that row (agents_offices: a direct customer's case in the
    report's month or the one before). 17k: the rate is won \u00f7 quoted,
    a quote still open on the day counted as not won."""
    f = monthly._figures(conn, month, day)
    if agents_offices:
        won, lost = f["won_ao"], f["lost_ao"]
    else:
        won, lost = f["won"], f["lost"]
    return won, lost, monthly.win_rate(f, agents_offices)


def status_on(conn, enquiry_id, day: str) -> str:
    """The status a case had on the day: its latest status event on or
    before it, as Repository.latest_event chooses; New with none by then."""
    row = conn.execute(
        "SELECT status FROM status_event WHERE enquiry_id = ? AND voided = 0 AND {} AND substr(at, 1, 10) <= ? "
        "ORDER BY {} LIMIT 1".format(status_mod.STATUS_KIND_SQL, status_mod.LATEST_EVENT_ORDER),
        (enquiry_id, day)).fetchone()
    return row["status"] if row is not None else "New"


def fold(status) -> str:
    """Won; Lost or cancelled; No decision yet (Quoted); Not quoted (the rest:
    New, Sourcing). A re-quote is a revised quote that went out (v30): its
    event is Quoted, so the case is No decision yet."""
    if status == "Won":
        return "won"
    if status in ("Lost", "Cancelled"):
        return "lost"
    if status == "Quoted":
        return "open"
    return "new"


def _case_ids(conn, month) -> list:
    return [r[0] for r in conn.execute("SELECT e.id {base} WHERE e.month = ? AND {cases} "
                                       "ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER)".format(base=BASE, cases=CASES),
                                       (month,))]


def moved(conn, month, today) -> Card:
    prev = monthly.previous_month(month)
    name, pname, nname = _month_name_of(month), _month_name_of(prev), _month_name_of(_next_month(month))
    day1 = _month_end(prev)
    day2 = min(_month_end(month), today)
    card = Card("18", "How {}’s requests moved in {}".format(pname, name))
    flows, first, second, raw2 = {}, {}, {}, {}
    for cid in _case_ids(conn, prev):
        a = fold(status_on(conn, cid, day1.isoformat()))
        s2 = status_on(conn, cid, day2.isoformat())
        b = fold(s2)
        flows[(a, b)] = flows.get((a, b), 0) + 1
        first[a] = first.get(a, 0) + 1
        second[b] = second.get(b, 0) + 1
        raw2[cid] = (a, s2)
    now = {}
    for cid in _case_ids(conn, month):
        b = fold(status_on(conn, cid, day2.isoformat()))
        now[b] = now.get(b, 0) + 1
    n_prev, n_now = sum(first.values()), sum(now.values())
    open1 = first.get("open", 0)
    decided = [s for a, s in raw2.values() if a == "open" and s in ("Won", "Lost", "Cancelled")]
    won = sum(1 for s in decided if s == "Won")
    lost = len(decided) - won
    card.big = "{} of {}".format(len(decided), open1)
    card.caption = "open {} quotes were decided in {}".format(pname, name)
    card.pill = ("{} won, {} {}".format(won, lost, "lost or cancelled" if "Cancelled" in decided else "lost"), "flat")
    card.table = Table(["{} \\ {}".format(_day(day1), _day(day2))] + [STATE_HEADS[k] for k, _, _ in STATES]
                       + ["Total"])
    for k, _, _ in STATES:
        cells = [flows.get((k, j), 0) for j, _, _ in STATES]
        card.table.rows.append(Row(STATE_HEADS[k], [str(v) for v in cells] + [str(sum(cells))]))
    card.table.rows.append(Row("{}’s {} requests on {}".format(name, n_now, _day(day2)), [], "head"))
    card.table.rows.append(Row(name, [str(now.get(k, 0)) for k, _, _ in STATES] + [str(n_now)]))
    if not n_prev and not n_now:
        card.empty = "No requests in {} or {}.".format(pname, name)
        return card
    card.svg = (flow_svg(first, second, flows, _day(day1), _day(day2))
                + '<div class="sub">{}’s {} requests on {}</div>'.format(name, n_now, _day(day2))
                + bar_svg(now))
    card.legend = [(colour, STATE_HEADS[k]) for k, _, colour in STATES]
    card.bullets.append([("{}’s {} requests on {}: ".format(pname, n_prev, _day(day2)), False),
                         (str(second.get("won", 0)), True), (" won, ", False),
                         (str(second.get("lost", 0)), True), (" lost or cancelled, ", False),
                         (str(second.get("open", 0)), True), (" still without a decision", False)])
    # 17j: the previous month's win rate on both days, and what was lost since
    rate = _rate_bullet(conn, month, day1, day2, raw2)
    if rate:
        card.bullets.append(rate)
    if second.get("new"):
        q = second["new"]
        card.bullets.append([(str(q), True), (" {} {} still not quoted after a month".format(
            pname, _plural(q, "request", "requests")), False)])
    card.bullets.append([("Open going into {}: ".format(nname), False), (str(second.get("open", 0)), True),
                         (" {} and ".format(pname), False), (str(now.get("open", 0)), True),
                         (" {} quotes with no decision".format(name), False)])
    return card


def _rate_bullet(conn, month, day1, day2, raw2) -> list:
    """'<Prev> win rate: a% on <day1>, b% on <day2>; n lost since, k of
    them No response' - [] when nothing was decided between the two days.
    A rate with nothing decided reads '-'."""
    since = [cid for cid, (a, s) in raw2.items() if s in ("Won", "Lost", "Cancelled")
             and status_on(conn, cid, day1.isoformat()) != s]
    if not since:
        return []
    prev = monthly.previous_month(month)
    # section 4's row: agents and offices when this month or the last has a direct customer's case
    ao = bool(monthly._figures(conn, month)["direct"] or monthly._figures(conn, prev)["direct"])
    _, _, a = win_rate_on(conn, prev, day1.isoformat(), ao)
    _, _, b = win_rate_on(conn, prev, day2.isoformat(), ao)
    lost = [cid for cid in since if raw2[cid][1] == "Lost"]
    quiet = 0
    for cid in lost:
        why = conn.execute("SELECT {} FROM v_enquiry e WHERE e.id = ?".format(
            monthly.reason_on_sql(day2.isoformat())), (cid,)).fetchone()[0]
        if (why or "").strip().casefold().startswith(NO_RESPONSE_PREFIX):
            quiet += 1

    def pct(v):
        return "-" if v is None else "{}%".format(v)

    parts = [("{} win rate: ".format(_month_name_of(prev)), False), (pct(a), True), (" on {}, ".format(_day(day1)), False),
             (pct(b), True), (" on {}".format(_day(day2)), False)]
    if lost:
        parts += [("; ", False), (str(len(lost)), True), (" lost since, ", False), (str(quiet), True),
                  (" of them No response", False)]
    return parts


FLOW_TOP, FLOW_HEIGHT, FLOW_GAP = 24.0, 118.8, 6.0
LEFT_X, RIGHT_X, BLOCK_W = 150, 330, 9


def _blocks(counts, unit) -> dict:
    """{state: (top, height)} down the column, a gap between states."""
    out, y = {}, FLOW_TOP
    for k, _, _ in STATES:
        h = counts.get(k, 0) * unit
        out[k] = (y, h)
        y += h + FLOW_GAP
    return out


def _labels(blocks, counts, x, anchor) -> list:
    """The labels beside a column, at their block's middle, pushed apart."""
    ys, out = [], []
    biggest = max(counts.values()) if counts else 0
    for k, word, _ in STATES:
        if not counts.get(k):
            continue
        top, h = blocks[k]
        ys.append([top + h / 2 + 3.5, k, word])
    for i in range(1, len(ys)):
        ys[i][0] = max(ys[i][0], ys[i - 1][0] + 11.5)
    for y, k, word in ys:
        out.append('<text x="{:.1f}" y="{:.1f}" {}>{} {}</text>'.format(
            x, y, _font(10.5, INK, anchor, "600" if counts[k] == biggest else "normal"), counts[k], word))
    return out


def flow_svg(first, second, flows, day1, day2) -> str:
    total = max(sum(first.values()), 1)
    unit = (FLOW_HEIGHT - 3 * FLOW_GAP) / total
    left, right = _blocks(first, unit), _blocks(second, unit)
    out = ['<svg viewBox="0 0 560 149" width="560" height="149" role="img">']
    lpos = {k: left[k][0] for k, _, _ in STATES}
    rpos = {k: right[k][0] for k, _, _ in STATES}
    colour = {k: c for k, _, c in STATES}
    a_x, b_x, mid = LEFT_X + BLOCK_W, RIGHT_X, (LEFT_X + BLOCK_W + RIGHT_X) / 2
    for a, _, _ in STATES:
        for b, _, _ in STATES:
            n = flows.get((a, b), 0)
            if not n:
                continue
            h = n * unit
            y1, y2 = lpos[a], rpos[b]
            lpos[a] += h
            rpos[b] += h
            out.append('<path d="M{ax},{y1:.1f} C{m},{y1:.1f} {m},{y2:.1f} {bx},{y2:.1f} L{bx},{y2h:.1f} C{m},{y2h:.1f} '
                       '{m},{y1h:.1f} {ax},{y1h:.1f} Z" fill="{c}" fill-opacity="{o}"/>'.format(
                           ax=a_x, bx=b_x, m=mid, y1=y1, y2=y2, y1h=y1 + h, y2h=y2 + h, c=colour[b],
                           o="0.16" if a == b else "0.42"))
    for blocks, x in ((left, LEFT_X), (right, RIGHT_X)):
        for k, _, c in STATES:
            top, h = blocks[k]
            if h:
                out.append('<rect x="{}" y="{:.1f}" width="{}" height="{:.1f}" fill="{}"/>'.format(x, top, BLOCK_W, h, c))
    out += _labels(left, first, LEFT_X - 7, "end")
    out += _labels(right, second, RIGHT_X + BLOCK_W + 7, "start")
    out.append('<text x="{:.1f}" y="12.0" {}>{}</text>'.format(LEFT_X + BLOCK_W / 2, _font(11, INK, "middle", "600"),
                                                               day1))
    out.append('<text x="{:.1f}" y="12.0" {}>{}</text>'.format(RIGHT_X + BLOCK_W / 2, _font(11, INK, "middle", "600"),
                                                               day2))
    out.append("</svg>")
    return "\n".join(out)


def bar_svg(counts) -> str:
    """This month's cases in the four states on the day, one bar."""
    parts = [(k, counts.get(k, 0), c) for k, _, c in STATES if counts.get(k, 0)]
    total = sum(n for _, n, _ in parts) or 1
    width = 552 - 2 * max(len(parts) - 1, 0)
    out = ['<svg viewBox="0 0 560 20" width="560" height="20" role="img">']
    x = 0.0
    for i, (k, n, c) in enumerate(parts):
        w = width * n / total
        if i == len(parts) - 1:
            r = min(4.0, w / 2)
            out.append('<path d="M{x:.1f},2 H{xr:.1f} a{r:.1f},{r:.1f} 0 0 1 {r:.1f},{r:.1f} V{yb:.1f} '
                       'a{r:.1f},{r:.1f} 0 0 1 -{r:.1f},{r:.1f} H{x:.1f} Z" fill="{c}"/>'.format(
                           x=x, xr=x + w - r, r=r, yb=18 - r, c=c))
        else:
            out.append('<path d="M{:.1f},2 H{:.1f} V18 H{:.1f} Z" fill="{}"/>'.format(x, x + w, x, c))
        if w >= 14:
            out.append('<text x="{:.1f}" y="14.0" {}>{}</text>'.format(
                x + w / 2, _font(10, "#fff" if k in ("won", "lost") else INK, "middle", "600"), n))
        x += w + 2
    out.append("</svg>")
    return "\n".join(out)


# ---------------------------------------------------------------- entry --
def build(conn, month, today=None) -> tuple:
    """([the four cards], dense)."""
    day = monthly.close_day(month, today)             # 17j: the outcomes on the closing day
    today = today or dt.date.today()
    cards = [what_we_win(conn, month, day), why_we_lost(conn, month, day), by_country(conn, month),
             moved(conn, month, today)]
    return cards, text_px(cards) > DENSE_PX


def wrapped(text, chars) -> int:
    """How many lines the text takes at about this many characters a line."""
    lines, used = 1, 0
    for word in text.split():
        if used and used + 1 + len(word) > chars:
            lines, used = lines + 1, len(word)
        else:
            used += (1 if used else 0) + len(word)
    return lines


def _lines(card, chars) -> int:
    from .monthly_analysis import plain
    if card.empty:
        return 1
    return sum(wrapped(plain(b), chars) for b in card.bullets)


#: the cards' text width in characters of 11.5 px (Segoe UI; from the live
#: copy: a 63-character bullet is one line in card 16, 89 in card 18)
CHARS = {"15": 108, "16": 64, "17": 68, "18": 100}
#: section 15's charts share this width (the card less its padding and gaps)
DOTS_PX = 571


def text_px(cards) -> float:
    """An estimate of page 5's two rows: the dots scale down to fit the card
    (118 px high at full size), a bullet line is 16 px, the donut 118 px,
    section 17's chart and legend 154 px, section 18's flow, line and bar 204."""
    size, lost, country, move = cards
    widths = size.chart.get("widths") or []
    scale = min(1.0, DOTS_PX / sum(widths)) if widths else 0
    row1 = max(118 * scale + 20 + 16 * _lines(size, CHARS["15"]), 126 + 16 * _lines(lost, CHARS["16"]))
    row2 = max(154 + 16 * _lines(country, CHARS["17"]), 204 + 16 * _lines(move, CHARS["18"]))
    return row1 + row2
