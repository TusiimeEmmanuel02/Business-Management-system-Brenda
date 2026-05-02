"""
Labelling Jobs page.

Tracks clothing-labelling orders (jersey, shirt, school uniform, ...) with
installment payments. Each payment is mirrored to a Sale row in the
Labelling category so it flows into dashboards and cash reports.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import List, Optional

from PySide6.QtCore import Qt, QDate, QDateTime
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.auth import current_user
from app.core.constants import (
    AUDIT_CREATE,
    AUDIT_DELETE,
    AUDIT_UPDATE,
    JOB_STATUSES,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COLLECTED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_IN_PROGRESS,
    JOB_STATUS_PENDING,
    LABELLING_ITEM_TYPES,
    PAYMENT_CASH,
    PAYMENT_METHODS,
    PAY_STATUSES,
    ROLE_ADMIN,
)
from app.core.utils import (
    format_date,
    format_datetime,
    format_money,
    parse_money,
)
from app.services import labelling_service
from app.services.audit_service import log_action
from app.ui.components.cards import Panel, StatCard, StatusBadge
from app.ui.components.dialogs import confirm, error, info, toast
from app.ui.components.tables import configure_table, make_item, make_money_item


PAYMENT_STATE_MAP = {
    "Unpaid": "danger",
    "Partially Paid": "warning",
    "Fully Paid": "success",
}

JOB_STATE_MAP = {
    JOB_STATUS_PENDING: "warning",
    JOB_STATUS_IN_PROGRESS: "info",
    JOB_STATUS_COMPLETED: "success",
    JOB_STATUS_COLLECTED: "muted",
    JOB_STATUS_CANCELLED: "muted",
}


# ---------------------------------------------------------------------------
# New / Edit job dialog
# ---------------------------------------------------------------------------

class JobDialog(QDialog):
    def __init__(self, parent: QWidget | None = None,
                 job: labelling_service.JobSummary | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Labelling Job" if job else "New Labelling Job")
        self.setModal(True)
        self.resize(480, 0)
        self._job = job

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        title = QLabel("Edit labelling job" if job else "Create new labelling job")
        title.setStyleSheet("font-size:16px; font-weight:700; color:#0F172A;")
        layout.addWidget(title)
        sub = QLabel("Capture customer details, item, quantity, price, and (optionally) a deposit.")
        sub.setStyleSheet("color:#64748B; font-size:12px;")
        layout.addWidget(sub)

        form = QFormLayout()
        form.setSpacing(8)

        self.customer_input = QLineEdit(job.customer_name if job else "")
        self.phone_input = QLineEdit(job.customer_phone if job else "")
        self.phone_input.setPlaceholderText("Optional")

        self.item_combo = QComboBox()
        self.item_combo.addItems(LABELLING_ITEM_TYPES)
        if job:
            idx = self.item_combo.findText(job.item_type)
            if idx >= 0:
                self.item_combo.setCurrentIndex(idx)

        self.description_input = QLineEdit(job.description if job else "")
        self.description_input.setPlaceholderText("e.g. School name, colours, sizes")

        self.qty_input = QSpinBox()
        self.qty_input.setRange(1, 100_000)
        self.qty_input.setValue(int(job.quantity) if job else 1)

        self.price_input = QLineEdit()
        self.price_input.setPlaceholderText("e.g. 5,000")
        if job:
            self.price_input.setText(format_money(job.unit_price, with_symbol=False))

        self.due_input = QDateEdit()
        self.due_input.setCalendarPopup(True)
        self.due_input.setDisplayFormat("dd MMM yyyy")
        if job and job.due_date:
            self.due_input.setDate(QDate(job.due_date.year, job.due_date.month, job.due_date.day))
        else:
            self.due_input.setDate(QDate.currentDate().addDays(3))

        # Deposit (only on create)
        self.deposit_input: Optional[QLineEdit] = None
        self.deposit_method_combo: Optional[QComboBox] = None
        if job is None:
            self.deposit_input = QLineEdit()
            self.deposit_input.setPlaceholderText("Optional, e.g. 10,000")
            self.deposit_method_combo = QComboBox()
            self.deposit_method_combo.addItems(PAYMENT_METHODS)
            self.deposit_method_combo.setCurrentText(PAYMENT_CASH)

        self.notes_input = QTextEdit()
        self.notes_input.setMaximumHeight(70)
        if job:
            self.notes_input.setPlainText(job.notes)

        form.addRow("Customer name *", self.customer_input)
        form.addRow("Customer phone", self.phone_input)
        form.addRow("Item type", self.item_combo)
        form.addRow("Description", self.description_input)
        form.addRow("Quantity", self.qty_input)
        form.addRow("Unit price (UGX)", self.price_input)
        form.addRow("Due date", self.due_input)
        if self.deposit_input is not None:
            form.addRow("Deposit (UGX)", self.deposit_input)
            form.addRow("Deposit method", self.deposit_method_combo)
        form.addRow("Notes", self.notes_input)
        layout.addLayout(form)

        # Live total preview
        self.total_label = QLabel("Total: UGX 0")
        self.total_label.setStyleSheet("font-size:14px; font-weight:700; color:#0F172A;")
        layout.addWidget(self.total_label)

        self.qty_input.valueChanged.connect(self._update_total)
        self.price_input.textChanged.connect(self._update_total)
        self._update_total()

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _update_total(self, *_) -> None:
        qty = self.qty_input.value()
        price = parse_money(self.price_input.text())
        total = qty * price
        self.total_label.setText(f"Total: {format_money(total)}")

    def _accept(self) -> None:
        name = self.customer_input.text().strip()
        if not name:
            error(self, "Missing customer", "Please enter the customer name.")
            return
        unit_price = parse_money(self.price_input.text())
        if unit_price <= 0:
            error(self, "Invalid price", "Unit price must be greater than 0.")
            return
        qty = self.qty_input.value()
        item_type = self.item_combo.currentText()
        description = self.description_input.text().strip()
        phone = self.phone_input.text().strip()
        notes = self.notes_input.toPlainText().strip()
        due_qd = self.due_input.date()
        due_date = date(due_qd.year(), due_qd.month(), due_qd.day())

        cu = current_user()
        try:
            if self._job is None:
                deposit = parse_money(self.deposit_input.text()) if self.deposit_input else 0
                deposit_method = (
                    self.deposit_method_combo.currentText()
                    if self.deposit_method_combo else PAYMENT_CASH
                )
                new_id = labelling_service.create_job(
                    customer_name=name,
                    customer_phone=phone,
                    item_type=item_type,
                    description=description,
                    quantity=qty,
                    unit_price=float(unit_price),
                    due_date=due_date,
                    deposit=float(deposit),
                    deposit_method=deposit_method,
                    notes=notes,
                    created_by=cu.id if cu else None,
                )
                log_action(
                    AUDIT_CREATE,
                    module="labelling",
                    description=(
                        f"Created labelling job #{new_id} for {name} - "
                        f"{format_money(qty * unit_price)}"
                    ),
                )
            else:
                labelling_service.update_job(
                    self._job.id,
                    customer_name=name,
                    customer_phone=phone,
                    item_type=item_type,
                    description=description,
                    quantity=qty,
                    unit_price=float(unit_price),
                    due_date=due_date,
                    notes=notes,
                )
                log_action(
                    AUDIT_UPDATE,
                    module="labelling",
                    description=f"Updated labelling job #{self._job.id} ({name})",
                )
        except labelling_service.LabellingError as exc:
            error(self, "Could not save job", str(exc))
            return
        self.accept()


# ---------------------------------------------------------------------------
# Payment dialog
# ---------------------------------------------------------------------------

class PaymentDialog(QDialog):
    def __init__(self, parent: QWidget, job: labelling_service.JobSummary) -> None:
        super().__init__(parent)
        self.setWindowTitle("Record Payment")
        self.setModal(True)
        self.resize(420, 0)
        self._job = job

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        title = QLabel("Record payment")
        title.setStyleSheet("font-size:16px; font-weight:700; color:#0F172A;")
        layout.addWidget(title)

        recap = QFrame()
        recap.setObjectName("Panel")
        rec_layout = QVBoxLayout(recap)
        rec_layout.setContentsMargins(14, 12, 14, 12)
        rec_layout.setSpacing(4)
        for label, value in [
            ("Customer", job.customer_name),
            ("Item", f"{job.quantity}\u00d7 {job.item_type}"),
            ("Total", format_money(job.total_amount)),
            ("Already paid", format_money(job.amount_paid)),
            ("Balance", format_money(job.balance)),
        ]:
            row = QHBoxLayout()
            l1 = QLabel(label)
            l1.setStyleSheet("color:#475569;")
            l2 = QLabel(value)
            l2.setStyleSheet("color:#0F172A; font-weight:600;")
            l2.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(l1)
            row.addStretch(1)
            row.addWidget(l2)
            rec_layout.addLayout(row)
        layout.addWidget(recap)

        form = QFormLayout()
        form.setSpacing(8)

        self.amount_input = QLineEdit()
        self.amount_input.setPlaceholderText(f"max {format_money(job.balance, with_symbol=False)}")
        self.amount_input.setText(format_money(job.balance, with_symbol=False))
        self.method_combo = QComboBox()
        self.method_combo.addItems(PAYMENT_METHODS)
        self.method_combo.setCurrentText(PAYMENT_CASH)
        self.notes_input = QLineEdit()
        self.notes_input.setPlaceholderText("Notes (optional)")

        form.addRow("Amount (UGX)", self.amount_input)
        form.addRow("Method", self.method_combo)
        form.addRow("Notes", self.notes_input)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        amount = parse_money(self.amount_input.text())
        if amount <= 0:
            error(self, "Invalid amount", "Please enter an amount greater than 0.")
            return
        if amount > self._job.balance + 0.01:
            error(self, "Too much", f"Amount cannot exceed the balance of {format_money(self._job.balance)}.")
            return
        cu = current_user()
        try:
            labelling_service.add_payment(
                self._job.id,
                amount=float(amount),
                payment_method=self.method_combo.currentText(),
                notes=self.notes_input.text().strip(),
                user_id=cu.id if cu else None,
            )
            log_action(
                AUDIT_CREATE,
                module="labelling",
                description=(
                    f"Recorded payment {format_money(amount)} for job #{self._job.id} "
                    f"({self._job.customer_name})"
                ),
            )
        except labelling_service.LabellingError as exc:
            error(self, "Could not record payment", str(exc))
            return
        self.accept()


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

STATUS_FILTERS = ("All",) + JOB_STATUSES + ("Has balance",)


class LabellingPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: List[labelling_service.JobSummary] = []
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(14)

        # Header
        head = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        title = QLabel("Labelling Jobs")
        title.setStyleSheet("font-size:22px; font-weight:700; color:#0F172A;")
        sub = QLabel("Track jersey, shirt, school uniform and clothing labelling orders.")
        sub.setStyleSheet("color:#64748B;")
        col.addWidget(title)
        col.addWidget(sub)
        head.addLayout(col)
        head.addStretch(1)

        self._add_btn = QPushButton("  + New Job")
        self._add_btn.setObjectName("PrimaryButton")
        self._add_btn.setCursor(Qt.PointingHandCursor)
        self._add_btn.clicked.connect(self._on_add)
        head.addWidget(self._add_btn)
        layout.addLayout(head)

        # KPI cards
        cards = QHBoxLayout()
        cards.setSpacing(12)
        self._card_pending = StatCard("Pending", accent="#D97706")
        self._card_progress = StatCard("In progress", accent="#0EA5E9")
        self._card_outstanding = StatCard("Outstanding balance", accent="#DC2626")
        self._card_today_pay = StatCard("Today's payments", accent="#16A34A")
        for c in (self._card_pending, self._card_progress, self._card_outstanding, self._card_today_pay):
            cards.addWidget(c)
        layout.addLayout(cards)

        # Filters
        filt = Panel(title="Filters")
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._status_combo = QComboBox()
        self._status_combo.addItems(STATUS_FILTERS)
        self._status_combo.currentTextChanged.connect(self.refresh)
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search customer name / phone / description")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.textChanged.connect(self.refresh)
        bar.addWidget(QLabel("Status:"))
        bar.addWidget(self._status_combo)
        bar.addWidget(self._search_input, stretch=1)
        filt.add_layout(bar)
        layout.addWidget(filt)

        # Table
        panel = Panel(title="Jobs")
        self.table = QTableWidget()
        configure_table(
            self.table,
            ["Date", "Customer", "Item", "Qty", "Total", "Paid", "Balance",
             "Pay status", "Job status", "Due"],
            stretch_last=False,
            resize_modes={i: QHeaderView.ResizeToContents for i in range(10)},
        )
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.table.cellDoubleClicked.connect(lambda *_: self._on_view())
        panel.add_widget(self.table)

        # Action bar
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self._view_btn = QPushButton("View / payments")
        self._view_btn.clicked.connect(self._on_view)
        self._pay_btn = QPushButton("Add payment")
        self._pay_btn.setObjectName("PrimaryButton")
        self._pay_btn.clicked.connect(self._on_payment)
        self._edit_btn = QPushButton("Edit")
        self._edit_btn.clicked.connect(self._on_edit)

        self._status_btn = QComboBox()
        self._status_btn.setEnabled(False)
        self._status_btn.addItems(("Set status...",) + JOB_STATUSES)
        self._status_btn.currentTextChanged.connect(self._on_status_change)

        self._delete_btn = QPushButton("Delete")
        self._delete_btn.setObjectName("DangerButton")
        self._delete_btn.clicked.connect(self._on_delete)

        actions.addStretch(1)
        for b in (self._view_btn, self._pay_btn, self._edit_btn, self._status_btn, self._delete_btn):
            actions.addWidget(b)
        panel.add_layout(actions)

        layout.addWidget(panel, stretch=1)
        self._update_actions()

    # ------------------------------------------------------------------

    def refresh(self) -> None:
        sel = self._status_combo.currentText()
        kwargs: dict = dict(search=self._search_input.text().strip(), limit=1000)
        if sel == "Has balance":
            kwargs["only_with_balance"] = True
        elif sel != "All":
            kwargs["status"] = sel
        try:
            rows = labelling_service.list_jobs(**kwargs)
        except Exception as exc:  # pragma: no cover
            error(self, "Could not load jobs", str(exc))
            return
        self._rows = rows
        self._populate_table(rows)
        self._update_cards()
        self._update_actions()

    def _populate_table(self, rows: List[labelling_service.JobSummary]) -> None:
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            self.table.setItem(r, 0, make_item(format_date(row.created_at), data=row.id))
            self.table.setItem(r, 1, make_item(
                f"{row.customer_name}" + (f"  ({row.customer_phone})" if row.customer_phone else "")
            ))
            self.table.setItem(r, 2, make_item(row.item_type + (f" - {row.description}" if row.description else "")))
            self.table.setItem(r, 3, make_item(str(row.quantity), align=Qt.AlignRight | Qt.AlignVCenter))
            self.table.setItem(r, 4, make_money_item(row.total_amount, bold=True))
            self.table.setItem(r, 5, make_money_item(row.amount_paid))
            self.table.setItem(r, 6, make_money_item(
                row.balance, bold=row.balance > 0,
            ))

            pay_badge = StatusBadge(row.payment_status, PAYMENT_STATE_MAP.get(row.payment_status, "muted"))
            job_badge = StatusBadge(row.job_status, JOB_STATE_MAP.get(row.job_status, "muted"))
            self.table.setCellWidget(r, 7, _wrap(pay_badge))
            self.table.setCellWidget(r, 8, _wrap(job_badge))

            self.table.setItem(r, 9, make_item(format_date(row.due_date) if row.due_date else "-"))

    def _update_cards(self) -> None:
        try:
            counts = labelling_service.status_counts()
            self._card_pending.set_value(str(counts.get(JOB_STATUS_PENDING, 0)))
            self._card_progress.set_value(str(counts.get(JOB_STATUS_IN_PROGRESS, 0)))
            self._card_outstanding.set_money(labelling_service.outstanding_balance())
            self._card_today_pay.set_money(labelling_service.today_revenue())
        except Exception as exc:  # pragma: no cover
            print(f"[v0] labelling cards refresh failed: {exc}")

    # ------------------------------------------------------------------

    def _selected(self) -> Optional[labelling_service.JobSummary]:
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
        has = sel is not None
        self._view_btn.setEnabled(has)
        self._edit_btn.setEnabled(has and sel.job_status != JOB_STATUS_CANCELLED)
        self._pay_btn.setEnabled(
            has and sel.balance > 0 and sel.job_status != JOB_STATUS_CANCELLED
        )
        self._status_btn.setEnabled(has)
        cu = current_user()
        self._delete_btn.setEnabled(has and cu is not None and cu.role == ROLE_ADMIN)
        # Reset combo without triggering refresh
        self._status_btn.blockSignals(True)
        self._status_btn.setCurrentIndex(0)
        self._status_btn.blockSignals(False)

    def _on_add(self) -> None:
        dlg = JobDialog(self)
        if dlg.exec() == QDialog.Accepted:
            toast(self, "Labelling job saved", "success")
            self.refresh()

    def _on_edit(self) -> None:
        sel = self._selected()
        if sel is None:
            return
        dlg = JobDialog(self, job=sel)
        if dlg.exec() == QDialog.Accepted:
            toast(self, "Job updated", "success")
            self.refresh()

    def _on_payment(self) -> None:
        sel = self._selected()
        if sel is None:
            return
        if sel.balance <= 0:
            info(self, "Fully paid", "This job has been fully paid already.")
            return
        dlg = PaymentDialog(self, sel)
        if dlg.exec() == QDialog.Accepted:
            toast(self, "Payment recorded", "success")
            self.refresh()

    def _on_status_change(self, text: str) -> None:
        if text in (None, "", "Set status..."):
            return
        sel = self._selected()
        if sel is None:
            return
        try:
            labelling_service.set_job_status(sel.id, text)
        except labelling_service.LabellingError as exc:
            error(self, "Could not update status", str(exc))
            return
        log_action(
            AUDIT_UPDATE,
            module="labelling",
            description=f"Set job #{sel.id} status to '{text}' ({sel.customer_name})",
        )
        toast(self, f"Status: {text}", "info")
        self.refresh()

    def _on_view(self) -> None:
        sel = self._selected()
        if sel is None:
            return
        try:
            payments = labelling_service.list_payments(sel.id)
        except Exception as exc:  # pragma: no cover
            error(self, "Could not load job", str(exc))
            return

        lines = [
            f"Customer: {sel.customer_name}",
            f"Phone: {sel.customer_phone or '-'}",
            f"Item: {sel.quantity}\u00d7 {sel.item_type}"
            + (f" - {sel.description}" if sel.description else ""),
            f"Created: {format_datetime(sel.created_at)}",
            f"Due: {format_date(sel.due_date) if sel.due_date else '-'}",
            f"Status: {sel.job_status}  /  {sel.payment_status}",
            "",
            f"Total: {format_money(sel.total_amount)}",
            f"Paid:  {format_money(sel.amount_paid)}",
            f"Bal:   {format_money(sel.balance)}",
            "",
            "Payments:",
        ]
        if not payments:
            lines.append("  (no payments yet)")
        else:
            for p in payments:
                lines.append(
                    f"  - {format_datetime(p.paid_on)}  {format_money(p.amount)}  ({p.payment_method})"
                    + (f"  - {p.notes}" if p.notes else "")
                )
        if sel.notes:
            lines.append("")
            lines.append(f"Notes: {sel.notes}")

        info(self, f"Job #{sel.id}", "\n".join(lines))

    def _on_delete(self) -> None:
        sel = self._selected()
        if sel is None:
            return
        if not confirm(
            self,
            "Delete job",
            (
                f"Delete labelling job for '{sel.customer_name}'? "
                "All recorded payments will also be removed from sales reports."
            ),
            destructive=True,
        ):
            return
        try:
            labelling_service.delete_job(sel.id)
        except labelling_service.LabellingError as exc:
            error(self, "Could not delete", str(exc))
            return
        log_action(
            AUDIT_DELETE,
            module="labelling",
            description=f"Deleted labelling job #{sel.id} ({sel.customer_name})",
        )
        toast(self, "Job deleted", "info")
        self.refresh()


def _wrap(widget: QWidget) -> QWidget:
    w = QWidget()
    row = QHBoxLayout(w)
    row.setContentsMargins(6, 0, 6, 0)
    row.addWidget(widget)
    row.addStretch(1)
    return w
