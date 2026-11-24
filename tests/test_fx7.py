"""Batch FX7 (2026-11-23, schema v58): the follow-ups the morning scan finds
go through Suggested updates; a wait's follow-ups never count for the
quote; WC2 with one office needs the signature to agree.

1. a chaser after the quote becomes a pending suggestion (kind followup)
   and nothing is on the timeline; Accept writes the event as the scan
   used to (date, channel mail, source mail, [mid:]) and Tracking moves on;
2. Dismiss writes nothing and the next run does not suggest the mail
   again; an expired one the same;
3. a follow-up logged by hand that day: no suggestion; one logged after the
   suggestion closes it, saying why;
4. a wait's chaser is suggested on our question with the wait's number,
   accepted, and the wait's ladder moves; a wait's follow-up dated the
   quote's day is not the quote's FU1 (wait note, chaser, Quoted the same
   day); the timeline pill 'FU1 · our question';
5. Tracking's row with a pending suggestion; Edit (the date, the note);
   the replies are still written by the scan;
6. WC2: one office named in the signature (its city, or its country's code)
   filled, one office not named listed, several as before;
7. the v57 to v58 upgrade keeps every suggestion of every kind;
8. the mixed-up case numbers are measured, nothing written.

Every company, person, mail, place and date here is invented; cases from
OPP-9501, .test domains.
"""

from __future__ import annotations

import re

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import (case_numbers, db, detect_followups, intake, mail_archive, outlook_bridge, parsing, paths,
                     schema_migrations, suggestions, tracking, wait, mfn_link)
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app
from test_rg2a import master_world

RECEIVED = "2026-11-02"            # a Monday
QUOTED = "2026-11-03"
CHASED = "2026-11-05"              # a Thursday
SUBJECT = "AIR RATE // MERROWDALE - TAVISH BAY // 6 CASES"
AGENT = ("pell@brackenquay.test", "Pell Orrin")
US = ("wren@jojofreight.example", "JJC-WREN TALLIS")
CHASER = "Hi Pell,\nFollowing up on the offer we sent, any feedback?\nBest regards,\nWren"


@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    path = tmp_path / "fx7.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    agent = add_party(repo, "Brackenquay Forwarding", country="Chile")
    repo.insert("party_domain", {"domain": "brackenquay.test", "party_id": agent})
    ids = {"path": path, "archive": archive, "agent": agent}
    for opp in ("OPP-9501", "OPP-9502", "OPP-9503"):
        subject = "{} {}".format(SUBJECT, opp)
        ids[opp] = add_enquiry(repo, opp, agent, received=RECEIVED, source_subject=subject,
                               thread_key=parsing.subject_thread_key(subject), conversation_id="CONV-" + opp,
                               service_mode="Air", origin_country="USA", dest_country="Chile")
    repo.commit()
    app = create_app(path)
    app.config["TESTING"] = True
    yield conn, repo, ids, app.test_client()
    conn.close()


def mail(at, sender, body, mid, opp, internal=False, to=(AGENT,)):
    return MailInfo(subject="RE: {} {}".format(SUBJECT, opp), received_on=at[:10], received_at=at, sent_at=at,
                    entry_id="EID-" + mid, conversation_id="CONV-" + opp, conversation_topic=SUBJECT,
                    sender_name=sender[1], sender_address=sender[0], sender_internal=internal,
                    to_addresses=[a for a, _ in to], body_excerpt=body, folder_path="\\Mailbox\\Inbox",
                    display_to="; ".join(n for _, n in to), message_id="<{}@fx7.test>".format(mid))


def store(ids, *mails):
    conn = mail_archive.open_archive(ids["archive"])
    mail_archive.store(conn, [mail_archive.record_from_info(m) for m in mails])
    conn.commit()
    conn.close()


def scan(conn, ids):
    with mail_archive.ArchiveSearch(ids["archive"]) as search:
        return detect_followups.run(conn, search)


