"""The /dashboard page (2026-09-25): the pipeline at a glance for
Victor - six tiles, the pipeline donut, requests per week, quoted within
two days, who sends the requests, the LATAM map, top requesters, top
lanes and why we lost.

Everything is the report rule's cases (SC1, direct_customers_out:
scope In, an Agent, office or direct customer asked, the rule leaves it in;
another office's cases too), legacy excluded, rows without received_on
skipped - the report's base, the Dropped rows kept for the Dropped slice. The charts are inline SVG built here in
Python: no chart library, no script (the one in-app chart exception,
agreed on 2026-09-23). The metric
definitions are ported from export-preview\\dashboard_sample_build.py
as they are; working days are tracking.business_days_between, the one
counter the app has. No OPP number is printed anywhere on the page.
"""

from __future__ import annotations

import collections
import datetime as dt
import html
import json
import math
import sqlite3
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from . import dates, db, favicon, paths, tracking
from . import direct_customers_out as dco, status as status_mod

#: The map (760 x 640, conic equal-area): land paths and one anchor per
#: country, served from the app's static folder, never fetched.
MAP_FILE = Path(__file__).resolve().parent / "web" / "static" / "americas.json"

#: Our country names against the map's.
NAME_FIX = {"USA": "United States of America", "Dominican Republic": "Dominican Rep.",
            "Trinidad & Tobago": "Trinidad and Tobago"}

#: batch 21: direct customers are a group of their own, in the orange
#: Iris saw in report v8
GROUPS = ("LATAM agents", "Other agents", "Our offices", "Direct customers")
PERIODS = (("month", "This month"), ("90", "Last 90 days"))
OPEN = ("New", "Quoted")          # Sourcing retired 2026-09-24
#: A quote nobody answered for this many working days is 'older than'.
STALE_QUOTE_DAYS = 10

C = dict(ink="#1c1f23", muted="#6a7178", line="#d8dce0", panel="#f6f7f9",
         accent="#00b3ea", accent_text="#008fbb", navy="#002060",
         new="#9aa3ad", sourcing="#f2a93b", quoted="#00b3ea", won="#83cd48", lost="#b3405a",
         dropped="#c8ccd1", latam="#e8456c", other="#83cd48", office="#00b3ea", ok="#5aa32a",
         customer="#f2a93b")
GROUP_COLOURS = {"LATAM agents": C["latam"], "Other agents": C["other"], "Our offices": C["office"],
                 "Direct customers": C["customer"]}

esc = html.escape


# ------------------------------------------------------------- helpers ----
def bdays(start, end):
    """Working days between two ISO dates, Monday to Friday, no holidays -
    the same count tracking's rules use (a Monday receipt quoted on the
    Wednesday is 2; Friday to Tuesday is 2; Thursday to Tuesday is 3).
    None when a date is missing or the end is before the start."""
    if not start or not end:
        return None
    return tracking.business_days_between(start[:10], end[:10])


def group_of(row) -> str:
    """Who asked: one of our offices, a direct customer (batch 21), an agent
    in Latin America, or another agent (the reference's rule)."""
    if row.get("requester_type") == "Office":
        return "Our offices"
    if row.get("requester_type") == "Direct Customer":
        return "Direct customers"
    if row.get("latam_agent") == "Y":                 # v36: an Agent in a LATAM-list country
        return "LATAM agents"
    return "Other agents"


def _day_label(d: dt.date) -> str:
    return dates.month_day(d)


def _month_label(month: str) -> str:
    return dt.date.fromisoformat(month + "-01").strftime("%B")


def window(today: dt.date, period: str) -> dict:
    """The period tab's window and the one before it, both [start, end)
    in ISO dates, with the words the tiles use."""
    if period == "90":
        start = today - dt.timedelta(days=90)
        prev = start - dt.timedelta(days=90)
        return {"key": "90", "start": start.isoformat(), "end": (today + dt.timedelta(days=1)).isoformat(),
                "label": "last 90 days", "title": "Requests, last 90 days",
                "prev_start": prev.isoformat(), "prev_end": start.isoformat(),
                "prev_label": "previous 90 days", "closed_title": "Closed in the last 90 days",
                "mix_label": "last 90 days", "in_window": "in the window"}
    first = today.replace(day=1)
    prev_first = (first - dt.timedelta(days=1)).replace(day=1)
    nxt = (first + dt.timedelta(days=32)).replace(day=1)
    return {"key": "month", "start": first.isoformat(), "end": nxt.isoformat(),
            "label": "this month", "title": "Requests, {} to date".format(first.strftime("%B")),
            "prev_start": prev_first.isoformat(), "prev_end": first.isoformat(),
            "prev_label": "{} whole month".format(prev_first.strftime("%B")),
            "closed_title": "Closed this month", "mix_label": "this month", "in_window": "this month"}


