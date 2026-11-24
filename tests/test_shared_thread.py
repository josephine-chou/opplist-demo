"""Batch N1: a case number in a pasted subject decides first; a subject two
cases share is split by date for the mails without a number.

An agent asks something new in an old thread: the new case derives the old
case's thread key and shares its Outlook conversation. Each case keeps the
unnumbered mails from its own registration day on; a numbered mail goes by
its number; each case's registered mail is its own. Every company, person,
mail, subject, place and date here is invented; cases from OPP-4101.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import ack, case_mail, case_numbers, db, detect_followups, mail_archive, outlook_bridge, parsing, \
    propose, suggestions
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app

SUBJECT = "Olive oil ex works Mendoza to Busan (trial order)"
KEY = parsing.subject_thread_key(SUBJECT)
AGENT = ("lin@brightwateragents.example", "Lin Okafor")
US = ("desk@jojofreight.example", "JJC-DESK")


def mail(subject, at, sender, name, body, mid, internal=False, entry=None, to=(US[0],), to_names=(),
         conversation="CONV-OIL"):
    return MailInfo(subject=subject, received_on=at[:10], received_at=at, entry_id=entry or "EID-" + mid,
                    conversation_id=conversation, conversation_topic=parsing.clean_subject(subject),
                    sender_name=name, sender_address=sender, sender_internal=internal,
                    to_addresses=list(to), to_names=list(to_names), body_excerpt=body,
                    folder_path="\\\\Personal Folder\\\\AGENTS", display_to="; ".join(to),
                    message_id="<{}@x>".format(mid))


MAILS = [
    mail("FW: " + SUBJECT, "2026-07-30 08:00", AGENT[0], AGENT[1], "Hi,\n\nA first look at olive oil.\n", "pre"),
    mail(SUBJECT, "2026-08-05 09:00", AGENT[0], AGENT[1], "Hi,\n\nPlease quote 1 x 20GP olive oil.\n", "o1",
         entry="EID-OLD"),
    mail("RE: " + SUBJECT, "2026-08-06 10:00", US[0], US[1], "Hi Lin,\n\nOur rate is USD 2,150 all-in.\n", "o2",
         internal=True, to=(AGENT[0],)),
    mail("RE: " + SUBJECT, "2026-09-23 11:00", AGENT[0], AGENT[1], "Hi,\n\nAny news on the rate?\n", "o3"),
    mail("RE: " + SUBJECT, "2026-10-05 08:30", AGENT[0], AGENT[1],
         "Hi,\n\nNew trial: 2 x 20GP to Busan, please quote.\n", "n1", entry="EID-NEW"),
    mail("RE: " + SUBJECT, "2026-10-06 09:15", AGENT[0], AGENT[1], "Hi,\n\nThe cargo is ready on the 20th.\n", "n2"),
    mail("RE: [OPP-4102] " + SUBJECT, "2026-08-10 12:00", AGENT[0], AGENT[1],
         "Hi,\n\nA note with the new number on it.\n", "n4"),
]


@pytest.fixture
def archive(tmp_path, monkeypatch):
    path = tmp_path / "archive" / "mail.db"
    path.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", path)
    conn = mail_archive.open_archive(path)
    mail_archive.store(conn, [mail_archive.record_from_info(m) for m in MAILS])
    mail_archive.set_meta(conn, "last_sync_at", "2026-10-06 12:00:00")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def world(tmp_path, archive):
    path = tmp_path / "shared.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    agent = add_party(repo, "Brightwater Agents", country="Chile")
    repo.insert("party_domain", {"domain": "brightwateragents.example", "party_id": agent})
    ids = {"agent": agent, "path": path}
    ids["old"] = add_enquiry(repo, "OPP-4101", agent, received="2026-08-05", source_subject=SUBJECT,
                             thread_key=KEY, entry_id="EID-OLD", conversation_id="CONV-OIL", service_mode="FCL",
                             created_by="outlook")
    repo.set_status(ids["old"], "Quoted", "2026-08-06")
    ids["new"] = add_enquiry(repo, "OPP-4102", agent, received="2026-10-05", source_subject="RE: " + SUBJECT,
                             thread_key=KEY, entry_id="EID-NEW", conversation_id="CONV-OIL", service_mode="FCL",
                             created_by="outlook")
    db.set_setting(conn, "next_opp_number", 4103)
    repo.commit()
    yield conn, repo, ids
    conn.close()


def case_row(conn, case_id):
    return conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (case_id,)).fetchone()


def card(conn, archive, case_id):
    with mail_archive.ArchiveSearch(archive) as search:
        return sorted(m.message_id for m in case_mail.case_mails(conn, search, case_row(conn, case_id)))


def client_of(ids):
    app = create_app(ids["path"])
    app.config["TESTING"] = True
    return app.test_client()


# ----------------------------------------------------- 1. the rule itself --
def test_each_case_holds_its_own_side_of_the_shared_thread(world, archive):
    conn, repo, ids = world
    # the older case: a mail before both, its registered mail, our quote, the August and September mails
    assert card(conn, archive, ids["old"]) == ["<o1@x>", "<o2@x>", "<o3@x>", "<pre@x>"]
    # the newer case: its registered mail, the mail after it, and the numbered mail whatever its date
    assert card(conn, archive, ids["new"]) == ["<n1@x>", "<n2@x>", "<n4@x>"]


def test_the_split_by_day():
    owners = [{"id": 1, "opp_no": "OPP-4101", "status": "Quoted", "received_on": "2026-08-05"},
              {"id": 2, "opp_no": "OPP-4102", "status": "New", "received_on": "2026-10-05"},
              {"id": 3, "opp_no": "OPP-4103", "status": "New", "received_on": None}]
    assert case_numbers.split_owners(owners, "2026-07-01") == {1, 3}         # before all: the earliest day's
    assert case_numbers.split_owners(owners, "2026-10-04") == {1, 3}
    assert case_numbers.split_owners(owners, "2026-10-05") == {2}
    [other] = [o for o in case_numbers.shared_with(2, owners[:2])]
    assert (other["theirs"], other["mine"]) == ("its mails up to Oct 4", "this case's from Oct 5")


def test_two_same_day_cases_keep_sharing(world, archive):
    conn, repo, ids = world
    agent = ids["agent"]
    a = add_enquiry(repo, "OPP-4103", agent, received="2026-11-02", source_subject="Pallets to Rosario",
                    thread_key=parsing.subject_thread_key("Pallets to Rosario"), service_mode="Air")
    b = add_enquiry(repo, "OPP-4104", agent, received="2026-11-02", source_subject="Pallets to Rosario",
                    thread_key=parsing.subject_thread_key("Pallets to Rosario"), service_mode="Air")
    repo.commit()
    later = mail("RE: Pallets to Rosario", "2026-11-09 10:00", AGENT[0], AGENT[1], "Hi,\n\nAny news?\n", "r1",
                 conversation="CONV-ROS")
    assert case_numbers.belongs(conn, a, later) and case_numbers.belongs(conn, b, later)
    owners = case_numbers.case_owners(conn, a, key=parsing.subject_thread_key("Pallets to Rosario"))
    assert case_numbers.shared_with(a, owners)[0]["same_day"] is True


def test_a_case_not_among_the_owners_keeps_today_s_reading(world):
    conn, repo, ids = world
    other = add_enquiry(repo, "OPP-4105", ids["agent"], received="2026-11-01", source_subject="Something else",
                        thread_key="SOMETHINGELSE", service_mode="Air")
    repo.commit()
    assert case_numbers.belongs(conn, other, MAILS[5])                 # found by other means: kept


def test_propose_and_detect_followups_on_the_older_case_skip_the_newer_mails(world, archive):
    conn, repo, ids = world
    with mail_archive.ArchiveSearch(archive) as search:
        found = detect_followups.thread_of(search, case_row(conn, ids["old"]), conn=conn)
        assert "<n2@x>" not in [m.message_id for m in found] and "<o3@x>" in [m.message_id for m in found]
        [proposal] = propose.propose(conn, search, opp_nos=["OPP-4101"], scope="all")
    assert not {"<n1@x>", "<n2@x>", "<n4@x>"} & {getattr(m, "message_id", "") for m in proposal.mails}


# -------------------------------------------- Update from Outlook, Register --
def test_update_from_outlook_on_the_older_case_names_the_newer(world):
    conn, repo, ids = world
    found = suggestions.membership(conn, case_row(conn, ids["old"]), MAILS[5])
    assert not found.belongs
    assert found.how == "this thread is also OPP-4102's: a mail of Oct 6 is OPP-4102's"
    assert found.others == [(ids["new"], "OPP-4102")]
    right = suggestions.membership(conn, case_row(conn, ids["new"]), MAILS[5])
    assert right.belongs and right.how == "same Outlook conversation (shared with OPP-4101: this case's from Oct 5)"
    # the August reply is still the older case's
    assert suggestions.membership(conn, case_row(conn, ids["old"]), MAILS[3]).belongs


def test_register_from_outlook_names_the_newer_case(world, monkeypatch):
    conn, repo, ids = world
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: MAILS[5])
    body = client_of(ids).post("/enquiries/from-outlook").data.decode("utf-8")
    # DD1: the thread's case is a row of "Already in OppList?"
    banner = body[body.index('id="already"'):body.index("</section>", body.index('id="already"'))]
    # DD1: OPP-4101 is still Quoted but came in on Aug 5, past the 14 days - seen before, its own line
    assert ">OPP-4102</a>" in banner and "same Outlook conversation" in banner and "OPP-4101" not in banner
    seen = body[body.index('id="seen-before"'):body.index('id="already"')]
    assert "Same Outlook conversation as" in seen and "OPP-4101" in seen and "Quoted" in seen
    assert "Update OPP-4101 from this mail" in seen                     # still open: a plain Update


# ------------------------------------------------- 2. Add another subject --
def subjects(conn, case_id):
    return [tuple(r) for r in conn.execute("SELECT kind, value, note FROM enquiry_thread WHERE enquiry_id = ? "
                                           "ORDER BY id", (case_id,))]


def test_this_case_s_number_attaches_nothing(world):
    conn, repo, ids = world
    page = client_of(ids).post("/enquiries/{}/subjects".format(ids["new"]), data={"subject": "RE: [OPP-4102] " + SUBJECT},
                               follow_redirects=True).data.decode()
    assert "Mails carrying OPP-4102 are this case&#39;s already (1 in the archive); nothing was attached." in page
    assert subjects(conn, ids["new"]) == [] and 'class="clash"' not in page


def test_another_case_s_number_is_refused_and_named(world):
    conn, repo, ids = world
    page = client_of(ids).post("/enquiries/{}/subjects".format(ids["new"]), data={"subject": "RE: [OPP-4101] " + SUBJECT},
                               follow_redirects=True).data.decode()
    assert "That subject carries the number of OPP-4101; nothing was attached." in page
    clash = page[page.index('class="clash"'):page.index("</p>", page.index('class="clash"'))]
    assert "This subject carries the number of" in clash and ">OPP-4101</a>" in clash and "Add anyway" not in clash
    assert subjects(conn, ids["new"]) == []


def test_two_numbers_are_refused_in_one_line(world):
    conn, repo, ids = world
    page = client_of(ids).post("/enquiries/{}/subjects".format(ids["new"]),
                               data={"subject": "RE: [OPP-4101][OPP-4102] " + SUBJECT}, follow_redirects=True).data.decode()
    assert "That subject carries two case numbers (OPP-4101, OPP-4102); nothing was attached." in page


def test_a_key_already_this_case_s_that_another_has_too(world):
    conn, repo, ids = world
    page = client_of(ids).post("/enquiries/{}/subjects".format(ids["new"]), data={"subject": "FW: " + SUBJECT},
                               follow_redirects=True).data.decode()
    assert ("That subject is already this case&#39;s (7 mails match). OPP-4101 has it too: its mails up to Oct 4, "
            "this case&#39;s from Oct 5.") in page


def test_a_key_another_case_s_only_offers_add_anyway(world):
    conn, repo, ids = world
    lone = add_enquiry(repo, "OPP-4106", ids["agent"], received="2026-10-20", source_subject="Wine to Seoul",
                       thread_key="WINETOSEOUL", service_mode="FCL")
    repo.commit()
    web = client_of(ids)
    page = web.post("/enquiries/{}/subjects".format(lone), data={"subject": "RE: " + SUBJECT},
                    follow_redirects=True).data.decode()
    clash = page[page.index('class="clash"'):]
    assert "This subject is already" in clash and ">Add anyway</button>" in clash
    assert subjects(conn, lone) == []
    page = web.post("/enquiries/{}/subjects".format(lone), data={"subject": "RE: " + SUBJECT, "anyway": "1"},
                    follow_redirects=True).data.decode()
    assert subjects(conn, lone) == [("thread_key", KEY, "RE: " + SUBJECT)]
    assert ("Subject added (7 mails match in the archive). OPP-4101 has it too: its mails up to Oct 4, this "
            "case&#39;s from Oct 20. OPP-4102 has it too: its mails Oct 5 to Oct 19, this case&#39;s from Oct 20.") in page


# ---------------------------------------------- 3. the Subjects card line --
def test_the_subjects_card_says_the_subject_is_shared(world):
    conn, repo, ids = world
    page = client_of(ids).get("/enquiries/{}".format(ids["new"])).data.decode()
    line = page[page.index('class="shared-line'):page.index("</span>", page.index('class="shared-line'))]
    assert "also <a href=\"/enquiries/{}\" class=\"mono\">OPP-4101</a>'s (Quoted): its mails up to Oct 4, " \
           "this case&#39;s from Oct 5".format(ids["old"]) in line


# ------------------------------------------------------- 4. Acknowledge --
# FX4 (2026-11-12): the Acknowledge button went; FX5 (2026-11-13): back, blank,
# on every New case registered from a mail and not acknowledged - a shared
# thread does not hide it (ack.state's old rules are gone)
def test_acknowledge_on_the_newer_case(world):
    conn, repo, ids = world
    page = client_of(ids).get("/enquiries/{}".format(ids["new"])).data.decode()
    assert ">Acknowledge</button>" in page and not hasattr(ack, "state")


def test_a_own_office_requester_is_recognised_by_its_prefix(world):
    conn, repo, ids = world
    hkg = add_party(repo, "JoJo JJH", ptype="Office", country="China")
    case = add_enquiry(repo, "OPP-4107", hkg, received="2026-10-07", entry_id="EID-HKG", service_mode="Air",
                       source_subject="Rate for wine to HKG", created_by="outlook")
    repo.commit()
    row = case_row(conn, case)
    ours = mail("RE: [OPP-4107] Rate for wine to HKG", "2026-10-07 15:00", US[0], US[1], "Hi Wing,\n\nNoted.\n",
                "h1", internal=True, to=("wing@jojofreight.example",), to_names=("JJH-WING LAM",))
    # FX4: the button that read this is gone; the reading itself is the morning scan's
    # (case_numbers.is_ours and to_requester, the Acknowledged event)
    internal = case_numbers.internal_domains(conn)
    assert case_numbers.is_ours(ours, internal, case_numbers.case_parties(conn, row))
    assert case_numbers.to_requester(conn, row, ours)
    theirs = mail("RE: [OPP-4107] Rate for wine to HKG", "2026-10-07 15:00", US[0], US[1], "Hi,\n\nNoted.\n",
                  "h2", internal=True, to=("someone@jojofreight.example",), to_names=("JJC-DESK",))
    assert not case_numbers.to_requester(conn, row, theirs)
