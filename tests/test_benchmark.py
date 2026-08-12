from app.benchmark import evaluate
from app.domain import Component, Panel, ProjectDocument


def project_with(quantity):
    project = ProjectDocument.create("Benchmark")
    project.panels.append(Panel("p", "P", "", components=[
        Component("c", category="MCB", poles="1P", amps="16A", quantity=quantity)
    ]))
    return project


def test_benchmark_reports_quantity_and_f1():
    report = evaluate(project_with(4), project_with(5))
    assert report.true_positive_quantity == 4
    assert report.precision == 1.0
    assert report.recall == 0.8
    assert report.f1 == 0.888889
    assert not report.exact_quantity_agreement
