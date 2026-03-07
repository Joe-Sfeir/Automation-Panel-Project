"""
SLD Component Extractor  v4.3
==============================
Pipeline:
  1. DWG → DXF  (ODA File Converter)
  2. DXF → structured text + spatial clusters
  3. DXF → PNG render (150 DPI, hatches skipped for speed)
  4. Gemini — Components + ratings + cable sizes  (text + image)
  5. Post-processing: group identical rows, sum qty
  6. Terminal calculation
       • Incomer  : 1 terminal per pole of main switch (input cable terminations)
       • Outgoing : 1 × PT "2x" terminal per circuit  (handles phase + neutral)
  7. Excel output  (Components | Terminal Schedule | Shop Drawing | Summary)

Usage:
    python dwg_test3.py drawing.dwg [--api-key YOUR_KEY] [--output out.xlsx]

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
from pydantic import BaseModel, Field
from typing import List

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE     = PROJECT_ROOT / ".venv" / "key.env"

def get_api_model() -> str:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("GEMINI_API_MODEL"):
            return line.split("=", 1)[1].strip()
    raise ValueError("GEMINI_API_MODEL not found in key.env")

GEMINI_MODEL = get_api_model()

def get_config() -> dict:
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
#  PYDANTIC SCHEMAS FOR STRUCTURED OUTPUT
# ─────────────────────────────────────────────────────────────────────────────
class Component(BaseModel):
    category: str = Field(description="MCB, MCCB, RCCB, RCBO, SWITCH, CONTACTOR, SPD, FUSE_HOLDER, PILOT_LAMP, METER, OTHER")
    label: str = Field(description="Model name ONLY if explicitly written (e.g., 'DPX250', 'CT 40/5'). Else '-'")
    poles: str = Field(description="1P, 1P+N, 2P, 3P, 4P, R/Y/B (PILOT_LAMP), 3P (CT)")
    amps: str = Field(description="Rated current e.g., 10A, 63A. CT ratio e.g., 40/5. Else '-'")
    curve: str = Field(description="B, C, or D. Default C for breakers. '-' for others.")
    breaking_capacity: str = Field(description="e.g. 6kA, 10kA. '-' if not applicable.")
    sensitivity: str = Field(description="e.g. 30mA for RCCB/RCBO. '-' if not applicable.")
    spd_type: str = Field(description="Surge protection details. '-' if not applicable.")
    cable_size: float = Field(description="Phase conductor mm² as a float. 0 if not shown. For incomer, extract from feeder cable annotation.")
    load_label: str = Field(description="Circuit name(s), comma-separated. Includes 'Spare' etc.")
    qty: int = Field(description="Total count of this exact component setup. From BLOCK INSTANCE COUNTS if available.")
    has_terminal: bool = Field(description="True ONLY for outgoing circuits feeding real external loads.")
    is_incomer: bool = Field(description="True ONLY for the main incoming device (first/top device in hierarchy).")
    confidence: float = Field(description="1.0 (both sources agree), 0.85 (minor inference), 0.75 (one source), 0.5 (conflict)")
    data_source: str = Field(description="'BOTH', 'DXF_ONLY', or 'IMAGE_ONLY'")
    ai_notes: str = Field(description="Explain inferences or conflicts. '-' if fully confident.")
    dxf_block: str = Field(default="-", description="DXF block name(s) from the BLOCK INSTANCE COUNTS table for this component. Comma-separated if multiple variants. '-' if not found.")

class SLDResponse(BaseModel):
    panel_ref: str = Field(description="The distribution board reference (e.g. DB-B1). 'UNKNOWN' if not found.")
    components: List[Component] = Field(description="Array of extracted components.")


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

# ─────────────────────────────────────────────────────────────────────────────
#  DEFAULT MANUFACTURER BY CATEGORY
#  Used to pre-populate the Manufacturer column in the raw components Excel.
#  The designer can override any row before running the matcher.
# ─────────────────────────────────────────────────────────────────────────────
#  Rules (same logic as infer_brand() in match_offer.py):
#    TERMINAL  → always Phoenix Contact  (matched against Phoenix PPL)
#    SPD       → always DEHN             (matched against DEHN PPL)
#    METER/CT  → always MBS              (matched against MBS PPL)
#    everything else → project default brand (set via --manufacturer arg)

BRAND_OVERRIDES = {
    "TERMINAL":   "PHOENIX",
    "SPD":        "DEHN",
}
# METER is handled separately: only CT rows → MBS, power meters → default brand


def default_brand_for(comp: dict, project_brand: str) -> str:
    """Return the default manufacturer for a component row."""
    cat   = comp.get("category", "OTHER").upper()
    label = (comp.get("label") or "").lower()

    if cat in BRAND_OVERRIDES:
        return BRAND_OVERRIDES[cat]

    # CT (current transformer) always uses MBS
    # Gemini may return category="CT" or category="METER" with label containing "ct"
    if cat == "CT" or (cat == "METER" and re.search(r"\bct\b", label)):
        return "MBS"

    return project_brand


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
            print("❌ ODA File Converter not found! ezdxf cannot read native .dwg files.")
            print("Please install ODA File Converter to process DWG files.")
            sys.exit(1)

    in_dir  = tmp_dir / "in";  in_dir.mkdir()
    out_dir = tmp_dir / "out"; out_dir.mkdir()
    shutil.copy2(dwg_path, in_dir / dwg_path.name)

    print(f"🔄  DWG → DXF  ({Path(oda_path).name})")
    try:
        # On Windows: hide the ODA window so it doesn't flash on screen
        kwargs = {}
        if platform.system() == "Windows":
            si = subprocess.STARTUPINFO()
            si.dwFlags    |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0   # SW_HIDE
            kwargs["startupinfo"] = si

        subprocess.run([str(oda_path), str(in_dir), str(out_dir),
                        "ACAD2018", "DXF", "0", "1"],
                       capture_output=True, text=True, timeout=120, **kwargs)
    except PermissionError:
        print("❌  PermissionError — run cmd as Administrator"); sys.exit(1)

    hits = list(out_dir.glob("*.dxf"))
    if not hits: print("❌  ODA produced no DXF"); sys.exit(1)
    print(f"   ✓ {hits[0].name}  ({hits[0].stat().st_size // 1024} KB)")
    return hits[0]


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2  DXF → Text payload  +  exact block counts
# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2  DXF → block counts  (attribute-aware, drawing-style agnostic)
# ─────────────────────────────────────────────────────────────────────────────
# Common attribute tag names drafters use for current rating
_RATING_TAGS = {"amps", "amp", "rating", "current", "in", "rated_current",
                "i_n", "in_", "current_rating", "breaker_rating", "ampere"}

def _extract_rating_from_attribs(attribs: dict) -> str:
    """
    Try to find the current rating from a block's attributes.
    Returns normalised string e.g. "16A", "10A", or "" if not found.
    """
    for tag, val in attribs.items():
        if tag.lower().strip() in _RATING_TAGS:
            # Normalise: strip spaces, ensure 'A' suffix
            val = val.strip()
            if re.match(r"^\d+(\.\d+)?$", val):
                return f"{val}A"
            if re.match(r"^\d+(\.\d+)?[Aa]$", val):
                return val.upper()
            return val   # return as-is if format unexpected
    return ""


def count_dxf_blocks(dxf_path: Path) -> dict:
    """
    Return two structures:

    block_counts  — by block name only (same as before, used for the payload header)
        block_name → {"count": int, "attrib_samples": [...]}

    rated_counts  — grouped by (block_name, rating_from_attrib)
        (block_name, rating) → {"count": int}
        rating is "" when no rating attribute was found.

    This makes reconciliation drawing-style agnostic:
    - Separate blocks per rating  → rated_counts key is ("MCB_16A", "")
    - One generic block+attrib    → rated_counts key is ("MCB_1PN", "16A")
    Both resolve to exact per-rating counts regardless of drafter style.
    """
    try:
        doc, _ = recover.readfile(str(dxf_path))
    except Exception:
        return {}, {}

    block_counts: dict[str, dict] = {}
    rated_counts: dict[tuple, dict] = {}

    for ent in doc.modelspace().query("INSERT"):
        try:
            name    = ent.dxf.name.strip()
            pos     = ent.dxf.insert
            attribs = {a.dxf.tag.strip(): a.dxf.text.strip()
                       for a in ent.attribs if a.dxf.tag.strip() and a.dxf.text.strip()}
            rating  = _extract_rating_from_attribs(attribs)

            # Block-level counts (for payload header)
            if name not in block_counts:
                block_counts[name] = {"count": 0, "positions": [], "attrib_samples": []}
            block_counts[name]["count"] += 1
            block_counts[name]["positions"].append(
                (round(float(pos[0]), 1), round(float(pos[1]), 1)))
            if len(block_counts[name]["attrib_samples"]) < 3 and attribs:
                block_counts[name]["attrib_samples"].append(attribs)

            # Rated counts (block + rating key)
            key = (name, rating)
            if key not in rated_counts:
                rated_counts[key] = {"count": 0}
            rated_counts[key]["count"] += 1

        except Exception:
            pass

    return block_counts, rated_counts


def extract_dxf(dxf_path: Path, block_counts: dict) -> str:
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

    # ── Build BLOCK COUNTS section — ground truth for Gemini ─────────────────
    # Show every block that appears more than once — works for any drawing type:
    # DB, MDB, ATS, MCC, EIB, UMDB, generator panels, motor starters, etc.
    repeating = {name: d for name, d in block_counts.items() if d["count"] > 1}

    lines = [f"=== DXF: {dxf_path.name} ===",
             f"Layers: {', '.join(sorted({b['layer'] for b in blocks}|{t['layer'] for t in texts}))}",
             "",
             "━━━ BLOCK INSTANCE COUNTS (EXACT — sourced directly from DXF geometry) ━━━",
             "These are hard counts of physical symbols. Use them as the definitive qty",
             "for each component type. Do NOT reduce these counts based on load labels.",
             ""]
    for name, d in sorted(repeating.items(), key=lambda x: -x[1]["count"]):
        samples = "; ".join(
            " ".join(f"{k}={v}" for k,v in s.items())
            for s in d["attrib_samples"]
        ) or "no attribs"
        lines.append(f"  BLOCK '{name}'  ×{d['count']}  | sample attribs: {samples}")

    lines += ["",
              f"--- BLOCK INSERTS ({len(blocks)}) ---"]
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
        # Remove HATCH entities from the in-memory doc before rendering.
        # Hatches are the main cause of slow renders and repeated abort messages —
        # they're not needed for component identification in the image.
        msp = doc.modelspace()
        hatches = list(msp.query("HATCH"))
        for h in hatches:
            msp.delete_entity(h)
        if hatches:
            print(f"   ⚡ Skipped {len(hatches)} HATCH entity/entities (speeds up render)")

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
  • Feeder cable annotation anywhere on the drawing (e.g. "4Cx10mm²+1Cx10mm²", "L=15m").
    Note the phase conductor size — you will assign it to the incomer's cable_size field.
  • Read the "BLOCK INSTANCE COUNTS" table at the top of the DXF data. These are exact
    geometric counts of symbol instances. For any component type where a block count
    is available, USE THAT COUNT as the qty — do not count from load labels.

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

                 CT RULE — MANDATORY:
                   ALWAYS include a CT row for every 3-phase panel, even if no ratings
                   are visible. qty is ALWAYS 3 (one per phase). poles="3P".
                   label="CT {ratio}" if ratio visible (e.g. "CT 40/5"), else label="CT".
                   amps = ratio string if visible (e.g. "40/5"), else "-".
                   has_terminal=false. is_incomer=false.

  OTHER        → ATS, VFD, busbar, transformer, etc.

SPARE CIRCUITS — CRITICAL RULE:
  Count by SYMBOLS, not by labels.
  Scan the busbar/distribution block visually. Every position that has a breaker
  symbol physically drawn and connected — regardless of whether its load label
  says "Spare", "St-By", "Standby", "---", a dash, or is blank — is a real
  installed component. Include it in the qty of its matching rating group.
  The only exception is a position that is completely empty with NO breaker
  symbol drawn at all (labelled "SPACE" or "EMPTY"). Do NOT exclude a position
  just because its load label is a dash or blank.

GROUPING — CRITICAL:
  Components with SAME category + label + poles + amps + curve + breaking_capacity
  MUST be ONE row with summed qty.
  QTY AUTHORITY — in order of priority:
    1. BLOCK INSTANCE COUNTS table (exact, from DXF geometry) — always wins
    2. Visual symbol count from the image
    3. Load label count (least reliable — labels can be missing for spare positions)
  If the block count table shows block 'MCB_1PN' × 14, your qty for that MCB group
  must be 14, even if only 12 load labels are readable.
  Different amps or poles = separate rows.
  NEVER group MCB with MCCB.
  load_label = comma-separated list of all circuit names (including "Spare"/"St-By"/"---").

FIELDS:
  label               → Model name ONLY if explicitly written on the drawing next to
                        the component (e.g. "DPX250", "iC40", "NS160", "CT 40/5").
                        DO NOT infer or guess the model from brand knowledge or context.
                        If no model name is physically written on the drawing → use "-".
  poles               → 1P | 1P+N | 2P | 3P | 4P | R/Y/B (PILOT_LAMP) | 3P (CT)
  amps                → Rated current. MCCB: "63A / 160AF". CT: "40/5". "-" if not shown.
  curve               → B/C/D. Default "C" for MCB/MCCB/RCBO. "-" for others.
  breaking_capacity   → kA from global note or device label. "-" for non-breaking.
  sensitivity         → mA for RCCB/RCBO only. "-" for others.
  spd_type            → SPD only: type, Up, Imax. "-" for others.
  cable_size          → Phase conductor mm² as NUMBER. "4x2.5mm²"→2.5. 0 if not shown.
                        INCOMER RULE: Scan the ENTIRE drawing for a feeder cable
                        annotation — it may appear anywhere: near the supply line,
                        top-left corner, near the panel title, or on the incoming
                        wire itself. Common formats: "4Cx10mm²+1Cx10mm²", "3x10+N",
                        "4x16mm²", "TO MDB-B1  4Cx10mm²+1Cx10mm²", "L=15m  4Cx10mm²".
                        Extract the phase conductor size (the first/larger value).
                        Assign it to the incomer device's cable_size. Never leave 0.
  load_label          → Circuit name(s). Comma-separate if grouped.
  qty                 → Total count after grouping (includes spare circuits).
  has_terminal        → true ONLY for outgoing circuits feeding real external loads
                        (sockets, lighting circuits, sub-panels, motors, HVAC).
                        Spare/St-By circuits of a matching type → same rule as that type.
                        false for: SPD fuses, pilot lamp fuses, meter connections, CTs,
                        group RCCBs protecting other breakers, panel-internal items.
                        Rule: has cable_size > 0 AND feeds an external load → true.
  is_incomer          → true ONLY for the single main incomer device (main switch, main
                        MCCB or main RCCB at the very top of the panel feeding everything
                        else). false for every other component including sub-incomer RCCBs.
                        RULE: if category=SWITCH and it is the first/top device in the
                        panel hierarchy (i.e. all other devices are downstream of it),
                        it IS the incomer → is_incomer=true, no exceptions.
  confidence          → 1.0 both agree | 0.85 minor inference | 0.75 one source | 0.5 conflict
  data_source         → "BOTH" | "DXF_ONLY" | "IMAGE_ONLY"
  ai_notes            → Explain inferences/conflicts. "-" if fully confident.
  dxf_block           → The DXF block name(s) from the BLOCK INSTANCE COUNTS table that
                        correspond to this component row. Comma-separated if multiple block
                        variants map to the same component type.
                        e.g. "MCB_1PN_16" or "MCB_1PN_16, MCB_1PN_SP".
                        "-" if no matching block found in the counts table.

Return the data adhering exactly to the requested schema.
"""


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 4  GEMINI  — Single focused call with Structured Outputs
# ─────────────────────────────────────────────────────────────────────────────
def _make_contents(prompt: str, text: str, png_path: Path | None) -> list:
    contents = [prompt, text]
    if png_path and png_path.exists():
        contents.append(types.Part.from_bytes(
            data=png_path.read_bytes(), mime_type="image/png"))
    return contents