def quoted(repo, case, at=QUOTED):
    repo.set_status(case, "Quoted", at)
    repo.commit()


def followups(conn, case):
    return conn.execute("SELECT * FROM status_event WHERE enquiry_id = ? AND kind = 'followup' ORDER BY id",
                        (case,)).fetchall()


def pending(conn, case):
    return conn.execute("SELECT * FROM suggestion WHERE enquiry_id = ? AND kind = 'followup' AND state = 'pending'",
                        (case,)).fetchall()


def r3(conn, today):
    return {item.enquiry_id: item.extra.get("step") for s in tracking.run(conn, today) if s.rule == "R3"
            for item in s.items}


# ------------------------------------------------- 1. after the quote --
def test_a_chaser_after_the_quote_becomes_a_suggestion_and_nothing_is_on_the_timeline(world):
    conn, repo, ids, client = world
    case = ids["OPP-9501"]
    quoted(repo, case)
    store(ids, mail(CHASED + " 09:30", US, CHASER, "c1", "OPP-9501", internal=True))
    report = scan(conn, ids)
    assert report.followups == 1 and any("(suggested)" in line for line in report.lines)
    assert followups(conn, case) == []
    row = pending(conn, case)[0]
    assert (row["proposed_status"], row["proposed_date"], row["message_id"], row["entry_id"], row["source"]) == \
        ("quote", CHASED, "<c1@fx7.test>", "EID-c1", "scan")
    assert row["evidence_sender"] == "JJC-WREN TALLIS <wren@jojofreight.example>"
    assert row["evidence_quote"].startswith("Hi Pell, Following up on the offer we sent")
    page = client.get("/enquiries/{}".format(case)).data.decode("utf-8")
    assert "FU1 found in the mail: Nov 5, JJC-WREN TALLIS, Hi Pell, Following up" in page
    assert 'kind-followup' in page


def test_accept_writes_the_event_as_the_scan_did_and_tracking_moves_on(world):
    conn, repo, ids, client = world
    case = ids["OPP-9501"]
    quoted(repo, case)
    store(ids, mail(CHASED + " 09:30", US, CHASER, "c1", "OPP-9501", internal=True))
    scan(conn, ids)
    assert r3(conn, "2026-11-06")[case] == "FU1"
    row = pending(conn, case)[0]
    said = suggestions.accept(db.Repository(conn, source="test"), row["id"])
    assert said == "OPP-9501: FU1 logged on {}".format(CHASED)
    fu = followups(conn, case)[0]
    assert (fu["at"], fu["seq"], fu["channel"], fu["source"], fu["voided"]) == (CHASED, 1, "mail", "mail", 0)
    assert fu["note"].startswith("FU detected: RE: AIR RATE") and fu["note"].endswith("[mid:<c1@fx7.test>]")
    decided = conn.execute("SELECT state, decided_as, event_ref, edited FROM suggestion WHERE id = ?",
                           (row["id"],)).fetchone()
    assert tuple(decided) == ("accepted", "followup", "status_event:{}".format(fu["id"]), 0)
    assert r3(conn, "2026-11-06").get(case) != "FU1"
    assert int(conn.execute("SELECT fu_count FROM v_enquiry WHERE id = ?", (case,)).fetchone()[0]) == 1
    # the next run knows the mail by its event
    assert scan(conn, ids).followups == 0 and len(followups(conn, case)) == 1


# ------------------------------------------------- 2. dismissed, expired --
def test_dismiss_writes_nothing_and_the_mail_is_not_suggested_again(world):
    conn, repo, ids, client = world
    case = ids["OPP-9501"]
    quoted(repo, case)
    store(ids, mail(CHASED + " 09:30", US, CHASER, "c1", "OPP-9501", internal=True))
    scan(conn, ids)
    suggestions.dismiss(db.Repository(conn, source="test"), pending(conn, case)[0]["id"])
    assert followups(conn, case) == []
    again = scan(conn, ids)
    assert again.followups == 0 and again.skipped >= 1
    assert pending(conn, case) == [] and followups(conn, case) == []


