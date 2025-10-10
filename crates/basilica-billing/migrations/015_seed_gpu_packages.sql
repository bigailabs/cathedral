-- Migration 015: Seed billing packages for validator-supported GPU models

INSERT INTO billing.billing_packages
  (package_id, name, description, hourly_rate, gpu_model,
   billing_period, priority, active, metadata,
   base_rate_per_hour, cpu_rate_per_hour, disk_iops_rate,
   storage_rate_per_gb_hour, network_rate_per_gb, disk_io_rate_per_gb,
   cpu_rate_per_core_hour, memory_rate_per_gb_hour,
   included_storage_gb_hours, included_network_gb, included_disk_io_gb,
   included_cpu_core_hours, included_memory_gb_hours,
   updated_at)
VALUES
  ('a100', 'A100 GPU Package', 'NVIDIA A100 80GB GPU compute instances',
   2.50, 'A100', 'Hourly', 100, true,
   '{"gpu_vram_gb": 80, "generation": "Ampere", "tensor_cores": "3rd Gen"}'::jsonb,
   2.50, 0.05, 0.0001,
   0.10, 0.05, 0.02, 0.05, 0.01,
   100, 50, 100, 10, 50,
   NOW()),

  ('h100', 'H100 GPU Package', 'NVIDIA H100 80GB GPU compute instances',
   3.50, 'H100', 'Hourly', 110, true,
   '{"gpu_vram_gb": 80, "generation": "Hopper", "tensor_cores": "4th Gen"}'::jsonb,
   3.50, 0.05, 0.0001,
   0.10, 0.05, 0.02, 0.05, 0.01,
   100, 50, 100, 10, 50,
   NOW()),

  ('h200', 'H200 GPU Package', 'NVIDIA H200 141GB GPU compute instances',
   5.00, 'H200', 'Hourly', 115, true,
   '{"gpu_vram_gb": 141, "generation": "Hopper", "tensor_cores": "4th Gen"}'::jsonb,
   5.00, 0.04, 0.00008,
   0.08, 0.04, 0.015, 0.04, 0.008,
   200, 100, 200, 20, 100,
   NOW()),

  ('b200', 'B200 GPU Package', 'NVIDIA B200 GPU compute instances',
   15.00, 'B200', 'Hourly', 120, true,
   '{"gpu_vram_gb": 192, "generation": "Blackwell", "tensor_cores": "5th Gen"}'::jsonb,
   15.00, 0.03, 0.00005,
   0.05, 0.03, 0.01, 0.03, 0.005,
   500, 200, 500, 50, 200,
   NOW()),

  ('custom', 'Custom Package', 'Custom GPU configurations and other models',
   1.00, 'custom', 'Hourly', 50, true,
   '{}'::jsonb,
   1.00, 0.05, 0.0001,
   0.10, 0.05, 0.02, 0.05, 0.01,
   50, 25, 50, 5, 25,
   NOW())

ON CONFLICT (package_id) DO UPDATE SET
  name = EXCLUDED.name,
  description = EXCLUDED.description,
  hourly_rate = EXCLUDED.hourly_rate,
  gpu_model = EXCLUDED.gpu_model,
  priority = EXCLUDED.priority,
  active = EXCLUDED.active,
  metadata = EXCLUDED.metadata,
  base_rate_per_hour = EXCLUDED.base_rate_per_hour,
  cpu_rate_per_hour = EXCLUDED.cpu_rate_per_hour,
  disk_iops_rate = EXCLUDED.disk_iops_rate,
  storage_rate_per_gb_hour = EXCLUDED.storage_rate_per_gb_hour,
  network_rate_per_gb = EXCLUDED.network_rate_per_gb,
  disk_io_rate_per_gb = EXCLUDED.disk_io_rate_per_gb,
  cpu_rate_per_core_hour = EXCLUDED.cpu_rate_per_core_hour,
  memory_rate_per_gb_hour = EXCLUDED.memory_rate_per_gb_hour,
  included_storage_gb_hours = EXCLUDED.included_storage_gb_hours,
  included_network_gb = EXCLUDED.included_network_gb,
  included_disk_io_gb = EXCLUDED.included_disk_io_gb,
  included_cpu_core_hours = EXCLUDED.included_cpu_core_hours,
  included_memory_gb_hours = EXCLUDED.included_memory_gb_hours,
  updated_at = NOW();

COMMENT ON TABLE billing.billing_packages IS 'GPU billing packages - add new rows to support additional GPU models';
