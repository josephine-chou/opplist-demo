"""Batch 13g: one mail never makes two cases, a place is one word to the
duplicate score, a weight like '9.100 kg' reads the way the mail writes
numbers, and an exact repeat of a status only confirms.

Every company, person, number, reference, date and mail here is invented."""

from __future__ import annotations

import re

import pytest

from conftest import add_party, seed_reference
from opplist import db, dedupe, intake, outlook_bridge, parsing
from opplist.web import create_app


# ------------------------------------------------ B: suspected duplicates --
@pytest.fixture
def world(repo):
    seed_reference(repo)
    for key, country in (("PORTO ALEGRE", "Brazil"), ("TIANJIN", "China"), ("DALIAN", "China")):
        repo.insert("location", {"key": key, "country": country, "type": "City"})
    kestrel = add_party(repo, "Kestrel Forwarding", country="Brazil")
    tern = add_party(repo, "Tern Cargo Ltda", country="Brazil")
    repo.commit()
    return repo, kestrel, tern


def case(repo, opp_no, party, **extra):
    values = {"opp_no": opp_no, "requester_party_id": party, "status": "New",
              "received_on": "2026-11-02", "service_mode": "FCL",
              "origin_country": "China", "origin_loc": "TIANJIN",
              "dest_country": "Brazil", "dest_loc": "PORTO ALEGRE"}
    values.update(extra)
    enquiry_id = repo.insert("enquiry", values)
    repo.commit()
    return enquiry_id


def probe(party, **extra):
    values = {"requester_party_id": party, "received_on": "2026-11-04", "service_mode": "FCL",
              "origin_country": "China", "origin_loc": "DALIAN",
              "dest_country": "Brazil", "dest_loc": "PORTO ALEGRE"}
    values.update(extra)
    return values


def test_a_two_word_place_is_one_token():
    tokens = dedupe.case_tokens({"commodity": "ceramic tiles", "origin_loc": "Porto Alegre",
                                 "dest_loc": "TIANJIN"})
    assert tokens == {"ceramic", "tile", "@porto alegre", "@tianjin"}
    assert "porto" not in tokens and "alegre" not in tokens


def test_two_agents_sharing_one_two_word_city_are_not_offered(world):
    repo, kestrel, tern = world
    case(repo, "OPP-2201", kestrel)
    assert dedupe.find(repo.conn, probe(tern)) == []
    df = dedupe.document_frequency(repo.conn)
    other = repo.conn.execute("SELECT e.*, e.origin_loc AS origin_key, e.dest_loc AS dest_key "
                              "FROM v_enquiry e WHERE opp_no = 'OPP-2201'").fetchone()
    match = dedupe.score_pair(probe(tern), other, df)
    assert match.score == 35 and match.reasons == ["rare words: PORTO ALEGRE (unique)"]


def test_place_and_commodity_words_alone_never_reach_possible_between_agents(world):
    repo, kestrel, tern = world
    case(repo, "OPP-2201", kestrel, commodity="glazed ceramic tiles")
    assert dedupe.find(repo.conn, probe(tern, commodity="glazed ceramic tiles")) == []
    # the same requester still scores on them
    [match] = dedupe.find(repo.conn, probe(kestrel, commodity="glazed ceramic tiles"))
    assert match.opp_no == "OPP-2201" and match.score >= dedupe.POSSIBLE


def test_two_agents_with_the_same_end_customer_are_offered(world):
    repo, kestrel, tern = world
    case(repo, "OPP-2201", kestrel, origin_loc="DALIAN", end_customer="Lumen Paper SA")
    [match] = dedupe.find(repo.conn, probe(tern, end_customer="Lumen Paper SA"))
    assert match.opp_no == "OPP-2201" and "same end customer and lane" in match.reasons


def test_two_agents_with_the_same_po_number_in_their_subjects_are_offered(world):
    repo, kestrel, tern = world
    case(repo, "OPP-2201", kestrel, source_subject="COTACAO FRETE - PEDIDO: 208.415 - LUMEN")
    [match] = dedupe.find(repo.conn, probe(tern, source_subject="RE: rate request PO 208415 Dalian"))
    assert match.opp_no == "OPP-2201" and "same reference: 208415" in match.reasons
    assert match.score >= dedupe.POSSIBLE


