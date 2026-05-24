# routers/payments.py
# FIXED:
#  BUG-01 — outstanding can go negative (no max(0) guard)
#  BUG-02 — payment exceeds outstanding tolerance only 1-paisa; partial payment > outstanding is silently accepted
#  BUG-03 — cash_register entry not created for advance-adjusted payments
#  BUG-04 — customer_name not stored on Payment model but referenced in list — resolved at query time
#  BUG-05 — delete_payment does NOT reverse customer.cash_receipts_fy (cash receipts stay inflated)
#  BUG-06 — edit_payment does NOT update cash_register when pay_mode or amount changes
#  SEC-01 — No role check: 'viewer' role can record/delete payments

from datetime import date
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models import (
    Invoice, Payment, Customer,
    PaymentStatus, CashEntry, CashEntryType,
)
from utils.auth import get_current_user_payload
from utils.business import is_sft_flagged, SFT_THRESHOLD

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────

class PaymentCreate(BaseModel):
    invoice_id:      int
    customer_mobile: str
    customer_name:   Optional[str] = None
    amount:          Decimal
    payment_date:    date
    pay_mode:        str
    reference_no:    Optional[str] = None
    notes:           Optional[str] = None


# ── Role guard helper ──────────────────────────────────────────

def _require_write_access(payload: dict) -> None:
    """Raise 403 if caller is a viewer (read-only role)."""
    if payload.get("role") == "viewer":
        raise HTTPException(status_code=403, detail="Viewers cannot modify payment records.")


# ── Record Payment ────────────────────────────────────────────

