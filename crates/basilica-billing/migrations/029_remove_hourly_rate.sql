-- Remove hourly_rate column from community_rentals
-- The hourly rate is now derived at runtime from: base_price_per_gpu * gpu_count
-- This matches how secure_cloud_rentals already works

ALTER TABLE billing.community_rentals DROP COLUMN hourly_rate;
