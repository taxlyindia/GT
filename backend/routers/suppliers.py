# routers/suppliers.py
# Supplier management: profiles, purchase invoices (with stock integration),
# payments, advances, ledger — mirrors customer/payments/advances patterns.

from __future__ import annotations
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models import (
    Supplier, SupplierInvoice, SupplierInvoiceItem,
    SupplierPayment, SupplierAdvance, StockItem, StockTransaction,
    CategoryEnum, UnitEnum, StockTxnType, PayModeEnum, CashEntry,
)
from utils.auth import get_current_user_payload

router = APIRouter(tags=["Suppliers"])


# ── Pydantic Schemas ──────────────────────────────────────────

class SupplierCreate(BaseModel):
    name:    str
    mobile:  str
    gstin:   Optional[str] = None
    pan:     Optional[str] = None
    address: Optional[str] = None
    email:   Optional[str] = None
    state:   Optional[str] = None

class SupplierUpdate(BaseModel):
    name:    Optional[str] = None
    gstin:   Optional[str] = None
    pan:     Optional[str] = None
    address: Optional[str] = None
    email:   Optional[str] = None
    state:   Optional[str] = None

class SupplierInvoiceItemIn(BaseModel):
    category:       str
    purity:         Optional[str] = None
    description:    str
    hsn_code:       str = "7113"
    qty:            float
    unit:           str = "grm"
    rate:           float
    making_charges: float = 0.0

class SupplierInvoiceCreate(BaseModel):
    supplier_mobile: str
    invoice_no:      str
    invoice_date:    date
    gst_rate:        float = 3.0
    gst_type:        str   = "CGST+SGST"
    notes:           Optional[str] = None
    items:           List[SupplierInvoiceItemIn]

class SupplierPaymentCreate(BaseModel):
    supplier_mobile:  str
    invoice_id:       Optional[int] = None
    amount:           float
    payment_date:     date
    pay_mode:         str = "Cash"
    reference_no:     Optional[str] = None
    notes:            Optional[str] = None

class SupplierPaymentUpdate(BaseModel):
    payment_date: Optional[date]  = None
    amount:       Optional[float] = None
    pay_mode:     Optional[str]   = None
    reference_no: Optional[str]   = None
    notes:        Optional[str]   = None

class SupplierAdvanceCreate(BaseModel):
    supplier_mobile: str
    amount:          float
    advance_date:    date
    pay_mode:        str = "Cash"
    notes:           Optional[str] = None

class SupplierAdvanceUpdate(BaseModel):
    advance_date: Optional[date]  = None
    amount:       Optional[float] = None
    pay_mode:     Optional[str]   = None
    notes:        Optional[str]   = None

class SupplierAdvanceAllocation(BaseModel):
    invoice_id:       int
    allocated_amount: float


# ── Supplier CRUD ─────────────────────────────────────────────

