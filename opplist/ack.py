"""What the acknowledgment's code left behind (batch A1;
FX4, 2026-11-12: Acknowledge, its template and its draft are gone - she
has her own template tool).

Still used: the subject every number-carrying subject is written with
(numbered_subject: the team site's
reply), whether the registered mail is a colleague's
forward and whom it came from (is_colleague_forward, forward_target:
firebase_reply), the Acknowledged event's date (acknowledged_on: the case
page), and the questions the case leaves open (questions: wait.prefill).
"""

from __future__ import annotations

import re

from . import ack_questions, parsing
from . import routes as routes_mod


# --------------------------------------------------------------- the case --
def _route_a(conn, enquiry_id):
    live = routes_mod.routes_of(conn, enquiry_id) if routes_mod.has_routes(conn) else []
    return live[0] if live else None


def _lines(conn, enquiry_id) -> list:
    return conn.execute("SELECT * FROM enquiry_cargo WHERE enquiry_id = ? AND voided = 0 ORDER BY seq",
                        (enquiry_id,)).fetchall()


def facts(conn, row) -> dict:
    """What the case already answers, for the standard questions."""
    keys = row.keys()
    lines = _lines(conn, row["id"])
    route = _route_a(conn, row["id"])

    def case(name):
        return row[name] if name in keys else None

    pieces = sum(int(l["pieces"] or 0) for l in lines)
    weight = case("gross_kg") or case("air_weight_kg") or sum(float(l["weight_kg"] or 0) for l in lines) or None
    cbm = case("volume_cbm") or case("lcl_cbm") or sum(float(l["cbm"] or 0) for l in lines) or None
    dims = any(l["dim_l"] or l["length_cm"] or (l["dims_text"] or "").strip() for l in lines)
    tags = {t.strip().casefold() for t in (case("special_handling") or "").replace(";", ",").split(",") if t.strip()}
    return {
        "pieces": pieces or None, "weight": weight, "cbm": cbm, "dimensions": dims or None,
        "commodity": (case("commodity") or "").strip() or None,
        "container_type": case("container_type"), "container_qty": case("container_qty"),
        "origin_alt": (route["origin_alt"] if route else None) or case("origin_alt"),
        "dest_alt": (route["dest_alt"] if route else None) or case("dest_alt"),
        "dg": True if "dg" in tags else None,
    }


def gaps(conn, row) -> list:
    """forms.missing's fields for route A with the case's values (the
    Still missing of the case page), the requester and date left out."""
    from .web import forms
    route = _route_a(conn, row["id"])
    values = {k: row[k] for k in row.keys()}
    if route:
        values.update({k: route.get(k) for k in routes_mod.LANE_FIELDS if k in route})
        values["service_scope"] = route.get("service_scope")
    return [f for f in forms.missing(values) if f not in ("requester_party_id", "received_on")]


def questions(conn, row) -> list:
    """The words to ask: the gaps, then the standard questions not answered."""
    route = _route_a(conn, row["id"])
    service = route.get("service_scope") if route else None
    return ack_questions.to_ask(facts(conn, row), gaps(conn, row), row["service_mode"], service,
                                (route.get("incoterm") if route else None) or row["incoterm"])


# ---------------------------------------------------------- the subject --
#: N2: the reply and forward prefixes a draft's subject drops - parsing's
#: list without its calendar words (Cancelled, Canceled, Cancelar, Recall),
#: which stay part of the subject
_SUBJECT_PREFIX = re.compile(
    r"^[\s　]*(RE|RES|RV|AW|ANTW|FW|FWD|ENC|TR"
    r"|回复|回覆|答复|答覆|转发|轉發|轉寄|回信)"
    r"[\s　]*[:：][\s　]*", re.IGNORECASE)


def numbered_subject(subject: str, opp_no: str) -> str:
    """N2: 'RE: [OPP-4001] X' - every case number and every reply or forward
    prefix at the start taken out (again and again: they interleave, 'RE:
    [OPP-1542] RE: X'), then one 'RE: [OPP-nnnn] ' in front. Stable: the
    result, and 'RE: ' + the result, come back unchanged."""
    rest = subject or ""
    for _ in range(50):
        before = rest
        rest = parsing.strip_case_numbers(rest).strip()
        rest = _SUBJECT_PREFIX.sub("", rest, count=1)
        if rest == before:
            break
    rest = " ".join(rest.split())
    return "RE: [{}] {}".format(opp_no, rest).strip()


# ------------------------------------------- a colleague's forward (A2b) --
_REPLY = re.compile(r"^[\s\u3000]*(RE|RES|RV|AW)[\s\u3000]*[:\uff1a]", re.IGNORECASE)
_FORWARD = re.compile(r"^[\s\u3000]*(FW|FWD|RV|ENC|TR)[\s\u3000]*[:\uff1a]\s*", re.IGNORECASE)
def reply_subject(subject: str) -> str:
    """'RE: ' in front unless it already starts with a reply prefix."""
    subject = " ".join((subject or "").split())
    return subject if _REPLY.match(parsing.strip_case_numbers(subject)) else "RE: " + subject


def forward_subject(subject: str) -> str:
    """The forward's own subject with its FW / FWD / RV / ENC / TR taken off."""
    rest = " ".join(parsing.strip_case_numbers(subject or "").split())
    while _FORWARD.match(rest):
        rest = _FORWARD.sub("", rest, count=1)
    return rest


def forward_target(conn, mail) -> dict:
    """A2b: what the acknowledgment of a colleague's forward is written to:
    {'original', 'name', 'address', 'cc', 'subject'} - the original from
    intake.forward_original (the mail _forward_draft read), the colleague
    on Cc, the subject without the forward's prefix."""
    from . import intake
    quoted = parsing.quoted_mails(getattr(mail, "body_excerpt", "") or "")
    original, address = intake.forward_original(quoted, intake._internal_domains(conn, mail))
    subject = (original.subject if original is not None and original.subject
               else forward_subject(getattr(mail, "subject", "")))
    return {"original": original, "name": (original.name or address) if original is not None else "",
            "address": address, "cc": getattr(mail, "sender_address", "") or "", "subject": reply_subject(subject)}


# ------------------------------------------------- the Acknowledged event --
def acknowledged_on(conn, enquiry_id) -> str:
    row = conn.execute("SELECT MIN(at) FROM status_event WHERE enquiry_id = ? AND kind = 'ack' AND voided = 0",
                       (enquiry_id,)).fetchone()
    return row[0] or ""


def is_colleague_forward(mail) -> bool:
    """AL3: the registered mail is a JJC colleague's forward; such a case's
    acknowledgment is a new mail to the original sender (A2b). CX1: the
    mail decides - at registration the form's flag, afterwards the
    archive's copy of the registered mail, else Outlook's (no quick note
    is read or written any more)."""
    from . import intake
    return mail is not None and intake.forward_kind(mail) == "colleague"
