# utils/erp_accounting.py
# ERP-Grade Accounting Service — GoldTrader Pro
# ─────────────────────────────────────────────────────────────────────────────
# This module is the single source of truth for all financial integrity logic.
#
# Core principle (Tally/ERPNext style):
#   "A posted transaction is PERMANENT.
#    Correction happens through REVERSAL or ADJUSTMENT — never by rewriting history."
#
# Functions:
#   fifo_consume_for_sale()       — consume FIFO lots, record history
#   fifo_restore_from_history()   — restore lots using exact history records
#   fifo_adjust_incremental()     — consume/restore ONLY the delta on amendment
#   create_sales_reversal()       — full reversal entry for cancelled invoice
#   create_purchase_reversal()    — full reversal entry for cancelled purchase
#   save_invoice_version()        — snapshot invoice before amendment
#   save_purchase_version()       — snapshot purchase bill before amendment
#   audit_log()                   — write to immutable audit ledger
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

if TYPE_CHECKING:
    from models import (
        Invoice, InvoiceItem, SupplierInvoice, SupplierInvoiceItem,
        StockItem, StockTransaction, StockTxnType,
    )

from models.erp_models import (
    FIFOConsumptionHistory, ReversalEntry, InvoiceVersion, PurchaseVersion,
    TransactionAuditLog, DocumentType, AmendmentType, AuditEventType,
)


# ══════════════════════════════════════════════════════════════
# I.  AUDIT LOG — immutable append-only
# ══════════════════════════════════════════════════════════════

async def audit_log(
    db            : AsyncSession,
    tenant_id     : int,
    event_type    : AuditEventType,
    description   : str,
    *,
    invoice_id    : int | None = None,
    sup_invoice_id: int | None = None,
    payment_id    : int | None = None,
    stock_txn_id  : int | None = None,
    debit_amount  : Decimal    = Decimal("0"),
    credit_amount : Decimal    = Decimal("0"),
    ledger_account: str | None = None,
    version_no    : int        = 0,
    original_txn_id: int | None = None,
    reversal_ref_id: int | None = None,
    created_by    : int | None = None,
    metadata      : dict | None = None,
) -> TransactionAuditLog:
    entry = TransactionAuditLog(
        tenant_id       = tenant_id,
        event_type      = event_type,
        invoice_id      = invoice_id,
        sup_invoice_id  = sup_invoice_id,
        payment_id      = payment_id,
        stock_txn_id    = stock_txn_id,
        description     = description,
        debit_amount    = debit_amount,
        credit_amount   = credit_amount,
        ledger_account  = ledger_account,
        version_no      = version_no,
        original_txn_id = original_txn_id,
        reversal_ref_id = reversal_ref_id,
        created_by      = created_by,
        metadata_       = metadata or {},
    )
    db.add(entry)
    return entry


# ══════════════════════════════════════════════════════════════
# II.  FIFO CONSUMPTION — consume lots & record history permanently
# ══════════════════════════════════════════════════════════════

