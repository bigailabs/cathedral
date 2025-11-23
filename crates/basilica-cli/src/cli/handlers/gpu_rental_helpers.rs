//! Common helper functions for GPU rental operations

use crate::progress::{complete_spinner_and_clear, complete_spinner_error, create_spinner};
use basilica_common::types::ComputeCategory;
use basilica_sdk::types::{ListRentalsQuery, RentalState};
use basilica_sdk::BasilicaClient;
use color_eyre::eyre::{eyre, Result};
use console::{style, Term};
use dialoguer::Select;

/// Resolve target rental ID - if not provided, fetch active rentals and prompt for selection
///
/// # Arguments
/// * `target` - Optional rental ID provided by user
/// * `api_client` - Authenticated API client
/// * `require_ssh` - If true, only show rentals with SSH access
pub async fn resolve_target_rental(
    target: Option<String>,
    api_client: &BasilicaClient,
    require_ssh: bool,
) -> Result<String> {
    if let Some(t) = target {
        return Ok(t);
    }

    let spinner = if require_ssh {
        create_spinner("Fetching rentals with SSH access...")
    } else {
        create_spinner("Fetching active rentals...")
    };

    // Fetch active rentals
    let query = Some(ListRentalsQuery {
        status: Some(RentalState::Active),
        gpu_type: None,
        min_gpu_count: None,
    });

    let rentals_list = api_client
        .list_rentals(query)
        .await
        .inspect_err(|_| complete_spinner_error(spinner.clone(), "Failed to load rentals"))?;

    complete_spinner_and_clear(spinner);

    // Filter for SSH-enabled rentals if required
    let eligible_rentals = if require_ssh {
        rentals_list
            .rentals
            .into_iter()
            .filter(|r| r.has_ssh)
            .collect()
    } else {
        rentals_list.rentals
    };

    if eligible_rentals.is_empty() {
        return if require_ssh {
            Err(
                eyre!("No rentals with SSH access found. SSH credentials are only available for rentals created in this session")
            )
        } else {
            Err(eyre!("No active rentals found"))
        };
    }

    // Use interactive selector to choose a rental (use compact mode for better readability)
    let selector = crate::interactive::InteractiveSelector::new();
    Ok(selector.select_rental(&eligible_rentals, false)?)
}

/// Unified rental item for selection across both compute types
#[derive(Clone)]
struct UnifiedRentalItem {
    rental_id: String,
    compute_type: ComputeCategory,
    provider_or_node: String,
    gpu_info: String,
    status: String,
    created_at: String,
}

/// Resolve target rental ID with unified selection across compute types
///
/// # Arguments
/// * `target` - Optional rental ID provided by user
/// * `compute_filter` - Optional compute category to filter rentals
/// * `api_client` - Authenticated API client
///
/// # Returns
/// Returns (rental_id, compute_category) tuple
pub async fn resolve_target_rental_unified(
    target: Option<String>,
    compute_filter: Option<ComputeCategory>,
    api_client: &BasilicaClient,
) -> Result<(String, ComputeCategory)> {
    // If target provided, determine type based on filter or default
    if let Some(t) = target {
        let compute_type = compute_filter.unwrap_or(ComputeCategory::SecureCloud);
        return Ok((t, compute_type));
    }

    let spinner = create_spinner("Fetching active rentals...");

    // Fetch rentals based on filter
    let (community_rentals, secure_rentals) = match compute_filter {
        Some(ComputeCategory::CommunityCloud) => {
            // Fetch only community cloud
            let query = Some(ListRentalsQuery {
                status: Some(RentalState::Active),
                gpu_type: None,
                min_gpu_count: None,
            });
            let rentals = api_client.list_rentals(query).await.inspect_err(|_| {
                complete_spinner_error(spinner.clone(), "Failed to load community cloud rentals")
            })?;
            (Some(rentals), None)
        }
        Some(ComputeCategory::SecureCloud) => {
            // Fetch only secure cloud
            let rentals = api_client
                .list_secure_cloud_rentals()
                .await
                .inspect_err(|_| {
                    complete_spinner_error(spinner.clone(), "Failed to load secure cloud rentals")
                })?;
            (None, Some(rentals))
        }
        None => {
            // Fetch both types in parallel
            let (community_result, secure_result) = tokio::join!(
                api_client.list_rentals(Some(ListRentalsQuery {
                    status: Some(RentalState::Active),
                    gpu_type: None,
                    min_gpu_count: None,
                })),
                api_client.list_secure_cloud_rentals()
            );

            let community = community_result.ok();
            let secure = secure_result.ok();

            (community, secure)
        }
    };

    complete_spinner_and_clear(spinner);

    // Build unified list
    let mut unified_items: Vec<UnifiedRentalItem> = Vec::new();

    // Add community cloud rentals
    if let Some(community) = community_rentals {
        for rental in community.rentals.iter() {
            let gpu_info = if rental.gpu_specs.is_empty() {
                "Unknown GPU".to_string()
            } else if rental.gpu_specs.len() == 1 {
                rental.gpu_specs[0].name.clone()
            } else {
                format!("{}x {}", rental.gpu_specs.len(), rental.gpu_specs[0].name)
            };

            unified_items.push(UnifiedRentalItem {
                rental_id: rental.rental_id.clone(),
                compute_type: ComputeCategory::CommunityCloud,
                provider_or_node: rental.node_id.clone(),
                gpu_info,
                status: format!("{:?}", rental.state),
                created_at: rental.created_at.clone(),
            });
        }
    }

    // Add secure cloud rentals (only active ones - where stopped_at is None)
    if let Some(secure) = secure_rentals {
        for rental in secure.rentals.iter() {
            // Skip stopped rentals
            if rental.stopped_at.is_some() {
                continue;
            }

            let gpu_info = if rental.gpu_count > 1 {
                format!("{}x {}", rental.gpu_count, rental.gpu_type.to_uppercase())
            } else {
                rental.gpu_type.to_uppercase()
            };

            unified_items.push(UnifiedRentalItem {
                rental_id: rental.rental_id.clone(),
                compute_type: ComputeCategory::SecureCloud,
                provider_or_node: rental.provider.clone(),
                gpu_info,
                status: rental.status.clone(),
                created_at: rental.created_at.to_rfc3339(),
            });
        }
    }

    if unified_items.is_empty() {
        return Err(eyre!("No active rentals found"));
    }

    // Format items for selection
    let items: Vec<String> = unified_items
        .iter()
        .map(|item| {
            let type_label = match item.compute_type {
                ComputeCategory::CommunityCloud => "Community",
                ComputeCategory::SecureCloud => "Secure   ",
            };

            format!(
                "{} | {:<20} | {:<25} | {:<12} | {}",
                style(type_label).cyan(),
                item.provider_or_node,
                item.gpu_info,
                item.status,
                item.created_at
            )
        })
        .collect();

    // Use dialoguer to select
    let theme = dialoguer::theme::ColorfulTheme::default();
    let selection = Select::with_theme(&theme)
        .with_prompt("Select rental to stop")
        .items(&items)
        .default(0)
        .interact_opt()
        .map_err(|e| eyre!("Selection failed: {}", e))?;

    let selection = match selection {
        Some(s) => s,
        None => return Err(eyre!("Selection cancelled")),
    };

    // Clear the selection prompt line
    let term = Term::stdout();
    let _ = term.clear_last_lines(1);

    let selected = &unified_items[selection];
    Ok((selected.rental_id.clone(), selected.compute_type))
}
