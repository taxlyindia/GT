-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 08: ERP-Grade Accounting & Inventory Audit System
-- GoldTrader Pro — Taxly India Private Limited
-- Rule: "Posted transaction is permanent. Correction happens through reversal
--        or adjustment, never by rewriting history."
-- ═══════════════════════════════════════════════════════════════════════════
-- Run this migration on existing databases. It is additive — no existing
-- data is removed or modified. All ALTER TABLE statements use IF NOT EXISTS.
-- ═══════════════════════════════════════════════════════════════════════════

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────
-- 1.  FIFO Consumption History
--     Every sale must permanently record exactly which purchase lot was
--     consumed, how much, and at what cost. Cancellation and amendment
--     restore/adjust ONLY these specific lot references.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS fifo_consumption_history (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Source document
    invoice_item_id     INTEGER NOT NULL REFERENCES invoice_items(id) ON DELETE CASCADE,
    invoice_id          INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,

    -- Which FIFO purchase lot was consumed
    purchase_txn_id     INTEGER NOT NULL REFERENCES stock_transactions(id),
    stock_item_id       INTEGER NOT NULL REFERENCES stock_items(id),

    -- How much was consumed from this lot, and at what original cost
    consumed_qty        NUMERIC(15, 3)  NOT NULL,
    cost_rate           NUMERIC(15, 2)  NOT NULL,   -- original purchase rate of the lot
    cost_value          NUMERIC(15, 2)  NOT NULL,   -- consumed_qty * cost_rate

    -- Amendment tracking: which amendment version created this record
    amendment_version   INTEGER DEFAULT 0,          -- 0 = original sale
    is_reversed         BOOLEAN DEFAULT FALSE,      -- set TRUE on cancellation
    reversed_at         TIMESTAMPTZ,
    reversed_by         INTEGER REFERENCES users(id),

    created_at          TIMESTAMPTZ DEFAULT NOW(),
    created_by          INTEGER REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS ix_fifo_invoice    ON fifo_consumption_history(invoice_id);
CREATE INDEX IF NOT EXISTS ix_fifo_item       ON fifo_consumption_history(invoice_item_id);
CREATE INDEX IF NOT EXISTS ix_fifo_stock      ON fifo_consumption_history(stock_item_id);
CREATE INDEX IF NOT EXISTS ix_fifo_purchase   ON fifo_consumption_history(purchase_txn_id);
CREATE INDEX IF NOT EXISTS ix_fifo_tenant     ON fifo_consumption_history(tenant_id);


-- ─────────────────────────────────────────────────────────────────────────
-- 2.  Reversal Entries
--     Tracks the link between original transactions and their reversal
--     counterparts. Both sales invoices and purchase bills are covered.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS reversal_entries (
    id                      SERIAL PRIMARY KEY,
    tenant_id               INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- What type of document was reversed
    document_type           VARCHAR(30) NOT NULL
                            CHECK (document_type IN (
                                'sales_invoice', 'purchase_bill'
                            )),

    -- Original document
    original_invoice_id     INTEGER,                -- FK to invoices.id  (sales)
    original_sup_invoice_id INTEGER,                -- FK to supplier_invoices.id (purchase)

    -- Reversal document (credit note / debit note)
    reversal_invoice_id     INTEGER,                -- FK to invoices.id  (sales reversal)
    reversal_sup_invoice_id INTEGER,                -- FK to supplier_invoices.id (purchase reversal)

    -- Amounts reversed (full invoice amounts)
    subtotal_reversed       NUMERIC(15, 2) NOT NULL DEFAULT 0,
    cgst_reversed           NUMERIC(15, 2) NOT NULL DEFAULT 0,
    sgst_reversed           NUMERIC(15, 2) NOT NULL DEFAULT 0,
    igst_reversed           NUMERIC(15, 2) NOT NULL DEFAULT 0,
    total_reversed          NUMERIC(15, 2) NOT NULL DEFAULT 0,

    -- Audit
    cancelled_by            INTEGER REFERENCES users(id),
    cancelled_at            TIMESTAMPTZ DEFAULT NOW(),
    cancellation_reason     TEXT,
    notes                   TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_reversal_orig_inv  ON reversal_entries(original_invoice_id);
CREATE INDEX IF NOT EXISTS ix_reversal_orig_sup  ON reversal_entries(original_sup_invoice_id);
CREATE INDEX IF NOT EXISTS ix_reversal_tenant    ON reversal_entries(tenant_id);


-- ─────────────────────────────────────────────────────────────────────────
-- 3.  Invoice Versions (Amendment History)
--     Every time a sales invoice is amended, a complete snapshot of the
--     previous state is saved here before the amendment is applied.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS invoice_versions (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    invoice_id          INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,

    version_no          INTEGER NOT NULL DEFAULT 1,       -- starts at 1 for first amendment
    amendment_type      VARCHAR(30) NOT NULL
                        CHECK (amendment_type IN (
                            'financial',      -- qty/rate/items changed
                            'non_financial'   -- address/GSTIN/notes only
                        )),

    -- Snapshot of invoice header at time of amendment
    snapshot_invoice_date   DATE,
    snapshot_customer_name  VARCHAR(200),
    snapshot_customer_pan   VARCHAR(10),
    snapshot_customer_state VARCHAR(50),
    snapshot_customer_gstin VARCHAR(15),
    snapshot_pay_mode       VARCHAR(20),
    snapshot_gst_type       VARCHAR(20),
    snapshot_gst_rate       NUMERIC(5, 2),
    snapshot_subtotal       NUMERIC(15, 2),
    snapshot_cgst           NUMERIC(15, 2),
    snapshot_sgst           NUMERIC(15, 2),
    snapshot_igst           NUMERIC(15, 2),
    snapshot_grand_total    NUMERIC(15, 2),
    snapshot_notes          TEXT,

    -- Snapshot of items (stored as JSONB for full fidelity)
    snapshot_items          JSONB,

    -- The diff applied by this amendment
    adjustment_subtotal     NUMERIC(15, 2) DEFAULT 0,   -- +/- change in subtotal
    adjustment_cgst         NUMERIC(15, 2) DEFAULT 0,
    adjustment_sgst         NUMERIC(15, 2) DEFAULT 0,
    adjustment_igst         NUMERIC(15, 2) DEFAULT 0,
    adjustment_grand_total  NUMERIC(15, 2) DEFAULT 0,

    amendment_reason        TEXT,
    amended_by              INTEGER REFERENCES users(id),
    amended_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_inv_ver_invoice ON invoice_versions(invoice_id);
CREATE INDEX IF NOT EXISTS ix_inv_ver_tenant  ON invoice_versions(tenant_id);

-- Composite uniqueness: each invoice can have at most one row per version_no
CREATE UNIQUE INDEX IF NOT EXISTS uq_inv_ver ON invoice_versions(invoice_id, version_no);


-- ─────────────────────────────────────────────────────────────────────────
-- 4.  Purchase Bill Versions (Amendment History)
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS purchase_versions (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    invoice_id          INTEGER NOT NULL REFERENCES supplier_invoices(id) ON DELETE CASCADE,

    version_no          INTEGER NOT NULL DEFAULT 1,
    amendment_type      VARCHAR(30) NOT NULL
                        CHECK (amendment_type IN ('financial', 'non_financial')),

    snapshot_invoice_no     VARCHAR(30),
    snapshot_invoice_date   DATE,
    snapshot_supplier_name  VARCHAR(200),
    snapshot_gst_type       VARCHAR(20),
    snapshot_gst_rate       NUMERIC(5, 2),
    snapshot_subtotal       NUMERIC(15, 2),
    snapshot_cgst           NUMERIC(15, 2),
    snapshot_sgst           NUMERIC(15, 2),
    snapshot_igst           NUMERIC(15, 2),
    snapshot_grand_total    NUMERIC(15, 2),
    snapshot_notes          TEXT,
    snapshot_items          JSONB,

    adjustment_subtotal     NUMERIC(15, 2) DEFAULT 0,
    adjustment_cgst         NUMERIC(15, 2) DEFAULT 0,
    adjustment_sgst         NUMERIC(15, 2) DEFAULT 0,
    adjustment_igst         NUMERIC(15, 2) DEFAULT 0,
    adjustment_grand_total  NUMERIC(15, 2) DEFAULT 0,

    amendment_reason        TEXT,
    amended_by              INTEGER REFERENCES users(id),
    amended_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_pur_ver_invoice ON purchase_versions(invoice_id);
CREATE INDEX IF NOT EXISTS ix_pur_ver_tenant  ON purchase_versions(tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_pur_ver ON purchase_versions(invoice_id, version_no);


-- ─────────────────────────────────────────────────────────────────────────
-- 5.  Transaction Audit Log
--     Immutable ledger of every financial event. Every entry here is
--     permanent — corrections are new rows, never updates to old ones.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS transaction_audit_log (
    id                  BIGSERIAL PRIMARY KEY,
    tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- What event triggered this log entry
    event_type          VARCHAR(50) NOT NULL
                        CHECK (event_type IN (
                            'invoice_created',
                            'invoice_cancelled',
                            'invoice_amended',
                            'purchase_created',
                            'purchase_cancelled',
                            'purchase_amended',
                            'payment_received',
                            'payment_recorded',
                            'fifo_consumed',
                            'fifo_restored',
                            'fifo_adjusted',
                            'stock_adjusted'
                        )),

    -- Document references
    invoice_id          INTEGER REFERENCES invoices(id),
    sup_invoice_id      INTEGER REFERENCES supplier_invoices(id),
    payment_id          INTEGER REFERENCES payments(id),
    stock_txn_id        INTEGER REFERENCES stock_transactions(id),

    -- Human-readable summary
    description         TEXT NOT NULL,

    -- Financial impact of this event
    debit_amount        NUMERIC(15, 2) DEFAULT 0,
    credit_amount       NUMERIC(15, 2) DEFAULT 0,
    ledger_account      VARCHAR(100),               -- e.g. "Customer Ledger", "Sales", "CGST Payable"

    -- Version tracking
    version_no          INTEGER DEFAULT 0,
    original_txn_id     INTEGER,                    -- for reversal entries
    reversal_ref_id     INTEGER,                    -- link to reversal_entries.id

    -- Who, when
    created_by          INTEGER REFERENCES users(id),
    created_at          TIMESTAMPTZ DEFAULT NOW(),

    -- Additional metadata (JSON)
    metadata            JSONB
);

CREATE INDEX IF NOT EXISTS ix_audit_tenant    ON transaction_audit_log(tenant_id);
CREATE INDEX IF NOT EXISTS ix_audit_invoice   ON transaction_audit_log(invoice_id);
CREATE INDEX IF NOT EXISTS ix_audit_sup_inv   ON transaction_audit_log(sup_invoice_id);
CREATE INDEX IF NOT EXISTS ix_audit_event     ON transaction_audit_log(tenant_id, event_type);
CREATE INDEX IF NOT EXISTS ix_audit_created   ON transaction_audit_log(created_at);


-- ─────────────────────────────────────────────────────────────────────────
-- 6.  Alter existing tables: add ERP tracking columns (safe ALTER)
-- ─────────────────────────────────────────────────────────────────────────

-- invoices: add cancellation audit + version tracking
ALTER TABLE invoices
    ADD COLUMN IF NOT EXISTS version_no              INTEGER  DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cancelled_by            INTEGER  REFERENCES users(id),
    ADD COLUMN IF NOT EXISTS cancelled_at            TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cancellation_reason     TEXT,
    ADD COLUMN IF NOT EXISTS reversal_ref_id         INTEGER  REFERENCES reversal_entries(id),
    ADD COLUMN IF NOT EXISTS original_transaction_id INTEGER  REFERENCES invoices(id);

-- supplier_invoices: same
ALTER TABLE supplier_invoices
    ADD COLUMN IF NOT EXISTS version_no              INTEGER  DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cancelled_by            INTEGER  REFERENCES users(id),
    ADD COLUMN IF NOT EXISTS cancelled_at            TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cancellation_reason     TEXT,
    ADD COLUMN IF NOT EXISTS reversal_ref_id         INTEGER  REFERENCES reversal_entries(id),
    ADD COLUMN IF NOT EXISTS original_transaction_id INTEGER  REFERENCES supplier_invoices(id);

-- stock_transactions: add FIFO lot reference and version tracking
ALTER TABLE stock_transactions
    ADD COLUMN IF NOT EXISTS version_no              INTEGER  DEFAULT 0,
    ADD COLUMN IF NOT EXISTS original_transaction_id INTEGER  REFERENCES stock_transactions(id),
    ADD COLUMN IF NOT EXISTS reversal_ref_id         INTEGER,
    ADD COLUMN IF NOT EXISTS updated_by              INTEGER  REFERENCES users(id),
    ADD COLUMN IF NOT EXISTS updated_at              TIMESTAMPTZ;

-- invoice_items: link to FIFO consumption
ALTER TABLE invoice_items
    ADD COLUMN IF NOT EXISTS version_no              INTEGER  DEFAULT 0,
    ADD COLUMN IF NOT EXISTS updated_by              INTEGER  REFERENCES users(id),
    ADD COLUMN IF NOT EXISTS updated_at              TIMESTAMPTZ;

-- supplier_invoice_items: link to purchase lot
ALTER TABLE supplier_invoice_items
    ADD COLUMN IF NOT EXISTS purchase_txn_id         INTEGER  REFERENCES stock_transactions(id),
    ADD COLUMN IF NOT EXISTS version_no              INTEGER  DEFAULT 0,
    ADD COLUMN IF NOT EXISTS updated_by              INTEGER  REFERENCES users(id),
    ADD COLUMN IF NOT EXISTS updated_at              TIMESTAMPTZ;


-- ─────────────────────────────────────────────────────────────────────────
-- 7.  Back-fill purchase_txn_id on existing supplier_invoice_items
--     (best-effort: matches on stock_item + reason containing invoice_no)
-- ─────────────────────────────────────────────────────────────────────────

UPDATE supplier_invoice_items sii
SET purchase_txn_id = st.id
FROM supplier_invoices si
JOIN stock_items sk ON sk.tenant_id = si.tenant_id
                    AND sk.category = sii.category
JOIN stock_transactions st ON st.stock_item_id = sk.id
                           AND st.txn_type = 'purchase'
                           AND st.reason LIKE '%' || si.invoice_no || '%'
WHERE sii.invoice_id = si.id
  AND sii.purchase_txn_id IS NULL;


-- ─────────────────────────────────────────────────────────────────────────
-- 8.  Backfill fifo_consumption_history for existing sale transactions
--     (approximate — uses sale txn's stored FIFO avg rate)
-- ─────────────────────────────────────────────────────────────────────────

INSERT INTO fifo_consumption_history (
    tenant_id, invoice_item_id, invoice_id, purchase_txn_id,
    stock_item_id, consumed_qty, cost_rate, cost_value,
    amendment_version, is_reversed, created_at, created_by
)
SELECT
    ii.tenant_id,
    ii.id                           AS invoice_item_id,
    ii.invoice_id,
    -- Find oldest purchase lot for this stock item as best estimate
    (SELECT st2.id FROM stock_transactions st2
     WHERE st2.stock_item_id = si.id
       AND st2.txn_type IN ('purchase', 'opening')
       AND st2.tenant_id = ii.tenant_id
     ORDER BY st2.txn_date, st2.id LIMIT 1)  AS purchase_txn_id,
    si.id                           AS stock_item_id,
    ii.qty                          AS consumed_qty,
    COALESCE(st.purchase_rate, 0)   AS cost_rate,
    ii.qty * COALESCE(st.purchase_rate, 0) AS cost_value,
    0, FALSE, ii.created_at, NULL
FROM invoice_items ii
JOIN invoices inv ON inv.id = ii.invoice_id AND inv.status != 'cancelled'
JOIN stock_items si ON si.tenant_id = ii.tenant_id AND si.category = ii.category
LEFT JOIN stock_transactions st ON st.txn_type = 'sale'
    AND st.reason = 'Sale — Invoice ID ' || ii.invoice_id
    AND st.stock_item_id = si.id
WHERE ii.category != 'Polish Charges'
  AND NOT EXISTS (
    SELECT 1 FROM fifo_consumption_history fch WHERE fch.invoice_item_id = ii.id
  )
  AND (SELECT st2.id FROM stock_transactions st2
       WHERE st2.stock_item_id = si.id AND st2.txn_type IN ('purchase','opening')
       AND st2.tenant_id = ii.tenant_id ORDER BY st2.txn_date, st2.id LIMIT 1) IS NOT NULL;


COMMIT;

-- ═══════════════════════════════════════════════════════════════════════════
-- Migration complete. Verify with:
--   SELECT count(*) FROM fifo_consumption_history;
--   SELECT count(*) FROM transaction_audit_log;
--   SELECT column_name FROM information_schema.columns WHERE table_name='invoices' AND column_name='version_no';
-- ═══════════════════════════════════════════════════════════════════════════
