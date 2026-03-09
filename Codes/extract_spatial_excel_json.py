"""
SLD Component Extractor  v5.2
==============================
Pipeline:
  1. DWG → DXF  (ODA File Converter — REQUIRED for .dwg; graceful abort if missing)
  2. DXF → structured text + spatial clusters
     • Block names scanned for embedded amp ratings (e.g. MCB_16A → "Implies 16A")
     • Cluster radius computed analytically from drawing geometry (p25 spacing)
  3. DXF → PNG render (150 DPI, hatches skipped for speed)
  4. Gemini — Components + ratings + cable sizes  (text + image)
     • Pydantic-driven structured output — JSON parsing can NEVER crash
  5. Post-processing:
     • group_components()   — merge identical rows, sum qty
     • _resolve_incomer()   — single authoritative pass to validate/correct
                              is_incomer BEFORE Excel write and terminal calc
  6. Terminal calculation   — PTTB (double-level) vs PT (single-level) per circuit
  7. Excel output           — Components | Terminal Schedule | Shop Drawing

Usage:
    python extract.py drawing.dwg [--api-key YOUR_KEY] [--output out.xlsx]

Install:
    pip install "ezdxf[draw]" google-genai openpyxl matplotlib Pillow pydantic

Changes in v5.2 vs v5.1:
  1. Adaptive cluster radius  — _compute_cluster_radius() uses 25th-percentile
     nearest-neighbour spacing between repeating block inserts so the radius
     tracks the actual panel grid rather than a hardcoded constant.
     Clamped to [30, 150] DXF units. Logged to console for QC.
  2. _resolve_incomer()  — dedicated pre-write validation pass that:
     a) Overrides Gemini if it flagged a non-SWITCH while a SWITCH exists
     b) Auto-detects incomer by SWITCH → MCCB → RCCB → MCB priority
        (replaces the old safety net that only lived inside calculate_terminals)
     c) Runs BEFORE write_excel() so the Excel file always contains the
        corrected is_incomer value — which match_offer.py depends on
  3. Prompt improvements  — stricter SWITCH multi-device and rating-conflict rules
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
from datetime import datetime
from pathlib import Path
from typing import Optional

# Force line-buffered output (fixes silent hanging in PowerShell / cmd)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

# ── Pydantic — for structured Gemini outputs (Upgrade #1) ────────────────────
from pydantic import BaseModel, Field

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
#  UPGRADE #1 — PYDANTIC SCHEMA FOR STRUCTURED GEMINI OUTPUT
#
#  Why:  Manually stripping "```json ... ```" fences and calling json.loads()
#        was fragile — any stray character caused a crash.  Pydantic structured
#        outputs guarantee the response is valid JSON that exactly matches the
#        schema, and the SDK will raise before we ever see invalid JSON.
# ─────────────────────────────────────────────────────────────────────────────

class Component(BaseModel):
    """Represents one row in the component extraction — one group of identical devices."""
    cable_size: float = Field(
            description=(
                "Phase conductor cross-section in mm² as a number (e.g. 2.5, 10.0). "
                "0 if not shown. CRITICAL: Outgoing switches/isolators (e.g. feeding sub-panels) "
                "often have cable sizes. Look carefully for them."
            )
        )
    category: str = Field(
        description=(
            "One of: MCB | MCCB | RCCB | RCBO | SWITCH | CONTACTOR | SPD | "
            "FUSE_HOLDER | PILOT_LAMP | METER | CT | OTHER"
        )
    )
    label: str = Field(
        description=(
            "Model/part name ONLY if physically written on the drawing (e.g. 'DPX250', "
            "'iC40', 'CT 40/5'). Use '-' if no model is written."
        )
    )
    poles: str = Field(
        description="1P | 1P+N | 2P | 3P | 4P | R/Y/B (PILOT_LAMP) | 3P (CT)"
    )
    amps: str = Field(
        description=(
            "Rated current as a string (e.g. '16A', '63A / 160AF', '40/5' for CT). "
            "Use '-' if not shown."
        )
    )
    curve: str = Field(
        description="Tripping curve: B | C | D. Default 'C' for MCB/MCCB/RCBO. '-' for all others."
    )
    breaking_capacity: str = Field(
        description="Short-circuit breaking capacity (e.g. '6kA', '10kA'). '-' for non-breaking devices."
    )
    sensitivity: str = Field(
        description="Residual current sensitivity in mA for RCCB/RCBO only (e.g. '30mA'). '-' for all others."
    )
    spd_type: str = Field(
        description="SPD devices only: type, Up voltage, Imax. '-' for all other devices."
    )

    load_label: str = Field(
        description="Circuit name(s). Comma-separated if this row groups multiple circuits."
    )
    qty: int = Field(
        description=(
            "Total quantity after grouping. ALWAYS defer to the BLOCK INSTANCE COUNTS table "
            "in the DXF data — it is the authoritative geometric count."
        )
    )
    has_terminal: bool = Field(
        description=(
            "True ONLY for outgoing circuits feeding real external loads "
            "(sockets, lighting, sub-panels, motors, HVAC). "
            "False for SPD fuses, pilot-lamp fuses, meter connections, CTs, "
            "group RCCBs protecting other breakers, or any panel-internal item."
        )
    )
    is_incomer: bool = Field(
        description=(
            "True ONLY for the single main incomer device "
            "(main switch / MCCB / RCCB at the very top of the panel). "
            "False for every other component, including sub-incomer RCCBs."
        )
    )
    confidence: float = Field(
        description=(
            "Extraction confidence: "
            "1.0 = both DXF and image agree; "
            "0.85 = minor inference needed; "
            "0.75 = single source only; "
            "0.5 = conflict between sources."
        )
    )
    data_source: str = Field(
        description="One of: BOTH | DXF_ONLY | IMAGE_ONLY"
    )
    ai_notes: str = Field(
        description="Explain inferences or conflicts. Use '-' if fully confident."
    )


class SLDResponse(BaseModel):
    """Top-level structured response from Gemini for one SLD drawing."""

    panel_ref: str = Field(
        description=(
            "The distribution board / panel reference found in the title block, "
            "drawing header, panel label, or notes. "
            "Examples: 'DB-B1', 'MDB', 'LP-GF', 'ATS-01'. "
            "Use 'UNKNOWN' only if truly nothing is found."
        )
    )
    components: list[Component] = Field(
        description="All electrical components extracted from the drawing."
    )


# ─────────────────────────────────────────────────────────────────────────────
#  TERMINAL MAP  — Phoenix Contact PT series
#  Naming: "Terminal 2x{size}mm²" for ≤6mm², "Terminal {size}mm²" for ≥10mm²
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
#  TERMINAL MAPS  —  Phoenix Contact product range, v5.1
#
#  PTTB_MAP  — Double-Level feed-through blocks (Phoenix Contact PTTB series)
#    One block body = TWO conductors (phase bottom, neutral top).
#    Used ONLY for 1-phase outgoing circuits (poles: 1P+N / 2P) with ≤ 4mm²
#    cable.  We stock one size only: PTTB 4 (accepts 1.5, 2.5, and 4mm² wire).
#
#  PT_MAP  — Single-Level feed-through blocks (Phoenix Contact PT series)
#    One block body = ONE conductor.
#    Used for: 1P circuits, 3P/4P circuits, and all incomer supply cables.
#    Minimum stocked size: PT 4 (accepts 1.5, 2.5, and 4mm² wire).
#
#  Tuple format: (max_cable_mm², display_name, product_description, nominal_mm²)
# ─────────────────────────────────────────────────────────────────────────────

PTTB_MAP = [
    (4.0, "Terminal 2x4mm² (PTTB)", "Phoenix Contact PTTB 4 double-level", 4.0),
]

PT_MAP = [
    (4.0,  "Terminal 4mm²",  "Phoenix Contact PT 4 single-level",   4.0),
    (6.0,  "Terminal 6mm²",  "Phoenix Contact PT 6 single-level",   6.0),
    (10.0, "Terminal 10mm²", "Phoenix Contact PT 10 single-level", 10.0),
    (16.0, "Terminal 16mm²", "Phoenix Contact PT 16 single-level", 16.0),
    (35.0, "Terminal 35mm²", "Phoenix Contact PT 35 single-level", 35.0),
    (70.0, "Terminal 70mm²", "Phoenix Contact PT 70 single-level", 70.0),
]

# Legacy alias — the incomer path in calculate_terminals() calls terminal_for_cable()
# which looks up PT_MAP.  Incomers always use single-level PT (one block per pole).
TERMINAL_MAP = PT_MAP

# ─────────────────────────────────────────────────────────────────────────────
#  DEFAULT MANUFACTURER BY CATEGORY
#  Used to pre-populate the Manufacturer column in the raw components Excel.
#  Rules:
#    TERMINAL  → always Phoenix Contact  (matched against Phoenix PPL)
#    SPD       → always DEHN             (matched against DEHN PPL)
#    CT        → always MBS
#    everything else → project default brand (set via --manufacturer arg)
# ─────────────────────────────────────────────────────────────────────────────
BRAND_OVERRIDES = {
    "TERMINAL": "PHOENIX",
    "SPD":      "DEHN",
}

def default_brand_for(comp: dict, project_brand: str) -> str:
    """Return the default manufacturer for a component row."""
    cat   = comp.get("category", "OTHER").upper()
    label = (comp.get("label") or "").lower()
    if cat in BRAND_OVERRIDES:
        return BRAND_OVERRIDES[cat]
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
def find_oda() -> Optional[str]:
    import glob
    if platform.system() == "Windows":
        for pat in [
            r"C:\Program Files\ODA\ODAFileConverter*\ODAFileConverter.exe",
            r"C:\Program Files\ODA\ODAFileConverter 26.12.0\ODAFileConverter.exe",
            r"C:\Program Files (x86)\ODA\ODAFileConverter*\ODAFileConverter.exe",
        ]:
            hits = glob.glob(pat)
            if hits: return hits[0]
    elif platform.system() == "Darwin":
        hits = glob.glob("/Applications/ODAFileConverter*.app/Contents/MacOS/ODAFileConverter")
        if hits: return hits[0]
    else:
        for p in ["/usr/bin/ODAFileConverter", "/usr/local/bin/ODAFileConverter"]:
            if Path(p).exists(): return p
    return None


def dwg_to_dxf(dwg_path: Path, oda_path: Optional[str], tmp_dir: Path) -> Path:
    """
    Convert a .dwg file to .dxf using the ODA File Converter.

    UPGRADE #4 — ODA Crash Guard
    ─────────────────────────────
    If ODA is not found AND the source file is a native .dwg binary, we must
    abort immediately.  ezdxf uses the DXF ASCII/binary format and cannot parse
    a .dwg binary — attempting to do so produces a fatal, confusing traceback.
    We now exit cleanly with a clear human-readable error message instead.
    """
    if not oda_path:
        oda_path = find_oda()

    if not oda_path or not Path(oda_path).exists():
        # ── UPGRADE #4: Graceful abort — never pass .dwg to ezdxf ────────────
        print("❌  ODA File Converter not found!")
        print("    ezdxf cannot read native .dwg binary files.")
        print("    Please install ODA File Converter from: https://www.opendesign.com/guestfiles/oda_file_converter")
        print("    (or pass a .dxf file directly if you already have one)")
        sys.exit(1)

    in_dir  = tmp_dir / "in";  in_dir.mkdir()
    out_dir = tmp_dir / "out"; out_dir.mkdir()
    shutil.copy2(dwg_path, in_dir / dwg_path.name)

    print(f"🔄  DWG → DXF  ({Path(oda_path).name})")
    try:
        kwargs = {}
        if platform.system() == "Windows":
            # Hide the ODA console window on Windows so it doesn't flash on-screen
            si = subprocess.STARTUPINFO()
            si.dwFlags    |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0   # SW_HIDE
            kwargs["startupinfo"] = si

        subprocess.run(
            [str(oda_path), str(in_dir), str(out_dir), "ACAD2018", "DXF", "0", "1"],
            capture_output=True, text=True, timeout=120, **kwargs,
        )
    except PermissionError:
        print("❌  PermissionError — run cmd as Administrator")
        sys.exit(1)

    hits = list(out_dir.glob("*.dxf"))
    if not hits:
        print("❌  ODA produced no DXF output")
        sys.exit(1)

    print(f"   ✓ {hits[0].name}  ({hits[0].stat().st_size // 1024} KB)")
    return hits[0]


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2  DXF → Text payload  +  exact block counts
# ─────────────────────────────────────────────────────────────────────────────

# UPGRADE #2 — Lazy Drafter Block Name Regex
# ───────────────────────────────────────────
# Some drafters skip block attributes entirely and encode the rating directly
# into the block name (e.g. MCB_16A, RCBO_32A, CB_1P_63A).
# We detect this pattern with a simple regex and store it in the block entry
# so the text payload can pass it as a hard hint to Gemini.
_BLOCK_AMP_RATING_RE = re.compile(
    r"(?<!\d)(\d{1,4}A)(?!\d)",   # e.g. "16A", "63A", "125A" — not "400Hz" or "240V"
    re.IGNORECASE,
)


def _extract_implied_rating(block_name: str) -> Optional[str]:
    """
    Inspect a block name for an embedded amp rating like '16A', '63A', '125A'.

    Returns the first match (e.g. "16A") or None if nothing is found.
    Only matches patterns that look like current ratings — the regex deliberately
    avoids matching voltage values (e.g. 230V, 400V) or frequencies (50Hz).
    """
    m = _BLOCK_AMP_RATING_RE.search(block_name)
    return m.group(1).upper() if m else None


def count_dxf_blocks(dxf_path: Path) -> dict:
    """
    Return a dict: block_name → {
        "count":           int,
        "attrib_samples":  [list of attrib dicts],
        "implied_rating":  str | None,   ← NEW in v5.0 (Upgrade #2)
    }

    Every INSERT entity in modelspace is counted.  Block names that appear more
    than once are the repeating symbols (MCBs, RCCBs, etc.) that Gemini tends to
    miscount.  We pass these counts — plus any implied amp ratings — to Gemini
    as authoritative hard facts.
    """
    try:
        doc, _ = recover.readfile(str(dxf_path))
    except Exception:
        return {}

    counts: dict[str, dict] = {}
    for ent in doc.modelspace().query("INSERT"):
        try:
            name   = ent.dxf.name.strip()
            pos    = ent.dxf.insert
            attribs = {
                a.dxf.tag.strip(): a.dxf.text.strip()
                for a in ent.attribs
                if a.dxf.tag.strip() and a.dxf.text.strip()
            }
            if name not in counts:
                # ── UPGRADE #2: scan the block name for an embedded rating ───
                counts[name] = {
                    "count":          0,
                    "positions":      [],
                    "attrib_samples": [],
                    "implied_rating": _extract_implied_rating(name),  # NEW
                }
            counts[name]["count"] += 1
            counts[name]["positions"].append(
                (round(float(pos[0]), 1), round(float(pos[1]), 1))
            )
            if len(counts[name]["attrib_samples"]) < 3 and attribs:
                counts[name]["attrib_samples"].append(attribs)
        except Exception:
            pass

    return counts


def extract_dxf(dxf_path: Path, block_counts: dict) -> str:
    """
    Build the text payload that is sent alongside the PNG to Gemini.

    UPGRADE #2 — Hard hints in BLOCK INSTANCE COUNTS section
    ──────────────────────────────────────────────────────────
    When a block name contains an embedded amp rating (e.g. MCB_16A → "16A"),
    we append "(Implies 16A)" to the block's line in the BLOCK INSTANCE COUNTS
    table.  This acts as an unambiguous hard hint so Gemini does not have to
    rely solely on visual OCR to determine the rating of that component type.
    """
    try:
        doc, auditor = recover.readfile(str(dxf_path))
    except Exception as e:
        print(f"❌  Cannot open DXF: {e}")
        sys.exit(1)

    if auditor.has_errors:
        print(f"  ⚠  DXF audit: {len(auditor.errors)} error(s)")

    msp    = doc.modelspace()
    blocks = []
    texts  = []

    for ent in msp.query("INSERT"):
        try:
            pos     = ent.dxf.insert
            attribs = {
                a.dxf.tag.strip(): a.dxf.text.strip()
                for a in ent.attribs
                if a.dxf.tag.strip() and a.dxf.text.strip()
            }
            blocks.append({
                "block":   ent.dxf.name,
                "x":       round(float(pos[0]), 1),
                "y":       round(float(pos[1]), 1),
                "layer":   ent.dxf.layer,
                "attribs": attribs,
            })
        except Exception:
            pass

    for ent in msp:
            if ent.dxftype() not in ("TEXT", "MTEXT"):
                continue
            try:
                txt = (
                    ent.dxf.text
                    if ent.dxftype() == "TEXT"
                    else ent.plain_mtext()
                ).strip()
                if not txt:
                    continue
                pos = getattr(ent.dxf, "insert", (0, 0, 0))
                
                # --- NEW: Grab the text height ---
                height = getattr(ent.dxf, "height", getattr(ent.dxf, "char_height", 10.0))
                
                texts.append({
                    "content": txt,
                    "x":       round(float(pos[0]), 1),
                    "y":       round(float(pos[1]), 1),
                    "height":  float(height), # Save the height
                    "layer":   ent.dxf.layer,
                })
            except Exception:
                pass
    # ── Adaptive cluster radius — derived from the drawing's text height ────────
    # Uses the median DXF text height so the radius scales with whatever font size
    # the drafter used — completely immune to title block block-spacing pollution.
    radius   = _compute_text_cluster_radius(texts)
    clusters = _cluster(texts, radius)
    # ── Build BLOCK COUNTS section — authoritative ground truth for Gemini ───
    # Only show blocks that appear more than once (the repeating component symbols).
    repeating = {name: d for name, d in block_counts.items() if d["count"] > 1}

    lines = [
        f"=== DXF: {dxf_path.name} ===",
        f"Layers: {', '.join(sorted({b['layer'] for b in blocks} | {t['layer'] for t in texts}))}",
        "",
        "━━━ BLOCK INSTANCE COUNTS (EXACT — sourced directly from DXF geometry) ━━━",
        "These are hard counts of physical symbols. Use them as the definitive qty",
        "for each component type. Do NOT reduce these counts based on load labels.",
        "Where a block name implies a current rating, it is shown as '(Implies XA)'",
        "— treat this as a strong hint for that component's rated current.",
        "",
    ]

    for name, d in sorted(repeating.items(), key=lambda x: -x[1]["count"]):
        samples = "; ".join(
            " ".join(f"{k}={v}" for k, v in s.items())
            for s in d["attrib_samples"]
        ) or "no attribs"

        # ── UPGRADE #2: append implied rating hint if found in block name ────
        implied = d.get("implied_rating")
        rating_hint = f"  ⚡ Implies {implied}" if implied else ""

        lines.append(
            f"  BLOCK '{name}'  ×{d['count']}{rating_hint}  | sample attribs: {samples}"
        )

    lines += [
        "",
        f"--- BLOCK INSERTS ({len(blocks)}) ---",
    ]
    for b in blocks:
        att = ", ".join(f"{k}={v}" for k, v in b["attribs"].items()) or "no attribs"
        lines.append(
            f"  BLOCK '{b['block']}' [{b['layer']}] @({b['x']},{b['y']}) | {att}"
        )

    lines += ["", f"--- TEXT CLUSTERS ({len(clusters)}) ---"]
    for i, cl in enumerate(clusters, 1):
        lines.append(
            f"  Cluster {i:03d} @({cl['cx']},{cl['cy']}): {'  |  '.join(cl['texts'])}"
        )

    lines += ["", f"--- ALL TEXT ({len(texts)}) ---"]
    for t in texts:
        lines.append(f"  [{t['layer']}] ({t['x']},{t['y']}): {t['content']}")

    return "\n".join(lines)


def _cluster(texts: list, radius: float) -> list:
    visited  = [False] * len(texts)
    clusters = []
    for i, t in enumerate(texts):
        if visited[i]:
            continue
        grp = [t]; visited[i] = True
        for j, t2 in enumerate(texts):
            if visited[j]:
                continue
            if ((t["x"] - t2["x"]) ** 2 + (t["y"] - t2["y"]) ** 2) ** 0.5 <= radius:
                grp.append(t2); visited[j] = True
        clusters.append({
            "texts": [g["content"] for g in grp],
            "cx":    round(sum(g["x"] for g in grp) / len(grp), 1),
            "cy":    round(sum(g["y"] for g in grp) / len(grp), 1),
        })
    return clusters






def _compute_text_cluster_radius(texts: list) -> float:
    """
    Derive the text-clustering radius from the drawing's median font height.

    WHY TEXT HEIGHT BEATS BLOCK SPACING
    ─────────────────────────────────────
    Block-spacing approaches (our previous v5.2 attempt) are polluted by
    title block INSERT entities that repeat across the full sheet at large
    spacings.  Text height is immune to this: we're measuring the unit in
    which labels are written, then using that unit to decide how far apart
    two text strings can be and still belong to the same label group.

    MULTIPLIER  1.5×
    ─────────────────
    A typical two-line label (e.g. "Living Room" on one line, "16A" below)
    has a line gap of ~1.5× the character height.  Using 1.5× as the radius
    tightly clusters the two lines without reaching the next breaker row.
    3× was too aggressive and pulled in adjacent circuit labels.

    MEDIAN vs MEAN
    ───────────────
    The drawing contains a few large title/heading text entities (height 30–50)
    alongside many small label entities (height 3–5).  The median stays anchored
    to the dominant small-label population — the mean would be skewed upward.

    CLAMPING  [15, 80] DXF units
    ─────────────────────────────
    Prevents collapse on drawings with unusually tiny fonts and prevents
    over-grouping on drawings where the only text is large heading text.
    """
    if not texts:
        return 40.0

    # Median of all text heights — anchored to the small component-label population
    heights       = sorted([t.get("height", 10.0) for t in texts])
    median_height = heights[len(heights) // 2]

    # 1.5× matches typical DXF line spacing for multi-line labels
    radius = median_height * 1.5

    # Clamp: [15, 80] DXF units
    radius = max(15.0, min(radius, 80.0))

    print(f"   📐 Auto cluster radius: {round(radius, 1)} units  "
          f"(median text height: {round(median_height, 1)})")
    return radius

# ─────────────────────────────────────────────────────────────────────────────
#  STEP 3  DXF → PNG
# ─────────────────────────────────────────────────────────────────────────────
def render_png(dxf_path: Path, out_png: Path, dpi: int = 150) -> Optional[Path]:
    print(f"🖼  Rendering DXF → PNG at {dpi} DPI …")
    try:
        doc = ezdxf.readfile(str(dxf_path))
        msp = doc.modelspace()
        # Strip HATCH entities — they're the primary cause of slow renders
        # and add no useful component information to the AI image.
        hatches = list(msp.query("HATCH"))
        for h in hatches:
            msp.delete_entity(h)
        if hatches:
            print(f"   ⚡ Skipped {len(hatches)} HATCH entity/entities (speeds up render)")

        fig = plt.figure(figsize=(24, 18), dpi=dpi)
        ax  = fig.add_axes([0, 0, 1, 1])
        ax.set_aspect("equal")
        ctx     = RenderContext(doc)
        backend = MatplotlibBackend(ax)
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
#  PROMPT  — Single focused call
#
#  UPGRADE #1 NOTE: The prompt no longer shows a raw JSON template.
#  Instead it refers to the schema which Gemini receives as a structured
#  constraint via response_schema=SLDResponse in the API config.
#  This separation of prompt logic from JSON shape reduces hallucinated structure.
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
  • Global notes (e.g. "B.C.: 6kA/400V") → apply to ALL breakers unless overridden per device
  • Panel name / reference and incomer rating
  • Feeder cable annotation anywhere on the drawing (e.g. "4Cx10mm²+1Cx10mm²", "L=15m").
    Extract the phase conductor size and assign it to the incomer's cable_size field.
  • Read the "BLOCK INSTANCE COUNTS" table at the top of the DXF data — these are exact
    geometric counts. For any component type where a block count is available, USE THAT
    COUNT as the qty. Do not count from load labels.
  • Where a block entry shows "⚡ Implies XA", treat that as the rated current for
    every instance of that block unless the drawing overrides it for a specific circuit.
  • COMBO DRAWINGS: If the drawing contains BOTH a schematic (SLD) on the left and a physical
   panel layout (Shop Drawing) on the right, completely IGNORE the physical layout. Base your 
   quantities and components ONLY on the schematic.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 2 — EXTRACT ALL COMPONENTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CATEGORIES:
  MCB          → Miniature Circuit Breaker (≤125A, DIN rail, no frame suffix)
  MCCB         → /100AF /160AF /250AF /400AF /630AF OR labeled MCCB OR >125A → ALWAYS MCCB
  RCCB         → Residual Current CB (no overcurrent)
  RCBO         → Residual Current Breaker with Overcurrent
  SWITCH       → Isolating switch / disconnector / ISOL.S / ISW.
                 CRITICAL — A panel may contain MORE THAN ONE isolating switch:
                   • The MAIN incomer switch (top of hierarchy, is_incomer=true)
                   • One or more OUTGOING isolators feeding sub-panels, motors,
                     AHUs, or dedicated loads (is_incomer=false, has_terminal=true).
                 Each switch MUST appear as its OWN separate row.
                 NEVER merge an outgoing isolator into an RCCB or MCB row.
                 NEVER assume there is only one switch just because one is obvious.
  CONTACTOR    → Contactor
  SPD          → Surge Protection Device
  FUSE_HOLDER  → Fuse holder/base with fuses. NEVER call this MCB.
  PILOT_LAMP   → ALL phase indication lamps as ONE row. qty=3 (3-phase), poles="R/Y/B".
  METER        → Digital power meter, energy meter, voltmeter, ammeter. NEVER skip.
  CT           → Current transformer. ALWAYS include for every 3-phase panel.
                 qty=3 always (one per phase). poles="3P". label="CT {ratio}" if visible.

SPARE CIRCUITS — CRITICAL RULE:
  Count by SYMBOLS, not by labels.
  Every position with a breaker symbol physically drawn and connected is a real installed
  component — regardless of whether its load label says "Spare", "St-By", "---", or blank.
  Only a position with NO breaker symbol at all (labelled "SPACE" or "EMPTY") is excluded.
  Do NOT exclude a position just because its load label is a dash or blank.

GROUPING — CRITICAL:
  Components with the SAME category + label + poles + amps + curve + breaking_capacity
  MUST be ONE row with summed qty.
  QTY AUTHORITY (in order of priority):
    1. BLOCK INSTANCE COUNTS table (exact, from DXF geometry) — always wins
    2. Visual symbol count from the image
    3. Load label count (least reliable — labels can be missing for spare positions)
  Different amps or poles = separate rows.
  NEVER group MCB with MCCB.
  load_label = comma-separated list of all circuit names (including "Spare"/"St-By"/"---").

FIELD RULES:
  label             → Model name ONLY if physically written next to the component.
                      DO NOT infer from brand knowledge. Use '-' if not written.
  amps              → Rated current. MCCB: "63A / 160AF". CT: "40/5". '-' if not shown.
  curve             → B/C/D. Default "C" for MCB/MCCB/RCBO. '-' for others.
  breaking_capacity → From global note or device label. '-' for non-breaking devices.
  sensitivity       → mA for RCCB/RCBO only. '-' for others.
  spd_type          → SPD only: type, Up, Imax. '-' for others.
  cable_size        → Phase conductor mm² as a NUMBER. "4x2.5mm²" → 2.5. 0 if not shown.
                      INCOMER RULE: Scan the ENTIRE drawing for the supply cable annotation
                      (may appear anywhere — near supply line, title block, on the cable).
                      Common formats: "4Cx10mm²+1Cx10mm²", "3x10+N", "4x16mm²", "L=15m 4Cx10mm²".
                      Extract the phase conductor size and assign to the incomer's cable_size.
  has_terminal      → true ONLY for outgoing circuits feeding real external loads 
                      (e.g., sockets, lighting, sub-panels, motors).
                      CRITICAL: Set to true if it leaves the panel, EVEN IF the cable size is missing!
                      false for: SPD fuses, pilot lamp fuses, meter connections, CTs,
                      group RCCBs protecting other breakers, any panel-internal item.
  is_incomer        → true ONLY for the single main incomer (top-most device in the panel).
                      TOPOLOGY RULE: The incomer is the device that:
                        (a) connects directly to the incoming supply cable, AND
                        (b) has ALL other panel devices downstream of it.
                      If a SWITCH exists at the top of the hierarchy, it IS the
                      incomer — even if an MCCB or RCCB appears below it.
  confidence        → 1.0 both agree | 0.85 minor inference | 0.75 one source | 0.5 conflict
  data_source       → "BOTH" | "DXF_ONLY" | "IMAGE_ONLY"
  ai_notes          → Explain inferences/conflicts. '-' if fully confident.

RATING CONFLICT RULE — CRITICAL:
  If the BLOCK INSTANCE COUNTS table shows e.g. '2x16 × 5' but your text reading
  suggests a different split (e.g. 4 × 16A + 1 × 10A), you MUST trust the block
  count (5 × 16A) and flag the conflict in ai_notes.
  A mismatch between block count and readable text labels almost always means a
  label was physically closer to the wrong symbol — the block count wins.
  Never silently discard block instances or reassign them to a different rating.

Respond with a JSON object that strictly conforms to the response schema you have been given.
The object must have exactly two keys: "panel_ref" and "components".
"""


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 4  GEMINI  — Single focused call with Pydantic structured output
# ─────────────────────────────────────────────────────────────────────────────
def _make_contents(prompt: str, text: str, png_path: Optional[Path]) -> list:
    """Assemble the multi-part contents list for the Gemini API call."""
    contents = [prompt, text]
    if png_path and png_path.exists():
        contents.append(
            types.Part.from_bytes(data=png_path.read_bytes(), mime_type="image/png")
        )
    return contents


def _gemini_call_structured(
    client,
    contents: list,
    label: str,
) -> str:
    """
    Execute a Gemini API call with Pydantic structured output.

    UPGRADE #1 — Pydantic Structured Output
    ────────────────────────────────────────
    By passing response_schema=SLDResponse and response_mime_type="application/json",
    the Gemini API guarantees the response is valid JSON that exactly matches the
    SLDResponse Pydantic model.  This completely eliminates:
      • Markdown code fence stripping (```json ... ```)
      • json.loads() crashes from malformed responses
      • Missing / extra keys that break downstream parsing

    Returns the raw JSON string (always valid — no stripping needed).
    """
    img_info = " + image" if len(contents) > 2 else ""
    print(f"  → {label}{img_info} … (30–90 sec, please wait)")

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            # ── UPGRADE #1: enforce structured JSON output via Pydantic schema ──
            response_mime_type="application/json",
            response_schema=SLDResponse,
            # Keep a generous timeout — large drawings with images take time
            http_options=types.HttpOptions(timeout=180_000),
        ),
    )
    # response.text is now GUARANTEED to be valid JSON matching SLDResponse.
    # No fence stripping. No defensive try/except around json.loads.
    return response.text.strip()


