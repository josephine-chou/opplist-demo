"""Possible duplicate parties (v8): two rows that look like one
company, offered in pairs for a person to merge.

Two ways a pair is found: they share a mail domain (one party's domain is
the other's contact's domain - party_domain itself allows a domain only
once), or their names, with the company suffixes stripped, are the same or
one is contained in the other with at least one real word in common. The
list is read-only; merging is a decision, taken on the page.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import matching

#: A shared word shorter than this says nothing ('S.A.', 'de').
MIN_COMMON = 4
#: Words of the trade that survive the suffix list and still name nobody:
#: 'GLOBAL LOGISTICS SOLUTIONS LLC CO.' is 'solutions' once stripped,
#: and 'Rapid Cargo SA de CV' is 'fast'. Neither is inside every other name.
GENERIC_WORDS = frozenset((
    "solution", "solutions", "shipping", "express", "global", "service",
    "services", "fast", "world", "worldwide", "green", "blue", "cargas",
    "trading", "trade", "total", "smart", "easy", "one", "new", "first",
    "air", "sea", "ocean", "land", "line", "lines", "company", "cia", "co",
    "srl", "sas", "sac", "spa", "ltda", "ltd", "inc", "llc", "the", "and",
    "del", "de", "la", "las", "los", "y", "e", "agencia", "agency", "agent",
    "agents", "agenciamento", "forwarders", "forwarder", "forwarding",
    "sociedad", "anonima", "limitada", "integral", "internacionales",
))
#: Regions and country-like words that are not in the country table.
REGION_WORDS = frozenset((
    "america", "americas", "american", "latin", "latam", "andina", "andean",
    "pacific", "atlantic", "caribbean", "caribe", "europe", "european",
    "asia", "asian", "sur", "norte", "south", "north", "central", "usa",
))


def _telling(common, countries) -> bool:
    """Do the words two names share actually name a company?"""
    words = [w for w in common
             if len(w) >= MIN_COMMON and w not in countries and w not in GENERIC_WORDS]
    if not words:
        return False
    # one lone word has to be a real name ('seavio', 'vektor'), not 'fast'
    return len(words) >= 2 or len(words[0]) >= 5


@dataclass
class Pair:
    a_id: int
    a_key: str
    b_id: int
    b_key: str
    why: str

    @property
    def key(self) -> str:
        return "{}-{}".format(min(self.a_id, self.b_id), max(self.a_id, self.b_id))


def _domains_by_party(conn) -> dict:
    out = {}
    for row in conn.execute("SELECT party_id, domain FROM party_domain"):
        out.setdefault(row["party_id"], set()).add(row["domain"].lower())
    for row in conn.execute("SELECT party_id, email FROM contact WHERE email LIKE '%@%'"):
        domain = matching.domain_of(row["email"])
        if domain and domain not in matching.FREE_MAIL_DOMAINS:
            out.setdefault(row["party_id"], set()).add(domain)
    return out


#: Placeholder keys that are bookkeeping, never a company to merge.
RESERVED_KEYS = frozenset(("(tbc)", "unknown", "colin"))


def find_duplicates(conn: sqlite3.Connection, limit: int = 300) -> list:
    """Pairs of live parties (not merged), most certain first. A placeholder
    is in (a case parked on 'Kessler Morrow Seaway Chile' is the same company
    as 'Kessler Morrow Seaway'), the reserved keys are not; a pair with a
    placeholder says so in its reason."""
    parties = [r for r in conn.execute(
        "SELECT id, key, full_name, placeholder FROM party "
        "WHERE merged_into IS NULL ORDER BY key COLLATE NOCASE")
        if not (r["placeholder"] and (r["key"] or "").strip().casefold() in RESERVED_KEYS)]
    domains = _domains_by_party(conn)
    pairs, seen = [], set()

    def add(a, b, why):
        if a["placeholder"] or b["placeholder"]:
            why += " (placeholder: {})".format(
                ", ".join(p["key"] for p in (a, b) if p["placeholder"]))
        pair = Pair(a["id"], a["key"], b["id"], b["key"], why)
        if pair.key not in seen:
            seen.add(pair.key)
            pairs.append(pair)

    # 1. a domain in common
    by_domain = {}
    for party in parties:
        for domain in domains.get(party["id"], ()):
            by_domain.setdefault(domain, []).append(party)
    for domain, owners in sorted(by_domain.items()):
        for index, a in enumerate(owners):
            for b in owners[index + 1:]:
                add(a, b, "same domain {}".format(domain))

    # 2. the same name once the suffixes are gone, or one inside the other.
    # A country's name is not a word in common: 'Chile Haulage' is not inside
    # 'Tolvex Chile SpA' (the agent-report review file was full of those).
    countries = set(REGION_WORDS)
    for row in conn.execute("SELECT name FROM country"):
        countries.update(matching.normalise_name(row["name"]))
    tokens = {}
    for party in parties:
        words = set(matching.normalise_name(party["key"]))
        if party["full_name"]:
            words |= set(matching.normalise_name(party["full_name"]))
        tokens[party["id"]] = words
    for index, a in enumerate(parties):
        words_a = tokens[a["id"]]
        if not words_a:
            continue
        for b in parties[index + 1:]:
            words_b = tokens[b["id"]]
            if not words_b:
                continue
            if words_a == words_b:
                add(a, b, "same name after the suffixes")
            elif (words_a <= words_b or words_b <= words_a) and _telling(
                    words_a & words_b, countries):
                small, big = (a, b) if words_a <= words_b else (b, a)
                add(a, b, "'{}' is contained in '{}'".format(small["key"], big["key"]))
        if len(pairs) >= limit:
            break
    return pairs[:limit]