def test_a_reference_is_five_characters_with_three_digits():
    assert dedupe.references("PEDIDO: 208.415 - REF AB-12 - 1X40HQ 12,400KG 2026-11-02") == {"208415"}
    assert dedupe.references("booking KX-77310, lot 4") == {"KX77310"}


def test_one_agent_with_the_same_lane_and_mode_scores_as_before(world):
    repo, kestrel, tern = world
    case(repo, "OPP-2201", kestrel, origin_loc="DALIAN")
    [match] = dedupe.find(repo.conn, probe(kestrel))
    assert match.reasons[:2] == ["same requester, lane and mode", "2 day(s) apart"]
    assert match.score == 60 + 15 + 50           # plus both places, unique here


# --------------------------------------------- C: one form, one case ---
@pytest.fixture
def app(tmp_path):
    path = tmp_path / "one-mail.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    add_party(repo, "Kestrel Forwarding", country="Brazil")
    add_party(repo, "Tern Cargo Ltda", country="Brazil")
    add_party(repo, "(TBC)", ptype="Unknown", country=None, placeholder=1)
    db.set_setting(conn, "next_opp_number", 2201)
    repo.commit()
    conn.close()
    application = create_app(path)
    application.config["TESTING"] = True
    return application


def party_id(app, key):
    conn = db.open_db(app.config["DB_PATH"])
    try:
        return conn.execute("SELECT id FROM party WHERE key = ?", (key,)).fetchone()["id"]
    finally:
        conn.close()


