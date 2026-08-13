"""Evidence-led component editing and direct pricing for the Technicat project.

THESIS: Review is an editable engineering takeoff, not an approval gate or a raw data editor.
OWN-WORLD: Drafting-white workplanes, graphite rules, technical blue selection, amber source cross-check notes.
STORY: Extracted rows flow directly to pricing; choose a panel to inspect, edit, include, or exclude a row.
FIRST VIEWPORT: Panel index left, BOM and terminal tables center, evidence and detail rail right; pricing at the foot.
FORM: Revision takeoff desk, assigned direction 7, seed 707d0947.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, and DESIGN.md
"""

from __future__ import annotations

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

from .exporting import export_pricing_json
from .persistence import load_project, save_project
from .pricing import PricingService, effective_component_manufacturer
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
    """Run catalog matching and Excel generation outside the GUI thread."""

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
            if should_cancel():
                raise InterruptedError("Pricing cancelled")
            save_project(project, self.project_path)
            self.progress.emit("Generating Excel offer")
            offer_path = export_pricing_json(
                pricing_path,
                self.project_path.parent / "offer.xlsx",
                project.export.visible_columns,
                project.export.attention,
            )
            if should_cancel():
                raise InterruptedError("Pricing cancelled")
        except InterruptedError:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.finished.emit(str(offer_path))


