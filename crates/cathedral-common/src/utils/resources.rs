//! Kubernetes resource quantity parsing utilities
//!
//! This module provides functions for parsing and formatting Kubernetes resource
//! quantities such as CPU (millicores) and memory (bytes).

use tracing::warn;

/// Parse a Kubernetes CPU resource string to millicores.
///
/// Accepts formats:
/// - Millicores: "500m" -> 500
/// - Cores (integer): "2" -> 2000
/// - Cores (decimal): "0.5" -> 500
///
/// Returns 0 on parse failure with warning log.
pub fn parse_cpu_to_milli(cpu: &str) -> i64 {
    let cpu = cpu.trim();
    if cpu.is_empty() {
        return 0;
    }
    if cpu.ends_with('m') {
        let value = cpu.trim_end_matches('m');
        match value.parse::<i64>() {
            Ok(v) => v,
            Err(_) => {
                warn!("Failed to parse CPU millicores: {}", cpu);
                0
            }
        }
    } else if cpu.contains('.') {
        match cpu.parse::<f64>() {
            Ok(v) => (v * 1000.0).round() as i64,
            Err(_) => {
                warn!("Failed to parse CPU cores (float): {}", cpu);
                0
            }
        }
    } else {
        match cpu.parse::<i64>() {
            Ok(v) => v.saturating_mul(1000),
            Err(_) => {
                warn!("Failed to parse CPU cores: {}", cpu);
                0
            }
        }
    }
}

/// Parse a GPU count string to integer, supporting fractional values.
///
/// Accepts formats:
/// - Integer: "2" -> 2
/// - Fractional: "0.5" -> 1 (rounds up)
///
/// Returns 0 on parse failure with warning log.
pub fn parse_gpu_count(gpu: &str) -> u32 {
    let gpu = gpu.trim();
    if gpu.is_empty() {
        return 0;
    }
    if gpu.contains('.') {
        match gpu.parse::<f64>() {
            Ok(v) => v.ceil() as u32,
            Err(_) => {
                warn!("Failed to parse GPU count (float): {}", gpu);
                0
            }
        }
    } else {
        match gpu.parse::<u32>() {
            Ok(v) => v,
            Err(_) => {
                warn!("Failed to parse GPU count: {}", gpu);
                0
            }
        }
    }
}

/// Parse a Kubernetes memory resource string to bytes.
///
/// Accepts formats:
/// - Binary units: "Ki", "Mi", "Gi", "Ti" (1024-based)
/// - Decimal units: "K"/"k", "M", "G", "T" (1000-based)
/// - Raw bytes: "1073741824"
///
/// Returns 0 on parse failure with warning log.
pub fn parse_memory_to_bytes(memory: &str) -> i64 {
    let memory = memory.trim();
    if memory.is_empty() {
        return 0;
    }

    let parse_with_warn = |value: &str, unit: &str| -> i64 {
        match value.parse::<i64>() {
            Ok(v) => v,
            Err(_) => {
                warn!(
                    "Failed to parse memory value '{}' with unit '{}'",
                    value, unit
                );
                0
            }
        }
    };

    if memory.ends_with("Ki") {
        parse_with_warn(memory.trim_end_matches("Ki"), "Ki").saturating_mul(1024)
    } else if memory.ends_with("Mi") {
        parse_with_warn(memory.trim_end_matches("Mi"), "Mi").saturating_mul(1024 * 1024)
    } else if memory.ends_with("Gi") {
        parse_with_warn(memory.trim_end_matches("Gi"), "Gi").saturating_mul(1024 * 1024 * 1024)
    } else if memory.ends_with("Ti") {
        parse_with_warn(memory.trim_end_matches("Ti"), "Ti")
            .saturating_mul(1024_i64 * 1024 * 1024 * 1024)
    } else if memory.ends_with('T') {
        parse_with_warn(memory.trim_end_matches('T'), "T")
            .saturating_mul(1000_i64 * 1000 * 1000 * 1000)
    } else if memory.ends_with('K') || memory.ends_with('k') {
        parse_with_warn(memory.trim_end_matches(['K', 'k']), "K").saturating_mul(1000)
    } else if memory.ends_with('M') {
        parse_with_warn(memory.trim_end_matches('M'), "M").saturating_mul(1000 * 1000)
    } else if memory.ends_with('G') {
        parse_with_warn(memory.trim_end_matches('G'), "G").saturating_mul(1000 * 1000 * 1000)
    } else {
        match memory.parse() {
            Ok(v) => v,
            Err(_) => {
                warn!("Failed to parse memory value: {}", memory);
                0
            }
        }
    }
}

