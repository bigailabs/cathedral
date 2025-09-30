-- Add gpu_memory_gb column to gpu_uuid_assignments
-- This stores the memory capacity in GB for each GPU
ALTER TABLE
  gpu_uuid_assignments
ADD
  COLUMN gpu_memory_gb REAL DEFAULT NULL;
