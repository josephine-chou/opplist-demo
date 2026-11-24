"""Batch 13b (2026-09-29, schema v34): the Cargo card keeps what was read and
checks it. Every mail text, name, lane, date and number in this file is
invented (tests use invented values only); case numbers start at
OPP-2001.

The four shapes of export-preview\\OppList_CargoCheck_SAMPLE.html - a total
above its lines, 'each' with a line repeated word for word, an LCL line, a
'1.250 Kgs' that means 1,250 - and one fixture per rule of the batch."""

from __future__ import annotations

import csv
import json
import re

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import (cargo, cargo_checks, cargo_report, db, intake, outlook_bridge, parsing, routes,
                     schema_migrations)
from opplist.web import create_app

LBS = 0.45359237

# ---------------------------------------------------------------- mails ----
TOTAL_AND_LINES = """Hello team,
Please quote air freight, door to airport.
Total Packages: 4 Packages
Total Gross Weight: 2,650 LBS
• 50 x 40 x 30 inches – 1,200 LBS (1 package)
• 50 x 40 x 30 inches – 900 LBS (1 package)
• 30 x 20 x 20 inches – 330 LBS (1 package)
• 30 x 20 x 20 inches – 220 LBS (1 package)
Thank you."""

EACH_AND_REPEAT = """Please advise your best rate.
* 3 pallets – 40x30x36 in, 200 lbs each (Stackable)
* 1 box – 12x10x8 in, 20 lbs
* 4 pallets – 40x30x36 in, 150 lbs each
* 4 pallets – 40x30x36 in, 150 lbs each
Best regards"""

LCL_LINE = """Good day,
CARGO DETAILS 6 pallets (110x90x80cm)
Estimated weight: 3,300kg
Kind regards"""

SHIPMENTS = """Dear team,
Shipment 1 :
CRATE 1 : DIM : 40 x 30 x 30 INCH, GW : 300 KG
CRATE 2 : DIM : 40 x 30 x 30 INCH, GW : 250 KG
TOTAL: 02 CRATE / total GW : 550 KG
Destination : by air to Lakeport airport
Shipment 2 :
pallet 1 : 100x80x90 cm / 120 kgs
pallet 2 : 100x80x90 cm / 130 kgs
Total : 2 pallets / 300 kgs
Destination : by air to Lakeport airport
Best regards"""

THOUSANDS = """Dear partner,
Gross weight: 1.450 Kgs
1 Wooden Case - 120cm x 150cm x 110cm = 1.250 Kgs
1 Wooden Case - 60cm x 80cm x 50cm = 200 Kgs
2 Wooden Case
Regards"""


#: The OPP-1464 shape (13b): a stated total in dot-thousands, the lines
#: '1 <package> - L x W x H cm = N Kgs' with the first N in dot-thousands,
#: then an older request quoted below 'From:' that must not count.
DOT_THOUSANDS_WITH_QUOTE = """Dear Sir,
Please quote air freight to Lakeport for the crates below.
Berat
1.250 Kgs
Qty package
3 Wooden Case
Dimensi
1 Wooden Case - 120cm x 150cm x 110cm = 1.020 Kgs
1 Wooden Case - 80cm x 90cm x 70cm = 150 Kgs
1 Wooden Case - 50cm x 60cm x 40cm = 80 Kgs
Best regards,
Rina Kusuma
Kalderan Forwarding

From: Rina Kusuma <rina@kalderan.test>
Sent: Friday, October 2, 2026 9:15 AM
Subject: RFQ PO-77001
Total Packages: 6 Packages
Total Gross Weight: 4,400 LBS
\u2022 50 x 40 x 30 inches \u2013 1,200 LBS (1 package)
\u2022 50 x 40 x 30 inches \u2013 900 LBS (1 package)
"""


def read(text, mail_id="ENTRY-TEST-1"):
    """(lines, stated) the way intake reads a body: blocks, then lines."""
    out, stated = [], []
    for block in parsing.cargo_blocks(parsing.body_lines(text)):
        if not block.has_cargo:
            continue
        lines, total = cargo.lines_from_block(block, mail_id)
        for line in lines:
            line["id"] = len(out)
            line["stated_id"] = len(stated) + 1 if total else None
            out.append(line)
        if total:
            stated.append(dict(total, id=len(stated) + 1))
    return out, stated


