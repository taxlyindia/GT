# routers/invoices.py
# Changes vs v4 original:
#  Issue 1  — Auto-save customer to master when creating invoice (upsert)
#  Issue 2  — GET /{id}/items endpoint so PDF preview can load line items
#  Issue 3  — PUT /{id}/amend  endpoint for full invoice edit
#  Issue 9  — Deduct stock qty_on_hand when invoice created; restore on cancel
#  P11      — TCS removed; PAN mandatory when invoice value > ₹2,00,000
#  P11      — cancel returns credit_note_no in response

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
    """Fields that can be edited after invoice creation."""
    invoice_date:    Optional[date]   = None
    customer_pan:    Optional[str]    = None
    customer_gstin:  Optional[str]    = None
    pay_mode:        Optional[str]    = None
    notes:           Optional[str]    = None
    amendment_note:  Optional[str]    = None

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


# ── Helpers ───────────────────────────────────────────────────

PAN_THRESHOLD = Decimal("200000")   # ₹2,00,000 — PAN mandatory above this

async def _upsert_customer(
    db: AsyncSession,
    tenant_id: int,
    mobile: str,
    name: str,
    state: str,
    pan: Optional[str],
    gstin: Optional[str],
) -> tuple["Customer", bool]:
    """
    Create customer if not exists, update name/PAN/GSTIN if new info available.
    Returns (customer, created_flag).
    Issue 1 fix.
    """
    customer = await db.get(Customer, (mobile, tenant_id))
    created = False
    if not customer:
        customer = Customer(
            mobile=mobile,
            tenant_id=tenant_id,
            name=name,
            state=state,
            pan=pan or None,
            gstin=gstin or None,
            cash_receipts_fy=Decimal("0"),
            sft_flagged=False,
        )
        db.add(customer)
        created = True
    else:
        # Update PAN / GSTIN if now provided and was missing
        if pan and not customer.pan:
            customer.pan = pan
        if gstin and not customer.gstin:
            customer.gstin = gstin
        # Always keep name in sync
        customer.name = name
    return customer, created


