# Technicat Automation desktop surface brief

## Scope and mode

Windows desktop application for an electrical estimator operating a DWG-to-offer workflow. The primary task is reviewing extracted panel BOM rows against source evidence before pricing.

## Direction

The application behaves like a revision takeoff desk: cool drafting-white work surfaces, graphite rules and typography, technical blue for the current task, amber for unresolved decisions, and green only for explicit approval. The interface uses familiar desktop navigation and dense ruled tables; it avoids generic dashboard cards, decorative motion, and console-first workflow framing.

The memorable interaction is a component decision rail: selecting a BOM row exposes its evidence, conflicts, confidence, manufacturer, and review actions in one place. Pricing stays visibly locked until every included component has an explicit decision.

Approved composition: `.impeccable/mocks/panel-review-approved.png`.

## Composition and implementation inventory

| Ingredient | Commitment | Medium |
|---|---|---|
| Workflow strip | Drawings → Extract → Review → Price → Export; current and blocked stages are explicit | Native Qt widgets |
| Panel navigator | Compact list with approved / review counts | Native Qt list |
| BOM review | Dense editable table; selected, conflict, approved, and excluded states | Native Qt table |
| Terminal schedule | Read-only calculated table below the BOM | Native Qt table |
| Decision rail | Evidence identifiers, confidence, conflicts, manufacturer and explicit decision actions | Native Qt widgets |
| Primary action | “Price approved components”; disabled with an actionable reason until the gate passes | Native Qt button and status text |
| Visual system | Square to 4 px corners, 1 px graphite/cool-gray rules, no floating-card shadows | Qt stylesheet |
| Typography | Segoe UI workhorse family; Cascadia Mono only for IDs, measurements, and logs | Installed Windows fonts |

## Constraints

- No fabricated commercial or benchmark claims.
- No client-specific extraction rules.
- Keyboard focus, scalable layouts, and non-color status cues are required.
- Large source drawings stay external; the canonical JSON stores paths, hashes, and evidence coordinates.

## Unresolved decisions

- A precise CAD-to-image coordinate transform is required before evidence crops can replace the current evidence metadata preview.
