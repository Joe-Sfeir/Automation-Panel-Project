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


def _default_cable_size(component: Component) -> float:
    amps_match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*A\s*", component.amps, re.I)
    amps = float(amps_match.group(1)) if amps_match else 0.0
    if amps <= 25:
        return 4.0
    if amps <= 40:
        return 10.0
    if amps <= 63:
        return 16.0
    if amps <= 100:
        return 35.0
    if amps <= 160:
        return 70.0
    return 0.0


def _drawing_cable_terminals(drawing_text: str) -> list[tuple[int, float]]:
    """Return first conductor groups from explicit cable callouts.

    Earth conductors after ``+`` are intentionally excluded: panel terminal
    schedules count the switched phase/neutral conductors, while protective
    earth lands on the earth bar.
    """
    result = []
    for match in re.finditer(
        r"(?<![+\d.])(\d+)\s*(?:C\s*)?X\s*(\d+(?:[.,]\d+)?)\s*MM.",
        str(drawing_text or ""),
        re.I,
    ):
        conductors = int(match.group(1))
        size = float(match.group(2).replace(",", "."))
        if 1 <= conductors <= 4 and 0 < size <= 160:
            result.append((conductors, size))
    return result


def calculate_terminals(
    components: list[Component], panel_id: str, drawing_text: str = "",
) -> list[Terminal]:
    grouped: dict[tuple[str, str, float], dict[str, object]] = defaultdict(
        lambda: {"quantity": 0, "circuits": [], "warnings": []}
    )

    # Large repeated boards encode the terminal schedule more faithfully in
    # repeated cable callouts than in the aggregated component rows.  Use the
    # callouts as primary evidence once repetition proves this is a schedule,
    # not an isolated feeder note.
    cable_groups = _drawing_cable_terminals(drawing_text)
    if len(cable_groups) >= 10:
        for index, (conductors, size) in enumerate(cable_groups):
            poles = f"{conductors}P"
            name, description, rating, per_circuit = terminal_type(size, poles)
            key = (name, description, rating)
            grouped[key]["quantity"] = int(grouped[key]["quantity"]) + per_circuit
            grouped[key]["circuits"].append(f"Explicit cable callout {index + 1}")

        return [
            Terminal(
                id=stable_id("terminal", panel_id, name), name=name,
                description=description, max_mm2=rating,
                quantity=int(values["quantity"]),
                circuits=list(dict.fromkeys(values["circuits"])),
                warnings=list(dict.fromkeys(values["warnings"])),
            )
            for (name, description, rating), values in sorted(grouped.items())
        ]

    for component in components:
        calculated_outgoing = (
            not component.is_incomer
            and component.category in {"MCB", "MCCB", "RCBO"}
        )
        if not component.is_incomer and not component.has_terminal and not calculated_outgoing:
            continue
        # High-current switchboards terminate power conductors directly on
        # busbars/lugs, not rail-mounted terminal blocks.
        amps_match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*A\s*", component.amps, re.I)
        if component.is_incomer and amps_match and float(amps_match.group(1)) > 160:
            continue
        size = parse_mm2(component.cable_size) or _default_cable_size(component)
        if not size:
            continue
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
