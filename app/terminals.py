from __future__ import annotations

import re
from collections import defaultdict

from .domain import Component, Terminal, stable_id


def parse_mm2(value: object) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    match = re.search(r"(\d+(?:[.,]\d+)?)", str(value or ""))
    return float(match.group(1).replace(",", ".")) if match else 0.0


def pole_count(value: str) -> int:
    text = (value or "").upper().replace(" ", "")
    if "+N" in text:
        match = re.match(r"(\d+)", text)
        return (int(match.group(1)) if match else 1) + 1
    match = re.match(r"(\d+)", text)
    return int(match.group(1)) if match else 1


def terminal_type(cable_mm2: float, poles: str, incomer: bool = False) -> tuple[str, str, float, int]:
    size = max(cable_mm2, 4.0)
    count = pole_count(poles)
    if size <= 4 and not incomer and count == 2:
        return "PTTB 4", "Phoenix Contact double-level terminal 2x4mm²", 4.0, 1
    ratings = (4.0, 6.0, 10.0, 16.0, 25.0, 35.0, 50.0, 70.0, 95.0, 150.0)
    rating = next((candidate for candidate in ratings if size <= candidate), ratings[-1])
    return f"PT {rating:g}", f"Phoenix Contact feed-through terminal {rating:g}mm²", rating, count


def calculate_terminals(components: list[Component], panel_id: str) -> list[Terminal]:
    grouped: dict[tuple[str, str, float], dict[str, object]] = defaultdict(
        lambda: {"quantity": 0, "circuits": [], "warnings": []}
    )
    for component in components:
        if not component.is_incomer and not component.has_terminal:
            continue
        # High-current switchboards terminate power conductors directly on
        # busbars/lugs, not rail-mounted terminal blocks.
        amps_match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*A\s*", component.amps, re.I)
        if component.is_incomer and amps_match and float(amps_match.group(1)) > 160:
            continue
        size = parse_mm2(component.cable_size)
        name, description, rating, per_circuit = terminal_type(size, component.poles, component.is_incomer)
        if component.is_incomer and "TRANSFER" in component.label.upper():
            per_circuit *= 2
        key = (name, description, rating)
        grouped[key]["quantity"] = int(grouped[key]["quantity"]) + per_circuit * max(1, component.quantity)
        label = component.load_label or component.label or component.id
        grouped[key]["circuits"].append(label)
        if not size:
            grouped[key]["warnings"].append(f"{label}: cable size missing; defaulted to 4mm²")
    terminals = []
    for (name, description, rating), values in sorted(grouped.items()):
        terminals.append(Terminal(
            id=stable_id("terminal", panel_id, name),
            name=name,
            description=description,
            max_mm2=rating,
            quantity=int(values["quantity"]),
            circuits=list(dict.fromkeys(values["circuits"])),
            warnings=list(dict.fromkeys(values["warnings"])),
        ))
    return terminals
