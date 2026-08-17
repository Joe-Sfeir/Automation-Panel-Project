import pytest

from Codes.pdf_price_list import _deduplicate, _phoenix_markers, _price


def test_pdf_price_accepts_integer_and_decimal_catalog_prices():
    assert _price("description 66") == 66.0
    assert _price("description 8.4") == 8.4
    assert _price("description 1,200.50") == 1200.5


def test_phoenix_marker_rejoins_a_wrapped_reference():
    blocks = [
        (133.0, 100.0, 210.0, 108.0, "PHC-FLT-SEC-P-T1-N/PE-"),
        (156.0, 110.0, 250.0, 120.0, "350/100-P 2905473"),
    ]

    markers = _phoenix_markers(blocks, 20)

    assert len(markers) == 1
    assert markers[0].reference == "PHC-FLT-SEC-P-T1-N/PE-350/100-P"
    assert markers[0].order_number == "2905473"


def test_duplicate_pdf_rows_use_the_higher_price_for_same_order_number():
    rows = [
        {"reference": "PHC-D-ST 4", "order_number": "3030420", "description": "A", "price": 0.52},
        {"reference": "PHC-D-ST 4", "order_number": "3030420", "description": "B", "price": 0.60},
    ]

    result = _deduplicate(rows)

    assert len(result) == 1
    assert result[0]["price"] == 0.60


def test_duplicate_reference_with_different_order_numbers_is_rejected():
    rows = [
        {"reference": "PHC-X", "order_number": "1111111", "description": "A", "price": 1.0},
        {"reference": "PHC-X", "order_number": "2222222", "description": "B", "price": 1.0},
    ]

    with pytest.raises(ValueError, match="different order numbers"):
        _deduplicate(rows)