def test_an_expired_one_counts_as_known_as_well(world):
    conn, repo, ids, client = world
    case = ids["OPP-9501"]
    quoted(repo, case)
    store(ids, mail(CHASED + " 09:30", US, CHASER, "c1", "OPP-9501", internal=True))
    scan(conn, ids)
    gone = suggestions.expire(db.Repository(conn, source="test"), today="2027-01-15")
    assert [r["kind"] for r in gone] == ["followup"]
    assert scan(conn, ids).followups == 0
    assert pending(conn, case) == [] and followups(conn, case) == []


# --------------------------------------------- 3. logged by hand that day --
def test_a_follow_up_logged_by_hand_that_day_wins(world):
    conn, repo, ids, client = world
    case = ids["OPP-9501"]
    quoted(repo, case)
    repo.add_followup(case, CHASED, channel="call")
    repo.commit()
    store(ids, mail(CHASED + " 09:30", US, CHASER, "c1", "OPP-9501", internal=True))
    assert scan(conn, ids).followups == 0
    assert conn.execute("SELECT COUNT(*) FROM suggestion WHERE kind = 'followup'").fetchone()[0] == 0
    assert [f["channel"] for f in followups(conn, case)] == ["call"]


def test_one_logged_by_hand_after_the_suggestion_closes_it(world):
    conn, repo, ids, client = world
    case = ids["OPP-9501"]
    quoted(repo, case)
    store(ids, mail(CHASED + " 09:30", US, CHASER, "c1", "OPP-9501", internal=True))
    scan(conn, ids)
    row = pending(conn, case)[0]
    page = client.post("/enquiries/{}/followups".format(case), data={"at": CHASED, "channel": "whatsapp"},
                       follow_redirects=True).data.decode("utf-8")
    assert "closed: this one stands" in page
    closed = conn.execute("SELECT * FROM suggestion WHERE id = ?", (row["id"],)).fetchone()
    assert closed["state"] == "dismissed" and suggestions.closed_why(closed) == "a follow-up was logged by hand that day"
    assert [f["channel"] for f in followups(conn, case)] == ["whatsapp"]
    page = client.get("/enquiries/{}".format(case)).data.decode("utf-8")
    assert "a follow-up was logged by hand that day" in page                # the dismissed list says why
    # and Accept on a suggestion whose day has one by hand writes nothing
    store(ids, mail("2026-11-10 09:30", US, CHASER, "c2", "OPP-9501", internal=True))
    scan(conn, ids)
    later = pending(conn, case)[0]
    repo.add_followup(case, "2026-11-10", channel="call")
    repo.commit()
    said = suggestions.accept(db.Repository(conn, source="test"), later["id"])
    assert "nothing written - a follow-up was logged by hand that day" in said
    assert [f["channel"] for f in followups(conn, case)] == ["whatsapp", "call"]


# ------------------------------------------------------------ 4. a wait --
def press(client, case, what="the dimensions of each case", at="2026-11-03"):
    return client.post("/enquiries/{}/status".format(case), data={"status": "waiting", "what": what, "at": at},
                       follow_redirects=True)


WAIT_CHASER = "Hi Pell,\nTo prepare our quotation we still need the following:\n- the dimensions\nBest regards,\nWren"


