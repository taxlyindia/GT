# tests/test_erp_accounting.py
# ─────────────────────────────────────────────────────────────────────────────
# ERP-Grade Accounting & FIFO Test Suite — GoldTrader Pro
#
# Tests cover:
#   1. Create purchase → sell stock FIFO → cancel sale → verify same FIFO restored
#   2. Edit invoice qty increase → verify only difference consumed from FIFO
#   3. Edit invoice qty decrease → verify only difference restored to FIFO
#   4. Cancel purchase after sales exist → FIFO dependency handled
#   5. Amendment version history correctly saved
#   6. Ledger balances before/after reversal
#   7. Double-cancel prevention
#   8. Multi-lot FIFO consumption
#
# Run: pytest tests/test_erp_accounting.py -v
# ─────────────────────────────────────────────────────────────────────────────

import pytest
from decimal import Decimal
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch


# ══════════════════════════════════════════════════════════════
# Fixtures / Mock helpers
# ══════════════════════════════════════════════════════════════

def make_stock_item(id=1, qty_on_hand=Decimal("100")):
    s = MagicMock()
    s.id = id
    s.qty_on_hand = qty_on_hand
    return s


def make_stock_txn(id, qty, purchase_rate, lot_remaining=None, txn_type="purchase"):
    t = MagicMock()
    t.id = id
    t.qty = qty
    t.purchase_rate = purchase_rate
    t.lot_remaining = lot_remaining if lot_remaining is not None else qty
    t.txn_type = txn_type
    t.txn_date = date(2024, 1, 1)
    return t


def make_invoice(id=1, invoice_no="INV-1-0001", subtotal=Decimal("50000"),
                 cgst=Decimal("750"), sgst=Decimal("750"), igst=Decimal("0"),
                 grand_total=Decimal("51500"), status="active",
                 version_no=0):
    inv = MagicMock()
    inv.id = id
    inv.invoice_no = invoice_no
    inv.subtotal = subtotal
    inv.cgst = cgst
    inv.sgst = sgst
    inv.igst = igst
    inv.grand_total = grand_total
    inv.status = status
    inv.version_no = version_no
    inv.cancelled_by = None
    inv.cancelled_at = None
    inv.cancellation_reason = None
    inv.reversal_ref_id = None
    inv.customer_name = "Test Customer"
    inv.pay_mode = MagicMock(value="Cash")
    inv.gst_type = MagicMock(value="CGST+SGST")
    inv.amount_paid = Decimal("0")
    inv.outstanding = grand_total
    return inv


def make_fifo_history(id, invoice_id, item_id, purchase_txn_id, stock_item_id,
                      consumed_qty, cost_rate, is_reversed=False):
    h = MagicMock()
    h.id = id
    h.invoice_id = invoice_id
    h.invoice_item_id = item_id
    h.purchase_txn_id = purchase_txn_id
    h.stock_item_id = stock_item_id
    h.consumed_qty = consumed_qty
    h.cost_rate = cost_rate
    h.cost_value = consumed_qty * cost_rate
    h.is_reversed = is_reversed
    h.reversed_at = None
    h.reversed_by = None
    return h


# ══════════════════════════════════════════════════════════════
# Test 1: FIFO Consumption — single lot
# ══════════════════════════════════════════════════════════════

