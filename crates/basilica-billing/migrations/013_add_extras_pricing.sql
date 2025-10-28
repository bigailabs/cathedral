-- Migration 013: Add extras pricing and included allowances to billing_packages
ALTER TABLE
  billing.billing_packages
ADD
  COLUMN IF NOT EXISTS storage_rate_per_gb_hour DECIMAL(20, 6) NOT NULL DEFAULT 0;

ALTER TABLE
  billing.billing_packages
ADD
  COLUMN IF NOT EXISTS network_rate_per_gb DECIMAL(20, 6) NOT NULL DEFAULT 0;

ALTER TABLE
  billing.billing_packages
ADD
  COLUMN IF NOT EXISTS disk_io_rate_per_gb DECIMAL(20, 6) NOT NULL DEFAULT 0;

ALTER TABLE
  billing.billing_packages
ADD
  COLUMN IF NOT EXISTS cpu_rate_per_core_hour DECIMAL(20, 6) NOT NULL DEFAULT 0;

ALTER TABLE
  billing.billing_packages
ADD
  COLUMN IF NOT EXISTS memory_rate_per_gb_hour DECIMAL(20, 6) NOT NULL DEFAULT 0;

ALTER TABLE
  billing.billing_packages
ADD
  COLUMN IF NOT EXISTS included_storage_gb_hours DECIMAL(20, 6) NOT NULL DEFAULT 0;

ALTER TABLE
  billing.billing_packages
ADD
  COLUMN IF NOT EXISTS included_network_gb DECIMAL(20, 6) NOT NULL DEFAULT 0;

ALTER TABLE
  billing.billing_packages
ADD
  COLUMN IF NOT EXISTS included_disk_io_gb DECIMAL(20, 6) NOT NULL DEFAULT 0;

ALTER TABLE
  billing.billing_packages
ADD
  COLUMN IF NOT EXISTS included_cpu_core_hours DECIMAL(20, 6) NOT NULL DEFAULT 0;

ALTER TABLE
  billing.billing_packages
ADD
  COLUMN IF NOT EXISTS included_memory_gb_hours DECIMAL(20, 6) NOT NULL DEFAULT 0;

COMMENT ON COLUMN billing.billing_packages.storage_rate_per_gb_hour IS 'Cost per GB-hour of storage beyond included amount';

COMMENT ON COLUMN billing.billing_packages.network_rate_per_gb IS 'Cost per GB of network transfer beyond included amount';

COMMENT ON COLUMN billing.billing_packages.disk_io_rate_per_gb IS 'Cost per GB of disk I/O beyond included amount';

COMMENT ON COLUMN billing.billing_packages.cpu_rate_per_core_hour IS 'Cost per CPU core-hour beyond included amount';

COMMENT ON COLUMN billing.billing_packages.memory_rate_per_gb_hour IS 'Cost per GB-hour of memory beyond included amount';