def call_gemini_components(
    text: str,
    png_path: Optional[Path],
    api_key: str,
    debug_dir: Path,
) -> tuple[str, list[dict]]:
    """
    Call Gemini to extract components and panel reference from the SLD.

    Returns (panel_ref: str, components: list[dict]).
    panel_ref is 'UNKNOWN' if not found.

    With structured output (Upgrade #1), the JSON is guaranteed to be valid.
    We still keep a try/except for belt-and-suspenders safety, but it should
    never trigger under normal conditions.
    """
    client   = genai.Client(api_key=api_key)
    contents = _make_contents(PROMPT_COMPONENTS, text, png_path)
    raw      = _gemini_call_structured(client, contents, "Gemini: Components")

    # Persist the raw response for debugging
    (debug_dir / "gemini_components_raw.txt").write_text(raw, encoding="utf-8")

    try:
        result = json.loads(raw)
        # Structured output always produces a dict with "panel_ref" + "components"
        if isinstance(result, dict) and "components" in result:
            panel_ref  = str(result.get("panel_ref", "UNKNOWN")).strip() or "UNKNOWN"
            components = result["components"]
            print(f"  ✓ Panel: {panel_ref}  |  {len(components)} component group(s)")
            return panel_ref, components

        # Safety fallback: bare array (should not occur with structured output)
        if isinstance(result, list):
            print("  ⚠  No panel_ref in response — using 'UNKNOWN'")
            print(f"  ✓ {len(result)} component group(s)")
            return "UNKNOWN", result

        print("  ⚠  Unexpected structure — check gemini_components_raw.txt")
        return "UNKNOWN", []

    except json.JSONDecodeError as e:
        # With structured output this should never happen, but log it clearly.
        print(f"  ⚠  JSON error: {e} — check gemini_components_raw.txt")
        return "UNKNOWN", []


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 5  GROUPING
# ─────────────────────────────────────────────────────────────────────────────
def group_components(components: list[dict]) -> list[dict]:
    """
    Merge component rows that share the same electrical identity into one row
    with a summed qty.  Identical = same category + label + poles + amps +
    curve + breaking_capacity.
    """
    merged: dict = {}
    for comp in components:
        cat = comp.get("category", "OTHER").upper()
        if cat == "PILOT_LAMP":
            key = ("PILOT_LAMP",)
        else:
            key = (
                cat,
                str(comp.get("label", "-")).strip(),
                str(comp.get("poles", "-")).strip(),
                str(comp.get("amps", "-")).strip(),
                str(comp.get("curve", "-")).strip(),
                str(comp.get("breaking_capacity", "-")).strip(),
            )

        if key not in merged:
            merged[key]             = dict(comp)
            merged[key]["category"] = cat
            merged[key]["qty"]      = int(comp.get("qty", 1))
            ll = comp.get("load_label", "-")
            merged[key]["_labels"]  = [ll] if ll and ll != "-" else []
        else:
            ex = merged[key]
            ex["qty"] += int(comp.get("qty", 1))
            ex["confidence"] = min(
                float(ex.get("confidence", 1.0)),
                float(comp.get("confidence", 1.0)),
            )
            ll = comp.get("load_label", "-")
            if ll and ll != "-":
                ex["_labels"].append(ll)
            # Retain the larger cable size
            ex["cable_size"] = max(
                parse_mm2(ex.get("cable_size", 0)),
                parse_mm2(comp.get("cable_size", 0)),
            )
            # If any row in the group has has_terminal=True, keep True
            if comp.get("has_terminal", False):
                ex["has_terminal"] = True

    result = []
    for comp in merged.values():
        labels = comp.pop("_labels", [])
        comp["load_label"] = ", ".join(labels) if labels else "-"
        # Normalise PILOT_LAMP fields
        if comp.get("category") == "PILOT_LAMP":
            comp.update({
                "poles": "R/Y/B", "amps": "-", "curve": "-",
                "breaking_capacity": "-", "qty": 3, "has_terminal": False,
            })
        result.append(comp)
    return result


