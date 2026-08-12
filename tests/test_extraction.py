import builtins

from app.cad import CadEntity, CadEvidenceGraph
from app.domain import ProjectDocument
from app.extraction import ExtractionService


def _single_panel_graph() -> CadEvidenceGraph:
    entities = []
    for index in range(3):
        entities.extend([
            CadEntity(f"s:b{index}", "block", layer="SLD", block_name="CB"),
            CadEntity(f"s:t{index}", "text", layer="SLD", text="16A"),
            CadEntity(f"l:b{index}", "block", layer="LAYOUT", block_name="2x16"),
        ])
    entities.append(CadEntity("l:fuse", "block", layer="LAYOUT", block_name="FUSE 3X32"))
    return CadEvidenceGraph("panel.dxf", entities)


def test_single_panel_layout_adds_devices_omitted_by_ai():
    service = ExtractionService("unused")
    components = service._components(
        "panel-1", [], _single_panel_graph(), "SCHNEIDER",
        {"cad_bbox": [0, 0, 1, 1], "image_size": [0, 0]},
        supplement_layout=True,
    )

    by_category = {component.category: component for component in components}
    assert by_category["MCB"].quantity == 3
    assert by_category["FUSE_HOLDER"].quantity == 3
    assert all(component.origin == "deterministic_layout" for component in components)
    assert all(component.conflicts for component in components)


def test_multi_panel_layout_is_not_duplicated_between_panels():
    service = ExtractionService("unused")
    components = service._components(
        "panel-1", [], _single_panel_graph(), "SCHNEIDER",
        {"cad_bbox": [0, 0, 1, 1], "image_size": [0, 0]},
        supplement_layout=False,
    )

    assert components == []


def test_layout_matching_uses_poles_to_avoid_duplicate_same_amp_groups():
    graph = _single_panel_graph()
    graph.entities.append(CadEntity("l:four-pole", "block", layer="LAYOUT", block_name="4x16"))
    service = ExtractionService("unused")
    components = service._components(
        "panel-1",
        [{"category": "MCB", "label": "Q1", "poles": "1P+N", "amps": "16A", "qty": 3}],
        graph, "SCHNEIDER", {"cad_bbox": [0, 0, 1, 1], "image_size": [0, 0]},
        supplement_layout=True,
    )

    mcbs = [component for component in components if component.category == "MCB"]
    assert [(component.poles, component.quantity) for component in mcbs] == [("1P+N", 3), ("4P", 1)]


def test_explicit_isolator_and_spd_callouts_are_recovered():
    graph = _single_panel_graph()
    graph.entities.extend([
        CadEntity("s:switch", "block", layer="SLD", x=100, y=0, block_name="SW"),
        CadEntity("s:switch-rating", "text", layer="SLD", x=101, y=0, height=1, text="4x63 Amp"),
        CadEntity("s:spd", "text", layer="SLD", x=110, y=0, height=1, text="SPD"),
        CadEntity("s:spd-type", "text", layer="SLD", x=111, y=0, height=1, text="TYPE 2 / CLASS C"),
    ])
    service = ExtractionService("unused")
    components = service._components(
        "panel-1", [], graph, "SCHNEIDER",
        {"cad_bbox": [0, 0, 120, 1], "image_size": [1200, 100]},
        supplement_layout=True,
    )

    switch = next(component for component in components if component.category == "SWITCH")
    spd = next(component for component in components if component.category == "SPD")
    assert (switch.poles, switch.amps, switch.quantity) == ("4P", "63A", 1)
    assert spd.spd_type == "TYPE 2"


def test_dwg_extraction_does_not_import_legacy_credential_module(tmp_path, monkeypatch):
    drawing = tmp_path / "panel.dwg"
    drawing.write_bytes(b"test dwg")
    project = ProjectDocument.create("Packaged extraction", "TEST")
    graph = CadEvidenceGraph("panel.dxf", [])

    real_import = builtins.__import__

    def reject_legacy_import(name, *args, **kwargs):
        if name == "Codes" or name.startswith("Codes.extract_spatial_json"):
            raise AssertionError("canonical extraction imported the legacy credential module")
        return real_import(name, *args, **kwargs)

    def fake_convert(_drawing, _oda_path, temporary_dir):
        converted = temporary_dir / "panel.dxf"
        converted.write_text("placeholder", encoding="utf-8")
        return converted

    def fake_render(_dxf_path, output_png, _dpi):
        output_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)
        return output_png

    monkeypatch.setattr(builtins, "__import__", reject_legacy_import)
    monkeypatch.setattr("app.extraction.convert_dwg_to_dxf", fake_convert)
    monkeypatch.setattr("app.extraction.render_dxf_png", fake_render)
    monkeypatch.setattr("app.extraction.parse_dxf_graph", lambda _path: graph)
    monkeypatch.setattr(
        "app.extraction.call_gemini_panels",
        lambda *_args: [{"panel_ref": "DB-1", "components": []}],
    )

    panels = ExtractionService("api-key", oda_path="ODAFileConverter.exe").extract(
        project, drawing, tmp_path / "project"
    )

    assert [panel.reference for panel in panels] == ["DB-1"]
    assert project.source_drawings[0].filename == "panel.dwg"
