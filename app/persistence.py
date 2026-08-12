from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .domain import Component, Panel, ProjectDocument, SourceDrawing, Terminal, stable_id, project_from_dict


CANONICAL_FILENAME = "project.technelec.json"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def save_project(project: ProjectDocument, path: str | Path) -> Path:
    if project.schema_version != 1:
        raise ValueError(f"Unsupported schema version: {project.schema_version}")
    return atomic_write_json(path, project.to_dict())


def _migrate_legacy_sld(data: dict[str, Any], source: Path) -> dict[str, Any]:
    panel_ref = str(data.get("panel_ref") or source.stem)
    source_file = str(data.get("source_file") or "")
    project = ProjectDocument.create(panel_ref, panel_ref)
    drawing_id = stable_id("drawing", source_file or source.name)
    if source_file:
        project.source_drawings.append(SourceDrawing(drawing_id, source_file, "", Path(source_file).name))
    components = []
    for index, row in enumerate(data.get("components", [])):
        normalized = dict(row)
        normalized["id"] = normalized.get("id") or stable_id(
            "component", panel_ref, normalized.get("label", ""), normalized.get("category", ""), index
        )
        normalized["quantity"] = int(normalized.pop("qty", normalized.get("quantity", 1)))
        normalized["origin"] = normalized.pop("data_source", "ai_extracted")
        normalized["notes"] = normalized.pop("ai_notes", "")
        normalized["review_status"] = "needs_review"
        components.append(Component(**{
            key: value for key, value in normalized.items() if key in Component.__dataclass_fields__
        }))
    terminals = []
    for index, row in enumerate(data.get("terminals", [])):
        name = str(row.get("name") or row.get("description") or "Terminal")
        terminals.append(Terminal(
            id=str(row.get("id") or stable_id("terminal", panel_ref, name, index)),
            name=name,
            description=str(row.get("description") or name),
            max_mm2=float(row.get("max_mm2", 0) or 0),
            quantity=int(row.get("qty", row.get("quantity", 0)) or 0),
            circuits=[value.strip() for value in str(row.get("circuits", "")).split(",") if value.strip()],
        ))
    panel = Panel(
        id=stable_id("panel", panel_ref, source_file),
        reference=panel_ref,
        source_drawing_id=drawing_id if source_file else "",
        extraction_status="complete",
        components=components,
        terminals=terminals,
    )
    project.panels.append(panel)
    return project.to_dict()


def migrate_payload(data: dict[str, Any], source: str | Path = "project.json") -> dict[str, Any]:
    version = data.get("schema_version")
    if version == 1:
        return data
    if version is None and "components" in data and "panel_ref" in data:
        return _migrate_legacy_sld(data, Path(source))
    raise ValueError(f"Unsupported or unrecognized project schema: {version!r}")


def load_project(path: str | Path) -> ProjectDocument:
    source = Path(path)
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    migrated = migrate_payload(data, source)
    project = project_from_dict(migrated)
    if project.schema_version != 1:
        raise ValueError(f"Unsupported schema version: {project.schema_version}")
    return project
