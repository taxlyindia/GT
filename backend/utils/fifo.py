"""
utils/fifo.py — Canonical FIFO stock engine
============================================
All stock mutations (sale, purchase, cancel, edit) go through helpers in this
module so the FIFO lot ledger is always in a consistent state.

FIFO rules enforced here
-------------------------
* Every PURCHASE or OPENING lot has:
    txn_type in (purchase, opening, adjustment with qty > 0)
    lot_remaining  = remaining qty available for future FIFO consumption
    purchase_rate  = cost basis per unit

* Every SALE lot has:
    txn_type = sale
    qty < 0  (negative = outbound)
    purchase_rate = FIFO-weighted avg cost of units consumed (for COGS reporting)
    lot_remaining = None  (sales don't have their own lot balance)

* Cancellation of a SALE:
    - Finds every IN-lot that was consumed by the original sale (oldest-first walk)
    - Restores lot_remaining on each consumed IN-lot by the amount it consumed
    - Adds a new StockTransaction: txn_type=sale_reversal(*), qty=+original_qty,
      purchase_rate=original_fifo_avg, reason references original sale txn
    - Increments stock.qty_on_hand

  (*) We re-use StockTxnType.adjustment for the reversal record; the reason
      string distinguishes it from a manual adjustment.

* Cancellation of a PURCHASE:
    - Finds the original IN-lot for this invoice
    - Clamps its lot_remaining to zero (no future FIFO consumption)
    - Adds a StockTransaction: txn_type=adjustment, qty=-original_qty,
      purchase_rate=original_purchase_rate
    - Decrements stock.qty_on_hand (clamped at 0)

* Edit of a SALE:
    1. Full sale-cancellation of old items (restores lots, adds reversal record)
    2. Full sale-deduction of new items (consumes lots FIFO, adds sale record)

* Edit of a PURCHASE:
    - Does NOT reverse+recreate the lot.
    - Mutates the existing IN-lot record (qty, purchase_rate, lot_remaining).
    - Adjusts stock.qty_on_hand by the delta.
    - Records a single StockTransaction of type=adjustment for audit trail.

Polish Charges items are skipped everywhere — they are calculation-only.
"""

from __future__ import annotations
from datetime import date
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

if TYPE_CHECKING:
    from models import StockItem, StockTransaction as STxn

# Import models at call time to avoid circular imports
from models import StockItem, StockTransaction, StockTxnType


# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────

def _is_polish(category) -> bool:
    val = category.value if hasattr(category, "value") else str(category)
    return val == "Polish Charges"


async def _find_stock_item(
    db: AsyncSession,
    tenant_id: int,
    category,
    purity: Optional[str],
    unit,
) -> Optional["StockItem"]:
    """
    Locate the StockItem for (tenant, category, purity, unit).
    Purity=None matches only NULL purity rows (exact match semantics for
    purchase/cancel; sale side uses the fuzzy _find_stock helper in invoices.py).
    """
    from sqlalchemy import or_
    stmt = (
        select(StockItem)
        .where(
            StockItem.tenant_id == tenant_id,
            StockItem.category  == category,
            StockItem.is_active == True,
        )
    )
    if purity:
        stmt = stmt.where(StockItem.purity == purity)
    else:
        stmt = stmt.where(StockItem.purity.is_(None))
    if unit:
        stmt = stmt.where(StockItem.unit == unit)
    result = await db.execute(stmt.limit(1))
    return result.scalar_one_or_none()


async def _get_fifo_in_lots(
    db: AsyncSession,
    stock_item_id: int,
) -> list["StockTransaction"]:
    """
    Return all IN-lots for a stock item in FIFO order (oldest txn_date, then id).
    IN-lots: qty > 0, txn_type in purchase/opening/adjustment.
    """
    result = await db.execute(
        select(StockTransaction)
        .where(
            StockTransaction.stock_item_id == stock_item_id,
            StockTransaction.qty > Decimal("0"),
            StockTransaction.txn_type.in_([
                StockTxnType.purchase,
                StockTxnType.opening,
                StockTxnType.adjustment,
            ]),
        )
        .order_by(StockTransaction.txn_date, StockTransaction.id)
    )
    return result.scalars().all()


