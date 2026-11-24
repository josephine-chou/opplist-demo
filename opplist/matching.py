"""Finding the party a mail came from.

Order of evidence, best first:

1. the sender's SMTP domain against party_domain - an exact fact;
2. failing that, a name out of the subject against party.key and full_name,
   with the legal-form suffixes stripped;
3. several hits are all shown and the user picks;
4. nothing is guessed: no match means an empty field and 'Add party...'.

Internal senders are the common case here. Victor forwards an agent's mail
from his company address, so the sender domain is our own and tells us
nothing about who asked. When the sender is internal, the external domains on
To and CC are used instead.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

#: Domains that identify a mailbox, never a company.
FREE_MAIL_DOMAINS = {
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "qq.com",
    "163.com", "live.com", "aol.com", "icloud.com", "me.com",
}

#: Legal forms and trade words that are not part of the identifying name.
NAME_SUFFIXES = {
    "sa", "s.a", "sac", "s.a.c", "sas", "s.a.s", "sau", "s.a.u", "spa",
    "srl", "s.r.l", "ltda", "ltd", "limited", "llc", "inc", "co", "corp",
    "corporation", "company", "group", "grupo", "logistics", "logistica",
    "logistics.", "logisticas", "cargo", "cargas", "international",
    "internacional", "transportes", "transport", "freight", "forwarding",
    "forwarders", "shipping", "lines", "line", "global", "worldwide",
    "de", "cv", "s", "the",
}

#: Mailbox providers by their first label, whatever the country suffix:
#: 'yahoo.com.br' and 'hotmail.co.uk' name nobody's company either.
FREE_MAIL_STEMS = {
    "gmail", "googlemail", "hotmail", "outlook", "yahoo", "ymail", "rocketmail",
    "live", "msn", "aol", "icloud", "me", "mac", "protonmail", "proton", "pm",
    "qq", "163", "126", "sina", "sohu", "foxmail", "yandex", "gmx", "mail",
    "email", "zoho", "fastmail", "tutanota", "terra", "uol", "bol",
}

#: Labels that are part of the public suffix, not of the company:
#: 'desert-demo.com.sa' -> 'desert-demo'. The reserved names 'example' and
#: 'test' count as suffixes too: 'lumenbrands.example' -> 'lumenbrands'.
PUBLIC_SUFFIX_LABELS = {"com", "co", "net", "org", "gov", "edu", "ac", "ltd",
                        "mil", "or", "ne", "biz", "info", "gob", "nom", "ind",
                        "example", "test"}

#: Country code top-level domains, to the names in the country table.
TLD_COUNTRIES = {
    "sa": "Saudi Arabia", "ae": "UAE", "qa": "Qatar", "kw": "Kuwait", "om": "Oman",
    "bh": "Bahrain", "jo": "Jordan", "eg": "Egypt", "il": "Israel", "tr": "Turkey",
    "cl": "Chile", "br": "Brazil", "mx": "Mexico", "pe": "Peru", "co": "Colombia",
    "ar": "Argentina", "uy": "Uruguay", "py": "Paraguay", "bo": "Bolivia",
    "ec": "Ecuador", "ve": "Venezuela", "gt": "Guatemala", "hn": "Honduras",
    "sv": "El Salvador", "ni": "Nicaragua", "cr": "Costa Rica", "pa": "Panama",
    "do": "Dominican Republic", "pr": "Puerto Rico", "cu": "Cuba", "jm": "Jamaica",
    "ca": "Canada", "us": "USA", "es": "Spain", "pt": "Portugal", "de": "Germany",
    "fr": "France", "it": "Italy", "uk": "UK", "gb": "UK", "nl": "Netherlands",
    "be": "Belgium", "ch": "Switzerland", "at": "Austria", "ie": "Ireland",
    "se": "Sweden", "no": "Norway", "dk": "Denmark", "fi": "Finland", "pl": "Poland",
    "cz": "Czech Republic", "hu": "Hungary", "ro": "Romania", "gr": "Greece",
    "ru": "Russia", "ua": "Ukraine", "in": "India", "pk": "Pakistan",
    "bd": "Bangladesh", "lk": "Sri Lanka", "cn": "China", "hk": "Hong Kong",
    "tw": "Taiwan", "jp": "Japan", "kr": "South Korea", "sg": "Singapore",
    "my": "Malaysia", "th": "Thailand", "vn": "Vietnam", "id": "Indonesia",
    "ph": "Philippines", "au": "Australia", "nz": "New Zealand", "za": "South Africa",
    "ke": "Kenya", "ng": "Nigeria", "ma": "Morocco", "gh": "Ghana", "tz": "Tanzania",
}

MATCH_DOMAIN = "sender domain"
#: The domain's own name is a party's name: desert-demo.com.sa ~ 'Desert Demo'.
MATCH_DOMAIN_NAME = "domain matches party name"
MATCH_RECIPIENT_DOMAIN = "recipient domain"
MATCH_NAME = "name in subject"
#: The party wrote the mail that is quoted under this one. A domain hit like
#: any other, but on an older mail in the thread rather than on this one.
MATCH_QUOTED = "quoted mail"


@dataclass
class PartyMatch:
    party_id: int
    key: str
    how: str
    detail: str = ""

    @property
    def confident(self) -> bool:
        """A domain hit is a fact; a name hit is a suggestion.

        A quoted mail is a fact about the thread, not about this mail, so it
        is offered as a candidate and never fills the field on its own.
        """
        return self.how in (MATCH_DOMAIN, MATCH_RECIPIENT_DOMAIN)


@dataclass
class SenderInfo:
    """What the mail says about who sent it and who else was on it."""

    address: str = ""
    name: str = ""
    internal: bool = False
    recipients: list = field(default_factory=list)   # SMTP addresses

    @property
    def domain(self) -> str:
        return domain_of(self.address)

    def external_domains(self) -> list:
        out = []
        for address in self.recipients:
            found = domain_of(address)
            if found and found not in FREE_MAIL_DOMAINS and found not in out:
                out.append(found)
        return out


def domain_of(address) -> str:
    address = (address or "").strip().lower()
    if "@" not in address:
        return ""
    return address.rsplit("@", 1)[1].strip(" <>")


def domain_stem(domain) -> str:
    """The company part of a domain: 'desert-demo.com.sa' -> 'desert-demo',
    'mail.bluefin-usa.example' -> 'bluefin-usa', 'us.harborline.example' -> 'harborline'."""
    labels = [part for part in (domain or "").lower().split(".") if part]
    while labels and labels[0] in ("www", "mail", "smtp", "webmail", "email", "correo", "mx"):
        labels.pop(0)
    if len(labels) >= 2 and len(labels[-1]) == 2:
        labels.pop()                              # the country code
    if len(labels) >= 2 and labels[-1] in PUBLIC_SUFFIX_LABELS:
        labels.pop()                              # com / co / net ...
    return labels[-1] if labels else ""


def squash(text) -> str:
    """'Desert Demo S.A.' -> 'desertdemosa': letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", (text or "").casefold())


