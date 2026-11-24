"""Batch FX4 (2026-11-12): Waiting on us by hand only, the MFN label,
prospects in the Requester picker, four Tracking cards hidden, the mail
templates taken out, the Make Outlook drafts folder for the team site, and
the Agent platform's rate-request counts.

Every company, person, mail, place, number and date here is invented;
cases from OPP-9981, domains .test.
"""

from __future__ import annotations

import json

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import (agent_rate_requests, assign_list, db, detect_followups, firebase_pull, firebase_sync as fs,
                     mail_archive, parsing, tracking, web)
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app
from test_firebase_sync import FakeFirestore

TODAY = "2026-12-14"                     # a Monday
SUBJECT = "RATE REQUEST // TALLOW BAY - QUILLEN // 3 PLT"
AGENT = ("ines@marrowfreight.test", "Ines Varrow")
US = ("pell@jojofreight.example", "JJC-PELL ODRAN")
QUESTION = "Could you also quote the delivery to the Quillen warehouse?"


def mail(at, sender, body, mid, internal=False, to=(AGENT,)):
    return MailInfo(subject="RE: " + SUBJECT, received_on=at[:10], received_at=at, sent_at=at, entry_id="EID-" + mid,
                    conversation_id="CONV-9981", conversation_topic=SUBJECT, sender_name=sender[1],
                    sender_address=sender[0], sender_internal=internal, to_addresses=[a for a, _ in to],
                    body_excerpt=body, folder_path="\\Mailbox\\Inbox", display_to="; ".join(n for _, n in to),
                    message_id="<{}@x>".format(mid))


QUOTE = mail("2026-12-08 10:00", US, "Hi Ines,\nOur rate is USD 610 all-in.\nPell", "q1", internal=True)


@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    monkeypatch.setattr(web, "_today", lambda: TODAY)
    path = tmp_path / "fx4.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    db.set_setting(conn, "owners", ["Ana", "Ben"])
    agent = add_party(repo, "Marrow Freight", country="Chile", mfn_no="777001")
    repo.insert("party_domain", {"domain": "marrowfreight.test", "party_id": agent})
    case = add_enquiry(repo, "OPP-9981", agent, received="2026-12-07", source_subject=SUBJECT,
                       thread_key=parsing.subject_thread_key(SUBJECT), conversation_id="CONV-9981",
                       service_mode="LCL", origin_country="USA", dest_country="Chile", scope="In")
    repo.set_status(case, "Quoted", "2026-12-08")
    repo.commit()
    yield {"conn": conn, "repo": repo, "case": case, "agent": agent, "archive": archive, "path": path,
           "tmp": tmp_path}
    conn.close()


def store(w, *mails):
    conn = mail_archive.open_archive(w["archive"])
    mail_archive.store(conn, [mail_archive.record_from_info(m) for m in mails])
    conn.commit()
    conn.close()


def scan(w):
    with mail_archive.ArchiveSearch(w["archive"]) as search:
        return detect_followups.run(w["conn"], search)


def notes(w, case=None):
    return [(r[0], r[1]) for r in w["conn"].execute(
        "SELECT note, source FROM status_event WHERE enquiry_id = ? AND kind = 'note' AND voided = 0 ORDER BY at, id",
        (case or w["case"],))]


def pending(w):
    return [r.opp_no for r in assign_list.pending_listed(w["conn"], TODAY)]


def client(w):
    app = create_app(w["path"])
    app.config["TESTING"] = True
    return app.test_client()


def page(c, url):
    return c.get(url).data.decode("utf-8")


# ------------------------------------------- 1. Waiting on us by hand only --
def test_a_scan_reply_that_asks_is_written_as_noted_and_puts_nothing_on_pending_on_us(world):
    w = world
    store(w, QUOTE, mail("2026-12-09 09:00", AGENT, "Hi Pell,\n" + QUESTION + "\nInes", "r1", to=(US,)))
    report = scan(w)
    assert report.replies == 1
    (note, source), = [n for n in notes(w) if n[0].startswith("Reply from")]
    assert note.startswith("Reply from Ines Varrow (noted): ") and source == "mail"
    assert pending(w) == []
    steps = [i.extra["step"] for i in tracking.rule_quoted_no_answer(w["conn"], TODAY, tracking.fu_settings(w["conn"]))]
    assert "Replied" not in steps


