# Electrical BOM Generator — Project Summary
### Date: February 2026 | For handoff / collaboration

---

## What This Project Does

We are building an automated system that:
1. Takes a Single Line Diagram (SLD) — an electrical drawing showing a panel's components
2. Uses AI (Google Gemini) to read the drawing and identify every component
3. Matches those components against a structured database of manufacturer products
4. Outputs a complete Bill of Materials (BOM) in Excel — with reference numbers, quantities, and prices

The end goal is that an electrical engineer drops an SLD image into the tool and gets back a
ready-to-use BOM in under a minute, instead of doing it manually.

---

## Current File Structure

```
project/
│
├── bom_gen.py              ← MAIN SCRIPT. Run this for every new project.
├── database.py             ← All database logic (called by bom_gen.py, never run directly)
├── parse_pricelist.py      ← ONE-TIME import tool. Loads manufacturer Excel price lists into DB.
├── .env                    ← YOUR API KEY LIVES HERE. Never share this file.
├── .env.example            ← Safe template showing what .env should look like
├── electrical_bom.db       ← The SQLite database. Single file. Contains everything.
└── README.md               ← Setup instructions
```

---

## The Database (electrical_bom.db)

This is a single SQLite file — no server needed, just a file on disk.
It contains 6 tables:

```
electrical_bom.db
│
├── manufacturers              ← One row per manufacturer (Legrand, Schneider, ABB...)
├── component_catalog          ← 13 MANUAL entries typed in by hand (OLD — to be retired)
│
├── price_list                 ← RAW price data from manufacturer Excel files
│                                 columns: manufacturer, reference, description_raw, price_euro, price_date
│
├── product_attributes         ← STRUCTURED/PARSED version of price_list
│                                 columns: category, poles, amps, curve, sensitivity,
│                                          breaking_cap, voltage, product_line,
│                                          parse_method, parse_confidence, needs_review
│
├── extracted_components       ← What Gemini reads from each SLD (written during each job)
│
└── bom_items                  ← Final BOM results per project (written at end of each job)
```

### Key design decisions:
- `price_list` is SACRED — never modified after import. Raw data stays clean.
- `product_attributes` is the parsed/structured version built on top of it.
- When a new price list comes in (e.g. Schneider 2027), you add new rows with a new
  price_date — old data stays for comparison. Nothing gets overwritten.
- The `needs_review` flag (0 or 1) marks rows where the AI or regex wasn't confident.
  Only review those rows instead of checking all 3,857 products.

---

## How Each Script Works

### bom_gen.py — The Daily Tool

```
[Engineer runs bom_gen.py]
        ↓
1. EXTRACT   — Sends SLD image to Gemini API
               Gemini returns JSON: [{category, poles, amps, curve, sensitivity, qty, confidence, notes}]

2. RULES     — Engineering rules engine adds calculated components automatically:
               - Pilot lamps (1 per phase)
               - Fuses (1 per phase)
               - Terminals (1 per outgoing circuit)
               - Main line terminals (always 2)

3. MATCH     — For each extracted component, queries the database to find the right product
               Uses score-based matching (amps = 2pts, poles/curve/sensitivity = 1pt each)
               If confidence ≥ 0.75 AND good match → auto-accepted
               If confidence < 0.75 OR weak match → flagged for review

4. REVIEW    — ALL flagged items presented TOGETHER at the end (not mid-process)
               Engineer can: accept suggestion / enter different part number / skip
               New part numbers can be saved to catalog permanently

5. EXPORT    — Writes final BOM to Excel: description, manufacturer, part number, qty,
               width in modules, AI confidence, source
```

### parse_pricelist.py — The One-Time Import Tool

```
[Run once per manufacturer price list Excel file]
        ↓
PASS 1 — REGEX
  Scans each product description with pattern matching
  Extracts: poles, amps, curve, sensitivity, breaking capacity, voltage
  Fast, free, works on ~27% of Schneider's 3,857 products (the clean descriptions)

PASS 2 — GEMINI AI (for everything regex couldn't parse confidently)
  Sends descriptions in batches of 80 to Gemini
  Gemini identifies: category, product line, all technical fields
  ~35-40 API calls for the full Schneider catalog
  Estimated cost: under $0.50 for entire 3,857-product catalog

Result: every product is stored in product_attributes with structured fields + confidence score
```

---

## What We Did Today (Chronological)

### 1. Started from a working prototype (bom_gen.py v1)
The original script had the right idea but several serious problems:
- API key written in plain text in the code
- Database was just a local Excel file (Master_Database.xlsx)
- Matching only checked Category + Poles + Amps (ignored Curve and Sensitivity)
- Script paused mid-execution waiting for input() calls at random points
- AI prompt was vague and didn't ask for confidence scores
- No handling for unknown or uncertain components

### 2. Fixed the API key security
Created a `.env` file system using the `python-dotenv` library.
The key now lives in `.env` which is never shared.
`bom_gen.py` reads it with `os.getenv("GEMINI_API_KEY")`.
If the key is missing, the script tells you clearly instead of crashing silently.

### 3. Replaced Excel database with SQLite
`database.py` now manages a proper relational database with 6 tables.
Relationships between tables (manufacturers → catalog → extracted → BOM).
Score-based matching engine that weights Amps highest (2pts) and other fields 1pt each.
`add_component_to_catalog()` function lets you add any new manufacturer's product
in one call — it creates the manufacturer automatically if they don't exist yet.

