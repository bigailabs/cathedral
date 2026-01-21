//! Helper functions for deploy command

use crate::error::{CliError, DeployError};
use crate::output::{print_info, print_success};
use crate::progress::{complete_spinner_and_clear, create_spinner};
use basilica_sdk::types::{DeploymentResponse, DeploymentSummary};
use basilica_sdk::BasilicaClient;
use color_eyre::eyre::eyre;
use console::{style, Term};
use dialoguer::{theme::ColorfulTheme, Select};
use std::collections::HashMap;

/// Generate RFC 1123 compliant deployment name from source
pub fn generate_deployment_name(source: &str) -> String {
    let path = std::path::Path::new(source);
    let stem = path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("deployment");

    // Sanitize for DNS label (RFC 1123)
    let sanitized: String = stem
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
        .collect();

    // Trim leading/trailing hyphens
    let sanitized = sanitized.trim_matches('-');

    // Enforce max length (63 chars for DNS labels, minus UUID suffix)
    let max_prefix_len = 63 - 9; // 8 for UUID + 1 for dash
    let prefix = if sanitized.len() > max_prefix_len {
        &sanitized[..max_prefix_len]
    } else {
        sanitized
    };

    // Handle empty result
    let prefix = if prefix.is_empty() {
        "deployment"
    } else {
        prefix
    };

    format!("{}-{}", prefix, &uuid::Uuid::new_v4().to_string()[..8])
}

