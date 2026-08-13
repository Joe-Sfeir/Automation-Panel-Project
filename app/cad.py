from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable


CAD_PARSER_VERSION = 5


@dataclass
class CadEntity:
    id: str
    kind: str
    handle: str = ""
    layer: str = ""
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    text: str = ""
    block_name: str = ""
    attributes: dict[str, str] = field(default_factory=dict)
    parent_id: str = ""
    points: list[list[float]] = field(default_factory=list)


@dataclass
class CadAssociation:
    block_id: str
    label_id: str
    distance: float
    normalized_distance: float
    topology_bonus: float
    confidence: float


@dataclass
class CadEvidenceGraph:
    source_path: str
    entities: list[CadEntity] = field(default_factory=list)
    associations: list[CadAssociation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source_path": self.source_path,
            "entities": [asdict(item) for item in self.entities],
            "associations": [asdict(item) for item in self.associations],
            "warnings": self.warnings,
        }


def graph_from_dict(data: dict) -> CadEvidenceGraph:
    return CadEvidenceGraph(
        source_path=str(data.get("source_path", "")),
        entities=[CadEntity(**item) for item in data.get("entities", [])],
        associations=[CadAssociation(**item) for item in data.get("associations", [])],
        warnings=list(data.get("warnings", [])),
    )


def _xy(point) -> tuple[float, float]:
    return float(point[0]), float(point[1])


def _distance(a: CadEntity, b: CadEntity) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _label_layer_compatible(block_layer: str, label_layer: str) -> bool:
    if label_layer == block_layer:
        return True
    label_name = label_layer.upper()
    if any(token in label_name for token in ("PAPER", "TITLE", "FRAME", "BORDER", "ANNOTATION", "ANOTATION")):
        return False
    block_root = re.sub(r"[^A-Z0-9]", "", block_layer.upper().replace("TEXT", ""))
    label_root = re.sub(r"[^A-Z0-9]", "", label_name.replace("TEXT", ""))
    return "TEXT" in label_name or (block_root and label_root and (block_root in label_root or label_root in block_root))


def associate_blocks_to_labels(entities: list[CadEntity]) -> list[CadAssociation]:
    blocks = [item for item in entities if item.kind == "block"]
    labels = [item for item in entities if item.kind in {"text", "attribute"} and item.text.strip()]
    if not blocks or not labels:
        return []
    heights = [item.height for item in labels if item.height > 0]
    text_scale = statistics.median(heights) if heights else 1.0
    xs = [item.x for item in entities]
    ys = [item.y for item in entities]
    diagonal = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) if len(xs) > 1 else text_scale
    search_radius = max(text_scale * 18.0, diagonal * 0.025, 1.0)
    associations = []
    for block in sorted(blocks, key=lambda item: item.id):
        candidates = []
        for label in labels:
            if not _label_layer_compatible(block.layer, label.layer):
                continue
            dx = abs(block.x - label.x)
            dy = abs(block.y - label.y)
            # Component labels in SLDs are normally arranged horizontally from
            # their symbols. Penalize vertical drift so adjacent circuit rows
            # cannot steal one another's ratings.
            distance = math.hypot(dx, dy * 4.0)
            if distance > search_radius:
                continue
            topology_bonus = 0.0
            normalized = distance / search_radius
            confidence = max(0.0, min(1.0, 1.0 - normalized + topology_bonus))
            candidates.append((confidence, distance, label.id, label, topology_bonus, normalized))
        if candidates:
            confidence, distance, _, label, bonus, normalized = max(candidates)
            associations.append(CadAssociation(
                block.id, label.id, round(distance, 6), round(normalized, 6),
                bonus, round(confidence, 6),
            ))
    return associations


