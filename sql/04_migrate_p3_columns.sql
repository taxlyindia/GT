-- ================================================================
-- GoldTrader Pro — P3 Migration: Add missing tenants columns
-- Run this ONCE on your existing PostgreSQL database.
-- Safe to re-run — all statements use IF NOT EXISTS.
-- ================================================================

-- New company profile columns added in P3
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS pan               VARCHAR(10);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS qr_code_url       TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS upi_id            VARCHAR(100);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS bank_name         VARCHAR(100);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS bank_account      VARCHAR(30);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS bank_ifsc         VARCHAR(20);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS bank_branch       VARCHAR(100);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS authorised_person VARCHAR(200);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS terms_conditions  TEXT;

-- Verify: show current tenants columns after migration
SELECT column_name, data_type, character_maximum_length
FROM information_schema.columns
WHERE table_name = 'tenants'
ORDER BY ordinal_position;
