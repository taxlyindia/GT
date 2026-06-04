# routers/stock.py — Stock management with FIFO lot tracking
from datetime import date
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models import StockItem, StockTransaction, StockTxnType
from utils.auth import get_tenant_payload as get_current_user_payload

router = APIRouter()

class StockCreate(BaseModel):
    category:      str
    purity:        Optional[str] = None
    description:   str
    unit:          str
    initial_qty:   Decimal = Decimal("0")
    purchase_rate: Optional[Decimal] = None

class StockAdjust(BaseModel):
    qty_change:    Decimal   # positive = in, negative = out
    purchase_rate: Optional[Decimal] = None
    reason:        Optional[str] = None
    txn_date:      date = Field(default_factory=date.today)  # evaluated per-request

class StockUpdate(BaseModel):
    """Fields that can be edited on an existing stock item."""
    description: Optional[str] = None
    category:    Optional[str] = None
    purity:      Optional[str] = None
    unit:        Optional[str] = None

@router.post("/", status_code=201)
async def add_stock_item(
    body:    StockCreate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    stock = StockItem(
        tenant_id=payload["tenant_id"],
        category=body.category,
        purity=body.purity,
        description=body.description,
        unit=body.unit,
        qty_on_hand=body.initial_qty,
        fifo_enabled=body.category != "Polish Charges",
    )
    db.add(stock)
    await db.flush()

    if body.initial_qty > 0:
        db.add(StockTransaction(
            tenant_id=payload["tenant_id"],
            stock_item_id=stock.id,
            txn_type=StockTxnType.opening,
            qty=body.initial_qty,
            purchase_rate=body.purchase_rate,
            txn_date=date.today(),
            lot_remaining=body.initial_qty,
        ))

    await db.commit()
    return {"message": "Stock item added", "stock_id": stock.id}

@router.post("/{stock_id}/adjust")
async def adjust_stock(
    stock_id: int,
    body:     StockAdjust,
    payload:  dict         = Depends(get_current_user_payload),
    db:       AsyncSession = Depends(get_db),
):
    stock = await db.get(StockItem, stock_id)
    if not stock or stock.tenant_id != payload["tenant_id"]:
        raise HTTPException(status_code=404, detail="Stock item not found")

    # Validate BEFORE mutating — avoids dirty session state if we raise
    new_qty = stock.qty_on_hand + body.qty_change
    if float(new_qty) < 0:
        raise HTTPException(status_code=400, detail="Insufficient stock quantity")

    stock.qty_on_hand = new_qty

    is_purchase = body.qty_change > 0
    txn_type = StockTxnType.purchase if is_purchase else StockTxnType.adjustment

    # For purchases (positive qty_change), set a reason that _parse_reason can
    # identify as a purchase IN so it appears correctly in Stock Ledger & FIFO report.
    reason = body.reason
    if is_purchase and not reason:
        reason = f"Purchase — Stock IN {body.txn_date}"

    db.add(StockTransaction(
        tenant_id=payload["tenant_id"],
        stock_item_id=stock_id,
        txn_type=txn_type,
        qty=body.qty_change,
        purchase_rate=body.purchase_rate,
        txn_date=body.txn_date,
        reason=reason,
        lot_remaining=body.qty_change if is_purchase else None,
    ))
    await db.commit()
    return {"message": "Stock adjusted", "qty_on_hand": float(stock.qty_on_hand)}

@router.put("/{stock_id}")
async def update_stock_item(
    stock_id: int,
    body:     StockUpdate,
    payload:  dict         = Depends(get_current_user_payload),
    db:       AsyncSession = Depends(get_db),
):
    """
    Edit stock item details (description, category, purity, unit).
    Does NOT change qty_on_hand — use /adjust for that.
    """
    stock = await db.get(StockItem, stock_id)
    if not stock or stock.tenant_id != payload["tenant_id"]:
        raise HTTPException(status_code=404, detail="Stock item not found")
    if not stock.is_active:
        raise HTTPException(status_code=400, detail="Stock item has been deleted")

    if body.description is not None:
        if not body.description.strip():
            raise HTTPException(status_code=422, detail="Description cannot be empty")
        stock.description = body.description.strip()
    if body.category is not None:
        stock.category    = body.category
        stock.fifo_enabled = body.category != "Polish Charges"
    if body.purity is not None:
        stock.purity      = body.purity or None   # empty string → NULL
    if body.unit is not None:
        stock.unit        = body.unit

    await db.commit()
    return {
        "message":     "Stock item updated",
        "stock_id":    stock.id,
        "description": stock.description,
        "category":    stock.category.value,
        "purity":      stock.purity,
        "unit":        stock.unit.value,
    }

@router.delete("/{stock_id}")
async def delete_stock_item(
    stock_id: int,
    payload:  dict         = Depends(get_current_user_payload),
    db:       AsyncSession = Depends(get_db),
):
    """
    Soft-delete a stock item (sets is_active=False).
    The item is hidden from inventory but all historical transactions are preserved.
    Cannot delete if qty_on_hand > 0 (must zero out via adjustment first).
    """
    stock = await db.get(StockItem, stock_id)
    if not stock or stock.tenant_id != payload["tenant_id"]:
        raise HTTPException(status_code=404, detail="Stock item not found")
    if not stock.is_active:
        raise HTTPException(status_code=400, detail="Stock item is already deleted")
    if stock.qty_on_hand > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete: item still has {float(stock.qty_on_hand):.3f} units on hand. "
                   "Adjust qty to zero first."
        )

    stock.is_active = False
    await db.commit()
    return {"message": "Stock item deleted", "stock_id": stock_id}