def call_gemini_components(text: str, png_path: Path | None,
                           api_key: str, debug_dir: Path) -> tuple[str, list[dict]]:
    """Returns (panel_ref, components).  panel_ref is 'UNKNOWN' if not found."""
    client   = genai.Client(api_key=api_key)
    contents = _make_contents(PROMPT_COMPONENTS, text, png_path)

    img_info = " + image" if png_path else ""
    print(f"  → Gemini: Components{img_info} … (60–180 sec for large drawings, please wait)")

    # Retry up to 3 times — large payloads occasionally hit 504/timeout on first attempt
    last_err = None
    for attempt in range(1, 4):
        try:
            if attempt > 1:
                print(f"  ↻  Retry {attempt}/3 …")
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                    response_schema=SLDResponse,
                    http_options=types.HttpOptions(timeout=360_000),  # 6 min
                ),
            )
            break  # success — exit retry loop
        except Exception as e:
            last_err = e
            err_str  = str(e)
            if any(x in err_str for x in ("DEADLINE_EXCEEDED", "ReadTimeout", "504", "503")):
                print(f"  ⚠  Attempt {attempt} timed out: {err_str[:120]}")
                if attempt == 3:
                    print("  ✗  All retries exhausted.")
                    raise
            else:
                raise  # non-timeout error — don't retry

    # Save raw response for debugging
    raw = response.text.strip()
    (debug_dir / "gemini_components_raw.txt").write_text(raw, encoding="utf-8")

    # Primary path: SDK parsed into Pydantic model
    if response.parsed:
        result     = response.parsed.model_dump()
        panel_ref  = result.get("panel_ref", "UNKNOWN").strip() or "UNKNOWN"
        components = result.get("components", [])
        print(f"  ✓ Panel: {panel_ref}  |  {len(components)} component group(s)")
        return panel_ref, components

    # Fallback: try parsing raw text as JSON
    print("  ⚠  response.parsed empty — attempting JSON fallback …")
    try:
        result = json.loads(raw)
        if isinstance(result, dict) and "components" in result:
            panel_ref  = str(result.get("panel_ref", "UNKNOWN")).strip() or "UNKNOWN"
            components = result["components"]
            print(f"  ✓ JSON fallback OK — Panel: {panel_ref}  |  {len(components)} group(s)")
            return panel_ref, components
    except (json.JSONDecodeError, KeyError):
        pass

    print("  ✗  All parsing failed — check gemini_components_raw.txt")
    return "UNKNOWN", []

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