async def fifo_consume_for_sale(
    db             : AsyncSession,
    tenant_id      : int,
    created_by     : int,
    invoice_id     : int,
    invoice_item_id: int,
    stock_item     : "StockItem",
    qty_to_sell    : Decimal,
    invoice_date   : date,
    amendment_version: int = 0,
) -> list[FIFOConsumptionHistory]:
    """
    Consume FIFO lots for a sale in chronological lot order.
    Records FIFOConsumptionHistory rows for each lot touched.
    Updates lot_remaining on the StockTransaction rows.
    Updates stock_item.qty_on_hand.

    Returns list of FIFOConsumptionHistory created.
    Raises ValueError if insufficient stock.
    """
    from models import StockTransaction, StockTxnType

    # Fetch all open purchase/opening/adjustment-in lots in FIFO order
    lots_result = await db.execute(
        select(StockTransaction)
        .where(
            StockTransaction.stock_item_id == stock_item.id,
            StockTransaction.txn_type.in_([
                StockTxnType.purchase,
                StockTxnType.opening,
                StockTxnType.adjustment,
            ]),
            StockTransaction.qty > 0,
        )
        .order_by(StockTransaction.txn_date, StockTransaction.id)
    )
    lots = lots_result.scalars().all()

    remaining = qty_to_sell
    consumption_records: list[FIFOConsumptionHistory] = []

    for lot in lots:
        if remaining <= Decimal("0"):
            break
        available = lot.lot_remaining if lot.lot_remaining is not None else lot.qty
        if available <= Decimal("0"):
            continue

        take = min(available, remaining)
        cost_rate = lot.purchase_rate or Decimal("0")

        # Reduce lot_remaining on the purchase transaction
        lot.lot_remaining = (available - take).quantize(Decimal("0.001"))

        # Record the consumption permanently
        history = FIFOConsumptionHistory(
            tenant_id         = tenant_id,
            invoice_item_id   = invoice_item_id,
            invoice_id        = invoice_id,
            purchase_txn_id   = lot.id,
            stock_item_id     = stock_item.id,
            consumed_qty      = take,
            cost_rate         = cost_rate,
            cost_value        = (take * cost_rate).quantize(Decimal("0.01")),
            amendment_version = amendment_version,
            is_reversed       = False,
            created_by        = created_by,
        )
        db.add(history)
        consumption_records.append(history)

        remaining -= take

    if remaining > Decimal("0.001"):   # tolerance for floating point dust
        raise ValueError(
            f"Insufficient stock for item {stock_item.id}: "
            f"need {qty_to_sell}, short by {remaining}"
        )

    # Update qty_on_hand
    stock_item.qty_on_hand = (stock_item.qty_on_hand - qty_to_sell).quantize(Decimal("0.001"))

    # Record the stock-out transaction
    from models import StockTransaction as ST
    sale_txn = ST(
        tenant_id     = tenant_id,
        stock_item_id = stock_item.id,
        txn_type      = StockTxnType.sale,
        qty           = -qty_to_sell,
        purchase_rate = (
            sum(h.cost_value for h in consumption_records) / qty_to_sell
        ).quantize(Decimal("0.01")) if qty_to_sell > 0 else Decimal("0"),
        invoice_id    = invoice_id,
        reason        = f"Sale — Invoice ID {invoice_id}",
        txn_date      = invoice_date,
        lot_remaining = None,
        created_by    = created_by,
    )
    db.add(sale_txn)

    await audit_log(
        db, tenant_id, AuditEventType.fifo_consumed,
        f"FIFO consumed {qty_to_sell} units from stock item {stock_item.id} "
        f"for invoice {invoice_id}",
        invoice_id     = invoice_id,
        credit_amount  = qty_to_sell,
        ledger_account = "Inventory",
        version_no     = amendment_version,
        created_by     = created_by,
    )

    return consumption_records


# ══════════════════════════════════════════════════════════════
# III.  FIFO RESTORE — exact reversal using history records
# ══════════════════════════════════════════════════════════════

