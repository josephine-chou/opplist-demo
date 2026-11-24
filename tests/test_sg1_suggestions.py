"""Batch SG1 (2026-11-04, no schema change): fewer suggestions, decided in
one click; one mail shows once.

1. a pending suggestion nobody decided in suggestion_expire_days (14) is
   dismissed by the morning run, marked expired; Put back works;
2. a status or note suggestion's card offers its Record as choices as
   buttons, each writing what Edit with that choice writes (Won what
   Accept writes); Lost... needs a reason;
3. the scan's status suggestion names its mail; detect_followups holds a
   mail's automatic note back while a status suggestion on it is pending,
   writes it after Dismiss, never after Accept; accepting a suggestion
   whose mail is already a timeline note strikes that note out.

Every company, person, mail, place and date here is invented; cases from
OPP-8901.
"""

from __future__ import annotations

import json

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, detect_followups, mail_archive, parsing, propose, suggestions
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app

TODAY = "2026-11-20"
SUBJECT = "RATE REQUEST // TARVEN - OSLAN // 3 PLT"
AGENT = ("pia@brackwatercargo.test", "Pia Lendell")
US = ("rui@jojofreight.example", "JJC-RUI MAREN")


def suggest(repo, case, opp, kind="status", status="Quoted", on="2026-11-12", quote="Thanks for the offer.",
            reason=None, note=None, created=None, **extra):
    values = {"enquiry_id": case, "opp_no": opp, "kind": kind, "proposed_status": status, "proposed_date": on,
              "evidence_quote": quote, "evidence_sender": "Pia Lendell <pia@brackwatercargo.test>",
              "evidence_date": on, "confidence": "high", "source": "scan", "state": "pending",
              "closed_reason": reason, "note": note, "created_at": created}
    values.update(extra)
    return repo.insert("suggestion", {k: v for k, v in values.items() if v is not None})


@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    path = tmp_path / "sg1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    agent = add_party(repo, "Brackwater Cargo", country="Chile")
    repo.insert("party_domain", {"domain": "brackwatercargo.test", "party_id": agent})
    ids = {"path": path, "archive": archive}
    for opp, conv in (("OPP-8901", "CONV-8901"), ("OPP-8902", "CONV-8902")):
        ids[opp] = add_enquiry(repo, opp, agent, received="2026-11-02", source_subject=SUBJECT + " " + opp,
                               thread_key=parsing.subject_thread_key(SUBJECT + " " + opp), conversation_id=conv,
                               service_mode="LCL", origin_country="USA", dest_country="Chile")
        repo.set_status(ids[opp], "Quoted", "2026-11-03")
    ids["OPP-8903"] = add_enquiry(repo, "OPP-8903", agent, received="2026-11-09", service_mode="Air",
                                  origin_country="USA", dest_country="Chile")
    repo.commit()
    app = create_app(path)
    app.config["TESTING"] = True
    yield conn, repo, ids, app.test_client()
    conn.close()


def get(conn, sid):
    return conn.execute("SELECT * FROM suggestion WHERE id = ?", (sid,)).fetchone()


def last_event(conn, case):
    return tuple(conn.execute("SELECT kind, status, at, note, reason, source FROM status_event "
                              "WHERE enquiry_id = ? AND voided = 0 ORDER BY id DESC LIMIT 1", (case,)).fetchone())


def page(client, case):
    return client.get("/enquiries/{}".format(case)).data.decode("utf-8")