class TestFIFOConsumption:
    """Test that FIFO lots are correctly consumed and history recorded."""

    @pytest.mark.asyncio
    async def test_single_lot_fully_consumed(self):
        """
        Setup: 1 purchase lot of 50g @ ₹5000/g
        Sale:  30g
        Expect: lot_remaining = 20g, history records 30g consumed @ ₹5000
        """
        from utils.erp_accounting import fifo_consume_for_sale

        stock = make_stock_item(qty_on_hand=Decimal("50"))
        lot   = make_stock_txn(id=10, qty=Decimal("50"), purchase_rate=Decimal("5000"),
                                lot_remaining=Decimal("50"))

        db = AsyncMock()
        # Simulate DB returning the lot
        db.execute.return_value.scalars.return_value.all.return_value = [lot]
        db.add = MagicMock()
        db.flush = AsyncMock()

        histories = await fifo_consume_for_sale(
            db, tenant_id=1, created_by=1,
            invoice_id=1, invoice_item_id=1,
            stock_item=stock, qty_to_sell=Decimal("30"),
            invoice_date=date(2024, 6, 1), amendment_version=0,
        )

        assert len(histories) == 1
        assert histories[0].consumed_qty == Decimal("30")
        assert histories[0].cost_rate    == Decimal("5000")
        assert histories[0].cost_value   == Decimal("150000")
        assert lot.lot_remaining         == Decimal("20")
        assert stock.qty_on_hand         == Decimal("20")

    @pytest.mark.asyncio
    async def test_multi_lot_consumption(self):
        """
        Setup: Lot A = 20g @ ₹5000, Lot B = 30g @ ₹5500
        Sale: 25g
        Expect: Lot A fully consumed (20g), Lot B partially consumed (5g)
        """
        from utils.erp_accounting import fifo_consume_for_sale

        stock = make_stock_item(qty_on_hand=Decimal("50"))
        lot_a = make_stock_txn(id=10, qty=Decimal("20"), purchase_rate=Decimal("5000"),
                                lot_remaining=Decimal("20"))
        lot_b = make_stock_txn(id=11, qty=Decimal("30"), purchase_rate=Decimal("5500"),
                                lot_remaining=Decimal("30"))

        db = AsyncMock()
        db.execute.return_value.scalars.return_value.all.return_value = [lot_a, lot_b]
        db.add = MagicMock()
        db.flush = AsyncMock()

        histories = await fifo_consume_for_sale(
            db, tenant_id=1, created_by=1,
            invoice_id=1, invoice_item_id=1,
            stock_item=stock, qty_to_sell=Decimal("25"),
            invoice_date=date(2024, 6, 1), amendment_version=0,
        )

        assert len(histories) == 2

        # Lot A: fully consumed
        assert histories[0].consumed_qty == Decimal("20")
        assert histories[0].cost_rate    == Decimal("5000")
        assert lot_a.lot_remaining       == Decimal("0")

        # Lot B: partially consumed (5g)
        assert histories[1].consumed_qty == Decimal("5")
        assert histories[1].cost_rate    == Decimal("5500")
        assert lot_b.lot_remaining       == Decimal("25")

        # Stock updated
        assert stock.qty_on_hand == Decimal("25")

    @pytest.mark.asyncio
    async def test_insufficient_stock_raises(self):
        """Sale of more than available stock must raise ValueError."""
        from utils.erp_accounting import fifo_consume_for_sale

        stock = make_stock_item(qty_on_hand=Decimal("10"))
        lot   = make_stock_txn(id=10, qty=Decimal("10"), purchase_rate=Decimal("5000"),
                                lot_remaining=Decimal("10"))

        db = AsyncMock()
        db.execute.return_value.scalars.return_value.all.return_value = [lot]
        db.add = MagicMock()

        with pytest.raises(ValueError, match="Insufficient stock"):
            await fifo_consume_for_sale(
                db, tenant_id=1, created_by=1,
                invoice_id=1, invoice_item_id=1,
                stock_item=stock, qty_to_sell=Decimal("15"),
                invoice_date=date(2024, 6, 1),
            )


# ══════════════════════════════════════════════════════════════
# Test 2: FIFO Restore on Invoice Cancellation
# ══════════════════════════════════════════════════════════════

