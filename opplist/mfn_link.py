"""OppList's agents linked to the MFN directory (batch WC1, 2026-11-05).

MFN-Parse writes the directory to ..\\data\\mfn\\directory\\ (mfn-offices.csv,
mfn-contacts.csv). This reads it, read-only, and proposes for every Agent
party that is not merged which MFN office it is:

- link        one office matches by domain in the party's country, or a
              group domain matches and the party's own office is found by
              name in its country;
- check       several offices in the country, a name-only match, a domain
              of another country's office, a MFN no. on the party naming a
              different office, the same name only abroad, or a party whose
              country is Unknown;
- not in MFN  MFN's file has the country and nothing matches;
- wait        MFN's file does not have the country yet;
- linked      written by an earlier --apply: only what MFN changed since is
              offered.

Countries are compared by ISO code (country.iso2 against MFN's
country_code), never by name. Two or more parties proposed for one office
are marked "merge?" and left alone until she decides; nothing is merged here.

Her decisions live in ..\\data\\mfn-link\\mfn-link-decisions.csv (party id,
decision: link / skip / a member id, group: a key / none, note). This module
creates that file with its header when it is missing and never writes it
otherwise.

Groups follow the layer of 16 Sep: offices in different countries stay
separate parties sharing one party_group. MFN offices that share a company
domain are one company group; Agent parties linked to (or matching by
domain) the offices of one company group are proposed as one party_group.
Name hints are shown, never written without her decision. A party already
in a group stays there, and no group is emptied or renamed.

--apply writes, after a restore point, in one transaction with change_log
source 'mfn-link': mfn_no, mfn_member, full_name, country, city, the former
name into aliases, MFN's domains (never another party's) and contacts (one
per e-mail address the party does not have), and the groups. Nothing else.
"""

from __future__ import annotations

import csv
import datetime as dt
import difflib
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import db, matching, paths

SOURCE = "mfn-link"
DIRECTORY_DIR = paths.DATA_DIR / "mfn" / "directory"
OUT_DIR = paths.DATA_DIR / "mfn-link"
PROPOSED_NAME = "mfn-link-proposed.csv"
DECISIONS_NAME = "mfn-link-decisions.csv"
DECISION_COLUMNS = ["party id", "decision", "group", "note"]

LINK, CHECK, NOT_IN_MFN, WAIT, LINKED = "link", "check", "not in MFN", "wait", "linked"
KINDS = (LINK, CHECK, NOT_IN_MFN, WAIT, LINKED)

#: Legal forms: dropped before two names are compared.
LEGAL_WORDS = frozenset((
    "sa", "sac", "sas", "sau", "saa", "sae", "srl", "srls", "ltda", "ltd", "limited", "llc",
    "inc", "co", "corp", "corporation", "company", "cia", "spa", "gmbh", "bv", "nv", "ag",
    "pte", "pty", "plc", "eirl", "eas", "sl", "slu", "sarl", "cv", "rl", "de", "kft", "oy",
    "ab", "jsc", "lda", "ltee", "the", "y", "and", "s", "e", "sde", "ca",
))
#: Words of the trade: a group key does not end on one, and a name made of
#: nothing else names nobody.
TRADE_WORDS = frozenset((
    "logistics", "logistic", "logistica", "logisticas", "logistique", "cargo", "cargas",
    "carga", "freight", "forwarding", "forwarder", "forwarders", "shipping", "transport",
    "transporte", "transportes", "transportation", "trans", "international", "internacional",
    "intl", "int", "express", "service", "services", "servicios", "solutions", "solution",
    "group", "grupo", "lines", "line", "agency", "agencia", "aduanas", "customs", "worldwide",
    "global",
))
#: Country names a key carries that are not in the country table.
EXTRA_COUNTRY_WORDS = ("brasil", "espana", "republica dominicana", "estados unidos", "rd")
#: Two-letter top-level domains that are not used as a country's.
GENERIC_TLDS = frozenset(("io", "me", "tv", "cc", "ws", "ai", "fm", "gg", "to", "ly", "eu"))


# ------------------------------------------------------------------ text ----
def _plain(text) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()


def _words(text) -> list:
    return re.findall(r"[a-z0-9]+", _plain(text))


def _no_parens(text) -> str:
    return re.sub(r"\([^)]*\)", " ", str(text or ""))


def _joined(text) -> list:
    """Words, with a run of single letters read as one: 'B&M' -> 'bm',
    'S.A.' -> 'sa', 'J.R. Cargo' -> 'ha cargo'."""
    out, run = [], ""
    for word in _words(text):
        if len(word) == 1:
            run += word
            continue
        if run:
            out.append(run)
            run = ""
        out.append(word)
    if run:
        out.append(run)
    return out


def name_form(text) -> str:
    """'Altura Logistics S.A. de C.V.' -> 'altura logistics': legal forms
    dropped. A name of trade words only names somebody when it has three."""
    words = [w for w in _joined(text) if w not in LEGAL_WORDS]
    if not words or (all(w in TRADE_WORDS for w in words) and len(words) < 3):
        return ""
    return " ".join(words)


def _forms(*texts) -> set:
    out = set()
    for text in texts:
        for variant in (text, _no_parens(text)):
            form = name_form(variant)
            if len(form) >= 3:
                out.add(form)
    return out


def tld_code(domain) -> str:
    """'orx.com.sg' -> 'SG'; '' for .com and the generic two-letter ones."""
    labels = [part for part in (domain or "").lower().split(".") if part]
    if len(labels) < 2 or len(labels[-1]) != 2 or labels[-1] in GENERIC_TLDS:
        return ""
    return "GB" if labels[-1] == "uk" else labels[-1].upper()


# ------------------------------------------------------------- directory ----
@dataclass
class Office:
    member_id: str
    company: str
    fka: str
    office_type: str
    city: str
    country: str
    code: str
    web_domain: str
    email_domain: str
    contacts: list = field(default_factory=list)     # dicts: name, title, email, domain

    def label(self) -> str:
        return "{} {} ({}, {})".format(self.member_id, self.company, self.city or "-", self.country)

    def head(self) -> bool:
        return "head" in (self.office_type or "").lower()


