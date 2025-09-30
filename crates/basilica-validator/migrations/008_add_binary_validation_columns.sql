-- Add binary validation columns to verification_logs
-- These track the binary validation timestamp and score
ALTER TABLE
  verification_logs
ADD
  COLUMN last_binary_validation TEXT;

ALTER TABLE
  verification_logs
ADD
  COLUMN last_binary_validation_score REAL;
