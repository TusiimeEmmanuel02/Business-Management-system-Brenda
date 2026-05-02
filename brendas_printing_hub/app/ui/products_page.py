"""
Fridge / Drinks Stock page.

Two tabs:
1. Products - CRUD for drinks (Ice Water, Rwenzori Water, Soda, Juice, ...).
   Each row shows current stock with a low-stock warning badge.
2. Stock movements - history of all in/out movements with notes.

Adding stock writes a StockMovement of type "Purchase" and increments the
product's current_stock atomically. Sales subtract stock automatically via
the sales_service.
"""
from __future__ import annotations

from datetime import date as date_, datetime
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
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
    QSpinBox,
    QTableWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.auth import current_user
from app.core.constants import (
    AUDIT_CREATE,
    AUDIT_DELETE,
    AUDIT_STOCK_ADJUST,
    AUDIT_UPDATE,
    PRODUCT_CATEGORIES,
    PRODUCT_CATEGORY_OTHER,
    ROLE_ADMIN,
    STOCK_MOVEMENT_ADJUSTMENT,
)
from app.core.utils import format_datetime, format_money, parse_money
from app.services import stock_service
from app.services.audit_service import log_action
from app.ui.components.cards import Panel, StatCard, StatusBadge
from app.ui.components.dialogs import confirm, error, toast
from app.ui.components.tables import configure_table, make_item, make_money_item


# ---------------------------------------------------------------------------
# Product editor dialog
# ---------------------------------------------------------------------------

class ProductDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, product=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Product" if product else "Add Product")
        self.setModal(True)
        self.resize(440, 0)
        self._product = product

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        title = QLabel("Edit fridge product" if product else "Add fridge product")
        title.setStyleSheet("font-size:16px; font-weight:700; color:#0F172A;")
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(8)

        self.name_input = QLineEdit(product.name if product else "")
        self.brand_input = QLineEdit(product.brand if product and product.brand else "")
        self.brand_input.setPlaceholderText("Optional, e.g. Coca-Cola")

        self.category_combo = QComboBox()
        for cat in PRODUCT_CATEGORIES:
            self.category_combo.addItem(cat)
        if product:
            idx = self.category_combo.findText(product.category)
            if idx >= 0:
                self.category_combo.setCurrentIndex(idx)
        else:
            self.category_combo.setCurrentText(PRODUCT_CATEGORY_OTHER)

        self.buy_input = QLineEdit()
        self.buy_input.setPlaceholderText("e.g. 1,500")
        if product:
            self.buy_input.setText(format_money(product.buying_price, with_symbol=False))

        self.sell_input = QLineEdit()
        self.sell_input.setPlaceholderText("e.g. 2,500")
        if product:
            self.sell_input.setText(format_money(product.selling_price, with_symbol=False))

        self.threshold_input = QSpinBox()
        self.threshold_input.setRange(0, 10_000)
        self.threshold_input.setValue(int(product.low_stock_threshold) if product else 5)

        self.opening_input: Optional[QSpinBox] = None
        if product is None:
            self.opening_input = QSpinBox()
            self.opening_input.setRange(0, 100_000)
            self.opening_input.setValue(0)

        self.active_check = QCheckBox("Active (visible in sale dialog)")
        self.active_check.setChecked(product.is_active if product else True)

        form.addRow("Name *", self.name_input)
        form.addRow("Brand", self.brand_input)
        form.addRow("Category", self.category_combo)
        form.addRow("Buying price (UGX)", self.buy_input)
        form.addRow("Selling price (UGX)", self.sell_input)
        form.addRow("Low stock at", self.threshold_input)
        if self.opening_input is not None:
            form.addRow("Opening stock", self.opening_input)
        form.addRow("", self.active_check)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        name = self.name_input.text().strip()
        if not name:
            error(self, "Missing name", "Please enter the product name.")
            return
        buy = parse_money(self.buy_input.text())
        sell = parse_money(self.sell_input.text())
        if buy < 0 or sell < 0:
            error(self, "Invalid price", "Prices cannot be negative.")
            return
        brand = self.brand_input.text().strip()
        category = self.category_combo.currentText()
        threshold = self.threshold_input.value()
        active = self.active_check.isChecked()

        cu = current_user()
        try:
            if self._product is None:
                opening = self.opening_input.value() if self.opening_input else 0
                new_id = stock_service.create_product(
                    name=name,
                    brand=brand,
                    category=category,
                    buying_price=float(buy),
                    selling_price=float(sell),
                    opening_stock=opening,
                    low_stock_threshold=threshold,
                    user_id=cu.id if cu else None,
                )
                if not active:
                    stock_service.update_product(new_id, is_active=False)
                log_action(
                    AUDIT_CREATE,
                    module="products",
                    description=f"Added product '{name}' (sell {format_money(sell)})",
                )
            else:
                stock_service.update_product(
                    self._product.id,
                    name=name,
                    brand=brand or None,
                    category=category,
                    buying_price=float(buy),
                    selling_price=float(sell),
                    low_stock_threshold=threshold,
                    is_active=active,
                )
                log_action(
                    AUDIT_UPDATE,
                    module="products",
                    description=f"Updated product '{name}'",
                )
        except stock_service.StockError as exc:
            error(self, "Could not save product", str(exc))
            return

        self.accept()


