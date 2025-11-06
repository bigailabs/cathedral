use basilica_common::types::GpuCategory;

/// Map DataCrunch GPU model to canonical GpuCategory
/// Returns the GPU category based on the model string
pub fn normalize_gpu_type(gpu_model: &str) -> GpuCategory {
    // Use GpuCategory's FromStr implementation which handles parsing
    gpu_model
        .parse()
        .unwrap_or_else(|_| GpuCategory::Other(gpu_model.to_string()))
}

/// Normalize region code
#[allow(dead_code)]
pub fn normalize_region(location_code: &str) -> String {
    // DataCrunch uses codes like "FIN-01", "ICE-01"
    // For now, keep as-is but lowercase
    location_code.to_lowercase()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_normalize_h100() {
        assert_eq!(normalize_gpu_type("NVIDIA H100"), GpuCategory::H100);
        assert_eq!(normalize_gpu_type("NVIDIA H100 NVL"), GpuCategory::H100);
    }

    #[test]
    fn test_normalize_a100() {
        assert_eq!(
            normalize_gpu_type("NVIDIA A100-PCIE-40GB"),
            GpuCategory::A100
        );
        assert_eq!(
            normalize_gpu_type("NVIDIA A100-SXM4-80GB"),
            GpuCategory::A100
        );
    }

    #[test]
    fn test_normalize_b200() {
        assert_eq!(normalize_gpu_type("NVIDIA B200"), GpuCategory::B200);
    }

    #[test]
    fn test_normalize_unknown() {
        match normalize_gpu_type("NVIDIA RTX 3090") {
            GpuCategory::Other(model) => assert!(model.contains("3090")),
            _ => panic!("Expected Other variant"),
        }
    }

    #[test]
    fn test_normalize_region() {
        assert_eq!(normalize_region("FIN-01"), "fin-01");
        assert_eq!(normalize_region("ICE-01"), "ice-01");
    }
}
