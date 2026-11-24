"""Finding the party a mail came from."""

from __future__ import annotations

import pytest

from conftest import add_party, seed_reference
from opplist import matching


@pytest.fixture
def parties(repo):
    """Invented parties (13g rewrite). Our own office has no domain: an
    internal domain never belongs to a party."""
    seed_reference(repo)
    brightwater = add_party(repo, "Brightwater", country="Chile",
                            full_name="Brightwater Group Andina SpA")
    klv = add_party(repo, "KLV", country="USA", full_name="KLV Logistics LLC")
    office = add_party(repo, "JoJo JJK", ptype="Office", country="USA")
    moorhen = add_party(repo, "Moorhen", country="China",
                        full_name="Moorhen Logistics Co., Ltd")
    repo.insert("party_domain", {"domain": "brightwater.example", "party_id": brightwater})
    repo.insert("party_domain", {"domain": "shipklv.example", "party_id": klv})
    repo.commit()
    return repo.conn, {"brightwater": brightwater, "klv": klv, "office": office, "moorhen": moorhen}


def test_domain_of():
    assert matching.domain_of("Rosa <rosa@brightwater.example>") == "brightwater.example"
    assert matching.domain_of("rosa@BRIGHTWATER.EXAMPLE") == "brightwater.example"
    assert matching.domain_of("") == ""
    assert matching.domain_of("not an address") == ""


def test_normalise_name_drops_legal_forms():
    assert matching.normalise_name("Brightwater Group Andina SpA") == ["brightwater", "andina"]
    assert matching.normalise_name("KLV Logistics LLC") == ["klv"]
    assert matching.normalise_name("Moorhen Logistics Co., Ltd") == ["moorhen"]


# --------------------------------------------------------- the sender ----
def test_an_external_sender_matches_on_its_domain(parties):
    conn, ids = parties
    sender = matching.SenderInfo(address="rosa@brightwater.example", name="Rosa")
    found = matching.find(conn, sender)
    assert [m.party_id for m in found] == [ids["brightwater"]]
    assert found[0].how == matching.MATCH_DOMAIN
    assert found[0].confident


def test_a_free_mail_domain_is_not_a_company(parties):
    conn, _ = parties
    sender = matching.SenderInfo(address="someone@gmail.com")
    assert matching.find(conn, sender) == []


def test_an_internal_sender_falls_back_to_to_and_cc(parties):
    """We forward the agent's mail; From says JoJo and means nothing."""
    conn, ids = parties
    sender = matching.SenderInfo(
        address="desk@jojofreight.example", name="JJC-DESK", internal=True,
        recipients=["pricing@shipklv.example", "ops@jojofreight.example"])
    found = matching.find(conn, sender)
    assert found[0].party_id == ids["klv"]
    assert found[0].how == matching.MATCH_RECIPIENT_DOMAIN


def test_an_internal_sender_is_never_matched_as_the_requester(parties):
    conn, ids = parties
    sender = matching.SenderInfo(address="desk@jojofreight.example", internal=True, recipients=[])
    assert [m.party_id for m in matching.find(conn, sender)] == []


def test_our_own_domain_is_never_on_file_for_a_party(repo, parties):
    conn, ids = parties
    assert matching.remember_domain(repo, ids["office"], "jojofreight.example") == ""
    sender = matching.SenderInfo(address="ops@jojofreight.example", internal=False)
    assert matching.learn_domain(repo, ids["office"], sender) == ""
    assert conn.execute("SELECT COUNT(*) FROM party_domain WHERE domain = 'jojofreight.example'").fetchone()[0] == 0


def test_free_mail_recipients_are_skipped(parties):
    conn, ids = parties
    sender = matching.SenderInfo(
        address="desk@jojofreight.example", internal=True,
        recipients=["someone@gmail.com", "rosa@brightwater.example"])
    assert [m.party_id for m in matching.find(conn, sender)] == [ids["brightwater"]]


# ------------------------------------------------------- names in subject --
def test_a_name_from_the_subject_matches_the_key(parties):
    conn, ids = parties
    sender = matching.SenderInfo(address="unknown@nowhere.example", internal=False)
    found = matching.find(conn, sender, names=["KLV"])
    assert [m.party_id for m in found] == [ids["klv"]]
    assert found[0].how == matching.MATCH_NAME
    assert not found[0].confident, "a name is a suggestion, not a fact"


def test_a_name_matches_through_the_legal_form(parties):
    conn, ids = parties
    sender = matching.SenderInfo(address="x@nowhere.example")
    found = matching.find(conn, sender, names=["KLV LOGISTICS"])
    assert ids["klv"] in [m.party_id for m in found]


def test_the_domain_wins_and_the_name_is_not_asked_for(parties):
    conn, ids = parties
    sender = matching.SenderInfo(address="rosa@brightwater.example")
    found = matching.find(conn, sender, names=["KLV"])
    assert found[0].party_id == ids["brightwater"]
    assert ids["klv"] in [m.party_id for m in found], "the name hit is still listed"


def test_several_hits_are_all_returned(repo, parties):
    conn, ids = parties
    other = add_party(repo, "KLV Andina", country="Chile", full_name="KLV")
    repo.commit()
    sender = matching.SenderInfo(address="x@nowhere.example")
    found = matching.find(conn, sender, names=["KLV"])
    assert {m.party_id for m in found} == {ids["klv"], other}


def test_no_evidence_means_no_guess(parties):
    conn, _ = parties
    sender = matching.SenderInfo(address="x@brand-new-agent.example")
    assert matching.find(conn, sender, names=["NOBODY HERE"]) == []


