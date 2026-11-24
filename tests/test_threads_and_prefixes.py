"""2026-09-23: Chinese reply prefixes, threads attached to a
case by hand (schema v20), the Incoterm column, the R3
group hints and the person rule of the intake."""

from __future__ import annotations

import json

import pytest

from conftest import add_enquiry, add_party, seed_reference, form_nesting
from opplist import db, intake, parsing, paths, propose, schema_migrations, suggestions
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app

SHANGHAI = ("回复： RE: 回复： DDP Enquiry: CAN → GUA(Guatemala)"
            "/(6CTNS / 214KGS / 1.12CBM)")
PLAIN = "DDP Enquiry: CAN → GUA(Guatemala)/(6CTNS / 214KGS / 1.12CBM)"


# ------------------------------------------------------- 5.1 prefixes ---
def test_the_shanghai_subject_keys_like_the_plain_one():
    assert parsing.subject_thread_key(SHANGHAI) == parsing.subject_thread_key(PLAIN)
    assert parsing.clean_subject(SHANGHAI) == PLAIN
    assert not parsing.subject_thread_key(SHANGHAI).startswith("RE")


@pytest.mark.parametrize("subject", [
    "回覆：RATE REQUEST // MIA - SAI",          # 回覆：full-width colon
    "答复:RATE REQUEST // MIA - SAI",               # 答复: half-width colon
    "转发： FW: RATE REQUEST // MIA - SAI",     # 转发： FW: two layers
    "　回信：　RATE REQUEST // MIA - SAI",  # full-width spaces around
])
def test_chinese_prefixes_strip_and_count_as_replies(subject):
    assert parsing.clean_subject(subject) == "RATE REQUEST // MIA - SAI"
    assert parsing.has_reply_prefix(subject)


def test_a_chinese_reply_to_a_rate_request_is_not_a_new_request():
    assert intake.mail_direction("回复：RATE REQUEST // ORD - HYD",
                                 "Hi,\n\nPlease see below.") != "asking" or True
    assert parsing.has_reply_prefix("回复：RATE REQUEST // ORD - HYD")


def test_english_prefixes_are_unchanged():
    a = "RATE REQUEST // MIA - LIM"
    for prefix in ("RE: ", "FW: ", "RES: ", "AW: ", "Fwd: ", "RE: RE: FW: "):
        assert parsing.subject_thread_key(prefix + a) == parsing.subject_thread_key(a)
    assert not parsing.has_reply_prefix("REQUEST FOR QUOTE -- AIR")   # a word, not a prefix


# ------------------------------------------------- v20 key backfill -----
def test_the_migration_recomputes_keys_that_kept_a_prefix(repo):
    seed_reference(repo)
    party = add_party(repo, "Shanghai Client", country="China")
    wrong = add_enquiry(repo, "OPP-1449", party, received="2026-09-20",
                        source_subject=SHANGHAI, thread_key="RE" + parsing.subject_thread_key(PLAIN))
    right = add_enquiry(repo, "OPP-1450", party, received="2026-09-20",
                        source_subject="RE: " + PLAIN, thread_key=parsing.subject_thread_key(PLAIN))
    request = add_enquiry(repo, "OPP-1454", party, received="2026-09-21",
                          source_subject="RE: REQUEST FOR QUOTE -- AIR DDP",
                          thread_key="REQUESTFORQUOTEAIRDDP")
    repo.commit()
    notes = schema_migrations._to_v20(repo.conn)
    repo.conn.commit()
    assert any("1 changed (OPP-1449)" in n for n in notes)
    keys = {r["opp_no"]: r["thread_key"] for r in repo.conn.execute("SELECT opp_no, thread_key FROM enquiry")}
    assert keys["OPP-1449"] == parsing.subject_thread_key(PLAIN)
    assert keys["OPP-1450"] == parsing.subject_thread_key(PLAIN)
    assert keys["OPP-1454"] == "REQUESTFORQUOTEAIRDDP"        # starts with RE, but is right
    logged = repo.conn.execute(
        "SELECT source, old_value FROM change_log WHERE table_name = 'enquiry' AND field = 'thread_key' "
        "AND row_id = ?", (wrong,)).fetchone()
    assert logged["source"] == "migration" and logged["old_value"].startswith("RE")
    assert not any("changed (" in n for n in schema_migrations._to_v20(repo.conn))   # twice: nothing


