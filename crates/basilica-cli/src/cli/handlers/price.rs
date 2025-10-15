use basilica_sdk::BasilicaClient;
use color_eyre::{eyre::eyre, Help, Result as EyreResult};

use crate::{
    error::CliError,
    output::{json_output, table_output},
    progress::{complete_spinner_and_clear, complete_spinner_error, create_spinner},
};

/// Handle the price check command
pub async fn handle_price(
    client: &BasilicaClient,
    gpu_type: Option<String>,
    hours: Option<u32>,
    all: bool,
    json: bool,
) -> EyreResult<(), CliError> {
    let spinner = create_spinner("Fetching pricing information...");

    // Fetch packages from billing service
    let packages_response = client.get_packages().await.map_err(|e| {
        complete_spinner_error(spinner.clone(), "Failed to fetch pricing");
        CliError::Internal(
            eyre!(e)
                .suggestion("Check your authentication and try again")
                .note("If this persists, the billing service may be temporarily unavailable"),
        )
    })?;

    // Optionally fetch balance for affordability calculation
    let balance = client.get_balance().await.ok();

    complete_spinner_and_clear(spinner);

    if json {
        json_output(&packages_response)?;
        return Ok(());
    }

    if all {
        // Display all GPU types in a table
        table_output::display_pricing_table(&packages_response, balance.as_ref())?;
    } else if let Some(gpu_type) = gpu_type {
        // Display specific GPU pricing
        let package = packages_response
            .packages
            .iter()
            .find(|p| p.name.to_lowercase() == gpu_type.to_lowercase())
            .ok_or_else(|| {
                let available_types: Vec<String> = packages_response
                    .packages
                    .iter()
                    .map(|p| p.name.clone())
                    .collect();
                CliError::Internal(
                    eyre!("GPU type '{}' not found", gpu_type)
                        .suggestion(format!(
                            "Available GPU types: {}",
                            available_types.join(", ")
                        ))
                        .note("Run `basilica price --all` to see all available GPU types"),
                )
            })?;

        table_output::display_gpu_pricing(package, hours, balance.as_ref())?;
    } else {
        // No specific GPU and no --all flag, show all by default
        table_output::display_pricing_table(&packages_response, balance.as_ref())?;
    }

    Ok(())
}
