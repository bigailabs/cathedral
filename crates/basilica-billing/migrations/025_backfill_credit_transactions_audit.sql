-- Migration 025: Backfill credit_transactions with historical rental charges
-- This migration creates audit trail records for all historical rental charges
-- that were previously only recorded in billing_events but not in credit_transactions

BEGIN;

-- ===========================================================================
-- 1. Backfill rental charges from billing_events (telemetry_processed events)
-- ===========================================================================

-- Insert credit_transactions for all historical rental charges
-- Uses billing_events.telemetry_processed to reconstruct the audit trail
INSERT INTO billing.credit_transactions (
    id,
    user_id,
    transaction_type,
    amount,
    balance_before,
    balance_after,
    reference_id,
    reference_type,
    description,
    metadata,
    created_at
)
SELECT
    gen_random_uuid() AS id,
    u.user_id,
    'debit' AS transaction_type,
    (be.event_data->>'incremental_cost')::decimal AS amount,
    0 AS balance_before,  -- Historical balance unknown
    0 AS balance_after,   -- Historical balance unknown
    be.entity_id AS reference_id,
    'rental' AS reference_type,
    'Historical rental charge (backfilled from migration 025)' AS description,
    jsonb_build_object(
        'backfilled', true,
        'original_event_id', be.event_id,
        'telemetry_timestamp', be.event_data->>'timestamp',
        'gpu_count', (be.event_data->'usage_metrics'->>'gpu_count')::int,
        'gpu_hours', COALESCE(be.event_data->'usage_metrics'->>'gpu_hours', '0'),
        'total_cost', COALESCE(be.event_data->>'total_cost', '0')
    ) AS metadata,
    be.created_at
FROM billing.billing_events be
JOIN billing.rentals r ON r.rental_id = be.entity_id::uuid
JOIN billing.users u ON r.user_id = u.external_id
WHERE be.event_type = 'telemetry_processed'
    AND be.entity_type = 'rental'
    AND be.event_data->>'incremental_cost' IS NOT NULL
    AND be.event_data->>'credits_deducted' IS NOT NULL
    AND (be.event_data->>'credits_deducted')::boolean = true
    AND be.entity_id IS NOT NULL
    AND be.event_data->'usage_metrics'->>'gpu_count' IS NOT NULL
    AND be.event_data->>'timestamp' IS NOT NULL
    -- Only backfill if not already exists (idempotent)
    AND NOT EXISTS (
        SELECT 1 FROM billing.credit_transactions ct
        WHERE ct.reference_id = be.entity_id
            AND ct.reference_type = 'rental'
            AND ct.created_at = be.created_at
    )
ORDER BY be.created_at ASC;

-- ===========================================================================
-- 2. Create summary comment on credit_transactions table
-- ===========================================================================

COMMENT ON TABLE billing.credit_transactions IS
    'Financial audit trail for all credit transactions. Records added before migration 025 were backfilled from billing_events with balance_before/balance_after set to 0 (unknown). Records after migration 025 have accurate balance snapshots.';

COMMENT ON COLUMN billing.credit_transactions.balance_before IS
    'Balance before transaction. Note: Historical records backfilled in migration 025 have this set to 0 (unknown).';

COMMENT ON COLUMN billing.credit_transactions.balance_after IS
    'Balance after transaction. Note: Historical records backfilled in migration 025 have this set to 0 (unknown).';

COMMENT ON COLUMN billing.credit_transactions.metadata IS
    'Transaction metadata. Historical backfilled records contain: {backfilled: true, original_event_id, telemetry_timestamp, gpu_count, gpu_hours, total_cost}.';

-- ===========================================================================
-- 3. Create verification view for auditing
-- ===========================================================================

CREATE OR REPLACE VIEW billing.credit_transactions_audit_summary AS
SELECT
    reference_type,
    COUNT(*) AS transaction_count,
    COUNT(CASE WHEN (metadata->>'backfilled')::boolean = true THEN 1 END) AS backfilled_count,
    COUNT(CASE WHEN (metadata->>'backfilled')::boolean IS NULL OR (metadata->>'backfilled')::boolean = false THEN 1 END) AS live_count,
    SUM(amount) AS total_amount,
    MIN(created_at) AS first_transaction,
    MAX(created_at) AS last_transaction
FROM billing.credit_transactions
GROUP BY reference_type
ORDER BY transaction_count DESC;

COMMENT ON VIEW billing.credit_transactions_audit_summary IS
    'Summary view showing transaction counts by type, distinguishing backfilled vs live records';

COMMIT;
