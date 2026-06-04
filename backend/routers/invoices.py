# routers/invoices.py — ERP Edition
# ─────────────────────────────────────────────────────────────────────────────
# ERP Accounting Rules enforced here:
#   1. Posted invoice is PERMANENT — never deleted or directly modified
#   2. Cancellation: status → 'cancelled' + reversal entry + FIFO restore from history
#   3. Amendment: version snapshot → incremental FIFO adjust (delta only) → adjustment entry
#   4. FIFO: every sale stores consumption history per lot; restore uses exact history
# ─────────────────────────────────────────────────────────────────────────────

import re
from decimal import Decimal
from datetime import date, datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Body
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models import (
    Invoice, InvoiceItem, Customer, StockItem, StockTransaction,
    InvoiceStatus, PaymentStatus, StockTxnType, CategoryEnum,
)
from models.erp_models import (
    InvoiceVersion, TransactionAuditLog, FIFOConsumptionHistory,
    AmendmentType, AuditEventType,
)
from utils.auth import get_current_user_payload
from utils.business import (
    calculate_gst, generate_invoice_no,
    is_sft_flagged, pan_is_mandatory,
)
from utils.erp_accounting import (
    fifo_consume_for_sale, fifo_restore_from_history, fifo_adjust_incremental,
    create_sales_reversal, save_invoice_version, audit_log,
)

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────

class InvoiceItemIn(BaseModel):
    category:       str
    purity:         Optional[str] = None
    description:    str
    hsn_code:       str = "7113"
    qty:            Decimal
    unit:           str
    rate:           Decimal
    polish_charges: Decimal = Decimal("0")
    making_charges: Decimal = Decimal("0")

class InvoiceCreate(BaseModel):
    invoice_date:    date
    customer_mobile: str = Field(..., pattern=r"^\d{10}$")
    customer_name:   str
    customer_pan:    Optional[str] = None
    customer_state:  str = "Delhi"
    customer_gstin:  Optional[str] = None
    pay_mode:        str
    gst_type:        str = "CGST+SGST"
    gst_rate:        Decimal = Decimal("3")
    round_off:       Decimal = Decimal("0")
    items:           list[InvoiceItemIn]
    notes:           Optional[str] = None

class InvoiceAmendNonFinancial(BaseModel):
    invoice_date:     Optional[date]   = None
    customer_pan:     Optional[str]    = None
    customer_gstin:   Optional[str]    = None
    customer_state:   Optional[str]    = None
    pay_mode:         Optional[str]    = None
    notes:            Optional[str]    = None
    amendment_reason: Optional[str]   = None

class InvoiceAmendFinancial(BaseModel):
    invoice_date:     Optional[date]         = None
    customer_pan:     Optional[str]          = None
    customer_gstin:   Optional[str]          = None
    customer_state:   Optional[str]          = None
    pay_mode:         Optional[str]          = None
    gst_type:         Optional[str]          = None
    gst_rate:         Optional[Decimal]      = None
    round_off:        Optional[Decimal]      = None
    notes:            Optional[str]          = None
    items:            list[InvoiceItemIn]
    amendment_reason: Optional[str]          = None

class CancelRequest(BaseModel):
    reason: str = "Cancelled by user"

class InvoiceOut(BaseModel):
    id:              int
    invoice_no:      str
    invoice_date:    date
    customer_mobile: str
    customer_name:   str
    customer_pan:    Optional[str]
    customer_state:  Optional[str]
    customer_gstin:  Optional[str]
    pay_mode:        str
    subtotal:        Decimal
    cgst:            Decimal
    sgst:            Decimal
    igst:            Decimal
    tcs_applicable:  bool
    tcs_amount:      Decimal
    round_off:       Optional[Decimal] = Decimal("0")
    grand_total:     Decimal
    outstanding:     Decimal
    payment_status:  str
    status:          str
    version_no:      Optional[int] = 0

    class Config:
        from_attributes = True