def schematic_layers(entities: list[CadEntity]) -> list[str]:
    """Select likely schematic layers without relying on client layer names."""
    stats: dict[str, Counter] = defaultdict(Counter)
    for entity in entities:
        stats[entity.layer][entity.kind] += 1
    candidates = []
    for layer, counts in stats.items():
        name = layer.upper()
        if any(token in name for token in ("PAPER", "TITLE", "FRAME", "BORDER", "ANNOTATION")):
            continue
        blocks = counts["block"]
        texts = counts["text"] + counts["attribute"]
        wires = counts["wire"]
        if not blocks or not texts:
            continue
        text_ratio = texts / blocks
        # Schematic layers combine many circuit labels with device symbols.
        # Physical-layout layers tend to have dense repeated blocks and very
        # little text. Wires help rank but never imply electrical topology.
        score = texts * 2.0 + min(wires, texts * 2.0) + blocks * 0.2
        candidates.append((score, text_ratio, layer))
    if not candidates:
        return sorted({entity.layer for entity in entities if entity.kind == "block"})
    best = max(score for score, _, _ in candidates)
    selected = [layer for score, ratio, layer in candidates if ratio >= 0.75 and score >= best * 0.55]
    if not selected:
        selected = [max(candidates)[2]]
    return sorted(selected)


def physical_layout_layers(entities: list[CadEntity], schematic: Iterable[str]) -> list[str]:
    """Find block-rich panel elevations that can confirm, but not add to, SLD counts."""
    schematic_set = set(schematic)
    by_layer: dict[str, list[CadEntity]] = defaultdict(list)
    for entity in entities:
        by_layer[entity.layer].append(entity)
    candidates = []
    device_name = re.compile(
        r"(?:^[1-4]\s*[xX]\s*\d+|MCB|MCCB|RCCB|RCD|FUSE|LAMP|METER|SPD|CT|SWITCH|CONTACTOR|RELAY|TIMER|PLC|POWER)",
        re.I,
    )
    for layer, layer_entities in by_layer.items():
        if layer in schematic_set:
            continue
        name = layer.upper()
        if any(token in name for token in ("PAPER", "TITLE", "FRAME", "BORDER", "ANNOTATION")):
            continue
        blocks = [item for item in layer_entities if item.kind == "block"]
        if len(blocks) < 3:
            continue
        recognizable = sum(bool(device_name.search(item.block_name)) for item in blocks)
        if recognizable / len(blocks) < 0.3:
            continue
        texts = sum(item.kind in {"text", "attribute"} for item in layer_entities)
        candidates.append((recognizable * 2 + len(blocks) - texts * 0.1, layer))
    if not candidates:
        return []
    best = max(score for score, _ in candidates)
    return sorted(layer for score, layer in candidates if score >= best * 0.55)


def parse_layout_block_name(name: str) -> dict | None:
    value = " ".join(str(name or "").upper().replace("×", "X").split())
    if not value:
        return None
    if "INDICATOR" in value and "LAMP" in value:
        return {"category": "PILOT_LAMP", "poles": "-", "amps": "-", "sensitivity": "-", "quantity_multiplier": 1}
    if "POWER METER" in value or value == "METER":
        return {"category": "METER", "poles": "-", "amps": "-", "sensitivity": "-", "quantity_multiplier": 1}
    if "FUSE" in value:
        count = re.search(r"(?:FUSE\s*)?(\d+)\s*X", value)
        return {"category": "FUSE_HOLDER", "poles": "-", "amps": "-", "sensitivity": "-", "quantity_multiplier": int(count.group(1)) if count else 1}
    if "SPD" in value or "SURGE" in value:
        return {"category": "SPD", "poles": "-", "amps": "-", "sensitivity": "-", "quantity_multiplier": 1}
    rating = re.search(r"\b([1-4])\s*X\s*(\d+(?:\.\d+)?)", value)
    if not rating:
        return None
    conductors, amps = int(rating.group(1)), rating.group(2)
    sensitivity = re.search(r"(\d+(?:\.\d+)?)\s*MA\b", value)
    if sensitivity or "RCD" in value or "RCCB" in value:
        category = "RCCB"
    elif any(token in value for token in ("SWITCH", "ISOL", "MTS", "INS")):
        category = "SWITCH"
    else:
        category = "MCB"
    poles = "1P+N" if conductors == 2 and category == "MCB" else f"{conductors}P"
    return {
        "category": category,
        "poles": poles,
        "amps": f"{amps}A",
        "sensitivity": f"{sensitivity.group(1)}mA" if sensitivity else "-",
        "quantity_multiplier": 1,
    }


