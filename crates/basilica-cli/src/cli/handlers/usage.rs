use basilica_sdk::BasilicaClient;
use color_eyre::{eyre::eyre, Help, Result as EyreResult};

use crate::{
    error::CliError,
    output::{json_output, table_output},
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

    table_output::display_rental_usage_detail(&usage)?;

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

    table_output::display_usage_history(&history)?;

    Ok(())
}
