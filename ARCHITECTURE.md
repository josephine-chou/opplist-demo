# Architecture

OppList is a single-user, local web app: a Python package, a SQLite database and
a Flask front end with server-rendered Jinja2 pages, one stylesheet and a small
amount of plain JavaScript. There is no build step and no front-end framework.
The server binds to `127.0.0.1` only.

The team site is the second half: plain HTML, CSS and ES modules that the rest
of the team opens in a browser. It reads documents OppList pushes and writes
actions that OppList takes in. In this copy the document store is a folder on
disk and the site runs on a snapshot (see "The team site" below), so nothing
opens a network connection.

```
browser ──HTTP──▶ Flask app (opplist/web) ──▶ Repository (opplist/db.py) ──▶ SQLite (opplist.db)
                        │                                   ▲
                        ├──▶ read-only views (v_enquiry, …) ─┘
                        ├──▶ mail archive (mail-archive/mail.db, read-only for pages)
                        └──▶ Outlook bridge (pywin32, optional, read-only)
CLI (python -m opplist …) ──▶ the same modules

sync run (every few minutes) ──▶ take in the site's actions ──▶ Repository
                             └─▶ push the lists and cases ──▶ document store ◀── team site (site/)
```

## Modules

| Area | Modules | What they do |
|---|---|---|
| Storage | `db.py`, `schema.sql`, `schema_migrations.py`, `backup.py` | Connection, schema, the `Repository` write layer, upgrades, restore points |
| Domain rules | `status.py`, `tracking.py`, `followup.py`, `cargo.py`, `shipments.py`, `case_notes.py` | Status machine, the Tracking rules, the follow-up ladder, cargo lines, shipment milestones and maps |
| Reading mail | `parsing.py`, `intake.py`, `matching.py`, `dedupe.py`, `outlook_bridge.py`, `mail_archive.py`, `case_mail.py` | Subject and body parsing, turning a selected mail into a pre-filled form, finding the party, duplicate detection, the Outlook bridge and the local mail archive |
| Suggestions | `propose.py`, `detect_followups.py`, `suggestions.py`, `party_domains.py` | Dry-run scans of the archive that propose status changes, follow-ups, re-quotes, shipment starts and party domains, always as suggestions a person accepts |
| Output | `export_xlsx.py`, `export_csv.py`, `autoexport.py`, `report.py`, `monthly.py`, `monthly_export.py`, `dashboard.py`, `export_analysis.py` | Workbook and CSV exports in fixed formats, the month report and the dashboard (inline SVG drawn in Python) |
| Checks | `checks.py`, `verify.py`, `leakcheck.py` | Consistency checks that list and never fix; migration verification (against the legacy workbook, or on a demo database against the demo data rebuilt from its seed and date); the portfolio leak check |
| Waiting and work lists | `wait.py`, `assign_list.py`, `ack.py`, `ack_questions.py` | Waiting for requester, the Assign List, acknowledgements and the questions we owe an answer |
| Vendors' rates and quotes | `site_rates.py`, `site_quotes.py`, `read_quote.py`, `read_quote_recorded.py`, `ratecard_sync.py` | Rates kept as versions, the quote card's quotes, Read quote, the push of vendors' costs to the Rate Card |
| The team site | `firebase_sync.py`, `firebase_pull.py`, `firebase_reply.py`, `firebase_vendors.py`, `site_close.py`, `site_edit.py`, `site_read.py`, `local_store.py` | The push of lists and cases, the take-in of actions and their answers, the Won or Lost that waits for the administrator, edits of cargo and routes, the document store of this copy |
| Configuration | `branding.py`, `paths.py`, `web/text.py` | Company facts with neutral defaults, file locations, every interface string |
| Demo | `demo.py`, `demo_world.py`, `demo_showcase.py`, `site_snapshot.py`, `site_snapshot_extras.py` | Deterministic synthetic data set and mail archive, the site's work done through the take-in, and the site's snapshot |
| One-off tools | `migrate_xlsx.py`, `migrate_master.py`, `import_*.py`, `fix_*.py`, `retire_sourcing.py`, `withdraw_requote.py`, `coordinates.py` | Idempotent data moves, each dry-run first and each behind a restore point |

## Data model

The core tables:

