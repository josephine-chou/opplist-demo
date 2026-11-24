"""Follow-ups on a quote (schema v13): the event, the R3 ladder,
the mail text, the case page, and the export contract untouched."""

from __future__ import annotations

import csv
import datetime as dt

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, export_csv, export_xlsx, followup, mail_archive, schema_migrations, tracking
from opplist.outlook_bridge import MailInfo

TODAY = "2026-09-16"          # a Wednesday


@pytest.fixture
def party(repo):
    seed_reference(repo)
    party_id = add_party(repo, "Tolvex", country="Chile")
    repo.commit()
    return party_id


def quoted_case(repo, party_id, opp_no="OPP-1407", quoted_on="2026-09-01", **extra):
    """Registered New on 08-28, quoted on `quoted_on`: quoted_on is the
    Quoted event's date, not the day it came in."""
    case = add_enquiry(repo, opp_no, party_id, status="New", received="2026-08-28", **extra)
    repo.set_status(case, "Quoted", quoted_on)
    repo.commit()
    return case


def r3(conn, today=TODAY) -> dict:
    section = {s.rule: s for s in tracking.run(conn, today=today)}["R3"]
    return {i.label: i for i in section.items}


# ------------------------------------------------------------- the event --
def test_schema_v13_has_the_followup_kind_and_columns(conn):
    assert schema_migrations.current_version(conn) >= 13   # v14 (master) came after
    columns = {r[1] for r in conn.execute("PRAGMA table_info(status_event)")}
    assert {"seq", "channel"} <= columns
    assert {"fu_count", "last_fu_on", "last_fu_seq"} <= {
        r[1] for r in conn.execute("PRAGMA table_info(v_enquiry)")}
    assert db.get_setting(conn, "fu1_days") == "1" and db.get_setting(conn, "fu_max") == "2"
    assert "{first_name}" in db.get_setting(conn, "fu_template_1")