@dataclass
class Directory:
    folder: Path
    date: str
    offices: dict
    by_domain: dict
    by_name: dict
    codes: set
    company: dict          # member id -> company group number
    company_offices: dict  # company group number -> [member ids]

    def domains(self, member_id) -> set:
        office = self.offices[member_id]
        out = {office.web_domain, office.email_domain}
        out.update(c["domain"] for c in office.contacts)
        return {d for d in out if d}


def _read_csv(path: Path) -> list:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _clean_domain(value) -> str:
    return (value or "").strip().lower().lstrip("@").rstrip(".")


_CACHE = {}


def cached_directory(internal=frozenset(), folder=None):
    """RG2b (WC2): the directory for Register, read once and again only when
    mfn-offices.csv changes; None when it is not there or cannot be read.
    Read only. The folder is paths.DATA_DIR's at the call (a test's own)."""
    folder = Path(folder or paths.DATA_DIR / "mfn" / "directory")
    offices_csv = folder / "mfn-offices.csv"
    try:
        stamp = offices_csv.stat().st_mtime
    except OSError:
        return None
    key = (str(folder), frozenset(internal))
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        directory = load_directory(folder, internal)
    except (OSError, ValueError, csv.Error):
        return None
    _CACHE.clear()
    _CACHE[key] = (stamp, directory)
    return directory


def load_directory(folder=None, internal=frozenset()) -> Directory:
    """The two files MFN-Parse writes. Free mail and our own domains are no
    company's and are dropped here."""
    folder = Path(folder or DIRECTORY_DIR)
    offices_csv, contacts_csv = folder / "mfn-offices.csv", folder / "mfn-contacts.csv"
    if not offices_csv.exists():
        raise FileNotFoundError("no {} (run MFN-Parse first, or give --dir)".format(offices_csv))

    def usable(domain):
        domain = _clean_domain(domain)
        if not domain or "." not in domain or matching.is_free_mail(domain) or domain in internal:
            return ""
        return domain

    offices = {}
    for row in _read_csv(offices_csv):
        member = (row.get("member_id") or "").strip()
        if not member:
            continue
        offices[member] = Office(
            member_id=member, company=(row.get("company") or "").strip(),
            fka=(row.get("formerly_known_as") or "").strip(),
            office_type=(row.get("office_type") or "").strip(),
            city=(row.get("city") or row.get("office_city") or "").strip(),
            country=(row.get("country") or "").strip(),
            code=(row.get("country_code") or "").strip().upper(),
            web_domain=usable(row.get("web_domain")), email_domain=usable(row.get("email_domain")))
    if contacts_csv.exists():
        for row in _read_csv(contacts_csv):
            office = offices.get((row.get("member_id") or "").strip())
            email = (row.get("email") or "").strip()
            if office is None:
                continue
            office.contacts.append({
                "name": (row.get("name") or "").strip(), "title": (row.get("title") or "").strip(),
                "email": email, "domain": usable(row.get("email_domain") or matching.domain_of(email))})

    by_domain, by_name = defaultdict(set), defaultdict(set)
    directory = Directory(folder, dt.date.fromtimestamp(offices_csv.stat().st_mtime).isoformat(),
                          offices, by_domain, by_name, {o.code for o in offices.values() if o.code},
                          {}, {})
    for member, office in offices.items():
        for domain in directory.domains(member):
            by_domain[domain].add(member)
        for form in _forms(office.company, office.fka):
            by_name[form].add(member)

    # company groups: offices that share a domain, chained
    parent = {m: m for m in offices}

    def find(m):
        while parent[m] != m:
            parent[m] = parent[parent[m]]
            m = parent[m]
        return m

    for members in by_domain.values():
        members = sorted(members)
        for other in members[1:]:
            a, b = find(members[0]), find(other)
            if a != b:
                parent[b] = a
    numbers = {}
    for member in sorted(offices, key=lambda m: (len(m), m)):
        root = find(member)
        numbers.setdefault(root, len(numbers) + 1)
        directory.company[member] = numbers[root]
        directory.company_offices.setdefault(numbers[root], []).append(member)
    return directory


# --------------------------------------------------------------- parties ----
@dataclass
class Party:
    id: int
    key: str
    full_name: str
    country: str
    iso: str
    city: str
    stage: str
    mfn_member: int | None
    mfn_no: str
    aliases: list
    group_id: int | None
    group_key: str
    domains: list
    emails: set
    cases: list            # opp numbers
    case_countries: Counter
    vendor_requests: int
    vendor_countries: Counter
    linked_log: int = 0    # the last change_log id mfn-link wrote on the link fields; 0 = never

    def forms(self) -> set:
        return _forms(self.key, self.full_name, *self.aliases)

    def own_codes(self) -> set:
        return {tld_code(d) for d in self.domains} - {""}


LINK_FIELDS = ("mfn_no", "mfn_member", "full_name", "country", "city", "aliases")


