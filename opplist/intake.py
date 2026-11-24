"""Turning a selected mail into a pre-filled registration.

This is the only place that joins the three halves: what Outlook said, what
the subject parses to, and what the database already knows. The CLI selftest
and the web endpoint both build the same draft, so what `outlook-selftest`
prints is exactly what the form will show.

Nothing here writes. Every value carries where it came from, so the form can
say 'from subject' or 'from sender domain' next to it and the user can tell a
fact from a guess.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field

from . import branding, cargo, dates, db, matching, packing, parsing

FROM_MAIL = "from the mail"
FROM_SUBJECT = "from subject"
FROM_SENDER = "from sender domain"
FROM_RECIPIENT = "from a To/CC domain"
FROM_BODY = "from body"
#: Read out of the original mail quoted under our own reply.
FROM_QUOTED = "from the quoted original mail"
#: 'TEXAS- EEUU' and nothing else: one place, taken as where it goes.
FROM_SINGLE = "single place in subject, assumed destination"
#: The office whose mail is 'ours' when the requester is looked for among
#: the quoted mails: a reply by JJC quoting JQM is JQM's enquiry.
HOME_OFFICE = branding.HOME_OFFICE
#: The sender's domain is nobody's on file, but its own name is a party's.
FROM_DOMAIN_NAME = matching.MATCH_DOMAIN_NAME
#: One of our offices's own mail: its display name's prefix ('JJK-OPS').
FROM_OFFICE_PREFIX = "the sender's office prefix"
#: The company whose request the office forwarded to us.
FROM_BELOW_OFFICE = "external company below the office that asked us"
#: Our own mail lists charges with amounts: we quoted.
FROM_OUR_QUOTE = "our reply quotes charges"
#: We sent this mail out ourselves, so nobody in it is the one asking.
FROM_OUTBOUND = "we asked - requester still to be confirmed"
#: We sent it out, but as an answer to the mail quoted below it: the person we
#: are writing to is the one who asked.
FROM_REPLY = "reply to their inquiry"
#: AL3: read out of the oldest outside mail in the chain a JJC colleague
#: forwarded to us.
FROM_CHAIN = "from the forwarded chain, first outside mail"
#: AL3: a chain whose newest mail is older than this, against the forward's
#: own date, is an old thread passed on as a new request.
OLD_THREAD_DAYS = 30
#: AL3: a colleague's forward whose forwarded header block cannot be read.
FORWARD_CHECK = "Forwarded mail: check Received on and Requester"

#: The party a case is parked against until the requester is known. '(TBC)'
#: means the search is still on and tracking rule R5 will keep asking;
#: 'Unknown' means it was given up on and R5 leaves it alone.
PLACEHOLDER_SEARCHING = "(TBC)"
PLACEHOLDER_ABANDONED = "Unknown"


@dataclass
class Draft:
    """A registration waiting for a person to confirm it."""

    values: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    lanes: list = field(default_factory=list)      # [{origin, dest, known}]
    party_candidates: list = field(default_factory=list)
    special_tags: list = field(default_factory=list)
    #: DD1: {tag: the line it was read from} for a tag the mail's words gave
    tag_lines: dict = field(default_factory=dict)
    references: list = field(default_factory=list)
    unknown_locations: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    mail: object = None
    subject: object = None
    #: True when we sent the mail out to somebody outside: Victor asking a
    #: carrier for a rate. The case is registered now, the requester later.
    outbound: bool = False
    #: True when we sent it out as a reply to the party we are writing to:
    #: they asked, we answered, so they are the requester.
    reply_to_requester: bool = False
    #: The key of the party in the requester field, for anything that prints
    #: the draft. A bare row id says nothing to a reader.
    requester_key: str = ""
    #: [{domain, addresses, party_id, key, known, checked, note}] - the people
    #: we asked, offered for logging as vendor_request rows.
    vendors: list = field(default_factory=list)
    #: field -> the body line a 'from body' value was read from, so the form
    #: can quote it back instead of asking her to trust the parser.
    evidence: dict = field(default_factory=dict)
    #: side -> [(place, line), ...] when the body states several destinations
    #: (or origins) and none of them can be pre-filled.
    body_conflicts: dict = field(default_factory=dict)
    #: side -> {'text': what the mail wrote, 'options': [{key, name, type,
    #: country}]} when that spelling is an alias of more than one place:
    #: nothing is pre-filled, the form offers the choice.
    location_choices: dict = field(default_factory=dict)
    #: The mails quoted under this one, nearest first (parsing.QuotedMail).
    quoted: list = field(default_factory=list)
    #: A party the form should offer to create: {'key', 'type', 'country',
    #: 'domain', 'sources', 'similar', 'why'}.
    new_party: dict | None = None
    #: A domain to attach to the requester when the case is saved: the one
    #: that named the party by its own name, or the one a New party is
    #: created for.
    learn_domain: str = ""
    #: D1: whom learn_domain was read for - 'new' (the New party) or the ids
    #: of the parties named like the domain ('12' or '12,15'); the save
    #: remembers the domain only when the requester saved is that party
    learn_for: str = ""
    #: D1: one muted line naming the outside companies in the quoted chain
    #: that are neither requester nor vendor ('gxship.example is in the chain
    #: but wrote to the requester, not to us')
    chain_line: str = ""
    #: D1 (1b): a thread we started - {'domain', 'asked_on', 'key'}: its
    #: domain is a vendor for the whole thread, asked on our first mail's date
    vendor_thread: dict | None = None
    #: 'asking' (we want a rate), 'quoting' (we give one) or '' for a mail
    #: we sent out (mail_direction).
    direction: str = ""
    #: F1: the words that made it 'quoting' (mail_direction_why).
    direction_words: str = ""
    #: One line above the vendors panel: who on the mail is the requester
    #: rather than somebody we asked.
    vendor_note: str = ""
    #: The consignments the mail describes, one dict per cargo line in the
    #: enquiry_cargo shape plus 'evidence' (2026-09-24). Shown
    #: in the Cargo section, editable, saved with the case.
    cargo_lines: list = field(default_factory=list)
    #: v34 (2026-09-29): the totals the mail states, one dict per block in
    #: the enquiry_cargo_stated shape; a line's 'stated_ref' is its index here.
    cargo_stated: list = field(default_factory=list)
    #: D7a (v50): the HS code and goods value the mail names, for the
    #: register page's first line when the reader made no line
    line_extra: dict = field(default_factory=dict)
    #: AL3: one line about the forwarded mail at the top of the draft, and
    #: how it is shown: 'check' (amber - the header block could not be
    #: read) or 'muted' (another office's forward: who wrote it, and when).
    forward_line: str = ""
    forward_level: str = ""
    #: AL3: 'Forwarded by <name>, <date>' on a colleague's forward; the
    #: register form carries it as the forward flag (CX1: nothing keeps it).
    forward_note: str = ""
    #: batch 23 (item 4): the destination of route A after its first place
    #: when the mail wrote 'X or Y' (or 'X / Y', both places on file): one
    #: route with all of them. Each lane carries its own as 'dest_more'.
    dest_more: list = field(default_factory=list)
    #: batch 14: the mail's attachments as read (attachments.FileText, an
    #: attached mail's own files flat after it), the amber checks where an
    #: attachment says something else than the body ('the mail says 4
    #: pallets, Packing List.pdf says 5 pallets'), and per file the lines
    #: the readers used, for Show the mail text
    attachment_files: list = field(default_factory=list)
    attachment_checks: list = field(default_factory=list)
    attachment_used: dict = field(default_factory=dict)
    #: batch 23: a route can hold several destinations (the master layer
    #: is active: its places have ids to keep in enquiry_route_place)
    multi_places: bool = False
    #: RG2a (layer 1): field -> where its value came from when the source
    #: label does not say it ('table': a label above its value; 'official
    #: list': a place built from the official code lists)
    read_from: dict = field(default_factory=dict)
    #: RG2a (F): key -> the new master place (official_places.place_for) a
    #: place read from the mail becomes when she registers; nothing before
    official_new: dict = field(default_factory=dict)
    #: RG2a (layer 2): side -> the mail's words the place was read from
    place_words: dict = field(default_factory=dict)

    @property
    def lane_count(self) -> int:
        return len(self.lanes)

    def set(self, name, value, source, evidence=None):
        if value in (None, "", []):
            return
        self.values[name] = value
        self.sources[name] = source
        if evidence:
            self.evidence[name] = evidence


def _known_locations(conn) -> set:
    """The keys a subject or body can name. Once the master layer is active
    every alias counts too: 'SAI' and 'CHI' still name a place on file
    although the keys are now SAN ANTONIO and CHICAGO, IL."""
    if db.master_active(conn):
        # A retired place is one somebody took off the map: the intake must
        # not offer it again (2026-09-17). The location view keeps it, so
        # the export's Lookup_Location sheet is unchanged.
        keys = {r["key"] for r in conn.execute(
            "SELECT key FROM master_location WHERE status <> 'retired'")}
        keys |= {r["alias"] for r in conn.execute(
            "SELECT a.alias FROM master_location_alias a "
            "JOIN master_location l ON l.id = a.location_id WHERE l.status <> 'retired'")}
        return keys
    return {r["key"] for r in conn.execute("SELECT key FROM location")}


def location_countries(conn) -> dict:
    """key -> country; with the master layer, alias -> its place's country."""
    out = {r["key"]: r["country"] for r in conn.execute("SELECT key, country FROM location")}
    if db.master_active(conn):
        for r in conn.execute(
                "SELECT a.alias, l.country FROM master_location_alias a "
                "JOIN master_location l ON l.id = a.location_id WHERE l.status <> 'retired'"):
            out.setdefault(r["alias"], r["country"])
    return out


def place_index(conn) -> dict:
    """What a place written in words can be matched against: location keys
    and codes, official names, aliases (Lookup_Location Aliases), and the
    country names themselves.

    With the master layer active the order is the spec's (5.2): the key
    first, then the aliases, then the UN/LOCODE and IATA codes, then the
    names. An alias that points at more than one place is remembered in
    `ambiguous`, so the draft can say the pick needs a look.
    """
    countries = {r["name"].casefold(): r["name"]
                 for r in conn.execute("SELECT name FROM country")}
    # RG2a (C): 'HK' in 'HK-SCL' is the country's code
    try:
        iso2 = {r["iso2"].upper(): r["name"] for r in conn.execute(
            "SELECT name, iso2 FROM country WHERE iso2 IS NOT NULL AND iso2 <> ''")}
    except sqlite3.Error:
        iso2 = {}
    by_name, country_of, ambiguous, places = {}, {}, {}, {}
    if db.master_active(conn):
        rows = conn.execute("SELECT l.key, l.name, l.type, l.unlocode, l.iata, l.country, "
                            "p.key AS parent_key FROM master_location l "
                            "LEFT JOIN master_location p ON p.id = l.parent_id "
                            "WHERE l.status <> 'retired' ORDER BY l.id").fetchall()
        for r in rows:
            country_of[r["key"]] = r["country"]
            places[r["key"]] = {"key": r["key"], "name": r["name"], "type": r["type"],
                                "country": r["country"], "parent_key": r["parent_key"]}
            by_name.setdefault(r["key"].casefold(), r["key"])
        key_names = set(by_name)
        for r in conn.execute(
                "SELECT a.alias, l.key FROM master_location_alias a "
                "JOIN master_location l ON l.id = a.location_id "
                "WHERE l.status <> 'retired' ORDER BY a.alias, l.id"):
            name = r["alias"].casefold()
            if name in key_names:
                continue                                  # the key wins over an alias
            if name in by_name and by_name[name] != r["key"]:
                ambiguous.setdefault(name, [by_name[name]])
                if r["key"] not in ambiguous[name]:
                    ambiguous[name].append(r["key"])
                continue
            by_name[name] = r["key"]
        for r in rows:
            for code in (r["unlocode"], r["iata"]):
                if code:
                    by_name.setdefault(code.strip().casefold(), r["key"])
        for r in rows:
            if r["name"]:
                by_name.setdefault(r["name"].strip().casefold(), r["key"])
        # RG2a (D): a city's own gateways, for the place the mode wants
        children = {}
        for place in places.values():
            if place["parent_key"]:
                children.setdefault(place["parent_key"], []).append(place)
        return {"by_name": by_name, "country_of": country_of, "countries": countries,
                "ambiguous": ambiguous, "places": places, "children": children, "iso2": iso2}

    rows = conn.execute("SELECT key, official_name, code_official, aliases, country "
                        "FROM location").fetchall()
    # An alias is written on purpose ('Miami' -> MIA, the port), so it wins
    # over a location that merely has the word as its key (the city row
    # MIAMI); keys and codes come next, official names last.
    for r in rows:
        country_of[r["key"]] = r["country"]
        for alias in (r["aliases"] or "").split(","):
            if alias.strip():
                by_name.setdefault(alias.strip().casefold(), r["key"])
    for r in rows:
        by_name.setdefault(r["key"].casefold(), r["key"])
        if r["code_official"]:
            by_name.setdefault(r["code_official"].strip().casefold(), r["key"])
    for r in rows:
        if r["official_name"]:
            by_name.setdefault(r["official_name"].strip().casefold(), r["key"])
    return {"by_name": by_name, "country_of": country_of, "countries": countries,
            "ambiguous": ambiguous, "places": places, "children": {}, "iso2": iso2}


def country_name(index, text) -> str | None:
    """The country table's name for 'EEUU', 'Brasil', 'United States' or
    'Spain'; None when it is not a country (or not one on file)."""
    text = (text or "").strip(" .,")
    if not text:
        return None
    found = index["countries"].get(text.casefold())
    if found:
        return found
    alias = parsing.country_alias(text)
    return index["countries"].get(alias.casefold()) if alias else None


def resolve_place(index, text) -> tuple:
    """(location key or None, country or None) for a place in words.

    'Miami port' -> ('MIA', 'USA'); 'Barcelona, Spain' -> ('BCN', 'Spain');
    'Spain' -> (None, 'Spain'); anything unknown -> (None, None). The part
    after a comma is read as the country when it is one.
    """
    text = parsing.lane_place(text or "")
    if not text:
        return None, None
    parts = [parsing.lane_place(p) for p in text.split(",") if p.strip()]
    primary = parts[0] if parts else ""
    hint = None
    for part in parts[1:]:
        hint = country_name(index, part) or hint
    for candidate in (text, primary):
        key = index["by_name"].get(candidate.casefold())
        if key:
            return key, index["country_of"].get(key) or hint
    country = country_name(index, primary)
    if country:
        return None, country
    return None, hint


def _fold_alternatives(draft, groups) -> None:
    """Batch 23 (item 4): lanes from one origin to destinations the mail
    wrote as alternatives ('San Antonio or Valparaiso') are one route with
    all of them - the first lane keeps the first place, 'dest_more' the
    rest (four at most). Other lanes stay routes of their own. Only where
    a route can hold several destinations (the master layer)."""
    if not draft.multi_places or not groups:
        return
    groups = [{str(p).upper() for p in group} for group in groups]
    kept = []
    for lane in draft.lanes:
        dest = (lane.get("dest") or "").upper()
        group = next((g for g in groups if dest in g), None)
        into = next((k for k in kept if group is not None and k.get("origin") == lane.get("origin")
                     and (k.get("dest") or "").upper() in group), None) if group is not None else None
        if into is None:
            kept.append(lane)
            continue
        more = into.setdefault("dest_more", [])
        if dest != (into.get("dest") or "").upper() and dest not in more and len(more) < 3:
            more.append(dest)
            into["primary"] = into.get("primary") or lane.get("primary")
    draft.lanes[:] = kept


def _resolve_lanes(draft, index) -> None:
    """Lanes read as words become location keys with their countries.
    'Lima' and 'Jorge Chavez International Airport' both being LIM, the
    two lanes they made become one."""
    _resolve_each(draft, index)
    seen, kept = set(), []
    for lane in draft.lanes:
        key = (lane["origin"], lane["dest"], lane.get("origin_country"), lane.get("dest_country"))
        if key in seen:
            continue
        seen.add(key)
        kept.append(lane)
    draft.lanes[:] = kept


#: Modes that mean a box or a truck: the city, not its airport.
_SURFACE_MODES = ("FCL", "LCL", "RORO", "Break Bulk", "Project", "Truck", "Cross-border Truck", "Multi-mode")

#: A two-letter token: a state code or a country code, and this mailbox
#: has CO, PA, PE, AR, CL that could be either. Never a place, never guessed.
_TWO_LETTERS = re.compile(r"^[A-Z]{2}$")


def _city_or_gateway(draft, options):
    """When one spelling names a city and its own gateways (MIAMI: the city
    MIAMI, FL and the airport MIA), the mode decides instead of a person:
    air takes the airport, a box or a truck takes the city, an unknown mode
    takes the city. None when the options are really different places (or
    the city has several airports and the mail is air)."""
    cities = [o for o in options if o.get("type") == "city"]
    if len(cities) != 1:
        return None
    city = cities[0]
    gateways = [o for o in options if o is not city]
    if any(o.get("parent_key") != city["key"] for o in gateways):
        return None
    mode = draft.values.get("service_mode")
    if mode == "Air":
        airports = [o for o in gateways if o.get("type") == "airport"]
        if len(airports) == 1:
            return airports[0]
        if airports:
            return None
        return city
    return city


def _offer_choice(draft, index, lane, side, place) -> bool:
    """A spelling that names more than one place: nothing is pre-filled for
    that side, the form gets the options (the same list the dropdown has)
    and the country when every option agrees on it. A city and its own
    gateways are not a choice: the mode picks (_city_or_gateway)."""
    keys = index.get("ambiguous", {}).get((place or "").casefold())
    if not keys:
        return False
    options = [index["places"].get(k, {"key": k, "name": k, "type": "", "country": None})
               for k in keys]
    picked = _city_or_gateway(draft, options)
    if picked is not None:
        lane[side] = picked["key"]
        lane[side + "_known"] = True
        lane[side + "_country"] = picked.get("country") or lane.get(side + "_country")
        mode = draft.values.get("service_mode")
        note = "'{}' names {}: {} picked ({}).".format(
            place, " and ".join(o["key"] for o in options), picked["key"],
            "air, so the airport" if picked.get("type") == "airport"
            else ("{}, so the city".format(mode) if mode else "mode unknown, so the city"))
        if note not in draft.notes:
            draft.notes.append(note)
        return True
    countries = {o.get("country") for o in options}
    lane[side] = ""
    lane[side + "_known"] = False
    lane[side + "_choices"] = keys
    if len(countries) == 1 and not lane.get(side + "_country"):
        lane[side + "_country"] = countries.pop()
    draft.location_choices.setdefault(side, {"text": place, "options": options})
    note = "'{}' names {} places ({}); pick one below.".format(
        place, len(keys), ", ".join(keys))
    if note not in draft.notes:
        draft.notes.append(note)
    return True


def _resolve_more(index, places) -> list:
    """The other places of a destination (batch 23) as location keys where
    they name one; the words as written otherwise (saving adds those, To
    confirm, like any lane endpoint from a mail)."""
    out = []
    for place in places or []:
        key, _country = resolve_place(index, place)
        key = key or (place or "").strip().upper()
        if key and key not in out:
            out.append(key)
    return out


def _resolve_each(draft, index) -> None:
    for lane in draft.lanes:
        for side in ("origin", "dest"):
            place = lane[side]
            if place and not lane.get(side + "_words"):
                lane[side + "_words"] = place               # RG2a: the mail's words, for layer 2
            if _offer_choice(draft, index, lane, side, place):
                continue
            if _TWO_LETTERS.match((place or "").strip()) and \
                    place.strip().casefold() not in index["by_name"]:
                lane[side] = ""
                lane[side + "_known"] = False
                note = ("'{}' is a two-letter code (a state or a country); not used as a "
                        "place - set the country by hand.".format(place.strip()))
                if note not in draft.notes:
                    draft.notes.append(note)
                continue
            key, country = resolve_place(index, place)
            if not key and place in index["country_of"]:
                lane[side + "_country"] = lane[side + "_country"] or index["country_of"][place]
                continue
            if key:
                lane[side] = key
                lane[side + "_known"] = True
                lane[side + "_country"] = country
            elif country and not lane[side + "_country"]:
                lane[side + "_country"] = country
                if place.casefold() == country.casefold():
                    lane[side] = ""             # a country alone is no location
                    lane[side + "_known"] = True
            if not key and place and country_name(index, place):
                # RG2a: 'BRAZIL', 'UNITED STATES' are the country, never a place
                # (even when a legacy row once gave the word a country of its own)
                lane[side] = ""
                lane[side + "_known"] = True
                lane[side + "_country"] = lane[side + "_country"] or country_name(index, place)
        if lane.get("dest_more"):
            # batch 23: the other places of the destination, as keys; none
            # without a first place, none twice
            more = _resolve_more(index, lane["dest_more"]) if lane["dest"] else []
            lane["dest_more"] = [k for k in more if k != lane["dest"]]


#: The fields that carry the enquiry itself. Where they were read from
#: decides which mail's date is the received date.
CONTENT_FIELDS = ("origin_loc", "dest_loc", "origin_country", "dest_country",
                  "origin_alt", "dest_alt", "qty_raw", "air_weight_kg",
                  "container_type", "container_qty", "commodity")


def _received_from_content(draft, mail, subject, chain, original) -> None:
    """received_on is the date of the mail the enquiry was read from.

    The selected mail's own words (its body, or its subject when it is not
    a reply) make it the source, even when older mails are quoted under it:
    the second shipment asked about in the same thread is a new enquiry on
    the day it was asked. Otherwise the quoted mail that gave the lane (or,
    failing that, the quantity or the commodity) gives the date. A mail
    with no content anywhere keeps its own date - except our own reply
    that quotes the original enquiry, which keeps the original's date.
    """
    own = {FROM_BODY}
    if not parsing.has_reply_prefix(mail.subject):
        own.add(FROM_SUBJECT)
    # RG2a (C): a side only the subject's codes gave does not decide the date
    sources = [draft.sources.get(name) for name in CONTENT_FIELDS if name in draft.values
               and not (draft.read_from.get(name) == "subject" and draft.sources.get(name) == FROM_SUBJECT)]
    if any(source in own for source in sources):
        return                                        # the selected mail itself
    by_label = {_quoted_label(q): q for q in chain}
    for source in sources:
        quoted = by_label.get(source)
        if quoted is not None and quoted.sent_on:
            draft.set("received_on", quoted.sent_on, _quoted_label(quoted))
            return
    if original is not None and original.sent_on and mail.sender_internal:
        draft.set("received_on", original.sent_on, _quoted_label(original))


def non_stackable_tag(conn):
    """Settings' special_handling entry for Non-stackable (as she wrote it),
    None while it is not on the list."""
    return next((t for t in (db.get_setting_list(conn, "special_handling") or [])
                 if re.sub(r"[^a-z]", "", str(t).lower()) == "nonstackable"), None)


def _non_stackable(conn, draft, mail, own_lines, chain) -> None:
    """DD1 item 4: tick Non-stackable (while it is on Settings'
    special_handling list) when the subject, the mail's own lines or a
    forward's quoted request say the cargo cannot be stacked; the line it
    was read from goes with it, and she can untick it. FX5: never from a
    question (parsing.non_stackable_line)."""
    tag = non_stackable_tag(conn)
    if tag is None or tag in draft.special_tags:
        return
    texts = [([getattr(mail, "subject", "") or ""], FROM_SUBJECT), (own_lines, FROM_BODY)]
    if forward_kind(mail):
        texts += [(parsing.body_lines(q.text), _quoted_label(q)) for q in chain]
    for lines, source in texts:
        line = parsing.non_stackable_line(lines)
        if line:
            draft.special_tags = list(draft.special_tags) + [tag]
            draft.tag_lines[tag] = "{}: \u201c{}\u201d".format(source, line[:160])
            return