def layout_device_groups(entities: list[CadEntity], layout_layers: Iterable[str]) -> list[dict]:
    layers = set(layout_layers)
    groups: dict[tuple, dict] = {}
    for entity in entities:
        if entity.kind != "block" or entity.layer not in layers:
            continue
        parsed = parse_layout_block_name(entity.block_name)
        if not parsed:
            continue
        key = (entity.layer, parsed["category"], parsed["poles"], parsed["amps"], parsed["sensitivity"])
        group = groups.setdefault(key, {
            "layer": entity.layer,
            "block_name": entity.block_name,
            "block_names": [],
            **parsed,
            "block_ids": [],
            "block_count": 0,
            "commercial_quantity": 0,
        })
        if entity.block_name not in group["block_names"]:
            group["block_names"].append(entity.block_name)
        group["block_ids"].append(entity.id)
        group["block_count"] += 1
        group["commercial_quantity"] += parsed["quantity_multiplier"]
    return sorted(groups.values(), key=lambda item: (item["layer"], item["category"], item["amps"], item["sensitivity"]))


def schematic_rating_groups(entities: list[CadEntity]) -> list[dict]:
    """Return exact, explicitly written schematic breaker-rating inventories.

    A physical panel layout is useful for total inventory, but its library block
    names can represent a commercial selection rather than the rating printed on
    the SLD.  Repeated labels such as ``2x10`` beside outgoing symbols are the
    stronger source for the specification split.  Unambiguous 2-conductor
    notation is promoted to an MCB group.  RCCB groups are created only when an
    explicit RCD/RCCB marker can be paired geometrically with both a 4-pole
    rating and a sensitivity label.
    """
    selected_layers = schematic_layers(entities)
    eligible_layers = {
        entity.layer for entity in entities
        if entity.layer in selected_layers
        or (
            entity.kind in {"text", "attribute"}
            and any(_label_layer_compatible(layer, entity.layer) for layer in selected_layers)
        )
    }
    groups: dict[tuple[str, str, str, str], dict] = {}
    pattern = re.compile(r"^\s*2\s*[X×]\s*(\d+(?:\.\d+)?)\s*(?:A|AMP)?\s*$", re.I)
    for entity in entities:
        if entity.kind not in {"text", "attribute"} or entity.layer not in eligible_layers:
            continue
        match = pattern.fullmatch(entity.text or "")
        if not match:
            continue
        amps = f"{match.group(1)}A"
        key = ("MCB", "1P+N", amps, "-")
        group = groups.setdefault(key, {
            "category": "MCB", "poles": "1P+N", "amps": amps,
            "sensitivity": "-", "count": 0, "label_ids": [], "labels": [], "layers": [],
        })
        group["count"] += 1
        group["label_ids"].append(entity.id)
        if entity.text not in group["labels"]:
            group["labels"].append(entity.text)
        if entity.layer not in group["layers"]:
            group["layers"].append(entity.layer)

    eligible_texts = [
        entity for entity in entities
        if entity.kind in {"text", "attribute"}
        and entity.layer in eligible_layers and entity.text.strip()
    ]
    heights = [entity.height for entity in eligible_texts if entity.height > 0]
    text_scale = statistics.median(heights) if heights else 1.0
    xs = [entity.x for entity in eligible_texts]
    ys = [entity.y for entity in eligible_texts]
    diagonal = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) if len(xs) > 1 else text_scale
    search_radius = max(text_scale * 25.0, diagonal * 0.025, 1.0)
    markers = [
        entity for entity in eligible_texts
        if re.search(r"\bRCC?B\b|\bRCD\b", entity.text, re.I)
    ]
    ratings = []
    sensitivities = []
    rating_pattern = re.compile(r"^\s*4\s*[X×]\s*(\d+(?:\.\d+)?)\s*(?:A|AMP)?\s*$", re.I)
    sensitivity_pattern = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*MA\s*$", re.I)
    for entity in eligible_texts:
        if match := rating_pattern.fullmatch(entity.text):
            ratings.append((entity, f"{match.group(1)}A"))
        if match := sensitivity_pattern.fullmatch(entity.text):
            sensitivities.append((entity, f"{match.group(1)}mA"))

    used_ratings: set[str] = set()
    used_sensitivities: set[str] = set()
    for marker in sorted(markers, key=lambda item: item.id):
        def nearest(candidates, used):
            ranked = []
            for entity, value in candidates:
                if entity.id in used or not _label_layer_compatible(marker.layer, entity.layer):
                    continue
                distance = math.hypot(marker.x - entity.x, (marker.y - entity.y) * 2.0)
                if distance <= search_radius:
                    ranked.append((distance, entity.id, entity, value))
            return min(ranked) if ranked else None

        rating = nearest(ratings, used_ratings)
        sensitivity = nearest(sensitivities, used_sensitivities)
        if not rating or not sensitivity:
            continue
        _, _, rating_entity, amps = rating
        _, _, sensitivity_entity, sensitivity_value = sensitivity
        used_ratings.add(rating_entity.id)
        used_sensitivities.add(sensitivity_entity.id)
        key = ("RCCB", "4P", amps, sensitivity_value)
        group = groups.setdefault(key, {
            "category": "RCCB", "poles": "4P", "amps": amps,
            "sensitivity": sensitivity_value, "count": 0,
            "label_ids": [], "labels": [], "layers": [],
        })
        group["count"] += 1
        group["label_ids"].extend([marker.id, rating_entity.id, sensitivity_entity.id])
        group["labels"].append(
            f"{rating_entity.text} {sensitivity_entity.text} {marker.text}"
        )
        for layer in (marker.layer, rating_entity.layer, sensitivity_entity.layer):
            if layer not in group["layers"]:
                group["layers"].append(layer)
    return sorted(
        groups.values(),
        key=lambda item: (item["category"], item["poles"], item["amps"], item["sensitivity"]),
    )