# ---------------------------------------------------------------------------
# Stock movement dialog (purchase / adjustment)
# ---------------------------------------------------------------------------

class StockMovementDialog(QDialog):
    def __init__(self, parent: QWidget, products: List, mode: str = "purchase") -> None:
        super().__init__(parent)
        self._mode = mode
        self._products = products
        self.setWindowTitle("Add Stock" if mode == "purchase" else "Adjust Stock")
        self.setModal(True)
        self.resize(440, 0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        title = QLabel(
            "Record a stock purchase" if mode == "purchase" else "Manual stock adjustment"
        )
        title.setStyleSheet("font-size:16px; font-weight:700; color:#0F172A;")
        sub = QLabel(
            "Use this when new stock arrives." if mode == "purchase"
            else "Use for spoilage, damage, or stock count corrections (positive or negative)."
        )
        sub.setStyleSheet("color:#64748B; font-size:12px;")
        sub.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(sub)

        form = QFormLayout()
        form.setSpacing(8)

        self.product_combo = QComboBox()
        for p in products:
            label = f"{p.name}" + (f" ({p.brand})" if p.brand else "")
            self.product_combo.addItem(
                f"{label}  -  in stock: {p.current_stock}", p.id
            )

        self.qty_input = QSpinBox()
        if mode == "purchase":
            self.qty_input.setRange(1, 1_000_000)
            self.qty_input.setValue(1)
        else:
            self.qty_input.setRange(-1_000_000, 1_000_000)
            self.qty_input.setValue(-1)

        if mode == "purchase":
            self.cost_input = QLineEdit()
            self.cost_input.setPlaceholderText("e.g. 1,500 (optional - updates buying price)")
        else:
            self.cost_input = None

        self.notes_input = QTextEdit()
        self.notes_input.setMaximumHeight(80)
        self.notes_input.setPlaceholderText(
            "Notes" if mode == "purchase" else "Reason for adjustment (required)"
        )

        form.addRow("Product *", self.product_combo)
        form.addRow("Quantity" + (" added *" if mode == "purchase" else " (+/-) *"),
                    self.qty_input)
        if self.cost_input is not None:
            form.addRow("Unit cost (UGX)", self.cost_input)
        form.addRow("Notes", self.notes_input)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        product_id = self.product_combo.currentData()
        if product_id is None:
            error(self, "No product", "Add at least one product first.")
            return
        qty = self.qty_input.value()
        if qty == 0:
            error(self, "Invalid quantity", "Quantity must be non-zero.")
            return
        notes = self.notes_input.toPlainText().strip()
        cu = current_user()

        try:
            if self._mode == "purchase":
                if qty <= 0:
                    error(self, "Invalid quantity",
                          "Purchase quantity must be greater than 0.")
                    return
                cost = parse_money(self.cost_input.text()) if self.cost_input else 0
                stock_service.record_purchase(
                    product_id,
                    quantity=qty,
                    unit_cost=float(cost),
                    notes=notes,
                    user_id=cu.id if cu else None,
                )
                log_action(
                    AUDIT_CREATE,
                    module="products",
                    description=f"Stock in: +{qty} for product #{product_id}",
                )
            else:
                if not notes:
                    error(self, "Reason required",
                          "Please describe why you are adjusting the stock.")
                    return
                stock_service.adjust_stock(
                    product_id,
                    delta=int(qty),
                    movement_type=STOCK_MOVEMENT_ADJUSTMENT,
                    notes=notes,
                    user_id=cu.id if cu else None,
                )
                log_action(
                    AUDIT_STOCK_ADJUST,
                    module="products",
                    description=f"Adjustment {qty:+d} for product #{product_id}: {notes}",
                )
        except stock_service.StockError as exc:
            error(self, "Could not save", str(exc))
            return
        self.accept()


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

class ProductsPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._products: List = []
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
        title = QLabel("Fridge & Drinks Stock")
        title.setStyleSheet("font-size:22px; font-weight:700; color:#0F172A;")
        sub = QLabel("Manage soda, water and other drinks. Stock is auto-reduced on each fridge sale.")
        sub.setStyleSheet("color:#64748B;")
        col.addWidget(title)
        col.addWidget(sub)
        head.addLayout(col)
        head.addStretch(1)

        self._add_btn = QPushButton("+ Add Product")
        self._add_btn.clicked.connect(self._on_add_product)
        self._stockin_btn = QPushButton("+ Stock In")
        self._stockin_btn.setObjectName("PrimaryButton")
        self._stockin_btn.clicked.connect(lambda: self._on_movement("purchase"))
        self._adjust_btn = QPushButton("Adjust Stock")
        self._adjust_btn.clicked.connect(lambda: self._on_movement("adjustment"))
        head.addWidget(self._add_btn)
        head.addWidget(self._stockin_btn)
        head.addWidget(self._adjust_btn)
        layout.addLayout(head)

        # KPI cards
        cards = QHBoxLayout()
        cards.setSpacing(12)
        self._card_total = StatCard("Active products", accent="#2563EB")
        self._card_low = StatCard("Low stock", accent="#DC2626")
        self._card_value = StatCard("Stock value (cost)", accent="#16A34A")
        self._card_revenue = StatCard("Stock value (selling)", accent="#0EA5E9")
        for c in (self._card_total, self._card_low, self._card_value, self._card_revenue):
            cards.addWidget(c)
        layout.addLayout(cards)

        # Tabs
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, stretch=1)

        # Products tab
        prod_tab = QWidget()
        prod_layout = QVBoxLayout(prod_tab)
        prod_layout.setContentsMargins(0, 12, 0, 0)
        prod_layout.setSpacing(10)

        filt_bar = QHBoxLayout()
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search by product or brand")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.textChanged.connect(self.refresh)
        self._inactive_check = QCheckBox("Show inactive")
        self._inactive_check.toggled.connect(self.refresh)
        filt_bar.addWidget(self._search_input, stretch=1)
        filt_bar.addWidget(self._inactive_check)
        prod_layout.addLayout(filt_bar)

        self.products_table = QTableWidget()
        configure_table(
            self.products_table,
            ["Name", "Brand", "Category", "Stock", "Low at",
             "Buying", "Selling", "Profit/unit", "Status"],
            stretch_last=False,
            resize_modes={i: QHeaderView.ResizeToContents for i in range(9)},
        )
        self.products_table.itemSelectionChanged.connect(self._update_actions)
        self.products_table.cellDoubleClicked.connect(lambda *_: self._on_edit_product())
        prod_layout.addWidget(self.products_table, stretch=1)

        # Per-row actions
        actions = QHBoxLayout()
        actions.addStretch(1)
        self._edit_btn = QPushButton("Edit")
        self._edit_btn.clicked.connect(self._on_edit_product)
        self._toggle_btn = QPushButton("Activate / Deactivate")
        self._toggle_btn.clicked.connect(self._on_toggle_product)
        actions.addWidget(self._edit_btn)
        actions.addWidget(self._toggle_btn)
        prod_layout.addLayout(actions)

        self.tabs.addTab(prod_tab, "Products")

        # Movement history tab
        mov_tab = QWidget()
        mov_layout = QVBoxLayout(mov_tab)
        mov_layout.setContentsMargins(0, 12, 0, 0)
        self.movements_table = QTableWidget()
        configure_table(
            self.movements_table,
            ["Date", "Product", "Type", "Quantity", "Unit cost", "Notes"],
            stretch_last=True,
            resize_modes={
                0: QHeaderView.ResizeToContents,
                2: QHeaderView.ResizeToContents,
                3: QHeaderView.ResizeToContents,
                4: QHeaderView.ResizeToContents,
            },
        )
        mov_layout.addWidget(self.movements_table, stretch=1)
        self.tabs.addTab(mov_tab, "Stock movements")

        self._update_actions()

    # ------------------------------------------------------------------

    def refresh(self) -> None:
        try:
            products = stock_service.list_products(
                active_only=not self._inactive_check.isChecked(),
                search=self._search_input.text().strip(),
            )
            movements = stock_service.list_movements(limit=300)
        except Exception as exc:  # pragma: no cover
            error(self, "Could not load products", str(exc))
            return
        self._products = products
        self._populate_products(products)
        self._populate_movements(movements)
        self._update_cards()
        self._update_actions()

    def _populate_products(self, rows: List) -> None:
        self.products_table.setRowCount(len(rows))
        for r, p in enumerate(rows):
            low = int(p.current_stock or 0) <= int(p.low_stock_threshold or 0)
            profit = float(p.selling_price or 0) - float(p.buying_price or 0)
            self.products_table.setItem(r, 0, make_item(p.name, data=p.id, bold=True))
            self.products_table.setItem(r, 1, make_item(p.brand or "-"))
            self.products_table.setItem(r, 2, make_item(p.category or "-"))

            stock_color = "#DC2626" if (low and p.is_active) else None
            self.products_table.setItem(
                r, 3, make_item(
                    str(p.current_stock),
                    align=Qt.AlignRight | Qt.AlignVCenter,
                    bold=low,
                    color=stock_color,
                )
            )
            self.products_table.setItem(
                r, 4, make_item(str(p.low_stock_threshold),
                                align=Qt.AlignRight | Qt.AlignVCenter)
            )
            self.products_table.setItem(r, 5, make_money_item(p.buying_price))
            self.products_table.setItem(r, 6, make_money_item(p.selling_price))
            self.products_table.setItem(r, 7, make_money_item(profit, bold=True))

            if not p.is_active:
                badge = StatusBadge("Inactive", "muted")
            elif low:
                badge = StatusBadge("Low stock", "danger")
            else:
                badge = StatusBadge("OK", "success")
            self.products_table.setCellWidget(r, 8, _wrap(badge))

    def _populate_movements(self, movements: List) -> None:
        # Build a simple lookup product_id -> name for nice labels
        product_lookup = {p.id: p for p in self._products}
        self.movements_table.setRowCount(len(movements))
        for r, m in enumerate(movements):
            product = product_lookup.get(m.product_id)
            if product is None:
                # Movement may belong to a hidden product - load on demand
                product = stock_service.get_product(m.product_id)
                if product is not None:
                    product_lookup[m.product_id] = product
            label = (product.name if product else f"Product #{m.product_id}")
            qty = int(m.quantity or 0)
            qty_text = f"{qty:+d}"
            color = "#16A34A" if qty > 0 else "#DC2626" if qty < 0 else None

            self.movements_table.setItem(r, 0, make_item(format_datetime(m.created_at)))
            self.movements_table.setItem(r, 1, make_item(label))
            self.movements_table.setItem(r, 2, make_item(m.movement_type))
            self.movements_table.setItem(
                r, 3, make_item(qty_text,
                                align=Qt.AlignRight | Qt.AlignVCenter,
                                bold=True, color=color)
            )
            self.movements_table.setItem(r, 4, make_money_item(m.unit_cost or 0))
            self.movements_table.setItem(r, 5, make_item(m.notes or ""))

    def _update_cards(self) -> None:
        try:
            all_products = stock_service.list_products(active_only=False)
        except Exception:
            all_products = []
        active = [p for p in all_products if p.is_active]
        low = [p for p in active if p.current_stock <= p.low_stock_threshold]
        cost_value = sum(float(p.buying_price or 0) * int(p.current_stock or 0) for p in active)
        sell_value = sum(float(p.selling_price or 0) * int(p.current_stock or 0) for p in active)

        self._card_total.set_value(str(len(active)))
        self._card_low.set_value(str(len(low)))
        self._card_low.set_sublabel(
            "Restock needed" if low else "All stocked", negative=bool(low)
        )
        self._card_value.set_money(cost_value)
        self._card_revenue.set_money(sell_value)

    # ------------------------------------------------------------------

    def _selected_product(self):
        row = self.products_table.currentRow()
        if row < 0:
            return None
        item = self.products_table.item(row, 0)
        if item is None:
            return None
        rid = item.data(Qt.UserRole)
        for p in self._products:
            if p.id == rid:
                return p
        return None

    def _update_actions(self) -> None:
        sel = self._selected_product()
        self._edit_btn.setEnabled(sel is not None)
        self._toggle_btn.setEnabled(sel is not None)
        if sel is not None:
            self._toggle_btn.setText("Deactivate" if sel.is_active else "Activate")
        else:
            self._toggle_btn.setText("Activate / Deactivate")

    def _on_add_product(self) -> None:
        dlg = ProductDialog(self)
        if dlg.exec() == QDialog.Accepted:
            toast(self, "Product saved", "success")
            self.refresh()

    def _on_edit_product(self) -> None:
        sel = self._selected_product()
        if sel is None:
            return
        dlg = ProductDialog(self, product=sel)
        if dlg.exec() == QDialog.Accepted:
            toast(self, "Product updated", "success")
            self.refresh()

    def _on_toggle_product(self) -> None:
        sel = self._selected_product()
        if sel is None:
            return
        new_state = not sel.is_active
        action = "Activate" if new_state else "Deactivate"
        if not confirm(
            self,
            f"{action} product",
            f"{action} '{sel.name}'?",
            destructive=not new_state,
        ):
            return
        try:
            stock_service.update_product(sel.id, is_active=new_state)
        except stock_service.StockError as exc:
            error(self, "Could not update", str(exc))
            return
        log_action(
            AUDIT_UPDATE,
            module="products",
            description=f"{action}d product '{sel.name}'",
        )
        toast(self, f"Product {action.lower()}d", "info")
        self.refresh()

    def _on_movement(self, mode: str) -> None:
        # Always reload products so stock counts in dropdown are fresh
        try:
            products = stock_service.list_products(active_only=True)
        except Exception as exc:  # pragma: no cover
            error(self, "Could not load products", str(exc))
            return
        if not products:
            error(self, "No products", "Add a product first.")
            return
        dlg = StockMovementDialog(self, products=products, mode=mode)
        if dlg.exec() == QDialog.Accepted:
            toast(self, "Stock recorded", "success")
            self.refresh()


def _wrap(widget: QWidget) -> QWidget:
    w = QWidget()
    row = QHBoxLayout(w)
    row.setContentsMargins(6, 0, 6, 0)
    row.addWidget(widget)
    row.addStretch(1)
    return w