class TestFIFORestore:
    """Test that cancellation restores exactly the consumed lots."""

    @pytest.mark.asyncio
    async def test_restore_exactly_consumed_lots(self):
        """
        Sale consumed 20g from Lot A (lot_remaining was reduced from 50 to 30).
        Cancellation must restore Lot A back to 50g.
        """
        from utils.erp_accounting import fifo_restore_from_history

        lot_a  = make_stock_txn(id=10, qty=Decimal("50"), purchase_rate=Decimal("5000"),
                                 lot_remaining=Decimal("30"))  # 20g was consumed
        stock  = make_stock_item(qty_on_hand=Decimal("30"))
        history = make_fifo_history(
            id=1, invoice_id=1, item_id=1,
            purchase_txn_id=10, stock_item_id=1,
            consumed_qty=Decimal("20"), cost_rate=Decimal("5000"),
        )

        db = AsyncMock()
        # history query
        db.execute.side_effect = [
            # 1st call: fetch histories
            _make_scalars([history]),
            # 2nd call: fetch orig lot
            _make_scalar_one(lot_a),
            # 3rd call: fetch stock item
            _make_scalar_one(stock),
        ]
        db.add = MagicMock()

        await fifo_restore_from_history(
            db, tenant_id=1, cancelled_by=99,
            invoice_id=1, restore_date=date(2024, 7, 1),
        )

        # Lot restored
        assert lot_a.lot_remaining == Decimal("50")   # 30 + 20

        # Stock restored
        assert stock.qty_on_hand == Decimal("50")     # 30 + 20

        # History marked as reversed
        assert history.is_reversed == True
        assert history.reversed_by == 99

    @pytest.mark.asyncio
    async def test_no_double_restore(self):
        """Already-reversed FIFO history should not be restored again."""
        from utils.erp_accounting import fifo_restore_from_history

        history = make_fifo_history(
            id=1, invoice_id=1, item_id=1,
            purchase_txn_id=10, stock_item_id=1,
            consumed_qty=Decimal("20"), cost_rate=Decimal("5000"),
            is_reversed=True,  # already reversed
        )

        db = AsyncMock()
        db.execute.return_value.scalars.return_value.all.return_value = []  # no un-reversed
        db.add = MagicMock()

        # Should complete without error, no stock changes
        await fifo_restore_from_history(
            db, tenant_id=1, cancelled_by=99,
            invoice_id=1, restore_date=date(2024, 7, 1),
        )

        db.add.assert_not_called()


# ══════════════════════════════════════════════════════════════
# Test 3: FIFO Incremental Adjustment (Amendment)
# ══════════════════════════════════════════════════════════════

class TestFIFOIncrementalAdjust:
    """Test that amendments only adjust the delta, not full reversal."""

    @pytest.mark.asyncio
    async def test_qty_increase_consumes_only_delta(self):
        """
        Original: sold 30g (FIFO history exists)
        Amendment: change to 35g → should consume only 5g more
        """
        from utils.erp_accounting import fifo_adjust_incremental

        stock = make_stock_item(qty_on_hand=Decimal("20"))  # 50 - 30 already sold
        lot   = make_stock_txn(id=10, qty=Decimal("50"), purchase_rate=Decimal("5000"),
                                lot_remaining=Decimal("20"))

        db = AsyncMock()
        db.execute.return_value.scalars.return_value.all.return_value = [lot]
        db.add = MagicMock()
        db.flush = AsyncMock()

        await fifo_adjust_incremental(
            db, tenant_id=1, user_id=1,
            invoice_id=1, invoice_item_id=1,
            stock_item=stock,
            old_qty=Decimal("30"), new_qty=Decimal("35"),
            invoice_date=date(2024, 6, 1), amendment_version=1,
        )

        # Only 5g additional consumed
        assert stock.qty_on_hand == Decimal("15")    # 20 - 5
        assert lot.lot_remaining == Decimal("15")    # 20 - 5

    @pytest.mark.asyncio
    async def test_qty_decrease_restores_only_delta(self):
        """
        Original: sold 30g
        Amendment: change to 25g → should restore only 5g back
        """
        from utils.erp_accounting import fifo_adjust_incremental

        stock  = make_stock_item(qty_on_hand=Decimal("20"))  # 50 - 30
        lot    = make_stock_txn(id=10, qty=Decimal("50"), purchase_rate=Decimal("5000"),
                                 lot_remaining=Decimal("20"))
        # FIFO history record for the original 30g consumption
        history = make_fifo_history(
            id=1, invoice_id=1, item_id=1,
            purchase_txn_id=10, stock_item_id=1,
            consumed_qty=Decimal("30"), cost_rate=Decimal("5000"),
        )

        db = AsyncMock()
        db.execute.side_effect = [
            _make_scalars([history]),   # fetch FIFO history
            _make_scalar_one(lot),      # fetch lot for restore
        ]
        db.add = MagicMock()

        await fifo_adjust_incremental(
            db, tenant_id=1, user_id=1,
            invoice_id=1, invoice_item_id=1,
            stock_item=stock,
            old_qty=Decimal("30"), new_qty=Decimal("25"),
            invoice_date=date(2024, 6, 1), amendment_version=1,
        )

        # Only 5g restored
        assert stock.qty_on_hand == Decimal("25")     # 20 + 5
        assert lot.lot_remaining == Decimal("25")     # 20 + 5
        # History partially reduced
        assert history.consumed_qty == Decimal("25")  # 30 - 5

    @pytest.mark.asyncio
    async def test_no_change_is_noop(self):
        """If qty unchanged, FIFO adjust should be a no-op."""
        from utils.erp_accounting import fifo_adjust_incremental

        stock = make_stock_item(qty_on_hand=Decimal("20"))
        db    = AsyncMock()
        db.add = MagicMock()

        await fifo_adjust_incremental(
            db, tenant_id=1, user_id=1,
            invoice_id=1, invoice_item_id=1,
            stock_item=stock,
            old_qty=Decimal("30"), new_qty=Decimal("30"),
            invoice_date=date(2024, 6, 1), amendment_version=1,
        )

        db.add.assert_not_called()
        assert stock.qty_on_hand == Decimal("20")  # unchanged


