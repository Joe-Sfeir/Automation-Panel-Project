"""
SLD Component Extractor  v4.2
==============================
Pipeline:
  1. DWG → DXF  (ODA File Converter)
  2. DXF → structured text + spatial clusters
  3. DXF → PNG render (150 DPI default)
  4. Gemini Call A — Components  (text + image)
  5. Gemini Call B — Enclosure + Additional Info  (text + image)
  6. Post-processing: group identical rows, sum qty
  7. Terminal calculation  (Phoenix Contact PT, 2 per outgoing circuit)
  8. Excel output  (Components | Terminals | Enclosure | Shop Drawing | Summary)

Usage:
    python sld_extractor_v4.py drawing.dwg --api-key YOUR_KEY [--output out.xlsx]

Install:
    pip install "ezdxf[draw]" google-genai openpyxl matplotlib Pillow
"""

import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

# Force line-buffered output (fixes silent hanging in PowerShell / cmd)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

import ezdxf
from ezdxf import recover
from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from google import genai
from google.genai import types

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from pathlib import Path

def get_api_key() -> str:
    env_file = Path(__file__).parent / "key.env"
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("GEMINI_API_KEY"):
            return line.split("=", 1)[1].strip()
    raise ValueError("GEMINI_API_KEY not found in key.env")
api_key = get_api_key()
# ─────────────────────────────────────────────────────────────────────────────
#  TERMINAL MAP  — Phoenix Contact PT series
#  Naming: "Terminal 2x{size}mm²" for ≤6mm², "Terminal {size}mm²" for ≥10mm²
# ─────────────────────────────────────────────────────────────────────────────
TERMINAL_MAP = [
    (1.5,  "Terminal 2x1.5mm²", "Phoenix Contact PT 1.5 feed-through", 17.5),
    (2.5,  "Terminal 2x2.5mm²", "Phoenix Contact PT 2.5 feed-through", 24.0),
    (4.0,  "Terminal 2x4mm²",   "Phoenix Contact PT 4 feed-through",   32.0),
    (6.0,  "Terminal 2x6mm²",   "Phoenix Contact PT 6 feed-through",   41.0),
    (10.0, "Terminal 10mm²",    "Phoenix Contact PT 10 feed-through",  57.0),
    (16.0, "Terminal 16mm²",    "Phoenix Contact PT 16 feed-through",  76.0),
    (35.0, "Terminal 35mm²",    "Phoenix Contact PT 35 feed-through", 125.0),
    (70.0, "Terminal 70mm²",    "Phoenix Contact PT 70 feed-through", 160.0),
]

def terminal_for_cable(mm2: float) -> dict:
    for max_mm2, name, desc, amps in TERMINAL_MAP:
        if mm2 <= max_mm2:
            return {"name": name, "description": desc, "max_mm2": max_mm2}
    return {"name": "Terminal 70mm²+", "description": "Cable >70mm² — check Phoenix catalog", "max_mm2": 70}

def parse_mm2(val) -> float:
    if val is None: return 0.0
    if isinstance(val, (int, float)): return float(val)
    hits = re.findall(r"(\d+(?:\.\d+)?)", str(val))
    return max((float(h) for h in hits), default=0.0)


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 1  DWG → DXF
# ─────────────────────────────────────────────────────────────────────────────
def find_oda() -> str | None:
    import glob
    if platform.system() == "Windows":
        for pat in [r"C:\Program Files\ODA\ODAFileConverter*\ODAFileConverter.exe",
                    r"C:\Program Files\ODA\ODAFileConverter 26.12.0\ODAFileConverter.exe",
                    r"C:\Program Files (x86)\ODA\ODAFileConverter*\ODAFileConverter.exe"]:
            hits = glob.glob(pat)
            if hits: return hits[0]
    elif platform.system() == "Darwin":
        hits = glob.glob("/Applications/ODAFileConverter*.app/Contents/MacOS/ODAFileConverter")
        if hits: return hits[0]
    else:
        for p in ["/usr/bin/ODAFileConverter", "/usr/local/bin/ODAFileConverter"]:
            if Path(p).exists(): return p
    return None

