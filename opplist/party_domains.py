"""Mail domains for the parties that have none (2026-09-15).

A party without a `party_domain` row is matched by name only, so the next
mail from them lands on '+ New' again. This finds candidates in the mail
archive for every such party that a case or a vendor request actually uses,
writes them to a CSV for a person to look at, and - only on --apply - adds
the ones marked add-domain. Existing domains are never overwritten and no
row is ever deleted.

Three kinds of evidence, each a line of its own in the CSV:

- thread: the case's conversation in the archive (enquiry.conversation_id,
  or the subject without RE / FW against conversation_topic). Outside
  senders count; for a mail we sent, the outside people on To / Cc.
- name: an archive domain whose stem (TLD and hyphens gone) contains a
  telling word of the party's name - five letters or more once the trade
  words, countries and cities are dropped - or the whole name squashed.
- duplicate-of: the candidate already belongs to another party, so this is
  probably the same company twice; the action becomes merge-into and the
  CSV's keep_id says which row survives (blank = do nothing).
"""

from __future__ import annotations

import csv
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import branding, db, mail_archive, matching, parsing, party_dupes, paths

PROPOSED_CSV = paths.DATA_DIR / "party-domains-proposed.csv"
COLUMNS = ["party_id", "party_key", "type", "country", "enquiries", "proposed_domain",
           "evidence", "hits", "sample_address", "conflict_party_id", "conflict_party_key",
           "action", "keep_id"]
#: Keys that are bookkeeping, not companies: never given a domain.
RESERVED_KEYS = frozenset(("(tbc)", "unknown", "colin"))
#: Our own mail: never a candidate.
INTERNAL_DOMAINS = frozenset((branding.INTERNAL_DOMAIN,))
#: Words of the trade that name nobody once the name is split.
TRADE_WORDS = frozenset((
    "logistics", "logistic", "logistica", "logisticas", "cargo", "cargas", "freight",
    "global", "international", "internacional", "shipping", "group", "grupo", "express",
    "service", "services", "servicios", "transport", "transportes", "transportation",
    "forwarding", "forwarders", "forwarder", "solutions", "solution", "worldwide",
    "trading", "company", "corporation", "limited", "imports", "import", "exports",
    "export", "consulting", "supply", "chain", "lines", "line", "agencia", "agency",
    # fragments of the trade that are whole words in some names ('TRANS
    # PARENT', 'Marine Cargo'): five letters, and inside half the archive
    "trans", "marine", "marino", "maritime", "maritima", "world", "united", "general",
    "pacific", "atlantic", "ocean", "airline", "airlines", "airways", "aviation",
    "carrier", "carriers", "trucking", "customs", "broker", "brokers", "delivery",
)) | frozenset(matching.NAME_SUFFIXES) | party_dupes.GENERIC_WORDS
#: A telling word is at least this long.
MIN_WORD = 5


@dataclass
class Candidate:
    party_id: int
    party_key: str
    type: str
    country: str
    enquiries: int
    proposed_domain: str
    evidence: str                      # thread | name | duplicate-of
    hits: int
    sample_address: str
    conflict_party_id: int | None = None
    conflict_party_key: str = ""
    action: str = "add-domain"         # add-domain | merge-into
    keep_id: str = ""

    def row(self) -> list:
        return [self.party_id, self.party_key, self.type, self.country, self.enquiries,
                self.proposed_domain, self.evidence, self.hits, self.sample_address,
                self.conflict_party_id or "", self.conflict_party_key, self.action,
                self.keep_id]


@dataclass
class Report:
    parties: int = 0
    with_candidates: int = 0
    without: int = 0
    candidates: int = 0
    merges: int = 0
    shared: int = 0
    csv_path: Path | None = None
    without_keys: list = field(default_factory=list)

    def lines(self) -> list:
        out = [
            "parties without a domain, in use : {}".format(self.parties),
            "  with a candidate               : {}".format(self.with_candidates),
            "  nothing found                  : {}".format(self.without),
            "candidate rows                   : {} ({} merge-into, {} proposed for more "
            "than one party - delete the wrong row before --apply)".format(
                self.candidates, self.merges, self.shared),
        ]
        if self.csv_path:
            out.append("written to                       : {}".format(self.csv_path))
        if self.without_keys:
            out.append("")
            out.append("nothing found for: " + ", ".join(self.without_keys[:40]))
            if len(self.without_keys) > 40:
                out.append("  ... and {} more".format(len(self.without_keys) - 40))
        return out


