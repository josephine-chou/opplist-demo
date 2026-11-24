"""Batch Q3 (2026-11-19, schema v56): the quote card's quotes taken into
OppList, the case page's Quotes card, the timeline's link and the Won form
starting from the last quote (INTERFACE.md section 7).

A fake Firestore client; no network. Every company, person, amount and date
here is invented: cases OPP-9301 upward, domains .test.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, firebase_pull, firebase_sync as fs, schema_migrations, site_quotes, suggestions
from opplist import web as web_module
from opplist.web import create_app
from test_rq2 import SiteFake

TODAY = "2027-06-20"
AT = dt.datetime(2027, 6, 10, 15, 0, tzinfo=dt.timezone.utc)


class Site(SiteFake):
    """SiteFake whose readTime is the newest action's at, so an action put
    in after a pull is found by the next one."""

    def __init__(self):
        super().__init__()
        self.gets = []

    def get(self, path):
        self.gets.append(path)
        return super().get(path)

    def run_query(self, query):
        out = super().run_query(query)
        ats = [firebase_pull.iso(d["at"]) for p, d in self.docs.items()
               if p.startswith("actions/") and isinstance(d.get("at"), dt.datetime)]
        out["readTime"] = max(ats) if ats else ""
        return out


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "q3.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    agent = add_party(repo, "Dunmere Cargo", country="Chile")
    ids = {"agent": agent,
           "harrow": add_party(repo, "Harrow Lines", country="USA"),
           "quell": add_party(repo, "Quell Freight", country="USA"),
           "office": add_party(repo, "JoJo Pinecrest", ptype="Office", country="USA"),
           "carrier": add_party(repo, "Orvell Shipping", ptype="Carrier", country="USA")}
    for opp in ("OPP-9301", "OPP-9302", "OPP-9303"):
        ids[opp] = add_enquiry(repo, opp, agent, received="2027-06-01", scope="In", service_mode="FCL",
                               origin_country="USA", dest_country="Chile")
    for vendor in ("harrow", "quell"):
        ids["vr-" + vendor] = repo.insert("vendor_request", {
            "enquiry_id": ids["OPP-9301"], "vendor_party_id": ids[vendor], "asked_on": "2027-06-02",
            "source": "manual"})
    repo.commit()
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    monkeypatch.setattr(fs, "_archive_search", lambda: None)
    yield {"conn": conn, "ids": ids, "path": path, "site": Site(), "state": {}}
    conn.close()


LINES = [{"charge": "OCEAN FREIGHT", "qty": "1", "unit": "40HQ", "rate": 1540, "word": "", "amount": 1540,
          "hand": False, "remark": ""},
         {"charge": "PICK UP", "qty": "1", "unit": "TRIP", "rate": 330, "word": "", "amount": 330, "hand": False,
          "remark": "Includes fuel"},
         {"charge": "EXPORT CUSTOMS", "qty": "1", "unit": "BL", "rate": 80, "word": "", "amount": 80, "hand": False,
          "remark": ""}]
COSTS = [{"cost": 1400, "markup": 140}, {"cost": 300, "markup": 30}, {"cost": None, "markup": None}]
HARROW = [{"seg": "Freight", "key": "Harrow Lines", "name": "Harrow Lines", "carrier": "MSC"},
          {"seg": "Origin", "key": "Harrow Lines", "name": "Harrow Lines", "carrier": ""}]


def quote(w, qid, n=1, opp="OPP-9301", date="2027-06-10", lines=LINES, costs=COSTS, sources=HARROW,
          rate_from="Harrow Lines", total=1950, at_cost=False, at=AT, put_doc=True, **extra):
    """The card's quote action with its quotes/{id} (and quoteCosts/{id})."""
    w["site"].docs["actions/" + qid] = {
        "type": "quote", "opp": opp, "by": "eric@site.test", "name": "Eric", "owner": "Eric", "app": "1111e",
        "at": at, "date": date, "what": "Quote sent", "sentBy": "Eric", "quoteId": qid, "rateFrom": rate_from,
        "sources": sources}
    if put_doc:
        put_quote(w, qid, n, opp, date, lines, costs, sources, rate_from, total, at_cost, at, **extra)


