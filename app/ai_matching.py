from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from pydantic import BaseModel, Field

from .ai_extraction import model_name
from .catalog import ensure_catalog_schema
from .domain import CandidateMatch, Component
from .offer_defaults import canonical_manufacturer
from .pricing import component_description


AI_MATCH_PROMPT_VERSION = "catalog-reranker-v2-manufacturer-locked"


class MatchAdvice(BaseModel):
    candidate_id: str | None = Field(
        description="One supplied candidate_id, or null when none should be recommended"
    )
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(description="One concise estimator-facing reason")
    cautions: list[str] = Field(default_factory=list)


def _api_key() -> str:
    configured = os.getenv("GEMINI_API_KEY", "").strip()
    if configured:
        return configured
    env_file = Path(".venv/key.env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("GEMINI_API_KEY="):
                configured = line.split("=", 1)[1].strip()
                if configured:
                    return configured
    raise RuntimeError("Gemini API key is not configured")


def _candidate_id(candidate: CandidateMatch) -> str:
    return f"{candidate.manufacturer}|{candidate.reference}"


def _request_payload(
    component: Component, candidates: list[CandidateMatch]
) -> dict:
    return {
        "requested": {
            "description": component_description(component),
            "category": component.category,
            "poles": component.poles,
            "current": component.amps,
            "curve": component.curve,
            "breaking_capacity": component.breaking_capacity,
            "sensitivity": component.sensitivity,
            "manufacturer": component.manufacturer,
        },
        "candidates": [
            {
                "candidate_id": _candidate_id(candidate),
                "manufacturer": candidate.manufacturer,
                "reference": candidate.reference,
                "description": candidate.description,
                "category": candidate.category,
                "product_kind": candidate.product_kind,
                "family": candidate.family,
                "poles": candidate.poles,
                "current": candidate.amps,
                "sensitivity": candidate.sensitivity,
                "price": candidate.price,
                "currency": candidate.currency,
                "deterministic_reason": candidate.reason,
                "requires_engineering_review": candidate.requires_engineering_review,
            }
            for candidate in candidates[:8]
        ],
    }


def _cache_key(model: str, payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256()
    digest.update(AI_MATCH_PROMPT_VERSION.encode("utf-8"))
    digest.update(model.encode("utf-8"))
    digest.update(body.encode("utf-8"))
    return digest.hexdigest()


def ask_ai_for_recommendation(
    database: str | Path,
    component: Component,
    candidates: list[CandidateMatch],
) -> tuple[CandidateMatch | None, MatchAdvice, bool]:
    """Rerank a deterministic shortlist; never search or invent catalog products."""
    requested_manufacturer = canonical_manufacturer(component.manufacturer)
    allowed = (
        [
            candidate for candidate in candidates
            if canonical_manufacturer(candidate.manufacturer) == requested_manufacturer
        ]
        if requested_manufacturer and requested_manufacturer != "-"
        else list(candidates)
    )
    shortlist = allowed[:8]
    if not shortlist:
        return None, MatchAdvice(
            candidate_id=None,
            confidence=1.0,
            summary="No electrically compatible catalog candidates were supplied.",
            cautions=[],
        ), True
    selected_model = model_name()
    payload = _request_payload(component, shortlist)
    cache_key = _cache_key(selected_model, payload)
    with sqlite3.connect(database) as connection:
        ensure_catalog_schema(connection)
        connection.commit()
        cached = connection.execute(
            "SELECT response_json FROM ai_match_cache WHERE cache_key=?", (cache_key,)
        ).fetchone()
        if cached:
            advice = MatchAdvice.model_validate_json(cached[0])
            return _resolve_candidate(shortlist, advice), advice, True

    from google import genai
    from google.genai import types

    prompt = """
You are reviewing a deterministic shortlist of electrical catalog products.
You may select only one supplied candidate_id or return null. Never invent a
reference. Category, product kind, poles, and residual sensitivity are hard
constraints already enforced by the application. Treat any rating change or
lower breaking capacity as an engineering decision and state it as a caution.
Prefer an exact requested rating, then the same manufacturer, then the smallest
adequate higher current only where the supplied deterministic reason identifies
it as a reviewed commercial recommendation. Keep the explanation concise.
""".strip()
    client = genai.Client(api_key=_api_key())
    response = client.models.generate_content(
        model=selected_model,
        contents=[prompt, json.dumps(payload, ensure_ascii=False, sort_keys=True)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=MatchAdvice,
            http_options=types.HttpOptions(
                timeout=90_000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        ),
    )
    advice = MatchAdvice.model_validate_json(response.text)
    selected = _resolve_candidate(shortlist, advice)
    if advice.candidate_id and selected is None:
        raise RuntimeError("AI returned a catalog reference outside the supplied shortlist")
    with sqlite3.connect(database) as connection:
        ensure_catalog_schema(connection)
        connection.execute(
            """
            INSERT INTO ai_match_cache(cache_key, response_json, model)
            VALUES(?,?,?)
            ON CONFLICT(cache_key) DO UPDATE SET
                response_json=excluded.response_json,
                model=excluded.model,
                created_at=datetime('now')
            """,
            (cache_key, advice.model_dump_json(), selected_model),
        )
        connection.commit()
    return selected, advice, False


def _resolve_candidate(
    candidates: list[CandidateMatch], advice: MatchAdvice
) -> CandidateMatch | None:
    if not advice.candidate_id:
        return None
    return next(
        (candidate for candidate in candidates if _candidate_id(candidate) == advice.candidate_id),
        None,
    )
