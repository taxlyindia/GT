"""
utils/fifo.py — Canonical FIFO stock engine
============================================
All stock mutations (sale, purchase, cancel, edit) go through helpers in this
module so the FIFO lot ledger is always in a consistent state.

FIFO RULES (v2 — canonical)
-----------------------------

Rule A — FIFO valuation applies to CREATE transactions only.
  Every new PURCHASE lot is stamped with its purchase_rate and becomes an
  IN-lot (lot_remaining = qty).  Every new SALE consumes IN-lots oldest-first,
  reducing lot_remaining on each consumed lot, and records the FIFO-weighted
  avg cost on the sale transaction.

Rule B — CANCEL = simple reversal at original invoice rate.
  • Cancel SALE   → restore stock (qty_on_hand + qty), record a positive
    adjustment at the ORIGINAL sale's FIFO avg cost.  The IN-lots that were
    consumed by the original sale are restored (lot_remaining += qty taken),
    walking lots in LIFO-of-consumption order so FIFO state is reconstructed.
    FIFO method does NOT re-run on the cancellation itself.
  • Cancel PURCHASE → zero the original lot's lot_remaining, record a negative
    adjustment at the ORIGINAL purchase rate, reduce qty_on_hand.
    FIFO method does NOT re-run on the cancellation itself.

Rule C — EDIT = alter original records in-place, no reverse/recreate.
  • Edit SALE   → find the original sale StockTransaction(s) and IN-lots
    consumed by this invoice, adjust qty/rate deltas directly.  qty_on_hand
    adjusted by the net delta.  One audit adjustment record written.
    FIFO does NOT re-run; the original FIFO cost basis is preserved for the
    unchanged portion and updated only for the changed qty/rate.
  • Edit PURCHASE → mutate the existing IN-lot (qty, rate, lot_remaining delta),
    adjust qty_on_hand by delta, write one audit adjustment record.
    FIFO does NOT re-run.

Rule D — FIFO valuation is maintained irrespective of cancel and edit.
  Cancellations and edits never trigger a new FIFO walk.  The FIFO lot ledger
  (lot_remaining values on IN-lots) is always kept consistent by the targeted
  mutations described in Rules B and C.

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
# SALE side — CREATE
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

    Rule A: FIFO walk applies on CREATE only.
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


# ─────────────────────────────────────────────────────────────
# SALE side — CANCEL (Rule B)
# ─────────────────────────────────────────────────────────────

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
    Cancel a previously recorded sale for `invoice_id` on `stock`.

    Rule B — Cancel = simple reversal at original invoice rate.
    ────────────────────────────────────────────────────────────
    1. Find the original sale transaction. Its purchase_rate is the FIFO
       weighted-avg cost captured at the time of the original sale.
    2. Restore lot_remaining on the IN-lots that the original sale consumed,
       walking in LIFO-of-consumption order (newest-consumed first) to
       reconstruct the exact pre-sale FIFO state.
    3. Increment stock.qty_on_hand.
    4. Record a positive StockTransaction (adjustment) at the ORIGINAL rate.

    FIFO does NOT re-run during cancellation — the rate is taken directly
    from the original sale record, not computed by a new FIFO walk.

    Returns the created reversal transaction.
    """
    # Step 1: Find original sale txn to capture the original FIFO avg rate
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

    # Step 2: Restore lot_remaining on consumed IN-lots (LIFO-of-consumption walk)
    # Walk in reverse order (most-recently-received lot first) to un-consume them —
    # this mirrors exactly how FIFO consumed them, reconstructing correct FIFO state.
    lots = await _get_fifo_in_lots(db, stock.id)
    qty_to_restore = qty
    for lot in reversed(lots):
        if qty_to_restore <= Decimal("0"):
            break
        # Capacity of this lot = original qty - current lot_remaining (i.e. how much was sold from it)
        capacity = lot.qty - (lot.lot_remaining or Decimal("0"))
        if capacity <= Decimal("0"):
            continue
        restore  = min(capacity, qty_to_restore)
        lot.lot_remaining = (lot.lot_remaining or Decimal("0")) + restore
        qty_to_restore   -= restore

    # Step 3: Update on-hand
    stock.qty_on_hand = stock.qty_on_hand + qty

    # Step 4: Record reversal at ORIGINAL sale rate — no new FIFO computation
    reversal_txn = StockTransaction(
        tenant_id     = tenant_id,
        stock_item_id = stock.id,
        txn_type      = StockTxnType.adjustment,
        qty           = qty,                     # positive = stock returning
        purchase_rate = original_fifo_rate,      # ORIGINAL rate, not re-computed
        invoice_id    = None,                    # not linked to the cancelled invoice
        txn_date      = reversal_date,
        lot_remaining = qty,                     # treated as a new IN-lot for future FIFO
        reason        = f"{reason_prefix} — Invoice ID {invoice_id}",
        created_by    = created_by,
    )
    db.add(reversal_txn)
    return reversal_txn