def reconcile_qty_from_dxf(components: list[dict],
                            block_counts: dict,
                            rated_counts: dict) -> list[dict]:
    """
    Override Gemini's qty using exact DXF counts from rated_counts.

    rated_counts keys are (block_name, rating) where rating is either:
      - a value extracted from a rating attribute  e.g. ("MCB_1PN", "16A")
      - an empty string when no attribute found    e.g. ("MCB_16A", "")

    Matching logic (drawing-style agnostic):
      1. Normalise Gemini's amps field to match rated_counts format  e.g. "16A"
      2. Find all rated_counts keys where rating matches amps (or rating is "")
         AND the block name is only used for that single amps value → safe to override
      3. Sum the counts for those keys → definitive qty
      4. If DXF total > AI qty → raise (missed spares).
         If DXF total < AI qty → keep AI (Gemini may have grouped variants correctly).
    """
    def normalise_amps(raw: str) -> str:
        """10A, 10 A, 10a  →  '10A'"""
        raw = str(raw).strip()
        m = re.match(r"^(\d+(?:\.\d+)?)\s*[Aa]?$", raw)
        return f"{m.group(1)}A" if m else raw.upper()

    # Pre-compute: for each (block_name, rating) key with a non-empty rating,
    # does that rating appear under ONLY ONE amps value?
    # If the same block appears with "10A", "16A", "20A" attribs, each key is unique.
    # If the block appears with no attrib, key is (name, "") — needs separate handling.

    # Map: block_name → set of ratings seen under it
    block_to_ratings: dict[str, set] = defaultdict(set)
    for (bname, rating) in rated_counts:
        block_to_ratings[bname].add(rating)

    for comp in components:
        cat      = comp.get("category", "").upper()
        ai_amps  = normalise_amps(comp.get("amps", ""))
        ai_qty   = int(comp.get("qty", 1))

        # Collect all rated_counts entries that could match this component
        candidates = []
        for (bname, rating), d in rated_counts.items():
            if rating == "":
                # Block has no rating attrib — only safe if block name is unique
                # to one component row (checked below via block_counts total)
                # Skip for now; handled by name-only fallback
                continue
            if normalise_amps(rating) == ai_amps:
                candidates.append((bname, rating, d["count"]))

        if not candidates:
            # Fallback: try block_name-only match when no rating attribs exist
            # Only safe if the block's total count == block appears for one rating group
            # (i.e. the block is dedicated to this amps value)
            # We can't know this without more context, so skip override.
            continue

        dxf_total = sum(c for _, _, c in candidates)

        if dxf_total == ai_qty:
            continue  # already correct

        if dxf_total > ai_qty:
            print(f"  🔧 qty ↑ raised: {cat} {comp.get('amps','')}  "
                  f"AI={ai_qty} → DXF={dxf_total}  "
                  f"(blocks: {', '.join(b for b,_,_ in candidates)})")
            comp["qty"] = dxf_total
            note = comp.get("ai_notes", "-")
            comp["ai_notes"] = (f"qty raised by DXF attrib count ({dxf_total}). " +
                                (note if note != "-" else ""))
        # DXF < AI: keep Gemini's count — it may have correctly unified sub-variants

    return components


