//! Model size estimation and GPU requirement calculation
//!
//! Estimates GPU memory requirements based on model name patterns.
//! Uses heuristics based on parameter count extracted from model names.

use regex::Regex;
use std::sync::LazyLock;

/// GPU requirements for a model
#[derive(Debug, Clone)]
pub struct GpuRequirements {
    /// Number of GPUs required
    pub gpu_count: u32,
    /// Estimated GPU memory required in GB
    pub memory_gb: u32,
    /// Recommended GPU model
    pub recommended_gpu: String,
}

/// Default GPU memory for available GPUs (40GB for A100)
const DEFAULT_GPU_MEMORY_GB: u32 = 40;

/// Regex pattern for extracting parameter count from model names
/// Matches patterns like "7b", "70b", "0.5b", "1.5b", "7B", etc.
static PARAM_REGEX: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(\d+\.?\d*)b").expect("Invalid regex pattern"));

/// Estimate GPU requirements based on model name
///
/// Uses heuristics based on parameter count extracted from model name.
/// Falls back to model family detection if parameter count cannot be extracted.
pub fn estimate_gpu_requirements(model: &str) -> GpuRequirements {
    let model_lower = model.to_lowercase();

    // Extract parameter count from model name
    let memory_gb = if let Some(params) = extract_param_count(&model_lower) {
        estimate_memory_from_params(params)
    } else {
        estimate_from_model_family(&model_lower)
    };

    let gpu_count = calculate_gpu_count(memory_gb, DEFAULT_GPU_MEMORY_GB);
    let recommended_gpu = recommend_gpu(memory_gb);

    GpuRequirements {
        gpu_count,
        memory_gb,
        recommended_gpu,
    }
}

/// Extract billion parameter count from model name
///
/// Patterns: "7b", "70b", "0.5b", "1.5b", "7B", etc.
fn extract_param_count(model: &str) -> Option<f32> {
    PARAM_REGEX
        .captures(model)
        .and_then(|cap| cap.get(1))
        .and_then(|m| m.as_str().parse::<f32>().ok())
}

/// Estimate VRAM needed in GB from parameter count
///
/// Rule of thumb: ~2GB per billion parameters for FP16
/// Add 20% overhead for KV cache and runtime
fn estimate_memory_from_params(params_billions: f32) -> u32 {
    // Base memory: 2GB per billion params * 1.2 overhead
    let base = params_billions * 2.0 * 1.2;
    // Round up to nearest 8GB
    (base as u32).div_ceil(8) * 8
}

/// Estimate memory from model family when parameter count is not available
fn estimate_from_model_family(model: &str) -> u32 {
    // Check for known large models
    if model.contains("llama-2-70b")
        || model.contains("llama-70b")
        || model.contains("mixtral")
        || model.contains("qwen-72b")
    {
        return 160; // ~70B params
    }

    if model.contains("llama-2-13b")
        || model.contains("llama-13b")
        || model.contains("codellama-34b")
    {
        return 32; // ~13-34B params
    }

    if model.contains("llama-2-7b")
        || model.contains("llama-7b")
        || model.contains("mistral-7b")
        || model.contains("qwen-7b")
    {
        return 16; // ~7B params
    }

    if model.contains("phi-2")
        || model.contains("gemma-2b")
        || model.contains("tinyllama")
        || model.contains("qwen3-0.6b")
        || model.contains("qwen2.5-0.5b")
    {
        return 8; // Small models
    }

    // Default: assume medium-sized model
    16
}

/// Calculate number of GPUs needed
fn calculate_gpu_count(required_memory_gb: u32, gpu_memory_gb: u32) -> u32 {
    let count = (required_memory_gb as f32 / gpu_memory_gb as f32).ceil() as u32;
    count.clamp(1, 8) // Maximum 8 GPUs
}

/// Recommend GPU based on memory requirements
/// Returns canonical GPU model names that match autoscaler offerings
fn recommend_gpu(memory_gb: u32) -> String {
    if memory_gb <= 80 {
        "A100".to_string()
    } else {
        "H100".to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_extract_param_count() {
        assert!((extract_param_count("llama-2-7b").unwrap() - 7.0).abs() < 0.001);
        assert!((extract_param_count("qwen3-0.6b").unwrap() - 0.6).abs() < 0.001);
        assert!((extract_param_count("mixtral-8x7b").unwrap() - 7.0).abs() < 0.001);
        assert!((extract_param_count("phi-2").unwrap_or(0.0) - 0.0).abs() < 0.001);
        // No 'b' suffix
    }

    #[test]
    fn test_estimate_memory_from_params() {
        // 7B model: 7 * 2 * 1.2 = 16.8 -> (16+7)/8*8 = 16GB
        assert_eq!(estimate_memory_from_params(7.0), 16);

        // 0.5B model: 0.5 * 2 * 1.2 = 1.2 -> (1+7)/8*8 = 8GB
        assert_eq!(estimate_memory_from_params(0.5), 8);

        // 70B model: 70 * 2 * 1.2 = 168 -> (168+7)/8*8 = 168GB
        assert_eq!(estimate_memory_from_params(70.0), 168);
    }

    #[test]
    fn test_calculate_gpu_count() {
        assert_eq!(calculate_gpu_count(8, 16), 1);
        assert_eq!(calculate_gpu_count(16, 16), 1);
        assert_eq!(calculate_gpu_count(32, 16), 2);
        assert_eq!(calculate_gpu_count(160, 16), 8); // Capped at 8
        assert_eq!(calculate_gpu_count(200, 16), 8); // Capped at 8
    }

    #[test]
    fn test_estimate_gpu_requirements() {
        let reqs = estimate_gpu_requirements("Qwen/Qwen3-0.6B");
        assert_eq!(reqs.gpu_count, 1);
        assert!(reqs.memory_gb <= 16);

        let reqs = estimate_gpu_requirements("meta-llama/Llama-2-7b");
        assert_eq!(reqs.gpu_count, 1); // 16GB fits in 1x40GB A100 GPU
        assert_eq!(reqs.memory_gb, 16);

        // 70b model requires ~168GB, with 40GB A100s that's 5 GPUs (168/40 = 4.2, ceil = 5)
        let reqs = estimate_gpu_requirements("meta-llama/Llama-2-70b");
        assert!(reqs.gpu_count >= 5);
    }

    #[test]
    fn test_recommend_gpu() {
        assert_eq!(recommend_gpu(8), "A100");
        assert_eq!(recommend_gpu(16), "A100");
        assert_eq!(recommend_gpu(32), "A100");
        assert_eq!(recommend_gpu(64), "A100");
        assert_eq!(recommend_gpu(160), "H100");
    }
}
