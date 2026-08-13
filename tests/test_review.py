from app.domain import Component, Panel, ProjectDocument
from app.review import summarize_review


def test_review_summary_tracks_pricing_scope_and_engineering_notes():
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
    assert summary.included == 2
    assert summary.ready_for_pricing


def test_review_is_ready_with_included_rows_without_approval():
    project = ProjectDocument.create("Test")
    project.panels.append(Panel("p1", "P1", "", components=[
        Component("a", review_status="approved"),
        Component("b", review_status="excluded"),
    ]))
    assert summarize_review(project).ready_for_pricing


def test_engineering_note_does_not_block_pricing():
    project = ProjectDocument.create("Test")
    component = Component("a", review_status="approved", conflicts=["quantity changed"])
    project.panels.append(Panel("p1", "P1", "", components=[component]))
    assert summarize_review(project).ready_for_pricing


def test_unconfigured_manufacturer_does_not_block_export():
    project = ProjectDocument.create("Test")
    component = Component("a", review_status="approved", manufacturer="36KA")
    project.panels.append(Panel("p1", "P1", "", components=[component]))
    assert summarize_review(project).ready_for_pricing


def test_all_excluded_rows_leave_nothing_to_price():
    project = ProjectDocument.create("Test")
    project.panels.append(Panel("p1", "P1", "", components=[
        Component("a", review_status="excluded"),
    ]))
    summary = summarize_review(project)
    assert summary.included == 0
    assert not summary.ready_for_pricing