# ─────────────────────────────────────────────────────────────
# SALE side — EDIT (Rule C)
# ─────────────────────────────────────────────────────────────

async def fifo_edit_sale_inplace(
    db: AsyncSession,
    *,
    tenant_id: int,
    created_by: int,
    invoice_id: int,
    invoice_date: date,
    stock: "StockItem",
    old_qty: Decimal,
    new_qty: Decimal,
    item_description: str = "",
) -> "StockTransaction":
    """
    Edit a sale record IN-PLACE (no reverse/recreate).

    Rule C — Edit = alter original records, no reverse/recreate, no new FIFO walk.
    ────────────────────────────────────────────────────────────────────────────────
    • Find the original sale StockTransaction for this invoice.
    • Compute qty_delta = new_qty - old_qty.
    • If qty_delta > 0 (selling more): consume additional qty from IN-lots
      using FIFO walk for the DELTA ONLY.  Update lot_remaining accordingly.
    • If qty_delta < 0 (selling less): restore |qty_delta| to IN-lots (LIFO
      of consumption order) so FIFO state is reconstructed.
    • Mutate the original sale transaction: update qty and recalculate
      purchase_rate (FIFO avg) based on cumulative cost.
    • Adjust stock.qty_on_hand by qty_delta.
    • Write one audit StockTransaction of type=adjustment.

    FIFO does NOT fully re-run — only the incremental delta is processed
    through FIFO, preserving the cost basis already captured on the original
    sale record for the unchanged portion.

    Returns the created audit transaction.
    Raises ValueError if insufficient stock for a qty increase.
    """
    qty_delta = new_qty - old_qty

    # Find original sale txn
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
    original_sale_txn = sale_res.scalar_one_or_none()

    original_rate = (
        original_sale_txn.purchase_rate
        if original_sale_txn and original_sale_txn.purchase_rate
        else Decimal("0")
    )

    if qty_delta > Decimal("0"):
        # Selling MORE: FIFO walk for the delta only
        lots = await _get_fifo_in_lots(db, stock.id)
        qty_to_consume = qty_delta
        delta_value    = Decimal("0")

        for lot in lots:
            if qty_to_consume <= Decimal("0"):
                break
            available = lot.lot_remaining if lot.lot_remaining is not None else lot.qty
            if available <= Decimal("0"):
                continue
            take = min(available, qty_to_consume)
            rate = lot.purchase_rate or Decimal("0")
            delta_value    += take * rate
            qty_to_consume -= take
            current_rem = lot.lot_remaining if lot.lot_remaining is not None else lot.qty
            lot.lot_remaining = max(Decimal("0"), current_rem - take)

        if qty_to_consume > Decimal("0.001"):
            raise ValueError(
                f"Insufficient FIFO stock for edit of {item_description}: "
                f"need additional {float(qty_delta):.3f}, shortage {float(qty_to_consume):.3f}"
            )

        # Recalculate blended FIFO avg for the full new qty
        if original_sale_txn:
            old_total_cost = original_rate * old_qty
            new_total_cost = old_total_cost + delta_value
            new_fifo_avg = (
                (new_total_cost / new_qty).quantize(Decimal("0.0001"))
                if new_qty > 0 else Decimal("0")
            )
            original_sale_txn.qty           = -new_qty
            original_sale_txn.purchase_rate = new_fifo_avg
            original_sale_txn.txn_date      = invoice_date

    elif qty_delta < Decimal("0"):
        # Selling LESS: restore |qty_delta| to IN-lots (LIFO-of-consumption walk)
        qty_to_restore = abs(qty_delta)
        lots = await _get_fifo_in_lots(db, stock.id)
        for lot in reversed(lots):
            if qty_to_restore <= Decimal("0"):
                break
            capacity = lot.qty - (lot.lot_remaining or Decimal("0"))
            if capacity <= Decimal("0"):
                continue
            restore = min(capacity, qty_to_restore)
            lot.lot_remaining = (lot.lot_remaining or Decimal("0")) + restore
            qty_to_restore   -= restore

        # Recalculate FIFO avg for the reduced qty
        if original_sale_txn and new_qty > 0:
            # The remaining portion still carries the original FIFO avg
            original_sale_txn.qty           = -new_qty
            original_sale_txn.purchase_rate = original_rate  # avg rate unchanged
            original_sale_txn.txn_date      = invoice_date
        elif original_sale_txn and new_qty <= 0:
            original_sale_txn.qty           = Decimal("0")
            original_sale_txn.purchase_rate = Decimal("0")

    else:
        # No qty change — just update date if needed
        if original_sale_txn:
            original_sale_txn.txn_date = invoice_date

    # Adjust stock.qty_on_hand by net delta (negative delta = was selling less → stock goes up)
    stock.qty_on_hand = max(Decimal("0"), stock.qty_on_hand - qty_delta)

    # Write one audit record for traceability
    audit_txn = StockTransaction(
        tenant_id     = tenant_id,
        stock_item_id = stock.id,
        txn_type      = StockTxnType.adjustment,
        qty           = -qty_delta,              # negative = sold more, positive = sold less
        purchase_rate = original_rate,
        invoice_id    = None,
        txn_date      = invoice_date,
        lot_remaining = None,
        reason        = (
            f"Edit Sale — Invoice ID {invoice_id} "
            f"(old qty={float(old_qty):.3f} → new qty={float(new_qty):.3f})"
        ),
        created_by    = created_by,
    )
    db.add(audit_txn)
    return audit_txn


