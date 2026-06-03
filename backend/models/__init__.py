# models/__init__.py — All ORM models (mirrors PostgreSQL schema)
# Mobile number is PRIMARY KEY for customers (per tenant)

from __future__ import annotations
import enum
from datetime import datetime, date
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    String, Integer, Numeric, Boolean, Date, DateTime,
    ForeignKey, Text, Enum as SAEnum, UniqueConstraint,
    Index, event
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base


# ── Enums ────────────────────────────────────────────────────

class PlanEnum(str, enum.Enum):
    demo    = "demo"
    annual  = "annual"
    expired = "expired"

class RoleEnum(str, enum.Enum):
    admin  = "admin"
    user   = "user"
    viewer = "viewer"

class AuthProvider(str, enum.Enum):
    password = "password"
    google   = "google"

class ApprovalStatus(str, enum.Enum):
    pending  = "pending"
    approved = "approved"
    rejected = "rejected"
    trial    = "trial"          # 10-day Google trial

class PayModeEnum(str, enum.Enum):
    Cash     = "Cash"
    UPI      = "UPI"
    Card     = "Card"
    NEFT     = "NEFT/RTGS"
    Cheque   = "Cheque"

class GSTTypeEnum(str, enum.Enum):
    CGST_SGST = "CGST+SGST"
    IGST      = "IGST"
    Exempt    = "Exempt"

class CategoryEnum(str, enum.Enum):
    Gold           = "Gold"
    Silver         = "Silver"
    Diamond        = "Diamond"
    PolishCharges  = "Polish Charges"

class UnitEnum(str, enum.Enum):
    grm = "grm"
    crt = "crt"

class CashEntryType(str, enum.Enum):
    cash_in      = "cash_in"
    cash_out     = "cash_out"
    cash_to_bank = "cash_to_bank"
    bank_in      = "bank_in"

class InvoiceStatus(str, enum.Enum):
    active    = "active"
    cancelled = "cancelled"
    draft     = "draft"

class PaymentStatus(str, enum.Enum):
    paid    = "paid"
    partial = "partial"
    unpaid  = "unpaid"

class StockTxnType(str, enum.Enum):
    purchase   = "purchase"
    sale       = "sale"
    adjustment = "adjustment"
    opening    = "opening"


# ── Tenant ───────────────────────────────────────────────────

class Tenant(Base):
    __tablename__ = "tenants"

    id:             Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_name:   Mapped[str]      = mapped_column(String(200), nullable=False)
    gstin:          Mapped[str|None] = mapped_column(String(15))
    phone:          Mapped[str|None] = mapped_column(String(15))
    email:          Mapped[str|None] = mapped_column(String(100))
    address:        Mapped[str|None] = mapped_column(Text)
    state:          Mapped[str|None] = mapped_column(String(50))
    logo_url:       Mapped[str|None] = mapped_column(Text)
    pan:            Mapped[str|None] = mapped_column(String(10))
    upi_id:         Mapped[str|None] = mapped_column(String(100))
    qr_code_url:    Mapped[str|None] = mapped_column(Text)
    bank_name:      Mapped[str|None] = mapped_column(String(100))
    bank_account_no:Mapped[str|None] = mapped_column(String(30))
    bank_ifsc:      Mapped[str|None] = mapped_column(String(15))
    bank_branch:    Mapped[str|None] = mapped_column(String(100))
    terms_conditions:Mapped[str|None]= mapped_column(Text)
    authorised_person:Mapped[str|None]=mapped_column(String(100))
    plan:           Mapped[PlanEnum] = mapped_column(SAEnum(PlanEnum), default=PlanEnum.demo)
    demo_expires_at:Mapped[datetime|None] = mapped_column(DateTime(timezone=True))
    is_active:      Mapped[bool]     = mapped_column(Boolean, default=True)
    created_at:     Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at:     Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    users:     Mapped[list[User]]     = relationship(back_populates="tenant", cascade="all, delete-orphan")
    customers: Mapped[list[Customer]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    invoices:  Mapped[list[Invoice]]  = relationship(back_populates="tenant", cascade="all, delete-orphan", overlaps="customer,invoices")


# ── User ─────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "username"),
        UniqueConstraint("tenant_id", "mobile"),
        Index("ix_users_tenant", "tenant_id"),
        Index("ix_users_mobile", "mobile"),
    )

    id:              Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id:       Mapped[int]            = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    username:        Mapped[str]            = mapped_column(String(50), nullable=False)
    mobile:          Mapped[str]            = mapped_column(String(15), nullable=False)
    email:           Mapped[str|None]       = mapped_column(String(100))
    password_hash:   Mapped[str]            = mapped_column(Text, nullable=False)
    role:            Mapped[RoleEnum]       = mapped_column(SAEnum(RoleEnum), default=RoleEnum.user)
    auth_provider:   Mapped[AuthProvider]   = mapped_column(SAEnum(AuthProvider), default=AuthProvider.password)
    google_id:       Mapped[str|None]       = mapped_column(String(100))
    approval_status: Mapped[ApprovalStatus] = mapped_column(SAEnum(ApprovalStatus), default=ApprovalStatus.approved)
    trial_expires_at:Mapped[datetime|None]  = mapped_column(DateTime(timezone=True))  # Google trial expiry
    company_name:    Mapped[str|None]       = mapped_column(String(200))               # For Google signups
    is_active:       Mapped[bool]           = mapped_column(Boolean, default=True)
    created_at:      Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    tenant: Mapped[Tenant] = relationship(back_populates="users")