def test_a_waits_chaser_is_suggested_on_our_question_and_moves_the_waits_ladder(world):
    conn, repo, ids, client = world
    case = ids["OPP-9502"]
    press(client, case)
    store(ids, mail("2026-11-05 10:00", US, WAIT_CHASER, "w1", "OPP-9502", internal=True))
    report = scan(conn, ids)
    assert report.waits == 1 and report.followups == 1
    row = pending(conn, case)[0]
    assert row["proposed_status"] == "question" and followups(conn, case) == []
    view = suggestions.view(conn, [row])[0]
    assert view["fu"].line.startswith("FU1 on our question found in the mail: Nov 5, JJC-WREN TALLIS")
    found = wait.live(conn, case)
    assert wait.ladder(conn, found, "2026-11-06")["fu_count"] == 0
    said = suggestions.accept(db.Repository(conn, source="test"), row["id"])
    assert said == "OPP-9502: FU1 on our question logged on 2026-11-05"
    fu = followups(conn, case)[0]
    assert (fu["seq"], fu["source"], fu["channel"]) == (1, "mail", "mail") and "[mid:<w1@fx7.test>]" in fu["note"]
    rung = wait.ladder(conn, wait.live(conn, case), "2026-11-06")
    assert (rung["fu_count"], rung["next"]) == (1, "FU2")


def test_a_waits_follow_up_dated_the_quotes_day_is_not_the_quotes_fu1(world):
    """OPP-1728's order: the wait note, the chaser (accepted), then Quoted
    dated the chaser's day."""
    conn, repo, ids, client = world
    case = ids["OPP-9502"]
    press(client, case)
    store(ids, mail("2026-11-05 10:00", US, WAIT_CHASER, "w1", "OPP-9502", internal=True))
    scan(conn, ids)
    suggestions.accept(db.Repository(conn, source="test"), pending(conn, case)[0]["id"])
    quoted(repo, case, at="2026-11-05")
    fu = followups(conn, case)[0]
    assert wait.of_waits(conn, case) == {fu["id"]: conn.execute(
        "SELECT id FROM status_event WHERE enquiry_id = ? AND note LIKE 'Waiting for requester:%'",
        (case,)).fetchone()[0]}
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    assert (row["fu_count"], row["last_fu_on"], row["fu_count_total"]) == (0, None, 1)
    ladder = tracking.ladder(conn, row, "2026-11-06")
    assert ladder["next"] == "FU1" and [s[0] for s in ladder["steps"]][:2] == ["Quoted", "FU1 due"]
    assert r3(conn, "2026-11-06")[case] == "FU1"
    # the quote's own FU1, logged by hand, is FU1
    assert db.Repository(conn).next_quote_seq(case) == 1
    page = client.get("/enquiries/{}".format(case)).data.decode("utf-8")
    assert "FU1 &middot; our question" in page


# ------------------------------------------------ 5. Tracking, Edit, replies --
def test_trackings_row_points_at_the_pending_suggestion(world, monkeypatch):
    import opplist.web as web
    monkeypatch.setattr(web, "_today", lambda: "2026-11-06")
    conn, repo, ids, client = world
    case = ids["OPP-9501"]
    quoted(repo, case)
    store(ids, mail(CHASED + " 09:30", US, CHASER, "c1", "OPP-9501", internal=True))
    scan(conn, ids)
    sid = pending(conn, case)[0]["id"]
    model = tracking.page_model(conn, "2026-11-06")
    fu1 = next(c for c in model["today_cards"] if c["key"] == "fu1")
    row = next(r for r in fu1["rows"] if r["enquiry_id"] == case)
    assert row["found_fu"]["id"] == sid
    page = client.get("/tracking").data.decode("utf-8")
    assert "Found in the mail on Nov 5: accept it in Suggested updates" in page
    assert "/enquiries/{}#sg-{}".format(case, sid) in page
    other = next(r for r in fu1["rows"] if r["enquiry_id"] != case) if len(fu1["rows"]) > 1 else None
    assert other is None or other["found_fu"] is None