def totals_of(lines, mode):
    return routes.totals(None, None, lines=lines, live_routes=[], case={"service_mode": mode})


def checks_of(lines, stated, mode, qty_raw=None, lim=None, decided=None):
    case = {"service_mode": mode, "qty_raw": qty_raw, "cargo_decided": decided}
    return cargo_checks.for_case(case, lines, stated, totals_of(lines, mode), lim)


# ------------------------------------------------ the four sample shapes ----
def test_a_total_above_its_lines_is_a_check_not_another_line():
    lines, stated = read(TOTAL_AND_LINES)
    assert [(l["pieces"], l["dim_l"], l["dims_unit"], l["weight_value"], l["weight_unit"]) for l in lines] == [
        (1, 50, "in", 1200, "lbs"), (1, 50, "in", 900, "lbs"), (1, 30, "in", 330, "lbs"), (1, 30, "in", 220, "lbs")]
    assert len(stated) == 1 and stated[0]["pieces"] == 4 and stated[0]["weight_value"] == 2650
    assert stated[0]["source_text"] == "Total Packages: 4 Packages\nTotal Gross Weight: 2,650 LBS"
    t = totals_of(lines, "Air")
    assert round(t["gross_kg"], 1) == round(2650 * LBS, 1) == 1202.0      # the lines, not lines plus total
    found = checks_of(lines, stated, "Air")
    total = [c for c in found if c.code == "total"]
    assert [c.kind for c in total] == ["ok"] and "add up to the same" in total[0].text
    assert cargo_checks.asks(found) == 0
    # a total the lines do not reach: amber, with both numbers
    lines[3]["weight_value"], lines[3]["weight_kg"] = 120, 120 * LBS
    total = [c for c in checks_of(lines, stated, "Air") if c.code == "total"][0]
    assert total.kind == "ask" and "2,650 LBS" in total.text and "1,156.7 kg" in total.text


def test_each_is_multiplied_and_a_repeated_line_is_not_counted():
    lines, stated = read(EACH_AND_REPEAT)
    assert stated == []
    assert [(l["pieces"], l["weight_basis"], round(l["weight_kg"], 1), l["counted"]) for l in lines] == [
        (3, "each", round(600 * LBS, 1), 1), (1, "total", round(20 * LBS, 1), 1),
        (4, "each", round(600 * LBS, 1), 1), (4, "each", round(600 * LBS, 1), 0)]
    t = totals_of(lines, "Air")
    assert round(t["gross_kg"], 2) == round(1220 * LBS, 2)
    repeat = [c for c in checks_of(lines, stated, "Air") if c.code == "repeat"]
    assert len(repeat) == 1 and repeat[0].kind == "ask" and repeat[0].row == 4
    assert repeat[0].text.startswith("Line 4 repeats line 3") and repeat[0].actions == [("Count it", "count", "1")]
    # counted as she chose: green, and it can be taken back
    lines[3]["counted"], lines[3]["decided"] = 1, "repeat"
    repeat = [c for c in checks_of(lines, stated, "Air") if c.code == "repeat"][0]
    assert repeat.kind == "ok" and repeat.actions == [("Count once", "count", "0")]


def test_an_lcl_line_has_no_air_words_and_its_w_m():
    lines, stated = read(LCL_LINE)
    assert len(lines) == 1 and stated == []
    line = lines[0]
    assert (line["pieces"], line["pack_word"], line["dim_l"], line["dims_unit"], line["weight_value"]) == \
        (6, "pallets", 110, "cm", 3300)
    t = totals_of(lines, "LCL")
    assert round(t["volume_cbm"], 3) == 4.752 and t["lcl_cbm"] == 4.752 and t["air_weight_kg"] is None
    assert checks_of(lines, stated, "LCL") == []
    labels = parsing.parse_cargo_labels(["Pieces: 3", "Gross weight: 400 kg", "Volume: 2 cbm", "Chargeable: 400 kg"])
    assert "chargeable" not in labels.qty_raw and "volumetric" not in labels.qty_raw