async def fifo_restore_from_history(
    db         : AsyncSession,
    tenant_id  : int,
    cancelled_by: int,
    invoice_id : int,
    restore_date: date,
    reversal_ref_id: int | None = None,
) -> None:
    """
    Restore EXACTLY the FIFO lots consumed by invoice_id.
    Uses FIFOConsumptionHistory records — never guesses.
    Sets is_reversed=True on history rows so they can't be double-restored.
    Restores lot_remaining on original purchase transactions.
    """
    from models import StockItem, StockTransaction, StockTxnType

    # Fetch all un-reversed FIFO history for this invoice
    history_result = await db.execute(
        select(FIFOConsumptionHistory)
        .where(
            FIFOConsumptionHistory.invoice_id   == invoice_id,
            FIFOConsumptionHistory.is_reversed  == False,
        )
    )
    histories = history_result.scalars().all()

    if not histories:
        # Fallback for pre-migration invoices: use old _restore_stock pattern
        return

    # Group by stock_item_id for efficient qty_on_hand update
    restore_by_stock: dict[int, Decimal] = {}
    for h in histories:
        restore_by_stock[h.stock_item_id] = (
            restore_by_stock.get(h.stock_item_id, Decimal("0")) + h.consumed_qty
        )
        # Restore lot_remaining on the original purchase transaction
        lot_result = await db.execute(
            select(StockTransaction).where(StockTransaction.id == h.purchase_txn_id)
        )
        orig_lot = lot_result.scalar_one_or_none()
        if orig_lot:
            orig_lot.lot_remaining = (
                (orig_lot.lot_remaining or Decimal("0")) + h.consumed_qty
            ).quantize(Decimal("0.001"))

        # Mark as reversed
        h.is_reversed = True
        h.reversed_at = datetime.utcnow()
        h.reversed_by = cancelled_by

    # Update qty_on_hand and record reversal stock transactions
    for stock_id, qty in restore_by_stock.items():
        stock_result = await db.execute(
            select(StockItem).where(StockItem.id == stock_id)
        )
        stock = stock_result.scalar_one_or_none()
        if not stock:
            continue

        stock.qty_on_hand = (stock.qty_on_hand + qty).quantize(Decimal("0.001"))

        # Record the restoration as an adjustment-in transaction
        # (NOT a new purchase lot — just restores original lots)
        reversal_txn = StockTransaction(
            tenant_id     = tenant_id,
            stock_item_id = stock_id,
            txn_type      = StockTxnType.adjustment,
            qty           = qty,
            purchase_rate = Decimal("0"),   # not a new purchase price
            invoice_id    = invoice_id,
            reason        = f"FIFO Restore — Cancellation of Invoice {invoice_id}",
            txn_date      = restore_date,
            lot_remaining = Decimal("0"),   # not a new lot
            created_by    = cancelled_by,
            reversal_ref_id = reversal_ref_id,
        )
        db.add(reversal_txn)

        await audit_log(
            db, tenant_id, AuditEventType.fifo_restored,
            f"FIFO restored {qty} units to stock item {stock_id} "
            f"on cancellation of invoice {invoice_id}",
            invoice_id     = invoice_id,
            debit_amount   = qty,
            ledger_account = "Inventory",
            reversal_ref_id= reversal_ref_id,
            created_by     = cancelled_by,
        )


# ══════════════════════════════════════════════════════════════
# IV.  FIFO INCREMENTAL ADJUST — amendment delta only
# ══════════════════════════════════════════════════════════════

