//! vLLM deployment template
//!
//! Provides a pre-configured deployment for vLLM OpenAI-compatible inference servers.
//! Handles GPU auto-detection, storage configuration, and health checks.

use crate::cli::commands::{TemplateCommonOptions, VllmOptions};
use crate::error::{CliError, DeployError};
use crate::output::{print_info, print_success};
use crate::progress::{complete_spinner_and_clear, create_spinner};
use basilica_sdk::types::{
    CreateDeploymentRequest, DeploymentResponse, GpuRequirementsSpec, HealthCheckConfig,
    PersistentStorageSpec, ProbeConfig, ResourceRequirements, StorageBackend, StorageSpec,
};
use basilica_sdk::BasilicaClient;
use std::collections::HashMap;
use std::time::{Duration, Instant};

use super::model_size::estimate_gpu_requirements;

/// Default vLLM Docker image
const VLLM_IMAGE: &str = "vllm/vllm-openai:latest";

/// Default model for quick testing
const DEFAULT_MODEL: &str = "Qwen/Qwen3-0.6B";

/// Default port for vLLM OpenAI API
const VLLM_PORT: u32 = 8000;

/// Maximum retries for transient failures
const MAX_RETRIES: u32 = 3;

/// Initial retry delay
const INITIAL_RETRY_DELAY_MS: u64 = 500;

/// Handle vLLM deployment
pub async fn handle_vllm_deploy(
    client: &BasilicaClient,
    model: Option<String>,
    common: TemplateCommonOptions,
    vllm: VllmOptions,
) -> Result<(), CliError> {
    let model = model.unwrap_or_else(|| DEFAULT_MODEL.to_string());

    // Always estimate GPU requirements to get recommended GPU
    let estimated = estimate_gpu_requirements(&model);

    // Use user-specified GPU count or auto-detected
    let gpu_count = common.gpu.unwrap_or_else(|| {
        print_info(&format!(
            "Auto-detected GPU requirements: {} GPU(s), ~{}GB VRAM ({})",
            estimated.gpu_count, estimated.memory_gb, estimated.recommended_gpu
        ));
        estimated.gpu_count
    });

    // Use user-specified GPU models or recommended GPU
    let gpu_models = if common.gpu_model.is_empty() {
        vec![estimated.recommended_gpu.clone()]
    } else {
        common.gpu_model.clone()
    };

    // Generate deployment name
    let name = common
        .name
        .clone()
        .unwrap_or_else(|| generate_vllm_name(&model));

    // Build vLLM command
    let (command, args) = build_vllm_command(&model, &vllm);

    // Parse environment variables
    let env = parse_env_vars(&common.env)?;

    // Build resources
    let resources = build_vllm_resources(gpu_count, &common, &gpu_models);

    // Build storage spec (enabled by default for model caching)
    let storage = if common.no_storage {
        None
    } else {
        Some(build_vllm_storage())
    };

    // Build health check
    let health_check = Some(build_vllm_health_check());

    // Create deployment request
    let request = CreateDeploymentRequest {
        instance_name: name.clone(),
        image: VLLM_IMAGE.to_string(),
        replicas: 1,
        port: VLLM_PORT,
        command: Some(command),
        args: Some(args),
        env: Some(env),
        resources: Some(resources),
        ttl_seconds: common.ttl,
        public: true,
        storage,
        health_check,
        enable_billing: true,
        queue_name: None,
        suspended: false,
        priority: None,
    };

    // Show spinner
    let spinner = create_spinner(&format!(
        "Creating vLLM deployment '{}' with model '{}'...",
        name, model
    ));

    // Create deployment with retry
    let response = create_with_retry(client, request).await?;

    complete_spinner_and_clear(spinner);

    let actual_name = response.instance_name.clone();

    // Wait for ready if not detached
    if !common.detach {
        let result = wait_for_ready(client, &actual_name, common.timeout).await?;

        match result {
            WaitResult::Ready(deployment) => {
                if common.json {
                    crate::output::json_output(&deployment)?;
                } else {
                    print_vllm_success(&deployment, &model);
                }
            }
            WaitResult::Failed(reason) => {
                return Err(CliError::Deploy(DeployError::DeploymentFailed {
                    name: actual_name,
                    reason,
                }));
            }
            WaitResult::Timeout => {
                return Err(CliError::Deploy(DeployError::Timeout {
                    name: actual_name,
                    timeout_secs: common.timeout,
                }));
            }
        }
    } else if common.json {
        crate::output::json_output(&response)?;
    } else {
        print_success(&format!(
            "vLLM deployment '{}' created (detached mode)",
            actual_name
        ));
        println!("  Check status: basilica deploy status {}", actual_name);
    }

    Ok(())
}

