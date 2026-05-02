"""
Computer Services catalog page.

CRUD for the services Brenda offers (typing, printing B/W, printing color,
photocopying, lamination, binding, scanning, designing, ...). Each service
has a name, category, unit type (per page / per item / ...), default price
and active flag. Sales reference these services so updating the default
price updates the suggested price in the Sale dialog.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.auth import current_user
from app.core.constants import (
    AUDIT_CREATE,
    AUDIT_DELETE,
    AUDIT_UPDATE,
    ROLE_ADMIN,
    SERVICE_UNITS,
    SERVICE_UNIT_ITEM,
)
from app.core.utils import format_money, parse_money
from app.services import service_catalog
from app.services.audit_service import log_action
from app.ui.components.cards import Panel, StatCard, StatusBadge
from app.ui.components.dialogs import confirm, error, toast
from app.ui.components.tables import configure_table, make_item, make_money_item


# ---------------------------------------------------------------------------
# Service editor dialog
# ---------------------------------------------------------------------------

class ServiceDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        service=None,
        existing_categories: Optional[List[str]] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Service" if service else "Add Service")
        self.setModal(True)
        self.resize(420, 0)
        self._service = service

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        title = QLabel("Edit service" if service else "Add a new service")
        title.setStyleSheet("font-size:16px; font-weight:700; color:#0F172A;")
        layout.addWidget(title)
        sub = QLabel("Services appear in the sale dialog with their default price.")
        sub.setStyleSheet("color:#64748B; font-size:12px;")
        layout.addWidget(sub)

        form = QFormLayout()
        form.setSpacing(8)

        self.name_input = QLineEdit(service.name if service else "")

        self.category_combo = QComboBox()
        self.category_combo.setEditable(True)
        for cat in existing_categories or []:
            self.category_combo.addItem(cat)
        if service and service.category is not None:
            self.category_combo.setCurrentText(service.category.name)

        self.unit_combo = QComboBox()
        for unit in SERVICE_UNITS:
            self.unit_combo.addItem(unit)
        self.unit_combo.setCurrentText(
            service.unit_type if service else SERVICE_UNIT_ITEM
        )

        self.price_input = QLineEdit()
        self.price_input.setPlaceholderText("e.g. 200")
        if service:
            self.price_input.setText(format_money(service.default_price, with_symbol=False))

        self.active_check = QCheckBox("Active (visible in sale dialog)")
        self.active_check.setChecked(service.is_active if service else True)

        form.addRow("Service name *", self.name_input)
        form.addRow("Category", self.category_combo)
        form.addRow("Unit", self.unit_combo)
        form.addRow("Default price (UGX)", self.price_input)
        form.addRow("", self.active_check)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        name = self.name_input.text().strip()
        if not name:
            error(self, "Missing name", "Please enter a service name.")
            return
        price = parse_money(self.price_input.text())
        if price < 0:
            error(self, "Invalid price", "Price cannot be negative.")
            return

        category = self.category_combo.currentText().strip()
        unit_type = self.unit_combo.currentText()
        is_active = self.active_check.isChecked()

        try:
            if self._service is None:
                new_id = service_catalog.create_service(
                    name=name,
                    category_name=category,
                    unit_type=unit_type,
                    default_price=float(price),
                )
                # Apply active flag if user unchecked
                if not is_active:
                    service_catalog.update_service(new_id, is_active=False)
                log_action(
                    AUDIT_CREATE,
                    module="services",
                    description=f"Added service '{name}' ({format_money(price)})",
                )
            else:
                service_catalog.update_service(
                    self._service.id,
                    name=name,
                    category_name=category,
                    unit_type=unit_type,
                    default_price=float(price),
                    is_active=is_active,
                )
                log_action(
                    AUDIT_UPDATE,
                    module="services",
                    description=f"Updated service '{name}' ({format_money(price)})",
                )
        except service_catalog.ServiceError as exc:
            error(self, "Could not save service", str(exc))
            return

        self.accept()


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

class ServicesPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: List = []
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(14)

        head = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        title = QLabel("Computer Services")
        title.setStyleSheet("font-size:22px; font-weight:700; color:#0F172A;")
        sub = QLabel("Manage the services and prices you offer (typing, printing, scanning, ...).")
        sub.setStyleSheet("color:#64748B;")
        col.addWidget(title)
        col.addWidget(sub)
        head.addLayout(col)
        head.addStretch(1)

        self._add_btn = QPushButton("  + Add Service")
        self._add_btn.setObjectName("PrimaryButton")
        self._add_btn.setCursor(Qt.PointingHandCursor)
        self._add_btn.clicked.connect(self._on_add)
        head.addWidget(self._add_btn)
        layout.addLayout(head)

        # Cards
        cards = QHBoxLayout()
        cards.setSpacing(12)
        self._card_total = StatCard("Active services", accent="#2563EB")
        self._card_inactive = StatCard("Inactive services", accent="#94A3B8")
        self._card_avg = StatCard("Average price", accent="#16A34A")
        cards.addWidget(self._card_total)
        cards.addWidget(self._card_inactive)
        cards.addWidget(self._card_avg)
        layout.addLayout(cards)

        # Filters
        filt = Panel(title="Filters")
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search by service name")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.textChanged.connect(self.refresh)
        self._inactive_check = QCheckBox("Show inactive")
        self._inactive_check.toggled.connect(self.refresh)
        bar.addWidget(self._search_input, stretch=1)
        bar.addWidget(self._inactive_check)
        filt.add_layout(bar)
        layout.addWidget(filt)

        # Table
        panel = Panel(title="Service catalog")
        self.table = QTableWidget()
        configure_table(
            self.table,
            ["Name", "Category", "Unit", "Default price", "Status"],
            stretch_last=False,
            resize_modes={
                1: QHeaderView.ResizeToContents,
                2: QHeaderView.ResizeToContents,
                3: QHeaderView.ResizeToContents,
                4: QHeaderView.ResizeToContents,
            },
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.table.cellDoubleClicked.connect(lambda *_: self._on_edit())
        panel.add_widget(self.table)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self._edit_btn = QPushButton("Edit")
        self._edit_btn.clicked.connect(self._on_edit)
        self._toggle_btn = QPushButton("Activate / Deactivate")
        self._toggle_btn.clicked.connect(self._on_toggle)
        actions.addWidget(self._edit_btn)
        actions.addWidget(self._toggle_btn)
        panel.add_layout(actions)

        layout.addWidget(panel, stretch=1)
        self._update_actions()

    # ------------------------------------------------------------------

    def refresh(self) -> None:
        try:
            rows = service_catalog.list_services(
                active_only=not self._inactive_check.isChecked(),
                search=self._search_input.text().strip(),
            )
        except Exception as exc:  # pragma: no cover
            error(self, "Could not load services", str(exc))
            return

        self._rows = rows
        self._populate_table(rows)
        self._update_cards()
        self._update_actions()

    def _populate_table(self, rows: List) -> None:
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            cat_name = row.category.name if row.category else "-"
            self.table.setItem(r, 0, make_item(row.name, data=row.id, bold=True))
            self.table.setItem(r, 1, make_item(cat_name))
            self.table.setItem(r, 2, make_item(row.unit_type))
            self.table.setItem(r, 3, make_money_item(row.default_price))
            badge = StatusBadge("Active" if row.is_active else "Inactive",
                                "success" if row.is_active else "muted")
            self.table.setCellWidget(r, 4, _wrap(badge))

    def _update_cards(self) -> None:
        try:
            all_rows = service_catalog.list_services(active_only=False)
        except Exception:
            all_rows = []
        active = [r for r in all_rows if r.is_active]
        inactive = [r for r in all_rows if not r.is_active]
        avg = (
            sum(float(r.default_price or 0) for r in active) / len(active)
            if active else 0
        )
        self._card_total.set_value(str(len(active)))
        self._card_inactive.set_value(str(len(inactive)))
        self._card_avg.set_money(avg)

    # ------------------------------------------------------------------

    def _selected(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        rid = item.data(Qt.UserRole)
        for r in self._rows:
            if r.id == rid:
                return r
        return None

    def _update_actions(self) -> None:
        sel = self._selected()
        self._edit_btn.setEnabled(sel is not None)
        self._toggle_btn.setEnabled(sel is not None)
        if sel is not None:
            self._toggle_btn.setText("Deactivate" if sel.is_active else "Activate")
        else:
            self._toggle_btn.setText("Activate / Deactivate")

    def _existing_categories(self) -> List[str]:
        try:
            cats = service_catalog.list_categories()
        except Exception:
            cats = []
        return [c.name for c in cats]

    def _on_add(self) -> None:
        dlg = ServiceDialog(self, existing_categories=self._existing_categories())
        if dlg.exec() == QDialog.Accepted:
            toast(self, "Service saved", "success")
            self.refresh()

    def _on_edit(self) -> None:
        sel = self._selected()
        if sel is None:
            return
        dlg = ServiceDialog(self, service=sel,
                            existing_categories=self._existing_categories())
        if dlg.exec() == QDialog.Accepted:
            toast(self, "Service updated", "success")
            self.refresh()

    def _on_toggle(self) -> None:
        sel = self._selected()
        if sel is None:
            return
        new_state = not sel.is_active
        action = "Activate" if new_state else "Deactivate"
        if not confirm(
            self,
            f"{action} service",
            f"{action} '{sel.name}'?",
            destructive=not new_state,
        ):
            return
        try:
            service_catalog.update_service(sel.id, is_active=new_state)
        except service_catalog.ServiceError as exc:
            error(self, "Could not update", str(exc))
            return
        log_action(
            AUDIT_UPDATE,
            module="services",
            description=f"{action}d service '{sel.name}'",
        )
        toast(self, f"Service {action.lower()}d", "info")
        self.refresh()


def _wrap(widget: QWidget) -> QWidget:
    w = QWidget()
    row = QHBoxLayout(w)
    row.setContentsMargins(6, 0, 6, 0)
    row.addWidget(widget)
    row.addStretch(1)
    return w