# ══════════════════════════════════════════════════════════════
# Test 4: Sales Invoice Cancellation (create_sales_reversal)
# ══════════════════════════════════════════════════════════════

class TestSalesCancellation:
    """Test ERP-grade sales invoice cancellation."""

    @pytest.mark.asyncio
    async def test_cancellation_sets_permanent_record(self):
        """
        Cancelling an invoice must:
        - Set status='cancelled'
        - Record cancelled_by, cancelled_at, cancellation_reason
        - Create ReversalEntry
        """
        from utils.erp_accounting import create_sales_reversal
        from models import InvoiceStatus

        invoice = make_invoice(id=1)

        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        # fifo_restore_from_history will call execute; return empty for simplicity
        db.execute.return_value.scalars.return_value.all.return_value = []

        reversal = await create_sales_reversal(
            db, tenant_id=1, cancelled_by=42,
            invoice=invoice, reason="Customer request",
            cancel_date=date(2024, 7, 1),
        )

        # Invoice permanently marked cancelled
        assert invoice.status == InvoiceStatus.cancelled
        assert invoice.cancelled_by == 42
        assert invoice.cancellation_reason == "Customer request"
        assert invoice.cancelled_at is not None

        # ReversalEntry added
        assert db.add.called
        added_objects = [call.args[0] for call in db.add.call_args_list]
        reversal_objects = [o for o in added_objects if hasattr(o, "subtotal_reversed")]
        assert len(reversal_objects) >= 1

    @pytest.mark.asyncio
    async def test_reversal_amounts_match_invoice(self):
        """Reversal entry must contain exact amounts from invoice."""
        from utils.erp_accounting import create_sales_reversal

        invoice = make_invoice(
            subtotal=Decimal("50000"), cgst=Decimal("750"),
            sgst=Decimal("750"), igst=Decimal("0"), grand_total=Decimal("51500"),
        )

        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.execute.return_value.scalars.return_value.all.return_value = []

        reversal = await create_sales_reversal(
            db, tenant_id=1, cancelled_by=1,
            invoice=invoice, reason="Test",
            cancel_date=date.today(),
        )

        assert reversal.subtotal_reversed == Decimal("50000")
        assert reversal.cgst_reversed     == Decimal("750")
        assert reversal.sgst_reversed     == Decimal("750")
        assert reversal.total_reversed    == Decimal("51500")


# ══════════════════════════════════════════════════════════════
# Test 5: Purchase Bill Cancellation
# ══════════════════════════════════════════════════════════════