def _in(value, start, end) -> bool:
    return bool(value) and start <= value[:10] < end


# --------------------------------------------------------------- data -----
#: SC1: the report rule, the Dropped rows kept (the Dropped slice)
COUNTED = dco.counted_sql("e", "p", with_dropped=True)
COUNTED_IDS = dco.counted_ids_sql(with_dropped=True)


def base_rows(conn: sqlite3.Connection, others: bool = False) -> list:
    """The report rule's cases (SC1), legacy excluded, a receipt date on
    the row; with others=True only the ones another office handles."""
    return [dict(r) for r in conn.execute(
        "SELECT e.* FROM v_enquiry e JOIN party p ON p.id = e.requester_party_id WHERE {} AND e.legacy = 0 "
        "AND e.received_on IS NOT NULL AND e.received_on <> '' {}ORDER BY e.received_on, e.id".format(
            COUNTED, "AND e.handled_by IS NOT NULL " if others else ""))]


def _route_rows(conn) -> list:
    """The report's base per route (v33): the cases of base_rows, a
    receipt date, with the case's quoted_on."""
    return [dict(r) for r in conn.execute(
        "SELECT r.*, e.quoted_on FROM v_route r JOIN v_enquiry e ON e.id = r.enquiry_id "
        "WHERE r.enquiry_id IN {} AND r.legacy = 0 "
        "AND r.received_on IS NOT NULL AND r.received_on <> '' ORDER BY r.received_on, r.enquiry_id, r.seq".format(
            COUNTED_IDS))]


def latam_countries(conn) -> set:
    """The LATAM list from the setting (USA is not in it), as the views read it."""
    return {r[0] for r in conn.execute("SELECT name FROM v_latam_country")}


def within_two(rows) -> tuple:
    """(median working days, share within 2 as a percentage, n) over the
    quoted rows given; (None, 0, 0) when none."""
    days = [bdays(r["received_on"], r["quoted_on"]) for r in rows if r.get("quoted_on")]
    days = [d for d in days if d is not None and d >= 0]
    if not days:
        return None, 0, 0
    within = sum(1 for d in days if d <= 2)
    return statistics.median(days), round(100 * within / len(days)), len(days)


def weeks_of(today: dt.date, count: int = 8) -> list:
    """The Mondays of the last `count` weeks, oldest first, this week last."""
    monday = today - dt.timedelta(days=today.weekday())
    return [monday - dt.timedelta(weeks=i) for i in range(count - 1, -1, -1)]


def _week_index(weeks, iso) -> int | None:
    d = dt.date.fromisoformat(iso[:10])
    for i, w in enumerate(weeks):
        if w <= d < w + dt.timedelta(days=7):
            return i
    return None


@dataclass
class Dashboard:
    today: str
    period: dict
    tiles: list = field(default_factory=list)          # [{label, big, sub, spark}]
    pipeline: dict = field(default_factory=dict)       # parts, on_us, on_them, closed, win_rate_90, won_90, lost_90
    weeks: list = field(default_factory=list)          # labels
    series: dict = field(default_factory=dict)         # group -> [n] * 8
    speed: list = field(default_factory=list)          # [(pct or None, n quoted)] per week
    mix: list = field(default_factory=list)            # [(group, n, colour)]
    requesters: list = field(default_factory=list)     # [{key, country, n, quoted, won}]
    lost: list = field(default_factory=list)           # [(reason, n)]
    map_rows: list = field(default_factory=list)       # [(country, {n, quoted, won})]
    map_since: str = ""
    lanes: list = field(default_factory=list)          # [{origin, dest, n, won}]
    schema_version: str = ""
    lost_note: str = ""
    others_line: str = ""                              # v31, under the tiles