def _poles_to_count(poles_str: str) -> int:
    """Convert a poles string to an integer conductor count.

    Used by calculate_terminals() to determine how many terminal blocks
    are needed per circuit (one per conductor/pole).
    """
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


# Poles strings that qualify a circuit for a double-level PTTB block.
# These are all 2-conductor outgoing circuits where phase + neutral can be
# stacked into one compact PTTB body, saving DIN rail space.
_PTTB_POLES = {"1P+N", "1PN", "2P"}

# Poles strings that always force single-level PT blocks regardless of cable size.
# (3-phase circuits — each conductor must occupy its own individual block.)
_PT_ALWAYS_POLES = {"3P", "4P", "3P+N", "4P+N", "TPN"}


def _terminal_for_outgoing(mm2: float, poles: str) -> tuple[dict, int]:
    """
    Select the correct Phoenix Contact terminal block for one outgoing circuit.

    Returns: (terminal_dict, blocks_per_circuit)
      terminal_dict       — the block to order (name, description, max_mm²)
      blocks_per_circuit  — how many blocks ONE circuit of this type consumes
                            (1 for PTTB, poles_count for single-level PT)

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    DECISION TREE
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    RULE 0 — Minimum floor
      Any cable < 4mm² is treated as 4mm² for inventory standardisation.
      (Shop stocks PTTB 4 and PT 4 only — both accept 1.5 / 2.5 / 4mm² wire.)

    RULE 1 — PTTB double-level  (1P+N / 1PN / 2P  AND  ≤ 4mm²)
      → PTTB 4 :  1 block per circuit
      One block body handles both conductors (phase bottom, neutral top).
      Qty formula:  circuit_qty × 1

    RULE 2 — PT single-level  (1P  AND  ≤ 4mm²)
      → PT 4 :  1 block per circuit
      The neutral for a 1P circuit terminates on the common neutral busbar
      inside the panel — no outgoing terminal block is needed for it.
      Qty formula:  circuit_qty × 1

    RULE 3 — PT single-level  (3P / 4P / 3P+N / TPN  OR  cable ≥ 6mm²)
      → PT {size} :  poles_count blocks per circuit
      Each conductor gets its own individual PT block.
      Qty formula:  circuit_qty × poles_count
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    """
    normalised_poles = str(poles).upper().strip()

    # ── RULE 0: Enforce 4mm² minimum for inventory standardisation ────────────
    effective_mm2 = max(mm2, 4.0)

    # ── RULE 1: Double-level PTTB (1-phase 2-conductor, ≤ 4mm²) ─────────────
    if normalised_poles in _PTTB_POLES and effective_mm2 <= 4.0:
        term = PTTB_MAP[0]   # Only one PTTB size stocked: PTTB 4
        return (
            {"name": term[1], "description": term[2], "max_mm2": term[3]},
            1,   # 1 PTTB block handles both conductors of this circuit
        )

    # ── RULE 2: Single-level PT for a plain 1P circuit (≤ 4mm²) ─────────────
    #    Neutral goes to the common busbar — no terminal block needed for it.
    if normalised_poles == "1P" and effective_mm2 <= 4.0:
        term = PT_MAP[0]     # PT 4
        return (
            {"name": term[1], "description": term[2], "max_mm2": term[3]},
            1,   # 1 PT block for the single outgoing phase conductor
        )

    # ── RULE 3: Single-level PT for multi-phase or large-cable circuits ───────
    poles_count = _poles_to_count(normalised_poles)
    for max_mm2, name, desc, nominal in PT_MAP:
        if effective_mm2 <= max_mm2:
            return (
                {"name": name, "description": desc, "max_mm2": nominal},
                poles_count,   # one PT block per outgoing conductor
            )
    # Fallback: cable larger than our largest stocked PT
    return (
        {"name": "Terminal 70mm²+",
         "description": "Cable > 70mm² — check Phoenix Contact catalog",
         "max_mm2": 70},
        poles_count,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  INCOMER RESOLUTION  — single authoritative pass, runs before BOTH
#  write_excel() and calculate_terminals() so every consumer sees the
#  corrected is_incomer value.
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_incomer(components: list[dict]) -> list[dict]:
    """
    Validate and correct is_incomer across the component list.

    WHY THIS EXISTS
    ───────────────
    Gemini sets is_incomer from visual context. It is usually right, but two
    failure modes exist:
      A) Gemini flags a non-SWITCH (e.g. an RCCB) as incomer while a SWITCH
         is also present — the SWITCH always wins on an IEC panel.
      B) Gemini forgets to set is_incomer on anything.

    The old safety net only handled case B, lived inside calculate_terminals(),
    and ran AFTER write_excel() — so the Excel file could contain a wrong value
    that match_offer.py then used for switch-family inference.

    This function handles BOTH cases and must be called in main() BEFORE
    write_excel() and calculate_terminals().

    PRIORITY ORDER FOR INCOMER DETECTION
    ──────────────────────────────────────
    1. SWITCH with largest cable_size  (INS / ISW — always the top device)
    2. MCCB  with largest cable_size  (for panels without a dedicated isolator)
    3. RCCB  with largest cable_size  (unusual but valid for small domestic panels)
    4. MCB   with largest amps        (last resort)
    """
    switches = [c for c in components if c.get("category", "").upper() == "SWITCH"]
    gemini_flagged = [c for c in components if c.get("is_incomer", False)]

    # ── Case A: Gemini flagged something but a SWITCH also exists and was missed ─
    if gemini_flagged and switches:
        flagged_are_switches = all(
            c.get("category", "").upper() == "SWITCH" for c in gemini_flagged
        )
        if not flagged_are_switches:
            # Gemini picked a non-SWITCH — override it
            for c in gemini_flagged:
                c["is_incomer"] = False
            gemini_flagged = []   # fall through to auto-detect below
            print("  ⚠  is_incomer override: Gemini flagged a non-SWITCH "
                  "while a SWITCH exists — correcting.")

    # ── Case B: No incomer set at all — auto-detect ────────────────────────────
    has_incomer = any(c.get("is_incomer", False) for c in components)
    if not has_incomer:
        candidate = None
        for priority_cat in ("SWITCH", "MCCB", "RCCB", "MCB"):
            matches = [
                c for c in components
                if c.get("category", "").upper() == priority_cat
            ]
            if matches:
                candidate = max(
                    matches,
                    key=lambda c: (
                        parse_mm2(c.get("cable_size", 0)),
                        parse_mm2(c.get("amps", 0)),
                    ),
                )
                break
        if candidate:
            candidate["is_incomer"] = True
            print(
                f"  ⚠  is_incomer auto-detected: "
                f"{candidate.get('category')} "
                f"{candidate.get('amps', '')} "
                f"{candidate.get('poles', '')} — verify this is correct."
            )
        else:
            print("  ⚠  is_incomer: could not identify any incomer device.")

    # ── Summary log ───────────────────────────────────────────────────────────
    incomer = next((c for c in components if c.get("is_incomer", False)), None)
    if incomer:
        print(
            f"  ✓  Incomer confirmed: {incomer.get('category')} "
            f"{incomer.get('amps', '')} {incomer.get('poles', '')} "
            f"cable={incomer.get('cable_size', '?')}mm²"
        )

    return components


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 6  TERMINALS
# ─────────────────────────────────────────────────────────────────────────────
def calculate_terminals(
    components: list[dict],
    incomer_cable_mm2: float = 0.0,
) -> list[dict]:
    """
    Calculate the Phoenix Contact terminal block schedule.

    Two terminal pools:

    INCOMER INPUT  (is_incomer=True)
      One single-level PT block per pole of the main switch.
      Cable size from: --incomer-cable CLI arg first, then Gemini's cable_size.
      Example: 40A 4P SWITCH with 10mm² supply → 4 × Terminal 10mm² (PT 10)
      Incomers always use single-level PT — supply cable sizes are always ≥ 10mm².

    OUTGOING CIRCUITS  (has_terminal=True, is_incomer=False)
      Block type and qty-per-circuit are determined by _terminal_for_outgoing():

        poles = 1P+N / 2P  AND  cable ≤ 4mm²
          → PTTB 4  double-level  — 1 block per circuit
          Example: 16 × 1P+N 16A 2.5mm² → 16 × PTTB 4

        poles = 1P  AND  cable ≤ 4mm²
          → PT 4  single-level  — 1 block per circuit
          (Neutral goes to the common panel busbar, no outgoing terminal needed.)
          Example: 3 × 1P 6A 1.5mm² → 3 × PT 4

        poles = 3P / 4P / 3P+N  OR  cable ≥ 6mm²
          → PT {size}  single-level  — poles_count blocks per circuit
          Example: 2 × 3P 32A 6mm² → 6 × PT 6
    """
    counts: defaultdict = defaultdict(
        lambda: {"qty": 0, "circuits": [], "description": "", "max_mm2": 0}
    )

    for comp in components:
        qty = int(comp.get("qty", 1))
        ll  = comp.get("load_label") or comp.get("label") or "?"
        amp = comp.get("amps", "")

        if comp.get("is_incomer", False):
            # ── Incomer: CLI override takes priority, then fall back to Gemini ─
            mm2 = (
                incomer_cable_mm2
                if incomer_cable_mm2 > 0
                else parse_mm2(comp.get("cable_size", 0))
            )
            if mm2 <= 0:
                print("  ⚠  Incomer cable size unknown — no incomer terminals calculated.")
                print("     Re-run with --incomer-cable 10 (or whatever mm² your supply cable is).")
                continue
            if incomer_cable_mm2 > 0:
                print(f"  ✓  Incomer cable size from --incomer-cable: {mm2}mm²")
            poles_count = _poles_to_count(comp.get("poles", "1P"))
            term        = terminal_for_cable(mm2)
            name        = term["name"]
            counts[name]["qty"]         += poles_count * qty
            counts[name]["description"]  = term["description"]
            counts[name]["max_mm2"]      = term["max_mm2"]
            counts[name]["circuits"].append(
                f"[INCOMER] {ll} {amp} {comp.get('poles', '')} "
                f"@{mm2}mm² ({poles_count} terminals)"
            )

        elif comp.get("has_terminal", False):
                    mm2 = parse_mm2(comp.get("cable_size", 0))
                    is_assumed = False
                    
                    # If the drafter forgot the cable size, default to 4.0mm
                    if mm2 <= 0:
                        mm2 = 4.0
                        is_assumed = True
                        
                        # ── NEW: Inject a warning into the AI Notes for the Excel output ──
                        existing_notes = str(comp.get("ai_notes", "-")).strip()
                        warning = "⚠️ Outgoing Cable required but size missing; terminal defaulted to 4mm²."
                        if existing_notes == "-" or not existing_notes:
                            comp["ai_notes"] = warning
                        else:
                            comp["ai_notes"] = f"{existing_notes} | {warning}"
                            
                    poles = comp.get("poles", "1P")

                    # _terminal_for_outgoing() applies the 4mm² floor, selects PTTB vs PT
                    term, blocks_per_circuit = _terminal_for_outgoing(mm2, poles)
                    name = term["name"]

                    counts[name]["qty"]         += qty * blocks_per_circuit
                    counts[name]["description"]  = term["description"]
                    counts[name]["max_mm2"]      = term["max_mm2"]
                    
                    # ── NEW: Highlight the assumption in the Terminal Schedule ──
                    mm2_str = "[ASSUMED 4.0]" if is_assumed else f"{mm2}"
                    
                    counts[name]["circuits"].append(
                        f"{ll} {amp} {poles} @{mm2_str}mm² "
                        f"→ {blocks_per_circuit} block(s) × {qty} circuit(s) "
                        f"= {qty * blocks_per_circuit}"
                    )
    return [
        {
            "name":        n,
            "description": d["description"],
            "max_mm2":     d["max_mm2"],
            "qty":         d["qty"],
            "circuits":    " | ".join(d["circuits"]),
        }
        for n, d in sorted(counts.items())
    ]


# ─────────────────────────────────────────────────────────────────────────────
#  EXCEL HELPERS
# ─────────────────────────────────────────────────────────────────────────────
T  = Side(style="thin",   color="BBBBBB")
K  = Side(style="medium", color="1F3864")
TB = Border(left=T, right=T, top=T, bottom=T)
KB = Border(left=K, right=K, top=K, bottom=K)
HF = PatternFill("solid", fgColor="1F3864")
AF = PatternFill("solid", fgColor="EBF0FA")
WF = PatternFill("solid", fgColor="FFF2CC")
RF = PatternFill("solid", fgColor="FCE4D6")
GF = PatternFill("solid", fgColor="E2EFDA")
ca = Alignment(horizontal="center", vertical="center")
la = Alignment(horizontal="left",   vertical="center", wrap_text=True)

CC = {
    "MCB":          "2E75B6",
    "MCCB":         "C00000",
    "RCCB":         "70AD47",
    "RCBO":         "375623",
    "SWITCH":       "7030A0",
    "CONTACTOR":    "FF6600",
    "SPD":          "FF0000",
    "FUSE_HOLDER":  "ED7D31",
    "PILOT_LAMP":   "BF8F00",
    "METER":        "00B0F0",
    "OTHER":        "808080",
}

def cf(conf: float) -> Optional[PatternFill]:
    v = float(conf)
    if v < 0.5:  return RF
    if v < 0.75: return WF
    return None

def _section_header(ws, text: str, nc: int, start_row: int, bg: str = "1F3864"):
    lc = get_column_letter(nc)
    ws.merge_cells(f"A{start_row}:{lc}{start_row}")
    c = ws[f"A{start_row}"]
    c.value     = text
    c.font      = Font(name="Arial", bold=True, size=13, color="FFFFFF")
    c.fill      = PatternFill("solid", fgColor=bg)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[start_row].height = 30

def _sub_row(ws, text: str, nc: int, row: int):
    lc = get_column_letter(nc)
    ws.merge_cells(f"A{row}:{lc}{row}")
    c = ws[f"A{row}"]
    c.value     = text
    c.font      = Font(name="Arial", italic=True, size=10, color="444444")
    c.fill      = PatternFill("solid", fgColor="D9E1F2")
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[row].height = 18

def _hdrs_at(ws, hs: list, ws_: list, row: int):
    for ci, (h, w) in enumerate(zip(hs, ws_), 1):
        c = ws.cell(row=row, column=ci, value=h)
        c.font      = Font(name="Arial", bold=True, size=10, color="FFFFFF")
        c.fill      = HF
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border    = KB
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.row_dimensions[row].height = 24


DF  = Font(name="Arial", size=10)
BLD = Font(name="Arial", bold=True, size=10)

CH = [
    "#", "Category", "Label", "Poles", "Amps", "Curve", "B.C.", "Sensitivity",
    "SPD Type", "Cable mm²", "Load / Circuit", "Qty", "Terminal?",
    "Manufacturer", "Conf.", "Source", "AI Notes",
]
CW = [4, 13, 14, 9, 13, 7, 8, 12, 22, 10, 32, 5, 10, 16, 8, 10, 48]

TH = ["#", "Terminal Type", "Description", "Max mm²", "Qty", "Circuits"]
TW = [4, 18, 34, 10, 8, 70]


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 7  EXCEL  —  single sheet layout
# ─────────────────────────────────────────────────────────────────────────────
def write_excel(
    components: list[dict],
    terminals:  list[dict],
    png_path:   Optional[Path],
    out_path:   Path,
    panel_ref:  str,
    src:        str,
    default_mfr: str = "SCHNEIDER",
):
    wb = Workbook()
    ws = wb.active
    ws.title = panel_ref[:31]   # Excel sheet name limit is 31 chars

    nc = max(len(CH), len(TH))

    # ── COMPONENTS SECTION ────────────────────────────────────────────────────
    total_comp = sum(int(c.get("qty", 1)) for c in components)
    _section_header(ws, f"{panel_ref}  ·  SLD Component Extraction  ·  {src}", len(CH), 1)
    total_t = sum(t["qty"] for t in terminals)
    _sub_row(
        ws,
        f"Components: {len(components)}   |   Units: {total_comp}   |   "
        f"Terminal types: {len(terminals)}   |   Total terminals: {total_t}",
        len(CH), 2,
    )
    _hdrs_at(ws, CH, CW, 3)

    for ri, comp in enumerate(components, 1):
        er    = ri + 3
        conf  = float(comp.get("confidence", 1.0))
        fill  = cf(conf) or (AF if ri % 2 == 0 else None)
        cat   = comp.get("category", "OTHER").upper()
        mm2   = parse_mm2(comp.get("cable_size", 0))
        has_t = comp.get("has_terminal", False)
        mfr   = default_brand_for(comp, default_mfr)

        vals = [
            ri, cat,
            comp.get("label",   "-"),
            comp.get("poles",   "-"),
            comp.get("amps",    "-"),
            comp.get("curve",   "-"),
            comp.get("breaking_capacity", "-"),
            comp.get("sensitivity",       "-"),
            comp.get("spd_type",          "-"),
            str(mm2) if mm2 > 0 else "-",
            comp.get("load_label", "-"),
            comp.get("qty",  1),
            "✓" if has_t else "-",
            mfr,
            f"{conf:.0%}",
            comp.get("data_source", "-"),
            comp.get("ai_notes",    "-"),
        ]
        for ci, v in enumerate(vals, 1):
            cell            = ws.cell(row=er, column=ci, value=v)
            cell.font       = DF
            cell.border     = TB
            cell.alignment  = la if ci == len(CH) else ca
            if fill:
                cell.fill = fill

        # Category column — brand colour per device type
        ws.cell(row=er, column=2).font = Font(
            name="Arial", bold=True, size=10, color=CC.get(cat, "808080")
        )
        # Manufacturer column — bold, distinct light-green fill
        mfr_col = CH.index("Manufacturer") + 1
        ws.cell(row=er, column=mfr_col).font = Font(name="Arial", bold=True, size=10)
        ws.cell(row=er, column=mfr_col).fill = PatternFill("solid", fgColor="E2EFDA")
        ws.row_dimensions[er].height = 16

    # ── TERMINAL ROWS — appended to the same components table ────────────────
    TERM_FILL = PatternFill("solid", fgColor="EBF0FA")
    mfr_col   = CH.index("Manufacturer") + 1

    for ti, t in enumerate(terminals, 1):
        ri = len(components) + ti
        er = ri + 3
        vals = [
            ri,
            "TERMINAL",         # Category
            t["name"],          # Label  e.g. "Terminal 2x4mm²"
            "-",                # Poles
            "-",                # Amps
            "-",                # Curve
            "-",                # B.C.
            "-",                # Sensitivity
            t["description"],   # SPD Type col → reused for terminal description
            str(t["max_mm2"]),  # Cable mm²
            t["circuits"],      # Load / Circuit
            t["qty"],           # Qty
            "-",                # Terminal? (it IS the terminal)
            "PHOENIX",          # Manufacturer
            "-",                # Conf.
            "-",                # Source
            "-",                # AI Notes
        ]
        for ci, v in enumerate(vals, 1):
            cell            = ws.cell(row=er, column=ci, value=v)
            cell.font       = DF
            cell.border     = TB
            cell.fill       = TERM_FILL
            cell.alignment  = la if ci == len(CH) else ca
        # Category col: blue colour to distinguish terminal rows
        ws.cell(row=er, column=2).font = Font(
            name="Arial", bold=True, size=10, color="2E75B6"
        )
        # Manufacturer col: same green highlight as component rows
        ws.cell(row=er, column=mfr_col).font = Font(name="Arial", bold=True, size=10)
        ws.cell(row=er, column=mfr_col).fill = PatternFill("solid", fgColor="E2EFDA")
        ws.row_dimensions[er].height = 16

    ws.freeze_panes = "A4"

    # ── SHOP DRAWING (separate tab — image cannot go inline) ──────────────────
    wd = wb.create_sheet("Drawing")
    wd.merge_cells("A1:D1")
    c           = wd["A1"]
    c.value     = f"{panel_ref}  ·  Drawing Render  ·  {src}"
    c.font      = Font(name="Arial", bold=True, size=12, color="FFFFFF")
    c.fill      = PatternFill("solid", fgColor="2E4057")
    c.alignment = Alignment(horizontal="center", vertical="center")
    wd.row_dimensions[1].height = 28

    if png_path and Path(png_path).exists():
        try:
            img        = XLImage(str(png_path))
            img.anchor = "A3"
            img.width  = 1100
            img.height = 800
            wd.add_image(img)
            wd.row_dimensions[3].height = 600
            print("   ✓ Shop drawing embedded")
        except Exception as e:
            wd["A3"] = f"Image could not be embedded: {e}"
    else:
        wd["A3"]       = "No drawing render available."
        wd["A3"].font  = Font(name="Arial", italic=True, size=10, color="888888")

    wb.save(out_path)
    print(f"\n✅  Excel saved → {out_path}")


def write_sld_json(
    components: list[dict],
    terminals:  list[dict],
    panel_ref:  str,
    src:        str,
    out_path:   Path,
) -> None:
    """
    Write the extraction result as a structured JSON file.

    This is the primary data interchange format between the extractor and
    the UI / matcher.  It stores exactly the same data that was going into
    Excel, but as typed JSON so:
      • The UI can read / display / edit it without openpyxl
      • The matcher can load it with json.load() — no Excel parsing needed
      • Terminals are re-calculated live in the UI if the user edits a component

    Schema
    ──────
    {
      "panel_ref":    str,        panel board reference  e.g. "DB-F1"
      "source_file":  str,        original drawing filename
      "extracted_at": str,        ISO-8601 timestamp
      "components": [             one entry per unique component group
        {
          "category":          str,
          "label":             str,
          "poles":             str,
          "amps":              str,
          "curve":             str,
          "breaking_capacity": str,
          "sensitivity":       str,
          "spd_type":          str,
          "cable_size":        float,
          "load_label":        str,
          "qty":               int,
          "has_terminal":      bool,
          "is_incomer":        bool,
          "confidence":        float,
          "data_source":       str,
          "ai_notes":          str
        }, ...
      ],
      "terminals": [              one entry per terminal block type
        {
          "name":        str,     e.g. "Terminal 2x4mm² (PTTB)"
          "description": str,
          "max_mm2":     float,
          "qty":         int,
          "circuits":    str
        }, ...
      ]
    }
    """
    payload = {
        "panel_ref":    panel_ref,
        "source_file":  src,
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
        "components":   components,
        "terminals":    terminals,
    }
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"✅  SLD JSON saved → {out_path}")


