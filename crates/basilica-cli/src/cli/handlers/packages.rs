//! Handler for listing billing packages

use crate::client::create_authenticated_client;
use crate::config::CliConfig;
use crate::error::CliError;
use crate::output::json_output;
use crate::progress::{complete_spinner_and_clear, complete_spinner_error, create_spinner};
use basilica_sdk::types::PackagesResponse;
use color_eyre::{eyre::eyre, Help, Result as EyreResult};
use tabled::settings::Style;
use tabled::{Table, Tabled};

/// Handle the `packages` command - list available billing packages
pub async fn handle_packages(json: bool, config: &CliConfig) -> EyreResult<(), CliError> {
    let api_client = create_authenticated_client(config).await?;

    let spinner = create_spinner("Fetching billing packages...");

    // Fetch packages
    let response = api_client.get_packages().await.map_err(|e| {
        complete_spinner_error(spinner.clone(), "Failed to fetch packages");
        CliError::Internal(
            eyre!(e)
                .suggestion("Check your authentication and try again")
                .note("If this persists, the billing service may be temporarily unavailable"),
        )
    })?;

    complete_spinner_and_clear(spinner);

    if json {
        json_output(&response)?;
    } else {
        display_packages(&response)?;
    }

    Ok(())
}

/// Display packages in a formatted table
fn display_packages(response: &PackagesResponse) -> Result<(), CliError> {
    if response.packages.is_empty() {
        println!("No billing packages available.");
        return Ok(());
    }

    #[derive(Tabled)]
    struct PackageRow {
        #[tabled(rename = "NAME")]
        name: String,
        #[tabled(rename = "HOURLY RATE")]
        hourly_rate: String,
        #[tabled(rename = "STATUS")]
        status: String,
        #[tabled(rename = "DESCRIPTION")]
        description: String,
    }

    let rows: Vec<PackageRow> = response
        .packages
        .iter()
        .map(|pkg| PackageRow {
            name: pkg.name.clone(),
            hourly_rate: format!("${}/hr", pkg.hourly_rate),
            status: if pkg.is_active {
                "Active".to_string()
            } else {
                "Inactive".to_string()
            },
            description: pkg.description.clone(),
        })
        .collect();

    let mut table = Table::new(rows);
    table.with(Style::modern());
    println!("{table}");

    println!("\nTotal packages: {}", response.packages.len());
    println!("Current package: {}", response.current_package_id);

    Ok(())
}
