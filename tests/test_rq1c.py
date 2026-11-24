"""Batch RQ1c (2026-11-16, no schema change): Read quote's answer schema
has no union types (a number not given is -1, a route not named 0, made
None before the check), and a paste that is only Outlook's list line is
not sent. Prompt version 4.

A fake API; no network. Every company, person, place, amount, date and
reference here is invented: cases OPP-9990 to OPP-9994, domains .test.
"""

from __future__ import annotations

import copy
import datetime as dt
import json

import pytest

from conftest import add_enquiry, add_party
from opplist import db, paths, read_quote as rq
from test_q2d import FakeAPI, KEY, answer, line, option, response

TODAY = "2027-06-08"


@pytest.fixture
def world(tmp_path, monkeypatch):
    conn = db.open_db(tmp_path / "rq1c.db")
    repo = db.Repository(conn, source="test")
    for name, region, iso2 in (("USA", "North America", "US"), ("Peru", "Latin America", "PE")):
        repo.insert("country", {"name": name, "region": region, "iso2": iso2})
    agent = add_party(repo, "Quenby Freight", country="Peru")
    vendor = add_party(repo, "Tolland Drayage", ptype="Carrier", country="USA")
    repo.insert("party_domain", {"domain": "tolland.test", "party_id": vendor})
    ids = {}
    for opp, mode in (("OPP-9992", "FCL"), ("OPP-9993", "Air")):
        ids[opp] = add_enquiry(repo, opp, agent, received="2027-06-01", scope="In", service_mode=mode,
                               origin_country="USA", dest_country="Peru", incoterm="FOB")
        ids["vr" + opp] = repo.insert("vendor_request", {
            "enquiry_id": ids[opp], "vendor_party_id": vendor, "asked_on": "2027-06-02",
            "thread_key": "RATE REQUEST // " + opp, "parts": "Freight"})
    repo.add_cargo_line(ids["OPP-9992"], {"container_qty": 3, "container_type": "40HQ", "commodity": "Kiln shelves"})
    repo.add_cargo_line(ids["OPP-9993"], {"pieces": 4, "pack_unit": "Pallets", "weight_value": 380, "weight_unit": "kg",
                                          "cbm_value": 2.4, "commodity": "Kiln shelves"})
    repo.commit()
    (paths.DATA_DIR / "secrets").mkdir(parents=True, exist_ok=True)
    (paths.DATA_DIR / "secrets" / "anthropic-key.txt").write_text(KEY + "\n", encoding="utf-8")
    monkeypatch.setattr(rq, "SLEEP", lambda s: None)
    monkeypatch.setattr(rq, "_now", lambda: dt.datetime(2027, 6, 8, 10, 0, 0))
    yield {"conn": conn, "ids": ids}
    conn.close()


def read(w, opp, ans):
    api = FakeAPI(response(ans))
    got = rq.read(w["conn"], w["ids"]["vr" + opp], rq.from_paste("Rates below", "2027-06-07", TODAY),
                  today=TODAY, transport=api)
    return got, api


def drafts(w, got):
    return [json.loads(r["draft"]) for r in w["conn"].execute(
        "SELECT draft FROM quote_read_draft WHERE quote_read_id = ? ORDER BY id", (got.id,))]


def as_sent(ans):
    """The same answer as the API sends it under RQ1c's schema: -1 for each
    number not given, 0 for a route not named."""
    ans = copy.deepcopy(ans)
    for route in ans["routes"]:
        if route["route"] is None:
            route["route"] = 0
        for opt in route["options"]:
            for l in opt["lines"]:
                for name in rq.LINE_NUMBERS:
                    if l[name] is None:
                        l[name] = -1
                for b in l["breaks"]:
                    for name in rq.BREAK_NUMBERS:
                        if b[name] is None:
                            b[name] = -1
    return ans


FCL = answer((None, [option([
    line("Ocean freight", unit="40'HQ", rate=2150, quote="O/F USD 2150/40HC"),
    line("Fuel", unit="", percent=6, percent_of="ocean freight", quote="+6% fuel"),
    line("Doc fee", seg="Origin", unit="SHIPMENT", rate=45, quote="Doc USD 45"),
    line("Inspection", seg="Customs", unit="SHIPMENT", word="AT COST", quote="Inspection at cost")],
    label="Weekly, direct")]))
AIR = answer((None, [option([
    line("Air freight", unit="KG", breaks=[{"from_kg": None, "to_kg": 45, "rate": 4.1},
                                          {"from_kg": 45, "to_kg": None, "rate": 3.6}],
         minimum=95, quote="AF -45 4.10 / +45 3.60, min 95"),
    line("Screening", seg="Origin", unit="KG", rate=0.15, quote="Screening 0.15/kg")])]))