/// Generate RFC 1123 compliant deployment name from model
fn generate_vllm_name(model: &str) -> String {
    // Extract model name without org prefix
    let model_part = model
        .split('/')
        .next_back()
        .unwrap_or(model)
        .to_lowercase()
        .chars()
        .filter_map(|c| {
            if c.is_ascii_alphanumeric() {
                Some(c)
            } else if c == '-' || c == '_' || c == '.' {
                Some('-')
            } else {
                None
            }
        })
        .collect::<String>();

    let sanitized = model_part.trim_matches('-');
    let prefix = if sanitized.len() > 40 {
        &sanitized[..40]
    } else {
        sanitized
    };

    let prefix = if prefix.is_empty() { "vllm" } else { prefix };

    format!("vllm-{}-{}", prefix, &uuid::Uuid::new_v4().to_string()[..8])
}

/// Build vLLM serve command and arguments
fn build_vllm_command(model: &str, opts: &VllmOptions) -> (Vec<String>, Vec<String>) {
    let mut args = vec![
        "serve".to_string(),
        model.to_string(),
        "--host".to_string(),
        "0.0.0.0".to_string(),
        "--port".to_string(),
        VLLM_PORT.to_string(),
    ];

    if let Some(tp) = opts.tensor_parallel_size {
        args.extend(["--tensor-parallel-size".to_string(), tp.to_string()]);
    }

    if let Some(max_len) = opts.max_model_len {
        args.extend(["--max-model-len".to_string(), max_len.to_string()]);
    }

    if let Some(ref dtype) = opts.dtype {
        args.extend(["--dtype".to_string(), dtype.clone()]);
    }

    if let Some(ref quant) = opts.quantization {
        args.extend(["--quantization".to_string(), quant.clone()]);
    }

    if let Some(ref name) = opts.served_model_name {
        args.extend(["--served-model-name".to_string(), name.clone()]);
    }

    if let Some(ref key) = opts.api_key {
        args.extend(["--api-key".to_string(), key.clone()]);
    }

    if let Some(util) = opts.gpu_memory_utilization {
        args.extend([
            "--gpu-memory-utilization".to_string(),
            util.to_string(),
        ]);
    }

    if opts.enforce_eager {
        args.push("--enforce-eager".to_string());
    }

    if opts.trust_remote_code {
        args.push("--trust-remote-code".to_string());
    }

    (vec!["vllm".to_string()], args)
}

/// Parse KEY=VALUE environment variable strings
fn parse_env_vars(env: &[String]) -> Result<HashMap<String, String>, DeployError> {
    let mut map = HashMap::new();

    for entry in env {
        let mut parts = entry.splitn(2, '=');
        let key = parts.next().ok_or_else(|| DeployError::Validation {
            message: format!("Invalid env var format: '{}'", entry),
        })?;
        let value = parts.next().ok_or_else(|| DeployError::Validation {
            message: format!("Invalid env var format: '{}'. Use KEY=VALUE", entry),
        })?;

        map.insert(key.to_string(), value.to_string());
    }

    Ok(map)
}

/// Build resource requirements for vLLM
fn build_vllm_resources(
    gpu_count: u32,
    common: &TemplateCommonOptions,
    gpu_models: &[String],
) -> ResourceRequirements {
    let gpus = if gpu_count > 0 {
        Some(GpuRequirementsSpec {
            count: gpu_count,
            model: gpu_models.to_vec(),
            min_cuda_version: None,
            min_gpu_memory_gb: None,
        })
    } else {
        None
    };

    ResourceRequirements {
        cpu: "4".to_string(),
        memory: common.memory.clone(),
        cpu_request: Some("2".to_string()),
        memory_request: Some("8Gi".to_string()),
        gpus,
    }
}

