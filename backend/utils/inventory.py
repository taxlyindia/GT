# utils/inventory.py
# ═══════════════════════════════════════════════════════════════════════════
# Inventory Posting Engine v2
# ═══════════════════════════════════════════════════════════════════════════
#
# CORE PRINCIPLE (enforced by this module):
#
#   EDIT   ≠ REVERSAL   → update original rows in-place, zero new register rows
#   CANCEL = EXACT REVERSAL → one mirror row per original, direction only reversed
#
# PUBLIC API:
#
#   post_sale(db, ctx)                → consume FIFO lots, write sale_out txn
#   post_purchase(db, ctx)            → write purchase_in txn
#   cancel_sale(db, invoice)          → exact FIFO reversal → sale_cancel_in txn
#   cancel_purchase(db, ctx)          → exact rate reversal → purchase_cancel_out txn
#   edit_sale_release(db, invoice_id) → release old FIFO allocations silently
#   edit_purchase_lot(db, ctx)        → update original lot in-place
#
# WHAT THIS ENGINE NEVER DOES:
#   • Create "Edit Reversal" rows
#   • Create "adjustment" rows during edit or cancel
#   • Recompute rates or FIFO at cancellation time
#   • Create any row during an edit (except for brand-new items added in edit)
# ═══════════════════════════════════════════════════════════════════════════

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models import (
    StockItem, StockTransaction, StockTxnType, StockMovementType, ReversalType,
    InventoryFifoConsumption,
)


# ─────────────────────────────────────────────────────────────────────────────
# Context objects (plain data; no DB dependency)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ItemCtx:
    """One line-item worth of context passed to the posting engine."""
    category:       object          # CategoryEnum or str
    purity:         Optional[str]
    qty:            Decimal
    rate:           Decimal         # purchase rate (for IN); sale rate (for OUT label only)
    invoice_id:     Optional[int]   = None
    invoice_item_id:Optional[int]   = None
    txn_date:       date            = field(default_factory=date.today)
    reason:         str             = ""
    created_by:     Optional[int]   = None


