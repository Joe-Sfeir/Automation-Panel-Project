from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cad import graph_from_dict
from app.accessories import calculate_commercial_accessories
from app.domain import Panel, stable_id
from app.extraction import ExtractionService
from app.multi_panel import deterministic_multi_panel_extraction
from app.persistence import load_project, save_project
from app.terminals import calculate_terminals


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-run deterministic reconciliation over cached structured model responses."
    )
    parser.add_argument("source_run", help="Dataset run containing project and debug artifacts")
    parser.add_argument("--out", required=True)
    arguments = parser.parse_args()

    source_run = Path(arguments.source_run)
    output = Path(arguments.out)
    service = ExtractionService("")
    processed = 0

    for project_path in sorted(source_run.glob("*/project.technelec.json")):
        project = load_project(project_path)
        if not project.panels:
            continue
        first_panel = project.panels[0]
        if len(first_panel.debug_paths) < 3:
            print(f"SKIP {project_path}: debug paths are incomplete")
            continue
        graph_path = Path(first_panel.debug_paths[0])
        if len(first_panel.debug_paths) >= 4:
            transform_path = Path(first_panel.debug_paths[2])
            debug_dir = Path(first_panel.debug_paths[3])
        else:
            transform_path = graph_path.with_name("render-metadata.json")
            debug_dir = Path(first_panel.debug_paths[2])
        raw_path = debug_dir / "gemini_panels_raw.json"
        if not graph_path.exists() or not raw_path.exists():
            print(f"SKIP {project_path}: cached graph/model artifacts are missing")
            continue

        graph = graph_from_dict(json.loads(graph_path.read_text(encoding="utf-8")))
        if transform_path.exists():
            transform = json.loads(transform_path.read_text(encoding="utf-8"))
        else:
            xs = [entity.x for entity in graph.entities]
            ys = [entity.y for entity in graph.entities]
            transform = {
                "cad_bbox": [min(xs), min(ys), max(xs), max(ys)] if xs and ys else [0, 0, 1, 1],
                "image_size": [0, 0],
            }
        raw_panels = json.loads(raw_path.read_text(encoding="utf-8")).get("panels", [])
        deterministic_panels = deterministic_multi_panel_extraction(graph)
        if deterministic_panels:
            source_drawing_id = first_panel.source_drawing_id
            rebuilt_panels = []
            for extracted in deterministic_panels:
                reference = str(extracted.get("panel_ref", "UNKNOWN") or "UNKNOWN").strip()
                panel_id = stable_id("panel", project.id, source_drawing_id, reference)
                components = service._components(
                    panel_id, extracted.get("components", []), graph,
                    project.pricing.default_manufacturer, transform,
                    supplement_layout=False,
                )
                components = service._apply_explicit_graph_rules(
                    panel_id, reference, components, graph,
                    project.pricing.default_manufacturer, transform,
                )
                rebuilt_panels.append(Panel(
                    id=panel_id, reference=reference,
                    source_drawing_id=source_drawing_id,
                    extraction_status="complete", components=components,
                    terminals=calculate_terminals(
                        components, panel_id,
                        " ".join(
                            entity.text for entity in graph.entities
                            if entity.kind in {"text", "attribute"}
                        ),
                    ),
                    commercial_accessories=calculate_commercial_accessories(
                        components, panel_id,
                        " ".join(
                            entity.text for entity in graph.entities
                            if entity.kind in {"text", "attribute"}
                        ),
                        project.pricing.default_manufacturer,
                    ),
                    render_paths=list(first_panel.render_paths),
                    debug_paths=list(first_panel.debug_paths),
                    review_status="needs_review",
                ))
            project.panels = rebuilt_panels
            raw_panels = deterministic_panels
        raw_by_reference = {
            str(row.get("panel_ref", "")).strip().upper(): row for row in raw_panels
        }
        if deterministic_panels:
            destination = output / project_path.parent.name / "project.technelec.json"
            save_project(project, destination)
            processed += 1
            print(f"OK {project_path.parent.name} ({len(project.panels)} deterministic panels)")
            continue
        for panel in project.panels:
            extracted = raw_by_reference.get(panel.reference.strip().upper())
            if extracted is None and len(project.panels) == len(raw_panels) == 1:
                extracted = raw_panels[0]
            if extracted is None:
                print(f"WARN {project_path}: no cached response for {panel.reference}")
                continue
            panel.components = service._components(
                panel.id,
                extracted.get("components", []),
                graph,
                project.pricing.default_manufacturer,
                transform,
                supplement_layout=len(raw_panels) == 1,
            )
            panel.components = service._apply_explicit_graph_rules(
                panel.id, panel.reference, panel.components, graph,
                project.pricing.default_manufacturer, transform,
            )
            panel.terminals = calculate_terminals(
                panel.components, panel.id,
                " ".join(
                    entity.text for entity in graph.entities
                    if entity.kind in {"text", "attribute"}
                ),
            )
            panel.commercial_accessories = calculate_commercial_accessories(
                panel.components, panel.id,
                " ".join(
                    entity.text for entity in graph.entities
                    if entity.kind in {"text", "attribute"}
                ),
                project.pricing.default_manufacturer,
            )

        destination = output / project_path.parent.name / "project.technelec.json"
        save_project(project, destination)
        processed += 1
        print(f"OK {project_path.parent.name}")

    print(f"Reconciled {processed} projects into {output}")
    return 0 if processed else 1


if __name__ == "__main__":
    raise SystemExit(main())
