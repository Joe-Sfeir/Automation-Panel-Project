from __future__ import annotations

import copy
import math

from .domain import Component, Panel, ProjectDocument, stable_id


SPECIAL_MANUFACTURERS = ("MBS", "PHOENIX CONTACT")

MANUFACTURER_ALIASES = {
    "PHOENIX": "PHOENIX CONTACT",
    # Commercially, this application uses the installed Phoenix Contact list
    # for the DEHN/Phoenix surge-arrester default. Do not create a phantom DEHN
    # catalog/manufacturer entry when opening older projects.
    "DEHN": "PHOENIX CONTACT",
}

METER_DEFAULT_REFERENCES = {
    "SCHNEIDER": "METSEPM2220",
    "EATON": "EAT-F500",
}

PILOT_LAMP_DEFAULT_REFERENCES = {
    "SCHNEIDER": {
        "RED": "XB7EV04BP",
        "YELLOW": "XB7EV05BP",
        "BLUE": "XB7EV06BP",
    },
    "EATON": {
        "RED": "C22-L-R-230",
        "YELLOW": "C22-L-Y-230",
        "BLUE": "C22-L-B-230",
    },
    "LEGRAND": {
        "RED": "024161",
        "YELLOW": "024164",
        "BLUE": "024163",
    },
}

CALCULATED_COMPONENT_ORIGINS = {
    "calculated_terminal_offer",
    "calculated_accessory_offer",
}

TERMINAL_DEFAULT_REFERENCES = {
    "PTTB 4": "PHC-PTTB 4",
    "PT 4": "PHC-PT 4",
    "PT 6": "PHC-PT 6",
    "PT 10": "PHC-PT 10",
}


def _was_manually_changed(component: Component, field_name: str) -> bool:
    return any(
        record.field == field_name
        and record.reason not in {"business default", "calculated offer sync"}
        for record in component.overrides
    )


def meter_reference(manufacturer: str) -> str:
    return METER_DEFAULT_REFERENCES.get(str(manufacturer or "").strip().upper(), "")


def pilot_reference(manufacturer: str, color: str) -> str:
    return PILOT_LAMP_DEFAULT_REFERENCES.get(
        str(manufacturer or "").strip().upper(), {}
    ).get(str(color or "").strip().upper(), "")


def canonical_manufacturer(manufacturer: str) -> str:
    selected = str(manufacturer or "").strip().upper()
    return MANUFACTURER_ALIASES.get(selected, selected)


def default_manufacturer(component: Component, project_default: str) -> str:
    category = component.category.upper()
    if category == "CT":
        return "MBS"
    if category == "SPD":
        return "PHOENIX CONTACT"
    return canonical_manufacturer(component.manufacturer or project_default)


def _apply_component_defaults(component: Component, project_default: str) -> None:
    manufacturer = default_manufacturer(component, project_default)
    if manufacturer and not _was_manually_changed(component, "manufacturer"):
        component.manufacturer = manufacturer

    if component.category == "METER" and not _was_manually_changed(component, "forced_reference"):
        reference = meter_reference(component.manufacturer or project_default)
        if reference:
            component.forced_reference = reference

    if component.category == "FUSE_HOLDER" and not _was_manually_changed(component, "poles"):
        pole_text = str(component.poles or "").upper()
        pole_count = 4 if "4" in pole_text else 3 if "3" in pole_text else 2 if "2" in pole_text else 1
        if pole_count > 1:
            if not _was_manually_changed(component, "quantity"):
                component.quantity = max(1, int(component.quantity)) * pole_count
            component.poles = "1P"
            component.notes = _append_note(
                component.notes,
                f"Commercial default: {pole_count} single-pole fuse holders replace each {pole_count}P holder.",
            )


def _append_note(existing: str, addition: str) -> str:
    existing = str(existing or "").strip()
    if addition in existing:
        return existing
    return f"{existing}\n{addition}".strip()


