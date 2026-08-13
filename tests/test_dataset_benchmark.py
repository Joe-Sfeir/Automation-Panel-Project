from collections import Counter

import json

from app.dataset_benchmark import _field_metrics, _score, component_key, evaluate_dataset
from app.domain import Component, Evidence, Panel, ProjectDocument, SourceDrawing
from app.persistence import save_project


def test_offer_unknown_spec_is_a_wildcard_not_a_mismatch():
    expected = Counter({("MCCB", "4P", "100A", "-", "-"): 2})
    predicted = Counter({("MCCB", "4P", "100A", "C", "36KA"): 2})
    score = _score("P1", predicted, expected)
    assert score.true_positive_quantity == 2
    assert score.f1 == 1.0


def test_concrete_offer_spec_must_match():
    expected = Counter({("MCB", "1P", "16A", "C", "6KA"): 1})
    predicted = Counter({("MCB", "1P", "20A", "C", "6KA"): 1})
    assert _score("P1", predicted, expected).f1 == 0.0


def test_spec_normalization_handles_engineering_notation():
    assert component_key({"category": "MCB", "poles": "1P+N", "amps": "16"})[:3] == (
        "MCB", "1PN", "16A"
    )
    assert component_key({"category": "SPD", "spd_type": "Type 2 / Class C, Imax 20kA"}) == (
        "SPD", "TYPE2"
    )


def test_field_metrics_do_not_turn_one_missing_field_into_a_total_row_failure():
    expected = {"P1": Counter({("MCB", "1PN", "16A", "C", "6KA"): 2})}
    predicted = {"P1": Counter({("MCB", "1PN", "16A", "-", "6KA"): 2})}
    fields = _field_metrics(["P1"], predicted, expected)

    assert fields["amps"]["f1"] == 1.0
    assert fields["breaking_capacity"]["f1"] == 1.0
    assert fields["curve"]["recall"] == 0.0


def test_layout_schematic_conflict_is_not_counted_as_layout_quantity_failure(tmp_path):
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps({
        "panels": [{"reference": "P1", "items": [{
            "scope": "sld_visible", "category": "MCB", "poles": "1P+N",
            "amps": "16A", "curve": "-", "breaking_capacity": "-", "quantity": 2,
        }]}]
    }), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"source_drawings": {"panel.dwg": ["P1"]}}), encoding="utf-8")
    project = ProjectDocument.create("Test")
    project.source_drawings.append(SourceDrawing("d", "panel.dwg", "", "panel.dwg"))
    project.panels.append(Panel("p", "P1", "d", components=[Component(
        "c", category="MCB", poles="1P+N", amps="16A", quantity=2,
        evidence=[Evidence("layout_inventory", "b1", block_name="2x16", layer="LAYOUT")],
        conflicts=["Physical-layout inventory 3 disagrees with explicit schematic specification count 2; schematic specification preserved"],
    )]))
    run = tmp_path / "run" / "drawing"
    save_project(project, run / "project.technelec.json")

    report = evaluate_dataset(expected, tmp_path / "run", manifest)

    assert report["block_backed_quantity"]["evaluated_rows"] == 0
    assert report["block_backed_quantity"]["layout_schematic_conflict_rows"] == 1