# ─────────────────────────────────────────────────────────────
# SALE side
# ─────────────────────────────────────────────────────────────

async def fifo_deduct_sale(
    db: AsyncSession,
    *,
    tenant_id: int,
    created_by: int,
    invoice_id: int,
    invoice_date: date,
    stock: "StockItem",
    qty: Decimal,
    item_description: str = "",
) -> "StockTransaction":
    """
    Consume `qty` from FIFO IN-lots for `stock`, reduce lot_remaining
    on each consumed lot, update stock.qty_on_hand, and record one
    sale StockTransaction with the FIFO-weighted avg purchase_rate.

    Returns the created sale transaction.
    Raises ValueError if insufficient stock (caller should pre-check).
    """
    lots = await _get_fifo_in_lots(db, stock.id)

    qty_to_consume  = qty
    weighted_value  = Decimal("0")
    consumed_detail: list[tuple["StockTransaction", Decimal]] = []

    for lot in lots:
        if qty_to_consume <= Decimal("0"):
            break
        available = lot.lot_remaining if lot.lot_remaining is not None else lot.qty
        if available <= Decimal("0"):
            continue
        take = min(available, qty_to_consume)
        rate = lot.purchase_rate or Decimal("0")
        weighted_value  += take * rate
        qty_to_consume  -= take
        consumed_detail.append((lot, take))

    if qty_to_consume > Decimal("0.001"):        # allow 1mg tolerance
        raise ValueError(
            f"Insufficient FIFO stock for {item_description}: "
            f"need {float(qty):.3f}, shortage {float(qty_to_consume):.3f}"
        )

    # Commit lot_remaining reductions
    for lot, taken in consumed_detail:
        current_rem = lot.lot_remaining if lot.lot_remaining is not None else lot.qty
        lot.lot_remaining = max(Decimal("0"), current_rem - taken)

    fifo_avg = (
        (weighted_value / qty).quantize(Decimal("0.0001"))
        if qty > 0 and weighted_value > 0
        else Decimal("0")
    )

    # Update on-hand
    stock.qty_on_hand = max(Decimal("0"), stock.qty_on_hand - qty)

    # Record sale transaction
    sale_txn = StockTransaction(
        tenant_id     = tenant_id,
        stock_item_id = stock.id,
        txn_type      = StockTxnType.sale,
        qty           = -qty,                    # negative = outbound
        purchase_rate = fifo_avg,                # FIFO cost basis captured
        invoice_id    = invoice_id,
        txn_date      = invoice_date,
        lot_remaining = None,
        reason        = f"Sale — Invoice ID {invoice_id}",
        created_by    = created_by,
    )
    db.add(sale_txn)
    return sale_txn