def put_quote(w, qid, n=1, opp="OPP-9301", date="2027-06-10", lines=LINES, costs=COSTS, sources=HARROW,
              rate_from="Harrow Lines", total=1950, at_cost=False, at=AT, **extra):
    doc = {"opp": opp, "n": n, "date": date, "how": "draft", "cur": "USD", "total": total, "atCost": at_cost,
           "lines": lines, "valid": "2027-06-30", "transit": "24 days", "routing": "direct",
           "remarks": "Subject to space", "terms": ["Rates subject to space.", "Insurance not included."],
           "subject": "RE: [{}] Quote".format(opp), "pdf": "{}_Quotation_{}.pdf".format(opp, n),
           "rateFrom": rate_from, "sources": sources, "by": "eric@site.test", "name": "Eric", "at": at}
    doc.update(extra)
    w["site"].docs["quotes/" + qid] = doc
    if costs is not None:
        w["site"].docs["quoteCosts/" + qid] = {
            "opp": opp, "cur": "USD", "pct": 10, "lines": costs, "cost": 1700, "profit": 250, "margin": 12.82,
            "by": "eric@site.test", "at": at}


def pull(w):
    return firebase_pull.pull(w["conn"], w["site"], w["state"], TODAY)


def page(w, opp="OPP-9301"):
    app = create_app(w["path"])
    app.config["TESTING"] = True
    return app.test_client().get("/enquiries/{}".format(w["ids"][opp])).data.decode("utf-8")


def count(w, table):
    return w["conn"].execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0]


# ----------------------------------------------------------- 2. take in --
def test_a_quote_and_its_costs_taken_in_after_the_action_and_linked_to_its_event(world):
    w = world
    quote(w, "qAlpha00000000000001")
    got = pull(w)
    assert got.applied == 1 and got.quotes == 1 and got.quotes_waiting == 0
    assert "1 quote taken in" in got.line()
    event = w["conn"].execute("SELECT * FROM status_event WHERE enquiry_id = ? AND status = 'Quoted'",
                              (w["ids"]["OPP-9301"],)).fetchone()
    assert event["note"].endswith("[quote:qAlpha00000000000001]")
    q = w["conn"].execute("SELECT * FROM quote").fetchone()
    assert (q["site_id"], q["enquiry_id"], q["status_event_id"], q["n"], q["date"], q["how"], q["cur"]) == (
        "qAlpha00000000000001", w["ids"]["OPP-9301"], event["id"], 1, "2027-06-10", "draft", "USD")
    assert (q["total"], q["at_cost"], q["valid"], q["transit"], q["routing"], q["rate_from"]) == (
        1950, 0, "2027-06-30", "24 days", "direct", "Harrow Lines")
    assert json.loads(q["terms"]) == ["Rates subject to space.", "Insurance not included."]
    assert json.loads(q["sources"])[0] == {"seg": "Freight", "key": "Harrow Lines", "name": "Harrow Lines",
                                           "carrier": "MSC"}
    assert (q["with_costs"], q["pct"], q["cost"], q["profit"], q["margin"]) == (1, 10, 1700, 250, 12.82)
    assert (q["by_name"], q["by_email"], q["costs_by"]) == ("Eric", "eric@site.test", "eric@site.test")
    lines = w["conn"].execute("SELECT * FROM quote_line ORDER BY seq").fetchall()
    assert [(l["seq"], l["charge"], l["qty"], l["rate"], l["amount"], l["cost"], l["markup"]) for l in lines] == [
        (1, "OCEAN FREIGHT", "1", 1540, 1540, 1400, 140), (2, "PICK UP", "1", 330, 330, 300, 30),
        (3, "EXPORT CUSTOMS", "1", 80, 80, None, None)]
    assert lines[1]["remark"] == "Includes fuel"
    # written through the Repository: the change log has it
    assert w["conn"].execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'quote'").fetchone()[0] >= 1
    # nothing written to the site but the action's answer
    assert not any(p.startswith(("quotes/", "quoteCosts/")) for c in w["site"].commits for p in c)
    # the card: the costs per line and the totals
    html = page(w)
    card = html[html.index('id="quotes"'):html.index('id="won"') if 'id="won"' in html else None]
    assert "USD 1,950.00" in card and "1,700.00" in card and "250.00" in card and "12.8%" in card
    assert "<th class=\"num\">Cost</th><th class=\"num\">Markup</th>" in card
    assert '<td class="num"><b>1,950.00</b></td><td class="num"><b>1,700.00</b></td>' in card


