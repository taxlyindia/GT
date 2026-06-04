# models/erp_models.py
# ERP-grade accounting models — GoldTrader Pro
# Rule: "Posted transaction is permanent. Correction through reversal/adjustment."

from __future__ import annotations
import enum
from datetime import datetime, date
from decimal import Decimal
from typing import Optional, Any

from sqlalchemy import (
    String, Integer, Numeric, Boolean, Date, DateTime,
    ForeignKey, Text, Enum as SAEnum, BigInteger,
    UniqueConstraint, Index, JSON
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base


# ── Enums ────────────────────────────────────────────────────

class AmendmentType(str, enum.Enum):
    financial     = "financial"       # qty/rate/items changed → adjustment entry
    non_financial = "non_financial"   # address/GSTIN/notes only → simple update


class DocumentType(str, enum.Enum):
    sales_invoice = "sales_invoice"
    purchase_bill = "purchase_bill"


class AuditEventType(str, enum.Enum):
    invoice_created  = "invoice_created"
    invoice_cancelled = "invoice_cancelled"
    invoice_amended  = "invoice_amended"
    purchase_created = "purchase_created"
    purchase_cancelled = "purchase_cancelled"
    purchase_amended = "purchase_amended"
    payment_received = "payment_received"
    payment_recorded = "payment_recorded"
    fifo_consumed    = "fifo_consumed"
    fifo_restored    = "fifo_restored"
    fifo_adjusted    = "fifo_adjusted"
    stock_adjusted   = "stock_adjusted"


# ── FIFO Consumption History ──────────────────────────────────

class FIFOConsumptionHistory(Base):
    """
    Permanent record of every FIFO lot consumed by a sale.
    Every invoice_item gets one or more rows here, one per purchase lot touched.
    On cancellation: is_reversed=True, quantities restored to lot.
    On amendment:    new rows with amendment_version > 0 for incremental qty only.
    """
    __tablename__ = "fifo_consumption_history"
    __table_args__ = (
        Index("ix_fifo_invoice",  "invoice_id"),
        Index("ix_fifo_item",     "invoice_item_id"),
        Index("ix_fifo_stock",    "stock_item_id"),
        Index("ix_fifo_purchase", "purchase_txn_id"),
        Index("ix_fifo_tenant",   "tenant_id"),
    )

    id                : Mapped[int]          = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id         : Mapped[int]          = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))

    invoice_item_id   : Mapped[int]          = mapped_column(ForeignKey("invoice_items.id", ondelete="CASCADE"))
    invoice_id        : Mapped[int]          = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    purchase_txn_id   : Mapped[int]          = mapped_column(ForeignKey("stock_transactions.id"))
    stock_item_id     : Mapped[int]          = mapped_column(ForeignKey("stock_items.id"))

    consumed_qty      : Mapped[Decimal]      = mapped_column(Numeric(15, 3), nullable=False)
    cost_rate         : Mapped[Decimal]      = mapped_column(Numeric(15, 2), nullable=False)
    cost_value        : Mapped[Decimal]      = mapped_column(Numeric(15, 2), nullable=False)  # consumed_qty * cost_rate

    amendment_version : Mapped[int]          = mapped_column(Integer, default=0)    # 0 = original sale
    is_reversed       : Mapped[bool]         = mapped_column(Boolean, default=False)
    reversed_at       : Mapped[datetime|None]= mapped_column(DateTime(timezone=True))
    reversed_by       : Mapped[int|None]     = mapped_column(ForeignKey("users.id"))

    created_at        : Mapped[datetime]     = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    created_by        : Mapped[int|None]     = mapped_column(ForeignKey("users.id"))


# ── Reversal Entries ──────────────────────────────────────────