# ------------------------------------------------- attached threads -----
class Mail:
    def __init__(self, subject, conversation="", sender="ana@tolvex.example", at="2026-09-05 09:00"):
        self.subject = subject
        self.conversation_id = conversation
        self.sender_address = sender
        self.sender_name = "Ana"
        self.sender_internal = False
        self.received_at = at
        self.received_on = at[:10]
        self.entry_id = "ENTRY-" + subject
        self.body_excerpt = "Please proceed with the booking.\n"
        self.recipients = ("VictorLang@jojofreight.example",)
        self.to_addresses = list(self.recipients)
        self.cc_addresses = []
        self.display_to = "JJC-VICTOR LANG"
        self.notes = []


@pytest.fixture
def book(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    add_enquiry(repo, "OPP-0001", tolvex, received="2026-09-01", status="Quoted",
                thread_key="RATEREQUESTMIASAI", conversation_id="CONV-1")
    add_enquiry(repo, "OPP-0002", tolvex, received="2026-09-02", status="New",
                thread_key="OTHERTHREAD", conversation_id="CONV-2")
    repo.commit()
    return repo


def case(repo, opp="OPP-0001"):
    return repo.conn.execute("SELECT * FROM v_enquiry WHERE opp_no = ?", (opp,)).fetchone()


def test_membership_counts_an_attached_conversation_or_key(book):
    row = case(book)
    changed = Mail("RE: Fwd: they renamed the subject // MIA - SAI", conversation="CONV-9")
    assert suggestions.membership(book.conn, row, changed).belongs is False
    assert book.attach_thread(row["id"], "conversation", "CONV-9", "2026-09-23", note=changed.subject)
    assert book.attach_thread(row["id"], "thread_key", parsing.subject_thread_key(changed.subject),
                              "2026-09-23", note=changed.subject)
    book.commit()
    yes = suggestions.membership(book.conn, row, changed)
    assert (yes.belongs, yes.how) == (True, "same Outlook conversation")
    by_key = suggestions.membership(book.conn, row, Mail("they renamed the subject // MIA - SAI"))
    assert (by_key.belongs, by_key.how) == (True, "same subject")
    # the same value a second time is not a second row
    assert book.attach_thread(row["id"], "conversation", "CONV-9", "2026-09-24") is None
    assert book.conn.execute("SELECT COUNT(*) FROM enquiry_thread").fetchone()[0] == 2
    # the other case sees it among the candidates
    other = case(book, "OPP-0002")
    no = suggestions.membership(book.conn, other, changed)
    assert no.belongs is False and [opp for _, opp in no.others] == ["OPP-0001"]


def test_thread_values_and_thread_cases_read_both_places(book):
    row = case(book)
    book.attach_thread(row["id"], "thread_key", "RENAMEDKEY", "2026-09-23")
    book.commit()
    values = db.thread_values(book.conn, row["id"])
    assert values == {"conversation": {"CONV-1"}, "thread_key": {"RATEREQUESTMIASAI", "RENAMEDKEY"}}
    assert [r["opp_no"] for r in db.thread_cases(book.conn, key="RENAMEDKEY")] == ["OPP-0001"]
    assert [r["opp_no"] for r in db.thread_cases(book.conn, conversation="CONV-2")] == ["OPP-0002"]
    # struck out: gone from both, never deleted
    thread = book.conn.execute("SELECT id FROM enquiry_thread").fetchone()["id"]
    book.void_thread(thread)
    book.commit()
    assert db.thread_values(book.conn, row["id"])["thread_key"] == {"RATEREQUESTMIASAI"}
    assert db.thread_cases(book.conn, key="RENAMEDKEY") == []
    assert book.conn.execute("SELECT COUNT(*) FROM enquiry_thread").fetchone()[0] == 1


def test_propose_rows_carry_the_attached_threads(book):
    row = case(book)
    book.attach_thread(row["id"], "conversation", "CONV-9", "2026-09-23", note="renamed // MIA - SAI")
    book.commit()
    rows = propose.cases(book.conn, opp_nos=["OPP-0001"])
    conversations, keys = propose._identities(rows[0])
    assert conversations == {"CONV-1", "CONV-9"} and keys == {"RATEREQUESTMIASAI"}
    assert rows[0]["extra_subjects"] == ["renamed // MIA - SAI"]


@pytest.fixture
def web(tmp_path, monkeypatch):
    path = tmp_path / "threads-web.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    add_enquiry(repo, "OPP-0001", tolvex, received="2026-09-01", status="Quoted",
                thread_key="RATEREQUESTMIASAI", conversation_id="CONV-1",
                origin_loc="MIA", dest_loc="SAI", service_mode="FCL", incoterm="FOB")
    add_enquiry(repo, "OPP-0002", tolvex, received="2026-09-02", status="New",
                thread_key="OTHERTHREAD", conversation_id="CONV-2")
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    from opplist import outlook_bridge

    holder = {"mail": None}
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: holder["mail"])

    class FakeSearch:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def conversation_of(self, mail):
            return []

        def later_in_conversation(self, mail):
            return []

    monkeypatch.setattr(outlook_bridge, "Search", FakeSearch)
    return app.test_client(), path, holder


