#!/usr/bin/env python3
"""
match_offer.py  –  Match a Technelec offer Excel against the pricing DB.

Three-tier matching per line item:
  Tier 1  – keyword scoring  (fast, free)              → score >= 60%
  Tier 2  – Claude picks best from top-5 candidates    → score 20-60%
  Tier 3  – Claude size substitution                   → nothing found

Usage:
    python match_offer.py --offer "SLD_BOM.xlsx" --db pricing.db --out offer_priced.xlsx
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

import openpyxl
import openpyxl.utils as xu
from openpyxl.styles import PatternFill, Font


# ── CONFIG ────────────────────────────────────────────────────────────────────

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
MODEL         = "claude-sonnet-4-20250514"

CURRENT_STEPS = [1, 2, 4, 6, 10, 13, 16, 20, 25, 32, 40, 50, 63, 80,
                 100, 125, 160, 200, 250, 315, 320, 400, 500, 630, 800, 1000, 1250, 1600]

FILL_GREEN  = PatternFill("solid", fgColor="C6EFCE")   # keyword match
FILL_YELLOW = PatternFill("solid", fgColor="FFEB9C")   # AI matched
FILL_BLUE   = PatternFill("solid", fgColor="BDD7EE")   # size substituted
FILL_GREY   = PatternFill("solid", fgColor="D9D9D9")   # not found
FONT_BOLD   = Font(bold=True)


# ── API KEY ───────────────────────────────────────────────────────────────────

def get_api_key() -> str:
    # 1. check environment variable
    key = os.getenv("ANTHROPIC_API_KEY")
    if key:
        return key
    # 2. check api.env file in same folder
    env_file = Path(__file__).parent / "api.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY"):
                return line.split("=", 1)[1].strip().strip('"')
    print("ERROR: No API key found.")
    print("  Option 1 - set environment variable:  set ANTHROPIC_API_KEY=sk-ant-...")
    print("  Option 2 - add to api.env file:       ANTHROPIC_API_KEY=sk-ant-...")
    sys.exit(1)


# ── CLAUDE API ────────────────────────────────────────────────────────────────

def claude(system: str, user: str, max_tokens: int = 1000) -> str:
    api_key = get_api_key()
    payload = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        ANTHROPIC_API,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["content"][0]["text"].strip()


def claude_pick_best(offer_desc: str, candidates: list) -> dict | None:
    """Tier 2: Claude picks the correct match from top keyword candidates."""
    cand_text = "\n".join(
        f"  [{i}] ref={c[1]}  desc={c[2]}  price={c[3]}"
        for i, c in enumerate(candidates)
    )
    system = "You are an expert in electrical components and product catalogues."
    user = f"""Match this offer item to the correct price list product.

OFFER ITEM: "{offer_desc}"

CANDIDATES:
{cand_text}

Consider: product type, poles, current rating, breaking capacity, curve letter.
Reply ONLY with JSON (no markdown):
{{"match": 0, "reason": "same poles and current"}}
or if none fit:
{{"match": null, "reason": "wrong product family"}}
"""
    raw = claude(system, user)
    try:
        raw = re.sub(r"^```json|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        result = json.loads(raw)
        idx = result.get("match")
        if idx is not None and 0 <= int(idx) < len(candidates):
            c = candidates[int(idx)]
            return {
                "product_id":  c[0],
                "reference":   c[1],
                "description": c[2],
                "price":       c[3],
                "note":        f"AI matched: {result.get('reason', '')}",
            }
    except Exception as e:
        print(f"    [AI] parse error: {e}")
    return None


def claude_size_substitute(offer_desc: str, manufacturer: str,
                           conn, edition_id: int, aliases: dict) -> dict | None:
    """Tier 3: Nothing found — ask Claude if a size substitution is possible."""
    system = "You are an expert in electrical components."
    user = f"""This item was NOT found in the {manufacturer} price list:
"{offer_desc}"

Possible reasons:
1. The exact current rating doesn't exist — need to step up to next standard size
2. The manufacturer uses a different model name
3. Genuinely not in this product range

Standard current steps: 6,10,16,20,25,32,40,50,63,80,100,125,160,200,250,320,400,500,630

