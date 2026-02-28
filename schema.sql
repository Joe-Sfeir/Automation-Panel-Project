-- ============================================================
--  TECHNELEC PRICING DATABASE  -  schema.sql
--  SQLite  |  Created 2026
-- ============================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ── 1. MANUFACTURERS ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS manufacturers (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT    NOT NULL UNIQUE   -- 'SCHNEIDER', 'EATON', 'LEGRAND' …
);

-- ── 2. PRICE LIST EDITIONS ───────────────────────────────────
CREATE TABLE IF NOT EXISTS ppl_editions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    manufacturer_id INTEGER NOT NULL,
    year            INTEGER NOT NULL,
    half            TEXT    NOT NULL CHECK(half IN ('H1','H2')),
    currency        TEXT    NOT NULL CHECK(currency IN ('EUR','USD')),
    imported_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(manufacturer_id, year, half),
    FOREIGN KEY (manufacturer_id) REFERENCES manufacturers(id)
);

-- ── 3. PRODUCTS ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS products (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ppl_edition_id INTEGER NOT NULL,
    reference      TEXT    NOT NULL,
    order_number   TEXT,                   -- manufacturer order/catalog number (optional)
    description    TEXT    NOT NULL,
    price          REAL    NOT NULL,
    UNIQUE(ppl_edition_id, reference),
    FOREIGN KEY (ppl_edition_id) REFERENCES ppl_editions(id)
);

CREATE INDEX IF NOT EXISTS idx_products_reference ON products(reference);
CREATE INDEX IF NOT EXISTS idx_products_edition   ON products(ppl_edition_id);

