from __future__ import annotations

from dataclasses import dataclass, field

from .domain import Component, Panel, ProjectDocument


INCLUDED_REVIEW_STATES = {"needs_review", "approved"}
FINAL_REVIEW_STATES = {"approved", "excluded"}


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
    def ready_for_pricing(self) -> bool:
        return self.total > 0 and not self.issues


def component_issues(panel: Panel, component: Component, default_manufacturer: str) -> list[ReviewIssue]:
    label = component.label if component.label not in {"", "-"} else component.category
    issues: list[ReviewIssue] = []
    if component.review_status not in FINAL_REVIEW_STATES:
        issues.append(ReviewIssue(panel.id, component.id, label, "Choose Approve or Exclude."))
    if component.review_status == "excluded":
        return issues
    if component.conflicts and not component.conflicts_acknowledged:
        issues.append(ReviewIssue(
            panel.id, component.id, label,
            "Acknowledge or resolve the extraction conflict before pricing.",
        ))
    if component.quantity < 1:
        issues.append(ReviewIssue(panel.id, component.id, label, "Quantity must be at least 1."))
    manufacturer = (component.manufacturer or default_manufacturer).strip().upper()
    if not manufacturer:
        issues.append(ReviewIssue(panel.id, component.id, label, "Select a manufacturer."))
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
            needs_review += int(state not in FINAL_REVIEW_STATES)
            conflicts += int(bool(component.conflicts) and state != "excluded")
            issues.extend(component_issues(panel, component, project.pricing.default_manufacturer))
            if (
                state != "excluded"
                and not manufacturer_is_configured(project, component)
                and not component.manufacturer_override_confirmed
            ):
                label = component.label if component.label not in {"", "-"} else component.category
                issues.append(ReviewIssue(
                    panel.id, component.id, label,
                    "Manufacturer is not configured; confirm the manual override or select an installed edition.",
                ))
    return ReviewSummary(total, approved, needs_review, excluded, conflicts, issues)


def manufacturer_is_configured(project: ProjectDocument, component: Component) -> bool:
    manufacturer = (component.manufacturer or project.pricing.default_manufacturer).strip().upper()
    configured = {project.pricing.default_manufacturer.strip().upper(), *project.pricing.editions.keys()}
    return bool(manufacturer and manufacturer in configured)


def assert_ready_for_pricing(project: ProjectDocument) -> None:
    summary = summarize_review(project)
    if summary.ready_for_pricing:
        return
    if not summary.total:
        raise ValueError("Pricing is blocked because the project has no extracted components.")
    preview = "; ".join(f"{issue.label}: {issue.message}" for issue in summary.issues[:5])
    remaining = len(summary.issues) - min(5, len(summary.issues))
    if remaining:
        preview += f"; and {remaining} more"
    raise ValueError(f"Pricing is blocked until component review is complete. {preview}")
