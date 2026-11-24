# The team site, offline

OppList runs on one computer. The team site is its second half: a web app the whole team opens, where
they see the lists OppList pushes (Assign List, Inquiries, Follow-ups, each case) and do their part of the
work (take a case, ask vendors for rates, type the vendors' rates, build a quote and its PDF, log a
follow-up, send a Won or Lost). OppList takes in what they did at its next sync.

This folder is that site as a static copy on invented data. There is no account, no server behind it and
no connection: the pages are plain HTML, CSS and ES modules with no build step.

## Open it

    run_demo.cmd            (or ./run_demo.sh)

then open `http://127.0.0.1:8766/site/`. The demo command writes the one file the site reads,
`demo/site/snapshot.json`, and the demo app serves it at `site/data/snapshot.json`.

To serve the folder as plain files instead (any static server, or GitHub Pages):

    python -m opplist demo --out demo --site-data site/data
    python -m http.server 8766 --directory site

ES modules do not load from `file://`, so a double click on `index.html` shows nothing.

## What is real and what stands in

| Part | In this copy |
|---|---|
| The pages and drawers (`views/`, `app.js`, `search.js`) | The site's own code. |
| Mail texts, quote arithmetic, the PDF (`askmail.js`, `quotemail.js`, `ratelib.js`, `editlib.js`, `quotepdf.js`) | The site's own code; the company's wording (standard terms, charge names, mail sentences, the address line) is written new for the invented company. |
| The cloud document database | `store/firestore.js`: the same calls (`doc`, `query`, `onSnapshot`, `setDoc`, `writeBatch` ...) answered from memory. The page modules only had their import lines changed. |
| The data | One snapshot, written by `python -m opplist demo` through OppList's own push code (`opplist/site_snapshot.py`), so both halves still meet at the same documents. |
| Sign-in | A visible "Viewing as" switch between three invented people: the administrator, a member with Costs, a member without. The store enforces what each may read (costs, the monthly report, the pasted mail of a Won or Lost, the list of users). |
| OppList taking actions in | Nobody pulls. What you do stays "not in the lists yet" and the pages lay it over the lists, which is the state the real site is in between two syncs. It is kept in this browser's `localStorage`; Reset demo forgets it. |
| Read quote | A request is answered with one recorded reading (`store/standin.js`). The reading itself was made by OppList's Read quote code from a recorded answer when the snapshot was built. |
| The Outlook helper ("Make Outlook drafts") | A separate program, not in this repository. The site still copies the request to the clipboard and logs the action. |
| The official place lists (UN/LOCODE, IATA) | Not shipped. The place picker offers OppList's own places and keeps anything else as typed. |
| Reporting an issue | Saved and shown under Issues; no mail is sent. |

The clock: the demo data is relative to the day it was generated, so the site does not read the
computer's clock. `lib.js` starts at the snapshot's moment and runs on from there.

## Tests

- `tests/*_test.mjs`: unit tests of the plain-function modules and of the store. They run in Node
  (`node site/tests/store_test.mjs`) and in a browser at `site/tests/run.html`.
- `tests/e2e.html`: opens the site in a frame and clicks through it (assign, follow-up, note, switch
  person, Users, reload, Reset, Ask vendors, a vendor's rates, the quote card, the PDF, Read quote).
- `../tests/test_site.py` (pytest): the snapshot's shape and bytes, that the site's files name no host
  and import no cloud client, that no module reads the computer's clock, that the app serves the folder.

## Third-party files

`vendor/jspdf-4.2.1.umd.min.js` (MIT, its notices are inside the file) and, in `vendor/fonts/`, Inter and
Newsreader under the SIL Open Font License (the two `OFL-*.txt` files). Nothing else comes from outside.

The jsPDF file is `dist/jspdf.umd.min.js` of the npm package `jspdf@4.2.1`, byte for byte, except that its
last line (the `sourceMappingURL` comment) is not here. Its SHA-256 is pinned in `opplist/leakcheck.py`
(`THIRD_PARTY`) and checked by `tests/test_site.py`.