/// Format bytes as human-readable Kubernetes memory quantity.
///
/// Returns the largest appropriate binary unit (Gi, Mi, Ki, or raw bytes).
pub fn format_bytes(bytes: i64) -> String {
    const GI: i64 = 1024 * 1024 * 1024;
    const MI: i64 = 1024 * 1024;
    const KI: i64 = 1024;

    if bytes >= GI {
        format!("{}Gi", bytes / GI)
    } else if bytes >= MI {
        format!("{}Mi", bytes / MI)
    } else if bytes >= KI {
        format!("{}Ki", bytes / KI)
    } else {
        format!("{}", bytes)
    }
}

/// Format millicores as human-readable Kubernetes CPU quantity.
///
/// Returns cores format for whole numbers (>=1000m), otherwise millicores.
/// Examples: 2000 -> "2", 500 -> "500m", 1500 -> "1500m"
pub fn format_milli_cpu(milli: i64) -> String {
    if milli >= 1000 && milli % 1000 == 0 {
        format!("{}", milli / 1000)
    } else {
        format!("{}m", milli)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_cpu_millicores() {
        assert_eq!(parse_cpu_to_milli("500m"), 500);
        assert_eq!(parse_cpu_to_milli("1000m"), 1000);
        assert_eq!(parse_cpu_to_milli("100m"), 100);
        assert_eq!(parse_cpu_to_milli("0m"), 0);
    }

    #[test]
    fn test_parse_cpu_cores() {
        assert_eq!(parse_cpu_to_milli("1"), 1000);
        assert_eq!(parse_cpu_to_milli("2"), 2000);
        assert_eq!(parse_cpu_to_milli("4"), 4000);
        assert_eq!(parse_cpu_to_milli("0"), 0);
    }

    #[test]
    fn test_parse_cpu_with_whitespace() {
        assert_eq!(parse_cpu_to_milli("  500m  "), 500);
        assert_eq!(parse_cpu_to_milli("  2  "), 2000);
    }

    #[test]
    fn test_parse_cpu_invalid() {
        assert_eq!(parse_cpu_to_milli("invalid"), 0);
        assert_eq!(parse_cpu_to_milli(""), 0);
        assert_eq!(parse_cpu_to_milli("abc"), 0);
    }

    #[test]
    fn test_parse_memory_binary_units() {
        assert_eq!(parse_memory_to_bytes("1Ki"), 1024);
        assert_eq!(parse_memory_to_bytes("1Mi"), 1024 * 1024);
        assert_eq!(parse_memory_to_bytes("1Gi"), 1024 * 1024 * 1024);
        assert_eq!(parse_memory_to_bytes("512Mi"), 512 * 1024 * 1024);
        assert_eq!(parse_memory_to_bytes("2Gi"), 2 * 1024 * 1024 * 1024);
    }

    #[test]
    fn test_parse_memory_decimal_units() {
        assert_eq!(parse_memory_to_bytes("1K"), 1000);
        assert_eq!(parse_memory_to_bytes("1k"), 1000);
        assert_eq!(parse_memory_to_bytes("1M"), 1000 * 1000);
        assert_eq!(parse_memory_to_bytes("1G"), 1000 * 1000 * 1000);
        assert_eq!(parse_memory_to_bytes("1T"), 1000_i64 * 1000 * 1000 * 1000);
        assert_eq!(
            parse_memory_to_bytes("2T"),
            2 * 1000_i64 * 1000 * 1000 * 1000
        );
    }

    #[test]
    fn test_parse_memory_raw_bytes() {
        assert_eq!(parse_memory_to_bytes("1024"), 1024);
        assert_eq!(parse_memory_to_bytes("1073741824"), 1073741824);
    }

    #[test]
    fn test_parse_memory_with_whitespace() {
        assert_eq!(parse_memory_to_bytes("  512Mi  "), 512 * 1024 * 1024);
    }

    #[test]
    fn test_parse_memory_invalid() {
        assert_eq!(parse_memory_to_bytes("invalid"), 0);
        assert_eq!(parse_memory_to_bytes(""), 0);
    }

    #[test]
    fn test_format_bytes_gi() {
        assert_eq!(format_bytes(1024 * 1024 * 1024), "1Gi");
        assert_eq!(format_bytes(2 * 1024 * 1024 * 1024), "2Gi");
    }

    #[test]
    fn test_format_bytes_mi() {
        assert_eq!(format_bytes(512 * 1024 * 1024), "512Mi");
        assert_eq!(format_bytes(1024 * 1024), "1Mi");
    }

    #[test]
    fn test_format_bytes_ki() {
        assert_eq!(format_bytes(512 * 1024), "512Ki");
        assert_eq!(format_bytes(1024), "1Ki");
    }

    #[test]
    fn test_format_bytes_raw() {
        assert_eq!(format_bytes(512), "512");
        assert_eq!(format_bytes(100), "100");
        assert_eq!(format_bytes(0), "0");
    }

    #[test]
    fn test_cpu_overflow_protection() {
        // Large value that could overflow without saturating_mul
        let large = parse_cpu_to_milli("9223372036854775");
        // The result is close to i64::MAX due to integer core -> milli conversion
        assert!(large > 0); // Should not overflow to negative
        assert!(large >= 9223372036854775000); // Should be very large
    }

    #[test]
    fn test_memory_overflow_protection() {
        // Large Ti value that could overflow
        let large = parse_memory_to_bytes("8388608Ti");
        assert_eq!(large, i64::MAX); // Should saturate to MAX
    }

    #[test]
    fn test_parse_cpu_float_cores() {
        assert_eq!(parse_cpu_to_milli("0.5"), 500);
        assert_eq!(parse_cpu_to_milli("1.5"), 1500);
        assert_eq!(parse_cpu_to_milli("0.25"), 250);
        assert_eq!(parse_cpu_to_milli("2.0"), 2000);
    }

    #[test]
    fn test_parse_gpu_count_integer() {
        assert_eq!(parse_gpu_count("1"), 1);
        assert_eq!(parse_gpu_count("2"), 2);
        assert_eq!(parse_gpu_count("8"), 8);
        assert_eq!(parse_gpu_count("0"), 0);
    }

    #[test]
    fn test_parse_gpu_count_fractional() {
        assert_eq!(parse_gpu_count("0.5"), 1);
        assert_eq!(parse_gpu_count("0.1"), 1);
        assert_eq!(parse_gpu_count("1.5"), 2);
        assert_eq!(parse_gpu_count("2.3"), 3);
    }

    #[test]
    fn test_parse_gpu_count_invalid() {
        assert_eq!(parse_gpu_count(""), 0);
        assert_eq!(parse_gpu_count("invalid"), 0);
    }

    #[test]
    fn test_format_milli_cpu() {
        assert_eq!(format_milli_cpu(500), "500m");
        assert_eq!(format_milli_cpu(1000), "1");
        assert_eq!(format_milli_cpu(2000), "2");
        assert_eq!(format_milli_cpu(1500), "1500m");
        assert_eq!(format_milli_cpu(250), "250m");
    }
}
