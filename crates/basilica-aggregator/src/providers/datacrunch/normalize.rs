use basilica_common::types::GpuCategory;

/// Map DataCrunch GPU model to canonical GpuCategory
/// Returns the GPU category based on the model string
pub fn normalize_gpu_type(gpu_model: &str) -> GpuCategory {
    // Use GpuCategory's FromStr implementation which handles parsing
    gpu_model
        .parse()
        .unwrap_or_else(|_| GpuCategory::Other(gpu_model.to_string()))
}

/// Parse interconnect type from GPU description
/// Format: "8x H100 SXM5 80GB" -> Some("SXM5")
/// Returns None if interconnect type not found in description
pub fn parse_interconnect(description: &str) -> Option<String> {
    // Known interconnect types
    let interconnect_types = ["SXM4", "SXM5", "SXM6", "PCIe", "PCIE"];

    // Split by whitespace and look for interconnect type
    for part in description.split_whitespace() {
        let part_upper = part.to_uppercase();
        for interconnect in &interconnect_types {
            if part_upper == *interconnect {
                // Normalize PCIe variations to "PCIe"
                return Some(if part_upper == "PCIE" {
                    "PCIe".to_string()
                } else {
                    part_upper
                });
            }
        }
    }

    None
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
    fn test_parse_interconnect_sxm4() {
        assert_eq!(
            parse_interconnect("1x A100 SXM4 80GB"),
            Some("SXM4".to_string())
        );
        assert_eq!(
            parse_interconnect("8x A100 SXM4 40GB"),
            Some("SXM4".to_string())
        );
    }

    #[test]
    fn test_parse_interconnect_sxm5() {
        assert_eq!(
            parse_interconnect("1x H100 SXM5 80GB"),
            Some("SXM5".to_string())
        );
        assert_eq!(
            parse_interconnect("8x H100 SXM5 80GB"),
            Some("SXM5".to_string())
        );
    }

    #[test]
    fn test_parse_interconnect_sxm6() {
        assert_eq!(
            parse_interconnect("1x B200 SXM6 180GB"),
            Some("SXM6".to_string())
        );
        assert_eq!(
            parse_interconnect("8x B200 SXM6 180GB"),
            Some("SXM6".to_string())
        );
    }

    #[test]
    fn test_parse_interconnect_pcie() {
        assert_eq!(
            parse_interconnect("1x A100 PCIe 80GB"),
            Some("PCIe".to_string())
        );
        assert_eq!(
            parse_interconnect("1x A100 PCIE 80GB"),
            Some("PCIe".to_string())
        );
    }

    #[test]
    fn test_parse_interconnect_none() {
        assert_eq!(parse_interconnect("1x RTX 4090 24GB"), None);
        assert_eq!(parse_interconnect("NVIDIA H100"), None);
    }
}