# ── Helpers ───────────────────────────────────────────────────

PAN_THRESHOLD = Decimal("200000")


async def _upsert_customer(db, tenant_id, mobile, name, state, pan, gstin):
    customer = await db.get(Customer, (mobile, tenant_id))
    created = False
    if not customer:
        customer = Customer(
            mobile=mobile, tenant_id=tenant_id, name=name,
            state=state, pan=pan or None, gstin=gstin or None,
            cash_receipts_fy=Decimal("0"), sft_flagged=False,
        )
        db.add(customer)
        created = True
    else:
        if pan and not customer.pan: customer.pan = pan
        if gstin and not customer.gstin: customer.gstin = gstin
        customer.name = name
    return customer, created


async def _find_stock(db, tenant_id, category, purity):
    from sqlalchemy import case as sa_case, or_
    filters = [StockItem.tenant_id == tenant_id, StockItem.category == category, StockItem.is_active == True]
    if purity:
        filters.append(or_(StockItem.purity == purity, StockItem.purity.is_(None)))
    stmt = (
        select(StockItem).where(*filters)
        .order_by(sa_case((StockItem.purity == purity, 0), else_=1) if purity else StockItem.id)
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _check_stock_availability(db, tenant_id, items):
    for item in items:
        cat_val  = getattr(item.category, "value", str(item.category))
        unit_val = getattr(item.unit, "value", str(item.unit))
        if cat_val == "Polish Charges":
            continue
        stock = await _find_stock(db, tenant_id, item.category, item.purity)
        if not stock:
            raise HTTPException(422, f"No stock item found for {cat_val}{' / ' + item.purity if item.purity else ''}. Add to Stock Master first.")
        if stock.qty_on_hand < item.qty:
            raise HTTPException(422, f"Insufficient stock for {cat_val}: available {float(stock.qty_on_hand):.3f} {unit_val}, requested {float(item.qty):.3f} {unit_val}.")


# ── Create Invoice ────────────────────────────────────────────

@router.post("/", status_code=201)
async def create_invoice(
    body:    InvoiceCreate,
    payload: dict          = Depends(get_current_user_payload),
    db:      AsyncSession  = Depends(get_db),
):
    """Create a new posted invoice with ERP FIFO tracking."""
    tenant_id = payload["tenant_id"]
    user_id   = int(payload["sub"])

    if not body.items:
        raise HTTPException(400, "Invoice must have at least one item.")
    if body.customer_pan and not re.match(r"^[A-Z]{5}[0-9]{4}[A-Z]$", body.customer_pan):
        raise HTTPException(422, "PAN format invalid. Expected: ABCDE1234F")

    subtotal  = Decimal("0")
    item_rows = []
    for item in body.items:
        amount = (item.qty * item.rate + item.polish_charges * item.rate + item.making_charges).quantize(Decimal("0.01"))
        subtotal += amount
        item_rows.append(InvoiceItem(
            tenant_id=tenant_id, category=item.category, purity=item.purity,
            description=item.description, hsn_code=item.hsn_code,
            qty=item.qty, unit=item.unit, rate=item.rate,
            polish_charges=item.polish_charges, making_charges=item.making_charges,
            amount=amount, version_no=0,
        ))

    gst         = calculate_gst(subtotal, body.gst_rate, body.gst_type)
    round_off   = body.round_off.quantize(Decimal("0.01"))
    grand_total = subtotal + gst["total_gst"] + round_off
    sec_269st   = body.pay_mode == "Cash" and grand_total >= Decimal("200000")

    if grand_total > PAN_THRESHOLD and not body.customer_pan:
        raise HTTPException(422, f"PAN mandatory — invoice ₹{grand_total:,.0f} exceeds ₹2,00,000.")

    existing_cust = (await db.execute(
        select(Customer).where(Customer.tenant_id == tenant_id, Customer.mobile == body.customer_mobile)
    )).scalar_one_or_none()
    if existing_cust and pan_is_mandatory(existing_cust.cash_receipts_fy) and not body.customer_pan:
        raise HTTPException(422, "PAN mandatory — customer FY cash receipts exceed ₹2,00,000.")

    seq        = ((await db.execute(select(func.count()).where(Invoice.tenant_id == tenant_id))).scalar() or 0) + 1
    invoice_no = generate_invoice_no(tenant_id, seq)

    await _check_stock_availability(db, tenant_id, item_rows)

    invoice = Invoice(
        tenant_id=tenant_id, invoice_no=invoice_no, invoice_date=body.invoice_date,
        customer_mobile=body.customer_mobile, customer_name=body.customer_name,
        customer_pan=body.customer_pan, customer_state=body.customer_state,
        customer_gstin=body.customer_gstin, pay_mode=body.pay_mode,
        gst_type=body.gst_type, gst_rate=body.gst_rate,
        subtotal=subtotal, cgst=gst["cgst"], sgst=gst["sgst"], igst=gst["igst"],
        tcs_applicable=False, tcs_base=Decimal("0"), tcs_amount=Decimal("0"),
        round_off=round_off, grand_total=grand_total, outstanding=grand_total,
        status=InvoiceStatus.active, payment_status=PaymentStatus.unpaid,
        notes=body.notes, created_by=user_id, version_no=0,
    )
    db.add(invoice)
    await db.flush()

    for item in item_rows:
        item.invoice_id = invoice.id
        db.add(item)
    await db.flush()

    _, customer_created = await _upsert_customer(
        db, tenant_id, body.customer_mobile, body.customer_name,
        body.customer_state, body.customer_pan, body.customer_gstin,
    )

    # ERP FIFO — consume lots and record history
    for item in item_rows:
        if getattr(item.category, "value", str(item.category)) == "Polish Charges":
            continue
        stock = await _find_stock(db, tenant_id, item.category, item.purity)
        if not stock:
            continue
        await fifo_consume_for_sale(
            db, tenant_id, user_id, invoice.id, item.id, stock,
            item.qty, body.invoice_date, amendment_version=0,
        )

    await audit_log(
        db, tenant_id, AuditEventType.invoice_created,
        f"Invoice {invoice_no} posted — {body.customer_name} ₹{grand_total}",
        invoice_id=invoice.id, debit_amount=grand_total,
        ledger_account="Customer Ledger", created_by=user_id,
        metadata={"invoice_no": invoice_no, "subtotal": float(subtotal)},
    )

    await db.commit()
    await db.refresh(invoice)
    return {
        "id": invoice.id, "invoice_no": invoice.invoice_no,
        "invoice_date": invoice.invoice_date.isoformat(),
        "customer_mobile": invoice.customer_mobile, "customer_name": invoice.customer_name,
        "customer_pan": invoice.customer_pan, "pay_mode": invoice.pay_mode.value,
        "subtotal": float(invoice.subtotal), "cgst": float(invoice.cgst),
        "sgst": float(invoice.sgst), "igst": float(invoice.igst),
        "tcs_applicable": False, "tcs_amount": 0.0,
        "round_off": float(invoice.round_off or 0),
        "sec_269st_violation": sec_269st,
        "grand_total": float(invoice.grand_total), "outstanding": float(invoice.outstanding),
        "payment_status": invoice.payment_status.value, "status": invoice.status.value,
        "version_no": invoice.version_no, "customer_created": customer_created,
    }


# ── List Invoices ─────────────────────────────────────────────

@router.get("/", response_model=list[InvoiceOut])
async def list_invoices(
    from_date: Optional[date] = Query(None), to_date: Optional[date] = Query(None),
    mobile: Optional[str] = Query(None), status: Optional[str] = Query(None),
    include_cancelled: bool = Query(False),
    q: Optional[str] = Query(None),       # full-text search: invoice_no / customer_name / mobile
    limit: Optional[int] = Query(None),   # for search modals
    payload: dict = Depends(get_current_user_payload), db: AsyncSession = Depends(get_db),
):
    tenant_id = payload["tenant_id"]
    stmt = select(Invoice).where(Invoice.tenant_id == tenant_id)
    if not include_cancelled: stmt = stmt.where(Invoice.status != InvoiceStatus.cancelled)
    stmt = stmt.order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
    if from_date: stmt = stmt.where(Invoice.invoice_date >= from_date)
    if to_date:   stmt = stmt.where(Invoice.invoice_date <= to_date)
    if mobile:    stmt = stmt.where(Invoice.customer_mobile == mobile)
    if status:    stmt = stmt.where(Invoice.payment_status == status)
    if q:
        stmt = stmt.where(
            Invoice.invoice_no.ilike(f"%{q}%") |
            Invoice.customer_name.ilike(f"%{q}%") |
            Invoice.customer_mobile.contains(q)
        )
    if limit:     stmt = stmt.limit(limit)
    return (await db.execute(stmt)).scalars().all()


# ── Get Single Invoice ────────────────────────────────────────

@router.get("/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(invoice_id: int, payload: dict = Depends(get_current_user_payload), db: AsyncSession = Depends(get_db)):
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(404, "Invoice not found")
    return invoice


# ── Get Invoice Items ─────────────────────────────────────────

@router.get("/{invoice_id}/items")
async def get_invoice_items(invoice_id: int, payload: dict = Depends(get_current_user_payload), db: AsyncSession = Depends(get_db)):
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(404, "Invoice not found")
    items = (await db.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id))).scalars().all()
    return {
        "invoice_id": invoice_id, "invoice_no": invoice.invoice_no, "version_no": invoice.version_no or 0,
        "items": [{"id": i.id, "category": i.category.value, "purity": i.purity or "",
                   "description": i.description, "hsn_code": i.hsn_code,
                   "qty": float(i.qty), "unit": i.unit.value, "rate": float(i.rate),
                   "polish_charges": float(i.polish_charges or 0), "making_charges": float(i.making_charges),
                   "amount": float(i.amount), "version_no": i.version_no or 0} for i in items],
    }