# ── Customer (Mobile = PK per tenant) ────────────────────────

class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        Index("ix_customers_tenant", "tenant_id"),
        Index("ix_customers_name",   "tenant_id", "name"),
    )

    mobile:           Mapped[str]      = mapped_column(String(15), primary_key=True)
    tenant_id:        Mapped[int]      = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    name:             Mapped[str]      = mapped_column(String(200), nullable=False)
    pan:              Mapped[str|None] = mapped_column(String(10))       # mandatory if cash_receipts_fy > 2L
    state:            Mapped[str]      = mapped_column(String(50), nullable=False)   # GST state (mandatory)
    gstin:            Mapped[str|None] = mapped_column(String(15))
    address:          Mapped[str|None] = mapped_column(Text)
    email:            Mapped[str|None] = mapped_column(String(100))
    cash_receipts_fy: Mapped[Decimal]  = mapped_column(Numeric(15, 2), default=0)   # rolling FY cash total
    sft_flagged:      Mapped[bool]     = mapped_column(Boolean, default=False)       # cash > 2L in FY
    created_at:       Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at:       Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant:   Mapped[Tenant]         = relationship(back_populates="customers")
    invoices: Mapped[list[Invoice]]  = relationship(
                                         back_populates="customer",
                                         primaryjoin="and_(Customer.tenant_id==Invoice.tenant_id, Customer.mobile==Invoice.customer_mobile)",
                                         foreign_keys="[Invoice.tenant_id, Invoice.customer_mobile]",
                                         overlaps="invoices,tenant")
    payments: Mapped[list[Payment]]  = relationship(
                                         back_populates="customer",
                                         primaryjoin="and_(Customer.tenant_id==Payment.tenant_id, Customer.mobile==Payment.customer_mobile)",
                                         foreign_keys="[Payment.tenant_id, Payment.customer_mobile]")
    advances: Mapped[list[Advance]]  = relationship(
                                         back_populates="customer",
                                         primaryjoin="and_(Customer.tenant_id==Advance.tenant_id, Customer.mobile==Advance.customer_mobile)",
                                         foreign_keys="[Advance.tenant_id, Advance.customer_mobile]")


# ── Invoice ───────────────────────────────────────────────────