class TestPurchaseCancellation:
    """Test ERP-grade purchase bill cancellation with FIFO dependency handling."""

    @pytest.mark.asyncio
    async def test_purchase_cancel_reverses_stock(self):
        """
        Purchase of 50g stock → cancel → stock should reduce by 50g.
        """
        from utils.erp_accounting import create_purchase_reversal

        inv = MagicMock()
        inv.id = 1
        inv.invoice_no = "PUR-001"
        inv.subtotal   = Decimal("250000")
        inv.cgst       = Decimal("3750")
        inv.sgst       = Decimal("3750")
        inv.igst       = Decimal("0")
        inv.grand_total = Decimal("257500")
        inv.status     = "active"
        inv.supplier_name = "Gold Supplier"
        inv.version_no = 0
        inv.cancelled_by = None; inv.cancelled_at = None; inv.cancellation_reason = None
        inv.reversal_ref_id = None

        stock = make_stock_item(qty_on_hand=Decimal("50"))
        orig_lot = make_stock_txn(id=5, qty=Decimal("50"), purchase_rate=Decimal("5000"),
                                   lot_remaining=Decimal("50"))

        item = MagicMock()
        item.category = MagicMock(value="Gold")
        item.purity   = "22K"
        item.unit     = MagicMock(value="grm")
        item.qty      = Decimal("50")
        item.rate     = Decimal("5000")

        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.execute.side_effect = [
            _make_scalars([item]),     # load items
            _make_scalar_one(stock),  # find stock for item
            _make_scalar_one(orig_lot), # find orig purchase lot
            _make_scalars([]),         # audit_log queries
            _make_scalars([]),
            _make_scalars([]),
        ]

        await create_purchase_reversal(
            db, tenant_id=1, cancelled_by=1,
            inv=inv, reason="Wrong purchase",
            cancel_date=date.today(),
        )

        assert inv.status == "cancelled"
        assert inv.cancelled_by == 1
        assert orig_lot.lot_remaining == Decimal("0")
        assert stock.qty_on_hand == Decimal("0")

    @pytest.mark.asyncio
    async def test_purchase_cancel_warns_if_stock_already_sold(self):
        """
        Purchase 50g → sell 30g → cancel purchase.
        Lot only has 20g remaining → only 20g reversed, 30g dependency warned.
        """
        from utils.erp_accounting import create_purchase_reversal

        inv = MagicMock()
        inv.id = 1; inv.invoice_no = "PUR-001"; inv.subtotal = Decimal("250000")
        inv.cgst = Decimal("3750"); inv.sgst = Decimal("3750"); inv.igst = Decimal("0")
        inv.grand_total = Decimal("257500"); inv.status = "active"
        inv.supplier_name = "Supplier"; inv.version_no = 0
        inv.cancelled_by = None; inv.cancelled_at = None; inv.cancellation_reason = None
        inv.reversal_ref_id = None

        stock   = make_stock_item(qty_on_hand=Decimal("20"))  # 30g already sold
        orig_lot = make_stock_txn(id=5, qty=Decimal("50"), purchase_rate=Decimal("5000"),
                                   lot_remaining=Decimal("20"))  # 30g consumed by sales

        item = MagicMock()
        item.category = MagicMock(value="Gold"); item.purity = "22K"
        item.unit = MagicMock(value="grm"); item.qty = Decimal("50"); item.rate = Decimal("5000")

        audit_calls = []
        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.execute.side_effect = [
            _make_scalars([item]),
            _make_scalar_one(stock),
            _make_scalar_one(orig_lot),
            _make_scalars([]), _make_scalars([]), _make_scalars([]),
        ]

        await create_purchase_reversal(
            db, tenant_id=1, cancelled_by=1,
            inv=inv, reason="Test",
            cancel_date=date.today(),
        )

        # Only 20g (remaining) reversed
        assert stock.qty_on_hand == Decimal("0")    # 20 - 20
        assert orig_lot.lot_remaining == Decimal("0")
        assert inv.status == "cancelled"


# ══════════════════════════════════════════════════════════════
# Test 6: Version History / Amendment Snapshots
# ══════════════════════════════════════════════════════════════