# ------------------------------------------------------------ learning ----
def test_the_domain_is_remembered_for_next_time(repo, parties):
    conn, ids = parties
    new_party = add_party(repo, "Cormorant Carga", country="Chile")
    sender = matching.SenderInfo(address="ops@cormorantcarga.example")
    assert matching.learn_domain(repo, new_party, sender) == "cormorantcarga.example"
    repo.commit()
    found = matching.find(conn, matching.SenderInfo(address="other@cormorantcarga.example"))
    assert [m.party_id for m in found] == [new_party]


def test_learning_uses_a_recipient_domain_for_an_internal_sender(repo, parties):
    conn, _ = parties
    new_party = add_party(repo, "Plover", country="Chile")
    sender = matching.SenderInfo(address="desk@jojofreight.example", internal=True,
                                 recipients=["quotes@ploverfreight.example"])
    assert matching.learn_domain(repo, new_party, sender) == "ploverfreight.example"


def test_a_domain_already_owned_is_never_reassigned(repo, parties):
    conn, ids = parties
    other = add_party(repo, "Someone Else", country="Chile")
    sender = matching.SenderInfo(address="rosa@brightwater.example")
    assert matching.learn_domain(repo, other, sender) == ""
    assert conn.execute(
        "SELECT party_id FROM party_domain WHERE domain = 'brightwater.example'"
    ).fetchone()["party_id"] == ids["brightwater"]


# ------------------------------------------ the domain's own name (5.5.5) --
def test_domain_stems_and_squashed_names():
    from opplist import matching

    assert matching.domain_stem("desert-demo.com.sa") == "desert-demo"
    assert matching.domain_stem("mail.azteca-demo.com.mx") == "azteca-demo"
    assert matching.domain_stem("us.inca-demo.com.pe") == "inca-demo"
    assert matching.domain_stem("andes-demo.cl") == "andes-demo"
    assert matching.domain_stem("yahoo.com.br") == "yahoo"
    assert matching.squash("Zenith Cargo S.A.") == "zenithcargosa"
    assert matching.is_free_mail("yahoo.com.br") and matching.is_free_mail("gmail.com")
    assert not matching.is_free_mail("desert-demo.com.sa")


def test_reserved_endings_stem_to_the_name_in_front():
    """'.example' and '.test' are endings, like '.com': the company is the
    label in front of them (needs matching.domain_stem to treat them as
    public suffixes)."""
    from opplist import matching

    assert matching.domain_stem("mail.bluefin-usa.example") == "bluefin-usa"
    assert matching.domain_stem("us.harborline.example") == "harborline"
    assert matching.domain_stem("tolvex.example") == "tolvex"
    assert matching.domain_stem("boxlane.test") == "boxlane"


def test_the_tld_names_the_country_when_it_is_on_file(repo):
    from opplist import matching

    repo.insert("country", {"name": "Saudi Arabia", "region": "Middle East"})
    repo.commit()
    assert matching.country_for_tld(repo.conn, "desert-demo.com.sa") == "Saudi Arabia"
    assert matching.country_for_tld(repo.conn, "x.example") == ""
    assert matching.country_for_tld(repo.conn, "x.de") == ""       # Germany not in this book
    assert matching.country_for_tld(repo.conn, "") == ""


def test_by_domain_stem_and_similar_parties(repo):
    from opplist import matching
    from conftest import add_party

    from conftest import seed_reference
    seed_reference(repo)
    add_party(repo, "Desert Demo", country="Chile")
    add_party(repo, "Desert Demo Haulage", country="Chile")
    add_party(repo, "Tolvex", country="Chile", aliases="Tolvex Chile, TOLVEX S.A.")
    add_party(repo, "Desert", country="Chile")
    repo.commit()
    hits = matching.by_domain_stem(repo.conn, "desert-demo.com.sa")
    assert [(m.key, m.how, m.detail) for m in hits] == \
        [("Desert Demo", "domain matches party name", "desert-demo ~ Desert Demo")]
    assert matching.by_domain_stem(repo.conn, "gmail.com") == []
    assert matching.by_domain_stem(repo.conn, "ab.example") == []
    # 'desert-demo' also looks for 'desert' on its own, as a '-intl' tail would
    similar = matching.similar_parties(repo.conn, "desert-demo.com.sa")
    assert [s["key"] for s in similar] == ["Desert Demo", "Desert Demo Haulage", "Desert"]
    assert matching.similar_parties(repo.conn, "unrelated.example") == []


def test_by_domain_stem_reads_an_alias_with_its_legal_form(repo):
    """The domain's name is one of the aliases, the legal form dropped."""
    from opplist import matching
    from conftest import add_party, seed_reference

    seed_reference(repo)
    add_party(repo, "Tolvex", country="Chile", aliases="Tolvex Chile, ANDES DEMO SPA")
    repo.commit()
    assert [m.key for m in matching.by_domain_stem(repo.conn, "andes-demo.cl")] == ["Tolvex"]


def test_remember_domain_adds_once_and_never_a_mailbox(repo):
    from opplist import matching
    from conftest import add_party

    from conftest import seed_reference
    seed_reference(repo)
    pid = add_party(repo, "Desert Demo", country="Chile")
    other = add_party(repo, "Other", country="Chile")
    repo.commit()
    assert matching.remember_domain(repo, pid, "desert-demo.com.sa") == "desert-demo.com.sa"
    assert matching.remember_domain(repo, other, "desert-demo.com.sa") == ""      # taken
    assert matching.remember_domain(repo, pid, "gmail.com") == ""
    assert repo.conn.execute("SELECT party_id FROM party_domain WHERE domain = 'desert-demo.com.sa'").fetchone()[0] == pid
    assert repo.conn.execute("SELECT count(*) FROM change_log WHERE table_name = 'party_domain'").fetchone()[0] >= 1