# ------------------------------------------------------------ 1. expiry --
def test_a_suggestion_expires_at_fourteen_days_and_put_back_brings_it_back(world):
    conn, repo, ids, client = world
    old = suggest(repo, ids["OPP-8901"], "OPP-8901", created="2026-10-21 08:20:00")
    young = suggest(repo, ids["OPP-8901"], "OPP-8901", status="Won", created="2026-10-22 08:20:00")
    repo.commit()
    gone = suggestions.expire(db.Repository(conn, source="scan"), today="2026-11-04")
    assert [r["id"] for r in gone] == [old]
    row = get(conn, old)
    assert (row["state"], row["decided_as"]) == ("dismissed", "dismissed")
    assert json.loads(row["decided_values"]) == {"days": 14, "expired": True}
    assert suggestions.is_expired(row) and get(conn, young)["state"] == "pending"
    logged = conn.execute("SELECT new_value, source FROM change_log WHERE table_name = 'suggestion' AND row_id = ? "
                          "AND field = 'state' ORDER BY id DESC LIMIT 1", (old,)).fetchone()
    assert tuple(logged) == ("dismissed", "scan")
    body = page(client, ids["OPP-8901"])
    gone_list = body[body.index('<details class="dismissed">'):]
    assert '<span class="pill tone-grey d-expired">expired</span>' in gone_list
    client.post("/suggestions/{}/put-back".format(old), data={"next": "/enquiries/{}".format(ids["OPP-8901"])})
    row = get(conn, old)
    assert (row["state"], row["decided_as"], row["decided_values"]) == ("pending", None, None)
    assert not suggestions.is_expired(row)


def test_the_days_are_a_setting_shown_in_settings(world):
    conn, repo, ids, client = world
    sid = suggest(repo, ids["OPP-8901"], "OPP-8901", created="2026-10-21 08:20:00")
    repo.commit()
    assert suggestions.expire_days(conn) == 14
    body = client.get("/settings").data.decode("utf-8")
    assert "Suggestions nobody decided expire after (days)" in body
    assert '<input name="suggestion_expire_days" value="14" inputmode="numeric">' in body
    db.set_setting(conn, "suggestion_expire_days", 30)
    conn.commit()
    assert suggestions.expire(db.Repository(conn, source="scan"), today="2026-11-04") == []
    assert get(conn, sid)["state"] == "pending"


# ------------------------------------------------------- 2. one click --
def test_the_card_offers_its_choices_as_buttons_with_the_suggested_one_marked(world):
    conn, repo, ids, client = world
    sid = suggest(repo, ids["OPP-8901"], "OPP-8901", status="Lost", reason="Price too high",
                  quote="We went with another offer.")
    repo.commit()
    body = page(client, ids["OPP-8901"])
    row = body[body.index('id="sg-{}"'.format(sid)):body.index('id="sg-edit-{}"'.format(sid))]
    acts = row[row.index('class="row-actions sg-record"'):row.index("</form>", row.index("sg-record"))]
    order = [acts.index(x) for x in ('value="requote">Re-quoted · round 2<', 'value="won">Won<',
                                     ">Lost...<", ">Cancelled...<", 'value="note">Note<')]
    assert order == sorted(order)
    assert 'class="button primary" type="button" data-toggle="sg-{}-lost" title="suggested"'.format(sid) in acts
    assert '<span class="sg-reason" id="sg-{}-lost" hidden>'.format(sid) in acts
    assert '<option value="Price too high" selected>Price too high</option>' in acts
    assert ">Accept</button>" not in row and ">Dismiss</button>" in row
    assert '<button type="button" class="link tiny" data-sg-edit="{}">Edit</button>'.format(sid) in row


def _twins(repo, ids, **kw):
    a = suggest(repo, ids["OPP-8901"], "OPP-8901", **kw)
    b = suggest(repo, ids["OPP-8902"], "OPP-8902", **kw)
    repo.commit()
    return a, b


@pytest.mark.parametrize("action,reason", [("requote", None), ("note", None), ("lost", "Price too high"),
                                           ("cancelled", "Order not confirmed")])
def test_each_button_writes_what_the_same_edit_writes(world, action, reason):
    conn, repo, ids, client = world
    a, b = _twins(repo, ids, status="Won", on="2026-11-14", quote="Go ahead and book it, please.")
    p = suggestions.plan(conn, get(conn, a), TODAY)
    form = {"next": "/", "record_as": action}
    if reason:
        form["reason_" + action] = reason
    client.post("/suggestions/{}/record".format(a), data=form)
    edit = {"next": "/", "record_as": action, "at": p.at, "note": p.note,
            "closed_reason": reason or "", "requote_reason": ""}
    client.post("/suggestions/{}/edit".format(b), data=edit)
    assert last_event(conn, ids["OPP-8901"]) == last_event(conn, ids["OPP-8902"])
    assert last_event(conn, ids["OPP-8901"])[2] == "2026-11-14"
    ra, rb = get(conn, a), get(conn, b)
    assert ra["state"] == "accepted" and ra["decided_as"] == action
    assert (ra["decided_as"], ra["decided_values"], ra["edited"]) == (rb["decided_as"], rb["decided_values"],
                                                                     rb["edited"])


