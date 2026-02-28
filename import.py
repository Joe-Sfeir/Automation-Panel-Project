#!/usr/bin/env python3
"""
import_ppl.py  –  Import a manufacturer PPL Excel into the Technelec pricing DB.
                  Three-tier column mapping: rules → Gemini → data-type heuristic.
                  Reads ALL sheets. Extracts both reference and order number when available.

Usage:
    python import.py --file "PPL_2026 _H1_-_SCHNEIDER.xlsx" --manufacturer SCHNEIDER --year 2026 --half H1 --db pricing.db
    python import.py --file "EatonPL2026.xlsx" --manufacturer EATON --year 2026 --half H1 --currency USD --db pricing.db
"""

import argparse
import json
import random
import re
import sqlite3
import urllib.request
from pathlib import Path

import openpyxl


# ── CONFIG ────────────────────────────────────────────────────────────────────

def get_config() -> dict:
    env_file = Path(__file__).parent / "key.env"
    config   = {"GEMINI_API_KEY": None, "GEMINI_MODEL": "gemini-2.0-flash"}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        for key in config:
            if line.startswith(key):
                config[key] = line.split("=", 1)[1].strip()
    if not config["GEMINI_API_KEY"]:
        raise ValueError("GEMINI_API_KEY not found in key.env")
    return config
def get_api_model() -> str:
    env_file = Path(__file__).parent / "key.env"
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("GEMINI_API_MODEL"): 
            return line.split("=", 1)[1].strip()
    raise ValueError("GEMINI_API_MODEL not found in key.env")
GEMINI_MODEL = get_api_model()

# ── GEMINI API ────────────────────────────────────────────────────────────────

def gemini(system: str, user: str) -> str:
    cfg     = get_config()
    url     = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={cfg['GEMINI_API_KEY']}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": system + "\n\n" + user}]}]
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["candidates"][0]["content"]["parts"][0]["text"].strip()


# ── TIER 1: Rule-based column mapper ─────────────────────────────────────────

def map_columns_rules(ws) -> tuple[dict, int]:
    """
    Scans first 10 rows for known header keywords.
    Also looks for a separate order_number column.
    Returns (col_map, header_row_1based) — values may be None if not found.
    """
    col_map = {'ref': None, 'order_number': None, 'desc': None, 'price': None}

    ref_keys   = ['reference', 'part no', 'part number', 'code', 'material', 'cat.no', 'cat no']
    order_keys = ['order number', 'ordernumber', 'order no', 'catalog number', 'catalog no']
    desc_keys  = ['description', 'designation', 'article', 'product', 'item', 'name']
    price_keys = ['list price', 'unit price', 'price', 'euro', 'usd', 'ppl', 'net price']

    for row_idx in range(1, 11):
        row_vals = [str(cell.value).lower().strip() if cell.value else ""
                    for cell in ws[row_idx]]
        found_any = False

        for col_idx, val in enumerate(row_vals):
            if not val:
                continue
            if col_map['order_number'] is None and any(k in val for k in order_keys):
                col_map['order_number'] = col_idx
                found_any = True
            elif col_map['ref'] is None and any(k in val for k in ref_keys):
                col_map['ref'] = col_idx
                found_any = True
            elif col_map['desc'] is None and any(k in val for k in desc_keys):
                col_map['desc'] = col_idx
                found_any = True
            elif col_map['price'] is None:
                if val == 'up' or any(k in val for k in price_keys):
                    col_map['price'] = col_idx
                    found_any = True

        # if ref wasn't found but order_number was, use order_number as ref too
        if col_map['order_number'] is not None and col_map['ref'] is None:
            col_map['ref'] = col_map['order_number']

        required = ['ref', 'desc', 'price']
        if found_any and all(col_map[k] is not None for k in required):
            return col_map, row_idx

    return col_map, 1


# ── TIER 2: Gemini column mapper ──────────────────────────────────────────────

