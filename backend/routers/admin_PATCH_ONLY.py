# ─────────────────────────────────────────────────────────────────────────────
# PATCH FOR: backend/routers/admin.py
#
# Add this endpoint to your existing admin.py file.
# Place it after the PATCH /tenants/{id}/reset-password endpoint.
# ─────────────────────────────────────────────────────────────────────────────

# ADD THESE IMPORTS at the top of admin.py if not already present:
#   from sqlalchemy import select, delete as sql_delete, text
#   from models import (Tenant, User, Invoice, InvoiceItem, Customer, Payment,
#                       Advance, CashEntry, StockItem, StockTransaction,
#                       SupplierInvoice, SupplierInvoiceItem,
#                       SupplierPayment, SupplierAdvance, Supplier,
#                       GoogleSignupRequest)

# ─────────────────────────────────────────────────────────────────────────────
# DELETE /api/admin/tenants/{tenant_id}  — Permanently erase a tenant
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/tenants/{tenant_id}")
async def permanently_delete_tenant(
    tenant_id: int,
    payload:   dict         = Depends(get_admin_payload),   # use your existing admin-auth dep
    db:        AsyncSession = Depends(get_db),
):
    """
    Permanently delete a tenant and ALL associated data.
    Cascades through every table that holds tenant_id rows:
      Users · Invoices · Invoice Items · Customers · Payments · Advances
      Cash Entries · Stock Items · Stock Transactions
      Supplier Invoices · Supplier Invoice Items · Supplier Payments
      Supplier Advances · Suppliers · Google Signup Requests
    Finally deletes the Tenant record itself.
    """
    # Verify tenant exists
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    tid = tenant_id

    # ── Delete in dependency order (children before parents) ───────
    await db.execute(sql_delete(StockTransaction).where(StockTransaction.tenant_id == tid))
    await db.execute(sql_delete(StockItem).where(StockItem.tenant_id == tid))

    await db.execute(sql_delete(SupplierInvoiceItem).where(SupplierInvoiceItem.tenant_id == tid))
    await db.execute(sql_delete(SupplierPayment).where(SupplierPayment.tenant_id == tid))
    await db.execute(sql_delete(SupplierAdvance).where(SupplierAdvance.tenant_id == tid))
    await db.execute(sql_delete(SupplierInvoice).where(SupplierInvoice.tenant_id == tid))
    await db.execute(sql_delete(Supplier).where(Supplier.tenant_id == tid))

    await db.execute(sql_delete(InvoiceItem).where(InvoiceItem.tenant_id == tid))
    await db.execute(sql_delete(Payment).where(Payment.tenant_id == tid))
    await db.execute(sql_delete(Advance).where(Advance.tenant_id == tid))
    await db.execute(sql_delete(Invoice).where(Invoice.tenant_id == tid))
    await db.execute(sql_delete(Customer).where(Customer.tenant_id == tid))

    await db.execute(sql_delete(CashEntry).where(CashEntry.tenant_id == tid))

    # Google signup requests (may use email-based link, not tenant_id foreign key)
    try:
        await db.execute(sql_delete(GoogleSignupRequest).where(GoogleSignupRequest.tenant_id == tid))
    except Exception:
        pass  # table may not have tenant_id column — skip gracefully

    # Delete all users belonging to this tenant
    await db.execute(sql_delete(User).where(User.tenant_id == tid))

    # Finally delete the tenant itself
    await db.delete(tenant)
    await db.commit()

    return {
        "message":   f"Tenant #{tid} permanently deleted",
        "tenant_id": tid,
    }

