"""Batch W3 (2026-11-11): the quote card's OppList half - the original
mail on every open case (cases/{OPP}.reply), the markup rules
(meta/sync.quoteRules), the quote action's quoteId - and the Enquiries
search by country and by every live route's places.

Every company, person, mail, place and date here is invented; cases from
OPP-9951, domains .test.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, enquiry_search, firebase_pull as fp, firebase_reply, firebase_sync as fs, outlook_bridge
from opplist.web import create_app
from test_firebase_sync import FakeFirestore

TODAY = "2026-12-14"
HERS = "desk@jojofreight.test"
AGENT = "marisol@quintcargo.test"
COLLEAGUE = "rafe@jojofreight.test"
ORIGINAL = "odalys@brackenline.test"
FORWARDED = ("From: Odalys Varga <{}>\nSent: Wednesday, December 2, 2026 9:05 AM\n"
             "To: JJC-DESK <{}>\nSubject: RATE REQUEST // DENVER - LIMA // 1 X 20GP\n").format(ORIGINAL, HERS)


class FakeArchive:
    """The archive search's methods resolve_registered reads, counting
    every lookup."""

    def __init__(self, mails):
        self.mails = mails
        self.lookups = 0

    def by_entry_id(self, entry_id):
        self.lookups += 1
        return next((m for m in self.mails if m.entry_id == entry_id), None)

    def by_message_id(self, message_id):
        self.lookups += 1
        return next((m for m in self.mails if m.message_id == message_id), None)

    def by_conversation(self, conversation_id, limit=200):
        self.lookups += 1
        return [m for m in self.mails if m.conversation_id == conversation_id]

    def own_addresses(self):
        return {HERS}

    def __exit__(self, *exc):
        return False


def mails():
    ordinary = outlook_bridge.MailInfo(
        subject="RE: Rates please // TOLEDO - SANTOS", received_on="2026-12-01", received_at="2026-12-01 09:10",
        entry_id="E-9951", conversation_id="C-9951", message_id="<q9951@quintcargo.test>",
        sender_name="Marisol Quint", sender_address=AGENT,
        to_addresses=[HERS, "ops@jojofreight.test"], cc_addresses=["pedro@quintcargo.test", "DESK@jojofreight.test"],
        body_excerpt="Hello, please quote 2 pallets Toledo to Santos.")
    forward = outlook_bridge.MailInfo(
        subject="Fwd: RATE REQUEST // DENVER - LIMA // 1 X 20GP", received_on="2026-12-02",
        received_at="2026-12-02 10:00", entry_id="E-9952", conversation_id="C-9952",
        message_id="<f9952@jojofreight.test>", sender_name="Rafe Moreno", sender_address=COLLEAGUE,
        sender_internal=True, to_addresses=[HERS],
        body_excerpt="Can you take this one?\n\n" + FORWARDED + "\nHello,\nPlease quote 1 x 20GP.\nOdalys\n")
    return [ordinary, forward]


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "w3.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "owners", ["Ana", "Ben"])
    db.set_setting(conn, "internal_domains", ["jojofreight.test"])
    quint = add_party(repo, "Quint Cargo", country="Brazil")
    bracken = add_party(repo, "Brackenline", country="Chile")
    repo.insert("contact", {"party_id": bracken, "name": "Tobin Hale", "email": "tobin@brackenline.test"})
    repo.insert("contact", {"party_id": bracken, "name": "Odalys Varga", "email": ORIGINAL, "is_primary": 1})
    ids = {}

    def case(opp, party, status="New", **extra):
        extra.setdefault("scope", "In")
        extra.setdefault("source_subject", "Rates please // TOLEDO - SANTOS")
        ids[opp] = add_enquiry(repo, opp, party, received="2026-12-01", origin_loc="MIA", dest_loc="SAI",
                               service_mode="Air", **extra)
        if status != "New":
            repo.set_status(ids[opp], status, "2026-12-03")

    case("OPP-9951", quint, entry_id="E-9951")                                 # an ordinary mail
    case("OPP-9952", bracken, "Quoted", entry_id="E-9952")                     # a colleague's forward
    case("OPP-9953", bracken, entry_id="E-GONE", source_subject="Air DENVER - LIMA")   # not in the archive
    case("OPP-9954", quint, "Lost", entry_id="E-9951")                         # closed
    case("OPP-9955", quint, entry_id="E-9951", handled_by="YUL")               # another office's
    repo.commit()
    archive = FakeArchive(mails())
    monkeypatch.setattr(fs, "_archive_search", lambda: archive)
    yield {"conn": conn, "repo": repo, "path": path, "ids": ids, "archive": archive, "state": tmp_path / "fs.json",
           "record": tmp_path / "exports.json", "reports": tmp_path / "reports"}
    conn.close()


def run(w, fake, **kw):
    kw.setdefault("today", TODAY)
    return fs.sync(w["path"], client=fake, state_file=w["state"], reports_dir=w["reports"], record=w["record"], **kw)


# --------------------------------------------------------- the reply --
def test_reply_of_an_ordinary_registered_mail(world):
    fake = FakeFirestore()
    assert run(world, fake).ok
    assert fake.docs["cases/OPP-9951"]["reply"] == {
        "on": "2026-12-01", "subject": "RE: [OPP-9951] Rates please // TOLEDO - SANTOS",
        "mid": "<q9951@quintcargo.test>", "name": "Marisol Quint", "first": "Marisol", "to": AGENT,
        # the other addresses on To and CC, hers left out (any case)
        "cc": ["ops@jojofreight.test", "pedro@quintcargo.test"], "via": ""}


def test_reply_of_a_colleagues_forward_goes_to_the_original_sender(world):
    fake = FakeFirestore()
    assert run(world, fake).ok
    assert fake.docs["cases/OPP-9952"]["reply"] == {
        "on": "2026-12-02", "subject": "RE: [OPP-9952] RATE REQUEST // DENVER - LIMA // 1 X 20GP", "mid": "",
        "name": "Odalys Varga", "first": "Odalys", "to": ORIGINAL, "cc": [COLLEAGUE], "via": "forward"}


def test_reply_of_a_mail_not_found_is_what_the_case_knows(world):
    fake = FakeFirestore()
    assert run(world, fake).ok
    assert fake.docs["cases/OPP-9953"]["reply"] == {
        "on": "2026-12-01", "subject": "RE: [OPP-9953] Air DENVER - LIMA", "mid": "",
        "name": "Odalys Varga", "first": "Odalys", "to": ORIGINAL, "cc": [], "via": ""}


def test_a_closed_case_and_another_offices_have_no_reply_and_a_case_closed_drops_it(world):
    w = world
    fake = FakeFirestore()
    assert run(w, fake).ok
    assert "reply" not in fake.docs["cases/OPP-9954"] and "reply" not in fake.docs["cases/OPP-9955"]
    # the rows of lists/inquiries never carry it
    assert all("reply" not in r for r in fake.docs["lists/inquiries"]["rows"])
    w["repo"].set_status(w["ids"]["OPP-9951"], "Lost", "2026-12-12")
    w["repo"].commit()
    assert run(w, fake).ok
    assert "reply" not in fake.docs["cases/OPP-9951"]
    assert "OPP-9951" not in fs.load_state(w["state"])["replies"]
    # back In after Out: it gains its reply again on the next push
    w["repo"].update("enquiry", w["ids"]["OPP-9952"], {"scope": "Out"})
    w["repo"].commit()
    assert run(w, fake).ok and fake.docs["cases/OPP-9952"] == {"opp": "OPP-9952", "gone": True}
    w["repo"].update("enquiry", w["ids"]["OPP-9952"], {"scope": "In"})
    w["repo"].commit()
    assert run(w, fake).ok and fake.docs["cases/OPP-9952"]["reply"]["to"] == ORIGINAL


def test_no_archive_search_on_a_push_where_nothing_changed(world):
    w = world
    fake = FakeFirestore()
    first = run(w, fake)
    assert first.ok and first.replies.searched == 3 and w["archive"].lookups > 0
    before = w["archive"].lookups
    commits = len(fake.commits)
    again = run(w, fake)
    assert again.ok and again.replies.searched == 0 and w["archive"].lookups == before
    assert fake.commits[commits:] == [["meta/sync"]]                    # nothing else rewritten
    # the mail not found is looked for again the next day, the found ones are not
    later = run(w, fake, today="2026-12-15")
    assert later.replies.searched == 1
    # a case whose registered mail changes is searched again
    w["repo"].update("enquiry", w["ids"]["OPP-9951"], {"entry_id": "E-9952"})
    w["repo"].commit()
    assert run(w, fake, today="2026-12-15").replies.searched == 1


def test_without_an_archive_the_case_knows_and_nothing_is_kept(world, monkeypatch):
    w = world
    monkeypatch.setattr(fs, "_archive_search", lambda: None)
    fake = FakeFirestore()
    assert run(w, fake).ok
    assert fake.docs["cases/OPP-9951"]["reply"]["mid"] == "" and fake.docs["cases/OPP-9951"]["reply"]["to"] == ""
    assert fs.load_state(w["state"])["replies"] == {}


def test_inquiry_rows_carry_the_countries(world):
    w = world
    w["repo"].update("enquiry", w["ids"]["OPP-9951"], {"origin_country": "USA", "dest_country": "Brazil"})
    w["repo"].commit()
    fake = FakeFirestore()
    assert run(w, fake).ok
    rows = {r["opp"]: r for r in fake.docs["lists/inquiries"]["rows"]}
    assert (rows["OPP-9951"]["oc"], rows["OPP-9951"]["dc"]) == ("USA", "Brazil")
    assert (fake.docs["cases/OPP-9951"]["oc"], fake.docs["cases/OPP-9951"]["dc"]) == ("USA", "Brazil")


def test_own_addresses_from_the_archive_stores(tmp_path):
    from opplist import mail_archive
    path = tmp_path / "mail.db"
    conn = mail_archive.open_archive(path)
    for n, store in enumerate(("Desk@JoJoFreight.test", "Personal Folder")):
        conn.execute("INSERT INTO message (message_id, entry_id, store, subject) VALUES (?, ?, ?, 'x')",
                     ("<m{}@x.test>".format(n), "E{}".format(n), store))
    conn.commit()
    conn.close()
    with mail_archive.ArchiveSearch(path) as search:
        assert firebase_reply.own_addresses(search) == {HERS}


# ---------------------------------------------------- the quote rules --
def test_quote_rules_from_settings(world):
    w = world
    fake = FakeFirestore()
    assert run(w, fake).ok
    assert fake.docs["meta/sync"]["quoteRules"] == {"markupPct": 10, "marginFloorPct": 5, "officesNoMarkup": True}
    app = create_app(w["path"])
    app.config["TESTING"] = True
    client = app.test_client()
    page = client.get("/settings").get_data(as_text=True)
    assert 'id="quote-card"' in page and 'name="quote_markup_pct" value="10"' in page
    client.post("/settings", data={"quote_markup_pct": "12.5", "quote_margin_floor_pct": "4"})
    assert run(w, fake).ok
    assert fake.docs["meta/sync"]["quoteRules"] == {"markupPct": 12.5, "marginFloorPct": 4, "officesNoMarkup": True}
    # a value that is not a percentage is refused and the old one stays
    answer = client.post("/settings", data={"quote_markup_pct": "lots", "quote_margin_floor_pct": "140"},
                         follow_redirects=True).get_data(as_text=True)
    assert "needs a number from 0 to 100" in answer
    assert fs.quote_rules(w["conn"]) == {"markupPct": 12.5, "marginFloorPct": 4, "officesNoMarkup": True}


# ---------------------------------------------------- the quote action --
def act(conn, action_id, opp, **fields):
    action = dict(fields, type="quote", opp=opp, by="dee@example.test", name="Dee Rowan", owner="Ana", app="test",
                  at=dt.datetime(2026, 12, 14, 15, 0, tzinfo=dt.timezone.utc))
    site = db.Repository(conn, source=fp.SOURCE)
    ok, message = fp.take_in(site, action_id, action, TODAY)
    site.commit()
    return ok, message


def last_note(conn, case_id):
    return conn.execute("SELECT note FROM status_event WHERE enquiry_id = ? AND voided = 0 ORDER BY id DESC LIMIT 1",
                        (case_id,)).fetchone()[0]


def test_a_quote_with_a_quote_id_is_marked_and_rate_from_kept(world):
    w = world
    conn, ids = w["conn"], w["ids"]
    ok, message = act(conn, "a1", "OPP-9951", date="2026-12-14", what="Air rate sent", sentBy="Ana",
                      quoteId="qT7xK2", rateFrom="Quint Cargo")
    assert ok and message == "OPP-9951 is Quoted on 2026-12-14."
    assert last_note(conn, ids["OPP-9951"]) == "Air rate sent (sent by Ana) [quote:qT7xK2]"
    fields = conn.execute("SELECT fields FROM site_action WHERE id = 'a1'").fetchone()[0]
    assert '"quoteId": "qT7xK2"' in fields and '"rateFrom": "Quint Cargo"' in fields
    # a Quoted case is re-quoted, marked the same way
    ok, message = act(conn, "a2", "OPP-9952", date="2026-12-14", what="", sentBy="Ben", quoteId="qR2", rateFrom="own")
    assert ok and message == "OPP-9952 re-quoted on 2026-12-14."
    assert last_note(conn, ids["OPP-9952"]) == "Re-quote: Sent by Ben [quote:qR2]"
    # the site's timeline does not show the mark
    fake = FakeFirestore()
    assert run(w, fake).ok
    assert fake.docs["cases/OPP-9951"]["tl"][0]["sub"] == "Air rate sent (sent by Ana) · logged by Dee Rowan"


def test_a_quote_without_them_is_taken_in_as_before(world):
    conn, ids = world["conn"], world["ids"]
    ok, message = act(conn, "a3", "OPP-9951", date="2026-12-14", what="Air rate sent", sentBy="Ana")
    assert ok and message == "OPP-9951 is Quoted on 2026-12-14."
    assert last_note(conn, ids["OPP-9951"]) == "Air rate sent (sent by Ana)"
    # an id that is not a document id is not written into the note
    ok, _ = act(conn, "a4", "OPP-9952", date="2026-12-14", what="", sentBy="Ben", quoteId="x] [mid:<y>")
    assert ok and last_note(conn, ids["OPP-9952"]) == "Re-quote: Sent by Ben"


# --------------------------------------------------------- the search --
def test_search_by_country_and_by_a_second_routes_place(world):
    w = world
    conn, repo, ids = w["conn"], w["repo"], w["ids"]
    repo.update("enquiry", ids["OPP-9951"], {"origin_country": "USA", "dest_country": "Brazil"})
    repo.insert("enquiry_route", {"enquiry_id": ids["OPP-9953"], "seq": 2, "origin_loc": "NGB", "dest_loc": "SAI",
                                  "origin_country": "China", "dest_country": "Chile"})
    repo.insert("enquiry_route", {"enquiry_id": ids["OPP-9952"], "seq": 2, "origin_loc": "NGB", "dest_loc": "SAI",
                                  "voided": 1})
    repo.commit()

    def found(query):
        sql, args = enquiry_search.clause(query)
        return sorted(r[0] for r in conn.execute("SELECT opp_no FROM v_enquiry e WHERE " + sql, args))

    assert found("BRAZIL") == ["OPP-9951"]
    assert found("ngb") == ["OPP-9953"]                       # a voided route does not count
    assert found("china") == ["OPP-9953"]
    assert enquiry_search.explain(conn, ids["OPP-9951"], None, "BRAZIL") == "Brazil · destination country"
    assert enquiry_search.explain(conn, ids["OPP-9953"], None, "NGB") == "NGB · route 2 origin"
    assert enquiry_search.explain(conn, ids["OPP-9953"], None, "China") == "China · route 2 origin country"
    app = create_app(w["path"])
    app.config["TESTING"] = True
    page = app.test_client().get("/enquiries?view=all&q=BRAZIL").get_data(as_text=True)
    assert "OPP-9951" in page and "Brazil · destination country" in page and "OPP-9953" not in page