def _quoted_label(quoted) -> str:
    """'from the quoted original mail (Tomas Ibanez 2026-08-03 11:20)'."""
    who = quoted.name or quoted.address or "?"
    return "{} ({}{})".format(FROM_QUOTED, who,
                              " " + quoted.sent_at if quoted.sent_at else "")


def _original_mail(quoted) -> object | None:
    """The mail that started it: the earliest quoted one not from the home
    office. None when nothing is quoted or every quote is ours."""
    for mail in reversed(quoted):
        if mail.office != HOME_OFFICE:
            return mail
    return None


def _written_to_us(quoted) -> bool:
    """The To or Cc line names our office ('JJC-VICTOR LANG', '#JJC-MFN')."""
    return bool(re.search(r"(?:^|[\s;,<'\"(])#?{}-".format(HOME_OFFICE),
                          "{} {}".format(quoted.to, quoted.cc)))


def _asking_mail(quoted) -> tuple:
    """(the mail written directly to us, the outside mails quoted below it).

    Bottom up, the first mail whose To or Cc names our office and that is
    not ours: the agent, or the office, that asked. The outside
    companies further down are whose shipment it is - the end customer,
    not the requester. (None, []) when no mail is to us.
    """
    below = []
    for mail in reversed(quoted):
        if mail.office == HOME_OFFICE:
            continue
        if _written_to_us(mail):
            return mail, below
        if not mail.office:
            below.append(mail)
    return None, []


def _written_to_company(quoted, internal_domains) -> bool:
    """D1: the To or Cc line names one of our offices (JJC or another, by its
    prefix) or an address on our own domains - a mail written to us, not
    one outside companies wrote to each other."""
    if _written_to_us(quoted):
        return True
    for line in (quoted.to, quoted.cc):
        if any(parsing._OFFICE_PREFIX.match(name) for name in _to_names(line)):
            return True
        if any(matching.domain_of(a) in internal_domains for a in parsing._ADDRESS.findall(line or "")):
            return True
    return False


def _line_domains(line, internal_domains) -> list:
    """The outside company domains on a written To or Cc line, in order."""
    out = []
    for address in parsing._ADDRESS.findall(line or ""):
        domain = matching.domain_of(address)
        if domain and domain not in internal_domains and not matching.is_free_mail(domain) and domain not in out:
            out.append(domain)
    return out


def vendor_thread_of(quoted, internal_domains) -> tuple:
    """D1 (1b): (domain, date) when the oldest mail in the quoted chain is
    ours, written to an outside company - a thread we started, so that
    company is a vendor we asked on that date; ('', '') otherwise."""
    if not quoted:
        return "", ""
    first = quoted[-1]
    ours = first.office == HOME_OFFICE or (
        not first.office and matching.domain_of(first.address) in internal_domains)
    if not ours:
        return "", ""
    to = _line_domains(first.to, internal_domains)
    if not to:
        return "", ""
    return to[0], first.sent_on


def _asker_is_ours(conn, draft) -> bool:
    """4.3.4 (D1): the party that asked us is on file as an Agent or a
    own office - only then are the companies below it whose shipment
    it is."""
    party_id = draft.values.get("requester_party_id")
    if not party_id:
        return False
    row = conn.execute("SELECT type FROM party WHERE id = ?", (party_id,)).fetchone()
    return row is not None and row["type"] in ("Agent", "Office")


def _below_domains(mails, skip=()) -> list:
    out = []
    for quoted in mails:
        domain = matching.domain_of(quoted.address)
        if domain and not matching.is_free_mail(domain) and domain not in skip and domain not in out:
            out.append(domain)
    return out


def _set_chain_line(draft, domains) -> None:
    domains = list(dict.fromkeys(d for d in domains if d))
    if domains:
        draft.chain_line = "{} {} in the chain but wrote to the requester, not to us".format(
            ", ".join(domains), "is" if len(domains) == 1 else "are")


def _below_the_asker(conn, draft, asking, below, texts, extra=()) -> None:
    """The companies quoted below the mail that asked us (4.3.4, D1): the
    End customer when the asker is an agent or one of our offices of ours;
    otherwise named in the muted line, End customer left empty. `extra`:
    other chain domains to name there."""
    named = []
    if below and _asker_is_ours(conn, draft):
        _end_customer_below(conn, draft, below, texts)
    elif below:
        named = _below_domains(below, skip={matching.domain_of(asking.address)} if asking is not None else ())
    _set_chain_line(draft, list(extra) + named)


def _name_for_domain(domain, texts) -> str:
    """'lumenbrands.example' with 'request for Lumen Brands' in the subject
    -> 'Lumen Brands': the words that squash to the domain's own name,
    else the domain's name capitalised."""
    stem = matching.squash(matching.domain_stem(domain))
    if len(stem) >= 4:
        for text in texts:
            words = re.findall(r"[A-Za-z][A-Za-z&'.\-]*", text or "")
            for size in (1, 2, 3, 4):
                for start in range(0, max(0, len(words) - size + 1)):
                    run = words[start:start + size]
                    if matching.squash(" ".join(run)) == stem:
                        return " ".join(run)
    return suggested_party_name(domain)


def _end_customer_below(conn, draft, mails, texts) -> None:
    """The first outside company quoted below the office that asked us: a
    direct customer or a domain nobody has -> End customer suggestion."""
    for quoted in mails:
        domain = matching.domain_of(quoted.address)
        if not domain or matching.is_free_mail(domain):
            continue
        found = matching.by_domain(conn, domain)
        if found:
            row = conn.execute("SELECT type FROM party WHERE id = ?",
                               (found[0].party_id,)).fetchone()
            if row and row["type"] != "Direct Customer":
                return                       # an agent of ours: not a customer
            name = found[0].key
        else:
            name = _name_for_domain(domain, texts + [quoted.subject, quoted.text])
        if "end_customer" not in draft.values:
            draft.set("end_customer", name, "{} ({})".format(FROM_BELOW_OFFICE, domain),
                      "{} {}".format(quoted.name or quoted.address, quoted.sent_at).strip())
        return


def _requester_from_quoted(conn, draft, original) -> None:
    """Our reply quoting somebody else's mail: they are the ones asking.

    One of our offices signs its mail 'JJV-MEI SUN'; the prefix names the
    party 'JoJo JJV'. An outside sender is matched by domain as ever.
    """
    who = "{}{}".format(original.name or original.address or "?",
                        " " + original.sent_at if original.sent_at else "")
    if original.office:
        _requester_from_office(conn, draft, original.office, original.name,
                               "{} ({})".format(FROM_QUOTED, who), "the quoted mail")
        return
    domain = matching.domain_of(original.address)
    if domain:
        _requester_from_domain(conn, draft, domain, "{} ({})".format(FROM_QUOTED, who))


def _requester_from_office(conn, draft, code, name, source, what) -> None:
    """The party 'JoJo <CODE>' as the requester, or '+ New' filled in
    with it (type Office, no domain: ours names nobody, 13g)."""
    key = branding.office_key(code, conn)
    row = conn.execute(
        "SELECT id, key FROM party WHERE type = 'Office' "
        "AND lower(trim(key)) = ? AND merged_into IS NULL", (key.lower(),)).fetchone()
    if row is not None:
        draft.set("requester_party_id", row["id"], source)
        draft.requester_key = row["key"]
        return
    draft.new_party = {"key": key, "type": "Office",
                       "why": "{} signs {}; no party '{}' on file".format(name, what, key)}
    draft.notes.append(
        "{} is from {} - the office {} is not on file yet; "
        "'+ New' is filled in with '{}'.".format(what[0].upper() + what[1:], name, code, key))