# ---------------------------------------------------------------- point 1 --
def test_the_schema_has_no_union_type_and_is_strict_everywhere():
    def walk(node):
        if isinstance(node, dict):
            for bad in ("anyOf", "oneOf", "allOf"):
                assert bad not in node
            if "type" in node:
                assert isinstance(node["type"], str)
            if node.get("type") == "object":
                assert node["additionalProperties"] is False and set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    schema = rq.answer_schema()
    walk(schema)
    route = schema["properties"]["routes"]["items"]
    assert route["properties"]["route"] == {"type": "integer"}
    the_line = route["properties"]["options"]["items"]["properties"]["lines"]["items"]["properties"]
    assert all(the_line[n] == {"type": "number"} for n in rq.LINE_NUMBERS)
    assert "files" in schema["required"]


def test_the_prompt_says_minus_one_and_zero_never_null():
    assert "null" not in rq.SYSTEM_PROMPT
    assert "A number the mail does not give is -1, never 0." in rq.SYSTEM_PROMPT
    assert "or 0 when it does not say and you cannot tell" in rq.SYSTEM_PROMPT
    assert rq.READ_QUOTE_PROMPT == 4


# ---------------------------------------------------------------- point 2 --
def test_nulls_makes_minus_one_and_zero_none():
    sent = as_sent(AIR)
    sent["routes"][0]["options"][0]["lines"][1]["qty"] = 0
    got = rq.nulls(sent)
    assert got["routes"][0]["route"] is None
    af, screening = got["routes"][0]["options"][0]["lines"]
    assert (af["rate"], af["min"], af["qty"], af["amount"], af["percent"]) == (None, 95, None, None, None)
    assert af["breaks"] == [{"from_kg": None, "to_kg": 45, "rate": 4.1}, {"from_kg": 45, "to_kg": None, "rate": 3.6}]
    assert screening["qty"] is None and screening["rate"] == 0.15
    assert rq.nulls(copy.deepcopy(AIR)) == AIR                     # an answer with None stays as it is
    assert rq.nulls("not an answer") == "not an answer"


@pytest.mark.parametrize("opp,ans", [("OPP-9992", FCL), ("OPP-9993", AIR)])
def test_minus_ones_make_the_same_drafts_as_nulls(world, opp, ans):
    with_nulls, _ = read(world, opp, ans)
    with_minus, api = read(world, opp, as_sent(ans))
    assert with_minus.status == "read" and with_minus.error == ""
    assert drafts(world, with_minus) == drafts(world, with_nulls)
    # the kept response is the answer as the API sent it; read again it is the same
    row = world["conn"].execute("SELECT response FROM quote_read WHERE id = ?", (with_minus.id,)).fetchone()
    kept = json.loads(json.loads(row[0])["content"][0]["text"])
    assert kept["routes"][0]["route"] == 0
    assert rq.answer_of(row[0]) == rq.nulls(as_sent(ans)) == ans


def test_a_qty_of_nought_is_counted_from_the_cargo(world):
    zero = as_sent(FCL)
    zero["routes"][0]["options"][0]["lines"][0]["qty"] = 0
    with_zero, _ = read(world, "OPP-9992", zero)
    with_nulls, _ = read(world, "OPP-9992", FCL)
    assert drafts(world, with_zero) == drafts(world, with_nulls)
    freight = [l for l in drafts(world, with_zero)[0]["options"][0]["lines"] if l["charge"] == "Ocean freight"]
    assert freight[0]["qty"] is None            # left to the drawer, which counts 3 x 40HQ, never 0


def test_the_again_report_reads_a_kept_response_through_the_same_step(world):
    first, _ = read(world, "OPP-9993", as_sent(AIR))
    api = FakeAPI(response(as_sent(AIR)))
    text = rq.again(world["conn"], object(), [first.id], today=TODAY, transport=api).read_text(encoding="utf-8")
    assert "-1" not in text
    assert text.count("Air freight") >= 2


# ---------------------------------------------------------------- point 4 --
LIST_LINE = ("From\tSubject\tReceived\tSize\tCategories\n"
             "Imre Sallow\tRE: RATE REQUEST // MIA - CLL\tMon 6/7/2027 4:20 PM\t88 KB\t")


def test_a_paste_of_outlooks_list_line_is_not_sent(world):
    before = world["conn"].execute("SELECT COUNT(*) FROM quote_read").fetchone()[0]
    with pytest.raises(rq.ReadQuoteError) as err:
        rq.from_paste(LIST_LINE, "2027-06-07", TODAY)
    assert str(err.value) == ("This is the line from Outlook's message list, not the mail. Open the mail, click in "
                              "its text, press Ctrl+A and Ctrl+C, and paste again.")
    assert world["conn"].execute("SELECT COUNT(*) FROM quote_read").fetchone()[0] == before
    assert rq.outlook_list_line("From\tSubject\tReceived\r\nrow one\r\nrow two")


@pytest.mark.parametrize("text", [
    LIST_LINE + "\nrow two\nrow three",                                    # more than 3 lines
    "From: Imre Sallow\nSubject: RE: RATE REQUEST // MIA - CLL\nReceived: today\nO/F USD 2150",
    "From Subject Received\nO/F USD 2150",                                 # no tabs
    "From\tSubject\tSize\nrow one",                                        # no Received
])
def test_a_mail_that_mentions_the_words_is_sent(text):
    assert not rq.outlook_list_line(text)
    assert rq.from_paste(text, "2027-06-07", TODAY).kind == "paste"
