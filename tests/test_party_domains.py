"""Domains for the parties that have none (party-domains), the placeholder
pairs on the duplicates list, and the party page's 'Merge into' box."""

from __future__ import annotations

import csv

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, mail_archive, party_domains, party_dupes
from opplist.outlook_bridge import MailInfo


def info(subject, sender, at="2026-08-14 09:30", conversation="", topic="",
         to=("VictorLang@jojofreight.example",), cc=(), internal=False, message_id=""):
    return MailInfo(
        subject=subject, received_on=at[:10], received_at=at,
        entry_id="EID-{}-{}".format(at, subject)[:60], conversation_id=conversation,
        conversation_topic=topic or subject, sender_name=sender.split("@")[0],
        sender_address=sender, sender_internal=internal, to_addresses=list(to),
        cc_addresses=list(cc), body_excerpt="", folder_path="\\Personal Folder\\X",
        message_id=message_id or "<{}-{}@x>".format(at.replace(" ", "T"), sender))


MAILS = [
    # Zenith Cargo's case, by conversation id: they wrote, we answered
    info("RATE REQUEST // MIA - LIM", "ana@zenithcargo.example", conversation="CONV-PC"),
    info("RE: RATE REQUEST // MIA - LIM", "VictorLang@jojofreight.example", at="2026-08-14 10:00",
         conversation="CONV-PC", to=("ana@zenithcargo.example",), cc=("ops@zenithcargo.example",),
         internal=True),
    # TQ Freight's case has no conversation id on the enquiry: the topic matches
    info("RE: QUOTE // ORD - SCL", "jose@tqfreight.example", conversation="CONV-RO",
         topic="QUOTE // ORD - SCL"),
    # a vendor we asked on Solstice's case: our mail, their address on To
    info("RATE REQUEST // ORD - PVG", "VictorLang@jojofreight.example", conversation="CONV-SE",
         to=("pricing@seavio.example",), internal=True),
    # a mail from a domain whose stem carries Andes Morrow's name, in no case thread
    info("Greetings from Santiago", "diego@andes-demo.cl", conversation="CONV-HM"),
    info("Greetings again", "diego@andes-demo.cl", at="2026-08-20 09:30", conversation="CONV-HM2"),
    # Tolvex Chile SpA's case: the sender's domain already belongs to 'Tolvex'
    info("RATE REQUEST // SCL - MIA", "renata@tolvex.example", conversation="CONV-GL"),
    # noise: a free mailbox and our own domain never count
    info("hello", "somebody@gmail.com", conversation="CONV-PC"),
    info("internal", "AvaT@jojofreight.example", conversation="CONV-PC", internal=True,
         to=("IrisChen@jojofreight.example",)),
]


@pytest.fixture
def world(tmp_path):
    """A db with the parties the rules need and an archive with MAILS."""
    path = tmp_path / "pd.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    repo.add_location("SANTIAGO", country="Chile")
    ids = {
        "zenith": add_party(repo, "Zenith Cargo", country="USA"),
        "tq": add_party(repo, "TQ Freight", country="Chile"),
        "solstice": add_party(repo, "Solstice", country="China"),
        "seavio": add_party(repo, "Seavio", country="USA"),
        "kessler": add_party(repo, "Andes Morrow Seaway", country="Chile"),
        "tolvex": add_party(repo, "Tolvex", country="Chile"),
        "tolvex2": add_party(repo, "Tolvex Chile SpA", country="Chile"),
        "jjc": add_party(repo, "JoJo JJC", ptype="Office", country="USA"),
        "tbc": add_party(repo, "(TBC)", ptype="Unknown", country=None, placeholder=1),
        "idle": add_party(repo, "Idle Company Never Used", country="USA"),
    }
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": ids["tolvex"]})
    add_enquiry(repo, "OPP-0001", ids["zenith"], received="2026-08-14", conversation_id="CONV-PC")
    add_enquiry(repo, "OPP-0002", ids["tq"], received="2026-08-14",
                source_subject="RE: QUOTE // ORD - SCL")
    case = add_enquiry(repo, "OPP-0003", ids["solstice"], received="2026-08-14",
                       conversation_id="CONV-SE")
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": ids["seavio"],
                                   "asked_on": "2026-08-14"})
    add_enquiry(repo, "OPP-0004", ids["kessler"], received="2026-08-14")
    add_enquiry(repo, "OPP-0005", ids["tolvex2"], received="2026-08-14", conversation_id="CONV-GL")
    add_enquiry(repo, "OPP-0006", ids["jjc"], received="2026-08-14", conversation_id="CONV-PC")
    add_enquiry(repo, "OPP-0007", ids["tbc"], received="2026-08-14", conversation_id="CONV-PC")
    repo.commit()
    conn.close()

    archive_path = tmp_path / "mail.db"
    archive = mail_archive.open_archive(archive_path)
    mail_archive.store(archive, [mail_archive.record_from_info(m) for m in MAILS])
    archive.commit()
    archive.close()
    return path, archive_path, ids