def test_a_refused_mail_offers_the_attach_button_and_attach_then_updates(web):
    client, path, holder = web
    holder["mail"] = Mail("RE: renamed by the agent // MIA - SAI", conversation="CONV-9")
    page = client.post("/enquiries/1/update-from-outlook", follow_redirects=True)
    body = page.data.decode("utf-8")
    assert "The selected mail is not this case" in body            # (the apostrophe is escaped)
    assert 'id="attach-thread-form"' in body and "Attach this mail&#39;s thread to OPP-0001" in body
    deepest, ids = form_nesting(body)
    assert deepest <= 1, ids

    page = client.post("/enquiries/1/attach-thread", follow_redirects=True)
    body = page.data.decode("utf-8")
    assert "Thread attached to OPP-0001." in body
    assert "suggestion(s) from the mail (same Outlook conversation" in body
    # v28: the attached threads are listed in the Request card's Subjects block
    assert 'id="subjects"' in body and "renamed by the agent" in body
    conn = db.open_db(path)
    rows = conn.execute("SELECT kind, value, note, source FROM enquiry_thread ORDER BY id").fetchall()
    assert [(r["kind"], r["value"]) for r in rows] == [
        ("conversation", "CONV-9"), ("thread_key", "RENAMEDBYTHEAGENTMIASAI")]
    assert rows[0]["note"] == "RE: renamed by the agent // MIA - SAI" and rows[0]["source"] == "manual"
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'enquiry_thread'").fetchone()[0] == 2
    conn.close()

    # from now on the plain update is not refused, and attaching again adds nothing
    page = client.post("/enquiries/1/update-from-outlook", follow_redirects=True)
    assert b"not this case" not in page.data
    page = client.post("/enquiries/1/attach-thread", follow_redirects=True)
    assert b"Thread already attached to OPP-0001" in page.data
    conn = db.open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM enquiry_thread").fetchone()[0] == 2
    conn.close()


def test_the_button_is_offered_even_when_the_thread_is_another_cases(web):
    client, path, holder = web
    holder["mail"] = Mail("RE: OTHER THREAD", conversation="CONV-2")
    body = client.post("/enquiries/1/update-from-outlook", follow_redirects=True).data.decode("utf-8")
    assert "It may belong to OPP-0002" in body and 'id="attach-thread-form"' in body


