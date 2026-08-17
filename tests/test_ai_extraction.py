import json

import pytest

from app.ai_extraction import call_gemini_panels, sanitize_component


def test_category_fields_are_sanitized_without_dropping_real_specs():
    spd = sanitize_component({
        "category": "spd", "amps": "20kA", "curve": "C", "breaking_capacity": "0",
        "sensitivity": "0", "spd_type": "Type 2, Imax 20kA", "ct_ratio": "0",
        "meter_accuracy": "0",
    })
    assert spd["amps"] == "-"
    assert spd["curve"] == "-"
    assert spd["breaking_capacity"] == "-"
    assert spd["spd_type"] == "TYPE 2"


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


def test_dehn_spd_name_maps_to_installed_phoenix_catalog():
    spd = sanitize_component({"category": "SPD", "manufacturer": "DEHN"})

    assert spd["manufacturer"] == "PHOENIX CONTACT"


def test_evidence_handles_cannot_leak_into_engineering_fields():
    row = sanitize_component({
        "category": "SPD", "poles": "1142701", "spd_type": "1142706",
        "manufacturer": "1142709",
        "evidence_ids": ["text:1142701", "text:1142706", "text:1142709"],
    })
    assert row["poles"] == "-"
    assert row["spd_type"] == "-"
    assert row["manufacturer"] == "-"


def test_depleted_billing_fails_without_trying_fallback(tmp_path, monkeypatch):
    calls = []

    class Models:
        def generate_content(self, **kwargs):
            calls.append(kwargs["model"])
            raise Exception("429 RESOURCE_EXHAUSTED: Your prepayment credits are depleted")

    class Client:
        def __init__(self, **_kwargs):
            self.models = Models()

    monkeypatch.setattr("google.genai.Client", Client)
    monkeypatch.setattr("app.ai_extraction.model_name", lambda: "primary-model")
    monkeypatch.setattr("app.ai_extraction.fallback_model_name", lambda: "fallback-model")

    with pytest.raises(RuntimeError, match="billing credits are depleted"):
        call_gemini_panels({}, None, "key", tmp_path)

    assert calls == ["primary-model"]


def test_depleted_billing_can_reconcile_a_stale_primary_response(tmp_path, monkeypatch):
    (tmp_path / "gemini_panels_raw.json").write_text(
        json.dumps({"panels": [{"panel_ref": "DB-1", "components": []}]}),
        encoding="utf-8",
    )
    (tmp_path / "gemini_panels_cache.json").write_text(
        json.dumps({"model": "primary-model", "request_sha256": "older-prompt"}),
        encoding="utf-8",
    )

    class Models:
        def generate_content(self, **_kwargs):
            raise Exception("429 RESOURCE_EXHAUSTED: Your prepayment credits are depleted")

    class Client:
        def __init__(self, **_kwargs):
            self.models = Models()

    messages = []
    monkeypatch.setattr("google.genai.Client", Client)
    monkeypatch.setattr("app.ai_extraction.model_name", lambda: "primary-model")
    monkeypatch.setattr("app.ai_extraction.fallback_model_name", lambda: "fallback-model")

    panels = call_gemini_panels({}, None, "key", tmp_path, progress=messages.append)

    assert panels == [{"panel_ref": "DB-1", "components": []}]
    assert any("billing is depleted" in message for message in messages)


def test_accuracy_run_can_disable_model_fallback(tmp_path, monkeypatch):
    calls = []

    class Models:
        def generate_content(self, **kwargs):
            calls.append(kwargs["model"])
            raise Exception("503 UNAVAILABLE: high demand")

    class Client:
        def __init__(self, **_kwargs):
            self.models = Models()

    monkeypatch.setattr("google.genai.Client", Client)
    monkeypatch.setattr("app.ai_extraction.model_name", lambda: "primary-model")
    monkeypatch.setattr("app.ai_extraction.fallback_model_name", lambda: "fallback-model")
    monkeypatch.setattr("app.ai_extraction.time.sleep", lambda _seconds: None)

    with pytest.raises(Exception, match="503"):
        call_gemini_panels({}, None, "key", tmp_path, allow_fallback=False)

    assert calls == ["primary-model"] * 4
