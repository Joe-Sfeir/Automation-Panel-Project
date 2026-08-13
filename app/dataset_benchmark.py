from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .cad import parse_layout_block_name
from .persistence import atomic_write_json, load_project


def norm(value: object) -> str:
    text = str(value or "").strip().upper()
    if text in {"", "-", "0", "0.0", "NONE", "N/A"}:
        return "-"
    return re.sub(r"[^A-Z0-9.]", "", text)


def panel_key(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper()).replace("R0", "").replace("R1", "")


def _poles(value: object) -> str:
    text = str(value or "").upper().replace(" ", "")
    if re.search(r"1P?\+?N|1PN", text):
        return "1PN"
    if re.search(r"3P?\+?N|3PN", text):
        return "3PN"
    match = re.search(r"([1-4])P", text)
    return f"{match.group(1)}P" if match else norm(value)


def _amps(value: object) -> str:
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*A?\b", str(value or "").upper())
    return f"{match.group(1)}A" if match else norm(value)


def component_key(row: dict[str, Any]) -> tuple[str, ...]:
    category = norm(row.get("category"))
    fields = [category]
    if category in {"MCB", "MCCB", "RCCB", "RCBO", "SWITCH", "CONTACTOR"}:
        fields.extend([_poles(row.get("poles")), _amps(row.get("amps"))])
    if category in {"MCB", "MCCB", "RCBO"}:
        fields.extend([norm(row.get("curve")), norm(row.get("breaking_capacity"))])
    if category in {"RCCB", "RCBO"}:
        fields.append(norm(row.get("sensitivity")))
    if category == "SPD":
        spd = str(row.get("spd_type", "") or "").upper()
        type_match = re.search(r"(?:TYPE|CLASS)\s*([123])\b", spd)
        fields.append(f"TYPE{type_match.group(1)}" if type_match else norm(spd))
    if category == "CT":
        fields.append(norm(row.get("ct_ratio") or row.get("amps")))
    return tuple(fields)


@dataclass
class PanelScore:
    panel_reference: str
    true_positive_quantity: int
    predicted_quantity: int
    expected_quantity: int
    precision: float
    recall: float
    f1: float
    missing: dict[str, int]
    extra: dict[str, int]


def _score(reference: str, predicted: Counter, expected: Counter) -> PanelScore:
    remaining_predicted = Counter(predicted)
    remaining_expected = Counter(expected)
    tp = 0
    # The commercial offer is authoritative for fields it states. A dash in
    # the offer is unknown, not proof that a more specific observed value is
    # wrong. Match the most specific truth rows first and treat unknown fields
    # as wildcards.
    expected_keys = sorted(expected, key=lambda key: sum(value != "-" for value in key), reverse=True)
    for expected_key in expected_keys:
        needed = remaining_expected[expected_key]
        candidates = [
            predicted_key for predicted_key, available in remaining_predicted.items()
            if available > 0 and len(predicted_key) == len(expected_key)
            and all(e == "-" or e == p for e, p in zip(expected_key, predicted_key))
        ]
        candidates.sort(key=lambda key: sum(a == b and a != "-" for a, b in zip(expected_key, key)), reverse=True)
        for predicted_key in candidates:
            allocated = min(needed, remaining_predicted[predicted_key])
            tp += allocated
            needed -= allocated
            remaining_predicted[predicted_key] -= allocated
            remaining_expected[expected_key] -= allocated
            if not needed:
                break
    p_total = sum(predicted.values())
    e_total = sum(expected.values())
    precision = tp / p_total if p_total else float(e_total == 0)
    recall = tp / e_total if e_total else float(p_total == 0)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return PanelScore(
        reference, tp, p_total, e_total, round(precision, 6), round(recall, 6), round(f1, 6),
        {"|".join(key): quantity for key, quantity in remaining_expected.items() if quantity > 0},
        {"|".join(key): quantity for key, quantity in remaining_predicted.items() if quantity > 0},
    )


def _category_counter(rows: Counter) -> Counter:
    result = Counter()
    for key, quantity in rows.items():
        result[(key[0],)] += quantity
    return result


