-- ================================================================
-- GoldTrader Pro — Migration: Fix users table SQL ↔ ORM mismatches
-- Run this ONCE against your live PostgreSQL database
-- Safe to re-run (all statements use IF NOT EXISTS / IF EXISTS)
-- ================================================================

-- 1. Add missing columns the ORM model expects but SQL schema lacks
ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_expires_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS company_name     VARCHAR(200);

-- 2. Fix approval_status CHECK constraint
--    Old constraint only allowed: pending, approved, rejected
--    ORM ApprovalStatus enum also has: trial
--    Without this fix every Google signup (status='trial') fails with
--    a CHECK constraint violation → HTTP 500

-- Drop the old constraint by name (PostgreSQL auto-names it)
-- We try both common auto-generated names to be safe
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_approval_status_check;
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_approval_status_check1;

-- Drop by finding the actual constraint name dynamically
DO $$
DECLARE
    con_name TEXT;
BEGIN
    SELECT conname INTO con_name
    FROM   pg_constraint
    WHERE  conrelid = 'users'::regclass
    AND    contype  = 'c'
    AND    pg_get_constraintdef(oid) LIKE '%approval_status%';

    IF con_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE users DROP CONSTRAINT %I', con_name);
        RAISE NOTICE 'Dropped constraint: %', con_name;
    ELSE
        RAISE NOTICE 'No approval_status constraint found (already dropped or never existed)';
    END IF;
END $$;

-- Add updated constraint that includes 'trial'
ALTER TABLE users ADD CONSTRAINT users_approval_status_check
    CHECK (approval_status IN ('pending', 'approved', 'rejected', 'trial'));

-- ================================================================
-- Verify the migration worked
-- ================================================================
DO $$
BEGIN
    -- Check columns exist
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE  table_name = 'users' AND column_name = 'trial_expires_at'
    ) THEN
        RAISE EXCEPTION 'MIGRATION FAILED: trial_expires_at column still missing';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE  table_name = 'users' AND column_name = 'company_name'
    ) THEN
        RAISE EXCEPTION 'MIGRATION FAILED: company_name column still missing';
    END IF;

    RAISE NOTICE '✅ Migration complete — users table now matches ORM model';
END $$;
