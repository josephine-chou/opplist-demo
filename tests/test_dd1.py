"""Batch DD1 (2026-11-12): "Already in OppList?" on the register page - the
same subject is the same case only while the old one is open and recent,
one block instead of three - Non-stackable read from the mail, weights of
five to seven figures, the person on the case page, and the warn colours.

Every company, person, mail, place, number and date here is invented;
cases from OPP-9941, domains .test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, dedupe, firebase_sync as fs, intake, outlook_bridge, parsing
from opplist import web as web_module
from opplist.web import create_app
from test_firebase_sync import FakeFirestore
from test_register_v3 import fields_of, with_values

DAY = "2026-12-10"                                  # the mail's day
SUBJECT = "AIR RATE // MIA - SAI // CORBEL PUMPS"
BODY = "Hi,\n\nPlease quote air MIA - SAI, 2 crates, 300 kg, 100 x 80 x 60 cm.\n\nThanks\nNell"
KEY = parsing.subject_thread_key(SUBJECT)


def a_mail(subject=SUBJECT, body=BODY, key="9941", conversation=None, on=DAY):
    return outlook_bridge.MailInfo(subject=subject, received_on=on, received_at=on + " 10:15",
                                   sender_address="nell@tarnwickfreight.test", sender_name="Nell Tarn",
                                   to_addresses=["desk@jojofreight.example"], body_excerpt=body,
                                   entry_id="ENTRY-" + key, conversation_id=conversation or "CONV-" + key)


@pytest.fixture
def web(tmp_path, monkeypatch):
    path = tmp_path / "dd1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "owners", ["Ana", "Ben"])
    tarn = add_party(repo, "Tarnwick Freight", country="Chile")
    repo.insert("party_domain", {"domain": "tarnwickfreight.test", "party_id": tarn})
    other = add_party(repo, "JoJo QMX", ptype="Office", country="Unknown")
    db.set_setting(conn, "next_opp_number", 9960)
    repo.commit()
    holder = {"mail": None}
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: holder["mail"])
    monkeypatch.setattr(web_module, "_today", lambda: DAY)
    app = create_app(path)
    app.config["TESTING"] = True
    yield {"client": app.test_client(), "path": path, "conn": conn, "repo": repo, "holder": holder,
           "tarn": tarn, "other": other}
    conn.close()


def old_case(w, opp, received, status="New", party=None, **extra):
    extra.setdefault("thread_key", KEY)
    extra.setdefault("source_subject", SUBJECT)
    case = add_enquiry(w["repo"], opp, party or w["other"], received=received, scope="In", **extra)
    if status != "New":
        w["repo"].set_status(case, status, received)
    w["repo"].commit()
    return case


def page(w, mail=None):
    w["holder"]["mail"] = mail or a_mail()
    got = w["client"].post("/enquiries/from-outlook")
    assert got.status_code == 200
    return got.data.decode("utf-8")


def register(w, fields):
    return w["client"].post("/enquiries/new", data=fields)


def count(w):
    return w["conn"].execute("SELECT COUNT(*) FROM enquiry").fetchone()[0]


def block(body):
    start = body.index('id="already"')
    return body[start:body.index("</section>", start)]


# ------------------------------------------- 1. when an older case is the same case --
def test_the_same_subject_as_a_closed_case_is_seen_before_and_register_saves_at_the_first_press(web):
    w = web
    old_case(w, "OPP-9941", "2026-11-17", status="Lost")
    body = page(w)
    assert 'id="already"' not in body
    line = body[body.index('class="seen-line"'):body.index("</p>", body.index('class="seen-line"'))]
    assert "Same subject as" in line and "OPP-9941" in line and "Lost" in line and "JoJo QMX" in line
    assert "23 days ago" in line and "a new request at a new time is a new case" in line
    assert "Open OPP-9941" in line and "from this mail" not in line           # closed: Open only
    before = count(w)
    got = register(w, fields_of(body))
    assert got.status_code == 302 and count(w) == before + 1


def test_the_same_subject_as_an_open_case_three_days_earlier_gives_the_block(web):
    w = web
    thread = old_case(w, "OPP-9942", "2026-12-07")
    # and a probable duplicate on its own subject: same requester, lane and mode, two days apart
    dup = old_case(w, "OPP-9943", "2026-12-08", party=w["tarn"], thread_key="OTHERKEY", source_subject="OTHER",
                   origin_loc="MIA", origin_country="USA", dest_loc="SAI", dest_country="Chile", service_mode="Air")
    body = page(w)
    b = block(body)
    assert "Already in OppList?" in b and "OPP-9942" in b and "Update OPP-9942 from this mail" in b
    assert "OPP-9943" not in b                                         # at load: the thread's case only
    before = count(w)
    got = register(w, fields_of(body))
    assert got.status_code == 200 and count(w) == before                # Not saved: pick one
    b = block(got.data.decode("utf-8"))
    assert "Update OPP-9942 from this mail" in b and "It is OPP-9943" in b
    assert b.count('data-case="{}"'.format(thread)) == 1 and 'data-case="{}"'.format(dup) in b
    # the open thread's case: Register a new case is not the primary button
    assert re.search(r'class="button" type="submit" form="enquiry-form"\s+name="decision" value="new">Register a new case',
                     b)


def test_twenty_days_earlier_is_seen_before_and_with_the_setting_at_30_it_is_the_same_case(web):
    w = web
    old_case(w, "OPP-9944", "2026-11-20")                               # open, 20 days before
    body = page(w)
    assert 'id="already"' not in body and "Same subject as" in body and "20 days ago" in body
    assert "Update OPP-9944 from this mail" in body                     # open: a plain Update on its line
    w["client"].post("/settings", data={"dedupe_same_subject_days": "30"})
    assert dedupe.same_subject_days(w["conn"]) == 30
    body = page(w)
    assert "Update OPP-9944 from this mail" in block(body) and 'class="seen-line"' not in body


def test_the_same_conversation_as_a_closed_case_is_seen_before(web):
    w = web
    old_case(w, "OPP-9945", "2026-12-08", status="Cancelled", thread_key="ANOTHER", source_subject="ANOTHER",
             conversation_id="CONV-SHARED")
    body = page(w, a_mail(conversation="CONV-SHARED"))
    assert 'id="already"' not in body
    assert "Same Outlook conversation as" in body and "OPP-9945" in body and "Cancelled" in body


def test_a_case_number_of_a_closed_case_gives_the_block_with_register_a_new_case_first(web):
    w = web
    case = old_case(w, "OPP-9946", "2026-10-01", status="Lost")
    body = page(w, a_mail(subject="RE: [OPP-9946] " + SUBJECT))
    b = block(body)
    assert "OPP-9946" in b and "case number in the subject" in b
    assert "from this mail" not in b and ">Open<" in b                    # closed: Open only
    assert re.search(r'class="button primary" type="button" data-hide="already"', b)
    assert "onclick=\"document.getElementById('declined_case').value='{}'\"".format(case) in b


def test_the_same_mail_gives_it_is_as_the_primary_button(web):
    w = web
    old_case(w, "OPP-9947", "2026-12-10", party=w["tarn"], thread_key="X9947", source_subject="X",
             entry_id="ENTRY-9941")
    body = page(w)
    got = register(w, fields_of(body))
    b = block(got.data.decode("utf-8"))
    assert re.search(r'class="button primary" type="submit" form="enquiry-form"\s+name="decision" value="duplicate"', b)
    assert "It is OPP-9947" in b and "Registered from this same mail" in b
    assert not re.search(r'class="button primary" type="submit" form="enquiry-form"\s+name="decision" value="new"', b)


def test_not_saved_yet_appears_once_and_the_old_texts_are_gone(web):
    w = web
    old_case(w, "OPP-9948", "2026-12-09")
    got = register(w, fields_of(page(w))).data.decode("utf-8")
    assert got.count("Not saved yet") == 1 and "Not saved yet: pick one." in got
    for gone in ("This thread is", "Register as new anyway", "three buttons", "This may already be registered",
                 "warn-summary", 'class="card dedupe"'):
        assert gone not in got


def test_no_at_load_is_not_asked_again_at_register(web):
    w = web
    thread = old_case(w, "OPP-9949", "2026-12-09")
    fields = fields_of(page(w))
    before = count(w)
    got = register(w, with_values(fields, declined_case=str(thread)))      # what the button sets
    assert got.status_code == 302 and count(w) == before + 1


def test_the_buttons_that_submit_the_registration_sit_outside_it_with_form(web):
    w = web
    old_case(w, "OPP-9950", "2026-12-09")
    got = register(w, fields_of(page(w))).data.decode("utf-8")
    assert got.index('id="already"') < got.index('id="enquiry-form"')
    js = (Path(web_module.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "button[form=\"' + draftForm.id + '\"]" in js


def test_score_pair_gives_a_same_subject_outside_the_rule_no_points_and_the_cap_applies():
    new = {"thread_key": "K1", "received_on": DAY, "requester_party_id": 1, "service_mode": "Air",
           "origin_loc": "MIA", "dest_loc": "SAI", "origin_country": "USA", "dest_country": "Chile",
           "commodity": "brass valves"}
    base = dict(id=7, opp_no="OPP-9951", thread_key="K1", entry_id=None, conversation_id=None, end_customer=None,
                requester_party_id=2, service_mode="Air", origin_loc="MIA", dest_loc="SAI", origin_country="USA",
                dest_country="Chile", commodity="brass valves", legacy=0, source_subject="", qty_raw="")
    lost = dedupe.score_pair(new, dict(base, status="Lost", received_on="2026-12-08"), {})
    assert "same subject as a Lost case" in lost.reasons and "same subject" not in lost.reasons
    assert lost.score < dedupe.POSSIBLE and lost.seen_before
    old = dedupe.score_pair(new, dict(base, status="Quoted", received_on="2026-11-17"), {})
    assert "same subject, 23 days earlier" in old.reasons and old.seen_before
    # rare words alone, two requesters: capped below possible (13g), the old subject no link
    rare = dedupe.score_pair(new, dict(base, status="Lost", received_on="2026-12-08"), {"brass": 1, "valves": 1})
    assert rare.score == dedupe.POSSIBLE - 1
    assert "different requesters, nothing shared but place and commodity words" in rare.reasons
    fresh = dedupe.score_pair(new, dict(base, status="New", received_on="2026-12-07"), {})
    assert fresh.score >= 100 and "same subject" in fresh.reasons and not fresh.seen_before


# ------------------------------------------------------------------ 3. colours --
def test_app_css_has_no_old_warn_colour_and_every_text_of_the_family_is_warn_ink():
    css = (Path(web_module.__file__).parent / "static" / "app.css").read_text(encoding="utf-8").lower()
    for old in ("#8a5a00", "#6b4a00", "#fdf3dc", "#ecd39a", "#fbeccc", "#d4a017", "#fff7e0"):
        assert old not in css, old
    assert "--warn: #f78600" in css and "--warn-ink: #a35400" in css and "--warn-bg: #fff4e6" in css
    assert "--warn-line: #fcd2a3" in css and "--warn-chip: #ffe8cc" in css
    assert not re.search(r"(?<![-\w])color:\s*var\(--warn\)", css)
    assert len(re.findall(r"(?<![-\w])color:\s*var\(--warn-ink\)", css)) >= 20
    assert "--tone-amber" in css or "#a86b00" in css                      # Tracking's amber stays


# ---------------------------------------------------------- 4. Non-stackable --
STACK_PHRASES = ["non-stackable", "non stackable", "nonstackable", "non- stackable", "not stackable", "no stack",
                 "no stacking", "unstackable", "do not stack", "cannot be stacked", "can't be stacked",
                 "can not be stacked", "Stackable: No", "stackable - no", "stackable? no", "Stackable | No",
                 "carga no apilable", "no apilar", "no estibable", "no remontable", "não empilhável", "não empilhar",
                 "NON-STACKABLE"]


@pytest.mark.parametrize("phrase", STACK_PHRASES)
def test_each_phrase_says_non_stackable(phrase):
    assert parsing.non_stackable_line(["2 crates, {}".format(phrase)])


@pytest.mark.parametrize("lines", [["stackable"], ["Stackable: Yes"], ["Hazardous: No", "Stackable: Yes"],
                                   ["Hazardous: No", "Stackable"]])
def test_these_do_not(lines):
    assert parsing.non_stackable_line(lines) == ""


def test_a_hyphen_at_the_end_of_a_line_joins_the_next():
    assert parsing.non_stackable_line(["The crates are non-", "stackable."]) == "The crates are non-stackable."


def tag_of(w, mail):
    draft = intake.build(w["conn"], mail)
    return "Non-stackable" in draft.special_tags, draft.tag_lines.get("Non-stackable", "")


def test_ticked_from_the_subject_a_body_line_and_a_forwards_quoted_request(web):
    w = web
    db.set_setting(w["conn"], "special_handling", ["DG", "Non-stackable"])
    w["conn"].commit()
    assert tag_of(w, a_mail(subject=SUBJECT + " NON STACKABLE")) == (True, 'from subject: “{} NON STACKABLE”'.format(SUBJECT))
    ticked, line = tag_of(w, a_mail(body=BODY.replace("Thanks", "Cargo cannot be stacked.\nThanks")))
    assert ticked and line == "from body: “Cargo cannot be stacked.”"
    forward = ("FYI please quote.\n\nFrom: Nell Tarn <nell@tarnwickfreight.test>\nSent: Monday, December 7, 2026 9:05 AM\n"
               "To: JJC-DESK <desk@jojofreight.example>\nSubject: " + SUBJECT + "\n\nHi,\n2 crates, 300 kg\nStackable: No\nNell\n")
    fwd = outlook_bridge.MailInfo(subject="FW: " + SUBJECT, received_on=DAY, received_at=DAY + " 11:00",
                                  sender_address="kip@jojofreight.example", sender_name="Kip Lowe", sender_internal=True,
                                  to_addresses=["desk@jojofreight.example"], body_excerpt=forward, entry_id="ENTRY-F",
                                  conversation_id="CONV-F")
    assert intake.forward_kind(fwd)
    ticked, line = tag_of(w, fwd)
    assert ticked and "Stackable: No" in line
    assert tag_of(w, a_mail(body=BODY.replace("Thanks", "Hazardous: No\nStackable: Yes\nThanks"))) == (False, "")
    # not on her list: not ticked
    db.set_setting(w["conn"], "special_handling", ["DG"])
    w["conn"].commit()
    assert tag_of(w, a_mail(subject=SUBJECT + " NON STACKABLE"))[0] is False


def test_the_register_page_shows_the_line_and_the_tick_can_be_taken_off(web):
    w = web
    db.set_setting(w["conn"], "special_handling", ["DG", "Non-stackable"])
    w["conn"].commit()
    body = page(w, a_mail(body=BODY.replace("Thanks", "Please note: do not stack.\nThanks")))
    box = body[body.index('id="special-handling"'):]
    box = box[:box.index("</fieldset>")]
    assert re.search(r'value="Non-stackable" aria-label="Non-stackable" checked', box)
    assert "Non-stackable from body: “Please note: do not stack.”" in box.replace("&#34;", '"')
    fields = fields_of(body)
    got = register(w, with_values(fields, special_handling=[]))           # she unticks it
    assert got.status_code == 302
    assert w["conn"].execute("SELECT special_handling FROM enquiry ORDER BY id DESC LIMIT 1").fetchone()[0] in (None, "")


# ------------------------------------------------------------- 5. the weights --
@pytest.mark.parametrize("text, value", [("58pkgs/27318kgs/61.42cbm", "27318"), ("27318 kgs", "27318"),
                                         ("43500 lbs", "43500"), ("1234567 kg", "1234567"),
                                         ("26,796kgs", "26,796")])
def test_five_to_seven_figures_with_a_unit_read_as_a_weight(text, value):
    assert [m.group("v") for m in parsing._WEIGHTS.finditer(text)] == [value]


def test_without_a_unit_five_figures_are_no_weight_and_dimensions_are_as_before():
    assert list(parsing._WEIGHTS.finditer("ref 27318 for 2 crates")) == []
    assert list(parsing._WEIGHTS.finditer("12345678 kg")) == []                # eight figures: not read
    assert parsing.parse_body_qty("27318 kgs").qty_raw == "Air:27318"
    assert parsing.parse_body_qty("27318").qty_raw in ("", None)
    dims = parsing._DIMS.search("120 x 80 x 60 cm")
    assert (dims.group("l"), dims.group("w"), dims.group("h")) == ("120", "80", "60")


# ------------------------------------------------------ 6. the person on the case page --
def test_the_person_is_changed_on_the_case_page_and_pushed(web, tmp_path):
    w = web
    case = old_case(w, "OPP-9952", "2026-12-09", party=w["tarn"])
    client = w["client"]
    body = client.get("/enquiries/{}".format(case)).data.decode("utf-8")
    select = body[body.index('name="owner" id="fld-owner"'):]
    select = select[:select.index("</select>")]
    assert re.findall(r'<option value="([^"]*)"', select) == ["Ana", "Ben", ""]
    assert re.search(r'<option value="" selected>Not assigned</option>', select)
    client.post("/enquiries/{}".format(case), data={"owner": "Ben"})
    assert w["conn"].execute("SELECT owner FROM enquiry WHERE id = ?", (case,)).fetchone()[0] == "Ben"
    logged = w["conn"].execute("SELECT old_value, new_value FROM change_log WHERE table_name = 'enquiry' "
                               "AND row_id = ? AND field = 'owner'", (case,)).fetchall()
    assert [tuple(r) for r in logged] == [(None, "Ben")]
    fake = FakeFirestore()
    assert fs.sync_once(w["conn"], fake, {}, today=DAY, reports_dir=tmp_path / "r", record=tmp_path / "x.json").ok
    assert {r["opp"]: r["owner"] for r in fake.docs["lists/inquiries"]["rows"]}["OPP-9952"] == "Ben"
    # a name not in owners is refused, nothing written
    got = client.post("/enquiries/{}".format(case), data={"owner": "Zed"}, follow_redirects=True).data.decode("utf-8")
    assert "is not one of the owners in Settings" in got.replace("&#39;", "'")
    assert w["conn"].execute("SELECT owner FROM enquiry WHERE id = ?", (case,)).fetchone()[0] == "Ben"
    # back to Not assigned
    client.post("/enquiries/{}".format(case), data={"owner": ""})
    assert w["conn"].execute("SELECT owner FROM enquiry WHERE id = ?", (case,)).fetchone()[0] is None


# replaces test_case_page_v2b.test_no_owner_select_on_the_case_page: the select is back (DD1),
# and a save of the card without the field still keeps the person
def test_a_save_without_the_owner_field_keeps_the_person(web):
    w = web
    case = old_case(w, "OPP-9953", "2026-12-09", party=w["tarn"], owner="Ana")
    assert 'name="owner" id="fld-owner"' in w["client"].get("/enquiries/{}".format(case)).data.decode("utf-8")
    w["client"].post("/enquiries/{}".format(case), data={"end_customer": "Corbel Pumps"}, follow_redirects=True)
    row = w["conn"].execute("SELECT owner, end_customer FROM enquiry WHERE id = ?", (case,)).fetchone()
    assert tuple(row) == ("Ana", "Corbel Pumps")


# replaces test_saving.test_the_panel_says_nothing_has_been_saved
def test_the_block_says_nothing_has_been_saved_and_the_page_guards_leaving(web):
    w = web
    old_case(w, "OPP-9954", "2026-12-09")
    body = register(w, fields_of(page(w))).data.decode("utf-8")
    assert "OPP-9954" in body and "Not saved yet: pick one." in body
    assert 'data-must-choose="1"' in body


# replaces test_suggestions.test_register_from_outlook_says_when_the_thread_is_a_case
def test_register_from_outlook_says_when_the_thread_is_a_case(web):
    w = web
    case = old_case(w, "OPP-9955", "2026-12-08")
    b = block(page(w))
    assert ">OPP-9955</a>" in b and "same subject" in b
    assert "Update OPP-9955 from this mail" in b and "Register a new case" in b
    body = page(w, a_mail(subject="A BRAND NEW SUBJECT ABOUT CRATES", key="9956"))
    assert 'id="already"' not in body
    assert 'action="/enquiries/{}/update-from-outlook"'.format(case) not in body


# replaces test_suggestions.test_the_update_button_is_not_trapped_inside_the_registration_form
def test_the_update_button_is_not_trapped_inside_the_registration_form(web):
    from conftest import form_nesting
    w = web
    case = old_case(w, "OPP-9957", "2026-12-08")
    body = page(w)
    deepest, ids = form_nesting(body)
    assert deepest == 1, "a form is nested inside another one"
    assert "update-existing" in ids and "enquiry-form" in ids
    assert ids.index("update-existing") < ids.index("enquiry-form")
    assert 'action="/enquiries/{}/update-from-outlook"'.format(case) in body
    # the block reads above the fields; Register a new case only folds it, at load
    assert body.index('id="already"') < body.index('id="enquiry-form"')
    assert 'data-hide="already"' in body and 'form="update-existing"' in body


# replaces test_threads_and_prefixes.test_register_from_outlook_finds_the_case_through_an_attached_thread
def test_register_from_outlook_finds_the_case_through_an_attached_thread(web):
    w = web
    case = old_case(w, "OPP-9958", "2026-12-08", thread_key="ANOTHERKEY", source_subject="ANOTHER")
    w["repo"].attach_thread(case, "conversation", "CONV-ATTACHED", "2026-12-08", note="renamed")
    w["repo"].commit()
    b = block(page(w, a_mail(subject="RE: renamed by the agent // MIA - SAI", conversation="CONV-ATTACHED")))
    assert ">OPP-9958</a>" in b and "same Outlook conversation" in b and "Update OPP-9958 from this mail" in b


# replaces test_web.test_a_matching_case_stops_the_save_and_shows_the_panel
def test_a_matching_case_stops_the_save_and_shows_the_block(web):
    w = web
    old_case(w, "OPP-9959", "2026-12-08", party=w["tarn"], thread_key="OTHERKEY", source_subject="OTHER",
             origin_loc="MIA", origin_country="USA", dest_loc="SAI", dest_country="Chile", service_mode="Air")
    before = count(w)
    got = register(w, fields_of(page(w)))
    assert got.status_code == 200 and count(w) == before                   # nothing written
    b = block(got.data.decode("utf-8"))
    assert "Already in OppList?" in b and "OPP-9959" in b and "It is OPP-9959" in b


# ---------------------------------- 9. a question logged by hand stays until a person answers it --
QUESTION = "Could you add the delivery to the Corbel plant?"


@pytest.fixture
def asked(web):
    """A quoted case, its question logged by hand on Dec 2 (Waiting on us)."""
    from opplist import detect_followups
    w = web
    case = old_case(w, "OPP-9961", "2026-11-25", status="Quoted", party=w["tarn"], thread_key="Q9961",
                    source_subject="Q9961")
    w["repo"].add_note(case, "2026-12-02", detect_followups.reply_note("Nell Tarn", QUESTION, kind="asks"),
                       source="manual")
    w["repo"].commit()
    w["case"] = case
    return w


def on_pending(w, today=DAY):
    from opplist import assign_list
    return {r.opp_no: r for r in assign_list.pending_listed(w["conn"], today)}


def replied_row(w, today=DAY):
    from opplist import tracking
    return next((i for i in tracking.rule_quoted_no_answer(w["conn"], today, tracking.fu_settings(w["conn"]))
                 if i.enquiry_id == w["case"] and i.extra.get("step") == "Replied"), None)


def test_a_noted_scan_reply_and_our_mail_on_later_days_leave_the_question_open(asked, tmp_path):
    from opplist import detect_followups
    w = asked
    # the morning scan: their later reply (written as noted) and our mail to them (a follow-up)
    w["repo"].add_note(w["case"], "2026-12-04", detect_followups.reply_note(
        "Nell Tarn", "Thanks, any news on the delivery?", "<n1@x>", kind="noted"), source="mail")
    w["repo"].add_followup(w["case"], "2026-12-07", note="FU detected: RE: Q9961 [mid:<f1@x>]", source="mail")
    w["repo"].commit()
    row = on_pending(w)["OPP-9961"]
    assert (row.asked_on, row.note) == ("2026-12-02", detect_followups.reply_note("Nell Tarn", QUESTION, kind="asks"))
    item = replied_row(w)
    assert item is not None and item.since == "2026-12-02" and item.extra["reply_note"].endswith(QUESTION)
    fake = FakeFirestore()
    assert fs.sync_once(w["conn"], fake, {}, today=DAY, reports_dir=tmp_path / "r", record=tmp_path / "x.json").ok
    pending = {r["opp"]: r for r in fake.docs["lists/assign"]["pending"]}
    assert pending["OPP-9961"]["on"] == "2026-12-02" and "Corbel plant" in pending["OPP-9961"]["what"]


def test_answered_takes_it_off(asked):
    w = asked
    got = w["client"].post("/enquiries/{}/answered".format(w["case"]), data={"at": DAY, "note": "sent the rate"},
                           follow_redirects=True).data.decode("utf-8")
    assert "left Pending on us" in got
    assert "OPP-9961" not in on_pending(w) and replied_row(w) is None


def test_an_answered_note_the_scan_wrote_does_not(asked):
    w = asked
    w["repo"].add_note(w["case"], "2026-12-05", "Answered by JJC-DESK: our old AL6a note", source="mail")
    w["repo"].commit()
    assert "OPP-9961" in on_pending(w)


def test_re_quoted_takes_it_off(asked):
    w = asked
    w["client"].post("/tracking/act", data={"do": "quick:requote", "rule": "R3", "kind": "enquiry",
                                            "ref_id": w["case"], "enquiry_id": w["case"], "at": "2026-12-08"})
    kinds = [r[0] for r in w["conn"].execute("SELECT kind FROM status_event WHERE enquiry_id = ? AND voided = 0",
                                             (w["case"],))]
    assert "requote" in kinds
    assert "OPP-9961" not in on_pending(w) and replied_row(w) is None


@pytest.mark.parametrize("status, reason", [("Won", None), ("Lost", "Price too high")])
def test_won_or_lost_takes_it_off(asked, status, reason):
    w = asked
    w["repo"].set_status(w["case"], status, "2026-12-09", closed_reason=reason)
    w["repo"].commit()
    assert "OPP-9961" not in on_pending(w) and replied_row(w) is None
