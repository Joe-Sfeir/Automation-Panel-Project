"""Evidence-led component editing and direct pricing for the Technicat project.

THESIS: Review is an editable engineering takeoff, not an approval gate or a raw data editor.
OWN-WORLD: Drafting-white workplanes, graphite rules, technical blue selection, amber source cross-check notes.
STORY: Extracted rows flow directly to pricing; choose a panel to inspect, edit, include, or exclude a row.
FIRST VIEWPORT: Panel index left, BOM and terminal tables center, evidence and detail rail right; pricing at the foot.
FORM: Revision takeoff desk, assigned direction 7, seed 707d0947.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, and DESIGN.md
"""

from __future__ import annotations

import copy
import sqlite3
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .ai_matching import ask_ai_for_recommendation
from .domain import CandidateMatch, Component, new_id
from .exporting import export_pricing_json
from .offer_defaults import (
    CALCULATED_COMPONENT_ORIGINS,
    SPECIAL_MANUFACTURERS,
    apply_manufacturer_change,
    apply_offer_defaults,
    sync_calculated_offer_components,
)
from .persistence import load_project, save_project
from .pricing import PricingService, component_description, effective_component_manufacturer
from .review import summarize_review
from .terminals import calculate_terminals


COMPONENT_COLUMNS = [
    ("Category", "category"), ("Label", "label"), ("Poles", "poles"),
    ("Amps", "amps"), ("Curve", "curve"), ("Breaking capacity", "breaking_capacity"),
    ("Sensitivity", "sensitivity"), ("SPD type", "spd_type"),
    ("CT ratio", "ct_ratio"), ("Meter accuracy", "meter_accuracy"),
    ("Qty", "quantity"), ("Cable mm²", "cable_size"),
    ("Terminal?", "has_terminal"), ("Incomer?", "is_incomer"),
    ("Manufacturer", "manufacturer"), ("Forced reference", "forced_reference"),
    ("Pricing scope", "review_status"),
]

STATUS_COLORS = {
    "included": QColor("#FFFFFF"),
    "approved": QColor("#FFFFFF"),
    "excluded": QColor("#ECEFF1"),
    "needs_review": QColor("#FFFFFF"),
}


class PricingExportWorker(QObject):
    """Run catalog matching outside the GUI thread; export is a separate decision."""

    progress = Signal(str)
    finished = Signal(str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, project_path: Path, database: Path):
        super().__init__()
        self.project_path = project_path
        self.database = database

    def run(self) -> None:
        def should_cancel() -> bool:
            return QThread.currentThread().isInterruptionRequested()

        try:
            project = load_project(self.project_path)
            pricing_path = self.project_path.parent / "pricing.json"
            service = PricingService(
                self.database, progress=self.progress.emit, should_cancel=should_cancel
            )
            service.price_project(project, pricing_path)
            suggested = 0
            by_component = {
                component.id: component
                for panel in project.panels
                for component in panel.components
            }
            for result in list(project.match_results):
                if should_cancel():
                    raise InterruptedError("Pricing cancelled")
                if result.review_status != "unmatched" or not result.recommendations:
                    continue
                component = by_component.get(result.component_id)
                if component is None:
                    continue
                self.progress.emit(
                    f"AI reviewing constrained shortlist: {component_description(component)}"
                )
                try:
                    candidate, advice, _ = ask_ai_for_recommendation(
                        self.database, component, result.recommendations
                    )
                except Exception as exc:
                    self.progress.emit(f"AI fallback unavailable; item remains unpriced: {exc}")
                    continue
                if candidate is None:
                    continue
                component.edit(
                    "manufacturer", candidate.manufacturer,
                    "automatic AI shortlist suggestion",
                )
                component.edit(
                    "forced_reference", candidate.reference,
                    "automatic AI shortlist suggestion",
                )
                component.forced_unit_price = None
                component.forced_currency = ""
                component.ai_review_required = True
                component.ai_suggestion_note = advice.summary
                if candidate.manufacturer and candidate.year and candidate.half:
                    project.pricing.editions[candidate.manufacturer] = {
                        "year": candidate.year,
                        "half": candidate.half,
                    }
                suggested += 1
            if suggested:
                self.progress.emit(
                    f"Repricing {suggested} AI-suggested item(s); engineer review required"
                )
                service.price_project(project, pricing_path)
            if should_cancel():
                raise InterruptedError("Pricing cancelled")
            save_project(project, self.project_path)
        except InterruptedError:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.finished.emit(str(pricing_path))


class AiRecommendationWorker(QObject):
    """Ask AI to rerank a deterministic shortlist without exposing a chat surface."""

    finished = Signal(object, object, bool)
    failed = Signal(str)

    def __init__(
        self, database: Path, component: Component, candidates: list[CandidateMatch]
    ) -> None:
        super().__init__()
        self.database = database
        self.component = component
        self.candidates = candidates

    def run(self) -> None:
        try:
            selected, advice, cached = ask_ai_for_recommendation(
                self.database, self.component, self.candidates
            )
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.finished.emit(selected, advice, cached)