# ── Amendment History ─────────────────────────────────────────

@router.get("/{invoice_id}/history")
async def get_invoice_history(invoice_id: int, payload: dict = Depends(get_current_user_payload), db: AsyncSession = Depends(get_db)):
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(404, "Invoice not found")
    versions = (await db.execute(
        select(InvoiceVersion).where(InvoiceVersion.invoice_id == invoice_id).order_by(InvoiceVersion.version_no)
    )).scalars().all()
    return {
        "invoice_id": invoice_id, "invoice_no": invoice.invoice_no,
        "current_version": invoice.version_no or 0, "status": invoice.status.value,
        "cancelled_at": invoice.cancelled_at.isoformat() if invoice.cancelled_at else None,
        "cancellation_reason": invoice.cancellation_reason,
        "versions": [{"version_no": v.version_no, "amendment_type": v.amendment_type.value,
                      "amendment_reason": v.amendment_reason, "amended_at": v.amended_at.isoformat(),
                      "snapshot_subtotal": float(v.snapshot_subtotal or 0),
                      "snapshot_grand_total": float(v.snapshot_grand_total or 0),
                      "adjustment_grand_total": float(v.adjustment_grand_total or 0),
                      "snapshot_items": v.snapshot_items} for v in versions],
    }


# ── Cancel Invoice (ERP Permanent + Reversal) ─────────────────