class ReversalEntry(Base):
    """
    Links original invoice/bill to its reversal document.
    Created on cancellation only.
    """
    __tablename__ = "reversal_entries"
    __table_args__ = (
        Index("ix_reversal_orig_inv", "original_invoice_id"),
        Index("ix_reversal_orig_sup", "original_sup_invoice_id"),
        Index("ix_reversal_tenant",   "tenant_id"),
    )

    id                      : Mapped[int]          = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id               : Mapped[int]          = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))

    document_type           : Mapped[DocumentType] = mapped_column(SAEnum(DocumentType))

    original_invoice_id     : Mapped[int|None]     = mapped_column(ForeignKey("invoices.id"))
    original_sup_invoice_id : Mapped[int|None]     = mapped_column(ForeignKey("supplier_invoices.id"))

    reversal_invoice_id     : Mapped[int|None]     = mapped_column(ForeignKey("invoices.id"))
    reversal_sup_invoice_id : Mapped[int|None]     = mapped_column(ForeignKey("supplier_invoices.id"))

    subtotal_reversed       : Mapped[Decimal]      = mapped_column(Numeric(15, 2), default=0)
    cgst_reversed           : Mapped[Decimal]      = mapped_column(Numeric(15, 2), default=0)
    sgst_reversed           : Mapped[Decimal]      = mapped_column(Numeric(15, 2), default=0)
    igst_reversed           : Mapped[Decimal]      = mapped_column(Numeric(15, 2), default=0)
    total_reversed          : Mapped[Decimal]      = mapped_column(Numeric(15, 2), default=0)

    cancelled_by            : Mapped[int|None]     = mapped_column(ForeignKey("users.id"))
    cancelled_at            : Mapped[datetime|None]= mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    cancellation_reason     : Mapped[str|None]     = mapped_column(Text)
    notes                   : Mapped[str|None]     = mapped_column(Text)
    created_at              : Mapped[datetime]     = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


# ── Invoice Versions (Amendment History) ──────────────────────

class InvoiceVersion(Base):
    """
    Snapshot of invoice state BEFORE an amendment was applied.
    Version 1 = first amendment (snapshot of original).
    Version 2 = second amendment (snapshot of v1 state). Etc.
    """
    __tablename__ = "invoice_versions"
    __table_args__ = (
        Index("ix_inv_ver_invoice", "invoice_id"),
        Index("ix_inv_ver_tenant",  "tenant_id"),
        UniqueConstraint("invoice_id", "version_no", name="uq_inv_ver"),
    )

    id                      : Mapped[int]              = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id               : Mapped[int]              = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    invoice_id              : Mapped[int]              = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))

    version_no              : Mapped[int]              = mapped_column(Integer, nullable=False)
    amendment_type          : Mapped[AmendmentType]    = mapped_column(SAEnum(AmendmentType))

    # Full header snapshot
    snapshot_invoice_date   : Mapped[date|None]        = mapped_column(Date)
    snapshot_customer_name  : Mapped[str|None]         = mapped_column(String(200))
    snapshot_customer_pan   : Mapped[str|None]         = mapped_column(String(10))
    snapshot_customer_state : Mapped[str|None]         = mapped_column(String(50))
    snapshot_customer_gstin : Mapped[str|None]         = mapped_column(String(15))
    snapshot_pay_mode       : Mapped[str|None]         = mapped_column(String(20))
    snapshot_gst_type       : Mapped[str|None]         = mapped_column(String(20))
    snapshot_gst_rate       : Mapped[Decimal|None]     = mapped_column(Numeric(5, 2))
    snapshot_subtotal       : Mapped[Decimal|None]     = mapped_column(Numeric(15, 2))
    snapshot_cgst           : Mapped[Decimal|None]     = mapped_column(Numeric(15, 2))
    snapshot_sgst           : Mapped[Decimal|None]     = mapped_column(Numeric(15, 2))
    snapshot_igst           : Mapped[Decimal|None]     = mapped_column(Numeric(15, 2))
    snapshot_grand_total    : Mapped[Decimal|None]     = mapped_column(Numeric(15, 2))
    snapshot_notes          : Mapped[str|None]         = mapped_column(Text)
    snapshot_items          : Mapped[Any]              = mapped_column(JSON)   # full items array

    # Net financial change this amendment introduces
    adjustment_subtotal     : Mapped[Decimal]          = mapped_column(Numeric(15, 2), default=0)
    adjustment_cgst         : Mapped[Decimal]          = mapped_column(Numeric(15, 2), default=0)
    adjustment_sgst         : Mapped[Decimal]          = mapped_column(Numeric(15, 2), default=0)
    adjustment_igst         : Mapped[Decimal]          = mapped_column(Numeric(15, 2), default=0)
    adjustment_grand_total  : Mapped[Decimal]          = mapped_column(Numeric(15, 2), default=0)

    amendment_reason        : Mapped[str|None]         = mapped_column(Text)
    amended_by              : Mapped[int|None]         = mapped_column(ForeignKey("users.id"))
    amended_at              : Mapped[datetime]         = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


# ── Purchase Bill Versions ─────────────────────────────────────