def test_without_quote_costs_the_costs_are_null_and_the_card_shows_none(world):
    w = world
    quote(w, "qBravo00000000000001", costs=None)
    pull(w)
    q = w["conn"].execute("SELECT * FROM quote").fetchone()
    assert (q["with_costs"], q["pct"], q["cost"], q["profit"], q["margin"]) == (0, None, None, None, None)
    assert {l[0] for l in w["conn"].execute("SELECT cost FROM quote_line")} == {None}
    card = site_quotes.view(w["conn"], w["ids"]["OPP-9301"])[0]
    assert (card["cost"], card["profit"], card["margin"], card["sum_cost"]) == ("", "", "", "")
    html = page(w)
    assert ">Markup</th>" not in html[html.index('id="quotes"'):]


def test_a_document_missing_at_the_first_run_is_taken_in_once_at_the_second(world, caplog):
    w = world
    quote(w, "qLate000000000000001", put_doc=False)
    with caplog.at_level(logging.WARNING, logger="opplist.firebase"):
        first = pull(w)
        again = pull(w)                                     # the same day: not logged again
    assert (first.quotes, first.quotes_waiting, again.quotes_waiting) == (0, 1, 1)
    said = [r.getMessage() for r in caplog.records if "qLate000000000000001" in r.getMessage()]
    assert len(said) == 1 and "tried again at the next run" in said[0]
    assert count(w, "quote") == 0
    put_quote(w, "qLate000000000000001")
    assert pull(w).quotes == 1
    assert count(w, "quote") == 1 and count(w, "quote_line") == 3
    # a run that sees the same mark again writes nothing (and reads nothing)
    w["site"].gets.clear()
    third = pull(w)
    assert (third.quotes, third.quotes_waiting) == (0, 0)
    assert count(w, "quote") == 1 and count(w, "quote_line") == 3
    assert not [p for p in w["site"].gets if p.startswith("quotes/")]
    assert w["state"][site_quotes.STATE_KEY] == {}


@pytest.mark.parametrize("change", ["other case", "41 lines", "currency", "a number"])
def test_a_document_that_fails_the_rules_is_not_written(world, change):
    w = world
    quote(w, "qBad0000000000000001")
    doc = w["site"].docs["quotes/qBad0000000000000001"]
    if change == "other case":
        doc["opp"] = "OPP-9302"
    elif change == "41 lines":
        doc["lines"] = [dict(LINES[0]) for _ in range(41)]
    elif change == "currency":
        doc["cur"] = "usd"
    else:
        doc["lines"] = [dict(LINES[0], rate="1540")]
    got = pull(w)
    assert (got.applied, got.quotes, got.quotes_waiting) == (1, 0, 1)
    assert count(w, "quote") == 0 and count(w, "quote_line") == 0


def test_text_is_cut_at_the_sites_limits(world):
    w = world
    quote(w, "qLong000000000000001", routing="r" * 250,
          lines=[dict(LINES[0], charge="C" * 90, remark="x" * 230)], costs=None)
    pull(w)
    q = w["conn"].execute("SELECT * FROM quote").fetchone()
    line = w["conn"].execute("SELECT * FROM quote_line").fetchone()
    assert (len(q["routing"]), len(line["charge"]), len(line["remark"])) == (200, 80, 200)


def test_at_most_fifty_a_run_oldest_event_first(world, monkeypatch):
    w = world
    monkeypatch.setattr(site_quotes, "RUN_MAX", 2)
    for k in range(3):
        quote(w, "qMany00000000000000{}".format(k), n=k + 1, at=AT + dt.timedelta(hours=k), date="2027-06-1{}".format(k))
    first = pull(w)
    assert first.quotes == 2
    assert [r[0] for r in w["conn"].execute("SELECT n FROM quote ORDER BY id")] == [1, 2]
    assert pull(w).quotes == 1


# ------------------------------------------------------------- 3. the card --
def test_no_quotes_card_without_a_quote(world):
    assert 'id="quotes"' not in page(world)


