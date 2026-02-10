-- Drop the redundant last_seen column from miners table.
-- updated_at is always set at the same time and serves the same purpose.
ALTER TABLE miners DROP COLUMN last_seen;