def test_a_weight_that_reads_two_ways_takes_the_plausible_one():
    lines, stated = read(THOUSANDS)
    assert [(l["weight_value"], l["weight_reading"], l["weight_written"]) for l in lines] == [
        (1250.0, "auto", "1.250 Kgs"), (200.0, None, "200 Kgs")]
    assert stated[0]["weight_value"] == 1450.0 and stated[0]["pieces"] == 2     # the total read the same way
    found = checks_of(lines, stated, "Air")
    reading = [c for c in found if c.code == "reading"][0]
    assert reading.kind == "ok" and '"1.250 Kgs" read as 1,250 kg' in reading.text
    assert reading.actions == [("Use 1.25 kg", "reading", "1.25")]
    assert [c.kind for c in found if c.code == "total"] == ["ok"]
    assert cargo_checks.asks(found) == 0


def test_two_shipments_give_their_lines_each_checked_against_its_own_total():
    lines, stated = read(SHIPMENTS)
    assert [(l["label"], l["pieces"], l["pack_word"], l["weight_value"], l["stated_id"]) for l in lines] == [
        ("Shipment 1", 1, "crate", 300, 1), ("Shipment 1", 1, "crate", 250, 1),
        ("Shipment 2", 1, "pallet", 120, 2), ("Shipment 2", 1, "pallet", 130, 2)]
    assert {l["deliver_to"] for l in lines} == {"Lakeport airport"}
    assert [(s["pieces"], s["pack_word"], s["weight_kg"]) for s in stated] == [(2, "crates", 550), (2, "pallets", 300)]
    totals = [c for c in checks_of(lines, stated, "Air") if c.code == "total"]
    assert [c.kind for c in totals] == ["ok", "ask"]
    assert "2 pallets, 300 kgs" in totals[1].text and "add up to 250.0 kg" in totals[1].text


# ------------------------------------------------------------ the rules ----
def test_rule_1_the_mail_text_is_kept_and_shown_numbered_like_the_rows():
    lines, stated = read(TOTAL_AND_LINES, mail_id="ENTRY-2001")
    assert {l["source_mail"] for l in lines} == {"ENTRY-2001"}
    assert lines[0]["source_text"] == "• 50 x 40 x 30 inches – 1,200 LBS (1 package)"
    shown = cargo_checks.mail_text(lines, stated, "whatever the case says")
    assert [m for m, _ in shown] == ["total", "1", "2", "3", "4"]
    # a manual line has nothing to show; a case without lines shows its qty_raw
    assert cargo_checks.mail_text([{"source_text": None}], [], "2 crates, 300 kg") == [("", "2 crates, 300 kg")]
    assert cargo_checks.mail_text([], [], None) == []


def test_rule_2_a_total_alone_is_the_cargo_and_a_missing_weight_is_carried_once():
    lines, stated = read("Please quote.\nTotal: 6 cartons / 180 kg\nThanks")
    assert stated == [] and [(l["pieces"], l["pack_word"], l["weight_value"]) for l in lines] == [(6, "cartons", 180)]
    # the lines give sizes only, the total the weight: one more line carries it
    lines, stated = read("3 crates 100x80x60 cm\n2 crates 50x40x40 cm\nTotal gross weight: 640 kg")
    assert [l["label"] for l in lines] == ["", "", "Total"] and lines[2]["weight_value"] == 640
    assert stated is None or stated == [] or stated[0]["weight_kg"] is None
    t = totals_of(lines, "Air")
    assert t["gross_kg"] == 640 and round(t["volume_cbm"], 3) == round(3 * 0.48 + 2 * 0.08, 3)
    # a summary line above its lines, without the word 'Total'
    body = TOTAL_AND_LINES.replace("Total Packages: 4 Packages\nTotal Gross Weight: 2,650 LBS", "4 packages, 2,650 lbs")
    lines, stated = read(body)
    assert len(lines) == 4 and stated[0]["pieces"] == 4 and stated[0]["weight_value"] == 2650