def _poles_to_count(poles_str: str) -> int:
    """Convert a poles string to an integer conductor count."""
    s = str(poles_str).upper().strip()
    mapping = {
        "1P": 1, "2P": 2, "3P": 3, "4P": 4,
        "1P+N": 2, "2P+N": 3, "3P+N": 4, "TPN": 4,
        "R/Y/B": 3,
    }
    if s in mapping:
        return mapping[s]
    m = re.match(r"^(\d+)", s)
    return int(m.group(1)) if m else 1


def _terminal_for_outgoing(mm2: float) -> dict:
    """
    Select the Phoenix Contact PT terminal for an outgoing circuit conductor.
    Industry practice: minimum terminal size is 4mm² regardless of cable size.
    This means a 2.5mm² cable always uses the PT 4 feed-through block.
    Sizes ≥ 4mm² are matched exactly (4→PT4, 6→PT6, 10→PT10 etc.).
    """
    effective_mm2 = max(mm2, 4.0)
    return terminal_for_cable(effective_mm2)


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 6  TERMINALS
# ─────────────────────────────────────────────────────────────────────────────
def calculate_terminals(components: list[dict], incomer_cable_mm2: float = 0.0) -> list[dict]:
    """
    Two terminal pools:

    INCOMER INPUT  (is_incomer=True)
      One terminal per pole of the main switch for the incoming supply cable.
      Cable size taken from: --incomer-cable CLI arg first, then Gemini's cable_size.
      e.g. INS40 4P with 10mm² supply → 4 × Terminal 10mm²

    OUTGOING CIRCUITS  (has_terminal=True, is_incomer=False)
      One Phoenix Contact PT "2x" feed-through terminal per circuit.
      Minimum size enforced at 4mm²: a 2.5mm² circuit → Terminal 2x4mm².
    """
    # ── Safety net: if Gemini forgot to set is_incomer, auto-detect it ────────
    has_incomer = any(c.get("is_incomer", False) for c in components)
    if not has_incomer:
        candidate = None
        for priority_cat in ("SWITCH", "MCCB", "RCCB", "MCB"):
            matches = [c for c in components if c.get("category","").upper() == priority_cat]
            if matches:
                candidate = max(matches, key=lambda c: (
                    parse_mm2(c.get("cable_size", 0)),
                    parse_mm2(c.get("amps", 0))
                ))
                break
        if candidate:
            candidate["is_incomer"] = True
            print(f"  ⚠  No is_incomer found — auto-flagged: "
                  f"{candidate.get('category')} {candidate.get('label','')} "
                  f"{candidate.get('amps','')} as incomer. Verify this is correct.")

    counts = defaultdict(lambda: {"qty": 0, "circuits": [], "description": "", "max_mm2": 0})

    for comp in components:
        qty = int(comp.get("qty", 1))
        ll  = comp.get("load_label") or comp.get("label") or "?"
        amp = comp.get("amps", "")

        if comp.get("is_incomer", False):
            # ── Incomer: CLI override takes priority, then fall back to Gemini ──
            mm2 = incomer_cable_mm2 if incomer_cable_mm2 > 0 \
                  else parse_mm2(comp.get("cable_size", 0))
            if mm2 <= 0:
                print(f"  ⚠  Incomer cable size unknown — no incomer terminals calculated.")
                print(f"     Re-run with --incomer-cable 10 (or whatever mm² your supply cable is).")
                continue
            if incomer_cable_mm2 > 0:
                print(f"  ✓  Incomer cable size from --incomer-cable: {mm2}mm²")
            poles_count = _poles_to_count(comp.get("poles", "1P"))
            term = terminal_for_cable(mm2)
            name = term["name"]
            counts[name]["qty"] += (poles_count * qty)
            counts[name]["description"] = term["description"]   
            counts[name]["max_mm2"]     = term["max_mm2"]
            counts[name]["circuits"].append(
                f"[INCOMER] {ll} {amp} {comp.get('poles','')} @{mm2}mm² ({poles_count} terminals)")

        elif comp.get("has_terminal", False):
            mm2 = parse_mm2(comp.get("cable_size", 0))
            if mm2 <= 0:
                continue
            term = _terminal_for_outgoing(mm2)
            name = term["name"]
            counts[name]["qty"]        += qty
            counts[name]["description"] = term["description"]
            counts[name]["max_mm2"]     = term["max_mm2"]
            counts[name]["circuits"].append(f"{ll} {amp} @{mm2}mm²")

    return [
        {"name": n, "description": d["description"], "max_mm2": d["max_mm2"],
         "qty": d["qty"], "circuits": " | ".join(d["circuits"])}
        for n, d in sorted(counts.items())
    ]

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
#  STEP 7  EXCEL  —  single sheet layout
# ─────────────────────────────────────────────────────────────────────────────
#  Column layout:
#    Components block  (rows 1-N)
#    Blank separator
#    Terminal block    (rows N+3 onwards)
#    Shop Drawing on a second tab (image cannot go inline)
# ─────────────────────────────────────────────────────────────────────────────
CH = ["#","Category","Label","Poles","Amps","Curve","B.C.","Sensitivity",
      "SPD Type","Cable mm²","Load / Circuit","Qty","Terminal?","Manufacturer","Conf.","Source","AI Notes"]