def test_an_old_scan_note_marked_as_asking_no_longer_counts(world):
    w = world
    w["repo"].add_note(w["case"], "2026-12-09", detect_followups.reply_note("Ines", QUESTION, kind="asks"),
                       source="mail")
    w["repo"].commit()
    assert detect_followups.note_kind(notes(w)[-1][0]) == "asks"           # the mark is still there
    assert pending(w) == [] and tracking.replied_note(w["conn"], w["case"]) is None


def test_the_al6a_answer_step_is_gone(world):
    w = world
    firebase_pull._waiting(w["repo"], w["conn"].execute("SELECT * FROM v_enquiry WHERE id = ?", (w["case"],)).fetchone(),
                           {"date": "2026-12-09", "what": QUESTION}, "", TODAY, source="manual")
    w["repo"].commit()
    # our mail to the requester after their question is not read as its answer any more
    store(w, QUOTE, mail("2026-12-10 11:00", US, "Hi Ines,\nYes, delivery adds USD 90.\nPell", "a1", internal=True))
    scan(w)
    assert not any(n.startswith("Answered by") for n, _ in notes(w))
    assert pending(w) == ["OPP-9981"]
    assert not hasattr(detect_followups, "_asked_since")


def test_waiting_on_us_and_answered_from_the_case_page(world):
    w = world
    c = client(w)
    got = page(c, "/enquiries/{}".format(w["case"]))
    assert 'id="waiting-form"' in got and 'id="answered-form"' not in got
    said = c.post("/enquiries/{}/waiting-on-us".format(w["case"]),
                  data={"at": TODAY, "what": QUESTION, "who": "Ben"}, follow_redirects=True).data.decode()
    assert "OPP-9981 is on Pending on us" in said and "Handled by is now Ben" in said
    note, source = notes(w)[-1]
    assert note == "Reply from Marrow Freight (needs an answer): " + QUESTION and source == "manual"
    assert pending(w) == ["OPP-9981"]                                   # at once, the same day
    assert "Waiting on us" in page(c, "/tracking") and 'id="replied"' in page(c, "/tracking")
    got = page(c, "/enquiries/{}".format(w["case"]))
    assert 'id="answered-form"' in got and 'id="waiting-form"' not in got
    said = c.post("/enquiries/{}/answered".format(w["case"]), data={"at": TODAY, "note": "sent the delivery rate"},
                  follow_redirects=True).data.decode()
    assert "left Pending on us: answered by Ben" in said
    assert notes(w)[-1] == ("Answered by Ben: sent the delivery rate", "manual")
    assert pending(w) == []


def test_waiting_on_us_refuses_an_empty_question_and_a_case_not_quoted(world):
    w = world
    c = client(w)
    said = c.post("/enquiries/{}/waiting-on-us".format(w["case"]), data={"what": "  "},
                  follow_redirects=True).data.decode()
    assert "What they asked needs 1 to 1,000 characters" in said
    other = add_enquiry(w["repo"], "OPP-9982", w["agent"], received="2026-12-10", scope="In")
    w["repo"].commit()
    said = c.post("/enquiries/{}/waiting-on-us".format(other), data={"what": QUESTION},
                  follow_redirects=True).data.decode()
    assert "OPP-9982 is New, not Quoted" in said and pending(w) == []


def test_the_same_from_the_site_is_on_tracking_at_once(world):
    w = world
    row = w["conn"].execute("SELECT * FROM v_enquiry WHERE id = ?", (w["case"],)).fetchone()
    firebase_pull._waiting(w["repo"], row, {"date": TODAY, "what": QUESTION}, "", TODAY)
    w["repo"].commit()
    assert notes(w)[-1][1] == "web" and pending(w) == ["OPP-9981"]
    firebase_pull._answered(w["repo"], row, {"date": TODAY, "note": "done"}, "Ana", TODAY)
    w["repo"].commit()
    assert pending(w) == []


def test_tracking_names_the_card_waiting_on_us():
    card = next(d for d in tracking.TODAY_CARDS if d[0] == "replied")
    assert card[3] == "Waiting on us" and "Answered clears it" in card[4]
    assert next(t for t in tracking.TILES if t[0] == "replied")[1] == "Waiting on us"