def _expand_phase_lamps(panel: Panel, project_default: str) -> None:
    expanded: list[Component] = []
    for component in panel.components:
        phase_set = component.category == "PILOT_LAMP" and str(component.poles).upper() in {
            "R/Y/B", "R/G/B", "RYB", "RGB",
        }
        already_split = component.origin == "business_default_phase_lamp"
        if not phase_set or already_split or _was_manually_changed(component, "forced_reference"):
            expanded.append(component)
            continue

        per_color = max(1, math.ceil(max(1, int(component.quantity)) / 3))
        manufacturer = (component.manufacturer or project_default).strip().upper()
        for color in ("RED", "YELLOW", "BLUE"):
            item = copy.deepcopy(component)
            item.id = stable_id("phase-lamp", component.id, color)
            item.label = f"Round pilot light {color.title()}"
            item.poles = color
            item.quantity = per_color
            item.manufacturer = manufacturer
            item.forced_reference = pilot_reference(manufacturer, color)
            item.origin = "business_default_phase_lamp"
            item.notes = _append_note(
                item.notes,
                "Commercial default: individual round red/yellow/blue pilot light; verify supply voltage.",
            )
            expanded.append(item)
    panel.components = expanded


def sync_calculated_offer_components(panel: Panel) -> None:
    desired: dict[str, Component] = {}
    for terminal in panel.terminals:
        component_id = stable_id("offer-terminal", terminal.id)
        desired[component_id] = Component(
            id=component_id,
            category="ACCESSORY",
            label=f"{terminal.name} — {terminal.description}",
            quantity=max(1, int(terminal.quantity)),
            manufacturer="PHOENIX CONTACT",
            forced_reference=TERMINAL_DEFAULT_REFERENCES.get(terminal.name, ""),
            origin="calculated_terminal_offer",
            notes=_append_note(
                "; ".join(terminal.warnings),
                "Engineering-calculated terminal quantity; verify before export.",
            ),
        )
    for accessory in panel.commercial_accessories:
        component_id = stable_id("offer-accessory", accessory.id)
        desired[component_id] = Component(
            id=component_id,
            category="ACCESSORY",
            label=accessory.description,
            quantity=max(1, int(accessory.quantity)),
            manufacturer=accessory.manufacturer,
            origin="calculated_accessory_offer",
            notes=_append_note(
                accessory.notes,
                "Engineering-calculated enclosure hardware; verify before export.",
            ),
        )

    existing = {component.id: component for component in panel.components}
    retained = [
        component for component in panel.components
        if component.origin not in CALCULATED_COMPONENT_ORIGINS or component.id in desired
    ]
    retained_ids = {component.id for component in retained}
    for component_id, calculated in desired.items():
        current = existing.get(component_id)
        if current is None:
            retained.append(calculated)
            continue
        if not _was_manually_changed(current, "quantity"):
            current.quantity = calculated.quantity
        if not _was_manually_changed(current, "manufacturer"):
            current.manufacturer = calculated.manufacturer
        current.label = calculated.label
        current.notes = calculated.notes
        if component_id not in retained_ids:
            retained.append(current)
    panel.components = retained


def apply_offer_defaults(project: ProjectDocument) -> None:
    project_default = project.pricing.default_manufacturer.strip().upper()
    for panel in project.panels:
        _expand_phase_lamps(panel, project_default)
        for component in panel.components:
            _apply_component_defaults(component, project_default)
        sync_calculated_offer_components(panel)


def apply_manufacturer_change(component: Component, manufacturer: str) -> None:
    selected = canonical_manufacturer(manufacturer)
    component.edit("manufacturer", selected, "offer manufacturer dropdown")
    component.edit("forced_reference", "", "offer manufacturer dropdown")
    component.edit("forced_unit_price", None, "offer manufacturer dropdown")
    component.edit("forced_currency", "", "offer manufacturer dropdown")
    component.ai_review_required = False
    component.ai_suggestion_note = ""
    if component.category == "METER":
        reference = meter_reference(selected)
        if reference:
            component.edit("forced_reference", reference, "business default")
    elif component.category == "PILOT_LAMP":
        reference = pilot_reference(selected, component.poles)
        if reference:
            component.edit("forced_reference", reference, "business default")
