"""Batch FX5 (2026-11-13, no schema change): Derek's Excel to her rules,
Non-stackable read from what is said (never from a question) and suggested
from later mails, and the commodity on the site's list rows.

1. the Inquiries (xlsx) leaves out a Dropped case and one another office
   handles, orders by case number, writes the modes alphabetically;
2. a question never ticks Non-stackable, the same words said do; non stack,
   non-stack, nonstack and non stacking count; a later mail from the other
   side suggests the tag (Update from Outlook and the morning scan), once;
   ours, a question, or a case that has it gets none; Accept adds it;
3. lists/inquiries rows carry `com`, cut to 80, absent when empty.

Every company, person, mail, place and date here is invented; cases from
OPP-9940, domains .test.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, firebase_sync, inquiries_xlsx as ix, intake, mail_archive, parsing, suggestions
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app

MONTH = "2027-03"
SUBJECT = "LCL RATE // DORVAL - PELLAN // 4 CRATES"
AGENT = ("ilsa@fernhollowlogistics.test", "Ilsa Varrow")
US = ("teo@jojofreight.example", "JJC-TEO BRANNICK")
SAID = "It is non stack and will need to go in a 40' container."
ASKED = "Is this stackable and will fit into a 20' or would you require a 40' for non-stackable?"


@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    path = tmp_path / "fx5.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    db.set_setting(conn, "special_handling", ["DG", "Non-stackable"])
    agent = add_party(repo, "Fernhollow Logistics", country="Chile")
    repo.insert("party_domain", {"domain": "fernhollowlogistics.test", "party_id": agent})
    ids = {"agent": agent, "archive": archive, "path": path}
    for n, status in ((9950, "New"), (9951, "Quoted"), (9952, "New"), (9953, "New")):
        opp = "OPP-{}".format(n)
        ids[opp] = add_enquiry(repo, opp, agent, received="2027-02-02", source_subject="{} {}".format(SUBJECT, opp),
                               thread_key=parsing.subject_thread_key("{} {}".format(SUBJECT, opp)),
                               conversation_id="CONV-{}".format(n), entry_id="EID-REG-{}".format(n),
                               service_mode="LCL", origin_country="USA", dest_country="Chile", scope="In")
        if status == "Quoted":
            repo.set_status(ids[opp], "Quoted", "2027-02-03")
    repo.commit()
    app = create_app(path)
    app.config["TESTING"] = True
    yield {"conn": conn, "repo": repo, "ids": ids, "client": app.test_client()}
    conn.close()


def mail(opp, at, sender, body, key, internal=False):
    n = opp[4:]
    return MailInfo(subject="RE: {} {}".format(SUBJECT, opp), received_on=at[:10], received_at=at, sent_at=at,
                    entry_id="EID-{}".format(key), conversation_id="CONV-{}".format(n), conversation_topic=SUBJECT,
                    sender_name=sender[1], sender_address=sender[0], sender_internal=internal,
                    to_addresses=[US[0] if not internal else AGENT[0]], body_excerpt=body,
                    folder_path="\\Mailbox\\Inbox", message_id="<{}@fx5.test>".format(key))


def store(w, *mails):
    conn = mail_archive.open_archive(w["ids"]["archive"])
    mail_archive.store(conn, [mail_archive.record_from_info(m) for m in mails])
    conn.commit()
    conn.close()


def scan(w, dry_run=False):
    with mail_archive.ArchiveSearch(w["ids"]["archive"]) as search:
        return suggestions.scan_non_stackable(db.Repository(w["conn"], source="scan"), search, dry_run=dry_run)


def tag_rows(w, opp):
    return w["conn"].execute("SELECT * FROM suggestion WHERE opp_no = ? AND kind = 'tag' AND note = ?",
                             (opp, "Non-stackable")).fetchall()


def case_of(w, opp):
    return w["conn"].execute("SELECT * FROM v_enquiry WHERE opp_no = ?", (opp,)).fetchone()


# ---------------------------------------------------- 1. Derek's Excel --
@pytest.fixture
def month(world):
    repo, agent = world["repo"], world["ids"]["agent"]

    def case(opp, received, **extra):
        extra.setdefault("scope", "In")
        return add_enquiry(repo, opp, agent, received=received, origin_loc="MIA", origin_country="USA",
                           dest_loc="SAI", dest_country="Chile", incoterm="FOB", commodity="Slate tiles", **extra)

    case("OPP-9945", "2027-03-01", service_mode="Air")                       # earliest, but a later number
    two = case("OPP-9941", "2027-03-10", service_mode="LCL", lcl_cbm=2.5)
    repo.add_route(two, {"origin_loc": "MIA", "origin_country": "USA", "dest_loc": "SAI", "dest_country": "Chile",
                         "service_mode": "Air"})
    dropped = case("OPP-9942", "2027-03-05", service_mode="Air")
    repo.set_status(dropped, "Dropped", "2027-03-06", closed_reason="Other")
    case("OPP-9943", "2027-03-05", service_mode="Air", handled_by="QZX")      # another office's
    case("OPP-9944", "2027-03-07", service_mode="FCL")
    repo.commit()
    return world


def test_the_file_leaves_out_dropped_and_other_offices_cases_and_orders_by_number(month):
    got = [r.values["Inquiry No."] for r in ix.rows(month["conn"], MONTH)]
    assert got == ["OPP-9941", "OPP-9944", "OPP-9945"]


def test_the_tbc_box_lists_the_same_rows(month):
    opps = {t["opp"] for t in ix.tbc_list(month["conn"], MONTH)}
    assert opps == {"OPP-9941", "OPP-9944", "OPP-9945"}           # 9944's boxes, 9941's and 9945's weight
    html = month["client"].get("/report?month=" + MONTH).data.decode("utf-8")
    box = html[html.index('id="inquiry-tbc"'):html.index("</section>", html.index('id="inquiry-tbc"'))]
    assert "OPP-9942" not in box and "OPP-9943" not in box and "OPP-9944" in box


def test_two_modes_are_written_alphabetically(month):
    row = {r.values["Inquiry No."]: r for r in ix.rows(month["conn"], MONTH)}["OPP-9941"]
    assert row.values["Service Mode"] == "Air & LCL"


# -------------------------------------------- 2a, 2b. what is said, not asked --
@pytest.mark.parametrize("words", ["non stack", "non-stack", "nonstack", "non stacking"])
def test_more_wording_ticks(words):
    assert parsing.non_stackable_line(["The 4 crates are {}.".format(words)])


def test_a_question_does_not_tick_and_the_same_words_said_do():
    assert parsing.non_stackable_line([ASKED]) == ""
    assert parsing.non_stackable_line(["Are the crates non-stackable?"]) == ""
    assert parsing.non_stackable_line(["The crates are non-stackable."]) == "The crates are non-stackable."
    # a question, then the answer on the same line: the answer is read
    assert parsing.non_stackable_line(["Is it non-stackable? Yes, it is non-stackable. Thanks"]) == \
        "Yes, it is non-stackable."


def test_registration_does_not_tick_a_question(world):
    asked = mail("OPP-9950", "2027-02-02 09:00", AGENT, "Hi,\n{}\nIlsa".format(ASKED), "reg-q")
    assert "Non-stackable" not in intake.build(world["conn"], asked).special_tags
    said = mail("OPP-9950", "2027-02-02 09:00", AGENT, "Hi,\n{}\nIlsa".format(SAID), "reg-s")
    draft = intake.build(world["conn"], said)
    assert "Non-stackable" in draft.special_tags and SAID in draft.tag_lines["Non-stackable"]


# ------------------------------------------------- 2c. from a later mail --
def test_update_from_outlook_suggests_it_once_and_the_scan_adds_none(world):
    case = case_of(world, "OPP-9950")
    later = mail("OPP-9950", "2027-02-04 10:00", AGENT, "Hi Teo,\n{} Thanks\nIlsa".format(SAID), "l1")
    suggestions.from_mail(world["repo"], case, later)
    rows = tag_rows(world, "OPP-9950")
    assert len(rows) == 1
    row = rows[0]
    assert (row["state"], row["source"], row["evidence_quote"], row["evidence_date"]) == \
        ("pending", "mail", SAID, "2027-02-04")
    assert row["evidence_sender"] == "Ilsa Varrow <ilsa@fernhollowlogistics.test>"
    assert row["entry_id"] is None and row["message_id"] is None     # its mail stays free for the others
    suggestions.from_mail(world["repo"], case, later)               # again: nothing new
    store(world, later)
    assert scan(world) == []
    assert len(tag_rows(world, "OPP-9950")) == 1


def test_the_scan_suggests_it_once_from_the_archive(world):
    store(world, mail("OPP-9951", "2027-02-03 08:00", US, "Hi Ilsa,\n{}\nTeo".format(ASKED), "s1", internal=True),
          mail("OPP-9951", "2027-02-05 09:00", AGENT, "Hi Teo,\n{}\nIlsa".format(SAID), "s2"))
    got = scan(world)
    assert got == [("OPP-9951", SAID, "Ilsa Varrow <ilsa@fernhollowlogistics.test>", "2027-02-05")]
    assert [r["source"] for r in tag_rows(world, "OPP-9951")] == ["scan"]
    assert scan(world) == []                                         # once
    assert len(tag_rows(world, "OPP-9951")) == 1


def test_ours_a_question_or_a_case_that_has_it_gets_none(world):
    repo = world["repo"]
    store(world,
          # ours, said: not the other side
          mail("OPP-9952", "2027-02-04 08:00", US, "Hi Ilsa,\nNoted: {}\nTeo".format(SAID), "n1", internal=True),
          # theirs, but asked
          mail("OPP-9952", "2027-02-04 09:00", AGENT, "Hi Teo,\nAre the crates non-stackable?\nIlsa", "n2"),
          # said, but the case has the tag already
          mail("OPP-9953", "2027-02-04 09:00", AGENT, "Hi Teo,\n{}\nIlsa".format(SAID), "n3"))
    repo.save_enquiry(world["ids"]["OPP-9953"], {"special_handling": "DG;Non-stackable"})
    repo.commit()
    assert scan(world) == []
    assert tag_rows(world, "OPP-9952") == [] and tag_rows(world, "OPP-9953") == []


def test_not_on_settings_list_none(world):
    db.set_setting(world["conn"], "special_handling", ["DG"])
    world["conn"].commit()
    store(world, mail("OPP-9950", "2027-02-04 09:00", AGENT, "Hi Teo,\n{}\nIlsa".format(SAID), "x1"))
    assert scan(world) == []


def test_the_registered_mail_is_not_a_later_mail(world):
    first = mail("OPP-9950", "2027-02-02 08:00", AGENT, "Hi,\n{}\nIlsa".format(SAID), "REG-9950")
    store(world, first)
    assert scan(world) == []


def test_accept_adds_the_tag_on_the_case_page(world):
    store(world, mail("OPP-9950", "2027-02-04 09:00", AGENT, "Hi Teo,\n{}\nIlsa".format(SAID), "a1"))
    scan(world)
    sid = tag_rows(world, "OPP-9950")[0]["id"]
    case = world["ids"]["OPP-9950"]
    html = world["client"].get("/enquiries/{}".format(case)).data.decode("utf-8")
    card = html[html.index('id="sg-{}"'.format(sid)):]
    card = card[:card.index("</tr>")]
    assert "Add Non-stackable" in card and SAID.replace("'", "&#39;") in card
    assert 'action="/suggestions/{}/accept"'.format(sid) in card and "data-sg-edit" not in card
    got = world["client"].post("/suggestions/{}/accept".format(sid), data={"next": "/enquiries/{}".format(case)},
                               follow_redirects=True).data.decode("utf-8")
    assert "OPP-9950: Non-stackable added to Special handling" in got
    assert case_of(world, "OPP-9950")["special_handling"] == "Non-stackable"
    assert tag_rows(world, "OPP-9950")[0]["state"] == "accepted"
    assert world["conn"].execute("SELECT 1 FROM change_log WHERE table_name = 'enquiry' AND row_id = ? "
                                 "AND field = 'special_handling'", (case,)).fetchone() is not None


def test_dismiss_holds_and_expiry_dismisses_like_the_others(world):
    store(world, mail("OPP-9950", "2027-02-04 09:00", AGENT, "Hi Teo,\n{}\nIlsa".format(SAID), "d1"))
    scan(world)
    sid = tag_rows(world, "OPP-9950")[0]["id"]
    suggestions.dismiss(world["repo"], sid)
    store(world, mail("OPP-9950", "2027-02-06 09:00", AGENT, "Hi Teo,\nAgain: non-stackable.\nIlsa", "d2"))
    assert scan(world) == []                                         # her answer stands
    suggestions.put_back(world["repo"], sid)
    expired = suggestions.expire(world["repo"], today="2099-01-01")
    assert sid in [r["id"] for r in expired]


# ------------------------------------------------------ 3. the site's rows --
def test_list_rows_carry_the_commodity_cut_and_absent_when_empty(world):
    repo = world["repo"]
    repo.save_enquiry(world["ids"]["OPP-9950"], {"commodity": "Cabinets  and sinks"})
    repo.save_enquiry(world["ids"]["OPP-9951"], {"commodity": "Marble " * 20})
    repo.commit()
    built = firebase_sync.build(world["conn"], today="2027-02-10")
    rows = {r["opp"]: r for r in built.inquiries}
    assert rows["OPP-9950"]["com"] == "Cabinets and sinks"
    assert len(rows["OPP-9951"]["com"]) == 80 and rows["OPP-9951"]["com"].endswith("…")
    assert "com" not in rows["OPP-9952"]
    assert "com" not in built.cases["OPP-9950"]                       # the case carries 'commodity'