def test_newest_first_requote_at_cost_and_the_sources_text(world):
    w = world
    quote(w, "qOne0000000000000001", n=1, sources=[
        {"seg": "Freight", "key": "own", "name": "Our own rate", "carrier": ""},
        {"seg": "Origin", "key": "", "name": "Pellow Trucking", "carrier": ""}], rate_from="own")
    quote(w, "qTwo0000000000000001", n=2, at=AT + dt.timedelta(days=1), date="2027-06-11", at_cost=True,
          lines=LINES + [{"charge": "DUTIES", "qty": "1", "unit": "", "rate": None, "word": "AT COST",
                          "amount": None, "hand": False, "remark": ""}],
          costs=COSTS + [{"cost": None, "markup": None}])
    pull(w)
    rows = site_quotes.view(w["conn"], w["ids"]["OPP-9301"])
    assert [(r["n"], r["requote"], r["open"]) for r in rows] == [(2, True, True), (1, False, False)]
    assert rows[0]["total"] == "USD 1,950.00 plus AT COST"
    assert rows[0]["rates_from"] == "Freight: Harrow Lines (MSC) · Origin: Harrow Lines"
    assert rows[1]["rates_from"] == "Freight: Our own rate · Origin: Pellow Trucking"
    assert rows[0]["lines"][3]["amount"] == "AT COST"
    assert rows[0]["detail"] == "4 lines · transit 24 days · routing direct · 2 terms"
    html = page(w)
    card = html[html.index('id="quotes"'):]
    assert card.index('id="quote-2"') < card.index('id="quote-1"')
    assert "<b>Quote 2</b> <span class=\"muted\">re-quote</span>" in card
    assert '<details class="q-detail" open>' in card and card.count("<details class=\"q-detail\">") == 1
    assert "A quote sent another way (from Outlook, or Just log it) is on the timeline only." in card


def test_the_event_struck_out_shows_the_quote_muted_and_put_back_as_before(world):
    w = world
    quote(w, "qStrk000000000000001")
    pull(w)
    event = w["conn"].execute("SELECT status_event_id FROM quote").fetchone()[0]
    repo = db.Repository(w["conn"], source="manual")
    repo.void_status_event(event, voided=True)
    repo.commit()
    assert count(w, "quote") == 1                           # stays in OppList
    assert site_quotes.view(w["conn"], w["ids"]["OPP-9301"])[0]["struck"] is True
    assert "struck out with its event" in page(w)
    repo.void_status_event(event, voided=False)
    repo.commit()
    assert site_quotes.view(w["conn"], w["ids"]["OPP-9301"])[0]["struck"] is False
    assert "struck out with its event" not in page(w)


# ---------------------------------------------------------- 4. the timeline --
def test_the_mark_is_a_link_when_the_quote_is_in_and_hidden_when_not(world):
    w = world
    quote(w, "qLink000000000000001", put_doc=False)
    pull(w)
    html = page(w)
    # hidden on the line; only the event's edit field keeps the note as stored
    assert html.count("[quote:") == len(re.findall(r'name="note" value="[^"]*\[quote:', html)) == 1
    assert 'class="quote-link"' not in html
    put_quote(w, "qLink000000000000001")
    pull(w)
    html = page(w)
    assert '<a class="quote-link" href="#quote-1">Quote 1</a>' in html
    assert html.count("[quote:") == len(re.findall(r'name="note" value="[^"]*\[quote:', html)) == 1
    # the note is stored as before
    note = w["conn"].execute("SELECT note FROM status_event WHERE note LIKE '%[quote:%'").fetchone()[0]
    assert note == "Quote sent (sent by Eric) [quote:qLink000000000000001]"
    assert site_quotes.note_html("Sent [mid:<a@b.test>] [quote:qX]", {"qX": 3}) == \
        'Sent <a class="quote-link" href="#quote-3">Quote 3</a>'
    assert site_quotes.note_html("<b> [quote:qY]", {}) == "&lt;b&gt;"


def test_a_pill_in_a_timeline_line_does_not_shrink():
    css = (Path(web_module.__file__).parent / "static" / "app.css").read_text(encoding="utf-8")
    assert ".timeline .event-line .pill { flex: 0 0 auto; overflow-wrap: normal; }" in css


# ------------------------------------------------------------- 5. Won --
def start(w, opp="OPP-9301"):
    return site_quotes.won_start(w["conn"], w["ids"][opp])


def sources_of(key, name=None, seg="Freight"):
    return [{"seg": seg, "key": key, "name": name or key, "carrier": ""}]


