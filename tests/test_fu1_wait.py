"""Batch L2 (2026-10-21): after FU1, two paths, and the Decide card closes
with the No response reason for the requester.

No answer to FU1: no FU2, the case goes to Decide fu1_wait_days (10)
working days after FU1. An answer after FU1 (a note the scan wrote, or
her own note of a call): FU2 due fu2_days after it, as before. Two
follow-ups sent: Decide fu_decide_days after FU2, as before. The batch
button and a Decide or R11 row's Lost... pick 'No response from agent' /
'... other office / sales' / '... customer' by the requester's type.

Every company, person, place, number and date here is invented; cases
from OPP-6901.
"""

from __future__ import annotations

import re

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, status as status_mod, tracking
from opplist import web as web_module
from opplist.web import create_app

QUOTED = "2026-11-02"                      # a Monday
FU1 = "2026-11-03"


def wd(start, n):
    return tracking.add_business_days(start, n)


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "l2.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    parties = {
        "agent": add_party(repo, "Quillon Forwarding", country="Chile"),
        "office": add_party(repo, "JoJo TPA", ptype="Office", country="USA"),
        "other": add_party(repo, "Brackwater Holdings", ptype="Unknown", country="USA"),
    }
    db.set_setting(conn, "closed_reasons_lost", ["Price too high", "No response from customer", "Other"])
    repo.commit()
    return {"conn": conn, "repo": repo, "path": path, "parties": parties}


def quoted_case(w, opp, who="agent", fus=(FU1,)):
    repo = w["repo"]
    case = add_enquiry(repo, opp, w["parties"][who], received="2026-10-29", service_mode="Air",
                       origin_country="USA", origin_loc="ORD", dest_country="Chile", dest_loc="SCL",
                       commodity="Spare pump seals")
    repo.set_status(case, "Quoted", QUOTED)
    for at in fus:
        repo.add_followup(case, at)
    repo.commit()
    return case


def step_on(w, case, day, settings=None):
    found = [i for i in tracking.rule_quoted_no_answer(w["conn"], day, settings or tracking.fu_settings(w["conn"]))
             if i.enquiry_id == case]
    return found[0].extra["step"] if found else ""