# ------------------------------------------------------------ 2. MFN label --
def test_the_mfn_label_follows_a_live_agent_with_a_mfn_number(world):
    w = world
    repo = w["repo"]
    customer = add_party(repo, "Pellow Tiles", ptype="Direct Customer", country="Chile", mfn_no="777002")
    office = add_party(repo, "JoJo QZT", ptype="Office", country="Unknown", mfn_no="777003")
    plain = add_party(repo, "Ostrel Cargo", country="Chile")
    for n, party in ((9983, customer), (9984, office), (9985, plain)):
        add_enquiry(repo, "OPP-{}".format(n), party, received="2026-12-10", scope="In")
    repo.commit()
    assert db.mfn_party_ids(w["conn"]) == {w["agent"]}
    c = client(w)
    listed = page(c, "/enquiries?view=all")
    assert listed.count('<span class="lbl fill mfn-chip">MFN</span>') == 1
    assert 'id="mfn-chip"' in page(c, "/enquiries/{}".format(w["case"]))
    other = w["conn"].execute("SELECT id FROM enquiry WHERE opp_no = 'OPP-9983'").fetchone()[0]
    assert 'id="mfn-chip"' not in page(c, "/enquiries/{}".format(other))
    built = fs.build(w["conn"], TODAY)
    rows = {r["opp"]: r for r in built.inquiries}
    assert rows["OPP-9981"]["mfn"] is True and built.cases["OPP-9981"]["mfn"] is True
    assert all("mfn" not in rows[o] for o in ("OPP-9983", "OPP-9984", "OPP-9985"))
    # merged away: no longer live, no label
    repo.update("party", w["agent"], {"merged_into": plain})
    repo.commit()
    assert db.mfn_party_ids(w["conn"]) == set()


# ------------------------------------------- 3. prospects in the picker --
def test_a_prospect_is_listed_and_becomes_active_when_picked_as_requester(world):
    w = world
    prospect = add_party(w["repo"], "Quorra Logistics", country="Chile", stage="prospect")
    w["repo"].commit()
    c = client(w)
    form = page(c, "/enquiries/new")
    assert "Quorra Logistics (prospect)" in form
    case_page = page(c, "/enquiries/{}".format(w["case"]))
    assert "Quorra Logistics (prospect)" in case_page
    # the prospects come after the active parties
    assert case_page.index("Quorra Logistics (prospect)") > case_page.index(">Marrow Freight<")
    w["repo"].save_enquiry(w["case"], {"requester_party_id": prospect})
    w["repo"].commit()
    assert w["conn"].execute("SELECT stage FROM party WHERE id = ?", (prospect,)).fetchone()[0] == "active"
    logged = w["conn"].execute("SELECT old_value, new_value FROM change_log WHERE table_name = 'party' "
                               "AND row_id = ? AND field = 'stage'", (prospect,)).fetchall()
    assert [tuple(r) for r in logged] == [("prospect", "active")]


def test_a_new_case_with_a_prospect_makes_it_active(world):
    w = world
    prospect = add_party(w["repo"], "Velk Shipping", country="Chile", stage="prospect")
    add_enquiry(w["repo"], "OPP-9986", prospect, received="2026-12-11", scope="In")
    w["repo"].commit()
    assert w["conn"].execute("SELECT stage FROM party WHERE id = ?", (prospect,)).fetchone()[0] == "active"


# --------------------------------------------------- 4. four cards hidden --
@pytest.fixture
def busy(world):
    """A Decide row (an old quote nobody chased), an R2 row (a vendor
    silent for weeks) and an R6 row (Won, whose rate?)."""
    w = world
    repo = w["repo"]
    old = add_enquiry(repo, "OPP-9987", w["agent"], received="2026-11-02", scope="In", service_mode="Air")
    repo.set_status(old, "Quoted", "2026-11-03")
    vendor = add_party(repo, "Tarrow Lines", ptype="Carrier", country="USA")
    silent = add_enquiry(repo, "OPP-9988", w["agent"], received="2026-11-20", scope="In", service_mode="Air")
    repo.insert("vendor_request", {"enquiry_id": silent, "vendor_party_id": vendor, "asked_on": "2026-11-20"})
    won = add_enquiry(repo, "OPP-9989", w["agent"], received="2026-11-10", scope="In", service_mode="Air")
    repo.set_status(won, "Quoted", "2026-11-11")
    repo.set_status(won, "Won", "2026-11-20")
    repo.insert("vendor_request", {"enquiry_id": won, "vendor_party_id": vendor, "asked_on": "2026-11-10",
                                   "replied_on": "2026-11-10", "machine_status": "Quoted"})
    repo.commit()
    w.update(old=old, silent=silent, won=won)
    return w


