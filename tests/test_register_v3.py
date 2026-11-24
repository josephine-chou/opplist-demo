"""Batch 15c: the register page is the case page's cards - Request, Routes,
Cargo, Register as - all open for input, with one Register at the bottom.
Units as written (lbs stays lbs, a size keeps its inches), a size without a
unit is asked and holds Register until it is answered, two destinations are
two routes, and Register as Quoted writes the Quoted event on its date.

Every company, person, mail, place pairing, number, reference and date here
is invented; cases from OPP-2801."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from conftest import add_party, seed_reference
from opplist import db, migrate_master, outlook_bridge
from opplist.web import create_app


# ------------------------------------------------------------- helpers --
class _FormFields(HTMLParser):
    """The fields a browser would send from form#enquiry-form: inputs (a
    checkbox or radio only when checked), the selected option of each select
    (else its first), textareas; nothing inside a <template>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields, self.inside, self.template = [], False, 0
        self._select, self._first, self._chosen, self._area = None, None, None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and a.get("id") == "enquiry-form":
            self.inside = True
        if tag == "template":
            self.template += 1
        if not self.inside or self.template or "disabled" in a:
            return
        if tag == "input" and a.get("name"):
            kind = a.get("type", "text")
            if kind in ("checkbox", "radio"):
                if "checked" in a:
                    self.fields.append((a["name"], a.get("value", "on")))
            elif kind not in ("submit", "button"):
                self.fields.append((a["name"], a.get("value", "")))
        elif tag == "select" and a.get("name"):
            self._select, self._first, self._chosen = a["name"], None, None
        elif tag == "option" and self._select:
            value = a.get("value")
            self._pending = value
            if self._first is None:
                self._first = value
            if "selected" in a:
                self._chosen = value
        elif tag == "textarea" and a.get("name"):
            self._area = [a["name"], ""]

    def handle_data(self, data):
        if self._area is not None:
            self._area[1] += data
        if self._select and getattr(self, "_pending", 0) is None:
            # an <option> without value= sends its text
            self._pending = data.strip()
            if self._first is None:
                self._first = self._pending
            if self._chosen is None and self._chosen_text_pending:
                self._chosen = self._pending

    def handle_endtag(self, tag):
        if tag == "template":
            self.template -= 1
        if tag == "form" and self.inside:
            self.inside = False
        if tag == "select" and self._select:
            self.fields.append((self._select, self._chosen if self._chosen is not None else (self._first or "")))
            self._select = None
        if tag == "textarea" and self._area is not None:
            self.fields.append(tuple(self._area))
            self._area = None

    _chosen_text_pending = False


def fields_of(html) -> dict:
    """The page's form as a dict of lists (what Flask's test client posts)."""
    parser = _FormFields()
    # options written as <option selected>lbs</option> carry their text as the value
    html = re.sub(r"<option( selected)?>([^<]*)</option>",
                  lambda m: '<option value="{}"{}>{}</option>'.format(m.group(2).strip(), m.group(1) or "", m.group(2)),
                  html)
    parser.feed(html)
    out = {}
    for name, value in parser.fields:
        out.setdefault(name, []).append(value)
    return out


def with_values(fields, **changes):
    out = {k: list(v) for k, v in fields.items()}
    for name, value in changes.items():
        out[name] = value if isinstance(value, list) else [value]
    return out