class ProjectReviewDialog(QDialog):
    """Editable, keyboard-friendly pricing surface for a canonical project."""

    def __init__(self, project_path: str | Path, database: str | Path, parent=None):
        super().__init__(parent)
        self.project_path = Path(project_path)
        self.database = Path(database)
        self.project = load_project(self.project_path)
        for panel in self.project.panels:
            for component in panel.components:
                component.manufacturer = effective_component_manufacturer(self.project, component)
        self._panel_index = 0
        self._loading = False
        self._dirty = False
        self._pricing_thread: QThread | None = None
        self._pricing_worker: PricingExportWorker | None = None
        self._preview_cache: dict[tuple[str, int], QPixmap] = {}
        self._preview_request = 0
        self.setObjectName("reviewDialog")
        self.setWindowTitle(f"Review & price — {self.project.name}")
        self.resize(1460, 900)
        self.setMinimumSize(1080, 680)
        self._build()
        self._refresh_panel_list()
        self._load_panel(0)

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
            stage.setProperty("state", "active" if name == "Review" else "complete" if index < 3 else "pending")
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
        self.panel_filter.addItems(["All panels", "Has cross-check notes", "Has exclusions"])
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

        split = QSplitter(Qt.Vertical)
        split.setChildrenCollapsible(False)

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
        split.addWidget(self.components)

        self.lower_tabs = QTabWidget()
        self.lower_tabs.setObjectName("reviewTabs")
        self.terminals = QTableWidget()
        self.terminals.setColumnCount(4)
        self.terminals.setHorizontalHeaderLabels(["Terminal", "Description", "Qty", "Circuits / warnings"])
        self.terminals.setEditTriggers(QTableWidget.NoEditTriggers)
        self.terminals.verticalHeader().setVisible(False)
        self.terminals.horizontalHeader().setStretchLastSection(True)
        self.lower_tabs.addTab(self.terminals, "Calculated terminals")

        self.accessories = QTableWidget()
        self.accessories.setColumnCount(5)
        self.accessories.setHorizontalHeaderLabels([
            "Commercial item", "Manufacturer", "Qty", "Origin", "Calculation note",
        ])
        self.accessories.setEditTriggers(QTableWidget.NoEditTriggers)
        self.accessories.verticalHeader().setVisible(False)
        self.accessories.horizontalHeader().setStretchLastSection(True)
        self.lower_tabs.addTab(self.accessories, "Commercial accessories")

        self.matches = QTableWidget()
        self.matches.setColumnCount(8)
        self.matches.setHorizontalHeaderLabels([
            "Component", "Edition", "Chosen reference", "Score", "Tier", "Price", "Match status", "Alternatives",
        ])
        self.matches.setEditTriggers(QTableWidget.NoEditTriggers)
        self.matches.verticalHeader().setVisible(False)
        self.matches.horizontalHeader().setStretchLastSection(True)
        self.lower_tabs.addTab(self.matches, "Pricing candidates")
        self.lower_tabs.currentChanged.connect(self._load_active_lower_table)
        split.addWidget(self.lower_tabs)
        split.setSizes([550, 240])
        layout.addWidget(split, 1)
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
        self.price_button = QPushButton("Price & export offer")
        self.price_button.setShortcut("Ctrl+P")
        self.price_button.setToolTip("Price all included components and export Excel (Ctrl+P)")
        self.price_button.setProperty("variant", "primary")
        self.price_button.clicked.connect(self.save_price_export)
        layout.addWidget(self.price_button)
        self.cancel_price_button = QPushButton("Cancel pricing")
        self.cancel_price_button.setToolTip("Request cancellation after the current catalog batch")
        self.cancel_price_button.setProperty("variant", "secondary")
        self.cancel_price_button.setEnabled(False)
        self.cancel_price_button.clicked.connect(self.cancel_pricing)
        layout.addWidget(self.cancel_price_button)
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
            if filter_text == "Has cross-check notes" and not note_rows:
                continue
            if filter_text == "Has exclusions" and not excluded:
                continue
            suffix = f"{included} INCLUDED"
            if note_rows:
                suffix += f" · {note_rows} NOTES"
            item = QListWidgetItem(f"{panel.reference}\n{len(panel.components)} items · {suffix}")
            item.setData(Qt.UserRole, index)
            item.setToolTip(f"{included} included, {excluded} excluded, {note_rows} with cross-check notes")
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
            f"{summary.included} included · {summary.excluded} excluded · {summary.conflicts} cross-check notes"
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
        if component.conflicts and component.review_status != "excluded":
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
        panel.terminals = calculate_terminals(panel.components, panel.id)
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
        self._save_quiet()
        self._refresh_panel_list()
        self._show_selected_component()
        self._update_gate()

    def _show_selected_component(self) -> None:
        rows = self._selected_rows()
        enabled = bool(rows)
        for button in (self.include_button, self.exclude_button):
            button.setEnabled(enabled)
        self.open_evidence_button.setEnabled(enabled)
        if not rows or not self.project.panels:
            self.component_title.setText("Select a BOM row")
            self.component_meta.setText("Source confidence and pricing scope will appear here.")
            self.evidence_text.clear()
            self.conflict_text.setText("No component selected")
            self.evidence_preview.setText("No render available")
            self.evidence_preview.setPixmap(QPixmap())
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
        self._queue_render_preview(panel)

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
        path = next((Path(value) for value in panel.render_paths if Path(value).exists()), None)
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
        panel = self.project.panels[self._panel_index]
        if index == 0:
            self._load_terminals(panel)
        elif index == 1:
            self._load_accessories(panel)
        elif index == 2:
            self._load_matches(panel)

    def _load_terminals(self, panel) -> None:
        self.terminals.setRowCount(len(panel.terminals))
        for row, terminal in enumerate(panel.terminals):
            values = [
                terminal.name, terminal.description, terminal.quantity,
                ", ".join([*terminal.circuits, *terminal.warnings]),
            ]
            for column, value in enumerate(values):
                self.terminals.setItem(row, column, QTableWidgetItem(str(value)))
        self.terminals.resizeColumnsToContents()

    def _load_accessories(self, panel) -> None:
        self.accessories.setRowCount(len(panel.commercial_accessories))
        for row, accessory in enumerate(panel.commercial_accessories):
            values = [
                accessory.description, accessory.manufacturer, accessory.quantity,
                accessory.origin, accessory.notes,
            ]
            for column, value in enumerate(values):
                self.accessories.setItem(row, column, QTableWidgetItem(str(value)))
        self.accessories.resizeColumnsToContents()

    def _load_matches(self, panel) -> None:
        component_ids = {component.id: component for component in panel.components}
        results = [result for result in self.project.match_results if result.component_id in component_ids]
        self.matches.setRowCount(len(results))
        for row, result in enumerate(results):
            component = component_ids[result.component_id]
            alternatives = "; ".join(f"{item.reference} ({item.score:.0%})" for item in result.alternatives)
            values = [
                component.label or component.category, result.edition, result.chosen_reference,
                f"{result.score:.0%}", result.tier,
                f"{result.unit_price:.2f} {result.currency}" if result.currency else "",
                result.review_status, alternatives,
            ]
            for column, value in enumerate(values):
                self.matches.setItem(row, column, QTableWidgetItem(str(value)))
        self.matches.resizeColumnsToContents()

    def _update_gate(self) -> None:
        summary = summarize_review(self.project)
        if summary.ready_for_pricing:
            self.gate_label.setText(
                f"READY TO PRICE · {summary.included} included · {summary.excluded} excluded · "
                f"{summary.conflicts} with source cross-check notes"
            )
            self.gate_label.setProperty("state", "ready")
            self.price_button.setEnabled(True)
            self.panel_status.setText("READY TO PRICE")
            self.panel_status.setProperty("state", "complete")
        else:
            self.gate_label.setText(
                "NOTHING TO PRICE · include at least one component with a valid quantity"
            )
            self.gate_label.setProperty("state", "blocked")
            self.price_button.setEnabled(False)
            self.panel_status.setText("NO INCLUDED ITEMS")
            self.panel_status.setProperty("state", "blocked")
        for widget in (self.gate_label, self.panel_status):
            widget.style().unpolish(widget)
            widget.style().polish(widget)

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

    def save_price_export(self) -> None:
        try:
            self._apply_table(self._panel_index)
            if not self._save_quiet():
                return
            pricing_path = self.project_path.parent / "pricing.json"
            PricingService(self.database).price_project(self.project, pricing_path)
            save_project(self.project, self.project_path)
            offer_path = export_pricing_json(
                pricing_path,
                self.project_path.parent / "offer.xlsx",
                self.project.export.visible_columns,
                self.project.export.attention,
            )
            self._load_matches(self.project.panels[self._panel_index])
        except Exception as exc:
            QMessageBox.critical(self, "Pricing/export failed", str(exc))
            return
        QMessageBox.information(
            self,
            "Offer exported",
            f"All included components were priced and the Excel offer was regenerated.\n"
            f"You can keep editing and export again whenever needed.\n\n{offer_path}",
        )

    def reject(self) -> None:
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