def _when(stamp) -> str:
    """'2026-09-29 10:14' -> 'Sep 29, 10:14 AM'; a date alone -> 'Sep 29'."""
    import datetime as dt
    text = str(stamp or "").strip()
    try:
        when = dt.datetime.strptime(text[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        try:
            when = dt.datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return text
        return "{} {}".format(when.strftime("%b"), when.day)
    return "{} {}, {}:{:02d} {}".format(when.strftime("%b"), when.day, when.hour % 12 or 12,
                                      when.minute, "AM" if when.hour < 12 else "PM")


def forward_kind(mail) -> str:
    """AL3: 'colleague' for a forward a JJC colleague sent us, 'office' for
    one another of our offices sent (13g's prefix rule), '' otherwise. A
    forward: FW / FWD / RV / ENC / TR in front of the subject, or a
    forwarded-message line in the body (parsing.is_forward)."""
    if not getattr(mail, "sender_internal", False):
        return ""
    if not parsing.is_forward(mail.subject, getattr(mail, "body_excerpt", "")):
        return ""
    return "office" if _sender_office(mail) else "colleague"


def _to_names(line) -> list:
    """The display names on a written To line: 'JJC-ANA <a@x>; JJV-BO <b@x>'
    -> ['JJC-ANA', 'JJV-BO']. Outlook separates with ';', Gmail with ','."""
    out = []
    for part in re.split(r";|(?<=>)\s*,", line or ""):
        name = re.sub(r"<[^>]*>|\[[^\]]*\]|[\w.%+\-]+@[\w.\-]+", "", part)
        name = " ".join(name.split()).strip(" '\"")
        if name:
            out.append(name)
    return out


def _party_of_contact(conn, address="", name="") -> tuple:
    """(party id, key) of the one party with a contact at this address, or
    of that name (name_key); (None, '') when none or several."""
    if address:
        rows = conn.execute(
            "SELECT DISTINCT p.id, p.key FROM contact c JOIN party p ON p.id = c.party_id "
            "WHERE lower(trim(c.email)) = ? AND p.merged_into IS NULL", (address.strip().lower(),)).fetchall()
    else:
        wanted = name_key(name)
        if not wanted:
            return None, ""
        rows = [r for r in conn.execute(
            "SELECT DISTINCT p.id, p.key, c.name FROM contact c JOIN party p ON p.id = c.party_id "
            "WHERE c.name IS NOT NULL AND p.merged_into IS NULL").fetchall()
            if name_key(r["name"]) == wanted]
        rows = list({r["id"]: r for r in rows}.values())
    if len(rows) == 1:
        return rows[0]["id"], rows[0]["key"]
    return None, ""


def name_key(name) -> str:
    """A person's name for comparing: accents, quote marks, case and extra
    spaces ignored ('"Constanza  Peña"' ~ 'constanza pena')."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[\"'`\u2018\u2019\u201c\u201d]", "", text)
    return " ".join(text.split()).casefold()


def _named_addresses(line) -> list:
    """[(name, address)] on a written To / Cc line, for the entries that
    give both ('Ana Ruiz <ana@x>; "Bo" <bo@x <mailto:bo@x>>')."""
    out = []
    for part in re.split(r";|(?<=>)\s*,", line or ""):
        found = parsing._ADDRESS.findall(part)
        if not found:
            continue
        name = re.sub(r"<[^>]*>?|\[[^\]]*\]?|[\w.%+\-]+@[\w.\-]+", "", part)
        name = " ".join(name.split()).strip(" '\"<>")
        if name:
            out.append((name, found[0].lower()))
    return out


def _address_in_chain(quoted, mail) -> str:
    """The address a sender named without one has elsewhere in the same
    chain - another quoted mail's From, To or Cc line, the name compared by
    name_key. '' when the name has none there, or two different ones."""
    wanted = name_key(mail.name)
    if not wanted:
        return ""
    found = set()
    for other in quoted:
        if other is mail:
            continue
        pairs = _named_addresses(other.to) + _named_addresses(other.cc)
        if other.name and other.address:
            pairs.append((other.name, other.address.lower()))
        found.update(address for name, address in pairs if name_key(name) == wanted)
    return found.pop() if len(found) == 1 else ""


def _outside_chain(quoted, internal_domains) -> list:
    """The dated mails of a forwarded chain written by somebody outside JJC:
    an agent, a customer, another of our offices. JJC's own are skipped."""
    out = []
    for mail in quoted:
        if not mail.sent_at or not (mail.address or mail.name):
            continue
        domain = matching.domain_of(mail.address)
        if mail.office == HOME_OFFICE or (not mail.office and domain and domain in internal_domains):
            continue
        out.append(mail)
    return out


def _forward_parts(quoted, internal_domains):
    """(outside, to_us, first, address) of a colleague's forward: the outside
    mails of the chain, those written to JoJo, the inquiry and its
    address (given, borrowed from the chain, or ''); first is None when no
    outside mail can be read."""
    outside = _outside_chain(quoted, internal_domains)
    if not outside:
        return [], [], None, ""
    # D1: the inquiry is the oldest outside mail written to us (JJC or
    # another office); a forwarder's mail to the customer below it is not
    to_us = [q for q in outside if _written_to_company(q, internal_domains)]
    first = min(to_us or outside, key=lambda q: q.sent_at)
    address = first.address
    if not address and not first.office:
        # named without an address: the one address that name has elsewhere
        # in the chain, unless it is one of ours
        borrowed = _address_in_chain(quoted, first)
        if borrowed and matching.domain_of(borrowed) not in internal_domains:
            address = borrowed
    return outside, to_us, first, address


def forward_original(quoted, internal_domains) -> tuple:
    """AL3 / A2b: which mail of a colleague's forward is the inquiry -
    (parsing.QuotedMail, address) - the one _forward_draft takes Received
    on and Requester from; (None, '') when the chain has none. The
    acknowledgment of a forward writes to it."""
    _outside, _to_us, first, address = _forward_parts(quoted, internal_domains)
    return first, address


def _forward_draft(conn, draft, mail, internal_domains) -> bool:
    """AL3: a JJC colleague's forward. The inquiry is the oldest mail in the
    forwarded chain written by somebody outside JJC; Received on, Requester
    and Handled by (its To line, batch 10's rule) all come from that mail,
    each saying so. When even the newest mail in the chain is more than
    OLD_THREAD_DAYS older than the forward, an old thread is being passed
    on as a new request: Received on stays the forward's, the Requester
    still comes from the chain, and the amber line gives the thread's
    dates. False when no outside mail can be read - nothing was changed."""
    outside, to_us, first, address = _forward_parts(draft.quoted, internal_domains)
    if first is None:
        return False
    where = "{}: {}, {}".format(FROM_CHAIN, _when(first.sent_at), first.address or first.name)
    if address and not first.address:
        where += ", address from elsewhere in the chain"
    dated = sorted(q.sent_at for q in draft.quoted if q.sent_at)
    forwarded_on = dates.as_date(mail.received_on)
    newest = dates.as_date(dated[-1])
    old = bool(forwarded_on and newest and (forwarded_on - newest).days > OLD_THREAD_DAYS)
    if old:
        draft.forward_line = "{} - an old thread passed on ({} to {}): check Received on and Requester".format(
            "Forwarded mail", dates.month_day(dated[0], year=True), dates.month_day(dated[-1], year=True))
        draft.forward_level = "check"
        draft.set("received_on", mail.received_on, "the forward's own date: the chain is an old thread")
    else:
        draft.set("received_on", first.sent_on, where)
    if first.office:
        _requester_from_office(conn, draft, first.office, first.name, where, "the forwarded mail")
    else:
        domain = matching.domain_of(address)
        party_id, key = _party_of_contact(conn, address=address) if address else (None, "")
        if party_id is None and address and domain:
            _requester_from_domain(conn, draft, domain, where)
        elif party_id is None and not address:
            party_id, key = _party_of_contact(conn, name=first.name)
            if party_id is None:
                draft.new_party = {"key": "", "type": "Agent", "country": "Unknown", "domain": "",
                                   "sources": {}, "similar": [],
                                   "why": "the forwarded mail names {} only, and no contact has that name".format(first.name)}
                draft.notes.append("The forwarded mail is from {} (no address), and no contact on file has "
                                   "that name; pick the requester or fill in 'New party'.".format(first.name))
        if party_id is not None:
            draft.set("requester_party_id", party_id, where)
            draft.requester_key = key
    if to_us:
        # D1: the outside mails older than the one written to us, not to us
        below = [q for q in outside if q.sent_at < first.sent_at and q not in to_us and not q.office]
        _below_the_asker(conn, draft, first, below, [mail.subject, first.subject])
    if not old:
        # an old thread's routing is not taken for the new request
        handler = office_from_to_names(_to_names(first.to))
        if handler:
            draft.set("handled_by", handler, where)
    return True


def _sender_office(mail) -> str:
    """The office code of a mail another of our offices wrote ('JJK-OPS'
    -> 'KUL'); '' for our own office's mail and for anybody outside."""
    if not getattr(mail, "sender_internal", False):
        return ""
    found = parsing._OFFICE_PREFIX.match(getattr(mail, "sender_name", "") or "")
    code = found.group(1) if found else ""
    return code if code and code != HOME_OFFICE else ""


def _ours_in_chain(quoted, internal_domains) -> bool:
    """A quoted mail JJC wrote (its prefix, or our domain with no office's)."""
    return quoted.office == HOME_OFFICE or (
        not quoted.office and matching.domain_of(quoted.address) in internal_domains)


def _passed_to_us(quoted) -> bool:
    """The To line itself names our office: a mail passed on to us, not one
    that only copied us."""
    return bool(re.search(r"(?:^|[\s;,<'\"(])#?{}-".format(HOME_OFFICE), quoted.to or ""))


def _quoted_inquiry(quoted, internal_domains) -> object | None:
    """RG1 (A, E): the inquiry a JJC mail quotes - the mail written to us
    that is not ours (_asking_mail), else AL3's earliest mail in the chain
    that is not JJC's when it was written to JoJo. When that is an
    office's mail To us (passing it on), an office further down that wrote
    to JoJo asked first and is the one. None when nothing quoted was
    written to us, or the chain starts with a mail of ours (a thread we
    started: the people in it answer us)."""
    if not quoted or _ours_in_chain(quoted[-1], internal_domains):
        return None
    asking, _below = _asking_mail(quoted)
    if asking is None:
        first = _original_mail(quoted)
        asking = first if first is not None and _written_to_company(first, internal_domains) else None
    if asking is not None and asking.office and _passed_to_us(asking):
        for mail in reversed(quoted):
            if mail is asking:
                break
            if mail.office and mail.office != HOME_OFFICE and _written_to_company(mail, internal_domains):
                return mail
    if asking is not None and not asking.office and not _asks_for_a_shipment(asking):
        # a sales visit, an introduction or a hello written to us is nobody's inquiry
        return None
    return asking


def _asks_for_a_shipment(quoted) -> bool:
    """RG1: an outside mail written to us is an inquiry when it asks
    (mail_direction), its subject says rate request or names a mode or
    boxes, or its text gives a quantity."""
    if mail_direction(quoted.subject, quoted.text) == "asking" or _RATE_REQUEST_SUBJECT.search(quoted.subject or ""):
        return True
    subject = parsing.parse_subject(quoted.subject or "")
    return bool(subject.service_mode or subject.containers or parsing.parse_body_qty(quoted.text or "").qty_raw)


def _requester_from_inquiry(conn, draft, inquiry) -> None:
    """RG1 (A): a mail of ours asking vendors, quoting the inquiry written to
    us - its sender is the requester instead of '(TBC)'; the people we wrote
    to stay vendors asked."""
    parked = {k: draft.values.pop(k) for k in ("requester_party_id",) if k in draft.values}
    source, key = draft.sources.pop("requester_party_id", None), draft.requester_key
    draft.requester_key = ""
    _requester_from_quoted(conn, draft, inquiry)
    if "requester_party_id" in draft.values or draft.new_party:
        draft.notes.append("The inquiry quoted below was written to us by {}: they are the requester; "
                           "the others we wrote to are vendors asked.".format(
                               draft.requester_key or inquiry.name or inquiry.address))
        return
    draft.values.update(parked)
    if source:
        draft.sources["requester_party_id"] = source
    draft.requester_key = key


#: RG1 (D): our first mail is our rate request when its subject says so or
#: its words ask them for their rate; anything else we started a thread
#: with (a promotion, an outreach mail) offered our services
_RATE_REQUEST_SUBJECT = re.compile(
    r"\b(rate\s+request|rates?\s+req|rfq|request\s+for\s+(a\s+)?(quot\w*|rates?)|quot(e|ation)\s+request|"
    r"cotizaci[oó]n|cota[cç][aã]o|solicitud\s+de\s+tarifa)\b", re.I)
_ASKS_FOR_THEIR_RATE = re.compile(
    r"\b(please|pls|kindly)\b[^.\n]{0,20}\b(quote|quotation)\b"
    r"|\byour\s+(best\s+|lowest\s+|updated\s+|most\s+competitive\s+)?(rates?|quot\w*|pricing|tariffs?)\b"
    r"|\b(provide|send|share|advise)\s+(us\s+)?(with\s+)?(the\s+)?(best\s+)?rates?\b", re.I)


def our_rate_request(quoted) -> bool:
    """RG1 (D): a mail of ours that asks them for a rate."""
    return bool(_RATE_REQUEST_SUBJECT.search(quoted.subject or "") or
                _ASKS_FOR_THEIR_RATE.search(quoted.text or ""))


def _answers_our_offer(mail, quoted, domain) -> bool:
    """RG1 (D): the thread starts with a mail of ours that offered our
    services rather than asking for a rate, and the company we wrote to
    answered asking us for one - their inquiry, not a vendor's reply."""
    if not quoted or our_rate_request(quoted[-1]):
        return False
    if matching.domain_of(mail.sender_address) == domain and mail_direction(
            mail.subject, "\n".join(parsing.body_lines(getattr(mail, "body_excerpt", "")))) == "asking":
        return True
    return any(matching.domain_of(q.address) == domain and quoted_asks_us(q) for q in quoted[:-1])


#: RG1 (C): 'NEW BOOKING // C/<customer> // S/<shipper> // ...'
_BOOKING_CUSTOMER = re.compile(r"(?:^|//)\s*C/\s*(?P<name>[^/]+?)\s*(?=//|$)", re.I)
FROM_BOOKING = "the C/ name in the booking subject"


def _whole_name(text) -> str:
    """Case and punctuation ignored, '&' read as 'and'."""
    return matching.squash((text or "").replace("&", " and "))


def booking_customer(conn, subject) -> tuple:
    """(party id, key) of the one party whose name is the C/ name of a
    booking subject, merged_into followed; (None, '') when the subject is
    not a booking, names nobody, or two parties."""
    if "BOOKING" not in (subject or "").upper():
        return None, ""
    found = _BOOKING_CUSTOMER.search(subject or "")
    wanted = _whole_name(found.group("name")) if found else ""
    if not wanted:
        return None, ""
    hits = set()
    for row in conn.execute("SELECT id, key, full_name, aliases, merged_into FROM party WHERE placeholder = 0"):
        names = [row["key"], row["full_name"]] + (row["aliases"] or "").split(",")
        if any(_whole_name(n) == wanted for n in names if n):
            hits.add(row["merged_into"] or row["id"])           # merges never chain (merge_parties)
    if len(hits) != 1:
        return None, ""
    party = hits.pop()
    row = conn.execute("SELECT key FROM party WHERE id = ?", (party,)).fetchone()
    return party, row["key"] if row else ""


def build(conn: sqlite3.Connection, mail, case_requester_domains=(), force_asking=False) -> Draft:
    """Pre-fill a registration from one mail. Reads the database, writes nothing.
    `force_asking` (F1, Log anyway): read the mail as us asking the people
    outside on it for a rate, whatever its words say. RG1 (C): a booking
    subject's C/ name, when it is one party's whole name, is the requester."""
    draft = _build(conn, mail, case_requester_domains, force_asking)
    party, key = booking_customer(conn, mail.subject)
    if party is not None and draft.values.get("requester_party_id") != party:
        draft.set("requester_party_id", party, FROM_BOOKING, mail.subject)
        draft.requester_key = key
        draft.new_party = None
        draft.vendors = [v for v in draft.vendors if v.get("party_id") != party]
    # RG2a (E): a company's name is not a place - the requester's, any
    # party's (carriers included), a company word, or a line of a signature
    index = getattr(draft, "_index", None)
    if index is not None:
        _refuse_company_places(conn, draft, index)
    # RG2b (1): a JJC colleague's forward or mail quoting the inquiry - the
    # Owner starts as that colleague
    try:
        _owner_from_colleague(conn, draft, mail)
    except sqlite3.Error:
        pass
    # RG2b (4, WC2): a new party whose domain the MFN directory has
    try:
        mfn_new_party(conn, draft)
    except (OSError, ValueError, sqlite3.Error):
        pass
    return draft


# ---------------------------------------------------------- RG2b owner --
#: setting: the owner who is her (else the Windows login name)
ME_SETTING = "owner_me"


def colleague_name(sender_name) -> str:
    """'JJC-VICTOR LANG' -> 'VICTOR LANG': a JJC display name without its
    office code; '' when the name carries another office's code."""
    text = " ".join((sender_name or "").split())
    found = parsing._OFFICE_PREFIX.match(text)
    if found:
        if found.group(1) != HOME_OFFICE:
            return ""
        text = text[found.end():].strip()
    return text


def owner_for_name(conn, name) -> str:
    """The one Settings owner whose first name or full name is `name`'s
    (compared without case); '' for none or two."""
    from . import assign_list
    words = name.split()
    if not words:
        return ""
    full, first = " ".join(words).casefold(), words[0].casefold()
    hits = [o for o in assign_list.owners(conn) if o.casefold() in (full, first)]
    return hits[0] if len(hits) == 1 else ""


def owner_me(conn) -> str:
    """The owner who is her: setting owner_me, else the owner named like the
    Windows login; '' when neither names one."""
    me = (db.get_setting(conn, ME_SETTING, "") or "").strip()
    if not me:
        import getpass
        try:
            me = getpass.getuser()
        except Exception:
            me = ""
    return owner_for_name(conn, me) if me else ""


def _owner_from_colleague(conn, draft, mail) -> None:
    """RG2b (1): the registered mail is a JJC colleague's - a forward, or a
    mail of ours quoting the inquiry written to us (the mails RG1 reads the
    requester from the quoted inquiry for): Owner starts as that colleague
    when one owner, and not her, has the name. She changes it before
    saving."""
    if not getattr(mail, "sender_internal", False) or _sender_office(mail):
        return
    if forward_kind(mail) != "colleague" and _quoted_inquiry(
            draft.quoted, _internal_domains(conn, mail)) is None:
        return
    name = colleague_name(getattr(mail, "sender_name", ""))
    owner = owner_for_name(conn, name)
    if not owner or owner == owner_me(conn):
        return
    draft.set("owner", owner, "the colleague whose mail this is ({})".format(mail.sender_name))


# ------------------------------------------------------------ RG2b WC2 --
MFN_LINE = "from the MFN directory"


def mfn_new_party(conn, draft, directory=None) -> None:
    """RG2b (4, WC2): the new party a domain no party has starts from the
    MFN directory's office with that domain (one office; several, the one
    in the mail's country - the new party's, else the domain's). Several
    abroad: listed to pick (new_party['mfn_offices']), nothing filled. The
    directory is only read; the party is made at Register, as before."""
    new = draft.new_party
    if not new or new.get("type") == "Office":
        return
    domain = (new.get("domain") or "").strip().lower()
    if not domain or matching.by_domain(conn, domain):
        return
    from . import mfn_link
    directory = directory if directory is not None else mfn_link.cached_directory(db.internal_domains(conn))
    if directory is None:
        return
    members = sorted(directory.by_domain.get(domain, ()))
    if not members:
        return
    iso = ""
    country = new.get("country") or ""
    if country and country != "Unknown":
        row = conn.execute("SELECT iso2 FROM country WHERE name = ?", (country,)).fetchone()
        iso = (row["iso2"] or "").upper() if row is not None and row["iso2"] else ""
    iso = iso or mfn_link.tld_code(domain)
    pool = members if len(members) == 1 else [m for m in members if directory.offices[m].code == iso]
    if len(pool) > 1:
        heads = [m for m in pool if directory.offices[m].head()]
        pool = heads if len(heads) == 1 else pool
    if len(members) == 1 and not signature_names(draft, domain, directory.offices[members[0]],
                                                  _mfn_office(conn, directory.offices[members[0]])):
        # FX7 (her decision 2): one office fills only when the mail's
        # signature names its city or its country; else it is listed to pick
        office = _mfn_office(conn, directory.offices[members[0]])
        new["mfn_offices"] = [office]
        new["mfn_why"] = "{}: {}, {}, {} - the mail's signature does not name it".format(
            MFN_LINE, office["company"], office["city"] or "-",
            office["country"] or directory.offices[members[0]].country or office["member"])
        return
    if len(pool) != 1:
        listed = members if not pool else pool
        new["mfn_offices"] = [_mfn_office(conn, directory.offices[m]) for m in listed]
        new["mfn_why"] = "{} MFN offices have {}{}: pick one or fill it in".format(
            len(listed), domain, "" if pool else ", none in {}".format(country or iso or "the mail's country"))
        return
    office = _mfn_office(conn, directory.offices[pool[0]])
    sources = new.setdefault("sources", {})
    new["key"] = office["company"]
    sources["key"] = MFN_LINE
    if office["country"]:
        new["country"] = office["country"]
        sources["country"] = MFN_LINE
    new["city"], new["mfn_no"] = office["city"], office["member"]
    new["domain"] = domain
    sources["domain"] = MFN_LINE
    new["mfn"] = "{} ({} {}, {})".format(MFN_LINE, office["member"], office["company"],
                                         office["city"] or office["country"] or "?")


def signature_texts(draft, domain) -> list:
    """FX7: the text whose signature speaks for the new party - the
    registered mail's own part when its sender writes from `domain`, else
    the quoted mails written from that domain (a forward's inquiry), each
    its own lines."""
    from . import mail_archive
    mail = getattr(draft, "mail", None)
    if mail is None:
        return []
    sender = matching.domain_of((getattr(mail, "sender_address", "") or "").lower())
    if sender == domain:
        return [mail_archive.top_part(getattr(mail, "body_excerpt", "") or "")]
    return [q.text for q in (getattr(draft, "quoted", None) or [])
            if matching.domain_of((q.address or "").lower()) == domain]


def _named(words, lines, cased=False) -> bool:
    words = " ".join((words or "").split())
    if not words:
        return False
    pattern = re.compile(r"(?<![A-Za-z0-9]){}(?![A-Za-z0-9])".format(re.escape(words)), 0 if cased else re.I)
    return any(pattern.search(line) for line in lines)


#: FX7: a signature's contact line - where its address sits when the mail
#: has no closing line ('Best regards,') to read under
_CONTACT_LINE = re.compile(
    r"^\s*(?:address|add|addr|direcci[oó]n|tel|tels|phone|mobile|mob|cell|fax|whatsapp|e-?mail|correo|"
    r"web|website|www\.|[tmefa])\s*[.:]", re.I)


#: FX7: a short line that starts with a closing ('Saludos cordiales/ Best
#: Regards/ ...', 'Best regards from Lima') - _CLOSING wants the whole line
_CLOSING_START = re.compile(
    r"^\s*(?:best\s+regards|kind\s+regards|warm\s+regards|regards|rgds|br|saludos|atentamente|cordialmente|"
    r"atte|thanks\s+(?:and|&)\s+(?:best\s+)?regards|thank\s+you\s+(?:and|&)\s+(?:best\s+)?regards|sincerely|cheers)\b",
    re.I)


def signature_block(texts) -> list:
    """FX7: the signature lines of each text - the lines under its closing
    line (_signature_lines); else under a short line that starts with a
    closing (_CLOSING_START); else the lines around its first contact line
    (Address:, Tel:, Email:, ...): three before it, eight from it."""
    out = []
    for text in texts:
        found = _signature_lines([text])
        lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
        if not found:
            for i, line in enumerate(lines):
                if len(line) <= 60 and _CLOSING_START.match(line):
                    found = lines[i + 1:i + 9]
                    break
        if not found:
            for i, line in enumerate(lines):
                if _CONTACT_LINE.match(line):
                    found = lines[max(i - 3, 0):i + 8]
                    break
        out.extend(found)
    return out


def signature_names(draft, domain, office, entry) -> bool:
    """FX7: the signature block (signature_block: _signature_lines, else the
    contact lines) of the mail's own part names the MFN office's city (as
    the directory writes it) or its country (OppList's name or MFN's, or
    the ISO code as a word) - whole words, without case; the ISO code only
    as written, in capitals ('IN', 'IT' and 'NO' are words too)."""
    lines = signature_block(signature_texts(draft, domain))
    if not lines:
        return False
    if _named(office.city, lines) or _named(entry.get("country"), lines) or _named(office.country, lines):
        return True
    return bool(office.code) and len(office.code) == 2 and _named(office.code.upper(), lines, cased=True)


def _mfn_office(conn, office) -> dict:
    row = conn.execute("SELECT name FROM country WHERE upper(iso2) = ?", (office.code,)).fetchone() \
        if office.code else None
    return {"member": office.member_id, "company": office.company, "city": office.city,
            "country": row["name"] if row is not None else "",
            "label": "{} ({}, {})".format(office.company, office.city or "-", office.country or office.code)}


def _build(conn: sqlite3.Connection, mail, case_requester_domains=(), force_asking=False) -> Draft:
    draft = Draft(mail=mail)
    draft.multi_places = db.master_active(conn)
    known = _known_locations(conn)
    index = place_index(conn)

    subject = parsing.parse_subject(mail.subject, known_locations=known,
                                    known_countries=set(index["countries"].values()))
    draft.subject = subject
    draft.notes.extend(mail.notes)

    # --- what the mail itself states -----------------------------------
    draft.set("received_on", mail.received_on, FROM_MAIL)
    draft.set("source_subject", mail.subject, FROM_MAIL)
    draft.set("thread_key", subject.thread_key, FROM_SUBJECT)
    draft.set("conversation_id", mail.conversation_id, FROM_MAIL)
    draft.set("entry_id", mail.entry_id, FROM_MAIL)

    draft.quoted = parsing.quoted_mails(getattr(mail, "body_excerpt", ""))
    original = _original_mail(draft.quoted) if mail.sender_internal else None
    asking, below = _asking_mail(draft.quoted) if mail.sender_internal else (None, [])
    if asking is not None:
        original = asking
    # The quoted chain, bottom first: the mail that started the thread is
    # the last one quoted, and it is the one that states the lane and the
    # cargo. Read upward from there and stop at the first mail that says.
    chain = [q for q in reversed(draft.quoted) if q.text]

    # --- what the subject suggests --------------------------------------
    # SA1, Victor's rule: a truck leg of an ocean or air move (IPI) takes
    # the mode the cargo came by, so a mail that says IPI gets no truck mode
    ipi = parsing.says_ipi(mail.subject, getattr(mail, "body_excerpt", ""))
    if ipi and subject.service_mode in parsing.TRUCK_MODES:
        draft.notes.append("The mail says {}: a truck leg of an ocean or air move takes the cargo's "
                           "mode, not {}.".format(ipi, subject.service_mode))
    else:
        draft.set("service_mode", subject.service_mode, FROM_SUBJECT)
    if subject.containers and not subject.service_mode:
        # '1X20GP' in the subject: boxes mean FCL, whatever else it says
        draft.set("service_mode", "FCL", FROM_SUBJECT)
    draft.set("incoterm", subject.incoterm, FROM_SUBJECT)
    draft.set("end_customer", subject.end_customer, FROM_SUBJECT)
    for qty, box in subject.containers:
        draft.set("container_type", box, FROM_SUBJECT)
        if qty:
            draft.set("container_qty", qty, FROM_SUBJECT)
        break
    draft.special_tags = subject.special_tags
    draft.references = subject.references
    draft.unknown_locations = list(subject.unknown_locations)
    own_lines = parsing.body_lines(getattr(mail, "body_excerpt", ""))
    # 'FRETE MARITIMO' / 'OCEAN' in the subject (13g): a sea shipment, the
    # mode left for her to pick among the sea modes
    sea = parsing.subject_sea_word(subject)
    if sea and "service_mode" not in draft.values:
        draft.notes.append(
            "The subject says {}: a sea shipment - pick the mode ({}).".format(
                sea, ", ".join(parsing.SEA_MODES[:4])))
    # --- the mode, from the words in the mail when the subject has none --
    if "service_mode" not in draft.values and not sea:
        if not _mode_from_text(draft, own_lines, FROM_BODY):
            for q in chain:
                if _mode_from_text(draft, parsing.body_lines(q.text), _quoted_label(q)):
                    break

    location_country = location_countries(conn)
    primary = subject.lane_groups[0] if subject.lane_groups else []
    for origin, dest in subject.lanes:
        draft.lanes.append({
            "origin": origin,
            "dest": dest,
            "origin_known": origin in known,
            "dest_known": dest in known,
            "origin_country": location_country.get(origin),
            "dest_country": location_country.get(dest),
            # Ticked by default only when the lanes came out of one segment.
            "primary": (origin, dest) in primary,
        })
    # batch 23: 'MIA - SAI OR VAP' is one route to either place
    _fold_alternatives(draft, subject.alternatives)
    if draft.lanes:
        first = draft.lanes[0]
        draft.set("origin_loc", first["origin"], FROM_SUBJECT)
        draft.set("dest_loc", first["dest"], FROM_SUBJECT)
        draft.set("origin_country", first["origin_country"], FROM_SUBJECT)
        draft.set("dest_country", first["dest_country"], FROM_SUBJECT)
    ticked = [l for l in draft.lanes if l["primary"]]
    if len(ticked) > 1:
        draft.notes.append(
            "{} lanes detected; saving creates one case per ticked lane, all "
            "in one request group.".format(len(ticked)))
    if len(draft.lanes) > len(ticked):
        draft.notes.append(
            "Another reading of the subject gives {}; left unticked."
            .format(", ".join("{} - {}".format(l["origin"], l["dest"])
                              for l in draft.lanes if not l["primary"])))
    _resolve_lanes(draft, index)
    # A country written where a place would go ('CHINA TO SANTOS') is not an
    # unknown location: the resolver read it as the country, so it leaves
    # the 'Add location...' list (D3: roll-ups are not places).
    draft.unknown_locations = [
        u for u in draft.unknown_locations
        if country_name(index, u) is None and u.casefold() not in index["by_name"]
        and not _TWO_LETTERS.match(u)]
    if draft.lanes:
        first = draft.lanes[0]
        for side in ("origin", "dest"):
            if first[side]:
                draft.set(side + "_loc", first[side], draft.sources.get(side + "_loc", FROM_SUBJECT))
            elif side + "_loc" in draft.values:
                del draft.values[side + "_loc"]
            draft.set(side + "_country", first[side + "_country"],
                      draft.sources.get(side + "_loc", FROM_SUBJECT))
    # --- RG2a (A): a colleague's forward or our own mail quoting the
    # customer's inquiry - the lane is the inquiry's (the mail RG1 takes the
    # requester from); the forwarder's own lines only fill what it leaves out
    if draft.lanes and any(not draft.lanes[0].get(s) and draft.lanes[0].get(s + "_country")
                           for s in ("origin", "dest")):
        # RG2a: the subject gave a side as a country only ('USA TO PENANG'):
        # the mail's own lines may name a place there
        _fill_missing_side(draft, getattr(mail, "body_excerpt", ""), known, location_country, index, FROM_BODY,
                           only_empty=True)
        for q in chain:
            if all(draft.lanes[0].get(s) or not draft.lanes[0].get(s + "_country") for s in ("origin", "dest")):
                break
            _fill_missing_side(draft, q.text, known, location_country, index, _quoted_label(q), only_empty=True,
                               tables=False)
    lane_inquiry = _lane_inquiry(conn, mail, draft.quoted) if not draft.lanes else None
    if lane_inquiry is not None:
        _lanes_from_text(draft, lane_inquiry.text, known, location_country, index, _quoted_label(lane_inquiry),
                         tables=True)
        if draft.lanes:
            _fill_missing_side(draft, getattr(mail, "body_excerpt", ""), known, location_country, index, FROM_BODY)
    # --- the body, only when the subject named no lane ---------------------
    if not draft.lanes:
        _lanes_from_body(draft, mail, known, location_country)
        _resolve_lanes(draft, index)
        if draft.lanes:
            first = draft.lanes[0]
            for side in ("origin", "dest"):
                if first[side]:
                    draft.set(side + "_loc", first[side], FROM_BODY,
                              draft.evidence.get(side + "_loc"))
                elif side in draft.location_choices and side + "_loc" in draft.values:
                    del draft.values[side + "_loc"]
                draft.set(side + "_country", first[side + "_country"], FROM_BODY)
    # --- and, failing that, the quoted chain from the bottom up ------------
    for q in chain:
        if draft.lanes:
            break
        if q is lane_inquiry:
            continue
        _lanes_from_text(draft, q.text, known, location_country, index, _quoted_label(q))
    # --- RG2a (C): the subject's codes and names ('HK-SCL', 'ex Ningbo',
    # 'CHINA TO USA') when the subject parser and the text named no lane; a
    # side the text gave stays the text's (the body wins)
    if not subject.lanes and not all(draft.values.get(n) for n in ("origin_loc", "dest_loc")):
        _subject_lane(draft, mail.subject, index)
    # RG2a (B): the subject's own place and country ('... TO PUNTA ARENAS -
    # CHILE') stays the destination over a table's cell that names another
    if subject.single_places and draft.read_from.get("dest_loc") == "table":
        place, country, written = subject.single_places[0]
        key, resolved = resolve_place(index, "{}, {}".format(place, country))
        if key and key != draft.values.get("dest_loc"):
            draft.read_from.pop("dest_loc", None)
            draft.set("dest_loc", key, FROM_SINGLE, written)
            draft.set("dest_country", resolved or index["country_of"].get(key), FROM_SINGLE, written)
            if draft.lanes:
                draft.lanes[0].update(dest=key, dest_known=True, dest_country=draft.values.get("dest_country"))
                draft.lanes[0].pop("dest_words", None)
    # --- one place and its country, nothing else: the destination ---------
    if not draft.lanes and "dest_country" not in draft.values and subject.single_places:
        _single_place_as_dest(draft, subject.single_places[0], index)
    for short in subject.abbreviations:
        draft.notes.append(
            "'{}' in the subject: three to five capitals, not a location code - "
            "possibly the end customer's abbreviation; not used as a place."
            .format(short))
    # --- pickup and delivery addresses ------------------------------------
    _addresses_from_text(draft, own_lines, FROM_BODY)
    for q in chain:
        if all(name in draft.values for name in ("origin_alt", "dest_alt", "end_customer")):
            break
        _addresses_from_text(draft, parsing.body_lines(q.text), _quoted_label(q))
    # --- 'Terms of sale: FCA' ------------------------------------------------
    if "incoterm" not in draft.values:
        for lines, source in [(own_lines, FROM_BODY)] + [(parsing.body_lines(q.text), _quoted_label(q))
                                                          for q in chain]:
            term, line = parsing.body_incoterm(lines)
            if term:
                draft.set("incoterm", term, source, line)
                break
            if line:
                draft.notes.append("'{}' - not an incoterm; Incoterm left empty.".format(line))
                break
    # --- 'Terms: door-to-door please': a scope, not an incoterm -----------
    for line in parsing.terms_lines(own_lines + [l for q in chain for l in parsing.body_lines(q.text)]):
        draft.notes.append("'{}' - a service scope, not an incoterm; Incoterm left empty.".format(line))
    # --- 'Service Level: 2 - 3 DAYS Economy Service' ----------------------
    for note in parsing.air_service_lines(own_lines + [l for q in chain for l in parsing.body_lines(q.text)]):
        if note not in draft.notes:
            draft.notes.append(note)
    # --- our own mail listing charges with amounts: we quoted -------------
    if mail.sender_internal and "status" not in draft.values:
        line = parsing.our_quote_line(own_lines)
        if line:
            draft.set("status", "Quoted", FROM_OUR_QUOTE, line)
    # --- DD1: Non-stackable, from the subject or the cargo's text ----------
    _non_stackable(conn, draft, mail, own_lines, chain)
    # --- and the quantity, when the subject counted no boxes ---------------
    if not subject.containers:
        _qty_from_body(draft, mail, subject)
        for q in chain:
            if "qty_raw" in draft.values:
                break
            _qty_from_body(draft, mail, subject, text=q.text, source=_quoted_label(q))
    else:
        for q in chain:
            if "qty_raw" in draft.values:
                break
            qty = parsing.parse_body_qty(q.text)
            if qty.qty_raw:
                draft.set("qty_raw", qty.qty_raw, _quoted_label(q), qty.evidence)
    # --- D7a: the HS code and the value of the goods, per cargo line -------
    _hs_and_value(draft, mail, own_lines, chain)
    # --- the commodity: 'general cargo: pump housings' -------------------
    if "commodity" not in draft.values:
        value, line = parsing.body_commodity(
            parsing.join_label_values(parsing.body_lines(getattr(mail, "body_excerpt", ""))))
        if value:
            draft.set("commodity", value, FROM_BODY, line)
        else:
            for q in chain:
                value, line = parsing.body_commodity(parsing.join_label_values(q.text.splitlines()))
                if value:
                    draft.set("commodity", value, _quoted_label(q), line)
                    break

    # --- 'the final destination is Puerto Montt, Chile' --------------------
    if not any(name in draft.values for name in ("dest_loc", "dest_country", "dest_alt")):
        if not _final_destination_from_text(draft, own_lines, index, FROM_FINAL_DEST):
            for q in chain:
                if _final_destination_from_text(draft, parsing.body_lines(q.text), index,
                                                "{} ({})".format(FROM_FINAL_DEST, _quoted_label(q))):
                    break

    # --- what was read as a place and cannot be one ----------------------
    _refuse_junk_places(draft, index, person_names(conn, mail, draft.quoted))
    # --- RG2a: a country read as a place ('BRAZIL') is the country
    for side in ("origin", "dest"):
        value = draft.values.get(side + "_loc")
        country = country_name(index, value) if value and value.casefold() not in index["by_name"] else None
        if country:
            source, evidence = draft.sources.get(side + "_loc"), draft.evidence.get(side + "_loc")
            draft.values.pop(side + "_loc")
            draft.sources.pop(side + "_loc", None)
            draft.evidence.pop(side + "_loc", None)
            draft.read_from.pop(side + "_loc", None)
            draft.unknown_locations = [u for u in draft.unknown_locations if u.upper() != value.upper()]
            if not draft.values.get(side + "_country"):
                draft.set(side + "_country", country, source or FROM_BODY, evidence)
    # --- RG2a (D): the master place the case's mode wants; (F) a place not
    # on file that the official code lists name, offered to be created
    _mode_places(draft, index)
    _official_new_places(conn, draft, index)
    _place_words(draft)
    draft._index = index

    # --- when it came in: the date of the mail the content was read from --
    _received_from_content(draft, mail, subject, chain, original)
    if not any(name in draft.values for name in ("dest_loc", "dest_country", "dest_alt")):
        draft.notes.append("destination not stated in this mail")

    # --- batch 23: route A's other destination places ('X or Y'), and the
    # Packing each cargo line's word means on her list (the word kept)
    if not draft.values.get("dest_loc"):
        draft.dest_more = []
    elif draft.lanes:
        draft.dest_more = list(draft.lanes[0].get("dest_more") or [])
    elif draft.dest_more:
        draft.dest_more = [k for k in _resolve_more(index, draft.dest_more) if k != draft.values["dest_loc"]]
    units = packing.units_of(conn)
    for line in draft.cargo_lines:
        if line.get("pack_word"):
            line["pack_unit"] = packing.unit_for(line["pack_word"], units)

    if draft.unknown_locations:
        draft.notes.append(
            "Not in the location list yet: {}. Saving offers 'Add "
            "location...'.".format(", ".join(draft.unknown_locations)))

    # --- who is asking ---------------------------------------------------
    sender = matching.SenderInfo(
        address=mail.sender_address, name=mail.sender_name,
        internal=mail.sender_internal, recipients=mail.recipients)
    internal_domains = _internal_domains(conn, mail)
    vendor_addresses = _outside_recipients(mail, internal_domains)
    # another of our offices on the To line is somebody we asked, too
    office_recipients = _office_recipients(mail) if mail.sender_internal else {}
    # RG1 (B): a mail another of our offices wrote is that office's
    # inquiry, before anything decides we sent it (its own colleagues on
    # the To line are not offices we asked)
    office_sender = _sender_office(mail)
    we_sent = bool((mail.sender_internal or force_asking) and (vendor_addresses or office_recipients)
                   and not office_sender)
    # D1 (1b): a thread we started with a vendor - our later mails are
    # follow-ups of that request and theirs are its replies, whoever sent
    # the selected one; never an inquiry. RG1 (D): unless our first mail
    # offered our services and they answered asking us for a rate
    if not forward_kind(mail) and not office_sender:
        thread_vendor, since = vendor_thread_of(draft.quoted, internal_domains)
        wanted = {str(d).strip().lower() for d in case_requester_domains if d}
        if thread_vendor and thread_vendor not in wanted and (
                thread_vendor == matching.domain_of(mail.sender_address) or thread_vendor in vendor_addresses) \
                and not _answers_our_offer(mail, draft.quoted, thread_vendor):
            _vendor_thread_draft(conn, draft, mail, thread_vendor, since, vendor_addresses, force_asking)
            return draft
    # RG1 (A, E): the inquiry a JJC mail quotes - its requester's, whoever
    # else the mail goes to
    inquiry = _quoted_inquiry(draft.quoted, internal_domains) \
        if mail.sender_internal and not office_sender else None
    # AL3: a mail a colleague forwarded to us - who asked, and when, is in
    # the forwarded header block, not in the forward's own From and date
    kind = "" if we_sent else forward_kind(mail)
    if kind == "colleague":
        draft.forward_note = "Forwarded by {}, {}".format(
            mail.sender_name or mail.sender_address or "a colleague",
            _when(getattr(mail, "received_at", "") or mail.received_on))
        if _forward_draft(conn, draft, mail, internal_domains):
            return draft
        draft.forward_line, draft.forward_level = FORWARD_CHECK, "check"
    elif kind == "office" and draft.quoted:
        top = draft.quoted[0]
        who = ("{} <{}>".format(top.name, top.address) if top.name and top.address
               else top.address or top.name)
        if who:
            draft.forward_line = "Forwarded mail from {}{}".format(
                who, ", " + _when(top.sent_at) if top.sent_at else "")
            draft.forward_level = "muted"
    # v31: an agent's enquiry to another of our offices, the home office only copied -
    # that office handles it; the agent is still the requester. Only the
    # Handled by select is prefilled: no line under it, no draft note (11d)
    handler = handling_office(mail)
    if handler:
        draft.set("handled_by", handler, "")
    quoted = [address for address in parsing.quoted_senders(
        getattr(mail, "body_excerpt", ""))
        if matching.domain_of(address) not in internal_domains]

    # A mail we sent out is either us asking a vendor or us answering the
    # party who asked us. The requester is on the mail when the subject
    # still carries a reply prefix and the person we are writing to wrote
    # a mail quoted underneath that asked us for a rate (a sales call or
    # a hello quoted under our question makes nobody a requester), or when
    # they are the case's own requester. They are never offered
    # as a vendor.
    top = "\n".join(parsing.body_lines(getattr(mail, "body_excerpt", "")))
    if we_sent:
        draft.direction, draft.direction_words = mail_direction_why(mail.subject, top)
        if force_asking:
            draft.direction, draft.direction_words = "asking", ""
    enquiry_domains = _enquiry_domains(draft.quoted, draft.direction, internal_domains)
    requester_domains = []
    if we_sent:
        if parsing.has_reply_prefix(mail.subject):
            requester_domains += [d for d in vendor_addresses if d in enquiry_domains]
        wanted = {str(d).strip().lower() for d in case_requester_domains if d}
        requester_domains += [d for d in vendor_addresses
                              if d in wanted and d not in requester_domains]
    # D1: our reply with several outside recipients in the quoted chain -
    # the requester is the one whose mail in the chain was written to us
    # (_asking_mail); the others there wrote to the requester, not to us,
    # and are neither requester nor vendor
    bystanders = []
    if we_sent and parsing.has_reply_prefix(mail.subject) and asking is not None:
        senders = {matching.domain_of(q.address) for q in draft.quoted if not q.office}
        in_chain = [d for d in vendor_addresses if d in senders]
        ask_domain = matching.domain_of(asking.address)
        if len(in_chain) > 1 and ask_domain in vendor_addresses:
            bystanders = [d for d in in_chain if d != ask_domain]
            requester_domains = [ask_domain] + [d for d in requester_domains
                                                if d != ask_domain and d not in bystanders]
    # RG1 (A): the quoted inquiry's sender, written to on this mail, asked -
    # they are not a vendor; an office that asked is not an office we asked
    asker_office = ""
    if we_sent and inquiry is not None:
        if inquiry.office:
            asker_office = inquiry.office
            office_recipients = {c: a for c, a in office_recipients.items() if c != inquiry.office}
        else:
            domain = matching.domain_of(inquiry.address)
            if domain in vendor_addresses and domain not in requester_domains and domain not in bystanders:
                requester_domains.insert(0, domain)
    vendor_candidates = {d: a for d, a in vendor_addresses.items()
                         if d not in requester_domains and d not in bystanders}
    # The whole mail is an answer to the requester unless it also asks
    # somebody else on it for a rate.
    draft.reply_to_requester = bool(requester_domains or asker_office) and not (
        draft.direction == "asking" and (vendor_candidates or office_recipients))
    draft.outbound = bool(we_sent and not draft.reply_to_requester)

    draft.party_candidates = matching.find(
        conn, sender, names=subject.names, quoted_addresses=quoted,
        skip_recipients=draft.outbound)
    if bystanders:
        # D1: a company that wrote to the requester is not one to pick
        draft.party_candidates = [m for m in draft.party_candidates if m.detail not in bystanders]

    if draft.reply_to_requester and not requester_domains:
        # RG1 (A): our answer to the office whose inquiry is quoted below
        _reply_draft(conn, draft, "", mail, office=inquiry)
        return draft
    if draft.reply_to_requester:
        _reply_draft(conn, draft, requester_domains[0], mail)
        if asking is not None and matching.domain_of(asking.address) == requester_domains[0]:
            # 4.3.4: the companies below the mail that asked us
            _below_the_asker(conn, draft, asking, below, [mail.subject, asking.subject], extra=bystanders)
        else:
            _set_chain_line(draft, bystanders)
        return draft
    if inquiry is not None:
        # RG1 (E): the office that asked first, not the one that passed it on
        original = inquiry
    if draft.outbound:
        if draft.direction == "quoting":
            _quoting_draft(conn, draft, mail, original)
        else:
            _outbound_draft(conn, draft, vendor_candidates,
                            [a for a in quoted if matching.domain_of(a) in enquiry_domains],
                            asking=draft.direction == "asking",
                            offices=office_recipients, copied=_office_copied(mail))
            if requester_domains:
                # Asking a vendor with the requester copied in: they are
                # the requester, not somebody we asked.
                _requester_from_domain(conn, draft, requester_domains[0], FROM_REPLY)
                draft.vendor_note = _requester_note(draft, requester_domains[0])
                draft.notes.append(draft.vendor_note)
            elif inquiry is not None:
                _requester_from_inquiry(conn, draft, inquiry)
            _set_chain_line(draft, bystanders)
        return draft
    # 13g: another of our offices wrote to us - it is the requester, known
    # by its prefix, never by our shared domain; an outside company quoted
    # below it is whose shipment it is
    office = office_sender
    if office:
        _requester_from_office(conn, draft, office, mail.sender_name or mail.sender_address,
                               "{} ({})".format(FROM_OFFICE_PREFIX, mail.sender_name), "this mail")
        outside = [q for q in draft.quoted if not q.office and q.address]
        if outside:
            _end_customer_below(conn, draft, outside, [mail.subject])
        draft.learn_domain = ""
        draft.learn_for = ""
        return draft
    if original is not None:
        _requester_from_quoted(conn, draft, original)
        if asking is not None and below:
            _below_the_asker(conn, draft, asking, below, [mail.subject, asking.subject])
        if "requester_party_id" in draft.values or draft.new_party:
            return draft

    confident = [m for m in draft.party_candidates if m.confident]
    if len(confident) == 1:
        source = (FROM_SENDER if confident[0].how == matching.MATCH_DOMAIN
                  else FROM_RECIPIENT)
        draft.set("requester_party_id", confident[0].party_id, source)
        draft.requester_key = confident[0].key
    elif len(draft.party_candidates) == 1:
        draft.set("requester_party_id", draft.party_candidates[0].party_id,
                  FROM_SUBJECT)
        draft.requester_key = draft.party_candidates[0].key
    elif len(draft.party_candidates) > 1:
        draft.notes.append(
            "{} possible requesters; pick one.".format(len(draft.party_candidates)))
    elif not mail.sender_internal and matching.domain_of(mail.sender_address):
        _requester_from_domain(conn, draft, matching.domain_of(mail.sender_address),
                               FROM_SENDER)
    else:
        draft.notes.append(
            "No party matched the sender or the subject. Use '+ New' "
            "and the domain will be remembered for next time.")
    return draft


def _requester_from_domain(conn, draft, domain, source) -> bool:
    """The requester for a domain. In order: the domain on
    file; the domain's own name equal to one party's name ('zenithcargo' ~
    'Zenith Cargo' - filled in, domain remembered on save); several such
    parties (listed to choose from); a mailbox provider (nothing guessed);
    else a New party pre-filled from the domain, with the parties whose
    names look alike listed above it. True when the field was filled."""
    domain = (domain or "").lower()
    if not domain:
        return False
    found = matching.by_domain(conn, domain)
    if found:
        draft.set("requester_party_id", found[0].party_id, source)
        draft.requester_key = found[0].key
        return True
    if matching.is_free_mail(domain):
        draft.new_party = {"key": "", "type": "Agent", "country": "Unknown", "domain": "",
                           "sources": {}, "similar": [],
                           "why": "{} is a public mailbox - the company is not guessed".format(domain)}
        draft.notes.append(
            "{} is a public mailbox, so the company behind it is not guessed; "
            "fill in 'New party' yourself.".format(domain))
        return False
    hits = matching.by_domain_stem(conn, domain)
    stem = matching.domain_stem(domain)
    if len(hits) == 1:
        draft.set("requester_party_id", hits[0].party_id,
                  "{} ({} ~ {})".format(FROM_DOMAIN_NAME, stem, hits[0].key))
        draft.requester_key = hits[0].key
        draft.learn_domain = domain
        draft.learn_for = str(hits[0].party_id)
        draft.notes.append(
            "No party has the domain {}, but its name matches '{}'; the domain "
            "is remembered for them when you save.".format(domain, hits[0].key))
        return True
    if hits:
        known = {m.party_id for m in draft.party_candidates}
        draft.party_candidates.extend(h for h in hits if h.party_id not in known)
        draft.learn_domain = domain
        draft.learn_for = ",".join(str(h.party_id) for h in hits)
        draft.notes.append(
            "{} parties are named like the domain {} ({}); pick one and the "
            "domain is remembered for them.".format(
                len(hits), domain, ", ".join(h.key for h in hits)))
        return False
    country = matching.country_for_tld(conn, domain)
    tld = domain.rsplit(".", 1)[-1]
    draft.new_party = {
        "key": suggested_party_name(domain), "type": "Agent",
        "country": country or "Unknown", "domain": domain,
        "sources": {
            "key": "from the domain {}".format(domain),
            "type": "the usual type for a company writing to us - change if not",
            "country": ("from the .{} domain".format(tld) if country
                        else "not in the domain - pick one"),
            "domain": "remembered for the new party when you save",
        },
        "similar": matching.similar_parties(conn, domain),
        "why": "no party has the domain {}".format(domain),
    }
    draft.learn_domain = domain
    draft.learn_for = "new"
    draft.notes.append(
        "No party has the domain {}; 'New party' is filled in from it{}."
        .format(domain, " - check the similar parties listed there first"
                if draft.new_party["similar"] else ""))
    return False


def _mode_from_text(draft, lines, source) -> bool:
    """Air / FCL / LCL from the words in the text. 'Ocean' with
    neither FCL nor LCL is noted, not filled. True when the text said."""
    mode, line = parsing.body_mode(lines)
    if mode in ("Air", "FCL", "LCL", "Sea-Air", "Cross-border Truck"):
        draft.set("service_mode", mode, source, line)
        return True
    if mode == "Ocean":
        draft.notes.append(
            "Ocean freight is mentioned ('{}') but neither FCL nor LCL; the "
            "mode is left for you to pick.".format(line))
        return True
    return False


def _addresses_from_text(draft, lines, source) -> None:
    """'Pick-up:' and 'Delivery to' blocks fill the alt fields; a US state
    and ZIP gives the country; a company on the first line is offered as
    the end customer."""
    found = parsing.parse_addresses(lines)
    for side, label in (("origin", "pickup"), ("dest", "delivery")):
        info = found.get(side)
        if not info:
            continue
        # The company on the first line is worth having even when a nearer
        # mail already gave the address ('Pick up from Flagstaff, AZ 86001').
        if info["company"] and "end_customer" not in draft.values:
            draft.set("end_customer", info["company"],
                      "{} (first line of the {} address)".format(source, label),
                      info["text"])
        if side + "_alt" in draft.values:
            continue
        draft.set(side + "_alt", info["text"], source, info["line"])
        if info["country"] and side + "_country" not in draft.values:
            draft.set(side + "_country", info["country"],
                      "{} (US state and ZIP in the {} address)".format(source, label),
                      info["text"])


def _single_place_as_dest(draft, single, index) -> None:
    """'ENVIO OHIO- EEUU': where it goes is stated, where it comes from
    is not. The place fills the destination (the location when it is on
    file, else only the country); the origin is left empty rather than
    guessed."""
    place, country, written = single
    key, resolved = resolve_place(index, "{}, {}".format(place, country))
    resolved = resolved or country_name(index, country)
    if key:
        draft.set("dest_loc", key, FROM_SINGLE, written)
    if resolved:
        draft.set("dest_country", resolved, FROM_SINGLE, written)
    if key or resolved:
        draft.notes.append(
            "Only one place in the subject ({}, {}); filled in as the destination"
            "{}. The origin is not guessed."
            .format(place, resolved or country,
                    "" if key else " country only - '{}' is not a location on file".format(place)))


#: Where a string went when it was read as a place and cannot be one.
FROM_NOT_A_PLACE = "read as a place, but it is not one"
#: A parsed 'place' longer than this is a sentence, not a location
#: ('RFQ 73105 USA-GHANA' and 'TERRALUX' both became rows on 2026-09-16).
JUNK_PLACE_MAX = 24
#: Four digits in a row: a reference number, never a place name.
_JUNK_DIGITS = re.compile(r"\d{4}")
#: Words that belong to a mail subject, not to a map.
_JUNK_WORDS = re.compile(
    r"\b(rfq|rfp|quote|quotes|quotation|rate|rates|pricing|request|requests|"
    r"shipment|shipments|booking|enquiry|inquiry|po|p/o)\b", re.I)
#: 'USA-GHANA': a lane written with a hyphen, not the name of a place.
_JUNK_HYPHEN = re.compile(r"\s*[-–—]\s*")


def person_key(name) -> str:
    """A person's name the way two spellings of it compare equal: accents
    folded ('Tomás Ibáñez' and 'TOMAS IBANEZ' are one person),
    case ignored, spaces collapsed."""
    import unicodedata

    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.split()).casefold()


_DISPLAY_NAME = re.compile(r"<[^>]*>|\[[^\]]*\]|\([^)]*\)|[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+")
#: A line that closes a mail; the line after it is the writer's name.
_CLOSING = re.compile(
    r"^\s*(best\s+regards|kind\s+regards|regards|rgds|thanks(\s*&\s*rgrds)?|thank\s+you|"
    r"saludos(\s+cordiales)?|atentamente|cordialmente|atte\.?|cheers|sincerely|br)\s*[,.!]?\s*"
    r"(/\s*(best\s+regards|saludos|regards)\s*[,.]?)?\s*$", re.I)
_NAME_LINE = re.compile(r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ.'\-]*(\s+[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ.'\-]*){1,3}$")


