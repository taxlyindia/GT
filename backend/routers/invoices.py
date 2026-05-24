# routers/invoices.py
# Inventory Engine v2 — uses utils/inventory.py for all stock movements.
#
# EDIT  → utils/inventory.edit_sale_release + post_sale  (no register rows during release)
# CANCEL→ utils/inventory.cancel_sale  (exact FIFO reversal, no recomputation)

import re
from decimal import Decimal
from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models import (
    Invoice, InvoiceItem, Customer, StockItem, StockTransaction,
    InvoiceStatus, PaymentStatus, StockTxnType, StockMovementType,
    CategoryEnum, InventoryFifoConsumption,
)
from utils.auth import get_current_user_payload
from utils.business import (
    calculate_gst, generate_invoice_no,
    is_sft_flagged, pan_is_mandatory,
)
from utils.inventory import (
    ItemCtx, post_sale, cancel_sale, edit_sale_release,
    _find_stock as _inv_find_stock,
)

router = APIRouter()

PAN_THRESHOLD = Decimal("200000")


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

class InvoiceAmend(BaseModel):
    invoice_date:   Optional[date] = None
    customer_pan:   Optional[str]  = None
    customer_gstin: Optional[str]  = None
    pay_mode:       Optional[str]  = None
    notes:          Optional[str]  = None
    amendment_note: Optional[str]  = None

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

    class Config:
        from_attributes = True

class InvoiceEditBody(BaseModel):
    invoice_date:    Optional[date]           = None
    customer_mobile: Optional[str]            = None
    customer_name:   Optional[str]            = None
    customer_pan:    Optional[str]            = None
    customer_state:  Optional[str]            = None
    customer_gstin:  Optional[str]            = None
    pay_mode:        Optional[str]            = None
    gst_type:        Optional[str]            = None
    gst_rate:        Optional[float]          = None
    round_off:       Optional[float]          = None
    notes:           Optional[str]            = None
    items:           Optional[list[InvoiceItemIn]] = None


# ── Helpers ───────────────────────────────────────────────────

async def _upsert_customer(db, tenant_id, mobile, name, state, pan, gstin):
    customer = await db.get(Customer, (mobile, tenant_id))
    created  = False
    if not customer:
        customer = Customer(
            mobile=mobile, tenant_id=tenant_id, name=name, state=state,
            pan=pan or None, gstin=gstin or None,
            cash_receipts_fy=Decimal("0"), sft_flagged=False,
        )
        db.add(customer)
        created = True
    else:
        if pan and not customer.pan:   customer.pan   = pan
        if gstin and not customer.gstin: customer.gstin = gstin
        customer.name = name
    return customer, created


async def _check_stock_availability(db, tenant_id, items):
    for item in items:
        cat_val  = getattr(item.category, 'value', str(item.category))
        unit_val = getattr(item.unit,     'value', str(item.unit))
        if cat_val == "Polish Charges":
            continue
        stock = await _inv_find_stock(db, tenant_id, item.category, item.purity)
        if not stock:
            raise HTTPException(422, f"No stock found for {cat_val}"
                f"{' / '+item.purity if item.purity else ''}. Add to Stock Master first.")
        if stock.qty_on_hand < item.qty:
            raise HTTPException(422,
                f"Insufficient stock for {cat_val}"
                f"{' / '+item.purity if item.purity else ''}: "
                f"available {float(stock.qty_on_hand):.3f} {unit_val}, "
                f"requested {float(item.qty):.3f} {unit_val}.")


# ── Create Invoice ────────────────────────────────────────────