# ---------------------------------------------------------------- targets --
def target_parties(conn) -> list:
    """Live parties with no domain that an enquiry or a vendor request names;
    Our offices and the reserved keys excluded."""
    rows = conn.execute("""
        SELECT p.id, p.key, p.full_name, p.type, p.country, p.placeholder,
               (SELECT COUNT(*) FROM enquiry e WHERE e.requester_party_id = p.id) AS enquiries,
               (SELECT COUNT(*) FROM vendor_request v WHERE v.vendor_party_id = p.id) AS asked
        FROM party p
        WHERE p.merged_into IS NULL AND p.type <> 'Office'
          AND NOT EXISTS (SELECT 1 FROM party_domain d WHERE d.party_id = p.id)
          AND (EXISTS (SELECT 1 FROM enquiry e WHERE e.requester_party_id = p.id)
               OR EXISTS (SELECT 1 FROM vendor_request v WHERE v.vendor_party_id = p.id))
        ORDER BY enquiries + asked DESC, p.key COLLATE NOCASE""").fetchall()
    return [r for r in rows if (r["key"] or "").strip().casefold() not in RESERVED_KEYS]


def _internal(conn) -> set:
    out = set(INTERNAL_DOMAINS)
    out.update(str(d).strip().lower() for d in db.get_setting_list(conn, "internal_domains")
               if str(d).strip())
    return out


def _outside(domain: str, internal: set) -> bool:
    return bool(domain) and domain not in internal and not matching.is_free_mail(domain)


# ---------------------------------------------------------- archive index --
@dataclass
class ArchiveIndex:
    """What the archive says, read once: the outside domains per
    conversation (id and topic) and over the whole archive."""

    by_conversation: dict = field(default_factory=lambda: defaultdict(Counter))
    by_topic: dict = field(default_factory=lambda: defaultdict(Counter))
    all_domains: Counter = field(default_factory=Counter)
    sample: dict = field(default_factory=dict)          # domain -> an address
    conversation_sample: dict = field(default_factory=dict)   # (key, domain) -> address

    def note(self, bucket, key, domain, address) -> None:
        bucket[key][domain] += 1
        self.conversation_sample.setdefault((key, domain), address)


def read_archive(archive_conn, internal: set) -> ArchiveIndex:
    import json

    index = ArchiveIndex()
    for row in archive_conn.execute(
            "SELECT conversation_id, conversation_topic, subject, sender_address, "
            "sender_internal, to_addresses, cc_addresses FROM message"):
        addresses = []
        if row["sender_internal"]:
            for raw in (row["to_addresses"], row["cc_addresses"]):
                try:
                    addresses.extend(json.loads(raw or "[]"))
                except ValueError:
                    pass
        else:
            addresses.append(row["sender_address"] or "")
        topic = parsing.clean_subject(row["conversation_topic"] or row["subject"] or "").casefold()
        for address in addresses:
            domain = matching.domain_of(address)
            if not _outside(domain, internal):
                continue
            index.all_domains[domain] += 1
            index.sample.setdefault(domain, address)
            if row["conversation_id"]:
                index.note(index.by_conversation, row["conversation_id"], domain, address)
            if topic:
                index.note(index.by_topic, topic, domain, address)
    return index


# ------------------------------------------------------------ name words --
def _place_words(conn) -> set:
    words = set(party_dupes.REGION_WORDS)
    for row in conn.execute("SELECT name FROM country"):
        words.update(matching.normalise_name(row["name"]))
    for row in conn.execute("SELECT key, official_name, aliases FROM location"):
        for text in (row["key"], row["official_name"], row["aliases"]):
            words.update(matching.normalise_name(text))
    return words


def telling_words(key: str, full_name: str, places: set) -> tuple:
    """(words of five letters or more that name the company, the whole name
    squashed when it is five letters or more)."""
    words = []
    for text in (key, full_name):
        for word in matching.normalise_name(text):
            if len(word) >= MIN_WORD and word not in TRADE_WORDS and word not in places \
                    and word.isalpha() and word not in words:
                words.append(word)
    whole = matching.squash(" ".join(
        w for w in matching.normalise_name(key) if w not in TRADE_WORDS and w not in places))
    return words, whole if len(whole) >= MIN_WORD else ""


