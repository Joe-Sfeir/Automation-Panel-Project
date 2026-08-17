# Technicat Automation — Project Handoff

Last updated: 2026-08-17
Repository branch: `main`
Base commit: `567e9c5`
Working state: verified extraction-parity and pricing-safety work remains uncommitted; do not reset, clean, or overwrite the working tree.

## Read this first

This file is the canonical continuation note for future chats. Before changing code, read:

1. `PROJECT-HANDOFF.md` — current status, decisions, findings, and next work.
2. `README.md` — setup, runtime workflow, benchmarking, and packaging.
3. `PRODUCT.md` — product purpose and engineering principles.
4. `DESIGN.md` — implemented desktop visual system.
5. `benchmark_runs/kornet_el_hamra/final-hybrid-report.json` — current measured benchmark.

The older `recap.txt` and `todo-project.txt` describe superseded Excel-based generations. They are historical context only.

## Current result

Technicat Automation is now an evidence-led Windows desktop workflow:

```text
DWG/DXF drawings
  -> deterministic CAD evidence graph + render
  -> structured Gemini extraction
  -> project.technelec.json
  -> strict per-row manufacturer/edition matching
  -> pricing.json
  -> editable offer review / recommendations / explicit row exclusions
  -> configurable client Excel offer
```

The runnable release is `dist/TechnicatAutomation/TechnicatAutomation.exe`. It uses a PyInstaller one-folder layout to avoid per-launch self-extraction and remained alive during a 12-second frozen startup smoke test. `Technicat.lnk` points to this stable path.

Verified state:

- 74 automated tests pass.
- SQLite integrity is `ok`; `pricing.db` contains 9,999 products across Schneider, Legrand, and Eaton.
- Impeccable UI finish verdict: `ship`, with no P0/P1/P2 findings.
- The review UI was rendered at 1080x680; its decision rail scrolls without overlap and keyboard focus is visible.
- All 13 supplied drawings and 17 mapped panels pass the source-preservation Kornet regression.
- The earlier 623/623 offer-parity result is historical: it depended on design normalizations and is no longer an acceptance target for this pricing-only tool.
- The packaged extraction path no longer imports `Codes/extract_spatial_json.py`. DWG conversion and DXF rendering now use the import-safe `app.cad_runtime` service, so frozen builds do not search PyInstaller's temporary `_MEI...` directory for `.venv/key.env`.
- The stable `dist/TechnicatAutomation/TechnicatAutomation.exe` was rebuilt on 2026-08-17, reached Windows input-idle, and remained alive during the frozen-app startup smoke test.

## 2026-08-17 pricing-review and catalog-taxonomy work

- Extraction completion now opens the pricing review directly. Pricing runs in the background and stops at `pricing.json`; Excel export is a separate explicit action after review.
- The offer preview mirrors the export rows and permits add, duplicate, specification, manufacturer, reference, quantity, and unit-price edits. Every edit makes the snapshot stale until **Apply & reprice** is run.
- Legacy and newly imported catalogs gain persistent product-family and structured-spec columns. Known families are classified deterministically; Gemini is asked only about unresolved reference-prefix groups and the accepted group result is cached.
- Strict matching treats category, standalone/add-on kind, poles, current, residual sensitivity, and category-relevant curve/breaking capacity as hard constraints. A Schneider 1P+N 10A iDPN Vigi RCBO can never satisfy a 4P 10A RCCB request.
- Unmatched rows expose ranked same-manufacturer/different-rating, other-manufacturer/exact-rating, and other-manufacturer/nearest-rating recommendations. Rating changes are always marked for engineering review and never auto-applied.
- Unpriced rows with deterministic recommendations automatically invoke the no-chat AI shortlist reviewer. It receives only the requested component plus at most eight supplied candidate records, must select one supplied ID or none, stores the structured response in `ai_match_cache`, and marks every applied result `ai_suggested` for explicit engineering review.
- Offer manufacturer cells are database-backed dropdowns. Changing a manufacturer immediately clears stale references/prices and reprices against that manufacturer’s latest installed edition.
- Business defaults are explicit and editable: Schneider meters use `METSEPM2220`, Eaton meters use `PXM2000`, CTs use MBS, SPDs use DEHN by default (Phoenix Contact remains selectable), multi-pole fuse holders expand to single-pole quantities, and R/Y/B pilot sets expand to individual round colored lamps.
- Calculated terminals, enclosures, plastrons, rails, and doors remain separate from source extraction but are materialized into the editable offer as amber engineering-calculated rows.
- Excel price-list imports stream their full live output in a modal terminal view. Header rows are rejected deterministically and reimports remove stale edition products.
- Currency totals are grouped independently. Mixed currencies are never summed into a synthetic grand total.
- Native Windows visual QA passed at 1460x900 and 1080x680 with the offer preview expanded and recommendations/AI controls above source evidence.

