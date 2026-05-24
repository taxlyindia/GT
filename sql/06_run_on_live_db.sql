-- ================================================================
-- GoldTrader Pro — LIVE DB FIX — Run this ONCE on your VPS
-- Safe to re-run — all statements use IF NOT EXISTS / DO $$ checks
-- Database: postgres (as per .env → postgresql+asyncpg://postgres:Taxly@localhost:5432/postgres)
-- Command:  psql -U postgres -d postgres -h localhost -f sql/06_run_on_live_db.sql
-- ================================================================

-- ── Fix 1: Add polish_charges to invoice_items ────────────────
-- This is the PRIMARY cause of the "Server error" on invoice creation.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name  = 'invoice_items'
          AND column_name = 'polish_charges'
    ) THEN
        ALTER TABLE invoice_items
            ADD COLUMN polish_charges NUMERIC(15, 2) NOT NULL DEFAULT 0;
        RAISE NOTICE '✅ Added polish_charges to invoice_items.';
    ELSE
        RAISE NOTICE 'ℹ️  polish_charges already exists on invoice_items — skipping.';
    END IF;
END $$;

-- ── Fix 2: P3 tenant columns (company profile fields) ─────────
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS pan               VARCHAR(10);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS qr_code_url       TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS upi_id            VARCHAR(100);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS bank_name         VARCHAR(100);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS bank_account      VARCHAR(30);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS bank_ifsc         VARCHAR(20);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS bank_branch       VARCHAR(100);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS authorised_person VARCHAR(200);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS terms_conditions  TEXT;

-- ── Verify: confirm all expected columns now exist ────────────
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'invoice_items'
ORDER BY ordinal_position;