def person_names(conn, mail, quoted=()) -> set:
    """The people around a mail, as person_key() spells them: the sender
    and every quoted sender, the names on the To / Cc lines, the name under
    each closing line, and every contact on file (2026-09-23). A
    place candidate that is one of these is somebody, not somewhere."""
    names = set()

    def add(text):
        for part in re.split(r"[;,]", _DISPLAY_NAME.sub("", text or "")):
            part = part.strip(" \"'<>")
            if part and not any(ch.isdigit() for ch in part):
                names.add(person_key(part))

    add(getattr(mail, "sender_name", ""))
    for line in (getattr(mail, "display_to", "") or "").split(";"):
        add(line)
    texts = [getattr(mail, "body_excerpt", "") or ""]
    for q in quoted:
        add(getattr(q, "name", ""))
        add(getattr(q, "to", ""))
        add(getattr(q, "cc", ""))
        texts.append(getattr(q, "text", "") or "")
    for text in texts:
        lines = [ln.strip() for ln in text.splitlines()]
        for i, line in enumerate(lines):
            if not _CLOSING.match(line):
                continue
            for following in lines[i + 1:i + 4]:
                if following:
                    if _NAME_LINE.match(following):
                        names.add(person_key(following))
                    break
    if conn is not None:
        try:
            for row in conn.execute("SELECT name FROM contact WHERE name IS NOT NULL"):
                names.add(person_key(row["name"]))
        except sqlite3.Error:
            pass
    return {n for n in names if n}


def junk_place(text, is_country=None, names=()) -> str:
    """Why this string cannot be a location, or '' when it could be.

    The intake reads places out of subjects and bodies, and a subject is
    full of things that look like one. Anything this refuses is kept as
    free text and offered to the user, never created as a location
    (2026-09-17). `names` are the people around the mail, from
    person_names(): 'con copia a Tomás Ibáñez' is a request to
    copy somebody, not a lane to somewhere (2026-09-23).
    """
    text = " ".join((text or "").split())
    if not text:
        return ""
    if names and person_key(text) in names:
        return "is a person's name"
    if len(text) > JUNK_PLACE_MAX:
        return "is longer than {} characters".format(JUNK_PLACE_MAX)
    if _JUNK_DIGITS.search(text):
        return "carries a four-digit number"
    found = _JUNK_WORDS.search(text)
    if found:
        return "contains '{}', a mail word".format(found.group(0).upper())
    if is_country is not None:
        parts = [p for p in _JUNK_HYPHEN.split(text) if p]
        if len(parts) > 1 and all(is_country(part) for part in parts):
            return "is a country-to-country lane"
    return ""


def _refuse_junk_places(draft, index, names=()) -> None:
    """Take the strings that cannot be places out of the lane fields and
    leave them as free text, with a line saying why. When one
    side of a lane is a person, the sentence it was read from was about
    that person, so the other side of the same reading goes too."""
    def is_country(part):
        return bool(country_name(index, part))

    def why_not(text):
        return junk_place(text, is_country, names)

    reasons = {side: why_not(draft.values.get("{}_loc".format(side)))
               for side in ("origin", "dest")}
    for side, other in (("origin", "dest"), ("dest", "origin")):
        if reasons[side] == "is a person's name" and not reasons[other] \
                and draft.values.get("{}_loc".format(other)) \
                and draft.sources.get("{}_loc".format(other)) == draft.sources.get("{}_loc".format(side)):
            reasons[other] = "is the other half of a sentence about {}".format(
                draft.values.get("{}_loc".format(side)))
    for side, label in (("origin", "origin"), ("dest", "destination")):
        name = "{}_loc".format(side)
        why = reasons[side]
        if not why:
            continue
        text = draft.values.pop(name)
        draft.sources.pop(name, None)
        draft.evidence.pop(name, None)
        alt = "{}_alt".format(side)
        if not draft.values.get(alt):
            draft.set(alt, text, FROM_NOT_A_PLACE)
        draft.notes.append(
            "'{}' {}, so it is not offered as a place; it is in the {} free-text "
            "field instead. 'Add location...' is still there if it really is one."
            .format(text, why, label))
    draft.unknown_locations = [place for place in draft.unknown_locations
                               if not why_not(place)]
    for lane in draft.lanes:
        sides = {side: why_not(lane.get(side)) for side in ("origin", "dest")}
        if any(w == "is a person's name" for w in sides.values()):
            sides = {side: sides[side] or "a sentence about a person" for side in sides}
        for side in ("origin", "dest"):
            if sides[side]:
                lane[side] = ""
                lane["{}_known".format(side)] = False
    draft.lanes = [lane for lane in draft.lanes if lane.get("origin") or lane.get("dest")]


FROM_FINAL_DEST = "'final destination' in the text"
#: 'the final destination is Puerto Montt, Chile', 'final destination: Cusco',
#: 'destino final Temuco' - mid-sentence, unlike the address labels that
#: start a line. The place runs to the end of the sentence.
_FINAL_DEST = re.compile(
    r"\b(?:final\s+destination|destino\s+final|destination\s+finale?)\b"
    r"\s*(?:is|will\s+be|would\s+be|es|ser[aá]|:|-|=)?\s*"
    r"(?P<place>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ.'\-]*(?:[ ]+[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ.'\-]*){0,3}"
    r"(?:,\s*[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ.'\-]*(?:[ ]+[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ.'\-]*){0,2})?)",
    re.I)
_FINAL_DEST_STOP = {"the", "a", "an", "to", "in", "at", "for", "of", "and", "is",
                    "will", "would", "be", "port", "airport", "address"}


def _final_destination_from_text(draft, lines, index, source) -> bool:
    """'For your info, the final destination is Puerto Montt, Chile': the
    place fills the destination - the location when it is on file, else
    only the country. True when something was filled."""
    for line in lines:
        found = _FINAL_DEST.search(line)
        if not found:
            continue
        place = found.group("place").strip(" .")
        words = place.split()
        if not words or words[0].lower() in _FINAL_DEST_STOP:
            continue
        key, country = resolve_place(index, place)
        if key:
            draft.set("dest_loc", key, source, line.strip())
            draft.set("dest_country", country or index["country_of"].get(key), source)
            return True
        if country:
            draft.set("dest_country", country, source, line.strip())
            draft.notes.append(
                "Final destination '{}' is not a location on file; only the "
                "country is filled in.".format(place))
            return True
        draft.notes.append(
            "Final destination '{}' matched no location or country; left for "
            "you to pick.".format(place))
        return False
    return False