def test_rule_3_a_repeat_counts_only_within_one_mail():
    lines, _ = read(EACH_AND_REPEAT)
    lines[3]["source_mail"] = "ENTRY-OTHER"
    lines[3]["counted"] = 1
    assert [c for c in checks_of(lines, [], "Air") if c.code == "repeat"] == []


@pytest.mark.parametrize("word", ["each", "c/u", "cada", "por bulto", "per piece", "por volume"])
def test_rule_4_each_words_multiply_by_the_pieces(word):
    lines, _ = read("5 cartons 40x40x40 cm, 12 kg {}".format(word))
    assert lines[0]["weight_basis"] == "each" and lines[0]["weight_kg"] == 60


def test_rule_4_per_piece_with_no_pieces_is_kept_not_counted_and_asked():
    lines, _ = read("Dimensions: 60x40x40 cm\nWeight: 30 kg each")
    line = lines[0]
    assert line["pieces"] is None and line["weight_value"] == 30 and line["weight_kg"] is None
    assert totals_of(lines, "Air")["gross_kg"] is None
    each = [c for c in checks_of(lines, [], "Air") if c.code == "each"][0]
    assert each.kind == "ask" and "30 kg each" in each.text and "How many pieces?" in each.text
    lines, _ = read("4 crates 50x50x50 cm, 0.2 cbm each, 80 kg")
    assert lines[0]["cbm_basis"] == "each" and lines[0]["cbm"] == 0.8


def test_rule_5_the_two_readings_of_a_number():
    assert parsing.readings("1.940") == (1.94, 1940.0)
    assert parsing.readings("1,940") == (1940.0, 1.94)
    for single in ("1.5", "36,5", "0.597", "12", "1,250.50"):
        assert parsing.readings(single)[1] is None, single
    # no volume: the plain reading stays and the card asks with both
    lines, _ = read("Weight: 2.400 kgs")
    assert lines[0]["weight_value"] == 2.4 and lines[0]["weight_reading"] is None
    reading = [c for c in checks_of(lines, [], "Air") if c.code == "reading"][0]
    assert reading.kind == "ask" and "2.4 kg or as 2,400 kg" in reading.text
    assert reading.actions[0] == ("Use 2,400 kg", "reading", "2400")
    # 'Keep' answers it
    lines[0]["decided"] = "reading"
    assert [c for c in checks_of(lines, [], "Air") if c.code == "reading"] == []


def test_rule_6_stated_values_are_compared_and_ocean_text_has_no_air_words():
    body = "Gross weight 300 kg, chargeable weight 520 kg\n2 pallets 120x100x100 cm, 150 kg each"
    lines, stated = read(body)
    assert len(lines) == 1 and stated[0]["chargeable_kg"] == 520 and stated[0]["weight_kg"] == 300
    found = checks_of(lines, stated, "Air")
    charge = [c for c in found if c.code == "chargeable"][0]
    assert charge.kind == "ask" and "520" in charge.text and "400" in charge.text     # 2.4 cbm = 400 kg
    near = read(body.replace("520 kg", "410 kg"))
    assert [c.kind for c in checks_of(*near, "Air") if c.code == "chargeable"] == ["ok"]
    assert [c.code for c in checks_of(*near, "LCL") if c.code == "chargeable"] == []     # not on ocean
    # a line's own stated volume against its size
    lines, _ = read("2 crates 100x100x100 cm, 300 kg, 3 cbm")
    stated_cbm = [c for c in checks_of(lines, [], "LCL") if c.code == "stated_cbm"][0]
    assert stated_cbm.kind == "ask" and "3 cbm" in stated_cbm.text and "2 cbm" in stated_cbm.text