/// Build storage spec for vLLM model caching
fn build_vllm_storage() -> StorageSpec {
    StorageSpec {
        persistent: Some(PersistentStorageSpec {
            enabled: true,
            backend: StorageBackend::R2,
            bucket: String::new(), // API assigns default
            region: Some("auto".to_string()),
            endpoint: None,
            credentials_secret: Some("basilica-r2-credentials".to_string()),
            sync_interval_ms: 1000,
            cache_size_mb: 4096, // 4GB cache for model files
            mount_path: "/root/.cache".to_string(),
        }),
    }
}

/// Build health check configuration for vLLM
fn build_vllm_health_check() -> HealthCheckConfig {
    HealthCheckConfig {
        liveness: Some(ProbeConfig {
            path: "/health".to_string(),
            port: Some(VLLM_PORT as u16),
            initial_delay_seconds: 60,
            period_seconds: 30,
            timeout_seconds: 10,
            failure_threshold: 3,
        }),
        readiness: Some(ProbeConfig {
            path: "/health".to_string(),
            port: Some(VLLM_PORT as u16),
            initial_delay_seconds: 30,
            period_seconds: 10,
            timeout_seconds: 5,
            failure_threshold: 3,
        }),
        startup: Some(ProbeConfig {
            path: "/health".to_string(),
            port: Some(VLLM_PORT as u16),
            initial_delay_seconds: 0,
            period_seconds: 10,
            timeout_seconds: 5,
            failure_threshold: 60, // Allow up to 10 minutes for model loading
        }),
    }
}

/// Create deployment with exponential backoff retry
async fn create_with_retry(
    client: &BasilicaClient,
    request: CreateDeploymentRequest,
) -> Result<DeploymentResponse, CliError> {
    use rand::Rng;

    let mut last_error = None;
    let mut delay = Duration::from_millis(INITIAL_RETRY_DELAY_MS);

    for attempt in 0..MAX_RETRIES {
        match client.create_deployment(request.clone()).await {
            Ok(response) => return Ok(response),
            Err(e) if is_quota_exceeded(&e) => {
                return Err(CliError::Deploy(DeployError::QuotaExceeded {
                    message: extract_quota_message(&e),
                }));
            }
            Err(e) if e.is_retryable() => {
                last_error = Some(e);
                if attempt < MAX_RETRIES - 1 {
                    let jitter_factor = rand::thread_rng().gen_range(0.75..1.25);
                    let jittered_delay = delay.mul_f64(jitter_factor);
                    tokio::time::sleep(jittered_delay).await;
                    delay *= 2;
                }
            }
            Err(e) => return Err(CliError::Api(e)),
        }
    }

    Err(CliError::Api(last_error.unwrap()))
}

/// Check if API error indicates quota exceeded
fn is_quota_exceeded(error: &basilica_sdk::error::ApiError) -> bool {
    match error {
        basilica_sdk::error::ApiError::QuotaExceeded { .. } => true,
        basilica_sdk::error::ApiError::ApiResponse { status, message } => {
            *status == 403
                || *status == 429
                || message.to_lowercase().contains("quota")
                || message.to_lowercase().contains("limit exceeded")
        }
        _ => false,
    }
}

/// Extract quota message from API error
fn extract_quota_message(error: &basilica_sdk::error::ApiError) -> String {
    match error {
        basilica_sdk::error::ApiError::QuotaExceeded { message } => message.clone(),
        basilica_sdk::error::ApiError::ApiResponse { message, .. } => message.clone(),
        _ => error.to_string(),
    }
}

/// Result of waiting for deployment
enum WaitResult {
    Ready(Box<DeploymentResponse>),
    Failed(String),
    Timeout,
}

