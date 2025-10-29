use basilica_sdk::BasilicaClient;
use color_eyre::{eyre::eyre, Help, Result as EyreResult};

use console::style;

use crate::{
    error::CliError,
    output::{format_credits, json_output},
    progress::{complete_spinner_and_clear, complete_spinner_error, create_spinner},
};

/// Handle the balance check command
pub async fn handle_check_balance(client: &BasilicaClient, json: bool) -> EyreResult<(), CliError> {
    let spinner = create_spinner("Fetching account balance...");

    let balance = client.get_balance().await.map_err(|e| {
        complete_spinner_error(spinner.clone(), "Failed to fetch balance");
        CliError::Internal(
            eyre!(e)
                .suggestion("Check your authentication and try again")
                .note("If this persists, the billing service may be temporarily unavailable"),
        )
    })?;

    complete_spinner_and_clear(spinner);

    if json {
        json_output(&balance)?;
    } else {
        display_balance(&balance);
    }

    Ok(())
}

fn display_balance(balance: &basilica_sdk::BalanceResponse) {
    println!("{}", style("Account Balance").bold());
    println!(
        "  {}: {} credits",
        style("Available").cyan(),
        style(format_credits(&balance.available)).green().bold()
    );
    println!(
        "  {}: {} credits",
        style("Total").cyan(),
        style(format_credits(&balance.total)).bold()
    );
    println!();
    println!(
        "{}: {}",
        style("Last Updated").dim(),
        style(&balance.last_updated).dim()
    );
}