Reply ONLY with JSON (no markdown):
{{"action": "substitute", "original_current": 20, "substitute_current": 25, "search_hint": "INS25 4P"}}
or:
{{"action": "not_available", "reason": "PLC is not in an electrical PPL"}}
"""
    raw = claude(system, user)
    try:
        raw = re.sub(r"^```json|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        result = json.loads(raw)
    except Exception as e:
        print(f"    [SUB] parse error: {e}")
        return None

    if result.get("action") != "substitute":
        return None

    hint = result.get("search_hint", "")
    orig = result.get("original_current")
    sub  = result.get("substitute_current")

    if not hint:
        return None

    tokens = normalise(hint, aliases)
    if not tokens:
        return None

    placeholders = ",".join("?" * len(tokens))
    sql = f"""
        SELECT p.id, p.reference, p.description, p.price,
               COUNT(DISTINCT pk.keyword) AS score
        FROM products p
        JOIN product_keywords pk ON pk.product_id = p.id
        WHERE p.ppl_edition_id = ?
          AND pk.keyword IN ({placeholders})
        GROUP BY p.id
        ORDER BY score DESC, p.price ASC
        LIMIT 1
    """
    row = conn.execute(sql, [edition_id] + tokens).fetchone()
    if row and row[4] > 0:
        return {
            "product_id":  row[0],
            "reference":   row[1],
            "description": row[2],
            "price":       row[3],
            "note":        f"SUBSTITUTED: {orig}A → {sub}A (next available size)",
            "substituted": True,
        }
    return None


# ── DYNAMIC COLUMN MAPPING ────────────────────────────────────────────────────

def map_columns(ws) -> dict:
    """Scan row 1 headers to find columns dynamically."""
    candidates = {
        'desc': ['description', 'item description', 'component', 'label', 'item'],
        'mfr':  ['brand', 'manufacturer', 'mfr', 'make'],
        'qty':  ['qty', 'quantity', 'count', 'amount'],
    }
    found = {}
    for col in range(1, ws.max_column + 1):
        val = ws.cell(row=1, column=col).value
        if not val:
            continue
        val_str = str(val).strip().lower()
        for key, names in candidates.items():
            if val_str in names:
                found[key] = col

    missing = [k for k in ['desc', 'qty'] if k not in found]
    if missing:
        print(f"ERROR: Could not find required columns: {', '.join(missing)}")
        print(f"  Row 1 contains: {[ws.cell(1,c).value for c in range(1, ws.max_column+1)]}")
        sys.exit(1)

    if 'mfr' not in found:
        print("Warning: No manufacturer/brand column found. Will process all rows.")
        found['mfr'] = None

    # output columns go after existing data
    next_col = ws.max_column + 1
    found['ref']   = next_col;     next_col += 1
    found['price'] = next_col;     next_col += 1
    found['total'] = next_col;     next_col += 1
    found['note']  = next_col

    return found


# ── NORMALISATION ─────────────────────────────────────────────────────────────

def load_aliases(conn) -> dict:
    rows = conn.execute("SELECT alias, canonical FROM term_aliases").fetchall()
    return {r[0].lower(): r[1].lower() for r in rows}


def normalise(text: str, aliases: dict) -> list:
    if not text:
        return []
    text = str(text).lower()
    text = re.sub(r'(\d+)x(\d+a)', lambda m: f"{m.group(1)}p {m.group(2)}", text)
    text = re.sub(r'[^\w\s]', ' ', text)
    for alias, canonical in sorted(aliases.items(), key=lambda x: -len(x[0])):
        text = re.sub(r'\b' + re.escape(alias) + r'\b', canonical, text)
    return [t for t in text.split() if t]


# ── TIER 1: KEYWORD MATCH ─────────────────────────────────────────────────────

def keyword_match(conn, aliases, description: str, edition_id: int) -> dict | None:
    tokens = normalise(description, aliases)
    if not tokens:
        return None
    placeholders = ",".join("?" * len(tokens))
    sql = f"""
        SELECT p.id, p.reference, p.description, p.price,
               COUNT(DISTINCT pk.keyword) AS score
        FROM products p
        JOIN product_keywords pk ON pk.product_id = p.id
        WHERE p.ppl_edition_id = ?
          AND pk.keyword IN ({placeholders})
        GROUP BY p.id
        ORDER BY score DESC, p.price ASC
        LIMIT 5
    """
    rows = conn.execute(sql, [edition_id] + tokens).fetchall()
    if not rows:
        return None
    best = rows[0]
    pct  = round(100 * best[4] / max(len(tokens), 1))
    return {
        "product_id":  best[0],
        "reference":   best[1],
        "description": best[2],
        "price":       best[3],
        "score":       best[4],
        "pct":         pct,
        "candidates":  rows,
        "note":        f"keyword match {pct}%",
    }


# ── MAIN ──────────────────────────────────────────────────────────────────────

def match_offer(db_path, offer_path, manufacturer, year, half, out_path):
    conn    = sqlite3.connect(str(db_path))
    aliases = load_aliases(conn)

    row = conn.execute(
        """SELECT pe.id FROM ppl_editions pe
           JOIN manufacturers m ON m.id = pe.manufacturer_id
           WHERE m.name=? AND pe.year=? AND pe.half=?""",
        (manufacturer.upper(), year, half),
    ).fetchone()
    if not row:
        print(f"ERROR: No price list found for {manufacturer} {year} {half}. Run import_ppl.py first.")
        conn.close()
        return
    edition_id = row[0]

    print(f"Reading offer: {offer_path} ...")
    wb = openpyxl.load_workbook(str(offer_path))
    ws = wb.active

    # dynamic column detection
    cols = map_columns(ws)
    print(f"  Columns found — desc:{cols['desc']}  qty:{cols['qty']}  mfr:{cols['mfr']}")
    print(f"  Output columns — ref:{cols['ref']}  price:{cols['price']}  total:{cols['total']}  note:{cols['note']}")

    # write output headers
    for col, label in [(cols['ref'],   "Matched Ref."),
                       (cols['price'], "Unit Price (EUR)"),
                       (cols['total'], "Total (EUR)"),
                       (cols['note'],  "Match Note")]:
        ws.cell(1, col).value = label
        ws.cell(1, col).font  = FONT_BOLD

    summary = {"tier1": 0, "tier2_ai": 0, "tier3_sub": 0, "not_found": 0, "skipped": 0}

    for row_num in range(2, ws.max_row + 1):
        desc_val = ws.cell(row_num, cols['desc']).value
        qty_val  = ws.cell(row_num, cols['qty']).value

        if not desc_val or not str(desc_val).strip():
            summary["skipped"] += 1
            continue

        # skip other manufacturers if column exists
        if cols['mfr']:
            mfr_val = ws.cell(row_num, cols['mfr']).value
            if mfr_val and str(mfr_val).strip().upper() != manufacturer.upper():
                summary["skipped"] += 1
                continue

        desc_str = str(desc_val).strip()
        try:
            qty = float(qty_val) if qty_val else 1.0
        except (TypeError, ValueError):
            qty = 1.0

        match = None
        fill  = FILL_GREY
        tier  = "not_found"

        # ── Tier 1: keyword ───────────────────────────────────────────────────
        km = keyword_match(conn, aliases, desc_str, edition_id)

        if km and km["pct"] >= 60:
            match = km
            fill  = FILL_GREEN
            tier  = "tier1"
            summary["tier1"] += 1

        # ── Tier 2: Claude picks from candidates ──────────────────────────────
        elif km and km["pct"] >= 20:
            print(f"  Row {row_num:4d} [AI?] {desc_str[:40]} (score {km['pct']}%) ...")
            ai = claude_pick_best(desc_str, km["candidates"])
            if ai:
                match = ai
                fill  = FILL_YELLOW
                tier  = "tier2_ai"
                summary["tier2_ai"] += 1
            else:
                sub = claude_size_substitute(desc_str, manufacturer, conn, edition_id, aliases)
                if sub:
                    match = sub
                    fill  = FILL_BLUE
                    tier  = "tier3_sub"
                    summary["tier3_sub"] += 1

        # ── Tier 3: nothing at all ────────────────────────────────────────────
        else:
            print(f"  Row {row_num:4d} [SUB?] {desc_str[:40]} (no keyword match) ...")
            sub = claude_size_substitute(desc_str, manufacturer, conn, edition_id, aliases)
            if sub:
                match = sub
                fill  = FILL_BLUE
                tier  = "tier3_sub"
                summary["tier3_sub"] += 1

        # ── write result ──────────────────────────────────────────────────────
        if match:
            ws.cell(row_num, cols['ref']).value   = match["reference"]
            ws.cell(row_num, cols['price']).value = match["price"]
            ws.cell(row_num, cols['total']).value = round(qty * match["price"], 2)
            ws.cell(row_num, cols['note']).value  = match.get("note", "")
            tag = {"tier1": "OK ", "tier2_ai": "AI ", "tier3_sub": "SUB"}.get(tier, "---")
            print(f"  Row {row_num:4d} [{tag}] {desc_str[:40]:40s} → {match['reference']} @ {match['price']}")
        else:
            ws.cell(row_num, cols['ref']).value  = "NOT FOUND"
            ws.cell(row_num, cols['note']).value = "No match in PPL"
            summary["not_found"] += 1
            print(f"  Row {row_num:4d} [---] {desc_str[:40]:40s} → NOT FOUND")

        for col in [cols['ref'], cols['price'], cols['total'], cols['note']]:
            ws.cell(row_num, col).fill = fill

    conn.close()

    # column widths
    for col, w in [(cols['ref'], 18), (cols['price'], 16),
                   (cols['total'], 14), (cols['note'], 45)]:
        ws.column_dimensions[xu.get_column_letter(col)].width = w

    wb.save(str(out_path))
    print(f"\nSaved: {out_path}")
    print(f"Summary:  keyword={summary['tier1']}  AI={summary['tier2_ai']}  "
          f"substituted={summary['tier3_sub']}  not_found={summary['not_found']}  skipped={summary['skipped']}")
    print()
    print("Colours:  GREEN=keyword  YELLOW=AI matched  BLUE=size substituted  GREY=not found")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--offer",        required=True)
    parser.add_argument("--db",           default="pricing.db")
    parser.add_argument("--manufacturer", default="SCHNEIDER")
    parser.add_argument("--year",         default=2026, type=int)
    parser.add_argument("--half",         default="H1")
    parser.add_argument("--out",          default="offer_priced.xlsx")
    args = parser.parse_args()
    match_offer(Path(args.db), Path(args.file if hasattr(args,'file') else args.offer),
                args.manufacturer, args.year, args.half, Path(args.out))