@dataclass
class PurchaseLotCtx:
    """Context for editing an existing purchase lot."""
    stock_item_id:       int
    original_invoice_no: str
    new_qty:             Decimal
    new_rate:            Decimal
    new_txn_date:        Optional[date]  = None
    new_invoice_no:      Optional[str]   = None
    created_by:          Optional[int]   = None


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _find_stock(
    db:        AsyncSession,
    tenant_id: int,
    category,
    purity:    Optional[str],
) -> Optional[StockItem]:
    """
    Find best-matching StockItem.
    Priority: exact purity > NULL-purity wildcard > any purity for category.
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
        .order_by(sa_case((StockItem.purity == purity, 0), else_=1) if purity else StockItem.id)
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _snap(qty: Decimal, rate: Optional[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
    """Return (original_qty, original_rate, original_value) snapshot values."""
    r = rate or Decimal("0")
    q = abs(qty)
    return q, r, (q * r).quantize(Decimal("0.01"))


# ─────────────────────────────────────────────────────────────────────────────
# PURCHASE POST (sale invoice creation)
# ─────────────────────────────────────────────────────────────────────────────

async def post_purchase(
    db:        AsyncSession,
    tenant_id: int,
    ctx:       ItemCtx,
    stock:     StockItem,
) -> StockTransaction:
    """
    Create a purchase_in StockTransaction.
    Called from suppliers.py when a supplier invoice is created.
    Returns the created StockTransaction (before flush).
    """
    cat_val = getattr(ctx.category, 'value', str(ctx.category))
    if cat_val == "Polish Charges":
        return None  # type: ignore

    o_qty, o_rate, o_val = _snap(ctx.qty, ctx.rate)

    txn = StockTransaction(
        tenant_id               = tenant_id,
        stock_item_id           = stock.id,
        txn_type                = StockTxnType.purchase,
        movement_type           = StockMovementType.purchase_in,
        qty                     = ctx.qty,
        purchase_rate           = ctx.rate,
        lot_remaining           = ctx.qty,
        original_qty            = o_qty,
        original_rate           = o_rate,
        original_value          = o_val,
        invoice_id              = ctx.invoice_id,
        reason                  = ctx.reason or f"Purchase IN",
        txn_date                = ctx.txn_date,
        created_by              = ctx.created_by,
    )
    db.add(txn)
    stock.qty_on_hand += ctx.qty
    return txn


# ─────────────────────────────────────────────────────────────────────────────
# SALE POST (sale invoice creation)
# ─────────────────────────────────────────────────────────────────────────────

async def post_sale(
    db:        AsyncSession,
    tenant_id: int,
    ctx:       ItemCtx,
    stock:     StockItem,
) -> StockTransaction:
    """
    Consume FIFO lots and create a sale_out StockTransaction with full snapshot.

    Steps:
    1. Walk open lots oldest-first (purchase_in / opening / sale_cancel_in)
    2. Consume qty from each, writing lot_remaining back immediately
    3. Build fifo_snapshot JSON and list of InventoryFifoConsumption rows
    4. Create the sale_out StockTransaction (flush to get id)
    5. Insert InventoryFifoConsumption rows (FK → sale_txn.id)
    6. Decrement stock.qty_on_hand

    Returns the created StockTransaction.
    """
    cat_val = getattr(ctx.category, 'value', str(ctx.category))
    if cat_val == "Polish Charges":
        return None  # type: ignore

    # ── Fetch open IN lots oldest-first ───────────────────────────────────
    batches_r = await db.execute(
        select(StockTransaction)
        .where(
            StockTransaction.stock_item_id == stock.id,
            StockTransaction.qty > 0,
            StockTransaction.movement_type.in_([
                StockMovementType.purchase_in,
                StockMovementType.opening,
                StockMovementType.sale_cancel_in,   # restored lots are re-available
                StockMovementType.adjustment,        # manual additions
            ]),
        )
        .order_by(StockTransaction.txn_date, StockTransaction.id)
    )
    batches = batches_r.scalars().all()

    # ── Walk lots, consume, accumulate ────────────────────────────────────
    qty_remaining  = ctx.qty
    weighted_value = Decimal("0")
    layer_list: list[tuple[StockTransaction, Decimal]] = []   # (lot, take)
    snapshot:   list[dict]                             = []

    for lot in batches:
        if qty_remaining <= 0:
            break
        available = lot.lot_remaining if lot.lot_remaining is not None else abs(lot.qty)
        if available <= 0:
            continue

        take  = min(available, qty_remaining)
        rate  = lot.purchase_rate or Decimal("0")
        value = (take * rate).quantize(Decimal("0.01"))

        weighted_value     += value
        qty_remaining      -= take
        layer_list.append((lot, take))

        # Persist lot_remaining immediately (persistent FIFO state)
        lot.lot_remaining = (available - take).quantize(Decimal("0.001"))

        snapshot.append({
            "lot_txn_id": lot.id,
            "qty":        float(take),
            "rate":       float(rate),
            "value":      float(value),
        })

    fifo_avg_rate = (
        (weighted_value / ctx.qty).quantize(Decimal("0.01"))
        if ctx.qty > 0 and weighted_value > 0
        else Decimal("0")
    )

    o_qty, o_rate, o_val = _snap(ctx.qty, fifo_avg_rate)

    # ── Create sale_out StockTransaction ──────────────────────────────────
    sale_txn = StockTransaction(
        tenant_id               = tenant_id,
        stock_item_id           = stock.id,
        txn_type                = StockTxnType.sale,
        movement_type           = StockMovementType.sale_out,
        qty                     = -ctx.qty,
        purchase_rate           = fifo_avg_rate,
        lot_remaining           = None,            # sale_out rows have no lot balance
        original_qty            = o_qty,
        original_rate           = o_rate,
        original_value          = o_val,
        fifo_snapshot           = snapshot,        # immutable FIFO JSON
        invoice_id              = ctx.invoice_id,
        reason                  = ctx.reason or f"Sale OUT",
        txn_date                = ctx.txn_date,
        created_by              = ctx.created_by,
    )
    db.add(sale_txn)
    await db.flush()   # need sale_txn.id for InventoryFifoConsumption FK

    # ── Persist per-layer consumption records ─────────────────────────────
    for lot, take in layer_list:
        rate  = lot.purchase_rate or Decimal("0")
        value = (take * rate).quantize(Decimal("0.01"))
        db.add(InventoryFifoConsumption(
            tenant_id         = tenant_id,
            movement_id       = sale_txn.id,
            invoice_id        = ctx.invoice_id,
            invoice_item_id   = ctx.invoice_item_id,
            purchase_layer_id = lot.id,
            consumed_qty      = take.quantize(Decimal("0.001")),
            consumed_rate     = rate,
            consumed_value    = value,
        ))

    # ── Decrement stock ───────────────────────────────────────────────────
    stock.qty_on_hand -= ctx.qty

    return sale_txn


# ─────────────────────────────────────────────────────────────────────────────
# SALE CANCEL (exact reversal — NO recomputation)
# ─────────────────────────────────────────────────────────────────────────────

async def cancel_sale(
    db:         AsyncSession,
    tenant_id:  int,
    created_by: int,
    invoice_id: int,
    invoice_no: str,
    item_qty:   Decimal,
    stock:      StockItem,
    cancel_date: date,
) -> None:
    """
    Exact reversal of a sale_out transaction for one invoice item.

    Algorithm:
    1. Find original sale_out StockTransaction for this invoice + stock item
    2. Load InventoryFifoConsumption rows (FIFO allocation snapshot)
    3. For each consumption row: restore lot_remaining on the original lot
    4. Create one sale_cancel_in row:
         qty           = +item_qty  (exact original qty)
         purchase_rate = original sale_txn.purchase_rate (FIFO avg — never recomputed)
         lot_remaining = item_qty   (re-enters as fresh FIFO lot)
         reversal_of_movement_id = original sale_txn.id
         reversal_type            = "sale_cancel"
    5. Increment stock.qty_on_hand

    NEVER recomputes FIFO. NEVER uses current rates. Direction only is reversed.
    """
    # ── Find the original sale_out transaction ────────────────────────────
    sale_txn_r = await db.execute(
        select(StockTransaction)
        .where(
            StockTransaction.stock_item_id == stock.id,
            StockTransaction.movement_type == StockMovementType.sale_out,
            StockTransaction.invoice_id    == invoice_id,
        )
        .order_by(StockTransaction.id.desc())
        .limit(1)
    )
    sale_txn = sale_txn_r.scalar_one_or_none()

    if sale_txn is None:
        # Fallback: look up by reason string (pre-migration invoices)
        sale_txn_r2 = await db.execute(
            select(StockTransaction)
            .where(
                StockTransaction.stock_item_id == stock.id,
                StockTransaction.txn_type      == StockTxnType.sale,
                StockTransaction.reason        == f"Sale — Invoice ID {invoice_id}",
            )
            .order_by(StockTransaction.id.desc())
            .limit(1)
        )
        sale_txn = sale_txn_r2.scalar_one_or_none()

    # ── Load FIFO consumption snapshot ────────────────────────────────────
    consumptions: list[InventoryFifoConsumption] = []
    if sale_txn:
        cons_r = await db.execute(
            select(InventoryFifoConsumption)
            .where(InventoryFifoConsumption.movement_id == sale_txn.id)
            .order_by(InventoryFifoConsumption.id)
        )
        consumptions = cons_r.scalars().all()

    # ── Determine exact original rate (never recomputed) ──────────────────
    original_rate = (
        sale_txn.purchase_rate
        if sale_txn and sale_txn.purchase_rate is not None
        else Decimal("0")
    )
    original_id = sale_txn.id if sale_txn else None

    if consumptions:
        # ── EXACT LAYER REVERSAL PATH ─────────────────────────────────────
        # Restore lot_remaining on each consumed lot.
        # This is the precise mirror of what post_sale() did.
        for cons in consumptions:
            lot = await db.get(StockTransaction, cons.purchase_layer_id)
            if lot is None:
                continue
            current = lot.lot_remaining or Decimal("0")
            lot.lot_remaining = (current + cons.consumed_qty).quantize(Decimal("0.001"))

    # ── Create exact reversal IN row ──────────────────────────────────────
    o_qty, o_rate, o_val = _snap(item_qty, original_rate)

    cancel_txn = StockTransaction(
        tenant_id               = tenant_id,
        stock_item_id           = stock.id,
        txn_type                = StockTxnType.adjustment,     # legacy compat
        movement_type           = StockMovementType.sale_cancel_in,
        qty                     = item_qty,                    # +IN (exact original qty)
        purchase_rate           = original_rate,               # exact original rate
        lot_remaining           = item_qty,                    # available for future sales
        original_qty            = o_qty,
        original_rate           = o_rate,
        original_value          = o_val,
        reversal_of_movement_id = original_id,
        reversal_type           = ReversalType.sale_cancel,
        invoice_id              = invoice_id,
        reason                  = f"Cancellation IN — Invoice {invoice_no}",
        txn_date                = cancel_date,
        created_by              = created_by,
    )
    db.add(cancel_txn)

    # ── Restore stock qty ─────────────────────────────────────────────────
    stock.qty_on_hand += item_qty


# ─────────────────────────────────────────────────────────────────────────────
# PURCHASE CANCEL (exact reversal — original rate only)
# ─────────────────────────────────────────────────────────────────────────────

async def cancel_purchase(
    db:                 AsyncSession,
    tenant_id:          int,
    created_by:         int,
    stock:              StockItem,
    original_txn:       StockTransaction,   # the purchase_in lot to reverse
    item_qty:           Decimal,
    invoice_no:         str,
    cancel_date:        date,
) -> None:
    """
    Exact reversal of a purchase_in transaction.

    Uses EXACT original rate from original_txn.purchase_rate.
    NEVER uses current FIFO, moving average, or any recomputed value.

    Steps:
    1. Zero the original lot's lot_remaining (stop FIFO from consuming it)
    2. Create purchase_cancel_out row at exact original rate
    3. Decrement stock.qty_on_hand (clamped at zero)
    """
    original_rate = (
        original_txn.purchase_rate
        if original_txn.purchase_rate is not None
        else Decimal("0")
    )

    # Zero the original lot — FIFO should not draw from it any further
    original_txn.lot_remaining = Decimal("0")

    o_qty, o_rate, o_val = _snap(item_qty, original_rate)

    cancel_txn = StockTransaction(
        tenant_id               = tenant_id,
        stock_item_id           = stock.id,
        txn_type                = StockTxnType.adjustment,     # legacy compat
        movement_type           = StockMovementType.purchase_cancel_out,
        qty                     = -item_qty,                   # -OUT (exact original qty)
        purchase_rate           = original_rate,               # EXACT original purchase rate
        lot_remaining           = None,
        original_qty            = o_qty,
        original_rate           = o_rate,
        original_value          = o_val,
        reversal_of_movement_id = original_txn.id,
        reversal_type           = ReversalType.purchase_cancel,
        invoice_id              = None,
        reason                  = f"Cancellation OUT — Purchase Invoice {invoice_no}",
        txn_date                = cancel_date,
        created_by              = created_by,
    )
    db.add(cancel_txn)

    # Clamp at zero (handles edge case where stock was already partially sold)
    stock.qty_on_hand = max(Decimal("0"), stock.qty_on_hand - item_qty)


# ─────────────────────────────────────────────────────────────────────────────
# EDIT SALE — release old FIFO allocations silently
# ─────────────────────────────────────────────────────────────────────────────

async def edit_sale_release(
    db:         AsyncSession,
    stock:      StockItem,
    invoice_id: int,
) -> Optional[StockTransaction]:
    """
    Release FIFO allocations for an existing sale without creating any
    visible register entry.  Called during invoice edit BEFORE reposting.

    Steps:
    1. Find the sale_out StockTransaction for this invoice + stock item
    2. Load InventoryFifoConsumption rows
    3. Restore lot_remaining on each consumed lot (exact undo of what post_sale did)
    4. Delete the sale_out StockTransaction row (InventoryFifoConsumption rows
       are cascade-deleted automatically via FK)
    5. Restore stock.qty_on_hand

    Returns old_qty restored (so caller can apply delta), or None if not found.
    No new rows created. Stock register stays clean.
    """
    # Find existing sale_out row
    sale_txn_r = await db.execute(
        select(StockTransaction)
        .where(
            StockTransaction.stock_item_id == stock.id,
            StockTransaction.movement_type == StockMovementType.sale_out,
            StockTransaction.invoice_id    == invoice_id,
        )
        .order_by(StockTransaction.id.desc())
        .limit(1)
    )
    sale_txn = sale_txn_r.scalar_one_or_none()

    if sale_txn is None:
        # Fallback for pre-migration rows tagged by reason string
        sale_txn_r2 = await db.execute(
            select(StockTransaction)
            .where(
                StockTransaction.stock_item_id == stock.id,
                StockTransaction.txn_type      == StockTxnType.sale,
                StockTransaction.reason        == f"Sale — Invoice ID {invoice_id}",
            )
            .order_by(StockTransaction.id.desc())
            .limit(1)
        )
        sale_txn = sale_txn_r2.scalar_one_or_none()

    if sale_txn is None:
        return None

    old_qty = abs(sale_txn.qty)

    # Restore lot_remaining on each consumed lot
    cons_r = await db.execute(
        select(InventoryFifoConsumption)
        .where(InventoryFifoConsumption.movement_id == sale_txn.id)
    )
    for cons in cons_r.scalars().all():
        lot = await db.get(StockTransaction, cons.purchase_layer_id)
        if lot is None:
            continue
        current = lot.lot_remaining or Decimal("0")
        lot.lot_remaining = (current + cons.consumed_qty).quantize(Decimal("0.001"))

    # Restore stock qty
    stock.qty_on_hand += old_qty

    # Delete the sale_out row; InventoryFifoConsumption rows cascade-delete
    await db.delete(sale_txn)

    return sale_txn


# ─────────────────────────────────────────────────────────────────────────────
# EDIT PURCHASE LOT — update original lot in-place
# ─────────────────────────────────────────────────────────────────────────────

async def edit_purchase_lot(
    db:        AsyncSession,
    tenant_id: int,
    stock:     StockItem,
    ctx:       PurchaseLotCtx,
) -> bool:
    """
    Update an existing purchase_in lot in-place during invoice edit.
    NO new rows created. Stock register stays clean.

    Steps:
    1. Find original purchase_in lot by stock_item_id + invoice reason string
    2. Compute already_consumed = original_qty - lot_remaining
    3. Update: qty, purchase_rate, lot_remaining = new_qty - already_consumed
    4. Update stock.qty_on_hand by delta (new_qty - old_qty)
    5. Update immutable snapshot fields to reflect the edit
       (original_* stores the NEW values — edit replaces original, not appends)

    Returns True if found and updated, False if lot not found.
    """
    reason = f"Supplier Invoice {ctx.original_invoice_no}"

    orig_r = await db.execute(
        select(StockTransaction)
        .where(
            StockTransaction.stock_item_id == stock.id,
            StockTransaction.movement_type == StockMovementType.purchase_in,
            StockTransaction.reason        == reason,
        )
        .order_by(StockTransaction.id.desc())
        .limit(1)
    )
    orig_txn = orig_r.scalar_one_or_none()

    if orig_txn is None:
        return False

    old_qty          = orig_txn.qty
    already_consumed = old_qty - (orig_txn.lot_remaining or Decimal("0"))
    already_consumed = max(Decimal("0"), already_consumed)
    new_lot_remaining= max(Decimal("0"), ctx.new_qty - already_consumed)

    # Update lot in-place
    orig_txn.qty           = ctx.new_qty
    orig_txn.purchase_rate = ctx.new_rate
    orig_txn.lot_remaining = new_lot_remaining.quantize(Decimal("0.001"))

    # Update immutable snapshot to reflect edit (edit replaces history)
    o_qty, o_rate, o_val = _snap(ctx.new_qty, ctx.new_rate)
    orig_txn.original_qty   = o_qty
    orig_txn.original_rate  = o_rate
    orig_txn.original_value = o_val

    if ctx.new_txn_date:
        orig_txn.txn_date = ctx.new_txn_date
    if ctx.new_invoice_no:
        orig_txn.reason = f"Supplier Invoice {ctx.new_invoice_no}"

    # Adjust stock by delta
    delta = ctx.new_qty - old_qty
    stock.qty_on_hand += delta

    return True
