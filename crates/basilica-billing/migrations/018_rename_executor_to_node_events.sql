-- Migration to rename executor_id to node_id in usage_events and telemetry_buffer tables
-- This migration is idempotent and handles the executor->nodes refactoring

DO $$
BEGIN
    -- Rename executor_id to node_id in usage_events table
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'billing'
          AND table_name = 'usage_events'
          AND column_name = 'executor_id'
    ) AND NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'billing'
          AND table_name = 'usage_events'
          AND column_name = 'node_id'
    ) THEN
        ALTER TABLE billing.usage_events
        RENAME COLUMN executor_id TO node_id;
        RAISE NOTICE 'Renamed executor_id to node_id in usage_events';
    ELSE
        RAISE NOTICE 'Column rename skipped for usage_events: node_id already exists or executor_id not found';
    END IF;

    -- Rename executor_id to node_id in telemetry_buffer table
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'billing'
          AND table_name = 'telemetry_buffer'
          AND column_name = 'executor_id'
    ) AND NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'billing'
          AND table_name = 'telemetry_buffer'
          AND column_name = 'node_id'
    ) THEN
        ALTER TABLE billing.telemetry_buffer
        RENAME COLUMN executor_id TO node_id;
        RAISE NOTICE 'Renamed executor_id to node_id in telemetry_buffer';
    ELSE
        RAISE NOTICE 'Column rename skipped for telemetry_buffer: node_id already exists or executor_id not found';
    END IF;
END $$;

-- Drop old indexes if they exist
DROP INDEX IF EXISTS billing.idx_usage_events_executor_id;

-- Create new indexes if they don't exist
CREATE INDEX IF NOT EXISTS idx_usage_events_node_id
    ON billing.usage_events(node_id);

-- Update comment for usage_events.node_id
COMMENT ON COLUMN billing.usage_events.node_id IS
    'Node identifier (formerly executor_id) - renamed as part of executor->nodes refactoring. REQUIRED: Node running the workload - must never be empty';

-- Update comment for telemetry_buffer.node_id
COMMENT ON COLUMN billing.telemetry_buffer.node_id IS
    'Node identifier (formerly executor_id) - renamed as part of executor->nodes refactoring';