def test_won_starts_from_a_vendor_of_the_case(world):
    w = world
    quote(w, "qWonA000000000000001", n=1, sources=sources_of("Quell Freight"), rate_from="Quell Freight")
    quote(w, "qWonA000000000000002", n=2, at=AT + dt.timedelta(days=1), date="2027-06-11")
    pull(w)
    assert start(w) == {"rate_used": "agent", "rate_party": None, "rate_request": w["ids"]["vr-harrow"],
                        "rate_hint": "from Quote 2 (Freight: Harrow Lines)"}
    html = page(w)
    won = html[html.index('data-act-form="won"'):]
    won = won[:won.index("</form>")]
    assert re.search(r'value="agent"\s+checked', won)
    assert 'value="{}" selected>Harrow Lines'.format(w["ids"]["vr-harrow"]) in won
    assert "from Quote 2 (Freight: Harrow Lines)" in won


def test_won_starts_from_an_office_a_carrier_or_own_and_not_from_a_typed_name(world):
    w = world
    quote(w, "qWonB000000000000001", opp="OPP-9302", sources=sources_of("JoJo Pinecrest"),
          rate_from="JoJo Pinecrest")
    pull(w)
    assert start(w, "OPP-9302") == {"rate_used": "office", "rate_party": w["ids"]["office"], "rate_request": None,
                                    "rate_hint": "from Quote 1 (Freight: JoJo Pinecrest)"}
    quote(w, "qWonB000000000000002", n=2, opp="OPP-9302", sources=sources_of("Orvell Shipping"),
          rate_from="Orvell Shipping", at=AT + dt.timedelta(days=1))
    pull(w)
    assert start(w, "OPP-9302")["rate_used"] == "carrier"
    assert start(w, "OPP-9302")["rate_party"] == w["ids"]["carrier"]
    quote(w, "qWonB000000000000003", n=3, opp="OPP-9302", sources=sources_of("own", "Our own rate"),
          rate_from="own", at=AT + dt.timedelta(days=2))
    pull(w)
    assert start(w, "OPP-9302") == {"rate_used": "own", "rate_party": None, "rate_request": None,
                                    "rate_hint": "from Quote 3 (Freight: Our own rate)"}
    quote(w, "qWonB000000000000004", n=4, opp="OPP-9302", sources=sources_of("", "Pellow Trucking"),
          rate_from="", at=AT + dt.timedelta(days=3))
    pull(w)
    assert start(w, "OPP-9302") is None                     # a typed name: the form as today
    html = page(w, "OPP-9302")
    won = html[html.index('data-act-form="won"'):]
    assert "from Quote" not in won[:won.index("</form>")]


def test_won_reads_an_office_or_a_carrier_by_its_type_before_its_rate_request(world):
    # RG2a (item 12): one of our offices or a carrier the case also asked
    # starts as itself, not as Agent rate with its request
    w = world
    repo = db.Repository(w["conn"], source="test")
    for who in ("office", "carrier"):
        w["ids"]["vr-" + who] = repo.insert("vendor_request", {
            "enquiry_id": w["ids"]["OPP-9303"], "vendor_party_id": w["ids"][who], "asked_on": "2027-06-02",
            "source": "manual"})
    repo.commit()
    quote(w, "qWonD000000000000001", opp="OPP-9303", sources=sources_of("JoJo Pinecrest"),
          rate_from="JoJo Pinecrest")
    pull(w)
    assert start(w, "OPP-9303") == {"rate_used": "office", "rate_party": w["ids"]["office"], "rate_request": None,
                                    "rate_hint": "from Quote 1 (Freight: JoJo Pinecrest)"}
    quote(w, "qWonD000000000000002", n=2, opp="OPP-9303", sources=sources_of("Orvell Shipping"),
          rate_from="Orvell Shipping", at=AT + dt.timedelta(days=1))
    pull(w)
    assert start(w, "OPP-9303") == {"rate_used": "carrier", "rate_party": w["ids"]["carrier"], "rate_request": None,
                                    "rate_hint": "from Quote 2 (Freight: Orvell Shipping)"}
    html = page(w, "OPP-9303")
    won = html[html.index('data-act-form="won"'):]
    won = won[:won.index("</form>")]
    assert re.search(r'value="carrier"\s+checked', won)
    assert "from Quote 2 (Freight: Orvell Shipping)" in won


