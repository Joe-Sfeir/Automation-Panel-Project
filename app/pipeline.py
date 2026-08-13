from __future__ import annotations

from pathlib import Path

from .domain import ProjectDocument
from .exporting import export_pricing_json
from .extraction import ExtractionService
from .persistence import CANONICAL_FILENAME, load_project, save_project
from .pricing import PricingService


class ProjectPipeline:
    def __init__(self, database: str | Path, api_key: str, oda_path: str = "", progress=None, should_cancel=None):
        self.database = Path(database)
        self.progress = progress or (lambda message: None)
        self.should_cancel = should_cancel or (lambda: False)
        # Accuracy runs must stay on the configured primary model. A silent
        # downgrade changes extraction behavior and invalidates golden results.
        self.extraction = ExtractionService(
            api_key, oda_path, progress=self.progress, allow_model_fallback=False
        )
        self.pricing = PricingService(database, progress=self.progress)

    def run(
        self,
        project_dir: str | Path,
        project_name: str,
        project_reference: str,
        drawings: list[str | Path],
        default_manufacturer: str,
        editions: dict[str, dict],
        tva_rate: float = 0.11,
        pricing_mode: str = "itemized",
        price_after_extraction: bool = False,
    ) -> dict:
        directory = Path(project_dir)
        directory.mkdir(parents=True, exist_ok=True)
        project_path = directory / CANONICAL_FILENAME
        project = load_project(project_path) if project_path.exists() else ProjectDocument.create(
            project_name, project_reference
        )
        project.name = project_name
        project.reference = project_reference or project_name
        project.pricing.default_manufacturer = default_manufacturer.upper()
        # The manifest is the user's current pricing choice, not an accumulating
        # history. Re-running a project with Schneider selected must not retain
        # a Legrand/Eaton edition from an earlier run.
        project.pricing.editions = {key.upper(): value for key, value in editions.items()}
        project.pricing.tva_rate = tva_rate
        project.export.pricing_mode = pricing_mode
        for index, drawing in enumerate(drawings, 1):
            if self.should_cancel():
                raise InterruptedError("Extraction cancelled")
            self.progress(f"Extracting drawing {index}/{len(drawings)}: {Path(drawing).name}")
            self.extraction.extract(project, drawing, directory)
            save_project(project, project_path)
            if self.should_cancel():
                raise InterruptedError("Extraction cancelled")
        pricing_path = directory / "pricing.json"
        offer_path = directory / "offer.xlsx"
        pricing = None
        if price_after_extraction:
            pricing = self.pricing.price_project(project, pricing_path)
            save_project(project, project_path)
            export_pricing_json(
                pricing_path,
                offer_path,
                visible_columns=project.export.visible_columns,
                attention=project.export.attention,
            )
        return {
            "project": project,
            "project_path": str(project_path),
            "pricing_path": str(pricing_path) if pricing is not None else "",
            "offer_path": str(offer_path) if pricing is not None else "",
            "pricing": pricing,
        }
