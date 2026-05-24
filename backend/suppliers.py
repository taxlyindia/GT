# routers/suppliers.py
# Supplier management: profiles, purchase invoices (with FIFO stock integration),
# payments, advances, ledger.
#
# FIFO rules for PURCHASE invoices:
#
#  CREATE  → fifo_add_purchase(): add new IN-lot with lot_remaining = qty,
#             purchase_rate = item rate, increment qty_on_hand.
#
#  CANCEL  → fifo_reverse_purchase(): zero original lot's lot_remaining,
#             add negative adjustment at ORIGINAL purchase_rate,
#             decrement qty_on_hand (clamped at 0).
#
#  EDIT    → fifo_edit_purchase_lot(): mutate the original lot IN-PLACE
#             (update qty, rate, lot_remaining by delta), add audit txn.
#             No reverse/recreate — only adjust delta on existing lot.
#             This preserves FIFO correctness for any sales that already
#             consumed part of the lot.
#
# ══════════════════════════════════════════════════════════════════

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
from utils.fifo import (
    fifo_add_purchase, fifo_cancel_purchase, fifo_edit_purchase_lot,
)
# backwards-compat alias
fifo_reverse_purchase = fifo_cancel_purchase

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
    hsn_code:       str   = "7113"
    qty:            float
    unit:           str   = "grm"
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

class SupplierInvoiceUpdate(BaseModel):
    """Full edit — header fields + optional complete item replacement."""
    invoice_no:   Optional[str]                         = None
    invoice_date: Optional[date]                        = None
    gst_rate:     Optional[float]                       = None
    gst_type:     Optional[str]                         = None
    notes:        Optional[str]                         = None
    items:        Optional[List[SupplierInvoiceItemIn]] = None

class SupplierPaymentCreate(BaseModel):
    supplier_mobile: str
    invoice_id:      Optional[int] = None
    amount:          float
    payment_date:    date
    pay_mode:        str = "Cash"
    reference_no:    Optional[str] = None
    notes:           Optional[str] = None

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


# ── GST helper (purchase side) ────────────────────────────────

