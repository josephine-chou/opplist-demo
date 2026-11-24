"""Batch W2a (2026-10-29): the rate request card, OppList's half
(OppListWeb INTERFACE.md section 5) - lists/vendors with the addresses the
mail archive gives, cases' oc and dc, meta/sync.teamCc, and the action
'ask' taken in as vendor requests from the 'web'.

Every company, person, address, place and date here is invented; cases
from OPP-7301, addresses at .test domains.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import (assign_list, case_mail, db, detect_followups, firebase_sync as fs, firebase_vendors,
                     intake, mail_archive, parsing, schema_migrations, tracking)
from opplist.outlook_bridge import MailInfo
from test_firebase_pull import FakeSite

TODAY = "2026-10-28"
SUBJECT = "[OPP-7301] RATE REQUEST // CHICAGO - SANTIAGO // AIR 300 KG // EXW"


def at(minute, hour=15):
    return dt.datetime(2026, 10, 28, hour, minute, tzinfo=dt.timezone.utc)


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "ask.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "owners", ["Ana", "Ben"])
    db.set_setting(conn, "internal_domains", ["jojofreight.test"])
    p = {}

    def party(key, domain=None, **extra):
        p[key] = add_party(repo, key, **extra)
        if domain:
            repo.insert("party_domain", {"domain": domain, "party_id": p[key]})
        return p[key]

    party("Brightwater Trading", "brightwater.test", country="Chile")                 # the requester
    party("Northgate Freight", "northgate.test", country="Chile", full_name="Northgate Freight Services SpA")
    party("Ironwood Line", "ironwood.test", country="Brazil")
    party("Riverside Haulage", "riverside.test", country="China")
    party("Copper Bay", "copperbay.test", country="Unknown")
    party("Sable Port", "sableport.test", country="USA")
    party("Maple Contact", country="Chile")                                            # no domain: contacts only
    repo.insert("contact", {"party_id": p["Maple Contact"], "email": "first@maple.test"})
    repo.insert("contact", {"party_id": p["Maple Contact"], "email": "lead@maple.test", "is_primary": 1})
    party("JoJo JJK", ptype="Office", country="Unknown")
    repo.insert("contact", {"party_id": p["JoJo JJK"], "email": "jjk-desk@jojofreight.test"})
    party("Old Pelican", "oldpelican.test", country="Chile")
    repo.update("party", p["Old Pelican"], {"merged_into": p["Northgate Freight"]})
    party("Voided Vendor", "voided.test", country="Chile")
    ids = {}

    def case(opp, origin, dest, mode, status="New", received="2026-10-01", **extra):
        extra.setdefault("scope", "In")
        ids[opp] = add_enquiry(repo, opp, p["Brightwater Trading"], received=received, origin_country=origin,
                               dest_country=dest, service_mode=mode, **extra)
        if status != "New":
            repo.set_status(ids[opp], status, "2026-10-03")
        return ids[opp]

    case("OPP-7301", "USA", "Chile", "Air")                       # a US export
    case("OPP-7302", "China", "USA", "FCL", "Quoted")             # a US import
    case("OPP-7303", "China", "Brazil", "LCL")                    # a cross-trade
    case("OPP-7304", "Unknown", "Chile", "Air")
    case("OPP-7305", None, None, "Air", "Lost")
    case("OPP-7306", "USA", "Chile", "Air", handled_by="YUL")
    case("OPP-7307", "USA", "Brazil", "Air", received="2026-10-26")

    def asked(key, opp, on, **extra):
        return repo.insert("vendor_request", dict(extra, enquiry_id=ids[opp], vendor_party_id=p[key], asked_on=on))

    asked("Northgate Freight", "OPP-7301", "2026-10-05", replied_on="2026-10-06", machine_status="Quoted")
    asked("Northgate Freight", "OPP-7302", "2026-10-12")
    asked("Northgate Freight", "OPP-7303", "2026-10-14")
    asked("Northgate Freight", "OPP-7303", "2026-10-20")          # the same case twice: one case
    asked("Ironwood Line", "OPP-7302", "2026-10-12")
    asked("Riverside Haulage", "OPP-7302", "2026-10-12")
    asked("Copper Bay", "OPP-7302", "2026-10-12")
    asked("Sable Port", "OPP-7302", "2026-10-12")
    asked("Maple Contact", "OPP-7304", "2026-10-15")
    asked("JoJo JJK", "OPP-7301", "2026-10-05")
    asked("Old Pelican", "OPP-7304", "2026-10-15")                # merged away: not listed
    asked("Voided Vendor", "OPP-7301", "2026-10-05", voided=1)    # struck out: not listed
    repo.commit()
    yield {"conn": conn, "repo": repo, "path": path, "ids": ids, "p": p, "tmp": tmp_path,
           "state": tmp_path / "fs.json", "record": tmp_path / "exports.json", "reports": tmp_path / "reports"}
    conn.close()


def run(w, fake=None, **kw):
    kw.setdefault("today", TODAY)
    fake = fake or FakeSite()
    result = fs.sync(w["path"], client=fake, state_file=w["state"], reports_dir=w["reports"], record=w["record"], **kw)
    w["conn"].rollback()
    return result, fake


def sent(when, to=(), cc=(), subject="RATE REQUEST", sender="desk@jojofreight.test", internal=True, mid=""):
    mail = MailInfo(subject=subject, received_on=when[:10], received_at=when, entry_id="EID-" + (mid or when + sender),
                    conversation_id="", conversation_topic=subject, sender_name=sender.split("@")[0],
                    sender_address=sender, sender_internal=internal, to_addresses=list(to), cc_addresses=list(cc),
                    body_excerpt="Hello,\n\nPlease quote.", folder_path="\\Personal Folder\\Sent Items",
                    display_to="; ".join(to), message_id=mid or "<{}-{}@x>".format(when, sender))
    return mail_archive.record_from_info(mail, sent_at=when if internal else "")


def build_archive(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = mail_archive.open_archive(path)
    mail_archive.store(conn, records)
    conn.commit()
    conn.close()
    return path


N = "@northgate.test"
ARCHIVE = [
    # Northgate: ana on To three times; noreply more often but never counts
    sent("2026-10-01 09:00", to=["ana" + N, "noreply" + N], cc=["bo" + N, "cy" + N, "fay" + N]),
    sent("2026-10-02 09:00", to=["ana" + N, "noreply" + N], cc=["bo" + N, "cy" + N, "dee" + N, "fay" + N]),
    sent("2026-10-03 09:00", to=["ana" + N, "NoReply" + N], cc=["bo" + N, "eve" + N, "dee" + N]),
    sent("2026-10-04 09:00", to=["cy" + N, "no-reply" + N]),
    # Ironwood: one each on To - the one written to last
    sent("2026-09-01 09:00", to=["gus@ironwood.test"]),
    sent("2026-09-20 09:00", to=["hal@ironwood.test"]),
    # Riverside: To beats CC, even a CC more often
    sent("2026-09-05 09:00", to=["jon@riverside.test"], cc=["ivy@riverside.test"]),
    sent("2026-09-06 09:00", cc=["ivy@riverside.test"]),
    # Copper Bay: never on To, so the most often on CC
    sent("2026-09-07 09:00", cc=["kim@copperbay.test"]),
    # Sable Port: nothing sent, so who wrote to us most often
    sent("2026-09-08 09:00", sender="lee@sableport.test", internal=False, to=["desk@jojofreight.test"]),
    sent("2026-09-09 09:00", sender="lee@sableport.test", internal=False, to=["desk@jojofreight.test"]),
    sent("2026-09-10 09:00", sender="mo@sableport.test", internal=False, to=["desk@jojofreight.test"]),
    sent("2026-09-11 09:00", sender="mailer-daemon@sableport.test", internal=False),
    sent("2026-09-12 09:00", sender="mailer-daemon@sableport.test", internal=False),
    sent("2026-09-13 09:00", sender="mailer-daemon@sableport.test", internal=False),
    # an office is known by its contacts only, whatever we sent at our own domain
    sent("2026-09-14 09:00", to=["kul-other@jojofreight.test"]),
]


# ------------------------------------------------------------ lists/vendors --
def test_the_vendor_rows_counts_countries_and_modes(world):
    rows, note = firebase_vendors.rows(world["conn"], world["tmp"] / "none" / "mail.db")
    by = {r["key"]: r for r in rows}
    assert [r["key"] for r in rows] == sorted(by, key=str.casefold)
    assert "Old Pelican" not in by and "Voided Vendor" not in by and "Brightwater Trading" not in by
    north = by["Northgate Freight"]
    assert north == {
        "key": "Northgate Freight", "name": "Northgate Freight", "full": "Northgate Freight Services SpA",
        "type": "Agent", "country": "Chile", "asked": 3, "quoted": 1, "lastAsked": "2026-10-20",
        # the far ends: a US export's destination, a US import's origin, both of a cross-trade
        "askedIn": ["Brazil", "Chile", "China"], "quotedIn": ["Chile"], "modes": ["Air", "FCL", "LCL"],
        "parts": [],                                       # Q2b: none recorded on its requests
        "roles": [],                                       # Q2c: none on the party
        "to": "", "cc": []}
    assert by["Ironwood Line"]["askedIn"] == ["China"] and by["Ironwood Line"]["quotedIn"] == []
    assert by["Maple Contact"]["askedIn"] == ["Chile"]                # Unknown left out
    assert by["Copper Bay"]["country"] == ""                           # Unknown -> ''
    assert note.startswith("no mail archive at ")


def test_to_and_cc_from_the_archive(world):
    archive = build_archive(world["tmp"] / "archive" / "mail.db", ARCHIVE)
    rows, note = firebase_vendors.rows(world["conn"], archive)
    by = {r["key"]: (r["to"], r["cc"]) for r in rows}
    assert note == ""
    # cc: on at least two of ana's mails, most often first, at most three
    assert by["Northgate Freight"] == ("ana" + N, ["bo" + N, "cy" + N, "dee" + N])
    assert by["Ironwood Line"] == ("hal@ironwood.test", [])
    assert by["Riverside Haulage"] == ("jon@riverside.test", [])
    assert by["Copper Bay"] == ("kim@copperbay.test", [])
    assert by["Sable Port"] == ("lee@sableport.test", [])
    assert by["Maple Contact"] == ("lead@maple.test", [])              # is_primary first
    assert by["JoJo JJK"] == ("jjk-desk@jojofreight.test", [])


def test_the_push_carries_vendors_countries_and_the_team_cc(world, monkeypatch):
    w = world
    result, fake = run(w)
    assert result.ok, result.reason
    assert result.vendors == 7 and result.vendors_to == 2          # no archive: Maple and KUL's contacts
    assert "no mail archive" in result.vendors_note
    assert fs.status(w["state"])["vendors"].startswith("vendors: 7 listed, 2 with an address; no mail archive")
    assert len(fake.docs["lists/vendors"]["rows"]) == 7
    assert fake.docs["meta/sync"]["teamCc"] == schema_migrations.RATE_REQUEST_CC
    assert "sync W3" in fake.docs["meta/sync"]["version"]
    cases = {k[len("cases/"):]: d for k, d in fake.docs.items() if k.startswith("cases/")}
    assert (cases["OPP-7301"]["oc"], cases["OPP-7301"]["dc"]) == ("USA", "Chile")
    assert (cases["OPP-7304"]["oc"], cases["OPP-7304"]["dc"]) == ("", "Chile")       # Unknown
    assert (cases["OPP-7305"]["oc"], cases["OPP-7305"]["dc"]) == ("", "")            # none
    # the archive turns up: only the vendors' list changes
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", build_archive(w["tmp"] / "archive" / "mail.db", ARCHIVE))
    second, _ = run(w, fake)
    assert second.ok and second.vendors_to == 7 and second.vendors_note == ""
    assert sorted(fake.commits[-2]) == ["lists/vendors"]
    # the setting emptied: no team CC on the card
    db.set_setting(w["conn"], "rate_request_cc", "")
    w["conn"].commit()
    run(w, fake)
    assert fake.docs["meta/sync"]["teamCc"] == ""


def test_settings_shows_and_saves_the_team_cc(world):
    from opplist.web import create_app
    app = create_app(world["path"])
    app.config["TESTING"] = True
    client = app.test_client()
    body = client.get("/settings").data.decode("utf-8")
    assert 'name="rate_request_cc"' in body and "JJC-MFN" in body
    client.post("/settings", data={"rate_request_cc": "  #TEAM  <team@jojofreight.test> "})
    conn = db.open_db(world["path"])
    assert db.get_setting(conn, "rate_request_cc") == "#TEAM <team@jojofreight.test>"
    conn.close()


# -------------------------------------------------------------- the ask --
def ask(fake, action_id, minute, opp, **fields):
    fields.setdefault("date", TODAY)
    fields.setdefault("subject", SUBJECT.replace("OPP-7301", opp))
    fields.setdefault("teamCc", True)
    fake.act(action_id, at(minute), "ask", opp, **fields)


def requests_of(w, opp):
    return w["conn"].execute(
        "SELECT v.*, p.key AS vendor_key FROM vendor_request v JOIN party p ON p.id = v.vendor_party_id "
        "WHERE v.enquiry_id = ? AND v.voided = 0 ORDER BY v.id", (w["ids"][opp],)).fetchall()


def answer(fake, action_id):
    return fake.docs["actionResults/" + action_id]


def test_listed_vendors_are_logged_from_the_web(world):
    w = world
    fake = FakeSite()
    ask(fake, "k1", 1, "OPP-7307", vendors=["northgate freight", "Ironwood Line", "Ghost Vendor"])
    result, _ = run(w, fake)
    assert result.ok, result.reason
    assert answer(fake, "k1")["ok"] is True
    assert answer(fake, "k1")["msg"] == ("OPP-7307: rate requests to Northgate Freight and Ironwood Line logged on "
                                         "2026-10-28. Left out: Ghost Vendor is not a vendor in OppList.")
    made = requests_of(w, "OPP-7307")
    subject = SUBJECT.replace("OPP-7301", "OPP-7307")
    assert [(r["vendor_key"], r["asked_on"], r["source"]) for r in made] == [
        ("Northgate Freight", TODAY, "web"), ("Ironwood Line", TODAY, "web")]
    assert made[0]["thread_key"] == parsing.subject_thread_key(subject)
    assert made[0]["notes"] == "Logged on the team site by Dee Rowan: {} (team copied)".format(subject)
    assert case_mail.vendor_subject(made[0]) == subject
    assert {r[0] for r in w["conn"].execute(
        "SELECT source FROM change_log WHERE table_name = 'vendor_request' AND row_id IN (?, ?)",
        (made[0]["id"], made[1]["id"]))} == {"web"}
    assert w["conn"].execute("SELECT status_confirmed FROM enquiry WHERE id = ?",
                             (w["ids"]["OPP-7307"],)).fetchone()[0] == 1
    # the case on the site lists them now (vendor = party key)
    assert [r["vendor"] for r in fake.docs["cases/OPP-7307"]["rr"]] == ["Northgate Freight", "Ironwood Line"]


def test_within_a_day_once_merged_keys_and_the_same_action_twice(world):
    w = world
    fake = FakeSite()
    ask(fake, "d1", 1, "OPP-7307", vendors=["Northgate Freight"], date="2026-10-27")
    ask(fake, "d2", 2, "OPP-7307", vendors=["Old Pelican", "Ironwood Line"], teamCc=False)   # merged into Northgate
    run(w, fake)
    assert answer(fake, "d2")["msg"] == ("OPP-7307: a rate request to Ironwood Line logged on 2026-10-28. "
                                         "Already on the case: Northgate Freight.")
    assert not requests_of(w, "OPP-7307")[1]["notes"].endswith("(team copied)")
    count = len(requests_of(w, "OPP-7307"))
    state = fs.load_state(w["state"])
    del state["pulled_at"]                                      # the same actions come back (a new project)
    fs.save_state(state, w["state"])
    again, _ = run(w, fake)
    assert again.pull.skipped == 2 and len(requests_of(w, "OPP-7307")) == count == 2
    assert w["conn"].execute("SELECT COUNT(*) FROM site_action").fetchone()[0] == 2


def test_refusals_closed_another_office_bad_date_nothing_matched(world):
    w = world
    fake = FakeSite()
    ask(fake, "r1", 1, "OPP-7305", vendors=["Northgate Freight"])
    ask(fake, "r2", 2, "OPP-7306", vendors=["Northgate Freight"])
    ask(fake, "r3", 3, "OPP-7307", vendors=["Northgate Freight"], date="28/10/2026")
    ask(fake, "r4", 4, "OPP-7307", vendors=["Ghost Vendor"], adhoc=[{"email": "not an address", "cc": []},
                                                                    {"email": "desk@jojofreight.test", "cc": []}])
    ask(fake, "r5", 5, "OPP-7399", vendors=["Northgate Freight"])
    result, _ = run(w, fake)
    assert result.ok and result.pull.refused == 5
    assert answer(fake, "r1")["msg"] == "OPP-7305 is closed (Lost), so the rate requests were not logged."
    assert answer(fake, "r2")["msg"] == "OPP-7306 is handled by YUL, so the rate requests were not logged."
    assert answer(fake, "r3")["msg"] == "OPP-7307: '28/10/2026' is not a date, so the rate requests were not logged."
    assert answer(fake, "r4")["msg"] == (
        "OPP-7307: nothing to log, so the rate requests were not logged (Ghost Vendor is not a vendor in OppList; "
        "not an address is not a mail address; desk@jojofreight.test is one of ours).")
    assert answer(fake, "r5")["msg"] == "OPP-7399 is not in OppList, so the rate requests were not logged."
    assert not requests_of(w, "OPP-7307")


def test_typed_addresses_known_new_free_mail_ours(world):
    w = world
    conn = w["conn"]
    add_party(w["repo"], intake.suggested_party_name("kestrelbay.test"))      # that key is taken
    w["repo"].commit()
    fake = FakeSite()
    ask(fake, "t1", 1, "OPP-7307", adhoc=[
        {"email": "rates@ironwood.test", "cc": []},                            # a domain on file
        {"email": "quotes@newharbor.test", "cc": []},                          # a new party
        {"email": "ops@newharbor.test", "cc": []},                             # the same party: one vendor
        {"email": "sales@kestrelbay.test", "cc": []},                          # its key is taken: the domain
        {"email": "someone@gmail.com", "cc": []},                           # a mailbox: its own party
        {"email": "desk@jojofreight.test", "cc": []}])                            # ours
    ask(fake, "t2", 2, "OPP-7303", adhoc=["Someone@gmail.com"])             # found again by the contact
    result, _ = run(w, fake)
    assert result.ok, result.reason
    harbor = intake.suggested_party_name("newharbor.test")
    assert [r["vendor_key"] for r in requests_of(w, "OPP-7307")] == [
        "Ironwood Line", harbor, "kestrelbay.test", "someone@gmail.com"]
    new = conn.execute("SELECT * FROM party WHERE key = ?", (harbor,)).fetchone()
    assert (new["type"], new["country"], new["source"], new["notes"]) == (
        "Unknown", "Unknown", "manual", "Added from a rate request Dee Rowan logged on the team site.")
    assert conn.execute("SELECT domain FROM party_domain WHERE party_id = ?", (new["id"],)).fetchall()[0][0] == \
        "newharbor.test"
    assert [r[0] for r in conn.execute("SELECT email FROM contact WHERE party_id = ? ORDER BY id", (new["id"],))] == \
        ["quotes@newharbor.test"]
    free = conn.execute("SELECT id FROM party WHERE key = ?", ("someone@gmail.com",)).fetchone()[0]
    assert not conn.execute("SELECT 1 FROM party_domain WHERE party_id = ?", (free,)).fetchone()
    assert [r["vendor_key"] for r in requests_of(w, "OPP-7303")][-1] == "someone@gmail.com"
    assert conn.execute("SELECT COUNT(*) FROM party WHERE lower(key) = ?", ("someone@gmail.com",)).fetchone()[0] == 1
    msg = answer(fake, "t1")["msg"]
    assert "{}, kestrelbay.test and someone@gmail.com are new in OppList (Unknown).".format(harbor) in msg
    assert "desk@jojofreight.test is one of ours" in msg
    assert "new in OppList" not in answer(fake, "t2")["msg"]


def test_an_address_for_a_listed_vendor_becomes_its_contact(world):
    w = world
    conn = w["conn"]
    fake = FakeSite()
    ask(fake, "c1", 1, "OPP-7307", vendors=["Maple Contact", "Northgate Freight"],
        to={"Maple Contact": "desk@maplefreight.test", "Northgate Freight": "ana@northgate.test",
            "Ironwood Line": "nobody@ironwood.test"})                            # not in vendors: ignored
    run(w, fake)
    maple, north, iron = w["p"]["Maple Contact"], w["p"]["Northgate Freight"], w["p"]["Ironwood Line"]
    emails = lambda pid: [r[0] for r in conn.execute("SELECT email FROM contact WHERE party_id = ? ORDER BY id",
                                                     (pid,))]
    assert emails(maple)[-1] == "desk@maplefreight.test"
    assert conn.execute("SELECT domain FROM party_domain WHERE party_id = ?", (maple,)).fetchone()[0] == \
        "maplefreight.test"                                                  # it had no domain
    assert emails(north) == ["ana@northgate.test"]
    assert emails(iron) == []
    # the next push writes to it
    assert {r["key"]: r["to"] for r in fake.docs["lists/vendors"]["rows"]}["Maple Contact"] == "lead@maple.test"


def test_the_scans_vendor_suggestion_is_dismissed(world):
    w = world
    conn = w["conn"]
    case = w["ids"]["OPP-7302"]
    made = w["repo"].insert("suggestion", {"enquiry_id": case, "opp_no": "OPP-7302", "kind": "vendor",
                                           "vendor_key": "Ironwood", "vendor_domain": "ironwood.test",
                                           "proposed_date": TODAY, "source": "mail"})
    other = w["repo"].insert("suggestion", {"enquiry_id": case, "opp_no": "OPP-7302", "kind": "vendor",
                                            "vendor_key": "Riverside Haulage", "proposed_date": TODAY,
                                            "source": "mail"})
    w["repo"].commit()
    fake = FakeSite()
    ask(fake, "s1", 1, "OPP-7302", vendors=["Ironwood Line"])
    run(w, fake)
    row = conn.execute("SELECT * FROM suggestion WHERE id = ?", (made,)).fetchone()
    assert (row["state"], row["decided_as"]) == ("dismissed", "dismissed")
    assert row["note"] == "Logged on the team site by Dee Rowan on 2026-10-28"
    assert conn.execute("SELECT state FROM suggestion WHERE id = ?", (other,)).fetchone()[0] == "pending"
    assert answer(fake, "s1")["msg"].endswith("The mail's suggestion for Ironwood Line was dismissed.")


# ------------------------------------------------------ the mail and Next --
def test_the_mail_card_gives_a_web_request_its_tab(world, monkeypatch):
    w = world
    fake = FakeSite()
    ask(fake, "m1", 1, "OPP-7307", vendors=["Northgate Freight"])
    run(w, fake)
    subject = SUBJECT.replace("OPP-7301", "OPP-7307")
    archive = build_archive(w["tmp"] / "archive" / "mail.db", [
        sent("2026-10-28 10:00", to=["ana" + N], cc=["desk@jojofreight.test"], subject=subject, mid="<ask@x>"),
        sent("2026-10-29 08:00", sender="ana" + N, internal=False, to=["desk@jojofreight.test"],
             subject="RE: " + subject, mid="<rate@x>")])
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    row = w["conn"].execute("SELECT * FROM v_enquiry WHERE id = ?", (w["ids"]["OPP-7307"],)).fetchone()
    search, _ = case_mail.open_search()
    try:
        tabs = case_mail.tabs(w["conn"], search, row)
    finally:
        search.__exit__(None, None, None)
    assert [t["label"] for t in tabs] == ["Agent thread", "Northgate Freight"]
    assert [m.message_id for m in tabs[1]["mails"]] == ["<ask@x>", "<rate@x>"]


def numbered(when, sender, internal, words, subject):
    return MailInfo(subject=subject, received_on=when[:10], received_at=when, entry_id="E" + when,
                    conversation_id="", conversation_topic=subject, sender_name=sender.split("@")[0],
                    sender_address=sender, sender_internal=internal, to_addresses=[], cc_addresses=[],
                    body_excerpt=words, folder_path="\\Inbox", display_to="", message_id="<{}@x>".format(when))


def test_a_vendors_answer_on_a_numbered_thread_is_not_the_requesters_reply(world, monkeypatch):
    w = world
    conn = w["conn"]
    subject = SUBJECT.replace("OPP-7301", "OPP-7302")
    thread = [numbered("2026-10-28 10:00", "desk@jojofreight.test", True, "Hi team, please quote as below.", subject),
              numbered("2026-10-29 09:00", "ana" + N, False, "Could you confirm the pickup address? Rate below.",
                       "RE: " + subject)]
    monkeypatch.setattr(detect_followups, "thread_of", lambda search, row, conn=None: thread)
    monkeypatch.setattr(detect_followups, "requester_domains", lambda conn, search, row: {"brightwater.test"})
    row = conn.execute("SELECT * FROM v_enquiry WHERE opp_no = 'OPP-7302'").fetchone()
    report = detect_followups.Report()
    before = conn.execute("SELECT COUNT(*) FROM status_event").fetchone()[0]
    detect_followups.scan_case(conn, db.Repository(conn, source="import"), None, row, {"jojofreight.test"}, report,
                               dry_run=False)
    assert (report.followups, report.replies) == (0, 0)
    assert not hasattr(report, "answers")                    # FX4: the AL6a answer step is gone
    assert conn.execute("SELECT COUNT(*) FROM status_event").fetchone()[0] == before
    assert "OPP-7302" not in [r.opp_no for r in assign_list.pending_listed(conn, "2026-10-30")]


def test_next_after_an_ask(world):
    w = world
    conn = w["conn"]
    case = w["ids"]["OPP-7307"]
    nudge = lambda day: tracking.case_nudge(conn, conn.execute("SELECT * FROM v_enquiry WHERE id = ?",
                                                                (case,)).fetchone(), day)
    assert nudge(TODAY)["rule"] == "R1"                                        # nothing asked, 2 days
    fake = FakeSite()
    ask(fake, "n1", 1, "OPP-7307", vendors=["Northgate Freight", "Ironwood Line"])
    run(w, fake)
    assert nudge(TODAY) == {"rule": "", "text": "Send the quote", "hint": "", "days": None}
    assert nudge("2026-10-30")["text"] == "Vendor Northgate Freight no reply, 2 days"
    first = requests_of(w, "OPP-7307")[0]
    w["repo"].update("vendor_request", first["id"], {"replied_on": "2026-10-29", "machine_status": "Quoted"})
    w["repo"].commit()
    assert nudge("2026-10-30")["text"] == "Send the quote"                    # T3: a rate in hand


# --------------------------------------------------------------------- v44 --
def test_v44_lets_web_into_vendor_request_and_suggestion_and_keeps_every_row(tmp_path):
    path = tmp_path / "old.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    agent = add_party(repo, "Brightwater Trading", country="Chile")
    vendor = add_party(repo, "Northgate Freight", country="Chile")
    case = add_enquiry(repo, "OPP-7350", agent, received="2026-10-01")
    for day in ("2026-10-02", "2026-10-03"):
        repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": vendor, "asked_on": day})
    repo.insert("suggestion", {"enquiry_id": case, "opp_no": "OPP-7350", "kind": "vendor", "vendor_key": "Northgate"})
    repo.commit()
    counts = [conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in ("vendor_request", "suggestion")]
    # back to v43: the old CHECK lists, no rate_request_cc
    conn.execute("PRAGMA foreign_keys = OFF")
    for (view,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'").fetchall():
        conn.execute("DROP VIEW {}".format(view))
    for table in ("vendor_request", "suggestion"):
        old = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
        conn.execute(old.replace(",'web'", "").replace("CREATE TABLE {}".format(table),
                                                       "CREATE TABLE {}_old".format(table), 1))
        conn.execute("INSERT INTO {0}_old SELECT * FROM {0}".format(table))
        conn.execute("DROP TABLE {}".format(table))
        conn.execute("ALTER TABLE {0}_old RENAME TO {0}".format(table))
    conn.execute("DELETE FROM setting WHERE key = 'rate_request_cc'")
    conn.execute("UPDATE setting SET value = '43' WHERE key = 'schema_version'")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO vendor_request (enquiry_id, vendor_party_id, source) VALUES (?, ?, 'web')",
                     (case, vendor))
    conn.close()
    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION >= 44
    assert any(n.startswith("vendor_request rebuilt ({} rows".format(counts[0])) for n in notes)
    assert any(n.startswith("suggestion rebuilt ({} rows".format(counts[1])) for n in notes)
    assert any(n.startswith("restore point") for n in notes)
    assert db.get_setting(conn, "rate_request_cc") == schema_migrations.RATE_REQUEST_CC
    assert conn.execute("SELECT COUNT(*) FROM v_enquiry").fetchone()[0] == 1
    repo = db.Repository(conn, source="test")
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": vendor, "asked_on": "2026-10-04",
                                   "source": "web"})
    repo.insert("suggestion", {"enquiry_id": case, "opp_no": "OPP-7350", "kind": "status", "source": "web"})
    repo.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO vendor_request (enquiry_id, vendor_party_id, source) VALUES (?, ?, 'site')",
                     (case, vendor))
    conn.close()
