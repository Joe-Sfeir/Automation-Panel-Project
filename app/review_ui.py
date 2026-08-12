"""Evidence-led component review for the canonical Technicat project.

THESIS: Review is an engineering sign-off sheet, not a generic dashboard or a raw data editor.
OWN-WORLD: Drafting-white workplanes, graphite rules, technical blue selection, amber conflicts, green approvals.
STORY: Choose a panel, inspect and edit a row, verify its evidence, then explicitly approve or exclude it.
FIRST VIEWPORT: Panel index left, BOM and terminal tables center, evidence and decision rail right; pricing gate at the foot.
FORM: Revision takeoff desk, assigned direction 7, seed 707d0947.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, and DESIGN.md
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPixmap
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
from .pricing import PricingService
from .review import manufacturer_is_configured, summarize_review
from .terminals import calculate_terminals


COMPONENT_COLUMNS = [
    ("Category", "category"), ("Label", "label"), ("Poles", "poles"),
    ("Amps", "amps"), ("Curve", "curve"), ("Breaking capacity", "breaking_capacity"),
    ("Sensitivity", "sensitivity"), ("SPD type", "spd_type"),
    ("CT ratio", "ct_ratio"), ("Meter accuracy", "meter_accuracy"),
    ("Qty", "quantity"), ("Cable mm²", "cable_size"),
    ("Terminal?", "has_terminal"), ("Incomer?", "is_incomer"),
    ("Manufacturer", "manufacturer"), ("Forced reference", "forced_reference"),
    ("Decision", "review_status"),
]

STATUS_COLORS = {
    "approved": QColor("#E7F4EA"),
    "excluded": QColor("#ECEFF1"),
    "needs_review": QColor("#FFF4D6"),
}


class ProjectReviewDialog(QDialog):
    """Editable, keyboard-friendly sign-off surface for a canonical project."""

    def __init__(self, project_path: str | Path, database: str | Path, parent=None):
        super().__init__(parent)
        self.project_path = Path(project_path)
        self.database = Path(database)
        self.project = load_project(self.project_path)
        self._panel_index = 0
        self._loading = False
        self._dirty = False
        self.setObjectName("reviewDialog")
        self.setWindowTitle(f"Panel review — {self.project.name}")
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
        self.panel_filter.addItems(["All panels", "Needs review", "Approved"])
        self.panel_filter.setAccessibleName("Filter panels by review status")
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

        lower = QTabWidget()
        lower.setObjectName("reviewTabs")
        self.terminals = QTableWidget()
        self.terminals.setColumnCount(4)
        self.terminals.setHorizontalHeaderLabels(["Terminal", "Description", "Qty", "Circuits / warnings"])
        self.terminals.setEditTriggers(QTableWidget.NoEditTriggers)
        self.terminals.verticalHeader().setVisible(False)
        self.terminals.horizontalHeader().setStretchLastSection(True)
        lower.addTab(self.terminals, "Calculated terminals")

        self.matches = QTableWidget()
        self.matches.setColumnCount(8)
        self.matches.setHorizontalHeaderLabels([
            "Component", "Edition", "Chosen reference", "Score", "Tier", "Price", "Review", "Alternatives",
        ])
        self.matches.setEditTriggers(QTableWidget.NoEditTriggers)
        self.matches.verticalHeader().setVisible(False)
        self.matches.horizontalHeader().setStretchLastSection(True)
        lower.addTab(self.matches, "Pricing candidates")
        split.addWidget(lower)
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
        title = QLabel("COMPONENT DECISION")
        title.setObjectName("railTitle")
        layout.addWidget(title)

        self.component_title = QLabel("Select a BOM row")
        self.component_title.setObjectName("decisionTitle")
        self.component_title.setWordWrap(True)
        layout.addWidget(self.component_title)

        self.component_meta = QLabel("Evidence and conflicts will appear here.")
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

        conflict_label = QLabel("CONFLICT / NOTES")
        conflict_label.setObjectName("railSection")
        layout.addWidget(conflict_label)
        self.conflict_text = QLabel("No component selected")
        self.conflict_text.setObjectName("conflictText")
        self.conflict_text.setWordWrap(True)
        layout.addWidget(self.conflict_text)
        layout.addStretch(1)

        self.approve_button = QPushButton("Approve selected")
        self.approve_button.setShortcut("Alt+A")
        self.approve_button.setToolTip("Approve selected rows (Alt+A). Conflicts require acknowledgement.")
        self.approve_button.setProperty("variant", "approve")
        self.review_button = QPushButton("Mark needs review")
        self.review_button.setShortcut("Alt+R")
        self.review_button.setToolTip("Return selected rows to review (Alt+R)")
        self.review_button.setProperty("variant", "warning")
        self.exclude_button = QPushButton("Exclude selected")
        self.exclude_button.setShortcut("Alt+E")
        self.exclude_button.setToolTip("Exclude selected rows from pricing (Alt+E)")
        self.exclude_button.setProperty("variant", "danger")
        for button in (self.approve_button, self.review_button, self.exclude_button):
            button.setEnabled(False)
            layout.addWidget(button)
        self.approve_button.clicked.connect(lambda: self._set_selected_status("approved"))
        self.review_button.clicked.connect(lambda: self._set_selected_status("needs_review"))
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
        self.save_button = QPushButton("Save review")
        self.save_button.setShortcut("Ctrl+S")
        self.save_button.setToolTip("Save review edits (Ctrl+S)")
        self.save_button.setProperty("variant", "secondary")
        self.save_button.clicked.connect(self.save)
        layout.addWidget(self.save_button)
        self.price_button = QPushButton("Price approved components")
        self.price_button.setShortcut("Ctrl+P")
        self.price_button.setToolTip("Price approved components (Ctrl+P)")
        self.price_button.setProperty("variant", "primary")
        self.price_button.clicked.connect(self.save_price_export)
        layout.addWidget(self.price_button)
        close_button = QPushButton("Close")
        close_button.setProperty("variant", "secondary")
        close_button.clicked.connect(self.reject)
        layout.addWidget(close_button)
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
            approved = sum(item.review_status == "approved" for item in panel.components)
            excluded = sum(item.review_status == "excluded" for item in panel.components)
            pending = len(panel.components) - approved - excluded
            state = "approved" if pending == 0 and panel.components else "needs_review"
            if filter_text == "Needs review" and state == "approved":
                continue
            if filter_text == "Approved" and state != "approved":
                continue
            suffix = "APPROVED" if state == "approved" else f"{pending} TO REVIEW"
            item = QListWidgetItem(f"{panel.reference}\n{len(panel.components)} items · {suffix}")
            item.setData(Qt.UserRole, index)
            item.setToolTip(f"{approved} approved, {excluded} excluded, {pending} need review")
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
            f"{summary.approved} approved · {summary.needs_review} to review · {summary.excluded} excluded"
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
        self.components.setRowCount(len(panel.components))
        for row, component in enumerate(panel.components):
            for column, (_, attribute) in enumerate(COMPONENT_COLUMNS):
                item = QTableWidgetItem(str(getattr(component, attribute)))
                if attribute == "review_status":
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.components.setItem(row, column, item)
            self._style_component_row(row, component)
        widths = [92, 96, 62, 70, 58, 118, 92, 78, 82, 105, 52, 82, 72, 68, 112, 124, 100]
        for column, width in enumerate(widths):
            self.components.setColumnWidth(column, width)
        self._set_column_view()
        self._load_terminals(panel)
        self._load_matches(panel)
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
        panel.review_status = "needs_review"
        self._dirty = True
        self._save_quiet()
        self._refresh_panel_list()
        self.panel_heading.setText(f"EXTRACTED BOM — {reference}")

    def _style_component_row(self, row: int, component) -> None:
        base = STATUS_COLORS.get(component.review_status, STATUS_COLORS["needs_review"])
        if component.conflicts and component.review_status == "needs_review":
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
        self._load_terminals(panel)

    @staticmethod
    def _update_panel_status(panel) -> None:
        panel.review_status = (
            "approved"
            if panel.components and all(item.review_status in {"approved", "excluded"} for item in panel.components)
            else "needs_review"
        )

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
            if status == "approved" and component.conflicts and not component.conflicts_acknowledged:
                answer = QMessageBox.question(
                    self,
                    "Acknowledge extraction conflict",
                    f"{component.label or component.category} has an unresolved extraction conflict:\n\n"
                    + "\n".join(component.conflicts)
                    + "\n\nApprove this row and record that you reviewed the conflict?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    continue
                component.conflicts_acknowledged = True
            if status == "approved" and not manufacturer_is_configured(self.project, component):
                answer = QMessageBox.question(
                    self,
                    "Confirm manufacturer override",
                    f"{component.manufacturer or '(blank)'} has no configured price-list edition.\n\n"
                    "Approve this row and record the manufacturer as an explicit manual override?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    continue
                component.manufacturer_override_confirmed = True
            component.review_status = status
            if status != "approved":
                component.conflicts_acknowledged = False
            self._style_component_row(row, component)
            decision_item = self.components.item(row, len(COMPONENT_COLUMNS) - 1)
            if decision_item:
                decision_item.setText(status)
        self._update_panel_status(panel)
        self._dirty = True
        self._save_quiet()
        self._refresh_panel_list()
        self._show_selected_component()
        self._update_gate()

    def _show_selected_component(self) -> None:
        rows = self._selected_rows()
        enabled = bool(rows)
        for button in (self.approve_button, self.review_button, self.exclude_button):
            button.setEnabled(enabled)
        self.open_evidence_button.setEnabled(enabled)
        if not rows or not self.project.panels:
            self.component_title.setText("Select a BOM row")
            self.component_meta.setText("Evidence and conflicts will appear here.")
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
            f"Confidence  {component.confidence:.0%}\n"
            f"Decision  {component.review_status.replace('_', ' ')}"
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
        self.conflict_text.setText("\n".join(conflict_lines) if conflict_lines else "No unresolved extraction conflict.")
        self._load_render_preview(panel)

    def _load_render_preview(self, panel) -> None:
        path = next((Path(value) for value in panel.render_paths if Path(value).exists()), None)
        if path is None:
            self.evidence_preview.setPixmap(QPixmap())
            self.evidence_preview.setText("No render available")
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.evidence_preview.setText("Render could not be loaded")
            return
        self.evidence_preview.setPixmap(pixmap.scaled(265, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation))

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
                f"READY FOR PRICING · {summary.approved} approved · {summary.excluded} excluded"
            )
            self.gate_label.setProperty("state", "ready")
            self.price_button.setEnabled(True)
            self.panel_status.setText("REVIEW COMPLETE")
            self.panel_status.setProperty("state", "complete")
        else:
            self.gate_label.setText(
                f"PRICING LOCKED · {summary.needs_review} decisions remaining · {summary.conflicts} rows carry conflicts"
            )
            self.gate_label.setProperty("state", "blocked")
            self.price_button.setEnabled(False)
            self.panel_status.setText("REVIEW REQUIRED")
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
            QMessageBox.critical(self, "Pricing blocked", str(exc))
            return
        QMessageBox.information(
            self,
            "Offer updated",
            f"Pricing JSON and the Excel offer were regenerated.\n\n{offer_path}",
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
