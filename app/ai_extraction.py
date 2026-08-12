from __future__ import annotations

import json
import os
import re
import hashlib
import time
from pathlib import Path

from pydantic import BaseModel, Field


class ExtractedComponent(BaseModel):
    category: str = Field(description="Observed electrical device category")
    label: str = Field(description="Observed model/block label, or '-' when absent")
    poles: str = Field(description="Pole configuration, or '-'")
    amps: str = Field(description="Rated current only, or '-'")
    curve: str = Field(description="B/C/D only for overcurrent breakers, or '-'")
    breaking_capacity: str = Field(description="Breaking capacity only for breakers, or '-'")
    sensitivity: str = Field(description="Residual sensitivity only for RCCB/RCBO, or '-'")
    spd_type: str = Field(description="SPD type/Up/Imax only for SPD devices, or '-'")
    ct_ratio: str = Field(description="CT primary/secondary ratio only, e.g. 80/5, or '-'")
    meter_accuracy: str = Field(description="Meter accuracy class only, e.g. Class 1.0, or '-'")
    cable_size: float = Field(description="Observed phase conductor cross-section in mm2, 0 when absent")
    load_label: str = Field(description="Observed downstream circuit/load names")
    qty: int = Field(description="Number of physical device instances in this group")
    manufacturer: str = Field(description="Observed manufacturer, or project default when not visible")
    has_terminal: bool
    is_incomer: bool
    confidence: float = Field(ge=0, le=1)
    data_source: str = Field(description="BOTH, DXF_ONLY, or IMAGE_ONLY")
    ai_notes: str
    evidence_ids: list[str] = Field(description="CAD entity IDs supporting this row")


class ExtractedPanel(BaseModel):
    panel_ref: str
    components: list[ExtractedComponent]


class DrawingExtraction(BaseModel):
    panels: list[ExtractedPanel]


PROMPT = """
You are extracting observable electrical devices from a CAD single-line diagram (SLD).
Return every distinct panel found in the drawing. A drawing may contain one panel, several
panels, or several repeated panel schematics.

EVIDENCE RULES
- Use only devices physically supported by the supplied CAD evidence or rendered drawing.
- Never add a device merely because it is customary or normally required. In particular,
  do not invent CTs, meters, fuses, terminals, control gear, or accessories.
- The drawing may also contain a physical panel-layout/elevation that duplicates devices.
  `layout_block_groups` is an independent inventory/count cross-check. Use recognizable
  rating/device block groups to confirm or correct schematic quantities, but never add the
  layout and schematic counts together. Cite the representation supporting the final count.
- `block_label_associations` links individual schematic block IDs to nearby labels. Include
  every supporting block/text ID in `evidence_ids`. For grouped rows include all physical
  instance block IDs. Do not reuse one block ID in two different component groups.
- Association hints are geometric evidence, not infallible electrical topology. If text is
  ambiguous, lower confidence and explain it instead of guessing.
- Do not use raw block-name totals as a rating-group quantity when a generic symbol such as
  CB1 is reused for several ratings. Group per-instance associations by observed specs.
- A layout block name such as `2x10`, `4X16 30mA`, `POWER METER`, or `indicator lamp red`
  is meaningful inventory evidence. Its `count` is deterministic for that representation.

FIELD RULES
- `amps` is rated current only. Do not put SPD kA/Imax, CT ratios, or meter accuracy in it.
- `ct_ratio` is used only for CTs; `meter_accuracy` only for meters; `spd_type` only for SPDs.
- Use '-' for absent or inapplicable string fields and 0 for absent cable_size.
- Use breaker curve and breaking capacity only when written globally or locally.
- Do not default curve C unless it is explicitly printed in the drawing.
- A schematic notation such as `2x10` next to `(1P+N)` means a 10A 1P+N
  protective device. The leading 2 describes switched conductors/modules; it
  does not mean a 2P device. Preserve the explicit `(1P+N)` notation.
- `confidence=1.0` requires unambiguous agreement between CAD text/blocks and the image.
  Inferred, image-only, or ambiguous values must be below 0.8 and require review.
- Terminals and enclosure/plastron/rail/door items are calculated/commercial outputs and
  must not be returned as extracted components.

CATEGORIES
MCB, MCCB, RCCB, RCBO, SWITCH, CONTACTOR, SPD, FUSE_HOLDER, PILOT_LAMP,
METER, CT, RELAY, SELECTOR_SWITCH, TIMER, PLC, POWER_SUPPLY, OTHER.

Return one `DrawingExtraction` object conforming exactly to the response schema.
"""

