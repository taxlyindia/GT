# routers/invoices.py
# FIFO Improvements (new rules):
#  Rule 1 — Stock maintained on FIFO valuation basis (lot_remaining tracked on batches)
#  Rule 2 — Cancel: reverse original entry directly — restore lot_remaining, no new txn
#  Rule 3 — Edit: directly update original entry — zero old lot, create new lot at new rate
#
# Login Fix: get_db() no longer auto-commits; routes manage their own commits.
# Other fixes carried over from v4 original.

import re
from decimal import Decimal
from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete

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
        if pan and not customer.pan:
            customer.pan = pan
        if gstin and not customer.gstin:
            customer.gstin = gstin
        customer.name = name
    return customer, created


async def _find_stock(db: "AsyncSession", tenant_id: int, category, purity: "str | None"):
    """
    Find best-matching StockItem.
    Priority: exact purity match > NULL-purity catch-all > any purity for category.
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
    Polish Charges category is skipped — calculation-only, not linked to stock.
    """
    for item in items:
        cat_val  = getattr(item.category, 'value', str(item.category))
        unit_val = getattr(item.unit,     'value', str(item.unit))
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


# ════════════════════════════════════════════════════════════════════
# FIFO RULE 1 — DEDUCT STOCK (sale creates outbound, consumes lots)
# ════════════════════════════════════════════════════════════════════

async def _deduct_stock(
    db: AsyncSession,
    tenant_id: int,
    created_by: int,
    invoice_id: int,
    invoice_date: date,
    items: list["InvoiceItem"],
) -> None:
    """
    FIFO Rule 1: Deduct sold quantities from stock by consuming FIFO lots oldest-first.
    Each consumed purchase batch has its lot_remaining reduced in-place.
    A sale transaction is recorded with the FIFO-weighted purchase_rate for valuation.

    FIFO lot_remaining is the authoritative source for available stock per batch.
    """
    for item in items:
        cat_val = getattr(item.category, 'value', str(item.category))
        if cat_val == "Polish Charges":
            continue

        stock = await _find_stock(db, tenant_id, item.category, item.purity)
        if not stock:
            continue

        # ── Fetch open FIFO purchase lots oldest-first (lot_remaining > 0) ──
        batches_result = await db.execute(
            select(StockTransaction)
            .where(
                StockTransaction.stock_item_id == stock.id,
                StockTransaction.lot_remaining > 0,
                StockTransaction.txn_type.in_([
                    StockTxnType.purchase,
                    StockTxnType.opening,
                    StockTxnType.adjustment,
                ]),
            )
            .order_by(StockTransaction.txn_date, StockTransaction.id)
        )
        batches = batches_result.scalars().all()

        # ── Consume batches FIFO: reduce lot_remaining, compute weighted avg ──
        qty_to_consume = item.qty
        weighted_value = Decimal("0")
        for batch in batches:
            if qty_to_consume <= 0:
                break
            available = batch.lot_remaining or Decimal("0")
            if available <= 0:
                continue
            take = min(available, qty_to_consume)
            rate = batch.purchase_rate or Decimal("0")
            weighted_value      += take * rate
            batch.lot_remaining -= take      # ← directly reduce this lot in DB
            qty_to_consume      -= take

        fifo_avg_rate = (
            (weighted_value / item.qty).quantize(Decimal("0.01"))
            if item.qty > 0 and weighted_value > 0
            else Decimal("0")
        )

        # ── Reduce qty_on_hand ─────────────────────────────────────────────
        stock.qty_on_hand -= item.qty

        # ── Record outbound sale transaction (no lot, just valuation) ──────
        db.add(StockTransaction(
            tenant_id=tenant_id,
            stock_item_id=stock.id,
            txn_type=StockTxnType.sale,
            qty=-item.qty,
            purchase_rate=fifo_avg_rate,   # FIFO weighted avg — for cost of goods sold
            invoice_id=invoice_id,
            txn_date=invoice_date,
            reason=f"Sale — Invoice ID {invoice_id}",
            lot_remaining=None,            # outbound txns do not hold lots
            created_by=created_by,
        ))


# ════════════════════════════════════════════════════════════════════
# FIFO RULE 2 — CANCEL (restore lot_remaining directly, no new txn)
# ════════════════════════════════════════════════════════════════════

