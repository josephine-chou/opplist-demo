"""Suggested updates (schema v12): the scan writes pending rows,
Update from Outlook reads one mail and what followed, Accept writes through
the repository with source 'mail', Dismiss remembers the mail."""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, propose, schema_migrations, suggestions
from opplist.web import create_app


class Mail:
    def __init__(self, subject, sender, at, body, internal=False, name="", recipients=(),
                 conversation="CONV-1", entry=None):
        self.subject = subject
        self.sender_address = sender
        self.sender_name = name or sender.split("@")[0]
        self.sender_internal = internal
        self.received_on = at[:10]
        self.received_at = at
        self.body_excerpt = body
        self.to_addresses = list(recipients)
        self.cc_addresses = []
        self.recipients = list(recipients)
        self.display_to = "; ".join(recipients)
        self.conversation_id = conversation
        self.entry_id = entry or "ID-{}-{}".format(at, sender)
        self.conversation_topic = subject
        self.folder_path = ""
        self.notes = []
        self.selection_count = 1
        self.light = False


class Search:
    def __init__(self, conversations):
        self.conversations = conversations

    def conversation_of(self, mail):
        return list(self.conversations.get(mail.conversation_id, []))

    def hydrate(self, mails):
        return list(mails)


@pytest.fixture
def book(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    add_enquiry(repo, "OPP-0001", tolvex, received="2026-09-01", status="Quoted",
                thread_key="RATEREQUESTMIASAI", conversation_id="CONV-1",
                origin_loc="MIA", dest_loc="SAI", service_mode="FCL")
    add_enquiry(repo, "OPP-0002", tolvex, received="2026-09-02", status="New",
                thread_key="OTHERTHREAD", conversation_id="CONV-2")
    repo.commit()
    return repo


def case(repo, opp="OPP-0001"):
    return repo.conn.execute("SELECT * FROM v_enquiry WHERE opp_no = ?", (opp,)).fetchone()


def test_the_schema_is_v12_with_the_table_and_the_mail_sources(book):
    conn = book.conn
    assert schema_migrations.current_version(conn) >= 12
    assert "suggestion" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("status_event", "vendor_request"):
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
        assert "'mail'" in schema_migrations._check_values(sql, "source")


def test_membership_by_conversation_or_thread_key_and_the_other_cases(book):
    row = case(book)
    yes = suggestions.membership(book.conn, row, Mail("RE: RATE REQUEST // MIA - SAI", "ana@tolvex.example",
                                                      "2026-09-05 09:00", "x", conversation="CONV-9"))
    assert (yes.belongs, yes.how) == (True, "same subject")
    yes = suggestions.membership(book.conn, row, Mail("something else", "ana@tolvex.example",
                                                      "2026-09-05 09:00", "x", conversation="CONV-1"))
    assert (yes.belongs, yes.how) == (True, "same Outlook conversation")
    no = suggestions.membership(book.conn, row, Mail("RE: OTHER THREAD", "ana@tolvex.example",
                                                     "2026-09-05 09:00", "x", conversation="CONV-2"))
    assert no.belongs is False and [opp for _, opp in no.others] == ["OPP-0002"]


def test_a_win_in_the_mail_becomes_a_pending_status_suggestion(book):
    row = case(book)
    mail = Mail("RE: RATE REQUEST // MIA - SAI", "ana@tolvex.example", "2026-09-06 10:00",
                "Hi Victor,\n\nPlease proceed with the booking, rate accepted.\n\nAna",
                name="Ana", recipients=("VictorLang@jojofreight.example",))
    made = suggestions.from_mail(book, row, mail, Search({"CONV-1": [mail]}))
    assert len(made) == 1
    pending = suggestions.pending(book.conn, row["id"])
    assert len(pending) == 1
    s = pending[0]
    assert (s["kind"], s["proposed_status"], s["proposed_date"], s["source"]) == \
        ("status", "Won", "2026-09-06", "mail")
    assert "please proceed" in s["evidence_quote"].lower()
    assert s["entry_id"] == mail.entry_id and s["confidence"] == "high"
    assert suggestions.describe(s) == "Set Won on 2026-09-06"
    # the same mail again: nothing new
    assert suggestions.from_mail(book, row, mail, Search({"CONV-1": [mail]})) == []
    # accept: a status event with source mail, through the repository
    said = suggestions.accept(book, s["id"])
    assert said == "OPP-0001 set to Won on 2026-09-06"
    event = book.conn.execute("SELECT status, at, source, note FROM status_event WHERE enquiry_id = ? "
                              "ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
    assert (event["status"], event["at"], event["source"]) == ("Won", "2026-09-06", "mail")
    assert "please proceed" in event["note"].lower()
    assert case(book)["status"] == "Won"
    assert book.conn.execute("SELECT state FROM suggestion WHERE id = ?", (s["id"],)).fetchone()[0] == "accepted"
    assert book.conn.execute("SELECT count(*) FROM change_log WHERE table_name = 'suggestion'").fetchone()[0] >= 2
    with pytest.raises(ValueError):
        suggestions.accept(book, s["id"])


def test_our_mail_to_a_vendor_suggests_logging_it_and_accept_logs(book):
    row = case(book)
    mail = Mail("RATE REQUEST // MIA - SAI", "VictorLang@jojofreight.example", "2026-09-06 11:00",
                "Hi, please quote 2 x 40HQ MIA to SAI.\n", internal=True, name="JJC-VICTOR LANG",
                recipients=("rates@harborline.example",))
    made = suggestions.from_mail(book, row, mail, Search({"CONV-1": [mail]}))
    pending = suggestions.pending(book.conn, row["id"])
    assert [p["kind"] for p in pending] == ["vendor"]
    s = pending[0]
    assert (s["vendor_key"], s["vendor_domain"], s["proposed_date"]) == \
        ("Harborline", "harborline.example", "2026-09-06")
    said = suggestions.accept(book, s["id"])
    assert said == "OPP-0001: vendor request to Harborline logged on 2026-09-06"
    vr = book.conn.execute("SELECT v.asked_on, v.source, p.key FROM vendor_request v "
                           "JOIN party p ON p.id = v.vendor_party_id WHERE v.enquiry_id = ?",
                           (row["id"],)).fetchone()
    assert (vr["asked_on"], vr["source"], vr["key"]) == ("2026-09-06", "mail", "Harborline")
    assert book.conn.execute("SELECT party_id FROM party_domain WHERE domain = 'harborline.example'").fetchone()


def test_nothing_decided_is_a_note_and_dismiss_remembers_the_mail(book):
    row = case(book)
    mail = Mail("RE: RATE REQUEST // MIA - SAI", "ana@tolvex.example", "2026-09-07 08:00",
                "Hi Victor,\n\nCan you confirm the transit time?\n", name="Ana",
                recipients=("VictorLang@jojofreight.example",))
    suggestions.from_mail(book, row, mail, Search({"CONV-1": [mail]}))
    pending = suggestions.pending(book.conn, row["id"])
    assert [p["kind"] for p in pending] == ["note"]
    assert pending[0]["note"] == "Can you confirm the transit time?"
    said = suggestions.dismiss(book, pending[0]["id"])
    assert said.endswith("that mail will not be suggested again")
    assert suggestions.pending(book.conn, row["id"]) == []
    assert suggestions.from_mail(book, row, mail, Search({"CONV-1": [mail]})) == []
    assert book.conn.execute("SELECT count(*) FROM status_event WHERE enquiry_id = ?",
                             (row["id"],)).fetchone()[0] == 1              # nothing written
    # accepting a note writes a dated note
    later = Mail("RE: RATE REQUEST // MIA - SAI", "ana@tolvex.example", "2026-09-08 08:00",
                 "Any news?\n", name="Ana", recipients=("VictorLang@jojofreight.example",))
    suggestions.from_mail(book, row, later, Search({"CONV-1": [later]}))
    s = suggestions.pending(book.conn, row["id"])[0]
    assert suggestions.accept(book, s["id"]) == "OPP-0001: note added on 2026-09-08"
    event = book.conn.execute("SELECT kind, note, at FROM status_event WHERE enquiry_id = ? "
                              "ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
    assert (event["kind"], event["note"], event["at"]) == ("note", "Any news?", "2026-09-08")


def test_later_mails_in_the_conversation_are_read_earlier_ones_not(book):
    row = case(book)
    earlier = Mail("RE: RATE REQUEST // MIA - SAI", "ana@tolvex.example", "2026-09-04 08:00",
                   "Too expensive, we will not proceed.\n", name="Ana",
                   recipients=("VictorLang@jojofreight.example",))
    selected = Mail("RE: RATE REQUEST // MIA - SAI", "VictorLang@jojofreight.example", "2026-09-05 08:00",
                    "Revised rate attached.\n", internal=True, name="JJC-VICTOR LANG",
                    recipients=("ana@tolvex.example",))
    later = Mail("RE: RATE REQUEST // MIA - SAI", "ana@tolvex.example", "2026-09-06 08:00",
                 "Great, please proceed with the booking.\n", name="Ana",
                 recipients=("VictorLang@jojofreight.example",))
    suggestions.from_mail(book, row, selected, Search({"CONV-1": [earlier, selected, later]}))
    s = suggestions.pending(book.conn, row["id"])
    assert [(p["kind"], p["proposed_status"], p["proposed_date"]) for p in s] == \
        [("status", "Won", "2026-09-06")]


def test_the_scan_writes_only_changes_and_only_once(book):
    p1 = propose.Proposal(opp_no="OPP-0001", requester="Tolvex", current_status="Quoted",
                          proposed_status="Won", proposed_date="2026-09-06",
                          evidence_sender="Ana <ana@tolvex.example>", evidence_date="2026-09-06",
                          evidence_quote="please proceed", confidence="high")
    p2 = propose.Proposal(opp_no="OPP-0002", requester="Tolvex", current_status="New",
                          proposed_status="New", proposed_date="", note="waiting on customer")
    counts = suggestions.from_proposals(book, [p1, p2])
    assert counts == {"written": 1, "same": 0, "unchanged": 1}
    assert suggestions.from_proposals(book, [p1, p2]) == {"written": 0, "same": 1, "unchanged": 1}
    rows = suggestions.pending(book.conn)
    assert [(r["opp_no"], r["source"], r["proposed_status"]) for r in rows] == [("OPP-0001", "scan", "Won")]


# ------------------------------------------------------------- the pages --
@pytest.fixture
def web(tmp_path, monkeypatch):
    path = tmp_path / "web.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    add_enquiry(repo, "OPP-0001", tolvex, received="2026-09-01", status="Quoted",
                thread_key="RATEREQUESTMIASAI", conversation_id="CONV-1",
                origin_loc="MIA", dest_loc="SAI", service_mode="FCL")
    add_enquiry(repo, "OPP-0002", tolvex, received="2026-09-02", status="New",
                thread_key="OTHERTHREAD", conversation_id="CONV-2")
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    from opplist import outlook_bridge
    holder = {"mail": None, "conversations": {}}
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: holder["mail"])

    class FakeSearch:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def conversation_of(self, mail):
            return list(holder["conversations"].get(mail.conversation_id, []))

        def hydrate(self, mails):
            return list(mails)

    monkeypatch.setattr(outlook_bridge, "Search", FakeSearch)
    return app.test_client(), path, holder


# test_update_from_outlook_shows_the_suggestion_and_accept_writes_it: replaced in batch SG1 by tests/test_sg1_suggestions.py (Accept became a button per Record as; the old test was on real data).


def test_a_mail_of_another_thread_is_refused_with_the_candidates(web):
    client, path, holder = web
    holder["mail"] = Mail("RE: OTHER THREAD", "ana@tolvex.example", "2026-09-06 10:00", "x",
                          conversation="CONV-2")
    body = client.post("/enquiries/1/update-from-outlook", follow_redirects=True).data.decode("utf-8")
    assert "The selected mail is not this case" in body and "It may belong to OPP-0002" in body
    conn = db.open_db(path)
    assert conn.execute("SELECT count(*) FROM suggestion").fetchone()[0] == 0
    conn.close()


# test_the_tracking_page_lists_pending_suggestions_and_dismiss_clears_one: replaced in batch T1 by tests/test_tracking_order.py (it pinned the old card order or the Tracking suggestions card).


# test_register_from_outlook_says_when_the_thread_is_a_case: DD1 (2026-11-12) shows the thread's case as a row of
# 'Already in OppList?'; rewritten with invented data in tests/test_dd1.py.


def test_the_cli_flag_writes_suggestions(tmp_path, monkeypatch, capsys):
    from opplist import __main__ as cli
    from opplist import outlook_bridge

    path = tmp_path / "cli.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    add_enquiry(repo, "OPP-0001", tolvex, received="2026-09-01", status="Quoted")
    repo.commit()
    conn.close()

    class FakeSearch:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(outlook_bridge, "Search", FakeSearch)
    monkeypatch.setattr(propose, "propose", lambda conn, search, **kw: [propose.Proposal(
        opp_no="OPP-0001", requester="Tolvex", current_status="Quoted", proposed_status="Won",
        proposed_date="2026-09-06", evidence_sender="Ana", evidence_date="2026-09-06",
        evidence_quote="please proceed", confidence="high")])
    code = cli.main(["propose-status", "--db", str(path), "--open", "--write-suggestions",
                     "--source", "outlook", "--out", str(tmp_path / "out.csv")])
    assert code == 0
    assert "suggestions: 1 written, 0 already there, 0 keep the status" in capsys.readouterr().out
    conn = db.open_db(path)
    assert conn.execute("SELECT opp_no, proposed_status, state, source FROM suggestion").fetchall()[0][:4] == \
        ("OPP-0001", "Won", "pending", "scan")
    conn.close()


# test_the_update_button_is_not_trapped_inside_the_registration_form: rewritten with invented data on DD1's
# block in tests/test_dd1.py.