async def fifo_reverse_sale(
    db: AsyncSession,
    *,
    tenant_id: int,
    created_by: int,
    invoice_id: int,
    reversal_date: date,
    stock: "StockItem",
    qty: Decimal,
    reason_prefix: str = "Cancelled",
) -> "StockTransaction":
    """
    Reverse a previously recorded sale for `invoice_id` on `stock`.

    Strategy
    --------
    1. Find the original sale transaction (txn_type=sale, invoice_id=invoice_id,
       stock_item_id=stock.id). Its purchase_rate is the FIFO-weighted avg we captured.
    2. Walk FIFO IN-lots in REVERSE order (newest-first) to un-consume them —
       this restores lot_remaining on the lots that were originally consumed,
       in exact LIFO-of-consumption order which reconstructs the correct FIFO state.
    3. Add a StockTransaction(type=adjustment, qty=+qty) as the reversal record.
    4. Increment stock.qty_on_hand.

    Returns the created reversal transaction.
    """
    # 1. Find original sale txn
    sale_res = await db.execute(
        select(StockTransaction)
        .where(
            StockTransaction.stock_item_id == stock.id,
            StockTransaction.txn_type      == StockTxnType.sale,
            StockTransaction.invoice_id    == invoice_id,
        )
        .order_by(StockTransaction.id.desc())
        .limit(1)
    )
    sale_txn = sale_res.scalar_one_or_none()
    original_fifo_rate = (
        sale_txn.purchase_rate if sale_txn and sale_txn.purchase_rate
        else Decimal("0")
    )

    # 2. Restore lot_remaining on consumed IN-lots (reverse FIFO walk)
    #    We restore qty_needed from newest in-lot backwards — mirrors exactly
    #    how FIFO deduction consumed them.
    lots = await _get_fifo_in_lots(db, stock.id)
    # Reverse-iterate: most-recently-received lot first
    qty_to_restore = qty
    for lot in reversed(lots):
        if qty_to_restore <= Decimal("0"):
            break
        # Capacity of this lot = original qty - current lot_remaining
        capacity = lot.qty - (lot.lot_remaining or Decimal("0"))
        if capacity <= Decimal("0"):
            continue
        restore  = min(capacity, qty_to_restore)
        lot.lot_remaining = (lot.lot_remaining or Decimal("0")) + restore
        qty_to_restore   -= restore

    # 3. Update on-hand
    stock.qty_on_hand = stock.qty_on_hand + qty

    # 4. Record reversal transaction
    reversal_txn = StockTransaction(
        tenant_id     = tenant_id,
        stock_item_id = stock.id,
        txn_type      = StockTxnType.adjustment,
        qty           = qty,                     # positive = stock returning
        purchase_rate = original_fifo_rate,      # same cost basis as original sale
        invoice_id    = None,                    # not linked to invoice (it's cancelled)
        txn_date      = reversal_date,
        lot_remaining = qty,                     # treated as a new IN-lot for future FIFO
        reason        = f"{reason_prefix} — Invoice ID {invoice_id}",
        created_by    = created_by,
    )
    db.add(reversal_txn)
    return reversal_txn


# ─────────────────────────────────────────────────────────────
# PURCHASE side
# ─────────────────────────────────────────────────────────────

async def fifo_add_purchase(
    db: AsyncSession,
    *,
    tenant_id: int,
    created_by: Optional[int],
    invoice_no: str,
    invoice_date: date,
    stock: "StockItem",
    qty: Decimal,
    purchase_rate: Decimal,
) -> "StockTransaction":
    """
    Record a new purchase IN-lot for `stock`, update qty_on_hand.
    Returns the created StockTransaction.
    """
    stock.qty_on_hand = stock.qty_on_hand + qty

    txn = StockTransaction(
        tenant_id     = tenant_id,
        stock_item_id = stock.id,
        txn_type      = StockTxnType.purchase,
        qty           = qty,
        purchase_rate = purchase_rate,
        invoice_id    = None,
        txn_date      = invoice_date,
        lot_remaining = qty,                     # full lot available for FIFO
        reason        = f"Purchase — Supplier Invoice {invoice_no}",
        created_by    = created_by,
    )
    db.add(txn)
    return txn


