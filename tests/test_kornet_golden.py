import json
import re
from collections import Counter
from pathlib import Path

from app.cad import graph_from_dict, schematic_rating_groups
from app.ai_extraction import sanitize_component
from app.accessories import calculate_commercial_accessories
from app.extraction import ExtractionService
from app.multi_panel import deterministic_multi_panel_extraction
from app.terminals import calculate_terminals


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmark_runs" / "kornet_el_hamra"


def test_all_ordinary_kornet_panels_preserve_explicit_outgoing_rating_counts():
    expected = json.loads((BENCHMARK / "expected-offer.json").read_text(encoding="utf-8"))
    manifest = json.loads((BENCHMARK / "benchmark-manifest.json").read_text(encoding="utf-8"))
    truth = {
        panel["reference"]: Counter({
            item["amps"]: int(item["quantity"])
            for item in panel["items"]
            if item.get("scope") == "sld_visible"
            and item.get("category") == "MCB"
            and item.get("poles") == "1P+N"
        })
        for panel in expected["panels"]
    }

    checked = []
    for project_path in sorted((BENCHMARK / "final_hybrid").glob("*/project.technelec.json")):
        project = json.loads(project_path.read_text(encoding="utf-8"))
        filename = project.get("source_drawings", [{}])[0].get("filename", "")
        references = manifest["source_drawings"].get(filename, [])
        if len(references) != 1:
            continue
        graph_paths = list(project_path.parent.glob("artifacts/*/cad-evidence.json"))
        assert graph_paths, f"Missing golden CAD evidence for {filename}"
        graph = graph_from_dict(json.loads(graph_paths[0].read_text(encoding="utf-8")))
        observed = Counter({
            group["amps"]: int(group["count"])
            for group in schematic_rating_groups(graph.entities)
            if group["category"] == "MCB" and group["poles"] == "1P+N"
        })
        reference = references[0]
        assert observed == truth[reference], reference
        checked.append(reference)

    assert len(checked) == 12


def _core_bom_key(item):
    def value(name, default="-"):
        if isinstance(item, dict):
            return str(item.get(name, default) or default).upper()
        return str(getattr(item, name, default) or default).upper()

    quantity = item.get("quantity", 1) if isinstance(item, dict) else item.quantity
    return (
        value("category"), value("poles"), value("amps"), value("curve"),
        value("breaking_capacity"), value("sensitivity"), value("spd_type"),
        value("ct_ratio"), int(quantity),
    )


def _aggregate_core_bom(items):
    result = Counter()
    for item in items:
        key = _core_bom_key(item)
        result[key[:-1]] += key[-1]
    return result


def test_db1_post_processing_preserves_the_structured_extraction_exactly():
    """DB1 guards the recognition→pricing boundary against design mutations."""
    source = BENCHMARK / "final" / "Kornet El Hamra_DB-B1-R0"
    artifact = source / "artifacts" / "Kornet El Hamra_DB-B1-R0"
    graph = graph_from_dict(json.loads((artifact / "cad-evidence.json").read_text(encoding="utf-8")))
    response = json.loads((artifact / "debug" / "gemini_panels_raw.json").read_text(encoding="utf-8"))
    rows = response["panels"][0]["components"]
    components = ExtractionService("unused")._components(
        "db1-checkpoint", rows, graph, "SCHNEIDER",
        {"cad_bbox": [0, 0, 1, 1], "image_size": [0, 0]},
        supplement_layout=True,
    )

    assert len(components) == len(rows)
    fields = (
        "category", "poles", "amps", "curve", "breaking_capacity",
        "sensitivity", "spd_type", "ct_ratio", "quantity",
    )
    for component, raw in zip(components, rows):
        sanitized = sanitize_component(raw)
        expected_values = {
            **{field: sanitized.get(field, "-") for field in fields if field != "quantity"},
            "quantity": max(1, int(sanitized.get("qty", 1) or 1)),
        }
        assert {field: getattr(component, field) for field in fields} == expected_values
        assert component.manufacturer == "SCHNEIDER"
        assert component.review_status == "included"
        assert not component.origin.lower().startswith("engineering")

    assert not any(component.category == "CT" for component in components)
    assert next(item for item in components if item.category == "FUSE_HOLDER").quantity == 1
    assert next(item for item in components if item.category == "RCCB" and item.sensitivity == "300mA").amps == "16A"


def test_complete_kornet_post_processing_never_engineers_the_component_bom():
    service = ExtractionService("unused")
    checked = 0

    for project_path in sorted((BENCHMARK / "final").glob("*/project.technelec.json")):
        artifact_dirs = list(project_path.parent.glob("artifacts/*"))
        assert len(artifact_dirs) == 1
        artifact = artifact_dirs[0]
        graph = graph_from_dict(json.loads((artifact / "cad-evidence.json").read_text(encoding="utf-8")))
        raw = json.loads((artifact / "debug" / "gemini_panels_raw.json").read_text(encoding="utf-8"))
        deterministic = deterministic_multi_panel_extraction(graph)
        extracted_panels = deterministic or raw["panels"]
        for extracted in extracted_panels:
            reference = extracted["panel_ref"]
            components = service._components(
                f"golden-{reference}", extracted.get("components", []), graph, "SCHNEIDER",
                {"cad_bbox": [0, 0, 1, 1], "image_size": [0, 0]},
                supplement_layout=len(extracted_panels) == 1,
            )
            assert all(component.manufacturer == "SCHNEIDER" for component in components), reference
            assert all(component.review_status == "included" for component in components), reference
            assert not any(
                component.origin.lower().startswith("engineering") for component in components
            ), reference
            if not deterministic:
                assert len(components) == len(extracted.get("components", [])), reference
            checked += 1

    assert checked == 17