class PurchaseVersion(Base):
    """Same as InvoiceVersion but for supplier purchase bills."""
    __tablename__ = "purchase_versions"
    __table_args__ = (
        Index("ix_pur_ver_invoice", "invoice_id"),
        Index("ix_pur_ver_tenant",  "tenant_id"),
        UniqueConstraint("invoice_id", "version_no", name="uq_pur_ver"),
    )

    id                      : Mapped[int]              = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id               : Mapped[int]              = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    invoice_id              : Mapped[int]              = mapped_column(ForeignKey("supplier_invoices.id", ondelete="CASCADE"))

    version_no              : Mapped[int]              = mapped_column(Integer, nullable=False)
    amendment_type          : Mapped[AmendmentType]    = mapped_column(SAEnum(AmendmentType))

    snapshot_invoice_no     : Mapped[str|None]         = mapped_column(String(30))
    snapshot_invoice_date   : Mapped[date|None]        = mapped_column(Date)
    snapshot_supplier_name  : Mapped[str|None]         = mapped_column(String(200))
    snapshot_gst_type       : Mapped[str|None]         = mapped_column(String(20))
    snapshot_gst_rate       : Mapped[Decimal|None]     = mapped_column(Numeric(5, 2))
    snapshot_subtotal       : Mapped[Decimal|None]     = mapped_column(Numeric(15, 2))
    snapshot_cgst           : Mapped[Decimal|None]     = mapped_column(Numeric(15, 2))
    snapshot_sgst           : Mapped[Decimal|None]     = mapped_column(Numeric(15, 2))
    snapshot_igst           : Mapped[Decimal|None]     = mapped_column(Numeric(15, 2))
    snapshot_grand_total    : Mapped[Decimal|None]     = mapped_column(Numeric(15, 2))
    snapshot_notes          : Mapped[str|None]         = mapped_column(Text)
    snapshot_items          : Mapped[Any]              = mapped_column(JSON)

    adjustment_subtotal     : Mapped[Decimal]          = mapped_column(Numeric(15, 2), default=0)
    adjustment_cgst         : Mapped[Decimal]          = mapped_column(Numeric(15, 2), default=0)
    adjustment_sgst         : Mapped[Decimal]          = mapped_column(Numeric(15, 2), default=0)
    adjustment_igst         : Mapped[Decimal]          = mapped_column(Numeric(15, 2), default=0)
    adjustment_grand_total  : Mapped[Decimal]          = mapped_column(Numeric(15, 2), default=0)

    amendment_reason        : Mapped[str|None]         = mapped_column(Text)
    amended_by              : Mapped[int|None]         = mapped_column(ForeignKey("users.id"))
    amended_at              : Mapped[datetime]         = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


# ── Transaction Audit Log ─────────────────────────────────────

class TransactionAuditLog(Base):
    """
    Append-only ledger of every financial event.
    Never updated. Corrections appear as new rows.
    """
    __tablename__ = "transaction_audit_log"
    __table_args__ = (
        Index("ix_audit_tenant",  "tenant_id"),
        Index("ix_audit_invoice", "invoice_id"),
        Index("ix_audit_sup_inv", "sup_invoice_id"),
        Index("ix_audit_event",   "tenant_id", "event_type"),
        Index("ix_audit_created", "created_at"),
    )

    id              : Mapped[int]              = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id       : Mapped[int]              = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))

    event_type      : Mapped[AuditEventType]   = mapped_column(SAEnum(AuditEventType))

    invoice_id      : Mapped[int|None]         = mapped_column(ForeignKey("invoices.id"))
    sup_invoice_id  : Mapped[int|None]         = mapped_column(ForeignKey("supplier_invoices.id"))
    payment_id      : Mapped[int|None]         = mapped_column(ForeignKey("payments.id"))
    stock_txn_id    : Mapped[int|None]         = mapped_column(ForeignKey("stock_transactions.id"))

    description     : Mapped[str]              = mapped_column(Text, nullable=False)

    debit_amount    : Mapped[Decimal]          = mapped_column(Numeric(15, 2), default=0)
    credit_amount   : Mapped[Decimal]          = mapped_column(Numeric(15, 2), default=0)
    ledger_account  : Mapped[str|None]         = mapped_column(String(100))

    version_no      : Mapped[int]              = mapped_column(Integer, default=0)
    original_txn_id : Mapped[int|None]         = mapped_column(Integer)  # for reversals
    reversal_ref_id : Mapped[int|None]         = mapped_column(Integer)

    created_by      : Mapped[int|None]         = mapped_column(ForeignKey("users.id"))
    created_at      : Mapped[datetime]         = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    metadata_       : Mapped[Any]              = mapped_column("metadata", JSON)