def load_parties(conn) -> list:
    iso = {r["name"]: (r["iso2"] or "").upper() for r in conn.execute("SELECT name, iso2 FROM country")}
    groups = {r["id"]: r["key"] for r in conn.execute("SELECT id, key FROM party_group")}
    domains, emails = defaultdict(list), defaultdict(set)
    for r in conn.execute("SELECT domain, party_id FROM party_domain ORDER BY domain"):
        domains[r["party_id"]].append(r["domain"])
    for r in conn.execute("SELECT party_id, email FROM contact WHERE email IS NOT NULL AND email <> ''"):
        emails[r["party_id"]].add(r["email"].strip().lower())
    cases, case_countries = defaultdict(list), defaultdict(Counter)
    for r in conn.execute("SELECT requester_party_id AS pid, opp_no, origin_country, dest_country "
                          "FROM v_enquiry WHERE requester_party_id IS NOT NULL ORDER BY opp_no"):
        cases[r["pid"]].append(r["opp_no"])
        for c in {r["origin_country"], r["dest_country"]} - {None, ""}:
            case_countries[r["pid"]][c] += 1
    vendor, vendor_countries = Counter(), defaultdict(Counter)
    for r in conn.execute("SELECT v.vendor_party_id AS pid, e.origin_country, e.dest_country "
                          "FROM vendor_request v JOIN v_enquiry e ON e.id = v.enquiry_id "
                          "WHERE COALESCE(v.voided, 0) = 0"):
        vendor[r["pid"]] += 1
        for c in {r["origin_country"], r["dest_country"]} - {None, ""}:
            vendor_countries[r["pid"]][c] += 1
    linked = {}
    marks = ",".join("?" for _ in LINK_FIELDS)
    for r in conn.execute("SELECT row_id, MAX(id) AS last FROM change_log WHERE table_name = 'party' "
                          "AND source = ? AND field IN ({}) GROUP BY row_id".format(marks),
                          (SOURCE,) + LINK_FIELDS):
        linked[r["row_id"]] = r["last"]
    out = []
    for r in conn.execute("SELECT * FROM party WHERE type = 'Agent' AND merged_into IS NULL ORDER BY id"):
        try:
            aliases = [a for a in json.loads(r["aliases"] or "[]") if isinstance(a, str)]
        except ValueError:
            aliases = []
        country = r["country"] or ""
        out.append(Party(
            id=r["id"], key=r["key"], full_name=r["full_name"] or "", country=country,
            iso="" if country in ("", "Unknown") else iso.get(country, ""), city=r["city"] or "",
            stage=r["stage"], mfn_member=r["mfn_member"], mfn_no=(r["mfn_no"] or "").strip(),
            aliases=aliases, group_id=r["group_id"], group_key=groups.get(r["group_id"], ""),
            domains=domains[r["id"]], emails=emails[r["id"]], cases=cases[r["id"]],
            case_countries=case_countries[r["id"]], vendor_requests=vendor[r["id"]],
            vendor_countries=vendor_countries[r["id"]],
            linked_log=linked.get(r["id"], 0) if (r["mfn_no"] and r["mfn_member"] == 1) else 0))
    return out


# -------------------------------------------------------------- proposal ----
@dataclass
class Row:
    party: Party
    proposal: str = ""
    member: str = ""                 # the office proposed
    why: str = ""
    others: list = field(default_factory=list)
    dom_matches: set = field(default_factory=set)   # every office the party's domains match
    merge_with: list = field(default_factory=list)
    decision: str = ""
    decided_group: str = ""
    note: str = ""
    target: str = ""                 # the office written on --apply ('' = none)
    trouble: str = ""                # a decision that cannot be done
    changes: dict = field(default_factory=dict)
    group: str = ""
    group_evidence: str = ""
    hint: str = ""

    @property
    def writes(self) -> bool:
        return bool(self.target) and not self.trouble


def _ratio(a, b) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _pick(members, party: Party, directory: Directory) -> str:
    """Of several offices: the one with the party's name (its full name to
    the letter first), else the one in the party's city, else the one head
    office, else the closest name."""
    members = sorted(members, key=lambda m: (len(m), m))
    forms = party.forms()
    named = [m for m in members if _forms(directory.offices[m].company, directory.offices[m].fka) & forms]
    if len(named) == 1:
        return named[0]
    pool = named or members
    def same(m, name):
        return bool(name) and " ".join(_words(directory.offices[m].company)) == " ".join(_words(name))

    for narrow in ([m for m in pool if same(m, party.full_name)],
                   [m for m in pool if same(m, party.key)],
                   [m for m in pool if party.city and _plain(directory.offices[m].city) == _plain(party.city)],
                   [m for m in pool if directory.offices[m].head()]):
        if len(narrow) == 1:
            return narrow[0]
        if narrow:
            pool = narrow
    best = max(pool, key=lambda m: max((_ratio(f, name_form(directory.offices[m].company))
                                        for f in forms), default=0.0))
    return best


def _countries(members, directory) -> str:
    return ", ".join(sorted({directory.offices[m].country for m in members}))


def _same_company(member, dom, directory) -> bool:
    """The office found by name belongs to the company whose domain matched
    (or MFN lists no domain for it)."""
    return not directory.domains(member) or         directory.company[member] in {directory.company[m] for m in dom}


def propose(party: Party, directory: Directory) -> Row:
    row = Row(party)
    iso = party.iso
    dom = set()
    hit_domain = {}
    for domain in party.domains:
        for member in directory.by_domain.get(domain, ()):
            dom.add(member)
            hit_domain.setdefault(member, domain)
    row.dom_matches = dom
    names = set()
    for form in party.forms():
        names |= directory.by_name.get(form, set())
    on_file = party.mfn_no if party.mfn_no in directory.offices else ""

    def done(kind, member, why, others=()):
        row.proposal, row.member, row.why = kind, member, why
        row.others = sorted(set(others) - {member}, key=lambda m: (len(m), m))
        return row

    if not iso:
        found = dom or names or ({on_file} if on_file else set())
        if not found:
            return done(WAIT, "", "OppList's country is {}; nothing in MFN matches by domain "
                        "or name in any country read".format(party.country or "empty"))
        member = _pick(found, party, directory)
        how = "domain {}".format(hit_domain[member]) if member in hit_domain else (
            "name" if member in names else "MFN no. on file")
        return done(CHECK, member, "OppList's country is {}: matched by {} in {}; a party without "
                    "a country is never linked by itself".format(
                        party.country or "empty", how, directory.offices[member].country), found)

    in_dom = {m for m in dom if directory.offices[m].code == iso}
    in_name = {m for m in names if directory.offices[m].code == iso}
    kind = member = why = ""
    if len(in_dom) == 1:
        member = next(iter(in_dom))
        kind, why = LINK, "domain {} is this office's, in {}".format(
            hit_domain[member], directory.offices[member].country)
    elif dom and not in_dom and len(in_name) == 1 and _same_company(next(iter(in_name)), dom, directory):
        member = next(iter(in_name))
        kind, why = LINK, "group domain {} matches; the party's own office is found by name in {}".format(
            hit_domain[min(dom)], directory.offices[member].country)
    elif len(in_dom) > 1:
        member = _pick(in_dom, party, directory)
        kind, why = CHECK, "{} offices in {} match by domain: pick one (proposed: {})".format(
            len(in_dom), directory.offices[member].country,
            "the party's name" if _forms(directory.offices[member].company) & party.forms()
            else "head office" if directory.offices[member].head() else "the closest name")
    elif in_name:
        member = _pick(in_name, party, directory)
        kind, why = CHECK, "name only{}: no domain of the party is this office's".format(
            "" if len(in_name) == 1 else " ({} offices in the country)".format(len(in_name)))
    elif dom:
        # only another country's offices carry the domain: an office is
        # offered when the party's own domain carries that country's code
        # (a same name abroad on a group domain is a sister office, item 1e)
        backed = {m for m in dom if directory.offices[m].code in party.own_codes()}
        member = _pick(backed, party, directory) if backed else ""
        kind, why = CHECK, "the domain {} is another country's office ({}){}".format(
            hit_domain[min(dom)], _countries(dom, directory),
            "" if member else "; no office proposed: give a member id to link one")
    elif names and iso not in party.own_codes():
        member = _pick(names, party, directory)
        kind, why = CHECK, "the same name only in {}".format(_countries(names, directory))
    if not kind and on_file:
        member = on_file
        if directory.offices[on_file].code == iso:
            kind, why = LINK, "the MFN no. on the party is this office"
        else:
            kind, why = CHECK, "the MFN no. on the party is an office in {}".format(
                directory.offices[on_file].country)
    if not kind:
        if iso in directory.codes:
            return done(NOT_IN_MFN, "", "MFN's file has {} and nothing matches".format(party.country))
        return done(WAIT, "", "MFN-Parse has not read {} yet".format(party.country))
    if party.mfn_no and member and party.mfn_no != member:
        kind = CHECK
        why += "; the party's MFN no. {} names a different office{}".format(
            party.mfn_no, " ({})".format(directory.offices[party.mfn_no].label())
            if party.mfn_no in directory.offices else " (not in MFN's file)")
    return done(kind, member, why, dom | names)


