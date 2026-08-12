from __future__ import annotations

import argparse
import json
import os
import traceback
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure:
        reconfigure(encoding="utf-8", errors="replace")

from app.domain import ProjectDocument
from app.extraction import ExtractionService
from app.persistence import atomic_write_json, save_project


def _api_key() -> str:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    env_file = Path(".venv/key.env")
    if not key and env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("GEMINI_API_KEY="):
                key = line.split("=", 1)[1].strip()
                break
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    return key


def main() -> int:
    parser = argparse.ArgumentParser(description="Run canonical extraction over a directory of DWG/DXF drawings")
    parser.add_argument("source_dir")
    parser.add_argument("--out", required=True)
    parser.add_argument("--oda-path", default="")
    parser.add_argument("--manufacturer", default="SCHNEIDER")
    parser.add_argument("--model", default="")
    parser.add_argument("--only", default="", help="Case-insensitive filename substring")
    parser.add_argument("--resume", action="store_true")
    arguments = parser.parse_args()
    source = Path(arguments.source_dir)
    output = Path(arguments.out)
    output.mkdir(parents=True, exist_ok=True)
    drawings = sorted([*source.glob("*.dwg"), *source.glob("*.dxf")], key=lambda path: path.name.lower())
    if arguments.only:
        drawings = [path for path in drawings if arguments.only.lower() in path.name.lower()]
    if not drawings:
        raise RuntimeError(f"No DWG/DXF drawings found in {source}")
    if arguments.model:
        os.environ["GEMINI_API_MODEL"] = arguments.model
    service = ExtractionService(_api_key(), arguments.oda_path, progress=print)
    summary_path = output / "run-summary.json"
    summary = []
    if arguments.resume and summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8")).get("drawings", [])
    latest = {item["drawing"]: item for item in summary}
    completed = {drawing for drawing, item in latest.items() if item.get("status") == "success"}
    for index, drawing in enumerate(drawings, 1):
        if str(drawing) in completed:
            print(f"\n=== {index}/{len(drawings)} {drawing.name} (already complete) ===", flush=True)
            continue
        run_dir = output / drawing.stem
        project = ProjectDocument.create(drawing.stem, drawing.stem)
        project.pricing.default_manufacturer = arguments.manufacturer.upper()
        print(f"\n=== {index}/{len(drawings)} {drawing.name} ===", flush=True)
        try:
            panels = service.extract(project, drawing, run_dir)
            project_path = save_project(project, run_dir / "project.technelec.json")
            latest[str(drawing)] = {
                "drawing": str(drawing), "status": "success",
                "panel_references": [panel.reference for panel in panels],
                "components": sum(len(panel.components) for panel in panels),
                "terminals": sum(len(panel.terminals) for panel in panels),
                "project_path": str(project_path),
            }
        except Exception as exc:
            traceback.print_exc()
            latest[str(drawing)] = {"drawing": str(drawing), "status": "failed", "error": str(exc)}
        summary = [latest[str(path)] for path in drawings if str(path) in latest]
        atomic_write_json(summary_path, {"drawings": summary})
    return 0 if summary and all(item["status"] == "success" for item in summary) else 1


if __name__ == "__main__":
    raise SystemExit(main())