@router.post("/", status_code=201)
async def create_invoice(
    body:    InvoiceCreate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    tenant_id = payload["tenant_id"]
    uid       = int(payload["sub"])

    if not body.items:
        raise HTTPException(400, "Invoice must have at least one item.")
    if body.customer_pan and not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', body.customer_pan):
        raise HTTPException(422, "PAN format invalid. Expected: ABCDE1234F")

    subtotal, item_rows = Decimal("0"), []
    for item in body.items:
        amt = (item.qty * item.rate + item.polish_charges * item.rate + item.making_charges).quantize(Decimal("0.01"))
        subtotal += amt
        item_rows.append(InvoiceItem(
            tenant_id=tenant_id, category=item.category, purity=item.purity,
            description=item.description, hsn_code=item.hsn_code,
            qty=item.qty, unit=item.unit, rate=item.rate,
            polish_charges=item.polish_charges, making_charges=item.making_charges,
            amount=amt,
        ))

    gst         = calculate_gst(subtotal, body.gst_rate, body.gst_type)
    round_off   = body.round_off.quantize(Decimal("0.01"))
    grand_total = subtotal + gst["total_gst"] + round_off

    if grand_total > PAN_THRESHOLD and not body.customer_pan:
        raise HTTPException(422,
            f"PAN mandatory — invoice value ₹{grand_total:,.0f} exceeds ₹2,00,000.")

    existing = (await db.execute(
        select(Customer).where(Customer.tenant_id == tenant_id, Customer.mobile == body.customer_mobile)
    )).scalar_one_or_none()
    if existing and pan_is_mandatory(existing.cash_receipts_fy) and not body.customer_pan:
        raise HTTPException(422, "PAN mandatory — customer's FY cash receipts exceed ₹2,00,000.")

    seq        = ((await db.execute(select(func.count()).where(Invoice.tenant_id == tenant_id))).scalar() or 0) + 1
    invoice_no = generate_invoice_no(tenant_id, seq)

    await _check_stock_availability(db, tenant_id, item_rows)

    invoice = Invoice(
        tenant_id=tenant_id, invoice_no=invoice_no,
        invoice_date=body.invoice_date,
        customer_mobile=body.customer_mobile, customer_name=body.customer_name,
        customer_pan=body.customer_pan, customer_state=body.customer_state,
        customer_gstin=body.customer_gstin, pay_mode=body.pay_mode,
        gst_type=body.gst_type, gst_rate=body.gst_rate,
        subtotal=subtotal, cgst=gst["cgst"], sgst=gst["sgst"], igst=gst["igst"],
        tcs_applicable=False, tcs_base=Decimal("0"), tcs_amount=Decimal("0"),
        grand_total=grand_total, outstanding=grand_total,
        status=InvoiceStatus.active, payment_status=PaymentStatus.unpaid,
        notes=body.notes, created_by=uid,
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

    # ── Post stock movements via inventory engine ──────────────────────────
    for item in item_rows:
        cat_val = getattr(item.category, 'value', str(item.category))
        if cat_val == "Polish Charges":
            continue
        stock = await _inv_find_stock(db, tenant_id, item.category, item.purity)
        if not stock:
            continue
        ctx = ItemCtx(
            category=item.category, purity=item.purity,
            qty=item.qty, rate=item.rate,
            invoice_id=invoice.id, invoice_item_id=item.id,
            txn_date=body.invoice_date,
            reason=f"Sale — Invoice {invoice_no}",
            created_by=uid,
        )
        await post_sale(db, tenant_id, ctx, stock)

    await db.commit()
    await db.refresh(invoice)

    sec_269st = (body.pay_mode == "Cash" and grand_total >= Decimal("200000"))
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
        "customer_created": customer_created,
    }


# ── List Invoices ─────────────────────────────────────────────

@router.get("/", response_model=list[InvoiceOut])
async def list_invoices(
    from_date:         Optional[date] = Query(None),
    to_date:           Optional[date] = Query(None),
    mobile:            Optional[str]  = Query(None),
    status:            Optional[str]  = Query(None),
    include_cancelled: bool           = Query(False),
    payload:           dict           = Depends(get_current_user_payload),
    db:                AsyncSession   = Depends(get_db),
):
    tenant_id = payload["tenant_id"]
    q = select(Invoice).where(Invoice.tenant_id == tenant_id)
    if not include_cancelled:
        q = q.where(Invoice.status != InvoiceStatus.cancelled)
    q = q.order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
    if from_date: q = q.where(Invoice.invoice_date >= from_date)
    if to_date:   q = q.where(Invoice.invoice_date <= to_date)
    if mobile:    q = q.where(Invoice.customer_mobile == mobile)
    if status:    q = q.where(Invoice.payment_status == status)
    return (await db.execute(q)).scalars().all()


# ── Get Single Invoice ────────────────────────────────────────

@router.get("/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: int,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    inv = await db.get(Invoice, invoice_id)
    if not inv or inv.tenant_id != payload["tenant_id"]:
        raise HTTPException(404, "Invoice not found")
    return inv


# ── Get Invoice Items ─────────────────────────────────────────

@router.get("/{invoice_id}/items")
async def get_invoice_items(
    invoice_id: int,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    inv = await db.get(Invoice, invoice_id)
    if not inv or inv.tenant_id != payload["tenant_id"]:
        raise HTTPException(404, "Invoice not found")
    items = (await db.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id))).scalars().all()
    return {
        "invoice_id": invoice_id, "invoice_no": inv.invoice_no,
        "items": [{
            "id": i.id, "category": i.category.value, "purity": i.purity or "",
            "description": i.description, "hsn_code": i.hsn_code,
            "qty": float(i.qty), "unit": i.unit.value, "rate": float(i.rate),
            "polish_charges": float(i.polish_charges or 0),
            "making_charges": float(i.making_charges), "amount": float(i.amount),
        } for i in items],
    }


# ── Amend Invoice (non-financial fields only) ─────────────────

@router.put("/{invoice_id}/amend")
async def amend_invoice(
    invoice_id: int,
    body:       InvoiceAmend,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    inv = await db.get(Invoice, invoice_id)
    if not inv or inv.tenant_id != payload["tenant_id"]:
        raise HTTPException(404, "Invoice not found")
    if inv.status == InvoiceStatus.cancelled:
        raise HTTPException(400, "Cannot amend a cancelled invoice")

    if body.invoice_date   is not None: inv.invoice_date   = body.invoice_date
    if body.customer_gstin is not None: inv.customer_gstin = body.customer_gstin or None
    if body.pay_mode       is not None: inv.pay_mode       = body.pay_mode
    if body.notes          is not None: inv.notes          = body.notes

    if body.customer_pan is not None:
        pan = body.customer_pan.upper().strip()
        if pan and not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', pan):
            raise HTTPException(422, "PAN format invalid. Expected: ABCDE1234F")
        inv.customer_pan = pan or None
        cust = await db.get(Customer, (inv.customer_mobile, payload["tenant_id"]))
        if cust and pan:
            cust.pan = pan

    await db.commit()
    await db.refresh(inv)
    return {"message": "Invoice amended successfully", "invoice_no": inv.invoice_no, "invoice_id": inv.id}


# ── Cancel Invoice ────────────────────────────────────────────
#
# EXACT REVERSAL via utils/inventory.cancel_sale().
# No recomputation. No current rates. Only direction changes.

@router.put("/{invoice_id}/cancel")
async def cancel_invoice(
    invoice_id: int,
    body:       dict         = {},
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    inv = await db.get(Invoice, invoice_id)
    if not inv or inv.tenant_id != payload["tenant_id"]:
        raise HTTPException(404, "Invoice not found")
    if inv.status == InvoiceStatus.cancelled:
        raise HTTPException(400, "Invoice already cancelled")

    tenant_id  = payload["tenant_id"]
    uid        = int(payload["sub"])
    cancel_dt  = date.today()

    items = (await db.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id))).scalars().all()

    for item in items:
        cat_val = getattr(item.category, 'value', str(item.category))
        if cat_val == "Polish Charges":
            continue
        stock = await _inv_find_stock(db, tenant_id, item.category, item.purity)
        if not stock:
            continue
        await cancel_sale(
            db         = db,
            tenant_id  = tenant_id,
            created_by = uid,
            invoice_id = invoice_id,
            invoice_no = inv.invoice_no,
            item_qty   = item.qty,
            stock      = stock,
            cancel_date= cancel_dt,
        )

    inv.status = InvoiceStatus.cancelled
    await db.commit()

    return {
        "message":        f"Invoice {inv.invoice_no} cancelled",
        "credit_note_no": f"CN-{inv.invoice_no}",
        "invoice_no":     inv.invoice_no,
    }


# ── Full Invoice Edit ─────────────────────────────────────────
#
# EDIT PRINCIPLE: NEVER creates reversal or adjustment rows in the register.
#
# For each old item:
#   1. edit_sale_release() → restores lot_remaining on consumed lots,
#      deletes old sale_out StockTransaction (no visible row)
# For each new item:
#   2. post_sale() → fresh FIFO consumption, new sale_out row + allocations
#
# Net result: stock register shows original sale entry replaced cleanly.

@router.put("/{invoice_id}/edit")
async def edit_invoice(
    invoice_id: int,
    body:       InvoiceEditBody,
    payload:    dict          = Depends(get_current_user_payload),
    db:         AsyncSession  = Depends(get_db),
):
    tenant_id = payload["tenant_id"]
    uid       = int(payload["sub"])
    invoice   = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != tenant_id:
        raise HTTPException(404, "Invoice not found")
    if invoice.status.value == "cancelled":
        raise HTTPException(400, "Cannot edit a cancelled invoice")

    if body.customer_pan and not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', body.customer_pan):
        raise HTTPException(422, "PAN format invalid. Expected: ABCDE1234F")

    # ── Header fields ─────────────────────────────────────────
    if body.invoice_date    is not None: invoice.invoice_date    = body.invoice_date
    if body.customer_mobile is not None: invoice.customer_mobile = body.customer_mobile
    if body.customer_name   is not None: invoice.customer_name   = body.customer_name
    if body.customer_pan    is not None: invoice.customer_pan    = body.customer_pan
    if body.customer_state  is not None: invoice.customer_state  = body.customer_state
    if body.customer_gstin  is not None: invoice.customer_gstin  = body.customer_gstin
    if body.pay_mode        is not None: invoice.pay_mode        = body.pay_mode
    if body.gst_type        is not None: invoice.gst_type        = body.gst_type
    if body.gst_rate        is not None: invoice.gst_rate        = Decimal(str(body.gst_rate))
    if body.notes           is not None: invoice.notes           = body.notes

    if body.round_off is not None and body.items is None:
        new_round   = Decimal(str(body.round_off)).quantize(Decimal("0.01"))
        base_total  = invoice.subtotal + invoice.cgst + invoice.sgst + invoice.igst + invoice.tcs_amount
        invoice.grand_total = base_total + new_round
        invoice.outstanding = max(Decimal("0"), invoice.grand_total - invoice.amount_paid)
        invoice.payment_status = (
            PaymentStatus.paid    if invoice.outstanding <= 0 else
            PaymentStatus.partial if invoice.amount_paid > 0  else
            PaymentStatus.unpaid
        )

    if body.items is not None:
        if not body.items:
            raise HTTPException(400, "Invoice must have at least one item")

        old_items = (await db.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id))).scalars().all()

        # ── Release old FIFO allocations silently ─────────────────────────
        # edit_sale_release restores lot_remaining and deletes the old sale_out
        # row. No visible register entry created.
        for old_item in old_items:
            cat_val = getattr(old_item.category, 'value', str(old_item.category))
            if cat_val == "Polish Charges":
                continue
            stock = await _inv_find_stock(db, tenant_id, old_item.category, old_item.purity)
            if stock:
                await edit_sale_release(db, stock, invoice_id)

        # Delete old invoice items
        for old_item in old_items:
            await db.delete(old_item)
        await db.flush()

        # ── Build new items ───────────────────────────────────────────────
        new_gst_type = body.gst_type or invoice.gst_type.value
        new_gst_rate = Decimal(str(body.gst_rate)) if body.gst_rate else invoice.gst_rate

        subtotal, new_rows = Decimal("0"), []
        for item in body.items:
            amt = (item.qty * item.rate + item.polish_charges * item.rate + item.making_charges).quantize(Decimal("0.01"))
            subtotal += amt
            new_rows.append(InvoiceItem(
                tenant_id=tenant_id, invoice_id=invoice_id,
                category=item.category, purity=item.purity,
                description=item.description, hsn_code=item.hsn_code,
                qty=item.qty, unit=item.unit, rate=item.rate,
                polish_charges=item.polish_charges, making_charges=item.making_charges,
                amount=amt,
            ))

        gst       = calculate_gst(subtotal, new_gst_rate, new_gst_type)
        new_round = Decimal(str(body.round_off)) if body.round_off is not None else (invoice.round_off or Decimal("0"))
        new_grand = subtotal + gst["total_gst"] + new_round

        if new_grand > PAN_THRESHOLD and not (body.customer_pan or invoice.customer_pan):
            raise HTTPException(422, f"PAN mandatory — invoice value ₹{new_grand:,.0f} exceeds ₹2,00,000.")

        invoice.subtotal    = subtotal
        invoice.cgst        = gst["cgst"]; invoice.sgst = gst["sgst"]; invoice.igst = gst["igst"]
        invoice.gst_rate    = new_gst_rate
        invoice.grand_total = new_grand
        invoice.outstanding = max(Decimal("0"), new_grand - invoice.amount_paid)
        invoice.payment_status = (
            PaymentStatus.paid    if invoice.outstanding <= 0 else
            PaymentStatus.partial if invoice.amount_paid > 0  else
            PaymentStatus.unpaid
        )

        for item in new_rows:
            db.add(item)
        await db.flush()

        # ── Post new sale_out transactions via inventory engine ────────────
        for item in new_rows:
            cat_val = getattr(item.category, 'value', str(item.category))
            if cat_val == "Polish Charges":
                continue
            stock = await _inv_find_stock(db, tenant_id, item.category, item.purity)
            if not stock:
                continue
            ctx = ItemCtx(
                category=item.category, purity=item.purity,
                qty=item.qty, rate=item.rate,
                invoice_id=invoice_id, invoice_item_id=item.id,
                txn_date=invoice.invoice_date,
                reason=f"Sale — Invoice {invoice.invoice_no}",
                created_by=uid,
            )
            await post_sale(db, tenant_id, ctx, stock)

    await db.commit()
    await db.refresh(invoice)

    return {
        "id": invoice.id, "invoice_no": invoice.invoice_no,
        "message": "Invoice updated successfully",
        "grand_total": float(invoice.grand_total),
        "outstanding": float(invoice.outstanding),
        "payment_status": invoice.payment_status.value,
    }