def ladder_of(w, case, day):
    row = w["conn"].execute("SELECT * FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    return tracking.ladder(w["conn"], row, today=day)


# ------------------------------------------------------------ the rule --
def test_fu1_with_no_answer_waits_then_decide_on_day_ten_never_fu2(world):
    case = quoted_case(world, "OPP-6901")
    assert tracking.fu_settings(world["conn"])["fu1_wait_days"] == 10
    for n in range(0, 10):
        assert step_on(world, case, wd(FU1, n)) == "", n
    assert step_on(world, case, wd(FU1, 10)) == "Decide"
    for n in range(0, 25):
        assert step_on(world, case, wd(FU1, n)) != "FU2", n


def test_fu1_then_a_scan_note_is_fu2_five_working_days_after_the_note(world):
    case = quoted_case(world, "OPP-6902")
    note_day = "2026-11-06"
    world["repo"].add_note(case, note_day, "Reply from Maren Quill (noted): thanks, checking with the consignee "
                                           "[mid:<l2-6902@x>]", source="mail")
    world["repo"].commit()
    assert step_on(world, case, wd(note_day, 4)) == ""
    assert step_on(world, case, wd(note_day, 5)) == "FU2"


def test_fu1_then_her_own_note_is_the_same(world):
    case = quoted_case(world, "OPP-6903")
    world["repo"].add_note(case, FU1, "Called Maren, she is waiting on the factory date")      # same day as FU1
    world["repo"].commit()
    assert step_on(world, case, wd(FU1, 4)) == ""
    assert step_on(world, case, wd(FU1, 5)) == "FU2"


def test_a_note_before_fu1_is_no_answer(world):
    repo = world["repo"]
    case = quoted_case(world, "OPP-6904", fus=())
    repo.add_note(case, QUOTED, "Sent the rate sheet as well")
    repo.add_followup(case, FU1)
    repo.commit()
    assert step_on(world, case, wd(FU1, 5)) == ""
    assert step_on(world, case, wd(FU1, 10)) == "Decide"


def test_fu1_and_fu2_sent_decide_after_fu_decide_days_as_now(world):
    fu2 = "2026-11-10"
    case = quoted_case(world, "OPP-6905", fus=(FU1, fu2))
    assert step_on(world, case, wd(fu2, 4)) == ""
    assert step_on(world, case, wd(fu2, 5)) == "Decide"


def test_the_setting_set_to_seven(world):
    db.set_setting(world["conn"], "fu1_wait_days", 7)
    world["conn"].commit()
    case = quoted_case(world, "OPP-6906")
    assert step_on(world, case, wd(FU1, 6)) == ""
    assert step_on(world, case, wd(FU1, 7)) == "Decide"
    rung = ladder_of(world, case, wd(FU1, 2))
    assert rung["steps"][-1] == ("Decide", wd(FU1, 7), "now")


def test_fu_step_alone():
    s = tracking.FU_DEFAULTS
    assert tracking.fu_step(1, 9, s) == "" and tracking.fu_step(1, 10, s) == "Decide"
    assert tracking.fu_step(1, 30, s) == "Decide"
    assert tracking.fu_step(1, 4, s, answered=True) == "" and tracking.fu_step(1, 5, s, answered=True) == "FU2"
    assert tracking.fu1_answered("2026-11-03", "2026-11-03") and not tracking.fu1_answered("2026-11-02", "2026-11-03")
    assert not tracking.fu1_answered(None, "2026-11-03")


# ---------------------------------------------------------- the ladder --
def test_the_ladder_with_no_answer_is_quoted_fu1_decide(world):
    case = quoted_case(world, "OPP-6907")
    rung = ladder_of(world, case, wd(FU1, 3))
    assert rung["steps"] == [("Quoted", QUOTED, "done"), ("FU1", FU1, "done"),
                             ("Decide", wd(FU1, 10), "now")]
    assert (rung["next"], rung["next_due"], rung["days_left"]) == ("Decide", wd(FU1, 10), 7)
    # before FU1 the ladder has no FU2 either
    fresh = quoted_case(world, "OPP-6908", fus=())
    rung = ladder_of(world, fresh, QUOTED)
    assert [s[0] for s in rung["steps"]] == ["Quoted", "FU1 due", "Decide"]
    assert rung["steps"][-1][1] == wd(wd(QUOTED, 1), 10)


def test_the_ladder_shows_fu2_once_they_answer(world):
    case = quoted_case(world, "OPP-6909")
    world["repo"].add_note(case, "2026-11-05", "WhatsApp from Maren: client still deciding")
    world["repo"].commit()
    rung = ladder_of(world, case, "2026-11-06")
    assert [s[0] for s in rung["steps"]] == ["Quoted", "FU1", "FU2 due", "Decide"]
    assert rung["steps"][2][1] == wd("2026-11-05", 5)
    assert rung["steps"][3][1] == wd(wd("2026-11-05", 5), 5)


def test_the_case_page_draws_both_ladders(world, monkeypatch):
    waits = quoted_case(world, "OPP-6910")
    answered = quoted_case(world, "OPP-6911")
    world["repo"].add_note(answered, "2026-11-05", "Called Maren: will confirm next week")
    world["repo"].commit()
    monkeypatch.setattr(web_module, "_today", lambda: "2026-11-06")
    client = create_app(world["path"]).test_client()

    def rungs(case):
        page = client.get("/enquiries/{}".format(case)).data.decode("utf-8")
        ladder = page[page.index('<div class="ladder">'):]
        ladder = ladder[:ladder.index("</div>")]
        return [" ".join(s.split()) for s in re.findall(r'<span class="l-step[^"]*">([^<]*)</span>', ladder)]

    assert [r.split(" ")[0] for r in rungs(waits)] == ["Quoted", "FU1", "Decide"]
    assert [r.split(" ")[0] for r in rungs(answered)] == ["Quoted", "FU1", "FU2", "Decide"]


# ------------------------------------------------- the Decide card --
def test_the_no_response_reason_by_requester_type():
    assert status_mod.no_response_reason("Agent") == "No response from agent"
    assert status_mod.no_response_reason("Office") == "No response from other office / sales"
    for other in ("Unknown", "Carrier", "Direct Customer", None):
        assert status_mod.no_response_reason(other) == "No response from customer"


def show_decide(path):
    """FX4: the Decide card is hidden by default (Settings brings it back)."""
    conn = db.open_db(path)
    db.set_setting(conn, tracking.HIDDEN_SETTING, "r9,r2,r6")
    conn.commit()
    conn.close()


def decide_cases(w):
    """Three cases on Decide today: an agent's, an office's, anyone else's."""
    ids = {"agent": quoted_case(w, "OPP-6912", "agent"), "office": quoted_case(w, "OPP-6913", "office"),
           "other": quoted_case(w, "OPP-6914", "other"), "agent2": quoted_case(w, "OPP-6915", "agent")}
    return ids, wd(FU1, 12)


def test_the_batch_closes_each_case_with_its_requesters_reason(world, monkeypatch):
    ids, today = decide_cases(world)
    monkeypatch.setattr(web_module, "_today", lambda: today)
    client = create_app(world["path"]).test_client()
    page = client.get("/tracking").data.decode("utf-8")
    # FX4: hidden by default, named in the line under Today's work
    assert 'id="decide-R3"' not in page and "Decide 4" in page
    show_decide(world["path"])
    page = client.get("/tracking").data.decode("utf-8")
    assert "Mark selected Lost: No response<" in page
    for key, reason in (("agent", "No response from agent"), ("office", "No response from other office / sales"),
                        ("other", "No response from customer")):
        assert re.search(r'name="ids" value="{}" form="decide-R3" class="decide-pick" data-reason="{}"'.format(
            ids[key], re.escape(reason)), page), key
    done = client.post("/tracking/decide-lost", data={"ids": [str(i) for i in ids.values()], "at": today},
                       follow_redirects=True).data.decode("utf-8")
    assert "Marked Lost (No response from agent): OPP-6912, OPP-6915" in done
    assert "Marked Lost (No response from other office / sales): OPP-6913" in done
    assert "Marked Lost (No response from customer): OPP-6914" in done
    conn = db.open_db(world["path"])
    got = {r["opp_no"]: (r["status"], r["closed_reason"]) for r in conn.execute(
        "SELECT opp_no, status, closed_reason FROM v_enquiry WHERE opp_no BETWEEN 'OPP-6912' AND 'OPP-6915'")}
    assert got == {"OPP-6912": ("Lost", "No response from agent"),
                   "OPP-6913": ("Lost", "No response from other office / sales"),
                   "OPP-6914": ("Lost", "No response from customer"),
                   "OPP-6915": ("Lost", "No response from agent")}
    # the reasons that were missing are on the Lost list now, after the old ones
    assert status_mod.closed_reasons(conn, "Lost")[:3] == ["Price too high", "No response from customer", "Other"]
    assert set(status_mod.closed_reasons(conn, "Lost")[3:]) == {"No response from agent",
                                                                "No response from other office / sales"}


def test_the_confirm_counts_per_reason(world):
    from opplist.web import text
    assert text.TEXT["decide_confirm"].format(n=3, reasons="2 x No response from agent\n1 x No response from customer") == \
        "Mark 3 case(s) Lost?\n2 x No response from agent\n1 x No response from customer"
    script = (web_module.__file__.rsplit("__init__.py", 1)[0] + "static/app.js")
    with open(script, encoding="utf-8") as fh:
        js = fh.read()
    assert 'getAttribute("data-reason")' in js and '.replace("{reasons}", lines)' in js


def test_a_decide_row_and_an_r11_row_pick_the_reason_by_default(world, monkeypatch):
    ids, today = decide_cases(world)
    # R11 reads the mail archive; here its rows are given
    silent_case = quoted_case(world, "OPP-6916", "office", fus=())

    def fake_silent(conn, day):
        row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (silent_case,)).fetchone()
        return [tracking.Item("R11", "enquiry", silent_case, enquiry_id=silent_case, label="OPP-6916",
                              days=31, since=QUOTED, extra={"country": "USA", "city": "", "quoted": QUOTED,
                                                            "fu_count": 0, "last_fu": ""})]

    monkeypatch.setattr(tracking, "rule_silent", fake_silent)
    monkeypatch.setattr(web_module, "_today", lambda: today)
    show_decide(world["path"])                                    # FX4: hidden by default
    client = create_app(world["path"]).test_client()
    page = client.get("/tracking").data.decode("utf-8")

    def picked(card_key, ref):
        box = page[page.index('id="reason-{}-{}-lost"'.format(card_key, ref)):]
        box = box[:box.index("</select>")]
        return re.findall(r'<option value="([^"]*)" selected>', box)

    assert picked("decide", ids["agent"]) == ["No response from agent"]
    assert picked("decide", ids["office"]) == ["No response from other office / sales"]
    assert picked("decide", ids["other"]) == ["No response from customer"]
    assert picked("silent", silent_case) == ["No response from other office / sales"]
    # choosing a reason that is not on the list yet adds it and closes the case
    client.post("/tracking/act", data={"do": "quick:Lost", "rule": "R11", "kind": "enquiry", "ref_id": silent_case,
                                       "enquiry_id": silent_case, "at": today,
                                       "closed_reason": "No response from other office / sales"})
    conn = db.open_db(world["path"])
    assert tuple(conn.execute("SELECT status, closed_reason FROM v_enquiry WHERE id = ?", (silent_case,)).fetchone()) == \
        ("Lost", "No response from other office / sales")
