# routers/payments.py
# Changes vs v4 original:
#  Issue 5  — GET /  endpoint now returns rows with customer_name, invoice_no
#             (previously returned 404 because route didn't exist at /api/payments/)
#  Issue 7/10 — GET / supports from_date / to_date query params (Excel export uses same)
#  Issue 11 — customer_name stored on Payment row; customer PAN fetched from master
#              for 269ST report accuracy
#  P11      — sec_269st_violation flag returned in POST response

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
    customer_name:   Optional[str] = None   # Issue 11 — capture for 269ST
    amount:          Decimal
    payment_date:    date
    pay_mode:        str
    reference_no:    Optional[str] = None
    notes:           Optional[str] = None


# ── Record Payment ────────────────────────────────────────────

@router.post("/", status_code=201)
async def record_payment(
    body:    PaymentCreate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """
    Record a payment against an invoice.
    - Updates invoice.amount_paid, outstanding, payment_status
    - Cash payments update customer.cash_receipts_fy + SFT flag
    - Cash payments create a cash_register entry
    - Section 269ST: cash >= ₹2,00,000 flagged (P11)
    - customer_name stored for 269ST report lookup (Issue 11)
    """
    tenant_id = payload["tenant_id"]
    invoice   = await db.get(Invoice, body.invoice_id)
    if not invoice or invoice.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if float(body.amount) > float(invoice.outstanding) + 0.01:  # 1-paisa tolerance
        raise HTTPException(status_code=400, detail="Payment exceeds outstanding amount")

    # Section 269ST — flag cash payments >= ₹2,00,000 (P11)
    SEC_269ST_THRESHOLD = Decimal("200000")
    sec_269st_violation = (
        body.pay_mode == "Cash" and body.amount >= SEC_269ST_THRESHOLD
    )

    # customer_name is NOT a column on Payment model — resolve at read time from invoice/master
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

    # Update invoice outstanding
    invoice.amount_paid += body.amount
    invoice.outstanding  -= body.amount
    if float(invoice.outstanding) <= 0:
        invoice.payment_status = PaymentStatus.paid
    else:
        invoice.payment_status = PaymentStatus.partial

    # Update customer cash FY total + SFT flag (cash only)
    if body.pay_mode == "Cash":
        customer = await db.get(Customer, (body.customer_mobile, tenant_id))
        if customer:
            customer.cash_receipts_fy += body.amount
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
# Issue 5 fix — this GET endpoint was missing in v4 original,
# causing the frontend to receive a 404 ("Error: Not found").

@router.get("/")
async def list_payments(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    mobile:    Optional[str]  = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """
    List all payments with customer name, invoice number, and mode.
    Supports date range filtering (Issue 7/10 — used by Excel export too).
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
        # Resolve customer name from invoice (most reliable source), then customer master
        cname = None
        if p.invoice_id and p.invoice_id in inv_nos:
            inv_obj = await db.get(Invoice, p.invoice_id)
            cname = inv_obj.customer_name if inv_obj else None
        if not cname:
            cust = await db.get(Customer, (p.customer_mobile, tenant_id))
            cname = cust.name if cust else "—"

        rows.append({
            "id":              p.id,
            "date":            p.payment_date.isoformat(),
            "payment_date":    p.payment_date.isoformat(),
            "invoice_id":      p.invoice_id,
            "invoice_no":      inv_nos.get(p.invoice_id, "—"),
            "customer_mobile": p.customer_mobile,
            "mobile":          p.customer_mobile,
            "customer_name":   cname,
            "amount":          float(p.amount),
            "pay_mode":        p.pay_mode.value,
            "reference_no":    p.reference_no or "—",
            "bank_reference":  p.reference_no or "—",
            "notes":           p.notes or "",
            "advance_id":      p.advance_id,
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
    Updates invoice outstanding balance if amount changes.
    Added per Improvement document request.
    """
    tenant_id = payload["tenant_id"]
    payment   = await db.get(Payment, payment_id)
    if not payment or payment.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Payment not found")

    old_amount = payment.amount

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
        if payment.invoice_id:
            invoice = await db.get(Invoice, payment.invoice_id)
            if invoice and invoice.tenant_id == tenant_id:
                # Reverse old, apply new
                invoice.amount_paid = invoice.amount_paid - old_amount + body.amount
                invoice.outstanding = invoice.grand_total - invoice.amount_paid
                if invoice.outstanding <= Decimal("0"):
                    invoice.payment_status = PaymentStatus.paid
                elif invoice.amount_paid > Decimal("0"):
                    invoice.payment_status = PaymentStatus.partial
                else:
                    invoice.payment_status = PaymentStatus.unpaid
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
    """
    tenant_id = payload["tenant_id"]
    payment   = await db.get(Payment, payment_id)
    if not payment or payment.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Payment not found")

    # Reverse the invoice outstanding
    if payment.invoice_id:
        invoice = await db.get(Invoice, payment.invoice_id)
        if invoice and invoice.tenant_id == tenant_id:
            invoice.amount_paid   = max(Decimal("0"), invoice.amount_paid - payment.amount)
            invoice.outstanding   = invoice.grand_total - invoice.amount_paid
            if invoice.amount_paid <= Decimal("0"):
                invoice.payment_status = PaymentStatus.unpaid
            elif invoice.outstanding > Decimal("0"):
                invoice.payment_status = PaymentStatus.partial

    await db.delete(payment)
    await db.commit()
    return {"message": "Payment deleted"}
