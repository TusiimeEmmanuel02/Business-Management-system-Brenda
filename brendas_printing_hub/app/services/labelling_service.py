"""
Labelling jobs business logic.

A LabellingJob represents a clothing-labelling order (jersey, shirt, school
uniform, etc.). Customers can pay in installments, so each job has a list of
LabellingPayments. The job's payment_status (Unpaid / Partially Paid / Fully
Paid) and job_status (Pending / In Progress / Completed / Collected /
Cancelled) are tracked separately.

Recording a payment also records a Sale (category=Labelling) so labelling
income shows up in the dashboard, sales reports and cash tally exactly like
other revenue.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_, datetime
from typing import List, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.orm import joinedload

from app.core.constants import (
    JOB_STATUSES,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COLLECTED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_IN_PROGRESS,
    JOB_STATUS_PENDING,
    LABELLING_ITEM_TYPES,
    PAYMENT_METHODS,
    PAY_STATUSES,
    PAY_STATUS_PAID,
    PAY_STATUS_PARTIAL,
    PAY_STATUS_UNPAID,
    SALE_CATEGORY_LABELLING,
)
from app.core.utils import day_bounds, month_bounds
from app.database.models import (
    LabellingJob,
    LabellingPayment,
    Sale,
    SaleItem,
)
from app.database.session import session_scope


class LabellingError(Exception):
    """Raised for invalid labelling-job input."""


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------

@dataclass
class JobSummary:
    id: int
    customer_name: str
    customer_phone: str
    item_type: str
    description: str
    quantity: int
    unit_price: float
    total_amount: float
    amount_paid: float
    balance: float
    payment_status: str
    job_status: str
    due_date: Optional[date_]
    created_at: datetime
    notes: str

    @property
    def is_active(self) -> bool:
        return self.job_status not in (JOB_STATUS_COLLECTED, JOB_STATUS_CANCELLED)


@dataclass
class PaymentSummary:
    id: int
    job_id: int
    paid_on: datetime
    amount: float
    payment_method: str
    notes: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _derive_payment_status(total: float, paid: float) -> str:
    if paid <= 0:
        return PAY_STATUS_UNPAID
    if paid + 0.0001 < total:
        return PAY_STATUS_PARTIAL
    return PAY_STATUS_PAID


def _to_summary(job: LabellingJob) -> JobSummary:
    total = float(job.total_amount or 0)
    paid = float(job.amount_paid or 0)
    return JobSummary(
        id=job.id,
        customer_name=job.customer_name,
        customer_phone=job.customer_phone or "",
        item_type=job.item_type,
        description=job.description or "",
        quantity=int(job.quantity or 0),
        unit_price=float(job.unit_price or 0),
        total_amount=total,
        amount_paid=paid,
        balance=max(0.0, total - paid),
        payment_status=job.payment_status,
        job_status=job.job_status,
        due_date=job.due_date,
        created_at=job.created_at,
        notes=job.notes or "",
    )


# ---------------------------------------------------------------------------
# Job mutations
# ---------------------------------------------------------------------------

def create_job(
    *,
    customer_name: str,
    item_type: str,
    quantity: int,
    unit_price: float,
    description: str = "",
    customer_phone: str = "",
    due_date: Optional[date_] = None,
    deposit: float = 0.0,
    deposit_method: str = "Cash",
    notes: str = "",
    created_by: Optional[int] = None,
) -> int:
    if not customer_name.strip():
        raise LabellingError("Customer name is required.")
    if item_type not in LABELLING_ITEM_TYPES:
        raise LabellingError(f"Unknown item type '{item_type}'.")
    quantity = int(quantity)
    if quantity <= 0:
        raise LabellingError("Quantity must be greater than 0.")
    unit_price = float(unit_price)
    if unit_price < 0:
        raise LabellingError("Unit price cannot be negative.")
    deposit = float(deposit or 0)
    total = unit_price * quantity
    if deposit < 0 or deposit > total:
        raise LabellingError("Deposit must be between 0 and the total amount.")
    if deposit > 0 and deposit_method not in PAYMENT_METHODS:
        raise LabellingError(f"Unknown payment method '{deposit_method}'.")

    payment_status = _derive_payment_status(total, deposit)

    with session_scope() as session:
        job = LabellingJob(
            customer_name=customer_name.strip(),
            customer_phone=(customer_phone or "").strip() or None,
            item_type=item_type,
            description=(description or "").strip() or None,
            quantity=quantity,
            unit_price=unit_price,
            total_amount=total,
            amount_paid=deposit,
            payment_status=payment_status,
            job_status=JOB_STATUS_PENDING,
            due_date=due_date,
            notes=(notes or "").strip() or None,
            created_by=created_by,
        )
        session.add(job)
        session.flush()

        if deposit > 0:
            _record_payment(
                session,
                job=job,
                amount=deposit,
                method=deposit_method,
                notes="Initial deposit",
                user_id=created_by,
            )

        return job.id


def update_job(
    job_id: int,
    *,
    customer_name: Optional[str] = None,
    customer_phone: Optional[str] = None,
    item_type: Optional[str] = None,
    description: Optional[str] = None,
    quantity: Optional[int] = None,
    unit_price: Optional[float] = None,
    due_date: Optional[date_] = None,
    notes: Optional[str] = None,
    job_status: Optional[str] = None,
) -> None:
    with session_scope() as session:
        job = session.get(LabellingJob, job_id)
        if job is None:
            raise LabellingError("Job not found.")

        if customer_name is not None:
            if not customer_name.strip():
                raise LabellingError("Customer name is required.")
            job.customer_name = customer_name.strip()
        if customer_phone is not None:
            job.customer_phone = customer_phone.strip() or None
        if item_type is not None:
            if item_type not in LABELLING_ITEM_TYPES:
                raise LabellingError(f"Unknown item type '{item_type}'.")
            job.item_type = item_type
        if description is not None:
            job.description = description.strip() or None
        if quantity is not None:
            quantity = int(quantity)
            if quantity <= 0:
                raise LabellingError("Quantity must be greater than 0.")
            job.quantity = quantity
        if unit_price is not None:
            unit_price = float(unit_price)
            if unit_price < 0:
                raise LabellingError("Unit price cannot be negative.")
            job.unit_price = unit_price
        if due_date is not None:
            job.due_date = due_date or None
        if notes is not None:
            job.notes = notes.strip() or None
        if job_status is not None:
            if job_status not in JOB_STATUSES:
                raise LabellingError(f"Unknown job status '{job_status}'.")
            job.job_status = job_status

        # Re-derive total + payment status if quantity / price changed
        new_total = float(job.unit_price or 0) * int(job.quantity or 0)
        if new_total < float(job.amount_paid or 0):
            raise LabellingError(
                "New total cannot be less than amount already paid."
            )
        job.total_amount = new_total
        job.payment_status = _derive_payment_status(new_total, float(job.amount_paid or 0))


def set_job_status(job_id: int, status: str) -> None:
    update_job(job_id, job_status=status)


def delete_job(job_id: int) -> None:
    """Hard-delete a job (Admin only at the UI layer). Removes its associated
    Sale row (created from payments) so dashboards stay consistent."""
    with session_scope() as session:
        job = session.get(LabellingJob, job_id)
        if job is None:
            raise LabellingError("Job not found.")
        # Remove the Sale row (if any) tagged with this job's notes.
        # We don't store a hard FK, so we match on the per-job marker.
        marker = f"[Labelling job #{job_id}]"
        sales = list(session.execute(
            select(Sale).where(
                Sale.category == SALE_CATEGORY_LABELLING,
                Sale.notes.like(f"%{marker}%"),
            )
        ).scalars())
        for s in sales:
            session.delete(s)
        session.delete(job)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

def add_payment(
    job_id: int,
    *,
    amount: float,
    payment_method: str = "Cash",
    paid_on: Optional[datetime] = None,
    notes: str = "",
    user_id: Optional[int] = None,
) -> int:
    if amount is None or float(amount) <= 0:
        raise LabellingError("Payment amount must be greater than 0.")
    if payment_method not in PAYMENT_METHODS:
        raise LabellingError(f"Unknown payment method '{payment_method}'.")

    with session_scope() as session:
        job = session.get(LabellingJob, job_id)
        if job is None:
            raise LabellingError("Job not found.")
        if job.job_status == JOB_STATUS_CANCELLED:
            raise LabellingError("Cannot add payment to a cancelled job.")
        balance = float(job.total_amount or 0) - float(job.amount_paid or 0)
        if float(amount) > balance + 0.0001:
            raise LabellingError(
                f"Payment exceeds remaining balance of {balance:.0f}."
            )

        payment = _record_payment(
            session,
            job=job,
            amount=float(amount),
            method=payment_method,
            paid_on=paid_on,
            notes=notes,
            user_id=user_id,
        )
        return payment.id


def _record_payment(
    session,
    *,
    job: LabellingJob,
    amount: float,
    method: str,
    paid_on: Optional[datetime] = None,
    notes: str = "",
    user_id: Optional[int] = None,
) -> LabellingPayment:
    """Internal: append payment + update job paid amount + create matching Sale."""
    when = paid_on or datetime.utcnow()

    payment = LabellingPayment(
        job_id=job.id,
        amount=float(amount),
        payment_method=method,
        paid_on=when,
        notes=(notes or "").strip() or None,
        recorded_by=user_id,
    )
    session.add(payment)

    job.amount_paid = float(job.amount_paid or 0) + float(amount)
    job.payment_status = _derive_payment_status(
        float(job.total_amount or 0), float(job.amount_paid or 0)
    )
    # Auto-bump from Pending -> In Progress when first money lands
    if job.job_status == JOB_STATUS_PENDING and job.amount_paid > 0:
        job.job_status = JOB_STATUS_IN_PROGRESS

    # Mirror the payment as a Sale so it shows up in revenue / dashboard.
    description = f"Labelling: {job.item_type} for {job.customer_name}"
    sale = Sale(
        sale_date=when,
        category=SALE_CATEGORY_LABELLING,
        payment_method=method,
        total_amount=float(amount),
        notes=f"[Labelling job #{job.id}] {notes}".strip(),
        recorded_by=user_id,
    )
    session.add(sale)
    session.flush()
    session.add(SaleItem(
        sale_id=sale.id,
        description=description,
        quantity=1.0,
        unit_price=float(amount),
        line_total=float(amount),
    ))

    return payment


def list_payments(job_id: int) -> List[PaymentSummary]:
    with session_scope() as session:
        rows = list(session.execute(
            select(LabellingPayment)
            .where(LabellingPayment.job_id == job_id)
            .order_by(LabellingPayment.paid_on.desc())
        ).scalars())
        return [
            PaymentSummary(
                id=r.id,
                job_id=r.job_id,
                paid_on=r.paid_on,
                amount=float(r.amount or 0),
                payment_method=r.payment_method,
                notes=r.notes or "",
            )
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def list_jobs(
    *,
    status: Optional[str] = None,
    payment_status: Optional[str] = None,
    only_active: bool = False,
    only_with_balance: bool = False,
    search: str = "",
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: int = 500,
) -> List[JobSummary]:
    with session_scope() as session:
        stmt = select(LabellingJob).order_by(
            LabellingJob.created_at.desc(), LabellingJob.id.desc()
        )
        if status:
            stmt = stmt.where(LabellingJob.job_status == status)
        if payment_status:
            stmt = stmt.where(LabellingJob.payment_status == payment_status)
        if only_active:
            stmt = stmt.where(LabellingJob.job_status.notin_(
                (JOB_STATUS_COLLECTED, JOB_STATUS_CANCELLED)
            ))
        if start:
            stmt = stmt.where(LabellingJob.created_at >= start)
        if end:
            stmt = stmt.where(LabellingJob.created_at < end)
        rows = list(session.execute(stmt).scalars())

        if search:
            term = search.lower()
            rows = [
                r for r in rows
                if term in r.customer_name.lower()
                or term in (r.customer_phone or "").lower()
                or term in (r.description or "").lower()
                or term in r.item_type.lower()
            ]

        if only_with_balance:
            rows = [r for r in rows if float(r.amount_paid or 0) < float(r.total_amount or 0)]

        rows = rows[:limit]
        return [_to_summary(r) for r in rows]


def get_job(job_id: int) -> Optional[JobSummary]:
    with session_scope() as session:
        job = session.execute(
            select(LabellingJob)
            .options(joinedload(LabellingJob.payments))
            .where(LabellingJob.id == job_id)
        ).unique().scalar_one_or_none()
        if job is None:
            return None
        return _to_summary(job)


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------

def total_revenue_in_range(start: datetime, end: datetime) -> float:
    """Total labelling payments collected in the window."""
    with session_scope() as session:
        value = session.execute(
            select(func.sum(LabellingPayment.amount))
            .where(and_(
                LabellingPayment.paid_on >= start,
                LabellingPayment.paid_on < end,
            ))
        ).scalar()
        return float(value or 0)


def outstanding_balance() -> float:
    """Sum of (total - paid) across non-cancelled jobs."""
    with session_scope() as session:
        rows = list(session.execute(
            select(LabellingJob.total_amount, LabellingJob.amount_paid)
            .where(LabellingJob.job_status != JOB_STATUS_CANCELLED)
        ).all())
        return float(sum(
            max(0.0, float(t or 0) - float(p or 0)) for t, p in rows
        ))


def status_counts() -> dict:
    counts = {s: 0 for s in JOB_STATUSES}
    with session_scope() as session:
        rows = session.execute(
            select(LabellingJob.job_status, func.count(LabellingJob.id))
            .group_by(LabellingJob.job_status)
        ).all()
        for status, n in rows:
            counts[status] = int(n or 0)
    return counts


def today_revenue() -> float:
    return total_revenue_in_range(*day_bounds(date_.today()))


def month_revenue() -> float:
    return total_revenue_in_range(*month_bounds(date_.today()))