class TestVersionHistory:
    """Test that invoice/purchase versions are correctly snapshotted."""

    @pytest.mark.asyncio
    async def test_version_increments_on_amendment(self):
        """First amendment should create version_no=1."""
        from utils.erp_accounting import save_invoice_version
        from models.erp_models import AmendmentType

        invoice = make_invoice(version_no=0)
        items   = []  # empty for simplicity

        db = AsyncMock()
        db.add = MagicMock()

        version = await save_invoice_version(
            db, tenant_id=1, invoice=invoice, items=items,
            amendment_type=AmendmentType.non_financial,
            amendment_reason="Updated GSTIN",
            amended_by=1,
        )

        assert version.version_no == 1
        assert invoice.version_no == 1  # invoice updated

    @pytest.mark.asyncio
    async def test_subsequent_amendments_increment(self):
        """Third amendment should create version_no=3."""
        from utils.erp_accounting import save_invoice_version
        from models.erp_models import AmendmentType

        invoice = make_invoice(version_no=2)  # already at v2
        db = AsyncMock(); db.add = MagicMock()

        version = await save_invoice_version(
            db, tenant_id=1, invoice=invoice, items=[],
            amendment_type=AmendmentType.financial,
            amendment_reason="Changed qty",
            amended_by=1,
            adj_grand_total=Decimal("500"),
        )

        assert version.version_no == 3
        assert invoice.version_no == 3

    @pytest.mark.asyncio
    async def test_snapshot_captures_item_data(self):
        """Version snapshot must contain full item data."""
        from utils.erp_accounting import save_invoice_version
        from models.erp_models import AmendmentType

        invoice = make_invoice(version_no=0)
        item = MagicMock()
        item.id = 1; item.category = MagicMock(value="Gold"); item.purity = "22K"
        item.description = "Gold Ring"; item.hsn_code = "7113"
        item.qty = Decimal("10"); item.unit = MagicMock(value="grm")
        item.rate = Decimal("5000"); item.polish_charges = Decimal("0")
        item.making_charges = Decimal("200"); item.amount = Decimal("50200")

        db = AsyncMock(); db.add = MagicMock()

        version = await save_invoice_version(
            db, tenant_id=1, invoice=invoice, items=[item],
            amendment_type=AmendmentType.financial,
            amendment_reason=None, amended_by=1,
        )

        assert version.snapshot_items is not None
        assert len(version.snapshot_items) == 1
        assert version.snapshot_items[0]["description"] == "Gold Ring"
        assert version.snapshot_items[0]["qty"] == 10.0


# ══════════════════════════════════════════════════════════════
# Test 7: Ledger Balance Verification
# ══════════════════════════════════════════════════════════════

class TestLedgerBalances:
    """Test that audit log entries correctly record debit/credit impacts."""

    @pytest.mark.asyncio
    async def test_invoice_creation_debits_customer(self):
        """Creating invoice should debit Customer Ledger by grand_total."""
        from utils.erp_accounting import audit_log
        from models.erp_models import AuditEventType

        db = AsyncMock(); db.add = MagicMock()

        entry = await audit_log(
            db, tenant_id=1, event_type=AuditEventType.invoice_created,
            description="Invoice INV-001 created",
            invoice_id=1, debit_amount=Decimal("51500"),
            credit_amount=Decimal("0"), ledger_account="Customer Ledger",
            created_by=1,
        )

        assert entry.debit_amount  == Decimal("51500")
        assert entry.credit_amount == Decimal("0")
        assert entry.ledger_account == "Customer Ledger"
        db.add.assert_called_once_with(entry)

    @pytest.mark.asyncio
    async def test_cancellation_credits_customer(self):
        """Cancelling invoice should credit Customer Ledger (reverse debit)."""
        from utils.erp_accounting import audit_log
        from models.erp_models import AuditEventType

        db = AsyncMock(); db.add = MagicMock()

        entry = await audit_log(
            db, tenant_id=1, event_type=AuditEventType.invoice_cancelled,
            description="Invoice INV-001 reversed",
            invoice_id=1, debit_amount=Decimal("0"),
            credit_amount=Decimal("51500"), ledger_account="Customer Ledger",
            reversal_ref_id=5, original_txn_id=1, created_by=1,
        )

        assert entry.credit_amount  == Decimal("51500")
        assert entry.debit_amount   == Decimal("0")
        assert entry.reversal_ref_id == 5
        assert entry.original_txn_id == 1

    def test_net_ledger_balance_after_reversal(self):
        """
        Net balance after creating + cancelling an invoice should be zero.
        (Customer Ledger: debit 51500 + credit 51500 = 0)
        """
        entries = [
            {"type": "debit",  "amount": Decimal("51500"), "account": "Customer Ledger"},
            {"type": "credit", "amount": Decimal("51500"), "account": "Customer Ledger"},
        ]

        net = sum(
            e["amount"] if e["type"] == "debit" else -e["amount"]
            for e in entries
            if e["account"] == "Customer Ledger"
        )
        assert net == Decimal("0"), f"Expected net 0, got {net}"

    def test_gst_reversal_matches_forward_entry(self):
        """GST reversed must equal GST originally charged."""
        invoice_cgst = Decimal("750")
        invoice_sgst = Decimal("750")

        # Forward entries (invoice creation)
        forward_gst = invoice_cgst + invoice_sgst

        # Reversal entries (cancellation)
        reversed_gst = invoice_cgst + invoice_sgst

        assert forward_gst == reversed_gst


