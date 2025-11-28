//! Common helper functions for GPU rental operations

use crate::progress::{complete_spinner_and_clear, complete_spinner_error, create_spinner};
use basilica_aggregator::models::GpuOffering;
use basilica_common::types::ComputeCategory;
use basilica_sdk::types::{ListAvailableNodesQuery, ListRentalsQuery, NodeSelection, RentalState};
use basilica_sdk::{ApiError, BasilicaClient};
use basilica_validator::api::types::AvailableNode;
use color_eyre::eyre::{eyre, Result};
use console::{style, Term};
use dialoguer::Select;
use rust_decimal::prelude::ToPrimitive;
use std::time::Duration;
use tokio::time::timeout;
use tracing::warn;

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
            // Fetch both types in parallel with 5-second timeout for community cloud
            let community_future = api_client.list_rentals(Some(ListRentalsQuery {
                status: Some(RentalState::Active),
                gpu_type: None,
                min_gpu_count: None,
            }));
            let (community_result, secure_result) = tokio::join!(
                async {
                    match timeout(Duration::from_secs(5), community_future).await {
                        Ok(result) => result,
                        Err(_) => {
                            warn!("Validator request timed out after 5 seconds");
                            Err(ApiError::Timeout)
                        }
                    }
                },
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

/// Represents a selected GPU offering from either cloud type
pub enum SelectedOffering {
    /// Secure cloud offering (from aggregator)
    SecureCloud(GpuOffering),
    /// Community cloud offering (node selection)
    CommunityCloud(NodeSelection),
}

/// Internal struct for unified offering display
#[derive(Clone)]
struct UnifiedOfferingItem {
    compute_type: ComputeCategory,
    display_gpu: String,
    display_provider: String,
    display_memory: String,
    display_price: String,
    // Original data for creating the offering
    secure_offering: Option<GpuOffering>,
    community_node: Option<AvailableNode>,
}

/// Resolve GPU offering with unified selection across compute types
///
/// When no target is specified, fetches available offerings from both clouds
/// and presents a unified selector for the user to choose from.
///
/// # Arguments
/// * `api_client` - Authenticated API client
/// * `gpu_filter` - Optional GPU type filter (e.g., "h100", "a100")
/// * `gpu_count_filter` - Optional GPU count filter
/// * `country_filter` - Optional country filter for location-based filtering
/// * `min_gpu_memory_filter` - Optional minimum GPU memory filter
///
/// # Returns
/// Returns `SelectedOffering` enum containing either secure or community cloud data
pub async fn resolve_offering_unified(
    api_client: &BasilicaClient,
    gpu_filter: Option<&str>,
    gpu_count_filter: Option<u32>,
    country_filter: Option<&str>,
    min_gpu_memory_filter: Option<u32>,
) -> Result<SelectedOffering> {
    let spinner = create_spinner("Fetching available GPUs from all clouds...");

    // Fetch offerings from both clouds in parallel with 5-second timeout for community cloud
    let community_future = api_client.list_available_nodes(Some(ListAvailableNodesQuery {
        available: Some(true),
        min_gpu_memory: min_gpu_memory_filter,
        gpu_type: gpu_filter.map(|s| s.to_string()),
        min_gpu_count: gpu_count_filter,
        location: country_filter.map(|c| basilica_common::LocationProfile {
            city: None,
            region: None,
            country: Some(c.to_string()),
        }),
    }));
    let (secure_result, community_result) =
        tokio::join!(api_client.list_secure_cloud_gpus(), async {
            match timeout(Duration::from_secs(5), community_future).await {
                Ok(result) => result,
                Err(_) => {
                    warn!("Validator request timed out after 5 seconds");
                    Err(ApiError::Timeout)
                }
            }
        });

    complete_spinner_and_clear(spinner);

    // Build unified list
    let mut unified_items: Vec<UnifiedOfferingItem> = Vec::new();

    // Add secure cloud offerings
    if let Ok(offerings) = secure_result {
        for offering in offerings {
            // Apply GPU type filter if specified
            if let Some(filter) = gpu_filter {
                if !offering
                    .gpu_type
                    .as_str()
                    .to_uppercase()
                    .contains(&filter.to_uppercase())
                {
                    continue;
                }
            }

            // Apply GPU count filter if specified
            if let Some(count) = gpu_count_filter {
                if offering.gpu_count != count {
                    continue;
                }
            }

            // Calculate total instance price (API already includes markup)
            let price_per_gpu = offering.hourly_rate_per_gpu.to_f64().unwrap_or(0.0);
            let total_price = price_per_gpu * (offering.gpu_count as f64);

            let memory_str = if let Some(mem_per_gpu) = offering.gpu_memory_gb_per_gpu {
                format!("{}GB", mem_per_gpu * offering.gpu_count)
            } else {
                "N/A".to_string()
            };

            unified_items.push(UnifiedOfferingItem {
                compute_type: ComputeCategory::SecureCloud,
                display_gpu: format!(
                    "{}x {}",
                    offering.gpu_count,
                    offering.gpu_type.as_str().to_uppercase()
                ),
                display_provider: format!("{}", offering.provider),
                display_memory: memory_str,
                display_price: format!("${:.2}/hr", total_price),
                secure_offering: Some(offering),
                community_node: None,
            });
        }
    }

    // Add community cloud offerings
    if let Ok(response) = community_result {
        for node in response.available_nodes {
            // Apply GPU count filter if specified (exact match for community)
            if let Some(count) = gpu_count_filter {
                if node.node.gpu_specs.len() as u32 != count {
                    continue;
                }
            }

            // Format GPU info
            let gpu_info = if node.node.gpu_specs.is_empty() {
                "Unknown GPU".to_string()
            } else {
                let gpu = &node.node.gpu_specs[0];
                if node.node.gpu_specs.len() > 1 {
                    format!("{}x {}", node.node.gpu_specs.len(), gpu.name)
                } else {
                    format!("1x {}", gpu.name)
                }
            };

            // Format memory
            let memory_str = if node.node.gpu_specs.is_empty() {
                "N/A".to_string()
            } else {
                let total_mem: u32 = node.node.gpu_specs.iter().map(|g| g.memory_gb).sum();
                format!("{}GB", total_mem)
            };

            // Format price (convert from cents to dollars)
            let price_str = if let Some(rate_cents) = node.node.hourly_rate_cents {
                format!("${:.2}/hr", rate_cents as f64 / 100.0)
            } else {
                "Market".to_string()
            };

            // Format provider/location
            let location = node
                .node
                .location
                .clone()
                .unwrap_or_else(|| "Unknown".to_string());

            unified_items.push(UnifiedOfferingItem {
                compute_type: ComputeCategory::CommunityCloud,
                display_gpu: gpu_info,
                display_provider: location,
                display_memory: memory_str,
                display_price: price_str,
                secure_offering: None,
                community_node: Some(node),
            });
        }
    }

    if unified_items.is_empty() {
        return Err(eyre!(
            "No GPU offerings available. Try different filters or check back later."
        ));
    }

    // Helper to truncate strings to fit column width (unicode-safe)
    fn truncate(s: &str, max_len: usize) -> String {
        let char_count = s.chars().count();
        if char_count <= max_len {
            s.to_string()
        } else {
            let truncated: String = s.chars().take(max_len - 1).collect();
            format!("{}…", truncated)
        }
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
                "{} │ {:<20} │ {:<20} │ {:<8} │ {}",
                style(type_label).cyan(),
                truncate(&item.display_gpu, 20),
                truncate(&item.display_provider, 20),
                item.display_memory,
                style(&item.display_price).green()
            )
        })
        .collect();

    // Show header hint
    println!(
        "{}",
        style("  Cloud     │ GPU                  │ Provider/Location    │ Memory   │ Price").dim()
    );

    // Use dialoguer to select
    let theme = dialoguer::theme::ColorfulTheme::default();
    let selection = Select::with_theme(&theme)
        .with_prompt("Select GPU offering")
        .items(&items)
        .default(0)
        .interact_opt()
        .map_err(|e| eyre!("Selection failed: {}", e))?;

    let selection = match selection {
        Some(s) => s,
        None => return Err(eyre!("Selection cancelled")),
    };

    // Clear the header and selection prompt lines
    let term = Term::stdout();
    let _ = term.clear_last_lines(2);

    let selected = &unified_items[selection];

    // Return appropriate offering type
    match selected.compute_type {
        ComputeCategory::SecureCloud => {
            let offering = selected
                .secure_offering
                .clone()
                .ok_or_else(|| eyre!("Internal error: secure cloud offering data missing"))?;
            Ok(SelectedOffering::SecureCloud(offering))
        }
        ComputeCategory::CommunityCloud => {
            let node = selected
                .community_node
                .clone()
                .ok_or_else(|| eyre!("Internal error: community cloud node data missing"))?;
            Ok(SelectedOffering::CommunityCloud(NodeSelection::NodeId {
                node_id: node.node.id,
            }))
        }
    }
}