def test_rule_7_the_checks_and_their_limits():
    lim = dict(cargo_checks.DEFAULT_LIMITS)
    density = [cargo.normalise({"id": 0, "pieces": 1, "dim_l": 200, "dim_w": 100, "dim_h": 150, "dims_unit": "cm",
                                "weight_value": 5, "weight_unit": "kg", "counted": 1})]
    assert [c.code for c in checks_of(density, [], "Air", lim=lim) if c.kind == "ask"] == ["density"]
    big_air = [cargo.normalise({"id": 0, "pieces": 10, "dim_l": 200, "dim_w": 200, "dim_h": 100, "dims_unit": "cm",
                                "weight_value": 4000, "weight_unit": "kg", "counted": 1})]
    assert "air" in [c.code for c in checks_of(big_air, [], "Air", lim=lim)]
    big_lcl = [dict(big_air[0])]
    lcl = [c for c in checks_of(big_lcl, [], "LCL", lim=lim) if c.code == "lcl"][0]
    assert lcl.kind == "ask" and "Is it FCL?" in lcl.text
    assert [c for c in checks_of(big_lcl, [], "LCL", lim=dict(lim, lcl_max_cbm=50)) if c.code == "lcl"] == []
    heavy = [cargo.normalise({"id": 0, "container_type": "40HQ", "container_qty": 1, "weight_value": 30,
                              "weight_unit": "t", "counted": 1})]
    assert [c.code for c in checks_of(heavy, [], "FCL", lim=lim)] == ["fcl"]
    light = [dict(heavy[0], container_type="20GP", weight_kg=18000.0)]
    assert checks_of(light, [], "FCL", lim=lim) == []
    unitless, _ = read("1 pallet 64 x 48 x 40, 500 kg")
    unit = [c for c in checks_of(unitless, [], "Air") if c.code == "unit"][0]
    # 15c: Use in first - 500 kg in 64 x 48 x 40 cm is far too dense, in inches it fits
    assert unit.actions == [("Use in", "unit", "in"), ("Use cm", "unit", "cm")] and unitless[0]["length_cm"] is None
    mode = [c for c in checks_of([], [], "Air", qty_raw="FCL:2 x 40HQ") if c.code == "qty_mode"][0]
    assert "FCL:2 x 40HQ" in mode.text and mode.kind == "ask"
    assert [c.code for c in checks_of([], [], "LCL", qty_raw="4 crates, volumetric 300 kg")] == ["qty_mode"]
    assert checks_of([], [], "Multi-mode", qty_raw="Air:40 | LCL:0.3") == []
    # answered on the case: green
    answered = checks_of([], [], "Air", qty_raw="FCL:2 x 40HQ", decided="qty_mode")
    assert [c.kind for c in answered] == ["ok"]


# ----------------------------------------------------------- the app ----
@pytest.fixture
def app_world(tmp_path):
    path = tmp_path / "cargo14.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "next_opp_number", 2010)
    party = add_party(repo, "Harbrook Forwarding", country="USA")
    case = add_enquiry(repo, "OPP-2001", party, received="2026-10-05", service_mode="Air",
                       origin_loc="MIA", origin_country="USA", dest_loc="SAI", dest_country="Chile")
    lines, stated = read(EACH_AND_REPEAT, mail_id="ENTRY-2001")
    for line in lines:
        line.pop("id"), line.pop("stated_id"), line.pop("evidence", None)
        repo.add_cargo_line(case, line)
    manual = add_enquiry(repo, "OPP-2002", party, received="2026-10-06", service_mode="LCL",
                         origin_loc="MIA", origin_country="USA", dest_loc="SAI", dest_country="Chile",
                         qty_raw="2 crates, 300 kg, 1.5 cbm")
    conn.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, {"party": party, "case": case, "manual": manual}


def test_the_case_page_shows_the_mail_text_the_checks_and_their_count(app_world):
    client, path, ids = app_world
    body = client.get("/enquiries/{}".format(ids["case"])).data.decode("utf-8")
    assert 'id="cargo-check-count" data-n="1">check 1<' in body
    assert "Show the mail text" in body and 'id="cargo-source" hidden' in body
    assert '<span class="mk">4</span><span class="src-text">* <span class="num">4</span> pallets' in body
    assert "Line 4 repeats line 3" in body and 'name="answer" value="count|' in body
    # a case without lines shows its quantity text
    body = client.get("/enquiries/{}".format(ids["manual"])).data.decode("utf-8")
    assert '<span class="mk"></span><span class="src-text"><span class="num">2</span> crates' in body


