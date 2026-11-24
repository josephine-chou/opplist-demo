"""Batch MF1: a thread key that keeps a bare-number reference, no subject
fallback on a weak key, the acknowledgment and Open in Outlook on the
registered mail, the stale-archive line, and Who asked? from mail for a
(TBC) case. FX4 (2026-11-12): the acknowledgment's draft and the
follow-up drafts' reply target are gone; Open in Outlook stays.

Every company, person, address, place, reference and number here is
invented (.test domains); cases from OPP-9301.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import case_mail, db, direct_customers_out as dco, mail_archive, outlook_bridge, parsing, rekey_threads, who_asked
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app

DESK = "desk@jojofreight.example"


def info(subject, at, sender, body="Hello,\nPlease see below.\nRegards", conversation="", to=(DESK,),
         cc=(), internal=None, entry="", message_id=""):
    internal = sender.endswith("@jojofreight.example") if internal is None else internal
    return MailInfo(subject=subject, received_on=at[:10], received_at=at, entry_id=entry or "EID-" + message_id.strip("<>"),
                    conversation_id=conversation, conversation_topic=subject, sender_name=sender.split("@")[0],
                    sender_address=sender, sender_internal=internal, to_addresses=list(to), cc_addresses=list(cc),
                    body_excerpt=body, folder_path="\\Personal Folder\\QUOTES", display_to="; ".join(to),
                    message_id=message_id)


def build_archive(path, mails, synced="2026-11-09 08:15:00"):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = mail_archive.open_archive(path)
    mail_archive.store(conn, [mail_archive.record_from_info(m) for m in mails])
    mail_archive.set_meta(conn, "last_sync_at", synced)
    conn.commit()
    conn.close()
    return path


# ------------------------------------------------------------ 1. the key --
def test_a_reference_that_leaves_no_letter_stays_in_the_key():
    assert parsing.clean_subject("RE: TOVA-QTE-0448") == "TOVA-QTE-0448"
    assert parsing.subject_thread_key("RE: TOVA-QTE-0448") == "TOVAQTE0448"
    assert parsing.subject_thread_key("FW: RE: PX55120-S/NO-8812") == "PX55120SNO8812"
    # the rule before MF1, which the rekey command compares with
    assert parsing.subject_thread_key("RE: TOVA-QTE-0448", keep_bare_ref=False) == "0448"


def test_subjects_whose_key_keeps_a_letter_are_keyed_as_before():
    for subject, key in (("RE: QX2611905 - OCEAN FREIGHT / ORLANDO - TEMUCO", "OCEANFREIGHTORLANDOTEMUCO"),
                         ("RE: QX41/48812 - AIR QUOTE // DENVER - CUSCO", "AIRQUOTEDENVERCUSCO"),
                         ("RE: RATE REQUEST // DOOR - TEMUCO // GJ7714", "RATEREQUESTDOORTEMUCO"),
                         ("RE: 3 PALLETS TO LIMA", "3PALLETSTOLIMA")):
        assert parsing.subject_thread_key(subject) == key
        assert parsing.subject_thread_key(subject, keep_bare_ref=False) == key


@pytest.fixture
def keyed(tmp_path):
    path = tmp_path / "rekey.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    heron = add_party(repo, "Heron Cargo", country="Chile")
    old = add_enquiry(repo, "OPP-9301", heron, received="2026-10-12", source_subject="RE: TOVA-QTE-0448",
                      thread_key="0448")
    hers = add_enquiry(repo, "OPP-9302", heron, received="2026-10-13", source_subject="RE: TOVA-QTE-0449",
                       thread_key="TOVA0449MANUAL")
    plain = add_enquiry(repo, "OPP-9303", heron, received="2026-10-14", source_subject="RE: QX2611905 - LIMA RATE",
                        thread_key="LIMARATE")
    repo.insert("enquiry_thread", {"enquiry_id": plain, "kind": "thread_key", "value": "0551",
                                   "note": "RE: TOVA-QTE-0551", "added_on": "2026-10-15", "source": "manual"})
    repo.commit()
    conn.close()
    return path, old, hers, plain


def test_rekey_lists_then_applies_the_keys_the_old_rule_made(keyed):
    path, old, hers, plain = keyed
    conn = db.open_db(path)
    report = rekey_threads.run(conn, dry_run=True)
    assert [(c[0], c[2], c[3]) for c in report.cases] == [("OPP-9301", "0448", "TOVAQTE0448")]
    assert [(t[0], t[2], t[3]) for t in report.threads] == [("OPP-9303", "0551", "TOVAQTE0551")]
    assert [t[0] for t in report.left] == ["OPP-9302"]            # a key she set is hers
    assert conn.execute("SELECT thread_key FROM enquiry WHERE id = ?", (old,)).fetchone()[0] == "0448"
    before = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]

    report = rekey_threads.run(conn, dry_run=False)
    assert conn.execute("SELECT thread_key FROM enquiry WHERE id = ?", (old,)).fetchone()[0] == "TOVAQTE0448"
    assert conn.execute("SELECT thread_key FROM enquiry WHERE id = ?", (hers,)).fetchone()[0] == "TOVA0449MANUAL"
    assert conn.execute("SELECT value FROM enquiry_thread WHERE enquiry_id = ?", (plain,)).fetchone()[0] \
        == "TOVAQTE0551"
    log = conn.execute("SELECT table_name, field, old_value, new_value, action_id FROM change_log "
                       "WHERE id > (SELECT MAX(id) FROM change_log) - ? AND source = 'rekey-threads'",
                       (conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] - before,)).fetchall()
    assert sorted((r[0], r[1], r[2], r[3]) for r in log) == [
        ("enquiry", "thread_key", "0448", "TOVAQTE0448"), ("enquiry_thread", "value", "0551", "TOVAQTE0551")]
    assert len({r[4] for r in log}) == 1                          # one action

    again = rekey_threads.run(conn, dry_run=True)
    assert not again.cases and not again.threads
    conn.close()


def test_the_rekey_command_makes_a_restore_point_before_it_writes(keyed, tmp_path, capsys):
    from opplist.__main__ import main
    path, old, _hers, _plain = keyed
    assert main(["rekey-threads", "--db", str(path)]) == 0
    assert "dry run: nothing written" in capsys.readouterr().out
    assert main(["rekey-threads", "--db", str(path), "--apply", "--restore-dir", str(tmp_path / "rp")]) == 0
    out = capsys.readouterr().out
    assert "restore point:" in out and "OPP-9301  0448 -> TOVAQTE0448" in out
    assert list((tmp_path / "rp").glob("opplist_before_rekey-threads_*.db"))
    conn = db.open_db(path)
    assert conn.execute("SELECT thread_key FROM enquiry WHERE id = ?", (old,)).fetchone()[0] == "TOVAQTE0448"
    conn.close()


def test_a_new_case_gets_the_new_key(tmp_path):
    conn = db.open_db(tmp_path / "new.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    heron = add_party(repo, "Heron Cargo", country="Chile")
    case = repo.create_enquiry({"requester_party_id": heron, "received_on": "2026-11-02",
                                "source_subject": "RE: TOVA-QTE-0612"}, "2026-11-02")
    assert conn.execute("SELECT thread_key FROM enquiry WHERE id = ?", (case,)).fetchone()[0] == "TOVAQTE0612"
    conn.close()


# --------------------------------------------------- 2. no weak fallback --
UNRELATED = [info("BOOKING 0448 // HARBOR LINE // SHIPPER COPY", "2026-11-05 10:00", "ops@harborline.test",
                  conversation="CONV-OTHER", message_id="<u1@test>"),
             info("RE: BOOKING 0448 // HARBOR LINE // SHIPPER COPY", "2026-11-05 12:00", DESK,
                  conversation="CONV-OTHER", to=("ops@harborline.test",), message_id="<u2@test>"),
             info("RE: HARBOR LINE QUOTE ORLANDO", "2026-11-04 09:00", "ops@harborline.test",
                  conversation="CONV-HL", message_id="<u3@test>")]


def test_the_subject_search_never_runs_on_a_weak_key(tmp_path, monkeypatch):
    assert not case_mail.strong_subject("0448") and not case_mail.strong_subject("HL 0448")
    assert case_mail.strong_subject("TOVA-QTE-0448")
    archive = build_archive(tmp_path / "a" / "mail.db", UNRELATED)
    with mail_archive.ArchiveSearch(archive) as search:
        assert case_mail.mails_for(search, "", [], set(), ["RE: 0448"]) == []          # no letter
        assert case_mail.mails_for(search, "", [], set(), ["RE: HL 0448"]) == []       # under eight
        # a subject with a letter and eight characters still falls back
        assert [m.message_id for m in case_mail.mails_for(search, "", [], set(), ["HARBOR LINE QUOTE"])] \
            == ["<u3@test>"]
        # the rule before MF1 would have shown the other thread
        monkeypatch.setattr(case_mail, "strong_subject", lambda clean: True)
        assert {m.message_id for m in case_mail.mails_for(search, "", [], set(), ["RE: 0448"])} \
            >= {"<u1@test>", "<u2@test>"}


def test_an_empty_agent_thread_says_so_with_the_sync_date(tmp_path, monkeypatch):
    archive = build_archive(tmp_path / "a" / "mail.db", UNRELATED, synced=dt.datetime.now().strftime("%Y-%m-%d 08:15:00"))
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    path = tmp_path / "weak.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    heron = add_party(repo, "Heron Cargo", country="Chile")
    case = add_enquiry(repo, "OPP-9304", heron, received="2026-11-05", source_subject="RE: 0448",
                       thread_key="0448", entry_id="EID-9304", conversation_id="CONV-9304")
    repo.commit()
    conn.close()
    html = create_app(path).test_client().get("/enquiries/{}".format(case)).get_data(as_text=True)
    assert 'data-tab="case">Agent thread <b>0</b>' in html
    assert "No mail of this case in the archive yet (last synced {} 08:15).".format(
        dt.date.today().isoformat()) in html
    assert "HARBOR LINE" not in html


# ---------------------------------------- 3. the registered mail first --
@pytest.fixture
def registered(tmp_path, monkeypatch):
    """OPP-9305 registered today from a mail the archive has not synced yet;
    the archive has two newer mails of its conversation."""
    archive = build_archive(tmp_path / "a" / "mail.db", [
        info("RE: RE: TOVA-QTE-0730", "2026-11-09 15:00", "lena@heroncargo.test", conversation="CONV-9305",
             message_id="<n1@test>"),
        info("RE: RE: RE: TOVA-QTE-0730", "2026-11-09 16:30", DESK, conversation="CONV-9305",
             to=("lena@heroncargo.test",), message_id="<n2@test>")])
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    path = tmp_path / "reg.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    heron = add_party(repo, "Heron Cargo", country="Chile")
    repo.insert("party_domain", {"domain": "heroncargo.test", "party_id": heron})
    case = add_enquiry(repo, "OPP-9305", heron, received="2026-11-09", source_subject="RE: TOVA-QTE-0730",
                       thread_key="TOVAQTE0730", entry_id="EID-9305-REG", conversation_id="CONV-9305")
    case_mail.remember_registered(repo, case, "<reg9305@test>", "2026-11-09")
    repo.commit()
    conn.close()
    calls = {"open": []}
    monkeypatch.setattr(outlook_bridge, "open_item", lambda **k: calls["open"].append((k["entry_id"], k["message_id"])))
    monkeypatch.setattr(outlook_bridge, "read_item", lambda **k: info(
        "RE: TOVA-QTE-0730", "2026-11-09 11:00", "lena@heroncargo.test", entry=k.get("entry_id", "")))
    return create_app(path).test_client(), path, case, calls


def test_acknowledge_replies_to_the_registered_mail(registered, monkeypatch):
    """FX5: Acknowledge (blank) is a Reply All of the registered mail, never
    the thread's newer one - Open in Outlook's rule."""
    client, _path, case, calls = registered
    replies = []
    monkeypatch.setattr(outlook_bridge, "blank_reply_all",
                        lambda entry_id, make_subject, message_id="", folders=(): replies.append(
                            (entry_id, message_id, make_subject("RE: TOVA-QTE-0730"))))
    client.post("/enquiries/{}/acknowledge".format(case))
    assert replies == [("EID-9305-REG", "<reg9305@test>", "RE: [OPP-9305] TOVA-QTE-0730")]
    assert not hasattr(outlook_bridge, "draft_reply_all") and calls["open"] == []


def test_open_in_outlook_on_the_title_opens_the_registered_mail(registered):
    client, _path, case, calls = registered
    client.post("/enquiries/{}/open-in-outlook".format(case))
    assert calls["open"] == [("EID-9305-REG", "<reg9305@test>")]


def test_a_case_without_a_registered_mail_still_opens_the_newest(registered):
    client, path, case, calls = registered
    conn = db.open_db(path)
    db.Repository(conn, source="test").update("enquiry", case, {"entry_id": None})
    conn.execute("UPDATE enquiry_thread SET voided = 1 WHERE enquiry_id = ?", (case,))
    conn.commit()
    conn.close()
    client.post("/enquiries/{}/open-in-outlook".format(case))
    assert calls["open"] == [("EID-n2@test", "<n2@test>")]


# ----------------------------------------------------- 4. a stale archive --
def test_the_archive_is_behind_after_more_than_one_working_day(tmp_path):
    archive = build_archive(tmp_path / "a" / "mail.db", UNRELATED, synced="2026-11-05 08:19:00")   # a Thursday
    monday = dt.datetime(2026, 11, 9, 9, 0)
    info_ = mail_archive.status(archive, now=monday)
    assert info_["behind"]
    assert info_["behind_line"] == ("Mail archive last synced 2026-11-05 08:19. "
                                    "Run python -m opplist mail-sync with Outlook open.")
    friday = build_archive(tmp_path / "b" / "mail.db", UNRELATED, synced="2026-11-06 08:15:00")
    assert not mail_archive.status(friday, now=monday)["behind"]                   # one working day
    assert not mail_archive.status(tmp_path / "none" / "mail.db", now=monday)["behind"]


def test_the_case_page_and_tracking_show_the_stale_line(tmp_path, monkeypatch):
    archive = build_archive(tmp_path / "a" / "mail.db", UNRELATED, synced="2026-01-05 08:19:00")
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    path = tmp_path / "stale.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    heron = add_party(repo, "Heron Cargo", country="Chile")
    case = add_enquiry(repo, "OPP-9306", heron, received="2026-11-05", source_subject="RE: LIMA AIR RATE")
    repo.commit()
    conn.close()
    client = create_app(path).test_client()
    line = "Mail archive last synced 2026-01-05 08:19. Run python -m opplist mail-sync with Outlook open."
    assert line in client.get("/enquiries/{}".format(case)).get_data(as_text=True)
    assert line in client.get("/tracking").get_data(as_text=True)


# ------------------------------------------------------- 5. Who asked? --
FORWARD_BODY = ("Can you take this one? I am out today.\n\n"
                "From: Lio Brandt <lio@marlowcargo.test>\nSent: Monday, October 19, 2026 9:05 AM\n"
                "To: JJC-DESK <desk@jojofreight.example>\nSubject: RATE REQUEST // DALLAS - SANTOS\n\n"
                "Hello,\nPlease quote 2 x 40HQ Dallas to Santos, floor tiles.\nLio\n")


@pytest.fixture
def tbc(tmp_path, monkeypatch):
    archive = build_archive(tmp_path / "a" / "mail.db", [
        info("RATE REQUEST // DALLAS - SANTOS", "2026-10-19 10:00", "ana@jojofreight.example", conversation="CONV-9310",
             to=("rates@pelican.test",), cc=("mara@corvidfreight.test",), message_id="<w1@test>"),
        info("RE: RATE REQUEST // DALLAS - SANTOS", "2026-10-19 14:00", "rates@pelican.test",
             conversation="CONV-9310", to=("ana@jojofreight.example",), message_id="<w2@test>"),
        info("FW: RATE REQUEST // DALLAS - SANTOS", "2026-10-20 08:30", "rafe@jojofreight.example", body=FORWARD_BODY,
             conversation="CONV-9310", to=(DESK,), message_id="<w3@test>")])
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    path = tmp_path / "tbc.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    p = {"tbc": add_party(repo, "(TBC)", placeholder=1, country="Unknown"),
         "pelican": add_party(repo, "Pelican Lines"), "gull": add_party(repo, "Gull Haulage"),
         "corvid": add_party(repo, "Corvid Freight", country="Chile"),
         "finch": add_party(repo, "Harbor Finch", country="Chile"),
         "wren": add_party(repo, "Wren Logistics", country="Chile"),
         "tern": add_party(repo, "Tern Forwarding", country="Chile"),
         "kite": add_party(repo, "Kite Shipping", country="Chile")}
    repo.insert("party_domain", {"domain": "pelican.test", "party_id": p["pelican"]})
    repo.insert("party_domain", {"domain": "corvidfreight.test", "party_id": p["corvid"]})
    lane = {"origin_country": "USA", "dest_country": "Chile", "dest_loc": "SAI", "service_mode": "FCL"}
    c = {"tbc": add_enquiry(repo, "OPP-9310", p["tbc"], received="2026-10-20", conversation_id="CONV-9310",
                            source_subject="RATE REQUEST // DALLAS - SANTOS",
                            thread_key=parsing.subject_thread_key("RATE REQUEST // DALLAS - SANTOS"), **lane),
         "same": add_enquiry(repo, "OPP-9311", p["finch"], received="2026-10-25", **lane),
         "edge": add_enquiry(repo, "OPP-9312", p["kite"], received="2026-10-30", **lane),
         "other_vendor": add_enquiry(repo, "OPP-9313", p["wren"], received="2026-10-22", **lane),
         "too_late": add_enquiry(repo, "OPP-9314", p["tern"], received="2026-10-31", **lane)}
    for case, vendor in (("tbc", "pelican"), ("same", "pelican"), ("edge", "pelican"),
                         ("other_vendor", "gull"), ("too_late", "pelican")):
        repo.insert("vendor_request", {"enquiry_id": c[case], "vendor_party_id": p[vendor], "asked_on": "2026-10-20"})
    repo.commit()
    conn.close()
    return create_app(path).test_client(), path, p, c


def _candidates(path, case):
    conn = db.open_db(path)
    try:
        row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (case,)).fetchone()
        return who_asked.candidates(conn, row)
    finally:
        conn.close()


def test_who_asked_reads_the_case_s_mails_without_its_vendors(tbc):
    _client, path, p, c = tbc
    found = [x for x in _candidates(path, c["tbc"]) if x.kind == "mail"]
    assert [(x.party_key, x.address) for x in found] == [("Corvid Freight", "mara@corvidfreight.test"),
                                                         ("", "lio@marlowcargo.test")]
    copied, forwarded = found
    assert copied.party_id == p["corvid"]
    assert copied.evidence == ("2026-10-19, ana@jojofreight.example: RATE REQUEST // DALLAS - SANTOS "
                               "(copied: mara@corvidfreight.test)")
    assert forwarded.party_id is None and "(forwarded from lio@marlowcargo.test)" in forwarded.evidence
    assert forwarded.suggested_key                                            # Add party starts with a name
    assert all("pelican" not in x.address for x in found)


def test_the_same_shipment_needs_a_shared_vendor_and_ten_days(tbc):
    _client, path, p, c = tbc
    found = [x for x in _candidates(path, c["tbc"]) if x.kind == "shipment"]
    assert [(x.other_opp, x.party_key) for x in found] == [("OPP-9311", "Harbor Finch"), ("OPP-9312", "Kite Shipping")]
    assert found[0].evidence == "same shipment as OPP-9311, requester Harbor Finch"


def test_a_case_with_a_known_requester_has_no_candidates(tbc):
    _client, path, p, c = tbc
    assert _candidates(path, c["same"]) == []


def test_the_case_page_and_tracking_show_who_asked(tbc):
    client, path, p, c = tbc
    page = client.get("/enquiries/{}".format(c["tbc"])).get_data(as_text=True)
    assert "Who asked?" in page and "same shipment as OPP-9311, requester Harbor Finch" in page
    assert "Duplicate of OPP-9311" in page and "Use as requester" in page and "Add party" in page
    tracking = client.get("/tracking").get_data(as_text=True)
    assert 'id="who-asked-{}"'.format(c["tbc"]) in tracking
    assert "same shipment as OPP-9311, requester Harbor Finch" in tracking


def test_use_as_requester_writes_the_requester_and_the_note(tbc):
    client, path, p, c = tbc
    evidence = "same shipment as OPP-9311, requester Harbor Finch"
    client.post("/enquiries/{}/who-asked/use".format(c["tbc"]), data={"party_id": p["finch"], "evidence": evidence})
    conn = db.open_db(path)
    assert conn.execute("SELECT requester_party_id FROM enquiry WHERE id = ?", (c["tbc"],)).fetchone()[0] == p["finch"]
    notes = [tuple(r) for r in conn.execute("SELECT note, source FROM status_event WHERE enquiry_id = ? "
                                            "AND kind = 'note'", (c["tbc"],))]
    assert ("Requester from mail: Harbor Finch ({})".format(evidence), "manual") in notes
    # through the repository, as the case page's own save (change_log source ui)
    assert conn.execute("SELECT source FROM change_log WHERE table_name = 'enquiry' AND row_id = ? "
                        "AND field = 'requester_party_id'", (c["tbc"],)).fetchone()[0] == "ui"
    conn.close()
    assert _candidates(path, c["tbc"]) == []                  # no longer (TBC)


def test_use_as_requester_leaves_scope_alone(tbc):
    # SC1: a requester changing from (TBC) never sets scope; the report rule
    # leaves the not-LATAM direct customer out by itself
    client, path, p, c = tbc
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    shop = add_party(repo, "Osprey Outfitters", ptype="Direct Customer", country="USA")
    repo.update("enquiry", c["tbc"], {"dest_country": "USA"})
    repo.commit()
    conn.close()
    client.post("/enquiries/{}/who-asked/use".format(c["tbc"]), data={"party_id": shop, "evidence": "x"})
    conn = db.open_db(path)
    assert conn.execute("SELECT scope FROM enquiry WHERE id = ?", (c["tbc"],)).fetchone()[0] == "In"
    assert dco.reason_of(conn, c["tbc"]) in (dco.NOT_LATAM, dco.BEFORE)
    conn.close()


def test_duplicate_of_drops_the_case_with_that_reason(tbc):
    client, path, p, c = tbc
    client.post("/enquiries/{}/who-asked/duplicate".format(c["tbc"]), data={"of_id": c["same"]})
    conn = db.open_db(path)
    row = conn.execute("SELECT status, closed_reason FROM enquiry WHERE id = ?", (c["tbc"],)).fetchone()
    assert tuple(row) == ("Dropped", "Duplicate of OPP-9311")
    conn.close()


def test_add_party_learns_the_domain_and_then_offers_the_party(tbc):
    client, path, p, c = tbc
    client.post("/enquiries/{}/who-asked/add-party".format(c["tbc"]),
                data={"key": "Marlow Cargo", "address": "lio@marlowcargo.test", "type": "Agent", "country": "Chile"})
    conn = db.open_db(path)
    party = conn.execute("SELECT id, type, country FROM party WHERE key = 'Marlow Cargo'").fetchone()
    assert tuple(party)[1:] == ("Agent", "Chile")
    assert conn.execute("SELECT party_id FROM party_domain WHERE domain = 'marlowcargo.test'").fetchone()[0] == party[0]
    # the requester is not set until Use as requester
    assert conn.execute("SELECT requester_party_id FROM enquiry WHERE id = ?", (c["tbc"],)).fetchone()[0] == p["tbc"]
    conn.close()
    assert ("Marlow Cargo", "lio@marlowcargo.test") in [(x.party_key, x.address) for x in _candidates(path, c["tbc"])]


def test_nothing_is_written_by_showing_the_box(tbc):
    client, path, p, c = tbc
    conn = db.open_db(path)
    before = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    conn.close()
    client.get("/enquiries/{}".format(c["tbc"]))
    client.get("/tracking")
    conn = db.open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == before
    conn.close()
