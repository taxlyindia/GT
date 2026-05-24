# routers/invoices.py  — Sale Invoice CRUD with correct FIFO engine
# ══════════════════════════════════════════════════════════════════
#
# FIFO rules implemented (see utils/fifo.py for full documentation):
#
#  CREATE  → fifo_deduct_sale(): consume IN-lots oldest-first, reduce
#             lot_remaining, record sale txn with FIFO avg cost.
#
#  CANCEL  → fifo_reverse_sale(): restore lot_remaining on consumed lots
#             (reverse-FIFO order), add positive adjustment txn at same
#             FIFO avg rate, increment qty_on_hand.
#             Invoice status → cancelled; credit note number returned.
#
#  EDIT    → Step 1: fifo_reverse_sale() on every OLD item (exact mirror)
#             Step 2: delete old InvoiceItem rows + old sale StockTransactions
#             Step 3: fifo_deduct_sale() on every NEW item (fresh FIFO walk)
#             Financial totals fully recomputed; outstanding adjusted.
#
# ══════════════════════════════════════════════════════════════════

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
    InvoiceStatus, PaymentStatus, StockTxnType, CategoryEnum,
)
from utils.auth import get_current_user_payload
from utils.business import (
    calculate_gst, generate_invoice_no,
    is_sft_flagged, pan_is_mandatory,
)
from utils.fifo import fifo_deduct_sale, fifo_cancel_sale, fifo_edit_sale, _find_stock_item
# backwards-compat alias
fifo_reverse_sale = fifo_cancel_sale

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

class InvoiceAmend(BaseModel):
    """Non-financial metadata edit (date, PAN, GSTIN, pay_mode, notes)."""
    invoice_date:    Optional[date]   = None
    customer_pan:    Optional[str]    = None
    customer_gstin:  Optional[str]    = None
    pay_mode:        Optional[str]    = None
    notes:           Optional[str]    = None
    amendment_note:  Optional[str]    = None

class InvoiceEditBody(BaseModel):
    """Full edit — can replace header fields AND line items."""
    invoice_date:    Optional[date]              = None
    customer_mobile: Optional[str]               = None
    customer_name:   Optional[str]               = None
    customer_pan:    Optional[str]               = None
    customer_state:  Optional[str]               = None
    customer_gstin:  Optional[str]               = None
    pay_mode:        Optional[str]               = None
    gst_type:        Optional[str]               = None
    gst_rate:        Optional[float]             = None
    round_off:       Optional[float]             = None
    notes:           Optional[str]               = None
    items:           Optional[list[InvoiceItemIn]] = None

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


# ── Constants ─────────────────────────────────────────────────

PAN_THRESHOLD     = Decimal("200000")
SEC_269ST_THRESH  = Decimal("200000")


# ── Customer upsert helper ────────────────────────────────────

async def _upsert_customer(
    db: AsyncSession,
    tenant_id: int,
    mobile: str,
    name: str,
    state: str,
    pan: Optional[str],
    gstin: Optional[str],
) -> tuple["Customer", bool]:
    customer = await db.get(Customer, (mobile, tenant_id))
    created  = False
    if not customer:
        customer = Customer(
            mobile=mobile, tenant_id=tenant_id,
            name=name, state=state,
            pan=pan or None, gstin=gstin or None,
            cash_receipts_fy=Decimal("0"), sft_flagged=False,
        )
        db.add(customer)
        created = True
    else:
        if pan   and not customer.pan:   customer.pan   = pan
        if gstin and not customer.gstin: customer.gstin = gstin
        customer.name = name
    return customer, created


# ── Stock item finder (sale-side fuzzy: purity=None matches any) ──