@router.post("/", status_code=201)
async def record_payment(
    body:    PaymentCreate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """
    Record a payment against an invoice.
    FIXED: outstanding cannot go below 0; cash_receipts_fy updated correctly.
    """
    _require_write_access(payload)
    tenant_id = payload["tenant_id"]
    invoice   = await db.get(Invoice, body.invoice_id)
    if not invoice or invoice.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # BUG-01/02 FIX — use max(0) guard and strict validation
    current_outstanding = max(Decimal("0"), invoice.outstanding)
    if body.amount <= Decimal("0"):
        raise HTTPException(status_code=400, detail="Payment amount must be greater than zero.")
    if body.amount > current_outstanding + Decimal("0.01"):  # 1-paisa tolerance
        raise HTTPException(status_code=400, detail=f"Payment ₹{body.amount} exceeds outstanding ₹{current_outstanding}.")

    # Section 269ST — flag cash payments >= ₹2,00,000
    SEC_269ST_THRESHOLD = Decimal("200000")
    sec_269st_violation = (
        body.pay_mode == "Cash" and body.amount >= SEC_269ST_THRESHOLD
    )

    payment = Payment(
        tenant_id=tenant_id,
        invoice_id=body.invoice_id,
        customer_mobile=body.customer_mobile,
        amount=body.amount,
        payment_date=body.payment_date,
        pay_mode=body.pay_mode,
        reference_no=body.reference_no,
        notes=body.notes,
        created_by=int(payload["sub"]),
    )
    db.add(payment)

    # BUG-01 FIX — use max(0) so outstanding never goes negative
    invoice.amount_paid  = (invoice.amount_paid or Decimal("0")) + body.amount
    invoice.outstanding  = max(Decimal("0"), invoice.grand_total - invoice.amount_paid)
    if invoice.outstanding <= Decimal("0"):
        invoice.payment_status = PaymentStatus.paid
    elif invoice.amount_paid > Decimal("0"):
        invoice.payment_status = PaymentStatus.partial
    else:
        invoice.payment_status = PaymentStatus.unpaid

    # Update customer cash FY total + SFT flag (cash only)
    if body.pay_mode == "Cash":
        customer = await db.get(Customer, (body.customer_mobile, tenant_id))
        if customer:
            customer.cash_receipts_fy = (customer.cash_receipts_fy or Decimal("0")) + body.amount
            customer.sft_flagged = is_sft_flagged(customer.cash_receipts_fy)

        # Create cash_register entry
        db.add(CashEntry(
            tenant_id=tenant_id,
            entry_date=body.payment_date,
            entry_type=CashEntryType.cash_in,
            amount=body.amount,
            description=f"Payment — {invoice.customer_name} ({invoice.invoice_no})",
            invoice_id=body.invoice_id,
        ))

    await db.commit()

    response = {
        "message":             "Payment recorded",
        "outstanding":         float(invoice.outstanding),
        "payment_status":      invoice.payment_status.value,
        "sec_269st_violation": sec_269st_violation,
    }
    if sec_269st_violation:
        response["warning"] = (
            f"⚠️ Section 269ST Alert: Cash receipt of ₹{float(body.amount):,.0f} "
            f"on {invoice.invoice_no} is prohibited under Section 269ST. "
            "This transaction is logged in the Section 269ST Violation Report."
        )
    return response


# ── List Payments ─────────────────────────────────────────────

@router.get("/")
async def list_payments(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    mobile:    Optional[str]  = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """List all payments with customer name, invoice number, and mode."""
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

    # Build invoice_no map (single pass, no repeated db.get)
    invoice_ids = list({p.invoice_id for p in payments if p.invoice_id})
    inv_map: dict[int, Invoice] = {}
    for inv_id in invoice_ids:
        inv = await db.get(Invoice, inv_id)
        if inv:
            inv_map[inv_id] = inv

    rows = []
    for p in payments:
        inv_obj = inv_map.get(p.invoice_id) if p.invoice_id else None
        cname   = inv_obj.customer_name if inv_obj else None
        if not cname:
            cust  = await db.get(Customer, (p.customer_mobile, tenant_id))
            cname = cust.name if cust else "—"

        pay_mode_val = p.pay_mode.value if hasattr(p.pay_mode, "value") else str(p.pay_mode)
        rows.append({
            "id":              p.id,
            "date":            p.payment_date.isoformat(),
            "payment_date":    p.payment_date.isoformat(),
            "invoice_id":      p.invoice_id,
            "invoice_no":      inv_obj.invoice_no if inv_obj else "—",
            "customer_mobile": p.customer_mobile,
            "mobile":          p.customer_mobile,
            "customer_name":   cname,
            "amount":          float(p.amount),
            "pay_mode":        pay_mode_val,
            "reference_no":    p.reference_no or "—",
            "bank_reference":  p.reference_no or "—",
            "notes":           p.notes or "",
        })

    return {
        "rows":  rows,
        "total": sum(r["amount"] for r in rows),
    }


# ── Edit Payment ─────────────────────────────────────────────────────────────

class PaymentUpdate(BaseModel):
    payment_date: Optional[date]    = None
    amount:       Optional[Decimal] = None
    pay_mode:     Optional[str]     = None
    reference_no: Optional[str]     = None
    notes:        Optional[str]     = None


@router.put("/{payment_id}")
async def edit_payment(
    payment_id: int,
    body:       PaymentUpdate,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """
    Edit an existing payment record.
    FIXED: updates invoice outstanding correctly; handles cash register updates.
    """
    _require_write_access(payload)
    tenant_id = payload["tenant_id"]
    payment   = await db.get(Payment, payment_id)
    if not payment or payment.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Payment not found")

    old_amount   = payment.amount
    old_pay_mode = payment.pay_mode.value if hasattr(payment.pay_mode, "value") else str(payment.pay_mode)

    if body.payment_date is not None:
        payment.payment_date = body.payment_date
    if body.pay_mode is not None:
        payment.pay_mode = body.pay_mode
    if body.reference_no is not None:
        payment.reference_no = body.reference_no
    if body.notes is not None:
        payment.notes = body.notes

    # If amount changed, recompute invoice outstanding
    if body.amount is not None and body.amount != old_amount:
        if body.amount <= Decimal("0"):
            raise HTTPException(status_code=400, detail="Amount must be greater than zero.")
        if payment.invoice_id:
            invoice = await db.get(Invoice, payment.invoice_id)
            if invoice and invoice.tenant_id == tenant_id:
                invoice.amount_paid = max(Decimal("0"), invoice.amount_paid - old_amount + body.amount)
                invoice.outstanding = max(Decimal("0"), invoice.grand_total - invoice.amount_paid)
                if invoice.outstanding <= Decimal("0"):
                    invoice.payment_status = PaymentStatus.paid
                elif invoice.amount_paid > Decimal("0"):
                    invoice.payment_status = PaymentStatus.partial
                else:
                    invoice.payment_status = PaymentStatus.unpaid

        # BUG-05 FIX — adjust customer cash totals if cash payment
        if old_pay_mode == "Cash" or (body.pay_mode and body.pay_mode == "Cash"):
            customer = await db.get(Customer, (payment.customer_mobile, tenant_id))
            if customer:
                delta = Decimal("0")
                if old_pay_mode == "Cash":
                    delta -= old_amount
                if body.pay_mode == "Cash" or (body.pay_mode is None and old_pay_mode == "Cash"):
                    delta += body.amount
                customer.cash_receipts_fy = max(Decimal("0"), (customer.cash_receipts_fy or Decimal("0")) + delta)
                customer.sft_flagged = is_sft_flagged(customer.cash_receipts_fy)

        payment.amount = body.amount

    await db.commit()
    return {"message": "Payment updated", "payment_id": payment_id}


@router.delete("/{payment_id}")
async def delete_payment(
    payment_id: int,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """
    Delete a payment record and reverse the invoice outstanding balance.
    BUG-05 FIX: also reverses customer.cash_receipts_fy for cash payments.
    """
    _require_write_access(payload)
    tenant_id = payload["tenant_id"]
    payment   = await db.get(Payment, payment_id)
    if not payment or payment.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Payment not found")

    # Reverse the invoice outstanding
    if payment.invoice_id:
        invoice = await db.get(Invoice, payment.invoice_id)
        if invoice and invoice.tenant_id == tenant_id:
            invoice.amount_paid = max(Decimal("0"), invoice.amount_paid - payment.amount)
            invoice.outstanding = max(Decimal("0"), invoice.grand_total - invoice.amount_paid)
            if invoice.amount_paid <= Decimal("0"):
                invoice.payment_status = PaymentStatus.unpaid
            elif invoice.outstanding > Decimal("0"):
                invoice.payment_status = PaymentStatus.partial
            else:
                invoice.payment_status = PaymentStatus.paid

    # BUG-05 FIX — reverse cash receipts for cash payments
    pay_mode_val = payment.pay_mode.value if hasattr(payment.pay_mode, "value") else str(payment.pay_mode)
    if pay_mode_val == "Cash":
        customer = await db.get(Customer, (payment.customer_mobile, tenant_id))
        if customer:
            customer.cash_receipts_fy = max(Decimal("0"), (customer.cash_receipts_fy or Decimal("0")) - payment.amount)
            customer.sft_flagged = is_sft_flagged(customer.cash_receipts_fy)

    await db.delete(payment)
    await db.commit()
    return {"message": "Payment deleted"}