def _lanes_from_text(draft, text, known, location_country, index, source, tables=False) -> None:
    """The lane out of a quoted mail's text, labelled as such. RG2a (B):
    a label above its value is read in the inquiry (`tables`), not in the
    older mails further down a chain."""
    body = parsing.parse_body_lanes(text, known_locations=known, tables=tables)
    if not (body.origins or body.dests):
        return
    for origin, dest in body.lanes or [(o, "") for o in body.origins] or [("", d) for d in body.dests]:
        draft.lanes.append({
            "origin": origin.upper(), "dest": dest.upper(),
            "origin_known": origin.upper() in known, "dest_known": dest.upper() in known,
            "origin_country": location_country.get(origin.upper()),
            "dest_country": location_country.get(dest.upper()), "primary": True})
    if body.dest_alternatives:
        _fold_alternatives(draft, [body.dests])         # batch 23: 'X or Y' is one route
    _resolve_lanes(draft, index)
    first = draft.lanes[0]
    for side in ("origin", "dest"):
        if first[side]:
            draft.set(side + "_loc", first[side], source, body.evidence.get(side))
            if body.tabled.get(side):
                draft.read_from[side + "_loc"] = "table"
        draft.set(side + "_country", first[side + "_country"], source)


# ------------------------------------------------- RG2a: the lane's words --
def _lane_inquiry(conn, mail, quoted):
    """RG2a (A): the customer's inquiry a colleague's forward (AL3's
    forward_original) or a mail of ours (RG1's _quoted_inquiry) quotes,
    when it has text; None otherwise."""
    if not quoted:
        return None
    internal = _internal_domains(conn, mail)
    if forward_kind(mail) == "colleague":
        first, _address = forward_original(quoted, internal)
        return first if first is not None and first.text else None
    if getattr(mail, "sender_internal", False) and not _sender_office(mail):
        inquiry = _quoted_inquiry(quoted, internal)
        return inquiry if inquiry is not None and inquiry.text else None
    return None


def _fill_missing_side(draft, text, known, location_country, index, source, only_empty=False,
                       tables=True) -> None:
    """RG2a (A): route A was read from the inquiry; the forwarder's own
    lines (one reading a side) fill a side it left out or named nothing on
    file for, and give the exact gateway of the inquiry's city ('POD:
    Santiago' quoted, 'Dest: SCL' ours). A side both say alike is ours, so
    Received on stays the mail's."""
    if not draft.lanes:
        return
    lane = draft.lanes[0]
    body = parsing.parse_body_lanes(text, known_locations=known, tables=tables)
    filled = False
    for side, places in (("origin", body.origins), ("dest", body.dests)):
        if len(places) != 1 or side in body.conflicts:
            continue
        own_key, _country = resolve_place(index, places[0])
        if only_empty:
            # a subject side that is only a country: a place on file in that country
            if lane.get(side) or not own_key or (lane.get(side + "_country") and
                                                 index["country_of"].get(own_key) != lane.get(side + "_country")):
                continue
        if lane.get(side) and own_key and own_key == lane.get(side):
            draft.set(side + "_loc", own_key, source, body.evidence.get(side))      # the same place: ours
            if draft.values.get(side + "_country"):
                draft.sources[side + "_country"] = source
            continue
        own = index.get("places", {}).get(own_key or "") or {}
        refines = bool(lane.get(side) and own.get("parent_key") and own.get("parent_key") == lane.get(side))
        if lane.get(side) and not refines and (lane.get(side + "_known") or places[0].upper() not in known):
            continue            # the inquiry said it - unless it named nothing on file and these lines do
        if lane.get(side):
            draft.values.pop(side + "_loc", None)
            draft.read_from.pop(side + "_loc", None)
        lane[side] = places[0].upper()
        lane.pop(side + "_words", None)
        lane[side + "_known"] = lane[side] in known
        lane[side + "_country"] = lane.get(side + "_country") or location_country.get(lane[side])
        filled = True
    if not filled:
        return
    _resolve_lanes(draft, index)
    lane = draft.lanes[0]
    for side in ("origin", "dest"):
        if lane.get(side) and not draft.values.get(side + "_loc"):
            draft.set(side + "_loc", lane[side], source, body.evidence.get(side))
            if body.tabled.get(side):
                draft.read_from[side + "_loc"] = "table"
            draft.set(side + "_country", lane.get(side + "_country"), source)


#: US states and Canadian provinces: a two-letter word that is one of them is
#: never read as a country's code
_STATE_CODES = frozenset().union(*db.STATE_CODES.values())

#: RG2a (C): how a subject joins two places, the surest first: a word ('to',
#: 'x', '>'), a dash between spaces, a dash between two words, a slash
_SUBJECT_JOINS = (
    re.compile(r"\s+(?:to|x)\s+|\s*(?:->|>|→)\s*", re.I),
    re.compile(r"\s+[-–—]+\s+"),
    re.compile(r"(?<=[A-Za-z])[-–](?=[A-Za-z])"),
    re.compile(r"\s*/\s*"),
)
#: 'ex Ningbo', 'EX USA', 'FM FLL': where it comes from (never 'ex works')
_SUBJECT_EX = re.compile(r"(?:^|(?<=\s))(?:ex|fm)[\s.\-]+(?!works?\b|w\b|wh\b)", re.I)
_SUBJECT_LEAD = {"EX", "FM", "FROM", "DE", "DESDE"}


def _side_of_subject(index, text, near_end: bool):
    """(key, country, words) for the place a subject writes next to a join:
    up to three words on that side, the longest that is a place on file, a
    country, or a country's ISO code (a two-letter state is not); None."""
    text = re.sub(r"[()\[\]{}\"]", " ", text or "")
    words = [w for w in text.split() if w]
    iso = index.get("iso2", {})
    for n in range(min(3, len(words)), 0, -1):
        piece = words[-n:] if near_end else words[:n]
        if near_end and piece and piece[0].upper().strip(".:") in _SUBJECT_LEAD and len(piece) > 1:
            piece = piece[1:]
        candidate = " ".join(piece).strip(" ,.;:*")
        options = [candidate]
        if "/" in candidate:
            parts = [p for p in candidate.split("/") if p.strip()]
            options += [parts[-1]] if near_end else [parts[0]]
        for option in options:
            option = parsing.lane_place(option).strip(" ,.;:*")
            if len(option) < 2 or option.upper() in parsing.LANE_NOISE:
                continue
            if re.fullmatch(r"[A-Za-z]{2}", option):
                code = option.upper()
                if option.isupper() and code in iso and code not in _STATE_CODES:
                    return "", iso[code], option
                continue
            key, country = resolve_place(index, option)
            if key:
                return key, country or index["country_of"].get(key), option
            whole = country_name(index, option)
            if whole:
                return "", whole, option
    return None


def _subject_lane(draft, subject_text, index) -> None:
    """RG2a (C): a lane the subject parser did not see - two codes or names
    joined by '-', 'to', '/', '>' or 'x' ('HK-SCL', 'SSZ - LAX', 'CHINA TO
    USA'), or 'ex <place>' alone. Only sides the mail's text left empty are
    filled: where the body says otherwise, the body wins."""
    clean = parsing.clean_subject(subject_text or "")
    found = None
    for segment in re.split(r"//|\|\||\s\|\s", clean):
        segment = segment.replace("_", " ").strip()
        if not segment:
            continue
        for join in _SUBJECT_JOINS:
            for m in join.finditer(segment):
                a = _side_of_subject(index, segment[:m.start()], True)
                b = _side_of_subject(index, segment[m.end():], False)
                if a and b and (a[0], a[1]) != (b[0], b[1]):
                    found = (a, b)
                    break
            if found:
                break
        if found:
            break
    if not found:
        for segment in re.split(r"//|\|\||\s\|\s", clean):
            m = _SUBJECT_EX.search(segment)
            a = _side_of_subject(index, segment[m.end():], False) if m else None
            if a:
                found = (a, None)
                break
    if not found:
        return
    if not draft.lanes:
        draft.lanes.append({"origin": "", "dest": "", "origin_known": True, "dest_known": True,
                            "origin_country": None, "dest_country": None, "primary": True})
    lane = draft.lanes[0]
    for side, got in (("origin", found[0]), ("dest", found[1])):
        if not got or draft.values.get(side + "_loc") or lane.get(side):
            continue
        key, country, words = got
        own = draft.values.get(side + "_country")
        if own and country and own != country:
            continue                                   # the text said another country
        lane[side], lane[side + "_known"] = key, True
        lane[side + "_country"] = lane.get(side + "_country") or country
        lane[side + "_words"] = words.upper()
        if key:
            draft.set(side + "_loc", key, FROM_SUBJECT, subject_text)
            draft.read_from[side + "_loc"] = "subject"
        if not own:
            draft.set(side + "_country", country, FROM_SUBJECT, subject_text)
            draft.read_from[side + "_country"] = "subject"
    if not (lane.get("origin") or lane.get("dest")) and not draft.values.get("origin_loc") \
            and not draft.values.get("dest_loc"):
        if lane in draft.lanes and not any(lane.get(s + "_country") for s in ("origin", "dest")):
            draft.lanes.remove(lane)


#: RG2a (D): the modes that ship by sea
_OCEAN_MODES = ("FCL", "LCL", "RORO")
#: RG2a (D): a city read turned into its airport (Air) or its port (FCL,
#: LCL, RORO) when the mail names it as one. Off: on her cases of September
#: to November it made four right places wrong (she keeps PENANG, CHENNAI,
#: ATLANTA, GA and CAIRO for '<city> airport') and one wrong place right;
#: the reader-check before and after (2026-11-20) has the cases.
MODE_PLACES = False


def _mode_place(index, key, mode):
    """(the place the mode wants, [the options when several]) for a place
    on file: Air an airport (a city's own airport), FCL / LCL / RORO a
    seaport (else a ramp); (None, []) to keep it. The road modes keep what
    was read (a gateway turned into its city made LAX wrong on her cases)."""
    places, children = index.get("places", {}), index.get("children", {})
    place = places.get(key)
    if not place or not mode:
        return None, []
    kind = place.get("type")
    city = place if kind == "city" else places.get(place.get("parent_key") or "")
    if mode == "Air":
        if kind == "airport" or city is None:
            return None, []
        airports = [c for c in children.get(city["key"], []) if c.get("type") == "airport"]
        if len(airports) == 1:
            return airports[0], []
        return (None, airports) if len(airports) > 1 and kind == "city" else (None, [])
    if mode in _OCEAN_MODES:
        if kind in ("seaport", "ramp") or city is None:
            return None, []
        ports = [c for c in children.get(city["key"], []) if c.get("type") == "seaport"]
        if len(ports) == 1:
            return ports[0], []
        if len(ports) > 1:
            return (None, ports) if kind == "city" else (None, [])
        ramps = [c for c in children.get(city["key"], []) if c.get("type") == "ramp"]
        if len(ramps) == 1:
            return ramps[0], []
        return None, []
    return None, []


#: RG2a (D): the words that make a place in the mail its airport or its port
_AIRPORT_WORDS = r"(?:international\s+)?(?:airport|apt|aeropuerto|aeroporto)"
_PORT_WORDS = r"(?:sea\s*port|port|harbou?r|puerto|porto|terminal)"


def _named_as_gateway(draft, words, mode) -> bool:
    """RG2a (D): the mail writes the place as its airport (Air) or its port
    (FCL / LCL / RORO): 'Penang airport', 'Tianjin port', 'puerto de
    Valparaiso'. A city named alone stays the city - that is what she
    keeps for it (her cases of September to November)."""
    kind = _AIRPORT_WORDS if mode == "Air" else _PORT_WORDS if mode in _OCEAN_MODES else None
    if not kind:
        return False
    mail = draft.mail
    text = " ".join([getattr(mail, "subject", "") or "", getattr(mail, "body_excerpt", "") or ""])
    for word in {w for w in words if w and len(w) >= 3}:
        w = re.escape(word.split(",")[0].strip())
        if re.search(r"(?<![A-Za-z]){}\s*,?\s+{}\b".format(w, kind), text, re.I) or \
                re.search(r"\b{}\s+(?:of|de|do|da)\s+{}(?![A-Za-z])".format(kind, w), text, re.I):
            return True
    return False


def _mode_places(draft, index) -> None:
    """RG2a (D): a place read is turned into the master place she uses for
    the case's mode when the mail names it as that (an airport for Air, a
    port for the sea modes); two or more candidates are offered, as
    _offer_choice does."""
    mode = draft.values.get("service_mode")
    for side in ("origin", "dest") if MODE_PLACES else ():
        key = draft.values.get(side + "_loc")
        if not key or side in draft.location_choices:
            continue
        lane0 = draft.lanes[0] if draft.lanes else {}
        place = index.get("places", {}).get(key) or {}
        words = [lane0.get(side + "_words") or "", key, place.get("name") or ""]
        if not _named_as_gateway(draft, words, mode):
            continue
        wanted, options = _mode_place(index, key, mode)
        lane = draft.lanes[0] if draft.lanes and draft.lanes[0].get(side) == key else None
        if wanted is not None and wanted["key"] != key:
            draft.values[side + "_loc"] = wanted["key"]
            if lane is not None:
                lane[side] = wanted["key"]
                lane[side + "_known"] = True
            draft.notes.append("'{}' read; the case is {}, so {} ({}).".format(
                key, mode, wanted["key"], wanted.get("type") or "place"))
        elif options:
            draft.values.pop(side + "_loc", None)
            if lane is not None:
                lane[side] = ""
                lane[side + "_known"] = False
                lane[side + "_choices"] = [o["key"] for o in options]
            draft.location_choices[side] = {"text": key, "options": options}
            draft.notes.append("'{}' has {} {}s ({}); the case is {} - pick one below.".format(
                key, len(options), options[0].get("type"), ", ".join(o["key"] for o in options), mode))


def _official_new_places(conn, draft, index) -> None:
    """RG2a (F): a place read that is not on file, but that the official
    code lists name (a UN/LOCODE, an IATA airport, or the name of either in
    the mail's country), is offered as a new master place built from that
    row - created, active, only when she registers."""
    from . import official_places
    mode = draft.values.get("service_mode")
    for side in ("origin", "dest"):
        value = draft.values.get(side + "_loc")
        if not value or value.casefold() in index["by_name"] or country_name(index, value):
            continue
        lane = draft.lanes[0] if draft.lanes and draft.lanes[0].get(side) == value else None
        words = (lane or {}).get(side + "_words") or value
        country = draft.values.get(side + "_country")
        spec = None
        for text in dict.fromkeys([words, value]):
            spec = official_places.place_for(conn, text, country, mode)
            if spec:
                break
        if not spec or (country and spec["country"] != country):
            continue
        rules = db.place_rules(conn, spec["text"], spec["country"], spec["type"])
        if rules["error"]:
            continue
        key = rules["found"]["key"] if rules["found"] is not None else db.location_key(rules["text"])
        if rules["found"] is None:
            draft.official_new[key] = dict(spec, key=key, text=rules["text"])
            draft.notes.append("'{}' is not on file; the official list has it ({}, {}): it is added, active, "
                               "when you register.".format(value, spec["official"], spec["name"]))
        draft.values[side + "_loc"] = key
        draft.read_from[side + "_loc"] = "official list"
        draft.set(side + "_country", spec["country"], draft.sources.get(side + "_loc") or FROM_BODY)
        if lane is not None:
            lane[side] = key
            lane[side + "_known"] = True
            lane[side + "_country"] = lane.get(side + "_country") or spec["country"]
        draft.unknown_locations = [u for u in draft.unknown_locations if u.upper() not in (value.upper(), key)]


def _place_words(draft) -> None:
    """RG2a (layer 2): the words of the mail each side was read from."""
    for side in ("origin", "dest"):
        words = ""
        if draft.lanes:
            words = draft.lanes[0].get(side + "_words") or ""
        if not words and draft.values.get(side + "_loc") and draft.values.get(side + "_loc") not in draft.official_new:
            words = draft.values[side + "_loc"]
        if side in draft.location_choices and not words:
            words = draft.location_choices[side].get("text") or ""
        words = " ".join(str(words).upper().split())
        if words and not _TWO_LETTERS.match(words):
            draft.place_words[side] = words


#: RG2a (E): words that make a name a company's, never a place's
_COMPANY_WORDS = re.compile(
    r"(?:^|[\s,.(])(?:inc|llc|l\.l\.c|ltd|limited|corp|corporation|co\.|gmbh|s\.?a\.?\s+de\s+c\.?v|s\.r\.l|srl|"
    r"ltda|b\.v|plc|pte|logistics|shipping|forwarding|forwarders|trading|industries|international)\.?(?=$|[\s,.)])",
    re.I)


def _signature_lines(texts) -> list:
    """The lines under each closing line ('Best regards,') of the mail's own
    text and the inquiry's: the writer's name, company and address."""
    out = []
    for text in texts:
        lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if _CLOSING.match(line):
                out.extend(lines[i + 1:i + 9])
    return out


def _refuse_company_places(conn, draft, index) -> None:
    """RG2a (E): a place read that is a company's name - the requester's,
    any party's key or full name (carriers included), one carrying a
    company word (INC, LLC, LOGISTICS...) - or that was read from a
    signature block is not a place: it goes to the free-text field, with a
    line saying why. A place on file, or one the official lists name, stays."""
    companies = set()
    for r in conn.execute("SELECT key, full_name FROM party"):
        for name in (r["key"], r["full_name"]):
            if name and len(name.strip()) >= 3:
                companies.add(person_key(name))
    mail = draft.mail
    texts = [getattr(mail, "body_excerpt", "") or ""] + [q.text for q in draft.quoted]
    signature = [" ".join(l.split()).casefold() for l in _signature_lines(texts)]
    for side, label in (("origin", "origin"), ("dest", "destination")):
        name = side + "_loc"
        value = draft.values.get(name)
        if not value or value in index["places"] or value in draft.official_new:
            continue
        why = ""
        if person_key(value) in companies:
            why = "is a company's name (a party on file)"
        elif _COMPANY_WORDS.search(value):
            why = "is a company's name"
        else:
            evidence = " ".join(str(draft.evidence.get(name) or "").replace("…", "").split()).casefold()
            if evidence and draft.sources.get(name) != FROM_SUBJECT and \
                    any(evidence in line or (len(line) > 8 and line in evidence) for line in signature):
                why = "was read from a signature"
        if not why:
            continue
        draft.values.pop(name)
        draft.sources.pop(name, None)
        draft.evidence.pop(name, None)
        draft.read_from.pop(name, None)
        if not draft.values.get(side + "_alt"):
            draft.set(side + "_alt", value, FROM_NOT_A_PLACE)
        if draft.lanes and draft.lanes[0].get(side) == value:
            draft.lanes[0][side] = ""
            draft.lanes[0][side + "_known"] = False
        draft.place_words.pop(side, None)
        draft.unknown_locations = [u for u in draft.unknown_locations if u.upper() != value.upper()]
        draft.notes.append(
            "'{}' {}, so it is not offered as a place; it is in the {} free-text field instead. "
            "'Add location...' is still there if it really is one.".format(value, why, label))
    draft.lanes = [lane for lane in draft.lanes if lane.get("origin") or lane.get("dest")
                   or lane.get("origin_country") or lane.get("dest_country")]


#: RG2a (layer 1): where a field's value came from, in register_read's words
READ_FROM = ("subject", "quoted inquiry", "table", "body", "official list", "sender", "mail", "attachment", "other")
#: RG2a (layer 1): the fields register_read keeps, in the order of the form
READ_FIELDS = ("requester_party_id", "received_on", "origin_country", "origin_loc", "dest_country", "dest_loc",
               "service_mode", "incoterm", "commodity", "container_type", "container_qty")


def reader_values(conn, draft) -> dict:
    """RG2a (layer 1): {field: {'value', 'from'[, 'words']}} for every field
    the reader filled - what the register form carries to Register, where
    register_read keeps it beside the value saved. A party is its key."""
    out = {}
    for name in READ_FIELDS:
        value = draft.values.get(name)
        if value in (None, "", []):
            continue
        if name == "requester_party_id":
            row = conn.execute("SELECT key FROM party WHERE id = ?", (value,)).fetchone()
            value = row["key"] if row else value
        entry = {"value": str(value), "from": read_from(draft, name) or "other"}
        side = name[:-4] if name in ("origin_loc", "dest_loc") else ""
        if side and draft.place_words.get(side):
            entry["words"] = draft.place_words[side]
        out[name] = entry
    return out


def read_from(draft, name) -> str:
    """'subject', 'quoted inquiry', 'table', 'body', 'official list',
    'sender', 'mail', 'attachment' or 'other' for a field the reader filled;
    '' when it filled nothing."""
    if draft.values.get(name) in (None, "", []):
        return ""
    if name in draft.read_from:
        return draft.read_from[name]
    source = str(draft.sources.get(name) or "")
    if source.startswith(FROM_SUBJECT) or source in (FROM_SINGLE, FROM_BOOKING):
        return "subject"
    if FROM_QUOTED in source or source.startswith(FROM_CHAIN) or "forward" in source:
        return "quoted inquiry"
    if source.startswith(FROM_BODY) or source.startswith(FROM_FINAL_DEST) or source == FROM_OUR_QUOTE:
        return "body"
    if source in (FROM_SENDER, FROM_RECIPIENT, FROM_DOMAIN_NAME, FROM_OFFICE_PREFIX, FROM_REPLY,
                  FROM_BELOW_OFFICE) or "domain" in source:
        return "sender"
    if source == FROM_MAIL:
        return "mail"
    if source.startswith("from the attachment"):
        return "attachment"
    return "other"


#: H1 (2026-10-02): the note a number the reader could not turn into one leaves.
NUMBER_UNREAD = "A number in this mail could not be read ({}) - kept as written, nothing filled in from it."


def _qty_from_body(draft, mail, subject, text=None, source=FROM_BODY) -> None:
    """_read_qty, but a number never breaks the reading of a mail (H1): a
    value the reader cannot turn into a number leaves the quantity as the
    mail wrote it - the body line stays in 'Quantity as received' for her -
    with a note and a line in the log, and the rest of the mail is read."""
    before = (dict(draft.values), dict(draft.sources), dict(draft.evidence),
              list(draft.cargo_lines), list(draft.cargo_stated))
    try:
        _read_qty(draft, mail, subject, text=text, source=source)
    except (ValueError, ArithmeticError) as exc:
        draft.values, draft.sources, draft.evidence = before[0], before[1], before[2]
        draft.cargo_lines, draft.cargo_stated = before[3], before[4]
        from . import logs
        logs.get("intake").warning("a number could not be read in mail %r (%s): %s",
                                   (getattr(mail, "subject", "") or "")[:80], source, exc)
        bad = str(exc).split(": ", 1)[-1]
        note = NUMBER_UNREAD.format(bad)
        if note not in draft.notes:
            draft.notes.append(note)
        # the line that number sits on, kept as written for her to read
        digits = re.sub(r"\D", "", bad)
        body = getattr(mail, "body_excerpt", "") if text is None else text
        line = next((l for l in parsing.body_lines(body) if digits and digits in re.sub(r"\D", "", l)), "")
        if line and "qty_raw" not in draft.values:
            draft.set("qty_raw", " ".join(line.split())[:200], "{} - a number not read, kept as written".format(source),
                      " ".join(line.split()))


