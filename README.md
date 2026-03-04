# Electrical Pricing & SLD Extraction Platform

> AI-powered platform for extracting components from Single Line Diagrams,
> managing manufacturer price lists, and auto-pricing Bills of Materials.

---

## 📁 Project Structure

| File / Folder | Purpose |
|---|---|
| `Codes/dwg_test3.py` | SLD Component Extractor (DWG/DXF → Excel BOM) |
| `Codes/import.py` | Manufacturer PPL Importer (Excel → SQLite DB) |
| `Codes/match-offer.py` | BOM Auto-Pricer (offer Excel → priced Excel) |
| `Price Lists` | Manufacturers price list |
| `schema.sql` | SQLite schema (**missing — needs creating**) |
| `.venv/key_env` *(or `key.env`)* | API keys (Gemini + Anthropic) |

---

## ✅ What Has Been Built

### 1) ⚡ SLD Component Extractor — `dwg_test3.py` (v4.2)

Converts a client electrical Single Line Diagram (`.dwg` / `.dxf`) into a structured Excel workbook.

**Pipeline**
1. Convert DWG → DXF *(ODA File Converter)*
2. Parse DXF → structured text payload *(ezdxf: blocks, attributes, text clusters, layer map)*
3. Render DXF → PNG *(matplotlib, 150 DPI)*
4. Gemini call #1: extract components *(text + image)*
5. Gemini call #2: enclosure + additional metadata
6. Group identical rows & sum quantities
7. Terminal calculator *(Phoenix Contact PT series)*
8. Export Excel output *(6 sheets)*

**Component categories:** MCB · MCCB · RCCB · RCBO · SWITCH · CONTACTOR · SPD · FUSE_HOLDER · PILOT_LAMP · METER (CTs) · OTHER

**Excel output — 6 sheets:**

| Sheet | Contents |
|---|---|
| `Components` | Full BOM: category, label, poles, amps, curve, B.C., cable size, qty, confidence |
| `Terminal Schedule` | Phoenix Contact PT schedule per cable size |
| `Enclosure` | Panel ref, size, manufacturer, catalog number |
| `Additional Info` | AI-detected extras: load schedules, cable schedules, notes |
| `Shop Drawing` | Full DXF render embedded as image |
| `Summary` | Component count by category |

**Key AI features:**
- Dual-source cross-reference: DXF text **+** PNG image
- Global note inheritance (e.g. "B.C. 6kA/400V" → all breakers)
- CT ratio extraction ("40/5" format), confidence scoring, colour-coded rows
- Automatic terminal calculation: 2 per outgoing circuit, matched to cable size

---

### 2. 📥 PPL Importer — `import.py`

Imports any manufacturer's Excel price list into a local SQLite database.