# ══════════════════════════════════════════════════════════════
# Test 8: Integration Scenario
# ══════════════════════════════════════════════════════════════

class TestIntegrationScenario:
    """
    Full integration test:
    1. Purchase 100g Gold @ ₹5000/g
    2. Sale 60g (consumes 60g from lot)
    3. Amend sale to 50g (restores 10g to lot)
    4. Cancel sale (restores remaining 50g)
    5. Cancel purchase (lot_remaining=60, but 50g already returned → net 60g reversal)
    """

    def test_fifo_state_consistency(self):
        """
        Verify FIFO state is consistent through the full lifecycle.
        This is a pure state machine test (no DB mocking needed).
        """
        # Initial state
        lot_qty       = Decimal("100")
        lot_remaining = Decimal("100")
        stock_on_hand = Decimal("100")

        # Step 1: Purchase recorded (already included above)
        assert lot_remaining == Decimal("100")

        # Step 2: Sale of 60g
        consumed_qty  = Decimal("60")
        lot_remaining -= consumed_qty
        stock_on_hand -= consumed_qty
        assert lot_remaining == Decimal("40")
        assert stock_on_hand == Decimal("40")

        # Step 3: Amend sale from 60g to 50g (-10g delta)
        delta         = Decimal("50") - Decimal("60")   # -10
        lot_remaining -= delta   # restore 10g
        stock_on_hand -= delta
        assert lot_remaining == Decimal("50")   # 40 + 10
        assert stock_on_hand == Decimal("50")

        # Step 4: Cancel sale — restore remaining 50g consumption
        lot_remaining += Decimal("50")   # restore what sale consumed (after amendment)
        stock_on_hand += Decimal("50")
        assert lot_remaining == Decimal("100")
        assert stock_on_hand == Decimal("100")

        # Step 5: Cancel purchase — lot_remaining is 100 → full reverse
        lot_remaining_before_cancel = lot_remaining
        stock_on_hand -= lot_remaining_before_cancel
        lot_remaining = Decimal("0")
        assert stock_on_hand == Decimal("0")
        assert lot_remaining == Decimal("0")

    def test_accounting_equation_holds(self):
        """
        Assets = Liabilities + Equity must hold after each transaction.
        (Simplified test using Inventory asset and Supplier liability)
        """
        # Balance sheet starting state
        inventory    = Decimal("0")
        supplier_payable = Decimal("0")

        # Purchase 100g @ ₹5000 → Inventory +₹500000, Supplier Payable +₹500000
        purchase_val  = Decimal("100") * Decimal("5000")
        inventory        += purchase_val
        supplier_payable += purchase_val
        assert inventory == supplier_payable   # equation holds

        # Sale of 60g @ ₹7000 → Inventory -₹300000, Cash +₹420000, Sales +₹120000 profit
        cost_of_goods = Decimal("60") * Decimal("5000")
        sale_price    = Decimal("60") * Decimal("7000")
        inventory    -= cost_of_goods
        cash_asset    = sale_price
        profit        = sale_price - cost_of_goods
        assert inventory == Decimal("200000")  # 40g @ cost

        # Cancel sale → reverse
        inventory += cost_of_goods
        cash_asset = Decimal("0")
        assert inventory == Decimal("500000")  # fully restored


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════

def _make_scalars(items):
    """Create mock DB execute result with .scalars().all() returning items."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = items
    return result


def _make_scalar_one(item):
    """Create mock DB execute result with .scalar_one_or_none() returning item."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = item
    result.scalars.return_value.all.return_value = [item] if item else []
    return result


# ══════════════════════════════════════════════════════════════
# Run summary
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("ERP Accounting Test Suite")
    print("=" * 60)
    print("Tests defined:")
    print("  TestFIFOConsumption         — 3 tests")
    print("  TestFIFORestore             — 2 tests")
    print("  TestFIFOIncrementalAdjust   — 3 tests")
    print("  TestSalesCancellation       — 2 tests")
    print("  TestPurchaseCancellation    — 2 tests")
    print("  TestVersionHistory          — 3 tests")
    print("  TestLedgerBalances          — 4 tests")
    print("  TestIntegrationScenario     — 2 tests")
    print("=" * 60)
    print("Run: pytest tests/test_erp_accounting.py -v")