@router.put("/{invoice_id}/cancel")
async def cancel_invoice(
    invoice_id: int,
    body: CancelRequest = Body(default=CancelRequest()),
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    """
    ERP-grade cancellation — original invoice NEVER deleted.
    Creates reversal entry and restores exact FIFO lots.
    """
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(404, "Invoice not found")
    if invoice.status == InvoiceStatus.cancelled:
        raise HTTPException(400, "Invoice already cancelled")

    user_id = int(payload["sub"])
    reversal = await create_sales_reversal(
        db, payload["tenant_id"], user_id, invoice, body.reason, date.today(),
    )
    await db.commit()
    return {
        "message": f"Invoice {invoice.invoice_no} cancelled",
        "invoice_no": invoice.invoice_no,
        "credit_note_no": f"CN-{invoice.invoice_no}",
        "reversal_id": reversal.id,
        "cancelled_at": invoice.cancelled_at.isoformat(),
        "cancelled_by": user_id,
        "reason": body.reason,
        "amounts_reversed": {
            "subtotal": float(reversal.subtotal_reversed),
            "cgst": float(reversal.cgst_reversed),
            "sgst": float(reversal.sgst_reversed),
            "igst": float(reversal.igst_reversed),
            "total": float(reversal.total_reversed),
        },
    }


# ── Non-Financial Amendment ───────────────────────────────────

@router.put("/{invoice_id}/amend")
async def amend_invoice_non_financial(
    invoice_id: int, body: InvoiceAmendNonFinancial,
    payload: dict = Depends(get_current_user_payload), db: AsyncSession = Depends(get_db),
):
    """Non-financial amendment: address/GSTIN/notes. Saves version snapshot, no FIFO changes."""
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(404, "Invoice not found")
    if invoice.status == InvoiceStatus.cancelled:
        raise HTTPException(400, "Cannot amend a cancelled invoice")

    user_id = int(payload["sub"])
    current_items = (await db.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id))).scalars().all()

    await save_invoice_version(db, payload["tenant_id"], invoice, current_items,
        AmendmentType.non_financial, body.amendment_reason, user_id)

    if body.invoice_date   is not None: invoice.invoice_date   = body.invoice_date
    if body.customer_state is not None: invoice.customer_state = body.customer_state
    if body.customer_gstin is not None: invoice.customer_gstin = body.customer_gstin or None
    if body.notes          is not None: invoice.notes          = body.notes
    if body.pay_mode       is not None: invoice.pay_mode       = body.pay_mode
    if body.customer_pan is not None:
        pan = body.customer_pan.upper().strip()
        if pan and not re.match(r"^[A-Z]{5}[0-9]{4}[A-Z]$", pan):
            raise HTTPException(422, "PAN format invalid.")
        invoice.customer_pan = pan or None
        cust = await db.get(Customer, (invoice.customer_mobile, payload["tenant_id"]))
        if cust and pan: cust.pan = pan

    await audit_log(db, payload["tenant_id"], AuditEventType.invoice_amended,
        f"Non-financial amendment v{invoice.version_no} on {invoice.invoice_no}",
        invoice_id=invoice.id, version_no=invoice.version_no or 0, created_by=user_id,
        metadata={"reason": body.amendment_reason, "type": "non_financial"})

    await db.commit()
    await db.refresh(invoice)
    return {"message": "Invoice amended (non-financial)", "invoice_no": invoice.invoice_no,
            "invoice_id": invoice.id, "version_no": invoice.version_no}