CW = [4,13,14,9,13,7,8,12,22,10,32,5,10,16,8,10,48]

TH = ["#","Terminal Type","Description","Max mm²","Qty","Circuits"]
TW = [4,18,34,10,8,70]


def _section_header(ws, text, nc, start_row, bg="1F3864"):
    lc = get_column_letter(nc)
    ws.merge_cells(f"A{start_row}:{lc}{start_row}")
    c = ws[f"A{start_row}"]
    c.value = text
    c.font = Font(name="Arial", bold=True, size=12, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor=bg)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[start_row].height = 26


def _sub_row(ws, text, nc, row):
    lc = get_column_letter(nc)
    ws.merge_cells(f"A{row}:{lc}{row}")
    c = ws[f"A{row}"]
    c.value = text
    c.font = Font(name="Arial", italic=True, size=10, color="444444")
    c.fill = PatternFill("solid", fgColor="D9E1F2")
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[row].height = 18


def _hdrs_at(ws, hs, ws_, row):
    for ci, (h, w) in enumerate(zip(hs, ws_), 1):
        c = ws.cell(row=row, column=ci, value=h)
        c.font = Font(name="Arial", bold=True, size=10, color="FFFFFF")
        c.fill = HF
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = KB
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.row_dimensions[row].height = 24