## 2026-08-13 responsiveness work

- Retired the 97 MB one-file release path in favor of the stable one-folder application.
- Actual visible-window startup improved from 4.148 seconds (one-file) to 2.207 seconds (one-folder), a 46.8% reduction in the controlled confirmation run. Three-run GUI input-idle median improved from 2.608 seconds to 0.292 seconds.
- Pricing, catalog matching, and project-result persistence run on a dedicated Qt worker thread. The review window remains responsive and exposes progress plus cooperative cancellation; Excel generation occurs only after the reviewed snapshot is explicitly exported.
- Cancellation is checked between panels/components and every 250 catalog rows.
- Drawing previews are deferred, decoded at a bounded size, and cached. Component table updates are batched, and lower schedule/candidate tables load only on tab activation.
- Rendered review QA passed at 1460x900 and the supported 1080x680 minimum in both ready and active-pricing states.

## 2026-08-13 extraction-parity work

The immediate priority is end-to-end correctness across the Kornet El Hamra samples. DB1 is a checkpoint, not the objective. UI and adaptive-router work remain paused while the universal extraction and matching mechanisms are hardened.

Implemented and verified in the current uncommitted working tree:

- Restored rich CAD context inside the canonical `app/` architecture: schematic block inserts and attributes, repeated block counts, all selected text with height/layer/coordinates, adaptive text clusters, per-instance evidence IDs, and the full drawing render.
- Added deterministic schematic rating inventories. Across all 12 ordinary single-panel Kornet drawings, explicit `1P+N` MCB groups exactly match the expected `10A/16A/20A` quantities. This is enforced by `tests/test_kornet_golden.py`; runtime code contains no panel names or expected answers.
- Added explicit RCCB grouping from the schematic RCD/RCCB marker, 4-pole rating, and sensitivity. Cached-response sensitivity F1 improved from 61.5% to 95.8%.
- Physical-layout and repeated-label inventories are cross-check evidence only. They cannot add, remove, rerate, regroup, or overwrite a structured extraction row.
- Model fallback is visible in the extraction log. Accuracy runs support `--no-fallback` to pin one model and fail instead of silently changing model quality.
- The desktop pipeline is pinned to the configured primary model. Billing-credit exhaustion never triggers a weak-model fallback; an existing response may be reused only when it came from that same primary model, with a visible warning and current deterministic revalidation.
- Pricing matching now enforces structured category, poles, current, sensitivity, and curve compatibility. It never upsizes/downsizes an observed rating; unavailable exact specifications remain visibly unmatched.
- A catalog audit of all 82 expected protective-device rows found compatible candidates for every installed manufacturer. Four Socomec MTS rows remain unmatched because that manufacturer/edition is not installed.

Historical offer-parity reconciliation (retained for diagnosis, not a release target):

| Evaluation | Result |
|---|---:|
| Expected/predicted SLD-visible quantity | 623 / 623 |
| All-panel exact specification precision | 100.0% |
| All-panel exact specification recall | 100.0% |
| All-panel exact specification F1 | 100.0% |
| Missing/extra specification groups | 0 / 0 |
| Calculated terminal schedule | Exact for all 17 panels |

DB1 completed a fresh call using `gemini-3.6-flash`. The saved primary response contains 10 observed groups. The old 11-group canonical result came from adding a calculated CT and applying other design mutations; the current regression instead requires those 10 rows to survive unchanged apart from field sanitation and the selected pricing manufacturer.

The former post-extraction engineering stage has been removed from the runtime path. It had changed fuse quantities, RCCB ratings, breaker curves, and CT content after the model response. Pricing now receives the source-preserved component BOM directly.

## Benchmark interpretation

Source dataset (read-only):

`C:\Users\Joe\OneDrive - American University of Beirut\Z_Technicat\Archive\Automation Test File 2`

The final offer `ELIE-offre-KORNET EL HAMRA .xls` is authoritative for commercial pricing/export comparison, but it is not a clean extraction BOM. It contains calculated accessories, control items not observable in the SLD, and commercial substitutions whose ratings can differ from explicit drawing labels.

Historical engineering-target results (superseded by source-preservation tests):

| Evaluation | Precision | Recall | F1 |
|---|---:|---:|---:|
| All panels, exact component/spec row | 100.0% | 100.0% | 100.0% |
| Tuning panels, exact component/spec row | 100.0% | 100.0% | 100.0% |
| Holdout panels, exact component/spec row | 100.0% | 100.0% | 100.0% |

The ATS drawing remains a distinct deterministic topology: repeated panel anchors partition ATS-1/2/3, the totalizing switchboard, and the EDL totalizing panel before component construction. This avoids whole-sheet AI collapsing and double counting. The 100% result applies to the Kornet golden dataset only; do not extrapolate it to unrelated projects without new holdout drawings.