# ── Financial Amendment (Items/Qty/Rate Changed) ──────────────

@router.put("/{invoice_id}/edit")
async def amend_invoice_financial(
    invoice_id: int, body: InvoiceAmendFinancial,
    payload: dict = Depends(get_current_user_payload), db: AsyncSession = Depends(get_db),
):
    """
    ERP Financial Amendment — items/qty/rate changed.
    Only adjusts FIFO delta (difference), never rewrites old consumption history.
    """
    tenant_id = payload["tenant_id"]
    user_id   = int(payload["sub"])

    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != tenant_id:
        raise HTTPException(404, "Invoice not found")
    if invoice.status == InvoiceStatus.cancelled:
        raise HTTPException(400, "Cannot edit a cancelled invoice")
    if not body.items:
        raise HTTPException(400, "Financial amendment requires items list")
    if body.customer_pan and not re.match(r"^[A-Z]{5}[0-9]{4}[A-Z]$", body.customer_pan):
        raise HTTPException(422, "PAN format invalid.")

    old_items = (await db.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id))).scalars().all()

    new_gst_type = body.gst_type or getattr(invoice.gst_type, "value", str(invoice.gst_type))
    new_gst_rate = body.gst_rate if body.gst_rate is not None else invoice.gst_rate
    new_round    = (body.round_off or Decimal("0")).quantize(Decimal("0.01"))

    new_subtotal = Decimal("0")
    new_item_pairs = []
    for it in body.items:
        amount = (it.qty * it.rate + it.polish_charges * it.rate + it.making_charges).quantize(Decimal("0.01"))
        new_subtotal += amount
        new_item_pairs.append((it, amount))

    new_gst      = calculate_gst(new_subtotal, new_gst_rate, new_gst_type)
    new_grand    = new_subtotal + new_gst["total_gst"] + new_round

    if new_grand > PAN_THRESHOLD and not (body.customer_pan or invoice.customer_pan):
        raise HTTPException(422, f"PAN mandatory — amended invoice ₹{new_grand:,.0f} exceeds ₹2,00,000.")

    adj_subtotal    = new_subtotal - invoice.subtotal
    adj_cgst        = new_gst["cgst"] - invoice.cgst
    adj_sgst        = new_gst["sgst"] - invoice.sgst
    adj_igst        = new_gst["igst"] - invoice.igst
    adj_grand_total = new_grand - invoice.grand_total

    # Snapshot before changes
    await save_invoice_version(
        db, tenant_id, invoice, old_items, AmendmentType.financial,
        body.amendment_reason, user_id,
        adj_subtotal, adj_cgst, adj_sgst, adj_igst, adj_grand_total,
    )
    amendment_version = invoice.version_no   # incremented by save_invoice_version
    await db.flush()

    # Build natural key maps for FIFO delta calculation
    def item_key(it) -> str:
        cat = getattr(it, "category", "") 
        cat_val = getattr(cat, "value", str(cat)) if cat else ""
        purity  = (getattr(it, "purity", None) or "")
        desc    = (getattr(it, "description", "") or "")
        return f"{cat_val}|{purity}|{desc}"

    old_by_key = {item_key(i): i for i in old_items}
    new_by_key = {item_key(it): it for it, _ in new_item_pairs}

    # Removed items → restore FIFO
    for key, old_item in old_by_key.items():
        if getattr(old_item.category, "value", str(old_item.category)) == "Polish Charges":
            continue
        if key not in new_by_key:
            stock = await _find_stock(db, tenant_id, old_item.category, old_item.purity)
            if stock:
                await fifo_adjust_incremental(
                    db, tenant_id, user_id, invoice_id, old_item.id, stock,
                    old_qty=old_item.qty, new_qty=Decimal("0"),
                    invoice_date=invoice.invoice_date, amendment_version=amendment_version,
                )

    # Added or changed items
    for key, new_item in new_by_key.items():
        cat_str = new_item.category if isinstance(new_item.category, str) else new_item.category.value
        if cat_str == "Polish Charges":
            continue
        old_item = old_by_key.get(key)
        if old_item is None:
            # New item — need fresh FIFO
            cat_enum = CategoryEnum(cat_str) if isinstance(cat_str, str) else new_item.category
            stock = await _find_stock(db, tenant_id, cat_enum, new_item.purity)
            if not stock or stock.qty_on_hand < new_item.qty:
                raise HTTPException(422, f"Insufficient stock for new item {cat_str} {new_item.purity or ''}")
            placeholder = InvoiceItem(
                tenant_id=tenant_id, invoice_id=invoice_id,
                category=new_item.category, purity=new_item.purity,
                description=new_item.description, hsn_code=new_item.hsn_code,
                qty=new_item.qty, unit=new_item.unit, rate=new_item.rate,
                polish_charges=new_item.polish_charges, making_charges=new_item.making_charges,
                amount=(new_item.qty * new_item.rate + new_item.polish_charges * new_item.rate + new_item.making_charges).quantize(Decimal("0.01")),
                version_no=amendment_version,
            )
            db.add(placeholder)
            await db.flush()
            await fifo_consume_for_sale(
                db, tenant_id, user_id, invoice_id, placeholder.id,
                stock, new_item.qty, invoice.invoice_date, amendment_version,
            )
        else:
            if new_item.qty != old_item.qty:
                stock = await _find_stock(db, tenant_id, old_item.category, old_item.purity)
                if stock:
                    await fifo_adjust_incremental(
                        db, tenant_id, user_id, invoice_id, old_item.id, stock,
                        old_qty=old_item.qty, new_qty=new_item.qty,
                        invoice_date=invoice.invoice_date, amendment_version=amendment_version,
                    )

    # Replace InvoiceItem rows
    for old_item in old_items:
        await db.delete(old_item)
    await db.flush()

    for it, amount in new_item_pairs:
        db.add(InvoiceItem(
            tenant_id=tenant_id, invoice_id=invoice_id,
            category=it.category, purity=it.purity, description=it.description,
            hsn_code=it.hsn_code, qty=it.qty, unit=it.unit, rate=it.rate,
            polish_charges=it.polish_charges, making_charges=it.making_charges,
            amount=amount, version_no=amendment_version,
        ))

    # Update header and financials
    if body.invoice_date   is not None: invoice.invoice_date   = body.invoice_date
    if body.customer_pan   is not None: invoice.customer_pan   = body.customer_pan
    if body.customer_state is not None: invoice.customer_state = body.customer_state
    if body.customer_gstin is not None: invoice.customer_gstin = body.customer_gstin
    if body.pay_mode       is not None: invoice.pay_mode       = body.pay_mode
    if body.gst_type       is not None: invoice.gst_type       = body.gst_type
    if body.notes          is not None: invoice.notes          = body.notes

    old_paid = invoice.amount_paid
    invoice.gst_rate    = new_gst_rate
    invoice.subtotal    = new_subtotal
    invoice.cgst        = new_gst["cgst"]
    invoice.sgst        = new_gst["sgst"]
    invoice.igst        = new_gst["igst"]
    invoice.round_off   = new_round
    invoice.grand_total = new_grand
    invoice.outstanding = max(Decimal("0"), new_grand - old_paid)
    invoice.payment_status = (
        PaymentStatus.paid    if invoice.outstanding <= 0 else
        PaymentStatus.partial if old_paid > 0 else
        PaymentStatus.unpaid
    )

    await audit_log(
        db, tenant_id, AuditEventType.invoice_amended,
        f"Financial amendment v{amendment_version} on {invoice.invoice_no} — adj ₹{adj_grand_total:+.2f}",
        invoice_id=invoice.id, debit_amount=max(Decimal("0"), adj_grand_total),
        credit_amount=max(Decimal("0"), -adj_grand_total),
        ledger_account="Customer Ledger", version_no=amendment_version, created_by=user_id,
        metadata={"reason": body.amendment_reason, "adj_subtotal": float(adj_subtotal),
                  "adj_grand_total": float(adj_grand_total)},
    )

    await db.commit()
    await db.refresh(invoice)
    return {
        "id": invoice.id, "invoice_no": invoice.invoice_no,
        "message": f"Invoice updated (financial amendment v{amendment_version})",
        "version_no": invoice.version_no, "grand_total": float(invoice.grand_total),
        "outstanding": float(invoice.outstanding), "payment_status": invoice.payment_status.value,
        "adjustment": {"subtotal": float(adj_subtotal), "cgst": float(adj_cgst),
                       "sgst": float(adj_sgst), "igst": float(adj_igst),
                       "grand_total": float(adj_grand_total)},
    }
