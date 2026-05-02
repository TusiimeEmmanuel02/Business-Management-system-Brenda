"""
Report aggregation service.

Pulls together the numbers for the various report screens and exporters.
Reports are computed for an arbitrary [start, end) datetime window so the
UI can offer Today / This week / This month / Custom range presets.

Each function returns plain dataclasses or dicts so they're easy to render
in a table, write to PDF, or hand to openpyxl.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, func, select

from app.core.constants import (
    EXPENSE_CATEGORIES,
    JOB_STATUSES,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COLLECTED,
    JOB_STATUS_COMPLETED,
    PAYMENT_BANK,
    PAYMENT_CASH,
    PAYMENT_MOBILE_MONEY,
    SALE_CATEGORIES,
    SALE_CATEGORY_COMPUTER,
    SALE_CATEGORY_FRIDGE,
    SALE_CATEGORY_LABELLING,
    WITHDRAWAL_REASONS,
)
from app.core.utils import day_bounds, month_bounds, week_bounds
from app.database.models import (
    BusinessExpense,
    LabellingJob,
    LabellingPayment,
    OwnerWithdrawal,
    Product,
    Sale,
    SaleItem,
    StockMovement,
    DailyClosing,
    CashSession,
)
from app.database.session import session_scope


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------

@dataclass
class DailyReport:
    report_date: date_
    computer_sales: float = 0.0
    fridge_sales: float = 0.0
    labelling_sales: float = 0.0
    gross_sales: float = 0.0
    cash_sales: float = 0.0
    mobile_money_sales: float = 0.0
    bank_sales: float = 0.0
    business_expenses: float = 0.0
    net_profit: float = 0.0
    owner_withdrawals: float = 0.0
    expected_cash: float = 0.0
    actual_cash: float = 0.0
    cash_difference: float = 0.0
    has_closing: bool = False


@dataclass
class MonthlyReport:
    month_label: str  # e.g. "March 2026"
    start: date_
    end: date_
    computer_sales: float = 0.0
    fridge_sales: float = 0.0
    labelling_sales: float = 0.0
    gross_sales: float = 0.0
    cash_sales: float = 0.0
    mobile_money_sales: float = 0.0
    bank_sales: float = 0.0
    business_expenses: float = 0.0
    net_profit: float = 0.0
    owner_withdrawals: float = 0.0
    available_cash: float = 0.0


@dataclass
class CategoryReportRow:
    category: str
    total: float
    percentage: float


@dataclass
class StockReportRow:
    product_name: str
    brand: str
    stock_in: int
    stock_sold: int
    remaining: int
    buying_price: float
    selling_price: float
    stock_value: float        # remaining * buying_price
    estimated_profit: float   # remaining * (selling - buying)


@dataclass
class ExpenseReportRow:
    category: str
    total: float
    record_count: int
    percentage: float


@dataclass
class WithdrawalReportRow:
    reason: str
    total: float
    record_count: int
    percentage: float


@dataclass
class LabellingReportRow:
    status: str
    count: int
    total_value: float
    outstanding: float


@dataclass
class ProfitLossReport:
    start: date_
    end: date_
    label: str
    gross_sales: float
    sales_by_category: Dict[str, float]
    business_expenses: float
    expenses_by_category: Dict[str, float]
    net_profit: float
    owner_withdrawals: float
    withdrawals_by_reason: Dict[str, float]
    expected_cash: float


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------

def date_range_to_window(d_from: date_, d_to: date_) -> Tuple[datetime, datetime]:
    """Inclusive [d_from, d_to] dates -> half-open datetime window."""
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    start = datetime.combine(d_from, time.min)
    end = datetime.combine(d_to + timedelta(days=1), time.min)
    return start, end


def preset_window(name: str, *, today: Optional[date_] = None
                  ) -> Tuple[datetime, datetime, str]:
    """Return (start, end, label) for one of the canonical presets."""
    today = today or date_.today()
    if name == "Today":
        start, end = day_bounds(today)
        label = today.strftime("%d %b %Y")
    elif name == "Yesterday":
        y = today - timedelta(days=1)
        start, end = day_bounds(y)
        label = y.strftime("%d %b %Y")
    elif name == "This week":
        start, end = week_bounds(today)
        label = f"Week of {start.strftime('%d %b %Y')}"
    elif name == "This month":
        start, end = month_bounds(today)
        label = today.strftime("%B %Y")
    elif name == "Last 30 days":
        start = datetime.combine(today - timedelta(days=29), time.min)
        end = datetime.combine(today + timedelta(days=1), time.min)
        label = f"{(today - timedelta(days=29)).strftime('%d %b')} - {today.strftime('%d %b %Y')}"
    elif name == "Last 90 days":
        start = datetime.combine(today - timedelta(days=89), time.min)
        end = datetime.combine(today + timedelta(days=1), time.min)
        label = f"{(today - timedelta(days=89)).strftime('%d %b')} - {today.strftime('%d %b %Y')}"
    else:  # All time
        start = datetime(2000, 1, 1)
        end = datetime.combine(today + timedelta(days=1), time.min)
        label = "All time"
    return start, end, label


# ---------------------------------------------------------------------------
# Window aggregations
# ---------------------------------------------------------------------------

def _sales_by_category(session, start: datetime, end: datetime) -> Dict[str, float]:
    rows = session.execute(
        select(Sale.category, func.sum(Sale.total_amount))
        .where(and_(Sale.sale_date >= start, Sale.sale_date < end))
        .group_by(Sale.category)
    ).all()
    out = {cat: 0.0 for cat in SALE_CATEGORIES}
    for cat, total in rows:
        out[cat] = float(total or 0)
    return out


def _sales_by_payment(session, start: datetime, end: datetime) -> Dict[str, float]:
    rows = session.execute(
        select(Sale.payment_method, func.sum(Sale.total_amount))
        .where(and_(Sale.sale_date >= start, Sale.sale_date < end))
        .group_by(Sale.payment_method)
    ).all()
    out: Dict[str, float] = {}
    for pm, total in rows:
        out[pm] = float(total or 0)
    return out


def _sum_expenses(session, start: datetime, end: datetime,
                  *, cash_only: bool = False) -> float:
    stmt = select(func.sum(BusinessExpense.amount)).where(
        and_(BusinessExpense.expense_date >= start,
             BusinessExpense.expense_date < end)
    )
    if cash_only:
        stmt = stmt.where(BusinessExpense.payment_method == PAYMENT_CASH)
    return float(session.execute(stmt).scalar() or 0)


def _expenses_by_category(session, start: datetime, end: datetime
                          ) -> Dict[str, float]:
    rows = session.execute(
        select(BusinessExpense.category, func.sum(BusinessExpense.amount))
        .where(and_(BusinessExpense.expense_date >= start,
                    BusinessExpense.expense_date < end))
        .group_by(BusinessExpense.category)
    ).all()
    out = {cat: 0.0 for cat in EXPENSE_CATEGORIES}
    for cat, total in rows:
        out[cat] = float(total or 0)
    return out


def _expense_record_count(session, start: datetime, end: datetime
                          ) -> Dict[str, int]:
    rows = session.execute(
        select(BusinessExpense.category, func.count(BusinessExpense.id))
        .where(and_(BusinessExpense.expense_date >= start,
                    BusinessExpense.expense_date < end))
        .group_by(BusinessExpense.category)
    ).all()
    out = {cat: 0 for cat in EXPENSE_CATEGORIES}
    for cat, n in rows:
        out[cat] = int(n or 0)
    return out


def _sum_withdrawals(session, start: datetime, end: datetime) -> float:
    return float(session.execute(
        select(func.sum(OwnerWithdrawal.amount))
        .where(and_(OwnerWithdrawal.withdrawal_date >= start,
                    OwnerWithdrawal.withdrawal_date < end))
    ).scalar() or 0)


def _withdrawals_by_reason(session, start: datetime, end: datetime
                           ) -> Dict[str, float]:
    rows = session.execute(
        select(OwnerWithdrawal.reason, func.sum(OwnerWithdrawal.amount))
        .where(and_(OwnerWithdrawal.withdrawal_date >= start,
                    OwnerWithdrawal.withdrawal_date < end))
        .group_by(OwnerWithdrawal.reason)
    ).all()
    out = {r: 0.0 for r in WITHDRAWAL_REASONS}
    for reason, total in rows:
        out[reason] = float(total or 0)
    return out


def _withdrawal_record_count(session, start: datetime, end: datetime
                             ) -> Dict[str, int]:
    rows = session.execute(
        select(OwnerWithdrawal.reason, func.count(OwnerWithdrawal.id))
        .where(and_(OwnerWithdrawal.withdrawal_date >= start,
                    OwnerWithdrawal.withdrawal_date < end))
        .group_by(OwnerWithdrawal.reason)
    ).all()
    out = {r: 0 for r in WITHDRAWAL_REASONS}
    for reason, n in rows:
        out[reason] = int(n or 0)
    return out


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def daily_report(d: Optional[date_] = None) -> DailyReport:
    d = d or date_.today()
    start, end = day_bounds(d)
    with session_scope() as session:
        cats = _sales_by_category(session, start, end)
        pms = _sales_by_payment(session, start, end)
        expenses = _sum_expenses(session, start, end)
        cash_expenses = _sum_expenses(session, start, end, cash_only=True)
        withdrawals = _sum_withdrawals(session, start, end)

        # Opening cash + closing details if available
        opening_cash = float(session.execute(
            select(CashSession.opening_cash)
            .where(CashSession.session_date == d)
        ).scalar() or 0)
        closing = session.execute(
            select(DailyClosing).where(DailyClosing.session_date == d)
        ).scalar_one_or_none()

    gross = sum(cats.values())
    net = gross - expenses
    cash_sales = float(pms.get(PAYMENT_CASH, 0))
    expected = opening_cash + cash_sales - cash_expenses - withdrawals

    return DailyReport(
        report_date=d,
        computer_sales=cats.get(SALE_CATEGORY_COMPUTER, 0),
        fridge_sales=cats.get(SALE_CATEGORY_FRIDGE, 0),
        labelling_sales=cats.get(SALE_CATEGORY_LABELLING, 0),
        gross_sales=gross,
        cash_sales=cash_sales,
        mobile_money_sales=float(pms.get(PAYMENT_MOBILE_MONEY, 0)),
        bank_sales=float(pms.get(PAYMENT_BANK, 0)),
        business_expenses=expenses,
        net_profit=net,
        owner_withdrawals=withdrawals,
        expected_cash=expected if not closing else float(closing.expected_cash or 0),
        actual_cash=float(closing.actual_cash) if closing else 0.0,
        cash_difference=float(closing.difference) if closing else 0.0,
        has_closing=closing is not None,
    )


def monthly_report(d: Optional[date_] = None) -> MonthlyReport:
    d = d or date_.today()
    start, end = month_bounds(d)
    label = d.strftime("%B %Y")
    with session_scope() as session:
        cats = _sales_by_category(session, start, end)
        pms = _sales_by_payment(session, start, end)
        expenses = _sum_expenses(session, start, end)
        withdrawals = _sum_withdrawals(session, start, end)

    gross = sum(cats.values())
    net = gross - expenses
    available = gross - expenses - withdrawals

    return MonthlyReport(
        month_label=label,
        start=start.date(),
        end=(end - timedelta(days=1)).date(),
        computer_sales=cats.get(SALE_CATEGORY_COMPUTER, 0),
        fridge_sales=cats.get(SALE_CATEGORY_FRIDGE, 0),
        labelling_sales=cats.get(SALE_CATEGORY_LABELLING, 0),
        gross_sales=gross,
        cash_sales=float(pms.get(PAYMENT_CASH, 0)),
        mobile_money_sales=float(pms.get(PAYMENT_MOBILE_MONEY, 0)),
        bank_sales=float(pms.get(PAYMENT_BANK, 0)),
        business_expenses=expenses,
        net_profit=net,
        owner_withdrawals=withdrawals,
        available_cash=available,
    )


def sales_by_category_report(start: datetime, end: datetime
                             ) -> List[CategoryReportRow]:
    with session_scope() as session:
        cats = _sales_by_category(session, start, end)
    total = sum(cats.values()) or 1
    return [
        CategoryReportRow(
            category=cat,
            total=cats.get(cat, 0),
            percentage=(cats.get(cat, 0) / total) * 100 if total else 0,
        )
        for cat in SALE_CATEGORIES
    ]


def expense_report(start: datetime, end: datetime) -> List[ExpenseReportRow]:
    with session_scope() as session:
        totals = _expenses_by_category(session, start, end)
        counts = _expense_record_count(session, start, end)
    grand = sum(totals.values()) or 1
    return [
        ExpenseReportRow(
            category=cat,
            total=totals.get(cat, 0),
            record_count=counts.get(cat, 0),
            percentage=(totals.get(cat, 0) / grand) * 100 if grand else 0,
        )
        for cat in EXPENSE_CATEGORIES
    ]


def withdrawal_report(start: datetime, end: datetime
                      ) -> List[WithdrawalReportRow]:
    with session_scope() as session:
        totals = _withdrawals_by_reason(session, start, end)
        counts = _withdrawal_record_count(session, start, end)
    grand = sum(totals.values()) or 1
    return [
        WithdrawalReportRow(
            reason=reason,
            total=totals.get(reason, 0),
            record_count=counts.get(reason, 0),
            percentage=(totals.get(reason, 0) / grand) * 100 if grand else 0,
        )
        for reason in WITHDRAWAL_REASONS
    ]


def stock_report() -> List[StockReportRow]:
    """Stock report is point-in-time (current stock + lifetime in/out)."""
    with session_scope() as session:
        products = list(session.execute(
            select(Product).order_by(Product.name)
        ).scalars())

        # Sum stock-in (positive movements) and stock-out (negative movements)
        in_rows = session.execute(
            select(StockMovement.product_id, func.sum(StockMovement.quantity))
            .where(StockMovement.quantity > 0)
            .group_by(StockMovement.product_id)
        ).all()
        out_rows = session.execute(
            select(StockMovement.product_id, func.sum(StockMovement.quantity))
            .where(StockMovement.quantity < 0)
            .group_by(StockMovement.product_id)
        ).all()
        ins = {pid: int(q or 0) for pid, q in in_rows}
        outs = {pid: int(q or 0) for pid, q in out_rows}

        result: List[StockReportRow] = []
        for p in products:
            stock_in = ins.get(p.id, 0)
            stock_sold = -outs.get(p.id, 0)  # stored as negative -> positive count
            buy = float(p.buying_price or 0)
            sell = float(p.selling_price or 0)
            remaining = int(p.current_stock or 0)
            stock_value = remaining * buy
            estimated_profit = remaining * (sell - buy)
            result.append(StockReportRow(
                product_name=p.name,
                brand=p.brand or "",
                stock_in=stock_in,
                stock_sold=stock_sold,
                remaining=remaining,
                buying_price=buy,
                selling_price=sell,
                stock_value=stock_value,
                estimated_profit=estimated_profit,
            ))
        return result


def labelling_report() -> List[LabellingReportRow]:
    """Counts + totals + outstanding per job status."""
    with session_scope() as session:
        rows = session.execute(
            select(
                LabellingJob.job_status,
                func.count(LabellingJob.id),
                func.coalesce(func.sum(LabellingJob.total_amount), 0),
                func.coalesce(func.sum(LabellingJob.amount_paid), 0),
            ).group_by(LabellingJob.job_status)
        ).all()

    out: List[LabellingReportRow] = []
    by_status = {s: (0, 0.0, 0.0) for s in JOB_STATUSES}
    for status, count, total, paid in rows:
        by_status[status] = (int(count or 0), float(total or 0), float(paid or 0))

    for status in JOB_STATUSES:
        count, total, paid = by_status[status]
        outstanding = max(0.0, total - paid)
        if status == JOB_STATUS_CANCELLED:
            outstanding = 0.0
        out.append(LabellingReportRow(
            status=status,
            count=count,
            total_value=total,
            outstanding=outstanding,
        ))
    return out


def labelling_revenue_in_range(start: datetime, end: datetime) -> float:
    with session_scope() as session:
        return float(session.execute(
            select(func.sum(LabellingPayment.amount))
            .where(and_(LabellingPayment.paid_on >= start,
                        LabellingPayment.paid_on < end))
        ).scalar() or 0)


def profit_loss_report(start: datetime, end: datetime, label: str
                       ) -> ProfitLossReport:
    with session_scope() as session:
        cats = _sales_by_category(session, start, end)
        pms = _sales_by_payment(session, start, end)
        expenses = _sum_expenses(session, start, end)
        cash_expenses = _sum_expenses(session, start, end, cash_only=True)
        exp_by_cat = _expenses_by_category(session, start, end)
        withdrawals = _sum_withdrawals(session, start, end)
        wd_by_reason = _withdrawals_by_reason(session, start, end)

    gross = sum(cats.values())
    cash_sales = float(pms.get(PAYMENT_CASH, 0))
    net = gross - expenses
    expected_cash = cash_sales - cash_expenses - withdrawals

    # Drop zero-rows for cleaner display
    exp_by_cat = {k: v for k, v in exp_by_cat.items() if v > 0}
    wd_by_reason = {k: v for k, v in wd_by_reason.items() if v > 0}

    return ProfitLossReport(
        start=start.date(),
        end=(end - timedelta(days=1)).date(),
        label=label,
        gross_sales=gross,
        sales_by_category=cats,
        business_expenses=expenses,
        expenses_by_category=exp_by_cat,
        net_profit=net,
        owner_withdrawals=withdrawals,
        withdrawals_by_reason=wd_by_reason,
        expected_cash=expected_cash,
    )


# ---------------------------------------------------------------------------
# Audit log feed (used by the Audit Logs page)
# ---------------------------------------------------------------------------

def list_audit_logs(*, start: Optional[datetime] = None,
                    end: Optional[datetime] = None,
                    module: Optional[str] = None,
                    action: Optional[str] = None,
                    limit: int = 500) -> List:
    from app.database.models import AuditLog
    with session_scope() as session:
        stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
        if start:
            stmt = stmt.where(AuditLog.created_at >= start)
        if end:
            stmt = stmt.where(AuditLog.created_at < end)
        if module:
            stmt = stmt.where(AuditLog.module == module)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        rows = list(session.execute(stmt).scalars())
        for r in rows:
            session.expunge(r)
        return rows
