"""Batch Q2e (2026-11-17, schema v55): a vendor's options - several rates for
one route, each with its name - and a Read quote draft saved option by
option (INTERFACE.md section 13, with sections 9, 10 and 11).

A fake Firestore client (the team site and the Rate Card); no network.
Every company, person, amount and date here is invented: cases OPP-9201
upward, domains .test.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, firebase_pull, firebase_sync as fs, ratecard_sync, read_quote, schema_migrations, site_rates
from opplist import web as web_module
from opplist.web import create_app
from test_firebase_sync import FakeFirestore
from test_rq2 import SiteFake

TODAY = "2027-06-10"
AT = dt.datetime(2027, 6, 9, 15, 0, tzinfo=dt.timezone.utc)
LINES = [{"seg": "Freight", "charge": "Ocean freight", "unit": "40HQ", "rate": 1400, "min": None, "qty": 1,
          "amount": 1400, "word": None},
         {"seg": "Origin", "charge": "Pick up", "unit": "TRIP", "rate": 300, "min": None, "qty": 1, "amount": 300,
          "word": None}]


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "q2e.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    agent = add_party(repo, "Dunmere Cargo", country="Chile")
    harrow = add_party(repo, "Harrow Lines", ptype="Carrier", country="USA")
    quell = add_party(repo, "Quell Freight", country="USA")
    ids = {"path": path, "harrow": harrow, "quell": quell}
    for opp in ("OPP-9201", "OPP-9202"):
        ids[opp] = add_enquiry(repo, opp, agent, received="2027-06-01", scope="In", service_mode="FCL",
                               origin_country="USA", dest_country="Chile")
        for vendor in (harrow, quell):
            repo.insert("vendor_request", {"enquiry_id": ids[opp], "vendor_party_id": vendor,
                                           "asked_on": "2027-06-02", "source": "manual"})
    repo.commit()
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    yield {"conn": conn, "ids": ids, "path": path, "site": SiteFake(), "state": {}}
    conn.close()


def rates(w, action_id, total, label=None, vendor="Harrow Lines", opp="OPP-9201", cur="USD", valid="2027-06-30",
          at=AT, on=None, **extra):
    doc = {"opp": opp, "vendor": vendor, "cur": cur, "total": total, "lines": LINES, "valid": valid,
           "transit": "20 days", "carrier": "", "free": "", "note": "",
           "said": {"fuel": "incl", "local": "excl", "min": "none"}, "by": "nell@site.test", "name": "Nell", "at": at,
           "on": on}
    if label is not None:
        doc["label"] = label
    doc.update(extra)
    w["site"].docs["vendorRates/" + action_id] = doc
    action = {"type": "rates", "opp": opp, "by": "nell@site.test", "name": "Nell", "owner": "Nell", "app": "1117f",
              "at": at, "vendor": vendor, "cur": cur, "total": total, "valid": valid}
    if label is not None:
        action["label"] = label
    repo = db.Repository(w["conn"], source="web")
    ok, message = firebase_pull.take_in(repo, action_id, action, TODAY, client=w["site"])
    repo.commit()
    return ok, message


def card(w, opp="OPP-9201", vendor="harrow", today=TODAY):
    return site_rates.card(w["conn"], w["ids"][opp], today).get(w["ids"][vendor], [])


# ---------------------------------------------------------- 2, 3. options --
def test_two_labels_on_one_route_are_both_current_and_the_result_names_the_label(world):
    w = world
    assert rates(w, "rOpt0000000000000001", 2000, label="Truckload") == (
        True, "OPP-9201: rates from Harrow Lines, Truckload, kept (USD 2,000, valid until Jun 30).")
    assert rates(w, "rOpt0000000000000002", 1500, label="  Partial ")[1] == \
        "OPP-9201: rates from Harrow Lines, Partial, kept (USD 1,500, valid until Jun 30)."
    got = card(w)
    assert [(e["label"], e["total"], e["older"]) for e in got] == [("Truckload", "USD 2,000", 0),
                                                                   ("Partial", "USD 1,500", 0)]
    labels = [r[0] for r in w["conn"].execute("SELECT label FROM vendor_rate ORDER BY id")]
    assert labels == ["Truckload", "Partial"]
    # the case page: each option on a line of its own, its label first
    app = create_app(w["path"])
    app.config["TESTING"] = True
    page = app.test_client().get("/enquiries/{}".format(w["ids"]["OPP-9201"])).data.decode("utf-8")
    assert page.count('<span class="rates-label">Truckload</span><span class="rates-total">USD 2,000') == 1
    assert page.count('<span class="rates-label">Partial</span><span class="rates-total">USD 1,500') == 1


def test_the_same_label_is_a_new_version_of_that_option(world):
    w = world
    rates(w, "rOpt0000000000000001", 2000, label="Truckload")
    rates(w, "rOpt0000000000000002", 1500, label="Partial")
    # a correction the same day, the label typed differently
    rates(w, "rOpt0000000000000003", 1450, label="partial  ", at=AT + dt.timedelta(hours=1))
    got = card(w)
    assert [(e["label"], e["total"], e["older"]) for e in got] == [("Truckload", "USD 2,000", 0),
                                                                   ("partial", "USD 1,450", 1)]
    # on the Rate Card: one quote per option, the correction its rev 2
    docs = ratecard_sync.build(w["conn"], {})
    assert docs["OPP-9201_A_harrow-lines_partial_1"]["rev"] == 2
    assert docs["OPP-9201_A_harrow-lines_partial_1"]["total"] == 1450
    assert docs["OPP-9201_A_harrow-lines_truckload_1"]["rev"] == 1


def test_one_set_reads_as_before_and_a_label_over_60_is_refused(world):
    w = world
    assert rates(w, "rOne0000000000000001", 1900) == (
        True, "OPP-9201: rates from Harrow Lines kept (USD 1,900, valid until Jun 30).")
    rates(w, "rOne0000000000000002", 1850, label="", at=AT + dt.timedelta(hours=1))
    got = card(w)
    assert [(e["label"], e["total"], e["older"]) for e in got] == [("", "USD 1,850", 1)]
    ok, message = rates(w, "rLong000000000000001", 1700, label="x" * 61)
    assert ok is False and "the option's name is 61 characters, more than 60" in message
    assert w["conn"].execute("SELECT COUNT(*) FROM vendor_rate").fetchone()[0] == 2


def test_rows_before_v55_read_as_one_set(tmp_path):
    path = tmp_path / "old.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    p = add_party(repo, "Harrow Lines", ptype="Carrier", country="USA")
    e = add_enquiry(repo, "OPP-9203", p, received="2027-06-01", scope="In")
    for n in range(2):
        repo.insert("vendor_rate", {"enquiry_id": e, "vendor_party_id": p, "site_id": "v{}".format(n), "cur": "USD",
                                    "total": 100 + n, "at": "2027-06-0{}T10:00:00Z".format(n + 1)})
    q = repo.insert("quote_read", {"enquiry_id": e, "vendor_party_id": p, "source": "paste", "status": "read"})
    three = json.dumps({"options": [{"label": "A"}, {"label": "B"}, {"label": "C"}]})
    for n, (status, option) in enumerate((("open", None), ("saved", 1), ("discarded", None), ("expired", None))):
        repo.insert("quote_read_draft", {"quote_read_id": q, "site_id": "rq{}-{}".format(q, n), "draft": three,
                                         "status": status, "option": option})
    repo.commit()
    # back to v54: without label and taken
    conn.execute("ALTER TABLE vendor_rate DROP COLUMN label")
    conn.execute("ALTER TABLE quote_read_draft DROP COLUMN taken")
    conn.execute("UPDATE setting SET value = '54' WHERE key = 'schema_version'")
    conn.commit()
    notes = schema_migrations.upgrade(conn)
    assert "vendor_rate.label added (2 versions on file, each '')" in notes
    assert "vendor_rate rows before and after: 2 / 2" in notes
    assert "quote_read_draft rows before and after: 4 / 4; taken filled: discarded 1, expired 1, open 1, saved 1" in notes
    assert "foreign_key_check: nothing" in notes
    assert [r[0] for r in conn.execute("SELECT label FROM vendor_rate ORDER BY id")] == ["", ""]
    assert [json.loads(r[0]) for r in conn.execute("SELECT taken FROM quote_read_draft ORDER BY id")] == [
        ["", "", ""], ["", "saved", ""], ["skipped"] * 3, ["", "", ""]]
    assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION
    assert schema_migrations.upgrade(conn) == []                    # twice: nothing
    assert [(x["label"], x["older"]) for x in site_rates.card(conn, e, TODAY)[p]] == [("", 1)]
    conn.close()


# --------------------------------------------------------- 3, 4, 5. drafts --
def draft(w, labels=("HAPAG", "MAERSK SPOT", "ONE SPOT"), opp="OPP-9201", vendor="harrow"):
    repo = db.Repository(w["conn"], source="test")
    q = repo.insert("quote_read", {"enquiry_id": w["ids"][opp], "vendor_party_id": w["ids"][vendor],
                                   "source": "paste", "status": "read", "by_name": "Iris"})
    route = w["conn"].execute("SELECT id FROM enquiry_route WHERE enquiry_id = ?", (w["ids"][opp],)).fetchone()[0]
    options = [{"label": label, "cur": "USD", "lines": LINES, "valid": "2027-06-30", "transit": "", "carrier": "",
                "free": "", "said": {"fuel": "incl", "local": "excl", "min": "none"}, "note": ""} for label in labels]
    doc = {"schema": 1, "opp": opp, "vendor": "Harrow Lines", "route": 1, "on": "2027-06-08", "options": options,
           "warnings": [], "source": {"kind": "paste", "subject": "", "sent": "2027-06-08", "files": []},
           "readBy": "Iris"}
    site_id = "rq{}-1".format(q)
    repo.insert("quote_read_draft", {"quote_read_id": q, "site_id": site_id, "route_id": route,
                                     "draft": json.dumps(doc), "status": "open",
                                     "taken": json.dumps([""] * len(labels))})
    repo.commit()
    return site_id


def save(w, action_id, site_id, option, total, label, minutes=0):
    return rates(w, action_id, total, label=label, at=AT + dt.timedelta(minutes=minutes), draft=site_id,
                 option=option, how="read")


def discard(w, action_id, site_id, option=None):
    action = {"type": "discard", "opp": "OPP-9201", "by": "nell@site.test", "name": "Nell", "owner": "Nell",
              "app": "1117f", "at": AT, "draft": site_id}
    if option is not None:
        action["option"] = option
    repo = db.Repository(w["conn"], source="web")
    ok, message = firebase_pull.take_in(repo, action_id, action, TODAY, client=w["site"])
    repo.commit()
    return ok, message


def state(w, site_id):
    row = w["conn"].execute("SELECT d.*, v.site_id AS saved_as FROM quote_read_draft d "
                            "LEFT JOIN vendor_rate v ON v.id = d.vendor_rate_id WHERE d.site_id = ?",
                            (site_id,)).fetchone()
    return row["status"], read_quote.taken_of(row), row["saved_as"]


def test_a_draft_of_three_save_0_skip_1_save_2_is_saved(world):
    w = world
    site_id = draft(w)
    assert save(w, "rDraft00000000000001", site_id, 0, 1400, "HAPAG")[0] is True
    assert state(w, site_id) == ("open", ["saved", "", ""], "rDraft00000000000001")
    assert discard(w, "dSkip000000000000001", site_id, option=1) == (
        True, "OPP-9201: option 2 of the reading of Harrow Lines's mail was skipped.")
    assert state(w, site_id) == ("open", ["saved", "skipped", ""], "rDraft00000000000001")
    save(w, "rDraft00000000000002", site_id, 2, 1350, "ONE SPOT", minutes=5)
    assert state(w, site_id) == ("saved", ["saved", "skipped", "saved"], "rDraft00000000000001")
    kept = [tuple(r) for r in w["conn"].execute("SELECT label, how, draft_id FROM vendor_rate ORDER BY id")]
    assert kept == [("HAPAG", "read", site_id), ("ONE SPOT", "read", site_id)]
    # both options are the vendor's rates now
    assert [e["label"] for e in card(w)] == ["HAPAG", "ONE SPOT"]
    # another save of a saved option is a correction; the draft stays as it is
    save(w, "rDraft00000000000003", site_id, 0, 1390, "HAPAG", minutes=9)
    assert state(w, site_id) == ("saved", ["saved", "skipped", "saved"], "rDraft00000000000001")
    # the push carries taken
    doc = read_quote.draft_docs(w["conn"])["rateDrafts/" + site_id]
    assert (doc["status"], doc["taken"], doc["savedAs"]) == ("saved", ["saved", "skipped", "saved"],
                                                             "rDraft00000000000001")


def test_a_discard_without_option_after_one_save_skips_the_rest(world):
    w = world
    site_id = draft(w)
    save(w, "rDraft00000000000001", site_id, 1, 1500, "MAERSK SPOT")
    assert discard(w, "dRest000000000000001", site_id) == (
        True, "OPP-9201: the rest of the reading of Harrow Lines's mail was set aside.")
    assert state(w, site_id) == ("saved", ["skipped", "saved", "skipped"], "rDraft00000000000001")


def test_a_discard_of_an_option_already_taken_is_refused(world):
    w = world
    site_id = draft(w)
    save(w, "rDraft00000000000001", site_id, 0, 1400, "HAPAG")
    assert discard(w, "dTwice00000000000001", site_id, option=0) == (False, "That option was already saved or skipped.")
    discard(w, "dSkip000000000000001", site_id, option=1)
    assert discard(w, "dTwice00000000000002", site_id, option=1) == (False, "That option was already saved or skipped.")
    discard(w, "dSkip000000000000002", site_id, option=2)
    assert state(w, site_id)[0] == "saved"
    # once every option is taken, any option is refused the same way
    assert discard(w, "dTwice00000000000003", site_id, option=2) == (False, "That option was already saved or skipped.")


def test_a_discard_with_nothing_saved_is_discarded(world):
    w = world
    site_id = draft(w)
    assert discard(w, "dAll0000000000000001", site_id) == (
        True, "OPP-9201: the reading of Harrow Lines's mail was set aside.")
    assert state(w, site_id) == ("discarded", ["skipped"] * 3, None)
    # skipping each option one by one ends the same way
    other = draft(w, labels=("Truckload", "Partial"))
    discard(w, "dEach000000000000001", other, option=0)
    assert discard(w, "dEach000000000000002", other, option=1)[1] == \
        "OPP-9201: option 2 of the reading of Harrow Lines's mail was skipped; the reading is set aside."
    assert state(w, other) == ("discarded", ["skipped", "skipped"], None)


def test_one_option_draft_saves_and_closes_as_before(world):
    w = world
    site_id = draft(w, labels=("",))
    save(w, "rDraft00000000000001", site_id, 0, 1400, "")
    assert state(w, site_id) == ("saved", ["saved"], "rDraft00000000000001")


# --------------------------------------------------------------- 6. Lowest --
def test_lowest_across_options_a_tie_a_past_one_and_two_currencies(world):
    w = world
    rates(w, "rLow0000000000000001", 2000, label="Truckload")
    rates(w, "rLow0000000000000002", 1500, label="Partial")
    rates(w, "rLow0000000000000003", 1800, vendor="Quell Freight")
    lowest = {(e["label"], e["total"]): e["lowest"] for e in card(w) + card(w, vendor="quell")}
    assert lowest == {("Truckload", "USD 2,000"): False, ("Partial", "USD 1,500"): True, ("", "USD 1,800"): False}
    # a tie marks each
    rates(w, "rLow0000000000000004", 1500, vendor="Quell Freight", at=AT + dt.timedelta(hours=1))
    lowest = {(e["label"], e["total"]): e["lowest"] for e in card(w) + card(w, vendor="quell")}
    assert lowest == {("Truckload", "USD 2,000"): False, ("Partial", "USD 1,500"): True, ("", "USD 1,500"): True}
    # a past valid until is left out, however low
    rates(w, "rLow0000000000000005", 900, label="Old spot", valid="2027-06-09")
    got = {e["label"]: e for e in card(w)}
    assert got["Old spot"]["past"] is True and got["Old spot"]["lowest"] is False and got["Partial"]["lowest"] is True
    # another currency is apart: alone in EUR, so not Lowest
    rates(w, "rLow0000000000000006", 1000, label="Euro", cur="EUR")
    got = {e["label"]: e for e in card(w)}
    assert got["Euro"]["lowest"] is False and got["Partial"]["lowest"] is True
    rates(w, "rLow0000000000000007", 1100, cur="EUR", vendor="Quell Freight", label="Euro too")
    got = {e["label"]: e for e in card(w) + card(w, vendor="quell")}
    assert got["Euro"]["lowest"] is True and got["Euro too"]["lowest"] is False


# ------------------------------------------------------------ 7. Rate Card --
def test_the_rate_card_ids_routing_and_used_on_won(world):
    w = world
    rates(w, "rCard000000000000001", 1900, vendor="Quell Freight")
    rates(w, "rCard000000000000002", 2000, label="Truckload")
    rates(w, "rCard000000000000003", 1500, label="Partial / LTL")
    # a new day is a new quote of that option only
    rates(w, "rCard000000000000004", 1450, label="Partial / LTL", on=TODAY, at=AT + dt.timedelta(days=1))
    repo = db.Repository(w["conn"], source="ui")
    repo.set_status(w["ids"]["OPP-9201"], "Won", TODAY)
    repo.update("enquiry", w["ids"]["OPP-9201"], {"awarded_vendor_id": w["ids"]["harrow"]})
    repo.commit()
    docs = ratecard_sync.build(w["conn"], {})
    assert sorted(docs) == ["OPP-9201_A_harrow-lines_partial-ltl_1", "OPP-9201_A_harrow-lines_partial-ltl_2",
                            "OPP-9201_A_harrow-lines_truckload_1", "OPP-9201_A_quell-freight_1"]
    one = docs["OPP-9201_A_quell-freight_1"]
    assert (one["routing"], one["usedOnWon"], one["seq"]) == ("", False, 1)
    assert docs["OPP-9201_A_harrow-lines_truckload_1"]["routing"] == "Truckload"
    assert docs["OPP-9201_A_harrow-lines_partial-ltl_2"]["routing"] == "Partial / LTL"
    # every current option of the awarded vendor: the newest quote of each
    used = {k: d["usedOnWon"] for k, d in docs.items()}
    assert used == {"OPP-9201_A_harrow-lines_partial-ltl_1": False, "OPP-9201_A_harrow-lines_partial-ltl_2": True,
                    "OPP-9201_A_harrow-lines_truckload_1": True, "OPP-9201_A_quell-freight_1": False}
    # pushed, and the ids kept in the state
    client = FakeFirestore(project="demo-rate-card")
    st = {}
    assert ratecard_sync.push(w["conn"], client, st).written == 4
    assert "agentQuotes/OPP-9201_A_harrow-lines_truckload_1" in client.docs
    assert sorted(st["ids"].values()) == sorted(docs)


# ------------------------------------------------------------- 8. meta/sync --
def test_meta_sync_carries_options_and_the_drafts_are_pushed_with_taken(world):
    w = world
    site_id = draft(w)
    fs.sync_once(w["conn"], w["site"], w["state"], today=TODAY, reports_dir=w["path"].parent / "none")
    assert w["site"].docs["meta/sync"]["options"] is True
    assert w["site"].docs["rateDrafts/" + site_id]["taken"] == ["", "", ""]