**Three-tier column mapping:**
\```
Tier 1 → Rule-based keyword scan (fast, free — works 80%+ of files)
    ↓ fails
Tier 2 → Gemini AI reads first 10 rows (handles no-header, non-English)
    ↓ fails
Tier 3 → Data-type heuristic (price=floats, ref=short, desc=long strings)
\```

**Features:**
- Reads all sheets automatically
- Extracts both `reference` and `order_number` when they are separate columns
- Price cleaning: strips currency symbols, handles comma-formatted numbers
- Gemini validates a 25-row random sample before committing
- Full-text keyword tokenisation with alias normalisation
- Safe re-import: INSERT OR UPDATE (won't duplicate)
- EUR and USD currency support

**Manufacturers imported:** Schneider Electric (EUR), Eaton (USD)

**DB tables:** `manufacturers` · `ppl_editions` · `products` · `product_keywords` · `term_aliases`

---

### 3. 💰 Offer Matcher / Auto-Pricer — `match-offer.py`

Takes a BOM Excel and prices every line item against the PPL database.

**Three-tier matching:**
\```
Tier 1 → Keyword scoring ≥ 60%  → 🟢 GREEN  (fast, free)
Tier 2 → Claude picks best from top-5 candidates (20–60%)  → 🟡 YELLOW
Tier 3 → Claude suggests size substitution (< 20%)  → 🔵 BLUE
                                   nothing found  → ⬜ GREY

| Tier 1  | → Keyword scoring ≥ 60%  → 🟢 GREEN  (fast, free) |
| Tier 2  | → Claude picks best from top-5 candidates (20–60%)  → 🟡 YELLOW|
| Tier 3  | → Claude suggests size substitution (< 20%)  → 🔵 BLUE |
| nothing found |  → ⬜ GREY  |


**Output:** Appends 4 columns to original offer file:
`Matched Ref.` · `Unit Price` · `Total` · `Match Note`

**Dynamic column detection:** Finds description, qty, manufacturer columns regardless of order.

---

## 🔧 What Still Needs To Be Done (Backend)

### 🔴 High Priority

- [ ] **`schema.sql` — create and commit to repo**
  Referenced in `import.py` but the file doesn't exist. Needs full table
  definitions with foreign keys, indexes, and the `term_aliases` seed data.

- [ ] **`term_aliases` — populate with electrical abbreviations**
  Seed file needed: `mcb → miniature circuit breaker`, `1p → 1 pole`,
  `3p → 3 pole`, `mccb → moulded case`, `rccb → residual current`,
  `ic60 → ic60n`, `dpx → dpx`, `ins → ins`, etc.

- [ ] **SLD Extractor → Offer Matcher bridge**
  The BOM Excel from `dwg_test3.py` has different column names than what
  `match-offer.py` expects. Need a shared BOM format or an adapter function
  so the pipeline can run end-to-end without manual column renaming.

- [ ] **End-to-end pipeline runner**
  Single entry point that chains: DWG → extract → price → final Excel,
  with one command and clear progress output at each stage.

- [ ] **Fix `args.file` bug in `match-offer.py`**
  The last line references `args.file` which doesn't exist in argparse —
  should be `args.offer`. This crashes every run.

- [ ] **Multi-manufacturer matching**
  Currently only one manufacturer searched at a time. Need to search all
  imported PPLs and return cheapest / best match with manufacturer labelled.

- [ ] **Verify Gemini model string in `dwg_test3.py`**
  Currently calls `gemini-3-pro-preview` — confirm this is a valid model
  identifier and update if needed.

- [ ] **API retry / backoff logic**
  Gemini and Claude calls have no retry. One timeout kills the entire job.
  Add exponential backoff: 3 retries at 5s / 15s / 30s intervals.

- [ ] **Currency normalisation**
  Eaton is USD, Schneider is EUR. Cross-manufacturer comparison requires
  FX conversion — store a rate per PPL edition or call a live FX API.

- [ ] **PPL version archival**
  When H2 is imported, H1 should be archived (not deleted) so historical
  quotes still reference the correct price at time of issue.

### 🟡 Medium Priority

- [ ] **Better part number tokenisation**
  `A9F73120` should match whether searched whole or split (`A9F 73120`).
  Current tokeniser on punctuation-only breaks these references incorrectly.

- [ ] **Configurable match thresholds**
  60% (Tier 1) and 20% (Tier 2) are hardcoded. Make them CLI flags or
  a config file so they can be tuned without touching code.

- [ ] **Structured logging**
  Replace all `print()` with Python `logging`. Add `--verbose` / `--quiet` flags.

- [ ] **Duplicate product deduplication**
  Same reference sometimes appears in multiple sheets of one PPL.
  Need a "keep most-detailed description" deduplication pass on import.

- [ ] **Test suite**
  Unit tests for: column mapping (all 3 tiers), price cleaning, normalisation,
  terminal size calculation, component grouping, keyword scoring.

---

## 🖥️ UI Work That Needs To Be Done

The entire platform is currently CLI only. A full web UI is needed.

---

### 🏠 1. Dashboard

- KPI cards: manufacturers in DB, total products, jobs this month, last import dates
- Quick-action buttons: "Upload Drawing", "Price an Offer", "Import PPL"
- Recent jobs table with download links
- Alerts for failed jobs or results needing manual review

---

### 📤 2. SLD Extractor Page

- Drag-and-drop upload for `.dwg` / `.dxf`
- Step-by-step progress tracker with live status labels
- Drawing preview: PNG render shown beside component table
- Live results table — rows appear as Gemini streams output:
  - Colour-coded by confidence (green ≥ 90%, yellow 75–90%, red < 75%)
  - Inline cell editing to correct AI mistakes before export
  - Filter by category, checkbox to include/exclude rows
- Terminal schedule tab
- Enclosure details panel
- "Send to Pricer" button — passes BOM directly to Offer Pricer step
- Download Excel button

---

### 💵 3. PPL Manager

- Master table: Manufacturer · Year · Half · Currency · # Products · Imported · Actions
- Import wizard (4 steps):
  1. Upload Excel
  2. Column preview — detected columns highlighted (ref=blue, desc=green, price=orange),
     allow manual override
  3. Gemini validation result — confidence %, sample rows, any warnings
  4. Confirm → progress bar → import summary
- Per-manufacturer product browser with search, price filter, pagination
- PPL comparison view: select 2 manufacturers → side-by-side price table

---

### 🧾 4. Offer Pricer Page

- Upload offer BOM Excel
- Manufacturer selector (single or "Search All")
- Column mapping confirmation step with visual preview
- Live pricing progress: each row updates in real time as it's processed
- Results summary panel: total lines, matched, AI, substituted, not found, grand total
- Full colour-coded results table (green / yellow / blue / grey)
- "Not Found" rows: click → search drawer to manually find and assign a product
- "Substituted" rows: show original vs substituted product clearly
- Download priced Excel button

---

### 🔍 5. Global Product Search

- Search across all manufacturers and editions simultaneously
- Filters: manufacturer, year, half, price range
- Results: Reference · Description · Price · Manufacturer · Edition
- "Add to BOM" button for building a manual quote line-by-line

---

### ⚙️ 6. Settings Page

- API key management (Gemini, Anthropic) — masked, with test-connection button
- Default currency selector
- Match threshold sliders (Tier 1 and Tier 2 cutoffs)
- Terminal brand selector (currently hardcoded to Phoenix Contact PT)
- Alias dictionary table editor: add / edit / delete / bulk CSV import
- User management: list users, assign roles (Admin / Estimator / Viewer)

---

### 🗃️ 7. Job History

- Full log of every extraction and pricing job
- Re-download output from any past job
- Re-run a job with updated PPL prices
- Delete jobs with confirmation

---

## 🛠️ Suggested Tech Stack for UI

| Layer | Recommendation | Reason |
|---|---|---|
| Frontend | React + Tailwind CSS | Fast development, great ecosystem |
| Backend API | FastAPI (Python) | Reuses existing Python logic as modules |
| Database | SQLite → PostgreSQL | SQLite for solo use, Postgres for team |
| File storage | Local → S3 | Easy upgrade path |
| AI streaming | Server-Sent Events (SSE) | Live Gemini output in the browser |
| Auth | JWT + bcrypt | Simple, stateless |
| Deployment | Docker Compose | One command startup |

---

## 🚀 Quick Start (CLI)

\```bash
pip install ezdxf google-genai openpyxl matplotlib Pillow

# 1. Extract drawing
python dwg_test3.py drawing.dwg --output bom.xlsx

# 2. Import price lists
python import.py --file PPL_2026_H1__SCHNEIDER.xlsx --manufacturer SCHNEIDER --year 2026 --half H1 --db pricing.db
python import.py --file EatonPL2026.xlsx --manufacturer EATON --year 2026 --half H1 --currency USD --db pricing.db

# 3. Price the BOM
python match-offer.py --offer bom.xlsx --db pricing.db --manufacturer SCHNEIDER --year 2026 --half H1 --out bom_priced.xlsx
\```

---

## 🔑 API Keys Required

| Key | Used In | Where to set |
|---|---|---|
| `GEMINI_API_KEY` | `dwg_test3.py`, `import.py` | `key.env` in project root |
| `GEMINI_API_MODEL` | `import.py` | `key.env` in project root |
| `ANTHROPIC_API_KEY` | `match-offer.py` | `api.env` or env variable |

---
*Last updated: March 2026*

---

## Desktop App (first version)

A first local desktop UI is now scaffolded with PySide6:

- `run_app.py` -> app launcher
- `app/main.py` -> main window, settings dialog, new project workflow
- `app_config.json` -> saved automatically after first settings save

### Features in this first build

- Settings button in the top bar
  - Gemini API key
  - Gemini model
  - Pricing DB path
  - ODA converter path (optional)
  - Python executable path
- Main menu with **New Project**
- Workflow form:
  - Select `.dwg` / `.dxf`
  - Select output folder
  - Select one or more brands
  - Choose year / half / pricing mode
- Runs pipeline:
  1. `Codes/dwg_test3.py` to generate raw components file
  2. `Codes/match_offer.py` once per selected brand
- Live logs + step progress in UI

### Run locally

```powershell
pip install -r requirements-ui.txt
python run_app.py
```

### Build Windows `.exe`

```powershell
pip install pyinstaller
pyinstaller --noconfirm --windowed --name AutomationProjectApp run_app.py
```

The output app will be in:

- `dist/AutomationProjectApp/AutomationProjectApp.exe`

### Desktop app updates (iteration 2)

- Added Gemini connection test in Settings
- Added defaults in Settings (year / half / pricing mode)
- Added dynamic brand loading from `manufacturers` table in `pricing.db`
- Added custom brand entry (comma-separated)
- Added safer workflow execution:
  - masked API key in logs
  - environment injection for `GEMINI_API_KEY` and `GEMINI_API_MODEL`
  - command error tail in failure dialog
- Added persistent job history (`app_history.json`)
- Added History page with quick open-output-folder action
- Added "Open Last Output" button in project screen