def _key_field_map(key: tuple[str, ...]) -> dict[str, str]:
    category = key[0]
    names = ["category"]
    if category in {"MCB", "MCCB", "RCCB", "RCBO", "SWITCH", "CONTACTOR"}:
        names.extend(["poles", "amps"])
    if category in {"MCB", "MCCB", "RCBO"}:
        names.extend(["curve", "breaking_capacity"])
    if category in {"RCCB", "RCBO"}:
        names.append("sensitivity")
    if category == "SPD":
        names.append("spd_type")
    if category == "CT":
        names.append("ct_ratio")
    return dict(zip(names, key))


def _field_metrics(keys: list[str], predicted_by_panel: dict[str, Counter], expected_by_panel: dict[str, Counter]) -> dict[str, dict]:
    fields = ("poles", "amps", "curve", "breaking_capacity", "sensitivity", "spd_type", "ct_ratio")
    result = {}
    for field in fields:
        tp = predicted_total = expected_total = 0
        for panel in keys:
            predicted = Counter()
            expected = Counter()
            for key, quantity in predicted_by_panel.get(panel, Counter()).items():
                value = _key_field_map(key).get(field, "-")
                if value != "-":
                    predicted[(key[0], value)] += quantity
            for key, quantity in expected_by_panel.get(panel, Counter()).items():
                value = _key_field_map(key).get(field, "-")
                if value != "-":
                    expected[(key[0], value)] += quantity
            tp += sum((predicted & expected).values())
            predicted_total += sum(predicted.values())
            expected_total += sum(expected.values())
        precision = tp / predicted_total if predicted_total else float(expected_total == 0)
        recall = tp / expected_total if expected_total else float(predicted_total == 0)
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        result[field] = {
            "true_positive_quantity": tp,
            "predicted_quantity": predicted_total,
            "expected_quantity": expected_total,
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(f1, 6),
        }
    return result


def _aggregate_for(keys: list[str], predicted_by_panel: dict[str, Counter], expected_by_panel: dict[str, Counter]) -> dict:
    expected = Counter()
    predicted = Counter()
    for key in keys:
        expected.update(expected_by_panel[key])
        predicted.update(predicted_by_panel.get(key, Counter()))
    return {
        "specification": asdict(_score("AGGREGATE", predicted, expected)),
        "category": asdict(_score("AGGREGATE_CATEGORIES", _category_counter(predicted), _category_counter(expected))),
        "fields": _field_metrics(keys, predicted_by_panel, expected_by_panel),
    }


