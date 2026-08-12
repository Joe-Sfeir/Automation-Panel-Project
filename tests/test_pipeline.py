import pytest

from app.pipeline import ProjectPipeline


def test_pipeline_cancellation_preserves_completed_project(tmp_path, monkeypatch):
    calls = {"cancel": 0}

    def should_cancel():
        calls["cancel"] += 1
        return calls["cancel"] >= 2

    pipeline = ProjectPipeline(tmp_path / "pricing.db", "key", should_cancel=should_cancel)
    monkeypatch.setattr(pipeline.extraction, "extract", lambda project, drawing, directory: [])
    with pytest.raises(InterruptedError):
        pipeline.run(
            tmp_path / "project", "Test", "T", [tmp_path / "a.dwg", tmp_path / "b.dwg"],
            "SCHNEIDER", {}, price_after_extraction=False,
        )
    assert (tmp_path / "project" / "project.technelec.json").exists()
