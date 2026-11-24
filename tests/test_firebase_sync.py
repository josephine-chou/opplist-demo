"""Batch W1a (2026-10-27): OppList pushes its lists to the team site
(Firebase), push only - the documents of OppListWeb's INTERFACE.md, only
what changed, meta/sync last, a case that leaves the list written once as
gone, never in the tests and never over a copy.

Every company, person, mail, place and date here is invented; cases from
OPP-7101.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, firebase_sync as fs, paths
from opplist.web import create_app

TODAY = "2026-12-14"


def asks(who, words, mid):
    return "Reply from {} (needs an answer): {} [mid:<{}@x>]".format(who, words, mid)


class FakeFirestore:
    """The three methods of FirestoreClient, kept in memory; every commit
    is kept in order so a test can read what went in which."""

    def __init__(self, project="demo-opplist", fail_on=None):
        self.project = project
        self.docs = {}
        self.commits = []
        self.fail_on = fail_on

    def commit(self, writes):
        if self.fail_on is not None and len(self.commits) == self.fail_on:
            raise fs.FirebaseError("Firebase said 503: try later")
        self.commits.append([w.path for w in writes])
        for w in writes:
            doc = json.loads(json.dumps(w.doc, default=str))       # as it would come back
            for name in w.server_time:
                doc[name] = "REQUEST_TIME"
            self.docs[w.path] = doc
        return {}

    def get(self, path):
        return self.docs.get(path)

    def run_query(self, query):
        return {"docs": [], "readTime": ""}


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "team.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "owners", ["Ana", "Ben", "Cara"])
    kestrel = add_party(repo, "Kestrel Forwarding", country="Chile")
    pine = add_party(repo, "Pinewood Goods", ptype="Direct Customer")
    ids = {}

    def case(opp, status="New", received="2026-12-01", party=kestrel, **extra):
        extra.setdefault("scope", "In")
        ids[opp] = add_enquiry(repo, opp, party, received=received, origin_loc="SAI", dest_loc="MIA",
                               service_mode="Air", air_weight_kg=320.0,
                               source_subject="RE: [{}] RATE REQUEST // SAI - MIA // Air".format(opp), **extra)
        if status != "New":
            repo.set_status(ids[opp], status, "2026-12-03")
        return ids[opp]

    case("OPP-7101", owner="Ben")                                                 # waiting, assigned
    case("OPP-7102", received="2026-12-11")                                       # waiting, not assigned
    case("OPP-7103", "Quoted")                                                    # FU1 due
    case("OPP-7104", "Quoted")
    repo.add_note(ids["OPP-7104"], "2026-12-10", asks("Rosa Vell", "Could you add the THC at USD 85?", "r7104"),
                  source="manual")                     # pending on us (FX4: logged by hand on the case page)
    case("OPP-7105", "Lost")
    case("OPP-7106", "Quoted", party=pine)                                        # a direct customer's quote
    case("OPP-7107", "Quoted", handled_by="YUL")                                  # another office's case
    case("OPP-7108", "Dropped")
    repo.update("enquiry", ids["OPP-7108"], {"closed_reason": "Registered in error"})
    case("OPP-7109", scope="Out")                                                 # out of scope
    case("OPP-7110", "Won", legacy=1)                                             # legacy: listed
    repo.commit()
    yield {"conn": conn, "repo": repo, "path": path, "ids": ids, "tmp": tmp_path, "state": tmp_path / "fs.json",
           "record": tmp_path / "exports.json", "reports": tmp_path / "reports"}
    conn.close()


def run(w, fake, **kw):
    kw.setdefault("today", TODAY)
    return fs.sync(w["path"], client=fake, state_file=w["state"], reports_dir=w["reports"], record=w["record"], **kw)


# ------------------------------------------------- Firestore's values --
def test_values_go_to_firestore_types_and_back():
    moment = dt.datetime(2026, 12, 14, 15, 30, 5, tzinfo=dt.timezone.utc)
    doc = {"s": "Kestrel", "n": 7, "x": 2.5, "yes": True, "none": None, "at": moment,
           "list": [1, "a", {"deep": False}], "map": {"opp": "OPP-7101", "gone": True}}
    fields = fs.to_fields(doc)
    assert fields["n"] == {"integerValue": "7"}
    assert fields["yes"] == {"booleanValue": True}             # a bool is not an integer
    assert fields["none"] == {"nullValue": None}
    assert fields["at"] == {"timestampValue": "2026-12-14T15:30:05.000000Z"}
    assert fields["list"]["arrayValue"]["values"][2] == {"mapValue": {"fields": {"deep": {"booleanValue": False}}}}
    assert fs.from_fields(fields) == doc
    # what Firestore sends back: nanoseconds, a 'Z'
    assert fs.from_value({"timestampValue": "2026-12-14T15:30:05.123456789Z"}) == \
        dt.datetime(2026, 12, 14, 15, 30, 5, 123456, tzinfo=dt.timezone.utc)


def local_store(tmp_path, clock=None):
    """The store this edition pushes to: a folder of JSON documents."""
    from opplist import local_store as ls
    return ls.LocalStore("demo-opplist", tmp_path / "team-site", clock=clock, error=fs.FirebaseError)


def test_the_store_calls_commit_get_and_query(tmp_path):
    moment = dt.datetime(2026, 12, 14, 15, 0, 0, tzinfo=dt.timezone.utc)
    client = local_store(tmp_path, clock=lambda: moment)
    client.commit([fs.Write("meta/sync", {"owners": ["Ana"]}, server_time=("pushedAt",))])
    # one document, one file, the values typed; the store sets the time itself
    stored = json.loads((tmp_path / "team-site" / "meta" / "sync.json").read_text(encoding="utf-8"))
    assert stored["fields"]["owners"] == {"arrayValue": {"values": [{"stringValue": "Ana"}]}}
    assert stored["fields"]["pushedAt"] == {"timestampValue": "2026-12-14T15:00:00.000000Z"}
    assert client.get("meta/sync") == {"owners": ["Ana"], "pushedAt": moment}
    client.commit([fs.Write("cases/OPP-7101", {"opp": "OPP-7101"})])
    assert client.get("cases/OPP-7101") == {"opp": "OPP-7101"}
    assert client.get("cases/OPP-7199") is None
    client.commit([fs.Write("actions/a1", {"type": "assign", "at": moment}),
                   fs.Write("actions/a0", {"type": "note", "at": moment - dt.timedelta(hours=2)})])
    found = client.run_query({"from": [{"collectionId": "actions"}]})
    assert found == {"docs": [("actions/a0", {"type": "note", "at": moment - dt.timedelta(hours=2)}),
                              ("actions/a1", {"type": "assign", "at": moment})],
                     "readTime": "2026-12-14T15:00:00.000000Z"}
    # the pull's query: newer than a moment, oldest first
    after = fs.to_value(moment - dt.timedelta(hours=1))
    found = client.run_query({"from": [{"collectionId": "actions"}],
                              "where": {"fieldFilter": {"field": {"fieldPath": "at"}, "op": "GREATER_THAN",
                                                        "value": after}},
                              "orderBy": [{"field": {"fieldPath": "at"}, "direction": "ASCENDING"}]})
    assert [path for path, _ in found["docs"]] == ["actions/a1"]
    client.commit([fs.Delete("actions/a0")])
    assert client.get("actions/a0") is None
    # a time the store sets inside a map: the field path is dotted
    client.commit([fs.Write("meta/sync", {"readQuote": {"on": True}}, server_time=("pushedAt", "readQuote.appAt"))])
    assert client.get("meta/sync") == {"readQuote": {"on": True, "appAt": moment}, "pushedAt": moment}


def test_an_error_from_the_store_is_a_short_message(tmp_path):
    (tmp_path / "team-site").write_text("a file where the folder should be", encoding="utf-8")
    client = local_store(tmp_path)
    with pytest.raises(fs.FirebaseError, match=r"^the local store could not write \(\w+\)$"):
        client.commit([fs.Write("lists/assign", {"waiting": []})])
    with pytest.raises(fs.FirebaseError, match="does not answer the comparison ARRAY_CONTAINS"):
        client.run_query({"from": [{"collectionId": "actions"}],
                          "where": {"fieldFilter": {"field": {"fieldPath": "tags"}, "op": "ARRAY_CONTAINS",
                                                    "value": {"stringValue": "x"}}}})


# ----------------------------------------------------------- settings --
def test_the_store_is_off_until_it_is_switched_on_and_holds_no_key(world, monkeypatch):
    conn = world["conn"]
    assert fs.configured(conn) == (False, "the team site's local store is off (Settings, Team site)")
    assert fs.service_account(conn) == ""
    with pytest.raises(fs.FirebaseError, match="local store is off"):
        fs.client_for(conn)
    db.set_setting(conn, fs.STORE_SETTING, "on")
    assert fs.configured(conn) == (True, "")
    assert fs.service_account(conn) == "local store in {}".format(paths.DATA_DIR / "team-site")
    client = fs.client_for(conn)
    assert client.project == "demo-opplist" and client.folder == paths.DATA_DIR / "team-site"
    db.set_setting(conn, "firebase_project", "another-store")
    assert fs.client_for(conn).project == "another-store"
    # nothing of a key anywhere: no setting for one, no file looked for
    assert not hasattr(fs, "key_path") and not (paths.DATA_DIR / "secrets").exists()


def test_only_the_live_database_pushes(world, monkeypatch):
    assert not fs.is_live(world["path"])                       # a copy, a test
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(paths, "LIVE_DB_PATH", world["path"])
    assert fs.is_live(world["path"])
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    assert not fs.is_live(world["path"])                       # never in the tests


def test_no_client_over_a_copy(world, monkeypatch):
    def no_client(conn):
        raise AssertionError("a client was made over a copy")
    monkeypatch.setattr(fs, "client_for", no_client)
    result = fs.sync(world["path"], state_file=world["state"])
    assert not result.ok and "not the live database" in result.reason
    assert not world["state"].exists()                         # the state file is left alone
    syncer = fs.Syncer(world["path"])
    assert not syncer.enabled()
    syncer.schedule()
    syncer.start()
    assert syncer._timer is None and syncer._loop is None


# ---------------------------------------------------------- the lists --
def test_the_documents_as_interface_md_has_them(world):
    w = world
    fake = FakeFirestore()
    result = run(w, fake)
    assert result.ok, result.reason
    assign = fake.docs["lists/assign"]
    assert [r["opp"] for r in assign["waiting"]] == ["OPP-7102", "OPP-7101"]      # not assigned first
    assert assign["waiting"][1] == {"opp": "OPP-7101", "req": "Kestrel Forwarding", "subj": "RATE REQUEST // SAI - MIA // Air",
                                    "rcv": "2026-12-01", "owner": "Ben", "hold": ""}
    assert assign["pending"] == [{"opp": "OPP-7104", "req": "Kestrel Forwarding",
                                  "subj": "RATE REQUEST // SAI - MIA // Air", "on": "2026-12-10",
                                  "what": "Rosa Vell: Could you add the THC at (price)?", "owner": "",
                                  "by": "Iris"}]
    rows = {r["opp"]: r for r in fake.docs["lists/inquiries"]["rows"]}
    # In, not registered in error, legacy included; Out left out
    assert sorted(rows) == ["OPP-7101", "OPP-7102", "OPP-7103", "OPP-7104", "OPP-7105", "OPP-7106", "OPP-7107",
                            "OPP-7110"]
    assert set(rows["OPP-7101"]) == {"opp", "rcv", "req", "rtype", "subj", "lane", "mode", "qty", "st", "next", "due",
                                     "fu", "since", "owner", "office", "hold", "last", "lastText", "quoted",
                                     "closed", "pend", "pendingClose", "wait", "latam", "oc", "dc"}
    # SC1: latam only on a LATAM case (Kestrel is in Chile), legacy only on a legacy row
    assert "latam" not in rows["OPP-7106"] and rows["OPP-7110"]["legacy"] is True and "legacy" not in rows["OPP-7101"]
    assert rows["OPP-7101"]["lane"] == "SAI - MIA" and rows["OPP-7101"]["qty"] == "320 kg"
    assert rows["OPP-7102"]["next"] == "Send the quote"
    quoted = rows["OPP-7103"]
    assert quoted["st"] == "Quoted" and quoted["quoted"] == "2026-12-03" and quoted["since"] == "2026-12-03"
    assert quoted["next"] == quoted["fu"] == "FU1" and quoted["due"] == "2026-12-04"
    assert rows["OPP-7104"]["pend"] == {"on": "2026-12-10", "what": "Rosa Vell: Could you add the THC at (price)?",
                                        "by": "Iris"}
    assert rows["OPP-7104"]["fu"] == ""                        # Replied is pend, not fu
    assert rows["OPP-7106"]["next"] == "No follow-ups (direct customer)" and rows["OPP-7106"]["due"] == ""
    assert rows["OPP-7107"]["next"] == "Handled by YUL" and rows["OPP-7107"]["office"] == "YUL"
    assert rows["OPP-7105"]["closed"] == "2026-12-03" and rows["OPP-7105"]["next"] == ""
    case = fake.docs["cases/OPP-7104"]
    assert case["subjFull"] == "RE: [OPP-7104] RATE REQUEST // SAI - MIA // Air"
    assert case["tl"][0] == {"at": "2026-12-10", "text": "Reply from Rosa Vell",
                             "sub": "Could you add the THC at (price)?", "src": "manual"}
    assert "cases/OPP-7109" not in fake.docs and "cases/OPP-7108" not in fake.docs
    meta = fake.docs["meta/sync"]
    assert fake.commits[-1] == ["meta/sync"]                   # last
    assert meta["pushedAt"] == "REQUEST_TIME" and meta["pulledAt"].startswith("1970-01-01")
    assert meta["owners"] == ["Ana", "Ben", "Cara"]
    assert meta["counts"] == {"inquiries": 8, "waiting": 2, "pending": 1, "cases": 8}
    # MR1: November (the default month on 14 Dec) is not exported: live
    [live] = meta["reports"]
    assert {k: v for k, v in live.items() if k != "updatedAt"} == {"month": "2026-11", "title": "NOVEMBER REPORT",
                                                                  "exportedAt": "", "live": True}
    assert live["updatedAt"] == fake.docs["reports/2026-11"]["updatedAt"] and "sync W3" in meta["version"]


def test_nothing_from_claude_no_mail_text_on_a_status_no_prices(world):
    w = world
    case = w["ids"]["OPP-7105"]
    w["repo"].add_note(case, "2026-12-04", "Read by Claude (information given): the client took Gullwing Air",
                       source="mail")
    w["repo"].add_note(case, "2026-12-05", "Called Rosa; they could do $1,250.00 all in, we were at EUR 1300")
    w["repo"].set_status(w["ids"]["OPP-7101"], "Quoted", "2026-12-06",
                         note="JJC-DESK <desk@jojofreight.example> Please find our offer below: USD 4.10/kg", source="mail")
    w["repo"].insert("case_note", {"enquiry_id": case, "body": "Target was 900 USD\nsecond line"})   # before CX1
    w["repo"].commit()
    fake = FakeFirestore()
    assert run(w, fake).ok
    tl = fake.docs["cases/OPP-7105"]["tl"]
    assert [e["text"] for e in tl] == ["Note", "Lost", "New"]  # the Claude note is not there
    assert tl[0]["sub"] == "Called Rosa; they could do (price) all in, we were at (price)"
    assert "qnotes" not in fake.docs["cases/OPP-7105"]                  # CX1: no quick notes go up
    quoted = fake.docs["cases/OPP-7101"]["tl"][0]
    assert quoted["text"] == "Quoted" and quoted["sub"] == ""   # a status from a mail shows no mail
    # (MR1: the live month report is the report page itself, as an exported one always was)
    blob = json.dumps({k: v for k, v in fake.docs.items() if not k.startswith("reports/")})
    assert "Claude" not in blob and "Gullwing" not in blob and "1,250" not in blob and "4.10" not in blob
    assert fake.docs["lists/inquiries"]["rows"]
    row = next(r for r in fake.docs["lists/inquiries"]["rows"] if r["opp"] == "OPP-7105")
    assert row["lastText"] == "Note Dec 5: Called Rosa; they could do (price) all in, we were at (price)"[:59] + "…"


def test_only_what_changed_is_written_and_a_case_that_leaves_is_gone_once(world):
    w = world
    fake = FakeFirestore()
    first = run(w, fake)
    assert first.written == 5 + 8 + 1 + 1                      # five lists (W2a: vendors, WA1: parties, D7b: places), eight cases, the live
    #                                                            month report (MR1), meta/sync
    second = run(w, fake)
    assert second.ok and second.written == 1 and fake.commits[-1] == ["meta/sync"]
    # one case closes; another leaves the list
    w["repo"].set_status(w["ids"]["OPP-7103"], "Lost", "2026-12-12")
    w["repo"].update("enquiry", w["ids"]["OPP-7102"], {"scope": "Out"})
    w["repo"].commit()
    third = run(w, fake)
    assert sorted(fake.commits[-2]) == ["cases/OPP-7102", "cases/OPP-7103", "lists/assign", "lists/inquiries"]
    assert fake.docs["cases/OPP-7102"] == {"opp": "OPP-7102", "gone": True}
    assert third.gone == 1
    fourth = run(w, fake)
    assert fourth.written == 1                                 # gone is written once
    assert "cases/OPP-7102" in fake.docs                       # never deleted


def test_commits_of_at_most_the_batch_and_a_failure_keeps_what_landed(world, monkeypatch):
    w = world
    monkeypatch.setattr(fs, "BATCH", 4)
    fake = FakeFirestore(fail_on=1)                            # the second commit fails
    result = run(w, fake)
    assert not result.ok and "503" in result.reason
    assert len(fake.commits) == 1 and len(fake.commits[0]) == 4
    state = json.loads(w["state"].read_text(encoding="utf-8"))
    assert len(state["hashes"]) == 4 and state["last_error"].endswith("try later")
    fake.fail_on = None
    again = run(w, fake)
    assert again.ok and again.written == 14 - 4 + 1            # the rest (MR1: with the live report; WA1: parties; D7b: places), then meta/sync
    assert all(len(c) <= 4 for c in fake.commits)


def test_a_document_over_the_limit_is_reported_not_written(world, monkeypatch):
    w = world
    monkeypatch.setattr(fs, "MAX_BYTES", 2000)
    fake = FakeFirestore()
    result = run(w, fake)
    assert result.ok and any(x.startswith("lists/inquiries (") for x in result.too_big)
    assert "lists/inquiries" not in fake.docs and "lists/inquiries" not in json.loads(
        w["state"].read_text(encoding="utf-8"))["hashes"]
    assert fs.status(w["state"])["too_big"] == result.too_big


# ------------------------------------------------------------ reports --
def test_a_month_report_is_pushed_when_new_or_changed(world):
    w = world
    w["reports"].mkdir()
    (w["reports"] / "NOVEMBER REPORT.html").write_text("<html>november</html>", encoding="utf-8")
    (w["reports"] / "NOVEMBER REPORT v2.html").write_text("<html>draft</html>", encoding="utf-8")
    fake = FakeFirestore()
    assert run(w, fake).ok
    # the file already there, recorded once by its title: the latest November up to today
    assert fake.docs["reports/2026-11"]["html"] == "<html>november</html>"
    assert fake.docs["reports/2026-11"]["title"] == "NOVEMBER REPORT"
    assert [r["month"] for r in fake.docs["meta/sync"]["reports"]] == ["2026-11"]
    # an export writes the record; the next run pushes the new HTML
    out = w["tmp"] / "out" / "DECEMBER REPORT.html"
    out.parent.mkdir()
    out.write_text("<html>december</html>", encoding="utf-8")
    fs.record_export("2026-12", "DECEMBER REPORT", out, when=dt.datetime(2026, 12, 14, 9, 5), record=w["record"])
    run(w, fake)
    assert fake.docs["reports/2026-12"] == {"month": "2026-12", "title": "DECEMBER REPORT",
                                           "html": "<html>december</html>", "exportedAt": "2026-12-14T09:05:00"}
    assert [r["month"] for r in fake.docs["meta/sync"]["reports"]] == ["2026-11", "2026-12"]
    assert run(w, fake).written == 1                           # unchanged: only meta/sync
    # the November file is replaced on disk: written again; the seeding is not done twice
    (w["reports"] / "NOVEMBER REPORT.html").write_text("<html>november, again</html>", encoding="utf-8")
    run(w, fake)
    assert fake.docs["reports/2026-11"]["html"] == "<html>november, again</html>"
    assert json.loads(w["record"].read_text(encoding="utf-8"))["seeded"] is True


def test_write_html_records_the_month(world, monkeypatch):
    from opplist import monthly_export

    class Report:
        month, title = "2026-11", "NOVEMBER REPORT"
    monkeypatch.setattr(monthly_export, "render_html", lambda report: "<html>made</html>")
    path = monthly_export.write_html(Report(), out_dir=world["tmp"] / "docs")
    record = json.loads((paths.DATA_DIR / "report-exports.json").read_text(encoding="utf-8"))
    assert record["months"]["2026-11"]["title"] == "NOVEMBER REPORT"
    assert record["months"]["2026-11"]["path"] == str(path.resolve())


# ----------------------------------------------------------- the app --
def test_settings_shows_the_team_site_box_and_sync_now_never_pushes_a_copy(world, monkeypatch):
    w = world
    pushed = []
    monkeypatch.setattr(fs, "sync", lambda *a, **k: pushed.append(a))
    app = create_app(w["path"])
    app.config["TESTING"] = True
    web = app.test_client()
    page = web.get("/settings").data.decode("utf-8")
    assert 'id="team-site"' in page and "Team site" in page and "local store is off" in page
    said = web.post("/settings/team-site/sync", follow_redirects=True).data.decode("utf-8")
    assert "not over the live database" in said
    # a write in the app schedules nothing over a copy
    web.post("/settings/team-site/sync")
    assert app.firebase_syncer._timer is None and pushed == []


# ---------------------------------------------------- the 08:15 run --
def test_the_command_not_set_up_is_one_line_and_exit_0(world, capsys):
    from opplist import __main__ as cli
    assert cli.main(["firebase-sync", "--db", str(world["path"])]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and out[0] == "team site: not set up - the team site's local store is off (Settings, Team site)"


def test_the_scheduled_run_goes_through_the_guard(world, monkeypatch, capsys):
    from opplist import __main__ as cli, scheduled
    recorded, ran = [], []
    monkeypatch.setattr(scheduled, "record", lambda *a, **k: recorded.append(a))
    monkeypatch.setattr(cli, "cmd_firebase_sync", lambda args: ran.append(args) or 0)
    parser_func = cli.build_parser
    monkeypatch.setattr(cli, "build_parser", lambda: _with_func(parser_func(), cli))
    monkeypatch.setattr(scheduled, "guard", lambda db_path, command: 75)         # uncommitted code: skipped
    assert cli.main(["firebase-sync", "--db", str(world["path"]), "--scheduled"]) == 75
    assert ran == []
    monkeypatch.setattr(scheduled, "guard", lambda db_path, command: None)       # goes ahead
    assert cli.main(["firebase-sync", "--db", str(world["path"]), "--scheduled"]) == 0
    assert len(ran) == 1 and recorded[-1][:2] == ("firebase-sync --db {}".format(world["path"]), "done")


def _with_func(parser, cli):
    """The parser with firebase-sync pointing at the patched command."""
    for action in parser._subparsers._group_actions:
        for name, sub in action.choices.items():
            if name == "firebase-sync":
                sub.set_defaults(func=lambda args: cli.cmd_firebase_sync(args))
    return parser