- **enquiry**: one lane of one request. `opp_no` (`OPP-0001`) is never renumbered
  or reused; gaps stay. `group_id` ties the lanes of one request together.
  `requester_party_id` is who asked; `end_customer` is whose cargo it is.
- **status_event**: every status change, note and follow-up, with a date the user
  confirmed. The current status, the quote date, the close date and "days to
  quote" are all derived from these events.
- **enquiry_route**, **enquiry_cargo**: a case's routes (one request can ask for
  several destinations) and its cargo lines (pieces, sizes, weight, volume,
  containers, HS code, cargo value). The case's totals are computed from them.
- **vendor_request**: one time we asked a vendor for a rate: asked on, replied on,
  their reference, whether the rate was used.
- **vendor_rate**, **vendor_rate_line**: a vendor's rates for one route of a case,
  one row per version and option, never overwritten.
- **quote**, **quote_line**: a quote made with the quote card, its lines, and for
  the people allowed to see them its costs, markup, profit and margin. Each is tied
  to the status event (Quoted or re-quote) it belongs to.
- **quote_read**, **quote_read_draft**: every Read quote reading (what was sent,
  what came back, tokens and cost) and the drafts it made.
- **site_action**: every action taken in from the team site, applied or refused,
  with the message the person got back.
- **party**, **contact**, **party_domain**, **party_event**: agents, our own
  offices, direct customers and carriers in one table; contacts one per person;
  mail domains used to recognise a sender; relationship activity.
- **master_location**, **master_location_alias**, **master_area**: places with
  UN/LOCODE and IATA codes, aliases, a parent market city and coordinates.
- **shipment**, **shipment_event**: the booking that follows a won case, and its
  milestones, issues and ETA changes, each optionally tied to the mail it came from.
- **suggestion**: a proposed change with its evidence (sender, date, quoted
  sentence) waiting to be accepted or dismissed.
- **change_log**: every field change, old value and new value.
- **setting**: configuration; lists are stored as JSON.

### Rules the data layer enforces

1. **Nothing is deleted.** A case is closed or marked dropped, an event is voided,
   a note is soft-deleted. The only files the app deletes are its own expired
   daily backups.
2. **Every write goes through `Repository`**, which writes a field-level
   `change_log` row. Routes never run `UPDATE` themselves.
3. **Keys are normalised on the way in**: location keys upper-case and trimmed,
   party keys trimmed and unique case-insensitively. Forms only accept a party
   or a location picked from a list or added through "Add…", never free text.
4. **Parsing only suggests.** Whatever is read out of a subject, a sender or a
   body is pre-filled into a form and saved only when the user confirms.
5. **Outlook is read-only.** The app never creates, moves, deletes or sends mail.
   COM errors become messages; without pywin32 or Outlook every Outlook action
   shows a notice instead of failing.

## Derived values are defined once

Month, source type, region, LATAM flag, direction, TEU, quoted on, closed on,
follow-up counts, the re-quote round and the request group exist in exactly one
place: the SQL views `v_enquiry` and `v_party_stats`. Pages, the Tracking rules,
exports and reports all read the views. There is no second implementation in
Python to drift out of step.

Dates come in two flavours. `quoted_on` / `closed_on` use only exact events and
are what exports show; `quoted_on_any` / `closed_on_any` include approximate
dates (events whose date had to be inferred) and are used to place a case on a
timeline. An approximate date never reaches an export.

## Tracking rules

The Tracking page is the daily worklist. Each rule is a SQL query over the
views; working days are Monday to Friday; legacy and snoozed items are left out.

| Rule | Shows a case when |
|---|---|
| R1 New, no action | it is New, confirmed, older than a day, and nobody has been asked for a rate |
| R2 Vendor silent | a vendor was asked two working days ago or more and has not replied (one row per vendor) |
| R3 Quoted, follow up | the quote is waiting on the follow-up ladder: FU1, FU2, then Decide |
| R4 Confirm status | the status was never confirmed |
| R5 Identify requester | the requester is still the `(TBC)` placeholder |
| R6 Record who won | it was won, vendors were asked, and whose rate was used is not recorded |
| R7 Touch base | an active party has gone quiet longer than its cadence |
| R8 Fix location | an open case has a country but no location, or a location still to confirm |
| R9 Mail activity | a vendor replied after the case last moved |
| R10 Shipment needs attention | a shipment has an open issue, an ETA gone by, or a quiet week |
| R11 No reply for a month | it was quoted and the requester has not written since |
| R12 Waiting for requester | a New case waits for something from the requester; it has a follow-up ladder of its own |

