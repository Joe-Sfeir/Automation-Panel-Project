from app.domain import Component
from app.terminals import calculate_terminals, pole_count


def test_terminal_rules_group_calculated_values():
    components = [
        Component("a", category="MCB", poles="1P+N", cable_size=2.5, quantity=3, has_terminal=True),
        Component("b", category="MCCB", poles="3P+N", cable_size=10, quantity=1, is_incomer=True),
    ]
    terminals = calculate_terminals(components, "panel")
    by_name = {item.name: item for item in terminals}
    assert by_name["PTTB 4"].quantity == 3
    assert by_name["PT 10"].quantity == 4
    assert all(item.origin == "calculated" for item in terminals)


def test_pole_count_understands_neutral():
    assert pole_count("1P+N") == 2
    assert pole_count("3P+N") == 4
    assert pole_count("3P") == 3
