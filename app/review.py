from __future__ import annotations

from dataclasses import dataclass, field

from .domain import Component, Panel, ProjectDocument


INCLUDED_REVIEW_STATES = {"included", "needs_review", "approved"}
FINAL_REVIEW_STATES = {"included", "approved", "excluded"}


@dataclass(frozen=True)
class ReviewIssue:
    panel_id: str
    component_id: str
    label: str
    message: str


@dataclass(frozen=True)
class ReviewSummary:
    total: int = 0
    approved: int = 0
    needs_review: int = 0
    excluded: int = 0
    conflicts: int = 0
    issues: list[ReviewIssue] = field(default_factory=list)

    @property
    def included(self) -> int:
        """Rows that will be sent to pricing, regardless of legacy review state."""
        return self.total - self.excluded

    @property
    def ready_for_pricing(self) -> bool:
        return self.included > 0 and not self.issues


def component_issues(panel: Panel, component: Component, default_manufacturer: str) -> list[ReviewIssue]:
    label = component.label if component.label not in {"", "-"} else component.category
    issues: list[ReviewIssue] = []
    if component.review_status == "excluded":
        return issues
    if component.quantity < 1:
        issues.append(ReviewIssue(panel.id, component.id, label, "Quantity must be at least 1."))
    return issues


def summarize_review(project: ProjectDocument) -> ReviewSummary:
    total = approved = needs_review = excluded = conflicts = 0
    issues: list[ReviewIssue] = []
    for panel in project.panels:
        for component in panel.components:
            total += 1
            state = component.review_status
            approved += int(state == "approved")
            excluded += int(state == "excluded")
            needs_review += int(state == "needs_review")
            conflicts += int(bool(component.conflicts) and state != "excluded")
            issues.extend(component_issues(panel, component, project.pricing.default_manufacturer))
    return ReviewSummary(total, approved, needs_review, excluded, conflicts, issues)


def manufacturer_is_configured(project: ProjectDocument, component: Component) -> bool:
    manufacturer = (component.manufacturer or project.pricing.default_manufacturer).strip().upper()
    configured = {project.pricing.default_manufacturer.strip().upper(), *project.pricing.editions.keys()}
    return bool(manufacturer and manufacturer in configured)


def assert_ready_for_pricing(project: ProjectDocument) -> None:
    """Validate only data required to run pricing; review decisions are optional.

    The historical function name remains as a compatibility boundary for callers.
    Extraction/reconciliation notes and unconfigured manufacturers are intentionally
    non-blocking: pricing records them as visible unmatched results when necessary.
    """
    summary = summarize_review(project)
    if summary.ready_for_pricing:
        return
    if not summary.included:
        raise ValueError("Pricing cannot continue because there are no included components.")
    preview = "; ".join(f"{issue.label}: {issue.message}" for issue in summary.issues[:5])
    remaining = len(summary.issues) - min(5, len(summary.issues))
    if remaining:
        preview += f"; and {remaining} more"
    raise ValueError(f"Pricing cannot continue because component data is invalid. {preview}")
