# routers/reports.py
# Changes vs v4 original:
#  Issue 11 — /section-269st: customer_name and customer_pan fetched from master
#  Issue 12 — /fifo: qty_in and qty_out populated from StockTransaction records
#  Issue 13 — /cashbook, /payments, /itemwise endpoints added (were 404-ing)
#  P11      — TCS register kept but TCS values will be 0; added section-269st endpoint

from datetime import date
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models import (
    Invoice, InvoiceItem, Customer, Payment,
    CashEntry, StockItem, StockTransaction, SupplierInvoice,
)
from utils.auth import get_current_user_payload
from utils.business import current_fy, fifo_valuation, summarise_cash, SFT_THRESHOLD

router = APIRouter()


# ── Sales Register ────────────────────────────────────────────

@router.get("/sales")
async def sales_register(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Invoice-wise sales register with GST breakdown."""
    tenant_id = payload["tenant_id"]
    stmt = (
        select(Invoice)
        .where(Invoice.tenant_id == tenant_id, Invoice.status == "active")
        .order_by(Invoice.invoice_date.desc())
    )
    if from_date:
        stmt = stmt.where(Invoice.invoice_date >= from_date)
    if to_date:
        stmt = stmt.where(Invoice.invoice_date <= to_date)

    result   = await db.execute(stmt)
    invoices = result.scalars().all()

    rows = [{
        "invoice_no":      inv.invoice_no,
        "invoice_date":    inv.invoice_date.isoformat(),
        "customer_name":   inv.customer_name,
        "customer_mobile": inv.customer_mobile,
        "customer_pan":    inv.customer_pan or "—",
        "pay_mode":        inv.pay_mode.value,
        "subtotal":        float(inv.subtotal),
        "cgst":            float(inv.cgst),
        "sgst":            float(inv.sgst),
        "igst":            float(inv.igst),
        "tcs_amount":      float(inv.tcs_amount),
        "grand_total":     float(inv.grand_total),
        "payment_status":  inv.payment_status.value,
    } for inv in invoices]

    return {
        "rows":           rows,
        "total_subtotal": sum(r["subtotal"]  for r in rows),
        "total_gst":      sum(r["cgst"] + r["sgst"] + r["igst"] for r in rows),
        "total_tcs":      sum(r["tcs_amount"] for r in rows),
        "grand_total":    sum(r["grand_total"] for r in rows),
    }


# ── SFT Register ──────────────────────────────────────────────

@router.get("/sft")
async def sft_register(
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """SFT report — customers with cash receipts > ₹2,00,000 in current FY."""
    tenant_id = payload["tenant_id"]
    result = await db.execute(
        select(Customer).where(
            Customer.tenant_id   == tenant_id,
            Customer.sft_flagged == True,
        )
    )
    customers = result.scalars().all()
    rows = [{
        "customer_name":    c.name,
        "mobile":           c.mobile,
        "pan":              c.pan or None,
        "cash_receipts_fy": float(c.cash_receipts_fy),
        "sft_threshold":    float(SFT_THRESHOLD),
        "pan_missing":      not c.pan,
        "status":           "PAN Required" if not c.pan else "Flag for SFT",
    } for c in customers]

    return {
        "rows":              rows,
        "total_flagged":     len(rows),
        "total_pan_missing": sum(1 for r in rows if r["pan_missing"]),
    }


# ── GSTR-1 Register ───────────────────────────────────────────

@router.get("/gstr1")
async def gstr1_register(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """GSTR-1 register with HSN-wise taxable value and GST breakdown."""
    tenant_id = payload["tenant_id"]
    stmt = (
        select(Invoice)
        .where(Invoice.tenant_id == tenant_id, Invoice.status == "active")
        .order_by(Invoice.invoice_date.desc())
    )
    if from_date:
        stmt = stmt.where(Invoice.invoice_date >= from_date)
    if to_date:
        stmt = stmt.where(Invoice.invoice_date <= to_date)

    result   = await db.execute(stmt)
    invoices = result.scalars().all()

    rows = [{
        "invoice_no":     inv.invoice_no,
        "invoice_date":   inv.invoice_date.isoformat(),
        "customer_name":  inv.customer_name,
        "customer_gstin": inv.customer_gstin or "Unregistered",
        "customer_state": inv.customer_state or "",
        "hsn_code":       "7113",
        "gst_type":       inv.gst_type.value,
        "taxable_value":  float(inv.subtotal),
        "cgst_rate":      float(inv.gst_rate / 2),
        "cgst_amount":    float(inv.cgst),
        "sgst_rate":      float(inv.gst_rate / 2),
        "sgst_amount":    float(inv.sgst),
        "igst_amount":    float(inv.igst),
        "grand_total":    float(inv.grand_total),
    } for inv in invoices]

    return {
        "rows":          rows,
        "total_taxable": sum(r["taxable_value"] for r in rows),
        "total_cgst":    sum(r["cgst_amount"]   for r in rows),
        "total_sgst":    sum(r["sgst_amount"]   for r in rows),
        "total_igst":    sum(r["igst_amount"]   for r in rows),
    }


# ── Outstanding ───────────────────────────────────────────────

@router.get("/outstanding")
async def outstanding(
    mobile:  Optional[str] = Query(None),
    payload: dict          = Depends(get_current_user_payload),
    db:      AsyncSession  = Depends(get_db),
):
    """List all invoices with outstanding balances."""
    tenant_id = payload["tenant_id"]
    stmt = select(Invoice).where(
        Invoice.tenant_id      == tenant_id,
        Invoice.payment_status != "paid",
        Invoice.status         == "active",
    ).order_by(Invoice.invoice_date.desc())

    if mobile:
        stmt = stmt.where(Invoice.customer_mobile == mobile)

    result   = await db.execute(stmt)
    invoices = result.scalars().all()

    rows = [{
        "invoice_no":      inv.invoice_no,
        "invoice_date":    inv.invoice_date.isoformat(),
        "customer_name":   inv.customer_name,
        "customer_mobile": inv.customer_mobile,
        "grand_total":     float(inv.grand_total),
        "amount_paid":     float(inv.amount_paid),
        "outstanding":     float(inv.outstanding),
    } for inv in invoices]

    return {
        "rows":              rows,
        "total_outstanding": sum(r["outstanding"] for r in rows),
    }


# ── Cash Register Summary (dashboard KPI) ────────────────────

@router.get("/cash/summary")
async def cash_summary(
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """Return FY cash KPIs: collected, deposited in bank, cash on hand."""
    tenant_id = payload["tenant_id"]
    result = await db.execute(
        select(CashEntry)
        .where(CashEntry.tenant_id == tenant_id)
        .order_by(CashEntry.entry_date)
    )
    entries = result.scalars().all()
    raw     = [{"entry_date": e.entry_date, "entry_type": e.entry_type.value, "amount": e.amount}
               for e in entries]
    summary = summarise_cash(raw)
    return {
        "cash_collected_fy": float(summary["cash_collected_fy"]),
        "cash_deposited_fy": float(summary["cash_deposited_fy"]),
        "cash_on_hand":      float(summary["cash_on_hand"]),
    }


# ── Cash Book Report ──────────────────────────────────────────
# Issue 13 fix — endpoint was missing, frontend showed "Error: Not found"

@router.get("/cashbook")
async def cashbook_report(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Cash book with running balance — day-by-day cash in/out register."""
    tenant_id = payload["tenant_id"]
    stmt = (
        select(CashEntry)
        .where(CashEntry.tenant_id == tenant_id)
        .order_by(CashEntry.entry_date, CashEntry.id)
    )
    if from_date:
        stmt = stmt.where(CashEntry.entry_date >= from_date)
    if to_date:
        stmt = stmt.where(CashEntry.entry_date <= to_date)

    result  = await db.execute(stmt)
    entries = result.scalars().all()

    running = Decimal("0")
    rows    = []
    for e in entries:
        amount = Decimal(str(e.amount))
        etype  = e.entry_type.value
        if etype in ("cash_in", "bank_in"):
            running += amount
        elif etype in ("cash_out", "cash_to_bank"):
            running -= amount

        rows.append({
            "date":             e.entry_date.isoformat(),
            "type":             etype,
            "description":      e.description or "",
            "amount":           float(amount),
            "bank_reference":   e.bank_reference or "",
            "running_balance":  float(running),
        })

    total_in  = sum(r["amount"] for r in rows if r["type"] in ("cash_in", "bank_in"))
    total_out = sum(r["amount"] for r in rows if r["type"] in ("cash_out", "cash_to_bank"))

    return {
        "rows":      rows,
        "total_in":  total_in,
        "total_out": total_out,
        "balance":   float(running),
    }


# ── Payments Report ───────────────────────────────────────────
# Issue 13 fix — endpoint was missing, frontend showed "Error: Not found"
# Also used by Issue 5 (Payments page fetch)

@router.get("/payments")
async def payments_report(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    mobile:    Optional[str]  = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """
    Payments register — all payments with customer name, invoice number, mode.
    Used by both the Payments page (Issue 5) and the Reports tab (Issue 13).
    """
    tenant_id = payload["tenant_id"]
    stmt = (
        select(Payment)
        .where(Payment.tenant_id == tenant_id)
        .order_by(Payment.payment_date.desc(), Payment.id.desc())
    )
    if from_date:
        stmt = stmt.where(Payment.payment_date >= from_date)
    if to_date:
        stmt = stmt.where(Payment.payment_date <= to_date)
    if mobile:
        stmt = stmt.where(Payment.customer_mobile == mobile)

    result   = await db.execute(stmt)
    payments = result.scalars().all()

    # Build invoice_no map
    inv_nos: dict[int, str] = {}
    for p in payments:
        if p.invoice_id and p.invoice_id not in inv_nos:
            inv = await db.get(Invoice, p.invoice_id)
            inv_nos[p.invoice_id] = inv.invoice_no if inv else "—"

    rows = []
    for p in payments:
        cname = (p.customer_name
                 if hasattr(p, "customer_name") and p.customer_name
                 else None)
        if not cname:
            cust  = await db.get(Customer, (p.customer_mobile, tenant_id))
            cname = cust.name if cust else "—"

        rows.append({
            "id":              p.id,
            "date":            p.payment_date.isoformat(),
            "payment_date":    p.payment_date.isoformat(),
            "invoice_no":      inv_nos.get(p.invoice_id, "—"),
            "invoice_id":      p.invoice_id,
            "advance_id":      getattr(p, "advance_id", None),
            "customer_name":   cname,
            "mobile":          p.customer_mobile,
            "customer_mobile": p.customer_mobile,
            "amount":          float(p.amount),
            "pay_mode":        p.pay_mode.value if hasattr(p.pay_mode, "value") else str(p.pay_mode or ""),
            "reference_no":    p.reference_no or "—",
        })

    return {
        "rows":  rows,
        "total": sum(r["amount"] for r in rows),
    }


# ── Item-wise Sales Report ────────────────────────────────────
# Issue 13 fix — endpoint was missing, frontend showed "Error: Not found"

@router.get("/itemwise")
async def itemwise_report(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Item-wise sales report — one row per invoice line item."""
    tenant_id = payload["tenant_id"]
    stmt = (
        select(Invoice)
        .where(Invoice.tenant_id == tenant_id, Invoice.status == "active")
        .order_by(Invoice.invoice_date.desc())
    )
    if from_date:
        stmt = stmt.where(Invoice.invoice_date >= from_date)
    if to_date:
        stmt = stmt.where(Invoice.invoice_date <= to_date)

    result   = await db.execute(stmt)
    invoices = result.scalars().all()

    rows        = []
    grand_total = Decimal("0")

    for inv in invoices:
        items_result = await db.execute(
            select(InvoiceItem).where(InvoiceItem.invoice_id == inv.id)
        )
        for item in items_result.scalars():
            rows.append({
                "invoice_no":     inv.invoice_no,
                "invoice_date":   inv.invoice_date.isoformat(),
                "customer_name":  inv.customer_name,
                "customer_mobile":inv.customer_mobile,
                "pan":            inv.customer_pan or "—",
                "pay_mode":       inv.pay_mode.value,
                "category":       item.category.value,
                "purity":         item.purity or "—",
                "description":    item.description,
                "qty":            float(item.qty),
                "unit":           item.unit.value,
                "rate":           float(item.rate),
                "making_charges": float(item.making_charges),
                "amount":         float(item.amount),
            })
            grand_total += item.amount

    return {
        "rows":        rows,
        "grand_total": float(grand_total),
    }


# ── FIFO Stock Valuation ──────────────────────────────────────────────────────
#
# v29 fixes (based on screenshot analysis):
#
# ROOT CAUSE CONFIRMED: StockTransaction.invoice_id is always NULL in this system.
# The invoice reference is stored as a human-readable string in t.reason:
#   Purchase:   reason = "Supplier Invoice GO/2025-2"
#   Sale:       reason = "Sale — Invoice ID 5"
#   Cancelled:  reason = "Cancelled — Invoice INV-007"
#   Opening:    reason = None / ""
#   Adjustment: reason = custom text
#
# BUG 1 — inv_no / party columns swapped:
#   Was: inv_no = txn_label ("Purchase"), party = t.reason ("Supplier Invoice GO/2025-2")
#   Fix: inv_no = t.reason (the invoice reference), party = supplier/customer name via lookup
#
# BUG 2 — Cancelled adjustment value_in = 0:
#   System creates adjustment txn (qty>0) to restore cancelled stock, but purchase_rate=None.
#   Fix: When a cancellation-adjustment has no purchase_rate, use the current FIFO batch
#   rate (oldest layer) as the restoration rate — same rate at which stock was consumed.
#
# BUG 3 — Duplicate rows for same invoice:
#   _dedup used invoice_id for grouping, but invoice_id=None for all txns.
#   All txns went into no_inv → no dedup applied → duplicates shown.
#   Fix: When invoice_id is None, use (parsed_key_from_reason, direction) as dedup key.

import re

def _parse_reason(reason: str) -> tuple:
    """
    Parse t.reason to identify the invoice reference.
    Returns (kind, lookup_key):
      ('supplier_invno', 'GO/2025-2')  — purchase linked to SupplierInvoice by invoice_no
      ('sale_id', 5)                   — sale linked to Invoice by id
      ('cancelled_id', 5)              — cancellation, original Invoice id
      ('cancelled_invno', 'INV-007')   — cancellation, original Invoice invoice_no
      ('purchase_adj', None)           — manual purchase/stock-in adjustment (no invoice)
      ('none', None)                   — opening / plain adjustment
    """
    if not reason:
        return ('none', None)
    r = reason.strip()
    m = re.match(r'Supplier Invoice\s+(.+)', r, re.IGNORECASE)
    if m:
        return ('supplier_invno', m.group(1).strip())
    m = re.match(r'Sale\s*[\u2014\-]+\s*Invoice ID\s+(\d+)', r, re.IGNORECASE)
    if m:
        return ('sale_id', int(m.group(1)))
    m = re.match(r'Sale\s*[\u2014\-]+\s*(.+)', r, re.IGNORECASE)
    if m:
        return ('sale_invno', m.group(1).strip())
    m = re.match(r'Cancelled\s*[\u2014\-]+\s*Invoice\s+(?:ID\s+)?(.+)', r, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        if val.isdigit():
            return ('cancelled_id', int(val))
        return ('cancelled_invno', val)
    # Manual purchase stock-in (from adjust_stock with positive qty)
    m = re.match(r'Purchase\s*[\u2014\-]+\s*Stock IN', r, re.IGNORECASE)
    if m:
        return ('purchase_adj', None)
    # Edit reversal — stock restored when an invoice is re-edited
    m = re.match(r'Edit Reversal\s*[\u2014\-]+\s*Invoice\s+(\d+)', r, re.IGNORECASE)
    if m:
        return ('cancelled_id', int(m.group(1)))
    return ('none', None)


def _dedup_txns(all_txns: list) -> list:
    """
    Keep only the LATEST StockTransaction per logical transaction.
    For txns with invoice_id: group by (invoice_id, direction).
    For txns without invoice_id: group by (parsed reason key, direction).
    This handles both the standard case (invoice_id set) and this system's case
    (invoice_id always None, reference in reason).
    """
    seen: dict = {}
    for t in all_txns:
        direction = "IN" if t.qty > 0 else "OUT"
        if t.invoice_id is not None:
            key = ('inv', t.invoice_id, direction)
        else:
            kind, lookup_key = _parse_reason(t.reason or '')
            if kind in ('none', 'purchase_adj') or lookup_key is None:
                # Opening/adjustment/manual purchase — keep ALL (each is a separate lot)
                # Use txn id to make key unique so nothing is dropped
                key = ('unique', t.id, direction)
            else:
                key = (kind, str(lookup_key), direction)
        if key not in seen or t.id > seen[key].id:
            seen[key] = t
    return sorted(seen.values(), key=lambda x: (x.txn_date, x.id))


def _txn_type_label(raw: str) -> str:
    return {
        'opening':    'Opening Stock',
        'purchase':   'Purchase',
        'sale':       'Sale',
        'adjustment': 'Adjustment',
        'cancellation': 'Cancellation',
    }.get(str(raw).lower(), str(raw).title())


def _build_fifo_layers(batches: list, qty_out: "Decimal") -> tuple:
    from decimal import Decimal
    remaining = qty_out
    cogs = Decimal("0")
    for b in batches:
        if remaining <= 0:
            break
        take = min(b["qty_remaining"], remaining)
        cogs += take * b["purchase_rate"]
        b["qty_remaining"] -= take
        remaining -= take
    batches[:] = [b for b in batches if b["qty_remaining"] > 0]
    return cogs, batches


@router.get("/fifo")
async def fifo_report(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    as_of:     Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """
    FIFO Stock Valuation — v29 with corrected invoice resolution.

    Handles the case where StockTransaction.invoice_id is always NULL and the
    invoice reference is stored in t.reason as a human-readable string.
    Deduplication, party resolution, and cancelled-adjustment value_in are all fixed.
    """
    tenant_id = payload["tenant_id"]
    cutoff    = as_of or to_date or date.today()

    # ── Pre-fetch invoices (both types) into lookup dicts ────────
    # Keyed BOTH by id AND by invoice_no for flexible lookup
    inv_r = await db.execute(
        select(Invoice).where(Invoice.tenant_id == tenant_id)
    )
    all_invs = inv_r.scalars().all()
    inv_by_id:    dict = {inv.id:         inv for inv in all_invs}
    inv_by_no:    dict = {inv.invoice_no:  inv for inv in all_invs if inv.invoice_no}
    cancelled_ids = {inv.id for inv in all_invs if getattr(inv, 'status', None) in ('cancelled', 'Cancelled') or str(getattr(inv, 'status', '')).lower() == 'cancelled'}

    sinv_r = await db.execute(
        select(SupplierInvoice).where(SupplierInvoice.tenant_id == tenant_id)
    )
    all_sinvs    = sinv_r.scalars().all()
    sinv_by_id:  dict = {sinv.id:         sinv for sinv in all_sinvs}
    sinv_by_no:  dict = {sinv.invoice_no:  sinv for sinv in all_sinvs if sinv.invoice_no}

    # ── Fetch stock items ────────────────────────────────────────
    stocks_result = await db.execute(
        select(StockItem).where(
            StockItem.tenant_id == tenant_id,
            StockItem.category  != "Polish Charges",
            StockItem.is_active == True,
        ).order_by(StockItem.category, StockItem.purity, StockItem.description)
    )
    stocks = stocks_result.scalars().all()

    valuation_rows:    list = []
    invoice_movements: list = []
    category_map:      dict = {}

    for stock in stocks:
        cat  = stock.category.value if hasattr(stock.category, 'value') else str(stock.category)
        unit = stock.unit.value     if hasattr(stock.unit,     'value') else str(stock.unit)

        all_txns_result = await db.execute(
            select(StockTransaction)
            .where(
                StockTransaction.stock_item_id == stock.id,
                StockTransaction.txn_date      <= cutoff,
            )
            .order_by(StockTransaction.txn_date, StockTransaction.id)
        )
        txns = _dedup_txns(all_txns_result.scalars().all())

        fifo_batches:   list    = []
        qty_in_total    = Decimal("0")
        qty_out_total   = Decimal("0")
        value_in_total  = Decimal("0")
        value_out_total = Decimal("0")

        for t in txns:
            qty       = Decimal(str(t.qty))
            qty_abs   = abs(qty)
            raw_type  = t.txn_type.value if hasattr(t.txn_type, 'value') else str(t.txn_type)
            txn_label = _txn_type_label(raw_type)
            reason    = (t.reason or '').strip()

            # ── Purchase rate ──────────────────────────────────────
            rate = Decimal(str(t.purchase_rate)) if t.purchase_rate else Decimal("0")

            # ── Resolve inv_no and party from reason / invoice maps ─
            kind, lkey = _parse_reason(reason)

            if kind == 'supplier_invno':
                # Purchase: reason = "Supplier Invoice GO/2025-2"
                sinv = sinv_by_no.get(lkey)
                if sinv is None:
                    # Try partial match (GO/2025-2 vs GO/2025-002)
                    sinv = next((s for s in all_sinvs if s.invoice_no and lkey in s.invoice_no), None)
                inv_no = sinv.invoice_no if sinv else (lkey or reason)
                party  = (sinv.supplier_name or sinv.supplier_mobile) if sinv else reason

            elif kind == 'sale_id':
                # Sale: reason = "Sale — Invoice ID 5" → Invoice.id = 5
                cinv   = inv_by_id.get(lkey)
                inv_no = cinv.invoice_no if cinv else f"INV-{lkey}"
                party  = (cinv.customer_name or cinv.customer_mobile) if cinv else reason

            elif kind == 'sale_invno':
                # Sale: reason = "Sale — INV-007"
                cinv   = inv_by_no.get(lkey)
                inv_no = cinv.invoice_no if cinv else lkey
                party  = (cinv.customer_name or cinv.customer_mobile) if cinv else reason

            elif kind in ('cancelled_id', 'cancelled_invno'):
                # Cancellation adjustment
                if kind == 'cancelled_id':
                    orig = inv_by_id.get(lkey)
                else:
                    orig = inv_by_no.get(lkey)
                inv_no = orig.invoice_no if orig else (f"INV-{lkey}" if kind == 'cancelled_id' else lkey)
                party  = (orig.customer_name or orig.customer_mobile) if orig else reason
                txn_label = "Cancelled Sale"   # override label for clarity

            else:
                # Opening stock, manual purchase adjustment, or plain adjustment
                inv_no = txn_label if not reason else reason
                party  = stock.description

            # ── FIFO IN / OUT ────────────────────────────────────
            if qty > 0:
                # ── Cancellation / restoration rate resolution ────────────
                # Since _restore_stock now always stores the original FIFO purchase_rate
                # on the adjustment transaction, rate will already be correct.
                # Fallback (for OLD cancellation transactions created before this fix):
                # if rate is still zero, use oldest batch rate or avg consumed rate.
                if rate == Decimal("0") and fifo_batches:
                    rate = fifo_batches[0]["purchase_rate"]
                elif rate == Decimal("0") and not fifo_batches and value_out_total > 0:
                    if qty_out_total > 0:
                        rate = value_out_total / qty_out_total

                fifo_batches.append({
                    "qty_remaining": qty,
                    "purchase_rate": rate,
                    "txn_date":      t.txn_date,
                })
                value_in       = qty * rate
                qty_in_total   += qty
                value_in_total += value_in
                cogs            = Decimal("0")
                fifo_rate       = float(rate)

            else:
                # Stock OUT — consume FIFO batches
                cogs, fifo_batches = _build_fifo_layers(fifo_batches, qty_abs)
                qty_out_total   += qty_abs
                value_out_total += cogs
                value_in        = Decimal("0")
                fifo_rate = float(cogs / qty_abs) if qty_abs > 0 else 0.0

            # Append to movements within date window
            in_window = (from_date is None or t.txn_date >= from_date)
            if in_window:
                invoice_movements.append({
                    "date":        t.txn_date.isoformat(),
                    "direction":   "IN" if qty > 0 else "OUT",
                    "txn_type":    txn_label,
                    "invoice_no":  inv_no,
                    "party":       party,
                    "category":    cat,
                    "purity":      stock.purity or "—",
                    "description": stock.description,
                    "unit":        unit,
                    "qty":         float(qty_abs),
                    "rate":        round(fifo_rate, 2),
                    "value_in":    round(float(value_in), 2),
                    "value_out":   round(float(cogs),     2),
                    "reason":      reason,
                })

        # Closing batches
        closing_batches = [
            {
                "qty_remaining": round(float(b["qty_remaining"]), 4),
                "purchase_rate": round(float(b["purchase_rate"]), 2),
                "batch_value":   round(float(b["qty_remaining"] * b["purchase_rate"]), 2),
            }
            for b in fifo_batches if b["qty_remaining"] > 0
        ]
        closing_value   = sum(Decimal(str(b["batch_value"])) for b in closing_batches)
        qty_on_hand_val = qty_in_total - qty_out_total
        avg_rate        = (
            float(closing_value / qty_on_hand_val) if qty_on_hand_val > 0 else 0.0
        )

        valuation_rows.append({
            "category":        cat,
            "purity":          stock.purity or "—",
            "description":     stock.description,
            "unit":            unit,
            "qty_in":          float(qty_in_total),
            "qty_out":         float(qty_out_total),
            "qty_on_hand":     round(float(qty_on_hand_val), 4),
            "avg_rate":        round(avg_rate, 2),
            "total_value":     round(float(closing_value), 2),
            "value_in_total":  round(float(value_in_total),  2),
            "value_out_total": round(float(value_out_total), 2),
            "closing_batches": closing_batches,
        })

        if cat not in category_map:
            category_map[cat] = {
                "qty_in":    Decimal("0"), "qty_out":   Decimal("0"),
                "value_in":  Decimal("0"), "value_out": Decimal("0"),
                "total_value": Decimal("0"),
            }
        category_map[cat]["qty_in"]      += qty_in_total
        category_map[cat]["qty_out"]     += qty_out_total
        category_map[cat]["value_in"]    += value_in_total
        category_map[cat]["value_out"]   += value_out_total
        category_map[cat]["total_value"] += closing_value

    category_summary = [
        {
            "category":        cat,
            "qty_in":          float(v["qty_in"]),
            "qty_out":         float(v["qty_out"]),
            "qty_on_hand":     round(float(v["qty_in"] - v["qty_out"]), 4),
            "value_in_total":  round(float(v["value_in"]),    2),
            "value_out_total": round(float(v["value_out"]),   2),
            "total_value":     round(float(v["total_value"]), 2),
        }
        for cat, v in sorted(category_map.items())
    ]

    invoice_movements.sort(key=lambda m: m["date"])

    return {
        "as_of":             cutoff.isoformat(),
        "from_date":         from_date.isoformat() if from_date else "All time",
        "rows":              valuation_rows,
        "invoice_movements": invoice_movements,
        "category_summary":  category_summary,
        "grand_total":       round(sum(r["total_value"] for r in valuation_rows), 2),
    }




# ── Account Register ──────────────────────────────────────────
# New report: one row per invoice with category-wise item totals
# Columns: Invoice Date, Invoice No, Customer Name, Customer Mobile,
#          Gold, Silver, Diamond, Polish Charges, Making Charges,
#          CGST Amount, SGST Amount, IGST Amount, Grand Total

@router.get("/account")
async def account_register(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """
    Account Register — one row per invoice, with amounts broken out by
    category (Gold / Silver / Diamond / Polish Charges) plus Making Charges,
    GST components, and Grand Total.
    """
    tenant_id = payload["tenant_id"]
    stmt = (
        select(Invoice)
        .where(Invoice.tenant_id == tenant_id, Invoice.status == "active")
        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
    )
    if from_date:
        stmt = stmt.where(Invoice.invoice_date >= from_date)
    if to_date:
        stmt = stmt.where(Invoice.invoice_date <= to_date)

    result   = await db.execute(stmt)
    invoices = result.scalars().all()

    rows = []
    total_gold    = Decimal("0")
    total_silver  = Decimal("0")
    total_diamond = Decimal("0")
    total_polish  = Decimal("0")
    total_making  = Decimal("0")
    total_cgst    = Decimal("0")
    total_sgst    = Decimal("0")
    total_igst    = Decimal("0")
    total_grand   = Decimal("0")

    for inv in invoices:
        # Fetch all line items for this invoice
        items_result = await db.execute(
            select(InvoiceItem).where(InvoiceItem.invoice_id == inv.id)
        )
        items = items_result.scalars().all()

        # Aggregate amounts by category (amount = qty*rate; making_charges separate)
        gold_amt    = Decimal("0")
        silver_amt  = Decimal("0")
        diamond_amt = Decimal("0")
        polish_amt  = Decimal("0")
        making_total = Decimal("0")

        for item in items:
            cat = item.category.value  # "Gold" / "Silver" / "Diamond" / "Polish Charges"
            # item.amount includes making_charges already; subtract to get pure item value
            item_base = item.amount - item.making_charges
            making_total += item.making_charges

            if cat == "Gold":
                gold_amt    += item_base
            elif cat == "Silver":
                silver_amt  += item_base
            elif cat == "Diamond":
                diamond_amt += item_base
            elif cat == "Polish Charges":
                polish_amt  += item_base   # typically qty*rate for polish

        total_gold    += gold_amt
        total_silver  += silver_amt
        total_diamond += diamond_amt
        total_polish  += polish_amt
        total_making  += making_total
        total_cgst    += inv.cgst
        total_sgst    += inv.sgst
        total_igst    += inv.igst
        total_grand   += inv.grand_total

        rows.append({
            "invoice_date":    inv.invoice_date.isoformat(),
            "invoice_no":      inv.invoice_no,
            "customer_name":   inv.customer_name,
            "customer_mobile": inv.customer_mobile,
            "gold":            float(gold_amt),
            "silver":          float(silver_amt),
            "diamond":         float(diamond_amt),
            "polish_charges":  float(polish_amt),
            "making_charges":  float(making_total),
            "cgst":            float(inv.cgst),
            "sgst":            float(inv.sgst),
            "igst":            float(inv.igst),
            "grand_total":     float(inv.grand_total),
        })

    return {
        "rows": rows,
        "totals": {
            "gold":           float(total_gold),
            "silver":         float(total_silver),
            "diamond":        float(total_diamond),
            "polish_charges": float(total_polish),
            "making_charges": float(total_making),
            "cgst":           float(total_cgst),
            "sgst":           float(total_sgst),
            "igst":           float(total_igst),
            "grand_total":    float(total_grand),
        },
    }


# ── Customer Account Report ───────────────────────────────────

@router.get("/customer-account")
async def customer_account_report(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Customer Account Report — same format as /account (alias for front-end tab)."""
    tenant_id = payload["tenant_id"]
    stmt = (
        select(Invoice)
        .where(Invoice.tenant_id == tenant_id, Invoice.status == "active")
        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
    )
    if from_date: stmt = stmt.where(Invoice.invoice_date >= from_date)
    if to_date:   stmt = stmt.where(Invoice.invoice_date <= to_date)
    result   = await db.execute(stmt)
    invoices = result.scalars().all()

    rows = []
    tot  = {k: Decimal("0") for k in ["gold","silver","diamond","polish","making","cgst","sgst","igst","grand"]}

    for inv in invoices:
        items_r = await db.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == inv.id))
        items   = items_r.scalars().all()
        gold = silver = diamond = polish = making = Decimal("0")
        for item in items:
            base = item.amount - item.making_charges
            making += item.making_charges
            cat = item.category.value
            if cat == "Gold":            gold    += base
            elif cat == "Silver":        silver  += base
            elif cat == "Diamond":       diamond += base
            elif cat == "Polish Charges": polish += base
        for k, v in [("gold",gold),("silver",silver),("diamond",diamond),("polish",polish),
                     ("making",making),("cgst",inv.cgst),("sgst",inv.sgst),("igst",inv.igst),("grand",inv.grand_total)]:
            tot[k] += v
        rows.append({
            "invoice_date": inv.invoice_date.isoformat(), "invoice_no": inv.invoice_no,
            "customer_name": inv.customer_name, "customer_mobile": inv.customer_mobile,
            "gold": float(gold), "silver": float(silver), "diamond": float(diamond),
            "making": float(making), "cgst": float(inv.cgst), "sgst": float(inv.sgst),
            "igst": float(inv.igst), "grand_total": float(inv.grand_total),
        })
    return {"rows": rows, "totals": {k: float(v) for k, v in tot.items()}}


# ── Supplier Account Report ───────────────────────────────────

@router.get("/supplier-account")
async def supplier_account_report(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Supplier Account Report — mirrors Customer Account Report format for purchase invoices."""
    from models import SupplierInvoice, SupplierInvoiceItem
    tenant_id = payload["tenant_id"]
    stmt = (
        select(SupplierInvoice)
        .where(SupplierInvoice.tenant_id == tenant_id, SupplierInvoice.status == "active")
        .order_by(SupplierInvoice.invoice_date.desc(), SupplierInvoice.id.desc())
    )
    if from_date: stmt = stmt.where(SupplierInvoice.invoice_date >= from_date)
    if to_date:   stmt = stmt.where(SupplierInvoice.invoice_date <= to_date)
    result   = await db.execute(stmt)
    invoices = result.scalars().all()

    rows = []
    tot  = {k: Decimal("0") for k in ["gold","silver","diamond","polish","making","cgst","sgst","igst","grand","amount_paid","outstanding"]}

    for inv in invoices:
        items_r = await db.execute(select(SupplierInvoiceItem).where(SupplierInvoiceItem.invoice_id == inv.id))
        items   = items_r.scalars().all()
        gold = silver = diamond = polish = making = Decimal("0")
        for item in items:
            base = (item.amount or Decimal("0")) - (item.making_charges or Decimal("0"))
            making += item.making_charges or Decimal("0")
            cat = item.category.value if hasattr(item.category,"value") else str(item.category)
            if cat == "Gold":            gold    += base
            elif cat == "Silver":        silver  += base
            elif cat == "Diamond":       diamond += base
            elif cat == "Polish Charges": polish += base
        cgst = inv.cgst or Decimal("0")
        sgst = inv.sgst or Decimal("0")
        igst = inv.igst or Decimal("0")
        for k, v in [("gold",gold),("silver",silver),("diamond",diamond),("polish",polish),
                     ("making",making),("cgst",cgst),("sgst",sgst),("igst",igst),
                     ("grand",inv.grand_total),("amount_paid",inv.amount_paid),("outstanding",inv.outstanding)]:
            tot[k] += v
        rows.append({
            "invoice_date": inv.invoice_date.isoformat(),
            "invoice_no": inv.invoice_no, "invoice_number": inv.invoice_no,
            "supplier_name": inv.supplier_name or "", "supplier_mobile": inv.supplier_mobile,
            "gold": float(gold), "silver": float(silver), "diamond": float(diamond),
            "making": float(making), "cgst": float(cgst), "sgst": float(sgst), "igst": float(igst),
            "grand_total": float(inv.grand_total),
            "amount_paid": float(inv.amount_paid), "outstanding": float(inv.outstanding),
        })
    return {"rows": rows, "totals": {k: float(v) for k, v in tot.items()}}


# ── Cancelled Invoices Register ───────────────────────────────
# Added: /cancelled-invoices endpoint — the "Cancelled" report tab

@router.get("/cancelled-invoices")
async def cancelled_invoices_report(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """
    Returns all cancelled invoices for the tenant.
    Includes: invoice_no, invoice_date, cancelled_at, customer details,
    pay_mode, grand_total, notes.
    """
    tenant_id = payload["tenant_id"]

    stmt = (
        select(Invoice)
        .where(
            Invoice.tenant_id == tenant_id,
            Invoice.status    == "cancelled",
        )
        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
    )
    if from_date:
        stmt = stmt.where(Invoice.invoice_date >= from_date)
    if to_date:
        stmt = stmt.where(Invoice.invoice_date <= to_date)

    result   = await db.execute(stmt)
    invoices = result.scalars().all()

    rows = []
    for inv in invoices:
        cust = await db.get(Customer, (inv.customer_mobile, tenant_id))
        pan  = inv.customer_pan or (cust.pan if cust else None)
        rows.append({
            "invoice_no":      inv.invoice_no or f"INV-{inv.id}",
            "invoice_date":    inv.invoice_date.isoformat() if inv.invoice_date else None,
            "cancelled_at":    inv.updated_at.isoformat()   if hasattr(inv, "updated_at") and inv.updated_at else None,
            "customer_name":   inv.customer_name  or "—",
            "customer_mobile": inv.customer_mobile or "—",
            "customer_pan":    pan,
            "pay_mode":        inv.pay_mode.value if hasattr(inv.pay_mode, "value") else str(inv.pay_mode or "—"),
            "grand_total":     float(inv.grand_total or 0),
            "notes":           inv.notes or "",
        })

    return {
        "rows":        rows,
        "total_count": len(rows),
        "total_value": sum(r["grand_total"] for r in rows),
    }


# ── Section 269ST Violation Register ─────────────────────────
# Lists individual cash PAYMENTS of Rs.2,00,000 or more per the
# requirement in s.269ST of the Income Tax Act 1961.

@router.get("/section-269st")
async def section_269st_report(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """
    Section 269ST violation register.
    Returns each cash payment of Rs.2,00,000 or more.
    Penalty u/s 271DA = amount received in violation.
    """
    tenant_id = payload["tenant_id"]
    threshold = Decimal("200000")

    stmt = (
        select(Payment)
        .where(
            Payment.tenant_id == tenant_id,
            Payment.pay_mode  == "Cash",
            Payment.amount    >= threshold,
        )
        .order_by(Payment.payment_date.desc())
    )
    if from_date:
        stmt = stmt.where(Payment.payment_date >= from_date)
    if to_date:
        stmt = stmt.where(Payment.payment_date <= to_date)

    result   = await db.execute(stmt)
    payments = result.scalars().all()

    inv_cache: dict = {}
    for p in payments:
        if p.invoice_id and p.invoice_id not in inv_cache:
            inv_obj = await db.get(Invoice, p.invoice_id)
            if inv_obj:
                inv_cache[p.invoice_id] = inv_obj

    rows = []
    for p in payments:
        inv_obj = inv_cache.get(p.invoice_id) if p.invoice_id else None
        cust    = await db.get(Customer, (p.customer_mobile, tenant_id))
        cname   = (inv_obj.customer_name if inv_obj else None) or (cust.name if cust else "—")
        pan     = (inv_obj.customer_pan  if inv_obj else None) or (cust.pan  if cust else None)
        rows.append({
            "payment_date":    p.payment_date.isoformat(),
            "invoice_no":      inv_obj.invoice_no if inv_obj else "—",
            "customer_mobile": p.customer_mobile,
            "customer_name":   cname,
            "customer_pan":    pan,
            "cash_amount":     float(p.amount),
            "penalty_risk":    float(p.amount),
            "reference_no":    p.reference_no or "—",
        })

    return {
        "rows":               rows,
        "total_violations":   len(rows),
        "total_cash_amount":  sum(r["cash_amount"]  for r in rows),
        "total_penalty_risk": sum(r["penalty_risk"] for r in rows),
        "threshold":          float(threshold),
        "note": (
            "Section 269ST — No person shall receive Rs.2,00,000 or more in cash "
            "in a single transaction. Penalty u/s 271DA = amount received in violation."
        ),
    }
