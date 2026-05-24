-- ═══════════════════════════════════════════════════════════════
-- Migration 08: sale_fifo_allocations
-- Purpose: persist the exact per-lot FIFO consumption at sale time
--          so cancellation can perform an exact mirror reversal without
--          any recomputation.
--
-- How it works:
--   When a sale invoice is created, _deduct_stock walks the FIFO lots
--   (oldest first) and consumes qty from each.  For each lot consumed it
--   writes one row here recording:
--     - which sale StockTransaction row was created  (sale_txn_id)
--     - which purchase/opening lot was consumed      (lot_txn_id)
--     - how many units were taken from that lot      (qty_consumed)
--     - the purchase_rate of that lot at that moment (purchase_rate)
--
--   On cancellation, _restore_stock reads these rows, reverses each
--   lot_remaining update exactly, and creates one mirror IN entry per
--   original OUT entry — no recomputation, no current-rate lookup.
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS sale_fifo_allocations (
    id            SERIAL PRIMARY KEY,
    tenant_id     INTEGER  NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- The sale OUT StockTransaction this allocation belongs to
    sale_txn_id   INTEGER  NOT NULL REFERENCES stock_transactions(id) ON DELETE CASCADE,

    -- The purchase/opening IN lot that was consumed
    lot_txn_id    INTEGER  NOT NULL REFERENCES stock_transactions(id) ON DELETE CASCADE,

    -- Qty taken from the lot for this sale
    qty_consumed  NUMERIC(15,3) NOT NULL CHECK (qty_consumed > 0),

    -- Rate of that lot at the time of the sale (snapshot — never updated)
    purchase_rate NUMERIC(15,2) NOT NULL,

    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_fifo_alloc_sale_txn ON sale_fifo_allocations(sale_txn_id);
CREATE INDEX idx_fifo_alloc_lot_txn  ON sale_fifo_allocations(lot_txn_id);
CREATE INDEX idx_fifo_alloc_tenant   ON sale_fifo_allocations(tenant_id);

-- ── Run this on the live database ────────────────────────────────────────────
-- Existing sale transactions that predate this migration have no allocations.
-- Their cancellations will fall back to the stored purchase_rate (weighted avg)
-- on the sale transaction row, which is the best approximation available for
-- historical data.  All sales created after this migration will have full
-- per-lot allocation records and support exact-layer reversal.