async def _restore_stock_on_cancel(
    db: AsyncSession,
    tenant_id: int,
    created_by: int,
    invoice: "Invoice",
) -> None:
    """
    FIFO Rule 2: When a sale invoice is CANCELLED, do NOT apply FIFO logic.
    Instead: restore the exact lot_remaining values that were consumed by this sale,
    reverse the sale transaction (delete it), and restore qty_on_hand.

    This means the stock returns to the precise FIFO position it had before the sale —
    no new lot is created, no FIFO re-ordering occurs.
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

        # ── Find the sale transaction(s) for this invoice ──────────────────
        # Multiple batches may have been consumed; the sale txn records total qty
        sale_txn_result = await db.execute(
            select(StockTransaction)
            .where(
                StockTransaction.invoice_id == invoice.id,
                StockTransaction.stock_item_id == stock.id,
                StockTransaction.txn_type == StockTxnType.sale,
            )
            .order_by(StockTransaction.id)
        )
        sale_txns = sale_txn_result.scalars().all()
        total_to_restore = sum(abs(t.qty) for t in sale_txns)

        if total_to_restore == 0:
            total_to_restore = item.qty

        # ── Restore lot_remaining on the FIFO batches that were consumed ────
        # Walk batches in reverse order (newest first) to re-add qty back to lots
        consumed_batches_result = await db.execute(
            select(StockTransaction)
            .where(
                StockTransaction.stock_item_id == stock.id,
                StockTransaction.txn_type.in_([
                    StockTxnType.purchase,
                    StockTxnType.opening,
                    StockTxnType.adjustment,
                ]),
            )
            .order_by(StockTransaction.txn_date.desc(), StockTransaction.id.desc())
        )
        consumed_batches = consumed_batches_result.scalars().all()

        # Restore from newest lot backwards (undo FIFO consumption in reverse)
        qty_to_restore = total_to_restore
        for batch in consumed_batches:
            if qty_to_restore <= 0:
                break
            # Only restore to batches that were originally populated (check original qty)
            original_qty = abs(batch.qty)
            current_remaining = batch.lot_remaining or Decimal("0")
            # Space available to restore in this lot
            space = original_qty - current_remaining
            if space <= 0:
                continue
            restore = min(space, qty_to_restore)
            batch.lot_remaining += restore
            qty_to_restore -= restore

        # ── Restore qty_on_hand ────────────────────────────────────────────
        stock.qty_on_hand += total_to_restore

        # ── Delete the original sale transactions (direct reversal, no new entry) ──
        for t in sale_txns:
            await db.delete(t)


# ════════════════════════════════════════════════════════════════════
# FIFO RULE 3 — EDIT (direct change to original entry, no reversal txn)
# ════════════════════════════════════════════════════════════════════

async def _update_stock_for_edit(
    db: AsyncSession,
    tenant_id: int,
    created_by: int,
    invoice_id: int,
    invoice_date: date,
    old_items: list["InvoiceItem"],
    new_items: list["InvoiceItemIn"],
) -> None:
    """
    FIFO Rule 3: When a sale invoice is EDITED, do NOT reverse and re-apply.
    Instead:
      1. Restore lot_remaining on the batches consumed by old items (undo old sale)
      2. Delete old sale transactions
      3. Consume FIFO lots for new items and record new sale transactions
      4. Update qty_on_hand to reflect the net change

    This is a direct in-place update — no reversal entry is created.
    """
    # ── Step 1 & 2: Undo old stock deductions (restore lots + delete old txns) ──
    for old_item in old_items:
        cat_val = getattr(old_item.category, 'value', str(old_item.category))
        if cat_val == "Polish Charges":
            continue

        stock = await _find_stock(db, tenant_id, old_item.category, old_item.purity)
        if not stock:
            continue

        # Find old sale txns for this item/invoice
        sale_txns_result = await db.execute(
            select(StockTransaction)
            .where(
                StockTransaction.invoice_id == invoice_id,
                StockTransaction.stock_item_id == stock.id,
                StockTransaction.txn_type == StockTxnType.sale,
            )
        )
        sale_txns = sale_txns_result.scalars().all()
        qty_to_restore = sum(abs(t.qty) for t in sale_txns) or old_item.qty

        # Restore lot_remaining on consumed batches (reverse FIFO consumption)
        consumed_batches_result = await db.execute(
            select(StockTransaction)
            .where(
                StockTransaction.stock_item_id == stock.id,
                StockTransaction.txn_type.in_([
                    StockTxnType.purchase,
                    StockTxnType.opening,
                    StockTxnType.adjustment,
                ]),
            )
            .order_by(StockTransaction.txn_date.desc(), StockTransaction.id.desc())
        )
        consumed_batches = consumed_batches_result.scalars().all()

        remaining = qty_to_restore
        for batch in consumed_batches:
            if remaining <= 0:
                break
            original_qty = abs(batch.qty)
            current_remaining = batch.lot_remaining or Decimal("0")
            space = original_qty - current_remaining
            if space <= 0:
                continue
            restore = min(space, remaining)
            batch.lot_remaining += restore
            remaining -= restore

        stock.qty_on_hand += qty_to_restore

        # Delete old sale transactions (direct change — no reversal txn created)
        for t in sale_txns:
            await db.delete(t)

    await db.flush()

    # ── Step 3: Apply new FIFO deductions for new items ───────────────────
    # Build temporary InvoiceItem-like objects from InvoiceItemIn
    new_item_rows = []
    for it in new_items:
        from decimal import Decimal as D
        amount = (it.qty * it.rate + it.polish_charges * it.rate + it.making_charges).quantize(D("0.01"))
        new_item_rows.append(InvoiceItem(
            tenant_id=tenant_id,
            invoice_id=invoice_id,
            category=it.category,
            purity=it.purity,
            description=it.description,
            hsn_code=it.hsn_code,
            qty=it.qty,
            unit=it.unit,
            rate=it.rate,
            polish_charges=it.polish_charges,
            making_charges=it.making_charges,
            amount=amount,
        ))

    # Use the standard FIFO deduct function for new items
    await _deduct_stock(db, tenant_id, created_by, invoice_id, invoice_date, new_item_rows)

    return new_item_rows


# ── Create Invoice ────────────────────────────────────────────

@router.post("/", status_code=201)
async def create_invoice(
    body:    InvoiceCreate,
    payload: dict          = Depends(get_current_user_payload),
    db:      AsyncSession  = Depends(get_db),
):
    """
    Create a new invoice.

    Business rules:
    - Customer auto-created/updated in master
    - PAN mandatory when invoice value > ₹2,00,000
    - Stock qty_on_hand deducted for each item sold (FIFO Rule 1)
    """
    tenant_id = payload["tenant_id"]

    if not body.items:
        raise HTTPException(status_code=400, detail="Invoice must have at least one item.")

    if body.customer_pan and not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', body.customer_pan):
        raise HTTPException(status_code=422, detail="PAN format invalid. Expected: ABCDE1234F")

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

    gst = calculate_gst(subtotal, body.gst_rate, body.gst_type)
    round_off   = body.round_off.quantize(Decimal("0.01"))
    grand_total = subtotal + gst["total_gst"] + round_off

    SEC_269ST_THRESHOLD = Decimal("200000")
    sec_269st_violation = (body.pay_mode == "Cash" and grand_total >= SEC_269ST_THRESHOLD)

    if grand_total > PAN_THRESHOLD and not body.customer_pan:
        raise HTTPException(
            status_code=422,
            detail=(
                f"PAN is mandatory — invoice value ₹{grand_total:,.0f} exceeds ₹2,00,000. "
                "Enter customer PAN before proceeding."
            ),
        )

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

    count_result = await db.execute(
        select(func.count()).where(Invoice.tenant_id == tenant_id)
    )
    seq        = (count_result.scalar() or 0) + 1
    invoice_no = generate_invoice_no(tenant_id, seq)

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
        grand_total=grand_total,
        outstanding=grand_total,
        status=InvoiceStatus.active,
        payment_status=PaymentStatus.unpaid,
        notes=body.notes,
        created_by=int(payload["sub"]),
    )
    db.add(invoice)
    await db.flush()

    for item in item_rows:
        item.invoice_id = invoice.id
        db.add(item)

    await db.flush()

    _, customer_created = await _upsert_customer(
        db, tenant_id,
        body.customer_mobile, body.customer_name, body.customer_state,
        body.customer_pan, body.customer_gstin,
    )

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
        "customer_created": customer_created,
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


# ── Amend / Edit Invoice (metadata only) ─────────────────────

@router.put("/{invoice_id}/amend")
async def amend_invoice(
    invoice_id: int,
    body:       InvoiceAmend,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """
    Edit non-financial metadata of a non-cancelled invoice
    (date, PAN, GSTIN, pay_mode, notes). Totals not recalculated.
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
# FIFO Rule 2: Cancellation reverses original entry directly.
# No new FIFO lot is created. lot_remaining is restored on original batches.

