from app.offer_truth import parse_offer_description


def test_offer_truth_classifies_observable_and_non_observable_rows():
    breaker = parse_offer_description("DB-1", "MCB DPNN 16A 1P+N, C, 6KA", "SCHNEIDER", 4)
    terminal = parse_offer_description("DB-1", "Terminal 2x4mm2", "PHOENIX", 4)
    panel = parse_offer_description("DB-1", "Panel 15 MOD (PRISMA)", "SCHNEIDER", 1)
    assert (breaker.scope, breaker.category, breaker.poles, breaker.amps) == ("sld_visible", "MCB", "1P+N", "16A")
    assert terminal.scope == "calculated_terminal"
    assert panel.scope == "commercial_accessory"


def test_offer_truth_separates_engineering_defaults_and_classifies_kwh_meter():
    pilot = parse_offer_description("DB-1", "Pilot lamp", "SCHNEIDER", 3)
    fuse = parse_offer_description("DB-1", "Fuse holder", "SCHNEIDER", 3)
    meter = parse_offer_description("DB-1", "KWHM", "SCHNEIDER", 1)
    elcb = parse_offer_description("DB-1", "ELCB DPNN 25A/300mA", "SCHNEIDER", 2)

    assert (pilot.scope, pilot.evaluation_layer) == ("engineering_standard", "control_engineering")
    assert (fuse.scope, fuse.evaluation_layer) == ("engineering_standard", "protection_auxiliary")
    assert (meter.category, meter.evaluation_layer) == ("METER", "metering")
    assert elcb.category == "RCCB"
