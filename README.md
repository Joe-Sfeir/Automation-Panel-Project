# ⚡ Technelec: AI-Powered Electrical Panel Estimator & BOM Generator

Welcome to **Technelec**, an enterprise-grade automation pipeline built to solve the most painful bottleneck in electrical panel building: **turning messy Single Line Diagrams (SLDs) into perfectly priced Bills of Materials (BOMs).**

If you've ever spent 4 hours squinting at a PDF, manually counting 16A MCBs, guessing if a line is a 3-phase circuit, and digging through a 10,000-row Excel price list just to build a quote... this project is for you.

## 🧠 What does it do?

Technelec uses a **Hybrid AI + Deterministic Geometry Architecture** to read engineering drawings like a Senior Estimator. 

1. **Ingest:** Takes a client `.dwg`, `.dxf`, or `.pdf` drawing.
2. **Extract (The Python part):** Uses `ezdxf` to rip out exact block counts, attributes, and font-scaled text clusters. (We don't let AI guess quantities!).
3. **Analyze (The AI part):** Sends a rendered image + the structured CAD payload to Google's **Gemini 3.0 Pro Vision**. Gemini stitches the text to the symbols, understands the panel hierarchy, and outputs a strict, Pydantic-validated JSON array of components.
4. **Engineer (The Rules Engine):** Automatically applies shop-floor physics—sizing Phoenix Contact terminal blocks (PT vs. PTTB) based on phase counts and cable `mm²`.
5. **Price (The SQL Matcher):** Bounces the extracted components against a normalized, multi-vendor SQLite database (`pricing.db`), navigating vendor jargon (e.g., translating "TPN 6000A" to Eaton's "3P+N 6kA"), and spits out a final priced offer.

## 🏗️ Architecture & Workflow

This isn't a wrapper for a chatbot. It's a multi-stage pipeline:

```text
[ DWG / PDF ]  →  (ODA Converter)  →  [ DXF ]
                                         ↓
                                 [ ezdxf Extractor ]
                                   (Blocks, Text)
                                         ↓[ Gemini Vision API ] ←── (Payload + PNG Render)
         ↓
[ Pydantic JSON ]  →  (Terminal Math / Rules Engine)
         ↓
[ Matcher.py ]  ←── (SQLite Price List DB + Term Aliases)
         ↓
[ Final Excel / JSON Offer ]
```

## 🚀 Features

*   **Immune to the "Lazy Drafter":** If the drafter used perfect CAD attributes (`<AMPS="16A">`), Python grabs it instantly. If they just dropped dumb line-art and wrote "16A" floating nearby, Gemini Vision connects the dots spatially.
*   **Structured Outputs:** Uses Pydantic to force Gemini to return perfect JSON. No more regex-stripping markdown fences.
*   **Smart Terminal Logic:** Knows the difference between a 1P+N 2.5mm² circuit (gets one space-saving double-level PTTB 4 block) and a 3P 10mm² circuit (gets three single-level PT 10 blocks).
*   **The 3-Tier Matcher:**
    *   *Tier 1:* Instant SQL Keyword Match (Fast & Free).
    *   *Tier 2:* AI Disambiguation (Picks the best from top 5 tied SQL results).
    *   *Tier 3:* AI Substitution (If 20A doesn't exist in the catalog, AI safely up-sizes to 25A).
*   **Universal Price List Importer:** Throw a Schneider, Eaton, or ABB Excel price list at it. It uses data-type heuristics to map the columns and cleans the messy vendor data into a standardized SQL schema.

## 🛠️ Installation & Setup

**1. Clone the repo and install dependencies:**
```bash
git clone https://github.com/yourusername/technelec-estimator.git
cd technelec-estimator
pip install -r requirements.txt
```
*(Requires `ezdxf`, `google-genai`, `pydantic`, `pandas`, `openpyxl`, `matplotlib`, `pillow`)*

**2. ODA File Converter (Optional but highly recommended):**
If you want to process native binary `.dwg` files, install the free [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter). The script auto-detects it.

**3. API Key Setup:**
Create a `.venv/key.env` file in the project root:
```env
GEMINI_API_KEY=your_google_api_key_here
GEMINI_API_MODEL=gemini-3.0-pro-preview  # Or gemini-2.5-flash for testing
```

**4. Build the Database:**
Import a vendor price list (e.g., Schneider, Eaton) into the SQLite DB:
```bash
python import.py --file "Schneider_PL.xlsx" --manufacturer SCHNEIDER --year 2026 --half H1 --currency EUR --db pricing.db
```

## 🎮 How to Use It

**Step 1: Extract the SLD to JSON**
```bash
python extract_spatial.py path/to/client_drawing.dwg --manufacturer SCHNEIDER
```
*Outputs: `client_drawing_rawcomponents.sld.json`*

**Step 2: Price the Offer**
```bash
python match_offer_json.py --inputs client_drawing_rawcomponents.sld.json --manufacturer SCHNEIDER --out Final_Quote.json
```

## 🔮 What's Next?
*   **Streamlit Web UI:** Moving away from the CLI to a browser-based dashboard where engineers can visually review and override AI matches before exporting to Excel.
*   **AutoCAD Kit-of-Parts Drafter:** A script that takes the final BOM, opens AutoCAD, and auto-generates dynamic 2D DIN-rail footprints (18mm per module) ready for the draftsman to arrange.

## 🤝 Contributing
Panel builders of the world, unite! If you have better Regex for Eaton catalogs, or want to add support for ABB/Siemens aliases, pull requests are welcome. 

---
*Built with Python, SQL, and a deep hatred for manual data entry.*