/// Wait for deployment to become ready
async fn wait_for_ready(
    client: &BasilicaClient,
    name: &str,
    timeout_secs: u32,
) -> Result<WaitResult, CliError> {
    let start = Instant::now();
    let timeout = Duration::from_secs(timeout_secs as u64);
    let mut last_phase: Option<String> = None;
    let mut spinner = create_spinner("Waiting for vLLM deployment...");

    loop {
        if start.elapsed() > timeout {
            complete_spinner_and_clear(spinner);
            return Ok(WaitResult::Timeout);
        }

        let status = client.get_deployment(name).await.map_err(CliError::Api)?;

        // Update phase display
        if let Some(ref phase) = status.phase {
            if last_phase.as_ref() != Some(phase) {
                complete_spinner_and_clear(spinner);

                let phase_msg = format_phase_message(phase);
                print_info(&phase_msg);

                spinner = create_spinner(&format!(
                    "Phase: {} ({}/{})",
                    phase, status.replicas.ready, status.replicas.desired
                ));

                last_phase = Some(phase.clone());
            }
        }

        // Check terminal states
        if status.state == "Active" && status.replicas.ready >= status.replicas.desired {
            complete_spinner_and_clear(spinner);
            return Ok(WaitResult::Ready(Box::new(status)));
        }

        if status.state == "Failed" {
            complete_spinner_and_clear(spinner);
            let reason = status
                .message
                .unwrap_or_else(|| "Unknown error".to_string());
            return Ok(WaitResult::Failed(reason));
        }

        if status.state == "Terminating" || status.phase.as_deref() == Some("terminating") {
            complete_spinner_and_clear(spinner);
            return Ok(WaitResult::Failed(
                "Deployment is being terminated".to_string(),
            ));
        }

        // Dynamic sleep based on phase
        let sleep_duration = match status.phase.as_deref() {
            Some("scheduling") | Some("pulling") => Duration::from_secs(10),
            Some("storage_sync") => Duration::from_secs(3),
            _ => Duration::from_secs(5),
        };

        tokio::time::sleep(sleep_duration).await;
    }
}

/// Format human-readable phase message
fn format_phase_message(phase: &str) -> String {
    match phase {
        "pending" => "Deployment created, waiting for scheduler...".to_string(),
        "scheduling" => "Finding suitable GPU node...".to_string(),
        "pulling" => "Pulling vLLM container image...".to_string(),
        "initializing" => "Running init containers...".to_string(),
        "storage_sync" => "Syncing model cache storage...".to_string(),
        "starting" => "Starting vLLM server (loading model)...".to_string(),
        "health_check" => "Running health checks...".to_string(),
        "ready" => "vLLM server ready!".to_string(),
        _ => format!("Phase: {}", phase),
    }
}

/// Print vLLM deployment success message
fn print_vllm_success(deployment: &DeploymentResponse, model: &str) {
    print_success(&format!(
        "vLLM deployment '{}' is ready!",
        deployment.instance_name
    ));
    println!();
    println!("  Model:      {}", model);
    println!("  URL:        {}", deployment.url);
    println!("  State:      {}", deployment.state);
    println!(
        "  Replicas:   {}/{}",
        deployment.replicas.ready, deployment.replicas.desired
    );
    println!();
    println!("OpenAI-compatible API endpoints:");
    println!("  Chat:       {}/v1/chat/completions", deployment.url);
    println!("  Completions: {}/v1/completions", deployment.url);
    println!("  Models:     {}/v1/models", deployment.url);
    println!("  Health:     {}/health", deployment.url);
    println!();
    println!("Example usage:");
    println!("  curl {}/v1/chat/completions \\", deployment.url);
    println!("    -H \"Content-Type: application/json\" \\");
    println!("    -d '{{\"model\": \"{}\", \"messages\": [{{\"role\": \"user\", \"content\": \"Hello!\"}}]}}'", model);
    println!();
    println!("Commands:");
    println!(
        "  View status:  basilica deploy status {}",
        deployment.instance_name
    );
    println!(
        "  View logs:    basilica deploy logs {}",
        deployment.instance_name
    );
    println!(
        "  Delete:       basilica deploy delete {}",
        deployment.instance_name
    );
}