def by_party(candidates) -> dict:
    out = {}
    for c in candidates:
        out.setdefault(c.party_key, []).append(c)
    return out


def test_targets_are_the_used_parties_without_a_domain(world):
    path, archive_path, ids = world
    conn = db.open_db(path)
    keys = [r["key"] for r in party_domains.target_parties(conn)]
    conn.close()
    assert "Zenith Cargo" in keys and "Seavio" in keys          # requester and vendor
    assert "Tolvex" not in keys                                # has a domain
    assert "JoJo JJC" not in keys and "(TBC)" not in keys  # office, reserved
    assert "Idle Company Never Used" not in keys               # on no case


def test_the_three_kinds_of_evidence(world):
    path, archive_path, ids = world
    conn = db.open_db(path)
    with mail_archive.ArchiveSearch(archive_path) as search:
        candidates, report = party_domains.propose(conn, search._conn)
    conn.close()
    found = by_party(candidates)
    # a. thread by conversation id: the sender, and the people on our reply
    zenith = {c.proposed_domain: c for c in found["Zenith Cargo"]}
    assert set(zenith) == {"zenithcargo.example"}
    assert zenith["zenithcargo.example"].evidence == "thread"
    assert zenith["zenithcargo.example"].hits == 3                # ana, ana on To, ops on Cc
    assert zenith["zenithcargo.example"].sample_address == "ana@zenithcargo.example"
    assert zenith["zenithcargo.example"].action == "add-domain"
    # a. thread by topic when the enquiry has no conversation id
    assert [(c.proposed_domain, c.evidence) for c in found["TQ Freight"]] == [("tqfreight.example", "thread")]
    # a. a vendor on our outgoing mail - and the same domain turns up in the
    # requester's thread, so both rows say so and apply() will wait
    assert [(c.proposed_domain, c.evidence) for c in found["Seavio"]] == \
        [("seavio.example", "thread (also proposed for Solstice)")]
    assert [(c.proposed_domain, c.evidence) for c in found["Solstice"]] == \
        [("seavio.example", "thread (also proposed for Seavio)")]
    # b. the name inside a domain stem, counted over the whole archive
    kessler = found["Andes Morrow Seaway"]
    assert [(c.proposed_domain, c.evidence, c.hits) for c in kessler] == [("andes-demo.cl", "name", 2)]
    # c. a domain another party owns: the same company twice
    tolvex2 = found["Tolvex Chile SpA"]
    assert len(tolvex2) == 1
    assert (tolvex2[0].proposed_domain, tolvex2[0].evidence, tolvex2[0].action) == \
        ("tolvex.example", "duplicate-of", "merge-into")
    assert (tolvex2[0].conflict_party_id, tolvex2[0].conflict_party_key) == (ids["tolvex"], "Tolvex")
    assert tolvex2[0].keep_id == ""
    assert (report.parties, report.with_candidates, report.without) == (6, 6, 0)
    assert report.without_keys == []
    assert report.merges == 1 and report.shared == 2


def test_telling_words_drop_the_trade_the_places_and_the_short():
    places = {"chile", "santiago", "usa"}
    assert party_domains.telling_words("Kessler Morrow Seaway Chile", "", places) == \
        (["kessler", "morrow", "seaway"], "kesslermorrowseaway")
    assert party_domains.telling_words("Kite Logistics Service", "", places) == ([], "")
    assert party_domains.telling_words("KRV Global Solution", "", places) == ([], "")      # 'krv' is short
    assert party_domains.telling_words("Seavio", "Seavio Transport Inc", places) == (["seavio"], "seavio")
    assert party_domains.telling_words("Zenith Cargo", "", places) == (["zenith"], "zenith")


def test_the_dry_run_writes_the_csv_and_changes_nothing(world, tmp_path):
    path, archive_path, ids = world
    out = tmp_path / "proposed.csv"
    conn = db.open_db(path)
    before = conn.execute("SELECT COUNT(*) FROM party_domain").fetchone()[0]
    report = party_domains.run(conn, archive_path=archive_path, csv_path=out)
    after = conn.execute("SELECT COUNT(*) FROM party_domain").fetchone()[0]
    changes = conn.execute("SELECT COUNT(*) FROM change_log WHERE source = 'import'").fetchone()[0]
    conn.close()
    assert before == after == 1 and changes == 0
    assert report.csv_path == out
    with out.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == party_domains.COLUMNS
    # most used first, then by key: every party here has one case
    assert [r["party_key"] for r in rows][:2] == ["Andes Morrow Seaway", "Seavio"]
    assert any(r["action"] == "merge-into" and r["keep_id"] == "" for r in rows)
    assert not any("nothing found for" in line for line in report.lines())
    assert any("2 proposed for more than one party" in line for line in report.lines())