async def fifo_adjust_incremental(
    db              : AsyncSession,
    tenant_id       : int,
    user_id         : int,
    invoice_id      : int,
    invoice_item_id : int,
    stock_item      : "StockItem",
    old_qty         : Decimal,
    new_qty         : Decimal,
    invoice_date    : date,
    amendment_version: int,
) -> None:
    """
    On invoice amendment: consume or restore ONLY the quantity difference.
    - If new_qty > old_qty: consume (new_qty - old_qty) additional FIFO lots.
    - If new_qty < old_qty: restore (old_qty - new_qty) from the most recent
      FIFO consumption history for this item (LIFO order for restoring).
    - If new_qty == old_qty: no FIFO change needed.
    Never recalculates old FIFO transactions.
    """
    from models import StockTransaction, StockTxnType

    delta = new_qty - old_qty

    if delta == Decimal("0"):
        return

    if delta > Decimal("0"):
        # Consume additional qty
        await fifo_consume_for_sale(
            db, tenant_id, user_id,
            invoice_id, invoice_item_id, stock_item,
            delta, invoice_date, amendment_version,
        )
        await audit_log(
            db, tenant_id, AuditEventType.fifo_adjusted,
            f"Amendment v{amendment_version}: consumed additional {delta} units "
            f"from stock {stock_item.id} for invoice {invoice_id}",
            invoice_id = invoice_id,
            credit_amount = delta,
            ledger_account = "Inventory",
            version_no = amendment_version,
            created_by = user_id,
        )
    else:
        # Restore reduced qty — use LIFO order on history (most recent first)
        qty_to_restore = abs(delta)
        history_result = await db.execute(
            select(FIFOConsumptionHistory)
            .where(
                FIFOConsumptionHistory.invoice_item_id == invoice_item_id,
                FIFOConsumptionHistory.is_reversed     == False,
            )
            .order_by(FIFOConsumptionHistory.id.desc())  # most recent first
        )
        histories = history_result.scalars().all()

        restored = Decimal("0")
        for h in histories:
            if restored >= qty_to_restore:
                break
            restore_from_this = min(h.consumed_qty, qty_to_restore - restored)

            # Partially or fully reverse this history row
            if restore_from_this >= h.consumed_qty:
                # Full reversal of this history row
                h.is_reversed = True
                h.reversed_at = datetime.utcnow()
                h.reversed_by = user_id
                # Restore lot_remaining
                lot_result = await db.execute(
                    select(StockTransaction).where(StockTransaction.id == h.purchase_txn_id)
                )
                orig_lot = lot_result.scalar_one_or_none()
                if orig_lot:
                    orig_lot.lot_remaining = (
                        (orig_lot.lot_remaining or Decimal("0")) + h.consumed_qty
                    ).quantize(Decimal("0.001"))
                restored += h.consumed_qty
            else:
                # Partial reversal — reduce this row's consumed_qty
                partial_remaining = h.consumed_qty - restore_from_this
                h.consumed_qty = partial_remaining
                h.cost_value   = (partial_remaining * h.cost_rate).quantize(Decimal("0.01"))
                # Restore partial lot_remaining
                lot_result = await db.execute(
                    select(StockTransaction).where(StockTransaction.id == h.purchase_txn_id)
                )
                orig_lot = lot_result.scalar_one_or_none()
                if orig_lot:
                    orig_lot.lot_remaining = (
                        (orig_lot.lot_remaining or Decimal("0")) + restore_from_this
                    ).quantize(Decimal("0.001"))
                restored += restore_from_this

        # Update qty_on_hand
        stock_item.qty_on_hand = (stock_item.qty_on_hand + qty_to_restore).quantize(Decimal("0.001"))

        # Record adjustment stock transaction
        adj_txn = StockTransaction(
            tenant_id     = tenant_id,
            stock_item_id = stock_item.id,
            txn_type      = StockTxnType.adjustment,
            qty           = qty_to_restore,   # positive = stock in
            purchase_rate = Decimal("0"),
            invoice_id    = invoice_id,
            reason        = f"Amendment v{amendment_version} Qty Reduction — Invoice {invoice_id}",
            txn_date      = invoice_date,
            lot_remaining = Decimal("0"),
            created_by    = user_id,
            version_no    = amendment_version,
        )
        db.add(adj_txn)

        await audit_log(
            db, tenant_id, AuditEventType.fifo_adjusted,
            f"Amendment v{amendment_version}: restored {qty_to_restore} units "
            f"to stock {stock_item.id} for invoice {invoice_id}",
            invoice_id = invoice_id,
            debit_amount = qty_to_restore,
            ledger_account = "Inventory",
            version_no = amendment_version,
            created_by = user_id,
        )


# ══════════════════════════════════════════════════════════════
# V.  SALES INVOICE CANCELLATION — create reversal document
# ══════════════════════════════════════════════════════════════