def test_the_four_cards_are_hidden_by_default_and_the_rules_still_run(busy):
    w = busy
    conn = w["conn"]
    assert tracking.hidden_cards(conn) == {"r9", "decide", "r2", "r6"}
    model = tracking.page_model(conn, TODAY)
    keys = {c["key"] for c in model["today_cards"]}
    assert not keys & {"r9", "decide", "r2", "r6"}
    assert not {t["key"] for t in model["tiles"]} & {"r9", "decide"}
    hidden = {h["key"]: h["count"] for h in model["hidden"]}
    assert hidden == {"decide": 1, "r2": 1, "r6": 1}
    assert model["today_total"] == sum(c["count"] for c in model["today_cards"])
    # the rules themselves are unchanged
    steps = {i.enquiry_id: i.extra["step"] for i in tracking.rule_quoted_no_answer(conn, TODAY, tracking.fu_settings(conn))}
    assert steps[w["old"]] == "Decide"
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (w["old"],)).fetchone()
    nudge = tracking.case_nudge(conn, row, TODAY)
    badge = tracking.focus_count(conn, TODAY)
    # the muted line names them, with a link to Settings
    got = page(client(w), "/tracking")
    assert 'id="hidden-cards"' in got and "Decide 1" in got and "Vendor has not replied 1" in got \
        and "Won, whose rate? 1" in got and "A vendor answered" not in got.split('id="hidden-cards"')[1][:400]
    # unticked in Settings: back, with their tiles, Today's total and the badge
    c = client(w)
    c.post("/settings", data={"tracking_hidden_shown": "1"})
    assert tracking.hidden_cards(conn) == set()
    back = tracking.page_model(conn, TODAY)
    assert {"decide", "r2", "r6"} <= {c_["key"] for c_ in back["today_cards"]} and back["hidden"] == []
    assert {"r9", "decide"} <= {t["key"] for t in back["tiles"]}
    assert back["today_total"] == model["today_total"] + 3
    assert tracking.focus_count(conn, TODAY) == badge + 1                  # the Decide row
    assert tracking.case_nudge(conn, row, TODAY) == nudge
    assert 'id="hidden-cards"' not in page(c, "/tracking")


def test_settings_draws_one_tick_box_per_card_and_saves_the_ticked_ones(world):
    w = world
    c = client(w)
    got = page(c, "/settings")
    assert got.count('name="tracking_hidden"') == len(tracking.TODAY_CARDS)
    assert 'value="decide" checked' in got and 'value="fu1" checked' not in got
    c.post("/settings", data={"tracking_hidden_shown": "1", "tracking_hidden": ["r2", "fu2"]})
    assert db.get_setting(w["conn"], tracking.HIDDEN_SETTING) == "fu2,r2"
    # a form without the box leaves it alone
    c.post("/settings", data={"owners": "Ana\nBen"})
    assert db.get_setting(w["conn"], tracking.HIDDEN_SETTING) == "fu2,r2"


# --------------------------------------------- 5. the mail templates go --
# FX5 (2026-11-13): Acknowledge came back, blank (test_acknowledge)
@pytest.mark.parametrize("url", ["/enquiries/{id}/feedback",
                                 "/enquiries/{id}/followup/outlook", "/tracking/fu-outlook/{id}"])
def test_every_removed_route_is_404(world, url):
    assert client(world).post(url.format(id=world["case"])).status_code == 404


def test_their_buttons_and_boxes_are_gone(world):
    w = world
    c = client(w)
    case_page = page(c, "/enquiries/{}".format(w["case"]))
    for gone in (">Ask for feedback</button>", 'id="fu-text"', "data-copy=\"fu-text\"",
                 'name="step"', "Draft FU"):
        assert gone not in case_page
    assert "Open draft in Outlook" not in page(c, "/tracking")
    settings = page(c, "/settings")
    assert "Follow-up mail templates" not in settings and "Acknowledgment template" not in settings
    assert 'name="fu_template_1"' not in settings and 'name="ack_template"' not in settings
    assert "After Register" not in page(c, "/enquiries/new")


