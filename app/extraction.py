from __future__ import annotations

import tempfile
import json
import re
import struct
from pathlib import Path

from .ai_extraction import call_gemini_panels, sanitize_component
from .cad import CAD_PARSER_VERSION, CadEvidenceGraph, build_ai_evidence, graph_from_dict, layout_device_groups, physical_layout_layers, schematic_layers, parse_dxf_graph
from .cad_runtime import convert_dwg_to_dxf, render_dxf_png
from .domain import Component, Evidence, Panel, ProjectDocument, SourceDrawing, stable_id
from .persistence import atomic_write_json, sha256_file
from .terminals import calculate_terminals


def _normalize(value: object) -> str:
    return "".join(character for character in str(value or "").upper() if character.isalnum())


def _render_transform(graph: CadEvidenceGraph, render_path: Path) -> dict:
    width = height = 0
    try:
        with render_path.open("rb") as handle:
            header = handle.read(24)
        if header[:8] == b"\x89PNG\r\n\x1a\n":
            width, height = struct.unpack(">II", header[16:24])
    except OSError:
        pass
    xs = [entity.x for entity in graph.entities]
    ys = [entity.y for entity in graph.entities]
    bounds = [min(xs), min(ys), max(xs), max(ys)] if xs and ys else [0.0, 0.0, 1.0, 1.0]
    return {
        "cad_bbox": bounds,
        "image_size": [width, height],
        "mapping": "linear x; inverted linear y; full rendered extents",
    }


def _image_bbox(entity, transform: dict) -> list[float]:
    min_x, min_y, max_x, max_y = transform["cad_bbox"]
    width, height = transform["image_size"]
    if width <= 0 or height <= 0 or max_x == min_x or max_y == min_y:
        return []
    x = (entity.x - min_x) / (max_x - min_x) * width
    y = height - (entity.y - min_y) / (max_y - min_y) * height
    return [round(x - 4, 2), round(y - 4, 2), round(x + 4, 2), round(y + 4, 2)]