def _read_qty(draft, mail, subject, text=None, source=FROM_BODY) -> None:
    """Body scan: how much cargo, when the subject does not say.

    `qty_raw` is filled in the 'MODE:value' convention so that 'Parse into
    the fields' understands it, with the body line quoted beside it. The mode
    is only suggested when the subject named none: the subject is the better
    evidence and this must not argue with it.
    """
    body = getattr(mail, "body_excerpt", "") if text is None else text
    # how '9.100 kg' reads: the subject and the body together say (13g)
    style = parsing.number_style((getattr(mail, "subject", "") or "") + "\n" + (body or ""))
    qty = parsing.parse_body_qty(body, style=style)
    if qty.mode != "FCL":
        labels = parsing.parse_cargo_labels(parsing.body_lines(body), style=style)
        if labels.found:
            evidence = "; ".join(labels.lines)
            draft.set("qty_raw", labels.qty_raw, source, evidence)
            draft.set("air_weight_kg", round(labels.chargeable_kg, 1),
                      "{} - {}".format(source, labels.formula), evidence)
            # v34: the labelled form is one cargo line; a chargeable weight
            # it states is the sender's figure, kept to compare with ours
            draft.cargo_lines, draft.cargo_stated = _lines_from_labels(labels, getattr(mail, "entry_id", None))
            return
        lines = parsing.body_lines(body)
        packing = parsing.parse_packing(lines)
        if packing.is_packing_list:
            if packing.chargeable_kg is not None:
                draft.set("qty_raw", packing.qty_raw, source, packing.evidence)
                check = packing.chargeable_check
                draft.set("air_weight_kg", round(packing.chargeable_kg, 1),
                          "{} - {}{}".format(source, packing.formula, " · " + check if check else ""),
                          packing.evidence)
                if check and "differs" in check:
                    draft.notes.append("Chargeable weight: {} - '{}'.".format(check, packing.stated_chargeable_text))
            else:
                draft.set("qty_raw", " / ".join(l for i in packing.items for l in i.lines),
                          source, packing.evidence)
                draft.notes.append(
                    "The quantity numbers do not add up to a weight ({}); the lines "
                    "are in 'Quantity as received' - pick the right ones."
                    .format(", ".join(packing.candidates)))
            # one cargo line per item the mail lists, with the text it came
            # from; each block's total a check on its lines (v34)
            blocks = [b for b in parsing.cargo_blocks(lines) if b.has_cargo]
            draft.cargo_lines, draft.cargo_stated = [], []
            for block in blocks:
                block_lines, stated = cargo.lines_from_block(block, getattr(mail, "entry_id", None))
                if stated is not None:
                    for line in block_lines:
                        line["stated_ref"] = len(draft.cargo_stated)
                    draft.cargo_stated.append(stated)
                draft.cargo_lines.extend(block_lines)
            _cbm_from_packing(draft, packing, blocks, source)
            if len(draft.cargo_lines) > 1:
                _totals_from_lines(draft)
            return
    if qty.conflicts:
        draft.notes.append(
            "The body states {} different quantities ({}); none pre-filled - "
            "they may be one crate each.".format(
                len(qty.conflicts),
                ", ".join(value for value, _ in qty.conflicts)))
        return
    if not qty.qty_raw:
        return
    if qty.ask:
        written, one, other = qty.ask
        draft.set("qty_raw", qty.qty_raw, source, qty.evidence)
        draft.notes.append(
            "'{}' reads as {} or as {}, and nothing in the mail settles which; "
            "nothing was filled in - type the weight or volume.".format(written, one, other))
        return
    if qty.reading_note:
        source = "{} - {}".format(source, qty.reading_note)
    # 13g: a weight alone never makes it Air when the mail says it goes by sea
    sea = parsing.subject_sea_word(subject) or parsing.sea_word(
        "\n".join([getattr(mail, "body_excerpt", "") or "", body or ""]))
    if qty.mode == "Air" and sea and not subject.service_mode:
        kilos = qty.weight_kg
        qty.qty_raw = "{} kg".format(qty.qty_raw.split(":", 1)[1])
        qty.mode = None
        draft.set("qty_raw", qty.qty_raw, source, qty.evidence)
        if kilos is not None and "gross_kg" not in draft.values:
            draft.set("gross_kg", kilos, source, qty.evidence)
        if not parsing.subject_sea_word(subject):
            draft.notes.append("The mail says '{}': a weight alone does not make it Air; "
                               "the mode is left for you to pick.".format(sea))
        return
    draft.set("qty_raw", qty.qty_raw, source, qty.evidence)
    if qty.mode and not subject.service_mode and "service_mode" not in draft.values:
        draft.set("service_mode", qty.mode, source, qty.evidence)
    if qty.mode == "FCL" and "container_type" not in draft.values:
        parsed = parsing.parse_qty(qty.qty_raw)
        draft.set("container_type", parsed.container_type, source, qty.evidence)
        draft.set("container_qty", parsed.container_qty, source, qty.evidence)


def _lines_from_labels(labels, mail_id) -> tuple:
    """([line], [stated]) out of a labelled form ('Pieces: 4 / Gross weight:
    240 kg / Volume: 2 cbm'): one line with the numbers as stated."""
    line = cargo.normalise({
        "label": "", "pieces": labels.pieces, "pack_word": labels.piece_word or None,
        "weight_value": labels.weight_kg, "weight_unit": "kg" if labels.weight_kg is not None else None,
        "weight_basis": "total",
        "cbm_value": labels.volume_cbm, "cbm_basis": "total",
        "source": "mail", "source_mail": mail_id,
        "source_text": "\n".join(l for l in labels.lines if not parsing._CARGO_CHARGEABLE.match(l)),
        "counted": 1, "evidence": labels.lines[0] if labels.lines else "", "deliver_to": "",
    })
    stated = []
    if labels.chargeable_stated:
        text = [l for l in labels.lines if parsing._CARGO_CHARGEABLE.match(l)]
        stated.append({"source_mail": mail_id, "source_text": "\n".join(text),
                       "chargeable_kg": labels.chargeable_kg, "chargeable_written": text[0] if text else None})
        line["stated_ref"] = 0
    return [line], stated


def _cbm_from_packing(draft, packing, blocks, source) -> None:
    """lcl_cbm off the packing list (2026-09-24): a cbm the
    mail states is kept, with a note when the dims say something else
    (more than 10 % apart); otherwise the dims' cbm, formula in the label.
    For every mode (the Colombo mail, 2026-09-24): the form's Volume (cbm)
    field is there for Air too; air_weight_kg is computed as before."""
    stated = [b for b in blocks if b.stated_cbm is not None]
    computed = packing.cbm
    if stated:
        total = sum(b.stated_cbm for b in stated)
        draft.set("lcl_cbm", round(total, 3), "{} - as stated".format(source),
                  "; ".join(b.stated_cbm_text for b in stated))
        if computed is not None and total > 0 and abs(computed - total) / total > cargo.CBM_TOLERANCE:
            draft.notes.append("stated {} cbm, dims give {} cbm".format(
                parsing._fmt2(total), parsing._fmt2(computed)))
    elif computed is not None:
        draft.set("lcl_cbm", round(computed, 2), "{} - {}".format(source, packing.cbm_formula),
                  packing.evidence)


def _totals_from_lines(draft) -> None:
    """Several consignments: the case's three quantity fields are the lines'
    totals (cargo.enquiry_fields), as the repository will write them."""
    fields = cargo.enquiry_fields(cargo.summarise(draft.cargo_lines, draft.values.get("service_mode")))
    label = "totals of the {} cargo lines".format(len(draft.cargo_lines))
    for name, value in fields.items():
        if value is None:
            draft.values.pop(name, None)
            draft.sources.pop(name, None)
            continue
        draft.values[name] = value
        draft.sources[name] = label
        draft.evidence[name] = " / ".join(l.get("evidence") or "" for l in draft.cargo_lines).strip(" /")


# --------------------------------------------- which way a mail of ours goes --
#: The mail is asking for a rate: the panel 'log who we asked' is for these.
_ASK_SUBJECT = re.compile(
    r"\b(rate|rates|quote|quotes|quotation|rfq|pricing|prices?|costs?|tariff|"
    r"cotizaci[o\u00f3]n|cotizar|tarifas?|precios?)\b", re.I)
_ASK_BODY = re.compile(
    r"(please|kindly|pls|could you|can you)\s+(quote|advise|provide|send|share|offer|give)\b"
    r"|\byour\s+(best\s+|competitive\s+|lowest\s+)?(rates?|quote|quotation|pricing|prices?|costs?|offer)\b"
    r"|\b(rates?|quote|quotation|pricing|costs?)\s+(for|from|on)\b"
    r"|\bneed\s+[^\n]{0,25}?\b(rates?|quote|quotation|pricing)\b"
    # 'would you be able to handle this move', 'can you offer', 'do you
    # handle': asking whether they do it at all is asking for their rate
    r"|\b(would|will|could|can)\s+you\s+(be\s+able\s+to\s+)?(offer|handle|provide|quote)\b"
    r"|\bdo\s+you\s+(offer|handle|provide)\b"
    # a shipment we need moved into their hands: 'pick up ... and deliver to
    # your facility'
    r"|\bpick\s*up\b[^\n]{0,120}?\bdeliver(?:y)?\s+to\s+your\s+(facility|warehouse|premises|yard|depot)\b"
    r"|\b(cotiza|coticen|cotizar|precio|tarifa)\w*", re.I)
#: We are giving a rate: it is in the mail or attached to it. 'quote below'
#: counts only as a noun ('our quote below', 'rates are as below', 'quoted
#: below'): 'please quote below door to port' is us telling somebody to quote
#: (2026-09-17).
_GIVE_PHRASE = re.compile(
    r"(please|pls|kindly)\s+(find|see)\s+(the\s+|our\s+|my\s+|attached\s+|below\s+|enclosed\s+)*"
    r"(rate\s+|freight\s+)?(quote|quotation|quotes|rates?|pricing|offer|proposal)\b"
    r"|\b(our|the|my|attached|revised|updated)\s+(rates?|quote|quotation|offer)\s+(is\s+|are\s+)?"
    r"(attached|enclosed|as\s+follows|as\s+below|below)\b"
    r"|\b(rates?|quote|quotation)\s+(is|are)\s+(attached|enclosed|below|as\s+follows|as\s+below)\b"
    r"|\b(rates?|quote|quotation|quotes|pricing|offer)\s+(attached|enclosed|as\s+follows)\b"
    r"|\bquoted\s+below\b"
    r"|\battached\s+(is\s+|are\s+|please\s+find\s+)?(the\s+|our\s+)?(rate\s+)?(quote|quotation|rates?)\b"
    r"|\b(our|the)\s+(rates?|quote|quotation|offer)\s+(is|are|for|as|would be)\b", re.I)
#: An imperative aimed at the reader: what follows is a request, never a
#: rate we are giving.
_IMPERATIVE_QUOTE = re.compile(r"\b(please|pls|kindly|could\s+you|can\s+you)\s+(quote|advise|provide|offer)\b", re.I)
#: The customer's number, not ours: 'target rate USD 1,500' is a request.
_TARGET_RATE = re.compile(r"\b(target|budget|max(?:imum)?)\s+(rate|price|cost|budget|level)\b", re.I)
#: The subject says outright that a rate is wanted.
_ASK_SUBJECT_EXPLICIT = re.compile(
    r"rate\s+request|rfq|quote\s+request|request\s+for\s+quot|cotizaci[oó]n", re.I)
#: Charges with money next to them: a quote written into the mail.
_CHARGES_AMOUNT = re.compile(
    r"(charges?|rates?|freight|total|all[- ]in|o/f|thc|per\s+(kg|cbm|w/m|container|cntr|shipment))"
    r"[^\n]{0,60}?(usd|us\$|\$|eur|\u20ac|gbp|\u00a3)\s?\d"
    r"|(usd|us\$|\$|eur|\u20ac|gbp|\u00a3)\s?\d[\d,.]*[^\n]{0,60}?"
    r"(charges?|rates?|freight|total|all[- ]in|per\s+(kg|cbm|w/m|container|cntr|shipment))", re.I)


#: The one _ASK_BODY alternative that is also how an amount of our own is
#: described ('rate for', 'cost for', 'rate on', 'rates from').
_RATE_PREPOSITION = re.compile(r"(rates?|quote|quotation|pricing|costs?)\s+(for|from|on)", re.I)
#: A currency amount anywhere in the new part ('USD 3,850', '$2.10/k',
#: 'usd6400/40'hc', 'RMB 220'): the amount, whatever label sits by it.
_CURRENCY_AMOUNT = re.compile(
    r"(?:usd|us\$|\$|eur|€|gbp|£|rmb|cny)\s?\d|\b\d[\d,.]*\s?(?:usd|eur)\b", re.I)
#: A sentence that points the reader at the amounts the writer states:
#: 'please see below the new figures for your review', 'kindly find below
#: cost for sea and air', 'below rates, for consideration', "here's an
#: alternative", 'we are pleased to offer the following', 'rates stay the
#: same' (2026-09-25, measured on the archive: 60-odd of our quotes were
#: read as asking because 'rates for' / 'cost for' / 'rate from' in that
#: very sentence is also how a request is worded).
_POINTS_TO_OURS = re.compile(
    r"\b(?:see|find|note|refer\s+to)\s+(?:the\s+)?below\b"
    r"|\b(?:see|find)\s+(?:attached|enclosed)\b"
    # 'please find our new rates for 2 x 20GP', 'kindly see the lower
    # cost' - said to the reader ('if you can find the space ...' is us
    # asking, 2026-08-28)
    r"|\b(?:please|pls|kindly)\s+(?:see|find)\s+(?:(?:our|the|my|updated|revised|new|best|lower|all[- ]in)\s+){1,4}"
    r"(?:rates?|costs?|charges|quotation|quotes?|options?|offer|pricing)\b"
    r"|\bbelow\s+(?:is|are)\b"
    r"|\bbelow\s+(?:our\s+|the\s+)?(?:best\s+|updated\s+|revised\s+|all[- ]in\s+|promotion\s+)?"
    r"(?:rates?|costs?|charges|quotation|quotes?|options?|offer)\b"
    r"|\bhere(?:\s+is|\s+are|'s|’s)\b"
    r"|\bfor\s+(?:your\s+)?(?:reference|ref|review|consideration)\b"
    r"|\bpleased\s+to\s+(?:share|offer)\b"
    r"|\balternative\s+option\b"
    r"|\brates?\s+(?:remain|stay)s?\b", re.I)
#: A request aimed at the reader's own rate: an ask still needs one of
#: these when the writer states amounts of their own ('please kindly
#: provide your local charges', 'please quote', 'can you advise your
#: lowest rate'); 'please send us the HS code' or 'let me know' is not.
_ASKS_FOR_THEIR_RATE = re.compile(
    r"\bplease\s+quote\b"
    r"|\b(?:please|pls|kindly|could\s+you|can\s+you|would\s+you)\s+(?:kindly\s+)?"
    r"(?:quote|offer|provide|advise|send|share|give)\b[^\n.?]{0,40}?"
    r"\b(?:rates?|quot(?:e|ation)s?|costs?|charges|pricing|prices?|offer)\b"
    # the verb further on in the same request: 'kindly reach out to the
    # consignee ... and send the pickup details, rates and ...' ('offer'
    # left out: 'please see below our offer and the rate options' is ours)
    r"|\b(?:please|pls|kindly)\b[^\n.?]{0,80}?\b(?:quote|provide|advise|send)\b[^\n.?]{0,40}?"
    r"\b(?:rates?|quot(?:e|ation)s?|costs?|charges|pricing|prices?)\b", re.I)


#: F1 (2026-10-02): a currency amount labelled as the value of the goods
#: ('FOB:   USD 1.3 millions (total)', 'invoice value USD 48,000') on a line
#: with no charge word is the cargo's worth, not a price of ours - it counts
#: neither as money nor as a currency amount, the way a target rate does not
#: (OPP-1511: the agent's FOB value made the request read as a quote).
_GOODS_VALUE = re.compile(
    r"\b(?:fob|cif|cfr|invoice\s+value|cargo\s+value|goods\s+value|commercial\s+value|declared\s+value"
    r"|insured\s+value|value\s+of\s+(?:the\s+)?goods|valor(?:\s+fob)?|valor\s+da\s+mercadoria"
    r"|valor\s+de\s+la\s+mercanc[ií]a)\b", re.I)
#: The words that make a line a charge after all: 'FOB charges: USD 150',
#: 'ocean freight USD 2,500/40HC'.
_CHARGE_WORD = re.compile(
    r"\b(?:freight|charges?|rates?|fees?|thc|all[- ]in)\b|\bo/f\b"
    r"|\bper\s+(?:kg|cbm|w/m|container|cntr|shipment)\b"
    r"|/\s*(?:\d{2}\s*'?\s*(?:hc|hq|gp|dv|fr|ot|rf|nor|std)\b|kg\b|cbm\b|w/?m\b|cntr\b|container\b|teu\b|bl\b)", re.I)
#: The amount itself, with a written-out size ('1.3 millions', '48k').
_AMOUNT_WORDS = re.compile(
    r"(?:usd|us\$|\$|eur|€|gbp|£|rmb|cny|brl|r\$)\s?\d[\d,.]*(?:\s*(?:millions?|mil|mm|k)\b)?"
    r"|\b\d[\d,.]*\s?(?:usd|eur)\b", re.I)


#: D7a (v50): the amount on a goods-value line, with the currency written
#: before or after it ('USD 12,500', '12.500 EUR', 'R$ 48 mil')
_VALUE_AMOUNT = re.compile(
    r"(?<![\w$])(?P<cur>r\$|us\$|usd|eur|gbp|rmb|cny|brl|\$|€|£)\s?(?P<n>\d[\d,.]*\d|\d)(?:\s*(?P<mult>millions?|mil|mm|k)\b)?"
    r"|(?<![\w.,])(?P<n2>\d[\d,.]*\d|\d)(?:\s*(?P<mult2>millions?|mil|mm|k)\b)?\s*(?P<cur2>usd|eur|gbp|rmb|cny|brl)\b",
    re.I)


def goods_value_in(lines) -> tuple:
    """D7a: (amount, currency, line) - the value of the goods, read where F1
    reads it (_GOODS_VALUE: cargo, goods, invoice, commercial, declared, FOB
    or CIF value), never on a charge line (_CHARGE_WORD), and only with a
    currency written; (None, None, '') when none."""
    from . import cargo
    for line in lines or []:
        line = " ".join(str(line or "").split())
        words = _GOODS_VALUE.search(line)
        if not words or _CHARGE_WORD.search(line):
            continue
        m = _VALUE_AMOUNT.search(line, words.start()) or _VALUE_AMOUNT.search(line)
        if m is None:
            continue
        amount = cargo.amount_of(m.group("n") or m.group("n2"))
        currency = cargo.currency_of(m.group("cur") or m.group("cur2"))
        if amount is None or currency is None:
            continue
        mult = (m.group("mult") or m.group("mult2") or "").lower()
        if mult:
            amount *= cargo._MULTIPLIERS[mult]
        return round(amount, 2), currency, line
    return None, None, ""


def _hs_and_value(draft, mail, own_lines, chain) -> None:
    """D7a (v50): the HS code and the value of the goods for the cargo lines.
    One line takes what the mail says (its own words, and a forward's quoted
    request); several lines each take only what their own text
    (source_text) says - the rest is left for her, never guessed. With no
    line yet, the register page's first line takes the mail's
    (draft.line_extra)."""
    texts = list(own_lines)
    if forward_kind(mail):
        texts += [l for q in chain for l in parsing.body_lines(q.text)]

    def found(lines):
        codes, _where = parsing.hs_codes_in(lines)
        amount, currency, _line = goods_value_in(lines)
        out = {}
        if codes:
            out["hs_code"] = codes
        if amount is not None:
            out["cargo_value"], out["cargo_value_cur"] = amount, currency
        return out

    lines = draft.cargo_lines
    if len(lines) > 1:
        for line in lines:
            for key, value in found((line.get("source_text") or "").split("\n")).items():
                if line.get(key) in (None, ""):
                    line[key] = value
        return
    whole = found(texts)
    if lines:
        for key, value in whole.items():
            if lines[0].get(key) in (None, ""):
                lines[0][key] = value
    else:
        draft.line_extra = whole


def _without_goods_value(top: str) -> str:
    """The text with the amounts of goods-value lines taken out (F1)."""
    out = []
    for line in (top or "").split("\n"):
        if _GOODS_VALUE.search(line) and not _CHARGE_WORD.search(line):
            line = _AMOUNT_WORDS.sub(" ", line)
        out.append(line)
    return "\n".join(out)


def mail_direction(subject: str, top: str) -> str:
    """'quoting' when the writer's own words give a rate (a quote phrase,
    or charges with amounts), 'asking' when they ask for one (the subject
    or the body), '' when neither. Only the new part of the mail counts:
    the quoted chain underneath says what the other side wrote."""
    return mail_direction_why(subject, top)[0]


def mail_direction_why(subject: str, top: str) -> tuple:
    """(mail_direction, the words that made it quoting - '' otherwise),
    for the message a refused Log vendor request gives (F1)."""
    top = top or ""
    # the goods' value is not a price of ours (F1): the amount checks read
    # the text without it, everything else reads the text as written
    priced = _without_goods_value(top)
    # Our own amount above the quoted chain, and a sentence pointing the
    # reader at it: that is a rate being given, whatever 'rates for' or
    # 'please send us the HS code' sits in the same text - unless the
    # mail also asks for the reader's rate. A target or budget is theirs.
    amount = _CURRENCY_AMOUNT.search(priced)
    pointing = _POINTS_TO_OURS.search(top)
    if amount and not _TARGET_RATE.search(top) and pointing and not _ASKS_FOR_THEIR_RATE.search(top):
        return "quoting", _words(priced, amount, pointing)
    # Money in the text, unless it is the customer's target or budget.
    charges = _CHARGES_AMOUNT.search(priced)
    money = bool(charges) and not _TARGET_RATE.search(top)
    # An outright request - 'please quote ...' in the text, or RATE REQUEST
    # / RFQ in the subject of a mail that starts the thread (no RE:) - with
    # no money in the text is asking, whatever else the words look like
    # ('please quote below door to port'). A 'RE: RATE REQUEST' with 'rates
    # attached' is still our answer.
    imperative = bool(_IMPERATIVE_QUOTE.search(top))
    subject_ask = bool(_ASK_SUBJECT_EXPLICIT.search(subject or "")) and \
        not parsing.has_reply_prefix(subject)
    if imperative and not money:
        return "asking", ""
    give = _GIVE_PHRASE.search(top)
    if give:
        # 'QUOTE REQUEST // ENAEX' with 'rates attached' under it: the
        # subject is theirs, the rates are ours
        return "quoting", _words(top, give)
    if subject_ask and not money:
        return "asking", ""
    ask_body = _ASK_BODY.search(top)
    if ask_body and _RATE_PREPOSITION.fullmatch(ask_body.group(0)) \
            and _CURRENCY_AMOUNT.search(priced) and not _TARGET_RATE.search(top) \
            and not _ASKS_FOR_THEIR_RATE.search(top):
        # 'rate for' / 'rate on' next to an amount of the writer's own is
        # describing that amount ('spot rate $0.85/kg ... lowest rate on
        # market', OPP-1471): only a request aimed at the reader's rate
        # makes it asking (2026-09-25). The other asking phrases still count.
        ask_body = next((m for m in _ASK_BODY.finditer(top)
                         if not _RATE_PREPOSITION.fullmatch(m.group(0))), None)
    if money and (not ask_body or imperative or subject_ask):
        # charges under 'please quote below': a quote with a question in it
        return "quoting", _words(priced, charges)
    if ask_body or _ASK_SUBJECT.search(subject or ""):
        return "asking", ""
    return "", ""