def test_won_writes_what_accept_writes(world):
    conn, repo, ids, client = world
    a, b = _twins(repo, ids, status="Won", on="2026-11-14", quote="Go ahead and book it, please.")
    client.post("/suggestions/{}/record".format(a), data={"next": "/", "record_as": "won"})
    client.post("/suggestions/{}/accept".format(b), data={"next": "/"})
    assert last_event(conn, ids["OPP-8901"]) == last_event(conn, ids["OPP-8902"])
    assert last_event(conn, ids["OPP-8901"])[:3] == ("status", "Won", "2026-11-14")
    ra, rb = get(conn, a), get(conn, b)
    assert (ra["decided_as"], ra["decided_values"], ra["edited"]) == ("won", rb["decided_values"], 0)
    case = conn.execute("SELECT rate_used FROM enquiry WHERE id = ?", (ids["OPP-8901"],)).fetchone()[0]
    assert case == conn.execute("SELECT rate_used FROM enquiry WHERE id = ?", (ids["OPP-8902"],)).fetchone()[0]


def test_quoted_on_a_new_case_is_its_own_button(world):
    conn, repo, ids, client = world
    sid = suggest(repo, ids["OPP-8903"], "OPP-8903", on="2026-11-10", quote="Please find our offer below.")
    repo.commit()
    client.post("/suggestions/{}/record".format(sid), data={"next": "/", "record_as": "quoted"})
    assert last_event(conn, ids["OPP-8903"])[:3] == ("status", "Quoted", "2026-11-10")
    assert (get(conn, sid)["decided_as"], get(conn, sid)["edited"]) == ("quoted", 0)


def test_lost_needs_a_reason(world):
    conn, repo, ids, client = world
    sid = suggest(repo, ids["OPP-8901"], "OPP-8901", status="Lost", on="2026-11-14", quote="Not this time.")
    repo.commit()
    before = last_event(conn, ids["OPP-8901"])
    body = client.post("/suggestions/{}/record".format(sid), data={
        "next": "/enquiries/{}".format(ids["OPP-8901"]), "record_as": "lost", "reason_lost": ""},
        follow_redirects=True).data.decode("utf-8")
    assert "Lost needs a reason." in body
    assert get(conn, sid)["state"] == "pending" and last_event(conn, ids["OPP-8901"]) == before
    body = client.post("/suggestions/{}/record".format(sid), data={"next": "/", "record_as": "quoted"},
                       follow_redirects=True).data.decode("utf-8")
    assert "Record as: not one this case offers." in body and get(conn, sid)["state"] == "pending"


# ------------------------------------------------------ 3. one mail once --
def mail(at, sender, body, mid, internal=False, to=(AGENT,)):
    return MailInfo(subject="RE: " + SUBJECT + " OPP-8901", received_on=at[:10], received_at=at, sent_at=at,
                    entry_id="EID-" + mid, conversation_id="CONV-8901", conversation_topic=SUBJECT,
                    sender_name=sender[1], sender_address=sender[0], sender_internal=internal,
                    to_addresses=[a for a, _ in to], body_excerpt=body, folder_path="\\Mailbox\\Inbox",
                    display_to="; ".join(n for _, n in to), message_id="<{}@sg1.test>".format(mid))


QUOTE = mail("2026-11-03 10:00", US, "Hi Pia,\nOur rate is USD 520 all-in.\nRui", "q1", internal=True)
REPLY = mail("2026-11-06 09:00", AGENT, "Hi Rui,\nCan you do better on the rate?\nPia", "r1", to=(US,))
ANSWER = mail("2026-11-07 11:00", US, "Hi Pia,\nRevised: USD 480 all-in.\nRui", "a1", internal=True)


def store(ids, *mails):
    conn = mail_archive.open_archive(ids["archive"])
    mail_archive.store(conn, [mail_archive.record_from_info(m) for m in mails])
    conn.commit()
    conn.close()