class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "invoice_no"),
        Index("ix_invoices_tenant", "tenant_id"),
        Index("ix_invoices_date",   "tenant_id", "invoice_date"),
        Index("ix_invoices_customer","tenant_id", "customer_mobile"),
    )

    id:               Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id:        Mapped[int]           = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    invoice_no:       Mapped[str]           = mapped_column(String(30), nullable=False)
    invoice_date:     Mapped[date]          = mapped_column(Date, nullable=False)
    customer_mobile:  Mapped[str]           = mapped_column(String(15), nullable=False)   # FK = mobile PK
    customer_name:    Mapped[str]           = mapped_column(String(200), nullable=False)
    customer_pan:     Mapped[str|None]      = mapped_column(String(10))
    customer_state:   Mapped[str|None]      = mapped_column(String(50))
    customer_gstin:   Mapped[str|None]      = mapped_column(String(15))
    pay_mode:         Mapped[PayModeEnum]   = mapped_column(SAEnum(PayModeEnum))
    gst_type:         Mapped[GSTTypeEnum]   = mapped_column(SAEnum(GSTTypeEnum), default=GSTTypeEnum.CGST_SGST)
    gst_rate:         Mapped[Decimal]       = mapped_column(Numeric(5, 2), default=3)
    subtotal:         Mapped[Decimal]       = mapped_column(Numeric(15, 2), default=0)
    cgst:             Mapped[Decimal]       = mapped_column(Numeric(15, 2), default=0)
    sgst:             Mapped[Decimal]       = mapped_column(Numeric(15, 2), default=0)
    igst:             Mapped[Decimal]       = mapped_column(Numeric(15, 2), default=0)
    tcs_applicable:   Mapped[bool]          = mapped_column(Boolean, default=False)
    tcs_base:         Mapped[Decimal]       = mapped_column(Numeric(15, 2), default=0)
    tcs_amount:       Mapped[Decimal]       = mapped_column(Numeric(15, 2), default=0)     # 1% of tcs_base
    grand_total:      Mapped[Decimal]       = mapped_column(Numeric(15, 2), default=0)
    amount_paid:      Mapped[Decimal]       = mapped_column(Numeric(15, 2), default=0)
    outstanding:      Mapped[Decimal]       = mapped_column(Numeric(15, 2), default=0)
    status:           Mapped[InvoiceStatus] = mapped_column(SAEnum(InvoiceStatus), default=InvoiceStatus.active)
    payment_status:   Mapped[PaymentStatus] = mapped_column(SAEnum(PaymentStatus), default=PaymentStatus.unpaid)
    notes:            Mapped[str|None]      = mapped_column(Text)
    created_by:       Mapped[int|None]      = mapped_column(ForeignKey("users.id"))
    created_at:       Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at:       Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    # ERP audit fields (added by migration 08)
    version_no:              Mapped[int|None]      = mapped_column(Integer, default=0)
    cancelled_by:            Mapped[int|None]      = mapped_column(ForeignKey("users.id"))
    cancelled_at:            Mapped[datetime|None] = mapped_column(DateTime(timezone=True))
    cancellation_reason:     Mapped[str|None]      = mapped_column(Text)
    reversal_ref_id:         Mapped[int|None]      = mapped_column(Integer)
    original_transaction_id: Mapped[int|None]      = mapped_column(Integer)

    tenant:   Mapped[Tenant]          = relationship(back_populates="invoices", overlaps="customer,invoices")
    customer: Mapped[Customer]        = relationship(back_populates="invoices",
                                          primaryjoin="and_(Invoice.tenant_id==Customer.tenant_id, Invoice.customer_mobile==Customer.mobile)",
                                          foreign_keys="[Invoice.tenant_id, Invoice.customer_mobile]",
                                          overlaps="invoices,tenant")
    items:    Mapped[list[InvoiceItem]] = relationship(back_populates="invoice", cascade="all, delete-orphan")
    payments: Mapped[list[Payment]]   = relationship(back_populates="invoice")

    @property
    def round_off(self) -> Decimal:
        """
        Computed round-off: grand_total minus all known components.
        Works for both old invoices (returns 0) and new ones (returns actual round-off).
        Requires NO extra DB column — derived purely from existing stored values.
        """
        total_gst = (self.cgst or Decimal("0")) + (self.sgst or Decimal("0")) + (self.igst or Decimal("0"))
        tcs       = self.tcs_amount or Decimal("0")
        base      = (self.subtotal or Decimal("0")) + total_gst + tcs
        return (self.grand_total or Decimal("0")) - base


# ── Invoice Item ──────────────────────────────────────────────

class InvoiceItem(Base):
    __tablename__ = "invoice_items"
    __table_args__ = (
        Index("ix_items_invoice", "invoice_id"),
        Index("ix_items_tenant",  "tenant_id"),
    )

    id:             Mapped[int]          = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_id:     Mapped[int]          = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    tenant_id:      Mapped[int]          = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    category:       Mapped[CategoryEnum] = mapped_column(SAEnum(CategoryEnum))
    purity:         Mapped[str|None]     = mapped_column(String(10))      # 24K/22K/18K/14K/std/—
    description:    Mapped[str]          = mapped_column(String(300), nullable=False)
    hsn_code:       Mapped[str]          = mapped_column(String(10), default="7113")
    qty:            Mapped[Decimal]      = mapped_column(Numeric(12, 3), nullable=False)
    unit:           Mapped[UnitEnum]     = mapped_column(SAEnum(UnitEnum))
    rate:           Mapped[Decimal]      = mapped_column(Numeric(15, 2), nullable=False)
    polish_charges: Mapped[Decimal]      = mapped_column(Numeric(15, 2), nullable=True, default=0, server_default="0")
    making_charges: Mapped[Decimal]      = mapped_column(Numeric(15, 2), default=0)
    amount:         Mapped[Decimal]      = mapped_column(Numeric(15, 2), nullable=False)
    created_at:     Mapped[datetime]     = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    version_no:     Mapped[Optional[int]]   = mapped_column(Integer, default=0)
    updated_by:     Mapped[Optional[int]]   = mapped_column(ForeignKey("users.id"))
    updated_at:     Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    invoice: Mapped[Invoice] = relationship(back_populates="items")