def _stem(domain: str) -> str:
    return matching.domain_stem(domain).replace("-", "").replace("_", "")


# ------------------------------------------------------------- proposing --
def _case_threads(conn, party_id) -> list:
    """(conversation_id, cleaned subject) for every case the party is on."""
    rows = conn.execute("""
        SELECT e.conversation_id, e.source_subject FROM enquiry e
        WHERE e.requester_party_id = ?
           OR e.id IN (SELECT enquiry_id FROM vendor_request WHERE vendor_party_id = ?)
        """, (party_id, party_id)).fetchall()
    return [((r["conversation_id"] or "").strip(),
             parsing.clean_subject(r["source_subject"] or "").casefold()) for r in rows]


def propose(conn, archive_conn) -> tuple:
    """(candidates, report). Reads both databases, writes nothing."""
    internal = _internal(conn)
    index = read_archive(archive_conn, internal)
    places = _place_words(conn)
    owners = {r["domain"].lower(): r["party_id"]
              for r in conn.execute("SELECT domain, party_id FROM party_domain")}
    keys = {r["id"]: r["key"] for r in conn.execute("SELECT id, key FROM party")}
    stems = {domain: _stem(domain) for domain in index.all_domains}

    report = Report()
    out = []
    for party in target_parties(conn):
        report.parties += 1
        found = {}                       # domain -> Candidate (best evidence wins)

        def offer(domain, evidence, hits, address):
            if domain in found:
                found[domain].hits += hits
                return
            found[domain] = Candidate(
                party["id"], party["key"], party["type"], party["country"] or "",
                party["enquiries"], domain, evidence, hits, address)

        # a. the case threads
        for conversation_id, topic in _case_threads(conn, party["id"]):
            hits = Counter()
            if conversation_id and conversation_id in index.by_conversation:
                hits = index.by_conversation[conversation_id]
                key = conversation_id
            elif topic and topic in index.by_topic:
                hits = index.by_topic[topic]
                key = topic
            for domain, count in hits.items():
                offer(domain, "thread", count, index.conversation_sample.get((key, domain), ""))
        # b. the name inside a domain stem
        words, whole = telling_words(party["key"], party["full_name"] or "", places)
        if words or whole:
            for domain, stem in stems.items():
                if domain in found:
                    continue
                if (whole and whole in stem) or any(w in stem for w in words):
                    offer(domain, "name", index.all_domains[domain], index.sample.get(domain, ""))
        # c. already somebody else's: the same company twice
        for candidate in found.values():
            owner = owners.get(candidate.proposed_domain)
            if owner and owner != party["id"]:
                candidate.evidence = "duplicate-of"
                candidate.action = "merge-into"
                candidate.conflict_party_id = owner
                candidate.conflict_party_key = keys.get(owner, "")
        rows = sorted(found.values(), key=lambda c: (-c.hits, c.proposed_domain))
        if rows:
            report.with_candidates += 1
        else:
            report.without += 1
            report.without_keys.append(party["key"])
        out.extend(rows)
    # The same domain offered to two parties (a case's thread carries both
    # the requester and the vendor we asked): say so on both rows. apply()
    # leaves such rows alone until one of them is deleted from the CSV.
    proposed_to = defaultdict(list)
    for candidate in out:
        if candidate.action == "add-domain":
            proposed_to[candidate.proposed_domain].append(candidate.party_key)
    for candidate in out:
        others = [k for k in proposed_to.get(candidate.proposed_domain, []) if k != candidate.party_key]
        if others and candidate.action == "add-domain":
            candidate.evidence += " (also proposed for {})".format(", ".join(others))
    report.candidates = len(out)
    report.merges = sum(1 for c in out if c.action == "merge-into")
    report.shared = sum(1 for c in out if "also proposed for" in c.evidence)
    return out, report


def write_csv(candidates, path=None) -> Path:
    path = Path(path or PROPOSED_CSV)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for candidate in candidates:
            writer.writerow(candidate.row())
    return path


def run(conn, archive_path=None, csv_path=None) -> Report:
    """The dry run: propose, write the CSV, report. Nothing in opplist.db changes."""
    with mail_archive.ArchiveSearch(archive_path) as search:
        candidates, report = propose(conn, search._conn)
    report.csv_path = write_csv(candidates, csv_path)
    return report