# ------------------------------------------------------------- decisions ----
def ensure_decisions(path: Path) -> bool:
    """Create the decisions file with its header when it is missing. True
    when it was created. An existing file is never written."""
    path = Path(path)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerow(DECISION_COLUMNS)
    return True


def _column(fields, *names):
    wanted = {n.replace("_", " ").strip().lower() for n in names}
    for name in fields or ():
        if (name or "").replace("_", " ").strip().lower() in wanted:
            return name
    return None


def read_decisions(path: Path) -> dict:
    """party id -> {decision, group, note}. Only those columns are read;
    any other column (a copy of the proposed file) is ignored."""
    path = Path(path)
    if not path.exists():
        return {}
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with open(path, encoding=encoding, newline="") as handle:
                rows = list(csv.DictReader(handle))
                fields = rows[0].keys() if rows else []
            break
        except UnicodeDecodeError:
            continue
    else:
        return {}
    pid, dec = _column(fields, "party id", "party_id"), _column(fields, "decision")
    grp, note = _column(fields, "group"), _column(fields, "note", "notes")
    out = {}
    for r in rows:
        try:
            party_id = int(str(r.get(pid) or "").strip())
        except ValueError:
            continue
        decision = (r.get(dec) or "").strip() if dec else ""
        group = (r.get(grp) or "").strip() if grp else ""
        text = (r.get(note) or "").strip() if note else ""
        if decision or group or text:
            out[party_id] = {"decision": decision.lower() if not decision.isdigit() else decision,
                             "group": group, "note": text}
    return out


# --------------------------------------------------------------- changes ----
def _manual_since(conn, party_id, column, after_id) -> bool:
    row = conn.execute("SELECT source FROM change_log WHERE table_name = 'party' AND row_id = ? "
                       "AND field = ? AND id > ? ORDER BY id DESC LIMIT 1",
                       (party_id, column, after_id)).fetchone()
    return row is not None and row["source"] != SOURCE


def changes_for(conn, party: Party, office: Office, owners: dict, names: dict) -> dict:
    """What linking this party to this office writes. For a party linked
    before, a field she edited by hand since is left as she wrote it."""
    out = {}
    country = names.get(office.code, "")
    fields = {"mfn_no": office.member_id, "mfn_member": 1, "full_name": office.company,
              "country": country, "city": office.city}
    current = {"mfn_no": party.mfn_no, "mfn_member": party.mfn_member, "full_name": party.full_name,
               "country": party.country, "city": party.city}
    for column, value in fields.items():
        if value in ("", None) or str(current[column] or "") == str(value):
            continue
        if party.linked_log and column in ("full_name", "country", "city") and \
                _manual_since(conn, party.id, column, party.linked_log):
            continue
        out[column] = value
    if office.code and not country:
        out["country_missing"] = office.code
    if office.fka and office.fka.casefold() not in {a.casefold() for a in party.aliases} \
            and office.fka.casefold() != party.key.casefold():
        out["alias"] = office.fka
    domains, held = [], []
    for domain in sorted({office.web_domain, office.email_domain} - {""}):
        owner = owners.get(domain)
        if owner is None:
            domains.append(domain)
        elif owner != party.id:
            held.append(domain)
    if domains:
        out["domains"] = domains
    if held:
        out["held"] = held
    contacts, seen = [], set(party.emails)
    for contact in office.contacts:
        email = contact["email"].strip().lower()
        if not email or "@" not in email or email in seen:
            continue
        seen.add(email)
        contacts.append(contact)
    if contacts:
        out["contacts"] = contacts
    return out


def describe(changes: dict, party: Party) -> str:
    parts = []
    if "mfn_no" in changes:
        parts.append("member no. {}".format(changes["mfn_no"]))
    if "mfn_member" in changes and "mfn_no" not in changes:
        parts.append("MFN member")
    if "full_name" in changes:
        parts.append("full name {} -> {}".format(party.full_name or "(empty)", changes["full_name"]))
    if "country" in changes:
        parts.append("country {} -> {}".format(party.country or "(empty)", changes["country"]))
    if "country_missing" in changes:
        parts.append("no OppList country has code {}: country left".format(changes["country_missing"]))
    if "city" in changes:
        parts.append("city {} -> {}".format(party.city or "(empty)", changes["city"]))
    if "alias" in changes:
        parts.append("alias {}".format(changes["alias"]))
    if changes.get("domains"):
        parts.append("+ domains {}".format(", ".join(changes["domains"])))
    if changes.get("contacts"):
        parts.append("+ {} contact{}".format(len(changes["contacts"]),
                                             "" if len(changes["contacts"]) == 1 else "s"))
    if changes.get("held"):
        parts.append("domain {} is another party's: not moved".format(", ".join(changes["held"])))
    return "; ".join(parts)


