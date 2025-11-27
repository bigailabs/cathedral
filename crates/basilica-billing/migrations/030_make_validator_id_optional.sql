-- Make validator_id nullable for secure cloud rentals (no validator)
-- Community cloud rentals still provide validator_id, but secure cloud rentals don't have one
ALTER TABLE billing.usage_events ALTER COLUMN validator_id DROP NOT NULL;
