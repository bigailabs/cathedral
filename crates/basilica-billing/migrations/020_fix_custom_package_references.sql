-- Migration: Fix rentals with package_id='custom'
-- Date: 2025-10-15
-- Description: Updates all rentals with package_id='custom' to use 'h100' as fallback
--              since the 'custom' package has been deprecated and removed.

BEGIN;

-- Log how many rentals will be affected
DO $$
DECLARE
    affected_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO affected_count
    FROM billing.rentals
    WHERE package_id = 'custom';

    RAISE NOTICE 'Found % rentals with package_id=''custom'' that will be updated to ''h100''', affected_count;
END $$;

-- Update all rentals with package_id='custom' to use 'h100'
UPDATE billing.rentals
SET package_id = 'h100',
    updated_at = NOW()
WHERE package_id = 'custom';

-- Log completion
DO $$
DECLARE
    updated_count INTEGER;
BEGIN
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RAISE NOTICE 'Successfully updated % rentals from package_id=''custom'' to ''h100''', updated_count;
END $$;

COMMIT;
