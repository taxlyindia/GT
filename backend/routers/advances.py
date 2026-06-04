# routers/advances.py
# Changes vs v4 original:
#  Issue 6  — list_advances returns customer_name so frontend dropdown is populated
#  Issue 6  — record_advance stores customer_name
#  Issue 8  — no backend change needed (export.py handles the Excel download)

from datetime import date
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models import Advance, AdvanceAllocation, Invoice, Customer, PaymentStatus
from utils.auth import get_current_user_payload

router = APIRouter()


class AdvanceCreate(BaseModel):
    customer_mobile: str
    amount:          Decimal
    advance_date:    date
    pay_mode:        str
    notes:           Optional[str] = None

class AllocationItem(BaseModel):
    invoice_id:       int
    allocated_amount: Decimal

class AllocateRequest(BaseModel):
    allocations: list[AllocationItem]


@router.post("/", status_code=201)
async def record_advance(
    body:    AdvanceCreate,
    payload: dict          = Depends(get_current_user_payload),
    db:      AsyncSession  = Depends(get_db),
):
    """Record a customer advance payment."""
    tenant_id = payload["tenant_id"]

    # customer_name is NOT a column on Advance model — resolve at read time from master
    advance = Advance(
        tenant_id=tenant_id,
        customer_mobile=body.customer_mobile,
        amount=body.amount,
        remaining=body.amount,
        advance_date=body.advance_date,
        pay_mode=body.pay_mode,
        notes=body.notes,
        created_by=int(payload["sub"]),
    )
    db.add(advance)
    await db.commit()
    return {"message": "Advance recorded", "advance_id": advance.id}


@router.post("/{advance_id}/allocate")
async def allocate_advance(
    advance_id: int,
    body:       AllocateRequest,
    payload:    dict          = Depends(get_current_user_payload),
    db:         AsyncSession  = Depends(get_db),
):
    """Allocate advance balance against one or more outstanding invoices."""
    tenant_id = payload["tenant_id"]
    advance   = await db.get(Advance, advance_id)
    if not advance or advance.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Advance not found")

    total_alloc = sum(a.allocated_amount for a in body.allocations)
    if total_alloc > advance.remaining:
        raise HTTPException(
            status_code=400,
            detail=f"Allocation ₹{total_alloc:,.0f} exceeds remaining advance ₹{advance.remaining:,.0f}",
        )

    for alloc in body.allocations:
        inv = await db.get(Invoice, alloc.invoice_id)
        if not inv or inv.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail=f"Invoice {alloc.invoice_id} not found")
        if alloc.allocated_amount > inv.outstanding:
            raise HTTPException(
                status_code=400,
                detail=f"Allocation ₹{alloc.allocated_amount:,.0f} exceeds invoice outstanding ₹{inv.outstanding:,.0f}",
            )

        db.add(AdvanceAllocation(
            tenant_id=tenant_id,
            advance_id=advance_id,
            invoice_id=alloc.invoice_id,
            allocated_amount=alloc.allocated_amount,
            created_by=int(payload["sub"]),
        ))

        inv.amount_paid += alloc.allocated_amount
        inv.outstanding -= alloc.allocated_amount
        if float(inv.outstanding) <= 0:
            inv.payment_status = PaymentStatus.paid
        else:
            inv.payment_status = PaymentStatus.partial

    advance.remaining -= total_alloc
    await db.commit()
    return {"message": "Advance allocated", "remaining": float(advance.remaining)}


@router.get("/")
async def list_advances(
    mobile:  Optional[str] = None,
    payload: dict          = Depends(get_current_user_payload),
    db:      AsyncSession  = Depends(get_db),
):
    """List advances. Returns customer_name for display (Issue 6)."""
    tenant_id = payload["tenant_id"]
    q = (
        select(Advance)
        .where(Advance.tenant_id == tenant_id)
        .order_by(Advance.advance_date.desc())
    )
    if mobile:
        q = q.where(Advance.customer_mobile == mobile)

    result   = await db.execute(q)
    advances = result.scalars().all()

    rows = []
    for a in advances:
        # Resolve customer name — prefer stored value, fall back to master
        cname = getattr(a, "customer_name", None)
        if not cname:
            cust  = await db.get(Customer, (a.customer_mobile, tenant_id))
            cname = cust.name if cust else "—"

        rows.append({
            "id":              a.id,
            "customer_mobile": a.customer_mobile,
            "customer_name":   cname,
            "amount":          float(a.amount),
            "remaining":       float(a.remaining),
            "date":            a.advance_date.isoformat(),
            "mode":            a.pay_mode.value,
            "notes":           a.notes or "",
        })

    return rows


# ── Edit Advance ────────────────────────────────────────────────────────────

class AdvanceUpdate(BaseModel):
    advance_date: Optional[date] = None
    amount:       Optional[float] = None
    pay_mode:     Optional[str]  = None
    notes:        Optional[str]  = None


@router.put("/{advance_id}")
async def update_advance(
    advance_id: int,
    body:       AdvanceUpdate,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """Edit an advance entry (date, amount, mode, notes)."""
    tenant_id = payload["tenant_id"]
    advance   = await db.get(Advance, advance_id)
    if not advance or advance.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Advance not found")

    if body.advance_date is not None:
        advance.advance_date = body.advance_date
    if body.amount is not None:
        new_amount = Decimal(str(body.amount))
        diff = new_amount - advance.amount
        advance.amount    = new_amount
        advance.remaining = max(Decimal("0"), advance.remaining + diff)
    if body.pay_mode is not None:
        advance.pay_mode = body.pay_mode   # stored as-is; DB enum handles validation
    if body.notes is not None:
        advance.notes = body.notes

    await db.commit()
    await db.refresh(advance)
    return {"message": "Advance updated", "id": advance.id}


# ── Cancel (Delete) Advance ─────────────────────────────────────────────────

@router.delete("/{advance_id}")
async def cancel_advance(
    advance_id: int,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """Cancel an advance. Reverses any invoice allocations already applied."""
    tenant_id = payload["tenant_id"]
    advance   = await db.get(Advance, advance_id)
    if not advance or advance.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Advance not found")

    alloc_result = await db.execute(
        select(AdvanceAllocation).where(AdvanceAllocation.advance_id == advance_id)
    )
    for alloc in alloc_result.scalars().all():
        invoice = await db.get(Invoice, alloc.invoice_id)
        if invoice and invoice.tenant_id == tenant_id:
            invoice.amount_paid = max(Decimal("0"), invoice.amount_paid - alloc.amount_allocated)
            invoice.outstanding = invoice.grand_total - invoice.amount_paid
            if invoice.amount_paid <= Decimal("0"):
                invoice.payment_status = PaymentStatus.unpaid
            elif invoice.outstanding > Decimal("0"):
                invoice.payment_status = PaymentStatus.partial
        await db.delete(alloc)

    await db.delete(advance)
    await db.commit()
    return {"message": "Advance cancelled and allocations reversed"}
