"""Register from Outlook on whole mails (fixtures/intake_expected.csv +
body_samples.txt): the quoted original, 'from X to Y', containers,
commodity, weight notes, location aliases (5.2 - 2026-09-10)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from conftest import add_party, seed_reference
from opplist import intake, parsing

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _bodies():
    blocks, name, lines = {}, None, []
    for line in (FIXTURES / "body_samples.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("==="):
            if name:
                blocks[name] = "\n".join(lines).strip("\n")
            name, lines = line[3:].strip(), []
        elif name is not None:
            lines.append(line)
    if name:
        blocks[name] = "\n".join(lines).strip("\n")
    return blocks


BODIES = _bodies()
EXPECTED = list(csv.DictReader(
    [l for l in (FIXTURES / "intake_expected.csv").read_text(encoding="utf-8-sig").splitlines()
     if not l.startswith("#")]))


class Mail:
    def __init__(self, row):
        self.subject = row["subject"]
        self.received_at = row["received_at"]
        self.received_on = row["received_at"][:10]
        self.entry_id = "ENTRY-" + row["name"]
        self.conversation_id = "CONV-" + row["name"]
        self.conversation_topic = row["subject"]
        self.sender_name = row["sender_name"]
        self.sender_address = row["sender_address"]
        self.sender_internal = row["sender_address"].lower().endswith("@jojofreight.example")
        self.to_addresses = [a.strip() for a in row["to"].split(";") if a.strip()]
        self.cc_addresses = []
        self.recipients = list(self.to_addresses)
        self.body_excerpt = BODIES[row["name"]]
        self.selection_count = 1
        self.notes = []


@pytest.fixture
def book(repo):
    seed_reference(repo)
    repo.insert("country", {"name": "Spain", "region": "Europe"})
    repo.insert("country", {"name": "Canada", "region": "North America"})
    repo.insert("location", {"key": "SAV", "official_name": "Savannah", "type": "Seaport",
                             "country": "USA", "aliases": "Savannah, Savannah port"})
    repo.insert("location", {"key": "VLC", "official_name": "Valencia", "type": "Seaport",
                             "country": "Spain", "aliases": "Valencia"})
    add_party(repo, "JoJo JJC", ptype="Office", country="USA")
    add_party(repo, "JoJo JJV", ptype="Office", country="Canada")
    add_party(repo, "Tolvex", country="Chile")
    tolvex = repo.conn.execute("SELECT id FROM party WHERE key = 'Tolvex'").fetchone()[0]
    repo.insert("party_domain", {"domain": "tolvex-cl.example", "party_id": tolvex})
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    repo.insert("country", {"name": "Peru", "region": "Latin America"})
    repo.insert("country", {"name": "Mexico", "region": "Latin America"})
    repo.insert("country", {"name": "Saudi Arabia", "region": "Middle East"})
    repo.insert("location", {"key": "RUH", "official_name": "Riyadh", "type": "Airport",
                             "country": "Saudi Arabia"})
    repo.insert("location", {"key": "PHX", "official_name": "Phoenix Sky Harbor", "type": "Airport",
                             "country": "USA"})
    repo.insert("country", {"name": "Bangladesh", "region": "Asia"})
    repo.insert("country", {"name": "Paraguay", "region": "Latin America"})
    repo.insert("location", {"key": "DHAKA", "official_name": "Dhaka", "type": "City",
                             "country": "Bangladesh"})
    repo.insert("location", {"key": "DAC", "official_name": "Dhaka", "type": "Airport",
                             "country": "Bangladesh", "aliases": "Dhaka, Hazrat Shahjalal International Airport"})
    repo.insert("location", {"key": "ASU", "official_name": "Asuncion", "type": "Airport",
                             "country": "Paraguay",
                             "aliases": "Asuncion, Silvio Pettirossi, Silvio Pettirossi International, "
                                        "Silvio Pettirossi International Airport"})
    add_party(repo, "JoJo JQN", ptype="Office", country="USA")
    repo.insert("location", {"key": "SCL", "official_name": "Santiago", "type": "Airport",
                             "country": "Chile"})
    repo.insert("location", {"key": "MCO", "official_name": "Orlando International",
                             "type": "Airport", "country": "USA", "aliases": "Orlando"})
    repo.commit()
    return repo


@pytest.mark.parametrize("row", EXPECTED, ids=lambda r: r["name"])
def test_every_mail_fixture_prefills_the_way_it_should(book, row):
    draft = intake.build(book.conn, Mail(row))
    values = draft.values
    got_requester = draft.requester_key or (draft.new_party or {}).get("key", "")
    assert got_requester == row["requester"], draft.notes
    assert draft.sources.get("requester_party_id", "").startswith(row["requester_source"])
    for column in ("received_on", "service_mode", "container_type", "origin_country",
                   "origin_loc", "dest_country", "dest_loc", "commodity", "qty_raw",
                   "status", "air_weight_kg", "origin_alt", "dest_alt", "end_customer",
                   "incoterm"):
        assert str(values.get(column) or "") == row[column], column
    assert str(values.get("container_qty") or "") == row["container_qty"]
    assert "|".join(draft.references) == row["references"]


# --------------------------------------------------------------- pieces --
def test_the_quoted_mails_are_read_with_who_when_and_what():
    quoted = parsing.quoted_mails(BODIES["office-reply-quotes-the-original"])
    assert len(quoted) == 1
    mail = quoted[0]
    assert (mail.name, mail.address, mail.sent_at, mail.office) == \
        ("JJV-MEI SUN", "meisun@jojofreight.example", "2026-09-02 11:07", "JJV")
    assert mail.subject.startswith("Export shipment from Savannah port")
    assert "The shipper has confirmed" in mail.text and "Cargo weight" in mail.text
    assert "Sent:" not in mail.text and "Subject:" not in mail.text


@pytest.mark.parametrize("text,expected", [
    ("Wednesday, September 2, 2026 11:07 AM", "2026-09-02 11:07"),
    ("Monday, 14 September 2026 10:05", "2026-09-14 10:05"),
    ("quinta-feira, 15 de outubro de 2026 10:42", "2026-10-15 10:42"),
    ("lunes, 17 de agosto de 2026 9:30", "2026-08-17 09:30"),
    ("2026-09-02 11:07", "2026-09-02 11:07"),
    ("9/2/2026 11:07 AM", "2026-09-02 11:07"),
    ("some day", ""),
])
def test_sent_lines_in_the_languages_outlook_writes(text, expected):
    assert parsing.parse_sent(text) == expected


def test_the_subject_reads_from_x_to_y_and_keeps_the_country():
    parsed = parsing.parse_subject(
        "RE: Export shipment from Savannah port to Valencia, Spain 1X40HQ // ORTEGA")
    assert parsed.lanes == [("SAVANNAH", "VALENCIA, SPAIN")]
    assert parsed.containers == [(1, "40HQ")]
    assert parsed.references == []                       # 1X40HQ is a box count
    assert parsing.parse_subject("Rates Canada to Busan airport 3x20GP").lanes == \
        [("CANADA", "BUSAN")]
    assert parsing.parse_subject("RATE REQUEST // DEN -> GRU").lanes == [("DEN", "GRU")]


def test_places_resolve_through_keys_names_aliases_and_countries(book):
    index = intake.place_index(book.conn)
    assert intake.resolve_place(index, "Savannah port") == ("SAV", "USA")
    assert intake.resolve_place(index, "SAV") == ("SAV", "USA")
    assert intake.resolve_place(index, "Valencia, Spain") == ("VLC", "Spain")
    assert intake.resolve_place(index, "Valencia") == ("VLC", "Spain")
    assert intake.resolve_place(index, "Spain") == (None, "Spain")
    assert intake.resolve_place(index, "Atlantis") == (None, None)


def test_commodity_and_weight_note_come_off_the_body():
    lines = ["1x40HQ general cargo: ceramic floor tiles", "Cargo weight: roughly 22 tons"]
    assert parsing.body_commodity(lines) == ("ceramic floor tiles", "1x40HQ general cargo: ceramic floor tiles")
    assert parsing.body_commodity(["Commodity: diesel generator."]) == \
        ("diesel generator", "Commodity: diesel generator.")
    assert parsing.body_commodity(["nothing here"]) == ("", "")
    qty = parsing.parse_body_qty("\n".join(lines))
    assert qty.qty_raw == "FCL:1 x 40HQ (Cargo weight: roughly 22 tons)"
    assert qty.mode == "FCL"
    parsed = parsing.parse_qty(qty.qty_raw)
    assert (parsed.container_type, parsed.container_qty, parsed.air_weight_kg) == ("40HQ", 1, None)
    alone = parsing.parse_body_qty("Cargo weight: roughly 22 tons")
    assert alone.qty_raw == "Cargo weight: roughly 22 tons" and alone.mode is None


def test_the_home_office_is_skipped_and_an_unknown_office_is_offered_as_new(book):
    body = ("Hi\n\nFrom: JJC-AVA TRAN <AvaT@jojofreight.example>\nSent: Tuesday, September 8, 2026 3:10 PM\n"
            "To: x\nSubject: RE: something\n\nPassing this on.\n\n"
            "From: JJT-FRANK ZHOU <FrankZhou@jojofreight.example>\nSent: Tuesday, September 8, 2026 1:00 PM\n"
            "To: y\nSubject: something\n\nKindly quote from SAV to VLC.\n")
    row = {"name": "x", "subject": "RE: something", "sender_name": "JJC-VICTOR LANG",
           "sender_address": "VictorLang@jojofreight.example", "received_at": "2026-09-09 09:00",
           "to": "AvaT@jojofreight.example"}
    mail = Mail.__new__(Mail)
    Mail.__init__(mail, dict(row, name="office-reply-quotes-the-original"))
    mail.body_excerpt = body
    draft = intake.build(book.conn, mail)
    assert draft.requester_key == ""
    assert draft.new_party == {"key": "JoJo JJT", "type": "Office",
                               "why": "JJT-FRANK ZHOU signs the quoted mail; no party 'JoJo JJT' on file"}
    assert draft.values["received_on"] == "2026-09-08"
    assert draft.values["origin_loc"] == "SAV" and draft.values["dest_loc"] == "VLC"


def test_an_outside_original_is_matched_by_domain(book):
    body = ("ok\n\nFrom: Renata Vidal <renata@tolvex-cl.example>\nSent: Monday, 7 September 2026 10:00\n"
            "To: v\nSubject: rates\n\nKindly price 2x40HQ from Savannah port to Valencia, Spain.\n"
            "Commodity: cotton bales\n")
    row = {"name": "office-reply-quotes-the-original", "subject": "RE: rates",
           "sender_name": "JJC-VICTOR LANG", "sender_address": "VictorLang@jojofreight.example",
           "received_at": "2026-09-08 09:00", "to": "renata@tolvex-cl.example"}
    mail = Mail(row)
    mail.body_excerpt = body
    draft = intake.build(book.conn, mail)
    assert draft.requester_key == "Tolvex"
    assert draft.values["received_on"] == "2026-09-07"
    assert draft.values["origin_loc"] == "SAV" and draft.values["dest_loc"] == "VLC"
    assert draft.values["commodity"] == "cotton bales"
    assert (draft.values["container_type"], draft.values["container_qty"]) == ("40HQ", 2)
    assert draft.values["service_mode"] == "FCL"


# ------------------------------------ Spanish / Portuguese --
@pytest.mark.parametrize("subject,lanes", [
    ("Cotización de Quito a Orlando", [("QUITO", "ORLANDO")]),
    ("Tarifa desde Cusco hasta Dallas 4 pallets", [("CUSCO", "DALLAS")]),
    ("Quito hacia Orlando", [("QUITO", "ORLANDO")]),
    ("Pedido de cotação de Itajai para Jacksonville", [("ITAJAI", "JACKSONVILLE")]),
    ("origen: Quito / destino: Orlando", [("QUITO", "ORLANDO")]),
    ("POL Guayaquil - POD Tampa", [("GUAYAQUIL", "TAMPA")]),
    ("AOL UIO AOD MCO", [("UIO", "MCO")]),
    ("salida Cusco, llegada Dallas", [("CUSCO", "DALLAS")]),
    ("Origin: Cusco  Destination: Dallas", [("CUSCO", "DALLAS")]),
    # unchanged readings
    ("RATE REQUEST // DEN -> GRU", [("DEN", "GRU")]),
    ("SGN / HPH - CVG", [("SGN", "CVG"), ("HPH", "CVG")]),
    ("SOLICITUD DE COTIZACIÓN", []),
])
def test_lane_sentences_in_spanish_and_portuguese(subject, lanes):
    assert parsing.parse_subject(subject).lanes == lanes


def test_the_same_sentences_are_read_in_the_body():
    body = "Hola,\nnecesitamos tarifa aérea de Quito a Orlando para 12 cajas.\nGracias"
    result = parsing.parse_body_lanes(body)
    assert (result.origins, result.dests) == (["Quito"], ["Orlando"])
    assert result.evidence["origin"] == "de Quito a Orlando"
    result = parsing.parse_body_lanes("Origen: UIO\nDestino: MCO\n")
    assert (result.origins, result.dests) == (["UIO"], ["MCO"])
    result = parsing.parse_body_lanes("AOL: UIO / AOD: MCO\n")
    assert (result.origins, result.dests) == (["UIO"], ["MCO"])
    result = parsing.parse_body_lanes("Porto de origem: Itajai\nPorto de destino: Jacksonville\n")
    assert (result.origins, result.dests) == (["Itajai"], ["Jacksonville"])


@pytest.mark.parametrize("text,country", [
    ("EEUU", "USA"), ("EE.UU.", "USA"), ("EE. UU.", "USA"), ("U.S.A.", "USA"), ("USA", "USA"),
    ("Estados Unidos", "USA"), ("United States", "USA"), ("Brasil", "Brazil"),
    ("México", "Mexico"), ("Perú", "Peru"), ("España", "Spain"),
    ("Alemania", "Germany"), ("Reino Unido", "UK"), ("Japão", "Japan"),
    ("Florida", None), ("", None),
])
def test_country_aliases_in_spanish_and_portuguese(text, country):
    assert parsing.country_alias(text) == country


def test_places_resolve_through_the_country_aliases(book):
    index = intake.place_index(book.conn)
    assert intake.country_name(index, "EEUU") == "USA"
    assert intake.country_name(index, "Brasil") == "Brazil"
    assert intake.country_name(index, "Chile") == "Chile"
    assert intake.country_name(index, "Alemania") is None      # not in the country table
    assert intake.resolve_place(index, "Orlando, EEUU") == ("MCO", "USA")
    assert intake.resolve_place(index, "Estados Unidos") == (None, "USA")
    assert intake.resolve_place(index, "Cusco, Perú") == (None, "Peru")


def test_a_single_place_with_its_country_is_read_but_a_lane_wins():
    parsed = parsing.parse_subject(
        "RE: AIR DELIVERY COSTS NEEDED // TARIFA AEREA ENVIO FLORIDA- EEUU. // KRMT // TL0427-26",
        known_countries={"USA", "Chile"})
    assert parsed.lanes == []
    assert parsed.single_places == [("FLORIDA", "USA", "TARIFA AEREA ENVIO FLORIDA- EEUU.")]
    assert parsed.abbreviations == ["KRMT"]
    assert parsed.references == ["TL0427-26"]           # stripped from the thread key, kept here
    assert parsing.parse_subject("Recife, Brasil").single_places == [("Recife", "Brazil", "Recife, Brasil")]
    # a spaced dash is still 'origin - destination', as it always was
    assert parsing.parse_subject("Recife - Brasil").lanes == [("RECIFE", "BRASIL")]
    assert parsing.parse_subject("Cusco, Peru", known_countries={"Peru"}).single_places == \
        [("Cusco", "Peru", "Cusco, Peru")]
    assert parsing.parse_subject("Cusco, Ruritania").single_places == []  # not a country anyone knows
    assert parsing.parse_subject("RATE - URGENT").single_places == []
    assert parsing.parse_subject("USA - EEUU").single_places == []
    with_lane = parsing.parse_subject("UIO - MCO // Cusco, Peru", known_countries={"Peru"})
    assert with_lane.lanes and with_lane.single_places == []


def test_abbreviations_skip_codes_modes_and_incoterms():
    assert parsing.parse_subject("RATE // KRMT // FOB").abbreviations == ["KRMT"]
    assert parsing.parse_subject("RATE // UIO // AIR // EXW // OOG // POD",
                                 known_locations={"UIO"}).abbreviations == []
    assert parsing.parse_subject("RATE // TOOLONGNAME // ab").abbreviations == []


def _plain(row):
    """The fixture mail with a body that says nothing, so the subject rules
    are tested on their own."""
    mail = Mail(row)
    mail.body_excerpt = "Gracias.\n"
    return mail


def test_a_single_place_fills_the_destination_only(book):
    row = {"name": NAME, "subject": "RE: TARIFA AEREA ENVIO ORLANDO- EEUU. // KRMT",
           "sender_name": "Tomas Ibanez", "sender_address": "tomas.ibanez@tolvex.example",
           "received_at": "2026-08-31 11:20", "to": ""}
    draft = intake.build(book.conn, _plain(row))
    assert draft.values.get("dest_loc") == "MCO" and draft.values.get("dest_country") == "USA"
    assert draft.sources["dest_loc"] == "single place in subject, assumed destination"
    assert draft.evidence["dest_loc"] == "TARIFA AEREA ENVIO ORLANDO- EEUU."
    assert "origin_loc" not in draft.values and "origin_country" not in draft.values
    assert any(note.startswith("Only one place in the subject (ORLANDO, USA)") for note in draft.notes)
    assert any("'KRMT' in the subject" in note and "end customer" in note for note in draft.notes)
    # a state that is not a location on file: the country only
    row["subject"] = "RE: TARIFA AEREA ENVIO FLORIDA- EEUU."
    draft = intake.build(book.conn, _plain(row))
    assert "dest_loc" not in draft.values and draft.values["dest_country"] == "USA"
    assert any("'FLORIDA' is not a location on file" in note for note in draft.notes)
    # the same state once it is on file
    book.insert("location", {"key": "FLORIDA", "official_name": "Florida", "type": "State or Province",
                             "country": "USA"})
    book.commit()
    draft = intake.build(book.conn, _plain(row))
    assert draft.values["dest_loc"] == "FLORIDA" and draft.values["dest_country"] == "USA"
    # a body lane beats the single place
    mail = _plain(row)
    mail.body_excerpt = "Hola,\nCotización de Santiago a Orlando por favor.\n"
    draft = intake.build(book.conn, mail)
    assert (draft.values["origin_loc"], draft.values["dest_loc"]) == ("SCL", "MCO")
    assert draft.sources["dest_loc"] == "from body"


NAME = "agent-reply-single-place"


def test_the_quoted_chain_is_read_from_the_bottom_up(book):
    body = ("Attached is the insurance certificate\n\n"
            "From: JJC-FIONA LAM <FionaLam@jojofreight.example>\n"
            "Sent: Wednesday, August 26, 2026 2:40 PM\nTo: j\nSubject: RE: AIR // KRMT\n\n"
            "Kindly have the carrier correct the piece count as well.\n"
            "DEST: MIA\n\n"
            "From: Tomas Ibanez <tomas.ibanez@tolvex.example <mailto:tomas.ibanez@tolvex.example> >\n"
            "Sent: Tuesday, August 18, 2026 9:41 AM\nTo: w\nSubject: AIR // KRMT\n\n"
            "Could you send me your destination costs for this air shipment into Florida?\n"
            "AOD: MCO\nGeneral cargo\nCargo details:\n"
            "*\t5 pcs, 31,5 kg - 72 × 58 × 64 cm each\n"
            "*\t2 pcs , 9,5 kg - 45 × 38 × 30 cm each\n")
    row = {"name": NAME, "subject": "RE: AIR // KRMT", "sender_name": "Tomas Ibanez",
           "sender_address": "tomas.ibanez@tolvex.example", "received_at": "2026-08-31 11:20", "to": ""}
    mail = Mail(row)
    mail.body_excerpt = body
    draft = intake.build(book.conn, mail)
    assert [q.name for q in draft.quoted] == ["JJC-FIONA LAM", "Tomas Ibanez"]
    assert draft.values["dest_loc"] == "MCO" and draft.values["dest_country"] == "USA"
    assert draft.sources["dest_loc"] == \
        "from the quoted original mail (Tomas Ibanez 2026-08-18 09:41)"
    assert draft.evidence["dest_loc"] == "AOD: MCO"
    assert "origin_loc" not in draft.values                # the bottom mail names none
    assert draft.values["qty_raw"].startswith("5 pcs 72 x 58 x 64 cm, 31.5 kg each (157.5 kg)")
    assert draft.values["air_weight_kg"] == 239.8
    assert draft.sources["qty_raw"].startswith("from the quoted original mail (Tomas Ibanez")
    # the enquiry came in when the bottom mail was written
    assert draft.values["received_on"] == "2026-08-18"
    assert draft.sources["received_on"] == \
        "from the quoted original mail (Tomas Ibanez 2026-08-18 09:41)"
    assert draft.requester_key == "Tolvex"
    # the selected mail asks about a second shipment: its own words, its own date
    mail.body_excerpt = "Please also price 2 x 40HQ from Santiago to Orlando.\n\n" + body
    draft = intake.build(book.conn, mail)
    assert (draft.values["origin_loc"], draft.values["dest_loc"]) == ("SCL", "MCO")
    assert draft.values["qty_raw"] == "FCL:2 x 40HQ"
    assert draft.values["received_on"] == "2026-08-31"
    assert draft.sources["received_on"] == "from the mail"
    # nothing anywhere: the selected mail's own date
    mail.body_excerpt = "Thanks.\n"
    draft = intake.build(book.conn, mail)
    assert draft.values["received_on"] == "2026-08-31"
    # the mail on top of the chain is only reached when the bottom one says nothing
    mail.body_excerpt = body.replace("AOD: MCO\n", "")
    draft = intake.build(book.conn, mail)
    assert draft.values["dest_loc"] == "MIA"
    assert draft.sources["dest_loc"].startswith("from the quoted original mail (JJC-FIONA LAM")


# --------------------------------------- an unknown domain --
def _agent_row():
    return next(r for r in EXPECTED if r["name"] == "internal-forward-of-agent-request")


def test_an_unknown_domain_prefills_a_new_party_from_the_domain(book):
    draft = intake.build(book.conn, Mail(_agent_row()))
    assert "requester_party_id" not in draft.values
    new = draft.new_party
    assert (new["key"], new["type"], new["country"], new["domain"]) == \
        ("Desert Demo", "Agent", "Saudi Arabia", "desert-demo.com.sa")
    assert new["sources"]["key"] == "from the domain desert-demo.com.sa"
    assert new["sources"]["country"] == "from the .sa domain"
    assert new["similar"] == []
    assert draft.learn_domain == "desert-demo.com.sa"
    assert any("'New party' is filled in from it" in n for n in draft.notes)
    assert "destination not stated in this mail" in draft.notes
    # the TLD says nothing for .example, and a similar name is listed
    add_party(book, "Desert Demo Logistics", country="Saudi Arabia")
    book.commit()
    mail = Mail(_agent_row())
    mail.body_excerpt = mail.body_excerpt.replace("desert-demo.com.sa", "desertdemo-intl.example")
    draft = intake.build(book.conn, mail)
    assert draft.new_party["country"] == "Unknown"
    assert draft.new_party["sources"]["country"] == "not in the domain - pick one"
    assert [s["key"] for s in draft.new_party["similar"]] == ["Desert Demo Logistics"]


def test_a_domain_whose_name_is_a_party_fills_it_and_remembers_the_domain(book):
    add_party(book, "Desert Demo", country="Saudi Arabia")
    book.commit()
    draft = intake.build(book.conn, Mail(_agent_row()))
    assert draft.requester_key == "Desert Demo" and draft.new_party is None
    assert draft.sources["requester_party_id"] == \
        "domain matches party name (desert-demo ~ Desert Demo)"
    assert draft.learn_domain == "desert-demo.com.sa"
    # aliases and legal forms count too
    book.conn.execute("UPDATE party SET key = 'DD Saudi', aliases = 'Desert-Demo S.A.' WHERE key = 'Desert Demo'")
    book.commit()
    assert intake.build(book.conn, Mail(_agent_row())).requester_key == "DD Saudi"
    # two such parties: both offered, nothing filled
    add_party(book, "DesertDemo", country="Chile")
    book.commit()
    draft = intake.build(book.conn, Mail(_agent_row()))
    assert "requester_party_id" not in draft.values and draft.new_party is None
    assert sorted(m.key for m in draft.party_candidates) == ["DD Saudi", "DesertDemo"]
    assert draft.learn_domain == "desert-demo.com.sa"


def test_a_public_mailbox_names_no_company(book):
    mail = Mail(_agent_row())
    mail.body_excerpt = mail.body_excerpt.replace("omar@desert-demo.com.sa", "someone@gmail.com")
    draft = intake.build(book.conn, mail)
    assert draft.new_party["key"] == "" and draft.new_party["domain"] == ""
    assert draft.learn_domain == ""
    assert any("public mailbox" in n for n in draft.notes)


def test_the_body_says_the_mode_the_addresses_and_the_terms(book):
    row = dict(_agent_row(), name="our-quote-back-to-agent")
    draft = intake.build(book.conn, Mail(row))
    assert draft.values["service_mode"] == "Air"
    assert draft.evidence["service_mode"] == "Airline : QR"
    assert draft.values["origin_alt"] == "Sedona, AZ 86336"
    assert draft.values["end_customer"] == "Redrock Abrasives Inc"
    assert draft.sources["end_customer"].endswith("(first line of the pickup address)")
    assert draft.values["air_weight_kg"] == 10.1
    assert draft.sources["air_weight_kg"].endswith(
        "chargeable = max(actual 9 kg, volumetric 22 x 14 x 12 in = 60566.6 cm3 / 6000 = 10.1 kg)")
    assert "incoterm" not in draft.values
    assert any(n.startswith("'We need this door-to-door.' - a service scope") for n in draft.notes)
    assert "destination not stated in this mail" not in draft.notes
    # courier names do not set or change the mode; ocean words without FCL / LCL are noted
    mail = Mail(row)
    mail.body_excerpt = "Could UPS or DHL courier handle this more cheaply?\n"
    assert "service_mode" not in intake.build(book.conn, mail).values
    mail.body_excerpt = "Kindly price ocean freight on the next vessel out.\n"
    draft = intake.build(book.conn, mail)
    assert "service_mode" not in draft.values
    assert any("Ocean freight is mentioned" in n for n in draft.notes)
    mail.body_excerpt = "Ocean freight LCL, please.\n"
    assert intake.build(book.conn, mail).values["service_mode"] == "LCL"


def test_numbers_that_do_not_add_up_are_listed_to_pick_from(book):
    mail = Mail(_agent_row())
    mail.body_excerpt = "2 pallets\nDimensions: 120 x 80 x 100\nWeight: 450\n"
    draft = intake.build(book.conn, mail)
    assert "air_weight_kg" not in draft.values
    assert draft.values["qty_raw"] == "2 pallets / Dimensions: 120 x 80 x 100 / Weight: 450"
    assert any("do not add up to a weight (120, 80, 100, 450, 2 pallets)" in n for n in draft.notes)


# ---------------------------------- the mail written to us --
def _form_row():
    return next(r for r in EXPECTED if r["name"] == "our-answer-to-office-with-customer-form")


def test_the_quoted_mails_carry_their_to_and_cc_lines():
    quoted = parsing.quoted_mails(BODIES["our-answer-to-office-with-customer-form"])
    assert [q.name for q in quoted] == ["JQN-MARCO PERERA", "FREIGHTDESK"]
    assert quoted[0].to.startswith("JJC-VICTOR LANG")
    assert "JQN-LAURA BRANDT" in quoted[0].cc
    assert "JJC-" not in quoted[1].to and "JJC-" not in quoted[1].cc
    asking, below = intake._asking_mail(quoted)
    assert asking.name == "JQN-MARCO PERERA"
    assert [m.address for m in below] == ["freightdesk@lumenbrands.example"]


def test_the_company_below_the_office_is_the_end_customer_not_the_requester(book):
    draft = intake.build(book.conn, Mail(_form_row()))
    assert draft.requester_key == "JoJo JQN"
    assert draft.values["end_customer"] == "Lumen Brands"
    assert draft.sources["end_customer"] == \
        "external company below the office that asked us (lumenbrands.example)"
    assert draft.new_party is None
    assert draft.values["status"] == "Quoted"
    assert draft.sources["status"] == "our reply quotes charges"
    assert draft.evidence["status"] == "Please note destination charges :Airline THC USD 210"
    assert "Air service: 7 - 9 days economy" in draft.notes
    assert not any("door" in n for n in draft.notes)          # the long paragraph is no terms line
    assert draft.sources["air_weight_kg"].endswith("chargeable weight as stated")
    assert [(l["origin"], l["dest"]) for l in draft.lanes] == [("DAC", "ASU")]
    # the company below is an agent of ours: left alone
    tolvex = book.conn.execute("SELECT id FROM party WHERE key = 'Tolvex'").fetchone()[0]
    book.insert("party_domain", {"domain": "lumenbrands.example", "party_id": tolvex})
    book.commit()
    draft = intake.build(book.conn, Mail(_form_row()))
    assert draft.requester_key == "JoJo JQN" and "end_customer" not in draft.values
    # a direct customer below: named as the end customer by its party key
    book.conn.execute("UPDATE party SET type = 'Direct Customer' WHERE id = ?", (tolvex,))
    book.commit()
    assert intake.build(book.conn, Mail(_form_row())).values["end_customer"] == "Tolvex"


def test_names_for_a_domain_come_from_the_words_around_it():
    assert intake._name_for_domain("lumenbrands.example", ["request for Lumen Brands for X"]) == "Lumen Brands"
    assert intake._name_for_domain("desert-demo.com.sa", ["nothing here"]) == "Desert Demo"
    assert intake._name_for_domain("tolvex.example", ["Saludos, Tolvex Chile"]) == "Tolvex"