def test_edit_changes_the_date_and_the_note_and_keeps_the_mail(world):
    conn, repo, ids, client = world
    case = ids["OPP-9501"]
    quoted(repo, case)
    store(ids, mail(CHASED + " 09:30", US, CHASER, "c1", "OPP-9501", internal=True))
    scan(conn, ids)
    sid = pending(conn, case)[0]["id"]
    client.post("/suggestions/{}/edit".format(sid), data={"date": "2026-11-06", "note": "chased by mail, late",
                                                         "next": "/enquiries/{}".format(case)})
    fu = followups(conn, case)[0]
    assert (fu["at"], fu["seq"]) == ("2026-11-06", 1)
    assert fu["note"] == "chased by mail, late [mid:<c1@fx7.test>]"
    decided = conn.execute("SELECT decided_as, edited FROM suggestion WHERE id = ?", (sid,)).fetchone()
    assert tuple(decided) == ("followup", 1)
    assert scan(conn, ids).followups == 0


def test_the_replies_are_still_written_by_the_scan(world):
    conn, repo, ids, client = world
    case = ids["OPP-9501"]
    quoted(repo, case)
    store(ids, mail(CHASED + " 09:30", US, CHASER, "c1", "OPP-9501", internal=True),
          mail("2026-11-06 11:00", AGENT, "Hi Wren,\nThanks, we will revert soon.\nPell", "r1", "OPP-9501",
               to=(US,)))
    report = scan(conn, ids)
    assert (report.followups, report.replies) == (1, 1)
    reply = conn.execute("SELECT at, source, note FROM status_event WHERE enquiry_id = ? AND note LIKE 'Reply from%'",
                         (case,)).fetchone()
    assert (reply["at"], reply["source"]) == ("2026-11-06", "mail") and "we will revert soon" in reply["note"]


# ------------------------------------------------------------------- 6. WC2 --
OFFICES = ('"member_id","company","formerly_known_as","office_type","office_city","city","country",'
           '"country_code","web_domain","email_domain"\n'
           '"97001","Brightmere Cargo Ltda","","Head Office","Port Alder","Port Alder","Chile","CL",'
           '"brightmere.test","brightmere.test"\n'
           '"91002","Ventisca Carga SpA","","Head Office","Corral Bay","Corral Bay","Chile","CL",'
           '"ventisca.test","ventisca.test"\n'
           '"91003","Fernhollow Logistics","","Head Office","Port Wren","Port Wren","Brazil","BR",'
           '"fernhollow.test","fernhollow.test"\n'
           '"91004","Tidewick Shipping Brasil","","Branch Office","Quillmouth","Quillmouth","Brazil","BR",'
           '"tidewick.test","tidewick.test"\n'
           '"91005","Tidewick Shipping USA","","Head Office","Bransford","Bransford","USA","US",'
           '"tidewick.test","tidewick.test"\n')


@pytest.fixture
def mfn(tmp_path):
    conn = db.open_db(tmp_path / "wc2.db")
    repo = db.Repository(conn, source="test")
    master_world(repo, tmp_path)
    repo.commit()
    folder = paths.DATA_DIR / "mfn" / "directory"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "mfn-offices.csv").write_text(OFFICES, encoding="utf-8")
    mfn_link._CACHE.clear()
    yield conn
    conn.close()
    mfn_link._CACHE.clear()


def asking(sender, body, name="Sela Varn", key="9511"):
    return outlook_bridge.MailInfo(subject="Rate request", received_on="2027-03-04", received_at="2027-03-04 09:40",
                                   sender_address=sender, sender_name=name, sender_internal=False,
                                   to_addresses=["desk@jojofreight.test"], to_names=["JJC-DESK"], body_excerpt=body,
                                   entry_id="ENTRY-" + key, conversation_id="CONV-" + key)


def test_one_office_named_in_the_signature_by_its_city_is_filled(mfn):
    body = "Hello,\nPlease quote 3 cases.\nBest regards,\nSela Varn\nBrightmere Cargo\nAv. Costanera 12, Port Alder\n"
    new = intake.build(mfn, asking("sela@brightmere.test", body)).new_party
    assert (new["key"], new["country"], new["city"], new["mfn_no"]) == \
        ("Brightmere Cargo Ltda", "Chile", "Port Alder", "97001")
    assert "mfn_offices" not in new


