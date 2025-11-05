use crate::models::GpuType;

/// Map DataCrunch GPU model to canonical GpuType
pub fn normalize_gpu_type(gpu_model: &str, gpu_memory_gb: u32) -> Option<GpuType> {
    let model_lower = gpu_model.to_lowercase();

    if model_lower.contains("h100") {
        if gpu_memory_gb >= 90 {
            Some(GpuType::H100_94GB)
        } else {
            Some(GpuType::H100_80GB)
        }
    } else if model_lower.contains("a100") {
        if gpu_memory_gb >= 70 {
            Some(GpuType::A100_80GB)
        } else {
            Some(GpuType::A100_40GB)
        }
    } else if model_lower.contains("v100") {
        if gpu_memory_gb >= 30 {
            Some(GpuType::V100_32GB)
        } else {
            Some(GpuType::V100_16GB)
        }
    } else if model_lower.contains("a10") && !model_lower.contains("a100") {
        Some(GpuType::A10_24GB)
    } else if model_lower.contains("a6000") {
        Some(GpuType::A6000_48GB)
    } else if model_lower.contains("l40") {
        Some(GpuType::L40_48GB)
    } else if model_lower.contains("b200") {
        Some(GpuType::B200)
    } else if model_lower.contains("gh200") {
        Some(GpuType::GH200)
    } else {
        None
    }
}

/// Normalize region code
pub fn normalize_region(location_code: &str) -> String {
    // DataCrunch uses codes like "FIN-01", "ICE-01"
    // For now, keep as-is but lowercase
    location_code.to_lowercase()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_normalize_h100_80gb() {
        assert_eq!(
            normalize_gpu_type("NVIDIA H100", 80),
            Some(GpuType::H100_80GB)
        );
    }

    #[test]
    fn test_normalize_h100_94gb() {
        assert_eq!(
            normalize_gpu_type("NVIDIA H100 NVL", 94),
            Some(GpuType::H100_94GB)
        );
    }

    #[test]
    fn test_normalize_a100_40gb() {
        assert_eq!(
            normalize_gpu_type("NVIDIA A100-PCIE-40GB", 40),
            Some(GpuType::A100_40GB)
        );
    }

    #[test]
    fn test_normalize_a100_80gb() {
        assert_eq!(
            normalize_gpu_type("NVIDIA A100-SXM4-80GB", 80),
            Some(GpuType::A100_80GB)
        );
    }

    #[test]
    fn test_normalize_v100() {
        assert_eq!(
            normalize_gpu_type("Tesla V100-SXM2-16GB", 16),
            Some(GpuType::V100_16GB)
        );
        assert_eq!(
            normalize_gpu_type("Tesla V100-SXM2-32GB", 32),
            Some(GpuType::V100_32GB)
        );
    }

    #[test]
    fn test_normalize_unknown() {
        assert_eq!(normalize_gpu_type("NVIDIA RTX 3090", 24), None);
    }

    #[test]
    fn test_normalize_region() {
        assert_eq!(normalize_region("FIN-01"), "fin-01");
        assert_eq!(normalize_region("ICE-01"), "ice-01");
    }
}