### 4. Rewrote the Gemini prompt
New prompt is a full engineering brief. It tells Gemini:
- Exact list of valid categories
- What to do with components it doesn't recognize (still include, explain in notes)
- How to score its own confidence (1.0 = read clearly, 0.5 = significant guessing)
- Return format must be strict JSON with no markdown wrapping
- Flag anything outside known categories rather than silently forcing it into a wrong box

### 5. Fixed the review flow
All flagged items now appear TOGETHER at the end of the process.
Script no longer pauses randomly asking for input while processing.
Each review item shows: component details, AI notes, reason for flagging, suggested part.
Options: accept suggestion / enter manually / skip.
Manual entries can optionally be saved to catalog for future projects.

### 6. Discussed and designed the price list import system
You have the Schneider PPL 2026 H1 Excel — 3,857 products, reference + description + price.
Descriptions are inconsistent one-liners (not split by column).
Designed a two-pass parser:
  - Pass 1: Regex (free, instant, handles clean descriptions)
  - Pass 2: Gemini batch processing (handles everything else)
Two separate tables: price_list (raw, sacred) + product_attributes (parsed, queryable).
Built to support ANY manufacturer — change 3 lines at the top of the file per new Excel.

### 7. Discussed n8n
Decided NOT to use n8n yet.
n8n is useful for connecting working tools together — file watching, scheduling, notifications.
But the core logic is not finished yet. Introducing n8n now adds complexity before the
foundation is solid. Revisit after the matching engine is fixed and end-to-end flow works.

---

## THE CRITICAL PROBLEM THAT IS NOT FIXED YET

**This is the most important thing for your collaborator to understand.**

`bom_gen.py` and `parse_pricelist.py` are NOT connected yet.

They both write to the same `electrical_bom.db` file, but they use DIFFERENT tables:

```
bom_gen.py  ──queries──→  component_catalog     (13 manual entries, OLD table)

parse_pricelist.py  ──writes──→  product_attributes  (3,857 Schneider products, NEW table)
                                 price_list
```

The matching engine in `bom_gen.py` has NEVER seen the Schneider data.
All 3,857 Schneider products are sitting in the database completely invisible to the BOM generator.

### What needs to be built to fix this:

**Task 1 — Rewrite the matching engine in bom_gen.py**
Change `find_best_match()` in `database.py` to query `product_attributes` JOIN `price_list`
instead of the old `component_catalog` table.
The query should filter by: category, poles, amps, curve, sensitivity, manufacturer.
Return: reference number, manufacturer name, price, product line, confidence.

**Task 2 — Add price to BOM output**
Once the matching engine pulls from the right table, price comes with it automatically.
Add a "Unit Price (€)" and "Total Price (€)" column to the Excel export.
Add a subtotal row at the bottom.

**Task 3 — Retire component_catalog**
Once Task 1 is done, `component_catalog` is obsolete.
The 13 manual Legrand entries should be migrated into `product_attributes` format
or the Legrand price list Excel should be imported through `parse_pricelist.py` properly.

**Task 4 — Manufacturer filter in matching**
Right now matching finds ANY manufacturer's product that matches.
Need to add a preferred manufacturer setting — either per project or per category —
so it doesn't accidentally match a Schneider MCB when you specified Legrand.

---

## Problems / Limitations Still Open

| # | Problem | Priority | Notes |
|---|---------|----------|-------|
| 1 | Matching engine queries wrong table | CRITICAL | Blocks everything |
| 2 | No price in BOM output | HIGH | Easy once #1 fixed |
| 3 | component_catalog not retired | MEDIUM | Clean up after #1 |
| 4 | No manufacturer filter in matching | MEDIUM | Prevents wrong-brand matches |
| 5 | Engineering rules are hardcoded/simple | MEDIUM | Lamps/fuses/terminals logic too basic for complex panels |
| 6 | No DXF support yet | LOW | Currently only works with PNG/image of SLD |
| 7 | Single user, local only | LOW | Works fine for now, migrate to shared server later |
| 8 | No OneDrive datasheet links yet | LOW | Column exists in DB, not populated |
| 9 | parse_pricelist.py not tested end-to-end | MEDIUM | Needs a live run with real API key |

---

## Tech Stack

| Tool | Purpose |
|------|---------|
| Python 3.x | Everything |
| google-genai | Gemini API client |
| python-dotenv | API key management |
| pandas | Reading Excel files |
| openpyxl | Writing Excel output |
| sqlite3 | Database (built into Python, no install needed) |
| Pillow (PIL) | Opening SLD images before sending to Gemini |
| SQLite DB file | electrical_bom.db — single file, portable |

Install all dependencies:
```bash
pip install google-genai python-dotenv pillow pandas openpyxl
```

---

## What a New Collaborator Should Work On First

1. Read `database.py` — understand the table structure
2. Read `bom_gen.py` — specifically the `match_components()` and `find_best_match()` functions
3. Read `parse_pricelist.py` — understand what data it puts into `product_attributes`
4. **The job**: Rewrite `find_best_match()` in `database.py` to query
   `product_attributes JOIN price_list` instead of `component_catalog`
5. Then update `bom_gen.py`'s export step to include price columns

That one fix connects the entire system end to end.