# ─────────────────────────────────────────────────────────────
# PURCHASE side — CREATE
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

    Rule A: Each purchase creates an IN-lot with its exact purchase_rate.
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


# ─────────────────────────────────────────────────────────────
# PURCHASE side — CANCEL (Rule B)
# ─────────────────────────────────────────────────────────────

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
    Cancel a purchase IN-lot for `stock`.

    Rule B — Cancel = simple reversal at ORIGINAL purchase rate.
    ────────────────────────────────────────────────────────────
    1. Find the original purchase lot for this invoice.
    2. Zero out lot_remaining (no future FIFO sale can consume a cancelled lot).
    3. Record a negative StockTransaction at the ORIGINAL purchase_rate.
    4. Clamp qty_on_hand at 0 (some units may already have been sold).

    FIFO does NOT re-run during cancellation — rate taken from original record.

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

    # Record reversal at ORIGINAL purchase rate — no new FIFO computation
    reversal_txn = StockTransaction(
        tenant_id     = tenant_id,
        stock_item_id = stock.id,
        txn_type      = StockTxnType.adjustment,
        qty           = -qty,                    # negative = outbound
        purchase_rate = original_rate,           # ORIGINAL rate, not re-computed
        invoice_id    = None,
        txn_date      = reversal_date,
        lot_remaining = None,
        reason        = f"Cancellation — Supplier Invoice {invoice_no}",
        created_by    = created_by,
    )
    db.add(reversal_txn)
    return reversal_txn


# ─────────────────────────────────────────────────────────────
# PURCHASE side — EDIT (Rule C)
# ─────────────────────────────────────────────────────────────

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

    Rule C — Edit = alter original records, no reverse/recreate, no new FIFO walk.
    ────────────────────────────────────────────────────────────────────────────────
    • Mutates the existing IN-lot record (qty, purchase_rate, lot_remaining).
    • Only the unsold portion (lot_remaining) is adjusted for the qty delta;
      the sold portion's FIFO cost has already been captured on sale transactions
      and is NOT retroactively changed.
    • Adjusts stock.qty_on_hand by the delta.
    • Records one audit StockTransaction of type=adjustment.

    FIFO does NOT re-run — FIFO valuation is maintained because the original
    lot's cost is updated in place, and future FIFO sales will pick up the new rate.

    Returns the created audit transaction.
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
        # Adjust only the unsold remainder; already-sold qty's cost basis is fixed.
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