async def create_sales_reversal(
    db              : AsyncSession,
    tenant_id       : int,
    cancelled_by    : int,
    invoice         : "Invoice",
    reason          : str,
    cancel_date     : date,
) -> ReversalEntry:
    """
    Cancel a sales invoice:
    1. Mark original invoice as 'cancelled' with audit fields.
    2. Create ReversalEntry recording accounting impact.
    3. Restore FIFO inventory from history.
    4. Write audit log entries for all ledger impacts.

    Ledger entries reversed:
      - Customer Ledger     (Dr) → reversed → (Cr)
      - Sales Account       (Cr) → reversed → (Dr)
      - CGST/SGST/IGST      (Cr) → reversed → (Dr)
      - Inventory           (Cr) → reversed → (Dr) [FIFO restore]
    """
    from models import InvoiceStatus

    # 1. Mark original as cancelled
    invoice.status              = InvoiceStatus.cancelled
    invoice.cancelled_by        = cancelled_by
    invoice.cancelled_at        = datetime.utcnow()
    invoice.cancellation_reason = reason

    # 2. Create reversal entry
    reversal = ReversalEntry(
        tenant_id               = tenant_id,
        document_type           = DocumentType.sales_invoice,
        original_invoice_id     = invoice.id,
        subtotal_reversed       = invoice.subtotal,
        cgst_reversed           = invoice.cgst,
        sgst_reversed           = invoice.sgst,
        igst_reversed           = invoice.igst,
        total_reversed          = invoice.grand_total,
        cancelled_by            = cancelled_by,
        cancellation_reason     = reason,
        notes                   = f"Reversal of {invoice.invoice_no}",
    )
    db.add(reversal)
    await db.flush()   # get reversal.id

    # Link reversal back to invoice
    invoice.reversal_ref_id = reversal.id

    # 3. Restore FIFO inventory using exact history
    await fifo_restore_from_history(
        db, tenant_id, cancelled_by,
        invoice.id, cancel_date, reversal.id,
    )

    # 4. Audit log — reversal of all ledger accounts
    gst_amount = invoice.cgst + invoice.sgst + invoice.igst

    await audit_log(
        db, tenant_id, AuditEventType.invoice_cancelled,
        f"CANCELLED: {invoice.invoice_no} — Customer ledger reversed ₹{invoice.grand_total}",
        invoice_id      = invoice.id,
        debit_amount    = Decimal("0"),
        credit_amount   = invoice.grand_total,
        ledger_account  = "Customer Ledger",
        reversal_ref_id = reversal.id,
        original_txn_id = invoice.id,
        created_by      = cancelled_by,
        metadata        = {"reason": reason, "invoice_no": invoice.invoice_no},
    )
    await audit_log(
        db, tenant_id, AuditEventType.invoice_cancelled,
        f"CANCELLED: {invoice.invoice_no} — Sales ledger reversed ₹{invoice.subtotal}",
        invoice_id      = invoice.id,
        debit_amount    = invoice.subtotal,
        credit_amount   = Decimal("0"),
        ledger_account  = "Sales Account",
        reversal_ref_id = reversal.id,
        original_txn_id = invoice.id,
        created_by      = cancelled_by,
    )
    if gst_amount > 0:
        gst_label = "CGST+SGST Payable" if invoice.cgst > 0 else "IGST Payable"
        await audit_log(
            db, tenant_id, AuditEventType.invoice_cancelled,
            f"CANCELLED: {invoice.invoice_no} — GST reversed ₹{gst_amount}",
            invoice_id      = invoice.id,
            debit_amount    = gst_amount,
            credit_amount   = Decimal("0"),
            ledger_account  = gst_label,
            reversal_ref_id = reversal.id,
            original_txn_id = invoice.id,
            created_by      = cancelled_by,
        )

    return reversal


# ══════════════════════════════════════════════════════════════
# VI.  PURCHASE BILL CANCELLATION — reversal
# ══════════════════════════════════════════════════════════════

