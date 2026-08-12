from collections import Counter

from app.dataset_benchmark import _field_metrics, _score, component_key


def test_offer_unknown_spec_is_a_wildcard_not_a_mismatch():
    expected = Counter({("MCCB", "4P", "100A", "-", "-"): 2})
    predicted = Counter({("MCCB", "4P", "100A", "C", "36KA"): 2})
    score = _score("P1", predicted, expected)
    assert score.true_positive_quantity == 2
    assert score.f1 == 1.0


def test_concrete_offer_spec_must_match():
    expected = Counter({("MCB", "1P", "16A", "C", "6KA"): 1})
    predicted = Counter({("MCB", "1P", "20A", "C", "6KA"): 1})
    assert _score("P1", predicted, expected).f1 == 0.0


def test_spec_normalization_handles_engineering_notation():
    assert component_key({"category": "MCB", "poles": "1P+N", "amps": "16"})[:3] == (
        "MCB", "1PN", "16A"
    )
    assert component_key({"category": "SPD", "spd_type": "Type 2 / Class C, Imax 20kA"}) == (
        "SPD", "TYPE2"
    )


def test_field_metrics_do_not_turn_one_missing_field_into_a_total_row_failure():
    expected = {"P1": Counter({("MCB", "1PN", "16A", "C", "6KA"): 2})}
    predicted = {"P1": Counter({("MCB", "1PN", "16A", "-", "6KA"): 2})}
    fields = _field_metrics(["P1"], predicted, expected)

    assert fields["amps"]["f1"] == 1.0
    assert fields["breaking_capacity"]["f1"] == 1.0
    assert fields["curve"]["recall"] == 0.0
