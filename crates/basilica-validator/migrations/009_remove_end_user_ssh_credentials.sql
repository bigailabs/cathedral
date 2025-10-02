-- Remove end_user_ssh_credentials column from rentals table
-- This field is ephemeral and computed on-the-fly, not persisted
ALTER TABLE rentals DROP COLUMN end_user_ssh_credentials;