@router.get("/summary")
async def stock_summary(
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """Summary of stock by category — used by dashboard chart."""
    from sqlalchemy import func as sqlfunc
    tid = payload["tenant_id"]
    result = await db.execute(
        select(StockItem.category, sqlfunc.sum(StockItem.qty_on_hand).label("total"))
        .where(StockItem.tenant_id == tid, StockItem.is_active == True)
        .group_by(StockItem.category)
    )
    rows = result.all()
    return [{"category": r.category.value if hasattr(r.category,'value') else str(r.category),
             "total_qty": float(r.total or 0)} for r in rows]


@router.get("/")
async def list_stock(
    q:       Optional[str] = None,
    limit:   Optional[int] = None,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    stmt = select(StockItem).where(StockItem.tenant_id == payload["tenant_id"], StockItem.is_active == True)
    if q:
        stmt = stmt.where(StockItem.description.ilike(f"%{q}%"))
    stmt = stmt.order_by(StockItem.description)
    if limit:
        stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    stocks = result.scalars().all()
    return [{"id": s.id, "category": s.category.value, "purity": s.purity or "—",
             "description": s.description, "unit": s.unit.value,
             "qty_on_hand": float(s.qty_on_hand)} for s in stocks]


# ── Purity options for invoice purity dropdown ─────────────────────────────

@router.get("/purity-options")
async def get_purity_options(
    category: str,
    payload:  dict         = Depends(get_current_user_payload),
    db:       AsyncSession = Depends(get_db),
):
    """
    Return distinct purity values available in stock for a given category.
    Used to populate the purity dropdown in the invoice form.
    Also returns qty_on_hand so the frontend can warn about low/zero stock.
    """
    from sqlalchemy import select as sa_select
    result = await db.execute(
        sa_select(StockItem.purity, StockItem.qty_on_hand, StockItem.id)
        .where(
            StockItem.tenant_id == payload["tenant_id"],
            StockItem.category  == category,
            StockItem.is_active == True,
        )
        .order_by(StockItem.purity)
    )
    rows = result.all()
    return [
        {
            "purity":       r.purity,
            "qty_on_hand":  float(r.qty_on_hand),
            "stock_item_id": r.id,
        }
        for r in rows
    ]