def write_excel(components, terminals, png_path, out_path, panel_ref, src, default_mfr="SCHNEIDER"):
    wb = Workbook()
    ws = wb.active
    ws.title = panel_ref[:31]   # Excel sheet name limit = 31 chars

    nc = max(len(CH), len(TH))  # widest section determines merge width

    # ── COMPONENTS SECTION ────────────────────────────────────────────────────
    total_comp = sum(int(c.get("qty", 1)) for c in components)
    _section_header(ws, f"{panel_ref}  ·  SLD Component Extraction  ·  {src}", len(CH), 1)
    total_t = sum(t["qty"] for t in terminals)
    _sub_row(ws, f"Components: {len(components)}   |   Units: {total_comp}   |   Terminal types: {len(terminals)}   |   Total terminals: {total_t}", len(CH), 2)
    _hdrs_at(ws, CH, CW, 3)

    for ri, comp in enumerate(components, 1):
        er   = ri + 3
        conf = float(comp.get("confidence", 1.0))
        fill = cf(conf) or (AF if ri % 2 == 0 else None)
        cat  = comp.get("category", "OTHER").upper()
        mm2  = parse_mm2(comp.get("cable_size", 0))
        has_t = comp.get("has_terminal", False)
        mfr  = default_brand_for(comp, default_mfr)
        vals = [ri, cat,
                comp.get("label", "-"),   comp.get("poles", "-"),
                comp.get("amps", "-"),    comp.get("curve", "-"),
                comp.get("breaking_capacity", "-"), comp.get("sensitivity", "-"),
                comp.get("spd_type", "-"),
                str(mm2) if mm2 > 0 else "-",
                comp.get("load_label", "-"),
                comp.get("qty", 1),
                "✓" if has_t else "-",
                mfr,
                f"{conf:.0%}", comp.get("data_source", "-"), comp.get("ai_notes", "-")]
        for ci, v in enumerate(vals, 1):
            cell = ws.cell(row=er, column=ci, value=v)
            cell.font = DF; cell.border = TB
            cell.alignment = la if ci == len(CH) else ca
            if fill: cell.fill = fill
        ws.cell(row=er, column=2).font = Font(
            name="Arial", bold=True, size=10, color=CC.get(cat, "808080"))
        # Manufacturer column — bold, distinct light-green fill so designer spots it easily
        mfr_col = CH.index("Manufacturer") + 1
        ws.cell(row=er, column=mfr_col).font = Font(name="Arial", bold=True, size=10)
        ws.cell(row=er, column=mfr_col).fill = PatternFill("solid", fgColor="E2EFDA")
        ws.row_dimensions[er].height = 16

    # ── TERMINAL ROWS — appended to the same components table ────────────────
    # Each terminal type becomes a row with category="TERMINAL" in the same CH columns.
    # Columns used:
    #   #           → sequential (continuing from components)
    #   Category    → "TERMINAL"
    #   Label       → terminal name  (e.g. "Terminal 2x4mm²")
    #   Poles       → "-"
    #   Amps        → "-"
    #   Curve       → "-"
    #   B.C.        → "-"
    #   Sensitivity → "-"
    #   SPD Type    → description  (e.g. "Phoenix Contact PT 4 feed-through")
    #   Cable mm²   → max_mm2
    #   Load/Circuit→ circuits string
    #   Qty         → qty
    #   Terminal?   → "-"  (it IS the terminal)
    #   Manufacturer→ "PHOENIX"
    #   Conf.       → "-"
    #   Source      → "-"
    #   AI Notes    → "-"

    TERM_FILL = PatternFill("solid", fgColor="EBF0FA")   # light blue for terminal rows
    mfr_col   = CH.index("Manufacturer") + 1

    for ti, t in enumerate(terminals, 1):
        ri = len(components) + ti
        er = ri + 3
        vals = [
            ri,                    # #
            "TERMINAL",            # Category
            t["name"],             # Label  e.g. "Terminal 2x4mm²"
            "-",                   # Poles
            "-",                   # Amps
            "-",                   # Curve
            "-",                   # B.C.
            "-",                   # Sensitivity
            t["description"],      # SPD Type col → reused for terminal description
            str(t["max_mm2"]),     # Cable mm²
            t["circuits"],         # Load / Circuit
            t["qty"],              # Qty
            "-",                   # Terminal? (it IS the terminal)
            "PHOENIX",             # Manufacturer
            "-",                   # Conf.
            "-",                   # Source
            "-",                   # AI Notes
        ]
        for ci, v in enumerate(vals, 1):
            cell = ws.cell(row=er, column=ci, value=v)
            cell.font  = DF
            cell.border = TB
            cell.fill  = TERM_FILL
            cell.alignment = la if ci == len(CH) else ca
        # Category col: teal colour to distinguish terminal rows visually
        ws.cell(row=er, column=2).font = Font(name="Arial", bold=True, size=10, color="2E75B6")
        # Manufacturer col: same green highlight as component rows
        ws.cell(row=er, column=mfr_col).font = Font(name="Arial", bold=True, size=10)
        ws.cell(row=er, column=mfr_col).fill = PatternFill("solid", fgColor="E2EFDA")
        ws.row_dimensions[er].height = 16

    ws.freeze_panes = "A4"

    # ── SHOP DRAWING (separate tab — image can't go inline) ───────────────────
    wd = wb.create_sheet("Drawing")
    wd.merge_cells("A1:D1")
    c = wd["A1"]
    c.value = f"{panel_ref}  ·  Drawing Render  ·  {src}"
    c.font = Font(name="Arial", bold=True, size=12, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor="2E4057")
    c.alignment = Alignment(horizontal="center", vertical="center")
    wd.row_dimensions[1].height = 28

    if png_path and Path(png_path).exists():
        try:
            img = XLImage(str(png_path))
            img.anchor = "A3"; img.width = 1100; img.height = 800
            wd.add_image(img)
            wd.row_dimensions[3].height = 600
            print("   ✓ Shop drawing embedded")
        except Exception as e:
            wd["A3"] = f"Image could not be embedded: {e}"
    else:
        wd["A3"] = "No drawing render available."
        wd["A3"].font = Font(name="Arial", italic=True, size=10, color="888888")

    wb.save(out_path)
    print(f"\n✅  Excel saved → {out_path}")


