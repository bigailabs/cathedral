-- Migration 022: Remove reservation system, keep only incremental telemetry charging
-- This migration safely transitions from dual-path to single-path billing

BEGIN;

-- Step 1: Mark all active reservations as released
UPDATE billing.credit_reservations
SET status = 'released',
    released_at = NOW()
WHERE status = 'active';

-- Step 2: Return all reserved credits to available balance
-- This makes any locked credits immediately available
UPDATE billing.credits
SET reserved_balance = 0
WHERE reserved_balance > 0;

-- Step 3: Archive reservation data for audit trail
-- Preserve historical data before deletion
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
    gen_random_uuid(),
    cr.user_id,
    'reservation_archived',
    cr.amount,
    0,
    0,
    cr.rental_id::text,
    'reservation_migration_022',
    'Archived reservation during migration to pay-as-you-go billing',
    jsonb_build_object(
        'reservation_id', cr.id,
        'status', cr.status,
        'reserved_at', cr.reserved_at,
        'expires_at', cr.expires_at,
        'released_at', cr.released_at,
        'final_amount', cr.final_amount
    ),
    NOW()
FROM billing.credit_reservations cr
WHERE NOT EXISTS (
    SELECT 1 FROM billing.credit_transactions ct
    WHERE ct.reference_id = cr.id::text
    AND ct.transaction_type = 'reservation_archived'
);

-- Step 4: Drop reservation table
DROP TABLE IF EXISTS billing.credit_reservations CASCADE;

-- Step 5: Drop reserved_balance column from credits
ALTER TABLE billing.credits
DROP COLUMN IF EXISTS reserved_balance CASCADE;

-- Step 6: Drop max_duration_hours from rentals
ALTER TABLE billing.rentals
DROP COLUMN IF EXISTS max_duration_hours CASCADE;

-- Step 7: Update table comments
COMMENT ON TABLE billing.credits IS
    'User credit balances for pay-as-you-go billing. Reserved credits system removed in migration 022.';

COMMENT ON TABLE billing.rentals IS
    'GPU rental records. Charged incrementally via telemetry events. Reservation system removed in migration 022.';

-- Step 8: Create index for incremental charging queries
CREATE INDEX IF NOT EXISTS idx_rentals_incremental_charging
ON billing.rentals(user_id, status, updated_at)
WHERE status IN ('pending', 'active');

COMMIT;