def test_answering_a_check_writes_through_the_repository(app_world):
    client, path, ids = app_world
    conn = db.open_db(path)
    fourth = conn.execute("SELECT id FROM enquiry_cargo WHERE enquiry_id = ? AND seq = 4", (ids["case"],)).fetchone()[0]
    logged = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    conn.close()
    client.post("/enquiries/{}/cargo".format(ids["case"]), data={"action": "answer", "answer": "count|{}|1".format(fourth)})
    client.post("/enquiries/{}/cargo".format(ids["case"]), data={"action": "answer", "answer": "keep_case||air"})
    conn = db.open_db(path)
    line = conn.execute("SELECT counted, decided FROM enquiry_cargo WHERE id = ?", (fourth,)).fetchone()
    case = conn.execute("SELECT cargo_decided, gross_kg FROM enquiry WHERE id = ?", (ids["case"],)).fetchone()
    assert (line["counted"], line["decided"]) == (1, "repeat") and case["cargo_decided"] == "air"
    assert round(case["gross_kg"], 2) == round(1820 * LBS, 2)                 # the fourth line counts now
    assert conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] > logged
    conn.close()
    body = client.get("/enquiries/{}".format(ids["case"])).data.decode("utf-8")
    assert "counted, as you chose" in body and 'id="cargo-check-count"' not in body
    assert client.post("/enquiries/{}/cargo".format(ids["case"]),
                       data={"action": "answer", "answer": "nonsense||"}).status_code == 400


def test_a_size_without_unit_gets_one_from_the_answer(app_world):
    client, path, ids = app_world
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    lines, _ = read("1 pallet 64 x 48 x 40, 500 kg")
    line = dict(lines[0])
    for key in ("id", "stated_id", "evidence"):
        line.pop(key, None)
    line_id = repo.add_cargo_line(ids["manual"], line)
    conn.commit()
    conn.close()
    client.post("/enquiries/{}/cargo".format(ids["manual"]), data={"action": "answer", "answer": "unit|{}|in".format(line_id)})
    conn = db.open_db(path)
    row = conn.execute("SELECT dims_unit, length_cm FROM enquiry_cargo WHERE id = ?", (line_id,)).fetchone()
    conn.close()
    assert row["dims_unit"] == "in" and row["length_cm"] == round(64 * 2.54, 3)


