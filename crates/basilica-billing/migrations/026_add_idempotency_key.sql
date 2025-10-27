-- Migration 026: Add idempotency protection for event processing
-- This migration adds idempotency_key columns to prevent duplicate event processing

-- Step 1: Add idempotency_key column to usage_events (check exists first)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'billing'
        AND table_name = 'usage_events'
        AND column_name = 'idempotency_key'
    ) THEN
        ALTER TABLE billing.usage_events
        ADD COLUMN idempotency_key VARCHAR(255);
    END IF;
END $$;

-- Step 2: Create unique partial index (allows NULLs, prevents duplicates when set)
CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_events_idempotency_key
ON billing.usage_events(idempotency_key)
WHERE idempotency_key IS NOT NULL;

-- Step 3: Add format validation check constraint (check exists first)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'check_idempotency_key_format'
    ) THEN
        ALTER TABLE billing.usage_events
        ADD CONSTRAINT check_idempotency_key_format
        CHECK (idempotency_key IS NULL OR LENGTH(idempotency_key) > 0);
    END IF;
END $$;

-- Step 4: Add comment for documentation
COMMENT ON COLUMN billing.usage_events.idempotency_key IS
    'Unique key to prevent duplicate processing of same telemetry event. Format: {rental_uuid}:{timestamp}:{sha256_first_8_chars}';

-- Note: No backfill needed - existing events remain NULL and are not affected
-- Only new events will have idempotency keys set