PROMPT_VERSION = "sld-evidence-v3"


def model_name() -> str:
    configured = os.getenv("GEMINI_API_MODEL", "").strip()
    if configured:
        return configured
    env_file = Path(".venv/key.env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("GEMINI_API_MODEL="):
                configured = line.split("=", 1)[1].strip()
                if configured:
                    return configured
    return "gemini-3.6-flash"


def fallback_model_name() -> str:
    configured = os.getenv("GEMINI_FALLBACK_MODEL", "").strip()
    if configured:
        return configured
    env_file = Path(".venv/key.env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("GEMINI_FALLBACK_MODEL="):
                configured = line.split("=", 1)[1].strip()
                if configured:
                    return configured
    return "gemini-3.1-flash-lite"


def call_gemini_panels(evidence: dict, image_path: Path | None, api_key: str, debug_dir: Path) -> list[dict]:
    from google import genai
    from google.genai import types

    evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    selected_model = model_name()
    fallback_model = fallback_model_name()
    model_candidates = list(dict.fromkeys(model for model in (selected_model, fallback_model) if model))
    signature = hashlib.sha256()
    signature.update(PROMPT_VERSION.encode("utf-8"))
    signature.update(selected_model.encode("utf-8"))
    signature.update("|".join(model_candidates).encode("utf-8"))
    signature.update(PROMPT.encode("utf-8"))
    signature.update(evidence_json.encode("utf-8"))
    if image_path and image_path.exists():
        signature.update(image_path.read_bytes())
    request_hash = signature.hexdigest()
    debug_dir.mkdir(parents=True, exist_ok=True)
    raw_path = debug_dir / "gemini_panels_raw.json"
    cache_path = debug_dir / "gemini_panels_cache.json"
    if raw_path.exists() and cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if cache.get("request_sha256") == request_hash:
            payload = json.loads(raw_path.read_text(encoding="utf-8"))
            return list(payload.get("panels", []))

    contents: list = [PROMPT, evidence_json]
    if image_path and image_path.exists():
        contents.append(types.Part.from_bytes(data=image_path.read_bytes(), mime_type="image/png"))
    client = genai.Client(api_key=api_key)
    response = None
    actual_model = selected_model
    last_error: Exception | None = None
    for model_index, candidate_model in enumerate(model_candidates):
        actual_model = candidate_model
        for attempt in range(4):
            try:
                response = client.models.generate_content(
                    model=candidate_model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=DrawingExtraction,
                        http_options=types.HttpOptions(timeout=180_000),
                    ),
                )
                break
            except Exception as exc:
                last_error = exc
                message = str(exc)
                transient = (
                    "429" in message
                    or "RESOURCE_EXHAUSTED" in message
                    or "503" in message
                    or "UNAVAILABLE" in message
                    or "high demand" in message.lower()
                )
                if not transient:
                    raise
                hard_quota = "limit: 0" in message.lower() or "perday" in message.lower()
                if hard_quota and model_index + 1 < len(model_candidates):
                    break
                if attempt >= 3:
                    if model_index + 1 < len(model_candidates):
                        break
                    raise
                retry = re.search(r"retryDelay[^0-9]*(\d+(?:\.\d+)?)s", message, re.I)
                delay = min(55.0, max(2.0, float(retry.group(1)) + 1 if retry else 15.0 * (attempt + 1)))
                time.sleep(delay)
        if response is not None:
            break
    if response is None:
        if last_error is not None:
            raise last_error
        raise RuntimeError("Gemini extraction returned no response")
    raw = response.text.strip()
    raw_path.write_text(raw, encoding="utf-8")
    cache_path.write_text(json.dumps({
        "prompt_version": PROMPT_VERSION,
        "requested_model": selected_model,
        "model": actual_model,
        "request_sha256": request_hash,
    }, indent=2), encoding="utf-8")
    payload = json.loads(raw)
    return list(payload.get("panels", []))


def sanitize_component(row: dict) -> dict:
    result = dict(row)
    category = str(result.get("category", "OTHER") or "OTHER").upper().strip()
    result["category"] = category

    def clean(field: str) -> None:
        value = str(result.get(field, "-") or "-").strip()
        result[field] = "-" if value.upper() in {"", "0", "0.0", "NONE", "N/A"} else value

    for field in (
        "label", "poles", "amps", "curve", "breaking_capacity", "sensitivity",
        "spd_type", "ct_ratio", "meter_accuracy", "load_label", "manufacturer", "ai_notes",
    ):
        clean(field)
    amps = str(result.get("amps", "-")).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", amps):
        result["amps"] = f"{amps}A"
    poles = str(result.get("poles", "-")).upper().replace(" ", "")
    if poles in {"1P+N", "1PN", "1P/N"}:
        result["poles"] = "1P+N"
    elif re.fullmatch(r"[1-4]P", poles):
        result["poles"] = poles
    if category not in {"MCB", "MCCB", "RCBO"}:
        result["curve"] = "-"
        result["breaking_capacity"] = "-"
    elif result["curve"].upper() not in {"B", "C", "D"}:
        result["curve"] = "-"
    if category not in {"RCCB", "RCBO"}:
        result["sensitivity"] = "-"
    if category != "SPD":
        result["spd_type"] = "-"
    if category != "CT":
        result["ct_ratio"] = "-"
    if category != "METER":
        result["meter_accuracy"] = "-"
    if category in {"SPD", "CT", "METER", "PILOT_LAMP", "FUSE_HOLDER", "RELAY", "SELECTOR_SWITCH", "TIMER", "PLC", "POWER_SUPPLY"}:
        result["amps"] = "-"
    if category in {"CT", "METER", "PILOT_LAMP", "RELAY", "SELECTOR_SWITCH", "TIMER", "PLC", "POWER_SUPPLY"}:
        result["poles"] = "-"
    manufacturer = str(result.get("manufacturer", "-") or "-").strip().upper()
    # Manufacturer is a pricing constraint, so a misplaced specification or
    # device label must never be allowed to masquerade as a brand.  Keep only
    # recognisable electrical manufacturers; an unknown value falls back to
    # the project's selected default later in the pipeline and remains
    # editable in review.
    manufacturer_aliases = {
        "SCHNEIDER ELECTRIC": "SCHNEIDER",
        "SCHNEIDER": "SCHNEIDER",
        "MERLIN GERIN": "SCHNEIDER",
        "LEGRAND": "LEGRAND",
        "EATON": "EATON",
        "MOELLER": "EATON",
        "ABB": "ABB",
        "SIEMENS": "SIEMENS",
        "PHOENIX CONTACT": "PHOENIX CONTACT",
        "PHOENIX": "PHOENIX CONTACT",
        "DEHN": "DEHN",
        "SOCOMEC": "SOCOMEC",
        "LOVATO": "LOVATO",
        "FINDER": "FINDER",
        "OMRON": "OMRON",
    }
    result["manufacturer"] = manufacturer_aliases.get(manufacturer, "-")
    if category == "CT" and result["ct_ratio"] == "-":
        ratio = re.search(r"\b\d+\s*/\s*\d+\b", str(row.get("amps", "")))
        if ratio:
            result["ct_ratio"] = ratio.group(0).replace(" ", "")
    return result
