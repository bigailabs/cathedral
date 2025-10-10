-- Migration to rename executor_id to node_id in active_rentals_facts table
-- This migration is idempotent and handles the executor->nodes refactoring

-- Check if the old column exists and rename it
DO $$
BEGIN
    -- Only rename if executor_id exists and node_id doesn't
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'billing'
          AND table_name = 'active_rentals_facts'
          AND column_name = 'executor_id'
    ) AND NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'billing'
          AND table_name = 'active_rentals_facts'
          AND column_name = 'node_id'
    ) THEN
        -- Rename the column
        ALTER TABLE billing.active_rentals_facts
        RENAME COLUMN executor_id TO node_id;

        RAISE NOTICE 'Renamed executor_id to node_id in active_rentals_facts';
    ELSE
        RAISE NOTICE 'Column rename skipped: node_id already exists or executor_id not found';
    END IF;
END $$;

-- Drop the old index if it exists
DROP INDEX IF EXISTS billing.idx_active_rentals_facts_executor_id;

-- Create the new index if it doesn't exist
CREATE INDEX IF NOT EXISTS idx_active_rentals_facts_node_id
    ON billing.active_rentals_facts(node_id);

-- Add comment for documentation
COMMENT ON COLUMN billing.active_rentals_facts.node_id IS
    'Node identifier (formerly executor_id) - renamed as part of executor->nodes refactoring';