def dwg_to_dxf(dwg_path: Path, oda_path: str | None, tmp_dir: Path) -> Path:
    if not oda_path: oda_path = find_oda()
    if not oda_path or not Path(oda_path).exists():
        print("⚠  ODA not found — trying ezdxf direct DWG read …")
        return dwg_path

    in_dir  = tmp_dir / "in";  in_dir.mkdir()
    out_dir = tmp_dir / "out"; out_dir.mkdir()
    shutil.copy2(dwg_path, in_dir / dwg_path.name)

    print(f"🔄  DWG → DXF  ({Path(oda_path).name})")
    try:
        subprocess.run([str(oda_path), str(in_dir), str(out_dir),
                        "ACAD2018", "DXF", "0", "1"],
                       capture_output=True, text=True, timeout=120)
    except PermissionError:
        print("❌  PermissionError — run cmd as Administrator"); sys.exit(1)

    hits = list(out_dir.glob("*.dxf"))
    if not hits: print("❌  ODA produced no DXF"); sys.exit(1)
    print(f"   ✓ {hits[0].name}  ({hits[0].stat().st_size // 1024} KB)")
    return hits[0]


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2  DXF → Text payload
# ─────────────────────────────────────────────────────────────────────────────
def extract_dxf(dxf_path: Path) -> str:
    try:
        doc, auditor = recover.readfile(str(dxf_path))
    except Exception as e:
        print(f"❌  Cannot open DXF: {e}"); sys.exit(1)
    if auditor.has_errors:
        print(f"  ⚠  DXF audit: {len(auditor.errors)} error(s)")

    msp = doc.modelspace()
    blocks, texts = [], []

    for ent in msp.query("INSERT"):
        try:
            pos     = ent.dxf.insert
            attribs = {a.dxf.tag.strip(): a.dxf.text.strip()
                       for a in ent.attribs if a.dxf.tag.strip() and a.dxf.text.strip()}
            blocks.append({"block": ent.dxf.name,
                            "x": round(float(pos[0]),1), "y": round(float(pos[1]),1),
                            "layer": ent.dxf.layer, "attribs": attribs})
        except Exception: pass

    for ent in msp:
        if ent.dxftype() not in ("TEXT","MTEXT"): continue
        try:
            txt = (ent.dxf.text if ent.dxftype()=="TEXT" else ent.plain_mtext()).strip()
            if not txt: continue
            pos = getattr(ent.dxf, "insert", (0,0,0))
            texts.append({"content": txt,
                           "x": round(float(pos[0]),1), "y": round(float(pos[1]),1),
                           "layer": ent.dxf.layer})
        except Exception: pass

    clusters = _cluster(texts, 80.0)
    lines = [f"=== DXF: {dxf_path.name} ===",
             f"Layers: {', '.join(sorted({b['layer'] for b in blocks}|{t['layer'] for t in texts}))}",
             "", f"--- BLOCK INSERTS ({len(blocks)}) ---"]
    for b in blocks:
        att = ", ".join(f"{k}={v}" for k,v in b["attribs"].items()) or "no attribs"
        lines.append(f"  BLOCK '{b['block']}' [{b['layer']}] @({b['x']},{b['y']}) | {att}")
    lines += ["", f"--- TEXT CLUSTERS ({len(clusters)}) ---"]
    for i,cl in enumerate(clusters,1):
        lines.append(f"  Cluster {i:03d} @({cl['cx']},{cl['cy']}): {'  |  '.join(cl['texts'])}")
    lines += ["", f"--- ALL TEXT ({len(texts)}) ---"]
    for t in texts:
        lines.append(f"  [{t['layer']}] ({t['x']},{t['y']}): {t['content']}")
    return "\n".join(lines)

