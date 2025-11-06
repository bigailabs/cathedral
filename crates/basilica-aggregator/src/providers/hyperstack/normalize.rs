use basilica_common::types::GpuCategory;

/// Parse Hyperstack GPU string to extract model and memory
/// Format examples: "A100-80G-PCIe", "H100", "A100-40G"
/// Returns: (gpu_model, memory_gb_option)
pub fn parse_gpu_string(gpu_str: &str) -> (String, Option<u32>) {
    // Split by '-' to separate components
    let parts: Vec<&str> = gpu_str.split('-').collect();

    if parts.is_empty() {
        return (gpu_str.to_string(), None);
    }

    // First part is always the model (e.g., "A100", "H100")
    let model = parts[0].to_string();

    // Look for memory specification (e.g., "80G", "40G")
    let memory = parts.iter()
        .find_map(|part| {
            if part.ends_with('G') || part.ends_with("GB") {
                part.trim_end_matches("GB")
                    .trim_end_matches('G')
                    .parse::<u32>()
                    .ok()
            } else {
                None
            }
        });

    (model, memory)
}

/// Map Hyperstack GPU model to canonical GpuCategory
/// Hyperstack models: "A100-80G-PCIe", "H100", "B200", etc.
pub fn normalize_gpu_type(gpu_str: &str) -> GpuCategory {
    // Extract just the model part (before first '-')
    let (model, _) = parse_gpu_string(gpu_str);

    // Use GpuCategory's FromStr implementation which handles parsing
    model
        .parse()
        .unwrap_or_else(|_| GpuCategory::Other(gpu_str.to_string()))
}

/// Extract GPU memory in GB from Hyperstack GPU string
/// Returns None if memory info not found in string
pub fn parse_gpu_memory(gpu_str: &str) -> Option<u32> {
    let (_, memory) = parse_gpu_string(gpu_str);
    memory
}

/// Normalize region to "global" (as per DataCrunch pattern)
/// Hyperstack regions would be like "CANADA-1", "US-WEST-2", etc.
/// We simplify to "global" to match DataCrunch behavior
pub fn normalize_region(_region: &str) -> String {
    "global".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_a100_80g() {
        let (model, memory) = parse_gpu_string("A100-80G-PCIe");
        assert_eq!(model, "A100");
        assert_eq!(memory, Some(80));
    }

    #[test]
    fn test_parse_a100_40g() {
        let (model, memory) = parse_gpu_string("A100-40G");
        assert_eq!(model, "A100");
        assert_eq!(memory, Some(40));
    }

    #[test]
    fn test_parse_h100_no_memory() {
        let (model, memory) = parse_gpu_string("H100");
        assert_eq!(model, "H100");
        assert_eq!(memory, None);
    }

    #[test]
    fn test_parse_h100_with_memory() {
        let (model, memory) = parse_gpu_string("H100-80GB");
        assert_eq!(model, "H100");
        assert_eq!(memory, Some(80));
    }

    #[test]
    fn test_normalize_a100() {
        assert_eq!(normalize_gpu_type("A100-80G-PCIe"), GpuCategory::A100);
        assert_eq!(normalize_gpu_type("A100-40G"), GpuCategory::A100);
        assert_eq!(normalize_gpu_type("A100"), GpuCategory::A100);
    }

    #[test]
    fn test_normalize_h100() {
        assert_eq!(normalize_gpu_type("H100"), GpuCategory::H100);
        assert_eq!(normalize_gpu_type("H100-80GB"), GpuCategory::H100);
    }

    #[test]
    fn test_normalize_b200() {
        assert_eq!(normalize_gpu_type("B200"), GpuCategory::B200);
    }

    #[test]
    fn test_normalize_unknown() {
        match normalize_gpu_type("RTX-4090-24G") {
            GpuCategory::Other(model) => assert!(model.contains("RTX")),
            _ => panic!("Expected Other variant"),
        }
    }

    #[test]
    fn test_parse_memory() {
        assert_eq!(parse_gpu_memory("A100-80G-PCIe"), Some(80));
        assert_eq!(parse_gpu_memory("H100-80GB"), Some(80));
        assert_eq!(parse_gpu_memory("H100"), None);
    }

    #[test]
    fn test_normalize_region() {
        assert_eq!(normalize_region("CANADA-1"), "global");
        assert_eq!(normalize_region("US-WEST-2"), "global");
    }
}
