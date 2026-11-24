"""Batch F1: messages float where she is (toasts under the top bar), and a
rate request that states the cargo's value is still a rate request.

The goods value is not a charge (mail_direction), a refused Log vendor
request says why and offers Log anyway, and the toast's two kinds.

Every company, person, mail, place, amount and date here is invented;
cases from OPP-3141."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, intake, outlook_bridge
from opplist.web import create_app

STATIC = Path(__file__).resolve().parents[1] / "opplist" / "web" / "static"

# the shape of the agent's request on OPP-1511, every word invented
GOODS_VALUE_REQUEST = (
    "Dear team,\n"
    "Please find the cargo details for our client below.\n"
    "Commodity: press frames, out of gauge\n"
    "Pieces: 5 x 40FR\n"
    "FOB:   USD 2.4 millions (total)\n"
    "-Pls advise all EXW costs based on above cargo details (pick-up, permits, port, lashing, ocean freight)\n"
    "-Pls advise free time at destination\n"
    "Regards\nNoor")


# ------------------------------------------------------------- the rule --
@pytest.mark.parametrize("text, wanted", [
    (GOODS_VALUE_REQUEST, "asking"),
    ("Invoice value USD 61,500 for the two pumps.\nPlease quote door to door to Osorno.", "asking"),
    ("Valor da mercadoria: USD 18.000\nFavor cotizar aéreo EXW.", "asking"),
    ("FOB charges: USD 150 per shipment\nplease advise if acceptable", "quoting"),
    ("Ocean freight USD 2,500/40HC all in\n... please advise if we may proceed", "quoting"),
])
def test_the_goods_value_is_not_a_charge(text, wanted):
    assert intake.mail_direction("RE: EXW REQUEST // PRESS FRAMES", text) == wanted


def test_the_words_that_make_a_quote_are_given():
    assert intake.mail_direction_why("RE: PUMPS", "Ocean freight USD 2,500/40HC all in\nplease advise") == \
        ("quoting", '"freight USD 2,500/40HC all in"')
    assert intake.mail_direction_why("RE: PUMPS", GOODS_VALUE_REQUEST) == ("asking", "")


# ------------------------------------------------------- Log anyway --
@pytest.fixture
def web(tmp_path, monkeypatch):
    path = tmp_path / "f1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    agent = add_party(repo, "Quarrystone Forwarding", country="Chile")
    repo.insert("party_domain", {"domain": "quarrystone.example", "party_id": agent})
    case = add_enquiry(repo, "OPP-3141", agent, received="2026-11-03", origin_loc="MIA", origin_country="USA",
                       dest_loc="SAI", dest_country="Chile")
    repo.commit()
    conn.close()
    holder = {"mail": None}
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: holder["mail"])
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, holder, case


def our_mail(body, to=("ops@harrowline.example",), cc=("pia@quarrystone.example",), entry="ENTRY-3141"):
    return outlook_bridge.MailInfo(
        subject="RE: PRESS FRAMES // MIA - SAI", received_on="2026-11-04", received_at="2026-11-04 09:10",
        sender_address="lena@jojofreight.example", sender_name="JJC-LENA PARK", sender_internal=True,
        to_addresses=list(to), cc_addresses=list(cc), body_excerpt=body, entry_id=entry,
        conversation_id="CONV-3141")


def vendors(path):
    conn = db.open_db(path)
    try:
        return [r[0] for r in conn.execute(
            "SELECT p.key FROM vendor_request v JOIN party p ON p.id = v.vendor_party_id ORDER BY v.id")]
    finally:
        conn.close()


def test_a_refused_log_says_why_and_log_anyway_logs_it(web):
    client, path, holder, case = web
    holder["mail"] = our_mail("Hi all,\nOcean freight USD 2,500/40HC all in\nplease advise if we may proceed\nLena",
                              cc=())
    url = "/enquiries/{}/vendors/from-outlook".format(case)
    body = client.post(url, follow_redirects=True).data.decode("utf-8")
    toast = body[body.index('<div class="toast warn">'):]
    toast = toast[:toast.index("</div>")]
    assert ("Reads as us giving rates (&#34;freight USD 2,500/40HC all in&#34;) - nothing was logged."
            in toast or 'Reads as us giving rates ("freight USD 2,500/40HC all in") - nothing was logged.' in toast)
    assert 'name="anyway" value="1"' in toast and 'name="entry_id" value="ENTRY-3141"' in toast
    assert ">Log anyway</button>" in toast
    assert vendors(path) == []
    # Log anyway: the outside recipient, read as an asking mail would be
    page = client.post(url, data={"anyway": "1", "entry_id": "ENTRY-3141"}, follow_redirects=True)
    assert vendors(path) == ["Harrowline"]
    assert '<div class="toast done">' in page.data.decode("utf-8")
    # the same mail again: the same duplicate check as ever
    client.post(url, data={"anyway": "1", "entry_id": "ENTRY-3141"})
    assert vendors(path) == ["Harrowline"]


def test_log_anyway_on_a_mail_we_did_not_send_leaves_the_requester_out(web):
    client, path, holder, case = web
    # an outside mail copying a vendor and the requester: not one we sent out
    holder["mail"] = outlook_bridge.MailInfo(
        subject="PRESS FRAMES // MIA - SAI", received_on="2026-11-05", received_at="2026-11-05 08:00",
        sender_address="rui@gantrymarine.example", sender_name="Rui Okonjo", sender_internal=False,
        to_addresses=["lena@jojofreight.example"], cc_addresses=["ops@harrowline.example", "pia@quarrystone.example"],
        body_excerpt="Rui here - copying Harrowline on the frames.", entry_id="ENTRY-3142",
        conversation_id="CONV-3142")
    url = "/enquiries/{}/vendors/from-outlook".format(case)
    body = client.post(url, follow_redirects=True).data.decode("utf-8")
    assert "The selected mail is not one we sent out asking a vendor for a rate - nothing was logged." in body
    assert ">Log anyway</button>" in body
    client.post(url, data={"anyway": "1", "entry_id": "ENTRY-3142"})
    assert vendors(path) == ["Harrowline"]                     # never Quarrystone, the requester


def test_log_anyway_refuses_a_mail_other_than_the_one_shown(web):
    client, path, holder, case = web
    holder["mail"] = our_mail("Ocean freight USD 2,500/40HC all in\nplease advise", entry="ENTRY-OTHER")
    page = client.post("/enquiries/{}/vendors/from-outlook".format(case),
                       data={"anyway": "1", "entry_id": "ENTRY-3141"}, follow_redirects=True)
    assert "The selection in Outlook changed since" in page.data.decode("utf-8")
    assert vendors(path) == []


def test_the_goods_value_request_logs_without_asking(web):
    client, path, holder, case = web
    holder["mail"] = our_mail(GOODS_VALUE_REQUEST)
    page = client.post("/enquiries/{}/vendors/from-outlook".format(case), follow_redirects=True)
    assert "Logged 1 vendor request: Harrowline" in page.data.decode("utf-8")


# ------------------------------------------------------------- toasts --
def test_the_toasts_kinds_and_where_they_sit(web):
    client, path, holder, case = web
    # nothing to change: amber; a note added: green
    page = client.post("/enquiries/{}/note".format(case), data={"note": ""}, follow_redirects=True)
    assert '<div class="toast warn">' in page.data.decode("utf-8")
    page = client.post("/enquiries/{}/note".format(case), data={"note": "rang their desk"}, follow_redirects=True)
    body = page.data.decode("utf-8")
    assert '<div class="toast done">' in body and "Note added" in body
    assert 'class="flash"' not in body
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    rule = re.search(r"\.toasts \{([^}]*)\}", css).group(1)
    assert "position: fixed" in rule and "top: var(--topbar-h" in rule
    assert ".toasts, .no-print { display: none !important; }" in css           # hidden in print
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "var TOAST_MS = 6000;" in js
    # only the done kind fades; a warning waits for x or the next action
    arm = js[js.index("function toastArm"):js.index("function toastsArm")]
    assert 'if (!t.classList.contains("done")) { return; }' in arm and "mouseenter" in arm
    assert 'querySelectorAll(".toast.warn"), toastGone' in js
    # refresh() hands the new page's messages to the toasts
    refresh = js[js.index("function refresh(html)"):]
    assert "window.OppToasts.add(doc)" in refresh[:2500]
