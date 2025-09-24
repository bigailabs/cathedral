use basilica_sdk::BasilicaClient;
use color_eyre::{eyre::eyre, Help, Result as EyreResult};
use console::style;

use crate::{
    error::CliError,
    output::{json_output, print_info},
    progress::{complete_spinner_and_clear, complete_spinner_error, create_spinner},
};

/// Handle the main fund command - show deposit address
pub async fn handle_show_deposit_address(
    client: &BasilicaClient,
    json: bool,
) -> EyreResult<(), CliError> {
    let spinner = create_spinner("Fetching deposit account...");

    // Try to get existing deposit account first
    let account = match client.get_deposit_account().await {
        Ok(account) if account.exists => {
            complete_spinner_and_clear(spinner);
            account
        }
        Ok(_) | Err(_) => {
            // Account doesn't exist or error getting it, try to create one
            complete_spinner_and_clear(spinner.clone());

            let spinner = create_spinner("Creating deposit account...");
            match client.create_deposit_account().await {
                Ok(created) => {
                    complete_spinner_and_clear(spinner);
                    basilica_sdk::DepositAccountResponse {
                        user_id: created.user_id,
                        address: created.address,
                        exists: true,
                    }
                }
                Err(e) => {
                    complete_spinner_error(spinner, "Failed to create deposit account");
                    return Err(CliError::Internal(
                        eyre!(e)
                            .suggestion("Check your authentication and try again")
                            .note("Ensure you are logged in with 'basilica login'"),
                    ));
                }
            }
        }
    };

    if json {
        json_output(&account)?;
    } else {
        println!();
        println!("{}", style("Funding method: Bittensor (TAO)").bold());
        println!();
        println!("Send TAO to your unique deposit address:");
        println!(
            "  {}: {} (ss58, network=finney)",
            style("Address").cyan(),
            style(&account.address).green().bold()
        );
        println!("  {}: 0.1 TAO", style("Min amount").cyan());
        println!("  {}: 12", style("Confirmations required").cyan());
        println!();
        println!(
            "{}",
            style("Tip: you can send multiple transactions; we'll credit after 12 confs.")
                .dim()
                .italic()
        );
        println!();
        println!("Track status:");
        println!("  {}", style("basilica fund list").yellow());
    }

    Ok(())
}

/// Handle the fund list subcommand - show deposit history
pub async fn handle_list_deposits(
    client: &BasilicaClient,
    limit: u32,
    offset: u32,
    json: bool,
) -> EyreResult<(), CliError> {
    let spinner = create_spinner("Loading deposit history...");

    let deposits_response = client
        .list_deposits(Some(limit), Some(offset))
        .await
        .map_err(|e| {
            complete_spinner_error(spinner.clone(), "Failed to fetch deposits");
            CliError::Internal(
                eyre!(e)
                    .suggestion("Check your authentication and try again")
                    .note("If this persists, the payments service may be temporarily unavailable"),
            )
        })?;

    complete_spinner_and_clear(spinner);

    if json {
        json_output(&deposits_response)?;
    } else if deposits_response.deposits.is_empty() {
        print_info("No deposits found for your account");
        println!();
        println!("To fund your account, run:");
        println!("  {}", style("basilica fund").yellow());
    } else {
        display_deposits_table(&deposits_response)?;
    }

    Ok(())
}

fn display_deposits_table(
    response: &basilica_sdk::ListDepositsResponse,
) -> EyreResult<(), CliError> {
    use tabled::{builder::Builder, settings::Style};

    println!();
    println!("{}", style("# Shows historical deposits → credits").dim());
    println!();

    let mut builder = Builder::default();

    // Add header
    builder.push_record(["Date (UTC)", "TAO", "Tx Hash", "Conf", "Block", "Credited"]);

    let mut total_tao = 0.0;
    let mut total_credits = 0.0;

    for deposit in &response.deposits {
        let amount_tao: f64 = deposit.amount_tao.parse().unwrap_or(0.0);
        total_tao += amount_tao;

        // Format date
        let date = format_datetime(&deposit.observed_at);

        // Format tx hash (truncate to first 8 and last 3 chars)
        let tx_hash = if deposit.tx_hash.len() > 11 {
            format!(
                "{}...{}",
                &deposit.tx_hash[..8],
                &deposit.tx_hash[deposit.tx_hash.len() - 3..]
            )
        } else {
            deposit.tx_hash.clone()
        };

        // Format confirmations (12+ means finalized)
        let confirmations = if deposit.finalized_at.is_some() {
            "12+".to_string()
        } else {
            "-".to_string()
        };

        // Format credits (assuming 1 TAO = 1000 credits)
        let credited = if deposit.credited_at.is_some() {
            let credits = amount_tao * 1000.0;
            total_credits += credits;
            format!("{:.3}", credits)
        } else {
            "-".to_string()
        };

        builder.push_record([
            date.as_str(),
            &format!("{:.3}", amount_tao),
            tx_hash.as_str(),
            confirmations.as_str(),
            &deposit.block_number.to_string(),
            credited.as_str(),
        ]);
    }

    let mut table = builder.build();
    table.with(Style::blank());
    println!("{}", table);

    // Display totals
    println!();
    println!("{}:", style("Totals").bold());
    println!(
        "  {}: {} TAO",
        style("Deposits").cyan(),
        style(format!("{:.3}", total_tao)).green()
    );
    println!(
        "  {}: {}",
        style("Credits").cyan(),
        style(format!("{:.3}", total_credits)).green()
    );

    Ok(())
}

fn format_datetime(datetime_str: &str) -> String {
    // Parse and format the datetime string
    // Expected format: ISO 8601
    if let Ok(dt) = chrono::DateTime::parse_from_rfc3339(datetime_str) {
        dt.format("%Y-%m-%d %H:%M:%S").to_string()
    } else {
        // Fallback to the original string if parsing fails
        datetime_str.to_string()
    }
}