def _safe_filename(name: str) -> str:
    """Strip characters that are illegal in filenames on Windows/Mac/Linux."""
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="SLD Extractor v5.2")
    parser.add_argument("dwg",
        help="Path to the input drawing (.dwg or .dxf)")
    parser.add_argument("--api-key",  required=False,
        help="Gemini API key (overrides key.env)")
    parser.add_argument("--output",   default=None,
        help="Output .xlsx path. Defaults to {board_name}_rawcomponents.xlsx")
    parser.add_argument("--oda-path", default=None,
        help="Full path to ODAFileConverter.exe (optional override of auto-detection)")
    parser.add_argument("--dpi",      type=int, default=150,
        help="PNG render DPI (default: 150)")
    parser.add_argument("--no-image", action="store_true",
        help="Skip PNG rendering and send text-only to Gemini")
    parser.add_argument("--incomer-cable", type=float, default=0.0,
        help="Incomer supply cable size in mm² (e.g. 10). "
             "Use when Gemini cannot extract it from the drawing.")
    parser.add_argument("--manufacturer", default="SCHNEIDER",
        help="Project default brand pre-filled in the Manufacturer column "
             "(e.g. SCHNEIDER, EATON, LEGRAND). "
             "Per-category overrides: SPD=DEHN, terminals=PHOENIX, CT meters=MBS.")
    args = parser.parse_args()

    dwg_path = Path(args.dwg)
    if not dwg_path.exists():
        print(f"❌  File not found: {dwg_path}")
        sys.exit(1)

    # Resolve API key: CLI arg takes priority, falls back to key.env
    cfg     = get_config()
    api_key = args.api_key or cfg["GEMINI_API_KEY"]

    # Debug folder — raw Gemini responses saved here for post-mortem analysis
    out_dir   = dwg_path.parent
    debug_dir = out_dir / "sld_debug"
    debug_dir.mkdir(exist_ok=True)
    print(f"📁  Debug dumps → {debug_dir}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        # ── 1. DWG → DXF ─────────────────────────────────────────────────────
        dxf_path = (
            dwg_path
            if dwg_path.suffix.lower() == ".dxf"
            else dwg_to_dxf(dwg_path, args.oda_path, tmp_dir)
        )

        # ── 2. DXF → block counts + text payload ─────────────────────────────
        print("\n📋  Extracting DXF data …")
        block_counts = count_dxf_blocks(dxf_path)
        repeating    = {k: v for k, v in block_counts.items() if v["count"] > 1}

        # Show a preview of the most frequent symbols (useful for quick QC)
        print(f"   Block types: {len(block_counts)}  |  repeating symbols: {len(repeating)}")
        for name, d in sorted(repeating.items(), key=lambda x: -x[1]["count"])[:8]:
            implied = f"  [Implies {d['implied_rating']}]" if d.get("implied_rating") else ""
            print(f"     '{name}' × {d['count']}{implied}")

        text_payload = extract_dxf(dxf_path, block_counts)
        print(f"   Payload: {len(text_payload):,} chars")

        # ── 3. DXF → PNG ─────────────────────────────────────────────────────
        png_path = None
        if not args.no_image:
            png_path = render_png(dxf_path, tmp_dir / "render.png", args.dpi)

        # ── 4. Gemini: components + panel reference ───────────────────────────
        print("\n🤖  Gemini extraction …")
        panel_ref, components = call_gemini_components(
            text_payload, png_path, api_key, debug_dir
        )
        if not components:
            print("⚠  No components extracted. Check sld_debug/gemini_components_raw.txt")
            sys.exit(1)

        # ── 5. Resolve output path ────────────────────────────────────────────
        if args.output:
            out_path = Path(args.output)
        else:
            safe_name = _safe_filename(panel_ref)
            out_path  = out_dir / f"{safe_name}_rawcomponents.xlsx"
        print(f"   Output → {out_path}")

        # ── 6. Group identical components ─────────────────────────────────────
        print("\n🔗  Grouping identical components …")
        components = group_components(components)
        print(f"   {len(components)} unique group(s)")

        # ── 7. Resolve incomer — MUST run before write_excel AND terminals ─────
        # Single authoritative pass: validates/corrects is_incomer so both the
        # Excel file and the terminal calculation see the same correct value.
        # match_offer.py reads is_incomer from the Excel to infer switch families.
        print("\n🔍  Resolving incomer …")
        components = _resolve_incomer(components)

        # ── 8. Terminal schedule ──────────────────────────────────────────────
        print("\n🔌  Calculating terminals …")
        terminals = calculate_terminals(components, incomer_cable_mm2=args.incomer_cable)
        total_t   = sum(t["qty"] for t in terminals)
        print(f"   {len(terminals)} type(s)  |  {total_t} total terminals")

        # ── 9. Copy PNG out before the tempdir closes ─────────────────────────
        final_png = None
        if png_path and png_path.exists():
            final_png = debug_dir / "render.png"
            shutil.copy2(png_path, final_png)

        # ── 10. Write Excel ───────────────────────────────────────────────────
        print("\n📊  Writing Excel …")
        write_excel(
            components, terminals, final_png, out_path,
            panel_ref, dwg_path.name,
            default_mfr=args.manufacturer,
        )

        # ── 11. Write SLD JSON ────────────────────────────────────────────────
        json_path = out_path.with_suffix(".sld.json")
        write_sld_json(components, terminals, panel_ref, dwg_path.name, json_path)

    print(f"\n📁  Raw Gemini response saved in: {debug_dir}/gemini_components_raw.txt")


if __name__ == "__main__":
    main()