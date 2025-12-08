//! Deploy command handlers

use crate::cli::commands::{DeployAction, DeployCommand};
use crate::client::create_authenticated_client;
use crate::config::CliConfig;
use crate::error::{CliError, DeployError};
use crate::output::{json_output, print_error, print_info, print_success};
use crate::progress::{complete_spinner_and_clear, create_spinner};

mod create;
mod helpers;
mod validation;

/// Handle all deploy subcommands (matches existing handler pattern)
pub async fn handle_deploy(cmd: DeployCommand, config: &CliConfig) -> Result<(), CliError> {
    // Validate request before doing anything
    if cmd.action.is_none() && cmd.source.is_some() {
        validation::validate_deployment_request(&cmd)?;
    }

    // Create authenticated client
    let client = create_authenticated_client(config).await?;

    // Global output flags from parent command
    let json = cmd.json;
    let show_phases = cmd.show_phases;

    match cmd.action {
        Some(DeployAction::List) => handle_list(&client, json).await,
        Some(DeployAction::Status { name }) => {
            handle_status(&client, &name, json, show_phases).await
        }
        Some(DeployAction::Logs { name, follow, tail }) => {
            handle_logs(&client, &name, follow, tail).await
        }
        Some(DeployAction::Delete { name, yes }) => handle_delete(&client, &name, yes).await,
        Some(DeployAction::Scale { name, replicas }) => {
            handle_scale(&client, &name, replicas).await
        }
        None => {
            if let Some(source) = cmd.source.clone() {
                create::handle_create(&client, &source, cmd).await
            } else {
                print_error(
                    "No source specified. Use 'basilica deploy <source>' or 'basilica deploy ls'",
                );
                Ok(())
            }
        }
    }
}

/// List all deployments
async fn handle_list(client: &basilica_sdk::BasilicaClient, json: bool) -> Result<(), CliError> {
    let spinner = create_spinner("Fetching deployments...");

    let response = client.list_deployments().await.map_err(CliError::Api)?;

    complete_spinner_and_clear(spinner);

    if json {
        json_output(&response)?;
    } else {
        helpers::print_deployments_table(&response.deployments);
    }

    Ok(())
}

/// Get deployment status with phase tracking
async fn handle_status(
    client: &basilica_sdk::BasilicaClient,
    name: &str,
    json: bool,
    verbose: bool,
) -> Result<(), CliError> {
    let spinner = create_spinner(&format!("Fetching deployment '{}'...", name));

    let response = client.get_deployment(name).await.map_err(|e| {
        if matches!(e, basilica_sdk::error::ApiError::NotFound { .. }) {
            CliError::Deploy(DeployError::NotFound {
                name: name.to_string(),
            })
        } else {
            CliError::Api(e)
        }
    })?;

    complete_spinner_and_clear(spinner);

    if json {
        json_output(&response)?;
    } else {
        helpers::print_deployment_details(&response, verbose);
    }

    Ok(())
}

/// Stream deployment logs
async fn handle_logs(
    client: &basilica_sdk::BasilicaClient,
    name: &str,
    follow: bool,
    tail: Option<u32>,
) -> Result<(), CliError> {
    let response = client
        .get_deployment_logs(name, follow, tail)
        .await
        .map_err(CliError::Api)?;

    helpers::stream_logs_to_stdout(response).await
}

/// Delete a deployment
async fn handle_delete(
    client: &basilica_sdk::BasilicaClient,
    name: &str,
    skip_confirm: bool,
) -> Result<(), CliError> {
    if !skip_confirm {
        let confirm = dialoguer::Confirm::new()
            .with_prompt(format!("Delete deployment '{}'?", name))
            .default(false)
            .interact()
            .map_err(|e| {
                CliError::Internal(color_eyre::eyre::eyre!("Failed to get confirmation: {}", e))
            })?;

        if !confirm {
            print_info("Deletion cancelled");
            return Ok(());
        }
    }

    let spinner = create_spinner(&format!("Deleting deployment '{}'...", name));

    client
        .delete_deployment(name)
        .await
        .map_err(CliError::Api)?;

    complete_spinner_and_clear(spinner);

    print_success(&format!("Deployment '{}' deletion initiated", name));

    Ok(())
}

/// Scale deployment replicas
async fn handle_scale(
    client: &basilica_sdk::BasilicaClient,
    name: &str,
    replicas: u32,
) -> Result<(), CliError> {
    let spinner = create_spinner(&format!(
        "Scaling deployment '{}' to {} replicas...",
        name, replicas
    ));

    // Verify deployment exists before scaling
    client.get_deployment(name).await.map_err(|e| {
        if matches!(e, basilica_sdk::error::ApiError::NotFound { .. }) {
            CliError::Deploy(DeployError::NotFound {
                name: name.to_string(),
            })
        } else {
            CliError::Api(e)
        }
    })?;

    // Scale via dedicated endpoint
    client
        .scale_deployment(name, replicas)
        .await
        .map_err(CliError::Api)?;

    complete_spinner_and_clear(spinner);

    print_success(&format!(
        "Deployment '{}' scaled to {} replicas",
        name, replicas
    ));

    Ok(())
}
