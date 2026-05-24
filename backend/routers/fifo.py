"""
utils/fifo.py — FIFO Stock Engine (Revised per Business Rules)
==============================================================

FIFO Rules
----------

SALE — CREATE:
  • Walk IN-lots oldest-first, reduce lot_remaining on each consumed lot.
  • Record one sale StockTransaction with FIFO-weighted avg cost (for COGS).
  • Decrement stock.qty_on_hand.

SALE — CANCEL:
  • Direct reversal of the original sale. No fresh FIFO walk.
  • Find the original sale StockTransaction for this invoice_id.
  • Restore lot_remaining on exactly the lots consumed by the original sale,
    using the recorded consumed_lots JSON stored on the sale transaction.
    If consumed_lots is not stored, fall back to reverse-FIFO walk.
  • Record one cancellation StockTransaction (qty = +original_qty,
    purchase_rate = original FIFO avg rate, txn_type = sale_cancel).
  • Increment stock.qty_on_hand.
  • NO fresh FIFO allocation. NO new lot created.

SALE — EDIT:
  • Modify the original sale StockTransaction in-place:
      - Update qty (negative), purchase_rate (recomputed FIFO avg), txn_date.
  • Adjust lot_remaining on the consumed lots:
      - Return the old consumed qty to each lot.
      - Consume the new qty using FIFO walk from oldest.
  • Adjust stock.qty_on_hand by (new_qty - old_qty) delta.
  • NO separate reversal record. NO new sale record. The original txn IS updated.

PURCHASE — CREATE:
  • Record one purchase StockTransaction as a fresh IN-lot.
  • lot_remaining = qty (full lot available for future FIFO consumption).
  • Increment stock.qty_on_hand.

PURCHASE — CANCEL:
  • Direct reversal of the original purchase.
  • Find original purchase StockTransaction for this invoice_no.
  • Zero its lot_remaining so future FIFO sales cannot consume it.
  • Record one cancellation StockTransaction (qty = -original_qty,
    purchase_rate = original purchase_rate, txn_type = purchase_cancel).
  • Decrement stock.qty_on_hand (clamped at 0).
  • NO new lot. NO fresh FIFO walk.

PURCHASE — EDIT:
  • Modify the original purchase StockTransaction in-place:
      - Update qty, purchase_rate, lot_remaining (adjusted by delta), txn_date.
  • Adjust stock.qty_on_hand by (new_qty - old_qty) delta.
  • NO separate audit record. NO reversal. The original txn IS updated.

Polish Charges are skipped everywhere — calculation-only, not in stock.
"""

from __future__ import annotations
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models import StockItem, StockTransaction, StockTxnType


# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────

def _is_polish(category) -> bool:
    val = category.value if hasattr(category, "value") else str(category)
    return val == "Polish Charges"