-- ── 4. PRODUCT KEYWORDS  (normalised tokens) ─────────────────
CREATE TABLE IF NOT EXISTS product_keywords (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    keyword    TEXT    NOT NULL,  -- lower-case, normalised token
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_kw_product ON product_keywords(product_id);
CREATE INDEX IF NOT EXISTS idx_kw_keyword ON product_keywords(keyword);

-- ── 5. TERM ALIASES  (teach the engine your shorthand) ───────
-- When we tokenise an offer description we replace every alias
-- with its canonical form before searching.
CREATE TABLE IF NOT EXISTS term_aliases (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    alias     TEXT NOT NULL UNIQUE COLLATE NOCASE,  -- what YOU write
    canonical TEXT NOT NULL                          -- what the PPL says
);

-- ── 6. MATCH LOG  (optional audit trail) ─────────────────────
CREATE TABLE IF NOT EXISTS match_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    matched_at      TEXT    DEFAULT (datetime('now')),
    offer_file      TEXT,
    offer_desc      TEXT,
    manufacturer    TEXT,
    product_id      INTEGER,
    reference       TEXT,
    score           INTEGER,  -- number of keyword hits
    unit_price      REAL,
    quantity        REAL,
    total_price     REAL,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

-- ── SEED: default aliases ─────────────────────────────────────
-- poles / wiring
INSERT OR IGNORE INTO term_aliases(alias, canonical) VALUES
  ('1p+n',  '1pn'),
  ('dpnn',  '1pn'),
  ('1p n',  '1pn'),
  ('2p+n',  '2pn'),
  ('1p',      '1p'),
  ('1 p',     '1p'),
  ('1pn',     '1p+n'),
  ('1p+n',    '1p+n'),
  ('1p n',    '1p+n'),
  ('2p',      '2p'),
  ('3p',      '3p'),
  ('4p',      '4p'),
  ('tpn',     '3p+n'),
  ('3p+n',    '3p+n'),
  ('3p n',    '3p+n'),
  -- current notation
  ('6ka',   '6000a'),
  ('10ka',  '10000a'),
  ('15ka',  '15000a'),
  ('25ka',  '25000a'),
  ('36ka',  '36000a'),
  ('50ka',  '50000a'),
  ('70ka',  '70000a'),
  -- poles written as "4x100a"  →  split handled in code
  -- trip unit
  ('tmd',   'tmd'),
  ('tm100d','tmd 100a'),
  ('tm160d','tmd 160a'),
  ('tm200d','tmd 200a'),
  ('tm250d','tmd 250a'),
  ('tm320d','tmd 320a'),
  ('tm400d','tmd 400a'),
  ('tm500d','tmd 500a'),
  ('tm600d','tmd 600a'),
  -- sensitivity
  ('30ma',  '30ma'),
  ('300ma', '300ma'),
  -- curve
  ('curve c', 'c'),
  ('courbe c','c'),
  -- casing shortcuts
  ('ic60n',  'ic60n'),
  ('ic60h',  'ic60h'),
  ('ic60l',  'ic60l'),
  ('ins40',  'ins40'),
  ('ins63',  'ins63'),
  ('ins100', 'ins100'),
  ('ins160', 'ins160'),
  ('ins250', 'ins250'),
  ('ins320', 'ins320'),
  ('ins400', 'ins400'),
  ('ins500', 'ins500'),
  ('ins630', 'ins630'),
  ('nsx100f','nsx100f'),
  ('nsx160f','nsx160f'),
  ('nsx250f','nsx250f'),
  ('nsx400f','nsx400f'),
  ('nsx630f','nsx630f'),
  ('nsx100n','nsx100n'),
  ('nsx100h','nsx100h'),
  ('nsx250n','nsx250n'),
  ('nsx250h','nsx250h'),
  -- product type aliases   
  ('rccb id','rccb'),
  ('mccb',   'mccb'),
  ('mcb',    'mcb'),
  ('isw',    'isw'),
  ('acti9',  'acti9'),
  
  -- Schneider Specific PPL Jargon (From your PDF)
  ('acti 9',     'acti9'),     -- Standardize brand line
  ('ic60',       'ic60n'),     -- Default to N if frame not specified
  ('c60',        'ic60n'),     -- Old model mapping
  ('dpn',        'idpn'),      -- DPN breaker mapping
  ('vigi',       'vigi'),      -- Keep vigi as keyword
  ('vigicompact','vigi'),
  ('block',      'block'),     -- For Vigi blocks
  
  -- Component Types
  ('mcb',     'mcb'),
  ('mccb',    'mccb'),
  ('rcbo',    'rcbo'),
  ('rccb',    'rccb'),
  ('elcb',    'rccb'),     -- Old terminology for Earth Leakage
  ('spd',     'spd'),
  ('surge',   'spd'),
  ('arrester','spd'),
  
  -- Switch Disconnectors (The 25A vs 40A issue you saw earlier)
  ('switch',     'switch'),
  ('isolator',   'switch'),
  ('isw',        'switch'),    -- Schneider iSW family
  ('ins',        'switch'),    -- Schneider Compact INS family
  ('interpact',  'switch'),    -- Schneider Interpact family
  ('disconnect', 'switch'),
  
  -- Measurements
  ('amp',     'a'),
  ('amps',    'a'),
  ('a',       'a'),
  ('ka',      'ka'),
  ('ma',      'ma'),

  -- Eaton Pole configurations
  ('1pole',     '1p'),
  ('2pole',     '2p'),
  ('3pole',     '3p'),
  ('4pole',     '4p'),
  ('3+n pole',  '3p+n'),
  ('1+n pole',  '1p+n'),
  
  -- Eaton breaking capacities
  ('4.5ka',     '4500a'),
  ('4,5ka',     '4500a'),
  ('25k',       '25000a'),
  
  -- Eaton specific families (optional, but helps!)
  ('mmc4',      'mcb'),
  ('mmc6',      'mcb'),
  ('fazt',      'mcb'),
  ('mmct',      'mcb'),
  ('pkzm0',     'mpcb'),
  ('pkzm4',     'mpcb'),
  ('mrcm',      'rccb');