def test_apply_adds_the_new_and_never_touches_an_existing_domain(world, tmp_path):
    path, archive_path, ids = world
    out = tmp_path / "proposed.csv"
    conn = db.open_db(path)
    party_domains.run(conn, archive_path=archive_path, csv_path=out)
    # hand-edit the CSV the way a reviewer would: one row tries to take a
    # domain that is already Tolvex's, one merge gets a keep_id, one does not
    with out.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows.append({c: "" for c in party_domains.COLUMNS})
    rows[-1].update({"party_id": str(ids["kessler"]), "party_key": "Andes Morrow Seaway",
                     "proposed_domain": "tolvex.example", "action": "add-domain"})
    for row in rows:
        if row["action"] == "merge-into":
            row["keep_id"] = str(ids["tolvex"])
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=party_domains.COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    report = party_domains.apply(conn, csv_path=out, today="2026-09-15")
    domains = {r["domain"]: r["party_id"] for r in conn.execute("SELECT domain, party_id FROM party_domain")}
    assert domains["zenithcargo.example"] == ids["zenith"]
    assert domains["tqfreight.example"] == ids["tq"]
    assert "seavio.example" not in domains                            # proposed to Solstice and Seavio: waits
    assert domains["andes-demo.cl"] == ids["kessler"]
    assert domains["tolvex.example"] == ids["tolvex"]                  # untouched
    assert report.added == 3
    assert len(report.skipped_existing) == 1
    assert any("tolvex.example is Tolvex's already" in line for line in report.skipped_existing)
    assert len(report.skipped_shared) == 2
    assert any("seavio.example is proposed for 2 parties" in line for line in report.skipped_shared)
    assert report.merged == ["{} -> {}".format(ids["tolvex2"], ids["tolvex"])]
    merged = conn.execute("SELECT merged_into FROM party WHERE id = ?", (ids["tolvex2"],)).fetchone()
    assert merged["merged_into"] == ids["tolvex"]
    assert conn.execute("SELECT COUNT(*) FROM party").fetchone()[0] == 10   # nothing deleted
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE source = 'import' "
                        "AND table_name = 'party_domain'").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE source = 'import' "
                        "AND table_name = 'party' AND field = 'merged_into'").fetchone()[0] == 1
    # a second apply adds nothing and merges nothing more
    again = party_domains.apply(conn, csv_path=out, today="2026-09-15")
    assert again.added == 0 and again.merged == []
    assert any("was merged already" in line for line in again.bad)
    conn.close()


def test_a_merge_row_without_keep_id_is_left_alone(world, tmp_path):
    path, archive_path, ids = world
    out = tmp_path / "proposed.csv"
    conn = db.open_db(path)
    party_domains.run(conn, archive_path=archive_path, csv_path=out)
    report = party_domains.apply(conn, csv_path=out)
    assert report.merge_skipped == 1 and report.merged == []
    assert conn.execute("SELECT merged_into FROM party WHERE id = ?",
                        (ids["tolvex2"],)).fetchone()["merged_into"] is None
    conn.close()


