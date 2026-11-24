"""Batch L1: quoted cases with no reply for a month - Tracking's R11 card
by the agent's place, the Enquiries view, and reopening after a
No-response closing (the month report's section 12 went in 17a-2).

A test archive; every company, person, mail, place and date here is
invented; cases from OPP-5101.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, mail_archive, parsing, silent, suggestions, tracking
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app

TODAY = "2026-11-20"
US = ("desk@jojofreight.example", "JJC-DESK")


def mail(opp, at, sender, body, mid, internal=False, to=None):
    subject = "RATE REQUEST // {} // AIR".format(opp)
    return MailInfo(subject=("RE: " if internal else "") + subject, received_on=at[:10], received_at=at,
                    entry_id="EID-" + mid, conversation_id="CONV-" + opp, conversation_topic=parsing.clean_subject(subject),
                    sender_name=sender[1], sender_address=sender[0], sender_internal=internal,
                    to_addresses=[to or US[0]], body_excerpt=body, folder_path="\\Mailbox\\Inbox",
                    message_id="<{}@x>".format(mid))


@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    path = tmp_path / "l1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    for name in ("Peru",):
        repo.insert("country", {"name": name, "region": "Latin America"})
    parties = {}
    for key, name, country, city, domain, ptype in (
            ("heron", "Heronfield Cargo", "Chile", "Santiago", "heronfield.example", "Agent"),
            ("lima", "Limeña Air Agents", "Peru", "Lima", "limena.example", "Agent"),
            ("puno", "Puno Freight", "Peru", None, "punofreight.example", "Agent"),
            ("mill", "Millbrook Mills", "USA", None, "millbrook.example", "Direct Customer")):
        parties[key] = add_party(repo, name, ptype=ptype, country=country)
        if city:
            repo.update("party", parties[key], {"city": city})
        repo.insert("party_domain", {"domain": domain, "party_id": parties[key]})
    agent = {k: ("ops@" + d, n) for k, n, d in (("heron", "Ana Heron", "heronfield.example"),
                                                ("lima", "Luz Lima", "limena.example"),
                                                ("puno", "Pia Puno", "punofreight.example"),
                                                ("mill", "Max Mill", "millbrook.example"))}
    ids, mails = {}, []

    def case(opp, who, quoted, **extra):
        ids[opp] = add_enquiry(repo, opp, parties[who], received=quoted, source_subject="RATE REQUEST // {} // AIR".format(opp),
                               conversation_id="CONV-" + opp, service_mode="Air", origin_country="USA",
                               dest_country="Chile", **extra)
        repo.set_status(ids[opp], "Quoted", quoted)
        mails.append(mail(opp, quoted + " 08:00", agent[who], "Hi,\nPlease quote air.", "i" + opp))
        mails.append(mail(opp, quoted + " 15:00", US, "Hi,\nOur rate is USD 980.", "q" + opp, internal=True,
                          to=agent[who][0]))

    case("OPP-5101", "heron", "2026-10-15")                               # silent 36 days
    case("OPP-5102", "heron", "2026-10-15")                               # they wrote on day 20
    mails.append(mail("OPP-5102", "2026-11-04 10:00", agent["heron"], "Hi,\nStill with the client.", "rOPP-5102"))
    case("OPP-5103", "heron", "2026-10-10")                               # our follow-up on Nov 5
    repo.add_followup(ids["OPP-5103"], "2026-11-05")
    mails.append(mail("OPP-5103", "2026-11-05 09:00", US, "Hi,\nJust checking back on our quote.", "fOPP-5103",
                      internal=True, to=agent["heron"][0]))
    case("OPP-5104", "mill", "2026-10-10")                                # a direct customer
    case("OPP-5105", "heron", "2026-10-10")                               # held until December
    repo.update("enquiry", ids["OPP-5105"], {"hold_reason": "waiting for the factory", "hold_until": "2026-12-01"})
    case("OPP-5106", "lima", "2026-10-01")                                # silent, Peru / Lima
    case("OPP-5107", "puno", "2026-10-05")                                # silent, Peru / no city
    case("OPP-5108", "heron", "2026-11-01")                               # 19 days: not yet
    # closed for no response on Nov 10; they wrote on Nov 15
    case("OPP-5109", "lima", "2026-09-20")
    repo.set_status(ids["OPP-5109"], "Lost", "2026-11-10", closed_reason=silent.NO_RESPONSE)
    mails.append(mail("OPP-5109", "2026-11-15 11:00", agent["lima"], "Hi,\nThe client is ready now, is the rate valid?",
                      "rOPP-5109"))
    repo.commit()
    aconn = mail_archive.open_archive(archive)
    mail_archive.store(aconn, [mail_archive.record_from_info(m) for m in mails])
    mail_archive.set_meta(aconn, "last_sync_at", TODAY + " 07:00:00")
    aconn.commit()
    aconn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    yield {"conn": conn, "repo": repo, "ids": ids, "archive": archive, "web": app.test_client(), "path": path}
    conn.close()


def silent_now(w, days=None):
    with mail_archive.ArchiveSearch(w["archive"]) as search:
        return [c["row"]["opp_no"] for c in silent.silent_cases(w["conn"], search, TODAY, days)]


def test_which_cases_are_silent(world):
    w = world
    # L1b: the clock runs from the quote date; our follow-up of Nov 5 on
    # OPP-5103 (quoted Oct 10) does not move it
    assert silent_now(w) == ["OPP-5101", "OPP-5103", "OPP-5106", "OPP-5107"]
    assert "OPP-5108" not in silent_now(w, days=20) and "OPP-5108" in silent_now(w, days=19)


def test_the_card_groups_by_country_and_city_with_counts(world):
    w = world
    model = tracking.page_model(w["conn"], TODAY)
    card = next(c for c in model["today_cards"] if c["key"] == "silent")
    assert card["title"] == "No reply for 30+ days" and card["folded"]
    assert [(g["label"], g["count"]) for g in card["groups"]] == [
        ("Chile · Santiago", 2), ("Peru · Lima", 1), ("Peru · no city", 1)]
    first, second = card["rows"][:2]
    # days from the quote; the follow-up still shows in the step cell
    assert (first["opp"], first["days"], first["since"], first["pill"], first["sub"]) ==         ("OPP-5103", 41, "2026-10-10", "quoted Oct 10", "1 FU, last Nov 5")
    assert (second["opp"], second["days"], second["sub"]) == ("OPP-5101", 36, "no FU sent")
    tile = next(t for t in model["tiles"] if t["key"] == "silent")
    assert (tile["label"], tile["count"]) == ("No reply 30+ days", 4)
    # the Decide card keeps its own rows; FX4: it is hidden by default, back when unticked
    assert not any(c["key"] == "decide" for c in model["today_cards"])
    db.set_setting(w["conn"], tracking.HIDDEN_SETTING, "")
    w["conn"].commit()
    shown = tracking.page_model(w["conn"], TODAY)
    assert any(c["key"] == "decide" for c in shown["today_cards"])
    assert [g["label"] for g in next(c for c in shown["today_cards"] if c["key"] == "silent")["groups"]] == \
        ["Chile · Santiago", "Peru · Lima", "Peru · no city"]


def test_lost_from_the_row_with_the_reason_chosen_and_keep(world, monkeypatch):
    w = world
    from opplist import web as web_module
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    page = w["web"].get("/tracking").data.decode("utf-8")
    assert '<option value="No response from agent" selected>' in page         # L2: Heronfield is an agent
    assert "Chile · Santiago" in page and '>Keep</button>' in page
    w["web"].post("/tracking/act", data={"do": "quick:Lost", "rule": "R11", "kind": "enquiry",
                                         "ref_id": w["ids"]["OPP-5101"], "enquiry_id": w["ids"]["OPP-5101"],
                                         "closed_reason": "No response from agent", "at": TODAY})
    row = w["conn"].execute("SELECT status, closed_reason FROM v_enquiry WHERE id = ?", (w["ids"]["OPP-5101"],)).fetchone()
    assert tuple(row) == ("Lost", "No response from agent")
    w["web"].post("/tracking/act", data={"do": "keep", "rule": "R11", "kind": "enquiry",
                                         "ref_id": w["ids"]["OPP-5106"], "enquiry_id": w["ids"]["OPP-5106"], "at": TODAY})
    held = w["conn"].execute("SELECT hold_reason, hold_until FROM enquiry WHERE id = ?", (w["ids"]["OPP-5106"],)).fetchone()
    assert tuple(held) == ("Reviewed 2026-11-20, keep", "2026-12-20")
    assert silent_now(w) == ["OPP-5103", "OPP-5107"]


def test_the_enquiries_view_lists_them_with_their_country(world, monkeypatch):
    w = world
    monkeypatch.setattr(silent, "cached", lambda conn, today=None, days=None: [
        {"row": {"id": w["ids"][o]}} for o in ("OPP-5101", "OPP-5106", "OPP-5107")])
    page = w["web"].get("/enquiries?view=silent").data.decode("utf-8")
    rows = [line for line in page.splitlines() if 'class="enq-row"' in line]
    assert len(rows) == 3
    assert 'data-country="Chile"' in page and 'data-country="Peru"' in page
    assert "No reply 30+ days<b>3</b>" in page and 'data-group="country"' in page
    # the open view is unchanged
    assert len([l for l in w["web"].get("/enquiries?view=open").data.decode("utf-8").splitlines()
                if 'class="enq-row"' in l]) == 8


def test_a_reply_after_a_no_response_closing_suggests_reopening(world):
    w = world
    with mail_archive.ArchiveSearch(w["archive"]) as search:
        lines = silent.reopen_suggestions(w["conn"], search, today=TODAY, repo=db.Repository(w["conn"], source="scan"))
    assert lines == ["OPP-5109 closed 2026-11-10, they wrote 2026-11-15 - suggested"]
    row = w["conn"].execute("SELECT * FROM suggestion WHERE opp_no = 'OPP-5109'").fetchone()
    assert (row["kind"], row["proposed_status"], row["note"], row["proposed_date"]) == \
        ("status", "Quoted", silent.REOPEN_NOTE, "2026-11-15")
    suggestions.accept(db.Repository(w["conn"], source="ui"), row["id"])
    assert w["conn"].execute("SELECT status FROM enquiry WHERE opp_no = 'OPP-5109'").fetchone()[0] == "Quoted"
    events = [r[0] for r in w["conn"].execute("SELECT status FROM status_event WHERE enquiry_id = ? AND kind = 'status' "
                                              "AND voided = 0 ORDER BY at, id", (w["ids"]["OPP-5109"],))]
    assert events[-2:] == ["Lost", "Quoted"]                               # the closing stays in the timeline
    # nothing more for a case past 90 days
    assert silent.reopen_candidates(w["conn"], today="2027-03-01") == []


def test_the_cli_text_puts_the_focus_rules_first(tmp_path, monkeypatch):
    """Moved from test_tracking.py with invented data (L1)."""
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", tmp_path / "none" / "mail.db")
    conn = db.open_db(tmp_path / "cli.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Wrenhollow Agents", country="Chile")
    add_enquiry(repo, "OPP-5121", party, received="2026-09-01")                              # R1
    add_enquiry(repo, "OPP-5122", party, received="2026-09-01", status_confirmed=0)          # R4
    quoted = add_enquiry(repo, "OPP-5123", party, status="Quoted", received="2026-08-01")
    repo.insert("status_event", {"enquiry_id": quoted, "at": "2026-08-20", "status": "Quoted"})
    repo.commit()
    lines = tracking.render(tracking.run(conn, today="2026-09-24"))
    heads = [line for line in lines if line.startswith("[")]
    # no mail archive: R11 cannot know who replied, so it lists nothing
    assert [h.split("]")[0] + "]" for h in heads] == ["[R1]", "[R3]", "[R4]"]
    assert lines[0] == "-- focus: R1 and R3 --"
    assert "-- backlog, a few at a time --" in lines
    assert lines[-1] == "3 item(s) in 3 section(s)"
    conn.close()
