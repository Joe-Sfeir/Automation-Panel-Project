from app.domain import Component, Panel, ProjectDocument
from app.review import summarize_review


def test_review_summary_tracks_decisions_and_conflicts():
    project = ProjectDocument.create("Test")
    project.panels.append(Panel("p1", "P1", "", components=[
        Component("a", review_status="approved", conflicts=["quantity disagreement"]),
        Component("b", review_status="needs_review"),
        Component("c", review_status="excluded", conflicts=["ignored source"]),
    ]))
    summary = summarize_review(project)
    assert summary.total == 3
    assert summary.approved == 1
    assert summary.needs_review == 1
    assert summary.excluded == 1
    assert summary.conflicts == 1
    assert not summary.ready_for_pricing


def test_review_is_ready_after_all_included_rows_are_approved():
    project = ProjectDocument.create("Test")
    project.panels.append(Panel("p1", "P1", "", components=[
        Component("a", review_status="approved"),
        Component("b", review_status="excluded"),
    ]))
    assert summarize_review(project).ready_for_pricing


def test_approved_conflict_requires_explicit_acknowledgement():
    project = ProjectDocument.create("Test")
    component = Component("a", review_status="approved", conflicts=["quantity changed"])
    project.panels.append(Panel("p1", "P1", "", components=[component]))
    assert not summarize_review(project).ready_for_pricing
    component.conflicts_acknowledged = True
    assert summarize_review(project).ready_for_pricing


def test_unconfigured_manufacturer_requires_explicit_override():
    project = ProjectDocument.create("Test")
    component = Component("a", review_status="approved", manufacturer="36KA")
    project.panels.append(Panel("p1", "P1", "", components=[component]))
    assert not summarize_review(project).ready_for_pricing
    component.manufacturer_override_confirmed = True
    assert summarize_review(project).ready_for_pricing