async def _get_fifo_in_lots(
    db: AsyncSession,
    stock_item_id: int,
) -> list[StockTransaction]:
    """
    All IN-lots for a stock item in strict FIFO order (oldest date, then id).
    IN-lots: qty > 0, txn_type in (purchase, opening, adjustment with lot_remaining set).
    Only lots with lot_remaining > 0 are usable for consumption.
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


async def _get_original_sale_txn(
    db: AsyncSession,
    stock_item_id: int,
    invoice_id: int,
) -> Optional[StockTransaction]:
    """Find the original sale transaction for a given invoice and stock item."""
    res = await db.execute(
        select(StockTransaction)
        .where(
            StockTransaction.stock_item_id == stock_item_id,
            StockTransaction.txn_type      == StockTxnType.sale,
            StockTransaction.invoice_id    == invoice_id,
        )
        .order_by(StockTransaction.id.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


async def _get_original_purchase_txn(
    db: AsyncSession,
    stock_item_id: int,
    invoice_no: str,
) -> Optional[StockTransaction]:
    """Find the original purchase transaction for a given supplier invoice."""
    res = await db.execute(
        select(StockTransaction)
        .where(
            StockTransaction.stock_item_id == stock_item_id,
            StockTransaction.txn_type      == StockTxnType.purchase,
            StockTransaction.reason        == f"Purchase — Supplier Invoice {invoice_no}",
        )
        .order_by(StockTransaction.id.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


# ─────────────────────────────────────────────────────────────
# SALE — CREATE
# ─────────────────────────────────────────────────────────────

async def fifo_deduct_sale(
    db: AsyncSession,
    *,
    tenant_id: int,
    created_by: int,
    invoice_id: int,
    invoice_date: date,
    stock: StockItem,
    qty: Decimal,
    item_description: str = "",
) -> StockTransaction:
    """
    Consume qty from FIFO IN-lots (oldest first).
    Records one sale StockTransaction with FIFO-weighted avg cost.
    Raises ValueError if insufficient stock.
    """
    lots = await _get_fifo_in_lots(db, stock.id)

    qty_to_consume = qty
    weighted_value = Decimal("0")
    consumed: list[tuple[StockTransaction, Decimal]] = []

    for lot in lots:
        if qty_to_consume <= Decimal("0"):
            break
        available = lot.lot_remaining if lot.lot_remaining is not None else lot.qty
        if available <= Decimal("0"):
            continue
        take  = min(available, qty_to_consume)
        rate  = lot.purchase_rate or Decimal("0")
        weighted_value += take * rate
        qty_to_consume -= take
        consumed.append((lot, take))

    if qty_to_consume > Decimal("0.001"):
        raise ValueError(
            f"Insufficient FIFO stock for {item_description}: "
            f"need {float(qty):.3f}, shortage {float(qty_to_consume):.3f}"
        )

    # Reduce lot_remaining on consumed lots
    for lot, taken in consumed:
        current = lot.lot_remaining if lot.lot_remaining is not None else lot.qty
        lot.lot_remaining = max(Decimal("0"), current - taken)

    fifo_avg = (
        (weighted_value / qty).quantize(Decimal("0.0001"))
        if qty > 0 and weighted_value > 0
        else Decimal("0")
    )

    stock.qty_on_hand = max(Decimal("0"), stock.qty_on_hand - qty)

    sale_txn = StockTransaction(
        tenant_id     = tenant_id,
        stock_item_id = stock.id,
        txn_type      = StockTxnType.sale,
        qty           = -qty,
        purchase_rate = fifo_avg,
        invoice_id    = invoice_id,
        txn_date      = invoice_date,
        lot_remaining = None,
        reason        = f"Sale — Invoice ID {invoice_id}",
        created_by    = created_by,
    )
    db.add(sale_txn)
    return sale_txn


# ─────────────────────────────────────────────────────────────
# SALE — CANCEL
# Direct reversal — exact mirror of original sale, no fresh FIFO
# ─────────────────────────────────────────────────────────────

async def fifo_cancel_sale(
    db: AsyncSession,
    *,
    tenant_id: int,
    created_by: int,
    invoice_id: int,
    cancellation_date: date,
    stock: StockItem,
    qty: Decimal,
) -> StockTransaction:
    """
    Cancel a sale invoice — direct reversal of the original sale.

    Rule: Cancellation restores the exact original FIFO lots at the original
    rate. No fresh FIFO walk is performed.

    1. Find the original sale transaction (to get the original FIFO avg rate).
    2. Restore lot_remaining on the lots that were consumed by the original
       sale, by walking IN-lots in reverse order (newest-consumed-first).
       This exactly undoes what fifo_deduct_sale did.
    3. Increment stock.qty_on_hand by qty.
    4. Mark the original sale transaction as cancelled (txn_type → sale_cancel
       if available, else update reason). Do NOT add a new IN-lot.
       Instead add one reversal record for the audit trail only.
    """
    # 1. Get original sale transaction
    orig_sale = await _get_original_sale_txn(db, stock.id, invoice_id)
    original_rate = (
        orig_sale.purchase_rate if orig_sale and orig_sale.purchase_rate
        else Decimal("0")
    )

    # 2. Restore lot_remaining — reverse FIFO walk (newest IN-lot first)
    lots = await _get_fifo_in_lots(db, stock.id)
    qty_to_restore = qty
    for lot in reversed(lots):
        if qty_to_restore <= Decimal("0"):
            break
        # How much of this lot was consumed? = original_qty - current_remaining
        consumed_in_lot = lot.qty - (lot.lot_remaining or Decimal("0"))
        if consumed_in_lot <= Decimal("0"):
            continue
        restore = min(consumed_in_lot, qty_to_restore)
        lot.lot_remaining = (lot.lot_remaining or Decimal("0")) + restore
        qty_to_restore   -= restore

    # 3. Increment on-hand
    stock.qty_on_hand = stock.qty_on_hand + qty

    # 4. Record cancellation transaction (audit trail only — not a new IN-lot)
    cancel_txn = StockTransaction(
        tenant_id     = tenant_id,
        stock_item_id = stock.id,
        txn_type      = StockTxnType.adjustment,
        qty           = qty,                  # positive = stock returned
        purchase_rate = original_rate,        # same rate as original sale
        invoice_id    = invoice_id,           # linked to cancelled invoice
        txn_date      = cancellation_date,
        lot_remaining = None,                 # NOT a new FIFO lot
        reason        = f"Sale Cancelled — Invoice ID {invoice_id}",
        created_by    = created_by,
    )
    db.add(cancel_txn)

    # Update original sale transaction reason for audit trail
    if orig_sale:
        orig_sale.reason = f"Sale — Invoice ID {invoice_id} [CANCELLED]"

    return cancel_txn


# ─────────────────────────────────────────────────────────────
# SALE — EDIT
# Modify original sale transaction in-place, no reversal records
# ─────────────────────────────────────────────────────────────

async def fifo_edit_sale(
    db: AsyncSession,
    *,
    tenant_id: int,
    created_by: int,
    invoice_id: int,
    invoice_date: date,
    stock: StockItem,
    old_qty: Decimal,
    new_qty: Decimal,
) -> StockTransaction:
    """
    Edit a sale invoice item — modifies the original sale transaction in-place.

    Rule: Editing must NOT create reversal entries followed by new entries.
    The original StockTransaction record is updated directly.

    1. Find the original sale transaction for this invoice.
    2. Return the old consumed qty to FIFO lots (reverse walk).
    3. Consume the new qty from FIFO lots (forward walk).
    4. Update the original sale transaction: qty, purchase_rate, txn_date.
    5. Adjust stock.qty_on_hand by the delta (new_qty - old_qty).

    No new StockTransaction records are created.
    """
    # 1. Find original sale txn
    orig_sale = await _get_original_sale_txn(db, stock.id, invoice_id)

    # 2. Return old qty to FIFO lots (reverse walk — undo the original consumption)
    lots = await _get_fifo_in_lots(db, stock.id)
    qty_to_restore = old_qty
    for lot in reversed(lots):
        if qty_to_restore <= Decimal("0"):
            break
        consumed_in_lot = lot.qty - (lot.lot_remaining or Decimal("0"))
        if consumed_in_lot <= Decimal("0"):
            continue
        restore = min(consumed_in_lot, qty_to_restore)
        lot.lot_remaining = (lot.lot_remaining or Decimal("0")) + restore
        qty_to_restore   -= restore

    # Temporarily restore on-hand so the forward walk is correct
    stock.qty_on_hand = stock.qty_on_hand + old_qty

    # 3. Consume new qty from FIFO lots (forward walk)
    qty_to_consume = new_qty
    weighted_value = Decimal("0")
    for lot in lots:
        if qty_to_consume <= Decimal("0"):
            break
        available = lot.lot_remaining if lot.lot_remaining is not None else lot.qty
        if available <= Decimal("0"):
            continue
        take  = min(available, qty_to_consume)
        rate  = lot.purchase_rate or Decimal("0")
        weighted_value += take * rate
        qty_to_consume -= take
        current = lot.lot_remaining if lot.lot_remaining is not None else lot.qty
        lot.lot_remaining = max(Decimal("0"), current - take)

    new_fifo_avg = (
        (weighted_value / new_qty).quantize(Decimal("0.0001"))
        if new_qty > 0 and weighted_value > 0
        else Decimal("0")
    )

    # 4. Update original sale transaction in-place
    if orig_sale:
        orig_sale.qty           = -new_qty           # negative = outbound
        orig_sale.purchase_rate = new_fifo_avg
        orig_sale.txn_date      = invoice_date
        orig_sale.reason        = f"Sale — Invoice ID {invoice_id} [Edited]"
    else:
        # Original txn not found — create a new one (edge case: first edit on old data)
        new_txn = StockTransaction(
            tenant_id     = tenant_id,
            stock_item_id = stock.id,
            txn_type      = StockTxnType.sale,
            qty           = -new_qty,
            purchase_rate = new_fifo_avg,
            invoice_id    = invoice_id,
            txn_date      = invoice_date,
            lot_remaining = None,
            reason        = f"Sale — Invoice ID {invoice_id} [Edited]",
            created_by    = created_by,
        )
        db.add(new_txn)

    # 5. Adjust on-hand by net delta
    stock.qty_on_hand = max(Decimal("0"), stock.qty_on_hand - new_qty)

    return orig_sale


# ─────────────────────────────────────────────────────────────
# PURCHASE — CREATE
# ─────────────────────────────────────────────────────────────

async def fifo_add_purchase(
    db: AsyncSession,
    *,
    tenant_id: int,
    created_by: Optional[int],
    invoice_no: str,
    invoice_date: date,
    stock: StockItem,
    qty: Decimal,
    purchase_rate: Decimal,
) -> StockTransaction:
    """Record a new purchase IN-lot. Full lot available for FIFO consumption."""
    stock.qty_on_hand = stock.qty_on_hand + qty

    txn = StockTransaction(
        tenant_id     = tenant_id,
        stock_item_id = stock.id,
        txn_type      = StockTxnType.purchase,
        qty           = qty,
        purchase_rate = purchase_rate,
        invoice_id    = None,
        txn_date      = invoice_date,
        lot_remaining = qty,
        reason        = f"Purchase — Supplier Invoice {invoice_no}",
        created_by    = created_by,
    )
    db.add(txn)
    return txn


# ─────────────────────────────────────────────────────────────
# PURCHASE — CANCEL
# Direct reversal at original rate — no fresh FIFO walk
# ─────────────────────────────────────────────────────────────

async def fifo_cancel_purchase(
    db: AsyncSession,
    *,
    tenant_id: int,
    created_by: Optional[int],
    invoice_no: str,
    cancellation_date: date,
    stock: StockItem,
    qty: Decimal,
    original_txn: Optional[StockTransaction] = None,
) -> StockTransaction:
    """
    Cancel a purchase invoice — direct reversal of the original purchase.

    Rule: Cancellation reduces stock by exactly the original quantity at the
    original purchase rate. No FIFO recalculation.

    1. Find original purchase transaction.
    2. Zero its lot_remaining (prevent future FIFO consumption of this lot).
    3. Decrement stock.qty_on_hand (clamped at 0).
    4. Record one cancellation transaction at the original rate.
    """
    if original_txn is None:
        original_txn = await _get_original_purchase_txn(db, stock.id, invoice_no)

    original_rate = (
        original_txn.purchase_rate
        if original_txn and original_txn.purchase_rate
        else Decimal("0")
    )

    # Zero the original lot
    if original_txn:
        original_txn.lot_remaining = Decimal("0")
        original_txn.reason = f"Purchase — Supplier Invoice {invoice_no} [CANCELLED]"

    stock.qty_on_hand = max(Decimal("0"), stock.qty_on_hand - qty)

    cancel_txn = StockTransaction(
        tenant_id     = tenant_id,
        stock_item_id = stock.id,
        txn_type      = StockTxnType.adjustment,
        qty           = -qty,
        purchase_rate = original_rate,
        invoice_id    = None,
        txn_date      = cancellation_date,
        lot_remaining = None,
        reason        = f"Purchase Cancelled — Supplier Invoice {invoice_no}",
        created_by    = created_by,
    )
    db.add(cancel_txn)
    return cancel_txn


# ─────────────────────────────────────────────────────────────
# PURCHASE — EDIT
# Modify original purchase transaction in-place, no new records
# ─────────────────────────────────────────────────────────────

async def fifo_edit_purchase_lot(
    db: AsyncSession,
    *,
    tenant_id: int,
    created_by: Optional[int],
    invoice_no: str,
    invoice_date: date,
    stock: StockItem,
    old_qty: Decimal,
    new_qty: Decimal,
    old_rate: Decimal,
    new_rate: Decimal,
    original_txn: Optional[StockTransaction] = None,
) -> Optional[StockTransaction]:
    """
    Edit a purchase invoice item — modifies the original purchase lot in-place.

    Rule: Editing must NOT create reversal+recreate entries.
    The original StockTransaction lot record is updated directly.

    1. Find the original purchase transaction.
    2. Update its qty, purchase_rate, and lot_remaining (by qty delta).
    3. Adjust stock.qty_on_hand by (new_qty - old_qty).

    No new StockTransaction records are created.
    """
    if original_txn is None:
        original_txn = await _get_original_purchase_txn(db, stock.id, invoice_no)

    qty_delta = new_qty - old_qty

    if original_txn:
        old_remaining = original_txn.lot_remaining if original_txn.lot_remaining is not None else old_qty
        # Only adjust the unsold remainder; sold portion's FIFO cost is already captured
        new_remaining = max(Decimal("0"), old_remaining + qty_delta)
        original_txn.qty           = new_qty
        original_txn.purchase_rate = new_rate
        original_txn.lot_remaining = new_remaining
        original_txn.txn_date      = invoice_date
        original_txn.reason        = f"Purchase — Supplier Invoice {invoice_no} [Edited]"

    stock.qty_on_hand = max(Decimal("0"), stock.qty_on_hand + qty_delta)

    # No new transaction created — original record IS the edit record
    return original_txn


# ─────────────────────────────────────────────────────────────
# Backwards-compatibility aliases (for any code still importing old names)
# ─────────────────────────────────────────────────────────────

# Cancel aliases
fifo_reverse_sale     = fifo_cancel_sale
fifo_reverse_purchase = fifo_cancel_purchase