# ----------------------------------------------------------------- groups ---
def _drop_countries(words: list, country_words: list) -> list:
    text = " " + " ".join(words) + " "
    for phrase in country_words:
        text = text.replace(" {} ".format(phrase), " ")
    return text.split()


def hint_key(key, country_words, codes) -> str:
    """'Altura Logistics (El Salvador)' -> 'altura logistics': the key
    without its country, its legal form and a closing country code."""
    words = [w for w in _words(_no_parens(key)) if w not in LEGAL_WORDS and len(w) > 1]
    words = _drop_countries(words, country_words)
    while words and words[-1].upper() in codes:
        words.pop()
    return " ".join(words)


def _group_key(members: list, offices: list, directory: Directory, country_words) -> tuple:
    """(key, name) of a proposed group: the words the keys share at the
    start, trade words dropped from the end; else the shared web domain."""
    lists = [_drop_countries([w for w in _words(_no_parens(p.key)) if w not in LEGAL_WORDS],
                             country_words) for p in members]
    common = []
    for words in zip(*lists):
        if len(set(words)) != 1:
            break
        common.append(words[0])
    while common and common[-1] in TRADE_WORDS:
        common.pop()
    if common:
        first = re.findall(r"[^\W_]+", _no_parens(members[0].key))
        shown = [w for w in first if _plain(w) in common][:len(common)]
        return " ".join(common).upper(), " ".join(shown) or " ".join(common).upper()
    webs = Counter(directory.offices[m].web_domain for m in offices if directory.offices[m].web_domain)
    domain = webs.most_common(1)[0][0] if webs else ""
    if not domain:
        mails = Counter(d for m in offices for d in directory.domains(m))
        domain = mails.most_common(1)[0][0] if mails else ""
    stem = matching.domain_stem(domain)
    # 'xpdglobal' read back into the words a key or a company spells it with
    for name in [p.key for p in members] + [directory.offices[m].company for m in offices]:
        words = re.findall(r"[^\W_]+", _no_parens(name))
        for start in range(len(words)):
            for end in range(start + 1, len(words) + 1):
                if _plain("".join(words[start:end])) == stem.replace("-", ""):
                    return " ".join(words[start:end]).upper(), domain
    return stem.replace("-", " ").upper(), domain


def plan_groups(conn, rows: list, directory: Directory) -> dict:
    """Proposed groups, hints and conflicts. Sets row.group / group_evidence
    / hint; returns {'groups': {key: {...}}, 'conflicts': [...], 'hints': [...]}."""
    country_words = sorted({_plain(r["name"]) for r in conn.execute("SELECT name FROM country")
                            if r["name"] not in ("Unknown",)} |
                           {_plain(o.country) for o in directory.offices.values() if o.country} |
                           set(EXTRA_COUNTRY_WORDS), key=lambda w: -len(w))
    codes = {(r["iso2"] or "").upper() for r in conn.execute("SELECT iso2 FROM country")} - {""}
    existing = {r["key"]: r for r in conn.execute("SELECT * FROM party_group")}
    by_id = {r.party.id: r for r in rows}
    conflicts = []

    # company group -> parties linked (or proposed) to, or matching by
    # domain, its offices
    party_companies = defaultdict(set)
    for row in rows:
        if row.merge_with or row.decided_group.lower() == "none":
            continue
        offices = set(row.dom_matches)
        linked = row.target or (row.member if row.decision != "skip" else "")
        if linked in directory.offices:
            offices.add(linked)
        for member in offices:
            party_companies[row.party.id].add(directory.company[member])
    company_parties = defaultdict(list)
    for pid, companies in party_companies.items():
        for company in companies:
            company_parties[company].append(pid)
    proposed = {}
    for company, pids in sorted(company_parties.items()):
        if len(pids) < 2:
            continue
        twice = [p for p in pids if len([c for c in party_companies[p]
                                         if len(company_parties[c]) >= 2]) > 1]
        for pid in twice:
            conflicts.append("{} matches two MFN company groups: no group proposed for it".format(
                by_id[pid].party.key))
        pids = [p for p in pids if p not in twice]
        if len(pids) < 2:
            continue
        members = [by_id[p].party for p in sorted(pids)]
        offices = directory.company_offices[company]
        held = sorted({p.group_key for p in members if p.group_key})
        if len(held) == 1:
            key, name = held[0], existing[held[0]]["name"]
        else:
            key, name = _group_key(members, offices, directory, country_words)
        if not key:
            continue
        if key in proposed:
            key = "{} {}".format(key, company)
        heads = [m for m in offices if directory.offices[m].head()]
        notes = "MFN {}: {} office{} in {}".format(directory.date, len(offices),
                                                   "" if len(offices) == 1 else "s",
                                                   _countries(offices, directory))
        if len(heads) == 1:
            notes += "; head office {}, {}".format(directory.offices[heads[0]].company,
                                                    directory.offices[heads[0]].country)
        shared = sorted({d for m in offices for d in directory.domains(m) if len(directory.by_domain[d]) > 1})
        evidence = "{} Agent parties on one MFN company ({}); {}".format(
            len(members), "domains " + ", ".join(shared[:4]) if shared else "one office", notes)
        proposed[key] = {"key": key, "name": name, "notes": notes, "members": [],
                         "evidence": evidence, "existing": key in existing}
        for party in members:
            if party.group_key and party.group_key != key:
                conflicts.append("{} is in group {}; the MFN proposal is {}: left in {} (move it in "
                                 "Parties)".format(party.key, party.group_key, key, party.group_key))
                continue
            proposed[key]["members"].append(party.id)
            by_id[party.id].group = key
            by_id[party.id].group_evidence = evidence
    for key in [k for k, g in proposed.items() if len(g["members"]) < 2 and not g["existing"]]:
        for pid in proposed[key]["members"]:
            by_id[pid].group = by_id[pid].group_evidence = ""
        del proposed[key]

    # hints: never written without her decision
    member_keys = {}
    for key, group in proposed.items():
        for pid in group["members"]:
            member_keys.setdefault(hint_key(by_id[pid].party.key, country_words, codes), key)
    for row in rows:
        if row.party.group_key and row.party.group_key in existing:
            member_keys.setdefault(hint_key(row.party.key, country_words, codes), row.party.group_key)
    hints = []
    free = [r for r in rows if not r.group and not r.party.group_key and not r.merge_with]
    rest = []
    for row in free:
        key = hint_key(row.party.key, country_words, codes)
        if key and key in member_keys and any(w not in TRADE_WORDS for w in key.split()):
            row.hint = "join {}".format(member_keys[key])
            hints.append(("join", member_keys[key], [row.party.id]))
        else:
            rest.append(row)
    buckets = defaultdict(list)
    for row in rest:
        key = hint_key(row.party.key, country_words, codes)
        if key and any(w not in TRADE_WORDS for w in key.split()):
            buckets[key].append(row)
    for key, bucket in sorted(buckets.items()):
        if len(bucket) < 2 or len({r.party.country for r in bucket}) < 2:
            continue
        for row in bucket:
            row.hint = "new group {} ({})".format(key.upper(), ", ".join(
                "{} ({})".format(r.party.key, r.party.country) for r in bucket if r is not row))
        hints.append(("new", key.upper(), [r.party.id for r in bucket]))

    # her group decisions
    decided = {}
    for row in rows:
        value = row.decided_group.strip()
        if not value or value.lower() == "none":
            continue
        key = value.upper()
        if row.party.group_key and row.party.group_key != key:
            conflicts.append("{} is in group {}; decided {}: left in {} (move it in Parties)".format(
                row.party.key, row.party.group_key, key, row.party.group_key))
            continue
        decided[row.party.id] = key
    return {"groups": proposed, "conflicts": conflicts, "hints": hints, "decided": decided,
            "existing": existing}