def test_register_from_outlook_shows_the_card_and_saves_what_was_read(app_world, monkeypatch):
    client, path, ids = app_world
    mail = outlook_bridge.MailInfo(
        subject="RFQ air MIA-SAI crates 2026-10-07", sender_name="Pat Doe", sender_address="pat@harbrook.test",
        sender_internal=False, received_on="2026-10-07", received_at="2026-10-07 09:10", to_addresses=[],
        cc_addresses=[], to_names=[], cc_names=[], display_to="", conversation_id="CONV-2003",
        entry_id="ENTRY-2003", body_excerpt=TOTAL_AND_LINES, message_id="")
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: mail)
    body = client.post("/enquiries/from-outlook").data.decode("utf-8")
    # 15c: the register page's lines are 15b's editors; + Add line makes a new one from the template
    assert 'id="reg-lines"' in body and 'id="reg-line-tpl"' in body
    assert 'name="cargo_3_deliver_to"' in body and 'name="cargo___I___source" data-f="source" value="manual"' in body
    depth = deepest = 0
    for tag in re.findall(r"<(/?)form\b", body):
        depth += -1 if tag else 1
        deepest = max(deepest, depth)
    assert deepest == 1                                          # nothing nests in the register form
    assert 'name="cargo_0_source_text"' in body and 'name="stated_0_weight_value" value="2650.0"' in body
    assert "add up to the same" in body and "Show the mail text" in body
    conn = db.open_db(path)
    draft = intake.build(conn, mail)
    conn.close()
    form = {"received_on": "2026-10-07", "requester_party_id": str(ids["party"]), "service_mode": "Air",
            "origin_country": "USA", "origin_loc": "MIA", "dest_country": "Chile", "dest_loc": "SAI",
            "created_by": "outlook", "entry_id": "ENTRY-2003", "decision": "new", "cargo_decided": "air"}
    for i, line in enumerate(draft.cargo_lines):
        for key in cargo.FIELDS + ("source", "stated_ref"):
            if line.get(key) is not None:
                form["cargo_{}_{}".format(i, key)] = str(line[key])
        form["cargo_{}_counted_present".format(i)] = "1"
        form["cargo_{}_counted".format(i)] = "1"
    for j, total in enumerate(draft.cargo_stated):
        for key in cargo.STATED_FIELDS:
            form["stated_{}_{}".format(j, key)] = "" if total.get(key) is None else str(total[key])
    page = client.post("/enquiries/new", data=form, follow_redirects=True)
    assert page.status_code == 200, page.data[:400]
    conn = db.open_db(path)
    case = conn.execute("SELECT * FROM enquiry WHERE entry_id = 'ENTRY-2003'").fetchone()
    lines = conn.execute("SELECT * FROM enquiry_cargo WHERE enquiry_id = ? ORDER BY seq", (case["id"],)).fetchall()
    stated = conn.execute("SELECT * FROM enquiry_cargo_stated WHERE enquiry_id = ?", (case["id"],)).fetchall()
    conn.close()
    assert case["opp_no"] == "OPP-2010" and case["cargo_decided"] == "air"
    assert len(lines) == 4 and {l["source_mail"] for l in lines} == {"ENTRY-2003"}
    assert lines[0]["source_text"].endswith("1,200 LBS (1 package)") and lines[0]["weight_unit"] == "lbs"
    assert len(stated) == 1 and stated[0]["pieces"] == 4 and {l["stated_id"] for l in lines} == {stated[0]["id"]}
    assert round(case["gross_kg"], 1) == 1202.0


def test_the_0464_shape_through_the_register_path(app_world):
    """13b: the reader keeps all the lines of the dot-thousands shape, reads
    1.020 as 1,020 by its density, matches the stated total, and nothing of
    the older request quoted below 'From:' counts."""
    client, path, ids = app_world
    mail = outlook_bridge.MailInfo(
        subject="RFQ air crates to Lakeport", sender_name="Rina Kusuma", sender_address="rina@kalderan.test",
        sender_internal=False, received_on="2026-10-09", received_at="2026-10-09 10:00", to_addresses=[],
        cc_addresses=[], to_names=[], cc_names=[], display_to="", conversation_id="CONV-2006",
        entry_id="ENTRY-2006", body_excerpt=DOT_THOUSANDS_WITH_QUOTE, message_id="")
    conn = db.open_db(path)
    draft = intake.build(conn, mail)
    conn.close()
    lines = [cargo.normalise(dict(l)) for l in draft.cargo_lines]
    assert [(l["pieces"], l["pack_word"], l["dim_l"], l["weight_value"], l["weight_reading"]) for l in lines] == [
        (1, "case", 120, 1020, "auto"), (1, "case", 80, 150, None), (1, "case", 50, 80, None)]
    assert all("PO-77001" not in (l["source_text"] or "") and "LBS" not in (l["source_text"] or "") for l in lines)
    assert [(s["pieces"], s["weight_kg"]) for s in draft.cargo_stated] == [(3, 1250.0)]
    for n, line in enumerate(lines):
        line["id"], line["stated_id"] = n, 1
    t = totals_of(lines, "Air")
    assert (t["gross_kg"], round(t["volume_cbm"], 3)) == (1250.0, 2.604)
    assert round(t["volume_cbm"] * 1e6 / 6000, 1) == 434.0 and t["air_weight_kg"] == 1250.0
    assert draft.values.get("air_weight_kg") == 1250.0         # the case's chargeable, not the lines' added up
    found = checks_of(lines, [dict(draft.cargo_stated[0], id=1)], "Air")
    assert [c.kind for c in found if c.code in ("total", "reading")] == ["ok", "ok"]
    total = [c for c in found if c.code == "total"][0]
    assert "3 cases" in total.text and "1.250 Kgs" in total.text and cargo_checks.asks(found) == 0


