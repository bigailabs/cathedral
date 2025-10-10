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

/// Handle the usage history command
pub async fn handle_usage(
    client: &BasilicaClient,
    rental_id: Option<String>,
    limit: Option<u32>,
    offset: Option<u32>,
    json: bool,
) -> EyreResult<(), CliError> {
    if let Some(rental_id) = rental_id {
        handle_rental_usage_detail(client, &rental_id, json).await
    } else {
        handle_usage_history_list(client, limit, offset, json).await
    }
}

/// Handle detailed usage for a specific rental
async fn handle_rental_usage_detail(
    client: &BasilicaClient,
    rental_id: &str,
    json: bool,
) -> EyreResult<(), CliError> {
    let spinner = create_spinner("Fetching rental usage details...");

    let usage = client.get_rental_usage(rental_id).await.map_err(|e| {
        complete_spinner_error(spinner.clone(), "Failed to fetch rental usage");
        CliError::Internal(
            eyre!(e)
                .suggestion("Check that the rental ID is correct")
                .note("Run `basilica usage` to see all rentals"),
        )
    })?;

    complete_spinner_and_clear(spinner);

    if json {
        json_output(&usage)?;
        return Ok(());
    }

    display_rental_usage_detail(&usage)?;

    Ok(())
}

/// Handle usage history list
async fn handle_usage_history_list(
    client: &BasilicaClient,
    limit: Option<u32>,
    offset: Option<u32>,
    json: bool,
) -> EyreResult<(), CliError> {
    let spinner = create_spinner("Fetching usage history...");

    let history = client
        .list_usage_history(limit, offset)
        .await
        .map_err(|e| {
            complete_spinner_error(spinner.clone(), "Failed to fetch usage history");
            CliError::Internal(
                eyre!(e)
                    .suggestion("Check your authentication and try again")
                    .note("If this persists, the billing service may be temporarily unavailable"),
            )
        })?;

    complete_spinner_and_clear(spinner);

    if json {
        json_output(&history)?;
        return Ok(());
    }

    display_usage_history(&history)?;

    Ok(())
}

/// Display detailed usage for a specific rental
fn display_rental_usage_detail(
    usage: &basilica_sdk::types::RentalUsageResponse,
) -> EyreResult<(), CliError> {
    println!();
    println!(
        "{}: {}",
        style("Rental ID").cyan(),
        style(&usage.rental_id).bold()
    );
    println!(
        "{}: {}",
        style("Total Cost").cyan(),
        style(&usage.total_cost).green().bold()
    );
    println!();

    if let Some(summary) = &usage.summary {
        println!("{}", style("Resource Usage Summary").bold());
        println!();
        println!(
            "  {}: {:.1}%",
            style("Avg CPU Usage").cyan(),
            summary.avg_cpu_percent
        );
        println!(
            "  {}: {} MB",
            style("Avg Memory Usage").cyan(),
            summary.avg_memory_mb
        );
        println!(
            "  {}: {:.1}%",
            style("Avg GPU Utilization").cyan(),
            summary.avg_gpu_utilization
        );
        println!(
            "  {}: {} bytes",
            style("Total Network I/O").cyan(),
            summary.total_network_bytes
        );
        println!(
            "  {}: {} bytes",
            style("Total Disk I/O").cyan(),
            summary.total_disk_bytes
        );
        println!(
            "  {}: {} seconds ({:.1} hours)",
            style("Duration").cyan(),
            summary.duration_secs,
            summary.duration_secs as f64 / 3600.0
        );
        println!();
    }

    if !usage.data_points.is_empty() {
        #[derive(Tabled)]
        struct UsageDataRow {
            #[tabled(rename = "Timestamp")]
            timestamp: String,
            #[tabled(rename = "CPU %")]
            cpu_percent: String,
            #[tabled(rename = "Memory (MB)")]
            memory_mb: String,
            #[tabled(rename = "Cost")]
            cost: String,
        }

        let rows: Vec<UsageDataRow> = usage
            .data_points
            .iter()
            .map(|dp| UsageDataRow {
                timestamp: dp.timestamp.format("%Y-%m-%d %H:%M:%S UTC").to_string(),
                cpu_percent: format!("{:.1}%", dp.cpu_percent),
                memory_mb: dp.memory_mb.to_string(),
                cost: dp.cost.clone(),
            })
            .collect();

        println!("{}", style("Usage Data Points").bold());
        println!();
        println!("{}", Table::new(&rows));
        println!();
    } else {
        println!("{}", style("No usage data points available").yellow());
        println!();
    }

    Ok(())
}

/// Display usage history list
fn display_usage_history(
    history: &basilica_sdk::types::UsageHistoryResponse,
) -> EyreResult<(), CliError> {
    if history.rentals.is_empty() {
        println!();
        println!("{}", style("No rental usage history found").yellow());
        println!();
        return Ok(());
    }

    #[derive(Tabled)]
    struct UsageHistoryRow {
        #[tabled(rename = "Rental ID")]
        rental_id: String,
        #[tabled(rename = "Node ID")]
        node_id: String,
        #[tabled(rename = "Status")]
        status: String,
        #[tabled(rename = "Hourly Rate")]
        hourly_rate: String,
        #[tabled(rename = "Current Cost")]
        current_cost: String,
        #[tabled(rename = "Started")]
        started: String,
        #[tabled(rename = "Last Updated")]
        last_updated: String,
    }

    let mut rows: Vec<UsageHistoryRow> = history
        .rentals
        .iter()
        .map(|rental| {
            let hourly_rate = rental
                .hourly_rate
                .parse::<Decimal>()
                .ok()
                .map(|rate| format!("${:.2}/hr", rate))
                .unwrap_or_else(|| rental.hourly_rate.clone());

            let current_cost = rental
                .current_cost
                .parse::<Decimal>()
                .ok()
                .map(|cost| format!("${:.2}", cost))
                .unwrap_or_else(|| rental.current_cost.clone());

            UsageHistoryRow {
                rental_id: rental.rental_id.clone(),
                node_id: rental.node_id.clone(),
                status: rental.status.clone(),
                hourly_rate,
                current_cost,
                started: rental.start_time.format("%Y-%m-%d %H:%M UTC").to_string(),
                last_updated: rental.last_updated.format("%Y-%m-%d %H:%M UTC").to_string(),
            }
        })
        .collect();

    rows.sort_by(|a, b| b.started.cmp(&a.started));

    println!();
    println!(
        "{} ({} total)",
        style("Rental Usage History").bold(),
        style(history.total_count).cyan()
    );
    println!();
    println!("{}", Table::new(&rows));
    println!();

    let total_cost: Decimal = history
        .rentals
        .iter()
        .filter_map(|r| r.current_cost.parse::<Decimal>().ok())
        .sum();

    println!(
        "{}: {}",
        style("Total Cost (All Rentals)").cyan(),
        style(format!("${:.2}", total_cost)).green().bold()
    );
    println!();
    println!(
        "{} Run `basilica usage <rental-id>` to see detailed usage for a specific rental",
        style("Tip:").dim()
    );
    println!();

    Ok(())
}
