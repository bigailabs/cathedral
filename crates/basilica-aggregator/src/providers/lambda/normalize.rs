use basilica_common::types::GpuCategory;

/// Parsed GPU information from Lambda description
#[derive(Debug, Clone)]
pub struct GpuInfo {
    pub count: u32,
    pub model: String,
    pub memory_gb: u32,
}

/// Parse Lambda GPU description string
/// Format: "1x A10 (24 GB PCIe)" or "8x H100 (80 GB SXM5)"
/// Returns: GpuInfo with count, model, and memory
pub fn parse_gpu_description(description: &str) -> Option<GpuInfo> {
    // Pattern: "{count}x {model} ({memory} GB ...)"
    // Example: "1x A10 (24 GB PCIe)"

    let parts: Vec<&str> = description.split('(').collect();
    if parts.len() < 2 {
        return None;
    }

    // Parse first part: "1x A10 "
    let gpu_part = parts[0].trim();
    let gpu_tokens: Vec<&str> = gpu_part.split_whitespace().collect();
    if gpu_tokens.len() < 2 {
        return None;
    }

    // Extract count from "1x"
    let count = gpu_tokens[0].trim_end_matches('x').parse::<u32>().ok()?;

    // Extract model (everything after count until parenthesis)
    let model = gpu_tokens[1..].join(" ");

    // Parse second part: "24 GB PCIe)"
    let memory_part = parts[1].trim();
    let memory_tokens: Vec<&str> = memory_part.split_whitespace().collect();
    if memory_tokens.is_empty() {
        return None;
    }

    // Extract memory value
    let memory_gb = memory_tokens[0].parse::<u32>().ok()?;

    Some(GpuInfo {
        count,
        model,
        memory_gb,
    })
}

/// Map Lambda GPU model to canonical GpuCategory
pub fn normalize_gpu_type(gpu_model: &str) -> GpuCategory {
    // Use GpuCategory's FromStr implementation which handles parsing
    gpu_model
        .parse()
        .unwrap_or_else(|_| GpuCategory::Other(gpu_model.to_string()))
}

/// Pass through Lambda region name as-is
/// Lambda regions are like "us-west-1", "us-east-1", etc.
/// We store exactly what the API provides without transformation
pub fn normalize_region(region_name: &str) -> String {
    region_name.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_single_gpu() {
        let info = parse_gpu_description("1x A10 (24 GB PCIe)").unwrap();
        assert_eq!(info.count, 1);
        assert_eq!(info.model, "A10");
        assert_eq!(info.memory_gb, 24);
    }

    #[test]
    fn test_parse_multiple_gpus() {
        let info = parse_gpu_description("8x H100 (80 GB SXM5)").unwrap();
        assert_eq!(info.count, 8);
        assert_eq!(info.model, "H100");
        assert_eq!(info.memory_gb, 80);
    }

    #[test]
    fn test_parse_multi_word_model() {
        let info = parse_gpu_description("2x RTX A6000 (48 GB PCIe)").unwrap();
        assert_eq!(info.count, 2);
        assert_eq!(info.model, "RTX A6000");
        assert_eq!(info.memory_gb, 48);
    }

    #[test]
    fn test_normalize_h100() {
        assert_eq!(normalize_gpu_type("H100"), GpuCategory::H100);
        assert_eq!(normalize_gpu_type("NVIDIA H100"), GpuCategory::H100);
    }

    #[test]
    fn test_normalize_a100() {
        assert_eq!(normalize_gpu_type("A100"), GpuCategory::A100);
        assert_eq!(normalize_gpu_type("NVIDIA A100"), GpuCategory::A100);
    }

    #[test]
    fn test_normalize_unknown() {
        match normalize_gpu_type("RTX 3090") {
            GpuCategory::Other(model) => assert!(model.contains("3090")),
            _ => panic!("Expected Other variant"),
        }
    }

    #[test]
    fn test_normalize_region() {
        // Store exactly what Lambda API provides
        assert_eq!(normalize_region("US-WEST-1"), "US-WEST-1");
        assert_eq!(normalize_region("us-east-1"), "us-east-1");
    }
}
