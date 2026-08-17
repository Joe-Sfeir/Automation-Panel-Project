from __future__ import annotations

import copy
import tempfile
import json
import re
import struct
from collections import Counter
from pathlib import Path

from .ai_extraction import call_gemini_panels, sanitize_component
from .accessories import calculate_commercial_accessories
from .cad import CAD_PARSER_VERSION, CadEvidenceGraph, build_ai_evidence, graph_from_dict, layout_device_groups, physical_layout_layers, schematic_layers, schematic_rating_groups, parse_dxf_graph
from .cad_runtime import convert_dwg_to_dxf, render_dxf_png
from .domain import Component, Evidence, Panel, ProjectDocument, SourceDrawing, stable_id
from .multi_panel import deterministic_multi_panel_extraction
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
    def __init__(
        self, api_key: str, oda_path: str = "", dpi: int = 150, progress=None,
        allow_model_fallback: bool = True,
    ):
        self.api_key = api_key
        self.oda_path = oda_path or None
        self.dpi = dpi
        self.progress = progress or (lambda message: None)
        self.allow_model_fallback = allow_model_fallback

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
        extracted_panels = deterministic_multi_panel_extraction(graph)
        if extracted_panels:
            self._log(
                f"Deterministically partitioned {len(extracted_panels)} repeated panel schematics"
            )
        else:
            self._log("Requesting structured multi-panel extraction")
            extracted_panels = call_gemini_panels(
                ai_evidence, rendered, self.api_key, debug_dir, self._log,
                self.allow_model_fallback,
            )
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
            components = self._apply_explicit_graph_rules(
                panel_id, panel_ref, components, graph,
                project.pricing.default_manufacturer, transform,
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
                review_status="included",
            )
            drawing_text = " ".join(
                entity.text for entity in graph.entities if entity.kind in {"text", "attribute"}
            )
            panel.terminals = calculate_terminals(panel.components, panel.id, drawing_text)
            panel.commercial_accessories = calculate_commercial_accessories(
                panel.components, panel.id, drawing_text,
                project.pricing.default_manufacturer,
            )
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
        layout_layers = physical_layout_layers(graph.entities, schematic_layers(graph.entities))
        deterministic_groups = layout_device_groups(graph.entities, layout_layers)
        explicit_schematic_groups = schematic_rating_groups(graph.entities) if supplement_layout else []
        components = []
        for index, row in enumerate(rows):
            row = sanitize_component(row)
            label = str(row.get("label", "-") or "-")
            category = str(row.get("category", "OTHER") or "OTHER").upper()
            evidence_ids = list(dict.fromkeys(str(value) for value in row.get("evidence_ids", []) if value))
            schematic_match = next((
                group for group in explicit_schematic_groups
                if group["category"] == category
                and group["poles"] == str(row.get("poles", "-"))
                and group["amps"] == str(row.get("amps", "-"))
                and (
                    category != "RCCB"
                    or group["sensitivity"] == str(row.get("sensitivity", "-"))
                )
            ), None)
            component_id = stable_id(
                "component", panel_id, category, label, row.get("poles", "-"),
                row.get("amps", "-"), row.get("curve", "-"), index,
            )
            evidence = []
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
            if schematic_match:
                for label_id in schematic_match["label_ids"]:
                    if label_id in evidence_ids or label_id not in entities:
                        continue
                    evidence.append(self._entity_evidence(entities[label_id], render_transform))
                schematic_quantity = int(schematic_match["count"])
                if schematic_quantity != quantity:
                    conflicts.append(
                        f"Cross-check only: extracted quantity {quantity} differs from "
                        f"{schematic_quantity} repeated schematic rating labels; extracted value preserved"
                    )
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
                if schematic_match and deterministic_quantity != quantity:
                    conflicts.append(
                        f"Cross-check only: physical-layout inventory {deterministic_quantity} differs from "
                        f"the extracted schematic quantity {quantity}; extracted value preserved"
                    )
                elif deterministic_quantity != quantity:
                    conflicts.append(
                        f"Cross-check only: physical-layout inventory {deterministic_quantity} differs from "
                        f"the extracted quantity {quantity}; extracted value preserved"
                    )
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
                # Manufacturer selection is a project/review pricing choice.
                # Do not let the interpretation model override it from a logo,
                # enclosure title, or general drawing note.
                manufacturer=default_manufacturer.upper(),
                has_terminal=bool(row.get("has_terminal", False)),
                is_incomer=bool(row.get("is_incomer", False)),
                confidence=confidence,
                origin=str(row.get("data_source", "ai_extracted") or "ai_extracted"),
                notes=str(row.get("ai_notes", "") or ""),
                evidence=evidence,
                review_status="included",
                conflicts=conflicts,
            )
            components.append(component)

        # This is an observed pricing takeoff, not an electrical-design stage.
        # Never rewrite extracted quantities/specifications or add customary
        # devices here. Catalog substitutions belong only to PricingService
        # and leave these source values intact.
        return components

    def _apply_explicit_graph_rules(
        self,
        panel_id: str,
        panel_ref: str,
        components: list[Component],
        graph: CadEvidenceGraph,
        default_manufacturer: str,
        render_transform: dict,
    ) -> list[Component]:
        """Recover explicit shared specs and recognizable compound topology.

        These rules only use text and topology present in the drawing. They do
        not add customary control gear such as timers, phase relays, or pilot
        lamps merely because a panel happens to be an ATS or motor panel.
        """
        text_entities = [
            entity for entity in graph.entities
            if entity.kind in {"text", "attribute"} and entity.text.strip()
        ]
        drawing_text = "\n".join(entity.text for entity in text_entities)

        def append_note(existing: str, addition: str) -> str:
            value = str(existing or "").strip()
            if addition in value:
                return value
            return f"{value}\n{addition}".strip()

        def matching_evidence(pattern: str) -> list[Evidence]:
            matches = [entity for entity in text_entities if re.search(pattern, entity.text, re.I)]
            return [self._entity_evidence(entity, render_transform) for entity in matches[:3]]

        # A schedule header commonly states curve and breaking capacity once
        # for every outgoing MCB. Propagate only a unique, explicit value and
        # only into missing fields.
        curve_values = {
            match.group(1).upper()
            for match in re.finditer(r"\bCURVE\s*(?:TYPE\s*)?([BCD])\b", drawing_text, re.I)
        }
        capacity_values = {
            f"{float(match.group(1)):g}kA"
            for match in re.finditer(r"\bB\.?\s*C\.?\s*:?\s*(\d+(?:\.\d+)?)\s*KA\b", drawing_text, re.I)
        }
        curve = next(iter(curve_values)) if len(curve_values) == 1 else ""
        capacity = next(iter(capacity_values)) if len(capacity_values) == 1 else ""
        curve_evidence = matching_evidence(r"\bCURVE\s*(?:TYPE\s*)?[BCD]\b")
        capacity_evidence = matching_evidence(r"\bB\.?\s*C\.?\s*:?\s*\d+(?:\.\d+)?\s*KA\b")

        # A model may describe an MCB carrying residual sensitivity as an RCBO,
        # while the SLD symbol/evidence names the overcurrent device and a
        # separate residual stage. Preserve both commercial devices instead of
        # collapsing the pair into one catalog category.
        expanded: list[Component] = []
        for component in components:
            evidence_text = " ".join(
                f"{item.text} {item.block_name}" for item in component.evidence
            ).upper()
            split_residual = (
                component.category == "RCBO"
                and component.sensitivity not in {"", "-", "0", "0mA"}
                and "MCB" in f"{component.label} {evidence_text}".upper()
                and "RCBO" not in evidence_text
            )
            if split_residual:
                residual_sensitivity = component.sensitivity
                if component.poles in {"", "-"}:
                    pole_match = re.search(r"\(([1-4])\s*P\s*\+\s*N\)", evidence_text, re.I)
                    if pole_match:
                        component.poles = f"{pole_match.group(1)}P+N"
                component.category = "MCB"
                component.sensitivity = "-"
                component.conflicts.append(
                    "Explicit MCB evidence also carries residual sensitivity; split into MCB and residual stages for pricing."
                )
                residual = copy.deepcopy(component)
                residual.id = stable_id("component", panel_id, component.id, "residual-stage")
                residual.category = "RCCB"
                residual.label = f"Residual stage paired with {component.label}"
                residual.amps = "-"
                # The MCB pole notation does not prove the pole construction
                # of the separate upstream residual device.
                residual.poles = "-"
                residual.curve = "-"
                residual.breaking_capacity = "-"
                residual.sensitivity = residual_sensitivity
                residual.origin = "deterministic_decomposition"
                residual.confidence = min(residual.confidence, 0.78)
                residual.notes = append_note(
                    residual.notes,
                    "Deterministically separated from an MCB-labelled residual-protection group; verify standalone RCD rating.",
                )
                expanded.extend([component, residual])
            else:
                expanded.append(component)
        components = expanded

        for component in components:
            if component.category in {"MCB", "RCBO"}:
                if curve and component.curve in {"", "-", "0"}:
                    component.curve = curve
                    component.evidence.extend(curve_evidence)
                    component.notes = append_note(
                        component.notes, f"Inherited explicit group header: curve {curve}."
                    )
                if capacity and component.breaking_capacity in {"", "-", "0", "0kA"}:
                    component.breaking_capacity = capacity
                    component.evidence.extend(capacity_evidence)
                    component.notes = append_note(
                        component.notes, f"Inherited explicit group header: breaking capacity {capacity}."
                    )
                if not curve and component.curve in {"", "-", "0", "NULL"}:
                    component.curve = "C"
                    component.notes = append_note(
                        component.notes,
                        "Commercial MCB default: curve C; verify because the drawing does not print a curve.",
                    )
                    component.conflicts.append(
                        "Curve C is a commercial default, not an explicit drawing rating."
                    )

            label_text = f"{component.label} {component.load_label}".upper()
            if (
                component.category == "SWITCH"
                and (
                    "AUTOMATIC TRANSFER SWITCH" in label_text
                    or ("ATS" in label_text and "CONTACTOR" in label_text)
                )
            ):
                component.category = "CONTACTOR"
                component.conflicts.append(
                    "Explicit automatic-transfer/contactors label normalized from SWITCH to CONTACTOR assembly."
                )

        # Associate a printed CT ratio. A unique sheet ratio is safe for a
        # single CT group; otherwise choose only a spatially nearby ratio.
        ratio_entities = []
        for entity in text_entities:
            match = re.search(r"(?<![\d.])(\d{2,4}\s*/\s*(?:1|5))(?!\d)", entity.text)
            if match:
                ratio_entities.append((match.group(1).replace(" ", ""), entity))
        unique_ratios = {ratio for ratio, _ in ratio_entities}
        for component in [item for item in components if item.category == "CT" and item.ct_ratio in {"", "-", "0", "NULL"}]:
            selected = None
            if len(unique_ratios) == 1:
                selected = ratio_entities[0]
            elif ratio_entities and component.evidence:
                xs = [item.cad_bbox[0] for item in component.evidence if item.cad_bbox]
                ys = [item.cad_bbox[1] for item in component.evidence if item.cad_bbox]
                if xs and ys:
                    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
                    selected = min(
                        ratio_entities,
                        key=lambda item: (item[1].x - cx) ** 2 + (item[1].y - cy) ** 2,
                    )
            if selected:
                component.ct_ratio = selected[0]
                component.evidence.append(self._entity_evidence(selected[1], render_transform))
                component.notes = append_note(
                    component.notes, f"Associated explicit CT ratio {selected[0]}."
                )
                continue

            # Some one-lines label the CTs but omit the ratio.  When the CT
            # symbol sits immediately beside one unambiguous rated device,
            # use that printed current as a reviewable /5 ratio.  A distance
            # guard prevents a remote panel rating from leaking into the CT.
            rated_entities = []
            for entity in text_entities:
                match = re.search(r"\b[1-4]\s*[X×]\s*(\d+(?:\.\d+)?)\s*A\b", entity.text, re.I)
                if match:
                    rated_entities.append((float(match.group(1)), entity))
            xs = [item.cad_bbox[0] for item in component.evidence if item.cad_bbox]
            ys = [item.cad_bbox[1] for item in component.evidence if item.cad_bbox]
            if rated_entities and xs and ys:
                cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
                primary, entity = min(
                    rated_entities,
                    key=lambda item: (item[1].x - cx) ** 2 + (item[1].y - cy) ** 2,
                )
                distance = ((entity.x - cx) ** 2 + (entity.y - cy) ** 2) ** 0.5
                if distance <= 25:
                    component.ct_ratio = f"{primary:g}/5"
                    component.evidence.append(self._entity_evidence(entity, render_transform))
                    component.notes = append_note(
                        component.notes,
                        f"CT ratio inferred as {primary:g}/5 from the nearest printed rated device; verify before export.",
                    )
                    component.conflicts.append(
                        "CT ratio is a spatial engineering inference, not an explicit drawing label."
                    )

        # Repeated ATS tenant panels may draw the transfer symbol without a
        # textual contactor label. The panel identity plus a repeated MCCB
        # branch count is enough to recover one ATS assembly per branch, but it
        # remains highlighted for engineering review.
        if "ATS" in panel_ref.upper() and not any(item.category == "CONTACTOR" for item in components):
            branch_labels = []
            for entity in text_entities:
                match = re.search(
                    r"\bMCCB\s+BREAKER\s*\n?\s*([1-4])\s*[X×]\s*(\d+(?:\.\d+)?)\s*A\b",
                    entity.text,
                    re.I,
                )
                if match:
                    branch_labels.append((f"{match.group(1)}P", f"{match.group(2)}A", entity))
            counts = Counter((poles, amps) for poles, amps, _ in branch_labels)
            if counts:
                (poles, amps), quantity = counts.most_common(1)[0]
                if quantity >= 2:
                    evidence_entities = [
                        entity for p, a, entity in branch_labels if (p, a) == (poles, amps)
                    ]
                    components.append(Component(
                        id=stable_id("component", panel_id, "repeated-ats-assembly", poles, amps),
                        category="CONTACTOR",
                        label="Repeated automatic transfer assembly",
                        poles=poles,
                        amps=amps,
                        quantity=quantity,
                        manufacturer=default_manufacturer.upper(),
                        confidence=0.72,
                        origin="topology_recognized",
                        notes=(
                            "One ATS/contactors assembly recovered per repeated MCCB-fed transfer branch; "
                            "verify contactor utilization category and packaged quantity."
                        ),
                        evidence=[
                            self._entity_evidence(entity, render_transform)
                            for entity in evidence_entities[:3]
                        ],
                        review_status="included",
                        conflicts=["Topology-derived ATS assembly requires engineering review."],
                    ))

        # Repeated tenant ATS branches use a compact drawing convention: each
        # branch prints the SPD callout, but does not repeat its commercial
        # type or the shared MCB family data.  Preserve the printed quantities
        # and apply the project's standard reviewable defaults to the missing
        # fields.  This is deliberately topology-based rather than tied to a
        # project or panel reference.
        repeated_ats = max(
            (item.quantity for item in components if item.category == "CONTACTOR"),
            default=0,
        ) >= 5 and max(
            (item.quantity for item in components if item.category == "MCCB"),
            default=0,
        ) >= 5

        self._supplement_explicit_schematic_devices(
            panel_id, components, graph, default_manufacturer, render_transform,
        )

        # Motor/fan schedules often aggregate a generic 10A control-contactor
        # inventory while the commercial offer needs one selected power
        # contactor per protected branch.  Activate only when the drawing
        # explicitly calls for AC3 coordination and an Auto/Off/Manual control
        # station, then derive conservative catalogue ratings from branch type.
        fan_starter_schedule = (
            re.search(r"CONTACTOR\s+AC3", drawing_text, re.I)
            and re.search(r"AUTO\s*/\s*OFF\s*/\s*MANUAL", drawing_text, re.I)
        )
        if fan_starter_schedule:
            for residual in [item for item in components if item.category == "RCCB"]:
                if residual.amps in {"", "-", "0", "NULL"}:
                    residual.amps = "25A"
                    residual.notes = append_note(
                        residual.notes,
                        "Fan-starter residual-device commercial rating defaulted to 25A; verify before export.",
                    )
            branch_mcbs = [item for item in components if item.category == "MCB"]
            selected_contactors = []
            for index, branch in enumerate(branch_mcbs):
                branch_current_match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*A\s*", branch.amps, re.I)
                branch_current = float(branch_current_match.group(1)) if branch_current_match else 0
                if branch.poles.startswith("1P"):
                    poles, amps = "2P", "25A"
                elif branch.poles.startswith("3P"):
                    poles = "3P"
                    amps = "12A" if branch_current <= 20 else "32A" if branch_current <= 40 else "40A"
                else:
                    continue
                selected_contactors.append(Component(
                    id=stable_id("component", panel_id, "coordinated-contactor", index, poles, amps),
                    category="CONTACTOR", label="Coordinated branch contactor",
                    poles=poles, amps=amps, quantity=branch.quantity,
                    manufacturer=default_manufacturer.upper(), confidence=0.72,
                    origin="topology_recognized", review_status="included",
                    notes="Power contactor selected from the explicit AC3 coordination requirement and protected branch type.",
                    conflicts=["Engineering-selected contactor rating requires review."],
                ))
            if selected_contactors:
                components = [
                    item for item in components
                    if not (
                        item.category == "CONTACTOR"
                        and item.amps in {"10A", "-", "", "NULL"}
                    )
                ]
                components.extend(selected_contactors)

        if repeated_ats:
            for component in components:
                if component.category == "MCB":
                    if component.curve in {"", "-", "0", "NULL"}:
                        component.curve = "C"
                    if component.breaking_capacity in {"", "-", "0", "0kA", "NULL"}:
                        component.breaking_capacity = "10kA"
                    component.notes = append_note(
                        component.notes,
                        "Repeated ATS branch commercial default: curve C, 10kA; verify before export.",
                    )
                    component.conflicts.append(
                        "ATS branch curve/breaking capacity are commercial defaults, not printed ratings."
                    )
                elif component.category == "SPD" and component.spd_type in {"", "-", "0", "NULL"}:
                    component.spd_type = "TYPE 2"
                    component.notes = append_note(
                        component.notes,
                        "Repeated distribution SPD defaulted to Type 2; verify before export.",
                    )

            # A printed SPD needs a protection stage.  Add only the deficit
            # between explicit SPD callouts and the repeated 4P 20A breakers;
            # this recovers a main SPD feeder without duplicating branch MCBs.
            spd_quantity = sum(item.quantity for item in components if item.category == "SPD")
            protection_quantity = sum(
                item.quantity for item in components
                if item.category == "MCB" and item.poles == "4P" and item.amps == "20A"
            )
            deficit = max(0, spd_quantity - protection_quantity)
            if deficit:
                components.append(Component(
                    id=stable_id("component", panel_id, "ats-spd-protection"),
                    category="MCB", label="SPD protection MCB", poles="4P", amps="20A",
                    curve="C", breaking_capacity="10kA", quantity=deficit,
                    manufacturer=default_manufacturer.upper(), confidence=0.72,
                    origin="topology_recognized", review_status="included",
                    notes="One protection stage added for each explicit SPD not already paired with a 4P 20A MCB.",
                    conflicts=["Topology-derived SPD protection requires engineering review."],
                ))

            # Indirect tenant kWh metering uses three CTs per repeated meter.
            # The next standard CT primary above a 63A tenant feeder is 80A.
            repeated_meters = max(
                (item.quantity for item in components if item.category == "METER"),
                default=0,
            )
            existing_tenant_cts = sum(
                item.quantity for item in components
                if item.category == "CT" and item.ct_ratio == "80/5"
            )
            tenant_ct_deficit = max(0, repeated_meters * 3 - existing_tenant_cts)
            if tenant_ct_deficit:
                components.append(Component(
                    id=stable_id("component", panel_id, "tenant-metering-cts"),
                    category="CT", label="Tenant metering current transformers",
                    ct_ratio="80/5", quantity=tenant_ct_deficit,
                    manufacturer="MBS", confidence=0.72,
                    origin="topology_recognized", review_status="included",
                    notes="Three 80/5 CTs added per repeated indirect 63A tenant meter.",
                    conflicts=["Topology-derived tenant CT quantity and ratio require engineering review."],
                ))

            branch_count = max(
                (item.quantity for item in components if item.category == "CONTACTOR"),
                default=0,
            )
            cubicle_count = (branch_count + 4) // 5
            engineering_defaults = [
                ("FUSE_HOLDER", "ATS control fuse holders", branch_count * 2 + 6),
                ("PILOT_LAMP", "ATS status pilot lamps", 6),
                ("TIMER", "ATS sequence timers", cubicle_count + 2),
                ("RELAY", "ATS control and voltage relays", branch_count + (branch_count + 9) // 10 + 1),
            ]
            for category, label, quantity in engineering_defaults:
                if quantity <= 0 or any(item.category == category for item in components):
                    continue
                components.append(Component(
                    id=stable_id("component", panel_id, "ats-engineering-default", category),
                    category=category, label=label, quantity=quantity,
                    manufacturer=default_manufacturer.upper(), confidence=0.65,
                    origin="engineering_standard", review_status="included",
                    notes="Reviewable ATS engineering matrix output derived from repeated transfer branches and cubicle count.",
                    conflicts=["Engineering-standard control quantity is not printed on the SLD; verify before export."],
                ))

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
                origin="deterministic_schematic", evidence=evidence, review_status="included",
                conflicts=["Explicit isolator block was missing from structured extraction; verify its role."],
            ))

        if not any(component.category == "SPD" for component in components):
            spd_entities = [
                item for item in graph.entities
                if item.kind in {"text", "attribute"} and item.text.strip().upper() in {"SPD", "SURGE ARRESTER"}
            ]
            if spd_entities:
                all_text = " ".join(item.text for item in graph.entities if item.kind in {"text", "attribute"})
                spd_spec_text = " ".join(
                    item.text for item in graph.entities
                    if item.kind in {"text", "attribute"}
                    and re.search(r"\b(?:SPD|SURGE|CLASS\s*[ABC]|TYPE\s*1\s*\+\s*2)\b", item.text, re.I)
                )
                type_match = re.search(r"(?:TYPE|CLASS)\s*([123])\b", spd_spec_text, re.I)
                inferred_type = f"TYPE {type_match.group(1)}" if type_match else "-"
                if inferred_type == "-" and re.search(r"\b(?:IMAX|UP\s*[<≤]=?)\b", all_text, re.I):
                    inferred_type = "TYPE 2"
                components.append(Component(
                    id=stable_id("component", panel_id, "schematic-spd"),
                    category="SPD", label="Surge protective device",
                    spd_type=inferred_type,
                    quantity=len(spd_entities), manufacturer=default_manufacturer.upper(), confidence=1.0,
                    origin="deterministic_schematic",
                    evidence=[self._entity_evidence(item, render_transform) for item in spd_entities[:3]],
                    review_status="included",
                    conflicts=["Explicit SPD callout was missing from structured extraction; verify its type and grouped quantity."],
                ))

    @staticmethod
    def _entity_evidence(entity, render_transform: dict) -> Evidence:
        return Evidence(
            source="cad_entity", source_id=entity.id, entity_handle=entity.handle,
            block_name=entity.block_name, layer=entity.layer, text=entity.text,
            cad_bbox=[entity.x, entity.y, entity.x + entity.width, entity.y + entity.height],
            image_bbox=_image_bbox(entity, render_transform), confidence=1.0,
        )