def rows(path, sql, *args):
    conn = db.open_db(path)
    try:
        return [tuple(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def a_mail(subject, body, sender="ana@marlowfreight.example", to=("desk@jojofreight.example",), internal=False,
           on="2026-11-04", key="2801"):
    return outlook_bridge.MailInfo(subject=subject, received_on=on, received_at=on + " 10:15",
                                   sender_address=sender, sender_name=sender.split("@")[0].title(),
                                   sender_internal=internal, to_addresses=list(to), body_excerpt=body,
                                   entry_id="ENTRY-" + key, conversation_id="CONV-" + key)


CRATE_MAIL = ("Hola,\n\nFavor cotizar aereo EXW.\nRecogida: Corvid Pumps 41 Kiln Rd Dayton, OH 45402\n"
              "1 crate\n40 x 44 x 36\n900 lbs\n\nSaludos\nAna")


@pytest.fixture
def web(tmp_path, monkeypatch):
    path = tmp_path / "reg.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    for key, country in (("VALPARAISO", "Chile"), ("HOUSTON", "USA")):
        repo.insert("location", {"key": key, "country": country, "type": "City"})
    marlow = add_party(repo, "Marlow Freight", country="Chile")
    repo.insert("party_domain", {"domain": "marlowfreight.example", "party_id": marlow})
    harbor = add_party(repo, "Andes Cargo Ltda", country="Chile")
    tbc = add_party(repo, "(TBC)", ptype="Unknown", country=None, placeholder=1)
    db.set_setting(conn, "next_opp_number", 2801)
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    holder = {"mail": None}
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: holder["mail"])
    return app.test_client(), path, holder, {"marlow": marlow, "harbor": harbor, "tbc": tbc}


def register_page(client, holder, mail):
    holder["mail"] = mail
    page = client.post("/enquiries/from-outlook")
    assert page.status_code == 200
    return page.data.decode("utf-8")


# ------------------------------------------------- 3. units as written --
def test_a_size_without_a_unit_and_lbs_are_shown_as_written_and_asked(web):
    client, path, holder, ids = web
    body = register_page(client, holder, a_mail("RE: NUEVA COTIZACION AEREA 04/11 REF: 77120", CRATE_MAIL))
    line = body[body.index('id="reg-line-0"'):body.index('<template id="reg-line-tpl"')]
    # lbs stays lbs, with where it came from under it
    assert '<option selected>lbs</option>' in line and 'value="900"' in line
    assert "from the mail: 900 lbs" in line and "from the mail: 1 crate" in line
    # a size without a unit is not guessed: the picker asks, the line gives both readings
    assert '<select name="cargo_0_dims_unit" data-f="dims_unit" class="ask"><option value="" selected>cm or in?</option>' in line
    assert "from the mail: 40 x 44 x 36, no unit" in line
    ask = line[line.index('class="line-asks checks"'):]
    assert "The size 40 x 44 x 36 has no unit. As cm: 0.063 cbm, 6,443 kg per cbm, far too dense for one crate. " \
           "As in: 1.038 cbm, 393 kg per cbm." in ask
    assert re.search(r'class="button small-button primary" data-answer="unit" data-line="0" data-value="in">Use in<', ask)
    assert 'data-answer="unit" data-line="0" data-value="cm">Use cm<' in ask
    # the rail says what is left; the registered subject is in the header and on the form
    assert re.search(r'data-rail="cargo".*?<span class="state">unit to choose</span>', body, re.S)
    assert "Mail: <b class=\"mono\">RE: NUEVA COTIZACION AEREA 04/11 REF: 77120</b>" in body
    assert fields_of(body)["source_subject"] == ["RE: NUEVA COTIZACION AEREA 04/11 REF: 77120"]
    # gone from the register page (item 4)
    for name in ("air_weight_kg", "lcl_cbm", "commodity", "qty_raw\" id", "container_type", "container_qty",
                 "gross_kg", "volume_cbm"):
        assert 'name="{}"'.format(name) not in body.replace("cargo_0_", "x_").replace("cargo___I___", "x_"), name
    assert "Deliver to" not in body and "Quantity as received" not in body


def test_register_waits_for_the_size_s_unit_then_stores_lbs_and_inches(web):
    client, path, holder, ids = web
    fields = fields_of(register_page(client, holder, a_mail("RFQ AIR MIA - SAI REF 77121", CRATE_MAIL)))
    assert fields["cargo_0_dims_unit"] == [""] and fields["cargo_0_weight_unit"] == ["lbs"]
    page = client.post("/enquiries/new", data=fields)
    assert page.status_code == 400
    assert "the size 40 x 44 x 36 has no unit: choose Use in or Use cm on the line" in page.data.decode("utf-8")
    assert rows(path, "SELECT COUNT(*) FROM enquiry") == [(0,)]
    fields = fields_of(page.data.decode("utf-8"))                 # the refused page keeps everything
    assert fields["cargo_0_weight_value"] == ["900"] and fields["created_by"] == ["outlook"]
    page = client.post("/enquiries/new", data=with_values(fields, cargo_0_dims_unit="in"))
    assert page.status_code == 302, page.data.decode("utf-8")[:3000]
    [line] = rows(path, "SELECT weight_value, weight_unit, weight_kg, dim_l, dim_w, dim_h, dims_unit, length_cm, "
                        "source FROM enquiry_cargo")
    assert line[:2] == (900.0, "lbs") and line[2] == pytest.approx(408.233, abs=0.001)
    assert line[3:7] == (40.0, 44.0, 36.0, "in") and line[7] == pytest.approx(101.6) and line[8] == "mail"
    [(gross, volume, air, mode, subject)] = rows(path, "SELECT gross_kg, volume_cbm, air_weight_kg, service_mode, "
                                                       "source_subject FROM enquiry")
    assert gross == pytest.approx(408.2, abs=0.05) and volume == pytest.approx(1.038, abs=0.001)
    assert air == pytest.approx(408.2, abs=0.05) and mode == "Air" and subject == "RFQ AIR MIA - SAI REF 77121"


def test_a_written_cm_that_only_fits_as_inches_is_asked_but_does_not_hold_register(web):
    client, path, holder, ids = web
    mail = a_mail("RFQ AIR MIA - SAI REF 77122", CRATE_MAIL.replace("40 x 44 x 36", "40 x 44 x 36 cm"), key="2802")
    body = register_page(client, holder, mail)
    line = body[body.index('id="reg-line-0"'):body.index('<template id="reg-line-tpl"')]
    assert "The size is written 40 x 44 x 36 cm. As cm: 0.063 cbm, 6,443 kg per cbm, far too dense for one crate. " \
           "As in: 1.038 cbm, 393 kg per cbm." in line
    assert 'data-answer="unit" data-line="0" data-value="in">Use in<' in line
    assert 'data-answer="keep" data-line="0" data-value="size_unit">Use cm<' in line
    page = client.post("/enquiries/new", data=fields_of(body))
    assert page.status_code == 302
    assert rows(path, "SELECT dims_unit FROM enquiry_cargo") == [("cm",)]


def test_the_preview_follows_the_lines_and_writes_nothing(web):
    client, path, holder, ids = web
    fields = fields_of(register_page(client, holder, a_mail("RFQ AIR MIA - SAI REF 77123", CRATE_MAIL, key="2803")))
    before = rows(path, "SELECT COUNT(*) FROM change_log")
    res = client.post("/enquiries/new/cargo/preview", data=fields).get_json()
    assert res["ok"] and res["blocking"] and res["rail"] == "unit to choose" and res["count"] >= 1
    assert "As in: 1.038 cbm" in res["asks"]["0"]
    res = client.post("/enquiries/new/cargo/preview", data=with_values(fields, cargo_0_dims_unit="in")).get_json()
    assert not res["blocking"] and res["asks"]["0"].strip() == ""
    assert ">Chargeable<" in res["html"] and "408.2 kg" in res["html"]
    assert rows(path, "SELECT COUNT(*) FROM enquiry") == [(0,)]
    assert rows(path, "SELECT COUNT(*) FROM change_log") == before


# ----------------------------------------------- routes: two destinations --
def test_two_destinations_in_one_mail_are_two_routes_and_a_line_can_be_one_route_s(web):
    client, path, holder, ids = web
    mail = a_mail("RFQ FCL HOUSTON - SAI / VALPARAISO", "Hello,\n\nPlease quote FCL.\n2 x 40HC\n\nThanks", key="2804")
    body = register_page(client, holder, mail)
    assert 'data-route-key="1"' in body and "Route <span class=\"r-letter\">B</span>" in body
    fields = fields_of(body)
    assert fields["dest_loc"] == ["SAI"] and fields["route_1_dest_loc"] == ["VALPARAISO"]
    assert fields["route_1_dest_country"] == ["Chile"] and fields["route_1_service_mode"] == ["FCL"]
    assert fields["cargo_0_container_qty"] == ["2"] and fields["cargo_0_container_type"] == ["40HQ"]
    # the line goes to route B only (the ticks the page builds)
    data = with_values(fields, origin_country="USA", origin_loc="HOUSTON", route_1_origin_country="USA",
                       route_1_origin_loc="HOUSTON", cargo_0_routes_present="1", cargo_0_route=["1"],
                       requester_party_id=str(ids["marlow"]))
    page = client.post("/enquiries/new", data=data)
    assert page.status_code == 302, page.data.decode("utf-8")[:3000]
    routes_ = rows(path, "SELECT id, seq, origin_loc, dest_loc, dest_country, service_mode, source FROM enquiry_route "
                         "ORDER BY seq")
    assert [r[1:] for r in routes_] == [(1, "HOUSTON", "SAI", "Chile", "FCL", "mail"),
                                        (2, "HOUSTON", "VALPARAISO", "Chile", "FCL", "mail")]
    assert rows(path, "SELECT route_ids FROM enquiry_cargo") == [(str(routes_[1][0]),)]


# ---------------------------------------------------------- Register as --
def test_register_as_quoted_writes_the_quoted_event_on_its_date(web):
    client, path, holder, ids = web
    fields = fields_of(client.get("/enquiries/new").data.decode("utf-8"))
    assert fields["status"] == ["New"]
    data = with_values(fields, received_on="2026-11-04", requester_party_id=str(ids["marlow"]),
                       origin_country="USA", origin_loc="MIA", dest_country="Chile", dest_loc="SAI",
                       service_mode="Air", cargo_0_pieces="3", cargo_0_pack_word="cartons", cargo_0_weight_value="64",
                       status="Quoted", status_at="2026-11-02")
    page = client.post("/enquiries/new", data=data)
    assert page.status_code == 400 and b"the quote date is before the date it came in" in page.data
    page = client.post("/enquiries/new", data=with_values(data, status_at="2026-11-06"))
    assert page.status_code == 302
    [(case_id, status)] = rows(path, "SELECT id, status FROM enquiry")
    assert status == "Quoted"
    assert rows(path, "SELECT at, status FROM status_event WHERE enquiry_id = ? ORDER BY id", case_id) == \
        [("2026-11-04", "New"), ("2026-11-06", "Quoted")]


def test_a_new_enquiry_without_a_mail_has_empty_cards_and_registers(web):
    client, path, holder, ids = web
    body = client.get("/enquiries/new").data.decode("utf-8")
    for part in ('id="request"', 'id="routes"', 'id="cargo"', 'id="regas"', 'id="reg-submit"', ">Register<"):
        assert part in body, part
    assert 'name="source_subject"' in body and "from the mail:" not in body and "src-chip" not in body
    assert len(re.findall(r'class="reg-line" data-idx="\d+"', body)) == 1     # one empty line editor
    assert '<main class="case-page reg-page">' in body and '<fieldset class="f wide tags">' in body
    depth = deepest = 0
    for tag in re.findall(r"<(/?)form\b", body):
        depth += -1 if tag else 1
        deepest = max(deepest, depth)
    assert deepest == 1                                    # nothing nests in the register form
    fields = fields_of(body)
    data = with_values(fields, received_on="2026-11-04", requester_party_id=str(ids["harbor"]),
                       origin_country="USA", origin_loc="LAX", dest_country="Chile", dest_loc="SAI",
                       service_mode="LCL", source_subject="LCL LAX - SAI glass jars",
                       cargo_0_pieces="4", cargo_0_pack_word="pallets", cargo_0_weight_value="1,260",
                       cargo_0_cbm_value="150", cargo_0_cbm_unit="cft", cargo_0_commodity="glass jars")
    page = client.post("/enquiries/new", data=data)
    assert page.status_code == 302, page.data.decode("utf-8")[:3000]
    [(subject, lcl, commodity, created_by)] = rows(path, "SELECT source_subject, lcl_cbm, commodity, created_by FROM enquiry")
    assert subject == "LCL LAX - SAI glass jars" and lcl == pytest.approx(4.25, abs=0.01)
    assert commodity == "glass jars" and created_by == "manual"
    assert rows(path, "SELECT cbm_written, source FROM enquiry_cargo") == [("150 cft", "manual")]


def test_fcl_containers_and_a_free_text_mode_are_met_by_the_lines(web):
    client, path, holder, ids = web
    fields = fields_of(client.get("/enquiries/new").data.decode("utf-8"))
    base = with_values(fields, received_on="2026-11-04", requester_party_id=str(ids["harbor"]),
                       origin_country="USA", origin_loc="HOUSTON", dest_country="Chile", dest_loc="VALPARAISO")
    page = client.post("/enquiries/new", data=with_values(base, service_mode="FCL"))
    assert page.status_code == 400 and b"FCL needs its containers" in page.data
    page = client.post("/enquiries/new", data=with_values(base, service_mode="RORO"))
    assert page.status_code == 400 and b"RORO needs a cargo line: describe the pieces or the size" in page.data
    page = client.post("/enquiries/new", data=with_values(base, service_mode="FCL", cargo_0_container_qty="3",
                                                          cargo_0_container_type="40HQ", cargo_0_weight_value="21",
                                                          cargo_0_weight_unit="t", cargo_0_weight_basis="container"))
    assert page.status_code == 302, page.data.decode("utf-8")[:3000]
    [(qty, ctype, gross)] = rows(path, "SELECT container_qty, container_type, gross_kg FROM enquiry")
    assert (qty, ctype) == (3, "40HQ") and gross == pytest.approx(63000.0)


# ------------------------------------------ what the register page keeps --
def test_a_refused_register_keeps_every_field_typed(web):
    client, path, holder, ids = web
    fields = fields_of(client.get("/enquiries/new").data.decode("utf-8"))
    data = with_values(fields, received_on="2026-11-04", requester_party_id=str(ids["marlow"]),
                       origin_country="USA", origin_loc="LAX", dest_country="Chile", dest_loc="SAI",
                       service_mode="Air", remarks="keep this note", cargo_0_pieces="2", cargo_0_pack_word="drums",
                       cargo_0_weight_value="about 180 kg", cargo_0_commodity="resin")
    page = client.post("/enquiries/new", data=data)
    assert page.status_code == 400
    body = page.data.decode("utf-8")
    assert "needs a number" in body
    kept = fields_of(body)
    assert kept["cargo_0_weight_value"] == ["about 180 kg"] and kept["cargo_0_pack_word"] == ["drums"]
    assert kept["remarks"] == ["keep this note"] and kept["origin_loc"] == ["LAX"] and kept["dest_loc"] == ["SAI"]
    assert rows(path, "SELECT COUNT(*) FROM enquiry") == [(0,)]


def test_a_reply_to_the_requester_registers_as_quoted_on_the_sent_date_and_offers_no_vendors(web):
    client, path, holder, ids = web
    mail = a_mail("RE: RATE REQUEST // MIA - SAI // AIR",
                  "Hi Ana,\n\nPlease find our rates attached.\n\nFrom: Ana Ruiz <ana@marlowfreight.example>\n"
                  "Sent: Monday, November 2, 2026 9:00 AM\nSubject: RATE REQUEST // MIA - SAI // AIR\n\n"
                  "Please quote 2 pallets, 310 kg.", sender="desk@jojofreight.example",
                  to=("ana@marlowfreight.example",), internal=True, on="2026-11-05", key="2805")
    body = register_page(client, holder, mail)
    assert 'id="vendors"' not in body and "We sent this mail out" not in body
    fields = fields_of(body)
    assert fields["status"] == ["Quoted"] and fields["status_at"] == ["2026-11-05"]
    assert fields["requester_party_id"] == [str(ids["marlow"])] and fields["received_on"] == ["2026-11-02"]
    assert "dated the day the mail went out: 2026-11-05" in body
    page = client.post("/enquiries/new", data=fields)
    assert page.status_code == 302, page.data.decode("utf-8")[:3000]
    [(case_id,)] = rows(path, "SELECT id FROM enquiry")
    assert rows(path, "SELECT at, status FROM status_event WHERE enquiry_id = ? ORDER BY id", case_id) == \
        [("2026-11-02", "New"), ("2026-11-05", "Quoted")]


def test_a_fresh_ask_to_a_carrier_offers_it_and_registers_as_new(web):
    client, path, holder, ids = web
    mail = a_mail("RATE REQUEST // LAX - SAI", "Hi Tomas,\n\nPlease advise your rates.\n", sender="desk@jojofreight.example",
                  to=("tomas@pelicanlines.example",), internal=True, key="2806")
    body = register_page(client, holder, mail)
    assert 'name="vendor" value="pelicanlines.example" checked' in body
    fields = fields_of(body)
    assert fields["status"] == ["New"] and fields["requester_party_id"] == [str(ids["tbc"])]


def test_a_vendor_on_two_domains_is_one_party_when_given_one_name(web):
    client, path, holder, ids = web
    mail = a_mail("RE: Help needed in Texas", "Hi Nora,\n\nCan you handle a job like this from your side? We need a hazmat pick-up "
                  "in Houston, 4 x 40GP.\n\nFrom: Nora Pike <nora@brantel-usa.example>\nSent: Tuesday\n"
                  "Subject: Help needed in Texas\n\nWe handle hazmat.", sender="desk@jojofreight.example",
                  to=("nora@brantel-usa.example", "ops@brantelflorida.example"), internal=True, key="2807")
    body = register_page(client, holder, mail)
    assert 'name="vendor" value="brantel-usa.example" checked' in body
    assert 'name="vendor" value="brantelflorida.example" checked' in body
    fields = fields_of(body)
    assert fields["status"] == ["New"]
    data = with_values(fields, **{"vendor_key_brantel-usa.example": "Brantel", "vendor_key_brantelflorida.example": "Brantel",
                                  "origin_country": "USA", "origin_loc": "HOUSTON", "dest_country": "Chile",
                                  "dest_loc": "VALPARAISO", "decision": "new"})
    page = client.post("/enquiries/new", data=data)
    assert page.status_code == 302, page.data.decode("utf-8")[:3000]
    [(party_id,)] = rows(path, "SELECT id FROM party WHERE key = 'Brantel'")
    assert rows(path, "SELECT domain FROM party_domain WHERE party_id = ? ORDER BY domain", party_id) == \
        [("brantel-usa.example",), ("brantelflorida.example",)]
    assert rows(path, "SELECT vendor_party_id FROM vendor_request") == [(party_id,)]
    assert rows(path, "SELECT container_qty, container_type FROM enquiry") == [(4, "40GP")]


def test_an_unknown_domain_opens_new_party_filled_in_and_the_card_shows_the_chargeable_weight(web):
    client, path, holder, ids = web
    mail = a_mail("RFQ air MIA - SAI", "Dear team,\nPlease quote air freight EXW.\n2 cartons 40 x 30 x 30 cm 22 kg\n"
                  "Shipper: Corvid Pumps Inc\nBest regards\nLuis", sender="luis@andes-demo.cl", key="2808")
    body = register_page(client, holder, mail)
    fieldset = body[body.index('<fieldset class="inline-add" id="new-party"'):]
    fieldset = fieldset[:fieldset.index("</fieldset>")]
    assert " hidden" not in fieldset.split(">", 1)[0]
    assert 'value="Andes Demo"' in fieldset and "from the domain andes-demo.cl" in fieldset
    assert '<option value="Chile" selected>' in fieldset and "Andes Cargo Ltda" in fieldset
    strip = body[body.index('class="cargo-strip'):]
    strip = strip[:strip.index("</div>\n</div>") if "</div>\n</div>" in strip else 2000]
    assert ">Chargeable<" in strip and "22.0 kg" in strip


# -------------------------------------- one spelling, two places (master) --
@pytest.fixture
def master(tmp_path, monkeypatch):
    path = tmp_path / "master.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    for name, region, iso2 in (("USA", "North America", "US"), ("Chile", "Latin America", "CL"),
                               ("Unknown", "Undefined", None)):
        repo.insert("country", {"name": name, "region": region, "iso2": iso2})
    ids = {}
    for key, sub in (("SPRINGFIELD, IL", "IL"), ("SPRINGFIELD, MO", "MO")):
        ids[key] = repo.add_master_location(key, "USA", "city", name="Springfield", subdivision=sub, source="phase1")["id"]
        repo.add_master_alias("SPRINGFIELD", ids[key], "legacy_key")
    ids["VALPARAISO"] = repo.add_master_location("VALPARAISO", "Chile", "city", name="Valparaiso", source="phase1")["id"]
    conn.execute("ALTER TABLE location RENAME TO location_old")
    conn.execute(migrate_master.LOCATION_VIEW_SQL)
    party = add_party(repo, "Marlow Freight", country="Chile")
    repo.insert("party_domain", {"domain": "marlowfreight.example", "party_id": party})
    db.set_setting(conn, "next_opp_number", 2811)
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    monkeypatch.setattr(outlook_bridge, "read_selection",
                        lambda: a_mail("Rates from Springfield to Valparaiso // 1X40HQ", "Hi,\nPlease quote.\n", key="2811"))
    return app.test_client(), path, ids


def test_one_spelling_two_places_offers_the_choice_and_saving_takes_the_pick(master):
    client, path, ids = master
    body = client.post("/enquiries/from-outlook").data.decode("utf-8")
    assert "One spelling, several places" in body
    assert 'data-target="origin_loc" data-place="SPRINGFIELD, IL"' in body
    assert 'data-target="origin_loc" data-place="SPRINGFIELD, MO"' in body
    fields = fields_of(body)
    assert fields["dest_loc"] == ["VALPARAISO"]
    page = client.post("/enquiries/new", data=with_values(fields, origin_loc="SPRINGFIELD, MO", origin_country="USA"))
    assert page.status_code == 302, page.data.decode("utf-8")[:3000]
    assert rows(path, "SELECT origin_loc, origin_loc_id, dest_loc FROM enquiry WHERE opp_no = 'OPP-2811'") == \
        [("SPRINGFIELD, MO", ids["SPRINGFIELD, MO"], "VALPARAISO")]
