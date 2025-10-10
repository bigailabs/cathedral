use basilica_sdk::BasilicaClient;
use color_eyre::{eyre::eyre, Help, Result as EyreResult};
use console::style;
use rust_decimal::Decimal;
use tabled::{Table, Tabled};

use crate::{
    error::CliError,
    output::json_output,
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
        display_pricing_table(&packages_response, balance.as_ref())?;
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

        display_gpu_pricing(package, hours, balance.as_ref())?;
    } else {
        // No specific GPU and no --all flag, show all by default
        display_pricing_table(&packages_response, balance.as_ref())?;
    }

    Ok(())
}

/// Display pricing table for all GPU types
fn display_pricing_table(
    packages: &basilica_sdk::types::PackagesResponse,
    balance: Option<&basilica_sdk::types::BalanceResponse>,
) -> EyreResult<(), CliError> {
    if packages.packages.is_empty() {
        println!();
        println!("{}", style("No pricing packages available").yellow());
        println!();
        return Ok(());
    }

    #[derive(Tabled)]
    struct PricingRow {
        #[tabled(rename = "GPU Type")]
        gpu_type: String,
        #[tabled(rename = "Hourly Rate")]
        hourly_rate: String,
        #[tabled(rename = "8-Hour Cost")]
        eight_hour: String,
        #[tabled(rename = "24-Hour Cost")]
        twenty_four_hour: String,
        #[tabled(rename = "Hours Available")]
        hours_available: String,
    }

    let mut rows: Vec<(Decimal, PricingRow)> = Vec::new();

    // Parse balance once if available
    let available_balance = balance.and_then(|b| b.available.parse::<Decimal>().ok());

    for package in &packages.packages {
        if !package.is_active {
            continue;
        }

        let hourly_rate = package
            .hourly_rate
            .parse::<Decimal>()
            .map_err(|e| CliError::Internal(eyre!("Invalid hourly rate format: {}", e)))?;

        let eight_hour_cost = hourly_rate * Decimal::from(8);
        let twenty_four_hour_cost = hourly_rate * Decimal::from(24);

        let hours_available = if let Some(balance) = available_balance {
            if hourly_rate > Decimal::ZERO {
                let hours = balance / hourly_rate;
                format!("{:.1}h", hours)
            } else {
                "N/A".to_string()
            }
        } else {
            "-".to_string()
        };

        rows.push((
            hourly_rate,
            PricingRow {
                gpu_type: package.name.clone(),
                hourly_rate: format!("${:.2}/hr", hourly_rate),
                eight_hour: format!("${:.2}", eight_hour_cost),
                twenty_four_hour: format!("${:.2}", twenty_four_hour_cost),
                hours_available,
            },
        ));
    }

    // Sort by hourly rate ascending (numeric)
    rows.sort_by(|a, b| a.0.cmp(&b.0));

    // Extract rows after sorting
    let rows: Vec<PricingRow> = rows.into_iter().map(|(_, r)| r).collect();

    println!();
    println!("{}", style("GPU Pricing").bold());
    println!();
    println!("{}", Table::new(&rows));
    println!();

    if let Some(balance) = balance {
        println!(
            "{}: {} credits",
            style("Your Balance").cyan(),
            style(&balance.available).green().bold()
        );
        println!();
    }

    Ok(())
}

/// Display pricing for a specific GPU type
fn display_gpu_pricing(
    package: &basilica_sdk::types::BillingPackageInfo,
    hours: Option<u32>,
    balance: Option<&basilica_sdk::types::BalanceResponse>,
) -> EyreResult<(), CliError> {
    let hourly_rate = package
        .hourly_rate
        .parse::<Decimal>()
        .map_err(|e| CliError::Internal(eyre!("Invalid hourly rate format: {}", e)))?;

    println!();
    println!("{}", style(&package.name).bold().cyan());
    println!();
    println!("  {}: {}", style("Description").dim(), package.description);
    println!(
        "  {}: {}",
        style("Hourly Rate").cyan(),
        style(format!("${:.2}/hr", hourly_rate)).green().bold()
    );

    if let Some(hours) = hours {
        let total_cost = hourly_rate * Decimal::from(hours);
        println!();
        println!(
            "  {}: {} hours",
            style("Duration").cyan(),
            style(hours).yellow()
        );
        println!(
            "  {}: {}",
            style("Estimated Cost").cyan(),
            style(format!("${:.2}", total_cost)).green().bold()
        );
    }

    if let Some(balance) = balance {
        let available_balance = balance
            .available
            .parse::<Decimal>()
            .map_err(|e| CliError::Internal(eyre!("Invalid balance format: {}", e)))?;

        println!();
        println!(
            "  {}: {} credits",
            style("Your Balance").cyan(),
            style(format!("{:.2}", available_balance)).green()
        );

        if hourly_rate > Decimal::ZERO {
            let hours_available = available_balance / hourly_rate;
            println!(
                "  {}: {} hours",
                style("Hours Available").cyan(),
                style(format!("{:.1}", hours_available)).yellow()
            );

            if let Some(requested_hours) = hours {
                let total_cost = hourly_rate * Decimal::from(requested_hours);
                if total_cost > available_balance {
                    let shortfall = total_cost - available_balance;
                    println!();
                    println!(
                        "  {}: {} credits",
                        style("Shortfall").red().bold(),
                        style(format!("{:.2}", shortfall)).red()
                    );
                    println!(
                        "  {} Run `basilica fund` to add credits",
                        style("⚠").yellow()
                    );
                } else {
                    let remaining = available_balance - total_cost;
                    println!(
                        "  {}: {} credits",
                        style("Remaining After").dim(),
                        style(format!("{:.2}", remaining)).dim()
                    );
                }
            }
        }
    }

    println!();

    Ok(())
}