Two invariants are tested: every open, confirmed New case sits on R1, R2, R9 or
R12 (New means the ball is in our court, unless we are waiting for the requester),
and events written by back-fill tools (`source = 'import'`) never count as
activity.

The follow-up clock starts at the latest of the quote date, the last follow-up
and the last note. FU1 comes one working day later, FU2 five working days after
FU1, and after the configured number of follow-ups the case moves to Decide.
Follow-up mails are composed from rotating template pools per requester and
step; the app produces text to copy into Outlook, it never sends.

## The mail pipeline

1. **Register from Outlook.** The user selects a mail in Outlook and presses a
   button. The bridge reads the selection through COM (read-only) into a
   `MailInfo` record.
2. **Intake.** `intake.py` decides who is asking (sender, the quoted chain under
   our own reply, office prefixes on staff display names such as `JQM-CARLA RUIZ`,
   the external domains on To/CC when the sender is internal), what the lanes are
   (subject first, then the body; Spanish and Portuguese lane words; country
   aliases), the quantities, the mode, the incoterm and the cargo lines. It also
   works out the direction: a mail we sent to a vendor is us asking, not an
   enquiry arriving.
3. **Duplicate check.** `dedupe.py` scores recent cases for "probably the same
   request". Token rarity is computed over the whole corpus of enquiries, never
   over the candidate set: a word that looks unique inside a small candidate
   pool is often common overall.
