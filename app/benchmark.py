from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from .domain import Component, ProjectDocument
from .persistence import atomic_write_json, load_project


SPEC_FIELDS = ("category", "poles", "amps", "curve", "breaking_capacity", "sensitivity", "spd_type")


def _value(value: object) -> str:
    return "".join(character for character in str(value or "").upper() if character.isalnum())


def component_key(component: Component) -> tuple[str, ...]:
    return tuple(_value(getattr(component, field)) for field in SPEC_FIELDS)


def component_counts(project: ProjectDocument) -> Counter:
    result = Counter()
    for panel in project.panels:
        for component in panel.components:
            result[component_key(component)] += component.quantity
    return result


@dataclass
class BenchmarkReport:
    true_positive_quantity: int
    predicted_quantity: int
    verified_quantity: int
    precision: float
    recall: float
    f1: float
    exact_quantity_agreement: bool
    missing: dict[str, int]
    extra: dict[str, int]
    silent_high_confidence_contradictions: int


def evaluate(predicted: ProjectDocument, verified: ProjectDocument) -> BenchmarkReport:
    actual = component_counts(verified)
    guess = component_counts(predicted)
    keys = actual.keys() | guess.keys()
    true_positive = sum(min(actual[key], guess[key]) for key in keys)
    predicted_total = sum(guess.values())
    verified_total = sum(actual.values())
    precision = true_positive / predicted_total if predicted_total else float(not verified_total)
    recall = true_positive / verified_total if verified_total else float(not predicted_total)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    missing = {"|".join(key): actual[key] - guess[key] for key in keys if actual[key] > guess[key]}
    extra = {"|".join(key): guess[key] - actual[key] for key in keys if guess[key] > actual[key]}
    contradictions = sum(
        1
        for panel in predicted.panels
        for component in panel.components
        if component.confidence >= 0.8 and component.conflicts and component.review_status not in {"conflict", "needs_review"}
    )
    return BenchmarkReport(
        true_positive, predicted_total, verified_total,
        round(precision, 6), round(recall, 6), round(f1, 6),
        actual == guess, missing, extra, contradictions,
    )


def run_benchmark(predicted_path: str | Path, verified_path: str | Path, output_path: str | Path) -> BenchmarkReport:
    report = evaluate(load_project(predicted_path), load_project(verified_path))
    atomic_write_json(output_path, asdict(report))
    return report
