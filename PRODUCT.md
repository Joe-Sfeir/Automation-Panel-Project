# Product

<!-- impeccable:product-schema 1 -->

## Platform

adaptive

## Users

Electrical panel estimators and engineers at Technicat who review client SLD drawings, confirm panel BOMs, select manufacturer catalog products, and prepare offers on Windows workstations.

## Product Purpose

Turn heterogeneous client DWG/DXF single-line diagrams into evidence-backed, engineer-reviewed panel BOMs and priced offers. Success means dramatically less manual takeoff work without silently converting ambiguous drawing evidence into commercial commitments.

## Positioning

Technicat combines deterministic CAD evidence, constrained AI interpretation, explicit engineer review, terminal calculation rules, and local multi-manufacturer pricing in one auditable desktop workflow.

## Operating Context

Projects may contain many drawings, multiple panels in one drawing, schematic and physical-layout duplicates, inconsistent client layer/block conventions, and one combined commercial offer. Users work with DWG/DXF, ODA File Converter, Gemini, local JSON project files, SQLite pricing editions, and Excel client offers.

## Capabilities and Constraints

- Windows desktop application built with PySide6.
- One atomic `project.technelec.json` is the project source of truth; Excel is import/export only.
- Extraction must support multiple drawings and multiple panels per drawing.
- Matching is per component manufacturer and price-list edition.
- Pricing/export is blocked until extraction conflicts and required review decisions are resolved.
- Client-specific filenames, block names, and known BOM answers must never be hardcoded into extraction logic.
- The source drawings and reference offers are read-only benchmark evidence.

## Brand Commitments

The product is an internal Technicat Group engineering tool. Existing Technicat logo assets and the company name are factual; no customer, accuracy, or performance claims may be fabricated.

## Evidence on Hand

- Local pricing database with 9,999 products across Schneider, Eaton, and Legrand.
- A real Kornet El Hamra benchmark project containing 13 DWGs and a combined final offer at the user-provided archive path.
- Existing runtime history, extraction artifacts, and source drawings in the repository.
- No measured production-wide accuracy claim exists yet; benchmark results must identify tuning and untouched holdout subsets.

## Product Principles

- Evidence before confidence.
- Engineer approval before pricing.
- Generalize across client drawing conventions.
- Preserve traceability from offer row back to drawing evidence.
- Make unresolved ambiguity visible and actionable.

## Accessibility & Inclusion

Keyboard access, visible focus, high-contrast status semantics, scalable Windows display support, and reduced motion are required.
