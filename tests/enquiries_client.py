"""What the Enquiries page shows, worked out the way its script does.

Enquiries v2 (2026-09-25) renders every row of the view once and leaves
the grouping, sorting, filters and search to app.js (enquiries v2). The
tests have no browser, so this reads the rendered rows' data attributes and
applies the same rules as the script - scope, the dropped rows, the filter
chips, the sort, the grouping - from the same query string. Keep the two
in step: a rule changed in app.js is changed here.
"""

from __future__ import annotations

import html.parser
import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

STATUS_ORDER = ["New", "Quoted", "Won", "Lost", "Dropped"]
OTHERS = "others"      # v31: the last status group, the cases another office handles
HOME = "JJC"
SORT_ALIAS = {"received": "received_on", "last": "last_event_on", "last_activity_on": "last_event_on"}
DIRECTIONS = ["US Export", "US Import", "US Domestic", "Cross-trade", "To Confirm"]


class _Rows(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.head, self.text = [], [], None
        self._in_pool = self._in_row = self._in_text = False
        self._current = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and a.get("id") == "enq-pool":
            self._in_pool = True
        elif self._in_pool and tag == "th":
            self.head.append(a.get("data-col") or "?")
        elif self._in_pool and tag == "tr" and "enq-row" in (a.get("class") or ""):
            self._current = {k[5:]: v for k, v in a.items() if k.startswith("data-")}
            self._current["_text"] = ""
            self._in_row = True
        elif tag == "script" and a.get("id") == "enq-text":
            self._in_text = True
            self.text = ""

    def handle_endtag(self, tag):
        if tag == "table" and self._in_pool:
            self._in_pool = False
        elif tag == "tr" and self._in_row:
            self.rows.append(self._current)
            self._in_row = False
        elif tag == "script" and self._in_text:
            self._in_text = False

    def handle_data(self, data):
        if self._in_row:
            self._current["_text"] += data
        if self._in_text:
            self.text += data


@dataclass
class Page:
    rows: list                         # the rendered rows (dicts of their data attributes)
    head: list                         # the pool's column ids, in order
    shown: list = field(default_factory=list)      # the OPP numbers shown, in order
    groups: list = field(default_factory=list)     # [(key, [opp, ...])] in page order
    collapsed: list = field(default_factory=list)  # the group keys drawn collapsed
    dropped_line: str = ""


def opp_of(row) -> str:
    found = re.search(r"OPP-\d{4}", row["_text"])
    return found.group(0) if found else ""


def read(body: str, url: str = "/enquiries", stored: dict | None = None) -> Page:
    """The page as the script would draw it for `url` (its query string),
    with `stored` standing in for the browser's remembered group / sort."""
    parser = _Rows()
    parser.feed(body)
    text = json.loads(parser.text or "{}")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query, keep_blank_values=True).items()}
    stored = stored or {}
    url_dir = q.get("dir", "")
    sort = SORT_ALIAS.get(q.get("sort", ""), q.get("sort", "")) or stored.get("sort") or "received_on"

    def default_dir(key):
        return "asc" if key == "requester" else "desc"

    if url_dir in ("asc", "desc"):
        direction = url_dir
    elif q.get("sort"):
        direction = default_dir(sort)
    else:
        direction = stored.get("dir") or default_dir(sort)
    group = q.get("group") if q.get("group") in ("status", "requester", "month", "mode", "office") else stored.get("group", "status")
    latam = q.get("latam", "")
    filters = {
        "latam": "Y" if latam in ("1",) or latam.upper() == "Y" else ("N" if latam.upper() == "N" else ""),
        "mode": q.get("mode", ""),
        "direction": q.get("direction") or (url_dir if url_dir in DIRECTIONS else ""),
        "month": q.get("month", ""),
        "requester": q.get("requester", ""),
        "scope": q.get("scope") or "In",
        "status": q.get("status", ""),
        "owner": q.get("owner", ""),
    }
    show_dropped = q.get("dropped") == "1"
    search = q.get("_search", "")        # what is typed in the box (not ?q=, the server's search)

    def passes(r):
        f = filters
        if f["scope"] != "all" and r.get("scope") != f["scope"]:
            return False
        # v33: a mode or a direction matches any route (data-modes, data-directions)
        for key, attr in (("mode", "modes"), ("direction", "directions")):
            if f[key]:
                listed = r.get(attr) or "|{}|".format(r.get(key, ""))
                if "|{}|".format(f[key]) not in listed:
                    return False
        for key, attr in (("latam", "latam"), ("month", "month"),
                          ("requester", "requester"), ("status", "status"), ("owner", "owner")):
            if f[key] and r.get(attr, "") != f[key]:
                return False
        if search and search.lower() not in r.get("search", ""):
            return False
        return True

    def value(r, key):
        if key == "received_on":
            return r.get("received", "")
        if key == "last_event_on":
            return r.get("last", "")
        if key == "requester":
            return r.get("requester-name", "").lower()
        return r.get("s-" + key, "").lower()

    import functools

    def compare(a, b):
        x, y = value(a, sort), value(b, sort)
        if not x and y:
            return 1
        elif x and not y:
            return -1
        elif sort in ("opp_no", "qty", "request_group"):
            fx, fy = float(x or 0), float(y or 0)
            c = (fx > fy) - (fx < fy)
        else:
            c = (x > y) - (x < y)
        if direction == "desc":
            c = -c
        if c:
            return c
        ra, rb = a.get("received", ""), b.get("received", "")
        if ra != rb:
            return 1 if ra < rb else -1
        return int(b.get("s-opp_no") or 0) - int(a.get("s-opp_no") or 0)

    seen = [r for r in parser.rows if passes(r)]
    allow_dropped = text.get("view") == "closed" or bool(text.get("q")) or filters["status"] == "Dropped" or show_dropped
    dropped = sum(1 for r in seen if r.get("status") == "Dropped")
    listed = seen if allow_dropped else [r for r in seen if r.get("status") != "Dropped"]
    listed.sort(key=functools.cmp_to_key(compare))

    def key_of(r):
        if group == "status":
            return OTHERS if r.get("office") else r.get("status")
        if group == "office":
            return r.get("office") or HOME
        if group == "requester":
            return r.get("requester-name") or "?"
        if group == "month":
            return r.get("month") or "(none)"
        return r.get("mode") or "(none)"

    groups = {}
    for r in listed:
        groups.setdefault(key_of(r), []).append(r)
    if group == "status":
        keys = [k for k in STATUS_ORDER + [OTHERS] if k in groups]
    elif group == "office":
        keys = sorted(groups, key=lambda k: (k != HOME, -len(groups[k]), k))
    elif group == "month":
        keys = sorted(groups, reverse=True)
    else:
        keys = sorted(groups, key=lambda k: (-len(groups[k]), k))
    if text.get("view") == "closed":
        line = ""
    elif not allow_dropped:
        line = "{} dropped hidden".format(dropped) if dropped else ""
    else:
        line = "including {} dropped".format(dropped) if dropped else ""
    return Page(rows=parser.rows, head=parser.head,
                shown=[opp_of(r) for k in keys for r in groups[k]],
                groups=[(k, [opp_of(r) for r in groups[k]]) for k in keys],
                collapsed=[k for k in keys if group == "status" and k == OTHERS and text.get("view") != "others"],
                dropped_line=line)


def get(client, url: str, stored: dict | None = None) -> Page:
    """Fetch `url` with the test client and read it the way the page would."""
    return read(client.get(url).data.decode("utf-8"), url, stored)