def scan(conn, ids):
    with mail_archive.ArchiveSearch(ids["archive"]) as search:
        return detect_followups.run(conn, search, opp_nos=("OPP-8901",))


def notes(conn, case, prefix):
    return [tuple(r) for r in conn.execute(
        "SELECT at, note FROM status_event WHERE enquiry_id = ? AND voided = 0 AND note LIKE ? ORDER BY id",
        (case, prefix + "%"))]


def test_the_scans_status_suggestion_names_its_mail(world):
    conn, repo, ids, client = world
    thread = [propose.to_thread_mail(m, {"jojofreight.example"}, {"brackwatercargo.test"}, "", "")
              for m in (QUOTE, mail("2026-11-08 09:00", AGENT, "Hi Rui,\nPlease proceed with the booking.\nPia",
                                     "w1", to=(US,)))]
    p = propose.classify(thread, "Brackwater Cargo", requester_known=True)
    assert (p.proposed_status, p.message_id, p.entry_id) == ("Won", "<w1@sg1.test>", "EID-w1")
    p.opp_no, p.current_status = "OPP-8901", "Quoted"
    assert suggestions.from_proposals(db.Repository(conn, source="scan"), [p])["written"] == 1
    row = conn.execute("SELECT * FROM suggestion WHERE opp_no = 'OPP-8901'").fetchone()
    assert (row["message_id"], row["entry_id"], row["source"]) == ("<w1@sg1.test>", "EID-w1", "scan")


def test_a_reply_note_is_held_while_its_status_suggestion_is_pending_and_written_after_dismiss(world):
    conn, repo, ids, client = world
    store(ids, QUOTE, REPLY)
    sid = suggest(repo, ids["OPP-8901"], "OPP-8901", status="Lost", on="2026-11-06", reason="Price too high",
                  message_id="<r1@sg1.test>", entry_id="EID-r1")
    repo.commit()
    report = scan(conn, ids)
    assert (report.replies, report.held) == (0, 1)
    assert notes(conn, ids["OPP-8901"], "Reply from") == []
    assert notes(conn, ids["OPP-8901"], "Reply from") == [] and scan(conn, ids).held == 1      # still held
    suggestions.dismiss(repo, sid)
    report = scan(conn, ids)
    assert report.held == 0
    assert [at for at, _ in notes(conn, ids["OPP-8901"], "Reply from")] == ["2026-11-06"]


def test_a_reply_note_is_not_written_after_accept(world):
    conn, repo, ids, client = world
    store(ids, QUOTE, REPLY)
    sid = suggest(repo, ids["OPP-8901"], "OPP-8901", status="Lost", on="2026-11-06", reason="Price too high",
                  quote="Can you do better on the rate?", message_id="<r1@sg1.test>", entry_id="EID-r1")
    repo.commit()
    assert scan(conn, ids).held == 1
    # recorded as a note: the case stays Quoted and is scanned again
    client.post("/suggestions/{}/record".format(sid), data={"next": "/", "record_as": "note"})
    assert get(conn, sid)["state"] == "accepted"
    report = scan(conn, ids)
    assert (report.held, notes(conn, ids["OPP-8901"], "Reply from")) == (0, [])


def test_our_answer_is_never_an_automatic_note_with_or_without_a_suggestion(world):
    # FX4: the AL6a answer step is gone - there is nothing to hold back
    conn, repo, ids, client = world
    store(ids, QUOTE, REPLY)
    assert scan(conn, ids).replies == 1                 # the morning after their question
    store(ids, ANSWER)
    sid = suggest(repo, ids["OPP-8901"], "OPP-8901", on="2026-11-07", quote="Revised: USD 480 all-in.",
                  note=detect_followups.REQUOTED_NOTE, message_id="<a1@sg1.test>", entry_id="EID-a1")
    repo.commit()
    report = scan(conn, ids)
    assert not hasattr(report, "answers") and report.held == 0
    assert notes(conn, ids["OPP-8901"], "Answered by") == []
    suggestions.dismiss(repo, sid)
    scan(conn, ids)
    assert notes(conn, ids["OPP-8901"], "Answered by") == []