def test_one_office_whose_country_code_is_in_the_signature_is_filled(mfn):
    body = "Hello,\nPlease quote 3 cases.\nKind regards,\nSela Varn\nVentisca Carga\nValdivia - CL\n"
    new = intake.build(mfn, asking("sela@ventisca.test", body, key="9512")).new_party
    assert (new["key"], new["mfn_no"]) == ("Ventisca Carga SpA", "91002")


def test_one_office_the_signature_does_not_name_is_listed_to_pick(mfn):
    body = "Hello,\nPlease quote 3 cases.\nBest regards,\nSela Varn\nFernhollow Logistics\nMarrowgate, Lithuania\n"
    new = intake.build(mfn, asking("sela@fernhollow.test", body, key="9513")).new_party
    assert "mfn_no" not in new and new.get("sources", {}).get("key") != intake.MFN_LINE
    assert [o["company"] for o in new["mfn_offices"]] == ["Fernhollow Logistics"]
    assert new["mfn_why"] == ("from the MFN directory: Fernhollow Logistics, Port Wren, Brazil - "
                              "the mail's signature does not name it")
    # 'in' or 'br' written small is not a country's code
    body = "Hello,\nPlease quote.\nBest regards,\nSela\nFernhollow Logistics\nbr office in the north\n"
    assert "mfn_no" not in intake.build(mfn, asking("sela@fernhollow.test", body, key="9514")).new_party


def test_a_closing_with_more_words_or_only_a_contact_block_is_read_as_the_signature(mfn):
    body = "Hello,\nPlease quote 3 cases.\nSaludos cordiales/ Best Regards/ Hasta pronto\nSela Varn\nCorral Bay\n"
    assert intake.build(mfn, asking("sela@ventisca.test", body, key="9516")).new_party["mfn_no"] == "91002"
    body = ("Hello,\nPlease quote 3 cases to Quillmouth.\nSela Varn | Brightmere Cargo\n"
            "Address: Av. Costanera 12, Port Alder\nTel: +56 2 5550 1100\n")
    assert intake.build(mfn, asking("sela@brightmere.test", body, key="9517")).new_party["mfn_no"] == "97001"


def test_several_offices_as_before(mfn):
    body = "Hello,\nPlease quote.\nBest regards,\nIvo Sand\nTidewick Shipping\nQuillmouth, Brazil\n"
    new = intake.build(mfn, asking("ivo@tidewick.test", body, name="Ivo Sand", key="9515")).new_party
    assert [o["company"] for o in new["mfn_offices"]] == ["Tidewick Shipping Brasil", "Tidewick Shipping USA"]
    assert "mfn_no" not in new


# ------------------------------------------------------- 7. the upgrade --
def _as_v57(conn):
    """The suggestion table as v57 had it (no followup in either list), the
    v58 views gone, schema_version 57."""
    sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    sql = sql.replace(",'followup')", ")")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    for name in re.findall(r"CREATE VIEW\s+(\w+)\s+AS", sql):
        conn.execute("DROP VIEW IF EXISTS {}".format(name))
    schema_migrations._rebuild_table(conn, "suggestion", sql)
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    schema_migrations._rebuild_views(conn)
    for name in ("v_wait_followup", "v_wait"):
        conn.execute("DROP VIEW IF EXISTS {}".format(name))
    conn.execute("UPDATE setting SET value = '57' WHERE key = 'schema_version'")
    conn.commit()