def build(conn: sqlite3.Connection, today=None, period: str = "month") -> Dashboard:
    """Every number on the page, read-only, on the report's base."""
    today_d = dt.date.fromisoformat(today) if today else dt.date.today()
    today = today_d.isoformat()
    period = period if period in dict(PERIODS) else "month"
    win = window(today_d, period)
    d90 = (today_d - dt.timedelta(days=90)).isoformat()
    rows = base_rows(conn)
    dash = Dashboard(today=today, period=win, schema_version=str(db.get_setting(conn, "schema_version", "?")))

    by_status = collections.Counter(r["status"] for r in rows)
    open_rows = [r for r in rows if r["status"] in OPEN]
    # New is on our side, re-quote rounds included (they are New again)
    on_us = sum(1 for r in open_rows if r["status"] == "New")
    on_them = sum(1 for r in open_rows if r["status"] == "Quoted")

    in_win = [r for r in rows if _in(r["received_on"], win["start"], win["end"])]
    in_prev = [r for r in rows if _in(r["received_on"], win["prev_start"], win["prev_end"])]

    quoted_win = within_two([r for r in rows if _in(r.get("quoted_on"), win["start"], win["end"])])
    quoted_prev = within_two([r for r in rows if _in(r.get("quoted_on"), win["prev_start"], win["prev_end"])])

    closed_on = lambda r: r.get("closed_on") or ""
    won_win = sum(1 for r in rows if r["status"] == "Won" and _in(closed_on(r), win["start"], win["end"]))
    lost_win = sum(1 for r in rows if r["status"] == "Lost" and _in(closed_on(r), win["start"], win["end"]))
    cancelled_win = sum(1 for r in rows if r["status"] == "Cancelled" and _in(closed_on(r), win["start"], win["end"]))
    dropped_win = sum(1 for r in rows if r["status"] == "Dropped"
                      and _in(r.get("closed_on_any") or "", win["start"], win["end"]))
    won_90 = sum(1 for r in rows if r["status"] == "Won" and closed_on(r) >= d90)
    lost_90 = sum(1 for r in rows if r["status"] == "Lost" and closed_on(r) >= d90)
    win_rate_90 = round(100 * won_90 / (won_90 + lost_90)) if (won_90 + lost_90) else 0

    latam_win = sum(1 for r in in_win if r.get("latam") == "Y")
    latam_share = round(100 * latam_win / len(in_win)) if in_win else 0
    latam_won_90 = [r for r in rows if r["status"] == "Won" and closed_on(r) >= d90 and r.get("latam") == "Y"]
    chile_won_90 = sum(1 for r in latam_won_90 if "Chile" in (r.get("origin_country"), r.get("dest_country")))
    chile_share = round(100 * chile_won_90 / len(latam_won_90)) if latam_won_90 else 0

    monday = (today_d - dt.timedelta(days=today_d.weekday())).isoformat()
    fu_week = conn.execute(
        "SELECT COUNT(*) FROM status_event s JOIN enquiry e ON e.id = s.enquiry_id "
        "WHERE s.kind = 'followup' AND s.voided = 0 AND s.at >= ? AND e.legacy = 0 "
        "AND e.id IN {}".format(COUNTED_IDS),
        (monday,)).fetchone()[0]
    stale = sum(1 for r in open_rows if r["status"] == "Quoted"
                and (bdays(r.get("quoted_on") or r.get("quoted_on_any"), today) or 0) > STALE_QUOTE_DAYS)

    # weekly requests, three groups; response speed per week of receipt
    weeks = weeks_of(today_d)
    series = {g: [0] * len(weeks) for g in GROUPS}
    per_week = [[] for _ in weeks]
    for r in rows:
        i = _week_index(weeks, r["received_on"])
        if i is None:
            continue
        series[group_of(r)][i] += 1
        per_week[i].append(r)
    weekly_total = [sum(series[g][i] for g in GROUPS) for i in range(len(weeks))]
    speed = []
    for bucket in per_week:
        _, pct, n = within_two([r for r in bucket if r.get("quoted_on")])
        speed.append((pct if n else None, n))

    median, share, _n = quoted_win
    median_txt = "-" if median is None else "{:g}".format(median)
    dash.tiles = [
        {"label": win["title"], "big": str(len(in_win)),
         "sub": "{} <b>{}</b>".format(win["prev_label"], len(in_prev)), "spark": sparkline(weekly_total)},
        {"label": "Open now", "big": str(len(open_rows)),
         "sub": "on us <b>{}</b> &middot; on them <b>{}</b>".format(on_us, on_them), "spark": ""},
        {"label": "Quoted within 2 business days", "big": "{}%".format(share),
         "sub": "median {} day{} from receipt &middot; {} <b>{}%</b>".format(
             median_txt, "" if median_txt == "1" else "s", win["prev_label"].replace(" whole month", ""), quoted_prev[1]),
         "spark": ""},
        {"label": "Won {}".format(win["label"]), "big": str(won_win),
         "sub": "lost <b>{}</b>{} &middot; 90-day win rate <b>{}%</b>".format(
             lost_win, " &middot; cancelled <b>{}</b>".format(cancelled_win) if cancelled_win else "",
             win_rate_90), "spark": ""},
        {"label": "Quotes awaiting an answer", "big": str(on_them),
         "sub": "follow-ups sent this week <b>{}</b> &middot; older than {} business days <b>{}</b>".format(
             fu_week, STALE_QUOTE_DAYS, stale), "spark": ""},
        {"label": "LATAM share of requests", "big": "{}%".format(latam_share),
         "sub": "Chile is <b>{}%</b> of LATAM wins (90 days)".format(chile_share), "spark": ""},
    ]
    # SC1: of the period's requests, the ones another office handles
    dash.others_line = db.handled_elsewhere_line(
        r["handled_by"] for r in in_win if r.get("handled_by"))
    dash.pipeline = {
        "parts": [("New", by_status["New"], C["new"]), ("Quoted", by_status["Quoted"], C["quoted"])],
        "open": len(open_rows), "on_us": on_us, "on_them": on_them,
        # C1: Cancelled on its own, shown once there are any
        "closed": [("Won", won_win, C["won"]), ("Lost", lost_win, C["lost"])]
                  + ([("Cancelled", cancelled_win, C["dropped"])] if cancelled_win else [])
                  + [("Dropped", dropped_win, C["dropped"])],
        "closed_title": win["closed_title"],
        "win_rate_90": win_rate_90, "won_90": won_90, "lost_90": lost_90,
    }
    dash.weeks = [_day_label(w) for w in weeks]
    dash.series = series
    dash.speed = speed
    mix = collections.Counter(group_of(r) for r in in_win)
    dash.mix = [(g, mix[g], GROUP_COLOURS[g]) for g in GROUPS]

    dash.requesters = [dict(r) for r in conn.execute(
        "SELECT requester_key AS key, requester_country AS country, COUNT(*) AS n, "
        "MAX(requester_type = 'Direct Customer') AS customer, "
        "SUM(CASE WHEN quoted_on IS NOT NULL THEN 1 ELSE 0 END) AS quoted, "
        "SUM(CASE WHEN status = 'Won' THEN 1 ELSE 0 END) AS won "
        "FROM v_enquiry WHERE id IN " + COUNTED_IDS + " AND legacy = 0 "
        "AND received_on >= ? AND requester_key IS NOT NULL "
        "GROUP BY requester_key, requester_country ORDER BY n DESC, won DESC, requester_key LIMIT 7", (d90,))]
    # v33: lost reasons count lost routes (a Won OPP can have a lost route)
    lost_reasons = collections.Counter(
        r["lost_reason"] or "Not recorded" for r in _route_rows(conn)
        if r["result"] == "Lost" and (r["result_on"] or "") >= d90
        # SC1: the other mode of a quote-both case is never a lost case
        and (r["lost_reason"] or "") != status_mod.OTHER_MODE_REASON)
    dash.lost = sorted(lost_reasons.items(), key=lambda kv: (-kv[1], kv[0]))
    dash.lost_note = "Closed reasons have been recorded since 18 Sep 2026; earlier losses show as not recorded."

    # the map: LATAM countries touched since the first of last month
    first = today_d.replace(day=1)
    since = (first - dt.timedelta(days=1)).replace(day=1).isoformat()
    dash.map_since = _day_label(dt.date.fromisoformat(since))
    latam = latam_countries(conn)
    touch = collections.defaultdict(lambda: {"n": 0, "quoted": 0, "won": 0})
    # v33: the map counts routes - each route with its case's quote, and its
    # own result
    for r in _route_rows(conn):
        if (r["received_on"] or "") < since:
            continue
        for c in {r.get("origin_country"), r.get("dest_country")}:
            if c in latam:
                t = touch[c]
                t["n"] += 1
                if r.get("quoted_on"):
                    t["quoted"] += 1
                if r["result"] == "Won":
                    t["won"] += 1
    dash.map_rows = sorted(touch.items(), key=lambda kv: (-kv[1]["n"], -kv[1]["won"], kv[0]))
    # v33: top lanes count routes (v_route)
    dash.lanes = [dict(r) for r in conn.execute(
        "SELECT origin_country AS origin, dest_country AS dest, COUNT(*) AS n, "
        "SUM(CASE WHEN result = 'Won' THEN 1 ELSE 0 END) AS won FROM v_route "
        "WHERE enquiry_id IN " + COUNTED_IDS + " AND legacy = 0 AND received_on >= ? "
        "AND origin_country IS NOT NULL AND dest_country IS NOT NULL "
        "GROUP BY origin_country, dest_country ORDER BY n DESC, won DESC, origin_country, dest_country LIMIT 7",
        (d90,))]
    return dash