# ── Payment ───────────────────────────────────────────────────

class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        Index("ix_payments_tenant",   "tenant_id"),
        Index("ix_payments_invoice",  "invoice_id"),
        Index("ix_payments_customer", "tenant_id", "customer_mobile"),
    )

    id:              Mapped[int]        = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id:       Mapped[int]        = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    invoice_id:      Mapped[int|None]   = mapped_column(ForeignKey("invoices.id"))
    customer_mobile: Mapped[str]        = mapped_column(String(15), nullable=False)
    amount:          Mapped[Decimal]    = mapped_column(Numeric(15, 2), nullable=False)
    payment_date:    Mapped[date]       = mapped_column(Date, nullable=False)
    pay_mode:        Mapped[PayModeEnum]= mapped_column(SAEnum(PayModeEnum))
    reference_no:    Mapped[str|None]   = mapped_column(String(100))
    notes:           Mapped[str|None]   = mapped_column(Text)
    created_by:      Mapped[int|None]   = mapped_column(ForeignKey("users.id"))
    created_at:      Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    invoice:  Mapped[Invoice|None] = relationship(back_populates="payments")
    customer: Mapped[Customer]     = relationship(back_populates="payments",
                                       primaryjoin="and_(Payment.tenant_id==Customer.tenant_id, Payment.customer_mobile==Customer.mobile)",
                                       foreign_keys="[Payment.tenant_id, Payment.customer_mobile]")


# ── Cash Register ─────────────────────────────────────────────

class CashEntry(Base):
    __tablename__ = "cash_register"
    __table_args__ = (
        Index("ix_cash_tenant", "tenant_id"),
        Index("ix_cash_date",   "tenant_id", "entry_date"),
    )

    id:              Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id:       Mapped[int]           = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    entry_date:      Mapped[date]          = mapped_column(Date, nullable=False)
    entry_type:      Mapped[CashEntryType] = mapped_column(SAEnum(CashEntryType))
    amount:          Mapped[Decimal]       = mapped_column(Numeric(15, 2), nullable=False)
    description:     Mapped[str]           = mapped_column(Text, nullable=False)
    invoice_id:      Mapped[int|None]      = mapped_column(ForeignKey("invoices.id"))
    bank_reference:  Mapped[str|None]      = mapped_column(String(100))
    running_balance: Mapped[Decimal|None]  = mapped_column(Numeric(15, 2))
    created_by:      Mapped[int|None]      = mapped_column(ForeignKey("users.id"))
    created_at:      Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


# ── Advance ───────────────────────────────────────────────────

class Advance(Base):
    __tablename__ = "advances"

    id:              Mapped[int]        = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id:       Mapped[int]        = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    customer_mobile: Mapped[str]        = mapped_column(String(15), nullable=False)
    amount:          Mapped[Decimal]    = mapped_column(Numeric(15, 2), nullable=False)
    remaining:       Mapped[Decimal]    = mapped_column(Numeric(15, 2), nullable=False)
    advance_date:    Mapped[date]       = mapped_column(Date, nullable=False)
    pay_mode:        Mapped[PayModeEnum]= mapped_column(SAEnum(PayModeEnum))
    notes:           Mapped[str|None]   = mapped_column(Text)
    created_by:      Mapped[int|None]   = mapped_column(ForeignKey("users.id"))
    created_at:      Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    customer:     Mapped[Customer]               = relationship(back_populates="advances",
                                                     primaryjoin="and_(Advance.tenant_id==Customer.tenant_id, Advance.customer_mobile==Customer.mobile)",
                                                     foreign_keys="[Advance.tenant_id, Advance.customer_mobile]")
    allocations:  Mapped[list[AdvanceAllocation]] = relationship(back_populates="advance", cascade="all, delete-orphan")