def _calc_purchase_gst(
    subtotal: Decimal,
    gst_rate: Decimal,
    gst_type: str,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return (cgst, sgst, igst)."""
    cgst = sgst = igst = Decimal("0")
    total = (subtotal * gst_rate / 100).quantize(Decimal("0.01"))
    if gst_type in ("CGST+SGST", "intra"):
        # BUG-08 FIX: truncate-half so cgst+sgst == total exactly
        cgst = (total / 2).quantize(Decimal("0.01"), rounding="ROUND_DOWN")
        sgst = total - cgst
    elif gst_type in ("IGST", "inter"):
        igst = total
    return cgst, sgst, igst


# ── Stock item finder (purchase-side: exact purity+unit) ──────

async def _find_stock_exact(
    db: AsyncSession,
    tenant_id: int,
    category,
    purity: Optional[str],
    unit,
) -> Optional["StockItem"]:
    stmt = select(StockItem).where(
        StockItem.tenant_id == tenant_id,
        StockItem.category  == category,
        StockItem.is_active == True,
    )
    if purity:
        stmt = stmt.where(StockItem.purity == purity)
    else:
        stmt = stmt.where(StockItem.purity.is_(None))
    if unit:
        stmt = stmt.where(StockItem.unit == unit)
    res = await db.execute(stmt.limit(1))
    return res.scalar_one_or_none()


# ─────────────────────────────────────────────────────────────
# SUPPLIER CRUD
# ─────────────────────────────────────────────────────────────

@router.post("/", status_code=201)
async def create_supplier(
    body:    SupplierCreate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    tid = payload["tenant_id"]
    if await db.get(Supplier, (body.mobile, tid)):
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
        outstanding = float((await db.execute(
            select(func.coalesce(func.sum(SupplierInvoice.outstanding), 0))
            .where(SupplierInvoice.tenant_id == tid,
                   SupplierInvoice.supplier_mobile == s.mobile,
                   SupplierInvoice.status == "active")
        )).scalar() or 0)
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
    return {"mobile": s.mobile, "name": s.name, "gstin": s.gstin or "",
            "pan": s.pan or "", "address": s.address or "",
            "email": s.email or "", "state": s.state or ""}


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
    for field, val in body.model_dump(exclude_none=True).items():
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
    tid = payload["tenant_id"]
    s   = await db.get(Supplier, (mobile, tid))
    if not s:
        raise HTTPException(404, "Supplier not found")

    entries = []

    inv_r = await db.execute(
        select(SupplierInvoice)
        .where(SupplierInvoice.tenant_id == tid,
               SupplierInvoice.supplier_mobile == mobile,
               SupplierInvoice.status == "active")
        .order_by(SupplierInvoice.invoice_date)
    )
    for inv in inv_r.scalars().all():
        entries.append({
            "date": inv.invoice_date.isoformat(), "type": "Purchase Invoice",
            "reference": inv.invoice_no, "debit": float(inv.grand_total),
            "credit": 0.0, "notes": inv.notes or "",
        })

    pay_r = await db.execute(
        select(SupplierPayment)
        .where(SupplierPayment.tenant_id == tid,
               SupplierPayment.supplier_mobile == mobile,
               SupplierPayment.pay_mode != "Advance Adj")
        .order_by(SupplierPayment.payment_date)
    )
    for p in pay_r.scalars().all():
        entries.append({
            "date": p.payment_date.isoformat(), "type": "Payment",
            "reference": p.reference_no or f"PAY-{p.id}",
            "debit": 0.0, "credit": float(p.amount), "notes": p.notes or "",
        })

    adv_r = await db.execute(
        select(SupplierAdvance)
        .where(SupplierAdvance.tenant_id == tid,
               SupplierAdvance.supplier_mobile == mobile)
        .order_by(SupplierAdvance.advance_date)
    )
    for a in adv_r.scalars().all():
        entries.append({
            "date": a.advance_date.isoformat(), "type": "Advance",
            "reference": f"ADV-{a.id}", "debit": 0.0,
            "credit": float(a.amount), "notes": a.notes or "",
        })

    entries.sort(key=lambda x: x["date"])
    balance = 0.0
    for e in entries:
        balance += e["debit"] - e["credit"]
        e["balance"] = round(balance, 2)

    return {
        "supplier":       {"name": s.name, "mobile": s.mobile, "gstin": s.gstin or ""},
        "entries":        entries,
        "total_invoiced": round(sum(e["debit"]  for e in entries), 2),
        "total_paid":     round(sum(e["credit"] for e in entries), 2),
        "outstanding":    round(sum(e["debit"] - e["credit"] for e in entries), 2),
    }


# ─────────────────────────────────────────────────────────────
# PURCHASE INVOICES — CREATE
# ─────────────────────────────────────────────────────────────

@router.post("/invoices/", status_code=201)
async def create_supplier_invoice(
    body:    SupplierInvoiceCreate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """
    Create a purchase invoice.
    Each item adds a FIFO IN-lot via fifo_add_purchase():
    lot_remaining = qty, purchase_rate = item.rate.
    """
    tid = payload["tenant_id"]
    uid = payload.get("user_id")

    s = await db.get(Supplier, (body.supplier_mobile, tid))
    if not s:
        raise HTTPException(404, "Supplier not found")

    dup = (await db.execute(
        select(SupplierInvoice).where(
            SupplierInvoice.tenant_id  == tid,
            SupplierInvoice.invoice_no == body.invoice_no,
        )
    )).scalar_one_or_none()
    if dup:
        raise HTTPException(400, f"Invoice no {body.invoice_no!r} already exists")

    subtotal = Decimal("0")
    for it in body.items:
        subtotal += Decimal(str(it.qty * it.rate + it.making_charges))

    gst_rate = Decimal(str(body.gst_rate))
    cgst, sgst, igst = _calc_purchase_gst(subtotal, gst_rate, body.gst_type)
    grand_total = subtotal + cgst + sgst + igst

    inv = SupplierInvoice(
        tenant_id=tid, supplier_mobile=body.supplier_mobile,
        supplier_name=s.name, invoice_no=body.invoice_no,
        invoice_date=body.invoice_date, gst_rate=gst_rate,
        gst_type=body.gst_type, subtotal=subtotal,
        cgst=cgst, sgst=sgst, igst=igst,
        grand_total=grand_total, amount_paid=Decimal("0"),
        outstanding=grand_total, status="active",
        payment_status="unpaid", notes=body.notes, created_by=uid,
    )
    db.add(inv)
    await db.flush()

    for it in body.items:
        item_amt = Decimal(str(it.qty * it.rate + it.making_charges))
        db.add(SupplierInvoiceItem(
            invoice_id=inv.id, tenant_id=tid,
            category=CategoryEnum(it.category), purity=it.purity,
            description=it.description, hsn_code=it.hsn_code,
            qty=Decimal(str(it.qty)), unit=UnitEnum(it.unit),
            rate=Decimal(str(it.rate)),
            making_charges=Decimal(str(it.making_charges)),
            amount=item_amt,
        ))

        if it.category == "Polish Charges":
            continue

        # Resolve or create StockItem
        stock = await _find_stock_exact(
            db, tid, CategoryEnum(it.category), it.purity, UnitEnum(it.unit)
        )
        if not stock:
            stock = StockItem(
                tenant_id=tid, category=CategoryEnum(it.category),
                purity=it.purity, description=it.description,
                unit=UnitEnum(it.unit), qty_on_hand=Decimal("0"),
            )
            db.add(stock)
            await db.flush()

        # FIFO: add purchase IN-lot
        await fifo_add_purchase(
            db,
            tenant_id=tid, created_by=uid,
            invoice_no=body.invoice_no,
            invoice_date=body.invoice_date,
            stock=stock,
            qty=Decimal(str(it.qty)),
            purchase_rate=Decimal(str(it.rate)),
        )

    await db.commit()
    return {"message": "Supplier invoice created", "id": inv.id}


# ─────────────────────────────────────────────────────────────
# PURCHASE INVOICES — LIST / GET ITEMS
# ─────────────────────────────────────────────────────────────

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
        SupplierInvoice.status    == "active",
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
            "cgst":        float(inv.cgst), "sgst": float(inv.sgst),
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
    r     = await db.execute(
        select(SupplierInvoiceItem).where(SupplierInvoiceItem.invoice_id == invoice_id)
    )
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


# ─────────────────────────────────────────────────────────────
# PURCHASE INVOICES — CANCEL
# Exact reversal: zero the lot, add negative txn at original rate
# ─────────────────────────────────────────────────────────────

@router.delete("/invoices/{invoice_id}")
async def cancel_supplier_invoice(
    invoice_id: int,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """
    Cancel a purchase invoice.

    FIFO reversal per item:
    • Finds the original purchase StockTransaction lot for this invoice.
    • Zeros lot_remaining on that lot (no future FIFO sale can consume it).
    • Records a negative adjustment at the ORIGINAL purchase_rate.
    • Clamps qty_on_hand at 0 (some units may already have been sold).
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

        stock = await _find_stock_exact(db, tid, item.category, item.purity, item.unit)
        if not stock:
            continue

        await fifo_reverse_purchase(
            db,
            tenant_id=tid, created_by=uid,
            invoice_no=inv.invoice_no,
            reversal_date=date.today(),
            stock=stock,
            qty=item.qty,
        )

    inv.status = "cancelled"
    await db.commit()
    return {"message": "Invoice cancelled and stock reversed", "invoice_no": inv.invoice_no}


# ─────────────────────────────────────────────────────────────
# PURCHASE INVOICES — EDIT (in-place lot mutation)
# ─────────────────────────────────────────────────────────────

@router.put("/invoices/{invoice_id}")
async def update_supplier_invoice(
    invoice_id: int,
    body:       SupplierInvoiceUpdate,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """
    Edit a purchase invoice.

    FIFO rule — edit does NOT reverse and recreate lots:
    • For each changed item, fifo_edit_purchase_lot() is called which:
        - Mutates the existing IN-lot: updates qty, rate, lot_remaining (delta only)
        - Adjusts stock.qty_on_hand by the qty delta
        - Appends one audit StockTransaction (adjustment type, qty = delta)
    • This preserves any partial FIFO consumption already made by sales that
      consumed part of the lot — only the unsold remainder is adjusted.

    Header-only edits (no items): only GST/totals recalculated, no stock touched.
    """
    tid = payload["tenant_id"]
    uid = payload.get("user_id")

    inv = await db.get(SupplierInvoice, invoice_id)
    if not inv or inv.tenant_id != tid:
        raise HTTPException(404, "Invoice not found")
    if inv.status == "cancelled":
        raise HTTPException(400, "Cannot edit a cancelled invoice")

    # ── Update header fields ──────────────────────────────────
    old_invoice_no = inv.invoice_no   # needed for lot lookup
    if body.invoice_no   is not None: inv.invoice_no   = body.invoice_no
    if body.invoice_date is not None: inv.invoice_date = body.invoice_date
    if body.notes        is not None: inv.notes        = body.notes

    gst_rate = Decimal(str(body.gst_rate)) if body.gst_rate is not None else inv.gst_rate
    gst_type = body.gst_type if body.gst_type is not None else inv.gst_type

    # ── Replace items if provided ─────────────────────────────
    if body.items is not None:
        if not body.items:
            raise HTTPException(400, "Invoice must have at least one item")

        old_items_r = await db.execute(
            select(SupplierInvoiceItem).where(SupplierInvoiceItem.invoice_id == invoice_id)
        )
        old_items = old_items_r.scalars().all()

        # Build lookup from old items by (category, purity, unit) for matching
        old_map: dict[tuple, SupplierInvoiceItem] = {}
        for oi in old_items:
            key = (
                oi.category.value if hasattr(oi.category, "value") else str(oi.category),
                oi.purity or "",
                oi.unit.value if hasattr(oi.unit, "value") else str(oi.unit),
            )
            old_map[key] = oi

        # For each NEW item, find the matching old item and edit the lot in-place
        new_item_keys: set[tuple] = set()
        subtotal = Decimal("0")

        for it in body.items:
            item_amt  = Decimal(str(it.qty * it.rate + it.making_charges))
            subtotal += item_amt
            key       = (it.category, it.purity or "", it.unit)
            new_item_keys.add(key)

            if it.category == "Polish Charges":
                continue

            stock = await _find_stock_exact(
                db, tid, CategoryEnum(it.category), it.purity, UnitEnum(it.unit)
            )

            old_item = old_map.get(key)
            old_qty  = old_item.qty  if old_item else Decimal("0")
            old_rate = old_item.rate if old_item else Decimal(str(it.rate))

            if stock:
                await fifo_edit_purchase_lot(
                    db,
                    tenant_id=tid, created_by=uid,
                    invoice_no=old_invoice_no,     # look up original lot by old invoice_no
                    invoice_date=body.invoice_date or inv.invoice_date,
                    stock=stock,
                    old_qty=old_qty,
                    new_qty=Decimal(str(it.qty)),
                    old_rate=old_rate,
                    new_rate=Decimal(str(it.rate)),
                )
            elif not stock and it.qty > 0:
                # New item that didn't exist before — create StockItem + purchase lot
                stock = StockItem(
                    tenant_id=tid, category=CategoryEnum(it.category),
                    purity=it.purity, description=it.description,
                    unit=UnitEnum(it.unit), qty_on_hand=Decimal("0"),
                )
                db.add(stock)
                await db.flush()
                await fifo_add_purchase(
                    db,
                    tenant_id=tid, created_by=uid,
                    invoice_no=old_invoice_no,
                    invoice_date=body.invoice_date or inv.invoice_date,
                    stock=stock,
                    qty=Decimal(str(it.qty)),
                    purchase_rate=Decimal(str(it.rate)),
                )

        # Items present in old but NOT in new → reverse those lots
        for key, old_item in old_map.items():
            if key not in new_item_keys:
                cat_val = key[0]
                if cat_val == "Polish Charges":
                    continue
                stock = await _find_stock_exact(
                    db, tid, old_item.category, old_item.purity, old_item.unit
                )
                if stock:
                    await fifo_reverse_purchase(
                        db,
                        tenant_id=tid, created_by=uid,
                        invoice_no=old_invoice_no,
                        reversal_date=date.today(),
                        stock=stock,
                        qty=old_item.qty,
                    )

        # Rebuild InvoiceItems
        for oi in old_items:
            await db.delete(oi)
        await db.flush()

        for it in body.items:
            item_amt = Decimal(str(it.qty * it.rate + it.making_charges))
            db.add(SupplierInvoiceItem(
                invoice_id=invoice_id, tenant_id=tid,
                category=CategoryEnum(it.category), purity=it.purity,
                description=it.description, hsn_code=it.hsn_code,
                qty=Decimal(str(it.qty)), unit=UnitEnum(it.unit),
                rate=Decimal(str(it.rate)),
                making_charges=Decimal(str(it.making_charges)),
                amount=item_amt,
            ))

        # Recalculate totals
        cgst, sgst, igst = _calc_purchase_gst(subtotal, gst_rate, gst_type)
        grand_total = subtotal + cgst + sgst + igst
        inv.subtotal     = subtotal
        inv.cgst = cgst; inv.sgst = sgst; inv.igst = igst
        inv.gst_rate = gst_rate; inv.gst_type = gst_type
        inv.grand_total  = grand_total
        inv.outstanding  = max(Decimal("0"), grand_total - inv.amount_paid)
        inv.payment_status = (
            "paid"    if inv.outstanding <= 0
            else "partial" if inv.amount_paid > 0
            else "unpaid"
        )

    else:
        # Header-only: recalculate GST if rate/type changed
        if body.gst_rate is not None or body.gst_type is not None:
            cgst, sgst, igst = _calc_purchase_gst(inv.subtotal, gst_rate, gst_type)
            inv.cgst = cgst; inv.sgst = sgst; inv.igst = igst
            inv.gst_rate = gst_rate; inv.gst_type = gst_type
            inv.grand_total = inv.subtotal + cgst + sgst + igst
            inv.outstanding = max(Decimal("0"), inv.grand_total - inv.amount_paid)

    await db.commit()
    return {"message": "Invoice updated", "id": invoice_id}


# ─────────────────────────────────────────────────────────────
# SUPPLIER PAYMENTS
# ─────────────────────────────────────────────────────────────

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
        tenant_id=tid, supplier_mobile=body.supplier_mobile,
        invoice_id=body.invoice_id, amount=amt,
        payment_date=body.payment_date, pay_mode=body.pay_mode,
        reference_no=body.reference_no, notes=body.notes, created_by=uid,
    )
    db.add(pay)

    if body.invoice_id:
        inv = await db.get(SupplierInvoice, body.invoice_id)
        if inv and inv.tenant_id == tid:
            inv.amount_paid  = inv.amount_paid + amt
            inv.outstanding  = max(Decimal("0"), inv.grand_total - inv.amount_paid)
            inv.payment_status = (
                "paid"    if inv.outstanding == 0
                else "partial" if inv.amount_paid > 0
                else "unpaid"
            )

    await db.commit()

    if body.pay_mode.upper() == "CASH" or body.pay_mode == "Cash":
        try:
            sup_name   = s.name
            desc_parts = [f"Supplier payment — {sup_name}"]
            if body.invoice_id:
                inv_obj = await db.get(SupplierInvoice, body.invoice_id)
                if inv_obj:
                    desc_parts.append(f"Inv: {inv_obj.invoice_no}")
            if body.reference_no:
                desc_parts.append(f"Ref: {body.reference_no}")
            db.add(CashEntry(
                tenant_id=tid, entry_type="cash_out", amount=amt,
                entry_date=body.payment_date,
                description=" · ".join(desc_parts),
                bank_reference=body.reference_no,
            ))
            await db.commit()
        except Exception:
            pass

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
            "invoice_id": p.invoice_id, "amount": float(p.amount),
            "payment_date": p.payment_date.isoformat(),
            "pay_mode": p.pay_mode, "reference_no": p.reference_no or "—",
            "notes": p.notes or "",
        })
    return rows