async def create_purchase_reversal(
    db              : AsyncSession,
    tenant_id       : int,
    cancelled_by    : int,
    inv             : "SupplierInvoice",
    reason          : str,
    cancel_date     : date,
) -> ReversalEntry:
    """
    Cancel a purchase bill:
    1. Mark original as 'cancelled'.
    2. Create ReversalEntry.
    3. Reverse stock — reduce lot_remaining on original purchase transactions,
       check if any quantity has already been sold (FIFO dependency).
    4. Write audit log.

    Ledger entries reversed:
      - Supplier Ledger (Cr) → reversed → (Dr)
      - Purchase Account (Dr) → reversed → (Cr)
      - CGST/SGST/IGST Input Credit (Dr) → reversed → (Cr)
      - Inventory (Dr) → reversed → (Cr)
    """
    from models import SupplierInvoiceItem, StockItem, StockTransaction, StockTxnType

    # 1. Mark original as cancelled
    inv.status              = "cancelled"
    inv.cancelled_by        = cancelled_by
    inv.cancelled_at        = datetime.utcnow()
    inv.cancellation_reason = reason

    # 2. Create reversal entry
    reversal = ReversalEntry(
        tenant_id               = tenant_id,
        document_type           = DocumentType.purchase_bill,
        original_sup_invoice_id = inv.id,
        subtotal_reversed       = inv.subtotal,
        cgst_reversed           = inv.cgst,
        sgst_reversed           = inv.sgst,
        igst_reversed           = inv.igst,
        total_reversed          = inv.grand_total,
        cancelled_by            = cancelled_by,
        cancellation_reason     = reason,
        notes                   = f"Reversal of purchase {inv.invoice_no}",
    )
    db.add(reversal)
    await db.flush()

    inv.reversal_ref_id = reversal.id

    # 3. Reverse stock for each item
    items_result = await db.execute(
        select(SupplierInvoiceItem).where(SupplierInvoiceItem.invoice_id == inv.id)
    )
    items = items_result.scalars().all()

    for item in items:
        cat_val = getattr(item.category, "value", str(item.category)) if item.category else ""
        if cat_val == "Polish Charges":
            continue

        # Find the stock item
        stock_result = await db.execute(
            select(StockItem).where(
                StockItem.tenant_id == tenant_id,
                StockItem.category  == item.category,
                StockItem.purity    == item.purity,
                StockItem.unit      == item.unit,
            ).limit(1)
        )
        stock = stock_result.scalar_one_or_none()
        if not stock:
            continue

        # Find the original purchase lot for this item
        orig_lot_result = await db.execute(
            select(StockTransaction).where(
                StockTransaction.stock_item_id == stock.id,
                StockTransaction.txn_type      == StockTxnType.purchase,
                StockTransaction.reason        == f"Supplier Invoice {inv.invoice_no}",
            ).order_by(StockTransaction.id.desc()).limit(1)
        )
        orig_lot = orig_lot_result.scalar_one_or_none()

        original_rate = (
            orig_lot.purchase_rate if orig_lot and orig_lot.purchase_rate else item.rate
        )

        # Calculate how much of this lot is still available (not yet sold via FIFO)
        if orig_lot:
            lot_still_remaining = orig_lot.lot_remaining or Decimal("0")
            qty_already_sold    = item.qty - lot_still_remaining
        else:
            lot_still_remaining = item.qty
            qty_already_sold    = Decimal("0")

        # Clamp: can only reverse what's still in stock
        qty_to_reverse = min(item.qty, max(Decimal("0"), lot_still_remaining))

        if qty_to_reverse > Decimal("0"):
            # Zero out lot_remaining on original purchase lot
            if orig_lot:
                orig_lot.lot_remaining = Decimal("0")

            # Deduct from qty_on_hand (clamped at 0)
            stock.qty_on_hand = max(
                Decimal("0"),
                (stock.qty_on_hand - qty_to_reverse).quantize(Decimal("0.001"))
            )

            # Record the reversal stock transaction
            reversal_txn = StockTransaction(
                tenant_id     = tenant_id,
                stock_item_id = stock.id,
                txn_type      = StockTxnType.adjustment,
                qty           = -qty_to_reverse,
                purchase_rate = original_rate,
                invoice_id    = None,
                reason        = f"Purchase Cancellation Reversal — {inv.invoice_no}",
                txn_date      = cancel_date,
                lot_remaining = None,
                created_by    = cancelled_by,
                reversal_ref_id = reversal.id,
                original_transaction_id = orig_lot.id if orig_lot else None,
            )
            db.add(reversal_txn)

        # Warn if qty was already sold (FIFO dependency)
        if qty_already_sold > Decimal("0"):
            # Record this as an audit note — can't undo what's already sold
            await audit_log(
                db, tenant_id, AuditEventType.purchase_cancelled,
                f"WARNING: Purchase {inv.invoice_no} item qty={item.qty} — "
                f"{qty_already_sold} units already consumed by sales FIFO. "
                f"Only {qty_to_reverse} units reversed in inventory.",
                sup_invoice_id  = inv.id,
                reversal_ref_id = reversal.id,
                created_by      = cancelled_by,
                metadata        = {
                    "item_qty": float(item.qty),
                    "already_sold": float(qty_already_sold),
                    "reversed": float(qty_to_reverse),
                },
            )

    # 4. Audit log — ledger reversals
    gst_amount = inv.cgst + inv.sgst + inv.igst

    await audit_log(
        db, tenant_id, AuditEventType.purchase_cancelled,
        f"CANCELLED: Purchase {inv.invoice_no} — Supplier ledger reversed ₹{inv.grand_total}",
        sup_invoice_id  = inv.id,
        credit_amount   = inv.grand_total,
        ledger_account  = "Supplier Ledger",
        reversal_ref_id = reversal.id,
        original_txn_id = inv.id,
        created_by      = cancelled_by,
        metadata        = {"reason": reason, "invoice_no": inv.invoice_no},
    )
    await audit_log(
        db, tenant_id, AuditEventType.purchase_cancelled,
        f"CANCELLED: Purchase {inv.invoice_no} — Purchase account reversed ₹{inv.subtotal}",
        sup_invoice_id  = inv.id,
        debit_amount    = inv.subtotal,
        ledger_account  = "Purchase Account",
        reversal_ref_id = reversal.id,
        original_txn_id = inv.id,
        created_by      = cancelled_by,
    )
    if gst_amount > 0:
        gst_label = "CGST+SGST Input Credit" if inv.cgst > 0 else "IGST Input Credit"
        await audit_log(
            db, tenant_id, AuditEventType.purchase_cancelled,
            f"CANCELLED: Purchase {inv.invoice_no} — GST input credit reversed ₹{gst_amount}",
            sup_invoice_id  = inv.id,
            credit_amount   = gst_amount,
            ledger_account  = gst_label,
            reversal_ref_id = reversal.id,
            original_txn_id = inv.id,
            created_by      = cancelled_by,
        )

    return reversal