def _cluster(texts, radius):
    visited = [False]*len(texts); clusters = []
    for i,t in enumerate(texts):
        if visited[i]: continue
        grp=[t]; visited[i]=True
        for j,t2 in enumerate(texts):
            if visited[j]: continue
            if ((t["x"]-t2["x"])**2+(t["y"]-t2["y"])**2)**0.5 <= radius:
                grp.append(t2); visited[j]=True
        clusters.append({"texts":[g["content"] for g in grp],
                          "cx":round(sum(g["x"] for g in grp)/len(grp),1),
                          "cy":round(sum(g["y"] for g in grp)/len(grp),1)})
    return clusters


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 3  DXF → PNG
# ─────────────────────────────────────────────────────────────────────────────
def render_png(dxf_path: Path, out_png: Path, dpi: int = 150) -> Path | None:
    print(f"🖼  Rendering DXF → PNG at {dpi} DPI …")
    try:
        doc = ezdxf.readfile(str(dxf_path))
        fig = plt.figure(figsize=(24,18), dpi=dpi)
        ax  = fig.add_axes([0,0,1,1]); ax.set_aspect("equal")
        ctx = RenderContext(doc); backend = MatplotlibBackend(ax)
        Frontend(ctx, backend).draw_layout(doc.modelspace(), finalize=True)
        fig.savefig(str(out_png), dpi=dpi, bbox_inches="tight",
                    facecolor="white", format="png")
        plt.close(fig)
        print(f"   ✓ PNG  ({out_png.stat().st_size // 1024} KB)")
        return out_png
    except Exception as e:
        print(f"  ⚠  PNG render failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  PROMPTS  — Two separate calls for reliability
# ─────────────────────────────────────────────────────────────────────────────
PROMPT_COMPONENTS = """
You are a highly experienced electrical design engineer reviewing a Single Line Diagram (SLD).
You have TWO data sources:
  A) DXF structured text — exact block names, attributes, all text with coordinates.
  B) High-resolution image of the same drawing.

Cross-reference both. If they agree → high confidence. If they conflict → flag it.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 1 — CONTEXT SCAN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Identify before extracting:
  • 1-phase or 3-phase system?
  • Global notes (e.g. "B.C.: 6kA/400V") → apply to ALL breakers unless device overrides
  • Panel name and incomer rating

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 2 — EXTRACT ALL COMPONENTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CATEGORIES:
  MCB          → Miniature Circuit Breaker (≤125A, DIN rail, no frame suffix)
  MCCB         → /100AF /160AF /250AF /400AF /630AF OR labeled MCCB OR >125A = ALWAYS MCCB
  RCCB         → Residual Current CB (no overcurrent)
  RCBO         → Residual Current Breaker with Overcurrent
  SWITCH       → Isolating switch / disconnector / ISOL.S
  CONTACTOR    → Contactor
  SPD          → Surge Protection Device
  FUSE_HOLDER  → Fuse holder/base with fuses. NEVER call this MCB.
  PILOT_LAMP   → ALL phase indication lamps as ONE row. qty=3 (3-phase), poles="R/Y/B".
  METER        → Digital power meter, energy meter, CT, voltmeter, ammeter. NEVER skip.
                 CT RULE: Extract rating as "Primary/Secondary" string in amps field
                 (e.g. "40/5", "100/5", "400/5"). Secondary is always 5A standard.
                 3-phase system → always qty=3 CTs, poles="3P".
                 Label the CT row: label="CT {ratio}" e.g. label="CT 40/5".
  OTHER        → ATS, VFD, busbar, transformer, etc.

GROUPING — CRITICAL:
  Components with SAME category + label + poles + amps + curve + breaking_capacity
  MUST be ONE row with summed qty.
  Five 1P+N 10A MCB IC60H 6kA → ONE row, qty=5.
  Different amps or poles = separate rows.
  NEVER group MCB with MCCB.
  load_label = comma-separated list of all circuit names for grouped rows.

FIELDS:
  label               → Model name (e.g. "IC60H", "DPX250", "CT 40/5"). "-" if unknown.
  poles               → 1P | 1P+N | 2P | 3P | 4P | R/Y/B (PILOT_LAMP) | 3P (CT)
  amps                → Rated current. MCCB: "63A / 160AF". CT: "40/5". "-" if not shown.
  curve               → B/C/D. Default "C" for MCB/MCCB/RCBO. "-" for others.
  breaking_capacity   → kA from global note or device label. "-" for non-breaking.
  sensitivity         → mA for RCCB/RCBO only. "-" for others.
  spd_type            → SPD only: type, Up, Imax. "-" for others.
  cable_size          → Phase conductor mm² as NUMBER. "4x2.5mm²"→2.5. 0 if not shown.
  load_label          → Circuit name(s). Comma-separate if grouped.
  qty                 → Total count after grouping.
  has_terminal        → true ONLY for outgoing circuits feeding real external loads
                        (sockets, lighting circuits, sub-panels, motors, HVAC).
                        false for: SPD fuses, pilot lamp fuses, meter connections,
                        group RCCBs protecting other breakers, panel-internal items.
                        Rule: has cable_size > 0 AND feeds an external load → true.
  confidence          → 1.0 both agree | 0.85 minor inference | 0.75 one source | 0.5 conflict
  data_source         → "BOTH" | "DXF_ONLY" | "IMAGE_ONLY"
  ai_notes            → Explain inferences/conflicts. "-" if fully confident.

Return ONLY a valid JSON array. No markdown. No fences. Starts [ ends ].

[
  {
    "category": "MCB", "label": "IC60H", "poles": "1P+N", "amps": "10A",
    "curve": "C", "breaking_capacity": "6kA", "sensitivity": "-",
    "spd_type": "-", "cable_size": 2.5, "load_label": "L1, L2, L3",
    "qty": 3, "has_terminal": true, "confidence": 0.9,
    "data_source": "BOTH", "ai_notes": "B.C. from global note."
  }
]
"""

PROMPT_EXTRA = """
You are reviewing a Single Line Diagram (SLD). You have the DXF text data and the drawing image.

Extract TWO things only:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Look in the title block, drawing header, and notes for the panel enclosure details:
  ref          → Panel reference (e.g. "DB-B1", "MDB", "LP-01")
  size         → Module count or physical dimensions (e.g. "6x24 Modules", "1000x800x250mm")
  manufacturer → Brand if mentioned (e.g. "LEGRAND", "SCHNEIDER", "ABB", "HAGER")
  catalog      → Part/catalog number if shown (e.g. "337206", "LVS08167")

Use "-" for any field not found.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
2. ADDITIONAL INFORMATION DETECTED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scan for anything beyond the standard component list that could be useful:
  • Shop drawing / panel layout (physical arrangement view of the panel)
  • Load schedule or demand factor table
  • Cable schedule
  • Project/client title block information
  • Wiring notes or installation notes
  • Any specifications not captured elsewhere

For each item found:
  type        → "SHOP_DRAWING" | "LOAD_SCHEDULE" | "CABLE_SCHEDULE" | "TITLE_BLOCK" | "NOTE" | "OTHER"
  description → Clear description of what was found
  value       → The actual content if extractable, otherwise "-"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Return ONLY this exact JSON. No markdown. No fences.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{
  "enclosure": {
    "ref": "DB-B1",
    "size": "6x24 Modules",
    "manufacturer": "LEGRAND",
    "catalog": "337206"
  },
  "additional_info": [
    {
      "type": "SHOP_DRAWING",
      "description": "Panel layout shop drawing showing physical component arrangement",
      "value": "-"
    }
  ]
}
"""


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 4  GEMINI  — Two focused calls
# ─────────────────────────────────────────────────────────────────────────────
def _make_contents(prompt: str, text: str, png_path: Path | None) -> list:
    contents = [prompt, text]
    if png_path and png_path.exists():
        contents.append(types.Part.from_bytes(
            data=png_path.read_bytes(), mime_type="image/png"))
    return contents

def _gemini_call(client, contents: list, label: str) -> str:
    img_info = f" + image" if len(contents) > 2 else ""
    print(f"  → {label}{img_info} … (30–90 sec, please wait)")
    response = client.models.generate_content(
        model="gemini-3-pro-preview",
        contents=contents,
        config=types.GenerateContentConfig(
            http_options=types.HttpOptions(timeout=180_000),
        ),
    )
    raw = response.text.strip()
    # Strip markdown fences if present
    if "```" in raw:
        parts = raw.split("```")
        for p in parts:
            cleaned = p.strip()
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
            if cleaned.startswith("[") or cleaned.startswith("{"):
                return cleaned
    return raw

def call_gemini_components(text: str, png_path: Path | None, api_key: str, debug_dir: Path) -> list[dict]:
    client   = genai.Client(api_key=api_key)
    contents = _make_contents(PROMPT_COMPONENTS, text, png_path)
    raw      = _gemini_call(client, contents, "Call 1/2: Components")

    # Save debug dump
    (debug_dir / "gemini_components_raw.txt").write_text(raw, encoding="utf-8")

    try:
        result = json.loads(raw)
        if isinstance(result, list):
            print(f"  ✓ {len(result)} component group(s)")
            return result
        if isinstance(result, dict) and "components" in result:
            comps = result["components"]
            print(f"  ✓ {len(comps)} component group(s)")
            return comps
        print("  ⚠  Unexpected structure — check gemini_components_raw.txt")
        return []
    except json.JSONDecodeError as e:
        print(f"  ⚠  JSON error: {e} — check gemini_components_raw.txt")
        return []

def call_gemini_extra(text: str, png_path: Path | None, api_key: str, debug_dir: Path) -> dict:
    client   = genai.Client(api_key=api_key)
    contents = _make_contents(PROMPT_EXTRA, text, png_path)
    raw      = _gemini_call(client, contents, "Call 2/2: Enclosure + Info")

    (debug_dir / "gemini_extra_raw.txt").write_text(raw, encoding="utf-8")

    try:
        result = json.loads(raw)
        if isinstance(result, dict):
            enc = result.get("enclosure", {})
            info = result.get("additional_info", [])
            print(f"  ✓ Enclosure: {enc.get('ref','-')} | {len(info)} extra item(s)")
            return result
        print("  ⚠  Unexpected extra structure — check gemini_extra_raw.txt")
        return {"enclosure": {}, "additional_info": []}
    except json.JSONDecodeError as e:
        print(f"  ⚠  Extra JSON error: {e}")
        return {"enclosure": {}, "additional_info": []}


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 5  GROUPING
# ─────────────────────────────────────────────────────────────────────────────
def group_components(components: list[dict]) -> list[dict]:
    merged = {}
    for comp in components:
        cat = comp.get("category","OTHER").upper()
        if cat == "PILOT_LAMP":
            key = ("PILOT_LAMP",)
        else:
            key = (cat,
                   str(comp.get("label","-")).strip(),
                   str(comp.get("poles","-")).strip(),
                   str(comp.get("amps","-")).strip(),
                   str(comp.get("curve","-")).strip(),
                   str(comp.get("breaking_capacity","-")).strip())

        if key not in merged:
            merged[key] = dict(comp)
            merged[key]["category"] = cat
            merged[key]["qty"]      = int(comp.get("qty", 1))
            ll = comp.get("load_label","-")
            merged[key]["_labels"]  = [ll] if ll and ll != "-" else []
        else:
            ex = merged[key]
            ex["qty"] += int(comp.get("qty", 1))
            ex["confidence"] = min(float(ex.get("confidence",1.0)),
                                   float(comp.get("confidence",1.0)))
            ll = comp.get("load_label","-")
            if ll and ll != "-":
                ex["_labels"].append(ll)
            ex["cable_size"] = max(parse_mm2(ex.get("cable_size",0)),
                                   parse_mm2(comp.get("cable_size",0)))
            # If any row has has_terminal=True, keep it
            if comp.get("has_terminal", False):
                ex["has_terminal"] = True

    result = []
    for comp in merged.values():
        labels = comp.pop("_labels", [])
        comp["load_label"] = ", ".join(labels) if labels else "-"
        if comp.get("category") == "PILOT_LAMP":
            comp.update({"poles":"R/Y/B","amps":"-","curve":"-",
                          "breaking_capacity":"-","qty":3,"has_terminal":False})
        result.append(comp)
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 6  TERMINALS
# ─────────────────────────────────────────────────────────────────────────────
def calculate_terminals(components: list[dict]) -> list[dict]:
    """
    Only outgoing circuits (has_terminal=True) with cable_size > 0.
    Always 2 terminals per circuit (phase + neutral).
    Grouped by terminal size type.
    """
    counts = defaultdict(lambda: {"qty":0, "circuits":[], "description":"", "max_mm2":0})
    for comp in components:
        if not comp.get("has_terminal", False):
            continue
        mm2 = parse_mm2(comp.get("cable_size", 0))
        if mm2 <= 0:
            continue
        qty  = int(comp.get("qty", 1))
        term = terminal_for_cable(mm2)
        name = term["name"]
        counts[name]["qty"]        += 2 * qty
        counts[name]["description"] = term["description"]
        counts[name]["max_mm2"]     = term["max_mm2"]
        ll  = comp.get("load_label") or comp.get("label") or "?"
        amp = comp.get("amps","")
        counts[name]["circuits"].append(f"{ll} {amp} @{mm2}mm²")

    return [{"name": n, "description": d["description"], "max_mm2": d["max_mm2"],
             "qty": d["qty"], "circuits": " | ".join(d["circuits"])}
            for n, d in sorted(counts.items())]


# ─────────────────────────────────────────────────────────────────────────────
#  EXCEL HELPERS
# ─────────────────────────────────────────────────────────────────────────────
T = Side(style="thin",  color="BBBBBB")
K = Side(style="medium",color="1F3864")
TB = Border(left=T,right=T,top=T,bottom=T)
KB = Border(left=K,right=K,top=K,bottom=K)
HF = PatternFill("solid",fgColor="1F3864")
AF = PatternFill("solid",fgColor="EBF0FA")
WF = PatternFill("solid",fgColor="FFF2CC")
RF = PatternFill("solid",fgColor="FCE4D6")
GF = PatternFill("solid",fgColor="E2EFDA")
BF = PatternFill("solid",fgColor="DDEEFF")
IF = PatternFill("solid",fgColor="FFF2CC")
ca = Alignment(horizontal="center",vertical="center")
la = Alignment(horizontal="left",  vertical="center",wrap_text=True)

CC = {"MCB":"2E75B6","MCCB":"C00000","RCCB":"70AD47","RCBO":"375623",
      "SWITCH":"7030A0","CONTACTOR":"FF6600","SPD":"FF0000",
      "FUSE_HOLDER":"ED7D31","PILOT_LAMP":"BF8F00",
      "METER":"00B0F0","OTHER":"808080"}

def cf(conf):
    v = float(conf)
    if v < 0.5: return RF
    if v < 0.75: return WF
    return None

def title(ws,text,nc,bg="1F3864"):
    lc=get_column_letter(nc); ws.merge_cells(f"A1:{lc}1")
    c=ws["A1"]; c.value=text
    c.font=Font(name="Arial",bold=True,size=13,color="FFFFFF")
    c.fill=PatternFill("solid",fgColor=bg)
    c.alignment=Alignment(horizontal="center",vertical="center")
    ws.row_dimensions[1].height=30

def sub(ws,text,nc,row=2):
    lc=get_column_letter(nc); ws.merge_cells(f"A{row}:{lc}{row}")
    c=ws[f"A{row}"]; c.value=text
    c.font=Font(name="Arial",italic=True,size=10,color="444444")
    c.fill=PatternFill("solid",fgColor="D9E1F2")
    c.alignment=Alignment(horizontal="center",vertical="center")
    ws.row_dimensions[row].height=18

def hdrs(ws,hs,ws_,row=3):
    for ci,(h,w) in enumerate(zip(hs,ws_),1):
        c=ws.cell(row=row,column=ci,value=h)
        c.font=Font(name="Arial",bold=True,size=10,color="FFFFFF")
        c.fill=HF; c.alignment=Alignment(horizontal="center",vertical="center",wrap_text=True)
        c.border=KB
        ws.column_dimensions[get_column_letter(ci)].width=w
    ws.row_dimensions[row].height=24

DF = Font(name="Arial",size=10)
BLD= Font(name="Arial",bold=True,size=10)


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 7  EXCEL
# ─────────────────────────────────────────────────────────────────────────────
CH = ["#","Category","Label","Poles","Amps","Curve","B.C.","Sensitivity",
      "SPD Type","Cable mm²","Load / Circuit","Qty","Terminal?","Conf.","Source","AI Notes"]
CW = [4,13,14,9,13,7,8,12,22,10,28,5,10,8,10,48]

TH = ["#","Terminal Type","Description","Max mm²","Qty","Circuits"]
TW = [4,18,34,10,8,70]

EH = ["Field","Value"]
EW = [26,52]

IH = ["#","Type","Description","Value"]
IW = [4,18,46,46]


def write_excel(components, terminals, enclosure, add_info, png_path, out_path, src):
    wb = Workbook()

    # ── Sheet 1: Components ───────────────────────────────────────────────────
    ws = wb.active; ws.title = "Components"
    total = sum(int(c.get("qty",1)) for c in components)
    title(ws, f"SLD Component Extraction  ·  {src}", len(CH))
    sub(ws, f"Groups: {len(components)}   |   Total units: {total}", len(CH))
    hdrs(ws, CH, CW)
    for ri,comp in enumerate(components,1):
        er   = ri+3
        conf = float(comp.get("confidence",1.0))
        fill = cf(conf) or (AF if ri%2==0 else None)
        cat  = comp.get("category","OTHER").upper()
        mm2  = parse_mm2(comp.get("cable_size",0))
        has_t = comp.get("has_terminal",False)
        vals = [ri, cat,
                comp.get("label","-"), comp.get("poles","-"),
                comp.get("amps","-"),  comp.get("curve","-"),
                comp.get("breaking_capacity","-"), comp.get("sensitivity","-"),
                comp.get("spd_type","-"),
                str(mm2) if mm2>0 else "-",
                comp.get("load_label","-"),
                comp.get("qty",1),
                "✓" if has_t else "-",
                f"{conf:.0%}", comp.get("data_source","-"), comp.get("ai_notes","-")]
        for ci,v in enumerate(vals,1):
            cell = ws.cell(row=er,column=ci,value=v)
            cell.font=DF; cell.border=TB
            cell.alignment = la if ci==len(CH) else ca
            if fill: cell.fill=fill
        ws.cell(row=er,column=2).font = Font(
            name="Arial",bold=True,size=10,color=CC.get(cat,"808080"))
        ws.row_dimensions[er].height=16
    ws.freeze_panes="A4"

    # ── Sheet 2: Terminals ────────────────────────────────────────────────────
    wt = wb.create_sheet("Terminal Schedule")
    total_t = sum(t["qty"] for t in terminals)
    title(wt, f"Terminal Block Schedule  ·  {src}", len(TH))
    sub(wt, f"Types: {len(terminals)}   |   Total terminals: {total_t}", len(TH))
    hdrs(wt, TH, TW)
    if not terminals:
        wt.merge_cells(f"A4:{get_column_letter(len(TH))}4")
        c=wt["A4"]; c.value="No outgoing circuits with cable sizes found."
        c.font=Font(name="Arial",italic=True,size=10,color="888888")
        c.alignment=Alignment(horizontal="center")
    else:
        for ri,t in enumerate(terminals,1):
            er=ri+3
            for ci,v in enumerate([ri,t["name"],t["description"],
                                    t["max_mm2"],t["qty"],t["circuits"]],1):
                cell=wt.cell(row=er,column=ci,value=v)
                cell.border=TB
                cell.alignment = la if ci==len(TH) else ca
                cell.font = BLD if ci in (1,2,5) else DF
                if ri%2==0: cell.fill=GF
            wt.row_dimensions[er].height=16
        tr=len(terminals)+4
        wt.cell(row=tr,column=4,value="TOTAL").font=BLD
        c=wt.cell(row=tr,column=5,value=total_t); c.font=BLD; c.alignment=ca
    wt.freeze_panes="A4"

    # ── Sheet 3: Enclosure ────────────────────────────────────────────────────
    we = wb.create_sheet("Enclosure")
    title(we, f"Panel Enclosure  ·  {src}", 2, bg="2E4057")
    hdrs(we, EH, EW)
    for ri,(field,value) in enumerate([
        ("Panel Reference",      enclosure.get("ref","-")),
        ("Enclosure Size",        enclosure.get("size","-")),
        ("Manufacturer",          enclosure.get("manufacturer","-")),
        ("Catalog / Part Number", enclosure.get("catalog","-")),
    ],1):
        er=ri+3
        we.cell(row=er,column=1,value=field).font=BLD
        we.cell(row=er,column=2,value=value).font=DF
        for ci in range(1,3):
            we.cell(row=er,column=ci).border=TB
            we.cell(row=er,column=ci).alignment=la
            we.cell(row=er,column=ci).fill=BF
        we.row_dimensions[er].height=20

    # ── Sheet 4: Additional Info ──────────────────────────────────────────────
    wi = wb.create_sheet("Additional Info")
    title(wi, f"Additional Information Detected  ·  {src}", len(IH), bg="4472C4")
    sub(wi, "Items detected by AI as extra useful data present in this drawing.", len(IH))
    hdrs(wi, IH, IW)
    if not add_info:
        wi.merge_cells(f"A4:{get_column_letter(len(IH))}4")
        c=wi["A4"]; c.value="No additional information detected."
        c.font=Font(name="Arial",italic=True,size=10,color="888888")
        c.alignment=Alignment(horizontal="center")
    else:
        for ri,info in enumerate(add_info,1):
            er=ri+3
            for ci,v in enumerate([ri,info.get("type","-"),
                                    info.get("description","-"),
                                    info.get("value","-")],1):
                cell=wi.cell(row=er,column=ci,value=v)
                cell.border=TB
                cell.alignment = la if ci>2 else ca
                cell.font = BLD if ci in (1,2) else DF
                if ri%2==0: cell.fill=IF
            wi.row_dimensions[er].height=18
    wi.freeze_panes="A4"

    # ── Sheet 5: Shop Drawing ─────────────────────────────────────────────────
    wd = wb.create_sheet("Shop Drawing")
    title(wd, f"Drawing Render  ·  {src}", 4, bg="2E4057")
    wd["A2"] = "Full DXF render from the client's drawing file."
    wd["A2"].font=Font(name="Arial",italic=True,size=10,color="444444")
    wd.row_dimensions[2].height=18
    if png_path and Path(png_path).exists():
        try:
            img=XLImage(str(png_path))
            img.anchor="A4"; img.width=1100; img.height=800
            wd.add_image(img)
            wd.row_dimensions[4].height=600
            print("   ✓ Shop drawing embedded in Excel")
        except Exception as e:
            wd["A4"]=f"Image could not be embedded: {e}"
    else:
        wd["A4"]="No drawing render available."
        wd["A4"].font=Font(name="Arial",italic=True,size=10,color="888888")

    # ── Sheet 6: Summary ──────────────────────────────────────────────────────
    sm = wb.create_sheet("Summary")
    title(sm,"Component Summary by Category",3)
    for ci,h in enumerate(["Category","Groups","Total Units"],1):
        c=sm.cell(row=2,column=ci,value=h)
        c.font=Font(name="Arial",bold=True,size=10,color="FFFFFF")
        c.fill=PatternFill("solid",fgColor="2E4057")
        c.alignment=Alignment(horizontal="center")
    cs=defaultdict(lambda:{"groups":0,"units":0})
    for comp in components:
        cat=comp.get("category","OTHER").upper()
        cs[cat]["groups"]+=1; cs[cat]["units"]+=int(comp.get("qty",1))
    for ri,(cat,d) in enumerate(sorted(cs.items()),3):
        sm.cell(row=ri,column=1,value=cat).font=Font(
            name="Arial",bold=True,size=10,color=CC.get(cat,"000000"))
        sm.cell(row=ri,column=2,value=d["groups"])
        sm.cell(row=ri,column=3,value=d["units"])
        for ci in range(1,4):
            sm.cell(row=ri,column=ci).alignment=Alignment(horizontal="center")
            sm.cell(row=ri,column=ci).border=TB
    for col,w in zip(["A","B","C"],[18,12,14]):
        sm.column_dimensions[col].width=w

    wb.save(out_path)
    print(f"\n✅  Excel saved → {out_path}")


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="SLD Extractor v4.2")
    parser.add_argument("dwg")
    parser.add_argument("--api-key",  required=False)
    parser.add_argument("--output",   default="sld_components.xlsx")
    parser.add_argument("--oda-path", default=None)
    parser.add_argument("--dpi",      type=int, default=10)
    parser.add_argument("--no-image", action="store_true")
    args = parser.parse_args()

    dwg_path = Path(args.dwg)
    if not dwg_path.exists():
        print(f"❌  Not found: {dwg_path}"); sys.exit(1)

    # Debug folder — raw Gemini responses saved here
    debug_dir = Path(args.output).parent / "sld_debug"
    debug_dir.mkdir(exist_ok=True)
    print(f"📁  Debug dumps → {debug_dir}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        # 1. DWG → DXF
        dxf_path = dwg_path if dwg_path.suffix.lower()==".dxf" \
                   else dwg_to_dxf(dwg_path, args.oda_path, tmp_dir)

        # 2. DXF → text
        print("\n📋  Extracting DXF data …")
        text_payload = extract_dxf(dxf_path)
        print(f"   Payload: {len(text_payload):,} chars")

        # 3. DXF → PNG
        png_path = None
        if not args.no_image:
            png_path = render_png(dxf_path, tmp_dir/"render.png", args.dpi)

        # 4a. Gemini: components
        print("\n🤖  Gemini extraction …")
        components = call_gemini_components(text_payload, png_path, args.api_key or api_key, debug_dir)
        if not components:
            print("⚠  No components. Check sld_debug/gemini_components_raw.txt"); sys.exit(1)

        # 4b. Gemini: enclosure + extra info
        extra = call_gemini_extra(text_payload, png_path, args.api_key or api_key, debug_dir)
        enclosure = extra.get("enclosure", {})
        add_info  = extra.get("additional_info", [])

        # 5. Group
        print("\n🔗  Grouping identical components …")
        components = group_components(components)
        print(f"   {len(components)} unique group(s)")

        # 6. Terminals
        print("\n🔌  Calculating terminals …")
        terminals = calculate_terminals(components)
        total_t   = sum(t["qty"] for t in terminals)
        print(f"   {len(terminals)} type(s)  |  {total_t} total terminals")

        # Need to copy PNG before tempdir closes
        final_png = None
        if png_path and png_path.exists():
            final_png = debug_dir / "render.png"
            shutil.copy2(png_path, final_png)

        # 7. Excel
        print("\n📊  Writing Excel …")
        write_excel(components, terminals, enclosure, add_info,
                    final_png, args.output, dwg_path.name)

    print(f"\n📁  Raw Gemini responses saved in: {debug_dir}")
    print("    Check gemini_components_raw.txt and gemini_extra_raw.txt if results look wrong.")


if __name__ == "__main__":
    main()