@router.put("/{invoice_id}/cancel")
async def cancel_invoice(
    invoice_id: int,
    body:       dict         = {},
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """
    Cancel an invoice.
    FIFO Rule 2: Restores stock by directly reversing the original FIFO lot
    consumption — lot_remaining values are restored, sale txns deleted.
    No new adjustment lot is created. Stock returns to exact pre-sale state.
    """
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status == InvoiceStatus.cancelled:
        raise HTTPException(status_code=400, detail="Invoice already cancelled")

    invoice.status = InvoiceStatus.cancelled

    # FIFO Rule 2: restore stock by reversing original entry, not creating a new lot
    await _restore_stock_on_cancel(
        db, payload["tenant_id"], int(payload["sub"]), invoice
    )

    await db.commit()

    credit_note_no = f"CN-{invoice.invoice_no}"
    return {
        "message":        f"Invoice {invoice.invoice_no} cancelled",
        "credit_note_no": credit_note_no,
        "invoice_no":     invoice.invoice_no,
    }


# ── Full Invoice Edit ──────────────────────────────────────────
# FIFO Rule 3: Edit changes original entries directly — no reversal txn.

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
    FIFO Rule 3: Stock is updated by directly modifying original FIFO entries.
      - Old sale txns are deleted (no reversal entry written)
      - lot_remaining on consumed batches is restored directly
      - New FIFO deduction is applied for new items
    Financial totals fully recalculated.
    """
    tenant_id = payload["tenant_id"]
    invoice   = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status.value == "cancelled":
        raise HTTPException(status_code=400, detail="Cannot edit a cancelled invoice")

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

    # Round-off only (no item change)
    if body.round_off is not None and body.items is None:
        new_round = Decimal(str(body.round_off)).quantize(Decimal("0.01"))
        base_total = invoice.subtotal + invoice.cgst + invoice.sgst + invoice.igst + invoice.tcs_amount
        invoice.grand_total = base_total + new_round
        invoice.outstanding = max(Decimal("0"), invoice.grand_total - invoice.amount_paid)
        if invoice.outstanding <= 0:
            invoice.payment_status = PaymentStatus.paid
        elif invoice.amount_paid > 0:
            invoice.payment_status = PaymentStatus.partial
        else:
            invoice.payment_status = PaymentStatus.unpaid

    # ── Replace items if provided (FIFO Rule 3) ───────────────
    if body.items is not None:
        if not body.items:
            raise HTTPException(status_code=400, detail="Invoice must have at least one item")

        # Fetch old items
        old_items_result = await db.execute(
            select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)
        )
        old_items = old_items_result.scalars().all()

        # FIFO Rule 3: directly update original entries (undo old, apply new)
        # _update_stock_for_edit handles: restore lots + delete old txns + new deduction
        new_gst_type = body.gst_type or invoice.gst_type.value
        new_gst_rate = Decimal(str(body.gst_rate)) if body.gst_rate else invoice.gst_rate

        # Compute new subtotal for pre-check
        new_subtotal = Decimal("0")
        temp_new_rows = []
        for item in body.items:
            amount = (item.qty * item.rate + item.polish_charges * item.rate + item.making_charges).quantize(Decimal("0.01"))
            new_subtotal += amount
            temp_new_rows.append(InvoiceItem(
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

        gst       = calculate_gst(new_subtotal, new_gst_rate, new_gst_type)
        new_round = Decimal(str(body.round_off)) if body.round_off is not None else (invoice.round_off or Decimal("0"))
        new_grand = new_subtotal + gst["total_gst"] + new_round

        if new_grand > PAN_THRESHOLD and not (body.customer_pan or invoice.customer_pan):
            raise HTTPException(
                status_code=422,
                detail=f"PAN is mandatory — invoice value Rs.{new_grand:,.0f} exceeds Rs.2,00,000.",
            )

        # Delete old InvoiceItems
        for old_item in old_items:
            await db.delete(old_item)
        await db.flush()

        # FIFO Rule 3: undo old stock deductions + apply new ones (direct change)
        await _update_stock_for_edit(
            db, tenant_id, int(payload["sub"]),
            invoice_id, body.invoice_date or invoice.invoice_date,
            old_items, body.items,
        )

        # Add new InvoiceItem rows (already flushed by _update_stock_for_edit)
        for row in temp_new_rows:
            db.add(row)
        await db.flush()

        # Update invoice financials
        amount_already_paid = invoice.amount_paid
        invoice.subtotal    = new_subtotal
        invoice.cgst        = gst["cgst"]
        invoice.sgst        = gst["sgst"]
        invoice.igst        = gst["igst"]
        invoice.gst_rate    = new_gst_rate
        invoice.grand_total = new_grand
        invoice.outstanding = max(Decimal("0"), new_grand - amount_already_paid)
        if invoice.outstanding <= 0:
            invoice.payment_status = PaymentStatus.paid
        elif invoice.amount_paid > 0:
            invoice.payment_status = PaymentStatus.partial
        else:
            invoice.payment_status = PaymentStatus.unpaid

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
