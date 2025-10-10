-- Migration to rename executor_id to node_id in rentals table
-- This migration is idempotent and handles the executor->nodes refactoring

DO $$
BEGIN
    -- Rename executor_id to node_id in rentals table
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'billing'
          AND table_name = 'rentals'
          AND column_name = 'executor_id'
    ) AND NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'billing'
          AND table_name = 'rentals'
          AND column_name = 'node_id'
    ) THEN
        ALTER TABLE billing.rentals
        RENAME COLUMN executor_id TO node_id;
        RAISE NOTICE 'Renamed executor_id to node_id in rentals';
    ELSE
        RAISE NOTICE 'Column rename skipped for rentals: node_id already exists or executor_id not found';
    END IF;
END $$;

-- Drop old index if it exists
DROP INDEX IF EXISTS billing.idx_rentals_executor_id;

-- Create new index if it doesn't exist
CREATE INDEX IF NOT EXISTS idx_rentals_node_id
    ON billing.rentals(node_id);

-- Update comment for rentals.node_id
COMMENT ON COLUMN billing.rentals.node_id IS
    'Node identifier (formerly executor_id) providing the GPU resources - renamed as part of executor->nodes refactoring';