class ProjectReviewDialog(QDialog):
    """Editable, keyboard-friendly pricing surface for a canonical project."""

    def __init__(self, project_path: str | Path, database: str | Path, parent=None):
        super().__init__(parent)
        self.project_path = Path(project_path)
        self.database = Path(database)
        self.project = load_project(self.project_path)
        apply_offer_defaults(self.project)
        self._manufacturer_choices = self._load_manufacturer_choices()
        self._panel_index = 0
        self._loading = False
        self._dirty = False
        self._pricing_thread: QThread | None = None
        self._pricing_worker: PricingExportWorker | None = None
        self._pricing_outcome: tuple[str, str] | None = None
        self._ai_thread: QThread | None = None
        self._ai_worker: AiRecommendationWorker | None = None
        self._ai_candidate: CandidateMatch | None = None
        self._recommendation_items: list[CandidateMatch] = []
        self._pricing_stale = not bool(self.project.match_results)
        self._auto_pricing = self._pricing_stale and self.database.exists()
        self._preview_cache: dict[tuple[str, int], QPixmap] = {}
        self._preview_request = 0
        self.setObjectName("reviewDialog")
        self.setWindowTitle(f"Price & review — {self.project.name}")
        self.resize(1460, 900)
        self.setMinimumSize(1080, 680)
        self._build()
        self._refresh_panel_list()
        self._load_panel(0)
        if self.project.match_results:
            self.lower_tabs.setCurrentIndex(2)
        if self._auto_pricing:
            QTimer.singleShot(0, self.apply_reprice)

    def _load_manufacturer_choices(self) -> list[str]:
        values = set(SPECIAL_MANUFACTURERS)
        self._manufacturer_editions = {
            str(name).strip().upper(): {
                "year": int(config.get("year", 0) or 0),
                "half": str(config.get("half", "H1")).upper(),
            }
            for name, config in self.project.pricing.editions.items()
        }
        values.update(str(name).strip().upper() for name in self.project.pricing.editions)
        if self.database.exists():
            try:
                with sqlite3.connect(self.database) as connection:
                    for name, year, half in connection.execute(
                        """SELECT m.name, pe.year, pe.half
                           FROM ppl_editions pe
                           JOIN manufacturers m ON m.id=pe.manufacturer_id
                           ORDER BY pe.year DESC,
                             CASE pe.half WHEN 'H2' THEN 2 ELSE 1 END DESC"""
                    ):
                        manufacturer = str(name).strip().upper()
                        values.add(manufacturer)
                        self._manufacturer_editions.setdefault(
                            manufacturer,
                            {"year": int(year), "half": str(half).upper()},
                        )
            except sqlite3.Error:
                pass
        return sorted(value for value in values if value)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_workflow_strip())
        root.addWidget(self._build_drawing_register())

        workspace = QSplitter(Qt.Horizontal)
        workspace.setObjectName("reviewWorkspace")
        workspace.setChildrenCollapsible(False)
        workspace.addWidget(self._build_panel_navigator())
        workspace.addWidget(self._build_tables())
        workspace.addWidget(self._build_decision_rail())
        workspace.setStretchFactor(0, 0)
        workspace.setStretchFactor(1, 1)
        workspace.setStretchFactor(2, 0)
        workspace.setSizes([235, 930, 295])
        root.addWidget(workspace, 1)
        root.addWidget(self._build_footer())

    def _build_workflow_strip(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("workflowStrip")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(18, 10, 18, 10)
        layout.setSpacing(0)
        brand = QLabel("TECHNICAT AUTOMATION")
        brand.setObjectName("workbenchBrand")
        layout.addWidget(brand)
        layout.addSpacing(28)
        for index, name in enumerate(("Drawings", "Extract", "Review", "Price", "Export"), 1):
            stage = QLabel(f"{index}  {name.upper()}")
            stage.setObjectName("workflowStage")
            stage.setProperty(
                "state",
                "active" if name == "Price" else "complete" if index < 4 else "pending",
            )
            layout.addWidget(stage)
        layout.addStretch(1)
        self.project_label = QLabel(self.project.reference or self.project.name)
        self.project_label.setObjectName("projectReference")
        layout.addWidget(self.project_label)
        return frame

    def _build_drawing_register(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("drawingRegister")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 7, 12, 7)
        layout.setSpacing(8)
        label = QLabel("SOURCE DRAWINGS")
        label.setObjectName("railTitle")
        layout.addWidget(label)
        self.drawing_selector = QComboBox()
        self.drawing_selector.setAccessibleName("Filter panels by source drawing")
        self.drawing_selector.addItem("All drawings", "")
        panel_counts = {}
        for panel in self.project.panels:
            panel_counts[panel.source_drawing_id] = panel_counts.get(panel.source_drawing_id, 0) + 1
        for drawing in self.project.source_drawings:
            self.drawing_selector.addItem(
                f"{drawing.filename}  ·  {panel_counts.get(drawing.id, 0)} panel(s)", drawing.id
            )
        self.drawing_selector.currentIndexChanged.connect(self._refresh_panel_list)
        layout.addWidget(self.drawing_selector, 1)
        self.open_drawing_button = QPushButton("Open selected drawing")
        self.open_drawing_button.setProperty("variant", "secondary")
        self.open_drawing_button.setShortcut("Alt+D")
        self.open_drawing_button.setToolTip("Open the selected source drawing (Alt+D)")
        self.open_drawing_button.clicked.connect(self._open_selected_drawing)
        layout.addWidget(self.open_drawing_button)
        return frame

    def _build_panel_navigator(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("panelNavigator")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(10)
        title = QLabel("PANEL NAVIGATOR")
        title.setObjectName("railTitle")
        layout.addWidget(title)

        self.panel_filter = QComboBox()
        self.panel_filter.addItems([
            "All panels", "Unpriced", "Needs pricing review",
            "Has cross-check notes", "Has exclusions",
        ])
        self.panel_filter.setAccessibleName("Filter panels by source cross-check notes or exclusions")
        self.panel_filter.currentIndexChanged.connect(self._refresh_panel_list)
        layout.addWidget(self.panel_filter)

        reference_row = QHBoxLayout()
        self.panel_reference_edit = QComboBox()
        self.panel_reference_edit.setEditable(True)
        self.panel_reference_edit.setInsertPolicy(QComboBox.NoInsert)
        self.panel_reference_edit.setAccessibleName("Editable panel reference")
        rename_button = QPushButton("Rename")
        rename_button.setProperty("variant", "secondary")
        rename_button.clicked.connect(self._rename_panel)
        reference_row.addWidget(self.panel_reference_edit, 1)
        reference_row.addWidget(rename_button)
        layout.addLayout(reference_row)

        self.panel_list = QListWidget()
        self.panel_list.setObjectName("panelList")
        self.panel_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.panel_list.setAccessibleName("Panels in this project")
        self.panel_list.currentItemChanged.connect(self._panel_selected)
        layout.addWidget(self.panel_list, 1)

        self.panel_totals = QLabel()
        self.panel_totals.setObjectName("railSummary")
        self.panel_totals.setWordWrap(True)
        layout.addWidget(self.panel_totals)
        return frame

    def _build_tables(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        table_header = QFrame()
        table_header.setObjectName("tableToolbar")
        header_layout = QHBoxLayout(table_header)
        header_layout.setContentsMargins(12, 8, 12, 8)
        self.panel_heading = QLabel("EXTRACTED BOM")
        self.panel_heading.setObjectName("tableTitle")
        header_layout.addWidget(self.panel_heading)
        header_layout.addStretch(1)
        self.panel_status = QLabel()
        self.panel_status.setObjectName("statusChip")
        header_layout.addWidget(self.panel_status)
        layout.addWidget(table_header)

        self.table_split = QSplitter(Qt.Vertical)
        self.table_split.setChildrenCollapsible(False)

        self.components = QTableWidget()
        self.components.setObjectName("reviewTable")
        self.components.setColumnCount(len(COMPONENT_COLUMNS))
        self.components.setHorizontalHeaderLabels([label for label, _ in COMPONENT_COLUMNS])
        self.components.setAlternatingRowColors(False)
        self.components.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.components.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.components.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.components.verticalHeader().setVisible(False)
        self.components.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.components.horizontalHeader().setStretchLastSection(True)
        self.components.itemSelectionChanged.connect(self._show_selected_component)
        self.components.itemChanged.connect(self._table_changed)
        self.column_view = QComboBox()
        self.column_view.addItems(["Review columns", "All specifications"])
        self.column_view.setAccessibleName("Choose visible component columns")
        self.column_view.currentIndexChanged.connect(self._set_column_view)
        header_layout.insertWidget(1, self.column_view)
        self.add_component_button = QPushButton("Add item")
        self.add_component_button.setProperty("variant", "secondary")
        self.add_component_button.setToolTip("Add a manually specified offer item")
        self.add_component_button.clicked.connect(self._add_component)
        header_layout.insertWidget(2, self.add_component_button)
        self.duplicate_component_button = QPushButton("Duplicate")
        self.duplicate_component_button.setProperty("variant", "secondary")
        self.duplicate_component_button.setToolTip("Duplicate the selected component specifications")
        self.duplicate_component_button.clicked.connect(self._duplicate_component)
        self.duplicate_component_button.setEnabled(False)
        header_layout.insertWidget(3, self.duplicate_component_button)
        self.table_split.addWidget(self.components)

        self.lower_tabs = QTabWidget()
        self.lower_tabs.setObjectName("reviewTabs")
        self.terminals = QTableWidget()
        self.terminals.setColumnCount(4)
        self.terminals.setHorizontalHeaderLabels(["Terminal", "Description", "Qty", "Circuits / warnings"])
        self.terminals.setEditTriggers(QTableWidget.NoEditTriggers)
        self.terminals.verticalHeader().setVisible(False)
        self.terminals.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((95, 260, 55)):
            self.terminals.setColumnWidth(column, width)
        self.lower_tabs.addTab(self.terminals, "Calculated terminals")

        self.accessories = QTableWidget()
        self.accessories.setColumnCount(5)
        self.accessories.setHorizontalHeaderLabels([
            "Commercial item", "Manufacturer", "Qty", "Origin", "Calculation note",
        ])
        self.accessories.setEditTriggers(QTableWidget.NoEditTriggers)
        self.accessories.verticalHeader().setVisible(False)
        self.accessories.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((260, 105, 55, 150)):
            self.accessories.setColumnWidth(column, width)
        self.lower_tabs.addTab(self.accessories, "Commercial accessories")

        self.matches = QTableWidget()
        self.matches.setObjectName("offerPreviewTable")
        self.matches.setColumnCount(10)
        self.matches.setHorizontalHeaderLabels([
            "Requested component", "Manufacturer", "Catalog reference", "Qty",
            "Unit price", "Total", "Currency", "Match status", "Tier", "Decision note",
        ])
        self.matches.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.matches.setSelectionMode(QAbstractItemView.SingleSelection)
        self.matches.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.matches.verticalHeader().setVisible(False)
        self.matches.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((190, 105, 140, 52, 88, 88, 65, 115, 58, 260)):
            self.matches.setColumnWidth(column, width)
        self.matches.itemSelectionChanged.connect(self._offer_selected)
        self.matches.itemChanged.connect(self._offer_changed)
        self.lower_tabs.addTab(self.matches, "Offer preview")
        self.lower_tabs.currentChanged.connect(self._load_active_lower_table)
        self.table_split.addWidget(self.lower_tabs)
        self.table_split.setSizes([550, 240])
        layout.addWidget(self.table_split, 1)
        return container

    def _build_decision_rail(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setObjectName("decisionRailScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(285)
        rail = QFrame()
        rail.setObjectName("decisionRail")
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        title = QLabel("COMPONENT DETAILS")
        title.setObjectName("railTitle")
        layout.addWidget(title)

        self.component_title = QLabel("Select a BOM row")
        self.component_title.setObjectName("decisionTitle")
        self.component_title.setWordWrap(True)
        layout.addWidget(self.component_title)

        self.component_meta = QLabel("Source confidence and pricing scope will appear here.")
        self.component_meta.setObjectName("decisionMeta")
        self.component_meta.setWordWrap(True)
        self.component_meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.component_meta)

        recommendation_label = QLabel("PRICING RECOMMENDATIONS")
        recommendation_label.setObjectName("railSection")
        layout.addWidget(recommendation_label)
        self.match_summary = QLabel("Price the offer to see exact matches and alternatives.")
        self.match_summary.setObjectName("decisionMeta")
        self.match_summary.setWordWrap(True)
        layout.addWidget(self.match_summary)
        self.recommendations = QListWidget()
        self.recommendations.setObjectName("recommendationList")
        self.recommendations.setAccessibleName("Catalog recommendations for selected component")
        self.recommendations.setMaximumHeight(190)
        self.recommendations.currentRowChanged.connect(self._recommendation_selected)
        layout.addWidget(self.recommendations)
        self.use_recommendation_button = QPushButton("Use selected recommendation")
        self.use_recommendation_button.setProperty("variant", "secondary")
        self.use_recommendation_button.setEnabled(False)
        self.use_recommendation_button.clicked.connect(self._use_selected_recommendation)
        layout.addWidget(self.use_recommendation_button)
        self.ask_ai_button = QPushButton("Ask AI")
        self.ask_ai_button.setProperty("variant", "secondary")
        self.ask_ai_button.setToolTip(
            "Ask AI to review only the deterministic shortlist; no chat window is opened"
        )
        self.ask_ai_button.setEnabled(False)
        self.ask_ai_button.clicked.connect(self._ask_ai)
        self.ask_ai_button.setVisible(False)
        self.ai_advice = QLabel(
            "Unpriced rows are automatically checked against their constrained catalog shortlist. "
            "AI-added rows are highlighted and always require review."
        )
        self.ai_advice.setObjectName("conflictText")
        self.ai_advice.setWordWrap(True)
        layout.addWidget(self.ai_advice)

        section = QLabel("SOURCE EVIDENCE")
        section.setObjectName("railSection")
        layout.addWidget(section)
        self.evidence_preview = QLabel("No render available")
        self.evidence_preview.setObjectName("evidencePreview")
        self.evidence_preview.setAlignment(Qt.AlignCenter)
        self.evidence_preview.setMinimumHeight(150)
        layout.addWidget(self.evidence_preview)
        self.evidence_text = QTextEdit()
        self.evidence_text.setObjectName("evidenceText")
        self.evidence_text.setReadOnly(True)
        self.evidence_text.setMaximumHeight(145)
        layout.addWidget(self.evidence_text)
        self.open_evidence_button = QPushButton("Open source evidence")
        self.open_evidence_button.setProperty("variant", "secondary")
        self.open_evidence_button.setShortcut("Alt+O")
        self.open_evidence_button.setToolTip(
            "Open the source drawing for the selected evidence handles (Alt+O)"
        )
        self.open_evidence_button.setEnabled(False)
        self.open_evidence_button.clicked.connect(self._open_component_evidence)
        layout.addWidget(self.open_evidence_button)

        conflict_label = QLabel("SOURCE CROSS-CHECK NOTES")
        conflict_label.setObjectName("railSection")
        layout.addWidget(conflict_label)
        self.conflict_text = QLabel("No component selected")
        self.conflict_text.setObjectName("conflictText")
        self.conflict_text.setWordWrap(True)
        layout.addWidget(self.conflict_text)

        layout.addStretch(1)

        self.include_button = QPushButton("Include in pricing")
        self.include_button.setShortcut("Alt+I")
        self.include_button.setToolTip("Include selected rows in pricing (Alt+I)")
        self.include_button.setProperty("variant", "secondary")
        self.exclude_button = QPushButton("Exclude selected")
        self.exclude_button.setShortcut("Alt+E")
        self.exclude_button.setToolTip("Exclude selected rows from pricing (Alt+E)")
        self.exclude_button.setProperty("variant", "danger")
        for button in (self.include_button, self.exclude_button):
            button.setEnabled(False)
            layout.addWidget(button)
        self.include_button.clicked.connect(lambda: self._set_selected_status("included"))
        self.exclude_button.clicked.connect(lambda: self._set_selected_status("excluded"))
        scroll.setWidget(rail)
        return scroll

    def _build_footer(self) -> QWidget:
        footer = QFrame()
        footer.setObjectName("reviewFooter")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(14, 10, 14, 10)
        self.gate_label = QLabel()
        self.gate_label.setObjectName("gateLabel")
        layout.addWidget(self.gate_label, 1)
        self.save_state = QLabel("All changes saved")
        self.save_state.setObjectName("saveState")
        layout.addWidget(self.save_state)
        self.save_button = QPushButton("Save changes")
        self.save_button.setShortcut("Ctrl+S")
        self.save_button.setToolTip("Save component edits (Ctrl+S)")
        self.save_button.setProperty("variant", "secondary")
        self.save_button.clicked.connect(self.save)
        layout.addWidget(self.save_button)
        self.price_button = QPushButton("Apply & reprice")
        self.price_button.setShortcut("Ctrl+P")
        self.price_button.setToolTip("Save changed rows and recalculate catalog pricing (Ctrl+P)")
        self.price_button.setProperty("variant", "primary")
        self.price_button.clicked.connect(self.apply_reprice)
        layout.addWidget(self.price_button)
        self.cancel_price_button = QPushButton("Cancel pricing")
        self.cancel_price_button.setToolTip("Request cancellation after the current catalog batch")
        self.cancel_price_button.setProperty("variant", "secondary")
        self.cancel_price_button.setEnabled(False)
        self.cancel_price_button.clicked.connect(self.cancel_pricing)
        layout.addWidget(self.cancel_price_button)
        self.export_button = QPushButton("Export offer")
        self.export_button.setShortcut("Ctrl+E")
        self.export_button.setToolTip("Export the reviewed pricing snapshot to Excel (Ctrl+E)")
        self.export_button.setProperty("variant", "secondary")
        self.export_button.clicked.connect(self.export_offer)
        layout.addWidget(self.export_button)
        self.close_button = QPushButton("Close")
        self.close_button.setProperty("variant", "secondary")
        self.close_button.clicked.connect(self.reject)
        layout.addWidget(self.close_button)
        return footer

    def _refresh_panel_list(self) -> None:
        if not hasattr(self, "panel_list"):
            return
        selected_id = self.project.panels[self._panel_index].id if self.project.panels else ""
        filter_text = self.panel_filter.currentText() if hasattr(self, "panel_filter") else "All panels"
        drawing_id = self.drawing_selector.currentData() if hasattr(self, "drawing_selector") else ""
        self.panel_list.blockSignals(True)
        self.panel_list.clear()
        selected_item = None
        for index, panel in enumerate(self.project.panels):
            if drawing_id and panel.source_drawing_id != drawing_id:
                continue
            excluded = sum(item.review_status == "excluded" for item in panel.components)
            included = len(panel.components) - excluded
            note_rows = sum(bool(item.conflicts or item.notes) for item in panel.components if item.review_status != "excluded")
            component_ids = {item.id for item in panel.components if item.review_status != "excluded"}
            panel_results = [
                result for result in self.project.match_results
                if result.component_id in component_ids
            ]
            unpriced = sum(result.review_status == "unmatched" for result in panel_results)
            if not self._pricing_stale:
                unpriced += max(0, len(component_ids) - len(panel_results))
            review_rows = sum(
                result.review_status in {"check_candidate", "manual_selection", "ai_suggested"}
                for result in panel_results
            )
            if filter_text == "Unpriced" and not unpriced:
                continue
            if filter_text == "Needs pricing review" and not review_rows:
                continue
            if filter_text == "Has cross-check notes" and not note_rows:
                continue
            if filter_text == "Has exclusions" and not excluded:
                continue
            suffix = f"{included} INCLUDED"
            if note_rows:
                suffix += f" · {note_rows} NOTES"
            if unpriced:
                suffix += f" · {unpriced} UNPRICED"
            if review_rows:
                suffix += f" · {review_rows} CHECK"
            item = QListWidgetItem(f"{panel.reference}\n{len(panel.components)} items · {suffix}")
            item.setData(Qt.UserRole, index)
            item.setToolTip(
                f"{included} included, {excluded} excluded, {unpriced} unpriced, "
                f"{review_rows} requiring pricing review, {note_rows} with cross-check notes"
            )
            self.panel_list.addItem(item)
            if panel.id == selected_id:
                selected_item = item
        self.panel_list.blockSignals(False)
        if selected_item is not None:
            self.panel_list.setCurrentItem(selected_item)
        elif self.panel_list.count():
            self.panel_list.setCurrentRow(0)
        summary = summarize_review(self.project)
        self.panel_totals.setText(
            f"{len(self.project.panels)} panels\n"
            f"{summary.included} included · {summary.excluded} excluded · "
            f"{self._pricing_counts()['unpriced']} unpriced · "
            f"{self._pricing_counts()['review']} check"
        )

    def _panel_selected(self, current, previous) -> None:
        if current is None or self._loading:
            return
        try:
            self._apply_table(self._panel_index)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid component value", str(exc))
            if previous is not None:
                self.panel_list.blockSignals(True)
                self.panel_list.setCurrentItem(previous)
                self.panel_list.blockSignals(False)
            return
        self._panel_index = int(current.data(Qt.UserRole))
        self._load_panel(self._panel_index)

    def _load_panel(self, index: int) -> None:
        if not self.project.panels:
            self.components.setRowCount(0)
            self._update_gate()
            return
        self._loading = True
        panel = self.project.panels[index]
        self.panel_reference_edit.blockSignals(True)
        self.panel_reference_edit.clear()
        self.panel_reference_edit.addItem(panel.reference)
        self.panel_reference_edit.setCurrentText(panel.reference)
        self.panel_reference_edit.blockSignals(False)
        self.panel_heading.setText(f"EXTRACTED BOM — {panel.reference}")
        self.components.setUpdatesEnabled(False)
        self.components.blockSignals(True)
        try:
            self.components.setRowCount(len(panel.components))
            for row, component in enumerate(panel.components):
                for column, (_, attribute) in enumerate(COMPONENT_COLUMNS):
                    value = getattr(component, attribute)
                    if attribute == "review_status":
                        value = "Excluded" if component.review_status == "excluded" else "Included"
                    item = QTableWidgetItem(str(value))
                    if attribute == "review_status":
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    self.components.setItem(row, column, item)
                self._style_component_row(row, component)
        finally:
            self.components.blockSignals(False)
            self.components.setUpdatesEnabled(True)
        if not getattr(self, "_component_widths_initialized", False):
            widths = [92, 96, 62, 70, 58, 118, 92, 78, 82, 105, 52, 82, 72, 68, 112, 124, 100]
            for column, width in enumerate(widths):
                self.components.setColumnWidth(column, width)
            self._component_widths_initialized = True
        self._set_column_view()
        self.terminals.setRowCount(0)
        self.accessories.setRowCount(0)
        self.matches.setRowCount(0)
        self._load_active_lower_table(self.lower_tabs.currentIndex())
        self._loading = False
        if panel.components:
            self.components.selectRow(0)
        else:
            self._show_selected_component()
        self._update_gate()

    def _set_column_view(self) -> None:
        review_fields = {
            "category", "label", "poles", "amps", "quantity",
            "manufacturer", "forced_reference", "review_status",
        }
        show_all = self.column_view.currentText() == "All specifications"
        for column, (_, field) in enumerate(COMPONENT_COLUMNS):
            self.components.setColumnHidden(column, not show_all and field not in review_fields)

    def _add_component(self) -> None:
        if not self.project.panels or self._pricing_thread is not None:
            return
        try:
            self._apply_table(self._panel_index)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid component value", str(exc))
            return
        panel = self.project.panels[self._panel_index]
        panel.components.append(Component(
            id=new_id("component"),
            category="OTHER",
            label="Manual item",
            manufacturer=self.project.pricing.default_manufacturer,
            origin="manually_added",
            confidence=1.0,
            review_status="included",
        ))
        self._dirty = True
        self._mark_pricing_stale()
        self._load_panel(self._panel_index)
        self.components.selectRow(len(panel.components) - 1)

    def _duplicate_component(self) -> None:
        rows = self._selected_rows()
        if not rows or not self.project.panels or self._pricing_thread is not None:
            return
        try:
            self._apply_table(self._panel_index)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid component value", str(exc))
            return
        panel = self.project.panels[self._panel_index]
        source = panel.components[rows[0]]
        duplicate = copy.deepcopy(source)
        duplicate.id = new_id("component")
        duplicate.origin = "manually_added"
        duplicate.evidence = []
        duplicate.overrides = []
        duplicate.conflicts = []
        duplicate.notes = f"Duplicated from {source.label or source.category}"
        panel.components.insert(rows[0] + 1, duplicate)
        self._dirty = True
        self._mark_pricing_stale()
        self._load_panel(self._panel_index)
        self.components.selectRow(rows[0] + 1)

    def _drawing_for_panel(self, panel):
        return next(
            (drawing for drawing in self.project.source_drawings if drawing.id == panel.source_drawing_id),
            None,
        )

    def _open_selected_drawing(self) -> None:
        drawing_id = self.drawing_selector.currentData()
        drawing = next((item for item in self.project.source_drawings if item.id == drawing_id), None)
        if drawing is None and self.project.panels:
            drawing = self._drawing_for_panel(self.project.panels[self._panel_index])
        if drawing is None or not Path(drawing.path).exists():
            QMessageBox.warning(self, "Drawing unavailable", "The selected source drawing cannot be found.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(drawing.path))

    def _open_component_evidence(self) -> None:
        if self.project.panels and self._selected_rows():
            self._open_selected_drawing()

    def _rename_panel(self) -> None:
        if not self.project.panels:
            return
        reference = self.panel_reference_edit.currentText().strip()
        if not reference:
            QMessageBox.warning(self, "Panel reference required", "Enter a panel reference before renaming.")
            return
        panel = self.project.panels[self._panel_index]
        if panel.reference == reference:
            return
        panel.reference = reference
        panel.review_status = "included"
        self._dirty = True
        self._save_quiet()
        self._refresh_panel_list()
        self.panel_heading.setText(f"EXTRACTED BOM — {reference}")

    def _style_component_row(self, row: int, component) -> None:
        base = STATUS_COLORS.get(component.review_status, STATUS_COLORS["needs_review"])
        if (
            component.conflicts
            or component.origin in CALCULATED_COMPONENT_ORIGINS
            or component.ai_review_required
        ) and component.review_status != "excluded":
            base = QColor("#FFE8B0")
        for column in range(self.components.columnCount()):
            item = self.components.item(row, column)
            if item:
                item.setBackground(base)
                item.setToolTip("; ".join(component.conflicts) if component.conflicts else component.notes)

    def _table_changed(self, _item) -> None:
        if self._loading:
            return
        self._dirty = True
        self._mark_pricing_stale()
        self.save_state.setText("Unsaved edits")
        self.save_state.setProperty("state", "dirty")
        self.save_state.style().unpolish(self.save_state)
        self.save_state.style().polish(self.save_state)

    def _apply_table(self, index: int) -> None:
        if self._loading or not self.project.panels:
            return
        panel = self.project.panels[index]
        for row, component in enumerate(panel.components):
            for column, (_, attribute) in enumerate(COMPONENT_COLUMNS):
                if attribute == "review_status":
                    continue
                item = self.components.item(row, column)
                if item is None:
                    continue
                value = item.text().strip()
                if attribute == "quantity":
                    try:
                        value = max(1, int(value))
                    except ValueError as exc:
                        raise ValueError(f"Row {row + 1}: quantity must be an integer") from exc
                elif attribute == "cable_size":
                    try:
                        value = max(0.0, float(value.replace(",", ".")))
                    except ValueError as exc:
                        raise ValueError(f"Row {row + 1}: cable size must be numeric") from exc
                elif attribute in {"has_terminal", "is_incomer"}:
                    value = value.lower() in {"1", "true", "yes", "y", "checked"}
                elif attribute in {"category", "manufacturer"}:
                    value = value.upper()
                before = getattr(component, attribute)
                component.edit(attribute, value)
                if before != value:
                    self._dirty = True
                    self._mark_pricing_stale()
        panel.terminals = calculate_terminals(panel.components, panel.id)
        sync_calculated_offer_components(panel)
        self._update_panel_status(panel)
        if self.lower_tabs.currentIndex() == 0:
            self._load_terminals(panel)

    @staticmethod
    def _update_panel_status(panel) -> None:
        panel.review_status = "included" if any(
            item.review_status != "excluded" for item in panel.components
        ) else "excluded"

    def _selected_rows(self) -> list[int]:
        return sorted({index.row() for index in self.components.selectionModel().selectedRows()})

    def _set_selected_status(self, status: str) -> None:
        rows = self._selected_rows()
        if not rows or not self.project.panels:
            self._preview_request += 1
            return
        try:
            self._apply_table(self._panel_index)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid component value", str(exc))
            return
        panel = self.project.panels[self._panel_index]
        for row in rows:
            component = panel.components[row]
            component.review_status = status
            self._style_component_row(row, component)
            decision_item = self.components.item(row, len(COMPONENT_COLUMNS) - 1)
            if decision_item:
                decision_item.setText("Excluded" if status == "excluded" else "Included")
        self._update_panel_status(panel)
        self._dirty = True
        self._mark_pricing_stale()
        self._save_quiet()
        self._refresh_panel_list()
        self._show_selected_component()
        self._update_gate()

    def _show_selected_component(self) -> None:
        rows = self._selected_rows()
        enabled = bool(rows)
        for button in (self.include_button, self.exclude_button):
            button.setEnabled(enabled)
        self.duplicate_component_button.setEnabled(
            enabled and self._pricing_thread is None
        )
        self.open_evidence_button.setEnabled(enabled)
        if not rows or not self.project.panels:
            self.component_title.setText("Select a BOM row")
            self.component_meta.setText("Source confidence and pricing scope will appear here.")
            self.evidence_text.clear()
            self.conflict_text.setText("No component selected")
            self.evidence_preview.setText("No render available")
            self.evidence_preview.setPixmap(QPixmap())
            self._load_recommendations(None, None)
            return
        panel = self.project.panels[self._panel_index]
        component = panel.components[rows[0]]
        self.component_title.setText(f"{component.label or component.category} · {component.category}")
        manufacturer = component.manufacturer or self.project.pricing.default_manufacturer or "Not selected"
        self.component_meta.setText(
            f"Quantity  {component.quantity}\n"
            f"Manufacturer  {manufacturer}\n"
            f"Source confidence  {component.confidence:.0%}\n"
            f"Pricing scope  {'Excluded' if component.review_status == 'excluded' else 'Included'}\n"
            f"Interpretation  {'Cross-checked; extracted value preserved' if component.conflicts else 'As extracted'}"
        )
        evidence_lines = []
        for evidence in component.evidence:
            identity = evidence.source_id or evidence.entity_handle or evidence.source
            detail = evidence.text or evidence.block_name or evidence.layer
            evidence_lines.append(f"{identity}  |  {evidence.layer}  |  {detail}")
        self.evidence_text.setPlainText("\n".join(evidence_lines) if evidence_lines else "No source evidence was cited.")
        drawing = self._drawing_for_panel(panel)
        handles = [item.entity_handle for item in component.evidence if item.entity_handle]
        self.open_evidence_button.setToolTip(
            f"Open {drawing.filename if drawing else 'source drawing'}"
            + (f" · handles {', '.join(handles[:6])}" if handles else "")
            + " (Alt+O)"
        )
        conflict_lines = [*component.conflicts]
        if component.notes:
            conflict_lines.append(component.notes)
        self.conflict_text.setText("\n".join(conflict_lines) if conflict_lines else "No source discrepancy noted.")
        result = next(
            (item for item in self.project.match_results if item.component_id == component.id),
            None,
        )
        self._load_recommendations(component, result)
        self._queue_render_preview(panel)

    def _load_recommendations(self, component, result) -> None:
        self.recommendations.blockSignals(True)
        self.recommendations.clear()
        self._recommendation_items = []
        self._ai_candidate = None
        self.ai_advice.setText(
            "AI assistance is on demand and never changes a row automatically."
        )
        if component is None:
            self.match_summary.setText("Select a component to review its catalog decision.")
        elif self._pricing_stale:
            self.match_summary.setText(
                "Specifications changed. Apply & reprice to refresh exact matches and recommendations."
            )
        elif result is None:
            self.match_summary.setText("This component has not been priced yet.")
        else:
            price = f"{result.unit_price:.2f} {result.currency}" if result.currency else "unpriced"
            reference = result.chosen_reference or "No exact reference"
            self.match_summary.setText(
                f"{reference} · {price}\n{result.note}"
            )
            self._recommendation_items = list(result.recommendations)
            group_labels = {
                "same_manufacturer_exact": "SAME BRAND · EXACT",
                "same_manufacturer_nearest": "SAME BRAND · NEAREST RATING",
                "other_manufacturer_exact": "OTHER BRAND · EXACT",
                "other_manufacturer_nearest": "OTHER BRAND · NEAREST RATING",
            }
            for candidate in self._recommendation_items:
                warning = " · ENGINEERING CHECK" if candidate.requires_engineering_review else ""
                label = group_labels.get(candidate.recommendation_kind, "ALTERNATIVE")
                item = QListWidgetItem(
                    f"{label}{warning}\n"
                    f"{candidate.manufacturer} · {candidate.reference} · "
                    f"{candidate.amps} · {candidate.price:.2f} {candidate.currency}\n"
                    f"{candidate.reason}"
                )
                item.setToolTip(candidate.description)
                self.recommendations.addItem(item)
        self.recommendations.blockSignals(False)
        available = bool(self._recommendation_items) and not self._pricing_stale
        self.ask_ai_button.setEnabled(available and self._ai_thread is None)
        self.use_recommendation_button.setEnabled(False)

    def _recommendation_selected(self, row: int) -> None:
        self._ai_candidate = None
        self.use_recommendation_button.setText("Use selected recommendation")
        self.use_recommendation_button.setEnabled(
            0 <= row < len(self._recommendation_items) and not self._pricing_stale
        )

    def _use_selected_recommendation(self) -> None:
        candidate = self._ai_candidate
        if candidate is None:
            row = self.recommendations.currentRow()
            if row < 0 or row >= len(self._recommendation_items):
                return
            candidate = self._recommendation_items[row]
        self._apply_candidate(candidate)

    def _apply_candidate(self, candidate: CandidateMatch) -> None:
        rows = self._selected_rows()
        if not rows or not self.project.panels:
            return
        component = self.project.panels[self._panel_index].components[rows[0]]
        component.edit("manufacturer", candidate.manufacturer, "catalog recommendation")
        component.edit("forced_reference", candidate.reference, "catalog recommendation")
        component.edit("forced_unit_price", None, "catalog recommendation")
        component.edit("forced_currency", "", "catalog recommendation")
        component.ai_review_required = False
        component.ai_suggestion_note = ""
        if candidate.manufacturer and candidate.year and candidate.half:
            self.project.pricing.editions[candidate.manufacturer] = {
                "year": candidate.year,
                "half": candidate.half,
            }
        self._dirty = True
        self._mark_pricing_stale()
        self._sync_component_row(component)
        self._load_matches(self.project.panels[self._panel_index])
        self.match_summary.setText(
            f"Selected {candidate.manufacturer} {candidate.reference}. "
            "Apply & reprice to confirm its catalog price."
        )

    def _ask_ai(self) -> None:
        if self._ai_thread is not None or not self._recommendation_items:
            return
        rows = self._selected_rows()
        if not rows or not self.project.panels:
            return
        component = copy.deepcopy(
            self.project.panels[self._panel_index].components[rows[0]]
        )
        thread = QThread(self)
        worker = AiRecommendationWorker(
            self.database, component, copy.deepcopy(self._recommendation_items)
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._ai_succeeded)
        worker.failed.connect(self._ai_failed)
        for signal in (worker.finished, worker.failed):
            signal.connect(thread.quit)
            signal.connect(worker.deleteLater)
        thread.finished.connect(self._ai_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._ai_thread = thread
        self._ai_worker = worker
        self.ask_ai_button.setEnabled(False)
        self.ask_ai_button.setText("Reviewing shortlist…")
        self.ai_advice.setText(
            f"AI is reviewing {len(self._recommendation_items)} supplied catalog candidates."
        )
        thread.start()

    def _ai_succeeded(self, candidate, advice, cached: bool) -> None:
        self._ai_candidate = candidate
        cache_note = " · cached" if cached else ""
        cautions = "\n".join(f"• {value}" for value in advice.cautions)
        if candidate is None:
            self.ai_advice.setText(
                f"AI found no recommendation worth applying ({advice.confidence:.0%}{cache_note}).\n"
                f"{advice.summary}"
            )
            return
        matching_row = next((
            index for index, value in enumerate(self._recommendation_items)
            if value.manufacturer == candidate.manufacturer
            and value.reference == candidate.reference
        ), -1)
        if matching_row >= 0:
            self.recommendations.setCurrentRow(matching_row)
        self._ai_candidate = candidate
        self.use_recommendation_button.setText("Use AI recommendation")
        self.use_recommendation_button.setEnabled(True)
        self.ai_advice.setText(
            f"AI recommends {candidate.manufacturer} {candidate.reference} "
            f"({advice.confidence:.0%}{cache_note}).\n{advice.summary}"
            + (f"\n{cautions}" if cautions else "")
        )

    def _ai_failed(self, message: str) -> None:
        self.ai_advice.setText(
            f"AI assistance is unavailable: {message}\n"
            "The deterministic recommendations remain available."
        )

    def _ai_thread_finished(self) -> None:
        self._ai_thread = None
        self._ai_worker = None
        self.ask_ai_button.setText("Ask AI")
        self.ask_ai_button.setEnabled(
            bool(self._recommendation_items) and not self._pricing_stale
        )

    def _queue_render_preview(self, panel) -> None:
        self._preview_request += 1
        request = self._preview_request
        panel_id = panel.id
        render_paths = tuple(panel.render_paths)
        self.evidence_preview.setPixmap(QPixmap())
        self.evidence_preview.setText("Loading preview…")
        QTimer.singleShot(
            0, lambda: self._load_render_preview(panel_id, render_paths, request)
        )

    def _load_render_preview(
        self, panel_id: str, render_paths: tuple[str, ...], request: int
    ) -> None:
        if request != self._preview_request:
            return
        if not self.project.panels or self.project.panels[self._panel_index].id != panel_id:
            return
        path = next((Path(value) for value in render_paths if Path(value).exists()), None)
        if path is None:
            self.evidence_preview.setPixmap(QPixmap())
            self.evidence_preview.setText("No render available")
            return
        cache_key = (str(path), path.stat().st_mtime_ns)
        pixmap = self._preview_cache.get(cache_key)
        if pixmap is None:
            reader = QImageReader(str(path))
            source_size = reader.size()
            if source_size.isValid():
                source_size.scale(QSize(530, 300), Qt.KeepAspectRatio)
                reader.setScaledSize(source_size)
            image = reader.read()
            pixmap = QPixmap.fromImage(image)
            if not pixmap.isNull():
                self._preview_cache.clear()
                self._preview_cache[cache_key] = pixmap
        if pixmap.isNull():
            self.evidence_preview.setText("Render could not be loaded")
            return
        self.evidence_preview.setPixmap(pixmap.scaled(265, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _load_active_lower_table(self, index: int) -> None:
        if not self.project.panels or not hasattr(self, "lower_tabs"):
            return
        if hasattr(self, "table_split"):
            self.table_split.setSizes([340, 450] if index == 2 else [550, 240])
        panel = self.project.panels[self._panel_index]
        if index == 0:
            self._load_terminals(panel)
        elif index == 1:
            self._load_accessories(panel)
        elif index == 2:
            self._load_matches(panel)

    def _load_terminals(self, panel) -> None:
        self.terminals.setUpdatesEnabled(False)
        self.terminals.setRowCount(len(panel.terminals))
        for row, terminal in enumerate(panel.terminals):
            values = [
                terminal.name, terminal.description, terminal.quantity,
                ", ".join([*terminal.circuits, *terminal.warnings]),
            ]
            for column, value in enumerate(values):
                self.terminals.setItem(row, column, QTableWidgetItem(str(value)))
        self.terminals.setUpdatesEnabled(True)

    def _load_accessories(self, panel) -> None:
        self.accessories.setUpdatesEnabled(False)
        self.accessories.setRowCount(len(panel.commercial_accessories))
        for row, accessory in enumerate(panel.commercial_accessories):
            values = [
                accessory.description, accessory.manufacturer, accessory.quantity,
                accessory.origin, accessory.notes,
            ]
            for column, value in enumerate(values):
                self.accessories.setItem(row, column, QTableWidgetItem(str(value)))
        self.accessories.setUpdatesEnabled(True)

    def _load_matches(self, panel) -> None:
        self.matches.setUpdatesEnabled(False)
        self.matches.blockSignals(True)
        results = {result.component_id: result for result in self.project.match_results}
        components = [
            component for component in panel.components
            if component.review_status != "excluded"
        ]
        self.matches.setRowCount(len(components))
        for row, component in enumerate(components):
            result = results.get(component.id)
            manufacturer = component.manufacturer or (
                result.manufacturer if result else self.project.pricing.default_manufacturer
            )
            reference = component.forced_reference or (result.chosen_reference if result else "")
            unit_price = (
                component.forced_unit_price
                if component.forced_unit_price is not None
                else result.unit_price if result else 0.0
            )
            currency = component.forced_currency or (result.currency if result else "")
            total = round(float(unit_price or 0.0) * component.quantity, 2)
            status = "Modified" if self._pricing_stale else (
                result.review_status.replace("_", " ").title() if result else "Unpriced"
            )
            values = [
                component_description(component), manufacturer, reference, component.quantity,
                f"{float(unit_price or 0.0):.2f}", f"{total:.2f}", currency,
                status, result.tier if result else "NF", result.note if result else "Not priced yet",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, component.id)
                if column not in {2, 3, 4}:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.matches.setItem(row, column, item)
            manufacturer_combo = QComboBox()
            manufacturer_combo.setAccessibleName(
                f"Manufacturer for {component_description(component)}"
            )
            choices = list(self._manufacturer_choices)
            if manufacturer and manufacturer not in choices:
                choices.append(manufacturer)
                choices.sort()
            manufacturer_combo.addItems(choices)
            manufacturer_combo.setCurrentText(manufacturer)
            manufacturer_combo.currentTextChanged.connect(
                lambda value, component_id=component.id: self._manufacturer_changed(
                    component_id, value
                )
            )
            self.matches.setCellWidget(row, 1, manufacturer_combo)
            fill = QColor("#FFFFFF")
            if status == "Modified":
                fill = QColor("#FFF3D3")
            elif result and result.review_status == "unmatched":
                fill = QColor("#FFF5F3")
            elif result and result.review_status == "exact_match":
                fill = QColor("#E7F4EA")
            elif result and result.review_status in {"check_candidate", "manual_selection"}:
                fill = QColor("#FFF3D3")
            elif result and result.review_status == "ai_suggested":
                fill = QColor("#FFE8B0")
            for column in range(self.matches.columnCount()):
                self.matches.item(row, column).setBackground(fill)
        self.matches.blockSignals(False)
        self.matches.setUpdatesEnabled(True)

    def _manufacturer_changed(self, component_id: str, manufacturer: str) -> None:
        if self._loading or self.matches.signalsBlocked() or not self.project.panels:
            return
        panel = self.project.panels[self._panel_index]
        component = next(
            (row for row in panel.components if row.id == component_id), None
        )
        if component is None or component.manufacturer == manufacturer:
            return
        apply_manufacturer_change(component, manufacturer)
        edition = self._manufacturer_editions.get(manufacturer)
        if edition:
            self.project.pricing.editions[manufacturer] = dict(edition)
        self.project.match_results = [
            result for result in self.project.match_results
            if result.component_id != component.id
        ]
        self._dirty = True
        self._mark_pricing_stale()
        self.save_state.setText("Manufacturer changed — apply & reprice")
        self._sync_component_row(component)
        self._load_matches(panel)

    def _offer_selected(self) -> None:
        rows = self.matches.selectionModel().selectedRows() if self.matches.selectionModel() else []
        if not rows or not self.project.panels:
            return
        item = self.matches.item(rows[0].row(), 0)
        component_id = item.data(Qt.UserRole) if item else None
        panel = self.project.panels[self._panel_index]
        component_row = next(
            (index for index, component in enumerate(panel.components) if component.id == component_id),
            None,
        )
        if component_row is not None:
            self.components.selectRow(component_row)

    def _offer_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or self.matches.signalsBlocked() or not self.project.panels:
            return
        component_id = item.data(Qt.UserRole)
        panel = self.project.panels[self._panel_index]
        component = next(
            (row for row in panel.components if row.id == component_id), None
        )
        if component is None:
            return
        try:
            if item.column() == 1:
                apply_manufacturer_change(component, item.text().strip().upper())
            elif item.column() == 2:
                component.edit("forced_reference", item.text().strip())
                component.ai_review_required = False
                component.ai_suggestion_note = ""
            elif item.column() == 3:
                component.edit("quantity", max(1, int(item.text().strip())))
            elif item.column() == 4:
                raw = item.text().strip().replace(",", ".")
                value = None if raw == "" else max(0.0, float(raw))
                component.edit("forced_unit_price", value)
                component.ai_review_required = False
                component.ai_suggestion_note = ""
                if value is not None and not component.forced_currency:
                    result = next(
                        (row for row in self.project.match_results if row.component_id == component.id),
                        None,
                    )
                    component.edit(
                        "forced_currency",
                        result.currency if result and result.currency else self.project.pricing.currency,
                    )
        except ValueError:
            QMessageBox.warning(
                self, "Invalid offer value",
                "Quantity must be a whole number and unit price must be numeric.",
            )
            self._load_matches(panel)
            return
        self._dirty = True
        self._mark_pricing_stale()
        self.save_state.setText("Unsaved offer edits")
        self._sync_component_row(component)

    def _sync_component_row(self, component: Component) -> None:
        if not self.project.panels:
            return
        panel = self.project.panels[self._panel_index]
        row = next(
            (index for index, value in enumerate(panel.components) if value.id == component.id),
            None,
        )
        if row is None:
            return
        self.components.blockSignals(True)
        for column, (_, attribute) in enumerate(COMPONENT_COLUMNS):
            if attribute == "review_status":
                continue
            value = getattr(component, attribute)
            self.components.item(row, column).setText(str(value))
        self.components.blockSignals(False)

    def _update_gate(self) -> None:
        summary = summarize_review(self.project)
        pricing_running = self._pricing_thread is not None
        counts = self._pricing_counts()
        if pricing_running:
            self.gate_label.setProperty("state", "ready")
            self.price_button.setEnabled(False)
            self.export_button.setEnabled(False)
            self.panel_status.setText("PRICING")
            self.panel_status.setProperty("state", "running")
        elif summary.ready_for_pricing and self._pricing_stale:
            self.gate_label.setText(
                f"CHANGES NEED REPRICING · {summary.included} included · "
                f"{summary.excluded} excluded"
            )
            self.gate_label.setProperty("state", "blocked")
            self.price_button.setEnabled(True)
            self.export_button.setEnabled(False)
            self.panel_status.setText("MODIFIED")
            self.panel_status.setProperty("state", "blocked")
        elif summary.ready_for_pricing:
            totals = " · ".join(
                f"{value:.2f} {currency} HT"
                for currency, value in sorted(counts["totals"].items())
            ) or "0.00 HT"
            self.gate_label.setText(
                f"PRICED · {counts['exact']} exact · {counts['review']} check · "
                f"{counts['unpriced']} unpriced · {totals}"
            )
            self.gate_label.setProperty(
                "state", "ready" if counts["unpriced"] == 0 else "blocked"
            )
            self.price_button.setEnabled(True)
            self.export_button.setEnabled(bool(self.project.match_results))
            self.panel_status.setText(
                "PRICED" if counts["unpriced"] == 0 else f"{counts['unpriced']} UNPRICED"
            )
            self.panel_status.setProperty(
                "state", "complete" if counts["unpriced"] == 0 else "blocked"
            )
        else:
            self.gate_label.setText(
                "NOTHING TO PRICE · include at least one component with a valid quantity"
            )
            self.gate_label.setProperty("state", "blocked")
            self.price_button.setEnabled(False)
            self.export_button.setEnabled(False)
            self.panel_status.setText("NO INCLUDED ITEMS")
            self.panel_status.setProperty("state", "blocked")
        for widget in (self.gate_label, self.panel_status):
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def _pricing_counts(self) -> dict:
        included_ids = {
            component.id
            for panel in self.project.panels
            for component in panel.components
            if component.review_status != "excluded"
        }
        results = {
            result.component_id: result
            for result in self.project.match_results
            if result.component_id in included_ids
        }
        unpriced = sum(
            component_id not in results or results[component_id].review_status == "unmatched"
            for component_id in included_ids
        )
        review = sum(
            result.review_status in {"check_candidate", "manual_selection", "ai_suggested"}
            for result in results.values()
        )
        exact = sum(result.review_status == "exact_match" for result in results.values())
        totals: dict[str, float] = {}
        for result in results.values():
            if result.currency:
                totals[result.currency] = round(
                    totals.get(result.currency, 0.0) + result.total_price, 2
                )
        return {"unpriced": unpriced, "review": review, "exact": exact, "totals": totals}

    def _mark_pricing_stale(self) -> None:
        self._pricing_stale = True
        if hasattr(self, "export_button"):
            self.export_button.setEnabled(False)
        if hasattr(self, "gate_label") and self._pricing_thread is None:
            self._update_gate()

    def _save_quiet(self) -> bool:
        try:
            save_project(self.project, self.project_path)
        except Exception as exc:
            QMessageBox.warning(self, "Could not save review", str(exc))
            return False
        self._dirty = False
        self.save_state.setText("All changes saved")
        self.save_state.setProperty("state", "saved")
        self.save_state.style().unpolish(self.save_state)
        self.save_state.style().polish(self.save_state)
        return True

    def save(self) -> None:
        try:
            self._apply_table(self._panel_index)
        except Exception as exc:
            QMessageBox.warning(self, "Could not save review", str(exc))
            return
        self._save_quiet()
        self._refresh_panel_list()
        self._load_panel(self._panel_index)

    def apply_reprice(self) -> None:
        if self._pricing_thread is not None:
            return
        try:
            self._apply_table(self._panel_index)
            if not self._save_quiet():
                return
        except Exception as exc:
            QMessageBox.critical(self, "Pricing failed", str(exc))
            return

        thread = QThread(self)
        worker = PricingExportWorker(self.project_path, self.database)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._pricing_progress)
        worker.finished.connect(self._pricing_succeeded)
        worker.failed.connect(self._pricing_failed)
        worker.cancelled.connect(self._pricing_cancelled)
        for signal in (worker.finished, worker.failed, worker.cancelled):
            signal.connect(thread.quit)
            signal.connect(worker.deleteLater)
        thread.finished.connect(self._pricing_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._pricing_thread = thread
        self._pricing_worker = worker
        self._pricing_outcome = None
        self._set_pricing_running(True)
        thread.start()

    def save_price_export(self) -> None:
        """Backward-compatible action name used by older integrations."""
        self.apply_reprice()

    def _set_pricing_running(self, running: bool) -> None:
        self.components.setEnabled(not running)
        self.save_button.setEnabled(not running)
        self.add_component_button.setEnabled(not running)
        self.duplicate_component_button.setEnabled(not running and bool(self._selected_rows()))
        self.include_button.setEnabled(not running and bool(self._selected_rows()))
        self.exclude_button.setEnabled(not running and bool(self._selected_rows()))
        self.cancel_price_button.setEnabled(running)
        self.price_button.setEnabled(False if running else summarize_review(self.project).ready_for_pricing)
        self.price_button.setText("Pricing…" if running else "Apply & reprice")
        self.export_button.setEnabled(
            not running and not self._pricing_stale and bool(self.project.match_results)
        )
        self.ask_ai_button.setEnabled(
            not running and not self._pricing_stale and bool(self._recommendation_items)
            and self._ai_thread is None
        )
        if running:
            self.gate_label.setText("PRICING · Preparing catalog matches")
            self.gate_label.setProperty("state", "ready")
            self.panel_status.setText("PRICING")
            self.panel_status.setProperty("state", "running")
        for widget in (self.gate_label, self.panel_status):
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def _pricing_progress(self, message: str) -> None:
        self.gate_label.setText(f"PRICING · {message}")

    def cancel_pricing(self) -> None:
        if self._pricing_thread is None:
            return
        self._pricing_thread.requestInterruption()
        self.cancel_price_button.setEnabled(False)
        self.cancel_price_button.setText("Canceling…")
        self.gate_label.setText("CANCELING · finishing the current catalog batch")

    def _pricing_succeeded(self, pricing_path: str) -> None:
        self.project = load_project(self.project_path)
        self._pricing_outcome = ("success", pricing_path)

    def _pricing_failed(self, message: str) -> None:
        self._pricing_outcome = ("failed", message)

    def _pricing_cancelled(self) -> None:
        self._pricing_outcome = ("cancelled", "")

    def _pricing_thread_finished(self) -> None:
        outcome = self._pricing_outcome
        self._pricing_thread = None
        self._pricing_worker = None
        self.cancel_price_button.setText("Cancel pricing")
        self._set_pricing_running(False)
        self._load_panel(self._panel_index)
        self._update_gate()
        if outcome is None:
            return
        status, detail = outcome
        if status == "success":
            self._pricing_stale = False
            self._auto_pricing = False
            self._refresh_panel_list()
            self._load_panel(self._panel_index)
            self.lower_tabs.setCurrentIndex(2)
            self.gate_label.setText("PRICING COMPLETE · review unresolved rows before export")
            self._update_gate()
        elif status == "failed":
            self._auto_pricing = False
            QMessageBox.critical(self, "Pricing failed", detail)
        else:
            self.gate_label.setText("PRICING CANCELED · existing offer was left unchanged")

    def export_offer(self) -> None:
        if self._pricing_thread is not None:
            return
        if self._pricing_stale:
            QMessageBox.information(
                self, "Repricing required",
                "Apply & reprice the modified rows before exporting the offer.",
            )
            return
        pricing_path = self.project_path.parent / "pricing.json"
        if not pricing_path.exists() or not self.project.match_results:
            QMessageBox.information(
                self, "Nothing priced",
                "Price the offer before generating Excel.",
            )
            return
        counts = self._pricing_counts()
        if counts["unpriced"] or counts["review"]:
            answer = QMessageBox.question(
                self,
                "Export unresolved offer?",
                f"{counts['unpriced']} item(s) are unpriced and {counts['review']} require review.\n"
                "Export this pricing snapshot anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        try:
            offer_path = export_pricing_json(
                pricing_path,
                self.project_path.parent / "offer.xlsx",
                self.project.export.visible_columns,
                self.project.export.attention,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        QMessageBox.information(
            self, "Offer exported",
            f"The reviewed pricing snapshot was exported to:\n\n{offer_path}",
        )

    def reject(self) -> None:
        if self._pricing_thread is not None:
            QMessageBox.information(
                self,
                "Pricing in progress",
                "Cancel pricing and wait for it to stop before closing this window.",
            )
            return
        if self._ai_thread is not None:
            QMessageBox.information(
                self, "AI review in progress",
                "Wait for the catalog recommendation to finish before closing this window.",
            )
            return
        if self._dirty:
            answer = QMessageBox.question(
                self,
                "Save review changes?",
                "Save your component edits before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if answer == QMessageBox.Cancel:
                return
            if answer == QMessageBox.Save:
                self.save()
                if self._dirty:
                    return
        super().reject()