def test_the_command_dry_runs_by_default_and_applies_with_a_restore_point(world, tmp_path, capsys):
    from opplist.__main__ import main

    path, archive_path, ids = world
    out = tmp_path / "proposed.csv"
    assert main(["party-domains", "--db", str(path), "--archive", str(archive_path),
                 "--csv", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "dry run" in printed and "with a candidate               : 6" in printed
    assert out.exists()
    restore = tmp_path / "restore"
    assert main(["party-domains", "--db", str(path), "--csv", str(out), "--apply",
                 "--restore-dir", str(restore)]) == 0
    printed = capsys.readouterr().out
    assert "restore point:" in printed and "domains added          : 3" in printed
    assert [p.name[:29] for p in restore.iterdir()] == ["opplist_before_party-domains_"]
    conn = db.open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM party_domain").fetchone()[0] == 4
    conn.close()


def test_no_archive_is_a_message_not_a_crash(world, tmp_path, capsys):
    from opplist.__main__ import main

    path, archive_path, ids = world
    assert main(["party-domains", "--db", str(path), "--archive", str(tmp_path / "none.db"),
                 "--csv", str(tmp_path / "x.csv")]) == 1
    assert "No mail archive yet" in capsys.readouterr().out


# ------------------------------------------------ placeholders as duplicates --
def test_find_duplicates_pairs_a_placeholder_with_its_company(repo):
    seed_reference(repo)
    add_party(repo, "Kessler Morrow Seaway", country="Chile")
    add_party(repo, "Kessler Morrow Seaway Chile", ptype="Unknown", country=None, placeholder=1)
    add_party(repo, "(TBC)", ptype="Unknown", country=None, placeholder=1)
    add_party(repo, "Unknown", ptype="Unknown", country=None, placeholder=1)
    add_party(repo, "COLIN", ptype="Direct Customer", country=None, placeholder=1)
    add_party(repo, "Unknown Shipping Co", country="USA")
    repo.commit()
    pairs = party_dupes.find_duplicates(repo.conn)
    found = {(p.a_key, p.b_key): p.why for p in pairs}
    assert found[("Kessler Morrow Seaway", "Kessler Morrow Seaway Chile")] == \
        "'Kessler Morrow Seaway' is contained in 'Kessler Morrow Seaway Chile' " \
        "(placeholder: Kessler Morrow Seaway Chile)"
    assert not any("(TBC)" in a or "(TBC)" in b or a == "Unknown" or b == "Unknown"
                   or "COLIN" in (a, b) for a, b in found)


# --------------------------------------------------------- merge into (web) --
@pytest.fixture
def web(tmp_path):
    from opplist.web import create_app

    path = tmp_path / "merge.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    kmsg = add_party(repo, "KMSG", country="Chile")
    kessler = add_party(repo, "Kessler Morrow Seaway", country="Chile")
    ph = add_party(repo, "Kessler Morrow Seaway Chile", ptype="Unknown", country=None, placeholder=1)
    add_enquiry(repo, "OPP-0001", kmsg, received="2026-08-01")
    add_enquiry(repo, "OPP-0002", ph, received="2026-08-02")
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, {"kmsg": kmsg, "kessler": kessler, "ph": ph}


def _party(path, party_id):
    conn = db.open_db(path)
    row = conn.execute("SELECT key, merged_into, aliases FROM party WHERE id = ?", (party_id,)).fetchone()
    conn.close()
    return row


def test_the_party_page_offers_merge_into_with_every_key(web):
    client, path, ids = web
    body = client.get("/parties/{}".format(ids["kmsg"])).data.decode("utf-8")
    assert "Merge into" in body and 'list="merge-keys"' in body
    assert '<option value="Kessler Morrow Seaway">' in body
    assert '<option value="KMSG">' not in body                  # not itself
    assert 'name="keep" value="this"' in body and 'name="keep" value="other"' in body
    # the placeholder's page has it too
    body = client.get("/parties/{}".format(ids["ph"])).data.decode("utf-8")
    assert "Merge into" in body


def test_merge_into_keeping_this_one(web):
    client, path, ids = web
    page = client.post("/parties/merge", data={
        "party_id": ids["kmsg"], "other_key": "kessler morrow seaway", "keep": "this"},
        follow_redirects=True)
    assert page.status_code == 200 and b"Merged into KMSG" in page.data
    assert _party(path, ids["kessler"])["merged_into"] == ids["kmsg"]
    assert "Kessler Morrow Seaway" in _party(path, ids["kmsg"])["aliases"]


def test_merge_into_keeping_the_other_one(web):
    client, path, ids = web
    page = client.post("/parties/merge", data={
        "party_id": ids["ph"], "other_key": "Kessler Morrow Seaway", "keep": "other"},
        follow_redirects=True)
    assert page.status_code == 200 and b"Merged into Kessler Morrow Seaway" in page.data
    assert _party(path, ids["ph"])["merged_into"] == ids["kessler"]
    assert _party(path, ids["kessler"])["merged_into"] is None
    conn = db.open_db(path)
    assert conn.execute("SELECT requester_party_id FROM enquiry WHERE opp_no = 'OPP-0002'"
                        ).fetchone()[0] == ids["kessler"]
    conn.close()


def test_merge_into_refuses_an_unknown_key_and_itself(web):
    client, path, ids = web
    page = client.post("/parties/merge", data={
        "party_id": ids["kmsg"], "other_key": "Nobody", "keep": "this"}, follow_redirects=True)
    assert b"No party called &#39;Nobody&#39;" in page.data or b"No party called 'Nobody'" in page.data
    page = client.post("/parties/merge", data={
        "party_id": ids["kmsg"], "other_key": "KMSG", "keep": "other"}, follow_redirects=True)
    assert b"That is this party" in page.data
    assert _party(path, ids["kmsg"])["merged_into"] is None