async def _find_stock(db: "AsyncSession", tenant_id: int, category, purity: "str | None"):
    """
    Find best-matching StockItem.
    Priority: exact purity match > NULL-purity catch-all > any purity for category.
    This fixes the 'No stock' false-negative when stock master has NULL purity.
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
            sa_case((StockItem.purity == purity, 0), else_=1) if purity else StockItem.id
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _check_stock_availability(
    db: AsyncSession,
    tenant_id: int,
    items: list["InvoiceItem"],
) -> None:
    """
    Pre-check: raise 422 if any item has insufficient stock in stock master.
    Called BEFORE creating the invoice so nothing is committed on failure.
    Uses _find_stock() so NULL-purity stock master rows match any purity request.
    Polish Charges category is skipped — it is calculation-only, not linked to stock.
    NOTE: items may be InvoiceItem ORM objects whose category/unit are still plain
    strings (not yet coerced to Enum by SQLAlchemy). Use getattr(.value) safely.
    """
    for item in items:
        cat_val  = getattr(item.category, 'value', str(item.category))
        unit_val = getattr(item.unit,     'value', str(item.unit))
        # Polish Charges are calculation-only — no stock deduction or check needed
        if cat_val == "Polish Charges":
            continue
        stock = await _find_stock(db, tenant_id, item.category, item.purity)
        if not stock:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"No stock item found for {cat_val}"
                    f"{' / ' + item.purity if item.purity else ''}. "
                    "Please add the item to Stock Master first."
                ),
            )
        if stock.qty_on_hand < item.qty:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Insufficient stock for {cat_val}"
                    f"{' / ' + item.purity if item.purity else ''}: "
                    f"available {float(stock.qty_on_hand):.3f} {unit_val}, "
                    f"requested {float(item.qty):.3f} {unit_val}."
                ),
            )


async def _deduct_stock(
    db: AsyncSession,
    tenant_id: int,
    created_by: int,
    invoice_id: int,
    invoice_date: date,
    items: list["InvoiceItem"],
) -> None:
    """
    Deduct sold quantities from stock on hand (FIFO basis).
    Stock availability must be pre-checked via _check_stock_availability().
    Also records the FIFO-weighted average purchase_rate on the sale transaction
    so that cancellations can restore stock at the ORIGINAL purchase value.
    """
    for item in items:
        # Polish Charges are calculation-only — no stock deduction
        cat_val = getattr(item.category, 'value', str(item.category))
        if cat_val == "Polish Charges":
            continue

        stock = await _find_stock(db, tenant_id, item.category, item.purity)
        if not stock:
            continue  # pre-check already caught this; defensive skip

        # ── Compute FIFO avg purchase_rate for qty being sold ──────────────
        # Fetch all open purchase/opening batches in FIFO order (oldest first)
        batches_result = await db.execute(
            select(StockTransaction)
            .where(
                StockTransaction.stock_item_id == stock.id,
                StockTransaction.qty > 0,          # IN transactions only
                StockTransaction.txn_type.in_([
                    StockTxnType.purchase,
                    StockTxnType.opening,
                    StockTxnType.adjustment,
                ]),
            )
            .order_by(StockTransaction.txn_date, StockTransaction.id)
        )
        batches = batches_result.scalars().all()

        # Walk FIFO batches to find weighted avg rate for qty sold
        qty_to_consume = item.qty
        weighted_value = Decimal("0")
        for batch in batches:
            if qty_to_consume <= 0:
                break
            available = (batch.lot_remaining
                         if batch.lot_remaining is not None
                         else abs(batch.qty))
            if available <= 0:
                continue
            take = min(available, qty_to_consume)
            rate = batch.purchase_rate or Decimal("0")
            weighted_value += take * rate
            qty_to_consume -= take

        fifo_avg_rate = (
            (weighted_value / item.qty).quantize(Decimal("0.01"))
            if item.qty > 0 and weighted_value > 0
            else Decimal("0")
        )

        # ── Reduce qty_on_hand ─────────────────────────────────────────────
        stock.qty_on_hand = stock.qty_on_hand - item.qty

        # ── Record stock-out transaction with FIFO rate captured ───────────
        db.add(StockTransaction(
            tenant_id=tenant_id,
            stock_item_id=stock.id,
            txn_type=StockTxnType.sale,
            qty=-item.qty,
            purchase_rate=fifo_avg_rate,   # original purchase value — used on cancellation
            txn_date=invoice_date,
            reason=f"Sale — Invoice ID {invoice_id}",
            created_by=created_by,
        ))


async def _restore_stock(
    db: AsyncSession,
    tenant_id: int,
    created_by: int,
    invoice: "Invoice",
) -> None:
    """
    Restore stock qty_on_hand when an invoice is cancelled.
    Uses the ORIGINAL FIFO purchase_rate stored on the sale transaction
    so that FIFO valuation re-enters the stock at its original purchase value,
    not at the current market / batch rate.
    """
    items_result = await db.execute(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)
    )
    items = items_result.scalars().all()

    for item in items:
        cat_val = getattr(item.category, 'value', str(item.category))
        if cat_val == "Polish Charges":
            continue

        stock = await _find_stock(db, tenant_id, item.category, item.purity)
        if not stock:
            continue

        # ── Find the original sale transaction to recover purchase_rate ────
        # The sale transaction was created by _deduct_stock with reason:
        # "Sale — Invoice ID {invoice.id}" and purchase_rate = FIFO avg at sale time
        sale_txn_result = await db.execute(
            select(StockTransaction)
            .where(
                StockTransaction.stock_item_id == stock.id,
                StockTransaction.txn_type      == StockTxnType.sale,
                StockTransaction.reason        == f"Sale — Invoice ID {invoice.id}",
            )
            .order_by(StockTransaction.id.desc())
            .limit(1)
        )
        sale_txn = sale_txn_result.scalar_one_or_none()
        original_rate = (
            sale_txn.purchase_rate
            if sale_txn and sale_txn.purchase_rate
            else Decimal("0")
        )

        # ── Restore qty_on_hand ────────────────────────────────────────────
        stock.qty_on_hand += item.qty

        # ── Record restoration as a new FIFO IN lot at original rate ───────
        db.add(StockTransaction(
            tenant_id=tenant_id,
            stock_item_id=stock.id,
            txn_type=StockTxnType.adjustment,
            qty=item.qty,
            purchase_rate=original_rate,   # original purchase value — correct FIFO entry
            lot_remaining=item.qty,        # treat as a fresh FIFO lot
            txn_date=date.today(),
            reason=f"Cancelled — Invoice {invoice.invoice_no}",
            created_by=created_by,
        ))


# ── Create Invoice ────────────────────────────────────────────

@router.post("/", status_code=201)
async def create_invoice(
    body:    InvoiceCreate,
    payload: dict          = Depends(get_current_user_payload),
    db:      AsyncSession  = Depends(get_db),
):
    # SEC-01 FIX — viewers cannot create invoices
    if payload.get("role") == "viewer":
        raise HTTPException(status_code=403, detail="Viewers cannot create invoices.")
    """
    Create a new invoice.

    Business rules:
    - Customer auto-created/updated in master (Issue 1)
    - TCS always zero; 269ST violation flag returned in response
    - PAN mandatory when invoice value > ₹2,00,000 regardless of pay mode (P11)
    - Stock qty_on_hand deducted for each item sold (Issue 9)
    - Returns customer_created flag so frontend can show toast (Issue 1)
    """
    tenant_id = payload["tenant_id"]

    if not body.items:
        raise HTTPException(status_code=400, detail="Invoice must have at least one item.")

    # Validate PAN format if provided
    if body.customer_pan and not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', body.customer_pan):
        raise HTTPException(status_code=422, detail="PAN format invalid. Expected: ABCDE1234F")

    # Calculate line item totals
    subtotal  = Decimal("0")
    item_rows = []
    for item in body.items:
        amount = (item.qty * item.rate + item.polish_charges * item.rate + item.making_charges).quantize(Decimal("0.01"))
        subtotal += amount
        item_rows.append(InvoiceItem(
            tenant_id=tenant_id,
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
        ))

    # GST
    gst = calculate_gst(subtotal, body.gst_rate, body.gst_type)

    # grand_total = subtotal + GST + round_off (round_off can be +ve or -ve)
    round_off   = body.round_off.quantize(Decimal("0.01"))
    grand_total = subtotal + gst["total_gst"] + round_off

    # 269ST flag — cash receipt >= ₹2,00,000 is a violation (shown in response)
    SEC_269ST_THRESHOLD = Decimal("200000")
    sec_269st_violation = (body.pay_mode == "Cash" and grand_total >= SEC_269ST_THRESHOLD)

    # PAN mandatory if invoice value > ₹2,00,000 (any pay mode) — P11
    if grand_total > PAN_THRESHOLD and not body.customer_pan:
        raise HTTPException(
            status_code=422,
            detail=(
                f"PAN is mandatory — invoice value ₹{grand_total:,.0f} exceeds ₹2,00,000. "
                "Enter customer PAN before proceeding."
            ),
        )

    # Also check existing customer SFT status (cash FY threshold)
    existing_cust_res = await db.execute(
        select(Customer).where(
            Customer.tenant_id == tenant_id,
            Customer.mobile    == body.customer_mobile,
        )
    )
    existing_cust = existing_cust_res.scalar_one_or_none()
    if (existing_cust
            and pan_is_mandatory(existing_cust.cash_receipts_fy)
            and not body.customer_pan):
        raise HTTPException(
            status_code=422,
            detail="PAN is mandatory — customer's cumulative cash receipts this FY exceed ₹2,00,000.",
        )

    # BUG-07 FIX — use MAX(id) instead of COUNT(*) to avoid duplicate invoice_no
    # when invoices have been cancelled (count < max id → collision)
    max_id_result = await db.execute(
        select(func.max(Invoice.id)).where(Invoice.tenant_id == tenant_id)
    )
    seq        = (max_id_result.scalar() or 0) + 1
    invoice_no = generate_invoice_no(tenant_id, seq)
    # Ensure uniqueness in edge cases (race condition guard)
    while True:
        exists = await db.execute(
            select(Invoice.id).where(
                Invoice.tenant_id == tenant_id,
                Invoice.invoice_no == invoice_no
            ).limit(1)
        )
        if not exists.scalar_one_or_none():
            break
        seq += 1
        invoice_no = generate_invoice_no(tenant_id, seq)

    # Pre-check stock availability BEFORE any db.flush (nothing committed on failure)
    await _check_stock_availability(db, tenant_id, item_rows)

    invoice = Invoice(
        tenant_id=tenant_id,
        invoice_no=invoice_no,
        invoice_date=body.invoice_date,
        customer_mobile=body.customer_mobile,
        customer_name=body.customer_name,
        customer_pan=body.customer_pan,
        customer_state=body.customer_state,
        customer_gstin=body.customer_gstin,
        pay_mode=body.pay_mode,
        gst_type=body.gst_type,
        gst_rate=body.gst_rate,
        subtotal=subtotal,
        cgst=gst["cgst"],
        sgst=gst["sgst"],
        igst=gst["igst"],
        tcs_applicable=False,
        tcs_base=Decimal("0"),
        tcs_amount=Decimal("0"),
        grand_total=grand_total,   # grand_total already includes round_off
        outstanding=grand_total,
        status=InvoiceStatus.active,
        payment_status=PaymentStatus.unpaid,
        notes=body.notes,
        created_by=int(payload["sub"]),
    )
    db.add(invoice)
    await db.flush()   # get invoice.id

    for item in item_rows:
        item.invoice_id = invoice.id
        db.add(item)

    await db.flush()   # item IDs available

    # Auto-upsert customer (Issue 1)
    _, customer_created = await _upsert_customer(
        db, tenant_id,
        body.customer_mobile, body.customer_name, body.customer_state,
        body.customer_pan, body.customer_gstin,
    )

    # Deduct stock (Issue 9)
    await _deduct_stock(
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
        "customer_created": customer_created,   # Issue 1 — frontend toast
    }


# ── List Invoices ─────────────────────────────────────────────

@router.get("/", response_model=list[InvoiceOut])
async def list_invoices(
    from_date:        Optional[date] = Query(None),
    to_date:          Optional[date] = Query(None),
    mobile:           Optional[str]  = Query(None),
    status:           Optional[str]  = Query(None),
    include_cancelled: bool          = Query(False),
    payload:          dict           = Depends(get_current_user_payload),
    db:               AsyncSession   = Depends(get_db),
):
    """List invoices with optional filters."""
    tenant_id = payload["tenant_id"]
    q = select(Invoice).where(Invoice.tenant_id == tenant_id)

    if not include_cancelled:
        q = q.where(Invoice.status != InvoiceStatus.cancelled)

    q = q.order_by(Invoice.invoice_date.desc(), Invoice.id.desc())

    if from_date:
        q = q.where(Invoice.invoice_date >= from_date)
    if to_date:
        q = q.where(Invoice.invoice_date <= to_date)
    if mobile:
        q = q.where(Invoice.customer_mobile == mobile)
    if status:
        q = q.where(Invoice.payment_status == status)

    result = await db.execute(q)
    return result.scalars().all()


# ── Get Single Invoice ────────────────────────────────────────

@router.get("/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: int,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


# ── Get Invoice Items ─────────────────────────────────────────
# Issue 2 — PDF preview was showing "Items not available" because
# the items endpoint did not exist in v4 original.

@router.get("/{invoice_id}/items")
async def get_invoice_items(
    invoice_id: int,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """Return line items for an invoice. Used by PDF preview."""
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(status_code=404, detail="Invoice not found")

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


# ── Amend / Edit Invoice ──────────────────────────────────────
# Issue 3 — full edit endpoint. Only non-financial fields can be changed
# post-creation to preserve audit trail (date, PAN, GSTIN, pay_mode, notes).

@router.put("/{invoice_id}/amend")
async def amend_invoice(
    invoice_id: int,
    body:       InvoiceAmend,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """
    Edit / amend a non-cancelled invoice.
    Financial totals (subtotal, GST, grand_total) are NOT recalculated —
    only metadata fields are updated to preserve the original audit trail.
    """
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status == InvoiceStatus.cancelled:
        raise HTTPException(status_code=400, detail="Cannot amend a cancelled invoice")

    if body.invoice_date is not None:
        invoice.invoice_date = body.invoice_date
    if body.customer_pan is not None:
        pan = body.customer_pan.upper().strip()
        if pan and not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', pan):
            raise HTTPException(status_code=422, detail="PAN format invalid. Expected: ABCDE1234F")
        invoice.customer_pan = pan or None
        # Sync PAN to customer master
        cust = await db.get(Customer, (invoice.customer_mobile, payload["tenant_id"]))
        if cust and pan:
            cust.pan = pan
    if body.customer_gstin is not None:
        invoice.customer_gstin = body.customer_gstin or None
    if body.pay_mode is not None:
        invoice.pay_mode = body.pay_mode
    if body.notes is not None:
        invoice.notes = body.notes

    await db.commit()
    await db.refresh(invoice)

    return {
        "message":    "Invoice amended successfully",
        "invoice_no": invoice.invoice_no,
        "invoice_id": invoice.id,
    }


# ── Cancel Invoice ────────────────────────────────────────────

@router.put("/{invoice_id}/cancel")
async def cancel_invoice(
    invoice_id: int,
    body:       dict         = {},
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """
    Cancel an invoice and issue a credit note.
    Restores stock qty_on_hand for all items (Issue 9).
    """
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status == InvoiceStatus.cancelled:
        raise HTTPException(status_code=400, detail="Invoice already cancelled")

    invoice.status = InvoiceStatus.cancelled

    # Restore stock for cancelled invoice (Issue 9)
    await _restore_stock(db, payload["tenant_id"], int(payload["sub"]), invoice)

    await db.commit()

    credit_note_no = f"CN-{invoice.invoice_no}"
    return {
        "message":        f"Invoice {invoice.invoice_no} cancelled",
        "credit_note_no": credit_note_no,
        "invoice_no":     invoice.invoice_no,
    }


# ── Full Invoice Edit ──────────────────────────────────────────
# Issue 2: Replace invoice content (items + header) while keeping invoice_no and id.
# This cancels the old stock movement and recalculates everything fresh.

class InvoiceEditBody(BaseModel):
    invoice_date:    Optional[date]    = None
    customer_mobile: Optional[str]     = None
    customer_name:   Optional[str]     = None
    customer_pan:    Optional[str]     = None
    customer_state:  Optional[str]     = None
    customer_gstin:  Optional[str]     = None
    pay_mode:        Optional[str]     = None
    gst_type:        Optional[str]     = None
    gst_rate:        Optional[float]   = None
    round_off:       Optional[float]   = None
    notes:           Optional[str]     = None
    items:           Optional[list[InvoiceItemIn]]  = None


@router.put("/{invoice_id}/edit")
async def edit_invoice(
    invoice_id: int,
    body:       InvoiceEditBody,
    payload:    dict          = Depends(get_current_user_payload),
    db:         AsyncSession  = Depends(get_db),
):
    """
    Full invoice edit — update header fields and/or replace all line items.
    - Validates PAN if grand total > Rs.2,00,000
    - Restores old stock, deletes old items, creates new items, deducts new stock
    - Recalculates subtotal, GST, grand_total, outstanding
    """
    tenant_id = payload["tenant_id"]
    invoice   = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status.value == "cancelled":
        raise HTTPException(status_code=400, detail="Cannot edit a cancelled invoice")

    # Validate PAN format if provided
    if body.customer_pan and not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', body.customer_pan):
        raise HTTPException(status_code=422, detail="PAN format invalid. Expected: ABCDE1234F")

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
    # round_off header-only update (when items not changed — still recalc grand_total)
    if body.round_off is not None and body.items is None:
        new_round = Decimal(str(body.round_off)).quantize(Decimal("0.01"))
        # grand_total stores subtotal + gst + round_off; round_off is derived from it
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
            raise HTTPException(status_code=400, detail="Invoice must have at least one item")

        # 1. Restore stock for old items at ORIGINAL FIFO purchase_rate
        old_items_result = await db.execute(
            select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)
        )
        old_items = old_items_result.scalars().all()

        # Pre-fetch all sale transactions for this invoice keyed by stock_item_id
        sale_txns_result = await db.execute(
            select(StockTransaction).where(
                StockTransaction.txn_type == StockTxnType.sale,
                StockTransaction.reason  == f"Sale — Invoice ID {invoice_id}",
            )
        )
        sale_txns_by_stock = {t.stock_item_id: t for t in sale_txns_result.scalars().all()}

        for old_item in old_items:
            cat_val = getattr(old_item.category, 'value', str(old_item.category))
            if cat_val == "Polish Charges":
                continue
            purity_filter = (
                StockItem.purity.is_(None)
                if old_item.purity is None
                else StockItem.purity == old_item.purity
            )
            stock_result = await db.execute(
                select(StockItem).where(
                    StockItem.tenant_id == tenant_id,
                    StockItem.category  == old_item.category,
                    purity_filter,
                    StockItem.is_active == True,
                ).limit(1)
            )
            stock = stock_result.scalar_one_or_none()
            if stock:
                stock.qty_on_hand += old_item.qty   # restore qty

                # Recover original purchase_rate from the sale transaction
                sale_txn = sale_txns_by_stock.get(stock.id)
                original_rate = (
                    sale_txn.purchase_rate
                    if sale_txn and sale_txn.purchase_rate
                    else Decimal("0")
                )
                # Record as a proper FIFO IN lot at original purchase value
                db.add(StockTransaction(
                    tenant_id=tenant_id,
                    stock_item_id=stock.id,
                    txn_type=StockTxnType.adjustment,
                    qty=old_item.qty,
                    purchase_rate=original_rate,
                    lot_remaining=old_item.qty,
                    txn_date=date.today(),
                    reason=f"Edit Reversal — Invoice {invoice_id}",
                    created_by=int(payload["sub"]),
                ))

        # 2. Delete old items and old stock transactions for this invoice
        for old_item in old_items:
            await db.delete(old_item)
        await db.flush()

        old_txn_result = await db.execute(
            select(StockTransaction).where(
                StockTransaction.invoice_id == invoice_id,
                StockTransaction.txn_type   == StockTxnType.sale,
            )
        )
        for txn in old_txn_result.scalars().all():
            await db.delete(txn)
        await db.flush()

        # 3. Recalculate with new items
        new_gst_type = body.gst_type or invoice.gst_type.value
        new_gst_rate = Decimal(str(body.gst_rate)) if body.gst_rate else invoice.gst_rate

        subtotal  = Decimal("0")
        new_rows  = []
        for item in body.items:
            amount = (item.qty * item.rate + item.polish_charges * item.rate + item.making_charges).quantize(Decimal("0.01"))
            subtotal += amount
            new_rows.append(InvoiceItem(
                tenant_id=tenant_id,
                invoice_id=invoice_id,
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
            ))

        gst          = calculate_gst(subtotal, new_gst_rate, new_gst_type)
        new_round    = Decimal(str(body.round_off)) if body.round_off is not None else (invoice.round_off or Decimal("0"))
        new_grand    = subtotal + gst["total_gst"] + new_round

        # PAN check
        if new_grand > PAN_THRESHOLD and not (body.customer_pan or invoice.customer_pan):
            raise HTTPException(
                status_code=422,
                detail=f"PAN is mandatory — invoice value Rs.{new_grand:,.0f} exceeds Rs.2,00,000.",
            )

        # Update invoice financials
        amount_already_paid = invoice.amount_paid
        invoice.subtotal    = subtotal
        invoice.cgst        = gst["cgst"]
        invoice.sgst        = gst["sgst"]
        invoice.igst        = gst["igst"]
        invoice.gst_rate    = new_gst_rate
        invoice.grand_total = new_grand   # new_grand already includes round_off
        invoice.outstanding = max(Decimal("0"), new_grand - amount_already_paid)
        if invoice.outstanding <= 0:
            invoice.payment_status = PaymentStatus.paid
        elif invoice.amount_paid > 0:
            invoice.payment_status = PaymentStatus.partial
        else:
            invoice.payment_status = PaymentStatus.unpaid

        for item in new_rows:
            db.add(item)
        await db.flush()

        # 4. Deduct stock for new items
        await _deduct_stock(
            db, tenant_id, int(payload["sub"]),
            invoice_id, invoice.invoice_date, new_rows,
        )

    await db.commit()
    await db.refresh(invoice)

    return {
        "id":              invoice.id,
        "invoice_no":      invoice.invoice_no,
        "message":         "Invoice updated successfully",
        "grand_total":     float(invoice.grand_total),
        "outstanding":     float(invoice.outstanding),
        "payment_status":  invoice.payment_status.value,
    }
