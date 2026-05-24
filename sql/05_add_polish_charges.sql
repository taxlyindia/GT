-- Migration 05: Add polish_charges column to invoice_items
-- Run this ONCE against your live PostgreSQL database on Hostinger VPS.
-- Safe to run multiple times (uses IF NOT EXISTS check).

DO $$
BEGIN
    -- Add polish_charges to invoice_items if it doesn't exist
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'invoice_items'
          AND column_name = 'polish_charges'
    ) THEN
        ALTER TABLE invoice_items
            ADD COLUMN polish_charges NUMERIC(15, 2) NOT NULL DEFAULT 0;
        RAISE NOTICE 'Added polish_charges column to invoice_items.';
    ELSE
        RAISE NOTICE 'polish_charges already exists on invoice_items — skipping.';
    END IF;
END $$;