/// Parse KEY=VALUE environment variable strings
pub fn parse_env_vars(env: &[String]) -> Result<HashMap<String, String>, DeployError> {
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

/// Parse primary port from port specifications
pub fn parse_primary_port(ports: &[String]) -> Result<u16, DeployError> {
    let first_port = ports.first().ok_or_else(|| DeployError::Validation {
        message: "At least one port must be specified".to_string(),
    })?;

    let port_str = first_port.split(':').next().unwrap_or(first_port);
    port_str
        .parse::<u16>()
        .map_err(|_| DeployError::Validation {
            message: format!("Invalid port number: {}", port_str),
        })
}

/// Print deployment success message
pub fn print_deployment_success(deployment: &DeploymentResponse) {
    print_success(&format!(
        "Deployment '{}' created successfully!",
        deployment.instance_name
    ));
    println!();
    println!("  URL:      {}", deployment.url);
    println!("  State:    {}", deployment.state);
    println!(
        "  Replicas: {}/{}",
        deployment.replicas.ready, deployment.replicas.desired
    );

    if let Some(ref phase) = deployment.phase {
        println!("  Phase:    {}", phase);
    }

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

/// Print deployments table
pub fn print_deployments_table(deployments: &[DeploymentSummary]) {
    if deployments.is_empty() {
        print_info("No deployments found");
        return;
    }

    use tabled::{Table, Tabled};

    #[derive(Tabled)]
    struct Row {
        name: String,
        state: String,
        replicas: String,
        url: String,
        created: String,
    }

    let rows: Vec<Row> = deployments
        .iter()
        .map(|d| Row {
            name: d.instance_name.clone(),
            state: d.state.clone(),
            replicas: format!("{}/{}", d.replicas.ready, d.replicas.desired),
            url: truncate_url(&d.url, 50),
            created: d.created_at.clone(),
        })
        .collect();

    let table = Table::new(rows).to_string();
    println!("{}", table);
}

/// Print deployment details
pub fn print_deployment_details(deployment: &DeploymentResponse, verbose: bool) {
    println!("Deployment: {}", deployment.instance_name);
    println!();
    println!("  Namespace:  {}", deployment.namespace);
    println!("  State:      {}", deployment.state);
    println!("  URL:        {}", deployment.url);
    println!(
        "  Replicas:   {}/{}",
        deployment.replicas.ready, deployment.replicas.desired
    );
    println!("  Created:    {}", deployment.created_at);

    if let Some(ref updated) = deployment.updated_at {
        println!("  Updated:    {}", updated);
    }

    if let Some(ref phase) = deployment.phase {
        println!("  Phase:      {}", phase);
    }

    if verbose {
        if let Some(ref progress) = deployment.progress {
            println!();
            println!("Progress:");
            println!("  Current step: {}", progress.current_step);
            if let Some(pct) = progress.percentage {
                println!("  Progress:     {:.1}%", pct);
            }
            println!("  Elapsed:      {}s", progress.elapsed_seconds);
        }

        if let Some(ref pods) = deployment.pods {
            println!();
            println!("Pods:");
            for pod in pods {
                println!("  - {} ({})", pod.name, pod.status);
                if let Some(ref node) = pod.node {
                    println!("    Node: {}", node);
                }
            }
        }
    }
}

/// Stream logs to stdout with backpressure handling
pub async fn stream_logs_to_stdout(
    response: reqwest::Response,
) -> Result<(), crate::error::CliError> {
    use futures::StreamExt;
    use std::io::Write;

    let mut stream = response.bytes_stream();
    let stdout = std::io::stdout();
    let mut handle = stdout.lock();

    while let Some(chunk) = stream.next().await {
        let bytes = chunk.map_err(|e| {
            crate::error::CliError::Internal(color_eyre::eyre::eyre!("Stream error: {}", e))
        })?;
        handle.write_all(&bytes).map_err(|e| {
            crate::error::CliError::Internal(color_eyre::eyre::eyre!("Write error: {}", e))
        })?;
        handle.flush().map_err(|e| {
            crate::error::CliError::Internal(color_eyre::eyre::eyre!("Flush error: {}", e))
        })?;
    }

    Ok(())
}

/// Truncate URL for table display
fn truncate_url(url: &str, max_len: usize) -> String {
    if url.len() <= max_len {
        url.to_string()
    } else {
        format!("{}...", &url[..max_len - 3])
    }
}

/// Truncate string for display (unicode-safe)
fn truncate(s: &str, max_len: usize) -> String {
    if max_len == 0 {
        return String::new();
    }
    let char_count = s.chars().count();
    if char_count <= max_len {
        s.to_string()
    } else {
        let truncated: String = s.chars().take(max_len - 1).collect();
        format!("{}…", truncated)
    }
}

/// Resolve deployment name - if not provided, fetch deployments and prompt for selection
pub async fn resolve_deployment_name(
    name: Option<String>,
    client: &BasilicaClient,
) -> Result<String, CliError> {
    if let Some(n) = name {
        return Ok(n);
    }

    let spinner = create_spinner("Fetching deployments...");
    let response = client.list_deployments().await.map_err(CliError::Api)?;
    complete_spinner_and_clear(spinner);

    if response.deployments.is_empty() {
        return Err(CliError::Internal(eyre!(
            "No deployments found. Create one with 'basilica deploy <source>'"
        )));
    }

    // Format for selection display
    let items: Vec<String> = response
        .deployments
        .iter()
        .map(|d| {
            format!(
                "{:<30} │ {:<10} │ {}/{}",
                truncate(&d.instance_name, 30),
                d.state,
                d.replicas.ready,
                d.replicas.desired
            )
        })
        .collect();

    // Build prompt with header on next line
    let header = style("  Name                           │ State      │ Replicas").dim();
    let prompt = format!("Select deployment\n{}", header);

    let selection = Select::with_theme(&ColorfulTheme::default())
        .with_prompt(prompt)
        .items(&items)
        .default(0)
        .interact_opt()
        .map_err(|e| CliError::Internal(eyre!("Selection failed: {}", e)))?;

    let selection = selection.ok_or_else(|| CliError::Internal(eyre!("Selection cancelled")))?;

    // Clear prompt (includes header) and selection
    let _ = Term::stdout().clear_last_lines(2);

    Ok(response.deployments[selection].instance_name.clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_generate_deployment_name_sanitization() {
        // "My App_v2.py" -> file_stem="My App_v2" -> sanitized="my-app-v2" (space filtered, underscore to hyphen)
        let name = generate_deployment_name("My_App_v2.py");
        assert!(name.starts_with("my-app-v2-"));
        assert!(name.len() <= 63);
        assert!(name
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-'));
    }

    #[test]
    fn test_generate_deployment_name_long_input() {
        let long_source = format!("{}.py", "a".repeat(100));
        let name = generate_deployment_name(&long_source);
        assert!(name.len() <= 63);
    }

    #[test]
    fn test_parse_env_vars_valid() {
        let vars = vec!["KEY1=value1".to_string(), "KEY2=value2".to_string()];
        let result = parse_env_vars(&vars).unwrap();
        assert_eq!(result.get("KEY1"), Some(&"value1".to_string()));
        assert_eq!(result.get("KEY2"), Some(&"value2".to_string()));
    }

    #[test]
    fn test_parse_env_vars_with_equals_in_value() {
        let vars = vec!["URL=http://example.com?foo=bar".to_string()];
        let result = parse_env_vars(&vars).unwrap();
        assert_eq!(
            result.get("URL"),
            Some(&"http://example.com?foo=bar".to_string())
        );
    }

    #[test]
    fn test_parse_env_vars_invalid() {
        let vars = vec!["INVALID_NO_EQUALS".to_string()];
        assert!(parse_env_vars(&vars).is_err());
    }

    #[test]
    fn test_parse_primary_port_valid() {
        let ports = vec!["8000".to_string(), "9090".to_string()];
        assert_eq!(parse_primary_port(&ports).unwrap(), 8000);
    }

    #[test]
    fn test_parse_primary_port_with_name() {
        let ports = vec!["8000:http".to_string()];
        assert_eq!(parse_primary_port(&ports).unwrap(), 8000);
    }

    #[test]
    fn test_parse_primary_port_empty() {
        let ports: Vec<String> = vec![];
        assert!(parse_primary_port(&ports).is_err());
    }
}