def is_free_mail(domain) -> bool:
    domain = (domain or "").lower()
    return domain in FREE_MAIL_DOMAINS or domain_stem(domain) in FREE_MAIL_STEMS


def country_for_tld(conn, domain) -> str:
    """'desert-demo.com.sa' -> 'Saudi Arabia' when that country is on file;
    '' for .com and the rest."""
    labels = [part for part in (domain or "").lower().split(".") if part]
    if len(labels) < 2 or len(labels[-1]) != 2:
        return ""
    name = TLD_COUNTRIES.get(labels[-1], "")
    if not name:
        return ""
    row = conn.execute("SELECT name FROM country WHERE lower(name) = ?", (name.lower(),)).fetchone()
    return row["name"] if row else ""


def normalise_name(text) -> list:
    """'3QV Group Chile SpA' -> ['3qv', 'chile']. Suffix words are dropped."""
    if not text:
        return []
    words = re.split(r"[^A-Za-z0-9]+", str(text).casefold())
    return [w for w in words if w and w not in NAME_SUFFIXES]


# ------------------------------------------------------------------ lookup --
def by_domain(conn: sqlite3.Connection, domain: str, how=MATCH_DOMAIN) -> list:
    domain = (domain or "").strip().lower()
    if not domain or domain in FREE_MAIL_DOMAINS:
        return []
    rows = conn.execute(
        "SELECT p.id, p.key FROM party_domain d JOIN party p ON p.id = d.party_id "
        "WHERE d.domain = ?", (domain,)).fetchall()
    return [PartyMatch(r["id"], r["key"], how, domain) for r in rows]


def _party_names(conn):
    """(id, key, [names to compare]) for every live party: key, full name
    and aliases."""
    for row in conn.execute(
            "SELECT id, key, full_name, aliases FROM party "
            "WHERE merged_into IS NULL AND placeholder = 0"):
        names = [row["key"], row["full_name"] or ""]
        names.extend(a.strip() for a in (row["aliases"] or "").split(",") if a.strip())
        yield row["id"], row["key"], names


def by_domain_stem(conn: sqlite3.Connection, domain: str) -> list:
    """Parties whose name, squashed, is the domain's own name: 'Desert Demo'
    for desert-demo.com.sa. Mailbox providers and very short stems never
    match. Several hits are all returned for the user to choose from."""
    if is_free_mail(domain):
        return []
    stem = squash(domain_stem(domain))
    if len(stem) < 3:
        return []
    out = []
    for party_id, key, names in _party_names(conn):
        squashed = [squash(n) for n in names]
        # 'desert-demo' ~ 'Desert Demo'; also 'Desert Demo SA' with the legal
        # form dropped
        stripped = [squash(" ".join(normalise_name(n))) for n in names]
        if stem in squashed or stem in stripped:
            out.append(PartyMatch(party_id, key, MATCH_DOMAIN_NAME,
                                  "{} ~ {}".format(domain_stem(domain), key)))
    return out


