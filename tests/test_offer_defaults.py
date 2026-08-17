from app.domain import CommercialAccessory, Component, Panel, ProjectDocument, Terminal
from app.offer_defaults import apply_manufacturer_change, apply_offer_defaults


def test_business_defaults_and_calculated_material_enter_offer_components():
    project = ProjectDocument.create("Defaults")
    project.pricing.default_manufacturer = "SCHNEIDER"
    panel = Panel(
        "p1",
        "P1",
        "",
        components=[
            Component("meter", category="METER", manufacturer="SCHNEIDER"),
            Component("ct", category="CT", amps="100/5", quantity=3),
            Component("spd", category="SPD"),
            Component("fuse", category="FUSE_HOLDER", poles="3P", amps="32A"),
            Component("lamps", category="PILOT_LAMP", poles="R/Y/B", quantity=3),
        ],
        terminals=[Terminal("t1", "PT 4", "Terminal 4mm²", 4.0, 6)],
        commercial_accessories=[
            CommercialAccessory("a1", "Plastron", 4, "SCHNEIDER")
        ],
    )
    project.panels.append(panel)

    apply_offer_defaults(project)

    by_id = {component.id: component for component in panel.components}
    assert by_id["meter"].forced_reference == "METSEPM2220"
    assert by_id["ct"].manufacturer == "MBS"
    assert by_id["spd"].manufacturer == "PHOENIX CONTACT"
    assert by_id["fuse"].poles == "1P"
    assert by_id["fuse"].quantity == 3

    phase_lamps = [
        component for component in panel.components
        if component.origin == "business_default_phase_lamp"
    ]
    assert {component.poles for component in phase_lamps} == {"RED", "YELLOW", "BLUE"}
    assert {component.forced_reference for component in phase_lamps} == {
        "XB7EV04BP", "XB7EV05BP", "XB7EV06BP",
    }
    assert all(component.quantity == 1 for component in phase_lamps)
    assert any(component.origin == "calculated_terminal_offer" for component in panel.components)
    assert any(component.origin == "calculated_accessory_offer" for component in panel.components)
    terminal_offer = next(
        component for component in panel.components
        if component.origin == "calculated_terminal_offer"
    )
    assert terminal_offer.category == "ACCESSORY"
    assert terminal_offer.forced_reference == "PHC-PT 4"


def test_manufacturer_change_clears_stale_catalog_data_and_updates_meter_default():
    component = Component(
        "meter", category="METER", manufacturer="SCHNEIDER",
        forced_reference="METSEPM2220", forced_unit_price=302.47,
        forced_currency="EUR",
    )

    apply_manufacturer_change(component, "EATON")

    assert component.manufacturer == "EATON"
    assert component.forced_reference == "EAT-F500"
    assert component.forced_unit_price is None
    assert component.forced_currency == ""


def test_legacy_dehn_selection_uses_existing_phoenix_catalog_name():
    component = Component("spd", category="SPD", manufacturer="SCHNEIDER")

    apply_manufacturer_change(component, "DEHN")

    assert component.manufacturer == "PHOENIX CONTACT"
