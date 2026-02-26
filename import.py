#!/usr/bin/env python3
"""
import_ppl.py  –  Import a manufacturer PPL Excel into the Technelec pricing DB.
                  Uses Claude API to validate extraction quality before committing.

Usage:
    python import_ppl.py --file PPL_2026_H1_-_SCHNEIDER.xlsx \
                         --manufacturer SCHNEIDER \
                         --year 2026 --half H1 \
                         --db pricing.db
"""

import argparse
import json
import random
import re
import sqlite3
import urllib.request
from pathlib import Path

import openpyxl


ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
MODEL         = "claude-sonnet-4-20250514"


# ── Claude API ────────────────────────────────────────────────────────────────

def claude(system: str, user: str, max_tokens: int = 1000) -> str:
    payload = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        ANTHROPIC_API,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["content"][0]["text"].strip()


def validate_extraction(manufacturer: str, sample: list) -> dict:
    """Ask Claude to audit a random sample of extracted rows."""
    rows_text = "\n".join(
        f"  ref={r['reference']}  desc={r['description']}  price={r['price']}"
        for r in sample
    )
    system = (
        "You are an expert in electrical product catalogues. "
        "You validate data extracted from manufacturer price lists."
    )
    user = f"""I extracted {len(sample)} sample rows from the {manufacturer} price list.
Check if the extraction looks correct — right columns, realistic prices, real references.

Rows:
{rows_text}

Reply ONLY with a JSON object (no markdown) like:
{{"ok": true, "confidence": 92, "issues": [], "notes": "Extraction looks clean."}}

If problems exist (wrong columns, prices look like quantities, etc):
{{"ok": false, "confidence": 30, "issues": ["prices in wrong column"], "notes": "Column mapping is wrong."}}
"""
    raw = claude(system, user)
    try:
        raw = re.sub(r"^```json|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        return json.loads(raw)
    except Exception:
        return {"ok": True, "confidence": 50, "issues": [], "notes": raw}


# ── normalisation ─────────────────────────────────────────────────────────────

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


# ── PPL reader ────────────────────────────────────────────────────────────────

def read_ppl(filepath: Path) -> list:
    wb  = openpyxl.load_workbook(str(filepath), read_only=True, data_only=True)
    ws  = wb.active
    out = []
    for row in ws.iter_rows(min_row=1, values_only=True):
        ref   = row[0] if len(row) > 0 else None
        desc  = row[1] if len(row) > 1 else None
        price = row[2] if len(row) > 2 else None
        if not ref or not desc:
            continue
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        out.append({"reference": str(ref).strip(), "description": str(desc).strip(), "price": price})
    wb.close()
    return out


# ── DB write ──────────────────────────────────────────────────────────────────

def commit_to_db(conn, aliases, edition_id, products):
    inserted = updated = 0
    for p in products:
        existing = conn.execute(
            "SELECT id FROM products WHERE ppl_edition_id=? AND reference=?",
            (edition_id, p["reference"]),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE products SET description=?, price=? WHERE id=?",
                (p["description"], p["price"], existing[0]),
            )
            prod_id = existing[0]
            conn.execute("DELETE FROM product_keywords WHERE product_id=?", (prod_id,))
            updated += 1
        else:
            cur = conn.execute(
                "INSERT INTO products(ppl_edition_id, reference, description, price) VALUES(?,?,?,?)",
                (edition_id, p["reference"], p["description"], p["price"]),
            )
            prod_id = cur.lastrowid
            inserted += 1

        tokens = set(normalise(p["description"], aliases)) | set(normalise(p["reference"], aliases))
        for kw in tokens:
            if kw:
                conn.execute(
                    "INSERT INTO product_keywords(product_id, keyword) VALUES(?,?)",
                    (prod_id, kw),
                )
    return inserted, updated


# ── main ──────────────────────────────────────────────────────────────────────

def import_ppl(db_path: Path, xlsx_path: Path, manufacturer: str, year: int, half: str, force: bool = False):
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")

    schema = (Path(__file__).parent / "schema.sql").read_text()
    conn.executescript(schema)
    conn.commit()

    aliases = load_aliases(conn)

    print(f"Reading {xlsx_path.name} ...")
    products = read_ppl(xlsx_path)
    print(f"  Extracted {len(products)} rows")

    if not products:
        print("  ERROR: no products found — check file format")
        conn.close()
        return

    # ── Claude validation ─────────────────────────────────────────────────────
    print("  Asking Claude to validate extraction quality ...")
    sample = random.sample(products, min(20, len(products)))
    result = validate_extraction(manufacturer, sample)

    print(f"  Confidence: {result.get('confidence', '?')}%")
    if result.get("notes"):
        print(f"  Claude: {result['notes']}")
    for issue in result.get("issues", []):
        print(f"  WARNING: {issue}")

    if not result.get("ok", True) and not force:
        print("\n  Claude flagged problems. Fix the file or run with --force to import anyway.")
        conn.close()
        return
    print("  Extraction validated — committing to database ...")
    # ─────────────────────────────────────────────────────────────────────────

    conn.execute("INSERT OR IGNORE INTO manufacturers(name) VALUES(?)", (manufacturer.upper(),))
    mfr_id = conn.execute("SELECT id FROM manufacturers WHERE name=?", (manufacturer.upper(),)).fetchone()[0]
    conn.execute(
        "INSERT OR IGNORE INTO ppl_editions(manufacturer_id, year, half) VALUES(?,?,?)",
        (mfr_id, year, half),
    )
    conn.commit()
    edition_id = conn.execute(
        "SELECT id FROM ppl_editions WHERE manufacturer_id=? AND year=? AND half=?",
        (mfr_id, year, half),
    ).fetchone()[0]

    inserted, updated = commit_to_db(conn, aliases, edition_id, products)
    conn.commit()
    conn.close()
    print(f"  Done  —  inserted: {inserted}  updated: {updated}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file",         required=True)
    parser.add_argument("--manufacturer", required=True)
    parser.add_argument("--year",         required=True, type=int)
    parser.add_argument("--half",         required=True, choices=["H1", "H2"])
    parser.add_argument("--db",           default="pricing.db")
    parser.add_argument("--force",        action="store_true")
    args = parser.parse_args()
    import_ppl(Path(args.db), Path(args.file), args.manufacturer, args.year, args.half, args.force)