def _safe_filename(name: str) -> str:
    """Strip characters that are illegal in filenames on Windows/Mac/Linux."""
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip()


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="SLD Extractor v4.3")
    parser.add_argument("dwg")
    parser.add_argument("--api-key",  required=False, help="Gemini API key (overrides key.env)")
    parser.add_argument("--output",   default=None,
                        help="Output .xlsx path. Defaults to {board_name}_rawcomponents.xlsx")
    parser.add_argument("--oda-path", default=None)
    parser.add_argument("--dpi",            type=int,   default=50)
    parser.add_argument("--no-image",       action="store_true")
    parser.add_argument("--incomer-cable",  type=float, default=0.0,
                        help="Incomer supply cable size in mm² (e.g. 10). "
                             "Use this when Gemini does not extract it from the drawing.")
    parser.add_argument("--manufacturer",    default="SCHNEIDER",
                        help="Project default brand pre-filled in the Manufacturer column "
                             "(e.g. SCHNEIDER, EATON, LEGRAND). Overridden per-category "
                             "for SPD (DEHN), terminals (PHOENIX), and CT meters (MBS).")
    args = parser.parse_args()

    dwg_path = Path(args.dwg)
    if not dwg_path.exists():
        print(f"❌  Not found: {dwg_path}"); sys.exit(1)

    # Resolve API key: CLI arg takes priority, falls back to key.env
    cfg     = get_config()
    api_key = args.api_key or cfg["GEMINI_API_KEY"]

    # Debug folder — raw Gemini responses saved here
    out_dir   = dwg_path.parent
    debug_dir = out_dir / "sld_debug"
    debug_dir.mkdir(exist_ok=True)
    print(f"📁  Debug dumps → {debug_dir}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        # 1. DWG → DXF
        dxf_path = dwg_path if dwg_path.suffix.lower()==".dxf" \
                   else dwg_to_dxf(dwg_path, args.oda_path, tmp_dir)

        # 2. DXF → block counts + text payload
        print("\n📋  Extracting DXF data …")
        block_counts, rated_counts = count_dxf_blocks(dxf_path)
        repeating = {k: v for k, v in block_counts.items() if v["count"] > 1}
        print(f"   Block types: {len(block_counts)}  |  repeating symbols: {len(repeating)}")
        for name, d in sorted(repeating.items(), key=lambda x: -x[1]["count"])[:8]:
            print(f"     '{name}' × {d['count']}")
        # Show rated breakdown if attribs found
        rated_with_rating = {k: v for k, v in rated_counts.items() if k[1]}
        if rated_with_rating:
            print(f"   Rated entries (block+attrib): {len(rated_with_rating)}")
            for (bname, rating), d in sorted(rated_with_rating.items(), key=lambda x: -x[1]["count"])[:8]:
                print(f"     '{bname}' [{rating}] × {d['count']}")
        text_payload = extract_dxf(dxf_path, block_counts)
        print(f"   Payload: {len(text_payload):,} chars")

        # 3. DXF → PNG
        png_path = None
        if not args.no_image:
            png_path = render_png(dxf_path, tmp_dir/"render.png", args.dpi)

        # 4. Gemini: components + panel reference
        print("\n🤖  Gemini extraction …")
        panel_ref, components = call_gemini_components(
            text_payload, png_path, api_key, debug_dir)
        if not components:
            print("⚠  No components. Check sld_debug/gemini_components_raw.txt"); sys.exit(1)

        # 5. Resolve output path — use panel_ref for filename unless --output was given
        if args.output:
            out_path = Path(args.output)
        else:
            safe_name = _safe_filename(panel_ref)
            out_path  = out_dir / f"{safe_name}_rawcomponents.xlsx"
        print(f"   Output → {out_path}")

        # 6. Group
        print("\n🔗  Grouping identical components …")
        components = group_components(components)
        print(f"   {len(components)} unique group(s)")

        # 6b. Reconcile qty against exact DXF block counts
        print("\n🔧  Reconciling quantities against DXF block counts …")
        components = reconcile_qty_from_dxf(components, block_counts, rated_counts)

        # 7. Terminals
        print("\n🔌  Calculating terminals …")
        terminals = calculate_terminals(components, incomer_cable_mm2=args.incomer_cable)
        total_t   = sum(t["qty"] for t in terminals)
        print(f"   {len(terminals)} type(s)  |  {total_t} total terminals")

        # Copy PNG out before tempdir closes
        final_png = None
        if png_path and png_path.exists():
            final_png = debug_dir / "render.png"
            shutil.copy2(png_path, final_png)

        # 8. Excel
        print("\n📊  Writing Excel …")
        write_excel(components, terminals, final_png, out_path, panel_ref, dwg_path.name,
                     default_mfr=args.manufacturer)

    print(f"\n📁  Raw Gemini response saved in: {debug_dir}/gemini_components_raw.txt")


if __name__ == "__main__":
    main()