# utils/business.py — Core jewellery business logic
# TCS, SFT, FIFO valuation, GST calculation, Indian FY helpers
# FIXED:
#  BUG-07 — generate_invoice_no: using total invoice count as seq can produce duplicates
#           when invoices are cancelled (count < last id). Now caller passes the max seq.
#  BUG-08 — calculate_gst: CGST rounding splits (half+half) can be 1-paisa off from total
#           Fixed by computing one component as (total - other) to guarantee exact sum.

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional


# ── Indian Financial Year ─────────────────────────────────────

def current_fy() -> tuple[date, date]:
    """
    Return the start and end of the current Indian Financial Year.
    FY 2025-26 = 01-Apr-2025 to 31-Mar-2026
    """
    today = date.today()
    if today.month >= 4:
        fy_start = date(today.year, 4, 1)
        fy_end   = date(today.year + 1, 3, 31)
    else:
        fy_start = date(today.year - 1, 4, 1)
        fy_end   = date(today.year, 3, 31)
    return fy_start, fy_end


def is_in_current_fy(d: date) -> bool:
    """Return True if the given date falls in the current Indian FY."""
    start, end = current_fy()
    return start <= d <= end


def fy_label(d: Optional[date] = None) -> str:
    """Return the FY label string, e.g. 'FY 2025-26'."""
    if not d:
        d = date.today()
    start, _ = current_fy()
    return f"FY {start.year}-{str(start.year + 1)[2:]}"


# ── GST Calculation ───────────────────────────────────────────

def calculate_gst(
    subtotal: Decimal,
    gst_rate_pct: Decimal,
    gst_type: str,
) -> dict[str, Decimal]:
    """
    Calculate GST components.
    Returns: {cgst, sgst, igst, total_gst}

    BUG-08 FIX: For CGST+SGST, compute cgst = floor(total/2) and sgst = total - cgst
    to guarantee cgst + sgst == total_gst exactly (avoids 1-paisa rounding drift).
    """
    rate  = gst_rate_pct / Decimal("100")
    total = (subtotal * rate).quantize(Decimal("0.01"), ROUND_HALF_UP)

    if gst_type == "CGST+SGST":
        # Truncate-half for CGST; sgst absorbs the rounding remainder
        cgst = (total / 2).quantize(Decimal("0.01"), rounding="ROUND_DOWN")
        sgst = total - cgst  # exact complement → cgst + sgst == total always
        return {"cgst": cgst, "sgst": sgst, "igst": Decimal("0"), "total_gst": total}
    elif gst_type == "IGST":
        return {"cgst": Decimal("0"), "sgst": Decimal("0"), "igst": total, "total_gst": total}
    else:  # Exempt
        return {"cgst": Decimal("0"), "sgst": Decimal("0"), "igst": Decimal("0"), "total_gst": Decimal("0")}


# ── TCS — Section 206C(1F) ─────────────────────────────────────

TCS_THRESHOLD = Decimal("500000")   # ₹5,00,000
TCS_RATE      = Decimal("0.01")     # 1%

def calculate_tcs(subtotal: Decimal, pay_mode: str) -> dict[str, Decimal]:
    """
    Auto-calculate TCS per Section 206C(1F).
    TCS = 1% of subtotal IF:
      - Payment mode is Cash
      - Subtotal exceeds ₹5,00,000
    """
    if pay_mode == "Cash" and subtotal > TCS_THRESHOLD:
        tcs_amount = (subtotal * TCS_RATE).quantize(Decimal("0.01"), ROUND_HALF_UP)
        return {
            "tcs_applicable": True,
            "tcs_base":       subtotal,
            "tcs_amount":     tcs_amount,
        }
    return {
        "tcs_applicable": False,
        "tcs_base":       Decimal("0"),
        "tcs_amount":     Decimal("0"),
    }


# ── SFT — Statement of Financial Transactions ─────────────────

SFT_THRESHOLD = Decimal("200000")   # ₹2,00,000 cash receipts in FY

def is_sft_flagged(cash_receipts_fy: Decimal) -> bool:
    """
    Return True if a customer's total cash receipts in the FY exceed ₹2,00,000.
    PAN becomes mandatory for such customers.
    """
    return cash_receipts_fy > SFT_THRESHOLD


def pan_is_mandatory(cash_receipts_fy: Decimal) -> bool:
    """Same as is_sft_flagged — PAN required when SFT threshold crossed."""
    return is_sft_flagged(cash_receipts_fy)


# ── FIFO Stock Valuation ──────────────────────────────────────

def fifo_valuation(lots: list[dict]) -> dict:
    """
    Calculate FIFO stock valuation from a list of purchase lots.

    Each lot: {"qty_remaining": Decimal, "purchase_rate": Decimal}
    Polish Charges are excluded from FIFO valuation.

    Returns: {"total_qty": Decimal, "total_value": Decimal, "avg_rate": Decimal}
    """
    total_qty   = Decimal("0")
    total_value = Decimal("0")

    for lot in lots:
        qty  = Decimal(str(lot["qty_remaining"]))
        rate = Decimal(str(lot["purchase_rate"]))
        if qty > 0:
            total_qty   += qty
            total_value += qty * rate

    avg_rate = (
        (total_value / total_qty).quantize(Decimal("0.01"), ROUND_HALF_UP)
        if total_qty > 0 else Decimal("0")
    )

    return {
        "total_qty":   total_qty,
        "total_value": total_value.quantize(Decimal("0.01"), ROUND_HALF_UP),
        "avg_rate":    avg_rate,
    }


# ── Invoice Number Generator ──────────────────────────────────

def generate_invoice_no(tenant_id: int, seq: int) -> str:
    """
    Generate a sequential invoice number per tenant.
    Format: INV-{tenant_id}-{zero-padded 4-digit seq}
    Example: INV-1-0284

    BUG-07: Caller must pass max(invoice.id) + 1 (or existing count + 1 from
    a filtered query), NOT total row count, to avoid collisions when invoices
    are deleted/cancelled and count < max id.
    """
    return f"INV-{tenant_id}-{str(seq).zfill(4)}"


# ── Cash FY Summary ───────────────────────────────────────────

def summarise_cash(entries: list[dict]) -> dict[str, Decimal]:
    """
    Compute FY cash KPIs from cash_register entries.
    Returns:
      - cash_collected_fy:  all cash_in entries in current FY
      - cash_deposited_fy:  all cash_to_bank entries in current FY
      - cash_on_hand:       running balance (all time)
    """
    fy_start, fy_end = current_fy()
    cash_collected  = Decimal("0")
    cash_deposited  = Decimal("0")
    cash_on_hand    = Decimal("0")

    for entry in entries:
        entry_date = entry["entry_date"]
        amount     = Decimal(str(entry["amount"]))
        etype      = entry["entry_type"]

        if fy_start <= entry_date <= fy_end:
            if etype == "cash_in":
                cash_collected += amount
            elif etype == "cash_to_bank":
                cash_deposited += amount

        # Running balance (all time)
        if etype == "cash_in":
            cash_on_hand += amount
        elif etype in ("cash_out", "cash_to_bank"):
            cash_on_hand -= amount

    return {
        "cash_collected_fy": cash_collected,
        "cash_deposited_fy": cash_deposited,
        "cash_on_hand":      max(Decimal("0"), cash_on_hand),
    }
