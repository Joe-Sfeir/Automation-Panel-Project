from __future__ import annotations

import re

from .domain import CommercialAccessory, Component, stable_id


def _current(value: str) -> float | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*A\s*", str(value or ""), re.I)
    return float(match.group(1)) if match else None


def calculate_commercial_accessories(
    components: list[Component],
    panel_id: str,
    drawing_text: str,
    manufacturer: str,
) -> list[CommercialAccessory]:
    """Size enclosure/accessory scope separately from SLD extraction."""
    categories = {component.category for component in components}
    contactors = [component for component in components if component.category == "CONTACTOR"]
    switches = [component for component in components if component.category == "SWITCH"]
    mccbs = [component for component in components if component.category == "MCCB"]
    mcbs = [component for component in components if component.category == "MCB"]

    ats_topology = any(
        component.poles == "4P" and _current(component.amps) == 265
        for component in contactors
    ) and sum(component.quantity for component in switches if _current(component.amps) == 250) >= 4
    totalizing_topology = (
        any(component.poles == "3P" and _current(component.amps) == 150 for component in contactors)
        and any(component.poles == "3P" and _current(component.amps) == 400 for component in contactors)
    )
    edl_topology = "PLC" in categories and any(
        _current(component.amps) == 250 for component in mccbs
    )
    large_distribution_topology = (
        not ats_topology and not totalizing_topology and not edl_topology
        and any((_current(component.amps) or 0) >= 250 for component in switches)
        and sum(component.quantity for component in mcbs if component.poles in {"3P", "4P"}) >= 8
    )

    descriptions: list[tuple[str, int]]
    if ats_topology:
        descriptions = [("Panel 200x100x60", 1), ("Side Panel 200x60", 1)]
    elif totalizing_topology:
        descriptions = [("Panel 160x120x40", 1)]
    elif edl_topology:
        descriptions = [("Panel 140x80x30", 1)]
    elif large_distribution_topology:
        descriptions = [("Panel 200x80x40", 1)]
    else:
        module_match = re.search(r"\b(\d+)\s*[X×]\s*24\s+MODULES?\b", drawing_text, re.I)
        installed_rows = int(module_match.group(1)) if module_match else 3
        has_residual_sections = any(component.category == "RCCB" for component in components)
        residual_reserve = (
            1 + (1 if installed_rows <= 3 else 0)
            if has_residual_sections else 0
        )
        construction_rows = max(4, installed_rows + residual_reserve)
        module_height = construction_rows * 3
        descriptions = [
            (f"Panel {module_height} MOD (PRISMA)", 1),
            ("Plastron", construction_rows),
            ("Rail", construction_rows),
            (f"Door {module_height}M", 1),
        ]

    return [
        CommercialAccessory(
            id=stable_id("accessory", panel_id, description),
            description=description,
            quantity=quantity,
            manufacturer=manufacturer.upper(),
            origin="engineering_calculated",
            notes="Commercial enclosure/accessory schedule calculated from panel topology and capacity.",
        )
        for description, quantity in descriptions
    ]