def _text_clusters(entities: list[CadEntity]) -> tuple[float, list[dict]]:
    texts = [
        entity for entity in entities
        if entity.kind in {"text", "attribute"} and entity.text.strip()
    ]
    heights = sorted(entity.height for entity in texts if entity.height > 0)
    median_height = statistics.median(heights) if heights else 10.0
    radius = max(15.0, min(median_height * 1.5, 80.0))
    remaining = set(range(len(texts)))
    clusters = []
    while remaining:
        seed = min(remaining)
        remaining.remove(seed)
        members = [seed]
        frontier = [seed]
        while frontier:
            current = frontier.pop()
            source = texts[current]
            neighbours = [
                index for index in sorted(remaining)
                if texts[index].layer == source.layer
                and math.hypot(source.x - texts[index].x, source.y - texts[index].y) <= radius
            ]
            for index in neighbours:
                remaining.remove(index)
                members.append(index)
                frontier.append(index)
        group = [texts[index] for index in members]
        clusters.append({
            "layer": group[0].layer,
            "cx": round(sum(item.x for item in group) / len(group), 3),
            "cy": round(sum(item.y for item in group) / len(group), 3),
            "text_ids": [item.id for item in group],
            "texts": [item.text for item in group],
        })
    return radius, clusters


def build_ai_evidence(graph: CadEvidenceGraph) -> dict:
    selected_layers = schematic_layers(graph.entities)
    layout_layers = physical_layout_layers(graph.entities, selected_layers)
    selected = [
        entity for entity in graph.entities
        if entity.layer in selected_layers
        or (
            entity.kind in {"text", "attribute"}
            and "TEXT" in entity.layer.upper()
            and not any(token in entity.layer.upper() for token in ("TITLE", "FRAME", "PAPER", "ANNOTATION", "ANOTATION"))
        )
    ]
    associations = associate_blocks_to_labels(selected)
    entities = {entity.id: entity for entity in selected}
    association_rows = []
    associated_counts = Counter()
    for association in associations:
        block = entities[association.block_id]
        label = entities[association.label_id]
        nearby = []
        for candidate in selected:
            if candidate.kind not in {"text", "attribute"} or candidate.layer != block.layer or not candidate.text.strip():
                continue
            dx = abs(block.x - candidate.x)
            dy = abs(block.y - candidate.y)
            if dy <= max(candidate.height * 12, 300) and dx <= max(candidate.height * 90, 3500):
                nearby.append((math.hypot(dx, dy * 4), candidate))
        nearby.sort(key=lambda item: (item[0], item[1].id))
        association_rows.append({
            "block_id": block.id,
            "handle": block.handle,
            "block_name": block.block_name,
            "layer": block.layer,
            "x": block.x,
            "y": block.y,
            "label_id": label.id,
            "label": label.text,
            "label_x": label.x,
            "label_y": label.y,
            "confidence": association.confidence,
            "nearby_texts": [
                {"id": candidate.id, "text": candidate.text, "distance": round(distance, 3)}
                for distance, candidate in nearby[:8]
            ],
        })
        associated_counts[label.text.strip()] += 1
    cluster_radius, clusters = _text_clusters(selected)
    schematic_blocks = [
        entity for entity in selected
        if entity.kind == "block" and entity.layer in selected_layers
    ]
    repeating_blocks = Counter(entity.block_name for entity in schematic_blocks if entity.block_name)
    return {
        "schematic_layers": selected_layers,
        "physical_layout_layers": layout_layers,
        "schematic_rating_groups": schematic_rating_groups(graph.entities),
        "repeating_schematic_blocks": [
            {"block_name": name, "count": count}
            for name, count in sorted(repeating_blocks.items()) if count > 1
        ],
        "schematic_blocks": [
            {
                "id": entity.id, "handle": entity.handle, "block_name": entity.block_name,
                "layer": entity.layer, "x": entity.x, "y": entity.y,
                "attributes": entity.attributes,
            }
            for entity in schematic_blocks
        ],
        "block_label_associations": association_rows,
        "associated_label_counts": dict(sorted(associated_counts.items())),
        "unassociated_blocks": [
            {
                "id": entity.id, "handle": entity.handle, "name": entity.block_name,
                "layer": entity.layer, "x": entity.x, "y": entity.y,
                "attributes": entity.attributes,
            }
            for entity in selected
            if entity.kind == "block" and entity.id not in {row["block_id"] for row in association_rows}
        ],
        "layout_block_groups": [
            {
                "layer": layer,
                "block_name": block_name,
                "count": len(items),
                "block_ids": [item.id for item in items],
            }
            for (layer, block_name), items in sorted(
                {
                    key: [
                        entity for entity in graph.entities
                        if entity.kind == "block" and (entity.layer, entity.block_name) == key
                    ]
                    for key in {
                        (entity.layer, entity.block_name) for entity in graph.entities
                        if entity.kind == "block" and entity.layer in layout_layers
                    }
                }.items()
            )
        ],
        "layout_device_groups": layout_device_groups(graph.entities, layout_layers),
        "text_cluster_radius": cluster_radius,
        "text_clusters": clusters,
        "texts": [
            {
                "id": entity.id, "layer": entity.layer, "x": entity.x, "y": entity.y,
                "height": entity.height, "text": entity.text,
            }
            for entity in selected if entity.kind in {"text", "attribute"}
        ],
    }


