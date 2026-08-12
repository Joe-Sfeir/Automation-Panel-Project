# Technicat Automation — Project Handoff

Last updated: 2026-08-12  
Repository branch: `main`  
Base commit: `5e7396d`  
Working state: substantial verified work remains uncommitted; do not reset, clean, or overwrite the working tree.

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
  -> engineer review and conflict acknowledgement
  -> per-row manufacturer/edition matching
  -> pricing.json
  -> configurable client Excel offer
```

The runnable release is `dist/TechnicatAutomation.exe`. The executable was built with PyInstaller and remained alive during a 15-second offscreen startup smoke test.

Verified state:

- 38 automated tests pass.
- SQLite integrity is `ok`; `pricing.db` contains 9,999 products across Schneider, Legrand, and Eaton.
- Impeccable UI finish verdict: `ship`, with no P0/P1/P2 findings.
- The review UI was rendered at 1080x680; its decision rail scrolls without overlap and keyboard focus is visible.
- All 13 supplied drawings completed extraction using the primary/fallback model workflow.
- 100 of 100 evaluated block-backed rows have exact quantity agreement.
- The packaged extraction path no longer imports `Codes/extract_spatial_json.py`. DWG conversion and DXF rendering now use the import-safe `app.cad_runtime` service, so frozen builds do not search PyInstaller's temporary `_MEI...` directory for `.venv/key.env`.
- The corrected `dist/TechnicatAutomation.exe` was rebuilt on 2026-08-12 and remained alive during a 15-second offscreen frozen-app smoke test.

## Benchmark interpretation

Source dataset (read-only):

`C:\Users\Joe\OneDrive - American University of Beirut\Z_Technicat\Archive\Automation Test File 2`

The final offer `ELIE-offre-KORNET EL HAMRA .xls` is authoritative for commercial pricing/export comparison, but it is not a clean extraction BOM. It contains calculated accessories, control items not observable in the SLD, and commercial substitutions whose ratings can differ from explicit drawing labels.

Current measured results:

| Evaluation | Precision | Recall | F1 |
|---|---:|---:|---:|
| All panels, exact component/spec row | 72.8% | 52.0% | 60.7% |
| Non-ATS, exact component/spec row | 73.8% | 69.2% | 71.4% |
| Non-ATS, component category | 92.8% | 87.0% | 89.8% |
| Block-backed row quantity agreement | — | — | 100% (100/100) |

The ATS drawing is unusually structured and should be reported separately. Excluding the complete ATS drawing gives the most useful present-day measure for ordinary distribution panels: 89.8% category F1 and 71.4% exact specification F1.

Do not claim that the 95% extraction target has been reached. A manually verified extraction-only BOM is needed to measure that target fairly. Never alter an observed SLD value merely to reproduce a substituted offer line.

## Architecture and decisions

- `project.technelec.json` with `schema_version: 1` is the canonical project source of truth. Saves are atomic; source files are referenced by path and SHA-256. Excel is import/export only.
- The importable services under `app/` are canonical. The UI must not chain legacy scripts or workbooks through subprocess adapters.
- CAD extraction parses blocks, attributes, nested block contents, text, layers, geometry, and scale-aware block-to-label associations. Wire topology remains intentionally out of scope.
- Schematic and physical-layout representations are never summed. Layout blocks corroborate or recover inventory; unresolved differences become explicit review conflicts.
- Only explicit evidence may create a device. Do not infer customary CTs, meters, fuses, control gear, terminals, or accessories.
- Primary Gemini model: `gemini-3.6-flash`. Automatic fallback: `gemini-3.1-flash-lite`. Structured Pydantic output and evidence IDs are required.
- Every included component must be approved before pricing. Conflicts and unconfigured manufacturers require explicit acknowledgement; edits return a row to review.
- Matching uses each component's manufacturer and configured edition. Forced references are respected, candidate alternatives are exposed, and unmatched/substitution rows remain reviewable.
- Terminal schedules are calculated from reviewed component data and remain separate from extracted components.
- UI direction is the “Revision Takeoff Desk”: drafting-white surfaces, graphite rules, technical blue interaction, amber review state, and green approval.

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

- Exact non-ATS specification F1 is 71.4%, below the 95% objective. Missing/ambiguous curve, sensitivity, CT ratio, and offer-vs-drawing substitutions are the main gap.
- The existing final offer is insufficient to judge pure extraction accuracy. CTs and some control accessories appear in the offer without explicit CAD evidence.
- ATS extraction works better with the primary model than the fallback, but its commercial offer still contains non-SLD control inventory.
- ODA File Converter remains required for native DWG input.
- The working tree includes historical user artifacts and uncommitted changes. Preserve them and inspect overlaps before editing.

## Prioritized continuation

1. Select 5–10 diverse historical projects spanning different clients and drawing families; preserve their original CAD/PDF SLDs, optional shop drawings, and final offers as read-only sources.
2. Build manually reviewed extraction-only BOMs for their representative ordinary panels, keeping offer-only, calculated, commercial-only, and uncertain rows in separate scopes.
3. Run the current universal extractor unchanged to establish per-project and leave-one-family-out baselines for category, quantity, specification fields, contradictions, and review workload.
4. Record which observable drawing traits and extraction settings explain real successes or failures. Continue improving the universal extractor only through evidence-backed, cross-project changes.
5. Treat ATS/control drawings as a separate benchmark stratum and defer the adaptive router until the multi-project results justify its fingerprint features and policy variants.
6. When those results are stable, implement the local case store and explainable router described above, while preserving the universal fallback and review gate.

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