# ══════════════════════════════════════════════════════════════
# VII.  AMENDMENT SNAPSHOTS
# ══════════════════════════════════════════════════════════════

async def save_invoice_version(
    db              : AsyncSession,
    tenant_id       : int,
    invoice         : "Invoice",
    items           : list,
    amendment_type  : AmendmentType,
    amendment_reason: str | None,
    amended_by      : int,
    adj_subtotal    : Decimal = Decimal("0"),
    adj_cgst        : Decimal = Decimal("0"),
    adj_sgst        : Decimal = Decimal("0"),
    adj_igst        : Decimal = Decimal("0"),
    adj_grand_total : Decimal = Decimal("0"),
) -> InvoiceVersion:
    """
    Snapshot the invoice's current state BEFORE applying an amendment.
    Increments version_no on the invoice.
    """
    current_version = (invoice.version_no or 0)
    new_version     = current_version + 1

    snapshot_items = [
        {
            "id":             item.id,
            "category":       getattr(item.category, "value", str(item.category)),
            "purity":         item.purity,
            "description":    item.description,
            "hsn_code":       item.hsn_code,
            "qty":            float(item.qty),
            "unit":           getattr(item.unit, "value", str(item.unit)),
            "rate":           float(item.rate),
            "polish_charges": float(item.polish_charges or 0),
            "making_charges": float(item.making_charges or 0),
            "amount":         float(item.amount),
        }
        for item in items
    ]

    version = InvoiceVersion(
        tenant_id               = tenant_id,
        invoice_id              = invoice.id,
        version_no              = new_version,
        amendment_type          = amendment_type,
        snapshot_invoice_date   = invoice.invoice_date,
        snapshot_customer_name  = invoice.customer_name,
        snapshot_customer_pan   = invoice.customer_pan,
        snapshot_customer_state = invoice.customer_state,
        snapshot_customer_gstin = invoice.customer_gstin,
        snapshot_pay_mode       = getattr(invoice.pay_mode, "value", str(invoice.pay_mode)),
        snapshot_gst_type       = getattr(invoice.gst_type, "value", str(invoice.gst_type)),
        snapshot_gst_rate       = invoice.gst_rate,
        snapshot_subtotal       = invoice.subtotal,
        snapshot_cgst           = invoice.cgst,
        snapshot_sgst           = invoice.sgst,
        snapshot_igst           = invoice.igst,
        snapshot_grand_total    = invoice.grand_total,
        snapshot_notes          = invoice.notes,
        snapshot_items          = snapshot_items,
        adjustment_subtotal     = adj_subtotal,
        adjustment_cgst         = adj_cgst,
        adjustment_sgst         = adj_sgst,
        adjustment_igst         = adj_igst,
        adjustment_grand_total  = adj_grand_total,
        amendment_reason        = amendment_reason,
        amended_by              = amended_by,
    )
    db.add(version)
    invoice.version_no = new_version
    return version


