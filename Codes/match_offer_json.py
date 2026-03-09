#!/usr/bin/env python3
"""
match_offer.py  –  Price SLD component extractions → Technelec offer JSON.

Each --input is ONE .sld.json (= one DB/panel).
Pass multiple panels on the same command line; they appear as separate blocks.

Usage:
    python match_offer.py \\
        --inputs DB-ATTIC.sld.json DB-B1.sld.json ATS-1.sld.json \\
        --db pricing.db \\
        --manufacturer SCHNEIDER \\
        --year 2026 --half H1 \\
        --ref "KORNET EL HAMRA" \\
        --pricing-mode total \\       ← "total" (default) or "itemized"
        --out offer.json

  --pricing-mode total     Client sees only the panel lump-sum HT + TTC.
                           No unit prices visible. Best for privacy.
  --pricing-mode itemized  Client sees Qty | Unit Price | Line Total per item.
                           Use when the client needs a full breakdown.

Output: offer.json  —  structured pricing result readable by export.py / UI.
  {ref, manufacturer, year, half, pricing_mode, generated_at,
   grand_ht, grand_ttc, tva_rate,
   panels: [{panel_name, ht, ttc,
             items: [{desc, brand, qty, unit_price, total, tier, ref, ppl_desc}]}]}

Tier legend: T1=keyword match  T2=AI matched  T3=size substituted  NF=not found
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

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
#  SLD FILE READER  —  parses one .sld.json produced by extract_spatial.py
#
#  Each .sld.json = one panel.  Pass multiple files on the CLI for multi-panel
#  offers (same as before).  Returns list[dict] for API consistency.
#
#  .sld.json schema:
#    {
#      "panel_ref":    str,
#      "source_file":  str,
#      "extracted_at": str,
#      "components":   [ {category, label, poles, amps, curve,
#                          breaking_capacity, sensitivity, spd_type,
#                          cable_size, load_label, qty, has_terminal,
#                          is_incomer, confidence, data_source, ai_notes} ],
#      "terminals":    [ {name, description, max_mm2, qty, circuits} ]
#    }
# ─────────────────────────────────────────────────────────────────────────────

def read_sld_file(json_path: Path) -> list[dict]:
    """
    Parse one .sld.json produced by extract_spatial.py.

    Returns a list with a single panel dict — keeping the same list[dict]
    interface as the multi-panel Excel reader so run_match() is unchanged.

    Each entry:
        {
            "panel_name": str,
            "components": [{category, label, poles, amps, curve,
                            breaking_capacity, sensitivity, spd_type,
                            qty, has_terminal, is_incomer}],
            "terminals":  [{"description": str, "qty": int}]
        }
    """
    raw = json.loads(json_path.read_text(encoding="utf-8"))

    panel_name = raw.get("panel_ref") or json_path.stem

    # Components — pass through the fields match_offer needs; drop UI-only fields
    components = []
    for c in raw.get("components", []):
        components.append({
            "category":          str(c.get("category",          "OTHER")).strip().upper(),
            "label":             str(c.get("label",             "-")).strip(),
            "poles":             str(c.get("poles",             "-")).strip(),
            "amps":              str(c.get("amps",              "-")).strip(),
            "curve":             str(c.get("curve",             "-")).strip(),
            "breaking_capacity": str(c.get("breaking_capacity", "-")).strip(),
            "sensitivity":       str(c.get("sensitivity",       "-")).strip(),
            "spd_type":          str(c.get("spd_type",          "-")).strip(),
            "qty":               int(c.get("qty",  1)),
            "has_terminal":      bool(c.get("has_terminal", False)),
            "is_incomer":        bool(c.get("is_incomer",   False)),
        })

    # Terminals — matcher expects {description, qty}
    # .sld.json stores them as {name, description, max_mm2, qty, circuits}
    # "description" is the full Phoenix Contact product string — use that for matching
    terminals = []
    for t in raw.get("terminals", []):
        terminals.append({
            "description": str(t.get("description") or t.get("name") or "-").strip(),
            "qty":         int(t.get("qty", 0)),
        })

    return [{"panel_name": panel_name,
             "components": components,
             "terminals":  terminals}]




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
        return "DEHN"
    return default_mfr


# ─────────────────────────────────────────────────────────────────────────────
#  JSON OFFER WRITER
# ─────────────────────────────────────────────────────────────────────────────

def write_offer(panels: list, out_path: Path, ref: str,
                pricing_mode: str = "total",
                manufacturer: str = "",
                year: int = 0, half: str = "",
                tva: float = TVA_RATE):
    """
    Write the full pricing result as a structured JSON file.

    Schema:
    {
      "ref":          str,       project reference
      "manufacturer": str,
      "year":         int,
      "half":         str,
      "pricing_mode": str,       "total" | "itemized"
      "generated_at": str,       ISO-8601 timestamp
      "grand_ht":     float,
      "grand_ttc":    float,
      "tva_rate":     float,
      "panels": [
        {
          "panel_name": str,
          "ht":         float,
          "ttc":        float,
          "items": [
            {
              "desc":       str,
              "brand":      str,
              "qty":        int,
              "unit_price": float,
              "total":      float,
              "tier":       str,    "T1"|"T2"|"T3"|"NF"
              "ref":        str,
              "ppl_desc":   str
            }
          ]
        }
      ]
    }
    """
    grand_ht  = sum(p["ht"]  for p in panels)
    grand_ttc = sum(p["ttc"] for p in panels)

    payload = {
        "ref":          ref,
        "manufacturer": manufacturer,
        "year":         year,
        "half":         half,
        "pricing_mode": pricing_mode,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "grand_ht":     round(grand_ht,  2),
        "grand_ttc":    round(grand_ttc, 2),
        "tva_rate":     tva,
        "panels":       panels,
    }

    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n  Offer JSON saved -> {out_path}")




def run_match(
    inputs:       list,
    db:           str   = "pricing.db",
    manufacturer: str   = "SCHNEIDER",
    year:         int   = 2026,
    half:         str   = "H1",
    ref:          str   = "",
    tva:          float = TVA_RATE,
    pricing_mode: str   = "total",
    out:          str   = "offer.json",
    progress_cb         = None,
) -> dict:
    """
    Run the full match pipeline and write the offer JSON.

    Returns:
        {
            "panels":    list of panel dicts (each has panel_name, ht, ttc, items),
            "grand_ht":  float,
            "grand_ttc": float,
            "json_path": str,
            "errors":    list of str,
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
                "json_path": "", "errors": errors}

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
                "json_path": "", "errors": errors}

    edition_id = edition_row[0]
    mfr        = manufacturer.upper()

    log(f"\nPricing edition : {mfr} {year} {half}")
    log(f"Input files     : {len(inputs)}")
    log(f"Output          : {out}\n")

    all_panels = []

    for json_str in inputs:
        json_path = Path(json_str)
        if not json_path.exists():
            msg = f"WARNING: File not found — {json_path}, skipping"
            log(msg); errors.append(msg); continue

        log(f"{'─'*60}")
        log(f"Panel file : {json_path.name}")

        panels_in_file = read_sld_file(json_path)
        log(f"Panels found : {len(panels_in_file)}")

        for sld in panels_in_file:
            pname = sld["panel_name"]
            comps = sld["components"]
            terms = sld["terminals"]

            log(f"\n  ── {pname} ──")
            log(f"  Components : {len(comps)}  |  Terminal types: {len(terms)}")
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
                "json_path": "", "errors": errors}

    log(f"{'─'*60}")
    log("Writing offer JSON ...")
    write_offer(all_panels, out_path, ref,
                pricing_mode=pricing_mode,
                manufacturer=manufacturer,
                year=year, half=half, tva=tva)

    grand_ht  = sum(p["ht"]  for p in all_panels)
    grand_ttc = sum(p["ttc"] for p in all_panels)

    log(f"\n{'='*60}")
    log(f"  Panels processed : {len(all_panels)}")
    log(f"  Grand HT         : {grand_ht:,.2f}")
    log(f"  Grand TTC        : {grand_ttc:,.2f}  (TVA {tva*100:.0f}%)")
    log(f"{'='*60}")
    log(f"  Tier legend: T1=keyword  T2=AI  T3=substituted  NF=not found\n")

    return {"panels": all_panels, "grand_ht": grand_ht, "grand_ttc": grand_ttc,
            "json_path": str(out_path), "errors": errors}


def main():
    parser = argparse.ArgumentParser(
        description="Price SLD components → offer JSON.")
    parser.add_argument("--inputs",       nargs="+", required=True,
                        help=".sld.json files (one per panel, or multiple)")
    parser.add_argument("--db",           default="pricing.db")
    parser.add_argument("--manufacturer", default="SCHNEIDER")
    parser.add_argument("--year",         type=int, default=2026)
    parser.add_argument("--half",         default="H1", choices=["H1","H2"])
    parser.add_argument("--ref",          default="")
    parser.add_argument("--tva",          type=float, default=TVA_RATE)
    parser.add_argument("--pricing-mode", choices=["total","itemized"],
                        default="total", dest="pricing_mode")
    parser.add_argument("--out",          default="offer.json")
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