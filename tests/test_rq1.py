"""Batch RQ1 (2026-11-16, no schema change): Read quote reads what the
trial got wrong - "$" alone is USD, a price list by place is alternatives,
a percentage of other charges has no number, a price with no unit on more
than one container is warned about, a valid-until day without its year,
a carrier not named yet, an approximate price; prompt version 2; and
read-quote-trial --again.

A fake API and a fake Outlook; no network. Every company, person, place,
amount, date and reference here is invented: cases OPP-9980 to OPP-9989,
domains .test.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from conftest import add_enquiry, add_party
from opplist import __main__ as cli, db, paths, read_quote as rq
from test_q2d import FakeAPI, FakeOutlook, KEY, answer, line, option, response

TODAY = "2027-05-12"


@pytest.fixture
def world(tmp_path, monkeypatch):
    conn = db.open_db(tmp_path / "rq1.db")
    repo = db.Repository(conn, source="test")
    for name, region, iso2 in (("USA", "North America", "US"), ("Chile", "Latin America", "CL")):
        repo.insert("country", {"name": name, "region": region, "iso2": iso2})
    agent = add_party(repo, "Pellmoor Cargo", country="Chile")
    vendor = add_party(repo, "Brindle Haulage", ptype="Carrier", country="USA")
    repo.insert("party_domain", {"domain": "brindle.test", "party_id": vendor})
    ids = {}
    # OPP-9980: FCL, 2 x 40HQ; OPP-9981: FCL, 1 x 40HQ; OPP-9982: Air, 300 kg
    for opp, mode in (("OPP-9980", "FCL"), ("OPP-9981", "FCL"), ("OPP-9982", "Air")):
        ids[opp] = add_enquiry(repo, opp, agent, received="2027-05-01", scope="In", service_mode=mode,
                               origin_country="USA", dest_country="Chile", incoterm="FOB")
        ids["vr" + opp] = repo.insert("vendor_request", {
            "enquiry_id": ids[opp], "vendor_party_id": vendor, "asked_on": "2027-05-02",
            "thread_key": "RATE REQUEST // " + opp, "parts": "Freight"})
    repo.add_cargo_line(ids["OPP-9980"], {"container_qty": 2, "container_type": "40HQ", "commodity": "Lamp bases"})
    repo.add_cargo_line(ids["OPP-9981"], {"container_qty": 1, "container_type": "40HQ", "commodity": "Lamp bases"})
    repo.add_cargo_line(ids["OPP-9982"], {"pieces": 2, "pack_unit": "Pallets", "weight_value": 300, "weight_unit": "kg",
                                          "cbm_value": 1, "commodity": "Lamp bases"})
    repo.commit()
    (paths.DATA_DIR / "secrets").mkdir(parents=True, exist_ok=True)
    (paths.DATA_DIR / "secrets" / "anthropic-key.txt").write_text(KEY + "\n", encoding="utf-8")
    monkeypatch.setattr(rq, "SLEEP", lambda s: None)
    monkeypatch.setattr(rq, "_now", lambda: dt.datetime(2027, 5, 12, 9, 30, 0))
    yield {"conn": conn, "ids": ids}
    conn.close()


def read(w, opp, ans, day="2027-05-11", api=None, **kw):
    api = api or FakeAPI(response(ans))
    got = rq.read(w["conn"], w["ids"]["vr" + opp], rq.from_paste("Rates below", day, TODAY), today=TODAY,
                  transport=api, **kw)
    return got, api


def draft(w, got):
    return json.loads(w["conn"].execute("SELECT draft FROM quote_read_draft WHERE quote_read_id = ? ORDER BY id",
                                        (got.id,)).fetchone()["draft"])


# ---------------------------------------------------------------- point 1 --
def test_a_dollar_sign_alone_is_usd_and_the_others_stay_theirs(world):
    got, _ = read(world, "OPP-9982", answer((None, [option([line("AIR FREIGHT", rate=2.4, cur="$")], cur="$")])))
    d = draft(world, got)
    assert d["options"][0]["cur"] == "USD" and d["warnings"] == []
    assert [rq._currency(c) for c in ("$", " $ ", "USD $", "US$", "R$", "MX$", "C$", "CLP")] == \
        ["USD", "USD", "USD", "USD", "BRL", "MXN", "CAD", "CLP"]
    assert '"$" alone is "$", unless the mail says which currency its prices are in' in rq.SYSTEM_PROMPT


# ---------------------------------------------------------------- point 2 --
def test_a_price_list_by_place_is_one_option_a_place(world):
    def ramp(name, dray, pull):
        return option([line("Drayage", seg="Origin", unit="CNTR", rate=dray), line("Pre-pull", seg="Origin", unit="CNTR",
                                                                                   rate=pull)], label=name)
    got, _ = read(world, "OPP-9980", answer((None, [ramp("Ramp Wexley", 410, 90), ramp("Ramp Corran", 520, 140),
                                                    ramp("Ramp Ossory", 480, 140)])))
    opts = draft(world, got)["options"]
    assert [o["label"] for o in opts] == ["Ramp Wexley", "Ramp Corran", "Ramp Ossory"]
    assert [[l["rate"] for l in o["lines"]] for o in opts] == [[410, 90], [520, 140], [480, 140]]
    prompt = rq.SYSTEM_PROMPT
    assert "rail ramps, ports, airports, terminals, zones, truck types" in prompt
    assert "never charges to add up" in prompt and "the places past the third go in the first option's \"notes\"" in prompt
    assert "Example, in invented words" in prompt


# ---------------------------------------------------------------- point 3 --
def test_a_percentage_of_other_charges_has_no_number(world):
    lines = [line("AIR FREIGHT", rate=2.4),
             line("Local tax", seg="Other", unit="", rate=4, as_written="% of USD AIR FREIGHT + FSC",
                  quote="Local tax: 4% USD AIR FREIGHT + FSC"),
             line("Collect fee", seg="Other", unit="", percent=3, percent_of="the total charges",
                  quote="Collect fee 3% of the total charges")]
    got, _ = read(world, "OPP-9982", answer((None, [option(lines)])))
    d = draft(world, got)
    tax, fee = d["options"][0]["lines"][1:]
    assert tax["charge"] == "Local tax (4% of USD AIR FREIGHT + FSC as written)" and tax["rate"] is None
    assert tax["unit"] == "" and tax["asWritten"] == ""
    assert fee["charge"] == "Collect fee (3% of the total charges as written)" and fee["rate"] is None
    assert d["warnings"] == ["percentages of other charges: Local tax (4% of USD AIR FREIGHT + FSC), Collect fee "
                             "(3% of the total charges); work out the amounts or leave them out"]
    assert d["options"][0]["cur"] == "USD"
    assert '"percent"' in rq.SYSTEM_PROMPT and "percent_of" in json.dumps(rq.answer_schema())


# ---------------------------------------------------------------- point 4 --
def test_no_unit_on_two_containers_is_warned_and_on_one_not(world):
    lines = [line("LH + FSC", seg="Origin", unit="", rate=625, quote="LH + FSC - $625"),
             line("Chassis", seg="Origin", unit="DAY", rate=45),
             line("Dry run", seg="Origin", unit="", as_written="per move", rate=150),
             line("Tolls", seg="Origin", unit="", word="AT COST")]
    got, _ = read(world, "OPP-9980", answer((None, [option(lines)])))
    assert "no unit in the mail for LH + FSC; the drawer counts each once (per container it would be 2 times)" \
        in draft(world, got)["warnings"]
    assert sum("no unit in the mail" in x for x in draft(world, got)["warnings"]) == 1
    got, _ = read(world, "OPP-9981", answer((None, [option(lines)])))
    assert not any("no unit in the mail" in x for x in draft(world, got)["warnings"])


# ---------------------------------------------------------------- point 5 --
@pytest.mark.parametrize("words, sent, valid, unclear", [
    ("Valid: Dec/31", "2027-05-11", "2027-12-31", ""),
    ("valid till 31 Dec", "2027-05-11", "2027-12-31", ""),
    ("validez 31/12", "2027-05-11", "2027-12-31", ""),
    ("good until 12/31", "2027-05-11", "2027-12-31", ""),
    ("Valid: Jan/15", "2027-05-11", "2028-01-15", ""),
    ("valid May 11", "2027-05-11", "2027-05-11", ""),
    ("valid until 31/12/2028", "2027-05-11", "2028-12-31", ""),
    ("valid 05/06", "2027-05-11", "", "05/06"),
    ("valid till ETD", "2027-05-11", "", ""),
    ("valid 15 days", "2027-05-11", "", ""),
])
def test_valid_until_from_a_day_and_month(words, sent, valid, unclear):
    assert rq.valid_from(words, sent) == (valid, unclear)


def test_valid_until_filled_from_the_words_or_left_with_a_warning(world):
    got, _ = read(world, "OPP-9982", answer((None, [option([line("AIR FREIGHT", rate=2.4)], valid_text="Valid: Dec/31")])))
    opt = draft(world, got)["options"][0]
    assert opt["valid"] == "2027-12-31" and "Valid:" not in opt["note"]
    got, _ = read(world, "OPP-9982", answer((None, [option([line("AIR FREIGHT", rate=2.4)], valid_text="valid 05/06")])))
    d = draft(world, got)
    assert d["options"][0]["valid"] == "" and d["options"][0]["note"] == "Valid: valid 05/06"
    assert d["warnings"] == ["valid until 05/06: day or month first?"]
    got, _ = read(world, "OPP-9982", answer((None, [option([line("AIR FREIGHT", rate=2.4)], valid="2027-06-30",
                                                           valid_text="Valid: Dec/31")])))
    assert draft(world, got)["options"][0]["valid"] == "2027-06-30"       # the reading's date stands


# ---------------------------------------------------------------- point 6 --
def test_a_carrier_not_named_yet_is_dropped(world):
    opts = [option([line("AIR FREIGHT", rate=2.4)], label=str(n), carrier=c)
            for n, c in enumerate(("PENDING", "tbc.", "Corvel Air"))]
    got, _ = read(world, "OPP-9982", answer((None, opts)))
    assert [o["carrier"] for o in draft(world, got)["options"]] == ["", "", "Corvel Air"]
    prompt = rq.SYSTEM_PROMPT
    assert "PENDING, TBA, TBC, TBD and \"to be confirmed\" are not names" in prompt
    assert "time to load or to wait at the door is not free time" in prompt


# ---------------------------------------------------------------- point 7 --
def test_an_approximate_price_is_warned_about(world):
    lines = [line("THC", seg="Origin", unit="SHIPMENT", rate=1200, approx=True, quote="THC: USD 1,200.00"),
             line("Handling", seg="Origin", unit="SHIPMENT", rate=60, quote="Handling: USD 60.00 approx."),
             line("Doc fee", seg="Origin", unit="SHIPMENT", rate=35, quote="Doc fee: USD 35.00")]
    got, _ = read(world, "OPP-9982", answer((None, [option(lines)])))
    assert draft(world, got)["warnings"] == ["the vendor says approximate: THC, Handling"]
    assert "approx" in json.dumps(rq.answer_schema())


# ---------------------------------------------------------------- point 8 --
def test_the_prompt_version_and_each_reading_records_it(world):
    got, api = read(world, "OPP-9982", answer((None, [option([line("AIR FREIGHT", rate=2.4)])])))
    assert rq.READ_QUOTE_PROMPT == 4           # RQ1c
    assert world["conn"].execute("SELECT prompt_version FROM quote_read WHERE id = ?", (got.id,)).fetchone()[0] == 4
    assert api.calls[0]["body"]["system"] == rq.SYSTEM_PROMPT


def test_an_answer_without_the_new_fields_fails_the_check(world):
    old = answer((None, [option([line("AIR FREIGHT", rate=2.4)])]))
    for l in old["routes"][0]["options"][0]["lines"]:
        del l["percent"], l["percent_of"], l["approx"]
    got, _ = read(world, "OPP-9982", old)
    assert got.status == "failed" and "OppList's check" in got.error


# --------------------------------------------------------------- point 10 --
def test_read_again_puts_each_beside_the_earlier_reading(world, monkeypatch):
    w = world
    conn = w["conn"]
    first = answer((None, [option([line("Drayage", seg="Origin", unit="", rate=410, cur="$"),
                                   line("Drayage", seg="Origin", unit="", rate=520, cur="$")], cur="$")]))
    source = rq.Source(kind="mail", text="Drayage Ramp Wexley $410, Ramp Corran $520", subject="RE: RATE REQUEST",
                       sender="Dara Vell <dara@brindle.test>", sent="2027-05-10 14:05", entry_id="E7",
                       message_id="<7@brindle.test>")
    earlier = rq.read(conn, w["ids"]["vrOPP-9980"], source, today=TODAY, transport=FakeAPI(response(first)), trial=True)
    conn.execute("UPDATE quote_read SET prompt_version = 1 WHERE id = ?", (earlier.id,))
    conn.commit()
    now = answer((None, [option([line("Drayage", seg="Origin", unit="", rate=410, cur="$")], label="Ramp Wexley"),
                         option([line("Drayage", seg="Origin", unit="", rate=520, cur="$")], label="Ramp Corran")]))
    api = FakeAPI(response(now))
    monkeypatch.setattr(rq, "OUTLOOK", FakeOutlook(body="Drayage Ramp Wexley $410, Ramp Corran $520"))
    path = rq.again(conn, object(), [earlier.id, 999], today=TODAY, transport=api)
    text = path.read_text(encoding="utf-8")
    assert path.name == "read-quote-again-20270512.md"
    assert "## 1. OPP-9980 Brindle Haulage: reading {} again".format(earlier.id) in text
    assert "### Earlier: reading {} (prompt version 1".format(earlier.id) in text
    assert "### Now: reading {} (prompt version 4)".format(earlier.id + 1) in text
    assert "Route not named, option -: currency $" in text and "Route not named, option Ramp Corran: currency USD" in text
    assert "Option Ramp Wexley: USD" in text and "per container it would be 2 times" in text
    assert "## 2. Reading 999" in text and "Not on file." in text
    assert "1 read, 0 no prices, 1 failed" in text and "2027-05: USD 0.07 in 2 readings" in text
    assert rq.OUTLOOK.calls[0] == ("read_item", "E7", "<7@brindle.test>")
    assert "Dara Vell <dara@brindle.test>" in json.dumps(api.calls[0]["body"])
    assert tuple(conn.execute("SELECT COUNT(*), SUM(trial) FROM quote_read").fetchone()) == (2, 2)
    assert conn.execute("SELECT COUNT(*) FROM quote_read_draft").fetchone()[0] == 0          # never pushed
    # a second run the same day keeps the first file
    api.replies.append(response(now))
    assert rq.again(conn, object(), [earlier.id], today=TODAY, transport=api).name == "read-quote-again-20270512-2.md"
    args = cli.build_parser().parse_args(["read-quote-trial", "--again", "2", "3", "4"])
    assert args.again == [2, 3, 4]
