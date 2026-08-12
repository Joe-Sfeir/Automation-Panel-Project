import json

from app.domain import Component, Panel, ProjectDocument, stable_id
from app.persistence import load_project, save_project


def test_project_round_trip_and_atomic_save(tmp_path):
    project = ProjectDocument.create("Sample", "S-01")
    project.panels.append(Panel(
        id=stable_id("panel", project.id, "P1"),
        reference="P1",
        source_drawing_id="",
        components=[Component(id="component_1", category="MCB", quantity=3)],
    ))
    destination = tmp_path / "project.technelec.json"
    save_project(project, destination)
    loaded = load_project(destination)
    assert loaded.schema_version == 1
    assert loaded.panels[0].components[0].quantity == 3
    assert not list(tmp_path.glob("*.tmp"))


def test_legacy_sld_migration_separates_terminals(tmp_path):
    source = tmp_path / "legacy.sld.json"
    source.write_text(json.dumps({
        "panel_ref": "DB-1",
        "source_file": "DB-1.dwg",
        "components": [{"category": "MCB", "label": "Q1", "qty": 2}],
        "terminals": [{"name": "PT 4", "description": "Terminal 4mm2", "qty": 4}],
    }), encoding="utf-8")
    project = load_project(source)
    panel = project.panels[0]
    assert panel.components[0].quantity == 2
    assert panel.terminals[0].quantity == 4
    assert all(component.category != "TERMINAL" for component in panel.components)


def test_component_edit_records_override_history():
    component = Component(id="c1", quantity=1, review_status="approved")
    component.edit("quantity", 3, "engineer correction")
    assert component.quantity == 3
    assert component.origin == "manually_overridden"
    assert component.review_status == "needs_review"
    assert component.overrides[0].previous == 1
    assert component.overrides[0].reason == "engineer correction"
