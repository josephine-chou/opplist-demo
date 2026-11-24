"""Who the app works for, in one place.

Every company fact the code needs - the short name in front of an office
party's key, the internal mail domain, the home office code, the app's name -
comes from here or from the matching setting, never from a literal in a
module. The defaults describe the fictional company of the demo data,
JoJo Freight.
"""

from __future__ import annotations

import re

APP_NAME = "OppList"
COMPANY_NAME = "JoJo Freight"
#: Default of setting 'company_short': the word in front of an office code
#: in an office party's key ('JoJo JQM').
COMPANY_SHORT = "JoJo"
#: Default of setting 'internal_domains' (schema.sql seeds the same value).
INTERNAL_DOMAIN = "jojofreight.example"
#: The office this app works for. Its staff sign their mail 'JJC-NAME', a
#: case handled here stores handled_by NULL.
HOME_OFFICE = "JJC"
#: The home office as a sentence names it (Read quote's prompt).
HOME_OFFICE_NAME = COMPANY_NAME + " Chicago"
#: Default of setting 'rate_request_cc': the team address a rate request
#: mail copies.
TEAM_CC = "#{0}-MFN <{0}-MFN@{1}>".format(HOME_OFFICE, INTERNAL_DOMAIN)
#: The party type of the company's own offices.
OFFICE_TYPE = "Office"
#: The role (party.roles) that goes with OFFICE_TYPE.
OFFICE_ROLE = "own_office"


def company_short(conn=None) -> str:
    """Setting 'company_short', else COMPANY_SHORT."""
    if conn is not None:
        try:
            row = conn.execute("SELECT value FROM setting WHERE key = 'company_short'").fetchone()
        except Exception:                     # no setting table yet
            row = None
        if row is not None and (row[0] or "").strip():
            return row[0].strip()
    return COMPANY_SHORT


def office_key(code: str, conn=None) -> str:
    """'JQM' -> 'JoJo JQM': the key an office party is filed under."""
    return "{} {}".format(company_short(conn), (code or "").strip().upper())


def office_key_pattern(conn=None) -> "re.Pattern":
    """Matches an office party's key and captures the code:
    'JoJo JQM' -> 'JQM' (case-insensitive)."""
    return re.compile(r"^\s*{}\s+([a-z]{{3}})\b".format(re.escape(company_short(conn))), re.I)


def office_short(key: str, conn=None) -> str:
    """'JoJo JQM' -> 'JQM': what follows the company's short name in
    an office party's key, as written; '' when the key does not start so."""
    key, front = (key or ""), company_short(conn) + " "
    return key[len(front):] if key.casefold().startswith(front.casefold()) else ""


def office_code(key: str, conn=None) -> str:
    """'JoJo JQM' -> 'JQM'; '' when the key is not an office's."""
    found = office_key_pattern(conn).match(key or "")
    return found.group(1).upper() if found else ""
