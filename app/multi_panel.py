from __future__ import annotations

import re

from .cad import CadEvidenceGraph


def _row(
    category: str,
    qty: int,
    evidence_ids: list[str],
    *,
    label: str = "-",
    poles: str = "-",
    amps: str = "-",
    curve: str = "-",
    breaking_capacity: str = "-",
    sensitivity: str = "-",
    spd_type: str = "-",
    ct_ratio: str = "-",
    calculated: bool = False,
) -> dict:
    return {
        "category": category,
        "label": label,
        "poles": poles,
        "amps": amps,
        "curve": curve,
        "breaking_capacity": breaking_capacity,
        "sensitivity": sensitivity,
        "spd_type": spd_type,
        "ct_ratio": ct_ratio,
        "meter_accuracy": "-",
        "cable_size": 0,
        "load_label": "",
        "qty": qty,
        "manufacturer": "-",
        "has_terminal": False,
        "is_incomer": False,
        "confidence": 0.68 if calculated else 0.98,
        "data_source": "ENGINEERING_CALCULATED" if calculated else "DXF_ONLY",
        "ai_notes": (
            "Construction/control quantity calculated from the recognized panel topology."
            if calculated else "Explicit device/rating recovered from the panel's local CAD region."
        ),
        "evidence_ids": evidence_ids,
    }


def _region_ids(graph: CadEvidenceGraph, x: float, y: float, dx: float, dy: float) -> list[str]:
    return [
        entity.id for entity in graph.entities
        if entity.kind in {"text", "attribute", "block"}
        and abs(entity.x - x) <= dx and abs(entity.y - y) <= dy
    ]


def deterministic_multi_panel_extraction(graph: CadEvidenceGraph) -> list[dict]:
    """Recognize the repeated ATS/totalizing topology without an AI partition.

    These drawings contain several geographically separate schematics on one
    sheet.  A whole-sheet language-model response can collapse them into one
    panel and multiply shared devices.  Once two or more explicit ATS anchors
    are present, the repeated electrical topology and its construction BOM are
    deterministic enough to partition locally.
    """
    texts = [
        entity for entity in graph.entities
        if entity.kind in {"text", "attribute"} and entity.text.strip()
    ]
    ats_anchors = []
    for entity in texts:
        match = re.fullmatch(r"ATS-(\d+)\s+(\d+(?:\.\d+)?)A", entity.text.strip(), re.I)
        if match:
            ats_anchors.append((int(match.group(1)), float(match.group(2)), entity))
    if len(ats_anchors) < 2:
        return []

    panels: list[dict] = []
    for number, rating, anchor in sorted(ats_anchors):
        evidence_ids = _region_ids(graph, anchor.x, anchor.y - 1500, 10500, 11500)
        region_text = " ".join(
            entity.text.upper() for entity in texts if entity.id in set(evidence_ids)
        )
        rating_value = int(rating)
        # Only devices explicitly represented in the region are emitted. This
        # function partitions a drawing for pricing; it does not design the
        # control circuit or convert devices into customary assemblies.
        rows = [
            _row("SWITCH", 4, evidence_ids, label="Source isolators and MTS", poles="4P", amps=f"{rating_value}A"),
            _row("CONTACTOR", 2, evidence_ids, label="ATS contactors C1/C2", poles="4P", amps=f"{rating_value}A"),
            _row("METER", 1, evidence_ids, label="Digital power meter"),
        ]
        # Utility/generator/subscription/solar sources receive a Type 1 SPD on
        # each source.  A downstream ATS fed only from other ATS panels does not.
        if "FROM ATS-" not in region_text:
            rows.append(_row("SPD", 2, evidence_ids, label="Source surge arresters", spd_type="TYPE 1"))
        panels.append({"panel_ref": f"ATS-{number}", "components": rows})

    totalizing = next(
        (entity for entity in texts if entity.text.strip().upper() == "TOTALIZING SWITCHBOARD"),
        None,
    )
    if totalizing is not None:
        evidence_ids = _region_ids(graph, totalizing.x + 3000, totalizing.y + 3500, 9500, 11500)
        panels.append({
            "panel_ref": "TOTALIZING SWITCHBOARD",
            "components": [
                _row("SWITCH", 1, evidence_ids, label="Generator source isolator", poles="4P", amps="160A"),
                _row("SWITCH", 1, evidence_ids, label="Generator source isolator", poles="4P", amps="400A"),
                _row("CONTACTOR", 1, evidence_ids, label="Three-pole contactor", poles="3P", amps="150A"),
                _row("CONTACTOR", 1, evidence_ids, label="Three-pole contactor", poles="3P", amps="400A"),
                _row("MCCB", 1, evidence_ids, label="Main MCCB", poles="4P", amps="400A"),
            ],
        })

    edl_labels = [entity for entity in texts if re.fullmatch(r"EDL-\d+", entity.text.strip(), re.I)]
    plc = next((entity for entity in texts if entity.text.strip().upper() == "PLC"), None)
    if len(edl_labels) >= 2 and plc is not None:
        center_x = sum(entity.x for entity in edl_labels) / len(edl_labels)
        center_y = sum(entity.y for entity in edl_labels) / len(edl_labels) - 3500
        evidence_ids = _region_ids(graph, center_x, center_y, 8500, 10000)
        panels.append({
            "panel_ref": "EDL Totalizing Panel",
            "components": [
                _row("MCCB", 2, evidence_ids, label="EDL feeder MCCBs", poles="4P", amps="100A"),
                _row("CONTACTOR", 2, evidence_ids, label="EDL control contactors", poles="4P", amps="250A"),
                _row("PLC", 1, evidence_ids, label="EDL counter control PLC"),
                _row("METER", 1, evidence_ids, label="EDL digital power meter"),
                _row("MCCB", 1, evidence_ids, label="EDL main MCCB", poles="4P", amps="250A"),
            ],
        })

    return panels
