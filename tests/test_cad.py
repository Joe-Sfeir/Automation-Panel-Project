from app.cad import CadEntity, associate_blocks_to_labels, build_ai_evidence, CadEvidenceGraph, parse_dxf_graph, parse_layout_block_name, schematic_layers, schematic_rating_groups


def test_spatial_association_is_scale_aware_and_deterministic():
    entities = [
        CadEntity("block:1", "block", x=0, y=0, block_name="MCB"),
        CadEntity("block:2", "block", x=100, y=0, block_name="MCB"),
        CadEntity("text:1", "text", x=3, y=0, height=2, text="Q1"),
        CadEntity("text:2", "text", x=97, y=0, height=2, text="Q2"),
    ]
    associations = associate_blocks_to_labels(entities)
    assert [(item.block_id, item.label_id) for item in associations] == [
        ("block:1", "text:1"), ("block:2", "text:2")
    ]
    assert all(item.confidence > 0.5 for item in associations)


def test_association_never_crosses_layers_or_uses_wire_topology():
    entities = [
        CadEntity("block:1", "block", layer="SLD", x=0, y=0),
        CadEntity("text:wrong", "text", layer="LAYOUT", x=0.1, y=0, height=1, text="20A"),
        CadEntity("text:right", "text", layer="SLD", x=3, y=0.1, height=1, text="16A"),
    ]
    result = associate_blocks_to_labels(entities)
    assert result[0].label_id == "text:right"
    assert result[0].topology_bonus == 0.0


def test_dedicated_text_layer_can_label_schematic_block():
    entities = [
        CadEntity("block:1", "block", layer="E-Panelboards", x=0, y=0),
        CadEntity("text:1", "text", layer="E-Panelboards-Text", x=3, y=0, height=1, text="4x25A"),
    ]
    assert associate_blocks_to_labels(entities)[0].label_id == "text:1"


def test_schematic_layer_prefers_label_rich_device_layer():
    entities = []
    for index in range(5):
        entities.append(CadEntity(f"s:b{index}", "block", layer="SLD"))
        entities.append(CadEntity(f"s:t{index}", "text", layer="SLD", text=f"Q{index}"))
        entities.append(CadEntity(f"s:r{index}", "text", layer="SLD", text="16A"))
        entities.append(CadEntity(f"l:b{index}", "block", layer="LAYOUT"))
    assert schematic_layers(entities) == ["SLD"]


def test_physical_layout_counts_are_exposed_as_cross_check():
    entities = []
    for index in range(3):
        entities.extend([
            CadEntity(f"s:b{index}", "block", layer="SLD", block_name="CB1"),
            CadEntity(f"s:t{index}", "text", layer="SLD", text="16A"),
            CadEntity(f"l:b{index}", "block", layer="LAYOUT", block_name="2x16"),
        ])
    evidence = build_ai_evidence(CadEvidenceGraph("test.dxf", entities))
    assert evidence["schematic_layers"] == ["SLD"]
    assert evidence["physical_layout_layers"] == ["LAYOUT"]
    assert evidence["layout_block_groups"][0]["count"] == 3
    assert evidence["layout_device_groups"][0]["commercial_quantity"] == 3
    assert "schematic_blocks" in evidence
    assert "text_clusters" in evidence


def test_explicit_schematic_rating_groups_preserve_rating_split():
    entities = []
    for index, amps in enumerate((10, 10, 16, 20)):
        entities.extend([
            CadEntity(f"b:{index}", "block", layer="SLD", x=index * 100, block_name="CB1"),
            CadEntity(f"t:{index}", "text", layer="SLD", x=index * 100 + 3, height=2, text=f"2x{amps}"),
        ])
    groups = schematic_rating_groups(entities)
    assert [(group["amps"], group["count"]) for group in groups] == [
        ("10A", 2), ("16A", 1), ("20A", 1),
    ]


def test_explicit_rccb_marker_is_paired_with_rating_and_sensitivity():
    entities = [
        CadEntity("b:1", "block", layer="SLD", x=0, y=0, block_name="CB1"),
        CadEntity("t:rating", "text", layer="SLD", x=2, y=0, height=1, text="4x25 AMP"),
        CadEntity("t:sensitivity", "text", layer="SLD", x=2, y=1, height=1, text="30mA"),
        CadEntity("t:marker", "text", layer="SLD", x=2, y=2, height=1, text="ISOL.S.+RCD"),
    ]
    groups = schematic_rating_groups(entities)
    rccb = next(group for group in groups if group["category"] == "RCCB")
    assert (rccb["poles"], rccb["amps"], rccb["sensitivity"], rccb["count"]) == (
        "4P", "25A", "30mA", 1,
    )


def test_layout_block_names_become_general_device_evidence():
    assert parse_layout_block_name("2x16") == {
        "category": "MCB", "poles": "1P+N", "amps": "16A",
        "sensitivity": "-", "quantity_multiplier": 1,
    }
    assert parse_layout_block_name("4X25 30mA")["category"] == "RCCB"
    assert parse_layout_block_name("FUSE 3X32")["quantity_multiplier"] == 3


def test_nested_block_text_is_preserved(tmp_path):
    import ezdxf

    document = ezdxf.new()
    block = document.blocks.new("PANEL_BLOCK")
    block.add_text("ATS-1", dxfattribs={"layer": "SLD", "height": 2})
    document.modelspace().add_blockref("PANEL_BLOCK", (100, 50), dxfattribs={"layer": "SLD"})
    path = tmp_path / "nested.dxf"
    document.saveas(path)

    graph = parse_dxf_graph(path)
    texts = [entity for entity in graph.entities if entity.kind == "text"]
    assert any(entity.text == "ATS-1" and entity.parent_id and entity.layer == "SLD" for entity in texts)