class ExtractionService:
    def __init__(self, api_key: str, oda_path: str = "", dpi: int = 150, progress=None):
        self.api_key = api_key
        self.oda_path = oda_path or None
        self.dpi = dpi
        self.progress = progress or (lambda message: None)

    def _log(self, message: str) -> None:
        self.progress(message)

    def extract(self, project: ProjectDocument, drawing_path: str | Path, project_dir: str | Path) -> list[Panel]:
        drawing = Path(drawing_path).resolve()
        drawing_hash = sha256_file(drawing)
        drawing_id = stable_id("drawing", drawing_hash)
        existing = next((item for item in project.source_drawings if item.id == drawing_id), None)
        if existing is None:
            project.source_drawings.append(SourceDrawing(
                drawing_id, str(drawing), drawing_hash, drawing.name
            ))
        artifact_dir = Path(project_dir) / "artifacts" / drawing.stem
        artifact_dir.mkdir(parents=True, exist_ok=True)
        debug_dir = artifact_dir / "debug"
        debug_dir.mkdir(exist_ok=True)

        graph_path = artifact_dir / "cad-evidence.json"
        render_path = artifact_dir / "full-sheet.png"
        manifest_path = artifact_dir / "artifact-cache.json"
        cache_valid = False
        if graph_path.exists() and render_path.exists() and manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            cache_valid = (
                manifest.get("drawing_sha256") == drawing_hash
                and int(manifest.get("render_dpi", 0)) == self.dpi
                and int(manifest.get("cad_parser_version", 0)) == CAD_PARSER_VERSION
            )

        if cache_valid:
            self._log("Using cached CAD graph and full-sheet render")
            graph = graph_from_dict(json.loads(graph_path.read_text(encoding="utf-8")))
            rendered = render_path
        else:
            with tempfile.TemporaryDirectory() as temporary_name:
                temporary = Path(temporary_name)
                dxf_path = drawing if drawing.suffix.lower() == ".dxf" else convert_dwg_to_dxf(
                    drawing, self.oda_path, temporary
                )
                self._log("Building deterministic CAD evidence graph")
                graph = parse_dxf_graph(dxf_path)
                atomic_write_json(graph_path, graph.to_dict())
                rendered = render_dxf_png(dxf_path, render_path, self.dpi)
            atomic_write_json(manifest_path, {
                "drawing_sha256": drawing_hash,
                "render_dpi": self.dpi,
                "cad_parser_version": CAD_PARSER_VERSION,
            })

        ai_evidence = build_ai_evidence(graph)
        atomic_write_json(artifact_dir / "ai-evidence.json", ai_evidence)
        transform = _render_transform(graph, render_path)
        atomic_write_json(artifact_dir / "render-metadata.json", transform)
        self._log("Requesting structured multi-panel extraction")
        extracted_panels = call_gemini_panels(ai_evidence, rendered, self.api_key, debug_dir)
        if not extracted_panels:
            raise RuntimeError("No panels were extracted; inspect the drawing debug artifacts")

        panels = []
        for extracted in extracted_panels:
            panel_ref = str(extracted.get("panel_ref", "UNKNOWN") or "UNKNOWN").strip()
            panel_id = stable_id("panel", project.id, drawing_id, panel_ref)
            components = self._components(
                panel_id, extracted.get("components", []), graph,
                project.pricing.default_manufacturer, transform,
                supplement_layout=len(extracted_panels) == 1,
            )
            panel = Panel(
                id=panel_id,
                reference=panel_ref,
                source_drawing_id=drawing_id,
                extraction_status="complete",
                components=components,
                render_paths=[str(render_path)] if render_path.exists() else [],
                debug_paths=[
                    str(graph_path), str(artifact_dir / "ai-evidence.json"),
                    str(artifact_dir / "render-metadata.json"), str(debug_dir),
                ],
                review_status="needs_review",
            )
            panel.terminals = calculate_terminals(panel.components, panel.id)
            panels.append(panel)
        drawing_panel_ids = {panel.id for panel in panels}
        project.panels = [
            panel for panel in project.panels
            if panel.source_drawing_id != drawing_id or panel.id in drawing_panel_ids
        ]
        for panel in panels:
            project.panels = [existing for existing in project.panels if existing.id != panel.id]
            project.panels.append(panel)
        return panels

    def _components(
        self,
        panel_id: str,
        rows: list[dict],
        graph: CadEvidenceGraph,
        default_manufacturer: str,
        render_transform: dict,
        supplement_layout: bool = False,
    ) -> list[Component]:
        entities = {item.id: item for item in graph.entities}
        deterministic_groups = layout_device_groups(graph.entities, physical_layout_layers(graph.entities, schematic_layers(graph.entities)))
        components = []
        matched_layout_blocks: set[str] = set()
        for index, row in enumerate(rows):
            row = sanitize_component(row)
            label = str(row.get("label", "-") or "-")
            category = str(row.get("category", "OTHER") or "OTHER").upper()
            component_id = stable_id(
                "component", panel_id, category, label, row.get("poles", "-"),
                row.get("amps", "-"), row.get("curve", "-"), index,
            )
            evidence = []
            evidence_ids = list(dict.fromkeys(str(value) for value in row.get("evidence_ids", []) if value))
            for evidence_id in evidence_ids:
                entity = entities.get(evidence_id)
                if not entity:
                    continue
                evidence.append(Evidence(
                    source="cad_entity", source_id=entity.id, entity_handle=entity.handle,
                    block_name=entity.block_name, layer=entity.layer, text=entity.text,
                    cad_bbox=[entity.x, entity.y, entity.x + entity.width, entity.y + entity.height],
                    image_bbox=_image_bbox(entity, render_transform),
                    confidence=float(row.get("confidence", 0) or 0),
                ))
            quantity = max(1, int(row.get("qty", 1) or 1))
            conflicts = []
            matches = []
            for group in deterministic_groups:
                category_matches = group["category"] == category
                # Some panel-elevation libraries encode a residual device as
                # a bare `4X16` block. If no residual marker is in the block
                # name, allow its exact poles/rating to corroborate an RCCB
                # that the schematic identifies semantically.
                residual_layout_fallback = (
                    category in {"RCCB", "RCBO", "SWITCH"}
                    and group["category"] == "MCB"
                    and group["poles"] == str(row.get("poles", "-"))
                )
                if not category_matches and not residual_layout_fallback:
                    continue
                if category in {"MCB", "RCCB", "SWITCH"}:
                    if group["amps"] != str(row.get("amps", "-")):
                        continue
                    if group["poles"] != str(row.get("poles", "-")):
                        continue
                    if category == "RCCB" and group["sensitivity"] not in {"-", str(row.get("sensitivity", "-"))}:
                        continue
                elif category == "RCBO":
                    if group["amps"] != str(row.get("amps", "-")) or group["poles"] != str(row.get("poles", "-")):
                        continue
                matches.append(group)
            if len(matches) == 1:
                group = matches[0]
                matched_layout_blocks.update(group["block_ids"])
                deterministic_quantity = int(group["commercial_quantity"])
                for block_id in group["block_ids"]:
                    if block_id in evidence_ids:
                        continue
                    entity = entities[block_id]
                    evidence.append(Evidence(
                        source="layout_inventory", source_id=entity.id, entity_handle=entity.handle,
                        block_name=entity.block_name, layer=entity.layer,
                        cad_bbox=[entity.x, entity.y, entity.x + entity.width, entity.y + entity.height],
                        image_bbox=_image_bbox(entity, render_transform),
                        confidence=1.0,
                    ))
                if deterministic_quantity != quantity:
                    conflicts.append(
                        f"AI quantity {quantity} reconciled to deterministic physical-layout inventory {deterministic_quantity}"
                    )
                    quantity = deterministic_quantity
            cited_blocks = [entities[item.source_id] for item in evidence if entities[item.source_id].kind == "block"]
            blocks_by_layer: dict[str, set[str]] = {}
            for block in cited_blocks:
                blocks_by_layer.setdefault(block.layer, set()).add(block.id)
            # Schematic and physical-layout representations describe the same
            # physical inventory. Never sum them; use the strongest cited
            # per-layer instance count.
            block_evidence_count = max((len(ids) for ids in blocks_by_layer.values()), default=0)
            if not matches and block_evidence_count and block_evidence_count != quantity:
                conflicts.append(
                    f"AI quantity {quantity} reconciled to {block_evidence_count} cited block instances in one representation"
                )
                quantity = block_evidence_count
            confidence = float(row.get("confidence", 0) or 0)
            if not evidence and confidence >= 0.8:
                conflicts.append("High-confidence extraction has no cited CAD evidence")
                confidence = 0.65
            component = Component(
                id=component_id,
                category=category,
                label=label,
                poles=str(row.get("poles", "-") or "-"),
                amps=str(row.get("amps", "-") or "-"),
                curve=str(row.get("curve", "-") or "-"),
                breaking_capacity=str(row.get("breaking_capacity", "-") or "-"),
                sensitivity=str(row.get("sensitivity", "-") or "-"),
                spd_type=str(row.get("spd_type", "-") or "-"),
                ct_ratio=str(row.get("ct_ratio", "-") or "-"),
                meter_accuracy=str(row.get("meter_accuracy", "-") or "-"),
                cable_size=float(row.get("cable_size", 0) or 0),
                load_label=str(row.get("load_label", "") or ""),
                quantity=quantity,
                manufacturer=(
                    str(row.get("manufacturer")).upper()
                    if str(row.get("manufacturer", "-")).strip() not in {"", "-"}
                    else default_manufacturer.upper()
                ),
                has_terminal=bool(row.get("has_terminal", False)),
                is_incomer=bool(row.get("is_incomer", False)),
                confidence=confidence,
                origin=(
                    "deterministic_reconciled" if conflicts
                    else str(row.get("data_source", "ai_extracted") or "ai_extracted")
                ),
                notes=str(row.get("ai_notes", "") or ""),
                evidence=evidence,
                review_status="needs_review",
                conflicts=conflicts,
            )
            components.append(component)

        # A physical panel elevation is an independent, exact inventory of
        # repeated block-backed devices.  For a single-panel drawing it is
        # safe to add groups the vision model omitted; for a multi-panel sheet
        # we leave them unresolved because geometry alone cannot assign a
        # layout block to the correct panel.  Added rows always carry a review
        # conflict, so deterministic presence never becomes an unreviewed
        # commercial assumption.
        if supplement_layout:
            drawing_text = " ".join(
                entity.text for entity in graph.entities if entity.kind in {"text", "attribute"}
            )
            capacity_match = re.search(r"B\.?C\.?:?\s*(\d+(?:\.\d+)?)\s*KA\b", drawing_text, re.I)
            layout_breaking_capacity = f"{capacity_match.group(1)}kA" if capacity_match else "-"
            for group_index, group in enumerate(deterministic_groups):
                if any(block_id in matched_layout_blocks for block_id in group["block_ids"]):
                    continue
                category = group["category"]
                label = str(group.get("block_name") or category)
                evidence = []
                for block_id in group["block_ids"]:
                    entity = entities[block_id]
                    evidence.append(Evidence(
                        source="layout_inventory", source_id=entity.id,
                        entity_handle=entity.handle, block_name=entity.block_name,
                        layer=entity.layer,
                        cad_bbox=[entity.x, entity.y, entity.x + entity.width, entity.y + entity.height],
                        image_bbox=_image_bbox(entity, render_transform), confidence=1.0,
                    ))
                component_id = stable_id(
                    "component", panel_id, "layout", category, group["poles"],
                    group["amps"], group["sensitivity"], group_index,
                )
                components.append(Component(
                    id=component_id,
                    category=category,
                    label=label,
                    poles=group["poles"],
                    amps=group["amps"],
                    breaking_capacity=layout_breaking_capacity if category == "MCB" else "-",
                    sensitivity=group["sensitivity"],
                    quantity=max(1, int(group["commercial_quantity"])),
                    manufacturer=default_manufacturer.upper(),
                    confidence=1.0,
                    origin="deterministic_layout",
                    notes="Recovered from repeated device blocks in the physical panel layout.",
                    evidence=evidence,
                    review_status="needs_review",
                    conflicts=["Physical-layout inventory was missing from structured extraction; verify its specifications."],
                ))

            self._supplement_explicit_schematic_devices(
                panel_id, components, graph, default_manufacturer, render_transform,
            )
        return components

    def _supplement_explicit_schematic_devices(
        self,
        panel_id: str,
        components: list[Component],
        graph: CadEvidenceGraph,
        default_manufacturer: str,
        render_transform: dict,
    ) -> None:
        """Recover plainly labelled one-line devices omitted by the model.

        This intentionally covers only explicit evidence (isolator block labels
        and an SPD text callout).  It does not infer CTs or control gear from
        customary panel practice.
        """
        ai_evidence = build_ai_evidence(graph)
        entities = {item.id: item for item in graph.entities}

        existing_switches = {
            (component.poles, component.amps)
            for component in components if component.category == "SWITCH"
        }
        switch_groups: dict[tuple[str, str], list[str]] = {}
        for row in ai_evidence.get("block_label_associations", []):
            block_name = str(row.get("block_name", "")).upper()
            nearby = [str(row.get("label", "")), *[str(item.get("text", "")) for item in row.get("nearby_texts", [])]]
            if not (
                re.fullmatch(r"(?:SW|D\.S\.|ISOLATOR|SWITCH)", block_name)
                or any("ISOL.S." in value.upper() and "RCD" not in value.upper() for value in nearby)
            ):
                continue
            rating = next((
                re.search(r"\b([1-4])\s*[X×]\s*(\d+(?:\.\d+)?)\s*A?(?:MP)?\b", value, re.I)
                for value in nearby
                if re.search(r"\b([1-4])\s*[X×]\s*(\d+(?:\.\d+)?)\s*A?(?:MP)?\b", value, re.I)
            ), None)
            if not rating:
                continue
            key = (f"{rating.group(1)}P", f"{rating.group(2)}A")
            switch_groups.setdefault(key, []).append(str(row["block_id"]))
        for index, ((poles, amps), block_ids) in enumerate(sorted(switch_groups.items())):
            if (poles, amps) in existing_switches:
                continue
            evidence = [self._entity_evidence(entities[block_id], render_transform) for block_id in block_ids]
            components.append(Component(
                id=stable_id("component", panel_id, "schematic-switch", poles, amps, index),
                category="SWITCH", label=f"Isolator {poles} {amps}", poles=poles, amps=amps,
                quantity=len(block_ids), manufacturer=default_manufacturer.upper(), confidence=1.0,
                origin="deterministic_schematic", evidence=evidence, review_status="needs_review",
                conflicts=["Explicit isolator block was missing from structured extraction; verify its role."],
            ))

        if not any(component.category == "SPD" for component in components):
            spd_entities = [
                item for item in graph.entities
                if item.kind in {"text", "attribute"} and item.text.strip().upper() in {"SPD", "SURGE ARRESTER"}
            ]
            if spd_entities:
                all_text = " ".join(item.text for item in graph.entities if item.kind in {"text", "attribute"})
                type_match = re.search(r"(?:TYPE|CLASS)\s*([123])\b", all_text, re.I)
                components.append(Component(
                    id=stable_id("component", panel_id, "schematic-spd"),
                    category="SPD", label="Surge protective device",
                    spd_type=f"TYPE {type_match.group(1)}" if type_match else "-",
                    quantity=1, manufacturer=default_manufacturer.upper(), confidence=1.0,
                    origin="deterministic_schematic",
                    evidence=[self._entity_evidence(spd_entities[0], render_transform)],
                    review_status="needs_review",
                    conflicts=["Explicit SPD callout was missing from structured extraction; verify its type."],
                ))

    @staticmethod
    def _entity_evidence(entity, render_transform: dict) -> Evidence:
        return Evidence(
            source="cad_entity", source_id=entity.id, entity_handle=entity.handle,
            block_name=entity.block_name, layer=entity.layer, text=entity.text,
            cad_bbox=[entity.x, entity.y, entity.x + entity.width, entity.y + entity.height],
            image_bbox=_image_bbox(entity, render_transform), confidence=1.0,
        )
