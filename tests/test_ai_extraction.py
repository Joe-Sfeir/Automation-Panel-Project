from app.ai_extraction import sanitize_component


def test_category_fields_are_sanitized_without_dropping_real_specs():
    spd = sanitize_component({
        "category": "spd", "amps": "20kA", "curve": "C", "breaking_capacity": "0",
        "sensitivity": "0", "spd_type": "Type 2, Imax 20kA", "ct_ratio": "0",
        "meter_accuracy": "0",
    })
    assert spd["amps"] == "-"
    assert spd["curve"] == "-"
    assert spd["breaking_capacity"] == "-"
    assert spd["spd_type"] == "Type 2, Imax 20kA"


def test_ct_ratio_is_migrated_out_of_amps():
    ct = sanitize_component({"category": "CT", "amps": "80/5", "ct_ratio": "-"})
    assert ct["amps"] == "-"
    assert ct["ct_ratio"] == "80/5"


def test_invalid_curve_and_spec_shaped_manufacturer_are_rejected():
    row = sanitize_component({
        "category": "MCCB", "curve": "100", "manufacturer": "36kA", "amps": "100"
    })
    assert row["curve"] == "-"
    assert row["manufacturer"] == "-"
    assert row["amps"] == "100A"


def test_device_labels_are_not_accepted_as_manufacturers():
    relay = sanitize_component({"category": "RELAY", "label": "PFR", "manufacturer": "PFR"})
    plc = sanitize_component({"category": "PLC", "label": "PLC", "manufacturer": "PLC"})
    phoenix = sanitize_component({"category": "TERMINAL", "manufacturer": "Phoenix"})

    assert relay["manufacturer"] == "-"
    assert plc["manufacturer"] == "-"
    assert phoenix["manufacturer"] == "PHOENIX CONTACT"