# ------------------------------------------------------------------- plan ---
@dataclass
class Plan:
    rows: list
    directory: Directory
    groups: dict
    decisions: dict
    unknown_decisions: list
    country_names: dict            # iso2 -> OppList country name
    proposed_csv: Path | None = None
    decisions_csv: Path | None = None
    created_decisions: bool = False
    summary_path: Path | None = None

    def counts(self) -> Counter:
        return Counter(r.proposal for r in self.rows)

    def group_writes(self) -> dict:
        """party id -> group key that --apply sets."""
        out = {}
        by_id = {r.party.id: r for r in self.rows}
        for key, group in self.groups["groups"].items():
            for pid in group["members"]:
                if by_id[pid].decided_group.lower() == "none" or pid in self.groups["decided"]:
                    continue
                if by_id[pid].party.group_key != key:
                    out[pid] = key
        for pid, key in self.groups["decided"].items():
            if by_id[pid].party.group_key != key:
                out[pid] = key
        return out


def build(conn, directory: Directory, decisions: dict | None = None) -> Plan:
    names = {}
    for r in conn.execute("SELECT name, iso2 FROM country WHERE iso2 IS NOT NULL AND iso2 <> ''"):
        names.setdefault(r["iso2"].upper(), r["name"])
    owners = {r["domain"]: r["party_id"] for r in conn.execute("SELECT domain, party_id FROM party_domain")}
    parties = load_parties(conn)
    decisions = decisions or {}
    rows = []
    for party in parties:
        if party.linked_log:
            row = Row(party, proposal=LINKED, member=party.mfn_no)
            row.dom_matches = {m for d in party.domains for m in directory.by_domain.get(d, ())}
            row.why = "linked by an earlier --apply" if party.mfn_no in directory.offices else \
                "linked by an earlier --apply; member {} is not in MFN's file now".format(party.mfn_no)
        else:
            row = propose(party, directory)
        decided = decisions.get(party.id, {})
        row.decision, row.decided_group, row.note = (decided.get("decision", ""), decided.get("group", ""),
                                                     decided.get("note", ""))
        rows.append(row)
    known = {p.id for p in parties}
    unknown = sorted(pid for pid in decisions if pid not in known)

    # two or more parties on one office: merge? (a skipped row is out of it)
    by_office = defaultdict(list)
    for row in rows:
        office = row.decision if row.decision.isdigit() else row.member
        if office and row.decision != "skip":
            by_office[office].append(row)
    for member, same in by_office.items():
        if len(same) > 1:
            for row in same:
                row.merge_with = [r.party.id for r in same if r is not row]

    for row in rows:
        decision = row.decision
        if decision == "skip":
            continue
        if decision.isdigit():
            if decision not in directory.offices:
                row.trouble = "decided member {} is not in MFN's file".format(decision)
            else:
                row.target = decision
        elif decision == LINK:
            if row.member:
                row.target = row.member
            else:
                row.trouble = "decided link, but no office is proposed: give a member id"
        elif decision:
            row.trouble = "decision {!r} is not link, skip or a member id".format(decision)
        elif row.merge_with:
            continue
        elif row.proposal in (LINK, LINKED) and row.member in directory.offices:
            row.target = row.member
        if row.target:
            row.changes = changes_for(conn, row.party, directory.offices[row.target], owners, names)
    plan = Plan(rows, directory, {}, decisions, unknown, names)
    plan.groups = plan_groups(conn, rows, directory)
    return plan


# ----------------------------------------------------------------- output ---
PROPOSED_COLUMNS = [
    "party id", "decision", "group", "note", "key", "full name", "country", "city", "stage",
    "cases", "vendor requests", "proposal", "merge?", "why", "MFN member id", "MFN company",
    "MFN city", "MFN country", "what changes", "domains to add", "contacts to add",
    "other offices found", "country evidence", "group evidence", "group hint", "decision problem",
]