def parse_dxf_graph(path: str | Path) -> CadEvidenceGraph:
    try:
        import ezdxf
        from ezdxf import recover
    except ImportError as exc:
        raise RuntimeError("ezdxf is required to parse CAD evidence") from exc

    source = Path(path)
    try:
        document = ezdxf.readfile(source)
    except Exception:
        document, auditor = recover.readfile(source)
        warnings = [str(error) for error in getattr(auditor, "errors", [])]
    else:
        warnings = []
    entities: list[CadEntity] = []
    sequence = 0

    def identifier(entity, prefix: str) -> str:
        nonlocal sequence
        sequence += 1
        handle = str(getattr(entity.dxf, "handle", "") or "")
        return f"{prefix}:{handle or sequence}"

    def visit(entity, parent_id: str = "", depth: int = 0, inherited_layer: str = "") -> None:
        kind = entity.dxftype()
        raw_layer = str(getattr(entity.dxf, "layer", "") or "")
        layer = inherited_layer if raw_layer == "0" and inherited_layer else raw_layer
        if kind == "INSERT":
            insertion = _xy(entity.dxf.insert)
            item_id = identifier(entity, "block")
            attributes = {
                str(attribute.dxf.tag): str(attribute.dxf.text)
                for attribute in getattr(entity, "attribs", [])
            }
            entities.append(CadEntity(
                item_id, "block", str(getattr(entity.dxf, "handle", "") or ""),
                layer, *insertion, block_name=str(entity.dxf.name),
                attributes=attributes, parent_id=parent_id,
            ))
            for attribute in getattr(entity, "attribs", []):
                x, y = _xy(attribute.dxf.insert)
                entities.append(CadEntity(
                    identifier(attribute, "attribute"), "attribute",
                    str(getattr(attribute.dxf, "handle", "") or ""), layer, x, y,
                    height=float(getattr(attribute.dxf, "height", 1.0) or 1.0),
                    text=str(attribute.dxf.text), parent_id=item_id,
                ))
            try:
                if depth >= 8:
                    warnings.append(f"Nested block depth limit reached at {entity.dxf.name}")
                    return
                for child in entity.virtual_entities():
                    # virtual_entities are already transformed into the
                    # insert's world coordinates. Preserve nested text,
                    # attributes, geometry and inserts instead of retaining
                    # only the nested INSERT containers.
                    visit(child, item_id, depth + 1, layer)
            except Exception as exc:
                warnings.append(f"Could not expand block {entity.dxf.name}: {exc}")
            return
        if kind in {"TEXT", "MTEXT"}:
            point = getattr(entity.dxf, "insert", (0, 0))
            x, y = _xy(point)
            text = entity.plain_text() if kind == "MTEXT" else str(entity.dxf.text)
            entities.append(CadEntity(
                identifier(entity, "text"), "text",
                str(getattr(entity.dxf, "handle", "") or ""), layer, x, y,
                height=float(getattr(entity.dxf, "char_height", getattr(entity.dxf, "height", 1.0)) or 1.0),
                text=text, parent_id=parent_id,
            ))
            return
        if kind in {"LINE", "LWPOLYLINE", "POLYLINE"}:
            try:
                if kind == "LINE":
                    points = [list(_xy(entity.dxf.start)), list(_xy(entity.dxf.end))]
                else:
                    points = [list(_xy(point)) for point in entity.get_points()]
            except Exception:
                return
            if points:
                entities.append(CadEntity(
                    identifier(entity, "wire"), "wire",
                    str(getattr(entity.dxf, "handle", "") or ""), layer,
                    points[0][0], points[0][1], points=points, parent_id=parent_id,
                ))

    for entity in document.modelspace():
        visit(entity)
    graph = CadEvidenceGraph(str(source), entities, warnings=warnings)
    graph.associations = associate_blocks_to_labels(entities)
    return graph