class AdvanceAllocation(Base):
    __tablename__ = "advance_allocations"

    id:               Mapped[int]     = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id:        Mapped[int]     = mapped_column(ForeignKey("tenants.id"))
    advance_id:       Mapped[int]     = mapped_column(ForeignKey("advances.id"))
    invoice_id:       Mapped[int]     = mapped_column(ForeignKey("invoices.id"))
    allocated_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    allocated_at:     Mapped[datetime]= mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    created_by:       Mapped[int|None]= mapped_column(ForeignKey("users.id"))

    advance: Mapped[Advance] = relationship(back_populates="allocations")


# ── Stock ─────────────────────────────────────────────────────

class StockItem(Base):
    __tablename__ = "stock_items"
    __table_args__ = (Index("ix_stock_tenant", "tenant_id"),)

    id:           Mapped[int]          = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id:    Mapped[int]          = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    category:     Mapped[CategoryEnum] = mapped_column(SAEnum(CategoryEnum))
    purity:       Mapped[str|None]     = mapped_column(String(10))
    description:  Mapped[str]          = mapped_column(String(300), nullable=False)
    unit:         Mapped[UnitEnum]     = mapped_column(SAEnum(UnitEnum))
    qty_on_hand:  Mapped[Decimal]      = mapped_column(Numeric(15, 3), default=0)
    fifo_enabled: Mapped[bool]         = mapped_column(Boolean, default=True)
    is_active:    Mapped[bool]         = mapped_column(Boolean, default=True)
    created_at:   Mapped[datetime]     = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    transactions: Mapped[list[StockTransaction]] = relationship(back_populates="stock_item", cascade="all, delete-orphan")


class StockTransaction(Base):
    __tablename__ = "stock_transactions"
    __table_args__ = (
        Index("ix_stock_txn_item", "stock_item_id"),
        Index("ix_stock_txn_date", "tenant_id", "txn_date"),
    )

    id:            Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id:     Mapped[int]           = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    stock_item_id: Mapped[int]           = mapped_column(ForeignKey("stock_items.id"))
    txn_type:      Mapped[StockTxnType]  = mapped_column(SAEnum(StockTxnType))
    qty:           Mapped[Decimal]       = mapped_column(Numeric(15, 3), nullable=False)  # +in / -out
    purchase_rate: Mapped[Decimal|None]  = mapped_column(Numeric(15, 2))                  # for FIFO lots
    invoice_id:    Mapped[int|None]      = mapped_column(ForeignKey("invoices.id"))
    reason:        Mapped[str|None]      = mapped_column(Text)
    txn_date:      Mapped[date]          = mapped_column(Date, nullable=False)
    lot_remaining: Mapped[Decimal|None]  = mapped_column(Numeric(15, 3))                  # FIFO lot balance
    created_by:    Mapped[int|None]      = mapped_column(ForeignKey("users.id"))
    created_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    # ERP tracking fields (added migration 08)
    version_no:              Mapped[int|None]      = mapped_column(Integer, default=0)
    original_transaction_id: Mapped[int|None]      = mapped_column(Integer)
    reversal_ref_id:         Mapped[int|None]      = mapped_column(Integer)
    updated_by:              Mapped[int|None]      = mapped_column(ForeignKey("users.id"))
    updated_at:              Mapped[datetime|None] = mapped_column(DateTime(timezone=True))

    stock_item: Mapped[StockItem] = relationship(back_populates="transactions")


# ── Supplier ──────────────────────────────────────────────────