def _evidence(row: Row, directory: Directory) -> str:
    """Where MFN's country differs from OppList's: what else says where the
    party is."""
    office = directory.offices.get(row.target or row.member)
    if office is None or (row.party.iso and office.code == row.party.iso):
        return ""
    party = row.party
    parts = ["OppList city {}".format(party.city or "(empty)"),
             "domain codes {}".format(", ".join(sorted(party.own_codes())) or "none")]
    parts.append("cases {}".format(", ".join("{} {}".format(c, n) for c, n in
                                             party.case_countries.most_common()) or "none"))
    parts.append("vendor requests {}".format(", ".join("{} {}".format(c, n) for c, n in
                                                       party.vendor_countries.most_common()) or "none"))
    return "; ".join(parts)


def _proposed_rows(plan: Plan) -> list:
    out = []
    by_id = {r.party.id: r for r in plan.rows}
    for row in plan.rows:
        office = plan.directory.offices.get(row.target or row.member)
        party = row.party
        group = row.decided_group or row.group or party.group_key
        out.append({
            "party id": party.id, "decision": row.decision, "group": group, "note": row.note,
            "key": party.key, "full name": party.full_name, "country": party.country,
            "city": party.city, "stage": party.stage, "cases": len(party.cases),
            "vendor requests": party.vendor_requests, "proposal": row.proposal,
            "merge?": "; ".join(by_id[p].party.key for p in row.merge_with),
            "why": row.why, "MFN member id": office.member_id if office else "",
            "MFN company": office.company if office else "", "MFN city": office.city if office else "",
            "MFN country": office.country if office else "",
            "what changes": (describe(row.changes, party) or "nothing to change") if row.target else "",
            "domains to add": ", ".join(row.changes.get("domains", [])),
            "contacts to add": "; ".join(c["email"] for c in row.changes.get("contacts", [])),
            "other offices found": "; ".join(plan.directory.offices[m].label() for m in row.others[:8]),
            "country evidence": _evidence(row, plan.directory),
            "group evidence": row.group_evidence, "group hint": row.hint,
            "decision problem": row.trouble,
        })
    return out