## Architecture and decisions

- `project.technelec.json` with `schema_version: 1` is the canonical project source of truth. Saves are atomic; source files are referenced by path and SHA-256. Excel is import/export only.
- The importable services under `app/` are canonical. The UI must not chain legacy scripts or workbooks through subprocess adapters.
- CAD extraction parses blocks, attributes, nested block contents, text, layers, geometry, and scale-aware block-to-label associations. Wire topology remains intentionally out of scope.
- Schematic and physical-layout representations are never summed. Layout blocks may annotate a discrepancy but cannot change the extracted value.
- Recognition creates only evidence-backed devices. Runtime post-processing does not calculate CTs, standardize ratings, inject breaker curves, or manufacture ATS control quantities.
- Primary Gemini model: `gemini-3.6-flash`. The desktop accuracy path does not silently use `gemini-3.1-flash-lite`. Structured Pydantic output and evidence IDs are required.
- Extracted components use canonical `included` state and flow directly to strict pricing by default. Source cross-check notes do not change values; users may edit, explicitly exclude rows, reprice, and then export the reviewed snapshot.
- Each offer row’s selected manufacturer governs matching; the project manufacturer is only the fallback. Forced references are respected, exact alternatives and recommendation groups are exposed, and unmatched rows remain editable.
- Terminal schedules remain separate from extracted evidence, but calculated terminal purchasing rows are synchronized into the editable commercial offer.
- UI direction is the “Revision Takeoff Desk”: drafting-white surfaces, graphite rules, technical blue interaction, amber source cross-check notes, and explicit inclusion/exclusion scope.

## Future adaptive extraction vision

The long-term direction is an explainable, case-based extraction router:

```text
drawing fingerprint
  -> nearest compatible signed-off cases
  -> declarative extraction policy
  -> evidence-backed extraction
  -> engineer review and project sign-off
  -> trusted local learning case
```

This is the correct future architecture, but it is deliberately **not the next implementation step**. First benchmark 5–10 additional, diverse projects from different clients and drawing families. The observed successes and failures of the universal extractor must determine which fingerprint features and extraction policies are useful; do not design them from one project or optimize them around Kornet El Hamra.

Decisions for the future implementation:

- Start with case-based similarity routing, not LLM fine-tuning, neural training, or uncontrolled continuous learning. The current dataset is smaller than 50 projects and its offer-derived labels are noisy.
- Keep one configurable extractor with versioned, declarative policies. Do not create client-specific extractor scripts or store previous BOM answers as drawing features.
- Fingerprint drawing style using source modality, units/extents, scale distributions, layers, blocks, attributes, nested contents, text geometry, repeated-symbol patterns, block-to-text distances, panel regions, and hashed drawing vocabulary.
- Most historical inputs are CAD SLDs. Support vector/scanned PDF SLDs as separate modalities later; rare shop drawings are optional corroboration and must never be required or added to schematic quantities.
- Route broadly at drawing level, then refine per detected panel. Use compatible signed-off neighbours and combine similarity with agreement between their successful policies.
- If similarity or router confidence is too low, abstain: use the universal policy, mark the drawing as an unknown style, and require review. Never force a historical strategy onto a novel drawing.
- Show nearest cases, routing confidence, selected tools/settings, alternatives, and out-of-distribution status. Let the engineer override the policy before extraction.
- Treat final offers as weak evidence and review checklists only. Offer-only accessories, calculated items, substitutions, and conflicting specifications must never silently create or modify extracted components.
- Only a fully reviewed and signed-off project may become a trusted learning case. Partial edits and failed runs must not train or update the router.
- Store cases locally in a versioned SQLite database initially, with asset hashes, family tags, fingerprints, policy outcomes, approved BOMs, label quality, and import/export support.
- Rebuild the similarity index explicitly and offline after sign-off. Do not continuously retrain in the background.
- Use manually assigned client/drawing-family tags to prevent evaluation leakage, not as direct fingerprint inputs or shortcuts to known BOMs.
- Consider a learned policy-ranking model only after accumulating at least 100 signed-off panels across at least five drawing families.

Planned public interfaces are `LearningCase`, `DrawingFingerprint`, `ExtractionPolicy`, `RoutingDecision`, and `CaseStore`. Their schemas should be finalized only after the multi-project benchmark reveals stable, predictive features.

## Important lessons

