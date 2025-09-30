-- Add miner_id column to rentals table
-- This tracks which miner is providing the rental
ALTER TABLE
  rentals
ADD
  COLUMN miner_id TEXT NOT NULL DEFAULT '';