def rows(app, sql, *args):
    conn = db.open_db(app.config["DB_PATH"])
    try:
        return [tuple(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def form(app, **extra):
    values = {"received_on": "2026-11-04", "requester_party_id": str(party_id(app, "Kestrel Forwarding")),
              "service_mode": "FCL", "container_type": "40HQ", "container_qty": "1",
              "origin_country": "USA", "origin_loc": "MIA", "dest_country": "Chile", "dest_loc": "SAI",
              "commodity": "kiln shelves", "owner": "Victor"}
    values.update(extra)
    return values


def hidden_inputs(html):
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', html))


def test_every_render_of_the_register_form_carries_a_token(app):
    client = app.test_client()
    first = hidden_inputs(client.get("/enquiries/new").data.decode("utf-8"))["form_token"]
    second = hidden_inputs(client.get("/enquiries/new").data.decode("utf-8"))["form_token"]
    assert len(first) >= 16 and first != second
    body = client.get("/enquiries/new").data.decode("utf-8")
    assert 'data-saving="Saving..."' in body


def test_two_posts_with_one_token_create_one_case(app):
    client = app.test_client()
    token = hidden_inputs(client.get("/enquiries/new").data.decode("utf-8"))["form_token"]
    first = client.post("/enquiries/new", data=form(app, form_token=token))
    assert first.status_code == 302
    again = client.post("/enquiries/new", data=form(app, form_token=token, decision="new"),
                        follow_redirects=True)
    body = again.data.decode("utf-8")
    assert re.search(r"Already saved as OPP-2201 at \d\d:\d\d; nothing new was created\.", body)
    assert "Possible duplicate" not in body and 'name="decision"' not in body
    assert rows(app, "SELECT opp_no FROM enquiry ORDER BY id") == [("OPP-2201",)]


def test_a_refused_save_keeps_the_token(app):
    client = app.test_client()
    token = hidden_inputs(client.get("/enquiries/new").data.decode("utf-8"))["form_token"]
    refused = client.post("/enquiries/new", data=form(app, form_token=token, service_mode="Air",
                                                       container_type="", container_qty=""))
    assert refused.status_code == 400
    assert hidden_inputs(refused.data.decode("utf-8"))["form_token"] == token


def test_a_refused_registration_from_outlook_stays_from_outlook(app):
    client = app.test_client()
    tern = party_id(app, "Tern Cargo Ltda")
    mail = {"created_by": "outlook", "sender_address": "rafaela@terncargo.example",
            "sender_internal": "0", "recipient_addresses": "desk@ourco.example",
            "learn_domain": "terncargo.example", "entry_id": "ENTRY-2201",
            "conversation_id": "CONV-2201", "thread_key": "KILN SHELVES MIA SAI",
            "form_token": "tok-2201"}
    # an Air case with no weight is refused
    refused = client.post("/enquiries/new", data=form(app, requester_party_id=str(tern), service_mode="Air",
                                                       container_type="", container_qty="", **mail))
    assert refused.status_code == 400
    kept = hidden_inputs(refused.data.decode("utf-8"))
    for name in ("created_by", "sender_address", "recipient_addresses", "learn_domain",
                 "entry_id", "conversation_id", "thread_key", "form_token"):
        assert kept.get(name) == mail[name], name
    # the corrected form, as the refused page posts it
    saved = client.post("/enquiries/new", data=form(app, requester_party_id=str(tern), service_mode="Air",
                                                     container_type="", container_qty="", gross_kg="300",
                                                     **kept))
    assert saved.status_code == 302
    assert rows(app, "SELECT opp_no, created_by, entry_id FROM enquiry") == [("OPP-2201", "outlook", "ENTRY-2201")]
    assert rows(app, "SELECT party_id FROM party_domain WHERE domain = 'terncargo.example'") == [(tern,)]


def test_the_thread_check_runs_again_on_a_refused_page(app):
    client = app.test_client()
    client.post("/enquiries/new", data=form(app, conversation_id="CONV-2210", created_by="outlook",
                                            entry_id="ENTRY-2210", form_token="tok-a"))
    refused = client.post("/enquiries/new", data=form(app, conversation_id="CONV-2210", created_by="outlook",
                                                       entry_id="ENTRY-2211", service_mode="Air",
                                                       container_type="", container_qty="", form_token="tok-b"))
    body = refused.data.decode("utf-8")
    assert 'id="already"' in body and "OPP-2201" in body                # DD1: Already in OppList?


def test_a_match_from_the_same_mail_says_so_first(app):
    client = app.test_client()
    mail = {"created_by": "outlook", "entry_id": "ENTRY-2220", "conversation_id": "CONV-2220"}
    client.post("/enquiries/new", data=form(app, form_token="tok-1", **mail))
    # the same mail sent again from another render (no saved token): the panel
    page = client.post("/enquiries/new", data=form(app, form_token="tok-2", **mail))
    body = page.data.decode("utf-8")
    assert page.status_code == 200
    row = re.search(r'<tr class="band-[a-z]+"[^>]*>(.*?)</tr>', body, re.S).group(1)
    assert re.search(r"Registered from this same mail, \w{3} \d{1,2} \d\d:\d\d \((just now|\d+ minutes? ago)\)", row)
    # DD1: one row for the case (the thread's and the match), It is ... first; it sits outside the form
    assert re.search(r'<button class="button primary" type="submit" form="enquiry-form"\s+name="decision" value="duplicate"',
                     row)
    assert 'value="new"' in body                                  # New case stays there
    assert rows(app, "SELECT COUNT(*) FROM enquiry") == [(1,)]


def test_the_ticks_survive_the_duplicate_panel(app):
    """The panel re-renders the form; the vendor she ticked is still ticked."""
    client = app.test_client()
    client.post("/enquiries/new", data=form(app, form_token="tok-x"))
    page = client.post("/enquiries/new", data=form(app, form_token="tok-y", vendor="finchline.example",
                                                    **{"vendor_key_finchline.example": "Finchline",
                                                       "vendor_addr_finchline.example": "desk@finchline.example"}))
    body = page.data.decode("utf-8")
    assert "OPP-2201" in body and 'name="decision"' in body          # the panel came up
    assert 'value="finchline.example" checked' in body
    assert 'value="desk@finchline.example"' in body


# ---------------------------------------------------- D: reading the mail --
@pytest.fixture
def mail_db(repo):
    seed_reference(repo)
    party = add_party(repo, "Tern Cargo Ltda", country="Brazil")
    repo.insert("party_domain", {"domain": "terncargo.example", "party_id": party})
    repo.commit()
    return repo.conn


def a_mail(subject, body):
    return outlook_bridge.MailInfo(subject=subject, received_on="2026-11-04",
                                   sender_address="rafaela@terncargo.example", sender_name="Rafaela",
                                   to_addresses=["desk@ourco.example"], body_excerpt=body,
                                   entry_id="ENTRY-2230", conversation_id="CONV-2230")


PT_BODY = """Prezados,

Favor cotar o frete abaixo.
Termo: EXW
Coleta em Dalian
Destino: Porto Alegre
Mercadoria: prateleiras de forno
GROSS WEIGHT: 9.100 kg
Equipamento: a definir

Obrigada,
Rafaela"""


def test_a_portuguese_mail_reads_9_100_kg_as_nine_thousand_and_no_air(mail_db):
    draft = intake.build(mail_db, a_mail("COTACAO DE FRETE MARITIMO CHINA - PEDIDO: 208.415", PT_BODY))
    assert draft.values.get("service_mode") is None
    assert draft.values["qty_raw"] == "9100 kg"
    assert draft.values["gross_kg"] == 9100.0
    assert "9.100 kg read as 9,100 kg" in draft.sources["qty_raw"]
    assert any("The subject says MARITIMO" in n for n in draft.notes)


def test_every_spelling_of_the_maritime_word_is_a_sea_word():
    for word in ("MARITIMO", "Mar\u00edtimo", "MARITIMA", "mar\u00edtima", "OCEAN"):
        subject = parsing.parse_subject("COTIZACION FLETE {} // DALIAN - SANTOS".format(word))
        assert parsing.subject_sea_word(subject), word
        assert subject.service_mode is None


def test_a_weight_alone_is_not_air_when_the_body_says_sea(mail_db):
    body = "Hello,\n\nPlease quote by sea from Dalian.\nGross weight: 740 kg\n\nThanks"
    draft = intake.build(mail_db, a_mail("rate request Dalian", body))
    assert draft.values.get("service_mode") is None and draft.values["qty_raw"] == "740 kg"


def test_an_english_mail_keeps_its_decimals():
    assert parsing.parse_body_qty("Hi,\nGross weight: 1.5 kg\n").qty_raw == "Air:1.5"
    assert parsing.parse_body_qty("Hi,\nGross weight: 0.500 kg\n").qty_raw == "Air:0.5"
    assert parsing.parse_body_qty("Hi,\nRate 2.50 please\nGross weight: 9.100 kg\n").qty_raw == "Air:9.1"


def test_a_weight_that_nothing_settles_is_asked_not_filled():
    qty = parsing.parse_body_qty("Hi,\nGross weight: 9.100 kg\n")
    assert qty.mode is None and qty.qty_raw == "9.100 kg"
    assert qty.ask == ("9.100 kg", "9.1 kg", "9,100 kg")


def test_a_decimal_comma_elsewhere_settles_it():
    qty = parsing.parse_body_qty("Hi,\nVolume per crate 1,5\nGross weight: 9.100 kg\n")
    assert (qty.qty_raw, qty.reading_note) == ("Air:9100", "9.100 kg read as 9,100 kg")


# ------------------------------------------------ A: an exact repeat --
def test_an_exact_same_day_repeat_of_lost_writes_one_event(repo):
    seed_reference(repo)
    party = add_party(repo, "Kestrel Forwarding", country="Brazil")
    enquiry_id = repo.insert("enquiry", {"opp_no": "OPP-2240", "requester_party_id": party,
                                         "received_on": "2026-11-02", "status": "New"})
    for _ in range(2):
        repo.set_status(enquiry_id, "Lost", "2026-11-06", note="went with their own contract",
                        closed_reason="Price")
    assert repo.last_status_outcome == "confirmed"
    repo.set_status(enquiry_id, "Lost", "2026-11-06", note="their contract rate was 8% lower",
                    closed_reason="Price")
    assert repo.last_status_outcome == "written"
    assert [r[0] for r in repo.conn.execute(
        "SELECT note FROM status_event WHERE enquiry_id = ? AND status = 'Lost' ORDER BY id",
        (enquiry_id,))] == ["went with their own contract", "their contract rate was 8% lower"]


# ------------------------------- 10-11: a cargo line meets the Air / LCL rule --
def cargo_line(n, **values):
    out = {"cargo_{}_counted_present".format(n): "1", "cargo_{}_counted".format(n): "1",
           "cargo_{}_source".format(n): "mail"}
    out.update({"cargo_{}_{}".format(n, k): v for k, v in values.items()})
    return out


def test_one_skid_in_lbs_with_a_size_without_a_unit_registers_as_air(app):
    client = app.test_client()
    data = form(app, service_mode="Air", container_type="", container_qty="", form_token="tok-air",
                **cargo_line(0, pieces="1", pack_word="skid", dim_l="50", dim_w="40", dim_h="22",
                             dims_unit="", weight_value="372", weight_unit="lbs", weight_basis="total"))
    # 15c: a size is never registered without its unit - the line asks cm or in
    page = client.post("/enquiries/new", data=data)
    assert page.status_code == 400 and b"has no unit" in page.data
    assert rows(app, "SELECT COUNT(*) FROM enquiry") == [(0,)]
    page = client.post("/enquiries/new", data=dict(data, cargo_0_dims_unit="in"))
    assert page.status_code == 302, page.data.decode("utf-8")[:2000]
    [(unit, value, dims_unit, kg)] = rows(app, "SELECT weight_unit, weight_value, dims_unit, weight_kg FROM enquiry_cargo")
    assert (unit, value, dims_unit) == ("lbs", 372.0, "in")          # as written, with the unit chosen
    assert kg == pytest.approx(168.7, abs=0.1)
    [(mode, air_kg)] = rows(app, "SELECT service_mode, air_weight_kg FROM enquiry")
    assert mode == "Air" and air_kg == pytest.approx(168.7, abs=0.1)


def test_an_lcl_line_with_a_size_in_cm_and_no_stated_volume_meets_the_lcl_rule(app):
    client = app.test_client()
    data = form(app, service_mode="LCL", container_type="", container_qty="", form_token="tok-lcl",
                **cargo_line(0, pieces="3", pack_word="crates", dim_l="120", dim_w="100", dim_h="90",
                             dims_unit="cm", weight_value="610", weight_unit="kg", weight_basis="total"))
    assert client.post("/enquiries/new", data=data).status_code == 302
    [(cbm,)] = rows(app, "SELECT lcl_cbm FROM enquiry")
    assert cbm == pytest.approx(3.24)


def test_a_line_without_a_weight_is_named_not_a_box_that_is_not_there(app):
    client = app.test_client()
    data = form(app, service_mode="Air", container_type="", container_qty="", form_token="tok-nw",
                **cargo_line(0, pieces="2", pack_word="drums", dim_l="60", dim_w="60", dim_h="90", dims_unit=""))
    page = client.post("/enquiries/new", data=data)
    body = page.data.decode("utf-8")
    assert page.status_code == 400
    assert "cargo line 1" in body and "Edit on the Cargo card" in body
    assert "Gross weight box" not in body


# ------------------------------------------- 12: our own domain names nobody --
def test_our_own_domain_is_never_attached_to_a_party(repo):
    seed_reference(repo)
    office = add_party(repo, "JoJo JJK", ptype="Office")
    assert repo.attach_domain(office, "jojofreight.example") == ""
    assert repo.attach_domain(office, "ops.jojofreight.example") == ""
    with pytest.raises(ValueError):
        repo.insert("party_domain", {"domain": "jojofreight.example", "party_id": office})
    assert repo.attach_domain(office, "harrierlines.example") == "harrierlines.example"


def test_a_mail_from_an_office_gets_that_office_and_learns_no_domain(repo):
    seed_reference(repo)
    office = add_party(repo, "JoJo JJK", ptype="Office")
    other = add_party(repo, "JoJo BRQ", ptype="Office")
    repo.commit()
    mail = outlook_bridge.MailInfo(
        subject="Request Air Rate - FROM HARRIER TOOLS (040) 77001 to OSAKA AIRPORT", received_on="2026-11-05",
        sender_address="jjk-ops@jojofreight.example", sender_name="JJK-OPS", sender_internal=True,
        to_addresses=["desk@jojofreight.example"], to_names=["JJC-DESK"],
        body_excerpt="Hi team,\n\nPlease quote.\n1 SKID 50X40X22 @ 372 LBS\n\n"
                     "From: BRQ-Lena Hart <lena.hart@jojofreight.example>\nSent: Tuesday, November 4, 2026 9:10 AM\n"
                     "To: JJK-OPS\nSubject: FW: tools\n\nplease check\n",
        entry_id="ENTRY-2250", conversation_id="CONV-2250")
    draft = intake.build(repo.conn, mail)
    assert draft.values["requester_party_id"] == office and draft.values["requester_party_id"] != other
    assert not draft.learn_domain and not draft.new_party


def test_new_party_refuses_our_own_domain(app):
    client = app.test_client()
    page = client.post("/enquiries/new", data=form(app, requester_party_id="", new_party_key="JoJo JJK",
                                                    new_party_type="Office", new_party_country="USA",
                                                    new_party_domain="jojofreight.example", form_token="tok-np"))
    assert page.status_code == 400
    assert b"jojofreight.example is our own domain" in page.data
    assert rows(app, "SELECT COUNT(*) FROM enquiry") == [(0,)]
    assert rows(app, "SELECT COUNT(*) FROM party_domain") == [(0,)]