class Supplier(Base):
    __tablename__ = "suppliers"
    __table_args__ = (
        Index("ix_suppliers_tenant", "tenant_id"),
        Index("ix_suppliers_name",   "tenant_id", "name"),
    )

    mobile:     Mapped[str]      = mapped_column(String(15), primary_key=True)
    tenant_id:  Mapped[int]      = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    name:       Mapped[str]      = mapped_column(String(200), nullable=False)
    gstin:      Mapped[str|None] = mapped_column(String(15))
    pan:        Mapped[str|None] = mapped_column(String(10))
    address:    Mapped[str|None] = mapped_column(Text)
    email:      Mapped[str|None] = mapped_column(String(100))
    state:      Mapped[str]      = mapped_column(String(50), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    invoices: Mapped[list["SupplierInvoice"]] = relationship(
        back_populates="supplier",
        primaryjoin="and_(Supplier.tenant_id==SupplierInvoice.tenant_id, Supplier.mobile==SupplierInvoice.supplier_mobile)",
        foreign_keys="[SupplierInvoice.tenant_id, SupplierInvoice.supplier_mobile]",
        cascade="all, delete-orphan", overlaps="supplier")
    payments: Mapped[list["SupplierPayment"]] = relationship(
        back_populates="supplier",
        primaryjoin="and_(Supplier.tenant_id==SupplierPayment.tenant_id, Supplier.mobile==SupplierPayment.supplier_mobile)",
        foreign_keys="[SupplierPayment.tenant_id, SupplierPayment.supplier_mobile]",
        cascade="all, delete-orphan", overlaps="supplier")
    advances: Mapped[list["SupplierAdvance"]] = relationship(
        back_populates="supplier",
        primaryjoin="and_(Supplier.tenant_id==SupplierAdvance.tenant_id, Supplier.mobile==SupplierAdvance.supplier_mobile)",
        foreign_keys="[SupplierAdvance.tenant_id, SupplierAdvance.supplier_mobile]",
        cascade="all, delete-orphan", overlaps="supplier")


class SupplierInvoice(Base):
    __tablename__ = "supplier_invoices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "invoice_no"),
        Index("ix_sup_inv_tenant",   "tenant_id"),
        Index("ix_sup_inv_supplier", "tenant_id", "supplier_mobile"),
    )

    id:              Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id:       Mapped[int]      = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    supplier_mobile: Mapped[str]      = mapped_column(String(15), nullable=False)
    supplier_name:   Mapped[str]      = mapped_column(String(200), nullable=False)
    invoice_no:      Mapped[str]      = mapped_column(String(30), nullable=False)
    invoice_date:    Mapped[date]     = mapped_column(Date, nullable=False)
    gst_type:        Mapped[str]      = mapped_column(String(20), default="CGST+SGST")
    gst_rate:        Mapped[Decimal]  = mapped_column(Numeric(5, 2), default=3)
    subtotal:        Mapped[Decimal]  = mapped_column(Numeric(15, 2), default=0)
    cgst:            Mapped[Decimal]  = mapped_column(Numeric(15, 2), default=0)
    sgst:            Mapped[Decimal]  = mapped_column(Numeric(15, 2), default=0)
    igst:            Mapped[Decimal]  = mapped_column(Numeric(15, 2), default=0)
    grand_total:     Mapped[Decimal]  = mapped_column(Numeric(15, 2), default=0)
    amount_paid:     Mapped[Decimal]  = mapped_column(Numeric(15, 2), default=0)
    outstanding:     Mapped[Decimal]  = mapped_column(Numeric(15, 2), default=0)
    status:          Mapped[str]      = mapped_column(String(20), default="active")
    payment_status:  Mapped[str]      = mapped_column(String(20), default="unpaid")
    notes:           Mapped[str|None] = mapped_column(Text)
    created_by:      Mapped[int|None] = mapped_column(ForeignKey("users.id"))
    created_at:      Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at:      Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    # ERP audit fields (added by migration 08)
    version_no:              Mapped[int|None]      = mapped_column(Integer, default=0)
    cancelled_by:            Mapped[int|None]      = mapped_column(ForeignKey("users.id"))
    cancelled_at:            Mapped[datetime|None] = mapped_column(DateTime(timezone=True))
    cancellation_reason:     Mapped[str|None]      = mapped_column(Text)
    reversal_ref_id:         Mapped[int|None]      = mapped_column(Integer)
    original_transaction_id: Mapped[int|None]      = mapped_column(Integer)

    supplier: Mapped["Supplier"] = relationship(
        back_populates="invoices",
        primaryjoin="and_(SupplierInvoice.tenant_id==Supplier.tenant_id, SupplierInvoice.supplier_mobile==Supplier.mobile)",
        foreign_keys="[SupplierInvoice.tenant_id, SupplierInvoice.supplier_mobile]",
        overlaps="invoices,supplier")
    items: Mapped[list["SupplierInvoiceItem"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan")
    invoice_payments: Mapped[list["SupplierPayment"]] = relationship(
        back_populates="linked_invoice",
        foreign_keys="[SupplierPayment.invoice_id]")


class SupplierInvoiceItem(Base):
    __tablename__ = "supplier_invoice_items"
    __table_args__ = (Index("ix_sup_item_invoice", "invoice_id"),)

    id:             Mapped[int]          = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_id:     Mapped[int]          = mapped_column(ForeignKey("supplier_invoices.id", ondelete="CASCADE"))
    tenant_id:      Mapped[int]          = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    category:       Mapped[CategoryEnum] = mapped_column(SAEnum(CategoryEnum))
    purity:         Mapped[str|None]     = mapped_column(String(10))
    description:    Mapped[str]          = mapped_column(String(300), nullable=False)
    hsn_code:       Mapped[str]          = mapped_column(String(10), default="7113")
    qty:            Mapped[Decimal]      = mapped_column(Numeric(12, 3), nullable=False)
    unit:           Mapped[UnitEnum]     = mapped_column(SAEnum(UnitEnum))
    rate:           Mapped[Decimal]      = mapped_column(Numeric(15, 2), nullable=False)
    making_charges: Mapped[Decimal]      = mapped_column(Numeric(15, 2), default=0)
    amount:         Mapped[Decimal]      = mapped_column(Numeric(15, 2), nullable=False)
    created_at:     Mapped[datetime]     = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    # ERP tracking
    purchase_txn_id: Mapped[int|None]   = mapped_column(ForeignKey("stock_transactions.id"))
    version_no:      Mapped[int|None]   = mapped_column(Integer, default=0)
    updated_by:      Mapped[int|None]   = mapped_column(ForeignKey("users.id"))
    updated_at:      Mapped[datetime|None] = mapped_column(DateTime(timezone=True))


class SupplierPayment(Base):
    __tablename__ = "supplier_payments"
    __table_args__ = (
        Index("ix_sup_pay_tenant",   "tenant_id"),
        Index("ix_sup_pay_supplier", "tenant_id", "supplier_mobile"),
    )

    id:              Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id:       Mapped[int]      = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    supplier_mobile: Mapped[str]      = mapped_column(String(15), nullable=False)
    invoice_id:      Mapped[int|None] = mapped_column(ForeignKey("supplier_invoices.id"), nullable=True)
    amount:          Mapped[Decimal]  = mapped_column(Numeric(15, 2), nullable=False)
    payment_date:    Mapped[date]     = mapped_column(Date, nullable=False)
    pay_mode:        Mapped[str]      = mapped_column(String(20), default="Cash")
    reference_no:    Mapped[str|None] = mapped_column(String(100))
    notes:           Mapped[str|None] = mapped_column(Text)
    created_by:      Mapped[int|None] = mapped_column(ForeignKey("users.id"))
    created_at:      Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    supplier: Mapped["Supplier"] = relationship(
        back_populates="payments",
        primaryjoin="and_(SupplierPayment.tenant_id==Supplier.tenant_id, SupplierPayment.supplier_mobile==Supplier.mobile)",
        foreign_keys="[SupplierPayment.tenant_id, SupplierPayment.supplier_mobile]",
        overlaps="payments,supplier")
    linked_invoice: Mapped["SupplierInvoice"] = relationship(
        back_populates="invoice_payments",
        foreign_keys="[SupplierPayment.invoice_id]")


class SupplierAdvance(Base):
    __tablename__ = "supplier_advances"
    __table_args__ = (Index("ix_sup_adv_tenant", "tenant_id"),)

    id:              Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id:       Mapped[int]      = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    supplier_mobile: Mapped[str]      = mapped_column(String(15), nullable=False)
    amount:          Mapped[Decimal]  = mapped_column(Numeric(15, 2), nullable=False)
    remaining:       Mapped[Decimal]  = mapped_column(Numeric(15, 2), nullable=False)
    advance_date:    Mapped[date]     = mapped_column(Date, nullable=False)
    pay_mode:        Mapped[str]      = mapped_column(String(20), default="Cash")
    notes:           Mapped[str|None] = mapped_column(Text)
    created_by:      Mapped[int|None] = mapped_column(ForeignKey("users.id"))
    created_at:      Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    supplier: Mapped["Supplier"] = relationship(
        back_populates="advances",
        primaryjoin="and_(SupplierAdvance.tenant_id==Supplier.tenant_id, SupplierAdvance.supplier_mobile==Supplier.mobile)",
        foreign_keys="[SupplierAdvance.tenant_id, SupplierAdvance.supplier_mobile]",
        overlaps="advances,supplier")
