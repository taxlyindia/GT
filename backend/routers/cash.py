# routers/cash.py — Cash Register entries
from datetime import date
from decimal import Decimal
from typing import Optional
from fastapi import HTTPException
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models import CashEntry, CashEntryType
from utils.auth import get_tenant_payload as get_current_user_payload

router = APIRouter()

class CashEntryCreate(BaseModel):
    entry_date:     date
    entry_type:     str
    amount:         Decimal
    description:    str
    invoice_id:     Optional[int] = None
    bank_reference: Optional[str] = None

@router.post("/", status_code=201)
async def create_cash_entry(
    body:    CashEntryCreate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    entry = CashEntry(
        tenant_id=payload["tenant_id"],
        entry_date=body.entry_date,
        entry_type=CashEntryType(body.entry_type),
        amount=body.amount,
        description=body.description,
        invoice_id=body.invoice_id,
        bank_reference=body.bank_reference,
        created_by=int(payload["sub"]),
    )
    db.add(entry)
    await db.commit()
    return {"message": "Cash entry recorded"}

@router.get("/")
async def list_cash_entries(
    from_date: Optional[date] = None,
    to_date:   Optional[date] = None,
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    q = select(CashEntry).where(CashEntry.tenant_id == payload["tenant_id"]).order_by(CashEntry.entry_date.desc())
    if from_date: q = q.where(CashEntry.entry_date >= from_date)
    if to_date:   q = q.where(CashEntry.entry_date <= to_date)
    result = await db.execute(q)
    entries = result.scalars().all()
    return [{"id": e.id, "date": e.entry_date.isoformat(), "type": e.entry_type.value,
             "description": e.description, "amount": float(e.amount),
             "bank_reference": e.bank_reference} for e in entries]


# ── Edit / Delete Cash Entry ─────────────────────────────────────────────────

class CashEntryUpdate(BaseModel):
    entry_date:     Optional[date]    = None
    entry_type:     Optional[str]     = None
    amount:         Optional[Decimal] = None
    description:    Optional[str]     = None
    bank_reference: Optional[str]     = None


@router.put("/{entry_id}")
async def edit_cash_entry(
    entry_id: int,
    body:     CashEntryUpdate,
    payload:  dict         = Depends(get_current_user_payload),
    db:       AsyncSession = Depends(get_db),
):
    """
    Edit all fields of an existing cash register entry.
    Added per Improvement document request.
    """
    tenant_id = payload["tenant_id"]
    entry     = await db.get(CashEntry, entry_id)
    if not entry or entry.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Cash entry not found")

    if body.entry_date     is not None: entry.entry_date     = body.entry_date
    if body.entry_type     is not None: entry.entry_type     = CashEntryType(body.entry_type)
    if body.amount         is not None: entry.amount         = body.amount
    if body.description    is not None: entry.description    = body.description
    if body.bank_reference is not None: entry.bank_reference = body.bank_reference

    await db.commit()
    return {"message": "Cash entry updated", "entry_id": entry_id}


@router.delete("/{entry_id}")
async def delete_cash_entry(
    entry_id: int,
    payload:  dict         = Depends(get_current_user_payload),
    db:       AsyncSession = Depends(get_db),
):
    """Delete a cash register entry."""
    tenant_id = payload["tenant_id"]
    entry     = await db.get(CashEntry, entry_id)
    if not entry or entry.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Cash entry not found")
    await db.delete(entry)
    await db.commit()
    return {"message": "Cash entry deleted"}