def write_proposed(plan: Plan, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PROPOSED_COLUMNS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(_proposed_rows(plan))
    plan.proposed_csv = path
    return path


def _md(text) -> str:
    return str(text if text is not None else "").replace("|", "/").replace("\n", " ")


def summary(plan: Plan, today: str) -> list:
    d = plan.directory
    counts = plan.counts()
    by_id = {r.party.id: r for r in plan.rows}
    lines = ["# MFN link, {}".format(today), "",
             "Directory: `{}` ({} offices in {} countries, files of {}).".format(
                 d.folder, len(d.offices), len(d.codes), d.date),
             "Agent parties not merged: {}.".format(len(plan.rows)), "",
             "## Proposals", "", "| proposal | parties |", "|---|---:|"]
    for kind in KINDS:
        lines.append("| {} | {} |".format(kind, counts.get(kind, 0)))
    writes = [r for r in plan.rows if r.writes and r.changes]
    lines += ["", "Marked merge?: {} parties. On --apply: {} parties written, {} parties' group "
              "set.".format(sum(1 for r in plan.rows if r.merge_with), len(writes),
                            len(plan.group_writes())), ""]

    def case_text(party):
        return "{} case{}{}".format(len(party.cases), "" if len(party.cases) == 1 else "s",
                                    ": " + ", ".join(party.cases) if party.cases else "")

    moved = [r for r in plan.rows if _evidence(r, d)]
    lines += ["## MFN's country differs from OppList's ({})".format(len(moved)), "",
              "Proposed or decided. Written only for a decided row; its cases move between the "
              "month report's country sections.", ""]
    for row in moved:
        office = d.offices[row.target or row.member]
        lines.append("- **{}** (id {}): {} -> {} ({}), {}. Decision: {}. {}. Evidence: {}. {}.".format(
            row.party.key, row.party.id, row.party.country or "(empty)", office.country,
            office.label(), row.proposal, row.decision or "none", row.why,
            _evidence(row, d), case_text(row.party)))
    if not moved:
        lines.append("- none")

    foreign = [r for r in plan.rows if r.party.iso and (r.party.own_codes() - {r.party.iso})]
    lines += ["", "## Parties whose own domain carries another country's code ({})".format(len(foreign)), ""]
    for row in foreign:
        lines.append("- {} (id {}, {}): {}; proposal {}".format(
            row.party.key, row.party.id, row.party.country,
            ", ".join(x for x in row.party.domains if tld_code(x) and tld_code(x) != row.party.iso),
            row.proposal))
    if not foreign:
        lines.append("- none")

    groups = plan.groups
    lines += ["", "## Proposed groups ({}, {} parties)".format(
        len(groups["groups"]), sum(len(g["members"]) for g in groups["groups"].values())), ""]
    for key, group in groups["groups"].items():
        lines.append("- **{}**{}: {}. {}".format(
            key, " (existing)" if group["existing"] else "",
            ", ".join("{} ({})".format(by_id[p].party.key, by_id[p].party.country)
                      for p in group["members"]), group["evidence"]))
    joins = [h for h in groups["hints"] if h[0] == "join"]
    news = [h for h in groups["hints"] if h[0] == "new"]
    lines += ["", "## Group hints (written only when decided)", "",
              "To join a group ({}):".format(len(joins))]
    for _, key, pids in joins:
        lines.append("- {} ({}) -> {}".format(by_id[pids[0]].party.key, by_id[pids[0]].party.country, key))
    lines.append("")
    lines.append("New groups ({}, {} parties):".format(len(news), sum(len(h[2]) for h in news)))
    for _, key, pids in news:
        lines.append("- {}: {}".format(key, ", ".join("{} ({})".format(by_id[p].party.key, by_id[p].party.country)
                                                     for p in pids)))
    lines += ["", "## Group conflicts ({})".format(len(groups["conflicts"])), ""]
    lines += ["- " + c for c in groups["conflicts"]] or ["- none"]

    merges, seen = [], set()
    for row in plan.rows:
        if row.merge_with and row.party.id not in seen:
            same = [row.party.id] + row.merge_with
            seen.update(same)
            merges.append("- office {}: {}".format(
                d.offices[row.member].label() if row.member in d.offices else row.member,
                ", ".join("{} (id {})".format(by_id[p].party.key, p) for p in same)))
    lines += ["", "## merge? ({})".format(len(merges)), "",
              "Two or more parties on one office: nothing is written for them until decided "
              "(merge them in Parties first).", ""]
    lines += merges or ["- none"]

    gone = [r for r in plan.rows if r.party.mfn_member == 1 and r.proposal == NOT_IN_MFN]
    lines += ["", "## Marked MFN member today, not found ({})".format(len(gone)), ""]
    lines += ["- {} (id {}, {})".format(r.party.key, r.party.id, r.party.country) for r in gone] or ["- none"]

    decided = [r for r in plan.rows if r.decision or r.decided_group or r.note]
    lines += ["", "## Decisions read ({})".format(len(decided)), ""]
    for row in decided:
        lines.append("- {} (id {}): decision {}, group {}{}{}".format(
            row.party.key, row.party.id, row.decision or "-", row.decided_group or "-",
            "; note: " + row.note if row.note else "",
            "; **not done: {}**".format(row.trouble) if row.trouble else ""))
    for pid in plan.unknown_decisions:
        lines.append("- id {}: not an Agent party that is not merged; ignored".format(pid))
    if not decided and not plan.unknown_decisions:
        lines.append("- none")

    since = [r for r in plan.rows if r.proposal == LINKED]
    changed = [r for r in since if r.changes]
    lines += ["", "## Linked before: what MFN changed since ({} of {})".format(len(changed), len(since)), ""]
    for row in changed:
        lines.append("- {} (id {}, member {}): {}".format(row.party.key, row.party.id, row.party.mfn_no,
                                                          describe(row.changes, row.party)))
    for row in since:
        if row.party.mfn_no not in d.offices:
            lines.append("- {} (id {}): member {} is not in MFN's file now".format(
                row.party.key, row.party.id, row.party.mfn_no))
    if not changed and not any(r.party.mfn_no not in d.offices for r in since):
        lines.append("- nothing")
    return lines


def write_summary(plan: Plan, today: str, reports_dir=None) -> Path:
    folder = Path(reports_dir or paths.REPORTS_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "mfn-link-{}.md".format(today)
    path.write_text("\n".join(summary(plan, today)) + "\n", encoding="utf-8")
    plan.summary_path = path
    return path


def run(conn, directory_dir=None, out_dir=None, reports_dir=None, today=None) -> Plan:
    """The dry run: the proposal, the decisions file when missing, the
    proposed CSV and the summary. The database is only read."""
    today = today or dt.date.today().isoformat()
    out_dir = Path(out_dir or OUT_DIR)
    directory = load_directory(directory_dir, db.internal_domains(conn))
    decisions_csv = out_dir / DECISIONS_NAME
    created = ensure_decisions(decisions_csv)
    plan = build(conn, directory, read_decisions(decisions_csv))
    plan.decisions_csv, plan.created_decisions = decisions_csv, created
    write_proposed(plan, out_dir / PROPOSED_NAME)
    write_summary(plan, today, reports_dir)
    return plan


# ------------------------------------------------------------------ apply ---
@dataclass
class Applied:
    parties: int = 0
    fields: Counter = field(default_factory=Counter)
    domains: int = 0
    contacts: int = 0
    held: list = field(default_factory=list)
    groups_made: list = field(default_factory=list)
    grouped: int = 0
    troubles: list = field(default_factory=list)

    def lines(self) -> list:
        out = ["parties written     : {}".format(self.parties)]
        for column in ("mfn_no", "mfn_member", "full_name", "country", "city", "aliases"):
            out.append("  {:<18}: {}".format(column, self.fields.get(column, 0)))
        out += ["domains added       : {}".format(self.domains),
                "contacts added      : {}".format(self.contacts),
                "groups made         : {}{}".format(len(self.groups_made), " ({})".format(
                    ", ".join(self.groups_made)) if self.groups_made else ""),
                "parties grouped     : {}".format(self.grouped)]
        if self.held:
            out.append("domains of another party, not moved: {}".format(", ".join(sorted(set(self.held)))))
        for trouble in self.troubles:
            out.append("not done: {}".format(trouble))
        return out


def apply(conn, plan: Plan, today=None) -> Applied:
    """Write the plan in one transaction, change_log source mfn-link. The
    caller makes the restore point first."""
    today = today or dt.date.today().isoformat()
    repo = db.Repository(conn, source=SOURCE)
    done = Applied()
    try:
        for row in plan.rows:
            if row.trouble:
                done.troubles.append("{} (id {}): {}".format(row.party.key, row.party.id, row.trouble))
            if not row.writes or not row.changes:
                continue
            party, changes = row.party, row.changes
            office = plan.directory.offices[row.target]
            values = {c: changes[c] for c in ("mfn_no", "mfn_member", "full_name", "country", "city")
                      if c in changes}
            if "alias" in changes:
                values["aliases"] = json.dumps(party.aliases + [changes["alias"]], ensure_ascii=False)
            if values:
                repo.update("party", party.id, values)
                done.fields.update(values.keys())
            for domain in changes.get("domains", []):
                if repo.attach_domain(party.id, domain):
                    done.domains += 1
                else:
                    done.held.append(domain)
            done.held.extend(changes.get("held", []))
            for contact in changes.get("contacts", []):
                repo.insert("contact", {"party_id": party.id, "name": contact["name"] or None,
                                        "email": contact["email"].strip(),
                                        "title": contact["title"] or None,
                                        "notes": "MFN {}, {}".format(office.member_id, plan.directory.date)})
                done.contacts += 1
            repo.touch("party", party.id)
            done.parties += 1
        groups = plan.groups["groups"]
        ids = {}
        for pid, key in sorted(plan.group_writes().items()):
            if key not in ids:
                row = conn.execute("SELECT id FROM party_group WHERE key = ?", (key,)).fetchone()
                if row is not None:
                    ids[key] = row["id"]
                else:
                    group = groups.get(key)
                    ids[key] = repo.add_party_group(
                        key, group["name"] if group else key,
                        group["notes"] if group else "mfn-link {}: decided".format(today))
                    done.groups_made.append(key)
            repo.update("party", pid, {"group_id": ids[key]})
            done.grouped += 1
        repo.commit()
    except Exception:
        conn.rollback()
        raise
    return done
