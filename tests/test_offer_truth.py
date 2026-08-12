from app.offer_truth import parse_offer_description


def test_offer_truth_classifies_observable_and_non_observable_rows():
    breaker = parse_offer_description("DB-1", "MCB DPNN 16A 1P+N, C, 6KA", "SCHNEIDER", 4)
    terminal = parse_offer_description("DB-1", "Terminal 2x4mm2", "PHOENIX", 4)
    panel = parse_offer_description("DB-1", "Panel 15 MOD (PRISMA)", "SCHNEIDER", 1)
    assert (breaker.scope, breaker.category, breaker.poles, breaker.amps) == ("sld_visible", "MCB", "1P+N", "16A")
    assert terminal.scope == "calculated_terminal"
    assert panel.scope == "commercial_accessory"