- Deterministic CAD counts are more reliable than asking a vision model to count repeated devices.
- Client layer and block names vary; rules must describe geometry and electrical notation, never Kornet-specific filenames or known answers.
- Nearest-text association needs layer compatibility, scale-aware distance, and anisotropic vertical penalties; a fixed radius is not generalizable.
- Nested block text and dedicated text layers are essential for multi-panel drawings.
- Physical panel-layout blocks expose many repeated breakers, fuse holders, lamps, and meters, but layout and schematic counts represent the same devices.
- A commercial offer can contradict its drawing. Preserve source evidence, surface the conflict, and let an engineer decide during review.
- Manufacturer fields require strict validation: ratings and labels such as `36kA`, `PFR`, or `PLC` are not brands.
- Gemini quota/model availability changes. Cache by drawing hash, parser version, prompt, model, evidence, and render; retry transient 429/503 responses and fall back without hiding the actual model used.
- ATS/control drawings need their own benchmark stratum because their structure and commercial control content differ materially from ordinary distribution panels.
- In a PyInstaller one-file build, imported modules live under a temporary `_MEI...` directory. Runtime credentials and mutable configuration must resolve through Preferences/application data or explicit arguments, never relative to a bundled module's `__file__` path.

## Known limitations

- The 100% result is a Kornet-specific golden regression; cross-client/cross-template accuracy is not yet measured.
- The existing final offer mixes observed devices with engineering/commercial calculations. The application now separates those mechanisms internally, but the source workbook itself does not provide clean provenance labels for every row.
- ATS auxiliary/control quantities are topology-derived construction BOM items, not literal counts of every symbol on the single-line drawing, and remain reviewable.
- ODA File Converter remains required for native DWG input.
- The working tree includes historical user artifacts and uncommitted changes. Preserve them and inspect overlaps before editing.

## Prioritized continuation

1. Run a second, unrelated client/drawing family and create a new extraction-only golden set with explicit provenance.
2. Accept changes only when the Kornet 17-panel regression and the new cross-project holdout both remain green.
3. Extend commercial accessory/enclosure sizing as a separate scope; do not mix it into SLD component extraction.
4. Install/import the missing Socomec price list or explicitly configure another manufacturer for MTS rows before claiming end-to-end catalog coverage.
5. Build manually reviewed extraction-only BOMs for representative panels so pure extraction accuracy can be measured independently from commercial offer alignment.
6. After Kornet is stable, select 5–10 diverse historical projects and repeat the regression process before implementing the adaptive router.

## Commands

Run from the repository root in PowerShell:

```powershell
# Development app
.\.venv\Scripts\python.exe run_app.py

# Tests
.\.venv\Scripts\python.exe -m pytest -q

# Full cached dataset extraction
.\.venv\Scripts\python.exe Codes\run_dataset_extraction.py `
  "C:\Users\Joe\OneDrive - American University of Beirut\Z_Technicat\Archive\Automation Test File 2" `
  --out benchmark_runs\kornet_el_hamra\final_flash31_lite `
  --model gemini-3.1-flash-lite --resume

# Pinned accuracy run after Gemini billing is restored
.\.venv\Scripts\python.exe Codes\run_dataset_extraction.py `
  "C:\Users\Joe\OneDrive - American University of Beirut\Z_Technicat\Archive\Automation Test File 2" `
  --out benchmark_runs\kornet_el_hamra\rich_primary `
  --model gemini-3.6-flash --no-fallback

# Reapply deterministic reconciliation to cached model responses
.\.venv\Scripts\python.exe Codes\reconcile_cached_dataset.py `
  benchmark_runs\kornet_el_hamra\final_hybrid `
  --out benchmark_runs\kornet_el_hamra\reconciled_v4

# Current benchmark
.\.venv\Scripts\python.exe Codes\benchmark_dataset.py `
  benchmark_runs\kornet_el_hamra\expected-offer.json `
  benchmark_runs\kornet_el_hamra\final_hybrid `
  --manifest benchmark_runs\kornet_el_hamra\benchmark-manifest.json `
  --out benchmark_runs\kornet_el_hamra\final-hybrid-report.json

# Windows release
.\build_single_exe.ps1 -Python .\.venv\Scripts\python.exe
```

Gemini configuration belongs in Preferences or `.venv/key.env`; never commit the real key:

```env
GEMINI_API_KEY=your_key
GEMINI_API_MODEL=gemini-3.6-flash
GEMINI_FALLBACK_MODEL=gemini-3.1-flash-lite
```

## Prompt for a new chat

> Read `PROJECT-HANDOFF.md`, `README.md`, and `benchmark_runs/kornet_el_hamra/final-hybrid-report.json`. Treat them as the current project state. Verify the repository and working tree before changing anything, preserve the benchmark sources and existing uncommitted work, and continue from the first prioritized action that is not complete. Update `PROJECT-HANDOFF.md` at the end of the session with verified metrics, decisions, lessons, and the next concrete action.

## Session-close rule

At the end of every substantial session, update this file with:

- the date and base/current commit;
- tests and benchmark metrics actually run;
- decisions made and why;
- new lessons or rejected approaches;
- unresolved risks;
- the single next prioritized action.

Do not record unsupported claims, secrets, or transient speculation as project truth.
