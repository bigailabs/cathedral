-- Fix incorrect unit documentation for total_revenue column
-- The column stores USD values (sum of rental costs in USD), not TAO

COMMENT ON COLUMN billing.miner_revenue_summary.total_revenue IS
    'Sum of total_cost from all rentals in this period (USD)';