def test_the_settings_page_edits_the_limits(app_world):
    client, path, ids = app_world
    page = client.get("/settings").data.decode("utf-8")
    assert 'name="cargo_limit_lcl_max_cbm" value="25"' in page
    client.post("/settings", data={"cargo_limit_lcl_max_cbm": "40", "cargo_limit_density_min": "oops"})
    conn = db.open_db(path)
    limits = cargo_checks.limits(conn)
    conn.close()
    assert limits["lcl_max_cbm"] == 40 and limits["density_min"] == 30


def test_the_cargo_check_report_lists_and_changes_nothing(app_world, tmp_path):
    client, path, ids = app_world
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    add_enquiry(repo, "OPP-2004", ids["party"], received="2026-10-08", service_mode="Air", qty_raw="FCL:3 x 40HQ",
                origin_loc="MIA", origin_country="USA", dest_loc="SAI", dest_country="Chile")
    doubled = add_enquiry(repo, "OPP-2005", ids["party"], received="2026-10-08", service_mode="Air",
                          origin_loc="MIA", origin_country="USA", dest_loc="SAI", dest_country="Chile",
                          qty_raw="4 packages, 2650 lbs (1202 kg) / 1 package 50 x 40 x 30 in, 1200 lbs (544.3 kg) / "
                                  "1 package 50 x 40 x 30 in, 900 lbs (408.2 kg) / 1 package 30 x 20 x 20 in, 330 lbs "
                                  "(149.7 kg) / 1 package 30 x 20 x 20 in, 220 lbs (99.8 kg), volumetric 700 kg")
    repo.update("enquiry", doubled, {"air_weight_kg": 2404.0})
    conn.commit()
    changes = conn.total_changes
    before = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    found, by_check = cargo_report.write(conn, tmp_path / "cargo-check.csv")
    assert conn.total_changes == changes
    assert conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == before
    conn.close()
    rows = list(csv.DictReader((tmp_path / "cargo-check.csv").open(encoding="utf-8-sig")))
    assert list(rows[0].keys()) == list(cargo_report.COLUMNS)
    got = {(r["opp"], r["check"]) for r in rows}
    assert ("OPP-2001", "repeat") in got and ("OPP-2004", "qty_mode") in got and ("OPP-2005", "air_weight") in got
    air = [r for r in rows if r["opp"] == "OPP-2005" and r["check"] == "air_weight"][0]
    assert air["stored_air_weight_kg"] == "2404.0" and round(float(air["recomputed_air_weight_kg"]), 1) == 1202.0
    assert (tmp_path / "cargo-check.csv").read_bytes().startswith(b"\xef\xbb\xbf\"opp\"")


def test_schema_v34_upgrade_adds_the_table_columns_and_limits(tmp_path):
    path = tmp_path / "v33.db"
    conn = db.open_db(path)
    conn.execute("DROP TABLE enquiry_cargo_stated")
    for column in ("source_text", "weight_written", "decided", "cbm_written"):
        conn.execute("ALTER TABLE enquiry_cargo DROP COLUMN {}".format(column))
    conn.execute("ALTER TABLE enquiry DROP COLUMN cargo_decided")
    conn.execute("DELETE FROM setting WHERE key = 'cargo_limits'")
    conn.execute("UPDATE setting SET value = '33' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(enquiry_cargo)")}
    assert {"source_text", "weight_written", "decided", "cbm_written"} <= columns
    assert "cargo_decided" in {r[1] for r in conn.execute("PRAGMA table_info(enquiry)")}
    assert json.loads(db.get_setting(conn, "cargo_limits"))["fcl_max_kg_40"] == 25000
    assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION >= 34
    assert any("enquiry_cargo_stated table created" in n for n in notes)
    # the copy is named after the version the file is upgraded to
    assert any(n.startswith("restore point opplist_before_schema{}_".format(schema_migrations.CURRENT_VERSION))
               for n in notes)
    conn.close()
