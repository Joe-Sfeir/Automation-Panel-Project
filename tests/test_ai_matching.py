import sqlite3

from app.ai_matching import (
    MatchAdvice,
    _cache_key,
    _candidate_id,
    _request_payload,
    ask_ai_for_recommendation,
)
from app.catalog import ensure_catalog_schema
from app.domain import CandidateMatch, Component


def candidates(count=10):
    return [
        CandidateMatch(
            reference=f"REF-{index}",
            description=f"RCCB 4P {25 + index}A 30mA",
            price=50.0 + index,
            currency="EUR",
            score=0.9 - index / 100,
            manufacturer="SCHNEIDER",
            category="RCCB",
            product_kind="standalone",
            poles="4P",
            amps=f"{25 + index}A",
            sensitivity="30mA",
            recommendation_kind="same_manufacturer_nearest",
            reason="Deterministic shortlist candidate",
            requires_engineering_review=True,
        )
        for index in range(count)
    ]


def test_ai_payload_contains_only_requested_item_and_eight_allowed_candidates():
    component = Component(
        "c", category="RCCB", poles="4P", amps="10A", sensitivity="30mA"
    )
    payload = _request_payload(component, candidates())

    assert set(payload) == {"requested", "candidates"}
    assert len(payload["candidates"]) == 8
    assert [row["reference"] for row in payload["candidates"]] == [
        f"REF-{index}" for index in range(8)
    ]
    assert all("candidate_id" in row for row in payload["candidates"])


def test_cached_ai_decision_uses_only_a_supplied_candidate_without_api_call(
    tmp_path, monkeypatch
):
    database = tmp_path / "pricing.db"
    shortlist = candidates(3)
    component = Component(
        "c", category="RCCB", poles="4P", amps="10A", sensitivity="30mA"
    )
    model = "test-model"
    payload = _request_payload(component, shortlist)
    cache_key = _cache_key(model, payload)
    advice = MatchAdvice(
        candidate_id=_candidate_id(shortlist[1]),
        confidence=0.88,
        summary="Best of the supplied candidates.",
        cautions=["Rating change requires review."],
    )
    with sqlite3.connect(database) as connection:
        ensure_catalog_schema(connection)
        connection.execute(
            "INSERT INTO ai_match_cache(cache_key, response_json, model) VALUES(?,?,?)",
            (cache_key, advice.model_dump_json(), model),
        )
        connection.commit()
    monkeypatch.setattr("app.ai_matching.model_name", lambda: model)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    selected, returned_advice, cached = ask_ai_for_recommendation(
        database, component, shortlist
    )

    assert cached
    assert selected is shortlist[1]
    assert returned_advice.candidate_id == _candidate_id(shortlist[1])


def test_no_deterministic_candidates_returns_none_without_opening_database(tmp_path):
    selected, advice, cached = ask_ai_for_recommendation(
        tmp_path / "missing" / "pricing.db", Component("c"), []
    )

    assert selected is None
    assert advice.candidate_id is None
    assert cached


def test_ai_cannot_override_an_explicit_manufacturer_with_cross_brand_candidate(
    tmp_path,
):
    component = Component(
        "spd", category="SPD", manufacturer="PHOENIX CONTACT"
    )
    schneider = CandidateMatch(
        reference="A9L15597", description="Schneider 3P SPD", price=148.5,
        currency="EUR", score=0.9, manufacturer="SCHNEIDER", category="SPD",
        product_kind="standalone", recommendation_kind="other_manufacturer_exact",
    )

    selected, advice, cached = ask_ai_for_recommendation(
        tmp_path / "missing" / "pricing.db", component, [schneider]
    )

    assert selected is None
    assert advice.candidate_id is None
    assert cached
