"""Batch TK2 (2026-11-13, no schema change): a leading reference stays in
the thread key when what follows it is only common words, and the stored
keys this changes.

An agent writes every import air request as '<reference> - IMPORT AIR';
taking the reference away left IMPORTAIR, and every such request joined
one case's thread. Every company, person, mail, reference and date here is
invented; cases from OPP-9870, domains .test.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import __main__ as cli
from opplist import case_mail, db, mail_archive, parsing, rekey_threads
from opplist.outlook_bridge import MailInfo


# ------------------------------------------------------------ 1. the rule --
@pytest.mark.parametrize("subject", ["QXR-551203 - IMPORT AIR", "RE: QXR-551203 - IMPORT AIR",
                                     "RE: [OPP-9871] QXR-551203 - IMPORT AIR", "FW: RE: QXR-551203 - import air",
                                     "QXR-551203 // IMPORT AIR"])
def test_a_reference_before_common_words_stays_in_every_pass(subject):
    assert parsing.subject_thread_key(subject) == "QXR551203IMPORTAIR"
    assert parsing.subject_thread_key(subject, keep_common_ref=False) == "IMPORTAIR"        # the rule before


def test_a_reference_before_real_words_still_goes():
    assert parsing.subject_thread_key("QTNRV311-0826 - Shanghai to Chicago") == "SHANGHAITOCHICAGO"
    assert parsing.subject_thread_key("RE: QX41/72640 // Air rate request Tarvel") == "AIRRATEREQUESTTARVEL"


def test_mf1s_bare_number_rule_is_unchanged():
    assert parsing.subject_thread_key("RE: KALA-COT-061") == "KALACOT061"
    assert parsing.subject_thread_key("RE: TOVA-QTE-0448", keep_bare_ref=False, keep_common_ref=False) == "0448"


def test_the_spanish_and_portuguese_words_without_case_or_accents():
    assert parsing.only_common_words("Solicitud cotización flete marítimo")
    assert parsing.only_common_words("COTAÇÃO FRETE AÉREO")
    assert parsing.subject_thread_key("QXR-551210 - Solicitud cotización flete marítimo").startswith("QXR551210")
    assert not parsing.only_common_words("Cotización Valparaíso")
    assert not parsing.only_common_words("")


# ------------------------------------------------------- 2. one thread --
@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    path = tmp_path / "tk2.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    agent = add_party(repo, "Quarrow Logistics", country="Chile")
    repo.insert("party_domain", {"domain": "quarrowlogistics.test", "party_id": agent})
    ids = {"path": path, "archive": archive, "agent": agent}
    repo.commit()
    yield conn, repo, ids
    conn.close()


def mail(subject, at, key):
    return MailInfo(subject=subject, received_on=at[:10], received_at=at, sent_at=at, entry_id="EID-" + key,
                    conversation_id="CONV-" + key, conversation_topic=parsing.clean_subject(subject),
                    sender_name="Nara Quell", sender_address="nara@quarrowlogistics.test",
                    to_addresses=["desk@jojofreight.example"], body_excerpt="Please quote.",
                    folder_path="\\Mailbox\\Inbox", message_id="<{}@tk2.test>".format(key))


def test_a_cases_thread_no_longer_takes_another_references_mail(world):
    conn, repo, ids = world
    subject = "QXR-551203 - IMPORT AIR"
    case = add_enquiry(repo, "OPP-9871", ids["agent"], received="2027-02-03", source_subject=subject,
                       thread_key=parsing.subject_thread_key(subject), entry_id="EID-own", conversation_id="CONV-own")
    repo.commit()
    store = mail_archive.open_archive(ids["archive"])
    mail_archive.store(store, [mail_archive.record_from_info(m) for m in (
        mail(subject, "2027-02-03 09:00", "own"), mail("RE: [OPP-9871] QXR-551203 - IMPORT AIR", "2027-02-03 11:00", "reply"),
        mail("QXR-551877 - IMPORT AIR", "2027-02-04 09:00", "other"))])
    store.commit()
    store.close()
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    with mail_archive.ArchiveSearch(ids["archive"]) as search:
        got = sorted(m.entry_id for m in case_mail.case_mails(conn, search, row))
    assert got == ["EID-own", "EID-reply"]                         # not QXR-551877's


# --------------------------------------------------- 3. rekey-threads --
def test_rekey_threads_dry_run_and_apply_one_case_and_a_key_set_by_hand_left(world):
    conn, repo, ids = world
    made = add_enquiry(repo, "OPP-9872", ids["agent"], received="2027-02-03", source_subject="QXR-551203 - IMPORT AIR",
                       thread_key="IMPORTAIR")                                       # the old rule's key
    hers = add_enquiry(repo, "OPP-9873", ids["agent"], received="2027-02-03", source_subject="QXR-551204 - RATE REQUEST",
                       thread_key="QUARROWAPRIL")                                    # set by hand
    real = add_enquiry(repo, "OPP-9874", ids["agent"], received="2027-02-03",
                       source_subject="QTNRV311-0826 - Shanghai to Chicago", thread_key="SHANGHAITOCHICAGO")
    attached = repo.insert("enquiry_thread", {"enquiry_id": made, "kind": "thread_key", "value": "EXPORTOCEAN",
                                              "note": "QXR-551299 - EXPORT OCEAN", "added_on": "2027-02-04",
                                              "source": "manual"})
    repo.commit()
    conn.close()
    assert cli.main(["rekey-threads", "--db", str(ids["path"])]) == 0
    conn = db.open_db(ids["path"])
    try:
        report = rekey_threads.run(conn, dry_run=True)
        assert report.cases == [("OPP-9872", "QXR-551203 - IMPORT AIR", "IMPORTAIR", "QXR551203IMPORTAIR")]
        assert report.threads == [("OPP-9872", "QXR-551299 - EXPORT OCEAN", "EXPORTOCEAN", "QXR551299EXPORTOCEAN")]
        assert report.left == [("OPP-9873", "case key", "QUARROWAPRIL", "QXR551204RATEREQUEST", "not the old rule's key")]
        assert conn.execute("SELECT thread_key FROM enquiry WHERE id = ?", (made,)).fetchone()[0] == "IMPORTAIR"
    finally:
        conn.close()
    assert cli.main(["rekey-threads", "--db", str(ids["path"]), "--apply", "--no-backup"]) == 0
    conn = db.open_db(ids["path"])
    try:
        keys = dict(conn.execute("SELECT id, thread_key FROM enquiry WHERE id IN (?, ?, ?)", (made, hers, real)).fetchall())
        assert keys == {made: "QXR551203IMPORTAIR", hers: "QUARROWAPRIL", real: "SHANGHAITOCHICAGO"}
        assert conn.execute("SELECT value FROM enquiry_thread WHERE id = ?", (attached,)).fetchone()[0] == "QXR551299EXPORTOCEAN"
        logged = conn.execute("SELECT DISTINCT action_id, source FROM change_log WHERE field IN ('thread_key', 'value') "
                              "AND source = 'rekey-threads'").fetchall()
        assert len(logged) == 1                                                       # one action
        again = rekey_threads.run(conn, dry_run=True)
        assert again.cases == [] and again.threads == []
    finally:
        conn.close()