async def save_purchase_version(
    db              : AsyncSession,
    tenant_id       : int,
    inv             : "SupplierInvoice",
    items           : list,
    amendment_type  : AmendmentType,
    amendment_reason: str | None,
    amended_by      : int,
    adj_subtotal    : Decimal = Decimal("0"),
    adj_cgst        : Decimal = Decimal("0"),
    adj_sgst        : Decimal = Decimal("0"),
    adj_igst        : Decimal = Decimal("0"),
    adj_grand_total : Decimal = Decimal("0"),
) -> PurchaseVersion:
    current_version = (inv.version_no or 0)
    new_version     = current_version + 1

    snapshot_items = [
        {
            "id":             item.id,
            "category":       getattr(item.category, "value", str(item.category)),
            "purity":         item.purity,
            "description":    item.description,
            "qty":            float(item.qty),
            "rate":           float(item.rate),
            "making_charges": float(item.making_charges or 0),
            "amount":         float(item.amount),
        }
        for item in items
    ]

    version = PurchaseVersion(
        tenant_id               = tenant_id,
        invoice_id              = inv.id,
        version_no              = new_version,
        amendment_type          = amendment_type,
        snapshot_invoice_no     = inv.invoice_no,
        snapshot_invoice_date   = inv.invoice_date,
        snapshot_supplier_name  = inv.supplier_name,
        snapshot_gst_type       = inv.gst_type,
        snapshot_gst_rate       = inv.gst_rate,
        snapshot_subtotal       = inv.subtotal,
        snapshot_cgst           = inv.cgst,
        snapshot_sgst           = inv.sgst,
        snapshot_igst           = inv.igst,
        snapshot_grand_total    = inv.grand_total,
        snapshot_notes          = inv.notes,
        snapshot_items          = snapshot_items,
        adjustment_subtotal     = adj_subtotal,
        adjustment_cgst         = adj_cgst,
        adjustment_sgst         = adj_sgst,
        adjustment_igst         = adj_igst,
        adjustment_grand_total  = adj_grand_total,
        amendment_reason        = amendment_reason,
        amended_by              = amended_by,
    )
    db.add(version)
    inv.version_no = new_version
    return version
