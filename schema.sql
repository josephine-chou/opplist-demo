-- OppList schema v1 (2026-09-08). SQLite 3.35+.
-- Conventions: dates are ISO text 'YYYY-MM-DD'; timestamps 'YYYY-MM-DD HH:MM:SS' local time.
-- Booleans are INTEGER 0/1. Enumerations are CHECKed here and mirrored in opplist/status.py.
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ---------------------------------------------------------------- reference
CREATE TABLE country (
    name        TEXT PRIMARY KEY,                 -- as used in the workbook ('USA', 'UK', 'Hong Kong', 'Unknown')
    iso2        TEXT,
    iso3        TEXT,
    region      TEXT NOT NULL                     -- Asia / Latin America / North America / Europe / Middle East & Africa / Oceania / Antarctica / Undefined
);

CREATE TABLE location (
    key            TEXT PRIMARY KEY,              -- UPPER CASE, trimmed. 'MIA', 'LAEM CHABANG', 'SAVANNAH, GA'
    official_name  TEXT,
    type           TEXT CHECK (type IN ('Country','State or Province','City','Seaport','Airport','Aggregate (non-location)')),
    country        TEXT REFERENCES country(name),
    status         TEXT NOT NULL DEFAULT 'OK' CHECK (status IN ('OK','To Confirm')),
    notes          TEXT,
    code_system    TEXT CHECK (code_system IN ('UN/LOCODE','IATA','IATA City Code','ISO 3166-2','Country','Unresolved')),
    code_official  TEXT,
    parent_city    TEXT,
    is_seaport     INTEGER NOT NULL DEFAULT 0,
    is_airport     INTEGER NOT NULL DEFAULT 0,
    aliases        TEXT,                          -- v10: other spellings a mail uses, comma separated ('Miami, Miami port')
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ---------------------------------------------------------------- master (v14)
-- The master data layer: one set of tables, one writer (OppList through its
-- repository), and the v_master_* views as the contract other apps read.
-- After migrate-master the old location table is location_old and 'location'
-- is a compatibility view over master_location (created by the migration).
CREATE TABLE master_area (
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,          -- 'SANTIAGO'
    name          TEXT NOT NULL,
    country_code  TEXT NOT NULL                  -- ISO2
);

CREATE TABLE master_location (
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,          -- 'CHICAGO, IL', 'ORD', 'YANTIAN': upper case, no accents
    name          TEXT NOT NULL,                 -- the proper spelling, accents allowed
    type          TEXT NOT NULL CHECK (type IN ('city','airport','seaport','ramp')),
    country       TEXT NOT NULL REFERENCES country(name),
    country_code  TEXT,                          -- ISO2, from the country
    subdivision   TEXT,                          -- US / CA state code
    parent_id     INTEGER REFERENCES master_location(id),   -- a gateway points at the market city it serves; NULL for a city
    area_id       INTEGER REFERENCES master_area(id),
    unlocode      TEXT,
    iata          TEXT,
    is_seaport    INTEGER NOT NULL DEFAULT 0,
    is_airport    INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','to_confirm','retired')),
    note          TEXT,
    source        TEXT NOT NULL DEFAULT 'phase1' CHECK (source IN ('phase1','manual','intake')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    lat            REAL,                          -- v27: decimal degrees, filled by fill-coordinates (UN/LOCODE, OurAirports)
    lon            REAL,
    no_parent      INTEGER NOT NULL DEFAULT 0     -- v42: 1 = a gateway that has no parent city on purpose (the note says why),
                                                  -- a gateway with parent_id NULL and no_parent 0 is listed by check
);

CREATE TABLE master_location_alias (
    alias         TEXT NOT NULL,                 -- upper case
    location_id   INTEGER NOT NULL REFERENCES master_location(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('code','iata_city','unlocode','legacy_key','manual')),
    PRIMARY KEY (alias, location_id)
);
CREATE INDEX idx_master_alias ON master_location_alias(alias);

CREATE TABLE party_group (                        -- offices of one network in several countries
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,          -- 'MARVIS'
    name          TEXT NOT NULL,
    notes         TEXT
);

-- ---------------------------------------------------------------- parties
CREATE TABLE party (
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL,                  -- display key, e.g. 'Tolvex', 'JoJo JJC'. Unique case-insensitively (index below)
    full_name     TEXT,
    type          TEXT NOT NULL CHECK (type IN ('Agent','Office','Direct Customer','Unknown','Carrier','Coloader','Trucker')),
                                                  -- v9: Carrier; v52 (Q2b): Coloader and Trucker. Carrier = an airline or a
                                                  -- shipping line; Coloader = a consolidator selling space (an LCL or air
                                                  -- consol, an NVOCC); Trucker = truck or rail inland, IPI included
    country       TEXT REFERENCES country(name),
    city          TEXT,
    mfn_member    INTEGER,                        -- 1 = yes, 0 = known not, NULL = unknown
    mfn_no        TEXT,
    stage         TEXT NOT NULL DEFAULT 'active' CHECK (stage IN ('prospect','contacted','active','dormant')),
    cadence_days  INTEGER,                        -- NULL = use setting 'cadence_days_default'
    placeholder   INTEGER NOT NULL DEFAULT 0,     -- '(TBC)', 'Unknown'
    notes         TEXT,
    source        TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('xlsx','directory','manual','outlook','tracker')),
    aliases       TEXT,                           -- v8: JSON list of names this party was also known by (merged parties)
    merged_into   INTEGER REFERENCES party(id),   -- v8: set when this party was merged into another; the row stays
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    group_id      INTEGER REFERENCES party_group(id),  -- v14: the network this office belongs to (NULL = none decided)
    roles         TEXT NOT NULL DEFAULT ''       -- v14: '|'-separated: agent, carrier_air, carrier_ocean, trucker, broker, warehouse, direct_customer, own_office
);
CREATE UNIQUE INDEX ux_party_key_ci ON party (lower(trim(key)));

CREATE TABLE party_domain (
    domain     TEXT PRIMARY KEY,                  -- lower case, no '@'
    party_id   INTEGER NOT NULL REFERENCES party(id) ON DELETE CASCADE
);

CREATE TABLE contact (
    id          INTEGER PRIMARY KEY,
    party_id    INTEGER NOT NULL REFERENCES party(id) ON DELETE CASCADE,
    name        TEXT,
    email       TEXT,                             -- one address per row
    title       TEXT,
    is_primary  INTEGER NOT NULL DEFAULT 0,
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX ix_contact_party ON contact (party_id);
CREATE INDEX ix_contact_email ON contact (lower(email));

CREATE TABLE party_event (                        -- relationship timeline (outreach, replies, calls, visits)
    id          INTEGER PRIMARY KEY,
    party_id    INTEGER NOT NULL REFERENCES party(id) ON DELETE CASCADE,
    at          TEXT NOT NULL,                    -- date; if only free text is known, store first-of-month and put the text in summary
    type        TEXT NOT NULL CHECK (type IN ('outreach','reply','call','visit','meeting','note')),
    summary     TEXT,
    enquiry_id  INTEGER REFERENCES enquiry(id) ON DELETE SET NULL,
    source      TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','directory','scan','migration')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX ix_party_event_party ON party_event (party_id, at);

-- ---------------------------------------------------------------- enquiries
CREATE TABLE enquiry (
    id                 INTEGER PRIMARY KEY,
    opp_no             TEXT NOT NULL UNIQUE,      -- 'OPP-1401'. Never reused, never renumbered.
    group_id           INTEGER REFERENCES enquiry(id),  -- request group: several lanes from one request share the first enquiry's id
    received_on        TEXT,                      -- date the enquiry arrived (mail date)
    requester_party_id INTEGER NOT NULL REFERENCES party(id),
    end_customer       TEXT,                      -- actual shipper / consignee behind the request (free text, autocomplete)
    owner              TEXT,                      -- who handles it at the home office (setting 'owners')
    handled_by         TEXT,                      -- v31: the office that handles it (JQM, JJH, ...); NULL = the home office
    service_mode       TEXT CHECK (service_mode IN ('Air','Sea-Air','LCL','FCL','RORO','Break Bulk','Project','Truck','Cross-border Truck','Multi-mode','Not specified')),  -- v46: Truck (was Domestic); v48: Sea-Air, Cross-border Truck
    container_type     TEXT,                      -- from setting 'container_types'
    container_qty      INTEGER,
    air_weight_kg      REAL,                      -- v33: the chargeable weight on Air cases, NULL on others (repository)
    lcl_cbm            REAL,                      -- v33: the volume on LCL cases, NULL on others (repository)
    qty_raw            TEXT,                      -- quantity text as received ('FCL:3 x 40HQ', 'Air:200'); never computed words
    origin_country     TEXT REFERENCES country(name),
    origin_loc         TEXT,                      -- v14: the master_location key, written from origin_loc_id (kept for the exports and old readers)
    origin_alt         TEXT,                      -- free text: address, pickup place
    dest_country       TEXT REFERENCES country(name),
    dest_loc           TEXT,                      -- v14: as origin_loc
    dest_alt           TEXT,
    incoterm           TEXT CHECK (incoterm IS NULL OR incoterm IN ('EXW','FCA','FAS','FOB','CFR','CIF','CPT','CIP','DAP','DPU','DDP','DDU')),
    commodity          TEXT,
    special_handling   TEXT,                      -- ';'-separated tags from setting 'special_handling'
    status             TEXT NOT NULL DEFAULT 'New' CHECK (status IN ('New','Sourcing','Quoted','Won','Lost','Cancelled','Dropped')),  -- v38: Cancelled
    status_confirmed   INTEGER NOT NULL DEFAULT 1, -- 0 = migrated with blank status; tracking asks to confirm
    closed_reason      TEXT,                      -- setting 'closed_reasons'. Required by the UI when a case is
                                                  -- closed as Lost / Cancelled / Dropped in the app; migrated rows may be NULL
                                                  -- because the workbook had no reason. NULL means 'not recorded',
                                                  -- never 'Other'. The consistency check lists the blanks.
    legacy             INTEGER NOT NULL DEFAULT 0, -- inherited pre-hire record with unknown outcome
    hold_reason        TEXT,                      -- v11: why the case is parked ('On hold' in Set status); NULL = not on hold
    hold_until         TEXT,                      -- v11: tracking leaves it alone until this date (optional)
    lost_their_price   TEXT,                      -- v21: what the case was lost to, as written ('USD 950 + local charges'); optional
    lost_our_price     TEXT,                      -- v21: what we had quoted ('USD 1,180 all-in'); optional; both feed report section 7
    scope              TEXT NOT NULL DEFAULT 'In' CHECK (scope IN ('In','Out')),
                                                  -- v9: In = somebody asked us (the inbound business the app tracks);
                                                  -- Out = we are the customer's forwarder buying a rate (a direct
                                                  -- customer's shipment). Out cases stay on file but leave the list by
                                                  -- default, every tracking rule, and the inbound counts of the report;
                                                  -- their vendor requests count like any other
    awarded_vendor_id  INTEGER REFERENCES party(id),  -- convenience copy of the vendor_request marked awarded; NULL = own rate / unknown
    awarded_note       TEXT,                      -- free text about the rate used (v8: the note next to rate_used)
    rate_used          TEXT CHECK (rate_used IN ('agent','office','own','not_recorded','carrier')),
                                                  -- v8, Won cases: whose rate the business used. 'agent' = a vendor's
                                                  -- (awarded_vendor_id, and the vendor_request marked awarded);
                                                  -- 'office' = one of our offices (awarded_vendor_id); 'own' = our own
                                                  -- quote, a direct order, an airline / courier asked directly;
                                                  -- 'not_recorded' = chosen not to say; 'carrier' (v9) = an airline /
                                                  -- line / trucker asked directly (awarded_vendor_id, party type Carrier).
                                                  -- NULL = never answered (R6 asks)
    thread_key         TEXT,                      -- subject-derived key, same algorithm as Build-RateRequestBoard Get-ThreadKey
    conversation_id    TEXT,                      -- Outlook ConversationID of the registering mail
    entry_id           TEXT,                      -- Outlook EntryID of the registering mail (open in Outlook)
    source_subject     TEXT,                      -- original subject line
    remarks            TEXT,
    system_notes       TEXT,
    created_by         TEXT NOT NULL DEFAULT 'manual' CHECK (created_by IN ('manual','outlook','migration')),
    created_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    origin_loc_id      INTEGER REFERENCES master_location(id),  -- v14: the place (master_location); origin_loc follows it
    dest_loc_id        INTEGER REFERENCES master_location(id),  -- v14
    origin_loc_legacy  TEXT,                      -- v14: the text origin_loc held before migrate-master (never rewritten)
    dest_loc_legacy    TEXT,                      -- v14
    gross_kg           REAL,                      -- v33: gross weight; the counted cargo lines once per route they apply to,
    volume_cbm         REAL,                      -- v33: volume; typed on the Cargo card while the case has no counted line
    cargo_decided      TEXT,                      -- v34: the case-level cargo checks answered ('air_limit,qty_mode')
    latam_choice       TEXT CHECK (latam_choice IS NULL OR latam_choice IN ('Y','N'))
                                                  -- v49 (SC1): LATAM set by hand under More; NULL = automatic
                                                  -- (the countries and the requester, as before); v_enquiry.latam
                                                  -- and v_route.latam give her choice when there is one
);
CREATE INDEX ix_enquiry_received ON enquiry (received_on);
CREATE INDEX ix_enquiry_requester ON enquiry (requester_party_id);
CREATE INDEX ix_enquiry_status ON enquiry (status);
CREATE INDEX ix_enquiry_thread ON enquiry (thread_key);
CREATE INDEX ix_enquiry_group ON enquiry (group_id);

CREATE TABLE status_event (
    id          INTEGER PRIMARY KEY,
    enquiry_id  INTEGER NOT NULL REFERENCES enquiry(id) ON DELETE CASCADE,
    at          TEXT NOT NULL,                    -- date of the change (business date, not the click time)
    status      TEXT NOT NULL CHECK (status IN ('New','Sourcing','Quoted','Won','Lost','Cancelled','Dropped')),
    note        TEXT,
    kind        TEXT NOT NULL DEFAULT 'status'    -- 'note' = something happened, the status did not change; 'followup' (v13) = we chased the requester
                                                  -- 'requote' (v19) = a Sourcing chosen on purpose after a quote: it says what the case is, like 'status',
                                                  -- and on the same day it outranks the Quoted it undoes (status.STAGE_RANK)
                                                  -- 'ack' (v37, AL1) = our acknowledgement to the requester carrying the case
                                                  -- number, before the quote: our action, no status change, never a quote
                CHECK (kind IN ('status','note','followup','requote','ack')),
    seq         INTEGER,                          -- v13: followup number on the case (1, 2, 3); NULL for the other kinds
    channel     TEXT CHECK (channel IN ('mail','call','whatsapp','teams','other')),   -- v13: how the followup went out; v43: whatsapp, teams, other (the team site)
    voided      INTEGER NOT NULL DEFAULT 0,       -- 1 = struck out: kept and shown, but counts for nothing
    source      TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','scan','migration','import','mail','web')),  -- v12: 'mail' = accepted from a suggestion read out of Outlook; v43: 'web' = logged on the team site (site_action)
    approx      INTEGER NOT NULL DEFAULT 0,       -- 1 = date is a fallback (migrated without a real date)
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    reason      TEXT                              -- v22: why - the closed reason on a Lost / Cancelled / Dropped event, the requote reason
                                                  -- (setting 'requote_reasons') on a Quoted event that reopens a round or on a
                                                  -- requote event; NULL on the rest. enquiry.closed_reason stays the export's source
);
CREATE INDEX ix_status_event_enquiry ON status_event (enquiry_id, at);

CREATE TABLE vendor_request (                     -- one row per vendor we asked, per enquiry (buy side)
    id               INTEGER PRIMARY KEY,
    enquiry_id       INTEGER NOT NULL REFERENCES enquiry(id) ON DELETE CASCADE,
    vendor_party_id  INTEGER NOT NULL REFERENCES party(id),
    asked_on         TEXT,
    replied_on       TEXT,
    quoted           INTEGER NOT NULL DEFAULT 0,  -- 1 = they sent a usable rate
    their_ref        TEXT,
    machine_status   TEXT,                        -- tracker MachineStatus as imported ('Acknowledged', 'Asked, no reply', ...)
    gist             TEXT,                        -- tracker ReplyGist / short note
    awarded          INTEGER NOT NULL DEFAULT 0,  -- 1 = this vendor's rate was used in the won business
    voided           INTEGER NOT NULL DEFAULT 0,  -- v8: struck out (recorded by mistake); stays on the case, counts for nothing
    row_key          TEXT,                        -- tracker RowKey, to make re-imports idempotent
    source           TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','tracker','scan','mail','web')),  -- v12: 'mail' = accepted from a suggestion, v44: 'web' = logged on the team site (Ask vendors)
    notes            TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    conversation_id  TEXT,                        -- v23: the Outlook conversation of the mail we asked with (set when logged from Outlook)
    thread_key       TEXT,                        -- v23: its subject's thread key; the case page's Mail card reads both
    route_ids        TEXT,                        -- v33: the enquiry_route ids it covers, ',' separated; NULL = all
    parts            TEXT                         -- v52 (Q2b): what it was asked for, '|' separated, from Freight, Inland,
                                                  -- Origin, Destination, Customs, Door to door in that order; NULL = not said
);
CREATE UNIQUE INDEX ux_vendor_request_rowkey ON vendor_request (row_key) WHERE row_key IS NOT NULL;
CREATE INDEX ix_vendor_request_enquiry ON vendor_request (enquiry_id);
CREATE INDEX ix_vendor_request_vendor ON vendor_request (vendor_party_id);

CREATE TABLE vendor_rate (                        -- v51: a vendor's rates for a case as typed on the team site (Q2a,
    id              INTEGER PRIMARY KEY,          -- INTERFACE.md section 9): one row per vendorRates document, never
    enquiry_id      INTEGER NOT NULL REFERENCES enquiry(id) ON DELETE CASCADE,  -- changed; the vendor's newest on the
    vendor_party_id INTEGER NOT NULL REFERENCES party(id),                     -- case is its rates now
    site_id         TEXT NOT NULL UNIQUE,         -- the vendorRates document's id (= the rates action's id)
    at              TEXT,                         -- when it was saved on the site (UTC, as the action carries it)
    by_email        TEXT,
    by_name         TEXT,
    cur             TEXT,                         -- three letters
    total           REAL,                         -- what the lines with an amount come to for this shipment
    valid_until     TEXT,                         -- yyyy-mm-dd
    transit         TEXT,                         -- as the vendor wrote them (each <= 60)
    carrier         TEXT,
    free_time       TEXT,
    said_fuel       TEXT CHECK (said_fuel IS NULL OR said_fuel IN ('incl','excl','none','notsaid')),
    said_local      TEXT CHECK (said_local IS NULL OR said_local IN ('incl','excl','none','notsaid')),
    said_min        TEXT CHECK (said_min IS NULL OR said_min IN ('incl','excl','none','notsaid')),
    note            TEXT,                         -- <= 300
    source          TEXT NOT NULL DEFAULT 'web' CHECK (source IN ('web')),
    voided          INTEGER NOT NULL DEFAULT 0,   -- struck out, never deleted
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    route_id        INTEGER REFERENCES enquiry_route(id),  -- v52 (Q2b): the route the rates are for; NULL = not said
    received_on     TEXT,                         -- v52 (Q2b): the day the vendor sent them (yyyy-mm-dd)
    how             TEXT NOT NULL DEFAULT 'web' CHECK (how IN ('web','read')),  -- v53 (Q2d): typed, or saved from a Read quote draft
    draft_id        TEXT,                         -- v53 (Q2d): the rateDrafts id it was saved from (how = 'read')
    label           TEXT NOT NULL DEFAULT ''      -- v55 (Q2e): the option's name as typed (<= 60); '' = the vendor's one set; the rates now are the newest not struck out of each (case, vendor, route, label compared without case or extra spaces)
);
CREATE INDEX ix_vendor_rate_case ON vendor_rate (enquiry_id, vendor_party_id);

CREATE TABLE vendor_rate_line (                   -- v51: the lines of one vendor_rate, in order
    id       INTEGER PRIMARY KEY,
    rate_id  INTEGER NOT NULL REFERENCES vendor_rate(id) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,
    seg      TEXT NOT NULL CHECK (seg IN ('Freight','Inland','Origin','Destination','Customs','Other')),
    charge   TEXT,                                -- the quotation sheet's names
    unit     TEXT,                                -- per container type, KG, CBM, W/M, SHPT, TRIP ...
    rate     REAL,
    min      REAL,
    qty      REAL,                                -- what the unit counts on this shipment
    amount   REAL,                                -- max(rate x qty, min) or a typed amount
    word     TEXT CHECK (word IS NULL OR word IN ('AT COST','INCLUDED','IF APPLICABLE','N/A')),
    UNIQUE (rate_id, seq)
);

CREATE TABLE quote_read (                         -- v53 (Q2d): one Read quote - a vendor's reply read with the Anthropic API
    id                INTEGER PRIMARY KEY,        --; every reading kept, whatever it gave; never on the site
    enquiry_id        INTEGER NOT NULL REFERENCES enquiry(id),
    vendor_request_id INTEGER REFERENCES vendor_request(id),
    vendor_party_id   INTEGER NOT NULL REFERENCES party(id),
    source            TEXT NOT NULL CHECK (source IN ('mail','paste','files')),  -- v54 (RQ2): files = files sent from the team site with no mail among them
    entry_id          TEXT,                       -- the mail's (source mail)
    message_id        TEXT,
    subject           TEXT,
    sender            TEXT,
    sent_at           TEXT,                       -- 'YYYY-MM-DD HH:MM' her local time; a paste: the day she gave
    files             TEXT,                       -- JSON [{name, kind, size, sent, why}]
    text_sent         TEXT,                       -- the mail's clean text and the files' text (a PDF's or image's, for the record)
    model             TEXT,
    prompt_version    INTEGER,
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    cache_write_tokens INTEGER,
    cache_read_tokens INTEGER,
    cost_usd          REAL,                       -- from usage at the price table's prices
    response          TEXT,                       -- the API's answer as it came (JSON)
    status            TEXT NOT NULL CHECK (status IN ('read','no_prices','failed')),
    error             TEXT,
    trial             INTEGER NOT NULL DEFAULT 0, -- read-quote-trial: never pushed
    by_name           TEXT,                       -- who asked for the reading: Iris on 8765, the read action's name from the site
    site_action       TEXT,                       -- v54 (RQ2): the team site's read action (site_action.id); NULL for a reading started on 8765
    at                TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX ix_quote_read_case ON quote_read (enquiry_id, vendor_party_id);
CREATE INDEX ix_quote_read_site ON quote_read (site_action);

CREATE TABLE quote_read_draft (                   -- v53 (Q2d): one draft of a vendor's rates per route read (rateDrafts/{site_id})
    id             INTEGER PRIMARY KEY,
    quote_read_id  INTEGER NOT NULL REFERENCES quote_read(id),
    site_id        TEXT NOT NULL UNIQUE,         -- rq<quote_read id>-<route number, 0 for none>
    route_id       INTEGER REFERENCES enquiry_route(id),
    draft          TEXT NOT NULL,                -- the draft as pushed (JSON, without status and savedAs)
    status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','saved','discarded','replaced','expired')),
    vendor_rate_id INTEGER REFERENCES vendor_rate(id),  -- the version first saved from it
    option         INTEGER,                      -- which option was saved (0, 1, 2)
    changes        TEXT,                         -- JSON: what the person changed from the option (the first save)
    confirmed_by   TEXT,
    confirmed_at   TEXT,
    closed_at      TEXT,
    taken          TEXT                          -- v55 (Q2e): JSON, per option '' (not yet), 'saved' or 'skipped'; open until every option is taken
);

CREATE TABLE quote (                              -- v56 (Q3): a quote made with the team site's quote card (quotes/{site_id},
    id              INTEGER PRIMARY KEY,          -- and quoteCosts/{site_id} when a cost was typed; INTERFACE.md section 7),
    enquiry_id      INTEGER NOT NULL REFERENCES enquiry(id),  -- taken in after its quote action; never changed
    site_id         TEXT NOT NULL UNIQUE,         -- the quotes/{id} id (= the quote action's id, its [quote:<id>] mark)
    n               INTEGER NOT NULL,             -- the quote's number on the case (1, 2 ...)
    date            TEXT,                         -- the day it was logged as sent (yyyy-mm-dd)
    how             TEXT CHECK (how IS NULL OR how IN ('draft','hand')),
    cur             TEXT NOT NULL,                -- three capital letters
    total           REAL,                         -- the lines with an amount; NULL when no line has one
    at_cost         INTEGER NOT NULL DEFAULT 0,   -- 1: a line is AT COST
    valid           TEXT,                         -- yyyy-mm-dd
    transit         TEXT,                         -- <= 100
    routing         TEXT,                         -- <= 200
    remarks         TEXT,                         -- <= 1000
    terms           TEXT,                         -- JSON array of text: the ticked standard lines (<= 12)
    subject         TEXT,                         -- <= 300
    pdf             TEXT,                         -- the PDF's file name, <= 80
    rate_from       TEXT,                         -- a vendor key, 'own' or '' (<= 80)
    sources         TEXT,                         -- JSON as on the site: [{seg, key, name, carrier, label}] (<= 6)
    by_email        TEXT,
    by_name         TEXT,
    at              TEXT,                         -- the site's server time (UTC)
    status_event_id INTEGER REFERENCES status_event(id),  -- the Quoted or re-quote event whose note carries its mark
    with_costs      INTEGER NOT NULL DEFAULT 0,   -- 1: a quoteCosts document was there
    pct             REAL,                         -- the card's markup; this and the next five NULL without costs
    cost            REAL,
    profit          REAL,
    margin          REAL,                         -- percent
    costs_by        TEXT,
    costs_at        TEXT,
    taken_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX ix_quote_case ON quote (enquiry_id);
CREATE INDEX ix_quote_event ON quote (status_event_id);

CREATE TABLE quote_line (                         -- v56 (Q3): the lines of one quote, in order (<= 40)
    id       INTEGER PRIMARY KEY,
    quote_id INTEGER NOT NULL REFERENCES quote(id) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,
    charge   TEXT,                                -- <= 80
    qty      TEXT,                                -- as the card wrote it (text, <= 20)
    unit     TEXT,                                -- <= 20
    rate     REAL,
    word     TEXT,                                -- AT COST, INCLUDED and the like (<= 40)
    amount   REAL,
    hand     INTEGER NOT NULL DEFAULT 0,          -- 1: the amount was typed by hand
    remark   TEXT,                                -- <= 200
    cost     REAL,                                -- from quoteCosts; NULL where none was typed
    markup   REAL,
    UNIQUE (quote_id, seq)
);

CREATE TABLE register_read (                      -- v57 (RG2a): what the register reader filled on a registration from
    id           INTEGER PRIMARY KEY,             -- a mail, one row a field, beside the value saved; written at Register
    enquiry_id   INTEGER NOT NULL REFERENCES enquiry(id) ON DELETE CASCADE,   -- and nowhere else (RG2b's monthly
    field        TEXT NOT NULL,                   -- check reads it): requester_party_id, received_on, origin_country,
                                                  -- origin_loc, dest_country, dest_loc, service_mode, incoterm, commodity,
                                                  -- container_type, container_qty
    value_read   TEXT,                            -- what the reader put in the form (a party by its key)
    read_from    TEXT NOT NULL CHECK (read_from IN ('subject','quoted inquiry','table','body','official list',
                                                    'sender','mail','attachment','other')),
    value_saved  TEXT,                            -- what the case was saved with (NULL: she emptied it)
    words        TEXT,                            -- a place: the mail's words it was read from (upper case), for
                                                  -- 'Remember ... as ...' (layer 2)
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (enquiry_id, field)
);
CREATE INDEX ix_register_read_enquiry ON register_read (enquiry_id);

-- ---------------------------------------------------------------- housekeeping
CREATE TABLE change_log (                         -- every field-level edit made through the app
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    table_name  TEXT NOT NULL,
    row_id      INTEGER NOT NULL,
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    source      TEXT NOT NULL DEFAULT 'ui',       -- ui / outlook / import / migration
    action_id   INTEGER                           -- v41 (E2): one id per user action (a form post, an Accept, a Strike out, an import step); empty before v41
);
CREATE INDEX ix_change_log_row ON change_log (table_name, row_id);
CREATE INDEX ix_change_log_action ON change_log (action_id);

CREATE TABLE suggestion (                         -- v12: what the mail suggests for a case, waiting for a yes or a no
    id              INTEGER PRIMARY KEY,
    enquiry_id      INTEGER NOT NULL REFERENCES enquiry(id) ON DELETE CASCADE,
    opp_no          TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('status','vendor','note','shipment_milestone','shipment_issue','shipment_note','case_number','tag','edit','followup')),  -- v26: the shipment kinds; v37: case_number (AL1); v50: tag (note = the special handling tag to add, FX5), edit (a team site change that met hers, D7b); v58: followup (a chaser the scan found, FX7)
    shipment_id     INTEGER REFERENCES shipment(id) ON DELETE CASCADE,  -- v26: set on the shipment kinds
    message_id      TEXT,                         -- v26: the archived mail a shipment suggestion came from (never proposed twice)
    route_id        INTEGER REFERENCES enquiry_route(id),  -- v33: a Start shipment suggestion's won route
    proposed_eta    TEXT,                         -- v26: a new ETA the mail carried
    proposed_status TEXT,                         -- kind = status: New / Sourcing / Quoted / Won / Lost / Cancelled / Dropped; shipment kinds: the milestone or the issue title
    closed_reason   TEXT,
    proposed_date   TEXT,                         -- the date the change would carry (the mail's date)
    vendor_key      TEXT,                         -- kind = vendor: who we asked
    vendor_domain   TEXT,
    note            TEXT,                         -- kind = note: the line to add; any kind: the reader's remark
    evidence_sender TEXT,
    evidence_date   TEXT,
    evidence_quote  TEXT,
    confidence      TEXT,
    entry_id        TEXT,                         -- the Outlook mail it was read from, when one mail was
    source          TEXT NOT NULL DEFAULT 'scan' CHECK (source IN ('scan','mail','web')),  -- scan = the scheduled propose-status run; mail = Update from Outlook on the case page, v44: web = from the team site (phase 2b)
    state           TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','accepted','dismissed')),
    decided_at      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    named_cases     TEXT,                         -- v37, kind case_number: the cases the subject names, 'OPP-1504,OPP-1510'
    -- v40 (E1): what she recorded, beside what the app suggested (kept untouched above)
    decided_as      TEXT CHECK (decided_as IS NULL OR decided_as IN ('quoted','requote','won','lost','cancelled','note','history','milestone','start','vendor','attach','dismissed','followup')),  -- v58: followup (FX7)
    decided_values  TEXT,                         -- JSON: date, reason, note, Rate used and its pick, prices, routes, milestone, planned
    edited          INTEGER NOT NULL DEFAULT 0,   -- 1 when anything differs from what Accept would have written
    event_ref       TEXT                          -- the row written, 'status_event:123' / shipment_event / vendor_request / shipment
);
CREATE INDEX ix_suggestion_case ON suggestion (enquiry_id, state);

CREATE TABLE enquiry_thread (                     -- v20: a mail thread attached to a case by hand: the subject was
    id          INTEGER PRIMARY KEY,              -- changed, or the conversation header broke, and the case's own ids no longer find it
    enquiry_id  INTEGER NOT NULL REFERENCES enquiry(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('conversation','thread_key','message')),  -- v37: 'message' = one mail (its Message-ID, else
    value       TEXT NOT NULL,                    -- its entry id) attached from a case-number suggestion (AL1)
    added_on    TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'manual',
    note        TEXT,                             -- the subject of the mail it was attached from
    voided      INTEGER NOT NULL DEFAULT 0        -- struck out, never deleted
);
CREATE UNIQUE INDEX ux_enquiry_thread ON enquiry_thread (kind, value);

CREATE TABLE enquiry_cargo (                      -- v28: one consignment of a case. A mail with two
    id          INTEGER PRIMARY KEY,              -- shipments is two lines, each charged on its own; the case's own
    enquiry_id  INTEGER NOT NULL REFERENCES enquiry(id) ON DELETE CASCADE,  -- air_weight_kg / lcl_cbm / qty_raw
    seq         INTEGER NOT NULL,                 -- hold the totals while lines exist (cargo.enquiry_fields)
    label       TEXT,                             -- '1', 'Quote 02', 'to Clare, MI' - as the mail names it
    pieces      INTEGER,
    pack_word   TEXT,                             -- 'pallets', 'ctns', 'crates'
    dims_text   TEXT,                             -- as written: '120 x 100 x 100 cm', '2 x 52 x 87 x 62 in + ...'
    length_cm   REAL,                             -- the one size, in cm, when every piece shares it
    width_cm    REAL,
    height_cm   REAL,
    weight_kg   REAL,                             -- the line's gross weight
    cbm         REAL,                             -- stated in the mail or typed; NULL = derived from the dims
    deliver_to  TEXT,                             -- the delivery address or POD written for this line
    commodity   TEXT,
    notes       TEXT,                             -- 'Term : FCA in USA; stated 1.1 cbm, dims give 1.3 cbm'
    source      TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('mail','manual','migration')),  -- v35: 'migration' =
                                                  -- made from the case's typed numbers (migrate-cargo-lines, batch 15b)
    voided      INTEGER NOT NULL DEFAULT 0,       -- removed = struck out, never deleted
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    dim_l       REAL,                             -- v33: the size per piece as written, in dims_unit
    dim_w       REAL,
    dim_h       REAL,
    dims_unit   TEXT CHECK (dims_unit IS NULL OR dims_unit IN ('cm','in','mm','m')),
    weight_value REAL,                            -- v33: the weight as written, in weight_unit, per weight_basis
    weight_unit TEXT CHECK (weight_unit IS NULL OR weight_unit IN ('kg','lbs','t')),
    weight_basis TEXT NOT NULL DEFAULT 'total' CHECK (weight_basis IN ('total','each','container')),  -- v35:
                                                  -- 'container' = per container, times container_qty (FCL)
    container_type TEXT,                          -- v33: FCL lines, containers per shipment
    container_qty INTEGER,
    per_month   REAL,                             -- v33: how often (shipments a month, per route) - a project
    months      REAL,                             -- v33: for how long
    counted     INTEGER NOT NULL DEFAULT 1,       -- v33: 0 = left out of the totals (struck through, still listed)
    route_ids   TEXT,                             -- v33: the enquiry_route ids it applies to, ',' separated; NULL = all
    source_mail TEXT,                             -- v34: the Outlook EntryID of the mail the line was read from
    source_text TEXT,                             -- v34: the lines of text it was read from, one per line, as written
    weight_written TEXT,                          -- v34: the weight as written, '1.940 Kgs' (a separator that may mark thousands)
    weight_reading TEXT CHECK (weight_reading IS NULL OR weight_reading IN ('auto','chosen')),  -- v34: how
                                                  -- weight_value was picked when weight_written reads two ways
    cbm_value   REAL,                             -- v34: the volume as written, per cbm_basis; cbm follows it
    cbm_basis   TEXT NOT NULL DEFAULT 'total' CHECK (cbm_basis IN ('total','each')),
    cbm_written TEXT,                             -- v34: the volume as written, '6.05 cbm'
    stated_id   INTEGER REFERENCES enquiry_cargo_stated(id),  -- v34: the total the mail gives for this line's block
    decided     TEXT,                             -- v34: the line's cargo checks answered ('reading,repeat')
    pack_unit   TEXT,                             -- v39: the Packing chosen from setting packing_units, e.g. Pallets.
                                                  -- pack_word keeps the word as written, NULL = not chosen
    hs_code     TEXT,                             -- v50: one or more HS codes as written, ', ' separated ('2507.00.10')
    cargo_value REAL,                             -- v50: the value of the goods (never a charge)
    cargo_value_cur TEXT                          -- v50: its currency, three letters ('USD')
);
CREATE INDEX ix_enquiry_cargo_enquiry ON enquiry_cargo (enquiry_id, seq);

CREATE TABLE enquiry_cargo_stated (               -- v34: what a mail states about a block of cargo as a whole:
    id            INTEGER PRIMARY KEY,            -- a total ('8 packages, 7,605 lbs'), a volume, a chargeable weight.
    enquiry_id    INTEGER NOT NULL REFERENCES enquiry(id) ON DELETE CASCADE,  -- Never counted when the lines give the
    source_mail   TEXT,                           -- same quantity: then it is a check. The Outlook EntryID.
    source_text   TEXT,                           -- the lines it was read from, one per line, as written
    pieces        INTEGER,
    pack_word     TEXT,
    weight_value  REAL,                           -- as written, in weight_unit
    weight_unit   TEXT CHECK (weight_unit IS NULL OR weight_unit IN ('kg','lbs','t')),
    weight_written TEXT,
    weight_kg     REAL,
    cbm           REAL,
    cbm_written   TEXT,
    chargeable_kg REAL,                           -- 'chargeable weight 730 kg': the sender's figure, compared with ours
    chargeable_written TEXT,
    voided        INTEGER NOT NULL DEFAULT 0,     -- never deleted
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX ix_enquiry_cargo_stated_enquiry ON enquiry_cargo_stated (enquiry_id);

CREATE TABLE enquiry_route (                      -- v33: one route of a case: from where, to where, which
    id             INTEGER PRIMARY KEY,           -- part we do. Route A (seq 1) is copied onto the enquiry's own lane
    enquiry_id     INTEGER NOT NULL REFERENCES enquiry(id) ON DELETE CASCADE,  -- fields by the repository
    seq            INTEGER NOT NULL,              -- 1, 2, 3 shown as A, B, C; never renumbered
    origin_country TEXT REFERENCES country(name),
    origin_loc     TEXT,
    origin_loc_id  INTEGER REFERENCES master_location(id),
    origin_alt     TEXT,
    dest_country   TEXT REFERENCES country(name),
    dest_loc       TEXT,
    dest_loc_id    INTEGER REFERENCES master_location(id),
    dest_alt       TEXT,
    service_mode   TEXT CHECK (service_mode IS NULL OR service_mode IN ('Air','Sea-Air','LCL','FCL','RORO','Break Bulk','Project','Truck','Cross-border Truck','Multi-mode','Not specified')),  -- v46: Truck; v48: Sea-Air, Cross-border Truck
    incoterm       TEXT CHECK (incoterm IS NULL OR incoterm IN ('EXW','FCA','FAS','FOB','CFR','CIF','CPT','CIP','DAP','DPU','DDP','DDU')),
    service_scope  TEXT,                          -- setting 'service_scopes': Door to door, Door to port, ...
    pod            TEXT,                          -- port of discharge (optional); v39: the readable copy of its places
                                                  -- ('SAN ANTONIO / VALPARAISO', enquiry_route_place side pod) written
                                                  -- by the repository, or the text as written when it names no place
    result         TEXT CHECK (result IS NULL OR result IN ('Won','Lost','Cancelled')),  -- v38: Cancelled
    result_on      TEXT,
    lost_reason    TEXT,                          -- setting 'closed_reasons_lost', or 'closed_reasons_cancelled' on a Cancelled route
    notes          TEXT,
    voided         INTEGER NOT NULL DEFAULT 0,    -- struck out, never deleted
    source         TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','mail','migration')),
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (enquiry_id, seq)
);
CREATE INDEX ix_enquiry_route_enquiry ON enquiry_route (enquiry_id, voided);

CREATE TABLE enquiry_route_place (                -- v39: the places of a route's Dest. location and Port of discharge
    id           INTEGER PRIMARY KEY,             --, up to four each, shown with 'or': any of them will do,
    route_id     INTEGER NOT NULL REFERENCES enquiry_route(id) ON DELETE CASCADE,  -- so it is one route, one quote
    side         TEXT NOT NULL CHECK (side IN ('dest','pod')),
    seq          INTEGER NOT NULL CHECK (seq >= 1),   -- the order: dest seq 1 is enquiry_route.dest_loc_id, written by
    location_id  INTEGER NOT NULL REFERENCES master_location(id),  -- the repository with the rest of the list
    voided       INTEGER NOT NULL DEFAULT 0,      -- taken off the list: struck out, never deleted
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (route_id, side, seq)
);
CREATE INDEX ix_enquiry_route_place_route ON enquiry_route_place (route_id, side, voided);

CREATE TABLE case_note (                          -- v32: Iris's quick notes on a case. Private
    id          INTEGER PRIMARY KEY,              -- working notes: not a timeline event, not activity for any rule,
    enquiry_id  INTEGER NOT NULL REFERENCES enquiry(id) ON DELETE CASCADE,  -- not exported, not in the report
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    deleted_at  TEXT                              -- NULL while shown; Delete sets it, nothing is removed
);
CREATE INDEX ix_case_note_enquiry ON case_note (enquiry_id, deleted_at);

CREATE TABLE site_action (                        -- v43 (W1b): what people did on the team site (Firestore actions/{id}), taken in
    id            TEXT PRIMARY KEY,               -- once: the action's document id; a second pull of it is skipped
    type          TEXT NOT NULL,                  -- assign / quote / followup / waiting / answered, or what the site sent
    opp_no        TEXT,                           -- the case number the action named (the key on the site)
    enquiry_id    INTEGER REFERENCES enquiry(id), -- NULL when OppList has no such case
    by_email      TEXT,                           -- the person: their login, their name on the site, their Handled by name
    by_name       TEXT,
    by_owner      TEXT,
    at            TEXT NOT NULL,                  -- the action's time (UTC, ISO), the pull's order
    fields        TEXT,                           -- the action's own fields, JSON
    result        TEXT NOT NULL CHECK (result IN ('applied','refused')),
    message       TEXT,                           -- what actionResults/{id}.msg says to the person
    event_ids     TEXT,                           -- JSON list of the status_event ids it wrote
    change_action INTEGER,                        -- change_log.action_id of its writes (E2)
    taken_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    answered_at   TEXT                            -- when actionResults/{id} was written; NULL = still to answer
);
CREATE INDEX ix_site_action_case ON site_action (enquiry_id);

CREATE TABLE site_note (                          -- v43 (W1b): a team-site note (Firestore notes/{id}) with a case, as the
    id            TEXT PRIMARY KEY,               -- quick note it became ('<name>: <text>'); a later edit of its text
    enquiry_id    INTEGER NOT NULL REFERENCES enquiry(id),  -- updates that quick note. Notes without a case are not here
    opp_no        TEXT NOT NULL,
    case_note_id  INTEGER NOT NULL REFERENCES case_note(id),
    by_email      TEXT,
    by_name       TEXT,
    updated_at    TEXT,                           -- the note's last updatedAt taken in (UTC, ISO)
    taken_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX ix_site_note_case_note ON site_note (case_note_id);

CREATE TABLE shipment (                           -- v24: one shipment per won case; v33: per won route. Nothing
    id                INTEGER PRIMARY KEY,        -- here talks to a carrier system: every fact came from a mail or was typed in
    enquiry_id        INTEGER NOT NULL REFERENCES enquiry(id) ON DELETE CASCADE,
    route_id          INTEGER UNIQUE REFERENCES enquiry_route(id),  -- v33: the route it ships (existing shipments: route A)
    mode              TEXT NOT NULL CHECK (mode IN ('ocean','air','truck','sea_air','cross_border_truck')),  -- v48: Sea-Air, Cross-border Truck
    title             TEXT NOT NULL,              -- the lane text
    shipper_name      TEXT,
    shipper_address   TEXT,
    consignee_name    TEXT,
    consignee_address TEXT,
    carrier           TEXT,
    carrier_detail    TEXT,                       -- vessel / voyage, flight, truck type
    booking_no        TEXT,
    mbl               TEXT,
    hbl               TEXT,
    awb               TEXT,
    our_file          TEXT,
    agent_ref         TEXT,
    load_no           TEXT,
    bol               TEXT,
    customer_ref      TEXT,
    price_note        TEXT,
    special           TEXT,                       -- 'liftgate at both ends'
    via_points        TEXT,                       -- v27: 'New York; Suez' - names resolved against master_location for the map
    planned_pickup    TEXT,                       -- dates, ISO
    planned_departure TEXT,
    planned_arrival   TEXT,
    planned_delivery  TEXT,
    eta               TEXT,                       -- the latest expected arrival
    remarks           TEXT,
    closed            INTEGER NOT NULL DEFAULT 0,
    closed_reason     TEXT CHECK (closed_reason IN ('Delivered','Not shipped','Registered in error')),  -- v26
    created_at        TEXT,
    updated_at        TEXT
);

CREATE TABLE shipment_event (                     -- v24: a milestone, an issue, a note or an info line on a shipment
    id                INTEGER PRIMARY KEY,
    shipment_id       INTEGER NOT NULL REFERENCES shipment(id) ON DELETE CASCADE,
    at                TEXT NOT NULL,              -- 'YYYY-MM-DD HH:MM'
    kind              TEXT NOT NULL CHECK (kind IN ('milestone','issue','note','info')),
    milestone         TEXT,                       -- one of the mode's milestone names (shipments.MILESTONES), or NULL
    title             TEXT NOT NULL,
    detail            TEXT,
    eta               TEXT,                       -- a date when the event moved the ETA
    source            TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','mail')),
    message_id        TEXT,
    sender            TEXT,
    resolves_event_id INTEGER REFERENCES shipment_event(id),  -- set on the event that closes an issue
    voided            INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT
);
CREATE INDEX ix_shipment_event_shipment ON shipment_event (shipment_id, at);

CREATE TABLE tracking_snooze (
    id        INTEGER PRIMARY KEY,
    rule      TEXT NOT NULL,                      -- R1..R10
    ref_kind  TEXT NOT NULL CHECK (ref_kind IN ('enquiry','party','vendor_request','shipment')),  -- v24 adds shipment (R10)
    ref_id    INTEGER NOT NULL,
    until     TEXT NOT NULL,                      -- date
    UNIQUE (rule, ref_kind, ref_id)
);

CREATE TABLE setting (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL                          -- JSON for lists, plain text for scalars
);

INSERT INTO setting (key, value) VALUES
 ('schema_version', '6'),
 ('next_opp_number', '1401'),
 ('owners', '["Victor","Iris","Colin","Ava","Other"]'),
 -- Our own mail domains. A mail from one of these to somebody outside them is
 -- us asking a vendor for a rate, not an enquiry arriving.
 ('internal_domains', '["jojofreight.example"]'),
 -- The word in front of an office code in an office party's key
 -- ('JoJo JQM'); opplist/branding.py has the same default.
 ('company_short', 'JoJo'),
 ('container_types', '["20GP","40GP","40HQ","45HQ","20OT","40OT","20FR","40FR","20RF","40RF","40NOR","Mixed","Not specified"]'),
 -- v39: the cargo line's Packing dropdown; the Settings page edits it like container_types
 ('packing_units', '["Pallets","Skids","Crates","Cases","Boxes","Cartons","Packages","Pieces","Drums","Bags","Big bags","Rolls","Bundles","Units"]'),
 ('special_handling', '["OOG","Overweight","DG","Reefer","Temp Control","SOC","Live Animal","Transload","Flatbed","Non-stackable","Drayage"]'),
 -- v17: separate vocabularies, because the closings answer different
 -- questions. Lost = the shipment went ahead without us; Cancelled (v38) =
 -- the shipment did not go ahead at all; Dropped = this row should not
 -- exist. The old single list is kept for the workbook import,
 -- which reads the 2026 sheet's own words.
 ('closed_reasons_lost', '["Price too high","Rate too slow","No response from customer","Unable to handle","No vendor rate","Went with another forwarder","Customer chose the other mode","Other"]'),
 ('closed_reasons_cancelled', '["Shipment cancelled","Order not confirmed","Postponed with no new date","Other"]'),
 ('closed_reasons_dropped', '["Duplicate of OPP-xxxx","Registered in error"]'),
 -- v33: which part of a route we do
 ('service_scopes', '["Door to door","Door to port","Port to door","Port to port"]'),
 -- v34: the cargo checks' limits; the Settings page edits them
 ('cargo_limits', '{"density_min":30,"density_max":3000,"air_max_cbm":30,"air_max_kg":20000,"lcl_max_cbm":25,"lcl_max_kg":15000,"fcl_max_kg_20":20000,"fcl_max_kg_40":25000,"tolerance_pct":10}'),
 -- v22: why a quote was done again; the Settings page edits it like the closed reasons
 ('requote_reasons', '["Lower rate","Request changed","Rates expired","Different routing or carrier","Extra option asked","Correction","Other"]'),
 ('closed_reasons', '["Price","Transit Time","Space","Out of Scope","No Response from Customer","Customer Cancelled","Not Quoted","Unable to Handle","Duplicate","Registered in error","Other"]'),
 ('latam_countries', '["Chile","Peru","Brazil","Colombia","Uruguay","Ecuador","Bolivia","Argentina","Venezuela","Paraguay","El Salvador","Guatemala","Honduras","Panama","Costa Rica","Nicaragua","Puerto Rico","Mexico","Dominican Republic","Belize"]'),
 ('cadence_days_default', '45'),
 ('rule_new_days', '1'),
 ('rule_vendor_no_reply_days', '2'),
 ('rule_quoted_no_answer_days', '7'),
 -- v13: the follow-up ladder on a quote: working days from the
 -- clock (quoted / last follow-up / last note) to FU1, to FU2, to Decide;
 -- fu_max follow-ups before it is time to decide. The three mail templates.
 ('fu1_days', '1'),
 ('fu2_days', '5'),
 ('fu_decide_days', '5'),
 ('fu_max', '2'),
 -- The pool: versions separated by a line of ---, rotated per requester and
 -- step; blanks {first_name} {rates} {client} {valid_until_line}
 -- {open_question_line} (opplist/followup.py). Same text as
 -- schema_migrations.FU_SETTINGS.
 ('fu_template_1', 'Hi {first_name},

Are {rates} workable for this shipment? We can book as soon as you give the go-ahead, and we are glad to rework anything that does not fit.
{valid_until_line}
{open_question_line}
Thank you.
---
Hi {first_name},

Following up on {rates}: do they suit what you need? If you want to go ahead, or if another option should be priced, just say the word.
{valid_until_line}
{open_question_line}
Thank you.
---
Hi {first_name},

Checking back to ask how {rates} sit with you. Should a figure need another look, or should we check something else, send us a line and we will get onto it.
{valid_until_line}
{open_question_line}
Thank you.
---
Hi {first_name},

Hoping your week is going well. We are circling back to find out whether {rates} are a fit for {client}. Any feedback is welcome, and any piece of the offer can be revisited.
{valid_until_line}
{open_question_line}
Thank you.
---
Hi {first_name},

Following up with a short reminder about {rates}. Do the numbers work for {client}? If a leg or a charge should be priced again, we can do that quickly.
{valid_until_line}
{open_question_line}
Thank you.'),
 ('fu_template_2', 'Hi {first_name},

Following up on our offer below, which we have not heard back on yet. Is it still being weighed, or has the cargo plan moved on?
Thank you.
---
Hi {first_name},

Any update on where the offer below stands? If your side is still comparing, no rush; if the plan has been dropped, a short note is enough for us to close it.
Thank you.
---
Hi {first_name},

Checking back once more on the offer below. Is the move still going ahead? If something on your end has shifted, feel free to tell us.
Thank you.
---
Hi {first_name},

Is the cargo below still on track? When the dates slip, we are glad to price the move again once the ready date is firm.
We look forward to hearing from you.
---
Hi {first_name},

Touching base about the offer below. Could you tell us whether this shipment is live or paused for now? We can go over the figures again nearer the time it is ready.
Thank you.
---
Hi {first_name},

Following up to ask whether anything has moved on the cargo below. If the timeline is different now, send us the new dates and we will bring the offer up to date.
We look forward to hearing from you.'),
 ('fu_template_3', 'Hi {first_name},

Thanks once more for inviting us to quote. Has the business been awarded yet? If it has, a word on how our offer measured up would help us sharpen the next one.
Thank you.
---
Hi {first_name},

The shipment below has probably been placed by now. If it went to someone else, we would value hearing how far off we were, so the next offer lands closer.
Thank you.
---
Hi {first_name},

We appreciated the chance to bid on this one. Is there a final word on the shipment? Whichever way it went, your comments would help us serve your next requests better.
Thank you.
---
Hi {first_name},

Could you share how the bid below turned out? If another offer was chosen, was it mainly the price, the routing or something else? Knowing that lets us quote better next time.
Thank you.'),
 ('dedupe_window_days', '60'),
 ('export_dir', 'workbooks'),
 ('export_share_dir', ''),
 -- Where an export lands. 'preview' writes opplist\export-preview\ and touches
 -- nothing the PowerShell pipeline reads; 'publish' writes workbooks\ and data\.
 -- Stays 'preview' until the app's exports are meant to feed the pipeline.
 ('export_target', 'preview'),
 -- Re-export within a minute of a change. Pointless while the export is only a
 -- preview, so it is only acted on when export_target = 'publish'.
 ('auto_export', '1'),
 ('backup_keep', '30'),
 -- v14: the version of the v_master_* contract views
 ('master_contract_version', '1'),
 ('port', '8765');

-- ---------------------------------------------------------------- derived views
-- Everything the workbook used to compute with formulas lives here, so the app and the exporter share one definition.

-- v36 (batch 20): the LATAM countries, read from the latam_countries setting
-- (Settings) every time a view is read - nothing is cached, an edit of the
-- list changes LATAM everywhere at once. The country table keeps its regions
-- (geography, the exports' Region columns), but LATAM is this list only.
CREATE VIEW v_latam_country AS
SELECT DISTINCT TRIM(j.value) AS name
FROM setting s, json_each(s.value) j
WHERE s.key = 'latam_countries' AND json_valid(s.value) AND TRIM(j.value) <> '';

-- v45 (batch 17m): the FCL cargo lines v_enquiry.teu reads - every live line
-- (voided 0, counted) with a container type that is set and not 'Not specified',
-- with its containers' TEU each read from the type as written (spaces and the
-- quote marks out, upper case: 20... is 1, 40... or 45... is 2, anything else,
-- Mixed among them, unknown) and the live routes it counts on, as routes.totals
-- counts containers: the recorded route_ids still live, else every live route,
-- once when the case has none. A helper of v_enquiry only.
CREATE VIEW v_cargo_teu_line AS
SELECT c.id                                                          AS line_id,
       c.enquiry_id,
       c.container_qty                                               AS qty,
       CASE WHEN UPPER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(c.container_type, ' ', ''), '''', ''), '’', ''), '‘', ''), '"', '')) LIKE '20%' THEN 1 WHEN UPPER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(c.container_type, ' ', ''), '''', ''), '’', ''), '‘', ''), '"', '')) LIKE '40%' OR UPPER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(c.container_type, ' ', ''), '''', ''), '’', ''), '‘', ''), '"', '')) LIKE '45%' THEN 2 ELSE NULL END                                                         AS per_container,
       COALESCE(NULLIF((SELECT COUNT(*) FROM enquiry_route r WHERE r.enquiry_id = c.enquiry_id AND r.voided = 0
                          AND ',' || REPLACE(COALESCE(c.route_ids, ''), ' ', '') || ',' LIKE '%,' || r.id || ',%'), 0),
                NULLIF((SELECT COUNT(*) FROM enquiry_route r WHERE r.enquiry_id = c.enquiry_id AND r.voided = 0), 0),
                1)                                                   AS routes
FROM enquiry_cargo c
WHERE c.voided = 0 AND c.counted <> 0 AND TRIM(COALESCE(c.container_type, '')) <> ''
  AND UPPER(TRIM(c.container_type)) <> 'NOT SPECIFIED';

-- v58 (FX7): each wait for the requester (WR1) - a live 'Waiting for requester:' note - from its day
-- (since) to the day it ended (until, '9999-12-31' while live): the first live event after the note
-- that ends it as wait.py reads it - a status or requote event (the first Quoted among them), a
-- 'Reply from' or 'Info received' note - or a vendor request asked after it. 'After' on the note's own
-- day is the order the rows were written in. No semicolon in these comments: the view rebuild splits on them.
CREATE VIEW v_wait AS
SELECT w.id AS id, w.enquiry_id AS enquiry_id, substr(w.at, 1, 10) AS since,
       MIN(COALESCE((SELECT MIN(substr(x.at, 1, 10)) FROM status_event x
                     WHERE x.enquiry_id = w.enquiry_id AND x.voided = 0 AND x.id <> w.id
                       AND (substr(x.at, 1, 10) > substr(w.at, 1, 10)
                            OR (substr(x.at, 1, 10) = substr(w.at, 1, 10) AND x.id > w.id))
                       AND (x.kind IN ('status', 'requote')
                            OR (x.kind = 'note' AND (x.note LIKE 'Reply from%' OR x.note LIKE 'Info received%')))),
                    '9999-12-31'),
           COALESCE((SELECT MIN(substr(v.asked_on, 1, 10)) FROM vendor_request v
                     WHERE v.enquiry_id = w.enquiry_id AND v.voided = 0 AND COALESCE(v.asked_on, '') <> ''
                       AND substr(v.asked_on, 1, 10) >= substr(w.at, 1, 10)
                       AND (substr(v.asked_on, 1, 10) > substr(w.at, 1, 10)
                            OR COALESCE(v.created_at, '') >= COALESCE(w.created_at, ''))),
                    '9999-12-31'))                                   AS until
FROM status_event w
WHERE w.kind = 'note' AND w.voided = 0 AND w.note LIKE 'Waiting for requester:%';

-- v58 (FX7): a follow-up dated within a wait (its day on or after the wait note's, on or before the
-- day the wait ended, the quote's own day included) is the wait's: on our question, never the quote's.
CREATE VIEW v_wait_followup AS
SELECT f.id AS id, f.enquiry_id AS enquiry_id, w.id AS wait_event_id, w.since AS wait_since
FROM status_event f
JOIN v_wait w ON w.enquiry_id = f.enquiry_id
WHERE f.kind = 'followup' AND f.voided = 0
  AND substr(f.at, 1, 10) >= w.since AND substr(f.at, 1, 10) <= w.until;

CREATE VIEW v_enquiry AS
SELECT e.*,
       substr(e.received_on, 1, 7)                                   AS month,
       rp.key                                                        AS requester_key,
       rp.type                                                       AS requester_type,
       rp.country                                                    AS requester_country,
       rc.region                                                     AS requester_region,
       CASE rp.type WHEN 'Office' THEN 'Office' ELSE 'Agent' END AS source_type_legacy,
       oc.region                                                     AS origin_region,
       dc.region                                                     AS dest_region,
       -- v36: LATAM is the latam_countries list of Settings (v_latam_country), not the region
       CASE WHEN e.origin_country IN (SELECT name FROM v_latam_country) OR e.dest_country IN (SELECT name FROM v_latam_country)
            THEN 'Y' ELSE 'N' END                                    AS latam_lane,
       CASE WHEN rp.type = 'Agent' AND rp.country IN (SELECT name FROM v_latam_country)
            THEN 'Y' ELSE 'N' END                                    AS latam_agent,
       -- v47 (DC1): a Direct Customer in a LATAM country too, as an Agent there
       -- v49 (SC1): her choice (latam_choice) first, when she made one
       CASE WHEN e.latam_choice IN ('Y', 'N') THEN e.latam_choice
            WHEN e.origin_country IN (SELECT name FROM v_latam_country) OR e.dest_country IN (SELECT name FROM v_latam_country)
              OR (rp.type IN ('Agent', 'Direct Customer') AND rp.country IN (SELECT name FROM v_latam_country))
            THEN 'Y' ELSE 'N' END                                    AS latam,
       CASE
         WHEN e.origin_country IS NULL OR e.dest_country IS NULL THEN NULL
         WHEN e.origin_country = 'Unknown' OR e.dest_country = 'Unknown' THEN 'To Confirm'
         WHEN e.origin_country = 'USA' AND e.dest_country = 'USA' THEN 'US Domestic'
         WHEN e.origin_country = 'USA' THEN 'US Export'
         WHEN e.dest_country = 'USA' THEN 'US Import'
         ELSE 'Cross-trade' END                                      AS direction,
       -- v45 (17m): from the case's FCL cargo lines (v_cargo_teu_line): the sum of
       -- containers x TEU each x routes, empty when any of those lines has no count
       -- or a type it cannot read. A case with no such line reads its own type
       -- (the same way) times its own container_qty
       CASE WHEN EXISTS (SELECT 1 FROM v_cargo_teu_line l WHERE l.enquiry_id = e.id)
            THEN (SELECT CASE WHEN SUM(CASE WHEN COALESCE(l.qty, 0) = 0 OR l.per_container IS NULL THEN 1 ELSE 0 END) > 0
                              THEN NULL ELSE SUM(l.qty * l.per_container * l.routes) END
                  FROM v_cargo_teu_line l WHERE l.enquiry_id = e.id)
            ELSE CASE WHEN UPPER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(e.container_type, ' ', ''), '''', ''), '’', ''), '‘', ''), '"', '')) LIKE '20%' THEN 1 WHEN UPPER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(e.container_type, ' ', ''), '''', ''), '’', ''), '‘', ''), '"', '')) LIKE '40%' OR UPPER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(e.container_type, ' ', ''), '''', ''), '’', ''), '‘', ''), '"', '')) LIKE '45%' THEN 2 ELSE NULL END * e.container_qty END                         AS teu,
       -- Dates the record really carries. A migrated case that never had a quote
       -- or close date still gets an event so the timeline reads in order, but
       -- that date is a guess (approx = 1) and must not leave the app: exports
       -- and the monthly report read quoted_on / closed_on, never the _any pair.
       (SELECT MIN(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.status = 'Quoted' AND s.approx = 0 AND s.kind IN ('status','requote') AND s.voided = 0)       AS quoted_on,
       (SELECT MIN(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.status IN ('Won','Lost','Cancelled','Dropped') AND s.approx = 0 AND s.kind = 'status' AND s.voided = 0)   AS closed_on,
       -- The same dates including the approximate ones, for screens and rules
       -- that need to place a case on the timeline at all.
       (SELECT MIN(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.status = 'Quoted' AND s.kind IN ('status','requote') AND s.voided = 0)                        AS quoted_on_any,
       (SELECT MIN(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.status IN ('Won','Lost','Cancelled','Dropped') AND s.kind = 'status' AND s.voided = 0)                    AS closed_on_any,
       (SELECT MAX(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.voided = 0)                                                             AS last_event_on,
       COALESCE(e.group_id, e.id)                                    AS request_group,
       (SELECT COUNT(*) FROM vendor_request v WHERE v.enquiry_id = e.id AND v.voided = 0)                     AS vendor_count,
       (SELECT COUNT(*) FROM vendor_request v WHERE v.enquiry_id = e.id AND v.quoted = 1 AND v.voided = 0)    AS vendor_quoted_count,
       -- v13: the follow-ups sent on the quote, voided ones count for nothing.
       -- v16: the first three are this quote round only - a case quoted again starts at
       -- FU1 - and a follow-up dated on or after the latest live Quoted event belongs to
       -- the round it opened. fu_count_total is every follow-up the case ever had.
       -- v58 (FX7): a follow-up of a wait for the requester (v_wait_followup) is never the quote's.
       (SELECT COUNT(*) FROM status_event s WHERE s.enquiry_id = e.id AND s.kind = 'followup' AND s.voided = 0
          AND s.at >= COALESCE((SELECT MAX(q.at) FROM status_event q WHERE q.enquiry_id = e.id
                                 AND q.kind IN ('status','requote') AND q.status = 'Quoted' AND q.voided = 0), '') AND NOT EXISTS (SELECT 1 FROM v_wait_followup x WHERE x.id = s.id))   AS fu_count,
       (SELECT MAX(at) FROM status_event s WHERE s.enquiry_id = e.id AND s.kind = 'followup' AND s.voided = 0
          AND s.at >= COALESCE((SELECT MAX(q.at) FROM status_event q WHERE q.enquiry_id = e.id
                                 AND q.kind IN ('status','requote') AND q.status = 'Quoted' AND q.voided = 0), '') AND NOT EXISTS (SELECT 1 FROM v_wait_followup x WHERE x.id = s.id))   AS last_fu_on,
       (SELECT MAX(seq) FROM status_event s WHERE s.enquiry_id = e.id AND s.kind = 'followup' AND s.voided = 0
          AND s.at >= COALESCE((SELECT MAX(q.at) FROM status_event q WHERE q.enquiry_id = e.id
                                 AND q.kind IN ('status','requote') AND q.status = 'Quoted' AND q.voided = 0), '') AND NOT EXISTS (SELECT 1 FROM v_wait_followup x WHERE x.id = s.id))   AS last_fu_seq,
       (SELECT COUNT(*) FROM status_event s WHERE s.enquiry_id = e.id AND s.kind = 'followup' AND s.voided = 0)   AS fu_count_total,
       -- v18: quote rounds. v30 (Re-quote means re-quoted): every live event
       -- with status Quoted is a quote, whatever its kind - a requote event Iris writes with
       -- the Re-quoted button is status Quoted, so quoted_on, the follow-up round above and
       -- quote_rounds all read kind status and kind requote alike. in_requote is only for old
       -- data: a live requote event with status New or Sourcing (batch 8 wrote New, v19 to v28
       -- wrote Sourcing) and no live Quoted event after it (later date, or the same day with a
       -- higher id - the order status.quote_rounds reads), on a case that is not closed. A
       -- requote event whose own status is Quoted opens and closes its round at once.
       -- No semicolon in these comments: the view rebuild splits on them.
       (SELECT COUNT(*) FROM status_event s WHERE s.enquiry_id = e.id AND s.kind IN ('status','requote')
          AND s.status = 'Quoted' AND s.voided = 0)                                                                 AS quote_rounds,
       CASE WHEN e.status NOT IN ('Won','Lost','Cancelled','Dropped')
             AND EXISTS (SELECT 1 FROM status_event r WHERE r.enquiry_id = e.id AND r.voided = 0
                           AND r.kind = 'requote' AND r.status IN ('New','Sourcing')
                           AND NOT EXISTS (SELECT 1 FROM status_event q WHERE q.enquiry_id = e.id
                                             AND q.voided = 0 AND q.kind IN ('status','requote')
                                             AND q.status = 'Quoted'
                                             AND (q.at, q.id) > (r.at, r.id)))
            THEN 1 ELSE 0 END                                                                                      AS in_requote,
       -- v33: routes and the two ways a shipment is charged
       (SELECT COUNT(*) FROM enquiry_route r WHERE r.enquiry_id = e.id AND r.voided = 0)                     AS route_count,
       CASE WHEN e.service_mode = 'Air' AND (e.gross_kg IS NOT NULL OR e.volume_cbm IS NOT NULL)
            THEN MAX(COALESCE(e.gross_kg, 0), COALESCE(e.volume_cbm, 0) * 1000000.0 / 6000) END             AS chargeable_kg,
       CASE WHEN e.service_mode = 'LCL' AND (e.gross_kg IS NOT NULL OR e.volume_cbm IS NOT NULL)
            THEN MAX(COALESCE(e.volume_cbm, 0), COALESCE(e.gross_kg, 0) / 1000.0) END                        AS wm
FROM enquiry e
JOIN party rp ON rp.id = e.requester_party_id
LEFT JOIN country rc ON rc.name = rp.country
LEFT JOIN country oc ON oc.name = e.origin_country
LEFT JOIN country dc ON dc.name = e.dest_country;

-- v33: one row per live route with the lane values v_enquiry derives for a
-- case, per route (Top lanes, the LATAM map, the direction split, the region
-- counts and lost reasons count routes; requests and win rate count OPPs)
CREATE VIEW v_route AS
SELECT r.*,
       CHAR(64 + r.seq)                                              AS letter,
       e.opp_no, e.status, e.received_on, e.scope, e.legacy, e.handled_by, e.requester_party_id,
       substr(e.received_on, 1, 7)                                   AS month,
       rp.key                                                        AS requester_key,
       rp.type                                                       AS requester_type,
       rc.region                                                     AS requester_region,
       oc.region                                                     AS origin_region,
       dc.region                                                     AS dest_region,
       -- v36: LATAM is the latam_countries list of Settings (v_latam_country), not the region
       CASE WHEN r.origin_country IN (SELECT name FROM v_latam_country) OR r.dest_country IN (SELECT name FROM v_latam_country)
            THEN 'Y' ELSE 'N' END                                    AS latam_lane,
       CASE WHEN rp.type = 'Agent' AND rp.country IN (SELECT name FROM v_latam_country)
            THEN 'Y' ELSE 'N' END                                    AS latam_agent,
       -- v47 (DC1): a Direct Customer in a LATAM country too, as an Agent there
       -- v49 (SC1): the case's choice (latam_choice) first, when she made one
       CASE WHEN e.latam_choice IN ('Y', 'N') THEN e.latam_choice
            WHEN r.origin_country IN (SELECT name FROM v_latam_country) OR r.dest_country IN (SELECT name FROM v_latam_country)
              OR (rp.type IN ('Agent', 'Direct Customer') AND rp.country IN (SELECT name FROM v_latam_country))
            THEN 'Y' ELSE 'N' END                                    AS latam,
       CASE
         WHEN r.origin_country IS NULL OR r.dest_country IS NULL THEN NULL
         WHEN r.origin_country = 'Unknown' OR r.dest_country = 'Unknown' THEN 'To Confirm'
         WHEN r.origin_country = 'USA' AND r.dest_country = 'USA' THEN 'US Domestic'
         WHEN r.origin_country = 'USA' THEN 'US Export'
         WHEN r.dest_country = 'USA' THEN 'US Import'
         ELSE 'Cross-trade' END                                      AS direction
FROM enquiry_route r
JOIN enquiry e ON e.id = r.enquiry_id
JOIN party rp ON rp.id = e.requester_party_id
LEFT JOIN country rc ON rc.name = rp.country
LEFT JOIN country oc ON oc.name = r.origin_country
LEFT JOIN country dc ON dc.name = r.dest_country
WHERE r.voided = 0;

CREATE VIEW v_party_stats AS
SELECT p.*,
       c.region                                                       AS region,
       -- v36: in a country of the LATAM list, and a LATAM agent (an Agent there)
       CASE WHEN p.country IN (SELECT name FROM v_latam_country) THEN 'Y' ELSE 'N' END                  AS latam,
       CASE WHEN p.type = 'Agent' AND p.country IN (SELECT name FROM v_latam_country) THEN 'Y' ELSE 'N' END AS latam_agent,
       (SELECT COUNT(*) FROM enquiry e WHERE e.requester_party_id = p.id)                        AS total_enquiries,
       (SELECT COUNT(*) FROM enquiry e WHERE e.requester_party_id = p.id AND e.status = 'Won')   AS won,
       (SELECT MIN(received_on) FROM enquiry e WHERE e.requester_party_id = p.id)               AS first_enquiry_on,
       (SELECT MAX(received_on) FROM enquiry e WHERE e.requester_party_id = p.id)               AS last_enquiry_on,
       (SELECT COUNT(*) FROM vendor_request v WHERE v.vendor_party_id = p.id AND v.voided = 0)                   AS asked_count,
       (SELECT COUNT(*) FROM vendor_request v WHERE v.vendor_party_id = p.id AND v.quoted = 1 AND v.voided = 0)  AS quoted_count,
       (SELECT COUNT(*) FROM vendor_request v WHERE v.vendor_party_id = p.id AND v.awarded = 1 AND v.voided = 0) AS awarded_count,
       (SELECT MAX(x) FROM (
            SELECT MAX(received_on) AS x FROM enquiry e WHERE e.requester_party_id = p.id
            UNION ALL SELECT MAX(at) FROM party_event pe WHERE pe.party_id = p.id
            UNION ALL SELECT MAX(COALESCE(replied_on, asked_on)) FROM vendor_request v WHERE v.vendor_party_id = p.id AND v.voided = 0))  AS last_activity_on
FROM party p
LEFT JOIN country c ON c.name = p.country;


-- ---------------------------------------------------------------- master contract views (v14)
-- What the Rate Desk reads through a read-only ATTACH. Columns are only ever
-- added, never removed or renamed; a change bumps setting master_contract_version.
CREATE VIEW v_master_location AS
SELECT l.id, l.key, l.name, l.type, l.country, l.country_code, l.subdivision,
       l.parent_id, p.key AS parent_key, l.area_id, a.key AS area_key,
       l.unlocode, l.iata, l.is_seaport, l.is_airport, l.status,
       l.no_parent                                   -- v42 (RD1): added, nothing moved
FROM master_location l
LEFT JOIN master_location p ON p.id = l.parent_id
LEFT JOIN master_area a ON a.id = l.area_id;

CREATE VIEW v_master_alias AS
SELECT alias, location_id, kind FROM master_location_alias;

CREATE VIEW v_master_party AS
SELECT p.id, p.key, COALESCE(NULLIF(p.full_name,''), p.key) AS name, p.type, p.roles,
       p.country, p.city, p.mfn_member, p.stage, p.placeholder, p.merged_into,
       p.group_id, g.key AS group_key, g.name AS group_name
FROM party p LEFT JOIN party_group g ON g.id = p.group_id
WHERE p.merged_into IS NULL;

-- R0 (2026-09-17, schema v15): the demand side the Rate Desk reads - which
-- lanes were asked for and which vendors were asked. Contract version stays 1
-- (columns added, none removed).
CREATE VIEW v_master_enquiry_lane AS
SELECT e.id, e.opp_no, e.group_id, e.received_on, e.status, e.scope, e.service_mode,
       e.origin_loc_id, e.dest_loc_id, e.origin_country, e.dest_country, e.incoterm,
       e.requester_party_id, e.awarded_vendor_id, e.rate_used
FROM enquiry e;

CREATE VIEW v_master_vendor_request AS
SELECT vr.id, vr.enquiry_id, e.opp_no, vr.vendor_party_id, vr.asked_on, vr.replied_on,
       vr.quoted, vr.awarded, vr.voided,
       vr.route_ids                                  -- v42 (RD1): ',' separated enquiry_route ids, NULL = every route
FROM vendor_request vr JOIN enquiry e ON e.id = vr.enquiry_id;

-- RD1 (2026-10-19, schema v42): the demand side per route - one row per
-- route, place and cargo line, voided ones included (readers filter).
-- Contract version stays 1 (views and columns added, nothing removed).
CREATE VIEW v_master_route_lane AS
SELECT r.id AS route_id, r.enquiry_id, e.opp_no, r.seq, CHAR(64 + r.seq) AS letter,
       e.received_on, e.status, e.scope, r.service_mode, r.origin_loc_id, r.dest_loc_id,
       r.origin_country, r.dest_country, r.incoterm, r.service_scope, r.pod,
       r.result, r.result_on, e.requester_party_id, e.awarded_vendor_id, r.voided
FROM enquiry_route r JOIN enquiry e ON e.id = r.enquiry_id;

CREATE VIEW v_master_route_place AS
SELECT route_id, side, seq, location_id, voided FROM enquiry_route_place;

CREATE VIEW v_master_enquiry_cargo AS
SELECT c.id, c.enquiry_id, e.opp_no, c.seq, c.route_ids, c.counted, c.voided,
       c.pieces, c.pack_unit, c.weight_kg, c.cbm, c.container_type, c.container_qty,
       c.per_month, c.months
FROM enquiry_cargo c JOIN enquiry e ON e.id = c.enquiry_id;