def _words(text, *found) -> str:
    """'"USD 1.3 millions (total)"' - each match with the rest of its
    line, quoted, cut to a short line."""
    out = []
    for match in sorted((m for m in found if m), key=lambda m: m.start()):
        end = text.find("\n", match.end())
        words = " ".join(text[match.start():end if end >= 0 else len(text)].split())
        out.append('"{}"'.format(words if len(words) <= 60 else words[:57] + "..."))
    return ", ".join(out)


def quoted_asks_us(quoted) -> bool:
    """True when a mail quoted under ours is itself asking for a rate: an
    asking phrase in its text, or RATE REQUEST / QUOTE / RFQ / COTIZACION
    in its subject. A sales call ('anything BQR can assist you with'), an
    introduction or a hello is not an enquiry, and neither is a mail that
    hands us a rate."""
    return mail_direction(quoted.subject, quoted.text) == "asking"


def _enquiry_domains(quoted, direction, internal_domains) -> set:
    """The domains of the outside people whose quoted mail makes them the
    one asking. When our mail gives a rate, whoever we quote under it asked
    for it; otherwise only a quoted mail that asks for a rate
    counts, so the sender of a sales visit or a greeting we now write back
    to with a question stays a vendor candidate."""
    out = set()
    for mail in quoted:
        domain = matching.domain_of(mail.address)
        if not domain or domain in internal_domains or mail.office:
            continue
        if direction == "quoting" or quoted_asks_us(mail):
            out.add(domain)
    return out


def _internal_domains(conn, mail) -> set:
    """Our own mail domains: the setting, plus whatever we are sending from."""
    domains = {str(d).strip().lower()
               for d in db.get_setting_list(conn, "internal_domains")
               if str(d).strip()}
    if getattr(mail, "sender_internal", False):
        own = matching.domain_of(getattr(mail, "sender_address", ""))
        if own:
            domains.add(own)
    return domains


def _office_names(names) -> dict:
    """{code: [name, ...]} for the Our offices among display names
    like 'JJH-DANIEL YIP': the office prefix (parsing._OFFICE_PREFIX), not
    our own office (HOME_OFFICE) and not a group alias ('#JJC-MFN')."""
    out = {}
    for name in names or []:
        text = (name or "").strip()
        if not text or text.startswith("#"):
            continue
        found = parsing._OFFICE_PREFIX.match(text)
        if not found or found.group(1) == HOME_OFFICE:
            continue
        out.setdefault(found.group(1), []).append(text)
    return out


def _office_recipients(mail) -> dict:
    """{code: [name, ...]} - the our other offices on the To line
    (not Cc). Asking another office for the buying rate is a vendor
    request like any other (Rate used has 'Office rate' for it),
    but their addresses are on our own domain, so _outside_recipients
    never sees them; the office code in the display name does (OPP-1294,
    2026-09-25)."""
    return _office_names(getattr(mail, "to_names", None) or [])


def handling_office(mail) -> str:
    """The office that handles an enquiry sent to it with the home office
    only copied (v31, 2026-09-25): a mail from outside whose To line names
    another office by the code in a display name ('JJV-KEVIN ZHAO', the
    _office_recipients reading; # aliases do not count) and no home-office
    person. '' when a home-office person is in To, when no office is, or when we wrote it."""
    if getattr(mail, "sender_internal", False):
        return ""
    return office_from_to_names(getattr(mail, "to_names", None) or [])


def office_from_to_names(names) -> str:
    """Batch 10's rule on a list of To display names: the first other
    office's code, '' when a JJC person is among them or no office is."""
    first = ""
    for name in names:
        text = (name or "").strip()
        if not text or text.startswith("#"):
            continue
        found = parsing._OFFICE_PREFIX.match(text)
        if not found:
            continue
        if found.group(1) == HOME_OFFICE:
            return ""
        first = first or found.group(1)
    return first


def _office_copied(mail) -> list:
    """The office codes on the Cc line, named in a note and not offered."""
    return sorted(_office_names(getattr(mail, "cc_names", None) or []))


def _outside_recipients(mail, internal_domains) -> dict:
    """{domain: [address, ...]} for everyone on To/CC who is not one of us."""
    out = {}
    for address in getattr(mail, "recipients", []):
        domain = matching.domain_of(address)
        if not domain or domain in internal_domains or                 domain in matching.FREE_MAIL_DOMAINS:
            continue
        out.setdefault(domain, []).append(address)
    return out


def _domain_label(domain: str) -> str:
    """'us.harborline.example' and 'gsc.harborline.example' are one company, so a short
    first label in front of a longer name is skipped: both give 'harborline'."""
    labels = [part for part in (domain or "").lower().split(".") if part]
    if len(labels) >= 3 and (len(labels[0]) <= 3 or labels[0] in ("www", "mail")):
        labels = labels[1:]
    return labels[0] if labels else ""


def suggested_party_key(domain: str) -> str:
    """'bluefin-usa.example' -> 'Bluefin-Usa': the key import-tracker gives a vendor
    it has to invent (kept as it was, so re-imports find their own keys)."""
    return "-".join(part.capitalize() for part in _domain_label(domain).split("-") if part)


_STEM_SHAPE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_JOINED_SHAPE = re.compile(r"^[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+$")


def looks_like_domain_stem(key: str) -> bool:
    """'cargovia-qatar', 'zenith_cargo', 'tolvex', 'Cosco-Usa': a key that
    is still the domain's own label (all lower case, or words joined with
    - or _) rather than a name somebody typed."""
    key = (key or "").strip()
    return bool(_STEM_SHAPE.match(key) or _JOINED_SHAPE.match(key))


def name_from_stem(stem: str) -> str:
    """'cargovia-qatar' -> 'Cargovia Qatar', 'zenith_cargo' -> 'Zenith
    Cargo'. One word stays one word ('zenithcargo' -> 'Zenithcargo'): the
    domain-to-party matching (zenithcargo ~ 'Zenith Cargo') is untouched."""
    words = [w for w in re.split(r"[-_]+", (stem or "").strip()) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)


#: Country, state and region words a company hangs on the end of its domain
#: ('bqr-usa', 'bqrflorida', 'cargovia-qatar'). Joined on without a dash
#: only the ones of four letters or more are recognised ('bqrflorida' ->
#: bqr; 'focus' is not 'foc' + us).
_REGION_SUFFIXES = {
    "usa", "us", "america", "americas", "latam", "na",
    "florida", "miami", "texas", "houston", "chicago", "california", "la", "ny", "nyc",
    "canada", "ca", "mexico", "mx", "chile", "cl", "peru", "pe", "colombia", "co",
    "argentina", "ar", "brasil", "brazil", "br", "panama", "pa", "ecuador", "ec",
    "uruguay", "uy", "paraguay", "py", "bolivia", "bo", "venezuela", "ve",
    "costarica", "cr", "guatemala", "gt", "honduras", "hn", "salvador", "sv",
    "nicaragua", "ni", "dominicana", "do", "puertorico", "pr", "cuba",
    "uk", "eu", "europe", "asia", "qatar", "qa", "dubai", "uae", "india",
    "china", "cn", "hk", "hongkong", "taiwan", "tw", "japan", "jp", "korea", "kr",
    "singapore", "sg", "australia", "au", "global", "intl", "international",
}


def _strip_region(label: str) -> str:
    """'bqr-usa' -> 'bqr', 'bqrflorida' -> 'bqr', 'cargovia-qatar' ->
    'cargovia'; a label with no region on the end comes back as it is."""
    parts = [p for p in re.split(r"[-_]+", label or "") if p]
    if len(parts) > 1:
        if parts[-1] in _REGION_SUFFIXES:
            return "-".join(parts[:-1])
        return label
    for suffix in sorted(_REGION_SUFFIXES, key=len, reverse=True):
        if len(suffix) >= 4 and label.endswith(suffix) and len(label) > len(suffix):
            return label[:-len(suffix)]
    return label


def suggested_party_name(domain: str) -> str:
    """What the New party box is filled with: the domain's label as words.
    'cargovia-qatar.example' -> 'Cargovia Qatar'. A short initialism keeps
    its capitals once a country or region on the end is dropped: 'bqr-usa'
    and 'bqrflorida' both give 'BQR', the same company; a longer name keeps
    the region because it tells the branches apart."""
    label = _domain_label(domain)
    stem = _strip_region(label)
    if stem and len(stem) <= 3 and stem.isalpha():
        return stem.upper()
    return name_from_stem(label)


def _requester_note(draft, domain) -> str:
    who = draft.requester_key or domain
    return "Reply to the requester ({}), not a vendor request".format(who)


def _reply_draft(conn, draft, domain, mail=None, office=None) -> None:
    """Victor answering the agent who asked: they are the requester.

    The subject still says RE: and their own mail is quoted below (or they
    are the case's requester), so this is their enquiry coming back around,
    not us shopping for a rate. Nobody here is a vendor. When the
    answer carries the rate the case is Quoted on the day the mail went out;
    an answer that gives no rate yet leaves it New (Sourcing is retired,
    2026-09-24: a case is New until a quote goes out). RG1: `office`, the
    quoted mail of the office that asked, instead of a domain.
    """
    if office is not None:
        _requester_from_office(conn, draft, office.office, office.name, FROM_REPLY, "the quoted mail")
        domain = draft.requester_key or branding.office_key(office.office, conn)
        draft.notes.append("A reply to {}, whose inquiry is quoted below: they are the ones asking, "
                           "so nothing here is offered as a vendor.".format(domain))
    elif _requester_from_domain(conn, draft, domain, FROM_REPLY):
        draft.notes.append(
            "A reply to {}, whose mail is quoted below: they are the ones "
            "asking, so nothing here is offered as a vendor.".format(
                draft.requester_key))
    else:
        draft.notes.append(
            "This answers the mail quoted below, from {}: they are the ones "
            "asking.".format(domain))
    draft.vendor_note = _requester_note(draft, domain)
    if draft.direction == "asking":
        draft.set("status", "New", FROM_REPLY)
        draft.notes.append("The reply asks for something rather than giving a "
                           "rate, so the case stays New.")
    else:
        draft.set("status", "Quoted", FROM_REPLY)
        sent = getattr(mail, "received_on", "") if mail is not None else ""
        if sent:
            draft.set("status_at", sent, FROM_REPLY)


FROM_QUOTING = "we are giving rates in this mail"


def _quoting_draft(conn, draft, mail, original) -> None:
    """A mail of ours that hands out a rate to somebody outside who did not
    write the mail quoted under it: still our quote, so the case is Quoted
    on the day it went out. Nobody on it is a vendor we asked."""
    if original is not None:
        _requester_from_quoted(conn, draft, original)
    if "requester_party_id" not in draft.values and not draft.new_party:
        placeholder = conn.execute(
            "SELECT id, key FROM party WHERE placeholder = 1 "
            "AND lower(trim(key)) = ?", (PLACEHOLDER_SEARCHING.lower(),)).fetchone()
        if placeholder is not None:
            draft.set("requester_party_id", placeholder["id"], FROM_QUOTING)
            draft.requester_key = placeholder["key"]
    draft.set("status", "Quoted", FROM_QUOTING)
    if getattr(mail, "received_on", ""):
        draft.set("status_at", mail.received_on, FROM_QUOTING)
    draft.notes.append(
        "We are giving rates in this mail, so nobody on it is offered as a "
        "vendor; the case is Quoted on {}.".format(mail.received_on or "the day it went out"))


#: The pseudo-domain of an office vendor on the registration form: the
#: form keys its vendor rows by domain, and an office has none of its own.
OFFICE_DOMAIN = "office:"


def office_party(conn, code):
    """The Office party '<company_short> <CODE>' (case-insensitive, not
    merged away), or None."""
    return conn.execute(
        "SELECT id, key FROM party WHERE type = 'Office' AND merged_into IS NULL "
        "AND lower(trim(key)) = ? ORDER BY id LIMIT 1",
        (branding.office_key(code, conn).lower(),)).fetchone()


def _outbound_draft(conn, draft, vendor_addresses, quoted, asking=True, offices=None, copied=()) -> None:
    """A mail we sent out: register the case now, name the requester later.

    Victor asks a carrier before anybody has written to us, and Iris
    registers the case from that mail. Nobody in the mail is the requester, so
    it is parked on '(TBC)' - which is what tracking rule R5 chases - and the
    people we wrote to are offered as vendor requests - but only
    when the mail asks for a rate; a booking or a document going out offers
    nobody.
    """
    placeholder = conn.execute(
        "SELECT id, key FROM party WHERE placeholder = 1 "
        "AND lower(trim(key)) = ?",
        (PLACEHOLDER_SEARCHING.lower(),)).fetchone()
    if placeholder is not None:
        draft.set("requester_party_id", placeholder["id"], FROM_OUTBOUND)
        draft.requester_key = placeholder["key"]
    else:
        draft.notes.append(
            "This mail went out from us, so the requester is not in it, and "
            "there is no '{}' party on file to park it on yet."
            .format(PLACEHOLDER_SEARCHING))
    # Asking a vendor is not a quote: the case stays New (Sourcing retired).
    draft.set("status", "New", FROM_OUTBOUND)
    if not asking:
        draft.notes.append(
            "We sent this one, but nothing in it asks for a rate (no RATE "
            "REQUEST / QUOTE in the subject, no 'please quote' in the text), "
            "so nobody on it is offered as a vendor.")
        return
    draft.notes.append(
        "We sent this one. The requester is '{}' until you know who it is for; "
        "the people we wrote to are offered below as vendors asked."
        .format(PLACEHOLDER_SEARCHING))

    quoted_domains = {matching.domain_of(address) for address in quoted}
    for domain, addresses in vendor_addresses.items():
        found = matching.by_domain(conn, domain)
        in_quote = domain in quoted_domains
        draft.vendors.append({
            "domain": domain,
            "addresses": addresses,
            "party_id": found[0].party_id if found else None,
            "key": found[0].key if found else suggested_party_name(domain),
            "known": bool(found),
            # Somebody who also wrote earlier in this thread may be the one
            # asking rather than somebody we asked, so they start unticked.
            "checked": not in_quote,
            "note": ("wrote the mail quoted below - if they are the ones "
                     "asking, set them as the requester instead"
                     if in_quote else ""),
        })
    # our other offices we wrote to, one vendor each (2026-09-25): the
    # party '<company_short> <CODE>' when it is on file, else that key and
    # the vendors route creates it as an Office
    for code in sorted(offices or {}):
        party = office_party(conn, code)
        draft.vendors.append({
            "domain": OFFICE_DOMAIN + code,
            "addresses": list(offices[code]),
            "party_id": party["id"] if party else None,
            "key": party["key"] if party else branding.office_key(code, conn),
            "known": bool(party),
            "checked": True,
            "office": code,
            "note": "" if party else "office not on file yet - logging it creates the party as an Office",
        })
    if copied:
        draft.notes.append("{} copied, not offered".format(", ".join(copied)))


def _vendor_thread_draft(conn, draft, mail, domain, since, vendor_addresses, force_asking) -> None:
    """D1 (1b): a mail in a thread we started with a vendor. The case, if
    she registers one, is ours on '(TBC)' like any request we sent; the
    vendor is offered on our own mails (and on theirs with Log anyway),
    asked on the date of our first mail."""
    found = matching.by_domain(conn, domain)
    key = found[0].key if found else suggested_party_name(domain)
    draft.vendor_thread = {"domain": domain, "asked_on": since, "key": key}
    draft.outbound = True
    draft.direction = "asking"
    placeholder = conn.execute("SELECT id, key FROM party WHERE placeholder = 1 AND lower(trim(key)) = ?",
                               (PLACEHOLDER_SEARCHING.lower(),)).fetchone()
    if placeholder is not None:
        draft.set("requester_party_id", placeholder["id"], FROM_OUTBOUND)
        draft.requester_key = placeholder["key"]
    draft.set("status", "New", FROM_OUTBOUND)
    when = dates.month_day(since) if since else "earlier"
    draft.forward_line = "Our own request to {} ({}), first sent {}: a vendor thread, not an inquiry".format(
        key, domain, when)
    draft.forward_level = "check"
    draft.notes.append(
        "We wrote to {} first in this thread ({}): our later mails are follow-ups of that request and "
        "theirs are its replies. The requester is '{}' until you know who it is for.".format(
            key, when, PLACEHOLDER_SEARCHING))
    if mail.sender_internal or force_asking:
        draft.vendors.append({
            "domain": domain,
            "addresses": list(vendor_addresses.get(domain) or [mail.sender_address]),
            "party_id": found[0].party_id if found else None,
            "key": key, "known": bool(found), "checked": True,
            "note": "we wrote to them first in this thread, {}".format(when),
        })


def _lanes_from_body(draft, mail, known, location_country) -> None:
    """Body scan: the lane is often stated only in the mail itself.

    The subject wins whenever it names a lane; this runs only when it did
    not. Whatever comes back is a suggestion like any other pre-fill, and it
    carries the line it was read from so she can see what it was based on.
    """
    body = parsing.parse_body_lanes(getattr(mail, "body_excerpt", ""),
                                    known_locations=known)
    if not (body.origins or body.dests or body.conflicts):
        return

    origin_line = body.evidence.get("origin", "")
    dest_line = body.evidence.get("dest", "")
    for origin, dest in body.lanes:
        draft.lanes.append({
            "origin": origin.upper(),
            "dest": dest.upper(),
            "origin_known": origin.upper() in known,
            "dest_known": dest.upper() in known,
            "origin_country": location_country.get(origin.upper()),
            "dest_country": location_country.get(dest.upper()),
            "primary": True,          # one reading of one mail: all of it counts
        })
    if body.dest_alternatives:
        _fold_alternatives(draft, [body.dests])         # batch 23: 'X or Y' is one route
    for side in ("origin", "dest"):
        if body.tabled.get(side):
            draft.read_from[side + "_loc"] = "table"      # RG2a (layer 1)
    if draft.lanes:
        first = draft.lanes[0]
        draft.set("origin_loc", first["origin"], FROM_BODY, origin_line)
        draft.set("dest_loc", first["dest"], FROM_BODY, dest_line)
        draft.set("origin_country", first["origin_country"], FROM_BODY)
        draft.set("dest_country", first["dest_country"], FROM_BODY)
        if len(draft.lanes) > 1:
            draft.notes.append(
                "{} lanes read from the body; saving creates one case per "
                "ticked lane.".format(len(draft.lanes)))
    else:
        # One side only. Half a lane is still worth having in the form.
        if body.origins:
            place = body.origins[0].upper()
            draft.set("origin_loc", place, FROM_BODY, origin_line)
            draft.set("origin_country", location_country.get(place), FROM_BODY)
        if body.dests:
            place = body.dests[0].upper()
            draft.set("dest_loc", place, FROM_BODY, dest_line)
            draft.set("dest_country", location_country.get(place), FROM_BODY)
            if body.dest_alternatives and draft.multi_places:
                # batch 23: 'POD: San Antonio or Valparaiso', no origin given
                draft.dest_more = [d.upper() for d in body.dests[1:4] if d.upper() != place]

    draft.body_conflicts = body.conflicts
    for side, readings in body.conflicts.items():
        draft.notes.append(
            "The body gives {} different {}s ({}); none pre-filled - pick one."
            .format(len(readings), side,
                    ", ".join(place for place, _ in readings)))

    seen = {place.upper() for place in draft.unknown_locations}
    for place in ([p for p in body.origins] + [p for p in body.dests] +
                  [p for readings in body.conflicts.values()
                   for p, _ in readings]):
        key = place.upper()
        if key not in known and key not in seen:
            seen.add(key)
            draft.unknown_locations.append(key)


def describe(draft: Draft) -> list:
    """The plain text `outlook-selftest` prints."""
    mail = draft.mail
    lines = [
        "--- as Outlook gave it ---",
        "  subject        : {}".format(mail.subject),
        "  received       : {}  ({})".format(mail.received_at or "?",
                                             mail.received_on or "no date"),
        "  sender         : {} <{}>{}".format(
            mail.sender_name or "?", mail.sender_address or "no SMTP address",
            "  [internal Exchange]" if mail.sender_internal else ""),
        "  to             : {}".format(", ".join(mail.to_addresses) or "-"),
        "  cc             : {}".format(", ".join(mail.cc_addresses) or "-"),
        "  entry id       : {}".format(mail.entry_id or "-"),
        "  conversation   : {}".format(mail.conversation_id or "-"),
        "  body           : {} characters read".format(len(mail.body_excerpt)),
        "",
        "--- subject parsing ---",
        "  clean subject  : {}".format(draft.subject.clean),
        "  thread key     : {}".format(draft.subject.thread_key),
        "  segments       : {}".format(" | ".join(draft.subject.segments)),
        "  lanes          : {}".format(
            ", ".join("{} - {}{}".format(
                lane["origin"], " or ".join([lane["dest"]] + list(lane.get("dest_more") or [])),
                "" if lane["origin_known"] and lane["dest_known"] else "  (new location)")
                for lane in draft.lanes) or "-"),
        "  mode           : {}".format(
            ", ".join("{}{}".format(k, "" if m is None else " -> " + m)
                      for k, m in draft.subject.modes) or "-"),
        "  incoterm       : {}".format(", ".join(draft.subject.incoterms) or "-"),
        "  end customer   : {}".format(", ".join(draft.subject.end_customers) or "-"),
        "  containers     : {}".format(
            ", ".join("{}x {}".format(q or "?", t)
                      for q, t in draft.subject.containers) or "-"),
        "  special        : {}".format(", ".join(draft.special_tags) or "-"),
        "  references     : {}".format(", ".join(draft.references) or "-"),
        "  name candidates: {}".format(", ".join(draft.subject.names) or "-"),
        "",
        "--- party ---",
    ]
    if draft.party_candidates:
        for match in draft.party_candidates:
            lines.append("  {:<22} {:<18} {}".format(
                match.key, match.how, match.detail))
    elif draft.requester_key or draft.new_party:
        lines.append("  no domain or name match on this mail - see below")
    else:
        lines.append("  no match - the form will ask for '+ New'")

    if draft.quoted:
        lines.extend(["", "--- quoted mails (nearest first) ---"])
        for quoted in draft.quoted:
            lines.append("  {:<28} {:<17} {}".format(
                (quoted.name or quoted.address or "?")[:28], quoted.sent_at or "(no date)",
                (quoted.subject or "")[:60]))
    if draft.new_party:
        new = draft.new_party
        lines.append("  + New party suggested: {} ({}, {}){}".format(
            new["key"] or "?", new["type"], new.get("country", "?"),
            " domain " + new["domain"] if new.get("domain") else ""))
        for name, why in new.get("sources", {}).items():
            lines.append("      {:<8} {}".format(name, why))
        for similar in new.get("similar", []):
            lines.append("      similar: {} ({})".format(similar["key"], similar["detail"]))
    if draft.requester_key:
        lines.append("  requester: {}{}".format(
            draft.requester_key,
            "  (domain {} remembered on save)".format(draft.learn_domain)
            if draft.learn_domain else ""))

    if draft.cargo_lines:
        lines.extend(["", "--- cargo lines (one per consignment) ---"])
        for row in cargo.summarise(draft.cargo_lines, draft.values.get("service_mode"))["lines"]:
            lines.append("  {:<12} {}{}".format(
                (row.get("label") or "-")[:12], row["text"],
                "  -> " + row["deliver_to"][:50] if row.get("deliver_to") else ""))
    lines.extend(["", "--- what the form would pre-fill ---"])
    for name in ("received_on", "requester_party_id", "end_customer", "status",
                 "service_mode", "qty_raw", "air_weight_kg", "lcl_cbm", "container_type",
                 "container_qty", "origin_country", "origin_loc", "origin_alt",
                 "dest_country", "dest_loc", "dest_alt", "commodity", "incoterm",
                 "thread_key", "conversation_id", "entry_id", "source_subject"):
        if name in draft.values:
            value = draft.values[name]
            lines.append("  {:<18} {:<42} ({})".format(
                name, str(value)[:42], draft.sources.get(name, "")))
            if len(str(value)) > 42:
                lines.append("  {:<18} {}".format("", str(value)[42:]))
            if draft.evidence.get(name):
                lines.append("  {:<18} └ {}".format("", draft.evidence[name]))
    if draft.special_tags:
        lines.append("  {:<18} {}".format("special_handling",
                                          "; ".join(draft.special_tags)))
    if draft.outbound or draft.reply_to_requester or draft.direction:
        lines.extend(["", "--- our own mail ---",
                      "  direction      : {}".format(draft.direction or "neither asking nor quoting"),
                      "  reads as       : {}".format(
                          "reply to the requester" if draft.reply_to_requester
                          else "sent out by us" if draft.outbound else "-")])
        if draft.vendor_note:
            lines.append("  note           : {}".format(draft.vendor_note))
        if draft.vendors:
            lines.append("  vendors panel  :")
            for vendor in draft.vendors:
                lines.append("    [{}] {:<28} {}{}{}".format(
                    "x" if vendor.get("checked") else " ", vendor["domain"], vendor["key"],
                    "  (on file)" if vendor.get("known") else "  (new party)",
                    "  - " + vendor["note"] if vendor.get("note") else ""))
        elif draft.outbound:
            lines.append("  vendors panel  : none offered")
    if draft.notes:
        lines.extend(["", "--- notes ---"])
        lines.extend("  " + note for note in draft.notes)
    lines.extend(["", "Nothing was written. This is what the form would show."])
    return lines