def test_won_skips_a_struck_out_last_quote_and_an_awarded_request_wins(world):
    w = world
    quote(w, "qWonC000000000000001", n=1, sources=sources_of("Quell Freight"), rate_from="Quell Freight")
    quote(w, "qWonC000000000000002", n=2, at=AT + dt.timedelta(days=1), date="2027-06-11")
    pull(w)
    last = w["conn"].execute("SELECT status_event_id FROM quote WHERE n = 2").fetchone()[0]
    repo = db.Repository(w["conn"], source="manual")
    repo.void_status_event(last, voided=True)
    repo.commit()
    assert start(w)["rate_request"] == w["ids"]["vr-quell"]
    assert start(w)["rate_hint"] == "from Quote 1 (Freight: Quell Freight)"
    repo.update("vendor_request", w["ids"]["vr-harrow"], {"awarded": 1})
    repo.commit()
    assert start(w) is None
    html = page(w)
    won = html[html.index('data-act-form="won"'):]
    won = won[:won.index("</form>")]
    assert 'value="{}" selected>Harrow Lines'.format(w["ids"]["vr-harrow"]) in won and "from Quote" not in won


def test_a_suggestions_won_form_starts_the_same_way(world):
    w = world
    quote(w, "qWonD000000000000001")
    pull(w)
    repo = db.Repository(w["conn"], source="test")
    sid = repo.insert("suggestion", {"enquiry_id": w["ids"]["OPP-9301"], "opp_no": "OPP-9301", "kind": "status",
                                     "proposed_status": "Won", "proposed_date": "2027-06-15", "state": "pending",
                                     "evidence_sender": "Lia Moran", "evidence_quote": "we go with your rate"})
    repo.commit()
    row = w["conn"].execute("SELECT * FROM suggestion WHERE id = ?", (sid,)).fetchone()
    got = suggestions.prefill(w["conn"], row, TODAY)
    assert (got["rate_used"], got["rate_request"], got["rate_hint"]) == (
        "agent", w["ids"]["vr-harrow"], "from Quote 1 (Freight: Harrow Lines)")


# ------------------------------------------------- 1. schema, the scheduled run --
def test_v56_from_v55_keeps_every_row(tmp_path):
    path = tmp_path / "old.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    p = add_party(repo, "Harrow Lines", country="USA")
    e = add_enquiry(repo, "OPP-9304", p, received="2027-06-01", scope="In")
    repo.insert("vendor_request", {"enquiry_id": e, "vendor_party_id": p, "asked_on": "2027-06-02"})
    repo.insert("vendor_rate", {"enquiry_id": e, "vendor_party_id": p, "site_id": "v1", "cur": "USD", "total": 100})
    repo.set_status(e, "Quoted", "2027-06-03", note="Sent [quote:qOld000000000000001]")
    repo.commit()
    conn.execute("DROP TABLE quote_line")
    conn.execute("DROP TABLE quote")
    conn.execute("UPDATE setting SET value = '55' WHERE key = 'schema_version'")
    conn.commit()
    before = {t: conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0]
              for t in ("enquiry", "status_event", "vendor_request", "vendor_rate", "change_log")}
    notes = schema_migrations.upgrade(conn)
    assert "quote table created" in notes and "quote_line table created" in notes
    assert "foreign_key_check: nothing" in notes
    counted = next(n for n in notes if n.startswith("rows before and after: "))
    for table, n in before.items():
        assert "{} {} / {}".format(table, n, n) in counted
    assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION
    assert schema_migrations.upgrade(conn) == []
    assert [r[1] for r in conn.execute("PRAGMA table_info(quote)")][:4] == ["id", "enquiry_id", "site_id", "n"]
    # the mark of an event from before is taken in by the next run like any other
    assert [w[3] for w in site_quotes.waiting(conn)] == ["qOld000000000000001"]
    conn.close()


def test_the_scheduled_run_takes_quotes_in(world):
    w = world
    quote(w, "qSched00000000000001")
    got = fs.sync_once(w["conn"], w["site"], w["state"], today=TODAY, reports_dir=w["path"].parent / "none")
    assert got.ok, got.reason
    assert got.pull.quotes == 1 and count(w, "quote") == 1
    assert not any(p.startswith(("quotes/", "quoteCosts/")) for c in w["site"].commits for p in c)
