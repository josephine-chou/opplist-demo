# Design decisions

A selection from the project's decision log, rewritten for readers outside the
team. Each entry gives the decision, the reason, and what it cost.

## 1. The status set is small; re-quoting is not a status

Open cases are `New` (the ball is in our court) or `Quoted` (the ball is in
theirs). Closed cases are `Won`, `Lost` or `Dropped`. An intermediate
"Sourcing" status existed for a while and was retired: it described our internal
progress rather than who owed whom an answer, and every rule that looked at it
also had to look at New.

A re-quote is not a status either. Pressing **Re-quoted** writes a `Quoted`
event with `kind = requote` and a reason from a configurable list ("Lower rate",
"Rates expired", "Request changed", ...). The view derives quote rounds from
these events, so the follow-up ladder, the "days to quote" figure and the month
report all see each round without a sixth status value.
An earlier version read "please re-quote" as "back to New"; it turned out the
button meant "we have quoted again", so the meaning follows the button.

*Cost:* history still contains the retired status. Old events keep it, are shown
as they were and rank correctly; nothing new can write it.

## 2. Follow-ups are a ladder, not a timer

"Quoted and no answer for seven days" produced a list that was either too long
or too late. The rule became a ladder: FU1 one working day after the quote,
FU2 five working days after FU1, then **Decide** (win, lose, or wait a week).
The clock restarts on the last follow-up or note, and it only counts the current
quote round. A reply that needs an answer puts the case on Tracking at once; a
reply like "forwarded to my client, thanks" only restarts the clock.

The app composes the follow-up text from rotating template pools (several
versions per step, rotated per requester so the same agent does not get the
same sentence twice) and never sends it: the user copies it into Outlook and
logs it with one click.

## 3. A local mail archive instead of live Outlook queries

Early features asked Outlook directly (DASL queries through COM). It was slow,
it only saw display names on To/CC, and it needed Outlook open for every page.
`mail-sync` now copies headers, the top of each body and thread identifiers into
a SQLite archive with an FTS5 index. Pages, scans and the case page read the
archive; Outlook is needed only to sync and to register a selected mail.

Details that came out of running it: the incremental window is "last sync
minus 24 hours", not "newest mail minus a day" (a folder whose newest day is a
bulk send would otherwise be re-read every day); a partial sync of a few folders
never updates the global "last synced" stamp; one message may live in several
folders and all locations are kept, with the case folder preferred.

## 4. One view for derived values

Month, region, LATAM flag, direction, TEU, quoted on, closed on, follow-up
counts and the re-quote round are defined once, in `v_enquiry` and
`v_party_stats`. Python code, the Tracking rules, the exports and the reports
read the views. Two implementations of "quoted on" would disagree the first time
an edge case appeared; one definition cannot.

## 5. Views are rebuilt from a list, not from `sqlite_master`

A migration once dropped "all views" by scanning `sqlite_master` and recreated
the ones in `schema.sql`. A compatibility view created by a separate data
migration was not in `schema.sql`, so it vanished and every page failed until it
was restored. Since then, rebuilding views uses an explicit list of the views
`schema.sql` defines, and a permanent test asserts that no upgrade ever leaves
fewer schema objects than it started with.

## 6. Exact and approximate dates are kept apart

Imported history often has no quote or close date, but a timeline still needs an
order. Such events are written with `approx = 1`. The view offers
`quoted_on` / `closed_on` (exact only; what exports show) and
`quoted_on_any` / `closed_on_any` (including approximations; what timelines
use). An approximate date never appears in an export as if it were a fact.

## 7. Same-day events have a defined order

When several events share a date, the current status is the one with the latest
date, then the highest stage (New < Quoted = re-quote < Won = Lost = Dropped),
then the latest id. Re-quote ranks with Quoted so that "quoted, then re-quoted
the same afternoon" resolves to the later action. One ordering constant is used
by the repository, the verifier and the status page.

## 8. Back-fill events are history, not activity

Events written by one-off repair tools (`source = 'import'`) never count as
"something happened" for Tracking or the Last activity column. Otherwise running
a clean-up would silently push dozens of cases off the worklist.

## 9. Parsing suggests, the user decides

Everything read from a mail (lanes, quantities, incoterm, requester, end
customer, cargo lines) is pre-filled with its provenance and saved only on
confirmation. Scans of the archive (status changes, follow-ups, re-quotes,
shipment milestones, party domains) write *suggestions* with the sender, date and
quoted sentence as evidence. Nothing changes a case by itself.

## 10. Rarity is computed over the whole corpus

The duplicate detector weights shared words by how rare they are. Computing
that rarity inside the candidate set made ordinary words look unique and
produced many false matches; document frequency is always computed over all
enquiries.

## 11. Nothing is deleted

Cases are closed or dropped, events and vendor requests are voided, notes are
soft-deleted, parties are merged (the merged row points at the survivor). Every
field change is in `change_log`. Case numbers are never renumbered or reused.
Every bulk operation takes a restore point first and can be run twice without
creating duplicates.

## 12. A sentence about the future is not a milestone

The shipment scanner reads milestone phrases out of mail. "Our trucker will
collect the crates and they should be picked up by Thursday 10/08" contains
*picked up* and a date, but it describes a plan. A milestone phrase preceded in
the same sentence by an obligation or future form (has to be, needs to be,
should be, will be, is going to be ...) becomes a note "Picked up planned
10-08", not an event.