# ---------------------------------------------------------- svg helpers ---
def sparkline(vals, w=96, h=28, color=C["accent"]) -> str:
    if not vals or max(vals) == 0:
        return ""
    mx = max(vals)
    n = len(vals)
    pts = [(i * (w - 4) / max(n - 1, 1) + 2, h - 2 - (v / mx) * (h - 6)) for i, v in enumerate(vals)]
    d = " ".join("{}{:.1f},{:.1f}".format("M" if i == 0 else "L", x, y) for i, (x, y) in enumerate(pts))
    last = pts[-1]
    return ('<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}">'
            '<path d="{d}" fill="none" stroke="{c}" stroke-width="2"/>'
            '<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{c}"/></svg>'
            .format(w=w, h=h, d=d, c=color, x=last[0], y=last[1]))


def line_chart(labels, series, colors, w=640, h=230, last_anchor_end=False) -> str:
    """`last_anchor_end` (the month report, 17a): the last label ends at its
    point instead of centring on it, so a long one is not cut off."""
    L, R, T, B = 34, 12, 14, 30
    allv = [v for s in series.values() for v in s]
    mx = max(allv + [1])
    mx = int((mx + 4) // 5 * 5) if mx > 5 else 5
    n = max(len(labels), 2)
    X = lambda i: L + i * (w - L - R) / (n - 1)
    Y = lambda v: T + (h - T - B) * (1 - v / mx)
    out = ['<svg viewBox="0 0 {} {}" width="100%" height="{}" class="chart">'.format(w, h, h)]
    for g in range(0, mx + 1, max(1, mx // 5)):
        out.append('<line x1="{}" y1="{:.1f}" x2="{}" y2="{:.1f}" stroke="{}" stroke-dasharray="2 4"/>'
                   '<text x="{}" y="{:.1f}" class="ax" text-anchor="end">{}</text>'
                   .format(L, Y(g), w - R, Y(g), C["line"], L - 6, Y(g) + 4, g))
    for i, lb in enumerate(labels):
        anchor = "end" if last_anchor_end and i == len(labels) - 1 and i else "middle"
        out.append('<text x="{:.1f}" y="{}" class="ax" text-anchor="{}">{}</text>'.format(X(i), h - 10, anchor, esc(lb)))
    for name, vals in series.items():
        col = colors[name]
        d = " ".join("{}{:.1f},{:.1f}".format("M" if i == 0 else "L", X(i), Y(v)) for i, v in enumerate(vals))
        out.append('<path d="{}" fill="none" stroke="{}" stroke-width="2.5" stroke-linejoin="round"/>'.format(d, col))
        for i, v in enumerate(vals):
            out.append('<circle cx="{:.1f}" cy="{:.1f}" r="3.2" fill="#fff" stroke="{}" stroke-width="2"/>'
                       .format(X(i), Y(v), col))
    out.append("</svg>")
    return "".join(out)


def bar_chart(labels, vals, w=300, h=230, color=C["accent"], fmt=None, subs=None) -> str:
    """Bars with the value on top; `subs` puts a small line ('3 quoted')
    at the foot of each bar; None values draw a dash."""
    fmt = fmt or (lambda v: "{:g}".format(v))
    L, R, T, B = 30, 12, 14, 30
    mx = max([v for v in vals if v is not None] + [1])
    n = max(len(labels), 1)
    bw = (w - L - R) / n * 0.62
    Y = lambda v: T + (h - T - B) * (1 - v / mx)
    out = ['<svg viewBox="0 0 {} {}" width="100%" height="{}" class="chart">'.format(w, h, h)]
    out.append('<line x1="{}" y1="{}" x2="{}" y2="{}" stroke="{}"/>'.format(L, h - B, w - R, h - B, C["line"]))
    for i, (lb, v) in enumerate(zip(labels, vals)):
        cx = L + (i + 0.5) * (w - L - R) / n
        if i % 2 == len(labels) % 2:
            out.append('<text x="{:.1f}" y="{}" class="ax" text-anchor="middle">{}</text>'.format(cx, h - 10, esc(lb)))
        if v is None:
            out.append('<text x="{:.1f}" y="{}" class="ax" text-anchor="middle">-</text>'.format(cx, h - B - 6))
            continue
        y = Y(v)
        sub = subs[i] if subs and subs[i] else ""
        out.append('<rect x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" rx="3" fill="{}">{}</rect>'
                   '<text x="{:.1f}" y="{:.1f}" class="val" text-anchor="middle">{}</text>'
                   .format(cx - bw / 2, y, bw, h - B - y, color,
                           "<title>{} {}</title>".format(esc(lb), esc(sub)) if sub else "", cx, y - 5, esc(fmt(v))))
        if sub and h - B - y >= 54:
            # 'n quoted' written up the bar; a bar too short for it keeps the tooltip
            out.append('<text x="{x:.1f}" y="{y}" class="sub" transform="rotate(-90 {x:.1f} {y})">{t}</text>'
                       .format(x=cx + 3, y=h - B - 6, t=esc(sub)))
    out.append("</svg>")
    return "".join(out)


def hbar_list(items, color_of) -> str:
    mx = max([n for _, n in items] + [1])
    out = ['<div class="hbars">']
    for label, n in items:
        pct = 100 * n / mx
        out.append('<div class="hb"><span class="hb-l">{}</span>'
                   '<span class="hb-t"><span class="hb-f" style="width:{:.0f}%;background:{}"></span></span>'
                   '<span class="hb-n">{}</span></div>'.format(esc(label), pct, color_of(label), n))
    out.append("</div>")
    return "".join(out)


def donut(parts, size=170, thick=26, centre_label="open") -> str:
    total = sum(v for _, v, _ in parts) or 1
    r = (size - thick) / 2
    cx = cy = size / 2
    out = ['<svg viewBox="0 0 {s} {s}" width="{s}" height="{s}" class="donut">'.format(s=size)]
    a = -math.pi / 2
    for label, v, col in parts:
        if v == 0:
            continue
        frac = v / total
        b = a + 2 * math.pi * frac
        large = 1 if frac > 0.5 else 0
        x1, y1 = cx + r * math.cos(a), cy + r * math.sin(a)
        x2, y2 = cx + r * math.cos(b - 0.02), cy + r * math.sin(b - 0.02)
        out.append('<path d="M{:.1f},{:.1f} A{},{} 0 {} 1 {:.1f},{:.1f}" fill="none" stroke="{}" stroke-width="{}">'
                   '<title>{} {}</title></path>'.format(x1, y1, r, r, large, x2, y2, col, thick, esc(label), v))
        a = b
    shown = sum(v for _, v, _ in parts)
    out.append('<text x="{}" y="{}" text-anchor="middle" class="dn">{}</text>'
               '<text x="{}" y="{}" text-anchor="middle" class="dl">{}</text></svg>'
               .format(cx, cy - 2, shown, cx, cy + 16, esc(centre_label)))
    return "".join(out)


def legend(parts) -> str:
    return "".join('<div><span><i style="background:{}"></i>{}</span><b>{}</b></div>'.format(c, esc(l), v)
                   for l, v, c in parts)


def load_map(path=None) -> dict:
    with Path(path or MAP_FILE).open(encoding="utf-8") as handle:
        return json.load(handle)


def map_svg(map_rows, amap=None) -> str:
    """The Americas with a bubble per LATAM country touched: radius by
    count, a green core sized by the won share, labels from n >= 2."""
    amap = amap or load_map()
    W, H = amap["width"], amap["height"]
    out = ['<svg viewBox="0 0 {} {}" width="100%" class="map">'.format(W, H)]
    for feature in amap["land"]:
        out.append('<path d="{}" fill="#e9edf2" stroke="#fff" stroke-width="0.8"/>'.format(feature["d"]))
    mx = max([t["n"] for _, t in map_rows] + [1])
    for name, t in map_rows:
        key = NAME_FIX.get(name, name)
        p = amap["anchors"].get(key) or amap["centroids"].get(key)
        if not p:
            continue
        rad = 9 + 34 * (t["n"] / mx) ** 0.5
        won_r = rad * (t["won"] / t["n"]) ** 0.5 if t["won"] else 0
        out.append('<circle cx="{}" cy="{}" r="{:.1f}" fill="{c}" fill-opacity=".25" stroke="{c}" stroke-opacity=".7">'
                   '<title>{} {}</title></circle>'.format(p[0], p[1], rad, esc(name), t["n"], c=C["latam"]))
        if won_r:
            out.append('<circle cx="{}" cy="{}" r="{:.1f}" fill="{}" fill-opacity=".75"/>'.format(p[0], p[1], won_r, C["won"]))
        if t["n"] >= 2:
            dx, dy, anchor = 0, -rad - 4, "middle"
            if name in ("Uruguay", "Brazil"):
                dx, dy, anchor = rad + 4, 4, "start"
            if name == "Argentina":
                dx, dy, anchor = 0, rad + 13, "middle"
            out.append('<text x="{:.1f}" y="{:.1f}" text-anchor="{}" class="mlab">{} <tspan class="mnum">{}</tspan></text>'
                       .format(p[0] + dx, p[1] + dy, anchor, esc(name), t["n"]))
    out.append("</svg>")
    return "".join(out)


# ----------------------------------------------------------- rendering ----
def fragments(dash: Dashboard, amap=None) -> dict:
    """The SVG and list fragments the templates drop in, built once."""
    reason_colour = lambda lb: C["muted"] if lb == "Not recorded" else C["lost"]
    return {
        "line": line_chart(dash.weeks, dash.series, GROUP_COLOURS),
        "bars": bar_chart(dash.weeks, [s[0] for s in dash.speed], fmt=lambda v: "{:g}%".format(v),
                          subs=["{} quoted".format(n) if n else "" for _, n in dash.speed]),
        "pipeline_donut": donut(dash.pipeline["parts"]),
        "pipeline_legend": legend(dash.pipeline["parts"]),
        "closed_legend": legend(dash.pipeline["closed"]),
        "mix_donut": donut(dash.mix, size=150, thick=22, centre_label="requests"),
        "mix_legend": legend(dash.mix),
        "lost": hbar_list(dash.lost, reason_colour) if dash.lost else "",
        "map": map_svg(dash.map_rows, amap),
        "group_legend": "".join('<span><i style="background:{}"></i>{}</span>'.format(GROUP_COLOURS[g], g)
                                for g in GROUPS),
    }


def as_of(dash: Dashboard) -> str:
    d = dt.date.fromisoformat(dash.today)
    return "As of " + dates.month_day(d, weekday=True, year=True)


def file_name(today: str) -> str:
    return "DASHBOARD {}.html".format(today[:10])


#: The stylesheet, every rule under .dash so the app's own styles stay
#: out of it; the standalone file wraps its body in the same class.
CSS = """
.dash{--ink:#1c1f23;--muted:#6a7178;--line:#d8dce0;--panel:#f6f7f9;--accent:#008fbb;color:var(--ink);font-size:13.5px;line-height:1.45;}
.dash *{box-sizing:border-box}
.dash .dhead{display:flex;align-items:baseline;justify-content:space-between;gap:20px;margin-bottom:16px;flex-wrap:wrap;}
.dash h1{font-size:22px;margin:0;font-weight:600;}
.dash h1 span{color:var(--muted);font-weight:400;font-size:14px;margin-left:10px;}
.dash .tabs{display:flex;gap:4px;}
.dash .tab{padding:6px 14px;border-radius:999px;color:var(--muted);font-weight:600;font-size:12.5px;text-decoration:none;}
.dash .tab.on{background:#fff;color:var(--accent);box-shadow:0 1px 2px rgba(0,0,0,.08);}
.dash .asof{color:var(--muted);font-size:12.5px;display:flex;gap:10px;align-items:center;}
.dash .tiles{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:14px;}
.dash .tile{background:#fff;border-radius:8px;padding:16px 18px 14px;box-shadow:0 1px 2px rgba(0,0,0,.06);min-height:118px;}
.dash .t-label{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;font-weight:600;}
.dash .t-row{display:flex;align-items:flex-end;justify-content:space-between;gap:8px;margin-top:4px;}
.dash .t-big{font-size:40px;font-weight:600;line-height:1.05;color:#002060;}
.dash .t-sub{font-size:12px;color:var(--muted);margin-top:6px;}
.dash .t-sub b{color:var(--ink);font-weight:600;}
.dash .others-line{font-size:12.5px;color:#3d6384;margin:-4px 0 14px;}
.dash .grid{display:grid;grid-template-columns:2fr 1fr;gap:12px;margin-bottom:14px;}
.dash .grid3{display:grid;grid-template-columns:2fr 1fr 1fr;gap:12px;margin-bottom:14px;}
.dash .card{background:#fff;border-radius:8px;padding:14px 16px 12px;box-shadow:0 1px 2px rgba(0,0,0,.06);min-width:0;}
.dash .card h2{font-size:12px;margin:0 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;font-weight:600;display:flex;justify-content:space-between;align-items:baseline;gap:10px;}
.dash .card h2 small{text-transform:none;letter-spacing:0;font-weight:400;font-size:11.5px;text-align:right;}
.dash .mapwrap{display:grid;grid-template-columns:1fr 240px;gap:12px;align-items:start;}
.dash .map text.mlab{font-size:11px;font-weight:600;fill:#2b3138;paint-order:stroke;stroke:#fff;stroke-width:3px;}
.dash .map .mnum{fill:#e8456c;}
.dash table.mini{width:100%;border-collapse:collapse;font-size:12.5px;}
.dash table.mini th{text-align:left;color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em;padding:4px 6px;border-bottom:1px solid var(--line);background:none;}
.dash table.mini td{padding:5px 6px;border-bottom:1px solid #eef0f3;}
.dash table.mini td.num,.dash table.mini th.num{text-align:right;font-variant-numeric:tabular-nums;}
.dash table.mini td.won{color:#4f9a24;font-weight:600;}
.dash .muted{color:var(--muted);}
.dash .dc-tag{font-size:11px;font-weight:600;color:#9a5d00;background:#fdf0dc;border-radius:999px;padding:1px 7px;margin-left:4px;}
.dash .legend{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--muted);margin-top:4px;}
.dash .legend i,.dash .dlegend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px;}
.dash .chart .ax{font-size:10.5px;fill:var(--muted);}
.dash .chart .val{font-size:11px;fill:var(--ink);font-weight:600;}
.dash .chart .sub{font-size:9px;fill:#fff;font-weight:600;}
.dash .donutwrap{display:flex;gap:16px;align-items:center;}
.dash .donut .dn{font-size:26px;font-weight:600;fill:var(--ink);}
.dash .donut .dl{font-size:11px;fill:var(--muted);}
.dash .dlegend div{display:flex;justify-content:space-between;gap:14px;font-size:12.5px;padding:3px 0;min-width:120px;}
.dash .dlegend b{font-variant-numeric:tabular-nums;}
.dash .hbars{display:flex;flex-direction:column;gap:7px;margin-top:6px;}
.dash .hb{display:grid;grid-template-columns:150px 1fr 28px;gap:8px;align-items:center;font-size:12.5px;}
.dash .hb-t{height:10px;background:#eef0f3;border-radius:5px;overflow:hidden;}
.dash .hb-f{display:block;height:100%;border-radius:5px;}
.dash .hb-n{text-align:right;font-variant-numeric:tabular-nums;font-weight:600;}
.dash .note{font-size:11.5px;color:var(--muted);margin-top:8px;}
.dash .dfoot{color:var(--muted);font-size:11.5px;margin-top:18px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;}
@media (max-width:1100px){.dash .tiles{grid-template-columns:repeat(3,1fr);}.dash .grid,.dash .grid3{grid-template-columns:1fr;}.dash .mapwrap{grid-template-columns:1fr;}}
"""

STANDALONE_CSS = """
body{margin:0;background:#eef0f3;font-family:system-ui,"Segoe UI",Arial,sans-serif;}
.dash{max-width:1440px;margin:0 auto;padding:22px 26px 40px;}
""" + CSS


def render_standalone(dash: Dashboard, amap=None) -> str:
    """The page as one self-contained file: inline CSS and SVG, nothing
    fetched. The same body template as the app page."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    env = Environment(loader=FileSystemLoader(str(paths.PROJECT_DIR / "opplist" / "web" / "templates")),
                      autoescape=select_autoescape(["html"]))
    return env.get_template("dashboard_standalone.html").render(
        dash=dash, frag=fragments(dash, amap), as_of=as_of(dash), css=STANDALONE_CSS.strip(),
        periods=PERIODS, standalone=True, favicon=favicon.data_uri())


def write_html(dash: Dashboard, out_dir=None) -> Path:
    out_dir = Path(out_dir or paths.REPORTS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / file_name(dash.today)
    path.write_text(render_standalone(dash), encoding="utf-8")
    return path