def similar_parties(conn: sqlite3.Connection, domain: str, limit=8) -> list:
    """Parties whose name shares the domain's name, for the 'is it one of
    these?' list above a New party form: [{'id', 'key', 'detail'}]."""
    if is_free_mail(domain):
        return []
    stem = squash(domain_stem(domain))
    if len(stem) < 3:
        return []
    # 'harborline-intl' also looks for 'harborline' on its own
    parts = [stem] + [squash(p) for p in domain_stem(domain).split("-") if len(squash(p)) >= 4]
    out = []
    for party_id, key, names in _party_names(conn):
        for name in names:
            squashed = squash(name)
            if not squashed:
                continue
            if any(p in squashed for p in parts) or (len(squashed) >= 4 and squashed in stem):
                out.append({"id": party_id, "key": key, "detail": "{} ~ {}".format(domain_stem(domain), name)})
                break
        if len(out) >= limit:
            break
    return out


#: RG1: a word more party names than this share says nothing about which
#: party a name is (counted over all party names, never over the
#: candidates); a word shorter than this says nothing
RARE_AT_MOST = 3
MIN_WORD = 3


def _and_words(text) -> list:
    """normalise_name with '&' read as 'and' ('Bolts & Nuts' ~ 'BOLTS AND NUTS')."""
    return normalise_name((text or "").replace("&", " and "))


def name_frequency(conn) -> Counter:
    """How many live parties carry each name word (key, full name, aliases)."""
    df = Counter()
    for _id, _key, names in _party_names(conn):
        words = set()
        for n in names:
            words.update(_and_words(n))
        df.update(words)
    return df


def by_name(conn: sqlite3.Connection, name: str) -> list:
    """Token equality, or one name's tokens contained in the other's. RG1:
    a whole-name match wins over a partial one, and a partial one needs a
    shared word of MIN_WORD letters or more that at most RARE_AT_MOST
    party names carry ('Supply Outfit' is not offered for '... STOCK'D
    SUPPLY')."""
    wanted = _and_words(name)
    if not wanted:
        return []
    wanted_set = set(wanted)
    df = None
    whole, partial = [], []
    for row in conn.execute("SELECT id, key, full_name FROM party"):
        fields = [(f, set(_and_words(row[f]))) for f in ("key", "full_name")]
        fields = [(f, tokens) for f, tokens in fields if tokens]
        same = next((f for f, tokens in fields if tokens == wanted_set), None)
        if same:
            whole.append(PartyMatch(row["id"], row["key"], MATCH_NAME, "{} ~ {}".format(name, row[same])))
            continue
        for field_name, tokens in fields:
            if tokens <= wanted_set or wanted_set <= tokens:
                df = df if df is not None else name_frequency(conn)
                if any(len(w) >= MIN_WORD and df[w] <= RARE_AT_MOST for w in tokens & wanted_set):
                    partial.append(PartyMatch(row["id"], row["key"], MATCH_NAME,
                                              "{} ~ {}".format(name, row[field_name])))
                    break
    return whole or partial


def find(conn: sqlite3.Connection, sender: SenderInfo, names=(),
         quoted_addresses=(), skip_recipients=False) -> list:
    """Every candidate, best evidence first, no duplicates.

    `skip_recipients` is for a mail we sent out to a vendor: the addresses on
    To and CC are the vendors we asked, so reading a requester off them would
    put the carrier in the requester field.
    """
    matches = []
    seen = set()

    def add(found):
        for match in found:
            if match.party_id not in seen:
                seen.add(match.party_id)
                matches.append(match)

    if not sender.internal:
        add(by_domain(conn, sender.domain))
    if not matches and not skip_recipients:
        # Victor's forward: the agent is on To or CC, not in From.
        for domain in sender.external_domains():
            add(by_domain(conn, domain, MATCH_RECIPIENT_DOMAIN))
    # The mail quoted underneath: whoever wrote it is in this conversation.
    for address in quoted_addresses:
        add(by_domain(conn, domain_of(address), MATCH_QUOTED))
    for name in names:
        add(by_name(conn, name))
    return matches


def remember_domain(repo, party_id: int, domain: str) -> str:
    """Attach a domain to a party unless somebody already owns it or it is
    a mailbox provider. The domain when it was written, else ''."""
    domain = (domain or "").strip().lower()
    if not domain or is_free_mail(domain):
        return ""
    return repo.attach_domain(party_id, domain)       # never an internal one (13g)


def learn_domain(repo, party_id: int, sender: SenderInfo) -> str:
    """Remember the domain so the next mail matches by itself."""
    domain = sender.domain if not sender.internal else ""
    if not domain:
        externals = sender.external_domains()
        domain = externals[0] if externals else ""
    if not domain or domain in FREE_MAIL_DOMAINS:
        return ""
    return repo.attach_domain(party_id, domain)       # never an internal one (13g)