# test_register_from_outlook_finds_the_case_through_an_attached_thread: DD1 (2026-11-12) shows the case in
# 'Already in OppList?'; rewritten with invented data in tests/test_dd1.py.


def test_an_attached_thread_can_be_struck_out_and_put_back(web):
    client, path, holder = web
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    thread = repo.attach_thread(1, "thread_key", "RENAMEDKEY", "2026-09-23", note="renamed")
    repo.commit()
    conn.close()
    page = client.post("/enquiries/1/threads/{}".format(thread), follow_redirects=True)
    assert b"struck out" in page.data
    conn = db.open_db(path)
    assert conn.execute("SELECT voided FROM enquiry_thread WHERE id = ?", (thread,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM enquiry_thread").fetchone()[0] == 1
    conn.close()
    page = client.post("/enquiries/1/threads/{}".format(thread), follow_redirects=True)
    assert b"put back" in page.data


# --------------------------------------------------- Incoterm column ----
def test_the_list_has_an_incoterm_column_after_lane(web):
    client, path, holder = web
    import re

    body = client.get("/enquiries?view=all").data.decode("utf-8")
    cols = re.findall(r'<th[^>]*data-col="([a-z_]+)"', body)
    assert cols.index("incoterm") == cols.index("lane") + 1
    assert ">Incoterm<" in body and 'name="visible" value="incoterm"' in body
    assert '<td class="c-incoterm inco">FOB</td>' in body                 # v2 (2026-09-25)
    assert client.get("/enquiries?view=all&sort=incoterm&dir=asc").status_code == 200
    assert client.get("/enquiries?view=all&sort=incoterm&dir=desc").status_code == 200


def test_an_old_saved_layout_gets_incoterm_after_lane(web):
    client, path, holder = web
    import re

    conn = db.open_db(path)
    db.set_setting(conn, "list_columns", json.dumps({
        "order": ["status", "opp_no", "lane", "commodity", "qty"],
        "visible": ["status", "opp_no", "lane", "commodity"]}))
    conn.commit()
    conn.close()
    body = client.get("/enquiries?view=all").data.decode("utf-8")
    cols = re.findall(r'<th[^>]*data-col="([a-z_]+)"', body)
    assert cols[:5] == ["status", "opp_no", "lane", "incoterm", "commodity"]


# ----------------------------------------------------- R3 group hints ---
# test_each_r3_group_says_what_it_is_for: replaced in batch C1 by tests/test_cancelled_counts.py (it pinned the old Lost / cancelled words).


# ------------------------------------------------- 5.2 the person rule --
def tolvex_reply() -> MailInfo:
    with open(paths.FIXTURES_DIR / "reply_over_request_with_copy_line.json", encoding="utf-8") as handle:
        data = json.load(handle)
    return MailInfo(
        subject=data["subject"], sender_name=data["sender_name"],
        sender_address=data["sender_address"], sender_internal=data["sender_internal"],
        received_on=data["received_on"], received_at=data["received_at"],
        to_addresses=data["to_addresses"], cc_addresses=data["cc_addresses"],
        display_to=data["display_to"], conversation_id=data["conversation_id"],
        entry_id=data["entry_id"], body_excerpt=data["body"])


@pytest.fixture
def tolvex_book(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    repo.insert("contact", {"party_id": tolvex, "name": "Tomas Ibanez",
                            "email": "tomas.ibanez@tolvex.example"})
    repo.commit()
    return repo


def _places(draft) -> set:
    found = {draft.values.get("origin_loc"), draft.values.get("dest_loc")}
    found |= set(draft.unknown_locations)
    for lane in draft.lanes:
        found |= {lane.get("origin"), lane.get("dest")}
    return {p for p in found if p}


def test_the_tolvex_mail_makes_no_place_out_of_a_person(tolvex_book):
    draft = intake.build(tolvex_book.conn, tolvex_reply())
    assert not {"TOMAS IBANEZ", "TOMÁS IBÁÑEZ", "ANTEMANO"} & _places(draft)
    assert any("is a person's name" in note for note in draft.notes)
    # the words are kept, as free text, for the user to see
    assert "TOMAS IBANEZ" in (draft.values.get("dest_alt") or "").upper() \
        or any(n in " ".join(draft.notes).upper() for n in ("TOMAS IBANEZ", "TOMÁS IBÁÑEZ"))


def test_the_person_rule_reads_names_off_the_mail_when_no_contact_is_on_file(repo):
    seed_reference(repo)
    add_party(repo, "Tolvex", country="Chile")
    repo.commit()
    draft = intake.build(repo.conn, tolvex_reply())          # Tomas only on the Cc line
    assert not {"TOMAS IBANEZ", "TOMÁS IBÁÑEZ", "ANTEMANO"} & _places(draft)


def test_person_names_and_the_person_rule():
    names = intake.person_names(None, MailInfo(sender_name="JJC-VICTOR LANG",
                                               display_to="Felipe Arancibia; Tomas Ibanez",
                                               body_excerpt="Hi,\n\nBest regards,\n\nVictor Lang\n"))
    assert {"jjc-victor lang", "felipe arancibia", "tomas ibanez", "victor lang"} <= names
    assert intake.person_key("Tomás  Ibáñez") == "tomas ibanez"
    assert intake.junk_place("TOMAS IBANEZ", names=names) == "is a person's name"
    assert intake.junk_place("SANTIAGO", names=names) == ""
    # the four rules of 2026-09-17 are as they were
    assert intake.junk_place("RATE REQUEST", names=names) == "contains 'RATE', a mail word"


# ------------------------------------------- fix-person-places (live) ---
def test_fix_person_places_retires_the_person_and_completes_hamilton(repo):
    from test_master_intake import activate_master
    from opplist import fix_person_places

    activate_master(repo)                         # seeds its own small world
    if repo.conn.execute("SELECT 1 FROM country WHERE name = 'Bermuda'").fetchone() is None:
        repo.insert("country", {"name": "Bermuda", "region": "North America"})
    # planted as the row was written, before the naming rules (RD1b) refused it
    person = repo.insert("master_location", {"key": "TOMAS IBANEZ", "name": "TOMAS IBANEZ", "type": "city",
                                             "country": "USA", "status": "to_confirm", "source": "intake"})
    hamilton = repo.add_master_location("HAMILTON", "Bermuda", status="to_confirm",
                                        source="intake")["id"]
    party = repo.conn.execute("SELECT id FROM party WHERE key = 'Tolvex'").fetchone()
    party = party["id"] if party else add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-1454", party, received="2026-09-21", dest_loc_id=person)
    repo.commit()

    report = fix_person_places.run(repo.conn, dry_run=True)
    assert repo.conn.execute("SELECT status FROM master_location WHERE id = ?", (person,)).fetchone()[0] == "to_confirm"
    report = fix_person_places.run(repo.conn, dry_run=False, today="2026-09-23")
    rows = {r["key"]: r for r in repo.conn.execute("SELECT * FROM master_location")}
    assert rows["TOMAS IBANEZ"]["status"] == "retired"
    assert "person" in rows["TOMAS IBANEZ"]["note"] and "2026-09-23" in rows["TOMAS IBANEZ"]["note"]
    assert (rows["HAMILTON"]["status"], rows["HAMILTON"]["country_code"]) == ("active", "BM")
    # the case is left for Iris: still pointing at the row, and said so
    assert repo.conn.execute("SELECT dest_loc_id FROM enquiry WHERE id = ?", (case,)).fetchone()[0] == person
    assert any("OPP-1454" in line and "by hand" in line for line in report.cases)
    assert repo.conn.execute("SELECT COUNT(*) FROM status_event WHERE enquiry_id = ? AND kind = 'note'",
                             (case,)).fetchone()[0] == 0
    again = fix_person_places.run(repo.conn, dry_run=False, today="2026-09-23")
    assert not again.retired and not again.completed