@router.put("/payments/{payment_id}")
async def update_supplier_payment(
    payment_id: int,
    body:       SupplierPaymentUpdate,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    tid = payload["tenant_id"]
    p   = await db.get(SupplierPayment, payment_id)
    if not p or p.tenant_id != tid:
        raise HTTPException(404, "Payment not found")

    old_amt = p.amount
    for field, val in body.model_dump(exclude_none=True).items():
        setattr(p, field, val if field != "amount" else Decimal(str(val)))

    if p.amount != old_amt and p.invoice_id:
        inv = await db.get(SupplierInvoice, p.invoice_id)
        if inv and inv.tenant_id == tid:
            delta           = p.amount - old_amt
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

    if p.invoice_id:
        inv = await db.get(SupplierInvoice, p.invoice_id)
        if inv and inv.tenant_id == tid:
            inv.amount_paid    = max(Decimal("0"), inv.amount_paid - p.amount)
            inv.outstanding    = max(Decimal("0"), inv.grand_total - inv.amount_paid)
            inv.payment_status = "unpaid" if inv.amount_paid == 0 else "partial"

    await db.delete(p)
    await db.commit()
    return {"message": "Payment deleted"}


# ─────────────────────────────────────────────────────────────
# SUPPLIER ADVANCES
# ─────────────────────────────────────────────────────────────

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
        tenant_id=tid, supplier_mobile=body.supplier_mobile,
        amount=amt, remaining=amt, advance_date=body.advance_date,
        pay_mode=body.pay_mode, notes=body.notes, created_by=uid,
    )
    db.add(adv)
    await db.commit()

    if body.pay_mode.upper() == "CASH" or body.pay_mode == "Cash":
        try:
            db.add(CashEntry(
                tenant_id=tid, entry_type="cash_out", amount=amt,
                entry_date=body.advance_date,
                description=f"Supplier advance — {s.name} · ADV-{adv.id}",
            ))
            await db.commit()
        except Exception:
            pass

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
            "amount": float(a.amount), "remaining": float(a.remaining),
            "advance_date": a.advance_date.isoformat(),
            "pay_mode": a.pay_mode, "notes": a.notes or "",
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
        raise HTTPException(400, f"Exceeds advance remaining balance ₹{float(adv.remaining):,.2f}")
    if alloc_amt > inv.outstanding:
        raise HTTPException(400, f"Exceeds invoice outstanding ₹{float(inv.outstanding):,.2f}")

    adv.remaining    = adv.remaining - alloc_amt
    inv.amount_paid  = inv.amount_paid + alloc_amt
    inv.outstanding  = max(Decimal("0"), inv.grand_total - inv.amount_paid)
    inv.payment_status = (
        "paid"    if inv.outstanding == 0
        else "partial" if inv.amount_paid > 0
        else "unpaid"
    )

    sup_obj  = await db.get(Supplier, (adv.supplier_mobile, tid))
    sup_name = sup_obj.name if sup_obj else adv.supplier_mobile
    db.add(SupplierPayment(
        tenant_id=tid, supplier_mobile=adv.supplier_mobile,
        invoice_id=body.invoice_id, amount=alloc_amt,
        payment_date=adv.advance_date, pay_mode="Advance Adj",
        reference_no=f"ADV-{advance_id}",
        notes=f"Adjusted from advance ADV-{advance_id}",
        created_by=uid,
    ))
    await db.commit()

    return {
        "message":              "Advance adjusted against invoice",
        "advance_id":           advance_id,
        "invoice_id":           body.invoice_id,
        "allocated_amount":     float(alloc_amt),
        "advance_remaining":    float(adv.remaining),
        "invoice_outstanding":  float(inv.outstanding),
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
