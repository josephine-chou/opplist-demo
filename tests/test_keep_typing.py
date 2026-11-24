"""Batch 13c (2026-09-30): a refused cargo line never loses what was typed,
every weight or volume box takes a unit, and nothing but the live app
writes the live log. Every value here is invented; cases from OPP-2201."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import conftest
from conftest import add_enquiry, add_party, seed_reference
from opplist import cargo, db, logs, paths
from opplist.web import create_app

JSON = {"X-Save-First": "1", "Accept": "application/json"}


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "keep.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "next_opp_number", 2210)
    party = add_party(repo, "Brightwater Cargo", country="USA")
    air = add_enquiry(repo, "OPP-2201", party, received="2026-10-12", service_mode="Air",
                      origin_loc="LAX", origin_country="USA", dest_loc="SAI", dest_country="Chile")
    fcl = add_enquiry(repo, "OPP-2202", party, received="2026-10-13", service_mode="FCL",
                      origin_loc="LAX", origin_country="USA", dest_loc="SAI", dest_country="Chile",
                      container_type="40GP", container_qty=1)
    line = repo.add_cargo_line(fcl, {"pieces": 18, "pack_word": "drums", "container_type": "40GP",
                                     "container_qty": 1, "weight_value": 19, "weight_unit": "t"})
    conn.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, {"party": party, "air": air, "fcl": fcl, "line": line}


def lines_of(path, case):
    conn = db.open_db(path)
    rows = [dict(r) for r in conn.execute("SELECT * FROM enquiry_cargo WHERE enquiry_id = ? ORDER BY seq", (case,))]
    conn.close()
    return rows


# ------------------------------------------------ 1. a refusal keeps it --
def test_a_refused_add_answers_json_and_writes_nothing(world):
    client, path, ids = world
    before = lines_of(path, ids["fcl"])
    r = client.post("/enquiries/{}/cargo".format(ids["fcl"]), headers=JSON, data={
        "action": "add", "pieces": "20", "pack_word": "big bags", "per_month": "often", "months": "for a while",
        "container_qty": "1", "container_type": "40GP", "weight_value": "20", "weight_unit": "t"})
    assert r.status_code == 422
    body = r.get_json()
    assert body["ok"] is False
    assert {(e["field"], e["message"]) for e in body["errors"]} == {("per_month", "needs a number"),
                                                                   ("months", "needs a number")}
    assert lines_of(path, ids["fcl"]) == before                    # nothing written, nothing to retype


def test_a_refused_edit_answers_json_and_keeps_the_line(world):
    client, path, ids = world
    r = client.post("/enquiries/{}/cargo".format(ids["fcl"]), headers=JSON, data={
        "action": "save", "line_id": str(ids["line"]), "pieces": "eighteen", "weight_value": "19",
        "weight_unit": "t"})
    assert r.status_code == 422 and r.get_json()["errors"][0]["field"] == "pieces"
    assert lines_of(path, ids["fcl"])[0]["pieces"] == 18


def test_other_small_forms_answer_a_refusal_with_json(world):
    client, path, ids = world
    r = client.post("/enquiries/{}/status".format(ids["air"]), headers=JSON, data={"status": "note", "note": ""})
    assert r.status_code == 422 and r.get_json()["errors"][0]["field"] == "note"
    r = client.post("/enquiries/{}/status".format(ids["air"]), headers=JSON, data={"status": "Lost"})
    assert r.status_code == 422 and r.get_json()["errors"][0]["field"] == "closed_reason"
    r = client.post("/enquiries/{}/vendors".format(ids["air"]), headers=JSON, data={"asked_on": "2026-10-12"})
    assert r.status_code == 422 and r.get_json()["errors"][0]["field"] == "vendor_party_id"
    # without the page's script: the old flash and redirect
    r = client.post("/enquiries/{}/status".format(ids["air"]), data={"status": "note", "note": ""})
    assert r.status_code == 302


# test_the_page_script_keeps_the_clicked_form_and_checks_numbers: replaced in 15a by tests/test_case_v3.py (case page v3).


# test_number_boxes_are_marked_and_carry_their_units: replaced in 15b by tests/test_cargo_lines_only.py (cargo is lines only).


# -------------------------------------------------- 3 and 4. readings --
@pytest.mark.parametrize("text,want", [("2 a month", 2), ("2x", 2), ("2 per month", 2), ("3", 3), ("1,5 a month", 1.5)])
def test_how_often_reads_a_leading_number(text, want):
    values, errors = cargo.clean({"per_month": text})
    assert errors == {} and values["per_month"] == want


@pytest.mark.parametrize("text,want", [("24 months", 24), ("2 years", 24), ("1.5 years", 18), ("6", 6)])
def test_for_how_long_reads_months_and_years(text, want):
    values, errors = cargo.clean({"months": text})
    assert errors == {} and values["months"] == want


def test_pieces_words_go_to_packing_when_it_is_empty():
    values, _ = cargo.clean({"pieces": "25 big bags", "pack_word": ""})
    assert (values["pieces"], values["pack_word"]) == (25, "big bags")
    values, _ = cargo.clean({"pieces": "25 big bags", "pack_word": "sacks"})
    assert (values["pieces"], values["pack_word"]) == (25, "sacks")


@pytest.mark.parametrize("text,unit,value", [("25 t", "t", 25), ("25,000 kg", "kg", 25000), ("7605 lbs", "lbs", 7605)])
def test_a_unit_typed_with_the_weight_sets_the_unit(text, unit, value):
    values, errors = cargo.clean({"weight_value": text, "weight_unit": "kg"})
    assert errors == {} and (values["weight_value"], values["weight_unit"]) == (value, unit)


def test_a_whole_size_typed_in_the_first_box_fills_all_three():
    values, errors = cargo.clean({"dim_l": "52 x 42 x 47 in", "dim_w": "", "dim_h": "", "dims_unit": "cm"})
    assert errors == {} and (values["dim_l"], values["dim_w"], values["dim_h"], values["dims_unit"]) == (52, 42, 47, "in")


def test_an_add_with_words_saves(world):
    client, path, ids = world
    r = client.post("/enquiries/{}/cargo".format(ids["fcl"]), headers=JSON, data={
        "action": "add", "pieces": "20 big bags", "pack_word": "", "per_month": "2 a month", "months": "2 years",
        "container_qty": "1 x 40GP", "container_type": "40GP", "weight_value": "20 t", "weight_unit": "kg"})
    assert r.get_json() == {"ok": True}
    line = lines_of(path, ids["fcl"])[-1]
    assert (line["pieces"], line["pack_word"], line["per_month"], line["months"], line["weight_kg"]) == \
        (20, "big bags", 2, 24, 20000)


# ------------------------------------------ 6. units on the register page --
def register(client, party, **extra):
    data = {"received_on": "2026-10-14", "requester_party_id": str(party), "origin_country": "USA",
            "origin_loc": "LAX", "dest_country": "Chile", "dest_loc": "SAI", "decision": "new"}
    data.update(extra)
    return client.post("/enquiries/new", data=data, follow_redirects=True)


def test_the_register_page_has_unit_boxes_and_no_air_weight_box(world):
    client, path, ids = world
    body = client.get("/enquiries/new").data.decode("utf-8")
    # 15c: no typed boxes at all - the units sit beside the numbers of each cargo line
    assert 'name="gross_unit"' not in body and 'name="volume_unit"' not in body
    assert 'name="cargo_0_weight_unit" data-f="weight_unit"' in body and "<option>lbs</option>" in body
    assert 'name="cargo_0_cbm_unit" data-f="cbm_unit"' in body and "<option>cft</option>" in body
    assert 'name="air_weight_kg"' not in body and 'name="lcl_cbm"' not in body
    assert 'id="reg-lines"' in body and "Deliver to" not in body


# test_an_air_case_registers_with_a_weight_in_lbs: replaced in 15b by tests/test_cargo_lines_only.py (cargo is lines only).


# test_the_register_rules_are_met_by_the_boxes_in_any_unit: replaced in 15b by tests/test_cargo_lines_only.py (cargo is lines only).


# test_the_case_page_typed_boxes_take_a_unit: replaced in 15b by tests/test_cargo_lines_only.py (cargo is lines only).


# test_nothing_typed_shows_dashes_not_zero: replaced in 15b by tests/test_cargo_lines_only.py (cargo is lines only).


def test_the_register_page_reads_each_size_group_as_its_own_line(world, monkeypatch):
    from opplist import intake, outlook_bridge
    client, path, ids = world
    body = ("Please quote air freight.\n3 pcs 110x90x140 cm - 820.4 kg\n2 pcs 110x90x150 cm - 310.2 kg\n"
            "1 pc 110x90x120 cm - 95.5 kg\nTotal: 6 pallets / 1226.1 kg\nThanks")
    mail = outlook_bridge.MailInfo(subject="RFQ 90417 air", sender_name="Lee Harbor", sender_address="lee@brightwater.test",
                                   sender_internal=False, received_on="2026-10-15", received_at="2026-10-15 09:00",
                                   to_addresses=[], cc_addresses=[], to_names=[], cc_names=[], display_to="",
                                   conversation_id="CONV-2211", entry_id="ENTRY-2211", body_excerpt=body, message_id="")
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: mail)
    page = client.post("/enquiries/from-outlook").data.decode("utf-8")
    assert len(re.findall(r'class="reg-line" data-idx="\d+"', page)) == 3 and 'name="cargo_2_dim_h" data-f="dim_h" data-num value="120"' in page
    assert "add up to the same" in page


# --------------------------------------------------------- 5. the log --
def test_an_app_over_a_copy_logs_next_to_the_copy(tmp_path):
    copy = tmp_path / "copy" / "opplist.db"
    copy.parent.mkdir()
    db.open_db(copy).close()
    create_app(copy)
    assert logs.current_path() == (copy.parent / "opplist.log").resolve()
    assert Path(paths.LIVE_DATA_DIR).resolve() not in logs.current_path().resolve().parents


def test_the_log_never_opens_on_the_live_file_under_pytest(monkeypatch):
    monkeypatch.setattr(logs, "_configured", False)
    import logging
    logger = logging.getLogger(logs.LOGGER_NAME)
    saved = list(logger.handlers)
    try:
        for h in list(logger.handlers):
            logger.removeHandler(h)
        monkeypatch.setattr(paths, "LOG_PATH", paths.LIVE_LOG_PATH)
        logs.setup()
        assert logs.current_path() is not None and logs.current_path().resolve() != Path(paths.LIVE_LOG_PATH).resolve()
    finally:
        for h in list(logger.handlers):
            logger.removeHandler(h)
            h.close()
        for h in saved:
            logger.addHandler(h)


def test_the_run_guard_sees_a_new_file_in_the_live_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "LIVE_DATA_DIR", tmp_path / "live")
    (tmp_path / "live" / "mail-archive").mkdir(parents=True)
    (tmp_path / "live" / "opplist.log").write_text("old\n", encoding="utf-8")
    before = conftest._live_files()
    (tmp_path / "live" / "opplist.db-wal").write_text("", encoding="utf-8")        # the live app's: ignored
    (tmp_path / "live" / "mail-archive" / "mail.db").write_text("", encoding="utf-8")
    (tmp_path / "live" / "restore-points").mkdir()
    (tmp_path / "live" / "restore-points" / "opplist_before_apply-status_x.db").write_text("", encoding="utf-8")
    assert conftest._live_files() - before == {"restore-points/opplist_before_apply-status_x.db"}