async def fifo_reverse_purchase(
    db: AsyncSession,
    *,
    tenant_id: int,
    created_by: Optional[int],
    invoice_no: str,
    reversal_date: date,
    stock: "StockItem",
    qty: Decimal,
    original_txn: Optional["StockTransaction"] = None,
) -> "StockTransaction":
    """
    Reverse (cancel) a purchase IN-lot for `stock`.

    1. Zeros out lot_remaining on the original purchase lot so no future
       FIFO sale can accidentally consume a cancelled lot.
    2. Records a negative StockTransaction at the ORIGINAL purchase_rate.
    3. Clamps qty_on_hand at 0 (stock may have been partially sold already).

    Returns the created reversal transaction.
    """
    # Resolve original purchase transaction
    if original_txn is None:
        res = await db.execute(
            select(StockTransaction)
            .where(
                StockTransaction.stock_item_id == stock.id,
                StockTransaction.txn_type      == StockTxnType.purchase,
                StockTransaction.reason        == f"Purchase — Supplier Invoice {invoice_no}",
            )
            .order_by(StockTransaction.id.desc())
            .limit(1)
        )
        original_txn = res.scalar_one_or_none()

    original_rate = (
        original_txn.purchase_rate
        if original_txn and original_txn.purchase_rate
        else Decimal("0")
    )

    # Zero the original lot so FIFO won't consume it
    if original_txn:
        original_txn.lot_remaining = Decimal("0")

    # Reduce qty_on_hand (clamp at 0 — some units may already have been sold)
    stock.qty_on_hand = max(Decimal("0"), stock.qty_on_hand - qty)

    reversal_txn = StockTransaction(
        tenant_id     = tenant_id,
        stock_item_id = stock.id,
        txn_type      = StockTxnType.adjustment,
        qty           = -qty,                    # negative = outbound
        purchase_rate = original_rate,
        invoice_id    = None,
        txn_date      = reversal_date,
        lot_remaining = None,
        reason        = f"Cancellation — Supplier Invoice {invoice_no}",
        created_by    = created_by,
    )
    db.add(reversal_txn)
    return reversal_txn


async def fifo_edit_purchase_lot(
    db: AsyncSession,
    *,
    tenant_id: int,
    created_by: Optional[int],
    invoice_no: str,
    invoice_date: date,
    stock: "StockItem",
    old_qty: Decimal,
    new_qty: Decimal,
    old_rate: Decimal,
    new_rate: Decimal,
    original_txn: Optional["StockTransaction"] = None,
) -> "StockTransaction":
    """
    Edit an existing purchase IN-lot IN-PLACE (no reverse/recreate).

    Purchase edit rule: modify the lot record directly, adjust qty_on_hand
    by the delta, and write one audit StockTransaction.

    If the lot was partially sold already, we only adjust the unsold portion
    (lot_remaining). The sold portion's FIFO cost has already been captured
    on the sale transaction — we don't retroactively change it.
    """
    if original_txn is None:
        res = await db.execute(
            select(StockTransaction)
            .where(
                StockTransaction.stock_item_id == stock.id,
                StockTransaction.txn_type      == StockTxnType.purchase,
                StockTransaction.reason        == f"Purchase — Supplier Invoice {invoice_no}",
            )
            .order_by(StockTransaction.id.desc())
            .limit(1)
        )
        original_txn = res.scalar_one_or_none()

    qty_delta = new_qty - old_qty

    if original_txn:
        # Update the original lot: adjust qty and the unsold lot_remaining
        old_lot_remaining = original_txn.lot_remaining if original_txn.lot_remaining is not None else old_qty
        new_lot_remaining = max(Decimal("0"), old_lot_remaining + qty_delta)
        original_txn.qty           = new_qty
        original_txn.purchase_rate = new_rate
        original_txn.lot_remaining = new_lot_remaining
        original_txn.txn_date      = invoice_date

    # Adjust on-hand by delta
    stock.qty_on_hand = max(Decimal("0"), stock.qty_on_hand + qty_delta)

    # Audit record
    audit_txn = StockTransaction(
        tenant_id     = tenant_id,
        stock_item_id = stock.id,
        txn_type      = StockTxnType.adjustment,
        qty           = qty_delta,
        purchase_rate = new_rate,
        invoice_id    = None,
        txn_date      = invoice_date,
        lot_remaining = None,
        reason        = f"Edit — Supplier Invoice {invoice_no} (old qty={float(old_qty):.3f} rate={float(old_rate):.2f})",
        created_by    = created_by,
    )
    db.add(audit_txn)
    return audit_txn