def map_columns_gemini(sheet_name: str, sample_rows: list) -> dict | None:
    """
    Sends first 10 rows to Gemini to identify column indices.
    Only called when Tier 1 fails.
    """
    rows_text = "\n".join(
        f"  Row {i+1}: {[str(v)[:40] if v else '' for v in row]}"
        for i, row in enumerate(sample_rows)
    )
    system = "You are an expert at reading electrical product price list spreadsheets."
    user   = f"""I have a spreadsheet sheet called '{sheet_name}'.
Here are the first rows (columns are 0-indexed):

{rows_text}

IMPORTANT: This file may have NO header row — row 1 might already be product data.

Identify the 0-based column index for:
1. Product reference / part number (short alphanumeric code like A9F73106 or mMC4-C1/1)
2. Product description (longer text describing the product)
3. Unit price (small positive number, typically 1–5000)
4. Order number / catalog number if it exists separately from reference (or null)

Reply ONLY with JSON (no markdown):
{{"ref": 0, "desc": 1, "price": 2, "order_number": 3, "header_row": 0, "notes": "No header row, row 1 is data"}}

If there is no separate order number column use null:
{{"ref": 0, "desc": 1, "price": 2, "order_number": null, "header_row": 0, "notes": "..."}}

If you cannot identify columns:
{{"ref": null, "desc": null, "price": null, "order_number": null, "notes": "Cannot determine"}}
"""
    try:
        raw    = gemini(system, user)
        raw    = re.sub(r"^```json|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        result = json.loads(raw)
        if result.get("ref") is not None:
            print(f"    Gemini: Ref=Col {result['ref']+1}  Desc=Col {result['desc']+1}  Price=Col {result['price']+1}"
                  + (f"  OrderNo=Col {result['order_number']+1}" if result.get('order_number') is not None else ""))
            print(f"    Gemini notes: {result.get('notes', '-')}")
            return result
        print(f"    Gemini could not identify columns: {result.get('notes', '-')}")
        return None
    except Exception as e:
        print(f"    Gemini fallback error: {e}")
        return None


# ── TIER 3: Data-type heuristic ───────────────────────────────────────────────

def map_columns_heuristic(ws) -> dict | None:
    """
    Last resort when no headers exist and Gemini fails.
    Analyses data types across columns:
      - Price column   → mostly small positive floats
      - Ref column     → mostly short strings (< 20 chars)
      - Desc column    → mostly longer strings (> 20 chars)
    """
    print("    Trying data-type heuristic ...")

    # sample up to 30 data rows
    samples = {c: [] for c in range(1, ws.max_column + 1)}
    for r in range(1, 31):
        for c in range(1, ws.max_column + 1):
            v = ws.cell(r, c).value
            if v is not None:
                samples[c].append(v)

    price_col = ref_col = desc_col = None
    best_price_score = -1

    for c, vals in samples.items():
        if len(vals) < 5:
            continue

        # score as price column: how many are small positive floats
        float_count = sum(1 for v in vals if isinstance(v, (int, float)) and 0 < v < 50000)
        if float_count > best_price_score:
            best_price_score = float_count
            price_col = c - 1  # convert to 0-based

    for c, vals in samples.items():
        if c - 1 == price_col:
            continue
        str_vals  = [str(v).strip() for v in vals if v]
        if not str_vals:
            continue
        avg_len   = sum(len(s) for s in str_vals) / len(str_vals)
        short_pct = sum(1 for s in str_vals if len(s) < 20) / len(str_vals)

        if short_pct > 0.7 and avg_len < 20 and ref_col is None:
            ref_col = c - 1
        elif avg_len >= 20 and desc_col is None:
            desc_col = c - 1

    if price_col is not None and ref_col is not None and desc_col is not None:
        # sanity check: all different
        if len({price_col, ref_col, desc_col}) == 3:
            print(f"    Heuristic: Ref=Col {ref_col+1}  Desc=Col {desc_col+1}  Price=Col {price_col+1}")
            return {'ref': ref_col, 'order_number': None, 'desc': desc_col, 'price': price_col}

    print("    Heuristic failed — skipping sheet")
    return None


# ── COMBINED MAPPER ───────────────────────────────────────────────────────────

def map_columns(ws, sheet_name: str) -> tuple[dict | None, int]:
    col_map, header_row = map_columns_rules(ws)

    required = ['ref', 'desc', 'price']

    # sanity check: no two required columns can share the same index
    req_vals = [col_map[k] for k in required if col_map[k] is not None]
    if len(req_vals) == len(required) and len(set(req_vals)) == len(required):
        return col_map, header_row  # Tier 1 clean success

    # Tier 1 failed or has duplicates — try Gemini
    missing = [k for k in required if col_map[k] is None]
    if missing:
        print(f"    Rules incomplete (missing: {', '.join(missing)}) — asking Gemini ...")
    else:
        print(f"    Rules produced duplicate column indices — asking Gemini ...")

    sample_rows = []
    for r in range(1, 12):
        row = [ws.cell(r, c).value for c in range(1, min(ws.max_column + 1, 10))]
        if any(v is not None for v in row):
            sample_rows.append(row)
        if len(sample_rows) >= 10:
            break

    result = map_columns_gemini(sheet_name, sample_rows)

    if result and result.get("ref") is not None:
        # validate Gemini result — no duplicate indices
        g_ref   = result['ref']
        g_desc  = result['desc']
        g_price = result['price']
        g_order = result.get('order_number')
        if len({g_ref, g_desc, g_price}) == 3:
            col_map    = {'ref': g_ref, 'order_number': g_order, 'desc': g_desc, 'price': g_price}
            header_row = result.get('header_row', 0) + 1
            return col_map, header_row
        print("    Gemini returned duplicate indices — trying heuristic ...")

    # Tier 3: heuristic
    result = map_columns_heuristic(ws)
    if result:
        return result, 1

    return None, 1  # all tiers failed


# ── PRICE CLEANER ─────────────────────────────────────────────────────────────

def clean_price(val) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        p = float(val)
        return round(p, 2) if p > 0 else None
    s = str(val).replace('$','').replace('€','').replace('£','').replace(',','').strip()
    try:
        p = float(s)
        return round(p, 2) if p > 0 else None
    except ValueError:
        m = re.search(r'(\d+\.\d+|\d+)', s)
        if m:
            p = float(m.group(1))
            return round(p, 2) if p > 0 else None
        return None


# ── PPL READER (multi-sheet) ──────────────────────────────────────────────────

def read_ppl(filepath: Path) -> list:
    print(f"  Loading workbook: {filepath.name} ...")
    wb  = openpyxl.load_workbook(str(filepath), data_only=True)
    out = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]

        if ws.max_row < 3 or ws.max_column < 2:
            print(f"  Skipping '{sheet_name}' — too small")
            continue

        print(f"\n  Sheet: '{sheet_name}' ({ws.max_row} rows) ...")

        col_map, header_row = map_columns(ws, sheet_name)

        if col_map is None:
            print(f"    All mapping tiers failed — skipping sheet")
            continue

        has_order = col_map.get('order_number') is not None
        print(f"    Final map → Ref: Col {col_map['ref']+1}  "
              f"Desc: Col {col_map['desc']+1}  "
              f"Price: Col {col_map['price']+1}"
              + (f"  OrderNo: Col {col_map['order_number']+1}" if has_order else ""))

        max_col_needed = max(v for v in col_map.values() if v is not None)
        sheet_rows     = []
        skipped_blank  = skipped_noprice = 0

        for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
            if len(row) <= max_col_needed:
                continue

            ref  = row[col_map['ref']]
            desc = row[col_map['desc']]
            raw_price = row[col_map['price']]

            ref_str  = str(ref).strip()  if ref  else ""
            desc_str = str(desc).strip().replace('\n', ' ').replace('\r', '') if desc else ""

            if not ref_str or not desc_str:
                skipped_blank += 1
                continue

            price = clean_price(raw_price)
            if price is None:
                skipped_noprice += 1
                continue

            order_number = None
            if has_order:
                ov = row[col_map['order_number']]
                order_number = str(ov).strip() if ov else None

            sheet_rows.append({
                "reference":    ref_str,
                "order_number": order_number,
                "description":  desc_str,
                "price":        price,
            })

        count = len(sheet_rows)
        print(f"    Extracted: {count}  |  blank: {skipped_blank}  |  no-price: {skipped_noprice}")

        if count < 5:
            print(f"    Too few products — skipping sheet")
        else:
            out.extend(sheet_rows)

    wb.close()
    print(f"\n  Total extracted across all sheets: {len(out)}")
    return out