async def _find_stock_for_sale(
    db: AsyncSession,
    tenant_id: int,
    category,
    purity: Optional[str],
) -> Optional["StockItem"]:
    """
    For SALES: match exact purity first, then fall back to NULL-purity row.
    """
    from sqlalchemy import case as sa_case, or_
    filters = [
        StockItem.tenant_id == tenant_id,
        StockItem.category  == category,
        StockItem.is_active == True,
    ]
    if purity:
        filters.append(or_(StockItem.purity == purity, StockItem.purity.is_(None)))
    stmt = (
        select(StockItem)
        .where(*filters)
        .order_by(
            sa_case((StockItem.purity == purity, 0), else_=1)
            if purity else StockItem.id
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


# ── Stock availability pre-check ──────────────────────────────

async def _check_stock_availability(
    db: AsyncSession,
    tenant_id: int,
    items: list,
) -> None:
    for item in items:
        cat_val  = item.category.value if hasattr(item.category, "value") else str(item.category)
        unit_val = item.unit.value     if hasattr(item.unit,     "value") else str(item.unit)
        if cat_val == "Polish Charges":
            continue
        qty = item.qty if hasattr(item, "qty") else Decimal(str(item.qty))
        stock = await _find_stock_for_sale(db, tenant_id, item.category, item.purity)
        if not stock:
            raise HTTPException(422,
                f"No stock item found for {cat_val}"
                f"{' / ' + item.purity if item.purity else ''}. "
                "Add it to Stock Master first.")
        if stock.qty_on_hand < qty:
            raise HTTPException(422,
                f"Insufficient stock for {cat_val}"
                f"{' / ' + item.purity if item.purity else ''}: "
                f"available {float(stock.qty_on_hand):.3f} {unit_val}, "
                f"requested {float(qty):.3f} {unit_val}.")


# ── Build InvoiceItem rows from request schema ────────────────

def _build_item_rows(
    tenant_id: int,
    items_in: list[InvoiceItemIn],
    invoice_id: Optional[int] = None,
) -> tuple[list["InvoiceItem"], Decimal]:
    """Return (item_rows, subtotal)."""
    subtotal  = Decimal("0")
    item_rows = []
    for item in items_in:
        amount = (
            item.qty * item.rate
            + item.polish_charges * item.rate
            + item.making_charges
        ).quantize(Decimal("0.01"))
        subtotal += amount
        row = InvoiceItem(
            tenant_id=tenant_id,
            invoice_id=invoice_id,       # may be None until flush
            category=item.category,
            purity=item.purity,
            description=item.description,
            hsn_code=item.hsn_code,
            qty=item.qty,
            unit=item.unit,
            rate=item.rate,
            polish_charges=item.polish_charges,
            making_charges=item.making_charges,
            amount=amount,
        )
        item_rows.append(row)
    return item_rows, subtotal


# ── FIFO deduct all items for a sale invoice ─────────────────

async def _deduct_sale_stock(
    db: AsyncSession,
    tenant_id: int,
    created_by: int,
    invoice_id: int,
    invoice_date: date,
    item_rows: list["InvoiceItem"],
) -> None:
    """
    Run fifo_deduct_sale() for every non-Polish item.
    Stock availability must be pre-checked before calling.
    """
    for item in item_rows:
        cat_val = item.category.value if hasattr(item.category, "value") else str(item.category)
        if cat_val == "Polish Charges":
            continue
        stock = await _find_stock_for_sale(db, tenant_id, item.category, item.purity)
        if not stock:
            continue
        await fifo_deduct_sale(
            db,
            tenant_id=tenant_id,
            created_by=created_by,
            invoice_id=invoice_id,
            invoice_date=invoice_date,
            stock=stock,
            qty=item.qty,
            item_description=item.description,
        )


# ── FIFO reverse all items for a sale invoice ─────────────────

async def _reverse_sale_stock(
    db: AsyncSession,
    tenant_id: int,
    created_by: int,
    invoice_id: int,
    reversal_date: date,
    item_rows: list["InvoiceItem"],
    reason_prefix: str = "Cancelled",
) -> None:
    """
    Run fifo_cancel_sale() for every non-Polish item.
    Restores lot_remaining and increments qty_on_hand (direct reversal).
    """
    for item in item_rows:
        cat_val = item.category.value if hasattr(item.category, "value") else str(item.category)
        if cat_val == "Polish Charges":
            continue
        stock = await _find_stock_for_sale(db, tenant_id, item.category, item.purity)
        if not stock:
            continue
        await fifo_reverse_sale(
            db,
            tenant_id=tenant_id,
            created_by=created_by,
            invoice_id=invoice_id,
            reversal_date=reversal_date,
            stock=stock,
            qty=item.qty,
            reason_prefix=reason_prefix,
        )


# ═══════════════════════════════════════════════════════════════
# CREATE INVOICE
# ═══════════════════════════════════════════════════════════════

@router.post("/", status_code=201)
async def create_invoice(
    body:    InvoiceCreate,
    payload: dict          = Depends(get_current_user_payload),
    db:      AsyncSession  = Depends(get_db),
):
    """
    Create a sale invoice.
    Stock deducted via FIFO (oldest lots consumed first).
    """
    if payload.get("role") == "viewer":
        raise HTTPException(403, "Viewers cannot create invoices.")

    tenant_id = payload["tenant_id"]

    if not body.items:
        raise HTTPException(400, "Invoice must have at least one item.")

    if body.customer_pan and not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', body.customer_pan):
        raise HTTPException(422, "PAN format invalid. Expected: ABCDE1234F")

    item_rows, subtotal = _build_item_rows(tenant_id, body.items)

    gst         = calculate_gst(subtotal, body.gst_rate, body.gst_type)
    round_off   = body.round_off.quantize(Decimal("0.01"))
    grand_total = subtotal + gst["total_gst"] + round_off

    sec_269st_violation = (body.pay_mode == "Cash" and grand_total >= SEC_269ST_THRESH)

    if grand_total > PAN_THRESHOLD and not body.customer_pan:
        raise HTTPException(422,
            f"PAN is mandatory — invoice value ₹{grand_total:,.0f} exceeds ₹2,00,000.")

    # Check existing customer SFT status
    cust_res = await db.execute(
        select(Customer).where(
            Customer.tenant_id == tenant_id,
            Customer.mobile    == body.customer_mobile,
        )
    )
    existing_cust = cust_res.scalar_one_or_none()
    if existing_cust and pan_is_mandatory(existing_cust.cash_receipts_fy) and not body.customer_pan:
        raise HTTPException(422, "PAN is mandatory — customer's cumulative cash receipts this FY exceed ₹2,00,000.")

    # BUG-07 FIX: use MAX(id)+1 with uniqueness loop
    max_id = (await db.execute(
        select(func.max(Invoice.id)).where(Invoice.tenant_id == tenant_id)
    )).scalar() or 0
    seq        = max_id + 1
    invoice_no = generate_invoice_no(tenant_id, seq)
    while (await db.execute(
        select(Invoice.id).where(
            Invoice.tenant_id  == tenant_id,
            Invoice.invoice_no == invoice_no
        ).limit(1)
    )).scalar_one_or_none():
        seq += 1
        invoice_no = generate_invoice_no(tenant_id, seq)

    # Pre-check stock BEFORE any write
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
        notes=body.notes, created_by=int(payload["sub"]),
    )
    db.add(invoice)
    await db.flush()

    for row in item_rows:
        row.invoice_id = invoice.id
        db.add(row)
    await db.flush()

    _, customer_created = await _upsert_customer(
        db, tenant_id, body.customer_mobile, body.customer_name,
        body.customer_state, body.customer_pan, body.customer_gstin,
    )

    # Deduct stock via FIFO
    await _deduct_sale_stock(
        db, tenant_id, int(payload["sub"]),
        invoice.id, body.invoice_date, item_rows,
    )

    await db.commit()
    await db.refresh(invoice)

    return {
        "id":               invoice.id,
        "invoice_no":       invoice.invoice_no,
        "invoice_date":     invoice.invoice_date.isoformat(),
        "customer_mobile":  invoice.customer_mobile,
        "customer_name":    invoice.customer_name,
        "customer_pan":     invoice.customer_pan,
        "pay_mode":         invoice.pay_mode.value,
        "subtotal":         float(invoice.subtotal),
        "cgst":             float(invoice.cgst),
        "sgst":             float(invoice.sgst),
        "igst":             float(invoice.igst),
        "tcs_applicable":   False,
        "tcs_amount":       0.0,
        "round_off":        float(invoice.round_off or 0),
        "sec_269st_violation": sec_269st_violation,
        "grand_total":      float(invoice.grand_total),
        "outstanding":      float(invoice.outstanding),
        "payment_status":   invoice.payment_status.value,
        "status":           invoice.status.value,
        "customer_created": customer_created,
    }


# ═══════════════════════════════════════════════════════════════
# LIST / GET INVOICES
# ═══════════════════════════════════════════════════════════════

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
    result = await db.execute(q)
    return result.scalars().all()


@router.get("/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: int,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(404, "Invoice not found")
    return invoice


@router.get("/{invoice_id}/items")
async def get_invoice_items(
    invoice_id: int,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(404, "Invoice not found")
    result = await db.execute(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)
    )
    items = result.scalars().all()
    return {
        "invoice_id": invoice_id,
        "invoice_no": invoice.invoice_no,
        "items": [
            {
                "id":             item.id,
                "category":       item.category.value,
                "purity":         item.purity or "",
                "description":    item.description,
                "hsn_code":       item.hsn_code,
                "qty":            float(item.qty),
                "unit":           item.unit.value,
                "rate":           float(item.rate),
                "polish_charges": float(item.polish_charges) if item.polish_charges else 0.0,
                "making_charges": float(item.making_charges),
                "amount":         float(item.amount),
            }
            for item in items
        ],
    }


# ═══════════════════════════════════════════════════════════════
# CANCEL INVOICE
# Sale reversal — exact mirror of original FIFO deduction
# ═══════════════════════════════════════════════════════════════

@router.put("/{invoice_id}/cancel")
async def cancel_invoice(
    invoice_id: int,
    body:       dict         = {},
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """
    Cancel a sale invoice.

    FIFO cancellation (direct reversal — no fresh FIFO walk):
    • Finds all non-Polish line items.
    • For each item calls fifo_cancel_sale():
        - Restores lot_remaining on exactly the lots originally consumed,
          using reverse FIFO walk to undo the original deduction precisely.
        - Records one cancellation transaction at the original FIFO avg rate.
        - Increments stock.qty_on_hand.
    • Marks invoice as cancelled and returns a credit note reference.
    """
    if payload.get("role") == "viewer":
        raise HTTPException(403, "Viewers cannot cancel invoices.")

    tenant_id = payload["tenant_id"]
    invoice   = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != tenant_id:
        raise HTTPException(404, "Invoice not found")
    if invoice.status == InvoiceStatus.cancelled:
        raise HTTPException(400, "Invoice is already cancelled")

    # Load line items
    items_res = await db.execute(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)
    )
    items = items_res.scalars().all()

    # FIFO reversal for each item
    await _reverse_sale_stock(
        db, tenant_id, int(payload["sub"]),
        invoice_id, date.today(), items,
        reason_prefix="Cancelled",
    )

    invoice.status = InvoiceStatus.cancelled

    await db.commit()

    credit_note_no = f"CN-{invoice.invoice_no}"
    return {
        "message":        f"Invoice {invoice.invoice_no} cancelled",
        "credit_note_no": credit_note_no,
        "invoice_no":     invoice.invoice_no,
    }


# ═══════════════════════════════════════════════════════════════
# METADATA-ONLY AMEND (no items changed)
# ═══════════════════════════════════════════════════════════════

@router.put("/{invoice_id}/amend")
async def amend_invoice(
    invoice_id: int,
    body:       InvoiceAmend,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """
    Edit non-financial metadata only (date, PAN, GSTIN, pay_mode, notes).
    No stock movements — no FIFO changes.
    """
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(404, "Invoice not found")
    if invoice.status == InvoiceStatus.cancelled:
        raise HTTPException(400, "Cannot amend a cancelled invoice")

    if body.invoice_date   is not None: invoice.invoice_date   = body.invoice_date
    if body.customer_gstin is not None: invoice.customer_gstin = body.customer_gstin or None
    if body.pay_mode       is not None: invoice.pay_mode       = body.pay_mode
    if body.notes          is not None: invoice.notes          = body.notes
    if body.customer_pan is not None:
        pan = body.customer_pan.upper().strip()
        if pan and not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', pan):
            raise HTTPException(422, "PAN format invalid. Expected: ABCDE1234F")
        invoice.customer_pan = pan or None
        cust = await db.get(Customer, (invoice.customer_mobile, payload["tenant_id"]))
        if cust and pan:
            cust.pan = pan

    await db.commit()
    await db.refresh(invoice)
    return {"message": "Invoice amended", "invoice_no": invoice.invoice_no, "invoice_id": invoice.id}


# ═══════════════════════════════════════════════════════════════
# FULL EDIT INVOICE
# Sale edit = FIFO reverse old + FIFO deduct new
# ═══════════════════════════════════════════════════════════════

@router.put("/{invoice_id}/edit")
async def edit_invoice(
    invoice_id: int,
    body:       InvoiceEditBody,
    payload:    dict          = Depends(get_current_user_payload),
    db:         AsyncSession  = Depends(get_db),
):
    """
    Full invoice edit.

    FIFO logic when items are provided:
    ─────────────────────────────────────
    1. REVERSE: call fifo_reverse_sale() for every old item.
       This restores lot_remaining on consumed IN-lots at their original
       quantities and appends a positive adjustment transaction. The FIFO
       order is preserved because we restore newest-consumed lots first.

    2. DELETE: remove old InvoiceItem rows and old sale StockTransactions
       linked to this invoice (they are superseded by the reversal records).

    3. CREATE: call fifo_deduct_sale() for every new item.
       A fresh FIFO walk from the oldest available lot is performed.
       New sale transactions are recorded with the updated FIFO avg cost.

    4. RECALCULATE: subtotal, GST, grand_total, outstanding fully recomputed.

    When items are NOT provided, only header fields are updated (no FIFO impact).
    """
    if payload.get("role") == "viewer":
        raise HTTPException(403, "Viewers cannot edit invoices.")

    tenant_id = payload["tenant_id"]
    invoice   = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != tenant_id:
        raise HTTPException(404, "Invoice not found")
    if (invoice.status.value if hasattr(invoice.status, "value") else invoice.status) == "cancelled":
        raise HTTPException(400, "Cannot edit a cancelled invoice")

    if body.customer_pan and not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', body.customer_pan):
        raise HTTPException(422, "PAN format invalid. Expected: ABCDE1234F")

    # ── Update header fields ──────────────────────────────────
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

    # round_off-only edit when no items changing
    if body.round_off is not None and body.items is None:
        new_round  = Decimal(str(body.round_off)).quantize(Decimal("0.01"))
        base_total = invoice.subtotal + invoice.cgst + invoice.sgst + invoice.igst + invoice.tcs_amount
        invoice.grand_total = base_total + new_round
        invoice.outstanding = max(Decimal("0"), invoice.grand_total - invoice.amount_paid)
        if invoice.outstanding <= 0:
            invoice.payment_status = PaymentStatus.paid
        elif invoice.amount_paid > 0:
            invoice.payment_status = PaymentStatus.partial
        else:
            invoice.payment_status = PaymentStatus.unpaid

    # ── Replace items if provided ─────────────────────────────
    if body.items is not None:
        if not body.items:
            raise HTTPException(400, "Invoice must have at least one item")

        # Load existing items BEFORE any deletion
        old_items_res = await db.execute(
            select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)
        )
        old_items = old_items_res.scalars().all()

        # ── EDIT: Update items in-place, modify FIFO lots directly ──────
        # Rule: Edit must NOT create reversal+new entries.
        # The original sale StockTransaction is updated in-place.
        # FIFO lots are adjusted: old qty returned, new qty consumed.

        new_gst_type = body.gst_type or (
            invoice.gst_type.value if hasattr(invoice.gst_type, "value") else str(invoice.gst_type)
        )
        new_gst_rate = Decimal(str(body.gst_rate)) if body.gst_rate is not None else invoice.gst_rate
        new_round    = (
            Decimal(str(body.round_off)).quantize(Decimal("0.01"))
            if body.round_off is not None
            else (invoice.round_off or Decimal("0"))
        )

        # Build map of old items by (category, purity) for matching
        old_item_map: dict[tuple, InvoiceItem] = {}
        for oi in old_items:
            cat = oi.category.value if hasattr(oi.category, "value") else str(oi.category)
            key = (cat, oi.purity or "")
            old_item_map[key] = oi

        new_rows, subtotal = _build_item_rows(tenant_id, body.items, invoice_id)

        # Pre-check stock availability
        await _check_stock_availability(db, tenant_id, new_rows)

        # Apply FIFO edits in-place for each item
        for new_item in new_rows:
            cat_val = new_item.category.value if hasattr(new_item.category, "value") else str(new_item.category)
            if cat_val == "Polish Charges":
                continue
            key     = (cat_val, new_item.purity or "")
            old_item = old_item_map.get(key)
            old_qty  = old_item.qty if old_item else Decimal("0")

            stock = await _find_stock_for_sale(db, tenant_id, new_item.category, new_item.purity)
            if stock:
                await fifo_edit_sale(
                    db,
                    tenant_id=tenant_id,
                    created_by=int(payload["sub"]),
                    invoice_id=invoice_id,
                    invoice_date=invoice.invoice_date,
                    stock=stock,
                    old_qty=old_qty,
                    new_qty=new_item.qty,
                )

        # Delete old InvoiceItem rows and replace with new ones
        for old_item in old_items:
            await db.delete(old_item)
        await db.flush()
        for row in new_rows:
            db.add(row)
        await db.flush()

        gst       = calculate_gst(subtotal, new_gst_rate, new_gst_type)
        new_grand = subtotal + gst["total_gst"] + new_round

        if new_grand > PAN_THRESHOLD and not (body.customer_pan or invoice.customer_pan):
            raise HTTPException(422,
                f"PAN is mandatory — invoice value ₹{new_grand:,.0f} exceeds ₹2,00,000.")

        # Update invoice financials
        amount_paid         = invoice.amount_paid
        invoice.subtotal    = subtotal
        invoice.cgst        = gst["cgst"]
        invoice.sgst        = gst["sgst"]
        invoice.igst        = gst["igst"]
        invoice.gst_rate    = new_gst_rate
        invoice.grand_total = new_grand
        invoice.outstanding = max(Decimal("0"), new_grand - amount_paid)
        if invoice.outstanding <= 0:
            invoice.payment_status = PaymentStatus.paid
        elif invoice.amount_paid > 0:
            invoice.payment_status = PaymentStatus.partial
        else:
            invoice.payment_status = PaymentStatus.unpaid

    await db.commit()
    await db.refresh(invoice)

    return {
        "id":             invoice.id,
        "invoice_no":     invoice.invoice_no,
        "message":        "Invoice updated successfully",
        "grand_total":    float(invoice.grand_total),
        "outstanding":    float(invoice.outstanding),
        "payment_status": invoice.payment_status.value,
    }