# ---------------------------------------------------------------- applying --
@dataclass
class ApplyReport:
    rows: int = 0
    added: int = 0
    skipped_existing: list = field(default_factory=list)
    skipped_shared: list = field(default_factory=list)
    merged: list = field(default_factory=list)
    merge_skipped: int = 0
    bad: list = field(default_factory=list)

    def lines(self) -> list:
        out = [
            "rows in the CSV        : {}".format(self.rows),
            "domains added          : {}".format(self.added),
            "already on file (kept) : {}".format(len(self.skipped_existing)),
            "proposed to several    : {} (delete the wrong row, run again)".format(
                len(self.skipped_shared)),
            "parties merged         : {}".format(len(self.merged)),
            "merge rows left alone  : {} (keep_id blank)".format(self.merge_skipped),
        ]
        out.extend("  = {}".format(line) for line in self.skipped_existing)
        out.extend("  ~ {}".format(line) for line in self.skipped_shared)
        out.extend("  > {}".format(line) for line in self.merged)
        if self.bad:
            out.append("rows not understood    : {}".format(len(self.bad)))
            out.extend("  ? {}".format(line) for line in self.bad[:20])
        return out


def apply(conn: sqlite3.Connection, csv_path=None, today=None) -> ApplyReport:
    """Read the CSV back and do what its action column says. A domain that
    is already on file - anyone's - is left exactly as it is."""
    repo = db.Repository(conn, source="import")
    report = ApplyReport()
    path = Path(csv_path or PROPOSED_CSV)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    wanted_by = defaultdict(set)
    for row in rows:
        if (row.get("action") or "").strip() == "add-domain":
            wanted_by[(row.get("proposed_domain") or "").strip().lower()].add(
                (row.get("party_id") or "").strip())
    for row in rows:
        report.rows += 1
        action = (row.get("action") or "").strip()
        domain = (row.get("proposed_domain") or "").strip().lower()
        if action == "add-domain" and len(wanted_by.get(domain, ())) > 1:
            report.skipped_shared.append("{} is proposed for {} parties (row for {})".format(
                domain, len(wanted_by[domain]), row.get("party_key")))
            continue
        try:
            party_id = int(row.get("party_id") or 0)
        except ValueError:
            party_id = 0
        if not party_id:
            report.bad.append(str(dict(row))[:120])
            continue
        if action == "add-domain":
            if not domain:
                report.bad.append("{}: no domain".format(row.get("party_key")))
                continue
            owner = conn.execute("SELECT d.party_id, p.key FROM party_domain d "
                                 "JOIN party p ON p.id = d.party_id WHERE d.domain = ?",
                                 (domain,)).fetchone()
            if owner is not None:
                report.skipped_existing.append("{} is {}'s already (wanted for {})".format(
                    domain, owner["key"], row.get("party_key")))
                continue
            if db.is_internal_domain(conn, domain):
                report.bad.append("{}: {} is our own domain".format(row.get("party_key"), domain))
                continue
            repo.insert("party_domain", {"domain": domain, "party_id": party_id})
            report.added += 1
        elif action == "merge-into":
            keep = (row.get("keep_id") or "").strip()
            try:
                conflict_id = int(row.get("conflict_party_id") or 0)
            except ValueError:
                conflict_id = 0
            if not keep:
                report.merge_skipped += 1
                continue
            try:
                keep_id = int(keep)
            except ValueError:
                report.bad.append("{}: keep_id '{}' is not a number".format(row.get("party_key"), keep))
                continue
            if keep_id not in (party_id, conflict_id) or not conflict_id:
                report.bad.append("{}: keep_id {} is neither {} nor {}".format(
                    row.get("party_key"), keep_id, party_id, conflict_id))
                continue
            drop_id = conflict_id if keep_id == party_id else party_id
            try:
                repo.merge_party(keep_id, drop_id, today=today)
            except ValueError as exc:
                report.bad.append("{}: {}".format(row.get("party_key"), exc))
                continue
            report.merged.append("{} -> {}".format(drop_id, keep_id))
        else:
            report.bad.append("{}: action '{}'".format(row.get("party_key"), action))
    repo.commit()
    return report
