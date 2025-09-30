-- Add last_successful_validation column to miner_gpu_profiles
-- This tracks the timestamp of the last successful GPU validation for scoring purposes
ALTER TABLE
  miner_gpu_profiles
ADD
  COLUMN last_successful_validation TEXT;