#: How the requester was arrived at, in the width a table column allows.
_REQUESTER_SOURCE_SHORT = {
    FROM_SENDER: "sender",
    FROM_RECIPIENT: "To/CC",
    FROM_SUBJECT: "subject",
    FROM_OUTBOUND: "we asked",
    FROM_REPLY: "our reply",
}


def _requester_label(draft: Draft) -> str:
    """The party key the form would show, with a hint at how it was found."""
    party_id = draft.values.get("requester_party_id")
    if not party_id:
        return "-"
    short = _REQUESTER_SOURCE_SHORT.get(draft.sources.get("requester_party_id"), "")
    key = draft.requester_key
    if not key:
        for match in draft.party_candidates:
            if match.party_id == party_id:
                key = match.key
                break
    key = key or "#{}".format(party_id)
    return "{} ({})".format(key, short) if short else key


def _lane_label(draft: Draft) -> str:
    """The lane column. Where it came from matters as much as what it says."""
    if not draft.lanes:
        for side, readings in draft.body_conflicts.items():
            return "? {} {}s in body".format(len(readings), side)
        return "-"
    label = "{} - {}".format(draft.lanes[0]["origin"],
                             " or ".join([draft.lanes[0]["dest"]] + list(draft.lanes[0].get("dest_more") or [])))
    if draft.lane_count > 1:
        label += " +{}".format(draft.lane_count - 1)
    if FROM_BODY in (draft.sources.get("origin_loc"),
                     draft.sources.get("dest_loc")):
        label += " (body)"
    return label


def summarise(drafts: list, selection_count=None) -> list:
    """One line per selected mail plus a tally, for a count-by-hand check.

    The tally counts what was pre-filled, not what was right: only she can say
    whether MIA - LIM was the lane the mail actually asked about.
    """
    lines = ["--- {} mail(s) read ---".format(len(drafts))]
    if selection_count and selection_count > len(drafts):
        lines.append("  ({} of {} selected items were not mails)".format(
            selection_count - len(drafts), selection_count))
    row = "  {:>3}  {:<10}  {:<22}  {:<24}  {:<6}  {}"
    lines.append(row.format("#", "received", "requester", "lane", "mode",
                            "subject"))
    for number, draft in enumerate(drafts, start=1):
        lines.append(row.format(
            number,
            draft.values.get("received_on", "-"),
            _requester_label(draft)[:22],
            _lane_label(draft)[:24],
            draft.values.get("service_mode", "-")[:6],
            (draft.mail.subject or "")[:60]))

    total = len(drafts)
    dated = sum(1 for d in drafts if d.values.get("received_on"))
    requester = sum(1 for d in drafts if d.values.get("requester_party_id"))
    laned = sum(1 for d in drafts if d.lanes)
    from_body = sum(1 for d in drafts
                    if FROM_BODY in (d.sources.get("origin_loc"),
                                     d.sources.get("dest_loc")))
    undecided = sum(1 for d in drafts if d.body_conflicts)
    moded = sum(1 for d in drafts if d.values.get("service_mode"))
    unknown = sorted({key for d in drafts for key in d.unknown_locations})
    lines.extend([
        "",
        "--- pre-filled ---",
        "  received date  {}/{}".format(dated, total),
        "  requester      {}/{}".format(requester, total),
        "  lane           {}/{}{}".format(
            laned, total,
            "   ({} read from the body)".format(from_body) if from_body else ""),
        "  service mode   {}/{}".format(moded, total),
    ])
    if undecided:
        lines.append("  {} mail(s) name several places in the body; the form "
                     "lists them rather than choose".format(undecided))
    if unknown:
        lines.append("  new locations  {}".format(", ".join(unknown)))
    lines.extend([
        "",
        "This counts what was filled in, not what was correct - check the lines",
        "against the mails yourself. Nothing was written.",
    ])
    return lines


# ------------------------------------------------ batch 14: attachments --
FROM_ATTACHMENT = "from the attachment {}"
#: The request fields an attachment may fill (when the body left them
#: empty) or contradict (an amber check). Quantities are compared through
#: the cargo lines, not the 'Quantity as received' text.
ATTACHMENT_FIELDS = ("origin_loc", "origin_country", "dest_loc", "dest_country", "origin_alt",
                     "dest_alt", "incoterm", "service_mode", "container_type", "container_qty", "commodity")
#: (the weight and the volume are the cargo lines' totals, as for the body)
ATTACHMENT_QTY = ("qty_raw",)
ATTACHMENT_LABELS = {
    "origin_loc": "origin", "origin_country": "origin country", "dest_loc": "destination",
    "dest_country": "destination country", "origin_alt": "pickup place", "dest_alt": "delivery place",
    "incoterm": "incoterm", "service_mode": "mode", "container_type": "container type",
    "container_qty": "containers", "commodity": "commodity",
}
#: two weights or volumes closer than this are the same figure
ATTACHMENT_TOLERANCE = 0.02
#: Where a difference between the body and an attachment means something,
#: and is worth an amber check. An address is written a hundred ways and a
#: document's words often read as a mode, so those only ever fill a gap.
ATTACHMENT_CHECKED = ("origin_loc", "dest_loc", "incoterm", "container_type", "container_qty")
#: A document's own labels, never a commodity ('CONSIGNEE' off an air waybill).
_LABEL_WORDS = {"consignee", "shipper", "notify", "notify party", "description", "commodity", "goods",
                "cargo", "n/a", "na", "tbc", "item", "items", "marks", "remarks", "note", "notes", "total"}
_NOT_AN_ADDRESS = re.compile(r"[$€£]|\b(?:fee|fees|charges?|rates?|per shipment|insurance|date|"
                             r"total|invoice|price|cost|quotation|please|provide)\b", re.IGNORECASE)


def attachments_wanted(draft) -> bool:
    """Only a mail that asks us has its attachments read (batch 14 item 6):
    one from outside or a colleague's forward, or one of ours asking a
    vendor. Our own mail giving a rate (its quotation PDF) or answering
    the requester is not a request."""
    we_sent = draft.outbound or draft.reply_to_requester
    return not we_sent or draft.direction == "asking"


@dataclass
class _TextMail:
    """An attachment's text dressed as a mail, for the body readers."""

    body_excerpt: str = ""
    subject: str = ""
    received_on: str = ""
    received_at: str = ""
    entry_id: str = ""
    conversation_id: str = ""
    conversation_topic: str = ""
    sender_name: str = ""
    sender_address: str = ""
    sender_internal: bool = False
    to_addresses: list = field(default_factory=list)
    cc_addresses: list = field(default_factory=list)
    to_names: list = field(default_factory=list)
    cc_names: list = field(default_factory=list)
    display_to: str = ""
    selection_count: int = 1
    notes: list = field(default_factory=list)

    @property
    def recipients(self) -> list:
        return []


def content_of(conn, text: str, entry_id: str = "") -> Draft:
    """What the body readers find in one attachment's text: a draft whose
    request fields and cargo lines are all that is used."""
    return build(conn, _TextMail(body_excerpt=text, entry_id=entry_id))


def _content_of_file(conn, f, entry_id: str = "") -> Draft:
    """content_of() for one attachment. A sheet with a header row is read
    row by row - each row its own consignment, one cargo line each - and
    the rows' request fields are the first row's that has them."""
    if not getattr(f, "records", None):
        return content_of(conn, f.text, entry_id)
    merged = Draft()
    raws = []
    for record in f.records:
        sub = content_of(conn, "\n".join(record), entry_id)
        for name, value in sub.values.items():
            if name not in merged.values and value not in (None, "", []):
                merged.values[name] = value
                merged.sources[name] = sub.sources.get(name, "")
                if name in sub.evidence:
                    merged.evidence[name] = sub.evidence[name]
        offset = len(merged.cargo_stated)
        for line in sub.cargo_lines:
            line = dict(line)
            if line.get("stated_ref") is not None:
                line["stated_ref"] = int(line["stated_ref"]) + offset
            merged.cargo_lines.append(line)
        merged.cargo_stated.extend(sub.cargo_stated)
        if not merged.lanes and sub.lanes:
            merged.lanes = sub.lanes
        if sub.values.get("qty_raw"):
            raws.append(sub.values["qty_raw"])
    if len(raws) > 1:
        merged.values["qty_raw"] = " / ".join(raws)
    return merged


def _same(a, b) -> bool:
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        try:
            x, y = float(a), float(b)
        except (TypeError, ValueError):
            return False
        return abs(x - y) <= ATTACHMENT_TOLERANCE * max(abs(x), abs(y), 1e-9)
    return " ".join(str(a).split()).casefold() == " ".join(str(b).split()).casefold()


def _cargo_said(lines) -> dict:
    """{'pieces': '4 pallets', 'weight': '240 kg', 'cbm': '2.1 cbm'} - the
    totals of some cargo lines, as words, for an amber check; and the
    numbers under '_n'."""
    numbers = {"pieces": None, "weight_kg": None, "cbm": None}
    word = ""
    for line in lines:
        line = cargo.normalise(dict(line))
        for key in numbers:
            if line.get(key) is not None:
                numbers[key] = (numbers[key] or 0) + float(line[key])
        word = word or (line.get("pack_word") or "")
    out = {"_n": numbers}
    if numbers["pieces"] is not None:
        out["pieces"] = "{:g} {}".format(numbers["pieces"], word or "pieces").strip()
    if numbers["weight_kg"] is not None:
        out["weight"] = "{:g} kg".format(round(numbers["weight_kg"], 1))
    if numbers["cbm"] is not None:
        out["cbm"] = "{:g} cbm".format(round(numbers["cbm"], 3))
    return out


def _used_lines(sub, lines, goods) -> list:
    """The attachment's lines the readers took something from: the
    evidence of each value, the cargo lines' text, section 14's UN lines."""
    wanted = []
    for value in sub.evidence.values():
        wanted.extend(str(value).split("; "))
    for line in sub.cargo_lines:
        wanted.extend((line.get("source_text") or "").splitlines())
        wanted.append(line.get("evidence") or "")
    wanted = {" ".join(w.split()).casefold() for w in wanted if w and w.strip()}
    out = [l for l in lines if " ".join(l.split()).casefold() in wanted]
    for good in goods:
        out.extend(l for l in good.lines if good.un in l and l not in out)
    return out


def _plausible(conn, name, value, sub, known) -> bool:
    """An attachment's value is only taken when it looks like one: a place
    that is on file, an address of address length with a number in it, a
    commodity that is not one of the document's labels, a mode or a
    quantity that came with cargo the attachment describes."""
    text = " ".join(str(value).split())
    if name in ("origin_loc", "dest_loc"):
        return text.upper() in known
    if name in ("origin_country", "dest_country"):
        side = name.split("_")[0]
        return (sub.values.get(side + "_loc") or "").upper() in known
    if name in ("origin_alt", "dest_alt"):
        return 8 <= len(text) <= 100 and any(c.isdigit() for c in text) and not _NOT_AN_ADDRESS.search(text)
    if name == "commodity":
        return 3 <= len(text) <= 80 and text.casefold().strip(" :") not in _LABEL_WORDS
    if name == "service_mode":
        return bool(sub.values.get("container_type")) or _real_cargo(sub.cargo_lines)
    return True


def _real_cargo(lines) -> bool:
    """Cargo lines worth taking: at least one with a weight, a volume or
    containers (an invoice's piece counts alone are not a consignment)."""
    for line in lines:
        line = cargo.normalise(dict(line))
        if any(line.get(k) not in (None, "", 0) for k in ("weight_kg", "cbm", "container_qty")):
            return True
    return False


def add_attachments(conn, draft, files) -> None:
    """Read the mail's attachments into the draft (batch 14). The body wins:
    an attachment fills only what the body left empty, each value with
    'from the attachment <name>'; where the two disagree the body's value
    stays and an amber check names both. An SDS's section 14 sets DG and
    puts 'UN1993, Class 3, PG III (from <file>)' in its cargo line's note,
    one line per product. A file that cannot be read is named, never fatal."""
    from . import attachments as att
    from . import logs

    draft.attachment_files = att.every_file(files)
    entry_id = draft.values.get("entry_id") or ""
    known = {k.upper() for k in _known_locations(conn)}
    for f in draft.attachment_files:
        if not f.read:
            continue
        source = FROM_ATTACHMENT.format(f.name)
        try:
            sub = _content_of_file(conn, f, entry_id)
        except Exception:                                 # a reader never stops a registration
            logs.get("intake").exception("attachment %r could not be read", f.name)
            continue
        goods = att.dangerous_goods(f.lines)
        draft.attachment_used[f.name] = (list(f.lines) if getattr(f, "records", None)
                                         else _used_lines(sub, f.lines, goods))
        # --- the request fields
        had_lane = bool(draft.values.get("origin_loc") or draft.values.get("dest_loc"))
        for name in ATTACHMENT_FIELDS:
            value = sub.values.get(name)
            if value in (None, "", []) or not _plausible(conn, name, value, sub, known):
                continue
            have = draft.values.get(name)
            if have in (None, "", []):
                draft.set(name, value, source, sub.evidence.get(name))
            elif name in ATTACHMENT_CHECKED and not _same(have, value):
                check = "{}: the mail says {}, {} says {}".format(
                    ATTACHMENT_LABELS.get(name, name).capitalize(), have, f.name, value)
                if check not in draft.attachment_checks:
                    draft.attachment_checks.append(check)
        if not had_lane and not draft.lanes and sub.lanes and \
                all((sub.lanes[0].get(s) or "").upper() in known for s in ("origin", "dest") if sub.lanes[0].get(s)):
            draft.lanes = [dict(l) for l in sub.lanes]
        # --- the cargo
        real = _real_cargo(sub.cargo_lines)
        if real and not draft.cargo_lines:
            offset = len(draft.cargo_stated)
            for line in sub.cargo_lines:
                line = dict(line, attachment=f.name)
                if line.get("stated_ref") is not None:
                    line["stated_ref"] = int(line["stated_ref"]) + offset
                draft.cargo_lines.append(line)
            draft.cargo_stated.extend(sub.cargo_stated)
            for name in ATTACHMENT_QTY:
                if sub.values.get(name) not in (None, "") and draft.values.get(name) in (None, ""):
                    draft.set(name, sub.values[name], source, sub.evidence.get(name))
        elif real:
            ours, theirs = _cargo_said(draft.cargo_lines), _cargo_said(sub.cargo_lines)
            for key, n_key in (("pieces", "pieces"), ("weight", "weight_kg"), ("cbm", "cbm")):
                a, b = ours["_n"][n_key], theirs["_n"][n_key]
                if a is not None and b is not None and not _same(a, b):
                    check = "the mail says {}, {} says {}".format(ours[key], f.name, theirs[key])
                    if check not in draft.attachment_checks:
                        draft.attachment_checks.append(check)
        elif not draft.cargo_lines and (real or sub.values.get("container_type")):
            for name in ATTACHMENT_QTY:
                if sub.values.get(name) not in (None, "") and draft.values.get(name) in (None, ""):
                    draft.set(name, sub.values[name], source, sub.evidence.get(name))
        # --- an SDS: dangerous goods, one cargo line each
        if goods:
            if not draft.cargo_lines:
                # the body's numbers with no line yet ('4 X 40 HC'): the line
                # the register page would make of them comes first, so the
                # UN note goes on it rather than pushing it out (15b's rule)
                from . import cargo_lines_migration
                typed = dict(draft.values)
                typed.setdefault("gross_kg", draft.values.get("air_weight_kg"))
                typed.setdefault("volume_cbm", draft.values.get("lcl_cbm"))
                first = cargo_lines_migration.line_from_typed(typed, source="mail")
                if first:
                    draft.cargo_lines.append(dict(first, weight_unit=first.get("weight_unit") or (
                        "kg" if first.get("weight_value") is not None else None)))
            if "DG" not in draft.special_tags:
                draft.special_tags = list(draft.special_tags) + ["DG"]
                draft.sources["special_handling"] = source
            for good in goods:
                note = good.text(f.name)
                said = next((l for l in draft.cargo_lines if note in (l.get("notes") or "")), None)
                if said is not None:                       # already on a line (the case page)
                    said["_un"] = good.un
                    continue
                free = next((l for l in draft.cargo_lines if not l.get("_un")), None)
                if free is None:
                    free = {"label": "", "source": "mail", "source_mail": entry_id or None,
                            "commodity": good.name.title() if good.name else None, "counted": 1,
                            "deliver_to": "", "attachment": f.name, "evidence": note}
                    draft.cargo_lines.append(free)
                free["_un"] = good.un
                free["notes"] = "; ".join(p for p in ((free.get("notes") or "").strip(), note) if p)


#: The fields a finding on the case page writes when accepted: route A's
#: (edit_route) and the case's own. Anything else an attachment says is
#: offered as a note on the timeline.
ROUTE_FINDINGS = ("origin_alt", "dest_alt", "incoterm", "service_mode")
CASE_FINDINGS = ("commodity",)


def _finding_key(kind, payload) -> str:
    import hashlib
    import json as _json
    raw = _json.dumps([kind, payload], sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _line_words(line) -> str:
    line = cargo.normalise(dict(line))
    bits = []
    if line.get("pieces") is not None:
        bits.append("{:g} {}".format(float(line["pieces"]), line.get("pack_word") or "pieces").strip())
    if line.get("container_qty"):
        bits.append("{} x {}".format(line["container_qty"], line.get("container_type") or "?"))
    if line.get("weight_kg") is not None:
        bits.append("{:g} kg".format(round(float(line["weight_kg"]), 1)))
    if line.get("cbm") is not None:
        bits.append("{:g} cbm".format(round(float(line["cbm"]), 3)))
    if line.get("commodity"):
        bits.append(line["commodity"])
    if line.get("notes"):
        bits.append(line["notes"])
    return ", ".join(bits) or "a line with no numbers"


def attachment_findings(conn, case, lines, files) -> list:
    """What a mail's attachments would add to a case as it stands (batch 14
    item 7), as suggestions for the case page: [{key, kind, text, source,
    payload}]. The same readers and the same rule as registering - the
    case's values win, an attachment fills what is empty, a difference is
    an amber check. Nothing is written here."""
    case = dict(case)
    draft = Draft()
    for name in ATTACHMENT_FIELDS:
        if case.get(name) not in (None, ""):
            draft.values[name] = case[name]
    draft.values["entry_id"] = case.get("entry_id") or ""
    draft.special_tags = [t for t in (case.get("special_handling") or "").split(";") if t.strip()]
    before_tags = list(draft.special_tags)
    draft.cargo_lines = [dict(l, _case_line=l["id"], _was=l.get("notes") or "") for l in lines]
    add_attachments(conn, draft, files)
    out = []

    def add(kind, text, source, payload):
        out.append({"key": _finding_key(kind, payload), "kind": kind, "text": text, "source": source,
                    "payload": payload})

    for name in ATTACHMENT_FIELDS:
        source = draft.sources.get(name) or ""
        if not source.startswith(FROM_ATTACHMENT.format("")):
            continue
        value = draft.values[name]
        label = ATTACHMENT_LABELS.get(name, name).capitalize()
        if name in ROUTE_FINDINGS or name in CASE_FINDINGS:
            add("field", "{}: {}".format(label, value), source, {"field": name, "value": value})
        else:
            add("note", "{} says {} {}".format(source[len(FROM_ATTACHMENT.format("")):], label.lower(), value),
                source, {"note": "{} {} ({})".format(label, value, source)})
    for line in draft.cargo_lines:
        if line.get("_case_line"):
            if (line.get("notes") or "") != line.get("_was"):
                added = (line.get("notes") or "")[len(line["_was"]):].lstrip("; ")
                add("line_note", "Cargo line {} note: {}".format(line.get("seq") or "", added).replace("line  ", "line "),
                    "", {"line_id": line["_case_line"], "notes": line.get("notes")})
            continue
        keep = {k: v for k, v in line.items() if k in cargo.FIELDS and v not in (None, "")}
        keep["source"] = "mail"
        name = line.get("attachment") or ""
        add("line", "Add cargo line: {}".format(_line_words(line)), FROM_ATTACHMENT.format(name) if name else "",
            {"line": keep})
    if "DG" in draft.special_tags and "DG" not in before_tags:
        add("tag", "Special handling: DG", draft.sources.get("special_handling") or "",
            {"tags": ";".join(draft.special_tags)})
    for check in draft.attachment_checks:
        add("check", check, "", {"note": "Attachment: {}".format(check)})
    return out


def apply_finding(repo, enquiry_id: int, finding: dict, today: str) -> str:
    """Accept one finding (batch 14 item 7): through the repository, with
    change_log. Returns what was done, for the flash."""
    from . import routes as routes_mod
    kind, payload = finding["kind"], finding["payload"]
    if kind == "field" and payload["field"] in ROUTE_FINDINGS:
        route = routes_mod.route_a(repo.conn, enquiry_id)
        if route is None:
            raise ValueError("The case has no route to put it on.")
        repo.edit_route(route["id"], {payload["field"]: payload["value"]}, at=today)
    elif kind == "field" and payload["field"] in CASE_FINDINGS:
        repo.update("enquiry", enquiry_id, {payload["field"]: payload["value"]})
    elif kind == "line":
        repo.add_cargo_line(enquiry_id, payload["line"])
    elif kind == "line_note":
        line = repo.conn.execute("SELECT id FROM enquiry_cargo WHERE id = ? AND enquiry_id = ?",
                                 (payload["line_id"], enquiry_id)).fetchone()
        if line is None:
            raise ValueError("That cargo line is no longer on the case.")
        repo.edit_cargo_line(line["id"], {"notes": payload["notes"]})
    elif kind == "tag":
        repo.update("enquiry", enquiry_id, {"special_handling": payload["tags"]})
    elif kind in ("check", "note"):
        repo.add_note(enquiry_id, today, payload["note"])
    else:
        raise ValueError("Not a finding this page knows.")
    repo.touch("enquiry", enquiry_id)
    repo.commit()
    return "Accepted: {}".format(finding["text"])