def test_accepting_a_suggestion_strikes_out_the_older_note_of_its_mail(world):
    # FX4: on the scan's reply note (the Answered by note it used is no longer written)
    conn, repo, ids, client = world
    store(ids, QUOTE, REPLY)
    scan(conn, ids)                                     # the reply is on the timeline first
    replied = notes(conn, ids["OPP-8901"], "Reply from")
    assert [at for at, _ in replied] == ["2026-11-06"] and "[mid:<r1@sg1.test>]" in replied[0][1]
    sid = suggest(repo, ids["OPP-8901"], "OPP-8901", on="2026-11-06", quote="Can you do better on the rate?",
                  note=detect_followups.REQUOTED_NOTE, message_id="<r1@sg1.test>", entry_id="EID-r1")
    repo.commit()
    body = page(client, ids["OPP-8901"])
    row = body[body.index('id="sg-{}"'.format(sid)):body.index('id="sg-edit-{}"'.format(sid))]
    assert "Same mail as the timeline note of Nov 6" in row
    said = client.post("/suggestions/{}/record".format(sid), data={
        "next": "/enquiries/{}".format(ids["OPP-8901"]), "record_as": "requote"},
        follow_redirects=True).data.decode("utf-8")
    assert "the timeline note of 2026-11-06 on the same mail was struck out" in said
    assert notes(conn, ids["OPP-8901"], "Reply from") == []
    assert last_event(conn, ids["OPP-8901"])[:3] == ("requote", "Quoted", "2026-11-06")
    note_id = conn.execute("SELECT id FROM status_event WHERE enquiry_id = ? AND note LIKE 'Reply from%'",
                           (ids["OPP-8901"],)).fetchone()[0]
    assert conn.execute("SELECT voided FROM status_event WHERE id = ?", (note_id,)).fetchone()[0] == 1
    # reversible: Put back on the timeline
    repo.void_status_event(note_id, voided=False)
    repo.commit()
    assert len(notes(conn, ids["OPP-8901"], "Reply from")) == 1
    assert get(conn, sid)["state"] == "accepted"
    # and the next scan writes nothing new for that mail
    assert scan(conn, ids).replies == 0


def test_a_card_without_its_mail_on_the_timeline_says_nothing(world):
    conn, repo, ids, client = world
    sid = suggest(repo, ids["OPP-8901"], "OPP-8901", status="Won", message_id="<x9@sg1.test>")
    repo.commit()
    assert "Same mail as the timeline note" not in page(client, ids["OPP-8901"])
    assert suggestions.same_mail_note(conn, get(conn, sid)) is None


def test_update_from_outlook_shows_the_buttons_and_won_writes_it(world, monkeypatch):
    """Written for SG1 in place of test_suggestions' Tolvex one."""
    conn, repo, ids, client = world
    from opplist import outlook_bridge
    yes = mail("2026-11-08 09:00", AGENT, "Hi Rui,\nPlease proceed with the booking.\nPia", "w1", to=(US,))
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: yes)

    class FakeSearch:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def conversation_of(self, m):
            return []

        def hydrate(self, mails):
            return list(mails)

    monkeypatch.setattr(outlook_bridge, "Search", FakeSearch)
    body = client.post("/enquiries/{}/update-from-outlook".format(ids["OPP-8901"]),
                       follow_redirects=True).data.decode("utf-8")
    assert "1 suggestion(s) from the mail" in body and "Suggested updates" in body
    sid = conn.execute("SELECT id FROM suggestion WHERE opp_no = 'OPP-8901'").fetchone()[0]
    assert 'action="/suggestions/{}/record"'.format(sid) in body
    assert 'class="button primary" type="submit" name="record_as" value="won" title="suggested">Won<' in body
    body = client.post("/suggestions/{}/record".format(sid), data={
        "next": "/enquiries/{}".format(ids["OPP-8901"]), "record_as": "won"}, follow_redirects=True).data.decode("utf-8")
    assert "OPP-8901 set to Won on 2026-11-08" in body and "Suggested updates" not in body
    assert last_event(conn, ids["OPP-8901"])[:3] + last_event(conn, ids["OPP-8901"])[5:] == \
        ("status", "Won", "2026-11-08", "mail")