def test_the_template_texts_stay_in_the_setting_table(world):
    w = world
    db.set_setting(w["conn"], "fu_template_1", "Hi {first_name}, any news?")
    w["conn"].commit()
    client(w).post("/settings", data={"owners": "Ana\nBen"})
    assert db.get_setting(w["conn"], "fu_template_1") == "Hi {first_name}, any news?"


@pytest.mark.parametrize("channel, label", [("mail", "Log as sent"), ("call", "Log a call"),
                                            ("whatsapp", "WhatsApp"), ("teams", "Teams"), ("other", "Other")])
def test_a_follow_up_logged_from_the_case_page_by_each_channel_is_the_next_fu(world, channel, label):
    w = world
    w["repo"].add_followup(w["case"], "2026-12-09")                          # FU1 already
    w["repo"].commit()
    c = client(w)
    got = page(c, "/enquiries/{}".format(w["case"]))
    assert ">{}</button>".format(label) in got and "<span class=\"k\">FU2</span>" in got
    said = c.post("/enquiries/{}/followups".format(w["case"]), data={"at": TODAY, "channel": channel, "step": "1"},
                  follow_redirects=True).data.decode()
    assert "FU2 logged ({}, {}).".format(channel, TODAY) in said
    seq, got_channel = w["conn"].execute(
        "SELECT seq, channel FROM status_event WHERE enquiry_id = ? AND kind = 'followup' ORDER BY id DESC LIMIT 1",
        (w["case"],)).fetchone()
    assert (seq, got_channel) == (2, channel)


# ------------------------------------------ 6. the Make Outlook drafts folder --
def test_the_helper_folder_is_in_meta_sync(world):
    w = world
    fake = FakeFirestore()
    state = {}
    reports = w["tmp"] / "reports"
    assert fs.sync_once(w["conn"], fake, state, today=TODAY, reports_dir=reports, record=w["tmp"] / "x.json").ok
    assert fake.docs["meta/sync"]["helperFolder"] == ""
    client(w).post("/settings", data={"helper_folder": '"C:\\Team\\Make Outlook drafts"'})
    assert fs.helper_folder(w["conn"]) == "C:\\Team\\Make Outlook drafts"
    assert fs.sync_once(w["conn"], fake, state, today=TODAY, reports_dir=reports, record=w["tmp"] / "x.json").ok
    assert fake.docs["meta/sync"]["helperFolder"] == "C:\\Team\\Make Outlook drafts"
    assert 'name="helper_folder" value="C:\\Team\\Make Outlook drafts"' in page(client(w), "/settings")


