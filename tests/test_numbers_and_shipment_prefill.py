"""Batch H1 (2026-10-02): a European number never breaks the reading of a
mail, and the shipment page's pre-fill from an archived mail (the check T1
left out).

'13.482,71 KG': with both '.' and ',' in a number the last one is the
decimal mark. A number the reader still cannot read is kept as written,
with a note, and the rest of the mail is read.

Every company, person, mail, place, number and date here is invented;
cases from OPP-3181."""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, intake, mail_archive, outlook_bridge, parsing, shipments, suggestions
from opplist import web as web_module
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app


# ------------------------------------------------------------- numbers --
@pytest.mark.parametrize("written, value", [
    ("13.482,71", "13482.71"), ("1.234.567,8", "1234567.8"), ("13,482.71", "13482.71"),
    ("3,5", "3.5"), ("1.234.567", "1234567"), ("36,5", "36.5"), ("1,250.50", "1250.5"), ("9.100", "9.1"),
])
def test_the_last_separator_is_the_decimal_mark(written, value):
    assert parsing._number(written) == value


@pytest.mark.parametrize("text, qty, kg", [
    ("Peso bruto: 13.482,71 KG\nFavor cotizar el flat rack.", "Air:13482.71", 13482.71),
    ("Gross weight 1.234.567,8 kg", "Air:1234567.8", 1234567.8),
    ("Total weight: 13,482.71 kg", "Air:13482.71", 13482.71),
    ("Volume: 3,5 cbm", "LCL:3.5", None),
])
def test_the_body_reads_european_and_english_numbers(text, qty, kg):
    found = parsing.parse_body_qty(text)
    assert (found.qty_raw, found.weight_kg) == (qty, kg)


def test_nine_point_one_hundred_is_still_asked():
    found = parsing.parse_body_qty("Weight: 9.100 kg")
    assert found.qty_raw == "9.100 kg" and found.ask == ("9.100 kg", "9.1 kg", "9,100 kg")


def test_the_cargo_labels_read_the_grouped_number():
    labels = parsing.parse_cargo_labels(["Gross weight: 13.482,71 kg", "Volume: 3,5 cbm"])
    assert labels.found and labels.qty_raw == "13482.7 kg, 3.5 cbm"


def _mail(body, subject="RE: FLAT RACK // OAKLAND - TALCAHUANO", key="3181"):
    return MailInfo(subject=subject, received_on="2026-11-10", received_at="2026-11-10 10:00",
                    sender_address="pia@brambleshipping.example", sender_name="Pia Lund", sender_internal=False,
                    to_addresses=["desk@jojofreight.example"], body_excerpt=body, entry_id="ENTRY-" + key,
                    conversation_id="CONV-" + key)


def test_a_number_the_reader_cannot_read_is_kept_as_written(tmp_path, monkeypatch):
    path = tmp_path / "num.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(repo.conn, "shipments", "1")   # FX3: switched on for these tests
    repo.commit()

    def broken(*args, **kwargs):
        raise ValueError("could not convert string to float: '4.4.4,4'")
    monkeypatch.setattr(parsing, "parse_body_qty", broken)
    draft = intake.build(conn, _mail("Hola,\nCarga: 4.4.4,4 kg de repuestos\nOrigen Oakland, destino Talcahuano"))
    assert draft.values["qty_raw"] == "Carga: 4.4.4,4 kg de repuestos"
    assert "a number not read, kept as written" in draft.sources["qty_raw"]
    assert intake.NUMBER_UNREAD.format("'4.4.4,4'") in draft.notes
    assert draft.values["received_on"] == "2026-11-10"            # the rest of the mail is read
    conn.close()


def test_update_from_outlook_on_a_chain_with_such_a_number_returns_the_page(tmp_path, monkeypatch):
    # the thread in the archive: our quote, then the agent's reply with the
    # weight written the European way and a go-ahead
    ours = MailInfo(
        subject="FLAT RACK // OAKLAND - TALCAHUANO", received_on="2026-11-04", received_at="2026-11-04 16:00",
        sent_at="2026-11-04 16:00", message_id="<quote-3181@jojofreight.example>", sender_address="lena@jojofreight.example",
        sender_name="JJC-LENA PARK", sender_internal=True, to_addresses=["pia@brambleshipping.example"],
        conversation_id="CONV-3181", entry_id="ENTRY-Q-3181",
        body_excerpt="Dear Pia,\nOur rate for 1 x 40FR is attached.\nLena")
    reply = MailInfo(
        subject="RE: FLAT RACK // OAKLAND - TALCAHUANO", received_on="2026-11-10", received_at="2026-11-10 10:00",
        sent_at="2026-11-10 10:00", message_id="<reply-3181@brambleshipping.example>",
        sender_address="pia@brambleshipping.example", sender_name="Pia Lund", sender_internal=False,
        to_addresses=["lena@jojofreight.example"], conversation_id="CONV-3181", entry_id="ENTRY-R-3181",
        body_excerpt="Hola Lena,\nPeso bruto 13.482,71 KG, medidas 5,80 x 2,40 x 1,90 m.\nPlease book.\nPia")
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    aconn = mail_archive.open_archive(archive)
    mail_archive.store(aconn, [mail_archive.record_from_info(m) for m in (ours, reply)])
    # T2: synced just now, so the archive is never stale whatever day this runs
    mail_archive.set_meta(aconn, "last_sync_at", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    aconn.commit()
    aconn.close()
    path = tmp_path / "upd.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(repo.conn, "shipments", "1")   # FX3: switched on for these tests
    bramble = add_party(repo, "Bramble Shipping", country="Chile")
    repo.insert("party_domain", {"domain": "brambleshipping.example", "party_id": bramble})
    case = add_enquiry(repo, "OPP-3181", bramble, received="2026-11-03", conversation_id="CONV-3181",
                       source_subject="FLAT RACK // OAKLAND - TALCAHUANO", status="Quoted")
    repo.commit()
    conn.close()

    def no_outlook(*args, **kwargs):
        raise AssertionError("the thread is in the archive; Outlook is not asked")
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: ours)
    monkeypatch.setattr(outlook_bridge, "Search", no_outlook)
    app = create_app(path)
    app.config["TESTING"] = True
    page = app.test_client().post("/enquiries/{}/update-from-outlook".format(case), follow_redirects=True)
    assert page.status_code == 200
    body = page.data.decode("utf-8")
    assert "suggestion(s) from the mail" in body
    conn = db.open_db(path)
    made = suggestions.pending(conn)
    assert [(s["kind"], s["proposed_status"]) for s in made] == [("status", "Won")]
    conn.close()