def evaluate_dataset(expected_offer: str | Path, predictions_dir: str | Path, manifest_path: str | Path | None = None) -> dict[str, Any]:
    expected_payload = json.loads(Path(expected_offer).read_text(encoding="utf-8"))
    expected_by_panel: dict[str, Counter] = {}
    expected_refs: dict[str, str] = {}
    for panel in expected_payload.get("panels", []):
        key = panel_key(panel["reference"])
        expected_refs[key] = panel["reference"]
        counts = Counter()
        for row in panel.get("items", []):
            if row.get("scope") == "sld_visible":
                counts[component_key(row)] += int(row.get("quantity", 0))
        expected_by_panel[key] = counts

    predicted_by_panel: dict[str, Counter] = defaultdict(Counter)
    source_files: dict[str, str] = {}
    block_backed_rows = 0
    block_backed_agreeing_rows = 0
    layout_schematic_conflict_rows = 0
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8")) if manifest_path else {}
    drawing_panels = manifest.get("source_drawings", {})
    for project_path in Path(predictions_dir).glob("*/project.technelec.json"):
        project = load_project(project_path)
        expected_for_drawing = []
        if project.source_drawings:
            expected_for_drawing = drawing_panels.get(project.source_drawings[0].filename, [])
        mapped_keys: dict[str, str] = {}
        if len(expected_for_drawing) > 1:
            remaining_panels = list(project.panels)
            remaining_keys = [panel_key(reference) for reference in expected_for_drawing]
            scored = sorted(
                [
                    SequenceMatcher(None, panel_key(panel.reference), candidate).ratio(),
                    panel.id,
                    candidate,
                ]
                for panel in remaining_panels for candidate in remaining_keys
            )
            scored.sort(reverse=True)
            used_panels: set[str] = set()
            used_keys: set[str] = set()
            for _, panel_id, candidate in scored:
                if panel_id in used_panels or candidate in used_keys:
                    continue
                mapped_keys[panel_id] = candidate
                used_panels.add(panel_id)
                used_keys.add(candidate)
        for panel in project.panels:
            if len(expected_for_drawing) == 1:
                key = panel_key(expected_for_drawing[0])
            elif expected_for_drawing:
                key = mapped_keys.get(panel.id, panel_key(panel.reference))
            else:
                key = panel_key(panel.reference)
            source_files[key] = str(project_path)
            for component in panel.components:
                predicted_by_panel[key][component_key(asdict(component))] += component.quantity
                # A layout block may already have been cited by the model and
                # therefore retain source="cad_entity".  Recognise it by its
                # parseable device block name rather than the provenance label
                # assigned when the evidence was first attached.
                layout_evidence = [
                    item for item in component.evidence
                    if item.block_name and parse_layout_block_name(item.block_name)
                ]
                if layout_evidence:
                    preserves_schematic = any(
                        "schematic specification" in conflict.lower()
                        and "preserved" in conflict.lower()
                        for conflict in component.conflicts
                    )
                    if preserves_schematic:
                        layout_schematic_conflict_rows += 1
                        continue
                    by_layer: dict[str, dict[str, int]] = defaultdict(dict)
                    for item in layout_evidence:
                        identity = item.source_id or item.entity_handle
                        parsed = parse_layout_block_name(item.block_name)
                        multiplier = int(parsed["quantity_multiplier"]) if parsed else 1
                        by_layer[item.layer][identity] = multiplier
                    deterministic_quantity = max((sum(values.values()) for values in by_layer.values()), default=0)
                    block_backed_rows += 1
                    block_backed_agreeing_rows += int(deterministic_quantity == component.quantity)

    shared = sorted(expected_by_panel.keys() & predicted_by_panel.keys())
    scores = [_score(expected_refs[key], predicted_by_panel[key], expected_by_panel[key]) for key in shared]
    all_expected_keys = sorted(expected_by_panel)
    aggregate_metrics = _aggregate_for(all_expected_keys, predicted_by_panel, expected_by_panel)
    tuning_keys = [panel_key(value) for value in manifest.get("tuning_panels", []) if panel_key(value) in expected_by_panel]
    holdout_keys = [panel_key(value) for value in manifest.get("holdout_panels", []) if panel_key(value) in expected_by_panel]
    return {
        "aggregate": aggregate_metrics["specification"],
        "category_aggregate": aggregate_metrics["category"],
        "field_aggregate": aggregate_metrics["fields"],
        "block_backed_quantity": {
            "agreeing_rows": block_backed_agreeing_rows,
            "evaluated_rows": block_backed_rows,
            "exact_rate": round(block_backed_agreeing_rows / block_backed_rows, 6) if block_backed_rows else 1.0,
            "layout_schematic_conflict_rows": layout_schematic_conflict_rows,
        },
        "tuning": _aggregate_for(tuning_keys, predicted_by_panel, expected_by_panel) if tuning_keys else {},
        "holdout": _aggregate_for(holdout_keys, predicted_by_panel, expected_by_panel) if holdout_keys else {},
        "panels": [asdict(score) for score in scores],
        "mapped_panels": [expected_refs[key] for key in shared],
        "missing_prediction_panels": [expected_refs[key] for key in sorted(expected_by_panel.keys() - predicted_by_panel.keys())],
        "unexpected_prediction_panels": sorted(predicted_by_panel.keys() - expected_by_panel.keys()),
        "prediction_sources": source_files,
    }


def write_dataset_report(expected_offer: str | Path, predictions_dir: str | Path, output: str | Path, manifest_path: str | Path | None = None) -> dict[str, Any]:
    report = evaluate_dataset(expected_offer, predictions_dir, manifest_path)
    atomic_write_json(output, report)
    return report
