from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def stable_id(prefix: str, *parts: object) -> str:
    key = "|".join(str(part).strip() for part in parts)
    return f"{prefix}_{uuid5(NAMESPACE_URL, key).hex}"


@dataclass
class Evidence:
    source: str
    source_id: str = ""
    entity_handle: str = ""
    block_name: str = ""
    layer: str = ""
    text: str = ""
    cad_bbox: list[float] = field(default_factory=list)
    image_bbox: list[float] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class OverrideRecord:
    field: str
    previous: Any
    value: Any
    changed_at: str = field(default_factory=utc_now)
    reason: str = ""


@dataclass
class Component:
    id: str
    category: str = "OTHER"
    label: str = "-"
    poles: str = "-"
    amps: str = "-"
    curve: str = "-"
    breaking_capacity: str = "-"
    sensitivity: str = "-"
    spd_type: str = "-"
    ct_ratio: str = "-"
    meter_accuracy: str = "-"
    cable_size: float = 0.0
    load_label: str = ""
    quantity: int = 1
    manufacturer: str = ""
    forced_reference: str = ""
    has_terminal: bool = False
    is_incomer: bool = False
    confidence: float = 0.0
    origin: str = "ai_extracted"
    notes: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    review_status: str = "included"
    conflicts_acknowledged: bool = False
    manufacturer_override_confirmed: bool = False
    conflicts: list[str] = field(default_factory=list)
    overrides: list[OverrideRecord] = field(default_factory=list)

    def edit(self, field_name: str, value: Any, reason: str = "manual edit") -> None:
        if field_name not in self.__dataclass_fields__ or field_name in {"id", "overrides"}:
            raise ValueError(f"Component field is not editable: {field_name}")
        previous = getattr(self, field_name)
        if previous == value:
            return
        self.overrides.append(OverrideRecord(field_name, previous, value, reason=reason))
        setattr(self, field_name, value)
        self.origin = "manually_overridden"
        if self.review_status != "excluded":
            self.review_status = "included"
        if self.conflicts:
            self.conflicts_acknowledged = False
        if field_name == "manufacturer":
            self.manufacturer_override_confirmed = False


@dataclass
class Terminal:
    id: str
    name: str
    description: str
    max_mm2: float
    quantity: int
    circuits: list[str] = field(default_factory=list)
    origin: str = "calculated"
    warnings: list[str] = field(default_factory=list)


@dataclass
class CommercialAccessory:
    id: str
    description: str
    quantity: int
    manufacturer: str = ""
    origin: str = "engineering_calculated"
    notes: str = ""


@dataclass
class SourceDrawing:
    id: str
    path: str
    sha256: str
    filename: str
    added_at: str = field(default_factory=utc_now)


@dataclass
class Panel:
    id: str
    reference: str
    source_drawing_id: str
    extraction_status: str = "pending"
    components: list[Component] = field(default_factory=list)
    terminals: list[Terminal] = field(default_factory=list)
    commercial_accessories: list[CommercialAccessory] = field(default_factory=list)
    render_paths: list[str] = field(default_factory=list)
    debug_paths: list[str] = field(default_factory=list)
    review_status: str = "included"
    extraction_notes: list[str] = field(default_factory=list)


@dataclass
class CandidateMatch:
    reference: str
    description: str
    price: float
    currency: str
    score: float


@dataclass
class MatchResult:
    component_id: str
    manufacturer: str
    edition: str
    chosen_reference: str = ""
    chosen_description: str = ""
    alternatives: list[CandidateMatch] = field(default_factory=list)
    score: float = 0.0
    tier: str = "NF"
    unit_price: float = 0.0
    quantity: int = 0
    total_price: float = 0.0
    currency: str = ""
    review_status: str = "needs_review"
    note: str = ""


@dataclass
class PricingConfiguration:
    default_manufacturer: str = "SCHNEIDER"
    editions: dict[str, dict[str, Any]] = field(default_factory=dict)
    tva_rate: float = 0.11
    currency: str = "USD"


@dataclass
class ExportConfiguration:
    pricing_mode: str = "itemized"
    visible_columns: list[str] = field(default_factory=lambda: [
        "panel", "description", "manufacturer", "reference", "quantity",
        "unit_price", "total_price", "currency", "status",
    ])
    attention: str = ""


@dataclass
class ProjectDocument:
    id: str
    name: str
    reference: str
    schema_version: int = SCHEMA_VERSION
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    source_drawings: list[SourceDrawing] = field(default_factory=list)
    panels: list[Panel] = field(default_factory=list)
    pricing: PricingConfiguration = field(default_factory=PricingConfiguration)
    export: ExportConfiguration = field(default_factory=ExportConfiguration)
    match_results: list[MatchResult] = field(default_factory=list)

    @classmethod
    def create(cls, name: str, reference: str = "") -> "ProjectDocument":
        return cls(id=new_id("project"), name=name, reference=reference or name)

    def to_dict(self) -> dict[str, Any]:
        self.updated_at = utc_now()
        return asdict(self)


def _construct(cls, data: dict[str, Any], **nested: Any):
    fields = cls.__dataclass_fields__
    values = {key: value for key, value in data.items() if key in fields}
    values.update(nested)
    return cls(**values)


def project_from_dict(data: dict[str, Any]) -> ProjectDocument:
    drawings = [_construct(SourceDrawing, row) for row in data.get("source_drawings", [])]
    panels: list[Panel] = []
    for row in data.get("panels", []):
        components = []
        for item in row.get("components", []):
            evidence = [_construct(Evidence, evidence_row) for evidence_row in item.get("evidence", [])]
            overrides = [_construct(OverrideRecord, edit) for edit in item.get("overrides", [])]
            normalized = dict(item)
            if "qty" in normalized and "quantity" not in normalized:
                normalized["quantity"] = normalized.pop("qty")
            if normalized.get("review_status") in {"needs_review", "approved", ""}:
                normalized["review_status"] = "included"
            components.append(_construct(Component, normalized, evidence=evidence, overrides=overrides))
        terminals = []
        for item in row.get("terminals", []):
            normalized = dict(item)
            if "qty" in normalized and "quantity" not in normalized:
                normalized["quantity"] = normalized.pop("qty")
            circuits = normalized.get("circuits", [])
            if isinstance(circuits, str):
                normalized["circuits"] = [value.strip() for value in circuits.split(",") if value.strip()]
            terminals.append(_construct(Terminal, normalized))
        accessories = [
            _construct(CommercialAccessory, item)
            for item in row.get("commercial_accessories", [])
        ]
        normalized_panel = dict(row)
        if normalized_panel.get("review_status") in {"needs_review", "approved", ""}:
            normalized_panel["review_status"] = "included"
        panels.append(_construct(
            Panel, normalized_panel, components=components, terminals=terminals,
            commercial_accessories=accessories,
        ))
    results = []
    for row in data.get("match_results", []):
        alternatives = [_construct(CandidateMatch, item) for item in row.get("alternatives", [])]
        results.append(_construct(MatchResult, row, alternatives=alternatives))
    pricing = _construct(PricingConfiguration, data.get("pricing", {}))
    export = _construct(ExportConfiguration, data.get("export", {}))
    return _construct(
        ProjectDocument,
        data,
        source_drawings=drawings,
        panels=panels,
        pricing=pricing,
        export=export,
        match_results=results,
    )