# ── NORMALISATION ─────────────────────────────────────────────────────────────

def load_aliases(conn):
    rows = conn.execute("SELECT alias, canonical FROM term_aliases").fetchall()
    return {r[0].lower(): r[1].lower() for r in rows}


def normalise(text: str, aliases: dict) -> list:
    text = text.lower()
    text = re.sub(r'(\d+)x(\d+a)', lambda m: f"{m.group(1)}p {m.group(2)}", text)
    text = re.sub(r'[^\w\s]', ' ', text)
    for alias, canonical in sorted(aliases.items(), key=lambda x: -len(x[0])):
        text = re.sub(r'\b' + re.escape(alias) + r'\b', canonical, text)
    return [t for t in text.split() if t]


# ── GEMINI VALIDATION ─────────────────────────────────────────────────────────

def validate_extraction(manufacturer: str, sample: list) -> dict:
    rows_text = "\n".join(
        f"  ref={r['reference']}  order={r.get('order_number','-')}  desc={r['description'][:50]}  price={r['price']}"
        for r in sample
    )
    system = "You are an expert in electrical product catalogues. You validate extracted price list data."
    user   = f"""I extracted {len(sample)} sample rows from the {manufacturer} price list.
Check: correct columns, realistic prices, real references.

{rows_text}

Reply ONLY with JSON (no markdown):
{{"ok": true, "confidence": 92, "issues": [], "notes": "Looks clean."}}
or:
{{"ok": false, "confidence": 30, "issues": ["prices look like order numbers"], "notes": "Wrong column."}}
"""
    raw = gemini(system, user)
    try:
        raw = re.sub(r"^```json|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        return json.loads(raw)
    except Exception:
        return {"ok": True, "confidence": 50, "issues": [], "notes": raw}


# ── DB WRITE ──────────────────────────────────────────────────────────────────

def commit_to_db(conn, aliases, edition_id, products):
    inserted = updated = 0
    for p in products:
        existing = conn.execute(
            "SELECT id FROM products WHERE ppl_edition_id=? AND reference=?",
            (edition_id, p["reference"]),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE products SET order_number=?, description=?, price=? WHERE id=?",
                (p.get("order_number"), p["description"], p["price"], existing[0]),
            )
            prod_id = existing[0]
            conn.execute("DELETE FROM product_keywords WHERE product_id=?", (prod_id,))
            updated += 1
        else:
            cur = conn.execute(
                "INSERT INTO products(ppl_edition_id, reference, order_number, description, price) VALUES(?,?,?,?,?)",
                (edition_id, p["reference"], p.get("order_number"), p["description"], p["price"]),
            )
            prod_id = cur.lastrowid
            inserted += 1

        # tokenise description + reference + order_number
        tokens = (set(normalise(p["description"], aliases))
                  | set(normalise(p["reference"], aliases)))
        if p.get("order_number"):
            tokens |= set(normalise(p["order_number"], aliases))

        for kw in tokens:
            if kw:
                conn.execute(
                    "INSERT INTO product_keywords(product_id, keyword) VALUES(?,?)",
                    (prod_id, kw),
                )
    return inserted, updated


# ── MAIN ──────────────────────────────────────────────────────────────────────

def import_ppl(db_path: Path, xlsx_path: Path, manufacturer: str,
               year: int, half: str, currency: str, force: bool = False):

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")

    schema = (Path(__file__).parent / "schema.sql").read_text()
    conn.executescript(schema)
    conn.commit()

    # migrate existing DB: add order_number column if missing
    try:
        conn.execute("ALTER TABLE products ADD COLUMN order_number TEXT")
        conn.commit()
        print("  Migrated DB: added order_number column")
    except Exception:
        pass  # column already exists

    aliases = load_aliases(conn)

    print(f"Reading {xlsx_path.name} ...")
    products = read_ppl(xlsx_path)

    if not products:
        print("  ERROR: no products found — check file format")
        conn.close()
        return

    # ── Gemini final validation ───────────────────────────────────────────────
    print("\n  Asking Gemini to validate extraction quality ...")
    sample = random.sample(products, min(25, len(products)))
    result = validate_extraction(manufacturer, sample)

    print(f"  Confidence: {result.get('confidence', '?')}%")
    if result.get("notes"):
        print(f"  Gemini: {result['notes']}")
    for issue in result.get("issues", []):
        print(f"  WARNING: {issue}")

    if not result.get("ok", True) and not force:
        print("\n  Gemini flagged problems. Fix the file or run with --force to import anyway.")
        conn.close()
        return
    print("  Extraction validated — committing to database ...")
    # ─────────────────────────────────────────────────────────────────────────

    conn.execute("INSERT OR IGNORE INTO manufacturers(name) VALUES(?)", (manufacturer.upper(),))
    mfr_id = conn.execute(
        "SELECT id FROM manufacturers WHERE name=?", (manufacturer.upper(),)
    ).fetchone()[0]

    # INSERT OR REPLACE so currency always gets updated correctly
    conn.execute(
        """INSERT INTO ppl_editions(manufacturer_id, year, half, currency)
           VALUES(?,?,?,?)
           ON CONFLICT(manufacturer_id, year, half)
           DO UPDATE SET currency=excluded.currency, imported_at=datetime('now')""",
        (mfr_id, year, half, currency.upper()),
    )
    conn.commit()

    edition_id = conn.execute(
        "SELECT id FROM ppl_editions WHERE manufacturer_id=? AND year=? AND half=?",
        (mfr_id, year, half),
    ).fetchone()[0]

    inserted, updated = commit_to_db(conn, aliases, edition_id, products)
    conn.commit()
    conn.close()
    print(f"\n  Done  —  inserted: {inserted}  updated: {updated}  currency: {currency.upper()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file",         required=True)
    parser.add_argument("--manufacturer", required=True)
    parser.add_argument("--year",         required=True, type=int)
    parser.add_argument("--half",         required=True, choices=["H1", "H2"])
    parser.add_argument("--currency",     default="EUR", help="EUR or USD")
    parser.add_argument("--db",           default="pricing.db")
    parser.add_argument("--force",        action="store_true")
    args = parser.parse_args()
    import_ppl(Path(args.db), Path(args.file), args.manufacturer,
               args.year, args.half, args.currency, args.force)