# --------------------------------------------- 7. agent-rate-requests --
def test_agent_rate_requests_counts_both_sides_leaves_an_empty_office_out_and_keeps_a_backup(world):
    w = world
    repo = w["repo"]
    # a second live party with the same MFN number is added together
    twin = add_party(repo, "Marrow Freight Santiago", country="Chile", mfn_no="777001")
    add_party(repo, "Idle Forwarding", country="Chile", mfn_no="777009")          # nothing on either side
    vendor = add_party(repo, "Brisk Haul", country="USA", mfn_no="777005")
    won = add_enquiry(repo, "OPP-9990", twin, received="2026-12-01", scope="In")
    repo.set_status(won, "Quoted", "2026-12-02")
    repo.set_status(won, "Won", "2026-12-05")
    add_enquiry(repo, "OPP-9991", twin, received="2026-12-03", scope="Out")                       # not counted
    add_enquiry(repo, "OPP-9992", w["agent"], received="2026-12-04", scope="In",
                closed_reason="Registered in error", status="Dropped")                             # not counted
    repo.insert("vendor_request", {"enquiry_id": w["case"], "vendor_party_id": vendor, "asked_on": "2026-12-07",
                                   "replied_on": "2026-12-08", "machine_status": "Quoted", "awarded": 1})
    repo.insert("vendor_request", {"enquiry_id": won, "vendor_party_id": vendor, "asked_on": "2026-11-30"})
    repo.insert("vendor_request", {"enquiry_id": won, "vendor_party_id": vendor, "asked_on": "2026-11-29",
                                   "voided": 1})                                                   # not live
    repo.commit()
    folder = w["tmp"] / "outreach-import"
    folder.mkdir()
    (folder / "rate-requests.json").write_text('{"kind": "demo-rate-requests", "made": "2026-12-01", "offices": []}',
                                               encoding="utf-8")
    before = w["path"].read_bytes()
    result = agent_rate_requests.run(w["path"], today=TODAY, folder=folder)
    assert w["path"].read_bytes() == before                                 # the database is only read
    doc = json.loads((folder / "rate-requests.json").read_text(encoding="utf-8"))
    assert doc == result.doc
    assert {k: doc[k] for k in ("kind", "version", "made", "from", "source")} == {
        "kind": "demo-rate-requests", "version": 1, "made": TODAY, "from": "2026-11-30",
        "source": "OppList (counts only: no prices, customers or case numbers)"}
    assert doc["offices"] == [
        {"agentId": "777001", "theirs": {"n": 2, "quoted": 2, "won": 1, "last": "2026-12-07"},
         "ours": {"n": 0, "replied": 0, "quoted": 0, "won": 0, "last": ""}},
        {"agentId": "777005", "theirs": {"n": 0, "quoted": 0, "won": 0, "last": ""},
         "ours": {"n": 2, "replied": 1, "quoted": 1, "won": 1, "last": "2026-12-07"}}]
    assert result.backup is not None and result.backup.parent == folder / "_backup"
    assert json.loads(result.backup.read_text(encoding="utf-8"))["made"] == "2026-12-01"
    assert any("offices 0 -> 2 (2 new, 0 gone)" in line for line in result.lines)
    assert "total theirs: n 2, quoted 2, won 1" in result.lines
    # a second run the same day keeps both copies
    again = agent_rate_requests.run(w["path"], today=TODAY, folder=folder)
    assert again.backup != result.backup and again.backup.exists()


def test_agent_rate_requests_counts_nest(world):
    """won <= quoted <= n on theirs, won <= quoted <= replied <= n on ours: a
    Won case with no Quoted step is quoted; a Lost one that never reached
    Quoted is not; an awarded request is quoted and replied, a quoted one
    replied."""
    w = world
    repo = w["repo"]
    office = add_party(repo, "Ferrin Cargo", country="Chile", mfn_no="777011")
    vendor = add_party(repo, "Ostwick Haul", country="USA", mfn_no="777012")
    won_unquoted = add_enquiry(repo, "OPP-9993", office, received="2026-12-01", scope="In")
    repo.set_status(won_unquoted, "Won", "2026-12-04")                       # straight from New, no Quoted step
    lost_unquoted = add_enquiry(repo, "OPP-9994", office, received="2026-12-02", scope="In")
    repo.set_status(lost_unquoted, "Lost", "2026-12-05")
    repo.insert("vendor_request", {"enquiry_id": won_unquoted, "vendor_party_id": vendor,
                                   "asked_on": "2026-12-01", "awarded": 1})        # no reply date, no status
    repo.insert("vendor_request", {"enquiry_id": lost_unquoted, "vendor_party_id": vendor,
                                   "asked_on": "2026-12-02", "machine_status": "Quoted"})  # no reply date
    repo.insert("vendor_request", {"enquiry_id": lost_unquoted, "vendor_party_id": vendor,
                                   "asked_on": "2026-12-03"})                        # no answer at all
    repo.commit()
    folder = w["tmp"] / "nest"
    doc = agent_rate_requests.run(w["path"], today=TODAY, folder=folder).doc
    by_id = {o["agentId"]: o for o in doc["offices"]}
    assert by_id["777011"]["theirs"] == {"n": 2, "quoted": 1, "won": 1, "last": "2026-12-02"}
    assert by_id["777012"]["ours"] == {"n": 3, "replied": 2, "quoted": 2, "won": 1, "last": "2026-12-03"}
    for o in doc["offices"]:
        t, u = o["theirs"], o["ours"]
        assert t["won"] <= t["quoted"] <= t["n"]
        assert u["won"] <= u["quoted"] <= u["replied"] <= u["n"]
