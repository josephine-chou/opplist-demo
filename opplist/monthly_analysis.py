"""The month report's analysis page 4, "Analysis · Agents" (batch 17f, 2026-10-12; sample v16 page 4): three cards built from the
numbers, nothing typed by hand.

    12  LATAM agent base           the month and the four before: the LATAM
                                   agents with a request (section 4's
                                   Active), new or returning, the lapsed
                                   ones (quiet_agents), and of the month's
                                   agents those with one of that month's
                                   requests Won by its closing day (17l)
    13  LATAM requests by agent    the month's LATAM agent requests by agent
    14  Where the growth came from the month against the last by the four
                                   groups of sections 10 and 11

"LATAM agent requests" are the month's cases of the report's base (CASES)
whose requester is an Agent with v_enquiry.latam_agent 'Y'. A bullet is a
list of (text, bold) parts, so the page can set the numbers in bold and
the xlsx and the text read it plain.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field

from . import branding, monthly
from .monthly import BASE, CASES, FOLD, FOUR, GROUP, Row, Table

#: one LATAM agent's request
LATAM_AGENT = "p.type = 'Agent' AND e.latam_agent = 'Y'"
#: section 12: the month and the four before
BASE_MONTHS = 5
RETURNING = "#e8456c"
NEW = "#f6b3c2"
INK = "#0B0B0B"
GREY = "#52514E"
FAINT = "#898781"
#: section 13: bars for agents with two or more, at most this many
AGENT_BARS = 6
#: the three countries with the most LATAM agent requests over three months
COUNTRY_COLOURS = ("#2a78d6", "#eb6834", "#1baf7a")
OTHERS_COLOUR = "#c3c2b7"
#: names in a bullet, at most
NEW_NAMES = 6
ONE_EACH_NAMES = 8
#: 17f, Iris 10/12: the lapsed and the awarded lists stop at six too
LAPSED_NAMES = 6
AWARDED_NAMES = 6
#: page 4 is tightened (class dense) past this estimate of the cards' text
#: height, in px (see dense()); September 2026 on the live copy is 245 at
#: 762 px of the page's 794 (Edge, 2026-10-12)
DENSE_PX = 270
#: section 14
BEFORE_COLOUR = "#c9cdd2"
NOW_COLOUR = "#2a78d6"
#: 17i: section 14's office bullet - an office whose requests changed by
#: this much or more is named (else the changes of 1), at most this many a side
OFFICE_MOVE = 2
OFFICE_NAMES = 3
OFFICES = "Our offices"
#: section 13's label column (px, the name right-aligned at its end)
LABEL_PX = 116
FONT = "system-ui,-apple-system,Segoe UI,sans-serif"


@dataclass
class Card:
    number: str
    title: str
    big: str = ""
    caption: str = ""
    pill: tuple | None = None          # (text, 'up' / 'down' / 'flat')
    svg: str = ""
    legend: list = field(default_factory=list)     # (colour, label)
    bullets: list = field(default_factory=list)    # [[(text, bold)]]
    empty: str = ""                    # one line in place of the chart and the bullets
    chart: dict = field(default_factory=dict)      # section 12: base_svg's arguments
    table: Table | None = None         # the xlsx and the text


def plain(bullet) -> str:
    return "".join(text for text, _ in bullet)


def _abbr(month) -> str:
    return calendar.month_abbr[int(month[5:7])]


def _name(month) -> str:
    return calendar.month_name[int(month[5:7])]


def _months_back(month, n) -> list:
    """n months ending with month, oldest first."""
    out = [month]
    while len(out) < n:
        out.insert(0, monthly.previous_month(out[0]))
    return out


def _plural(n, one, many) -> str:
    return one if n == 1 else many


def _signed(n) -> str:
    return "{:+d}".format(n) if n else "0"


def _names(names, most) -> str:
    if len(names) <= most:
        return ", ".join(names)
    return "{} and {} more".format(", ".join(names[:most]), len(names) - most)


def _pill(delta, text):
    if delta > 0:
        return ("▲ " + text, "up")
    if delta < 0:
        return ("▼ " + text, "down")
    return (text, "flat")


def _font(size, colour, anchor, weight="normal") -> str:
    return 'font-size="{}" fill="{}" text-anchor="{}" font-weight="{}" font-family="{}"'.format(
        size, colour, anchor, weight, FONT)


def _num(v) -> str:
    """A coordinate: one decimal, '110.0' kept as the sample writes it."""
    return "{:.1f}".format(v)


# ------------------------------------------------------ the agent sets --
def latam_agents(conn, month) -> dict:
    """{party id: name} of the LATAM agents with a request in the month."""
    return {r["pid"]: r["agent"] or "" for r in conn.execute(
        "SELECT DISTINCT e.requester_party_id AS pid, e.requester_key AS agent {base} WHERE e.month = ? "
        "AND {cases} AND {who}".format(base=BASE, cases=CASES, who=LATAM_AGENT), (month,))}


def _first_month(conn, party_id) -> str:
    """The month of the party's first request ever (section 4's New)."""
    first = conn.execute("SELECT MIN(received_on) FROM enquiry WHERE requester_party_id = ? "
                         "AND COALESCE(received_on, '') <> ''", (party_id,)).fetchone()[0]
    return (first or "")[:7]


def _sorted(names) -> list:
    return sorted(names, key=lambda s: (s or "").casefold())


def awarded_by(conn, day) -> dict:
    """{party id: name} of the LATAM agents with a case of the base that is
    Won now and whose (first live) Won event is dated on or before day."""
    return {r["pid"]: r["agent"] or "" for r in conn.execute(
        "SELECT DISTINCT e.requester_party_id AS pid, e.requester_key AS agent {base} WHERE {cases} AND {who} "
        "AND e.status = 'Won' AND (SELECT MIN(s.at) FROM status_event s WHERE s.enquiry_id = e.id "
        "AND s.kind = 'status' AND s.status = 'Won' AND s.voided = 0) <= ?".format(
            base=BASE, cases=CASES, who=LATAM_AGENT), (day,))}


def awarded_in(conn, month, day) -> dict:
    """17l: {party id: name} of the LATAM agents with a request in the
    month (latam_agents) one of whose requests of that month is Won on
    day, the month's closing day (17j's won_on)."""
    return {r["pid"]: r["agent"] or "" for r in conn.execute(
        "SELECT DISTINCT e.requester_party_id AS pid, e.requester_key AS agent {base} WHERE e.month = ? "
        "AND {cases} AND {who} AND {won}".format(base=BASE, cases=CASES, who=LATAM_AGENT,
                                                  won=monthly.won_on(day)), (month,))}


def _month_end(month, today) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    last = dt.date(year, mon, calendar.monthrange(year, mon)[1])
    return min(last, today).isoformat()


# ------------------------------------------------- 12. LATAM agent base --
#: 17l: the black line and the table's last column, per month
AWARDED_LEGEND = "Awarded us that month"


def agent_base(conn, month, today=None) -> Card:
    today = today or dt.date.today()
    months = _months_back(month, BASE_MONTHS)
    cols = []
    for m in months:
        active = latam_agents(conn, m)
        new = {pid: name for pid, name in active.items() if _first_month(conn, pid) == m}
        cols.append({"month": m, "active": active, "new": new,
                     "lapsed": monthly.quiet_agents(conn, m),
                     "won": awarded_in(conn, m, monthly.close_day(m, today))})
    # the bullet stays cumulative: the agents that have ever awarded us
    for c in cols[-2:]:
        c["awarded"] = awarded_by(conn, _month_end(c["month"], today))
    now, before = cols[-1], cols[-2]
    name, prev = _name(month), _name(months[-2])
    card = Card("12", "LATAM agent base", big=str(len(now["active"])),
                caption="LATAM agents sent a request in {}".format(name))
    delta = len(now["active"]) - len(before["active"])
    card.pill = _pill(delta, "{} vs {}".format(abs(delta) if delta else 0, _abbr(months[-2])))
    card.chart = {"labels": [_abbr(c["month"]) for c in cols],
                  "returning": [len(c["active"]) - len(c["new"]) for c in cols],
                  "new": [len(c["new"]) for c in cols], "lapsed": [len(c["lapsed"]) for c in cols],
                  "awarded": [len(c["won"]) for c in cols]}
    card.svg = base_svg(**card.chart)
    card.legend = [(RETURNING, "Returning"), (NEW, "New this month"),
                   (INK, AWARDED_LEGEND)]
    new_names = _sorted(now["new"].values())
    if new_names:
        card.bullets.append([(str(len(new_names)), True), (" first-time {}: {}".format(
            _plural(len(new_names), "agent", "agents"), _names(new_names, NEW_NAMES)), False)])
    else:
        card.bullets.append([("No first-time agent in {}".format(name), False)])
    lapsed = now["lapsed"]
    if lapsed:
        card.bullets.append([(str(len(lapsed)), True), (" {} {} sent nothing in {}: {}".format(
            prev, _plural(len(lapsed), "agent", "agents"), name, _names(lapsed, LAPSED_NAMES)), False)])
    else:
        card.bullets.append([("Every {} agent sent a request again".format(prev), False)])
    ever = _sorted(now["awarded"].values())
    fresh = _sorted(name_ for pid, name_ in now["awarded"].items() if pid not in before["awarded"])
    card.bullets.append([("Agents that have ever awarded us: ", False), (str(len(ever)), True),
                         ((" ({})".format(_names(ever, AWARDED_NAMES)) if ever else ""), False),
                         ("; new in {}: {}".format(name, _names(fresh, AWARDED_NAMES)) if fresh
                          else "; no new one in {}".format(name), False)])
    card.table = Table(["Month", "Active", "Returning", "New", "Lapsed", AWARDED_LEGEND],
                       [Row(_name(c["month"]), [str(len(c["active"])), str(len(c["active"]) - len(c["new"])),
                                                str(len(c["new"])), str(len(c["lapsed"])), str(len(c["won"]))])
                        for c in cols])
    return card


def _step(peak) -> int:
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500):
        if -(-peak // step) <= 3:
            return step
    return 1000


def _top_rounded(x0, x1, bottom, top) -> str:
    r = min(4.0, (bottom - top) / 2, (x1 - x0) / 2)
    return ("M{x0},{b} V{yr} a{r},{r} 0 0 1 {r},{nr} H{xr} a{r},{r} 0 0 1 {r},{r} V{b} Z").format(
        x0=x0, b=_num(bottom), yr=_num(top + r), r=_num(r), nr=_num(-r), xr=_num(x1 - r))


def base_svg(labels, returning, new, lapsed, awarded, compact=False) -> str:
    """Section 12's columns, returning below and new above with a 2 px gap,
    the total over each, 'n lapsed' under the month; the awarded line."""
    w, left, right, top, bottom = 979, 28, 953, 20.0, 110.0
    height = 146
    if compact:                         # a dense page 4: 26 px lower
        top, bottom, height = 18.0, 84.0, 120
    totals = [a + b for a, b in zip(returning, new)]
    peak = max([1] + totals + awarded)
    step = _step(peak)
    ymax = -(-peak // step) * step
    unit = (bottom - top) / ymax
    pitch = (right - left) / len(labels)
    out = ['<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img">'.format(w=w, h=height)]
    tick = 0
    while tick <= ymax:
        y = bottom - tick * unit
        out.append('<line x1="{}" y1="{:.1f}" x2="{}" y2="{:.1f}" stroke="#E1E0D9" stroke-width="1"/>'.format(
            left, y, right, y))
        out.append('<text x="22.0" y="{:.1f}" {}>{}</text>'.format(y + 3.5, _font(10, GREY, "end"), tick))
        tick += step
    xs = []
    for i, label in enumerate(labels):
        cx = left + pitch * (i + 0.5)
        xs.append(cx)
        x0, x1 = cx - 14, cx + 14
        total = totals[i]
        top_y = bottom - total * unit
        if new[i] and returning[i]:
            split = bottom - returning[i] * unit
            if split + 2 < bottom - 0.5:
                out.append('<path d="M{x0},{b} V{s} H{x1} V{b} Z" fill="{c}"/>'.format(
                    x0=x0, x1=x1, b=_num(bottom), s=_num(split + 2), c=RETURNING))
            out.append('<path d="{}" fill="{}"/>'.format(_top_rounded(x0, x1, split, top_y), NEW))
        elif total:
            out.append('<path d="{}" fill="{}"/>'.format(_top_rounded(x0, x1, bottom, top_y),
                                                         NEW if new[i] else RETURNING))
        out.append('<text x="{:.1f}" y="{:.1f}" {}>{}</text>'.format(cx, top_y - 6, _font(10.5, INK, "middle", "600"),
                                                                      total))
        out.append('<text x="{:.1f}" y="{:.1f}" {}>{}</text>'.format(cx, bottom + 15, _font(10.5, GREY, "middle"), label))
        if lapsed[i]:
            out.append('<text x="{:.1f}" y="{:.1f}" {}>{} lapsed</text>'.format(cx, bottom + 28, _font(9.5, FAINT, "middle"),
                                                                               lapsed[i]))
    points = [(x, bottom - v * unit) for x, v in zip(xs, awarded)]
    out.append('<polyline points="{}" fill="none" stroke="{}" stroke-width="2" stroke-linejoin="round" '
               'stroke-linecap="round"/>'.format(" ".join("{:.1f},{:.1f}".format(x, y) for x, y in points), INK))
    for x, y in points:
        out.append('<circle cx="{:.1f}" cy="{:.1f}" r="4" fill="{}" stroke="#fff" stroke-width="2"/>'.format(x, y, INK))
    x, y = points[-1]
    out.append('<text x="{:.1f}" y="{:.1f}" {}>{}</text>'.format(x + 18, y + 4, _font(10.5, INK, "start", "600"),
                                                                 awarded[-1]))
    out.append("</svg>")
    return "\n".join(out)


# --------------------------------------------- 13. LATAM requests by agent --
def _by_agent(conn, month, day=None) -> list:
    """[(name, country, n, won)] of the month's LATAM agent requests, most
    first, ties by name; won on the closing day (17j)."""
    rows = conn.execute(
        "SELECT e.requester_party_id AS pid, e.requester_key AS agent, COALESCE(NULLIF(TRIM(p.country), ''), 'Unknown') "
        "AS country, COUNT(*) AS n, SUM({won}) AS won {base} WHERE e.month = ? AND {cases} AND {who} "
        "GROUP BY pid".format(won=monthly.won_on(day or monthly.close_day(month)), base=BASE, cases=CASES,
                              who=LATAM_AGENT), (month,)).fetchall()
    out = [(r["agent"] or "", r["country"], r["n"], r["won"] or 0) for r in rows]
    return sorted(out, key=lambda t: (-t[2], t[0].casefold()))


def _by_country(conn, months) -> dict:
    """{country: n} of the LATAM agent requests over the months."""
    marks = ", ".join("?" * len(months))
    return {r["country"]: r["n"] for r in conn.execute(
        "SELECT COALESCE(NULLIF(TRIM(p.country), ''), 'Unknown') AS country, COUNT(*) AS n {base} "
        "WHERE e.month IN ({marks}) AND {cases} AND {who} GROUP BY country".format(
            base=BASE, marks=marks, cases=CASES, who=LATAM_AGENT), tuple(months))}


# Segoe UI's advance widths in em, roughly: enough to tell a name that
# overruns the 116 px label column at 11 px
_NARROW = set("iljtfrI.,;:'|!() ")
_WIDE = set("mwMW@")


def _text_px(text, size) -> float:
    em = 0.0
    for ch in text:
        if ch in _NARROW:
            em += 0.29
        elif ch in _WIDE:
            em += 0.86
        elif ch.isupper() or ch.isdigit():
            em += 0.64
        else:
            em += 0.53
    return em * size


def fit(text, px=LABEL_PX, size=11) -> str:
    """The name, or as much of it as fits followed by '…'."""
    if _text_px(text, size) <= px:
        return text
    while text and _text_px(text + "…", size) > px:
        text = text[:-1]
    return text.rstrip() + "…"


def agent_requests(conn, month, today=None) -> Card:
    name = _name(month)
    card = Card("13", "LATAM requests by agent")
    agents = _by_agent(conn, month, monthly.close_day(month, today))
    card.table = Table(["Agent", "Country", "Requests"], [Row(a, [c, str(n)]) for a, c, n, _ in agents])
    if not agents:
        card.empty = "No LATAM agent request in {}.".format(name)
        return card
    total = sum(n for _, _, n, _ in agents)
    wins = sum(w for _, _, _, w in agents)
    first = agents[0]
    card.big = "{}%".format(round(100.0 * first[2] / total))
    card.caption = "of {}’s {} LATAM requests from one agent, {}".format(name, total, first[0])
    three = _by_country(conn, _months_back(month, 3))
    top = sorted(three, key=lambda c: (-three[c], c.casefold()))[:3]
    colour = {c: COUNTRY_COLOURS[i] for i, c in enumerate(top)}
    shown = [a for a in agents if a[2] >= 2][:AGENT_BARS]
    rest = agents[len(shown):]
    bars = [(a, n, colour.get(c, OTHERS_COLOUR)) for a, c, n, _ in shown]
    if rest:
        label = "{} more, one each".format(len(rest)) if all(n == 1 for _, _, n, _ in rest) \
            else "{} more agents".format(len(rest))
        bars.append((label, sum(n for _, _, n, _ in rest), OTHERS_COLOUR))
    card.svg = agents_svg(bars)
    used = {col for _, _, col in bars}
    card.legend = [(colour[c], c) for c in top if colour[c] in used]
    if OTHERS_COLOUR in used:
        card.legend.append((OTHERS_COLOUR, "Others"))
    lead = [(first[0] + ": ", False), (str(first[2]), True), (" of {} requests".format(total), False)]
    if wins and not first[3]:
        # 17g: none of the wins was the first agent's
        lead += [("; the ", False), (str(wins), True), (" LATAM agent {} in {} went to {}".format(
            _plural(wins, "win", "wins"), name, _plural(wins, "another agent", "other agents")), False)]
    elif wins:
        lead += [(", and ", False)] + ([("all ", False), (str(wins), True)] if first[3] == wins else
                                        [(str(first[3]), True), (" of the {}".format(wins), False)])
        lead.append((" LATAM agent {} in {}".format(_plural(wins, "win", "wins"), name), False))
    card.bullets.append(lead)
    nxt = agents[1:6]
    if nxt:
        n = sum(t[2] for t in nxt)
        card.bullets.append([("The next {}: ".format("five" if len(nxt) == 5 else
                                                       ("one" if len(nxt) == 1 else len(nxt))), False),
                             (str(n), True), (" {} ({}%)".format(_plural(n, "request", "requests"),
                                                                 round(100.0 * n / total)), False)])
    ones = [a for a, _, n, _ in agents if n == 1]
    if ones:
        card.bullets.append([(str(len(ones)), True), (" {}: {}".format(
            "agent sent one" if len(ones) == 1 else "agents sent one each", _names(ones, ONE_EACH_NAMES)), False)])
    return card


def agents_svg(bars) -> str:
    """Section 13's bars: the name, the bar in its country's colour, the count."""
    pitch, x0, span = 19, 128, 272
    peak = max(n for _, n, _ in bars) or 1
    height = pitch * len(bars) - 3
    out = ['<svg viewBox="0 0 470 {h}" width="470" height="{h}" role="img">'.format(h=height)]
    for i, (label, n, colour) in enumerate(bars):
        y = 2 + pitch * i
        out.append('<text x="120.0" y="{:.1f}" {}>{}</text>'.format(y + 10, _font(11, INK, "end"), _esc(fit(label))))
        end = x0 + span * n / peak
        r = min(4.0, (end - x0) / 2)
        out.append('<path d="M{x0},{y} H{xr} a{r},{r} 0 0 1 {r},{r} V{yb} a{r},{r} 0 0 1 -{r},{r} H{x0} Z" '
                   'fill="{c}"/>'.format(x0=x0, y=y, xr=_num(end - r), r=_num(r), yb=_num(y + 12 - r), c=colour))
        out.append('<text x="{:.1f}" y="{:.1f}" {}>{}</text>'.format(
            end + 6, y + 10, _font(10.5, INK, "start", "600" if i == 0 else "normal"), n))
    out.append("</svg>")
    return "\n".join(out)


def _esc(text) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------- 14. where the growth came from --
def _by_group(conn, month) -> dict:
    counts = {name: 0 for name, _ in FOUR}
    for r in conn.execute("SELECT {grp} AS grp, COUNT(*) AS n {base} WHERE e.month = ? AND {cases} "
                          "GROUP BY grp".format(grp=GROUP, base=BASE, cases=CASES), (month,)):
        counts[FOLD[r["grp"]]] += r["n"]
    return counts


def _by_office(conn, month) -> dict:
    """17i: {party key: requests} of the Our offices in the month."""
    return {r["office"]: r["n"] for r in conn.execute(
        "SELECT p.key AS office, COUNT(*) AS n {base} WHERE e.month = ? AND {cases} "
        "AND p.type = 'Office' GROUP BY p.id".format(base=BASE, cases=CASES), (month,))}


def office_short(key) -> str:
    """'JoJo JJZ' -> 'SZX'; a key without 'JoJo ' in front as it is."""
    key = key or ""
    return branding.office_short(key) or key


def _office_moves(then, now) -> tuple:
    """([(office, change)] risers, [...] fallers): the changes of
    OFFICE_MOVE or more, else of 1; the biggest first, then by name."""
    moves = [(o, now.get(o, 0) - then.get(o, 0)) for o in set(then) | set(now)]
    shown = [m for m in moves if abs(m[1]) >= OFFICE_MOVE] or [m for m in moves if abs(m[1]) == 1]
    order = sorted(shown, key=lambda m: (-abs(m[1]), office_short(m[0]).casefold()))
    return ([m for m in order if m[1] > 0][:OFFICE_NAMES], [m for m in order if m[1] < 0][:OFFICE_NAMES])


def _office_bullet(conn, month, a, b, then, now, lead) -> list:
    """17i: 'Our offices a -> b: SZX +3, LAX +2; TFU -3; 6 on LATAM
    lanes' - a -> b left out when the offices lead the card; an empty part
    with its '; ', and the ':' when nothing follows; [] when nothing is left."""
    risers, fallers = _office_moves(then, now)
    latam = conn.execute("SELECT COUNT(*) {base} WHERE e.month = ? AND {cases} AND p.type = 'Office' "
                         "AND e.latam_lane = 'Y'".format(base=BASE, cases=CASES), (month,)).fetchone()[0]
    parts = []
    for side in (risers, fallers):
        if side:
            part = []
            for i, (office, d) in enumerate(side):
                part += [((", " if i else "") + office_short(office) + " ", False), (_signed(d), True)]
            parts.append(part)
    if latam:
        parts.append([(str(latam), True), (" on LATAM lanes", False)])
    if lead and not parts:
        return []
    bullet = [(OFFICES, False)] if lead else [(OFFICES + " ", False), (str(a), True), (" → ", False), (str(b), True)]
    for i, part in enumerate(parts):
        bullet += [("; " if i else ": ", False)] + part
    return bullet


def growth(conn, month) -> Card:
    prev_month = monthly.previous_month(month)
    name, prev = _name(month), _name(prev_month)
    now, before = _by_group(conn, month), _by_group(conn, prev_month)
    groups = [(g, before[g], now[g]) for g, _ in FOUR if before[g] or now[g]]
    total, total_before = sum(now.values()), sum(before.values())
    card = Card("14", "Where the growth came from", big=str(total), caption="requests in {}".format(name))
    if total_before:
        delta = total - total_before
        card.pill = _pill(delta, "{}% vs {} ({})".format(abs(round(100.0 * delta / total_before)),
                                                         _abbr(prev_month), total_before))
    card.table = Table(["Group", prev, name, "Change"],
                       [Row(g, [str(a), str(b), _signed(b - a)]) for g, a, b in groups])
    # 17i: every office with requests in either month, after the groups
    office_now, office_then = _by_office(conn, month), _by_office(conn, prev_month)
    offices = sorted(set(office_now) | set(office_then),
                     key=lambda o: (-office_now.get(o, 0), -office_then.get(o, 0), o.casefold()))
    if offices:
        card.table.rows.append(Row(OFFICES, [], "head"))
        card.table.rows += [Row(o, [str(office_then.get(o, 0)), str(office_now.get(o, 0)),
                                    _signed(office_now.get(o, 0) - office_then.get(o, 0))]) for o in offices]
    if not groups:
        card.empty = "No requests in {} or {}.".format(prev, name)
        return card
    card.svg = growth_svg(groups)
    card.legend = [(BEFORE_COLOUR, prev), (NOW_COLOUR, name)]
    order = {g: i for i, (g, _) in enumerate(FOUR)}
    lead = sorted(groups, key=lambda t: (-abs(t[2] - t[1]), order[t[0]]))[0]
    g, a, b = lead
    bullet = [(g + " ", False), (str(a), True), (" → ", False), (str(b), True)]
    up = total - total_before
    if up > 0 and b > a:
        bullet += [(", ", False), ("{}%".format(round(100.0 * (b - a) / up)), True),
                   (" of the +{}".format(up), False)]
    card.bullets.append(bullet)
    then, so = _by_country(conn, [prev_month]), _by_country(conn, [month])
    if then or so:
        moved = [(c, so.get(c, 0) - then.get(c, 0)) for c in set(then) | set(so)]
        moved = sorted([m for m in moved if m[1]], key=lambda m: (-abs(m[1]), -m[1], m[0].casefold()))[:3]
        if moved:
            parts = []
            for i, (c, d) in enumerate(moved):
                parts += [((", " if i else "") + c + " ", False), (_signed(d), True)]
            card.bullets.append(parts)
    # 17i: the offices have a bullet of their own, the last one
    others = [t for t in groups if t[0] not in (g, OFFICES)]
    if others:
        parts = []
        for i, (other, a, b) in enumerate(others):
            parts += [((", " if i else "") + other + " ", False), (_signed(b - a), True)]
        card.bullets.append(parts)
    if offices:
        a, b = before[OFFICES], now[OFFICES]
        bullet = _office_bullet(conn, month, a, b, office_then, office_now, lead=g == OFFICES)
        if bullet:
            card.bullets.append(bullet)
    return card


def growth_svg(groups) -> str:
    """Section 14's pairs of bars: the previous month above, the month below,
    their counts at their ends, the change at the right."""
    pitch, x0, span = 35, 118, 214
    peak = max([1] + [max(a, b) for _, a, b in groups])
    height = pitch * len(groups) - 9
    out = ['<svg viewBox="0 0 400 {h}" width="400" height="{h}" role="img">'.format(h=height)]

    def bar(y, n, colour, weight, ink):
        if not n:
            out.append('<text x="126.0" y="{:.1f}" {}>0</text>'.format(y + 9, _font(10.5, ink, "start", weight)))
            return
        end = x0 + span * n / peak
        r = min(4.0, (end - x0) / 2)
        out.append('<path d="M{x0},{y} H{xr} a{r},{r} 0 0 1 {r},{r} V{yb} a{r},{r} 0 0 1 -{r},{r} H{x0} Z" '
                   'fill="{c}"/>'.format(x0=x0, y=y, xr=_num(end - r), r=_num(r), yb=_num(y + 10 - r), c=colour))
        out.append('<text x="{:.1f}" y="{:.1f}" {}>{}</text>'.format(end + 5, y + 9, _font(10.5, ink, "start", weight),
                                                                     n))

    for i, (g, a, b) in enumerate(groups):
        top = 2 + pitch * i
        out.append('<text x="108.0" y="{:.1f}" {}>{}</text>'.format(top + 15, _font(11.5, INK, "end"), _esc(g)))
        bar(top, a, BEFORE_COLOUR, "normal", GREY)
        bar(top + 12, b, NOW_COLOUR, "600", INK)
        out.append('<text x="396.0" y="{:.1f}" {}>{}</text>'.format(top + 15, _font(12, INK, "end", "600"),
                                                                    _signed(b - a)))
    out.append("</svg>")
    return "\n".join(out)


# ---------------------------------------------------------------- entry --
def build(conn, month, today=None) -> tuple:
    """([the three cards], dense): dense tightens page 4 and draws section
    12's chart lower."""
    cards = [agent_base(conn, month, today), agent_requests(conn, month, today), growth(conn, month)]
    tight = dense(cards)
    if tight:
        cards[0].svg = base_svg(compact=True, **cards[0].chart)
    return cards, tight


def _lines(card, chars) -> int:
    return sum(-(-len(plain(b)) // chars) for b in card.bullets) + (1 if card.empty else 0)


def text_px(cards) -> int:
    """An estimate of the height page 4's text and bars take: a bullet line
    is 16 px (about 150 characters across the top card, 78 in card 13,
    66 in card 14), a bar of 13 19 px, a group of 14 35 px, and card 13's
    caption a line more when it is long."""
    base, agents, groups = cards
    low13 = 16 * (_lines(agents, 78) + (1 if len(agents.caption) > 68 else 0)) + 19 * agents.svg.count("<path")
    low14 = 16 * _lines(groups, 66) + 35 * groups.svg.count('x="108.0"')
    return 16 * _lines(base, 150) + max(low13, low14)


def dense(cards) -> bool:
    return text_px(cards) > DENSE_PX


def table_for(card) -> Table:
    """The card's table with its bullets after it as note rows."""
    table = Table(list(card.table.columns), list(card.table.rows))
    for line in ([card.empty] if card.empty else []) + [plain(b) for b in card.bullets]:
        table.rows.append(Row(line, [], "note"))
    return table
