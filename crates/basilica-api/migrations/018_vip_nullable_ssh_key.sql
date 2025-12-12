-- Make ssh_key_id nullable for VIP rentals
-- VIP rentals don't use SSH keys from our system (managed externally)

-- Drop the NOT NULL constraint on ssh_key_id in active rentals table
-- The FOREIGN KEY constraint still works with NULL values
ALTER TABLE secure_cloud_rentals ALTER COLUMN ssh_key_id DROP NOT NULL;

-- Drop the NOT NULL constraint on ssh_key_id in terminated rentals table (for archiving VIP rentals)
ALTER TABLE terminated_secure_cloud_rentals ALTER COLUMN ssh_key_id DROP NOT NULL;

COMMENT ON COLUMN secure_cloud_rentals.ssh_key_id IS 'Reference to ssh_keys table. NULL for VIP rentals (SSH access managed externally)';
COMMENT ON COLUMN terminated_secure_cloud_rentals.ssh_key_id IS 'Reference to ssh_keys table. NULL for VIP rentals (SSH access managed externally)';
