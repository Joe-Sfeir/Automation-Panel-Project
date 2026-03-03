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
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

TVA_RATE   = 0.11        # Lebanese TVA / TTC multiplier
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models"
              "/{model}:generateContent?key={key}")
GEMINI_MOD = "gemini-2.0-flash"

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

def get_api_key() -> tuple[str, str]:
    """Returns (key, model). Reads GEMINI_API_KEY from env or key.env files."""
    model = GEMINI_MOD
    key   = os.getenv("GEMINI_API_KEY")
    if key:
        return key, model
    search_paths = [
        Path(__file__).resolve().parents[1] / ".venv" / "key.env",
        Path(__file__).parent / "key.env",
        Path(__file__).parent / "api.env",
    ]
    for env_file in search_paths:
        if not env_file.exists():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("GEMINI_API_KEY="):
                key = line.split("=", 1)[1].strip()
            if line.startswith("GEMINI_API_MODEL="):
                model = line.split("=", 1)[1].strip()
        if key:
            return key, model
    print("ERROR: No Gemini API key found.")
    print("  Set env variable GEMINI_API_KEY=AIza...  or add to key.env")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
#  GEMINI
# ─────────────────────────────────────────────────────────────────────────────

def gemini(system: str, user: str) -> str:
    api_key, model = get_api_key()
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
        GROUP BY p.id ORDER BY score DESC, p.price ASC LIMIT 5
    """, [edition_id] + tokens).fetchall()
    if not rows:
        return None
    best = rows[0]
    pct  = round(100 * best[4] / max(len(tokens), 1))
    return {"ref": best[1], "price": best[3], "pct": pct, "rows": rows}


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
            return {"ref": c[1], "price": c[3], "note": f"AI: {result.get('reason','')}"}
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
        return {"ref": row[1], "price": row[3], "note": f"SUB:{orig}A->{sub}A"}
    return None


def match_component(conn, aliases: dict, edition_id: int,
                    description: str, mfr: str) -> dict:
    """Three-tier match. Returns {price, ref, tier, note}. price=0.0 if not found."""
    km = keyword_match(conn, aliases, description, edition_id)
    if km:
        if km["pct"] >= 60:
            return {"price": km["price"], "ref": km["ref"],
                    "tier": "T1", "note": f"keyword {km['pct']}%"}
        if km["pct"] >= 20:
            ai = gemini_pick(description, km["rows"])
            if ai:
                return {"price": ai["price"], "ref": ai["ref"],
                        "tier": "T2", "note": ai["note"]}
    sub = gemini_sub(description, mfr, conn, edition_id, aliases)
    if sub:
        return {"price": sub["price"], "ref": sub["ref"],
                "tier": "T3", "note": sub["note"]}
    return {"price": 0.0, "ref": "", "tier": "NF", "note": "NOT FOUND"}


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
    Parse one sld_components.xlsx.
    Returns {panel_name, components[], terminals[]}
    """
    wb = openpyxl.load_workbook(str(xlsx_path), data_only=True)

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
    if "Components" in wb.sheetnames:
        ws  = wb["Components"]
        hdr = _find_header_row(ws)
        cm  = _col_map(ws, hdr)

        def gc(r, key):
            col = cm.get(key)
            return ws.cell(r, col).value if col else None

        for r in range(hdr + 1, ws.max_row + 1):
            if not isinstance(ws.cell(r, 1).value, (int, float)):
                continue
            comp = {
                "category":          str(gc(r, "category")          or "OTHER").strip(),
                "label":             str(gc(r, "label")             or "-").strip(),
                "poles":             str(gc(r, "poles")             or "-").strip(),
                "amps":              str(gc(r, "amps")              or "-").strip(),
                "curve":             str(gc(r, "curve")             or "-").strip(),
                "breaking_capacity": str(gc(r, "b.c.")              or "-").strip(),
                "sensitivity":       str(gc(r, "sensitivity")       or "-").strip(),
                "spd_type":          str(gc(r, "spd type")          or "-").strip(),
                "qty":               int(gc(r, "qty")               or 1),
                "has_terminal":      str(gc(r, "terminal?")         or "").strip() == "✓",
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
                    terminals.append({"description": str(name).strip(), "qty": int(qty)})
                except (TypeError, ValueError):
                    pass

    wb.close()
    return {"panel_name": panel_name, "components": components, "terminals": terminals}


# ─────────────────────────────────────────────────────────────────────────────
#  DESCRIPTION BUILDER  —  turns sld_components fields → offer description
# ─────────────────────────────────────────────────────────────────────────────

def build_description(comp: dict) -> str:
    """
    Examples matching the Elie offer style:
        MCB DPNN 10A 1P+N , C , 6KA
        Switch INS63 4x63A
        RCCB ID , 4P 25A , 30mA , AC
        CT 40/5
        Surge Arrester type 2
    """
    cat   = comp.get("category", "OTHER").upper()
    label = (comp.get("label")             or "-").strip()
    poles = (comp.get("poles")             or "-").strip()
    amps  = (comp.get("amps")              or "-").strip()
    curve = (comp.get("curve")             or "-").strip()
    bc    = (comp.get("breaking_capacity") or "-").strip()
    sens  = (comp.get("sensitivity")       or "-").strip()
    spd   = (comp.get("spd_type")          or "-").strip()

    def d(v):  return v if v and v != "-" else None
    def pn(p): m = re.match(r"(\d+)", p or ""); return m.group(1) if m else ""
    def an(a): return re.sub(r"[Aa]$", "", a or "").strip()

    if cat == "MCB":
        parts = [f"MCB {label}"]
        if d(amps):  parts.append(amps)
        if d(poles): parts.append(poles)
        suf = []
        if d(curve): suf.append(curve)
        if d(bc):    suf.append(bc)
        if suf:      parts.append(", " + " , ".join(suf))
        return " ".join(parts)

    if cat == "MCCB":
        parts = [f"MCCB {label}"]
        if d(amps):  parts.append(amps)
        if d(poles): parts.append(poles)
        return " ".join(parts)

    if cat == "RCCB":
        head = "RCCB"
        if d(label) and label.upper() not in ("RCCB", "-"):
            head += f" {label}"
        fields = []
        if d(poles) and d(amps): fields.append(f"{poles} {amps}")
        elif d(poles):            fields.append(poles)
        elif d(amps):             fields.append(amps)
        if d(sens):               fields.append(sens)
        fields.append("AC")
        return " , ".join([head] + fields)

    if cat == "RCBO":
        parts = [f"RCBO {label}"]
        if d(poles): parts.append(poles)
        if d(amps):  parts.append(amps)
        if d(curve): parts.append(f", {curve}")
        if d(bc):    parts.append(f", {bc}")
        if d(sens):  parts.append(f", {sens}")
        return " ".join(parts)

    if cat == "SWITCH":
        n = pn(poles); a = an(amps)
        desc = f"Switch {label}"
        if n and a:   desc += f" {n}x{a}A"
        elif d(amps): desc += f" {amps}"
        return desc

    if cat == "CONTACTOR":
        n = pn(poles); a = an(amps)
        desc = f"Contactor {label}"
        if n and a:   desc += f" {n}x{a}A/AC3"
        elif d(amps): desc += f" {amps}/AC3"
        return desc

    if cat == "SPD":
        if d(spd): return f"Surge Arrester {spd}"
        return "Surge Arrester"

    if cat == "FUSE_HOLDER": return "Fuse holder"
    if cat == "PILOT_LAMP":  return "Pilot lamp"
    if cat == "METER":       return label   # "CT 40/5", "Digital Power Meter", etc.

    return label   # OTHER, ATS, VFD, etc.


def infer_brand(comp: dict, default_mfr: str) -> str:
    """Guess brand from component type where clearly not the main manufacturer."""
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
    Writes the final offer Excel in the exact Elie format.

    pricing_mode:
        "total"    — client sees only the panel HT + TTC lump sum (default, private)
        "itemized" — client sees unit price + line total for every component

    panels: list of dicts:
        { "panel_name": str, "ht": float, "items": [ {...} ] }
    """
    show_prices = (pricing_mode == "itemized")
    wb = Workbook()
    ws = wb.active
    ws.title = "Offer"

    # Column widths (exact from original)
    for col, w in COL_WIDTHS.items():
        ws.column_dimensions[col].width = w

    # Extra columns for itemized mode: G = Unit Price, H = Line Total
    if show_prices:
        ws.column_dimensions["G"].width = 20
        ws.column_dimensions["H"].width = 20

    # Row counter (mutable list so inner helpers can modify it)
    r = [1]

    def wc(col_letter, value, *,
           bold=False, italic=False, size=12, fname="Arial",
           align="general", valign="center", wrap=False,
           numfmt=None, color="000000"):
        """Write to current row, specified column. Does NOT advance row."""
        col  = ord(col_letter.upper()) - ord("A") + 1
        cell = ws.cell(r[0], col, value)
        cell.font      = _font(name=fname, size=size, bold=bold,
                                italic=italic, color=color)
        cell.alignment = _al(h=align, v=valign, wrap=wrap)
        if numfmt:
            cell.number_format = numfmt
        ws.row_dimensions[r[0]].height = ROW_H_NORMAL
        return cell

    def nl(n=1):
        """Advance row counter by n (blank rows cost nothing)."""
        r[0] += n

    def thin():
        """Mark current row as the 8.1pt thin separator, then advance."""
        ws.row_dimensions[r[0]].height = ROW_H_THIN
        r[0] += 1

    # ── Letter header ────────────────────────────────────────────────────────
    wc("C", "MM ", bold=True, size=14, fname="MS Sans Serif"); nl()
    nl(2)   # rows 2-3 blank

    # Row 4: Att / Date
    wc("C", "Att : ", bold=True)
    wc("E", "Date :")
    wc("F", date.today(), numfmt="DD/MM/YYYY")
    nl()

    # Row 5: Réf.
    wc("E", "Réf. :")
    wc("F", ref, bold=True, italic=True)
    nl(); nl()   # row 6 blank

    # Row 7: Dear Sir
    wc("C", "Dear Sir,"); nl(); nl()   # row 8 blank

    # Rows 9-10
    wc("C", "    As per to your request, please find hereunder our best offer "
            "for the supply of the"); nl()
    wc("C", "electrical panelboards listed below to serve your project."); nl()
    nl(); nl()   # rows 11-12 blank

    # ── Panel blocks ──────────────────────────────────────────────────────────

    # Itemized mode: write column headers once, before the first panel
    if show_prices:
        wc("C", "Description",  bold=True, align="center", color="FFFFFF",
           fname="Arial", size=10)
        ws.cell(r[0], 3).fill = PatternFill("solid", fgColor="1F3864")
        wc("D", "Brand",        bold=True, align="center", color="FFFFFF",
           fname="Arial", size=10)
        ws.cell(r[0], 4).fill = PatternFill("solid", fgColor="1F3864")
        wc("F", "Qty",          bold=True, align="center", color="FFFFFF",
           fname="Arial", size=10)
        ws.cell(r[0], 6).fill = PatternFill("solid", fgColor="1F3864")
        wc("G", "Unit Price",   bold=True, align="center", color="FFFFFF",
           fname="Arial", size=10)
        ws.cell(r[0], 7).fill = PatternFill("solid", fgColor="1F3864")
        wc("H", "Line Total",   bold=True, align="center", color="FFFFFF",
           fname="Arial", size=10)
        ws.cell(r[0], 8).fill = PatternFill("solid", fgColor="1F3864")
        nl()
        thin()

    for panel in panels:
        name  = panel["panel_name"]
        ht    = panel["ht"]
        items = panel["items"]

        # Panel name + HT total
        ht_cell_addr = f"F{r[0]}"
        wc("C", name, bold=True)
        wc("F", ht, bold=True, align="center", numfmt=NF_PRICE)
        # Itemized mode: print column headers on the panel name row
        if show_prices:
            wc("G", "Unit Price (€)", bold=True, align="center",
               color="444444", size=10)
            wc("H", "Total (€)",      bold=True, align="center",
               color="444444", size=10)
        nl()

        # TTC row — formula =HT_cell*1.11 (exactly as in original)
        wc("E", "TTC:", bold=True, fname="MS Sans Serif",
           align="right", numfmt=NF_TTC_L)
        wc("F", f"={ht_cell_addr}*1.11",
           bold=True, align="center", numfmt=NF_PRICE)
        nl()

        # Thin separator before items
        thin()

        # Item rows: description | brand | qty  (+ prices in itemized mode)
        for item in items:
            wc("C", item["desc"])
            wc("D", item["brand"], align="center")
            wc("F", item["qty"],   align="center")
            if show_prices:
                wc("G", item["unit_price"], align="right",
                   numfmt=NF_PRICE)
                wc("H", item["total"],      align="right",
                   numfmt=NF_PRICE)
            nl()

        # Thin separator after last item (before next panel)
        thin()

    # ── Commercial conditions ─────────────────────────────────────────────────
    nl()
    wc("C", "Commercial Conditions:"); nl(); nl()

    wc("C", "B.1 Conditions of Sale", bold=True); nl()
    wc("C",
       "This quotation is submitted subject to TECHNICAT Group Conditions of Business. "
       "All prices have been offered in conjunction with these terms and conditions, "
       "and any deviation from these terms may result in the amendment of these prices.",
       wrap=True)
    ws.row_dimensions[r[0]].height = 47.25; nl(); nl()

    wc("C", "B.2 Validity", bold=True); nl()
    wc("C",
       "The prices indicated are open for acceptance up till One Week from date of this "
       "proposal. Thereafter this date the tender will be subject to extensions or revisions.",
       wrap=True)
    ws.row_dimensions[r[0]].height = 35.25; nl(); nl()

    wc("C", "B.3 Estimated Delivery", bold=True); nl()
    wc("C", "Based on current commitments:"); nl()
    wc("C", "* To be fixed later upon your priority schedule."); nl()
    wc("C",
       "- Dispatch time is our best estimate at time of quotation, and will be confirmed "
       "upon receipt of order. This confirmed date would form our contractual offer of dispatch.",
       wrap=True)
    ws.row_dimensions[r[0]].height = 31.5; nl()
    wc("C", "* Prices include transportation to the site."); nl(); nl()

    wc("C", "B.4 Warranty", bold=True); nl()
    wc("C", "A total warranty against any fault in the assembly is provided."); nl(); nl()

    wc("C", "B.5 Payment", bold=True); nl()
    wc("C", "* CASH PAYMENT."); nl()

    # ── Signature ─────────────────────────────────────────────────────────────
    nl(6)
    wc("B", "We trust the above shall meet your acceptance and look forward "
            "to hear from you soon."); nl(); nl()
    wc("B", "Sincerely Yours"); nl(); nl(3)
    wc("C", "ELIE BOU SERHAL"); nl()
    wc("C", "Electrical Eng.")

    # ── Pricing Detail  (hidden — for internal use only) ─────────────────────
    wd = wb.create_sheet("Pricing Detail")
    wd.sheet_state = "hidden"

    hdr_fill = PatternFill("solid", fgColor="1F3864")
    hdr_font = Font(name="Arial", bold=True, size=10, color="FFFFFF")
    headers  = ["Panel", "Description", "Brand", "Qty",
                "Unit Price (EUR)", "Total (EUR)", "Match Tier", "Matched Ref"]
    col_ws   = [20, 50, 15, 6, 17, 17, 12, 22]

    for ci, (h, w) in enumerate(zip(headers, col_ws), 1):
        cell            = wd.cell(1, ci, h)
        cell.font       = hdr_font
        cell.fill       = hdr_fill
        cell.alignment  = Alignment(horizontal="center", vertical="center")
        wd.column_dimensions[get_column_letter(ci)].width = w
    wd.row_dimensions[1].height = 22

    TIER_CLR = {"T1": "C6EFCE", "T2": "FFEB9C", "T3": "BDD7EE", "NF": "D9D9D9"}
    dr = 2
    for panel in panels:
        for item in panel["items"]:
            fill_c = PatternFill("solid", fgColor=TIER_CLR.get(item["tier"], "FFFFFF"))
            vals   = [panel["panel_name"], item["desc"], item["brand"], item["qty"],
                      item["unit_price"], item["total"], item["tier"], item["ref"]]
            for ci, v in enumerate(vals, 1):
                cell      = wd.cell(dr, ci, v)
                cell.fill = fill_c
                cell.font = Font(name="Arial", size=10)
                if ci in (5, 6):
                    cell.number_format = "#,##0.00"
                    cell.alignment     = Alignment(horizontal="right")
                elif ci in (3, 4, 7, 8):
                    cell.alignment = Alignment(horizontal="center")
            dr += 1

    # Colour legend
    dr += 1
    for text, color in [("T1 = Keyword match (>=60%)", "C6EFCE"),
                         ("T2 = AI matched",            "FFEB9C"),
                         ("T3 = Size substituted",      "BDD7EE"),
                         ("NF = Not found in DB",       "D9D9D9")]:
        cell      = wd.cell(dr, 1, text)
        cell.fill = PatternFill("solid", fgColor=color)
        cell.font = Font(name="Arial", size=9, italic=True)
        dr += 1

    wb.save(str(out_path))
    print(f"\n  Offer saved -> {out_path}")


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Price SLD components and produce Technelec offer (Elie format)."
    )
    parser.add_argument("--inputs",       nargs="+", required=True,
                        help="One sld_components.xlsx per panel/DB (space-separated)")
    parser.add_argument("--db",           default="pricing.db",
                        help="SQLite pricing database")
    parser.add_argument("--manufacturer", default="SCHNEIDER",
                        help="Manufacturer in the pricing DB edition to match against")
    parser.add_argument("--year",         type=int, default=2026)
    parser.add_argument("--half",         default="H1", choices=["H1", "H2"])
    parser.add_argument("--ref",          default="",
                        help="Project reference shown in offer header")
    parser.add_argument("--tva",          type=float, default=TVA_RATE,
                        help="TVA rate as decimal (default 0.11 = 11%%)")
    parser.add_argument("--pricing-mode",
                        choices=["total", "itemized"],
                        default="total",
                        dest="pricing_mode",
                        help=(
                            "total    → panel lump-sum HT + TTC only (default, keeps "
                            "unit prices private — best for client-facing offers).  "
                            "itemized → adds Qty | Unit Price | Line Total columns per "
                            "component (use when the client needs a full breakdown)."
                        ))
    parser.add_argument("--out",          default="offer.xlsx",
                        help="Output Excel file")
    args = parser.parse_args()

    # ── Connect DB ─────────────────────────────────────────────────────────────
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}"); sys.exit(1)

    conn    = sqlite3.connect(str(db_path))
    aliases = load_aliases(conn)

    edition_row = conn.execute("""
        SELECT pe.id FROM ppl_editions pe
        JOIN manufacturers m ON m.id = pe.manufacturer_id
        WHERE m.name = ? AND pe.year = ? AND pe.half = ?
    """, (args.manufacturer.upper(), args.year, args.half)).fetchone()

    if not edition_row:
        print(f"ERROR: No price list for {args.manufacturer} {args.year} {args.half}.")
        print("       Run import.py first to import the manufacturer PPL.")
        conn.close(); sys.exit(1)

    edition_id = edition_row[0]
    mfr        = args.manufacturer.upper()

    print(f"\nPricing edition : {mfr} {args.year} {args.half}")
    print(f"Input files     : {len(args.inputs)}")
    print(f"Output          : {args.out}\n")

    # ── Process each panel ─────────────────────────────────────────────────────
    all_panels = []

    for xlsx_str in args.inputs:
        xlsx_path = Path(xlsx_str)
        if not xlsx_path.exists():
            print(f"WARNING: File not found — {xlsx_path}, skipping"); continue

        print(f"{'─'*60}")
        print(f"Panel file : {xlsx_path.name}")

        sld   = read_sld_file(xlsx_path)
        pname = sld["panel_name"]
        comps = sld["components"]
        terms = sld["terminals"]

        print(f"Panel name : {pname}")
        print(f"Components : {len(comps)}  |  Terminal types: {len(terms)}")
        print()

        items    = []
        ht_total = 0.0

        # Match main components
        for comp in comps:
            desc  = build_description(comp)
            brand = infer_brand(comp, mfr)
            qty   = comp["qty"]

            result = match_component(conn, aliases, edition_id, desc, mfr)
            up     = result["price"]
            total  = round(up * qty, 2)
            ht_total += total

            tag = {"T1": "OK ", "T2": "AI ", "T3": "SUB", "NF": "---"}.get(
                result["tier"], "---")
            print(f"  [{tag}] {desc[:44]:44s}  x{qty:3d}  "
                  f"@ {up:8.2f}  = {total:10.2f}  {result['note']}")

            items.append({
                "desc":       desc,
                "brand":      brand,
                "qty":        qty,
                "unit_price": up,
                "total":      total,
                "tier":       result["tier"],
                "ref":        result["ref"],
            })

        # Match terminals
        for term in terms:
            desc   = term["description"]
            qty    = term["qty"]
            brand  = "PHOENIX"

            result = match_component(conn, aliases, edition_id, desc, mfr)
            up     = result["price"]
            total  = round(up * qty, 2)
            ht_total += total

            tag = {"T1": "OK ", "T2": "AI ", "T3": "SUB", "NF": "---"}.get(
                result["tier"], "---")
            print(f"  [{tag}] {desc[:44]:44s}  x{qty:3d}  "
                  f"@ {up:8.2f}  = {total:10.2f}  {result['note']}")

            items.append({
                "desc":       desc,
                "brand":      brand,
                "qty":        qty,
                "unit_price": up,
                "total":      total,
                "tier":       result["tier"],
                "ref":        result["ref"],
            })

        ttc = round(ht_total * (1 + args.tva), 2)
        print(f"\n  >> {pname}  HT = {ht_total:,.2f}  |  TTC = {ttc:,.2f}\n")

        all_panels.append({
            "panel_name": pname,
            "ht":         ht_total,
            "ttc":        ttc,
            "items":      items,
        })

    conn.close()

    if not all_panels:
        print("ERROR: No panels were processed."); sys.exit(1)

    # ── Write offer ─────────────────────────────────────────────────────────────
    print(f"{'─'*60}")
    print("Writing Excel offer ...")
    write_offer(all_panels, Path(args.out), args.ref,
                pricing_mode=args.pricing_mode)

    # Summary
    grand_ht  = sum(p["ht"]  for p in all_panels)
    grand_ttc = sum(p["ttc"] for p in all_panels)
    print(f"\n{'='*60}")
    print(f"  Panels processed : {len(all_panels)}")
    print(f"  Grand HT         : {grand_ht:,.2f}")
    print(f"  Grand TTC        : {grand_ttc:,.2f}  (TVA {args.tva*100:.0f}%)")
    print(f"{'='*60}")
    print(f"\n  Offer sheet     : {'itemized (unit prices + line totals visible)' if args.pricing_mode == 'itemized' else 'total only (panel HT + TTC — no item prices)'}")
    print(f"  Pricing Detail  : hidden sheet — unit prices, match tier, refs")
    print(f"  Colours (Pricing Detail): GREEN=keyword  YELLOW=AI  BLUE=substituted  GREY=not found\n")


if __name__ == "__main__":
    main()