@router.post("/", status_code=201)
async def create_supplier(
    body:    SupplierCreate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    tid = payload["tenant_id"]
    existing = await db.get(Supplier, (body.mobile, tid))
    if existing:
        raise HTTPException(400, "Supplier with this mobile already exists")
    sup = Supplier(
        mobile=body.mobile, tenant_id=tid,
        name=body.name, gstin=body.gstin, pan=body.pan,
        address=body.address, email=body.email, state=body.state or "",
    )
    db.add(sup)
    await db.commit()
    return {"message": "Supplier created", "mobile": sup.mobile}


@router.get("/")
async def list_suppliers(
    q:       Optional[str] = None,
    payload: dict          = Depends(get_current_user_payload),
    db:      AsyncSession  = Depends(get_db),
):
    tid  = payload["tenant_id"]
    stmt = select(Supplier).where(Supplier.tenant_id == tid)
    if q:
        stmt = stmt.where(
            (Supplier.name.ilike(f"%{q}%")) | (Supplier.mobile.ilike(f"%{q}%"))
        )
    r    = await db.execute(stmt.order_by(Supplier.name))
    sups = r.scalars().all()

    rows = []
    for s in sups:
        # Calculate outstanding
        inv_r  = await db.execute(
            select(func.coalesce(func.sum(SupplierInvoice.outstanding), 0))
            .where(SupplierInvoice.tenant_id == tid, SupplierInvoice.supplier_mobile == s.mobile,
                   SupplierInvoice.status == "active")
        )
        outstanding = float(inv_r.scalar() or 0)
        rows.append({
            "mobile": s.mobile, "name": s.name, "gstin": s.gstin or "",
            "pan": s.pan or "", "address": s.address or "",
            "email": s.email or "", "state": s.state or "",
            "outstanding": outstanding,
            "created_at": s.created_at.isoformat(),
        })
    return rows


@router.get("/{mobile}")
async def get_supplier(
    mobile:  str,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    tid = payload["tenant_id"]
    s   = await db.get(Supplier, (mobile, tid))
    if not s:
        raise HTTPException(404, "Supplier not found")
    return {
        "mobile": s.mobile, "name": s.name, "gstin": s.gstin or "",
        "pan": s.pan or "", "address": s.address or "",
        "email": s.email or "", "state": s.state or "",
    }


@router.put("/{mobile}")
async def update_supplier(
    mobile:  str,
    body:    SupplierUpdate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    tid = payload["tenant_id"]
    s   = await db.get(Supplier, (mobile, tid))
    if not s:
        raise HTTPException(404, "Supplier not found")
    for field, val in body.dict(exclude_none=True).items():
        setattr(s, field, val)
    await db.commit()
    return {"message": "Supplier updated"}


@router.delete("/{mobile}")
async def delete_supplier(
    mobile:  str,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    tid = payload["tenant_id"]
    s   = await db.get(Supplier, (mobile, tid))
    if not s:
        raise HTTPException(404, "Supplier not found")
    await db.delete(s)
    await db.commit()
    return {"message": "Supplier deleted"}


# ── Supplier Ledger ───────────────────────────────────────────

@router.get("/{mobile}/ledger")
async def supplier_ledger(
    mobile:  str,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """Combined ledger: purchase invoices (debit) + payments/advances (credit)."""
    tid = payload["tenant_id"]
    s   = await db.get(Supplier, (mobile, tid))
    if not s:
        raise HTTPException(404, "Supplier not found")

    entries = []

    # Purchase invoices → debit (we owe them)
    inv_r = await db.execute(
        select(SupplierInvoice)
        .where(SupplierInvoice.tenant_id == tid, SupplierInvoice.supplier_mobile == mobile,
               SupplierInvoice.status == "active")
        .order_by(SupplierInvoice.invoice_date)
    )
    for inv in inv_r.scalars().all():
        entries.append({
            "date": inv.invoice_date.isoformat(), "type": "Purchase Invoice",
            "reference": inv.invoice_no, "debit": float(inv.grand_total),
            "credit": 0.0, "notes": inv.notes or "",
        })

    # Payments -> credit (we paid them)
    # Exclude "Advance Adj" rows — those are advance allocations already counted
    # via the SupplierAdvance entry below. Including them would double-count.
    pay_r = await db.execute(
        select(SupplierPayment)
        .where(
            SupplierPayment.tenant_id == tid,
            SupplierPayment.supplier_mobile == mobile,
            SupplierPayment.pay_mode != "Advance Adj",
        )
        .order_by(SupplierPayment.payment_date)
    )
    for p in pay_r.scalars().all():
        entries.append({
            "date": p.payment_date.isoformat(), "type": "Payment",
            "reference": p.reference_no or f"PAY-{p.id}", "debit": 0.0,
            "credit": float(p.amount), "notes": p.notes or "",
        })

    # Advances -> credit (full amount when recorded; allocation tracked via remaining)
    # Show each advance once. The allocation to invoice reduces adv.remaining but the
    # full adv.amount appears in the ledger as the credit entry for the advance.
    adv_r = await db.execute(
        select(SupplierAdvance)
        .where(SupplierAdvance.tenant_id == tid, SupplierAdvance.supplier_mobile == mobile)
        .order_by(SupplierAdvance.advance_date)
    )
    for a in adv_r.scalars().all():
        entries.append({
            "date": a.advance_date.isoformat(), "type": "Advance",
            "reference": f"ADV-{a.id}", "debit": 0.0,
            "credit": float(a.amount), "notes": a.notes or "",
        })

    entries.sort(key=lambda x: x["date"])

    # Running balance
    balance = 0.0
    for e in entries:
        balance += e["debit"] - e["credit"]
        e["balance"] = round(balance, 2)

    total_invoiced = sum(e["debit"]  for e in entries)
    total_paid     = sum(e["credit"] for e in entries)

    return {
        "supplier":       {"name": s.name, "mobile": s.mobile, "gstin": s.gstin or ""},
        "entries":        entries,
        "total_invoiced": round(total_invoiced, 2),
        "total_paid":     round(total_paid,     2),
        "outstanding":    round(total_invoiced - total_paid, 2),
    }


# ── Supplier Invoices (Purchase Invoices) ────────────────────

@router.post("/invoices/", status_code=201)
async def create_supplier_invoice(
    body:    SupplierInvoiceCreate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    tid  = payload["tenant_id"]
    uid  = payload.get("user_id")

    # Validate supplier exists
    s = await db.get(Supplier, (body.supplier_mobile, tid))
    if not s:
        raise HTTPException(404, "Supplier not found")

    # Check duplicate invoice_no
    dup = await db.execute(
        select(SupplierInvoice).where(
            SupplierInvoice.tenant_id == tid,
            SupplierInvoice.invoice_no == body.invoice_no,
        )
    )
    if dup.scalar_one_or_none():
        raise HTTPException(400, f"Invoice no {body.invoice_no!r} already exists")

    # Build totals
    subtotal = Decimal("0")
    for it in body.items:
        subtotal += Decimal(str(it.qty * it.rate + it.making_charges))

    rate  = Decimal(str(body.gst_rate))
    gtype = body.gst_type
    cgst = sgst = igst = Decimal("0")
    if gtype == "CGST+SGST":
        cgst = sgst = (subtotal * rate / 200).quantize(Decimal("0.01"))
    elif gtype == "IGST":
        igst = (subtotal * rate / 100).quantize(Decimal("0.01"))

    grand_total = subtotal + cgst + sgst + igst

    inv = SupplierInvoice(
        tenant_id       = tid,
        supplier_mobile = body.supplier_mobile,
        supplier_name   = s.name,
        invoice_no      = body.invoice_no,
        invoice_date    = body.invoice_date,
        gst_rate        = rate,
        gst_type        = gtype,
        subtotal        = subtotal,
        cgst            = cgst,
        sgst            = sgst,
        igst            = igst,
        grand_total     = grand_total,
        amount_paid     = Decimal("0"),
        outstanding     = grand_total,
        status          = "active",
        payment_status  = "unpaid",
        notes           = body.notes,
        created_by      = uid,
    )
    db.add(inv)
    await db.flush()

    # Create items + update stock
    for it in body.items:
        item_subtotal = Decimal(str(it.qty * it.rate))
        making        = Decimal(str(it.making_charges))
        item_amt      = item_subtotal + making

        inv_item = SupplierInvoiceItem(
            invoice_id    = inv.id,
            tenant_id     = tid,
            category      = CategoryEnum(it.category),
            purity        = it.purity,
            description   = it.description,
            hsn_code      = it.hsn_code,
            qty           = Decimal(str(it.qty)),
            unit          = UnitEnum(it.unit),
            rate          = Decimal(str(it.rate)),
            making_charges= making,
            amount        = item_amt,
        )
        db.add(inv_item)

        # Auto-add to stock
        stock_r = await db.execute(
            select(StockItem).where(
                StockItem.tenant_id == tid,
                StockItem.category  == CategoryEnum(it.category),
                StockItem.purity    == it.purity,
                StockItem.unit      == UnitEnum(it.unit),
            ).limit(1)
        )
        stock = stock_r.scalar_one_or_none()

        if not stock:
            stock = StockItem(
                tenant_id   = tid,
                category    = CategoryEnum(it.category),
                purity      = it.purity,
                description = it.description,
                unit        = UnitEnum(it.unit),
                qty_on_hand = Decimal("0"),
            )
            db.add(stock)
            await db.flush()

        stock.qty_on_hand = stock.qty_on_hand + Decimal(str(it.qty))

        txn = StockTransaction(
            tenant_id     = tid,
            stock_item_id = stock.id,
            txn_type      = StockTxnType.purchase,
            qty           = Decimal(str(it.qty)),
            purchase_rate = Decimal(str(it.rate)),
            invoice_id    = None,
            reason        = f"Supplier Invoice {body.invoice_no}",
            txn_date      = body.invoice_date,
            lot_remaining = Decimal(str(it.qty)),
            created_by    = uid,
        )
        db.add(txn)

    await db.commit()
    return {"message": "Supplier invoice created", "id": inv.id}


@router.get("/invoices/")
async def list_supplier_invoices(
    mobile:    Optional[str]  = None,
    from_date: Optional[date] = None,
    to_date:   Optional[date] = None,
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    tid  = payload["tenant_id"]
    stmt = select(SupplierInvoice).where(
        SupplierInvoice.tenant_id == tid,
        SupplierInvoice.status == "active",
    )
    if mobile:    stmt = stmt.where(SupplierInvoice.supplier_mobile == mobile)
    if from_date: stmt = stmt.where(SupplierInvoice.invoice_date >= from_date)
    if to_date:   stmt = stmt.where(SupplierInvoice.invoice_date <= to_date)
    r    = await db.execute(stmt.order_by(SupplierInvoice.invoice_date.desc()))
    invs = r.scalars().all()
    return [
        {
            "id": inv.id, "invoice_no": inv.invoice_no,
            "invoice_date": inv.invoice_date.isoformat(),
            "supplier_mobile": inv.supplier_mobile,
            "supplier_name":   inv.supplier_name,
            "subtotal":    float(inv.subtotal),
            "cgst":        float(inv.cgst),  "sgst": float(inv.sgst),
            "igst":        float(inv.igst),
            "grand_total": float(inv.grand_total),
            "amount_paid": float(inv.amount_paid),
            "outstanding": float(inv.outstanding),
            "payment_status": inv.payment_status,
            "notes":       inv.notes or "",
        }
        for inv in invs
    ]


@router.get("/invoices/{invoice_id}/items")
async def get_supplier_invoice_items(
    invoice_id: int,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    tid = payload["tenant_id"]
    inv = await db.get(SupplierInvoice, invoice_id)
    if not inv or inv.tenant_id != tid:
        raise HTTPException(404, "Invoice not found")
    r     = await db.execute(select(SupplierInvoiceItem).where(SupplierInvoiceItem.invoice_id == invoice_id))
    items = r.scalars().all()
    return {"items": [
        {
            "id": it.id, "category": it.category.value, "purity": it.purity or "",
            "description": it.description, "hsn_code": it.hsn_code,
            "qty": float(it.qty), "unit": it.unit.value,
            "rate": float(it.rate), "making_charges": float(it.making_charges),
            "amount": float(it.amount),
        }
        for it in items
    ]}



# ════════════════════════════════════════════════════════════════════
# FIFO RULE 3 — EDIT PURCHASE INVOICE (direct change to original entry)
# ════════════════════════════════════════════════════════════════════

class SupplierInvoiceUpdate(BaseModel):
    """Full edit — header fields + optional complete item replacement."""
    invoice_no:      Optional[str]                         = None
    invoice_date:    Optional[date]                        = None
    gst_rate:        Optional[float]                       = None
    gst_type:        Optional[str]                         = None
    notes:           Optional[str]                         = None
    items:           Optional[List[SupplierInvoiceItemIn]] = None  # if provided, replaces ALL items


@router.put("/invoices/{invoice_id}")
async def update_supplier_invoice(
    invoice_id: int,
    body:       SupplierInvoiceUpdate,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """
    Full edit of a purchase invoice — all fields including qty, rate, items.
    FIFO Rule 3: Direct change to original entry — no reversal transaction created.
      - Old purchase lot is updated in-place (lot_remaining zeroed, qty_on_hand reduced)
      - Old items deleted
      - New items and stock lots created at new rates
      - Totals fully recalculated.
    """
    tid = payload["tenant_id"]
    uid = payload.get("user_id")

    inv = await db.get(SupplierInvoice, invoice_id)
    if not inv or inv.tenant_id != tid:
        raise HTTPException(404, "Invoice not found")
    if inv.status == "cancelled":
        raise HTTPException(400, "Cannot edit a cancelled invoice")

    # ── Update header fields ──────────────────────────────────
    if body.invoice_no   is not None: inv.invoice_no   = body.invoice_no
    if body.invoice_date is not None: inv.invoice_date = body.invoice_date
    if body.notes        is not None: inv.notes        = body.notes

    gst_rate = Decimal(str(body.gst_rate)) if body.gst_rate is not None else inv.gst_rate
    gst_type = body.gst_type if body.gst_type is not None else inv.gst_type

    # ── Replace items if provided (FIFO Rule 3) ───────────────
    if body.items is not None:
        if not body.items:
            raise HTTPException(400, "Invoice must have at least one item")

        old_items_r = await db.execute(
            select(SupplierInvoiceItem).where(SupplierInvoiceItem.invoice_id == invoice_id)
        )
        old_items = old_items_r.scalars().all()

        # FIFO Rule 3: Directly update original FIFO lot entries — no reversal txn
        for old_item in old_items:
            if old_item.category and old_item.category.value == "Polish Charges":
                continue
            stock_r = await db.execute(
                select(StockItem).where(
                    StockItem.tenant_id == tid,
                    StockItem.category  == old_item.category,
                    StockItem.purity    == old_item.purity,
                    StockItem.unit      == old_item.unit,
                ).limit(1)
            )
            stock = stock_r.scalar_one_or_none()
            if not stock:
                continue
            # Find and zero the original FIFO purchase lot (direct change — no reversal txn)
            orig_txn_r = await db.execute(
                select(StockTransaction).where(
                    StockTransaction.stock_item_id == stock.id,
                    StockTransaction.txn_type      == StockTxnType.purchase,
                    StockTransaction.reason        == f"Supplier Invoice {inv.invoice_no}",
                ).order_by(StockTransaction.id.desc()).limit(1)
            )
            orig_txn = orig_txn_r.scalar_one_or_none()
            if orig_txn:
                # Zero out this lot directly — the new item will create a fresh lot
                orig_txn.lot_remaining = Decimal("0")
            # Reduce qty_on_hand directly (no separate reversal transaction)
            stock.qty_on_hand = max(Decimal("0"), stock.qty_on_hand - old_item.qty)

        # Delete old items
        for old_item in old_items:
            await db.delete(old_item)
        await db.flush()

        # Create new items + add new FIFO lots at updated rates
        subtotal   = Decimal("0")
        new_inv_no = body.invoice_no or inv.invoice_no

        for it in body.items:
            item_amt  = Decimal(str(it.qty * it.rate + it.making_charges))
            subtotal += item_amt
            db.add(SupplierInvoiceItem(
                invoice_id=invoice_id, tenant_id=tid,
                category=CategoryEnum(it.category), purity=it.purity,
                description=it.description, hsn_code=it.hsn_code,
                qty=Decimal(str(it.qty)), unit=UnitEnum(it.unit),
                rate=Decimal(str(it.rate)),
                making_charges=Decimal(str(it.making_charges)),
                amount=item_amt,
            ))
            if it.category == "Polish Charges":
                continue
            stock_r = await db.execute(
                select(StockItem).where(
                    StockItem.tenant_id == tid,
                    StockItem.category  == CategoryEnum(it.category),
                    StockItem.purity    == it.purity,
                    StockItem.unit      == UnitEnum(it.unit),
                ).limit(1)
            )
            stock = stock_r.scalar_one_or_none()
            if not stock:
                stock = StockItem(
                    tenant_id=tid, category=CategoryEnum(it.category),
                    purity=it.purity, description=it.description,
                    unit=UnitEnum(it.unit), qty_on_hand=Decimal("0"),
                )
                db.add(stock)
                await db.flush()
            # Add qty at new rate — new FIFO lot created directly
            stock.qty_on_hand += Decimal(str(it.qty))
            db.add(StockTransaction(
                tenant_id=tid, stock_item_id=stock.id,
                txn_type=StockTxnType.purchase, qty=Decimal(str(it.qty)),
                purchase_rate=Decimal(str(it.rate)), invoice_id=None,
                reason=f"Supplier Invoice {new_inv_no}",
                txn_date=body.invoice_date or inv.invoice_date,
                lot_remaining=Decimal(str(it.qty)), created_by=uid,
            ))

        # Recalculate totals
        cgst = sgst = igst = Decimal("0")
        if gst_type in ("CGST+SGST", "intra"):
            cgst = sgst = (subtotal * gst_rate / 200).quantize(Decimal("0.01"))
        elif gst_type in ("IGST", "inter"):
            igst = (subtotal * gst_rate / 100).quantize(Decimal("0.01"))
        grand_total = subtotal + cgst + sgst + igst
        inv.subtotal = subtotal; inv.cgst = cgst; inv.sgst = sgst; inv.igst = igst
        inv.gst_rate = gst_rate; inv.gst_type = gst_type; inv.grand_total = grand_total
        inv.outstanding = max(Decimal("0"), grand_total - inv.amount_paid)
        inv.payment_status = ("paid" if inv.outstanding <= 0
                              else "partial" if inv.amount_paid > 0 else "unpaid")

    else:
        # Header-only: recalculate GST if rate/type changed
        if body.gst_rate is not None or body.gst_type is not None:
            subtotal = inv.subtotal
            cgst = sgst = igst = Decimal("0")
            if gst_type in ("CGST+SGST", "intra"):
                cgst = sgst = (subtotal * gst_rate / 200).quantize(Decimal("0.01"))
            elif gst_type in ("IGST", "inter"):
                igst = (subtotal * gst_rate / 100).quantize(Decimal("0.01"))
            inv.cgst = cgst; inv.sgst = sgst; inv.igst = igst
            inv.gst_rate = gst_rate; inv.gst_type = gst_type
            inv.grand_total = subtotal + cgst + sgst + igst
            inv.outstanding = max(Decimal("0"), inv.grand_total - inv.amount_paid)

    await db.commit()
    return {"message": "Invoice updated", "id": invoice_id}


# ════════════════════════════════════════════════════════════════════
# FIFO RULE 2 — CANCEL PURCHASE INVOICE (reverse original entry directly)
# ════════════════════════════════════════════════════════════════════

@router.delete("/invoices/{invoice_id}")
async def cancel_supplier_invoice(
    invoice_id: int,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """
    Cancel a purchase invoice and reverse the stock entry.
    FIFO Rule 2: Do NOT apply FIFO method on cancellation.
    Instead: directly reverse the original purchase entry —
      - The original FIFO lot is deleted (or zeroed if partially consumed)
      - qty_on_hand is reduced by the original purchased qty
      - No new adjustment transaction is written
    If the lot was partially consumed by sales, only the remaining lot is removed.
    """
    tid = payload["tenant_id"]
    uid = payload.get("user_id")

    inv = await db.get(SupplierInvoice, invoice_id)
    if not inv or inv.tenant_id != tid:
        raise HTTPException(404, "Invoice not found")
    if inv.status == "cancelled":
        raise HTTPException(400, "Invoice is already cancelled")

    items_r = await db.execute(
        select(SupplierInvoiceItem).where(SupplierInvoiceItem.invoice_id == invoice_id)
    )
    items = items_r.scalars().all()

    for item in items:
        if item.category and item.category.value == "Polish Charges":
            continue

        stock_r = await db.execute(
            select(StockItem).where(
                StockItem.tenant_id == tid,
                StockItem.category  == item.category,
                StockItem.purity    == item.purity,
                StockItem.unit      == item.unit,
            ).limit(1)
        )
        stock = stock_r.scalar_one_or_none()
        if not stock:
            continue

        # Find the original purchase lot for this invoice
        orig_txn_r = await db.execute(
            select(StockTransaction).where(
                StockTransaction.stock_item_id == stock.id,
                StockTransaction.txn_type      == StockTxnType.purchase,
                StockTransaction.reason        == f"Supplier Invoice {inv.invoice_no}",
            ).order_by(StockTransaction.id.desc()).limit(1)
        )
        orig_txn = orig_txn_r.scalar_one_or_none()

        if orig_txn:
            # FIFO Rule 2: zero out the original lot directly — no new txn created
            # The lot_remaining may have already been partially consumed by sales.
            # We remove only the remaining balance and reduce qty_on_hand accordingly.
            qty_still_in_lot = orig_txn.lot_remaining or Decimal("0")
            orig_txn.lot_remaining = Decimal("0")
            # Reduce qty_on_hand by the qty still remaining in this lot
            stock.qty_on_hand = max(Decimal("0"), stock.qty_on_hand - qty_still_in_lot)
        else:
            # No lot found — fall back: reduce qty directly (clamped at zero)
            stock.qty_on_hand = max(Decimal("0"), stock.qty_on_hand - item.qty)

    inv.status = "cancelled"
    await db.commit()
    return {"message": "Invoice cancelled and stock reversed", "invoice_no": inv.invoice_no}

# ── Supplier Payments ─────────────────────────────────────────

@router.post("/payments/", status_code=201)
async def record_supplier_payment(
    body:    SupplierPaymentCreate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    tid = payload["tenant_id"]
    uid = payload.get("user_id")

    s = await db.get(Supplier, (body.supplier_mobile, tid))
    if not s:
        raise HTTPException(404, "Supplier not found")

    amt = Decimal(str(body.amount))
    pay = SupplierPayment(
        tenant_id       = tid,
        supplier_mobile = body.supplier_mobile,
        invoice_id      = body.invoice_id,
        amount          = amt,
        payment_date    = body.payment_date,
        pay_mode        = body.pay_mode,
        reference_no    = body.reference_no,
        notes           = body.notes,
        created_by      = uid,
    )
    db.add(pay)

    # Update invoice if linked
    if body.invoice_id:
        inv = await db.get(SupplierInvoice, body.invoice_id)
        if inv and inv.tenant_id == tid:
            inv.amount_paid = inv.amount_paid + amt
            inv.outstanding = max(Decimal("0"), inv.grand_total - inv.amount_paid)
            if inv.outstanding == 0:
                inv.payment_status = "paid"
            elif inv.amount_paid > 0:
                inv.payment_status = "partial"

    await db.commit()

    # Auto-create Cash Book entry when payment mode is Cash
    if body.pay_mode.upper() == "CASH" or body.pay_mode == "Cash":
        try:
            sup_obj  = await db.get(Supplier, (body.supplier_mobile, tid))
            sup_name = sup_obj.name if sup_obj else body.supplier_mobile
            desc_parts = [f"Supplier payment — {sup_name}"]
            if body.invoice_id:
                inv_obj = await db.get(SupplierInvoice, body.invoice_id)
                if inv_obj:
                    desc_parts.append(f"Inv: {inv_obj.invoice_no or 'SINV-' + str(body.invoice_id)}")
            if body.reference_no:
                desc_parts.append(f"Ref: {body.reference_no}")
            cash = CashEntry(
                tenant_id      = tid,
                entry_type     = "cash_out",
                amount         = amt,
                entry_date     = body.payment_date,
                description    = " · ".join(desc_parts),
                bank_reference = body.reference_no,
            )
            db.add(cash)
            await db.commit()
        except Exception:
            pass  # Cash entry failure must not roll back the payment

    return {"message": "Payment recorded", "id": pay.id}


@router.get("/payments/")
async def list_supplier_payments(
    mobile:    Optional[str]  = None,
    from_date: Optional[date] = None,
    to_date:   Optional[date] = None,
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    tid  = payload["tenant_id"]
    stmt = select(SupplierPayment).where(SupplierPayment.tenant_id == tid)
    if mobile:    stmt = stmt.where(SupplierPayment.supplier_mobile == mobile)
    if from_date: stmt = stmt.where(SupplierPayment.payment_date >= from_date)
    if to_date:   stmt = stmt.where(SupplierPayment.payment_date <= to_date)
    r    = await db.execute(stmt.order_by(SupplierPayment.payment_date.desc()))
    pays = r.scalars().all()

    rows = []
    for p in pays:
        sup = await db.get(Supplier, (p.supplier_mobile, tid))
        rows.append({
            "id": p.id, "supplier_mobile": p.supplier_mobile,
            "supplier_name": sup.name if sup else "—",
            "invoice_id":    p.invoice_id,
            "amount":        float(p.amount),
            "payment_date":  p.payment_date.isoformat(),
            "pay_mode":      p.pay_mode,
            "reference_no":  p.reference_no or "—",
            "notes":         p.notes or "",
        })
    return rows


@router.put("/payments/{payment_id}")
async def update_supplier_payment(
    payment_id: int,
    body:       SupplierPaymentUpdate,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    tid     = payload["tenant_id"]
    p       = await db.get(SupplierPayment, payment_id)
    if not p or p.tenant_id != tid:
        raise HTTPException(404, "Payment not found")

    old_amt = p.amount  # save before mutation
    for field, val in body.dict(exclude_none=True).items():
        setattr(p, field, val if field != "amount" else Decimal(str(val)))

    # If amount changed and payment is linked to an invoice, re-sync that invoice
    new_amt = p.amount
    if new_amt != old_amt and p.invoice_id:
        inv = await db.get(SupplierInvoice, p.invoice_id)
        if inv and inv.tenant_id == tid:
            delta           = new_amt - old_amt
            inv.amount_paid = max(Decimal("0"), inv.amount_paid + delta)
            inv.outstanding = max(Decimal("0"), inv.grand_total - inv.amount_paid)
            inv.payment_status = (
                "paid"    if inv.outstanding == 0
                else "partial" if inv.amount_paid > 0
                else "unpaid"
            )

    await db.commit()
    return {"message": "Payment updated"}


@router.delete("/payments/{payment_id}")
async def delete_supplier_payment(
    payment_id: int,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    tid = payload["tenant_id"]
    p   = await db.get(SupplierPayment, payment_id)
    if not p or p.tenant_id != tid:
        raise HTTPException(404, "Payment not found")

    # Reverse invoice payment status
    if p.invoice_id:
        inv = await db.get(SupplierInvoice, p.invoice_id)
        if inv and inv.tenant_id == tid:
            inv.amount_paid = max(Decimal("0"), inv.amount_paid - p.amount)
            inv.outstanding = inv.grand_total - inv.amount_paid
            inv.payment_status = "unpaid" if inv.amount_paid == 0 else "partial"

    await db.delete(p)
    await db.commit()
    return {"message": "Payment deleted"}


# ── Supplier Advances ─────────────────────────────────────────

@router.post("/advances/", status_code=201)
async def record_supplier_advance(
    body:    SupplierAdvanceCreate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    tid = payload["tenant_id"]
    uid = payload.get("user_id")
    s   = await db.get(Supplier, (body.supplier_mobile, tid))
    if not s:
        raise HTTPException(404, "Supplier not found")

    amt = Decimal(str(body.amount))
    adv = SupplierAdvance(
        tenant_id       = tid,
        supplier_mobile = body.supplier_mobile,
        amount          = amt,
        remaining       = amt,
        advance_date    = body.advance_date,
        pay_mode        = body.pay_mode,
        notes           = body.notes,
        created_by      = uid,
    )
    db.add(adv)
    await db.commit()

    # Auto-create Cash Book entry when advance mode is Cash
    if body.pay_mode.upper() == "CASH" or body.pay_mode == "Cash":
        try:
            sup_obj  = await db.get(Supplier, (body.supplier_mobile, tid))
            sup_name = sup_obj.name if sup_obj else body.supplier_mobile
            desc_parts = [
                f"Supplier advance — {sup_name} ({body.supplier_mobile})",
                f"ADV-{adv.id}",
            ]
            if body.notes:
                desc_parts.append(body.notes)
            cash = CashEntry(
                tenant_id      = tid,
                entry_type     = "cash_out",
                amount         = amt,
                entry_date     = body.advance_date,
                description    = " · ".join(desc_parts),
                bank_reference = None,
            )
            db.add(cash)
            await db.commit()
        except Exception:
            pass  # Cash entry failure must not roll back the advance

    return {"message": "Advance recorded", "id": adv.id}


@router.get("/advances/")
async def list_supplier_advances(
    mobile:  Optional[str] = None,
    payload: dict          = Depends(get_current_user_payload),
    db:      AsyncSession  = Depends(get_db),
):
    tid  = payload["tenant_id"]
    stmt = select(SupplierAdvance).where(SupplierAdvance.tenant_id == tid)
    if mobile: stmt = stmt.where(SupplierAdvance.supplier_mobile == mobile)
    r    = await db.execute(stmt.order_by(SupplierAdvance.advance_date.desc()))
    advs = r.scalars().all()

    rows = []
    for a in advs:
        sup = await db.get(Supplier, (a.supplier_mobile, tid))
        rows.append({
            "id": a.id, "supplier_mobile": a.supplier_mobile,
            "supplier_name": sup.name if sup else "—",
            "amount":        float(a.amount),
            "remaining":     float(a.remaining),
            "advance_date":  a.advance_date.isoformat(),
            "pay_mode":      a.pay_mode,
            "notes":         a.notes or "",
        })
    return rows


@router.put("/advances/{advance_id}")
async def update_supplier_advance(
    advance_id: int,
    body:       SupplierAdvanceUpdate,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    tid = payload["tenant_id"]
    a   = await db.get(SupplierAdvance, advance_id)
    if not a or a.tenant_id != tid:
        raise HTTPException(404, "Advance not found")
    if body.amount is not None:
        new_amt = Decimal(str(body.amount))
        diff    = new_amt - a.amount
        a.amount    = new_amt
        a.remaining = max(Decimal("0"), a.remaining + diff)
    for field in ("advance_date", "pay_mode", "notes"):
        val = getattr(body, field)
        if val is not None:
            setattr(a, field, val)
    await db.commit()
    return {"message": "Advance updated"}


@router.post("/advances/{advance_id}/allocate", status_code=200)
async def allocate_supplier_advance(
    advance_id: int,
    body:       SupplierAdvanceAllocation,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """
    Adjust a supplier advance against an outstanding purchase invoice.
    Reduces advance.remaining and inv.outstanding by the allocated amount.
    Also creates a SupplierPayment record so it appears in the payment register.
    """
    tid = payload["tenant_id"]
    uid = payload.get("user_id")

    adv = await db.get(SupplierAdvance, advance_id)
    if not adv or adv.tenant_id != tid:
        raise HTTPException(404, "Advance not found")

    inv = await db.get(SupplierInvoice, body.invoice_id)
    if not inv or inv.tenant_id != tid:
        raise HTTPException(404, "Invoice not found")

    alloc_amt = Decimal(str(body.allocated_amount))
    if alloc_amt <= 0:
        raise HTTPException(400, "Allocated amount must be positive")
    if alloc_amt > adv.remaining:
        raise HTTPException(400, f"Allocated amount exceeds advance remaining balance (Rs. {float(adv.remaining):,.2f})")
    if alloc_amt > inv.outstanding:
        raise HTTPException(400, f"Allocated amount exceeds invoice outstanding (Rs. {float(inv.outstanding):,.2f})")

    # Deduct from advance remaining
    adv.remaining = adv.remaining - alloc_amt

    # Reduce invoice outstanding
    inv.amount_paid  = inv.amount_paid + alloc_amt
    inv.outstanding  = max(Decimal("0"), inv.grand_total - inv.amount_paid)
    if inv.outstanding == 0:
        inv.payment_status = "paid"
    elif inv.amount_paid > 0:
        inv.payment_status = "partial"

    # Create a SupplierPayment record for audit trail
    sup_obj  = await db.get(Supplier, (adv.supplier_mobile, tid))
    sup_name = sup_obj.name if sup_obj else adv.supplier_mobile
    pay = SupplierPayment(
        tenant_id       = tid,
        supplier_mobile = adv.supplier_mobile,
        invoice_id      = body.invoice_id,
        amount          = alloc_amt,
        payment_date    = adv.advance_date,
        pay_mode        = "Advance Adj",
        reference_no    = f"ADV-{advance_id}",
        notes           = f"Adjusted from advance ADV-{advance_id}",
        created_by      = uid,
    )
    db.add(pay)
    await db.commit()

    return {
        "message":          "Advance adjusted against invoice",
        "advance_id":       advance_id,
        "invoice_id":       body.invoice_id,
        "allocated_amount": float(alloc_amt),
        "advance_remaining": float(adv.remaining),
        "invoice_outstanding": float(inv.outstanding),
    }


@router.delete("/advances/{advance_id}")
async def cancel_supplier_advance(
    advance_id: int,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    tid = payload["tenant_id"]
    a   = await db.get(SupplierAdvance, advance_id)
    if not a or a.tenant_id != tid:
        raise HTTPException(404, "Advance not found")
    await db.delete(a)
    await db.commit()
    return {"message": "Advance cancelled"}