def test_v57_to_v58_keeps_every_suggestion_of_every_kind(tmp_path):
    conn = db.open_db(tmp_path / "old.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    agent = add_party(repo, "Brackenquay Forwarding", country="Chile")
    case = add_enquiry(repo, "OPP-9521", agent, received="2026-11-02")
    _as_v57(conn)
    stored = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'suggestion'").fetchone()[0]
    assert "'followup'" not in stored
    decided = [d for d in suggestions.DECIDED if d != "followup"]
    for i, kind in enumerate(k for k in suggestions.KINDS if k != "followup"):
        repo.insert("suggestion", {"enquiry_id": case, "opp_no": "OPP-9521", "kind": kind,
                                   "state": "accepted", "decided_as": decided[i % len(decided)],
                                   "evidence_quote": "row {}".format(i)})
    repo.insert("suggestion", {"enquiry_id": case, "opp_no": "OPP-9521", "kind": "status", "state": "pending"})
    repo.commit()
    before = [tuple(r) for r in conn.execute("SELECT * FROM suggestion ORDER BY id")]
    notes = schema_migrations.upgrade(conn)
    assert notes[0] == "schema upgraded to version 58"
    assert any(n.startswith("suggestion rebuilt (kind and decided_as take followup)") for n in notes)
    counted = next(n for n in notes if n.startswith("rows before and after: "))
    assert "suggestion {0} / {0}".format(len(before)) in counted and "foreign_key_check: nothing" in notes
    assert [tuple(r) for r in conn.execute("SELECT * FROM suggestion ORDER BY id")] == before
    assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION == 58
    # the new kind and decision are taken, and the views are there
    repo.insert("suggestion", {"enquiry_id": case, "opp_no": "OPP-9521", "kind": "followup", "state": "accepted",
                               "decided_as": "followup"})
    repo.commit()
    assert conn.execute("SELECT COUNT(*) FROM v_wait_followup").fetchone()[0] == 0
    assert schema_migrations.upgrade(conn) == []
    conn.close()


# ------------------------------------------------- 8. mixed-up numbers --
def test_a_number_put_into_another_thread_with_the_same_people_is_measured(world):
    conn, repo, ids, client = world
    case = ids["OPP-9503"]
    other = "SEA FREIGHT TO HOLLOWMERE - BQ - JOJO"
    first = MailInfo(subject=other, received_on="2026-11-04", received_at="2026-11-04 08:00",
                     sent_at="2026-11-04 08:00", entry_id="EID-o1", conversation_id="CONV-OTHER",
                     conversation_topic=other, sender_name=AGENT[1], sender_address=AGENT[0],
                     to_addresses=[US[0]], body_excerpt="Hi Wren,\nPlease quote sea freight.\nPell",
                     folder_path="\\Mailbox\\Inbox", message_id="<o1@fx7.test>")
    numbered = MailInfo(subject="RE: [OPP-9503] " + other, received_on="2026-11-05", received_at="2026-11-05 09:00",
                        sent_at="2026-11-05 09:00", entry_id="EID-o2", conversation_id="CONV-OTHER",
                        conversation_topic=other, sender_name=US[1], sender_address=US[0], sender_internal=True,
                        to_addresses=[AGENT[0]], body_excerpt="Hi Pell,\nWe need the volume.\nWren",
                        folder_path="\\Mailbox\\Sent Items", message_id="<o2@fx7.test>")
    own = mail("2026-11-03 09:00", US, "Hi Pell,\nWell received.\nWren", "k1", "OPP-9503", internal=True)
    own.subject = "RE: [OPP-9503] {} OPP-9503".format(SUBJECT)
    store(ids, first, numbered, own)
    changes = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    with mail_archive.ArchiveSearch(ids["archive"]) as search:
        rows = case_numbers.mismatches(conn, search)
    assert [(r["case"], r["date"], r["subject"]) for r in rows] == [("OPP-9503", "2026-11-05 09:00",
                                                                   "RE: [OPP-9503] " + other)]
    assert rows[0]["earlier in its conversation"] == "1 earlier with no number"
    assert rows[0]["sender"] == "JJC-WREN TALLIS <wren@jojofreight.example>"
    assert conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == changes          # nothing written
    out = paths.DATA_DIR / "fx7-mismatch.csv"
    case_numbers.write_mismatches(rows, out)
    text = out.read_bytes()
    assert text.startswith(b"\xef\xbb\xbf\"case\",\"date\"") and b"\r\n" in text
