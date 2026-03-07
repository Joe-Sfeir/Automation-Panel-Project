#!/usr/bin/env python3
"""
match_offer.py  –  Price SLD component extractions → Technelec offer (Elie format).

Each --input is ONE sld_components.xlsx (= one DB/panel).
Pass multiple panels on the same command line; they appear as separate blocks.

Usage:
    python match_offer.py \\
        --inputs DB-ATTIC.xlsx DB-B1.xlsx ATS-1.xlsx \\
        --db pricing.db \\
        --manufacturer SCHNEIDER \\
        --year 2026 --half H1 \\
        --ref "KORNET EL HAMRA" \\
        --pricing-mode total \\       ← "total" (default) or "itemized"
        --out offer.xlsx

  --pricing-mode total     Client sees only the panel lump-sum HT + TTC.
                           No unit prices visible. Best for privacy.
  --pricing-mode itemized  Client sees Qty | Unit Price | Line Total per item.
                           Use when the client needs a full breakdown.

Output layout (per panel, identical to the Elie offre):
    DB-ATTIC                              1 600.00 €   ← HT (sum of matched prices)
                             TTC:         1 776.00 €   ← =HT*1.11  (formula in cell)
    [thin separator row 8.1pt]
    Switch INS63 4x63A   SCHNEIDER        1
    Fuse holder          SCHNEIDER        3
    ...
    [thin separator row 8.1pt]
    DB-B1                                 2 100.00 €
    ...

A second hidden sheet "Pricing Detail" shows unit prices & match tier for Elie.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import date
from pathlib import Path

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

TVA_RATE   = 0.11        # Lebanese TVA / TTC multiplier
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models"
              "/{model}:generateContent?key={key}")

# Exact column widths from the original offer file
COL_WIDTHS = {"A": 14.66, "B": 1.88, "C": 43.99, "D": 22.99, "E": 10.1, "F": 24.43}
ROW_H_NORMAL = 15.6   # standard row height
ROW_H_THIN   = 8.1    # thin separator between panels

# Number formats (exact from original)
NF_PRICE = '#,##0.00 [$\u20ac-1]'   # HT and TTC values  (€ sign)
NF_TTC_L = '#,##0.00"F.F."'         # TTC: label cell number format


# ─────────────────────────────────────────────────────────────────────────────
#  API KEY  (same resolution order as import.py)
# ─────────────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parents[1]   # goes up from /Codes to project root
ENV_FILE     = PROJECT_ROOT / ".venv" / "key.env"

def get_api_model() -> str:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("GEMINI_API_MODEL"):
            return line.split("=", 1)[1].strip()
    raise ValueError("GEMINI_API_MODEL not found in key.env")

GEMINI_MODEL = get_api_model()
def get_api_key() -> dict:
    config = {"GEMINI_API_KEY": None, "GEMINI_MODEL": GEMINI_MODEL}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        for key in config:
            if line.startswith(key):
                config[key] = line.split("=", 1)[1].strip()
    if not config["GEMINI_API_KEY"]:
        raise ValueError("GEMINI_API_KEY not found in key.env")
    return config


# ─────────────────────────────────────────────────────────────────────────────
#  GEMINI
# ─────────────────────────────────────────────────────────────────────────────

def gemini(system: str, user: str) -> str:
    api_key = get_api_key()["GEMINI_API_KEY"]   # FIX: was passing entire dict into URL
    model = GEMINI_MODEL
    url     = GEMINI_URL.format(model=model, key=api_key)
    payload = json.dumps({"contents": [{"parts": [{"text": system + "\n\n" + user}]}]}).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["candidates"][0]["content"]["parts"][0]["text"].strip()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Gemini API error {e.code}: {e.reason}")


# ─────────────────────────────────────────────────────────────────────────────
#  DB  —  aliases + three-tier matching
# ─────────────────────────────────────────────────────────────────────────────

def load_aliases(conn) -> dict:
    rows = conn.execute("SELECT alias, canonical FROM term_aliases").fetchall()
    return {r[0].lower(): r[1].lower() for r in rows}


def normalise(text: str, aliases: dict) -> list:
    if not text:
        return []
    text = str(text).lower()
    text = re.sub(r"(\d+)x(\d+a)", lambda m: f"{m.group(1)}p {m.group(2)}", text)
    text = re.sub(r"[^\w\s]", " ", text)
    for alias, canonical in sorted(aliases.items(), key=lambda x: -len(x[0])):
        text = re.sub(r"\b" + re.escape(alias) + r"\b", canonical, text)
    return [t for t in text.split() if t]


def keyword_match(conn, aliases: dict, description: str, edition_id: int) -> dict | None:
    tokens = normalise(description, aliases)
    if not tokens:
        return None
    ph   = ",".join("?" * len(tokens))
    rows = conn.execute(f"""
        SELECT p.id, p.reference, p.description, p.price,
               COUNT(DISTINCT pk.keyword) AS score
        FROM products p
        JOIN product_keywords pk ON pk.product_id = p.id
        WHERE p.ppl_edition_id = ? AND pk.keyword IN ({ph})
        GROUP BY p.id ORDER BY score DESC, p.price ASC LIMIT 10
    """, [edition_id] + tokens).fetchall()
    if not rows:
        return None

    best_score = rows[0][4]
    tied = [r for r in rows if r[4] == best_score]

    if len(tied) > 1:
        # Tiebreak in two layers:
        #   1. "Critical" tokens — pole configs (1p+n, 3p …) and sensitivity (30ma, 300ma)
        #      These are physical specs: getting them wrong is worse than wrong model.
        #   2. "Label" tokens — specific model names (ic60h, ins40, ez9 …)
        # Whichever candidate satisfies more critical specs wins; then most label matches;
        # then cheapest.
        GENERIC = {'mcb','mccb','rccb','rcbo','switch','contactor','c','b','d','ac','dc',
                   'type','residual','miniature','circuit','breaker','single','indicator'}
        critical = [t for t in tokens
                    if re.match(r'^\d+p(\+n)?$', t)          # pole configs
                    or re.match(r'^\d+ma$', t)]               # sensitivity values
        labels   = [t for t in tokens
                    if t not in GENERIC
                    and t not in critical
                    and not re.match(r'^\d+[a-z]{0,3}$', t)] # not pure amps/volts

        def _tiebreak(row):
            pid = row[0]
            kws = {k[0] for k in conn.execute(
                "SELECT keyword FROM product_keywords WHERE product_id=?", (pid,)
            ).fetchall()}
            return (
                sum(1 for t in critical if t in kws),   # more critical hits = better
                sum(1 for t in labels   if t in kws),   # then more label hits
                -row[3],                                 # then cheaper
            )

        tied.sort(key=_tiebreak, reverse=True)
        rows = tied + [r for r in rows if r[4] < best_score]

    best = rows[0]
    pct  = round(100 * best[4] / max(len(tokens), 1))
    return {"ref": best[1], "price": best[3], "ppl_desc": best[2],
            "pct": pct, "rows": rows[:5]}


def gemini_pick(desc: str, candidates) -> dict | None:
    ctext = "\n".join(f"  [{i}] ref={c[1]} desc={c[2]} price={c[3]}"
                      for i, c in enumerate(candidates))
    system = "You are an expert in electrical components and product catalogues."
    user   = (f'Match this offer item to the correct price list product.\n'
              f'OFFER ITEM: "{desc}"\nCANDIDATES:\n{ctext}\n'
              f'Consider: product type, poles, current rating, breaking capacity, curve.\n'
              f'Reply ONLY with JSON (no markdown):\n'
              f'{{"match": 0, "reason": "same poles and current"}}\n'
              f'or: {{"match": null, "reason": "wrong product family"}}')
    try:
        raw    = re.sub(r"```json|```", "", gemini(system, user)).strip()
        result = json.loads(raw)
        idx    = result.get("match")
        if idx is not None and 0 <= int(idx) < len(candidates):
            c = candidates[int(idx)]
            return {"ref": c[1], "price": c[3], "ppl_desc": c[2],
                    "note": f"AI: {result.get('reason','')}"}
    except Exception as e:
        print(f"    [AI] parse error: {e}")
    return None


def gemini_sub(desc: str, mfr: str, conn, edition_id: int, aliases: dict) -> dict | None:
    system = "You are an expert in electrical components."
    user   = (f'This item was NOT found in the {mfr} price list: "{desc}"\n'
              f'Standard current steps: 6,10,16,20,25,32,40,50,63,80,100,125,160,200,250,'
              f'320,400,500,630\n'
              f'Reply ONLY with JSON (no markdown):\n'
              f'{{"action":"substitute","original_current":20,"substitute_current":25,'
              f'"search_hint":"INS25 4P"}}\n'
              f'or: {{"action":"not_available","reason":"..."}}')
    try:
        raw    = re.sub(r"```json|```", "", gemini(system, user)).strip()
        result = json.loads(raw)
    except Exception:
        return None
    if result.get("action") != "substitute":
        return None
    tokens = normalise(result.get("search_hint", ""), aliases)
    if not tokens:
        return None
    ph  = ",".join("?" * len(tokens))
    row = conn.execute(f"""
        SELECT p.id, p.reference, p.description, p.price,
               COUNT(DISTINCT pk.keyword) AS score
        FROM products p JOIN product_keywords pk ON pk.product_id = p.id
        WHERE p.ppl_edition_id = ? AND pk.keyword IN ({ph})
        GROUP BY p.id ORDER BY score DESC, p.price ASC LIMIT 1
    """, [edition_id] + tokens).fetchone()
    if row and row[4] > 0:
        orig = result.get("original_current")
        sub  = result.get("substitute_current")
        return {"ref": row[1], "price": row[3], "ppl_desc": row[2],
                "note": f"SUB:{orig}A->{sub}A"}
    return None


def match_component(conn, aliases: dict, edition_id: int,
                    description: str, mfr: str) -> dict:
    """
    Three-tier match. Returns {price, ref, tier, note}. price=0.0 if not found.

    Tier 1 — keyword score >= 60% AND no tie at the top → fast, certain
    Tier 1+AI — score >= 60% BUT tied at top → AI selector resolves the tie
    Tier 2 — score 20-59% → AI selector picks best from top-5
    Tier 3 — score < 20% or no results → AI suggests size substitution
    """
    km = keyword_match(conn, aliases, description, edition_id)
    if km:
        rows       = km["rows"]
        best_score = rows[0][4] if rows else 0
        top_tied   = len([r for r in rows if r[4] == best_score]) > 1

        if km["pct"] >= 60 and not top_tied:
            return {"price": km["price"], "ref": km["ref"], "ppl_desc": km["ppl_desc"],
                    "tier": "T1", "note": f"keyword {km['pct']}%"}

        if km["pct"] >= 20 or (km["pct"] >= 60 and top_tied):
            ai = gemini_pick(description, rows[:5])
            if ai:
                tier = "T1" if (km["pct"] >= 60 and top_tied) else "T2"
                return {"price": ai["price"], "ref": ai["ref"], "ppl_desc": ai["ppl_desc"],
                        "tier": tier, "note": ai["note"]}
            if km["pct"] >= 60:
                return {"price": km["price"], "ref": km["ref"], "ppl_desc": km["ppl_desc"],
                        "tier": "T1", "note": f"keyword {km['pct']}% (AI tie fallback)"}

    sub = gemini_sub(description, mfr, conn, edition_id, aliases)
    if sub:
        return {"price": sub["price"], "ref": sub["ref"], "ppl_desc": sub["ppl_desc"],
                "tier": "T3", "note": sub["note"]}
    return {"price": 0.0, "ref": "", "ppl_desc": "", "tier": "NF", "note": "NOT FOUND"}


# ─────────────────────────────────────────────────────────────────────────────
#  SLD FILE READER  —  parses one sld_components.xlsx
# ─────────────────────────────────────────────────────────────────────────────

def _find_header_row(ws, marker="#"):
    for r in range(1, 8):
        if ws.cell(r, 1).value == marker:
            return r
    return 3


def _col_map(ws, hdr_row: int) -> dict:
    result = {}
    for c in range(1, ws.max_column + 1):
        v = ws.cell(hdr_row, c).value
        if v is not None:
            result[str(v).strip().lower()] = c
    return result


def read_sld_file(xlsx_path: Path) -> dict:
    """
    Parse one SLD output Excel. Auto-detects two formats:

    FORMAT A — multi-sheet (sld_components.xlsx from old pipeline):
        Sheets: 'Components', 'Terminal Schedule', 'Enclosure', ...

    FORMAT B — single-sheet (rawcomponents.xlsx from dwg_test3.py):
        One sheet named after the panel (e.g. 'DB-B1') containing
        components in the top section and terminal schedule below,
        separated by a title row like 'DB-B1  ·  Terminal Block Schedule'.

    Returns {panel_name, components[], terminals[]}
    """
    wb = openpyxl.load_workbook(str(xlsx_path), data_only=True)

    # ── Detect format ────────────────────────────────────────────────────────
    is_format_a = "Components" in wb.sheetnames

    # ── FORMAT A ─────────────────────────────────────────────────────────────
    if is_format_a:
        # Panel name from Enclosure sheet
        panel_name = xlsx_path.stem
        if "Enclosure" in wb.sheetnames:
            we = wb["Enclosure"]
            for row in we.iter_rows(min_row=4, values_only=True):
                if row and row[0] == "Panel Reference":
                    val = row[1]
                    if val and str(val).strip() not in ("", "-"):
                        panel_name = str(val).strip()
                    break

        # Components
        components = []
        ws  = wb["Components"]
        hdr = _find_header_row(ws)
        cm  = _col_map(ws, hdr)

        def gc_a(r, key):
            col = cm.get(key)
            return ws.cell(r, col).value if col else None

        for r in range(hdr + 1, ws.max_row + 1):
            if not isinstance(ws.cell(r, 1).value, (int, float)):
                continue
            comp = {
                "category":          str(gc_a(r, "category")          or "OTHER").strip(),
                "label":             str(gc_a(r, "label")             or "-").strip(),
                "poles":             str(gc_a(r, "poles")             or "-").strip(),
                "amps":              str(gc_a(r, "amps")              or "-").strip(),
                "curve":             str(gc_a(r, "curve")             or "-").strip(),
                "breaking_capacity": str(gc_a(r, "b.c.")              or "-").strip(),
                "sensitivity":       str(gc_a(r, "sensitivity")       or "-").strip(),
                "spd_type":          str(gc_a(r, "spd type")          or "-").strip(),
                "qty":               int(gc_a(r, "qty")               or 1),
                "has_terminal":      str(gc_a(r, "terminal?")         or "").strip() == "✓",
            }
            components.append(comp)

        # Terminals
        terminals = []
        if "Terminal Schedule" in wb.sheetnames:
            wt  = wb["Terminal Schedule"]
            hdr = _find_header_row(wt)
            cm  = _col_map(wt, hdr)
            name_col = cm.get("terminal type", 2)
            qty_col  = cm.get("qty", 5)
            for r in range(hdr + 1, wt.max_row + 1):
                if not isinstance(wt.cell(r, 1).value, (int, float)):
                    continue
                name = wt.cell(r, name_col).value
                qty  = wt.cell(r, qty_col).value
                if name and qty:
                    try:
                        terminals.append({"description": str(name).strip(),
                                          "qty": int(qty)})
                    except (TypeError, ValueError):
                        pass

        wb.close()
        return {"panel_name": panel_name,
                "components": components,
                "terminals":  terminals}

    # ── FORMAT B — single panel sheet ────────────────────────────────────────
    # Find the data sheet (skip any 'Drawing' render sheet)
    data_sheet = None
    for name in wb.sheetnames:
        if name.lower() in ("drawing", "shop drawing", "summary"):
            continue
        ws_try = wb[name]
        # Must have the '#' header somewhere in the first 6 rows
        for r in range(1, 7):
            if ws_try.cell(r, 1).value == "#":
                data_sheet = name
                break
        if data_sheet:
            break

    if data_sheet is None:
        # Last resort: first sheet
        data_sheet = wb.sheetnames[0]

    ws = wb[data_sheet]

    # Panel name: from sheet name itself (most reliable in Format B)
    # Also try to parse from title row: "DB-B1  ·  SLD Component Extraction  ·  ..."
    panel_name = data_sheet
    title_val  = ws.cell(1, 1).value
    if title_val and "·" in str(title_val):
        panel_name = str(title_val).split("·")[0].strip()

    # Find component header row (row where col A == '#')
    comp_hdr = _find_header_row(ws)
    cm       = _col_map(ws, comp_hdr)

    def gc_b(r, key):
        col = cm.get(key)
        return ws.cell(r, col).value if col else None

    # Find where the terminal section starts:
    # It's the first row AFTER comp_hdr+1 where col A is '#' again (terminal header)
    # OR a title row like "DB-B1  ·  Terminal Block Schedule"
    term_hdr = None
    for r in range(comp_hdr + 2, ws.max_row + 1):
        v = ws.cell(r, 1).value
        if v == "#":
            term_hdr = r
            break
        if isinstance(v, str) and "Terminal Block Schedule" in v:
            # header row is 2 rows below the title
            for r2 in range(r + 1, r + 4):
                if ws.cell(r2, 1).value == "#":
                    term_hdr = r2
                    break
            break

    # Parse components (rows between comp_hdr+1 and term_hdr-1, or end)
    comp_end = (term_hdr - 1) if term_hdr else ws.max_row
    components = []
    for r in range(comp_hdr + 1, comp_end + 1):
        if not isinstance(ws.cell(r, 1).value, (int, float)):
            continue
        comp = {
            "category":          str(gc_b(r, "category")          or "OTHER").strip(),
            "label":             str(gc_b(r, "label")             or "-").strip(),
            "poles":             str(gc_b(r, "poles")             or "-").strip(),
            "amps":              str(gc_b(r, "amps")              or "-").strip(),
            "curve":             str(gc_b(r, "curve")             or "-").strip(),
            "breaking_capacity": str(gc_b(r, "b.c.")              or "-").strip(),
            "sensitivity":       str(gc_b(r, "sensitivity")       or "-").strip(),
            "spd_type":          str(gc_b(r, "spd type")          or "-").strip(),
            "qty":               int(gc_b(r, "qty")               or 1),
            "has_terminal":      str(gc_b(r, "terminal?")         or "").strip() == "✓",
        }
        components.append(comp)

    # Parse terminals (rows after term_hdr)
    terminals = []
    if term_hdr:
        tcm      = _col_map(ws, term_hdr)
        name_col = tcm.get("terminal type", 2)
        qty_col  = tcm.get("qty", 5)
        for r in range(term_hdr + 1, ws.max_row + 1):
            if not isinstance(ws.cell(r, 1).value, (int, float)):
                continue
            name = ws.cell(r, name_col).value
            qty  = ws.cell(r, qty_col).value
            if name and qty:
                try:
                    terminals.append({"description": str(name).strip(),
                                      "qty": int(qty)})
                except (TypeError, ValueError):
                    pass

    wb.close()
    return {"panel_name": panel_name,
            "components": components,
            "terminals":  terminals}


# ─────────────────────────────────────────────────────────────────────────────
#  DESCRIPTION BUILDER  —  pure-spec, no SLD label, works for any drawing
# ─────────────────────────────────────────────────────────────────────────────
#
#  Design principle:
#    The SLD label (e.g. "ISOL.S.", "RCD1", "SW", "IC60H") is a DWG block name.
#    It may be meaningful or completely arbitrary — we never rely on it for
#    the DB search query.  We build the query from electrical specs only:
#    category + poles + amps + curve + BC + sensitivity.
#    This guarantees the same spec always produces the same query regardless
#    of which drawing tool or engineer produced the SLD.
#
#  Display name vs search query:
#    build_description() returns a dict with two strings:
#      "query"   → 4-8 keyword tokens fed into keyword_match()
#      "display" → clean client-facing name shown in the offer Excel
#
#  Why no AI here?
#    The AI selector (gemini_pick) already runs whenever there is a tie at
#    the top keyword score.  The query just needs to pull the right candidates
#    into that top-5 — which pure specs do reliably.  Asking AI to generate
#    the query string would require it to know the PPL catalog by heart, which
#    it doesn't.

def build_description(comp: dict) -> dict:
    """
    Returns {"query": str, "display": str}.
    Both are derived from electrical specs only — never the SLD label.
    """
    cat  = (comp.get("category",          "") or "OTHER").upper().strip()
    poles= (comp.get("poles",             "") or "-").strip()
    amps = (comp.get("amps",              "") or "-").strip()
    curve= (comp.get("curve",             "") or "-").strip()
    bc   = (comp.get("breaking_capacity", "") or "-").strip()
    sens = (comp.get("sensitivity",       "") or "-").strip()
    spd  = (comp.get("spd_type",          "") or "-").strip()

    def v(x):  return x if x and x != "-" else None   # truthy check
    def pn(p): m = re.match(r"(\d+)", p or ""); return m.group(1) if m else ""
    def an(a): return re.sub(r"[Aa]$", "", a or "").strip()

    # ── MCB ──────────────────────────────────────────────────────────────────
    if cat == "MCB":
        # query: all discriminating specs — poles config is most important
        q_parts = ["MCB"]
        if v(poles): q_parts.append(poles)
        if v(amps):  q_parts.append(amps)
        if v(curve): q_parts.append(curve)
        if v(bc):    q_parts.append(bc.replace("k","000").replace("K","000")
                                       .replace("kA","000A").replace("KA","000A"))
        query = " ".join(q_parts)
        # display: professional offer style
        d_parts = [f"MCB {amps}" if v(amps) else "MCB"]
        if v(poles): d_parts.append(poles)
        if v(curve) or v(bc):
            d_parts.append(", " + " , ".join(x for x in [curve, bc] if v(x)))
        return {"query": query, "display": " ".join(d_parts)}

    # ── MCCB ─────────────────────────────────────────────────────────────────
    if cat == "MCCB":
        q_parts = ["MCCB"]
        if v(poles): q_parts.append(poles)
        if v(amps):  q_parts.append(amps)
        d_parts = [f"MCCB {amps}" if v(amps) else "MCCB"]
        if v(poles): d_parts.append(poles)
        return {"query": " ".join(q_parts), "display": " ".join(d_parts)}

    # ── RCCB ─────────────────────────────────────────────────────────────────
    if cat == "RCCB":
        # sensitivity is the key discriminator — always include it
        q_parts = ["RCCB"]
        if v(poles): q_parts.append(poles)
        if v(amps):  q_parts.append(amps)
        if v(sens):  q_parts.append(sens)
        q_parts.append("AC")
        query = " ".join(q_parts)
        fields = []
        if v(poles) and v(amps): fields.append(f"{poles} {amps}")
        if v(sens):               fields.append(sens)
        fields.append("AC")
        return {"query": query, "display": "RCCB " + " , ".join(fields)}

    # ── RCBO ─────────────────────────────────────────────────────────────────
    if cat == "RCBO":
        q_parts = ["RCBO"]
        if v(poles): q_parts.append(poles)
        if v(amps):  q_parts.append(amps)
        if v(curve): q_parts.append(curve)
        if v(sens):  q_parts.append(sens)
        d_parts = [f"RCBO {poles}" if v(poles) else "RCBO"]
        if v(amps):  d_parts.append(amps)
        if v(curve): d_parts.append(f", {curve}")
        if v(sens):  d_parts.append(f", {sens}")
        return {"query": " ".join(q_parts), "display": " ".join(d_parts)}

    # ── SWITCH ───────────────────────────────────────────────────────────────
    if cat == "SWITCH":
        n = pn(poles); a = an(amps)
        # "disconnector" is a keyword that appears in INS product descriptions
        # but NOT in ISW (push-button switch) descriptions — this resolves the tie
        q_parts = ["switch", "disconnector"]
        if v(poles): q_parts.append(poles)
        if v(amps):  q_parts.append(amps)
        query = " ".join(q_parts)
        disp = "Switch"
        if n and a: disp += f" {n}x{a}A"
        elif v(amps): disp += f" {amps}"
        return {"query": query, "display": disp}

    # ── CONTACTOR ────────────────────────────────────────────────────────────
    if cat == "CONTACTOR":
        n = pn(poles); a = an(amps)
        q_parts = ["contactor"]
        if v(poles): q_parts.append(poles)
        if v(amps):  q_parts.append(amps)
        disp = "Contactor"
        if n and a:   disp += f" {n}x{a}A/AC3"
        elif v(amps): disp += f" {amps}/AC3"
        return {"query": " ".join(q_parts), "display": disp}

    # ── SPD ──────────────────────────────────────────────────────────────────
    if cat == "SPD":
        q_parts = ["surge", "arrester", "SPD"]
        if v(poles): q_parts.append(poles)
        if v(bc):    q_parts.append(bc)
        disp = f"Surge Arrester {spd}" if v(spd) else "Surge Arrester"
        return {"query": " ".join(q_parts), "display": disp}

    # ── FUSE_HOLDER ──────────────────────────────────────────────────────────
    if cat == "FUSE_HOLDER":
        q_parts = ["fuse", "holder"]
        if v(poles): q_parts.append(poles)
        if v(amps):  q_parts.append(amps)
        disp = "Fuse holder"
        if v(poles): disp += f" {poles}"
        if v(amps):  disp += f" {amps}"
        return {"query": " ".join(q_parts), "display": disp}

    # ── PILOT_LAMP ───────────────────────────────────────────────────────────
    if cat == "PILOT_LAMP":
        # "light" not "lamp" — PPL uses "PILOT LIGHT"
        return {"query": "pilot light indicator AC", "display": "Pilot lamp"}

    # ── METER ────────────────────────────────────────────────────────────────
    if cat == "METER":
        label = (comp.get("label") or "").strip()
        is_ct = bool(re.search(r"\bct\b", label.lower()))
        if is_ct:
            # CT query: "current transformer" keywords, amps from label (e.g. "CT 40/5")
            ratio = re.search(r"\d+/\d+", label)
            q_parts = ["current", "transformer", "CT"]
            if ratio: q_parts.append(ratio.group())
            return {"query": " ".join(q_parts), "display": label or "CT"}
        else:
            q_parts = ["power", "meter", "energy", "digital"]
            if v(poles): q_parts.append(poles)
            return {"query": " ".join(q_parts),
                    "display": label if label and label != "-" else "Digital Power Meter"}

    # ── OTHER / fallback ─────────────────────────────────────────────────────
    label = (comp.get("label") or cat).strip()
    q_parts = [cat.lower()]
    if v(poles): q_parts.append(poles)
    if v(amps):  q_parts.append(amps)
    return {"query": " ".join(q_parts), "display": label}


def infer_brand(comp: dict, default_mfr: str) -> str:
    """Hard rules for components that are always a different brand."""
    cat   = comp.get("category", "").upper()
    label = (comp.get("label") or "").lower()
    if cat == "METER" and re.search(r"\bct\b", label):
        return "MBS"
    if cat == "SPD":
        return "DHEN"
    return default_mfr


# ─────────────────────────────────────────────────────────────────────────────
#  EXCEL OFFER WRITER  —  exact Elie format
# ─────────────────────────────────────────────────────────────────────────────

def _font(name="Arial", size=12, bold=False, italic=False, color="000000"):
    return Font(name=name, size=size, bold=bold, italic=italic, color=color)


def _al(h="general", v="center", wrap=False):
    return Alignment(horizontal=h, vertical=v, wrap_text=wrap)


def write_offer(panels: list, out_path: Path, ref: str,
                pricing_mode: str = "total"):
    """
    Write the Pricing Detail sheet — single flat table for the designer.

    Columns:
        Panel | Description | Brand | Qty | Ref.
        | Currency | Unit Price | Total | PPL Description | Tier

    The client-facing Offer letter is produced separately by export.py.
    pricing_mode and ref are accepted for API compatibility but not used here.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Pricing Detail"

    PD_SIDE   = Side(style="thin", color="BBBBBB")
    PD_BORDER = Border(left=PD_SIDE, right=PD_SIDE,
                       top=PD_SIDE, bottom=PD_SIDE)

    HEADERS = ["Panel", "Description", "Brand", "Qty", "Ref.",
               "Currency", "Unit Price", "Total",
               "PPL Description", "Tier"]
    WIDTHS  = [16,      42,              14,      5,    18,
               9,        13,              13,
               58,       7]

    hdr_fill = PatternFill("solid", fgColor="1F3864")
    hdr_font = Font(name="Arial", bold=True, size=10, color="FFFFFF")

    for ci, (h, w) in enumerate(zip(HEADERS, WIDTHS), 1):
        cell           = ws.cell(1, ci, h)
        cell.font      = hdr_font
        cell.fill      = hdr_fill
        cell.border    = PD_BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center",
                                   wrap_text=True)
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.row_dimensions[1].height = 24
    ws.freeze_panes = "A2"

    TIER_CLR = {"T1": "C6EFCE", "T2": "FFEB9C", "T3": "BDD7EE", "NF": "D9D9D9"}
    dr = 2

    for panel in panels:
        for item in panel["items"]:
            fill_c   = PatternFill("solid",
                                   fgColor=TIER_CLR.get(item.get("tier", ""), "FFFFFF"))
            currency = item.get("currency", "EUR")
            up       = item.get("unit_price")
            tot      = item.get("total")

            vals = [
                panel["panel_name"],
                item["desc"],
                item["brand"],
                item["qty"],
                item.get("ref") or "-",
                currency,
                up   if up   else "-",
                tot  if tot  else "-",
                item.get("ppl_desc") or "-",
                item.get("tier", ""),
            ]
            for ci, v in enumerate(vals, 1):
                cell        = ws.cell(dr, ci, v)
                cell.fill   = fill_c
                cell.border = PD_BORDER
                cell.font   = Font(name="Arial", size=10, bold=(ci == 5))
                if ci in (7, 8) and isinstance(v, (int, float)):
                    cell.number_format = "#,##0.00"
                    cell.alignment     = Alignment(horizontal="right", vertical="center")
                elif ci == 9:
                    cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
                elif ci in (3, 4, 5, 6, 10):
                    cell.alignment = Alignment(horizontal="center", vertical="center")
                else:
                    cell.alignment = Alignment(horizontal="left", vertical="center")
            ws.row_dimensions[dr].height = None
            dr += 1

    dr += 1
    for text, color in [
        ("T1 = Keyword match (>=60%, clear winner)", "C6EFCE"),
        ("T2 = AI matched",                          "FFEB9C"),
        ("T3 = Size substituted",                    "BDD7EE"),
        ("NF = Not found in DB",                     "D9D9D9"),
    ]:
        cell      = ws.cell(dr, 1, text)
        cell.fill = PatternFill("solid", fgColor=color)
        cell.font = Font(name="Arial", size=9, italic=True)
        dr += 1

    wb.save(str(out_path))
    print(f"\n  Pricing Detail saved -> {out_path}")


