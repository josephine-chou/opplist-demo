"""The archive on the web: the Tracking page's status line, and Update from
Outlook reading the rest of a thread from the archive before Outlook."""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, mail_archive, outlook_bridge
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app


def mail(subject, sender, at, body, name="Ana", recipients=("VictorLang@jojofreight.example",),
         conversation="CONV-1", entry="ENTRY-1", internal=False):
    return MailInfo(subject=subject, received_on=at[:10], received_at=at, entry_id=entry,
                    conversation_id=conversation, conversation_topic=subject,
                    sender_name=name, sender_address=sender, sender_internal=internal,
                    to_addresses=list(recipients), body_excerpt=body,
                    folder_path="\\Mailbox\\Inbox")


@pytest.fixture
def web(tmp_path, monkeypatch):
    path = tmp_path / "web.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    add_enquiry(repo, "OPP-0001", tolvex, received="2026-09-01", status="Quoted",
                thread_key="RATEREQUESTMIASAI", conversation_id="CONV-1",
                origin_loc="MIA", dest_loc="SAI", service_mode="FCL")
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    holder = {"mail": None, "outlook_calls": 0}
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: holder["mail"])

    class LiveSearch:
        def __init__(self, *a, **k):
            holder["outlook_calls"] += 1

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def conversation_of(self, m):
            return [m]

        def hydrate(self, mails):
            return list(mails)

    monkeypatch.setattr(outlook_bridge, "Search", LiveSearch)
    archive_path = tmp_path / "archive" / "mail.db"
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive_path)
    return app.test_client(), holder, archive_path


def fill(archive_path, mails, synced_at=None):
    conn = mail_archive.open_archive(archive_path)
    mail_archive.store(conn, [mail_archive.record_from_info(m) for m in mails])
    mail_archive.set_meta(conn, "last_sync_at", synced_at or
                          dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    conn.commit()
    conn.close()


def test_the_tracking_page_shows_the_archive_state(web):
    client, holder, archive_path = web
    body = client.get("/tracking").data.decode("utf-8")
    assert 'class="archive-status bad"' in body
    assert "no mail archive yet" in body and "Run: python -m opplist mail-sync" in body

    fill(archive_path, [mail("x", "a@b.example", "2026-09-01 10:00", "hi")],
         synced_at="2026-09-01 08:00:00")
    body = client.get("/tracking").data.decode("utf-8")
    assert 'class="archive-status bad"' in body and "last synced 2026-09-01 08:00" in body

    fill(archive_path, [], synced_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    body = client.get("/tracking").data.decode("utf-8")
    assert 'class="archive-status "' in body and "(1 mails)" in body            # v2: synced HH:MM (n mails)
    assert "Run: python -m opplist mail-sync" not in body


def test_update_from_outlook_reads_the_thread_from_the_archive_first(web):
    client, holder, archive_path = web
    selected = mail("RE: RATE REQUEST // MIA - SAI", "ana@tolvex.example", "2026-09-06 10:00",
                    "Any news?\n", entry="E-SELECTED")
    holder["mail"] = selected
    later = mail("RE: RATE REQUEST // MIA - SAI", "ana@tolvex.example", "2026-09-07 09:00",
                 "Please proceed with the booking.\n", entry="E-LATER")
    fill(archive_path, [selected, later])

    page = client.post("/enquiries/1/update-from-outlook", follow_redirects=True)
    body = page.data.decode("utf-8")
    assert "suggestion(s) from the mail (same Outlook conversation, thread read from mail archive)" in body
    assert "please proceed" in body.lower()
    assert holder["outlook_calls"] == 0                    # Outlook was not opened

    # a thread the archive has not seen goes to Outlook
    holder["mail"] = mail("RE: RATE REQUEST // MIA - SAI", "ana@tolvex.example",
                          "2026-09-08 10:00", "Thanks.\n", entry="E-NEW", conversation="CONV-1")
    fill(archive_path, [])                                 # archive stays as it was
    conn = mail_archive.open_archive(archive_path)
    conn.execute("DELETE FROM message")
    conn.commit()
    conn.close()
    page = client.post("/enquiries/1/update-from-outlook", follow_redirects=True)
    assert "thread read from Outlook" in page.data.decode("utf-8")
    assert holder["outlook_calls"] == 1
