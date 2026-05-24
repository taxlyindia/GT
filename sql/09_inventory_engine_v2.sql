-- ═══════════════════════════════════════════════════════════════════════
-- Migration 09: Inventory Engine v2
-- Implements full spec requirements:
--   • movement_type column on stock_transactions (explicit 6-value enum)
--   • Immutable snapshot columns: original_qty, original_rate, original_value
--   • Reversal linkage: reversal_of_movement_id, reversal_type
--   • Rename sale_fifo_allocations → inventory_fifo_consumption with full fields
--   • All existing rows backfilled safely
-- ═══════════════════════════════════════════════════════════════════════

BEGIN;

-- ── 1. Add movement_type to stock_transactions ─────────────────────────
-- Explicit 6-value type completely defines every movement in the register.
-- Allowed values per spec:
--   purchase_in          – stock received from supplier invoice
--   sale_out             – stock deducted for customer sale
--   purchase_cancel_out  – exact reversal of a purchase IN (cancellation only)
--   sale_cancel_in       – exact reversal of a sale OUT (cancellation only)
--   opening              – opening balance entry
--   adjustment           – manual stock correction (NOT used for edit/cancel)

ALTER TABLE stock_transactions
    ADD COLUMN IF NOT EXISTS movement_type VARCHAR(30)
        CHECK (movement_type IN (
            'purchase_in',
            'sale_out',
            'purchase_cancel_out',
            'sale_cancel_in',
            'opening',
            'adjustment'
        ));

-- Backfill movement_type from existing txn_type
UPDATE stock_transactions SET movement_type =
    CASE txn_type
        WHEN 'purchase'   THEN 'purchase_in'
        WHEN 'sale'       THEN 'sale_out'
        WHEN 'opening'    THEN 'opening'
        ELSE 'adjustment'
    END
WHERE movement_type IS NULL;

-- Make non-nullable now that backfill is done
ALTER TABLE stock_transactions
    ALTER COLUMN movement_type SET NOT NULL;

-- ── 2. Immutable historical snapshot columns ────────────────────────────
-- Stored at posting time; NEVER updated afterwards.
ALTER TABLE stock_transactions
    ADD COLUMN IF NOT EXISTS original_qty   NUMERIC(15,3),
    ADD COLUMN IF NOT EXISTS original_rate  NUMERIC(15,2),
    ADD COLUMN IF NOT EXISTS original_value NUMERIC(15,2);

-- Backfill from existing data
UPDATE stock_transactions
SET original_qty   = ABS(qty),
    original_rate  = COALESCE(purchase_rate, 0),
    original_value = ABS(qty) * COALESCE(purchase_rate, 0)
WHERE original_qty IS NULL;

-- ── 3. Reversal linkage columns ─────────────────────────────────────────
ALTER TABLE stock_transactions
    ADD COLUMN IF NOT EXISTS reversal_of_movement_id INTEGER
        REFERENCES stock_transactions(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS reversal_type VARCHAR(30)
        CHECK (reversal_type IN ('purchase_cancel', 'sale_cancel', NULL));

CREATE INDEX IF NOT EXISTS ix_stxn_reversal ON stock_transactions(reversal_of_movement_id)
    WHERE reversal_of_movement_id IS NOT NULL;

-- ── 4. JSON snapshot column ─────────────────────────────────────────────
-- Stores the FIFO layer allocation as a JSON snapshot at posting time.
-- Example: [{"lot_txn_id": 5, "qty": 100, "rate": 13000},
--           {"lot_txn_id": 8, "qty": 80,  "rate": 13175}]
ALTER TABLE stock_transactions
    ADD COLUMN IF NOT EXISTS fifo_snapshot JSONB;

-- ── 5. Drop old sale_fifo_allocations, create inventory_fifo_consumption ─
-- The old table had fewer fields; spec requires consumed_value + invoice linkage.
DROP TABLE IF EXISTS sale_fifo_allocations;

CREATE TABLE IF NOT EXISTS inventory_fifo_consumption (
    id                SERIAL      PRIMARY KEY,
    tenant_id         INTEGER     NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- The sale OUT / purchase_cancel_out StockTransaction this row belongs to
    movement_id       INTEGER     NOT NULL REFERENCES stock_transactions(id) ON DELETE CASCADE,

    -- Business document linkage
    invoice_id        INTEGER     REFERENCES invoices(id)        ON DELETE SET NULL,
    invoice_item_id   INTEGER     REFERENCES invoice_items(id)   ON DELETE SET NULL,

    -- The purchase/opening IN lot that was consumed
    purchase_layer_id INTEGER     NOT NULL REFERENCES stock_transactions(id) ON DELETE CASCADE,

    -- Consumption detail (immutable snapshot)
    consumed_qty      NUMERIC(15,3) NOT NULL CHECK (consumed_qty > 0),
    consumed_rate     NUMERIC(15,2) NOT NULL,
    consumed_value    NUMERIC(15,2) NOT NULL,   -- = consumed_qty * consumed_rate

    created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_fifo_cons_movement  ON inventory_fifo_consumption(movement_id);
CREATE INDEX IF NOT EXISTS ix_fifo_cons_layer     ON inventory_fifo_consumption(purchase_layer_id);
CREATE INDEX IF NOT EXISTS ix_fifo_cons_invoice   ON inventory_fifo_consumption(invoice_id);
CREATE INDEX IF NOT EXISTS ix_fifo_cons_tenant    ON inventory_fifo_consumption(tenant_id);

COMMIT;