def run_match(
    inputs:       list,
    db:           str   = "pricing.db",
    manufacturer: str   = "SCHNEIDER",
    year:         int   = 2026,
    half:         str   = "H1",
    ref:          str   = "",
    tva:          float = TVA_RATE,
    pricing_mode: str   = "total",
    out:          str   = "offer.xlsx",
    progress_cb         = None,
) -> dict:
    """
    Run the full match pipeline and write the offer Excel.

    Returns:
        {
            "panels":     list of panel dicts (each has panel_name, ht, ttc, items),
            "grand_ht":   float,
            "grand_ttc":  float,
            "excel_path": str,
            "errors":     list of str,
        }

    progress_cb(msg: str):
        Optional callable for live progress.  PySide6 usage:
            def on_log(msg):
                self.log_widget.append(msg)
            run_match(..., progress_cb=on_log)
    """
    def log(msg: str):
        print(msg)
        if progress_cb:
            progress_cb(msg)

    errors   = []
    out_path = Path(out)

    db_path = Path(db)
    if not db_path.exists():
        msg = f"ERROR: DB not found: {db_path}"
        log(msg); errors.append(msg)
        return {"panels": [], "grand_ht": 0.0, "grand_ttc": 0.0,
                "excel_path": "", "errors": errors}

    conn    = sqlite3.connect(str(db_path))
    aliases = load_aliases(conn)

    edition_row = conn.execute("""
        SELECT pe.id FROM ppl_editions pe
        JOIN manufacturers m ON m.id = pe.manufacturer_id
        WHERE m.name = ? AND pe.year = ? AND pe.half = ?
    """, (manufacturer.upper(), year, half)).fetchone()

    if not edition_row:
        msg = f"ERROR: No price list for {manufacturer} {year} {half}. Run import.py first."
        log(msg); errors.append(msg); conn.close()
        return {"panels": [], "grand_ht": 0.0, "grand_ttc": 0.0,
                "excel_path": "", "errors": errors}

    edition_id = edition_row[0]
    mfr        = manufacturer.upper()

    log(f"\nPricing edition : {mfr} {year} {half}")
    log(f"Input files     : {len(inputs)}")
    log(f"Output          : {out}\n")

    all_panels = []

    for xlsx_str in inputs:
        xlsx_path = Path(xlsx_str)
        if not xlsx_path.exists():
            msg = f"WARNING: File not found — {xlsx_path}, skipping"
            log(msg); errors.append(msg); continue

        log(f"{'─'*60}")
        log(f"Panel file : {xlsx_path.name}")

        sld   = read_sld_file(xlsx_path)
        pname = sld["panel_name"]
        comps = sld["components"]
        terms = sld["terminals"]

        log(f"Panel name : {pname}")
        log(f"Components : {len(comps)}  |  Terminal types: {len(terms)}")
        log("")

        items    = []
        ht_total = 0.0

        for comp in comps:
            bd    = build_description(comp)
            desc  = bd["display"]
            query = bd["query"]
            brand = infer_brand(comp, mfr)
            qty   = comp["qty"]

            result   = match_component(conn, aliases, edition_id, query, mfr)
            up       = result["price"]
            total    = round(up * qty, 2)
            ht_total += total

            tag     = {"T1":"OK ","T2":"AI ","T3":"SUB","NF":"---"}.get(result["tier"],"---")
            ref_str = result["ref"] if result["ref"] else "-"
            log(f"  [{tag}] {desc[:40]:40s}  x{qty:3d}  "
                f"@ {up:8.2f}  = {total:10.2f}  [{ref_str}]  {result['note']}")

            items.append({"desc": desc, "brand": brand, "qty": qty,
                          "unit_price": up, "total": total,
                          "tier": result["tier"], "ref": result["ref"],
                          "ppl_desc": result["ppl_desc"]})

        for term in terms:
            desc  = term["description"]
            qty   = term["qty"]
            brand = "PHOENIX"

            result   = match_component(conn, aliases, edition_id, desc, mfr)
            up       = result["price"]
            total    = round(up * qty, 2)
            ht_total += total

            tag     = {"T1":"OK ","T2":"AI ","T3":"SUB","NF":"---"}.get(result["tier"],"---")
            ref_str = result["ref"] if result["ref"] else "-"
            log(f"  [{tag}] {desc[:40]:40s}  x{qty:3d}  "
                f"@ {up:8.2f}  = {total:10.2f}  [{ref_str}]  {result['note']}")

            items.append({"desc": desc, "brand": brand, "qty": qty,
                          "unit_price": up, "total": total,
                          "tier": result["tier"], "ref": result["ref"],
                          "ppl_desc": result["ppl_desc"]})

        ttc = round(ht_total * (1 + tva), 2)
        log(f"\n  >> {pname}  HT = {ht_total:,.2f}  |  TTC = {ttc:,.2f}\n")
        all_panels.append({"panel_name": pname, "ht": ht_total,
                            "ttc": ttc, "items": items})

    conn.close()

    if not all_panels:
        msg = "ERROR: No panels were processed."
        log(msg); errors.append(msg)
        return {"panels": [], "grand_ht": 0.0, "grand_ttc": 0.0,
                "excel_path": "", "errors": errors}

    log(f"{'─'*60}")
    log("Writing Excel offer ...")
    write_offer(all_panels, out_path, ref, pricing_mode=pricing_mode)

    grand_ht  = sum(p["ht"]  for p in all_panels)
    grand_ttc = sum(p["ttc"] for p in all_panels)

    log(f"\n{'='*60}")
    log(f"  Panels processed : {len(all_panels)}")
    log(f"  Grand HT         : {grand_ht:,.2f}")
    log(f"  Grand TTC        : {grand_ttc:,.2f}  (TVA {tva*100:.0f}%)")
    log(f"{'='*60}")
    log(f"  Pricing Detail sheet: ref | unit price | PPL description — all matches visible")
    log(f"  Colours: GREEN=keyword  YELLOW=AI  BLUE=substituted  GREY=not found\n")

    return {"panels": all_panels, "grand_ht": grand_ht, "grand_ttc": grand_ttc,
            "excel_path": str(out_path), "errors": errors}


def main():
    parser = argparse.ArgumentParser(
        description="Price SLD components → Technelec offer Excel.")
    parser.add_argument("--inputs",       nargs="+", required=True)
    parser.add_argument("--db",           default="pricing.db")
    parser.add_argument("--manufacturer", default="SCHNEIDER")
    parser.add_argument("--year",         type=int, default=2026)
    parser.add_argument("--half",         default="H1", choices=["H1","H2"])
    parser.add_argument("--ref",          default="")
    parser.add_argument("--tva",          type=float, default=TVA_RATE)
    parser.add_argument("--pricing-mode", choices=["total","itemized"],
                        default="total", dest="pricing_mode")
    parser.add_argument("--out",          default="offer.xlsx")
    args = parser.parse_args()

    result = run_match(
        inputs=args.inputs, db=args.db, manufacturer=args.manufacturer,
        year=args.year, half=args.half, ref=args.ref, tva=args.tva,
        pricing_mode=args.pricing_mode, out=args.out,
    )
    if result["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()