## 13. Charts without a chart library

Only the dashboard and the month report have charts. They are inline SVG
generated in Python: no JavaScript library, no external resources, so the exported
HTML is one self-contained file. The shipment maps follow the same idea with
pre-projected land paths and a projection solved from the files' sample points.

## 14. Company facts live in one module

The portfolio copy routes the company name, the internal mail domain, the home
office code and the office party key format (`{company_short} {code}`) through
`opplist/branding.py` and settings, with neutral defaults. A leak check
(`python -m opplist leakcheck`) scans every publishable file against a list of
forbidden strings kept outside the repository.

## 15. The team site sends actions; OppList stays the master

When the rest of the team got a site of their own, the obvious design was a
shared database both sides edit. It would have meant two writers for every case
and every rule implemented twice, once for each side. Instead the site never
changes OppList's data. It reads what OppList pushes and writes *actions*
("take OPP-1227", "log a follow-up", "these are the vendor's rates"). OppList
takes each action in with the same function its own button calls, records it,
and answers it: applied, or refused with a sentence the person can act on ("is
closed, so the follow-up was not logged").

While an action waits for the next sync, the site lays it over the lists, so
everyone sees a case taken at once and nobody takes it twice.

*Cost:* the site is only as fresh as the last sync, and the computer OppList
runs on has to be on for actions to take effect. The site says when the lists
were last updated and that what people do is kept until then.

## 16. A Won or Lost from the site is a suggestion

Closing a case decides the month report, the win rate and which vendor gets
the credit. A colleague's Won or Lost, with the mail that says so pasted or
forwarded, becomes a pending suggestion with that mail as evidence, the same
kind of suggestion the mail scans make. The case changes when the administrator
accepts it. Until then the site shows it as waiting, and it comes back if it is
not confirmed.

## 17. Costs are a separate document, not a hidden column

The quote card shows costs, markups and margins to the administrator and to the
people ticked for Costs. Hiding them in the page would still have sent them to
every browser. So a quote is two documents: `quotes/{id}` with the sell side,
which everyone reads, and `quoteCosts/{id}` with the costs, which the store's
rules hand only to those people. The mail, the PDF and the clipboard never carry
a cost.

## 18. A vendor's rates are versions, and options have names

Vendors change their rates, and the case has to show what they said when we
quoted. Each save of a vendor's rates is a new version for that route, never an
edit of the old one; the case shows the newest and keeps the rest. When a vendor
offers alternatives in one mail (direct or via transshipment, two carriers),
each is saved as an option with its own name, so a quote can say which option it
used and Compare can set them side by side.

## 19. Read quote: the model reads, the rules decide, a person saves

Typing a vendor's rates from a mail or a PDF is slow and error-prone, and a
language model reads them well. But a model's answer is not a rate. The answer
must fit a fixed schema; OppList's own rules, not the model, decide which route
each price belongs to, which unit it is, and what to warn about; and nothing is
a vendor's rate until a person has checked the draft against the mail and saved
it. Every reading is kept with what was sent, what came back, the tokens and the
cost, and a monthly cap stops readings before the budget is spent. What people
change on a draft before saving it is kept too, which shows where the reading
goes wrong.

## 20. Waiting for requester is a note, not a status

A New case often cannot be quoted until the requester sends the dimensions or
the pickup address. A new status would have broken decision 1 (New means the
ball is in our court) and every rule that reads it. Instead the button writes one
timeline note with what we need. Whether the wait is still live is worked out
from the timeline each time: it ends with their reply, a vendor asked, a status
change, or Info received. While it lasts the case leaves "nothing done yet" and
gets a follow-up ladder of its own.

## 21. The work lists and the reports count different things

For a while, cases the reports should not count (a direct customer's request
from before they were registered systematically, a lane outside the report's
region) were set to scope Out, which also hid them from Tracking, the Assign
List and the team site, although someone still had to answer them. The two
questions were split: every inquiry stays on the work pages unless a person
takes it out by hand, and the reports apply their own rule of who is counted,
without touching the case.

## 22. A cap on writes, after two windows used up a day

The hosted store has a free daily quota of writes. One day two OppList windows
were open on the same computer, each pushing every few minutes, and between them
they used it up by the afternoon. Now only the app over the live database
pushes, it counts its writes per day, and it pauses before a push would take the
day past a cap, saying so in Settings. A push writes only the documents whose
content changed.

## 23. Sea-Air is charged on air weight

Sea-Air travels by sea to a hub and flies the last leg. For the chargeable weight
it counts as air (the greater of the actual weight and the volumetric weight),
and in the month report it is one of the "other" modes, like Cross-border
Truck. The two combined modes came in as real requests did, not as a design
exercise.

## 24. The portfolio copy replaces each outside service with one seam

The working tool talks to a hosted document store, a second store for the Rate
Card and a language model API. The portfolio copy can carry no key, project or
address of a real project, and its tests and demo must run with no network. So
each service sits behind one object with the same calls (a folder of JSON
documents for the two stores, recorded answers for the model), and everything
around it is the working code: the documents built, the actions taken in, the
guards, the checks of the model's answer, the cost arithmetic. The team site
runs the same way, on a snapshot that OppList's own push code writes.