4. **The form.** Every value arrives as a suggestion with its source ("from
   subject", "from the quoted original mail (…)"), and the user confirms.

**The mail archive.** `python -m opplist mail-sync` copies mail headers, the top
of each body and the thread identifiers from Outlook into a local SQLite archive
with a full-text index. Pages and scans read the archive, not Outlook: the case
page shows the thread behind a case, the shipment page lists the mails that
mention the booking, and the scanners propose status changes from what the
archive says. The live Outlook session is only needed to sync and to register.

## Shipments and maps

A won case can start a shipment (ocean, air or truck). Milestones are per mode
(`Booked`, `Departed`, `Arrived`, `Delivered` …). Events can be read from mails
in the archive: a milestone phrase with a date becomes a proposed event, an ETA
change moves the ETA, a complaint becomes an issue that a later event resolves.
A sentence that states a plan ("has to be picked up on…") is a plan, not an
event, and becomes a note.

The Route card draws the shipment on a map without any mapping library: a
Natural Earth world map for ocean and air, a US Albers map for trucks inside the
USA. Both files contain only pre-projected land paths; the projection parameters
were solved from sample points in the files so a lat / lon lands within half a
pixel. Points are origin, via points and destination joined by a dashed line; a
marker sits on the last confirmed milestone.

### Coordinate rules

Coordinates live on `master_location.lat` / `lon` and are filled once, offline,
by `python -m opplist fill-coordinates`, in this order: a manual CSV (always
first, never overwritten unless asked); the location's own UN/LOCODE in the
UNECE code list; an airport's IATA code in the OurAirports list; finally a name
plus country that is unique in the code list. The app never fetches coordinates
at run time. The demo data set ships with coordinates already filled.

## The team site

### Documents and actions

OppList pushes a small set of documents; the site only reads them:

| Document | What it holds |
|---|---|
| `meta/sync` | when OppList last pushed and last took actions in, the Handled by names, the quote rules (markup, margin floor), the lists the edit forms offer, which features are switched on |
| `lists/assign`, `lists/inquiries` | the Assign List and every open and recent case, one row each, with the values the Tracking rules computed |
| `lists/vendors`, `lists/parties`, `lists/places` | the vendors found in the mail with their addresses and where they quote, the requesters, the places |
| `cases/{OPP}` | one case: the request, routes, cargo lines, rate requests, timeline, the mail to reply to |
| `reports/{month}` | the month report as HTML |
| `rateDrafts/{id}` | a Read quote draft waiting to be checked |

What people do on the site is written as **actions** (`actions/{id}`: take a
case, assign it, log a quote or a follow-up, ask vendors, type a vendor's rates,
change cargo or a route, mark Waiting for requester, send a Won or Lost, ask for
a Read quote), sometimes with documents of the same id (`vendorRates`, `quotes`,
`quoteCosts`, `evidence`). Every sync run first takes in the actions newer than
its last pull, oldest first: each is applied through the Repository by the same
functions OppList's own buttons call, recorded in `site_action`, and answered in
`actionResults/{id}` (applied, or refused with a sentence for the person). Then
it pushes the lists, which already show what was taken in.

Until OppList has pulled, the site lays the actions newer than `meta/sync.pulledAt`
over the lists, so a case taken by one person is shown as taken to everyone at
once. A Won or Lost from the site does not close the case: it becomes a pending
suggestion with the pasted mail as evidence, and the administrator confirms it.

### Guards

- Only the app over its own database pushes; a copy, a test or a second window
  never does. A daily cap on writes pauses the push before the store's free
  quota is used up.
- What is pushed comes from the same functions as the pages (the Tracking
  ladder, the Assign List, last activity), never a second copy of a rule. No
  prices from the mail and nothing of a mail body beyond what Tracking shows.
- Costs live in their own documents (`quoteCosts`), which the store's rules
  give only to the administrator and the people ticked for Costs.

### The offline copy

`site/` is the site's own code with two seams: `site/store/firestore.js`
answers the store's calls from memory, loaded from one snapshot file, and
`site/store/auth.js` replaces sign-in with a "Viewing as" switch between three
invented people. The snapshot is written by `python -m opplist demo`
(`site_snapshot.py`): it runs OppList's push (`firebase_sync.sync_once`) on a
copy of the demo database into a local store, adds the documents only the site
writes (the people, notes, the work `demo_showcase.py` did through the take-in),
and saves everything as one JSON file. So the two halves still meet at the same
documents. The site reads its clock from the snapshot, so its dates stay right
however long after the demo was generated it is opened.

## Read quote

A vendor's reply (the mail with its PDFs and pictures, or a pasted message) is
sent with what OppList knows about the shipment to a language model, which
answers with the rate form as JSON against a fixed schema. OppList checks the
answer, and its own rules, not the model's, turn it into one draft per route,
with warnings (a rate the vendor called approximate, a currency written in a way
it had to guess, a door pickup asked and no pickup charge in the mail, a unit
not in the list, a validity date that could be day or month first). A person checks the draft against the mail and saves
it as that vendor's rates, or sets it aside; drafts expire after 14 days. Every
reading is kept with the tokens it used and its cost, and a monthly cap stops
readings before the budget is spent. In this copy the request is answered from a
recording (`read_quote_recorded.py`), and everything after the call runs as on
a real answer.

## Migrations and restore points

`schema.sql` always describes the current shape; `schema_migrations.py` carries
every step from version 2 to the current one, and `schema_version` in `setting`
records where a database is. Opening a database upgrades it, after a restore
point.

- Before any import, migration or bulk fix the database file is copied to
  `restore-points/opplist_before_<task>_<timestamp>.db`. Those are never pruned.
- On `serve`, a daily copy is taken if there is none for today; only these daily
  copies are pruned, keeping the newest `backup_keep`.
- Every bulk tool runs as a dry run first, prints what it would do, and can be
  run again without creating duplicates (natural keys: `row_key`, `opp_no`,
  `(party_id, type, at, source)`).
- Views are dropped and recreated from an explicit list in the migration code,
  not from whatever `sqlite_master` happens to contain (see DECISIONS.md).

## Export formats are contracts

The CSV exports (`opp-index.csv`, 8 columns; `agents.csv`, 5 columns) keep their
column names, order, UTF-8 BOM, CRLF line ends and quoting; the workbook's
Opportunities sheet keeps its 41 columns. Other tools read these files, so the
formats only change deliberately and tests pin them.

## Configuration and branding

`opplist/branding.py` holds every company fact with a neutral default: the app
name, the company's short name used in office party keys (`company_short`,
default `JoJo`, overridable in `setting`), the internal mail domain, the
home office code. `paths.py` puts all data in one folder: `./data`, or the folder
named by `OPPLIST_DATA_DIR` (the demo scripts use `./demo`).
