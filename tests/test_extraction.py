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


def test_single_panel_layout_never_creates_components_absent_from_schematic_result():
    service = ExtractionService("unused")
    components = service._components(
        "panel-1", [], _single_panel_graph(), "SCHNEIDER",
        {"cad_bbox": [0, 0, 1, 1], "image_size": [0, 0]},
        supplement_layout=True,
    )

    assert components == []


def test_cross_checks_do_not_rewrite_extracted_rating_groups():
    graph = CadEvidenceGraph("panel.dxf", [
        *[
            item
            for index, amps in enumerate((10, 10, 16, 20))
            for item in (
                CadEntity(f"s:b{index}", "block", layer="SLD", x=index * 100, block_name="CB1"),
                CadEntity(f"s:t{index}", "text", layer="SLD", x=index * 100 + 3, height=2, text=f"2x{amps}"),
            )
        ],
        *[CadEntity(f"l:b{index}", "block", layer="LAYOUT", block_name="2x16") for index in range(4)],
    ])
    service = ExtractionService("unused")
    components = service._components(
        "panel-1",
        [{"category": "MCB", "label": "2x16", "poles": "1P+N", "amps": "16A", "qty": 4}],
        graph, "SCHNEIDER", {"cad_bbox": [0, 0, 400, 1], "image_size": [0, 0]},
        supplement_layout=True,
    )
    ratings = {(component.amps, component.quantity) for component in components if component.category == "MCB"}
    assert ratings == {("16A", 4)}
    sixteen = next(component for component in components if component.category == "MCB" and component.amps == "16A")
    assert any("extracted value preserved" in conflict.lower() for conflict in sixteen.conflicts)


def test_cross_check_cannot_remove_an_extracted_component():
    graph = CadEvidenceGraph("panel.dxf", [
        CadEntity("s:cb", "block", layer="SLD", x=0, y=0, block_name="CB1"),
        CadEntity("s:out", "text", layer="SLD", x=2, y=0, height=1, text="2x16"),
        CadEntity("s:main", "text", layer="SLD", x=2, y=10, height=1, text="MAIN BREAKER 4x40A"),
        CadEntity("l:main", "block", layer="LAYOUT", x=100, y=100, block_name="4x32"),
        *[CadEntity(f"l:out{index}", "block", layer="LAYOUT", block_name="2x16") for index in range(3)],
    ])
    components = ExtractionService("unused")._components(
        "panel-1",
        [{
            "category": "MCB", "label": "4x32", "poles": "4P", "amps": "32A", "qty": 1,
            "evidence_ids": ["l:main", "s:main"],
        }],
        graph, "SCHNEIDER", {"cad_bbox": [0, 0, 100, 100], "image_size": [0, 0]},
        supplement_layout=True,
    )
    assert any(component.category == "MCB" and component.amps == "32A" for component in components)


def test_repeated_label_count_cannot_override_extracted_quantity():
    graph = CadEvidenceGraph("panel.dxf", [
        *[
            CadEntity(f"s:label{index}", "text", layer="SLD", x=index * 10, height=1, text="2x20")
            for index in range(3)
        ],
        CadEntity("s:device", "block", layer="SLD", block_name="CB1"),
        CadEntity("s:helper", "block", layer="SLD", block_name="CB1"),
    ])
    components = ExtractionService("unused")._components(
        "panel-1",
        [{
            "category": "MCB", "label": "2x20", "poles": "1P+N", "amps": "20A",
            "qty": 2, "manufacturer": "LEGRAND",
            "evidence_ids": ["s:device", "s:helper", "s:label0", "s:label1"],
        }],
        graph, "SCHNEIDER", {"cad_bbox": [0, 0, 30, 1], "image_size": [0, 0]},
        supplement_layout=True,
    )
    assert len(components) == 1
    assert components[0].quantity == 2
    assert components[0].manufacturer == "SCHNEIDER"
    assert any("extracted value preserved" in note.lower() for note in components[0].conflicts)


def test_multi_panel_layout_is_not_duplicated_between_panels():
    service = ExtractionService("unused")
    components = service._components(
        "panel-1", [], _single_panel_graph(), "SCHNEIDER",
        {"cad_bbox": [0, 0, 1, 1], "image_size": [0, 0]},
        supplement_layout=False,
    )

    assert components == []


def test_layout_matching_does_not_create_unmatched_protective_specifications():
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
    assert [(component.poles, component.quantity) for component in mcbs] == [("1P+N", 3)]


def test_post_processing_does_not_add_devices_missing_from_model_result():
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

    assert components == []


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
