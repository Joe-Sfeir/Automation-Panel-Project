# Technicat Automation Platform

Windows desktop tooling for extracting electrical-panel BOMs from DWG/DXF single-line diagrams, reviewing the evidence, matching each component against the correct manufacturer price-list edition, and exporting a client offer.

For the verified current state, benchmark interpretation, decisions, and next work, start with [`PROJECT-HANDOFF.md`](PROJECT-HANDOFF.md). The older `recap.txt` and `todo-project.txt` are archived historical notes.

## Canonical workflow

The application uses one versioned file per project:

```text
DWG / DXF drawings
      ↓
CAD evidence graph + full-sheet render
      ↓
Gemini structured extraction
      ↓
project.technelec.json  ← editable source of truth
      ↓
pricing.json            ← strict matches + review recommendations
      ↓
editable offer preview  ← add, modify, exclude, or choose a recommendation
      ↓
offer.xlsx              ← explicit client-facing export
```

`project.technelec.json` is the internal source of truth. Excel is used only at system boundaries; it is not an interchange format between extraction and pricing.

Each project can contain multiple source drawings and panels. Drawing paths and SHA-256 hashes are stored in the project JSON; renders, evidence graphs, and debug output remain as files in the adjacent `artifacts` directory.

## Desktop usage

1. Install Python 3.11+ and the runtime dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. For native DWG input, install ODA File Converter. DXF input does not require ODA.

3. Add the Gemini key and model in Preferences. The legacy-compatible environment file is `.venv/key.env`:

   ```env
   GEMINI_API_KEY=your_key
   GEMINI_API_MODEL=gemini-3.6-flash
   GEMINI_FALLBACK_MODEL=gemini-3.1-flash-lite
   ```

4. Start the desktop application:

   ```powershell
   python run_app.py
   ```

   A packaged build can instead be started directly from `dist\TechnicatAutomation.exe`. For that build, enter the key under **Preferences**; it is stored in the current Windows user's application-data folder, not embedded in the executable.

5. Add one or more drawings to the project manifest and select the available price-list editions. After extraction, the app opens **Price & review** and runs strict catalog pricing. The offer preview is editable: add or duplicate rows, change specifications, select manufacturers from the catalog dropdown, edit references or unit prices, and explicitly exclude anything outside the offer. Unmatched rows are automatically checked by AI against only their deterministic shortlist; an applied suggestion is amber, labeled for engineering review, and never hidden as an exact match. Calculated terminals and enclosure hardware appear in the same offer as amber calculated rows. Use **Apply & reprice** after changes, then **Export offer** when the snapshot is ready.

Price-list imports open a live terminal-style progress window. It streams column detection, extracted counts, family-classification progress, API rate-limit retries, validation, and database commit results while the import runs.

The active UI calls the importable services in `app/` directly. The older scripts in `Codes/` remain only as extraction/import compatibility modules and are not used as workbook-to-workbook subprocess adapters.

## Project schema

Schema version 1 exposes these main records:

- `ProjectDocument`: metadata, drawings, panels, pricing/export configuration, and match results.
- `Panel`: stable ID, drawing relationship, extraction/review state, components, terminals, and artifact paths.
- `Component`: stable ID, electrical specifications, quantity, manufacturer, forced reference, confidence, evidence, conflicts, and override history.
- `MatchResult`: component ID, selected edition/product, strict-match alternatives, safe recommendations, score/tier, price/currency, and review state.

Writes are atomic. Legacy `.sld.json` extraction files can be loaded and migrated in memory through `app.persistence.load_project`; their component and terminal sections remain separate.

## Benchmarking

The repository includes a normalized Kornet El Hamra truth set derived from the user-designated final offer, plus an explicit tuning/holdout manifest. Run the whole-dataset comparison with:

```powershell
python Codes/reconcile_cached_dataset.py benchmark_runs/kornet_el_hamra/final --out benchmark_runs/kornet_el_hamra/reconciled_current
python Codes/benchmark_dataset.py benchmark_runs/kornet_el_hamra/expected-offer.json benchmark_runs/kornet_el_hamra/reconciled_current --manifest benchmark_runs/kornet_el_hamra/benchmark-manifest.json --out benchmark-report.json
```

For canonical-project-to-project comparisons, use:

```powershell
python Codes/benchmark.py predicted/project.technelec.json verified/project.technelec.json --out benchmark-report.json
```

The report includes specification-aware precision, recall, F1, exact quantity agreement, missing/extra quantities, and silent high-confidence contradictions. Release targets are:

- 100% quantity agreement for block-backed devices.
- At least 95% component/specification F1 on the holdout drawings.
- Zero silent high-confidence contradictions.

These are acceptance targets, not general production accuracy claims. The dataset report separates the tuning and untouched holdout subsets and reports category-only, exact-row, per-field, and block-backed quantity scores.

The current Kornet El Hamra regression covers all 13 drawings and 17 mapped panels. Its release invariant is now source preservation: deterministic cross-checks may annotate discrepancies but may not add, remove, resize, regroup, or rerate structured extraction rows. The designated final offer remains comparison evidence only because it includes design calculations and substitutions that are not necessarily printed in the SLD. DB1 specifically guards the raw 10-row model result against the former reconciliation mutations.

## Tests

Install development dependencies and run the suite:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The current tests cover schema persistence/migration, CAD spatial association, terminal rules, manufacturer/edition selection, forced references, configurable Excel export, and benchmark metrics. UI smoke and interaction tests can run offscreen with `QT_QPA_PLATFORM=offscreen` on CI.

## Packaging

Build the Windows one-folder application from the repository root:

```powershell
.\build_app.ps1
```

The release uses a PyInstaller one-folder layout at `dist/TechnicatAutomation/`. This avoids unpacking the approximately 97 MB application on every launch while retaining the canonical Python services, pricing database, and runtime assets. Build it with `build_app.ps1`; the compatibility `build_single_exe.ps1` wrapper now delegates to the same fast-start build.