def test_add_followup_numbers_itself_and_leaves_the_status_alone(repo, conn, party):
    case = quoted_case(repo, party)
    conn.execute("UPDATE enquiry SET status_confirmed = 0 WHERE id = ?", (case,))
    first = repo.add_followup(case, "2026-09-03")
    second = repo.add_followup(case, "2026-09-10", channel="call", note="left a message")
    repo.commit()
    rows = conn.execute("SELECT seq, channel, note, kind, status FROM status_event "
                        "WHERE kind = 'followup' ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(1, "mail", "FU1 sent", "followup", "Quoted"),
                                        (2, "call", "left a message", "followup", "Quoted")]
    case_row = conn.execute("SELECT status, status_confirmed FROM enquiry WHERE id = ?", (case,)).fetchone()
    assert (case_row["status"], case_row["status_confirmed"]) == ("Quoted", 0)      # neither moved
    view = conn.execute("SELECT fu_count, last_fu_on, last_fu_seq, quoted_on, last_event_on "
                        "FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    assert tuple(view) == (2, "2026-09-10", 2, "2026-09-01", "2026-09-10")   # activity, not a quote date
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'status_event' "
                        "AND row_id IN (?, ?)", (first, second)).fetchone()[0] == 2
    # voided: gone from the count, and the next one takes its number
    repo.update("status_event", second, {"voided": 1})
    repo.add_followup(case, "2026-09-12")
    repo.commit()
    view = conn.execute("SELECT fu_count, last_fu_seq FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    assert tuple(view) == (2, 2)


def test_a_bad_channel_falls_back_to_mail_and_an_unknown_case_is_none(repo, conn, party):
    case = quoted_case(repo, party)
    repo.add_followup(case, "2026-09-03", channel="pigeon")
    assert conn.execute("SELECT channel FROM status_event WHERE kind = 'followup'").fetchone()[0] == "mail"
    assert repo.add_followup(9999, "2026-09-03") is None


# ------------------------------------------------------------ the ladder --
# test_fu_step_alone: replaced in L2 by tests/test_fu1_wait_rewrites.py with invented data (no FU2 without an answer to FU1)


def test_fu1_is_due_one_working_day_after_the_quote(repo, conn, party):
    quoted_case(repo, party, "OPP-0001", quoted_on="2026-09-15")     # yesterday: 1 working day
    quoted_case(repo, party, "OPP-0002", quoted_on="2026-09-16")     # today: not yet
    found = r3(conn)
    assert list(found) == ["OPP-0001"]
    assert found["OPP-0001"].extra["step"] == "FU1" and found["OPP-0001"].days == 1
    assert found["OPP-0001"].since == "2026-09-15" and found["OPP-0001"].extra["next_seq"] == 1
    assert "FU1" not in [i.extra.get("step") for i in r3(conn).values() if i.label == "OPP-0002"]


# test_fu2_is_due_five_working_days_after_fu1: replaced in L2 by tests/test_fu1_wait_rewrites.py with invented data (no FU2 without an answer to FU1)


def test_decide_comes_five_working_days_after_fu_max(repo, conn, party):
    a = quoted_case(repo, party, "OPP-0001", quoted_on="2026-09-01")
    b = quoted_case(repo, party, "OPP-0002", quoted_on="2026-09-01")
    for case, last in ((a, "2026-09-09"), (b, "2026-09-10")):
        repo.add_followup(case, "2026-09-03")
        repo.add_followup(case, last)
    repo.commit()
    found = r3(conn)
    assert list(found) == ["OPP-0001"]
    assert found["OPP-0001"].extra["step"] == "Decide" and found["OPP-0001"].extra["fu_count"] == 2
    # a third follow-up logged anyway: still Decide, never FU3
    repo.add_followup(a, "2026-09-09")
    repo.commit()
    assert r3(conn)["OPP-0001"].extra["step"] == "Decide"


def test_a_note_restarts_the_clock(repo, conn, party):
    case = quoted_case(repo, party, quoted_on="2026-09-10")       # 4 working days: FU1
    assert r3(conn)["OPP-1407"].extra["step"] == "FU1"
    repo.add_note(case, "2026-09-16", "they called, still deciding")
    repo.commit()
    assert "OPP-1407" not in r3(conn)                           # clock is today
    assert r3(conn, today="2026-09-17")["OPP-1407"].since == "2026-09-16"
    # a voided note does not
    event = conn.execute("SELECT id FROM status_event WHERE kind = 'note'").fetchone()["id"]
    repo.update("status_event", event, {"voided": 1})
    repo.commit()
    assert r3(conn)["OPP-1407"].since == "2026-09-10"


# test_the_settings_drive_the_ladder: replaced in L2 by tests/test_fu1_wait_rewrites.py with invented data (no FU2 without an answer to FU1)


def test_the_section_is_named_for_the_ladder_and_shows_the_step(repo, conn, party):
    quoted_case(repo, party, quoted_on="2026-09-10")
    section = {s.rule: s for s in tracking.run(conn, today=TODAY)}["R3"]
    assert section.title == "Quoted, follow up"
    assert "FU1" in [i.extra["step"] for i in section.items]
    assert any("[R3] Quoted, follow up" in line for line in tracking.render(tracking.run(conn, today=TODAY)))


# --------------------------------------------------------------- the text --
def test_dates_the_way_people_write_them():
    assert followup.short_date("2026-09-14") == "Sep 14"
    assert followup.parse_loose_date("Sep 30", 2026) == dt.date(2026, 9, 30)
    assert followup.parse_loose_date("September 30, 2026") == dt.date(2026, 9, 30)
    assert followup.parse_loose_date("30 Sep 2026") == dt.date(2026, 9, 30)
    assert followup.parse_loose_date("9/30/2026") == dt.date(2026, 9, 30)
    assert followup.parse_loose_date("30/09/2026") == dt.date(2026, 9, 30)
    assert followup.parse_loose_date("2026-09-30") == dt.date(2026, 9, 30)
    assert followup.parse_loose_date("30th September", 2026) == dt.date(2026, 9, 30)
    assert followup.parse_loose_date("someday") is None
    assert followup.valid_until("Rates are valid until Sep 30. Subject to space.", 2026) == dt.date(2026, 9, 30)
    assert followup.valid_until("Validity: 30/09/2026", 2026) == dt.date(2026, 9, 30)
    assert followup.valid_until("valid through October 15th, 2026") == dt.date(2026, 10, 15)
    assert followup.valid_until("no validity stated") is None


def test_first_names():
    assert followup.first_name_of("Renata Solis") == "Renata"
    assert followup.first_name_of("Solis, Renata") == "Renata"
    assert followup.first_name_of("RENATA SOLIS") == "Renata"
    assert followup.first_name_of("renata@tolvex.example") == ""
    assert followup.first_name_of("JJC-VICTOR LANG") == ""
    assert followup.first_name_of("") == ""


def fcl_case(repo, party, opp_no="OPP-1407", **extra):
    values = dict(origin_loc="LAX", origin_country="USA", dest_loc="SAI", dest_country="Chile",
                  service_mode="FCL", container_type="40HQ", container_qty=2)
    values.update(extra)
    return quoted_case(repo, party, opp_no, **values)


def no_sources():
    return followup.Sources("", None, "")


FU1_V1 = ("Hi Renata,\n\n"
          "Are the FCL rates below workable for this shipment? We can book as soon "
          "as you give the go-ahead, and we are glad to rework anything that does not fit.\n"
          "Our rates are valid until Sep 30.\n"
          "Also, please confirm the day the cartons are ready.\n"
          "Thank you.\n")


def test_fu1_version_1_with_everything_filled(repo, conn, party):
    case = fcl_case(repo, party)
    text = followup.build(conn, case, 1, variant=1,
                          sources=followup.Sources("Renata", dt.date(2026, 9, 30),
                                                   "Also, please confirm the day the cartons are ready."))
    assert text == FU1_V1


def test_fu1_version_2_with_nothing_from_the_archive(repo, conn, party):
    """No name, no validity, no question: the greeting loses the name and
    the two blank lines vanish, the closing sits right under the paragraph."""
    case = fcl_case(repo, party, service_mode="Air")
    assert followup.build(conn, case, 1, variant=2, sources=no_sources()) == (
        "Hi,\n\n"
        "Following up on the air rates below: do they suit what you need? If you want to go ahead, "
        "or if another option should be priced, just say the word.\n"
        "Thank you.\n")


def test_fu1_version_3_with_the_validity_alone(repo, conn, party):
    case = fcl_case(repo, party, service_mode="LCL", lcl_cbm=3.5)
    assert followup.build(conn, case, 1, variant=3,
                          sources=followup.Sources("Ana", dt.date(2026, 10, 15), "")) == (
        "Hi Ana,\n\n"
        "Checking back to ask how the LCL rates below sit with you. Should a figure need another look, "
        "or should we check something else, send us a line and we will get onto it.\n"
        "Our rates are valid until Oct 15.\n"
        "Thank you.\n")


def test_fu1_version_4_client_by_requester_type(repo, conn, party):
    """Version 4 carries {client}: an agent's client, an office's customer,
    'you' for a direct customer; the open question alone on its line."""
    case = fcl_case(repo, party)
    src = followup.Sources("Ana", None, "Also, is the cargo stackable?")
    assert followup.build(conn, case, 1, variant=4, sources=src) == (
        "Hi Ana,\n\n"
        "Hoping your week is going well. We are circling back to find out whether the FCL rates below "
        "are a fit for your client. Any feedback is welcome, and any piece of the offer can be revisited.\n"
        "Also, is the cargo stackable?\n"
        "Thank you.\n")
    office = add_party(repo, "JoJo JJV", ptype="Office", country="USA")
    direct = add_party(repo, "Saltera", ptype="Direct Customer", country="Chile")
    for who, phrase in ((office, "are a fit for your customer."), (direct, "are a fit for you.")):
        repo.update("enquiry", case, {"requester_party_id": who})
        repo.commit()
        assert phrase in followup.build(conn, case, 1, variant=4, sources=src), who


def test_fu1_version_5_and_a_mode_the_pool_does_not_name(repo, conn, party):
    case = quoted_case(repo, party, service_mode="RORO")
    assert followup.build(conn, case, 1, variant=5, sources=followup.Sources("Ana", None, "")) == (
        "Hi Ana,\n\n"
        "Following up with a short reminder about the rates below. Do the numbers work for your client? "
        "If a leg or a charge should be priced again, we can do that quickly.\n"
        "Thank you.\n")


def test_fu2_and_fu3_pools(repo, conn, party):
    case = fcl_case(repo, party)
    assert followup.build(conn, case, 2, variant=1, sources=followup.Sources("Ana", None, "")) == (
        "Hi Ana,\n\n"
        "Following up on our offer below, which we have not heard back on yet. Is it still being "
        "weighed, or has the cargo plan moved on?\n"
        "Thank you.\n")
    assert followup.build(conn, case, 2, variant=6, sources=no_sources()) == (
        "Hi,\n\n"
        "Following up to ask whether anything has moved on the cargo below. If the timeline is "
        "different now, send us the new dates and we will bring the offer up to date.\n"
        "We look forward to hearing from you.\n")
    assert followup.build(conn, case, 3, variant=4, sources=followup.Sources("Ana", None, "")) == (
        "Hi Ana,\n\n"
        "Could you share how the bid below turned out? If another offer was chosen, was it mainly the "
        "price, the routing or something else? Knowing that lets us quote better next time.\n"
        "Thank you.\n")
    assert [len(followup.versions(conn, n)) for n in (1, 2, 3)] == [5, 6, 4]
    assert all(v.startswith("Hi {first_name},\n\n") for n in (1, 2, 3) for v in followup.versions(conn, n))
    assert not any("{which}" in v or "{shipment}" in v or "{quote_date}" in v
                   for n in (1, 2, 3) for v in followup.versions(conn, n))


def test_the_rotation_follows_the_requesters_earlier_follow_ups(repo, conn, party):
    """FU1 sent twice before for this requester (on other cases): version 3 now."""
    case = fcl_case(repo, party)
    other = fcl_case(repo, party, "OPP-1408")
    repo.add_followup(other, "2026-09-03")                        # FU1 on another case
    third = fcl_case(repo, party, "OPP-1409")
    repo.add_followup(third, "2026-09-04")                        # FU1 again
    repo.add_followup(third, "2026-09-11")                        # FU2: another step, not counted
    stranger = add_party(repo, "Seavio", country="USA")
    theirs = fcl_case(repo, stranger, "OPP-1411")
    repo.add_followup(theirs, "2026-09-05")                       # somebody else's: not counted
    repo.commit()
    assert followup.rotation_index(conn, party, 1, 5) == 2
    assert followup.rotation_index(conn, party, 2, 6) == 1
    assert followup.rotation_index(conn, stranger, 1, 5) == 1
    built = followup.compose(conn, case, 1, sources=followup.Sources("Ana", None, ""))
    assert (built.index, built.count, built.seq) == (2, 5, 1)
    assert built.text.startswith("Hi Ana,\n\nChecking back to ask how the FCL rates below sit with you.")
    # a voided follow-up does not count
    event = conn.execute("SELECT id FROM status_event WHERE kind = 'followup' AND at = '2026-09-03'").fetchone()["id"]
    repo.update("status_event", event, {"voided": 1})
    repo.commit()
    assert followup.rotation_index(conn, party, 1, 5) == 1


def test_the_rotation_walks_all_six_fu2_versions_and_comes_back(repo, conn, party):
    case = fcl_case(repo, party)
    seen = []
    for n in range(7):
        built = followup.compose(conn, case, 2, sources=no_sources())
        seen.append(built.index)
        repo.add_followup(case, "2026-09-{:02d}".format(3 + n), seq=2)   # logged as FU2 each time
        repo.commit()
    assert seen == [0, 1, 2, 3, 4, 5, 0]


def test_variant_wraps_round(repo, conn, party):
    case = fcl_case(repo, party)
    src = followup.Sources("Ana", None, "")
    assert followup.compose(conn, case, 1, sources=src, variant=1).index == 0
    assert followup.compose(conn, case, 1, sources=src, variant=5).index == 4
    assert followup.compose(conn, case, 1, sources=src, variant=6).index == 0     # 6 of 5 wraps
    assert followup.compose(conn, case, 3, sources=src, variant=5).index == 0     # 5 of 4 wraps


def test_no_blank_line_before_the_closing_and_none_left_by_empty_blanks(repo, conn, party):
    case = fcl_case(repo, party)
    for seq in (1, 2, 3):
        for variant in range(1, len(followup.versions(conn, seq)) + 1):
            text = followup.build(conn, case, seq, variant=variant, sources=no_sources())
            body = text.split("\n\n", 1)[1]                        # after the greeting's blank line
            assert "\n\n" not in body, (seq, variant, text)
            assert text.rstrip("\n").endswith(("Thank you.", "We look forward to hearing from you."))
    # a template of her own with blank lines of its own keeps at most one
    db.set_setting(conn, "fu_template_1", "Hi {first_name},\n\n\n{valid_until_line}\n\n{open_question_line}\n\n\n"
                                          "Checking on {rates}.\n\n\n\nThank you.")
    repo.commit()
    text = followup.build(conn, case, 1, sources=no_sources())
    assert text == "Hi,\n\nChecking on the FCL rates below.\n\nThank you.\n"


def test_the_old_default_templates_are_replaced_by_the_pool(repo, conn, party):
    """A database still holding one of the earlier defaults is not hers:
    the current pool takes over. Anything she typed stays."""
    case = fcl_case(repo, party)
    for old in followup.OLD_FU_TEMPLATES["fu_template_2"]:
        db.set_setting(conn, "fu_template_2", old)
        repo.commit()
        assert len(followup.versions(conn, 2)) == 6
    db.set_setting(conn, "fu_template_2", "Hello {first_name} - news?")
    repo.commit()
    assert followup.versions(conn, 2) == ["Hello {first_name} - news?"]
    assert followup.build(conn, case, 2, sources=followup.Sources("Ana", None, "")) == "Hello Ana - news?\n"


def test_open_questions_from_our_quote_mail():
    q = followup.open_question
    assert q("Please find our rates below.\nPlease confirm the day the cartons are ready.\nThanks.") == \
        "Also, please confirm the day the cartons are ready."
    assert q("Rates are valid until Sep 30. Is the cargo stackable? Let us know the pickup address.") == \
        "Also, let us know the pickup address."
    assert q("Is the cargo stackable?") == "Also, is the cargo stackable?"
    assert q("Kindly advise the HS code") == "Also, kindly advise the HS code."
    assert q("Please find our rates below. Best regards.") == ""
    # the closing courtesy of every quote is not an open question
    assert q("Let us know if you would like to proceed.") == ""
    assert q("Kindly advise if any questions come up.") == ""
    assert q("Kindly advise the HS code. Let us know if you would like to proceed.") == "Also, kindly advise the HS code."
    assert q("Could you " + "x" * 120 + "?") == ""                 # over 120 characters
    assert q("") == ""


def _mail(subject, sender, name, at, body="", conversation="CONV-407", internal=False, entry=""):
    return MailInfo(subject=subject, received_on=at[:10], received_at=at, entry_id=entry or "E-" + at,
                    conversation_id=conversation, conversation_topic=subject, sender_name=name,
                    sender_address=sender, sender_internal=internal, to_addresses=["VictorLang@jojofreight.example"],
                    cc_addresses=[], body_excerpt=body, folder_path="\\X",
                    message_id="<{}@x>".format(at))


def test_the_name_and_the_validity_come_from_the_archive(repo, conn, party, tmp_path):
    case = quoted_case(repo, party, origin_loc="LAX", origin_country="USA", dest_loc="SAI",
                       dest_country="Chile", service_mode="FCL", container_type="40HQ", container_qty=2,
                       entry_id="ENTRY-407", conversation_id="CONV-407")
    archive_path = tmp_path / "mail.db"
    archive = mail_archive.open_archive(archive_path)
    mail_archive.store(archive, [mail_archive.record_from_info(m) for m in (
        _mail("RATE REQUEST // LAX - SAI", "renata@tolvex.example", "Renata Solis", "2026-08-28 09:00",
              entry="ENTRY-407"),
        _mail("RE: RATE REQUEST // LAX - SAI", "VictorLang@jojofreight.example", "JJC-VICTOR LANG",
              "2026-09-01 15:00", internal=True,
              body="Hi Renata,\n\nPlease find our rates below. Rates are valid until Sep 30.\n\n"
                   "From: Renata Solis\nSent: Friday\n\nvalid until Aug 1 (old)"),
    )])
    archive.commit()
    archive.close()
    assert followup.build(conn, case, 1, variant=1, archive_path=archive_path) == (
        "Hi Renata,\n\n"
        "Are the FCL rates below workable for this shipment? We can book as soon "
        "as you give the go-ahead, and we are glad to rework anything that does not fit.\n"
        "Our rates are valid until Sep 30.\n"
        "Thank you.\n")
    # no archive: the same text without the name and the validity line
    text = followup.build(conn, case, 1, variant=1, archive_path=tmp_path / "none.db")
    assert text.startswith("Hi,\n\n") and "valid until" not in text


# ---------------------------------------------------------------- the web --
@pytest.fixture
def web(tmp_path):
    from opplist.web import create_app

    path = tmp_path / "fu.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party_id = add_party(repo, "Tolvex", country="Chile")
    case = quoted_case(repo, party_id, quoted_on="2026-09-10", origin_loc="LAX", origin_country="USA",
                       dest_loc="SAI", dest_country="Chile", service_mode="FCL",
                       container_type="40HQ", container_qty=2)
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, case


# test_the_case_page_has_the_followup_section moved to test_draft_fu_note.py with invented data (24c: Another version is gone)


# test_log_as_sent_moves_the_tracking_row_to_the_next_step: replaced in L2 by tests/test_fu1_wait_rewrites.py with invented data (no FU2 without an answer to FU1)


def test_log_fu_from_the_tracking_row(web):
    client, path, case = web
    page = client.post("/tracking/act", data={
        "do": "followup", "rule": "R3", "kind": "enquiry", "ref_id": case, "enquiry_id": case,
        "at": "2026-09-10"}, follow_redirects=True)
    assert b"FU1 logged on OPP-1407 (mail, 2026-09-10)" in page.data
    page = client.post("/tracking/act", data={
        "do": "followup", "rule": "R3", "kind": "enquiry", "ref_id": case, "enquiry_id": case,
        "at": "2026-09-12", "channel": "call"}, follow_redirects=True)
    assert b"FU2 logged on OPP-1407 (call, 2026-09-12)" in page.data


# test_the_settings_page_edits_the_templates moved to test_draft_fu_note.py with invented data (24c: Another version is gone)


# ------------------------------------------------------- the export contract --
def test_the_export_contract_is_untouched(repo, conn, party, tmp_path):
    case = quoted_case(repo, party)
    repo.add_followup(case, "2026-09-03")
    repo.commit()
    path = export_csv.export_opp_index(conn, tmp_path / "opp-index.csv")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert len(rows[0]) == 8 and len(rows[1]) == 8
    assert len(export_xlsx.OPPORTUNITY_COLUMNS) == 41
    xlsx = export_xlsx.export(conn, tmp_path / "x.xlsx", db_path=tmp_path / "test.db")
    from openpyxl import load_workbook
    headers = [c.value for c in load_workbook(xlsx, read_only=True)["Opportunities"][1]]
    assert len(headers) == 41 and headers == export_xlsx.OPPORTUNITY_COLUMNS


# ------------------------------------------- quoted again: a new round ----
def test_a_case_quoted_again_starts_the_ladder_over(repo, conn, party):
    """2026-09-17: the ladder counts the follow-ups of the
    current quote round, so a case quoted again is chased from FU1."""
    case = quoted_case(repo, party, quoted_on="2026-09-01")
    repo.add_followup(case, "2026-09-02")
    repo.add_followup(case, "2026-09-08")
    repo.commit()
    row = conn.execute("SELECT fu_count, fu_count_total, last_fu_on, last_fu_seq "
                       "FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    assert tuple(row) == (2, 2, "2026-09-08", 2)
    assert r3(conn)["OPP-1407"].extra["step"] == "Decide"          # two sent, five days on

    repo.set_status(case, "Quoted", "2026-09-14")                  # they came back, we quoted again
    repo.commit()
    row = conn.execute("SELECT fu_count, fu_count_total, last_fu_on, last_fu_seq "
                       "FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    assert tuple(row) == (0, 2, None, None)
    item = r3(conn)["OPP-1407"]
    assert (item.extra["step"], item.since, item.extra["next_seq"]) == ("FU1", "2026-09-14", 1)

    # the next follow-up is FU1 again: one case, two FU1s on different days
    repo.add_followup(case, "2026-09-15")
    repo.commit()
    assert [(r["at"], r["seq"]) for r in conn.execute(
        "SELECT at, seq FROM status_event WHERE kind = 'followup' ORDER BY at")] == \
        [("2026-09-02", 1), ("2026-09-08", 2), ("2026-09-15", 1)]
    row = conn.execute("SELECT fu_count, fu_count_total, last_fu_seq FROM v_enquiry WHERE id = ?",
                       (case,)).fetchone()
    assert tuple(row) == (1, 3, 1)
    assert "OPP-1407" not in r3(conn)                                     # one working day after FU1


def test_a_voided_re_quote_gives_the_old_round_back(repo, conn, party):
    case = quoted_case(repo, party, quoted_on="2026-09-01")
    repo.add_followup(case, "2026-09-02")
    repo.set_status(case, "Quoted", "2026-09-14")
    repo.commit()
    assert conn.execute("SELECT fu_count FROM v_enquiry WHERE id = ?", (case,)).fetchone()[0] == 0
    event = conn.execute("SELECT id FROM status_event WHERE kind = 'status' AND at = '2026-09-14'").fetchone()["id"]
    repo.update("status_event", event, {"voided": 1})
    repo.commit()
    assert conn.execute("SELECT fu_count FROM v_enquiry WHERE id = ?", (case,)).fetchone()[0] == 1


# test_the_case_page_counts_the_round_and_names_the_total: rewritten with invented data in
# tests/test_fx4_rewritten.py (FX4: the FU picker went with the draft). The unread-reply
# tests went with the draft they held (FX4: she has her own template tool).