# ------------------------------------- the shipment page's pre-fill (T1) --
PREALERT = MailInfo(
    subject="PRE-ALERT // BRAMBLE // OAKLAND - TALCAHUANO", received_on="2026-11-12",
    received_at="2026-11-12 09:05", sent_at="2026-11-12 09:05", message_id="<prealert-3182@jojofreight.example>",
    sender_address="lena@jojofreight.example", sender_name="Lena Park", sender_internal=True,
    to_addresses=["pia@brambleshipping.example"], conversation_id="CONV-3182", entry_id="ENTRY-PRE-3182",
    body_excerpt="Dear Pia,\nThe flat rack sailed on MV Tern Harbour, ETD Nov 12.\nDocuments follow.\nLena")
ETA_MOVED = MailInfo(
    subject="RE: PRE-ALERT // BRAMBLE // OAKLAND - TALCAHUANO", received_on="2026-11-16",
    received_at="2026-11-16 14:20", sent_at="2026-11-16 14:20", message_id="<eta-3182@pellmarlines.example>",
    sender_address="ops@pellmarlines.example", sender_name="Pellmar Lines Ops", sender_internal=False,
    to_addresses=["lena@jojofreight.example"], conversation_id="CONV-3182", entry_id="ENTRY-ETA-3182",
    body_excerpt="Dear Lena,\nPlease note the revised ETA Talcahuano as 9 Dec due to port congestion.\nRegards")


@pytest.fixture
def ship_world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    aconn = mail_archive.open_archive(archive)
    mail_archive.store(aconn, [mail_archive.record_from_info(m) for m in (PREALERT, ETA_MOVED)])
    mail_archive.set_meta(aconn, "last_sync_at", "2026-11-17 08:15:00")
    aconn.commit()
    aconn.close()
    path = tmp_path / "ship.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(repo.conn, "shipments", "1")   # FX3: switched on for these tests
    bramble = add_party(repo, "Bramble Shipping", country="Chile")
    case = add_enquiry(repo, "OPP-3182", bramble, received="2026-10-20", service_mode="FCL", origin_country="USA",
                       dest_country="Chile", conversation_id="CONV-3182",
                       source_subject="PRE-ALERT // BRAMBLE // OAKLAND - TALCAHUANO")
    repo.set_status(case, "Won", "2026-10-28", rate_used="own")
    sid = shipments.start(repo, case, "Oakland to Talcahuano")
    shipments.edit(repo, sid, {"planned_departure": "2026-11-12", "planned_arrival": "2026-12-01"})
    repo.commit()
    conn.close()
    monkeypatch.setattr(web_module, "_today", lambda: "2026-11-17")
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, sid


def test_log_as_milestone_and_log_as_issue_open_prefilled_from_the_archived_mail(ship_world):
    client, path, sid = ship_world
    body = client.get("/shipments/{}".format(sid)).data.decode("utf-8")
    assert body.count(">Log as milestone<") == 2 and body.count(">Log as issue<") == 2
    # Log as milestone on our pre-alert: Departed, at the mail's time, from the mail
    body = client.get("/shipments/{}?from_mail={}&as=milestone".format(sid, "%3Cprealert-3182%40jojofreight.example%3E")).data.decode("utf-8")
    form = body[body.index('id="form-milestone"'):body.index('id="form-issue"')]
    assert " hidden" not in form.split(">")[0]
    assert '<option value="Departed" selected>' in form
    assert 'name="source" value="mail"' in form and 'name="message_id" value="&lt;prealert-3182@jojofreight.example&gt;"' in form
    assert 'name="sender" value="Lena Park"' in form and "from the mail of Lena Park" in form
    conn = db.open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM shipment_event").fetchone()[0] == 0       # nothing written yet
    conn.close()
    client.post("/shipments/{}/milestone".format(sid), data={
        "milestone": "Departed", "at": "2026-11-12T09:05", "detail": "sailed", "source": "mail",
        "message_id": "<prealert-3182@jojofreight.example>", "sender": "Lena Park"})
    conn = db.open_db(path)
    assert tuple(conn.execute("SELECT milestone, source, message_id, sender FROM shipment_event").fetchone()) == \
        ("Departed", "mail", "<prealert-3182@jojofreight.example>", "Lena Park")
    conn.close()
    # Log as issue on the carrier's mail: the new ETA is in the issue form
    body = client.get("/shipments/{}?from_mail={}&as=issue".format(sid, "%3Ceta-3182%40pellmarlines.example%3E")).data.decode("utf-8")
    form = body[body.index('id="form-issue"'):body.index('id="form-note"')]
    assert 'name="eta" value="2026-12-09"' in form and "ETA moved to Dec 9" in form
    # a mail the archive does not have: the page says so
    body = client.get("/shipments/{}?from_mail=%3Cgone%40x%3E&as=issue".format(sid), follow_redirects=True).data.decode("utf-8")
    assert "That mail is not in the archive any more." in body
