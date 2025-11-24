//! GPU rental command handlers

use crate::cli::commands::{ComputeCategoryArg, ListFilters, LogsOptions, PsFilters, UpOptions};
use crate::cli::handlers::gpu_rental_helpers::{
    resolve_offering_unified, resolve_target_rental_unified, SelectedOffering,
};
use crate::cli::handlers::ssh_keys::select_and_read_ssh_key;
use crate::client::create_authenticated_client;
use crate::config::CliConfig;
use crate::output::{
    compress_path, json_output, print_error, print_info, print_success, table_output,
};
use crate::progress::{complete_spinner_and_clear, complete_spinner_error, create_spinner};
use crate::ssh::{parse_ssh_credentials, SshClient};
use crate::CliError;
use basilica_common::types::{ComputeCategory, GpuCategory};
use basilica_common::utils::{parse_env_vars, parse_port_mappings};
use basilica_sdk::types::{
    GpuRequirements, ListAvailableNodesQuery, ListRentalsQuery, LocationProfile, NodeSelection,
    RentalState, ResourceRequirementsRequest, SshAccess, StartRentalApiRequest,
};
use basilica_sdk::ApiError;
use color_eyre::eyre::eyre;
use color_eyre::Section;
use console::style;
use reqwest::StatusCode;
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use std::collections::HashMap;
use std::fmt;
use std::path::PathBuf;
use std::str::FromStr;
use std::time::Duration;
use tracing::debug;
use uuid::Uuid;

/// Represents the target for the `up` command - either an node ID or GPU category
#[derive(Debug, Clone)]
pub enum TargetType {
    /// A specific node ID
    NodeId(String),
    /// A GPU category (h100, h200, b200, etc.)
    GpuCategory(GpuCategory),
}

/// Error type for TargetType parsing
#[derive(Debug, Clone)]
pub struct TargetTypeParseError {
    value: String,
}

impl fmt::Display for TargetTypeParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "'{}' is not a valid node ID (UUID) or GPU type (h100, b200, etc...)",
            self.value
        )
    }
}

impl std::error::Error for TargetTypeParseError {}

impl FromStr for TargetType {
    type Err = TargetTypeParseError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        // First check if it's a valid UUID v4 (node ID)
        if Uuid::parse_str(s).is_ok() {
            return Ok(TargetType::NodeId(s.to_string()));
        }

        // Then check if it's a known GPU type
        let gpu_category =
            GpuCategory::from_str(s).expect("GpuCategory::from_str returns Infallible");

        match gpu_category {
            GpuCategory::Other(_) => {
                // Not a valid UUID and not a known GPU type
                Err(TargetTypeParseError {
                    value: s.to_string(),
                })
            }
            _ => Ok(TargetType::GpuCategory(gpu_category)),
        }
    }
}

/// Helper function to fetch and filter secure cloud GPUs
async fn fetch_and_filter_secure_cloud(
    api_client: &basilica_sdk::BasilicaClient,
    gpu_category: Option<GpuCategory>,
    filters: &ListFilters,
) -> Result<Vec<basilica_aggregator::GpuOffering>, CliError> {
    let spinner = create_spinner("Fetching available GPUs...");
    let secure_result = api_client.list_secure_cloud_gpus().await;
    complete_spinner_and_clear(spinner);

    let gpus = secure_result.map_err(|e| -> CliError {
        CliError::Internal(
            eyre!(e)
                .suggestion("Check your internet connection and try again")
                .note("If this persists, GPUs may be temporarily unavailable"),
        )
    })?;

    // Apply filters
    let mut filtered_gpus: Vec<_> = gpus
        .into_iter()
        .filter(|gpu| {
            // Filter by GPU type if specified
            if let Some(ref category) = gpu_category {
                let category_str = category.as_str().to_uppercase();
                if !gpu.gpu_type.as_str().to_uppercase().contains(&category_str) {
                    return false;
                }
            }

            // Filter by availability
            if !gpu.availability {
                return false;
            }

            // Filter by GPU count
            if let Some(min_count) = filters.gpu_min {
                if gpu.gpu_count < min_count {
                    return false;
                }
            }

            // Filter by country
            if let Some(ref country) = filters.country {
                if !gpu.region.to_lowercase().contains(&country.to_lowercase()) {
                    return false;
                }
            }

            true
        })
        .collect();

    // Sort by total price (ascending) - per-GPU rate × gpu_count
    filtered_gpus.sort_by(|a, b| {
        let a_total = a.hourly_rate_per_gpu * rust_decimal::Decimal::from(a.gpu_count);
        let b_total = b.hourly_rate_per_gpu * rust_decimal::Decimal::from(b.gpu_count);
        a_total.partial_cmp(&b_total).unwrap()
    });

    Ok(filtered_gpus)
}

/// Helper function to fetch and filter community cloud nodes
async fn fetch_and_filter_community_cloud(
    api_client: &basilica_sdk::BasilicaClient,
    gpu_category: Option<GpuCategory>,
    filters: &ListFilters,
) -> Result<(Vec<basilica_sdk::AvailableNode>, HashMap<String, String>), CliError> {
    // Convert GPU category to string if provided
    let gpu_type = gpu_category.map(|gc| gc.as_str());

    // Build query from filters
    let query = ListAvailableNodesQuery {
        available: Some(true), // Filter for available nodes only
        min_gpu_memory: filters.memory_min,
        gpu_type,
        min_gpu_count: Some(filters.gpu_min.unwrap_or(0)),
        location: filters.country.as_ref().map(|country| LocationProfile {
            city: None,
            region: None,
            country: Some(country.clone()),
        }),
    };

    let spinner = create_spinner("Fetching available GPUs...");

    // Fetch available nodes
    let response = api_client
        .list_available_nodes(Some(query))
        .await
        .map_err(|e| -> CliError {
            complete_spinner_error(spinner.clone(), "Failed to fetch nodes");
            CliError::Internal(
                eyre!(e)
                    .suggestion("Check your internet connection and try again")
                    .note("If this persists, nodes may be temporarily unavailable"),
            )
        })?;

    complete_spinner_and_clear(spinner);

    // Build pricing map from nodes' hourly_rate_cents field
    // Map GPU type -> hourly rate string (e.g., "h100" -> "2.50")
    let pricing_map: HashMap<String, String> = response
        .available_nodes
        .iter()
        .filter_map(|node| {
            // Get the first GPU spec to determine GPU type
            let gpu_spec = node.node.gpu_specs.first()?;
            let category = GpuCategory::from_str(&gpu_spec.name).ok()?;
            let gpu_type = category.to_string().to_lowercase();

            // Get pricing from the node's hourly_rate_cents field
            let cents = node.node.hourly_rate_cents? as f64;
            let dollars = cents / 100.0;
            let rate_string = format!("{:.2}", dollars);

            Some((gpu_type, rate_string))
        })
        .collect();

    Ok((response.available_nodes, pricing_map))
}

/// Helper function to display secure cloud GPUs
fn display_secure_cloud_table(
    gpus: &[basilica_aggregator::GpuOffering],
    filters: &ListFilters,
) -> Result<(), CliError> {
    if gpus.is_empty() {
        print_info("No GPUs available matching your criteria");
        return Ok(());
    }

    if filters.compact {
        table_output::display_secure_cloud_offerings_compact(gpus)?;
    } else {
        table_output::display_secure_cloud_offerings_detailed(
            gpus,
            filters.detailed, // show_ids
        )?;
    }

    Ok(())
}

/// Helper function to display community cloud nodes
fn display_community_cloud_table(
    nodes: &[basilica_sdk::AvailableNode],
    pricing_map: &HashMap<String, String>,
    filters: &ListFilters,
) -> Result<(), CliError> {
    if nodes.is_empty() {
        print_info("No GPUs available matching your criteria");
        return Ok(());
    }

    if filters.compact {
        table_output::display_available_nodes_compact(nodes, pricing_map)?;
    } else {
        table_output::display_available_nodes_detailed(
            nodes,
            true,             // show_full_gpu_names
            filters.detailed, // show_ids
            pricing_map,
        )?;
    }

    Ok(())
}

/// Handle the `ls` command - list available nodes for rental
pub async fn handle_ls(
    gpu_category: Option<GpuCategory>,
    filters: ListFilters,
    compute: Option<ComputeCategoryArg>,
    json: bool,
    config: &CliConfig,
) -> Result<(), CliError> {
    let api_client = create_authenticated_client(config).await?;

    // Convert compute arg to compute category
    let compute_category = compute.map(|c| match c {
        ComputeCategoryArg::SecureCloud => ComputeCategory::SecureCloud,
        ComputeCategoryArg::CommunityCloud => ComputeCategory::CommunityCloud,
    });

    // Branch based on compute type
    match compute_category {
        Some(ComputeCategory::SecureCloud) => {
            // Fetch and filter secure cloud GPUs
            let filtered_gpus =
                fetch_and_filter_secure_cloud(&api_client, gpu_category, &filters).await?;

            if json {
                json_output(&filtered_gpus)?;
            } else {
                display_secure_cloud_table(&filtered_gpus, &filters)?;
            }
        }
        Some(ComputeCategory::CommunityCloud) => {
            // Fetch and filter community cloud nodes
            let (nodes, pricing_map) =
                fetch_and_filter_community_cloud(&api_client, gpu_category, &filters).await?;

            if json {
                // Create a simple response structure for JSON output
                #[derive(serde::Serialize)]
                struct NodesResponse<'a> {
                    available_nodes: &'a [basilica_sdk::AvailableNode],
                }
                let response = NodesResponse {
                    available_nodes: &nodes,
                };
                json_output(&response)?;
            } else {
                display_community_cloud_table(&nodes, &pricing_map, &filters)?;
            }
        }
        None => {
            // Display both tables when --compute flag is not specified

            // Fetch both in parallel
            let (secure_result, community_result) = tokio::join!(
                fetch_and_filter_secure_cloud(&api_client, gpu_category.clone(), &filters),
                fetch_and_filter_community_cloud(&api_client, gpu_category, &filters)
            );

            let secure_gpus = secure_result?;
            let (community_nodes, pricing_map) = community_result?;

            if json {
                // Create combined JSON output
                #[derive(serde::Serialize)]
                struct CombinedResponse<'a> {
                    secure_cloud: &'a [basilica_aggregator::GpuOffering],
                    community_cloud: &'a [basilica_sdk::AvailableNode],
                }
                let response = CombinedResponse {
                    secure_cloud: &secure_gpus,
                    community_cloud: &community_nodes,
                };
                json_output(&response)?;
            } else {
                // Display community cloud table
                println!("\n{}", style("Community Cloud GPUs").bold());
                display_community_cloud_table(&community_nodes, &pricing_map, &filters)?;

                // Separator
                println!();

                // Display secure cloud table
                println!("{}", style("Secure Cloud GPUs").bold());
                display_secure_cloud_table(&secure_gpus, &filters)?;
            }
        }
    }

    Ok(())
}

/// Ensure user has SSH key registered with Basilica API
///
/// Checks if user already has a key registered. If not, discovers SSH keys
/// in ~/.ssh, lets user select one interactively, and registers it.
/// Returns the SSH key ID if successful.
pub async fn ensure_ssh_key_registered(
    api_client: &basilica_sdk::BasilicaClient,
) -> Result<String, CliError> {
    // Check if user already has SSH key registered
    if let Some(key) = api_client.get_user_ssh_key().await? {
        return Ok(key.id);
    }

    // No key registered - run interactive selection flow
    print_info("No SSH key registered.");

    // Use shared key discovery and selection logic
    let selected_key = select_and_read_ssh_key().await?;

    // Prompt user for confirmation
    use dialoguer::Confirm;

    let filename = selected_key
        .path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("key");

    let confirmed = Confirm::new()
        .with_prompt(format!("Register {}?", filename))
        .default(true)
        .interact()
        .map_err(|e| CliError::Internal(eyre!(e).wrap_err("Failed to show confirmation prompt")))?;

    if !confirmed {
        return Err(CliError::Internal(eyre!(
            "SSH key required for GPU rentals"
        )));
    }

    // Register key
    let spinner = create_spinner("Registering SSH key...");
    let result = api_client
        .register_ssh_key("default", selected_key.content.trim())
        .await;

    match result {
        Ok(key) => {
            complete_spinner_and_clear(spinner);
            print_success("Successfully registered SSH key");
            Ok(key.id)
        }
        Err(e) => {
            complete_spinner_error(spinner, "Failed to register SSH key");
            Err(CliError::Api(e))
        }
    }
}

/// Interactive selector for GPU offerings
///
/// Displays formatted table and lets user choose with arrow keys.
/// Returns the selected offering.
fn interactive_offering_selector(
    offerings: &[basilica_aggregator::models::GpuOffering],
) -> Result<basilica_aggregator::models::GpuOffering, CliError> {
    use dialoguer::Select;

    if offerings.is_empty() {
        return Err(CliError::Internal(eyre!(
            "No GPU offerings available matching your criteria"
        )));
    }

    // Get markup percentage (using 15% as per design decision)
    let markup_percent = 15.0;

    // Format offerings for display
    let options: Vec<String> = offerings
        .iter()
        .map(|o| {
            // Calculate total instance price (per-GPU rate × gpu_count) with markup
            let base_price_per_gpu = o.hourly_rate_per_gpu.to_f64().unwrap_or(0.0);
            let total_price =
                base_price_per_gpu * (o.gpu_count as f64) * (1.0 + markup_percent / 100.0);

            let memory_str = if let Some(mem_per_gpu) = o.gpu_memory_gb_per_gpu {
                format!("{}GB", mem_per_gpu * o.gpu_count)
            } else {
                "N/A".to_string()
            };

            format!(
                "{}x {} │ {} │ {} │ ${:.2}/hr",
                o.gpu_count,
                o.gpu_type.as_str(),
                o.region,
                memory_str,
                total_price
            )
        })
        .collect();

    // Interactive selection
    let selection = Select::new()
        .with_prompt("Select GPU offering")
        .items(&options)
        .default(0)
        .interact()
        .map_err(|e| {
            CliError::Internal(eyre!(e).wrap_err("Failed to display offering selector"))
        })?;

    Ok(offerings[selection].clone())
}

/// Handle secure cloud rental with a pre-selected offering (from unified selector)
async fn handle_secure_cloud_rental_with_offering(
    api_client: basilica_sdk::BasilicaClient,
    offering: basilica_aggregator::models::GpuOffering,
    options: UpOptions,
    config: &CliConfig,
) -> Result<(), CliError> {
    // Get SSH key ID (SSH key registration already done in handle_up)
    let ssh_key_id = api_client
        .get_ssh_key()
        .await
        .map_err(|e| CliError::Internal(eyre!(e)))?
        .ok_or_else(|| {
            CliError::Internal(
                eyre!("No SSH key registered with Basilica")
                    .suggestion("Run 'basilica ssh-keys add' to register your SSH key"),
            )
        })?
        .id;

    // Start rental
    let spinner = create_spinner("Starting rental...");

    use basilica_sdk::types::{PortMappingRequest, StartSecureCloudRentalRequest};

    // Parse port mappings if provided
    let ports: Vec<PortMappingRequest> = if !options.ports.is_empty() {
        basilica_common::utils::parse_port_mappings(&options.ports)
            .map_err(|e| {
                complete_spinner_error(spinner.clone(), "Invalid port mapping");
                CliError::Internal(eyre!(e).wrap_err("Failed to parse port mappings"))
            })?
            .into_iter()
            .map(|pm| PortMappingRequest {
                container_port: pm.container_port,
                host_port: pm.host_port,
                protocol: pm.protocol,
            })
            .collect()
    } else {
        Vec::new()
    };

    // Parse environment variables if provided
    let environment = if !options.env.is_empty() {
        basilica_common::utils::parse_env_vars(&options.env).map_err(|e| {
            complete_spinner_error(spinner.clone(), "Invalid environment variables");
            CliError::Internal(eyre!(e).wrap_err("Failed to parse environment variables"))
        })?
    } else {
        HashMap::new()
    };

    let request = StartSecureCloudRentalRequest {
        offering_id: offering.id.clone(),
        ssh_public_key_id: ssh_key_id,
        container_image: options.image.clone(),
        environment,
        ports,
    };

    let response = api_client
        .start_secure_cloud_rental(request)
        .await
        .map_err(|e| {
            complete_spinner_error(spinner.clone(), "Failed to start rental");
            CliError::Api(e)
        })?;
    complete_spinner_and_clear(spinner);

    print_success(&format!(
        "Successfully started secure cloud rental {}",
        response.rental_id
    ));

    // Handle SSH based on options
    if options.no_ssh {
        return Ok(());
    }

    if options.detach {
        if let Some(ssh_cmd) = &response.ssh_command {
            display_secure_cloud_reconnection_instructions(
                &response.rental_id,
                ssh_cmd,
                config,
                "To connect to this rental:",
            )?;
        } else {
            println!();
            print_info("Instance is starting up. Use 'basilica ps' to check status.");
            print_info(&format!("SSH with: basilica ssh {}", response.rental_id));
        }
        return Ok(());
    }

    // Wait for rental to become active
    print_info("Waiting for rental to become active...");
    let rental = poll_secure_cloud_rental_status(&response.rental_id, &api_client).await?;

    if let Some(rental) = rental {
        if let Some(ssh_cmd) = &rental.ssh_command {
            print_info("Connecting to rental...");
            let (host, port, username) = parse_ssh_credentials(ssh_cmd)?;
            let ssh_access = SshAccess {
                host,
                port,
                username,
            };

            let private_key_path = {
                let ssh_key = api_client
                    .get_ssh_key()
                    .await
                    .map_err(|e| CliError::Internal(eyre!(e)))?
                    .ok_or_else(|| {
                        CliError::Internal(
                            eyre!("No SSH key registered with Basilica")
                                .suggestion("Run 'basilica ssh-keys add' to register your SSH key"),
                        )
                    })?;

                crate::ssh::find_private_key_for_public_key(&ssh_key.public_key)
                    .map_err(CliError::Internal)?
            };

            let ssh_client = SshClient::new(&config.ssh)?;
            match retry_ssh_connection(
                &ssh_client,
                &ssh_access,
                Some(private_key_path),
                Duration::from_secs(120),
            )
            .await
            {
                Ok(_) => {
                    print_info("SSH session closed");
                    display_secure_cloud_reconnection_instructions(
                        &response.rental_id,
                        ssh_cmd,
                        config,
                        "To reconnect to this rental:",
                    )?;
                }
                Err(e) => {
                    print_error(&format!("SSH connection failed: {}", e));
                    display_secure_cloud_reconnection_instructions(
                        &response.rental_id,
                        ssh_cmd,
                        config,
                        "Try manually connecting using:",
                    )?;
                }
            }
        } else {
            println!();
            print_info("Rental is active but SSH is not yet available");
            print_info(&format!("SSH with: basilica ssh {}", response.rental_id));
        }
    } else {
        println!();
        print_info("Rental is taking longer than expected to become active");
        print_info("Check status with: basilica ps");
        print_info(&format!("SSH with: basilica ssh {}", response.rental_id));
    }

    Ok(())
}

/// Handle community cloud rental with a pre-selected node (from unified selector)
async fn handle_community_cloud_rental_with_selection(
    api_client: basilica_sdk::BasilicaClient,
    node_selection: NodeSelection,
    options: UpOptions,
    config: &CliConfig,
) -> Result<(), CliError> {
    let spinner = create_spinner("Preparing rental request...");

    // Build rental request
    let container_image = options.image.unwrap_or_else(|| config.image.name.clone());

    let env_vars = parse_env_vars(&options.env)
        .map_err(|e| eyre!("Invalid argument: {}", e.to_string()))
        .inspect_err(|_e| {
            complete_spinner_error(spinner.clone(), "Environment variable parsing failed");
        })?;

    let port_mappings: Vec<basilica_sdk::types::PortMappingRequest> =
        parse_port_mappings(&options.ports)
            .map_err(|e| eyre!("Invalid argument: {}", e.to_string()))
            .inspect_err(|_e| {
                complete_spinner_error(spinner.clone(), "Port mapping parsing failed");
            })?
            .into_iter()
            .map(Into::into)
            .collect();

    let command = if options.command.is_empty() {
        vec!["/bin/bash".to_string()]
    } else {
        options.command
    };

    let request = StartRentalApiRequest {
        node_selection,
        container_image,
        environment: env_vars,
        ports: port_mappings,
        resources: ResourceRequirementsRequest {
            cpu_cores: options.cpu_cores.unwrap_or(0.0),
            memory_mb: options.memory_mb.unwrap_or(0),
            storage_mb: options.storage_mb.unwrap_or(0),
            gpu_count: options.gpu_count.unwrap_or(0),
            gpu_types: vec![],
        },
        command,
        volumes: vec![],
        no_ssh: options.no_ssh,
    };

    spinner.set_message("Creating rental...");
    let response = api_client.start_rental(request).await.map_err(|e| {
        complete_spinner_error(spinner.clone(), "Failed to create rental");
        CliError::Internal(
            eyre!(e)
                .note("The selected node is experiencing issues.")
                .suggestion("Try running 'basilica up' again to select a different node."),
        )
    })?;

    complete_spinner_and_clear(spinner);

    print_success(&format!(
        "Successfully started community cloud rental {}",
        response.rental_id
    ));

    // Handle SSH based on options
    if options.no_ssh {
        return Ok(());
    }

    let ssh_creds = match response.ssh_credentials {
        Some(ref creds) => creds,
        None => {
            print_info("SSH access not available (unexpected error)");
            return Ok(());
        }
    };

    if options.detach {
        display_ssh_connection_instructions(
            &response.rental_id,
            ssh_creds,
            config,
            "SSH connection options:",
        )?;
    } else {
        print_info("Waiting for rental to become active...");
        let rental_active = poll_rental_status(&response.rental_id, &api_client).await?;

        if rental_active {
            print_info("Connecting to rental...");
            let (host, port, username) = parse_ssh_credentials(ssh_creds)?;
            let ssh_access = SshAccess {
                host,
                port,
                username,
            };

            let private_key_path = {
                let ssh_key = api_client
                    .get_ssh_key()
                    .await
                    .map_err(|e| CliError::Internal(eyre!(e)))?
                    .ok_or_else(|| {
                        CliError::Internal(
                            eyre!("No SSH key registered with Basilica")
                                .suggestion("Run 'basilica ssh-keys add' to register your SSH key"),
                        )
                    })?;

                crate::ssh::find_private_key_for_public_key(&ssh_key.public_key)
                    .map_err(CliError::Internal)?
            };

            let ssh_client = SshClient::new(&config.ssh)?;
            match retry_ssh_connection(
                &ssh_client,
                &ssh_access,
                Some(private_key_path),
                Duration::from_secs(120),
            )
            .await
            {
                Ok(_) => {
                    print_info("SSH session closed");
                    display_ssh_connection_instructions(
                        &response.rental_id,
                        ssh_creds,
                        config,
                        "To reconnect to this rental:",
                    )?;
                }
                Err(e) => {
                    print_error(&format!("SSH connection failed: {}", e));
                    display_ssh_connection_instructions(
                        &response.rental_id,
                        ssh_creds,
                        config,
                        "Try manually connecting using:",
                    )?;
                }
            }
        } else {
            println!();
            print_info("Rental is taking longer than expected to become active");
            print_info("Check status with: basilica ps");
            print_info(&format!("SSH with: basilica ssh {}", response.rental_id));
        }
    }

    Ok(())
}

/// Handle secure cloud rental workflow
async fn handle_secure_cloud_rental(
    api_client: basilica_sdk::BasilicaClient,
    target: Option<TargetType>,
    options: UpOptions,
    config: &CliConfig,
) -> Result<(), CliError> {
    // Step 1: Ensure SSH key registered
    ensure_ssh_key_registered(&api_client).await?;

    // Step 2: List offerings
    let spinner = create_spinner("Fetching available GPUs...");
    let offerings = api_client.list_secure_cloud_gpus().await.map_err(|e| {
        complete_spinner_error(spinner.clone(), "Failed to fetch GPU offerings");
        CliError::Api(e)
    })?;
    complete_spinner_and_clear(spinner);

    // Step 3: Filter offerings if target specified
    let filtered_offerings: Vec<_> = if let Some(target_type) = target {
        match target_type {
            TargetType::GpuCategory(category) => {
                // Filter by GPU type
                offerings
                    .into_iter()
                    .filter(|o| {
                        let category_str = category.as_str().to_uppercase();
                        o.gpu_type.as_str().to_uppercase().contains(&category_str)
                    })
                    .collect()
            }
            TargetType::NodeId(_) => {
                return Err(CliError::Internal(eyre!(
                    "Node ID selection not supported for secure cloud. \
                     Use GPU category or interactive selector."
                )));
            }
        }
    } else {
        offerings
    };

    if filtered_offerings.is_empty() {
        print_info("No GPU offerings available matching your criteria");
        return Ok(());
    }

    // Step 4: Interactive selector
    let selected = interactive_offering_selector(&filtered_offerings)?;

    // Step 5: Delegate to shared handler
    handle_secure_cloud_rental_with_offering(api_client, selected, options, config).await
}

/// Handle the `up` command - provision GPU instances
pub async fn handle_up(
    target: Option<TargetType>,
    options: UpOptions,
    compute: Option<ComputeCategoryArg>,
    config: &CliConfig,
) -> Result<(), CliError> {
    let api_client = create_authenticated_client(config).await?;

    // If no compute flag specified AND no target specified, use unified selection from both clouds
    if compute.is_none() && target.is_none() {
        // Ensure SSH key is registered before proceeding (needed for both clouds)
        ensure_ssh_key_registered(&api_client).await?;

        // Use unified offering resolver to select from both clouds
        let selected = resolve_offering_unified(&api_client, None, options.gpu_count)
            .await
            .map_err(CliError::Internal)?;

        match selected {
            SelectedOffering::SecureCloud(offering) => {
                // Start secure cloud rental with selected offering
                return handle_secure_cloud_rental_with_offering(
                    api_client, offering, options, config,
                )
                .await;
            }
            SelectedOffering::CommunityCloud(node_selection) => {
                // Start community cloud rental with selected node
                return handle_community_cloud_rental_with_selection(
                    api_client,
                    node_selection,
                    options,
                    config,
                )
                .await;
            }
        }
    }

    // Determine compute category (default to secure cloud when --compute flag is used)
    let compute_category = compute
        .map(|c| match c {
            ComputeCategoryArg::SecureCloud => ComputeCategory::SecureCloud,
            ComputeCategoryArg::CommunityCloud => ComputeCategory::CommunityCloud,
        })
        .unwrap_or(ComputeCategory::SecureCloud);

    // Branch based on compute type
    match compute_category {
        ComputeCategory::SecureCloud => {
            return handle_secure_cloud_rental(api_client, target, options, config).await;
        }
        ComputeCategory::CommunityCloud => {
            // Fall through to existing community cloud implementation
        }
    }

    // Ensure SSH key is registered before proceeding
    ensure_ssh_key_registered(&api_client).await?;

    // Parse the target to determine node selection strategy
    let node_selection = if let Some(target_type) = target {
        match target_type {
            TargetType::NodeId(node_id) => {
                // Direct node ID provided
                NodeSelection::NodeId { node_id }
            }
            TargetType::GpuCategory(gpu_category) => {
                // GPU category specified - use automatic selection with exact matching
                let spinner =
                    create_spinner(&format!("Fetching available {} GPUs...", gpu_category));
                complete_spinner_and_clear(spinner);

                NodeSelection::ExactGpuConfiguration {
                    gpu_requirements: GpuRequirements {
                        min_memory_gb: 0, // Default, no minimum memory requirement
                        gpu_type: Some(gpu_category.as_str()),
                        gpu_count: options.gpu_count.unwrap_or(0),
                    },
                }
            }
        }
    } else {
        // No target specified - use interactive selection
        let spinner = create_spinner("Fetching available GPUs...");

        // Build query from options
        let query = ListAvailableNodesQuery {
            available: Some(true),
            min_gpu_memory: None,
            gpu_type: None,
            min_gpu_count: options.gpu_count,
            location: options.country.as_ref().map(|country| LocationProfile {
                city: None,
                region: None,
                country: Some(country.clone()),
            }),
        };

        let response = api_client.list_available_nodes(Some(query)).await.map_err(
            |e| -> crate::error::CliError {
                complete_spinner_error(spinner.clone(), "Failed to fetch nodes");
                eyre!("API request failed for list available nodes: {}", e).into()
            },
        )?;

        complete_spinner_and_clear(spinner);

        // Use interactive selector to choose an node
        // Compact mode uses grouped selector, otherwise use detailed selector
        let selector = crate::interactive::InteractiveSelector::new();
        let use_detailed = !options.compact;
        selector.select_node(
            &response.available_nodes,
            use_detailed,
            options.detailed,
            options.gpu_count,
        )?
    };

    let spinner = create_spinner("Preparing rental request...");

    // Build rental request
    let container_image = options.image.unwrap_or_else(|| config.image.name.clone());

    let env_vars = parse_env_vars(&options.env)
        .map_err(|e| eyre!("Invalid argument: {}", e.to_string()))
        .inspect_err(|_e| {
            complete_spinner_error(spinner.clone(), "Environment variable parsing failed");
        })?;

    // Parse port mappings if provided
    let port_mappings: Vec<basilica_sdk::types::PortMappingRequest> =
        parse_port_mappings(&options.ports)
            .map_err(|e| eyre!("Invalid argument: {}", e.to_string()))
            .inspect_err(|_e| {
                complete_spinner_error(spinner.clone(), "Port mapping parsing failed");
            })?
            .into_iter()
            .map(Into::into)
            .collect();

    let command = if options.command.is_empty() {
        vec!["/bin/bash".to_string()]
    } else {
        options.command
    };

    // Determine the selection mode for error messaging
    let is_direct_node_id = matches!(node_selection, NodeSelection::NodeId { .. });

    let request = StartRentalApiRequest {
        node_selection,
        container_image,
        environment: env_vars,
        ports: port_mappings,
        resources: ResourceRequirementsRequest {
            cpu_cores: options.cpu_cores.unwrap_or(0.0),
            memory_mb: options.memory_mb.unwrap_or(0),
            storage_mb: options.storage_mb.unwrap_or(0),
            gpu_count: options.gpu_count.unwrap_or(0),
            gpu_types: vec![],
        },
        command,
        volumes: vec![],
        no_ssh: options.no_ssh,
    };

    spinner.set_message("Creating rental...");
    let response = api_client
        .start_rental(request)
        .await
        .map_err(|e| -> CliError {
            complete_spinner_error(spinner.clone(), "Failed to create rental");
            CliError::Internal(
                eyre!(e)
                    .note("The selected node is experiencing issues.")
                    .with_suggestion(|| {
                        if is_direct_node_id {
                            "Try using a different node ID (e.g., 'basilica up <different-node-id>')."
                        } else {
                            "Simply rerun the same command to automatically try a different node."
                        }
                    })
            )
        })?;

    complete_spinner_and_clear(spinner);

    print_success(&format!(
        "Successfully started community cloud rental {}",
        response.rental_id
    ));

    // Handle SSH based on options
    if options.no_ssh {
        // SSH disabled entirely, nothing to do
        return Ok(());
    }

    // Check if we have SSH credentials
    let ssh_creds = match response.ssh_credentials {
        Some(ref creds) => creds,
        None => {
            print_info("SSH access not available (unexpected error)");
            return Ok(());
        }
    };

    if options.detach {
        // Detached mode: just show instructions and exit
        display_ssh_connection_instructions(
            &response.rental_id,
            ssh_creds,
            config,
            "SSH connection options:",
        )?;
    } else {
        // Auto-SSH mode: wait for rental to be active and connect
        print_info("Waiting for rental to become active...");

        // Poll for rental to become active
        let rental_active = poll_rental_status(&response.rental_id, &api_client).await?;

        if rental_active {
            // Parse SSH credentials and connect
            print_info("Connecting to rental...");
            let (host, port, username) = parse_ssh_credentials(ssh_creds)?;
            let ssh_access = SshAccess {
                host,
                port,
                username,
            };

            // Fetch API-registered SSH key and find matching private key
            let private_key_path = {
                let ssh_key = api_client
                    .get_ssh_key()
                    .await
                    .map_err(|e| CliError::Internal(eyre!(e)))?
                    .ok_or_else(|| {
                        CliError::Internal(
                            eyre!("No SSH key registered with Basilica")
                                .suggestion("Run 'basilica ssh-keys add' to register your SSH key")
                                .note("SSH keys are required to connect to rentals"),
                        )
                    })?;

                crate::ssh::find_private_key_for_public_key(&ssh_key.public_key)
                    .map_err(CliError::Internal)?
            };

            debug!(
                "Using private key for community cloud auto-SSH: {}",
                private_key_path.display()
            );

            // Use SSH client to open interactive session
            let ssh_client = SshClient::new(&config.ssh)?;
            match ssh_client
                .interactive_session(&ssh_access, Some(private_key_path))
                .await
            {
                Ok(_) => {
                    // SSH session ended normally
                    print_info("SSH session closed");
                    display_ssh_connection_instructions(
                        &response.rental_id,
                        ssh_creds,
                        config,
                        "To reconnect to this rental:",
                    )?;
                }
                Err(e) => {
                    print_error(&format!("SSH connection failed: {}", e));
                    display_ssh_connection_instructions(
                        &response.rental_id,
                        ssh_creds,
                        config,
                        "Try manually connecting using:",
                    )?;
                }
            }
        } else {
            // Timeout or error - show manual instructions
            print_info("Rental is taking longer than expected to become active");
            display_ssh_connection_instructions(
                &response.rental_id,
                ssh_creds,
                config,
                "You can manually connect once it's ready using:",
            )?
        }
    }

    Ok(())
}

/// Handle the `ps` command - list active rentals
pub async fn handle_ps(
    filters: PsFilters,
    compute: Option<ComputeCategoryArg>,
    json: bool,
    config: &CliConfig,
) -> Result<(), CliError> {
    use basilica_common::types::ComputeCategory;

    // Convert to Option<ComputeCategory> - None means show both
    let compute_category = compute.map(ComputeCategory::from);

    let api_client = create_authenticated_client(config).await?;

    // Branch based on compute category
    match compute_category {
        Some(ComputeCategory::CommunityCloud) => {
            // Existing community cloud logic
            let spinner = if filters.history {
                create_spinner("Fetching rental history...")
            } else {
                create_spinner("Fetching active rentals...")
            };

            // Build query from filters - default to "active" if no status specified
            let query = Some(ListRentalsQuery {
                status: if filters.history {
                    None // No filter - get all rentals
                } else {
                    filters.status.or(Some(RentalState::Active)) // Default to active
                },
                gpu_type: filters.gpu_type.clone(),
                min_gpu_count: filters.min_gpu_count,
            });

            // Fetch rentals, usage history, and pricing packages in parallel
            // Use a reasonable limit for usage history to cover active rentals
            let (rentals_result, usage_result, packages_result) = tokio::join!(
                api_client.list_rentals(query),
                api_client.list_usage_history(Some(100), None),
                api_client.get_packages()
            );

            let rentals_list = rentals_result.inspect_err(|_| {
                complete_spinner_error(spinner.clone(), "Failed to load rentals")
            })?;

            // Build usage map: rental_id -> usage record
            // If usage fetch fails, continue with empty map (graceful degradation)
            let usage_map: HashMap<String, basilica_sdk::types::RentalUsageRecord> = usage_result
                .ok()
                .map(|usage| {
                    usage
                        .rentals
                        .into_iter()
                        .map(|record| (record.rental_id.clone(), record))
                        .collect()
                })
                .unwrap_or_default();

            // Build pricing map: GPU type -> hourly rate
            // Package names are like "H100 GPU Package", we need to extract just "h100"
            let pricing_map: HashMap<String, String> = match packages_result {
                Ok(packages) => {
                    packages
                        .packages
                        .into_iter()
                        .filter(|p| p.is_active)
                        .filter_map(|p| {
                            // Extract GPU type from package name (e.g., "H100 GPU Package" -> "h100")
                            let gpu_type =
                                p.name.split_whitespace().next().map(|s| s.to_lowercase());

                            gpu_type.map(|t| (t, p.hourly_rate))
                        })
                        .collect()
                }
                Err(_e) => HashMap::new(),
            };

            complete_spinner_and_clear(spinner);

            if json {
                json_output(&rentals_list)?;
            } else if filters.history {
                // History mode: use usage_map data and filter out active rentals
                // Create a set of active rental IDs from the current rentals list
                let active_rental_ids: std::collections::HashSet<String> = rentals_list
                    .rentals
                    .iter()
                    .filter(|r| {
                        // Only include rentals that are currently active or provisioning
                        matches!(r.state, RentalState::Active | RentalState::Provisioning)
                    })
                    .map(|r| {
                        // Strip "rental-" prefix to match usage_map format
                        r.rental_id
                            .strip_prefix("rental-")
                            .unwrap_or(&r.rental_id)
                            .to_string()
                    })
                    .collect();

                // Filter usage_map to exclude active rentals
                let historical_rentals: Vec<_> = usage_map
                    .values()
                    .filter(|r| !active_rental_ids.contains(&r.rental_id))
                    .collect();

                table_output::display_usage_history_for_ps(&historical_rentals, filters.detailed)?;

                let total_cost: Decimal = historical_rentals
                    .iter()
                    .filter_map(|r| r.current_cost.parse::<Decimal>().ok())
                    .sum();

                println!();
                println!(
                    "{}: {}",
                    style("Total Cost").cyan(),
                    style(format!("${:.2}", total_cost)).green().bold()
                );

                println!("\nTotal: {} rentals", historical_rentals.len());

                display_ps_quick_start_commands();
            } else {
                table_output::display_rental_items(
                    &rentals_list.rentals[..],
                    !filters.compact,
                    filters.detailed,
                    &usage_map,
                    &pricing_map,
                    false,
                )?;

                println!("\nTotal: {} active rentals", rentals_list.rentals.len());

                display_ps_quick_start_commands();
            }
        }
        Some(ComputeCategory::SecureCloud) => {
            // Secure cloud rentals logic
            let spinner = if filters.history {
                create_spinner("Fetching rental history...")
            } else {
                create_spinner("Fetching active rentals...")
            };

            // Fetch secure cloud rentals
            let rentals_result = api_client.list_secure_cloud_rentals().await;

            let rentals_list = rentals_result.inspect_err(|_| {
                complete_spinner_error(spinner.clone(), "Failed to load secure cloud rentals")
            })?;

            complete_spinner_and_clear(spinner);

            if json {
                json_output(&rentals_list)?;
            } else {
                // Filter rentals based on history flag
                let rentals_to_display: Vec<_> = if filters.history {
                    // Show stopped rentals only
                    rentals_list
                        .rentals
                        .iter()
                        .filter(|r| r.stopped_at.is_some())
                        .collect()
                } else {
                    // Show active/running rentals only
                    rentals_list
                        .rentals
                        .iter()
                        .filter(|r| r.stopped_at.is_none())
                        .collect()
                };

                table_output::display_secure_cloud_rentals(
                    &rentals_to_display,
                    !filters.compact,
                    filters.detailed,
                )?;

                let label = if filters.history {
                    "historical secure cloud rentals"
                } else {
                    "active secure cloud rentals"
                };
                println!("\nTotal: {} {}", rentals_to_display.len(), label);

                display_ps_quick_start_commands();
            }
        }
        None => {
            // Dual-table display: show both community cloud and secure cloud rentals
            let spinner = create_spinner("Fetching rentals...");

            // Fetch both rental types in parallel
            let (community_result, secure_result) = tokio::join!(
                // Community cloud: rentals + usage + packages
                async {
                    let query = Some(ListRentalsQuery {
                        status: if filters.history {
                            None // No filter - get all rentals
                        } else {
                            filters.status.or(Some(RentalState::Active)) // Default to active
                        },
                        gpu_type: filters.gpu_type.clone(),
                        min_gpu_count: filters.min_gpu_count,
                    });

                    let (rentals_result, usage_result, packages_result) = tokio::join!(
                        api_client.list_rentals(query),
                        api_client.list_usage_history(Some(100), None),
                        api_client.get_packages()
                    );

                    (rentals_result, usage_result, packages_result)
                },
                // Secure cloud: just rentals
                api_client.list_secure_cloud_rentals()
            );

            // Process community cloud data
            let (community_rentals_result, community_usage_result, community_packages_result) =
                community_result;

            let community_rentals_list = community_rentals_result.inspect_err(|_| {
                complete_spinner_error(spinner.clone(), "Failed to load community cloud rentals")
            })?;

            // Build usage map: rental_id -> usage record
            let usage_map: HashMap<String, basilica_sdk::types::RentalUsageRecord> =
                community_usage_result
                    .ok()
                    .map(|usage| {
                        usage
                            .rentals
                            .into_iter()
                            .map(|record| (record.rental_id.clone(), record))
                            .collect()
                    })
                    .unwrap_or_default();

            // Build pricing map: GPU type -> hourly rate
            let pricing_map: HashMap<String, String> = match community_packages_result {
                Ok(packages) => packages
                    .packages
                    .into_iter()
                    .filter(|p| p.is_active)
                    .filter_map(|p| {
                        let gpu_type = p.name.split_whitespace().next().map(|s| s.to_lowercase());
                        gpu_type.map(|t| (t, p.hourly_rate))
                    })
                    .collect(),
                Err(_e) => HashMap::new(),
            };

            // Process secure cloud data
            let secure_rentals_list = secure_result.inspect_err(|_| {
                complete_spinner_error(spinner.clone(), "Failed to load secure cloud rentals")
            })?;

            complete_spinner_and_clear(spinner);

            if json {
                // JSON output: combine both rental types
                use serde_json::json;
                let output = json!({
                    "community_cloud": community_rentals_list,
                    "secure_cloud": secure_rentals_list
                });
                json_output(&output)?;
            } else {
                // Display community cloud table
                println!("\n{}", style("Community Cloud Rentals").bold());

                if filters.history {
                    // History mode: use usage_map data and filter out active rentals
                    let active_rental_ids: std::collections::HashSet<String> =
                        community_rentals_list
                            .rentals
                            .iter()
                            .filter(|r| {
                                matches!(r.state, RentalState::Active | RentalState::Provisioning)
                            })
                            .map(|r| {
                                r.rental_id
                                    .strip_prefix("rental-")
                                    .unwrap_or(&r.rental_id)
                                    .to_string()
                            })
                            .collect();

                    let historical_rentals: Vec<_> = usage_map
                        .values()
                        .filter(|r| !active_rental_ids.contains(&r.rental_id))
                        .collect();

                    table_output::display_usage_history_for_ps(
                        &historical_rentals,
                        filters.detailed,
                    )?;

                    let total_cost: Decimal = historical_rentals
                        .iter()
                        .filter_map(|r| r.current_cost.parse::<Decimal>().ok())
                        .sum();

                    println!();
                    println!(
                        "{}: {}",
                        style("Total Cost").cyan(),
                        style(format!("${:.2}", total_cost)).green().bold()
                    );

                    println!(
                        "\nTotal: {} community cloud rentals",
                        historical_rentals.len()
                    );
                } else {
                    // Active rentals
                    table_output::display_rental_items(
                        &community_rentals_list.rentals[..],
                        !filters.compact,
                        filters.detailed,
                        &usage_map,
                        &pricing_map,
                        false,
                    )?;

                    println!(
                        "\nTotal: {} community cloud rentals",
                        community_rentals_list.rentals.len()
                    );
                }

                // Separator between tables
                println!();

                // Display secure cloud table
                println!("{}", style("Secure Cloud Rentals").bold());

                let secure_rentals_to_display: Vec<_> = if filters.history {
                    secure_rentals_list
                        .rentals
                        .iter()
                        .filter(|r| r.stopped_at.is_some())
                        .collect()
                } else {
                    secure_rentals_list
                        .rentals
                        .iter()
                        .filter(|r| r.stopped_at.is_none())
                        .collect()
                };

                table_output::display_secure_cloud_rentals(
                    &secure_rentals_to_display,
                    !filters.compact,
                    filters.detailed,
                )?;

                let label = if filters.history {
                    "historical secure cloud rentals"
                } else {
                    "secure cloud rentals"
                };
                println!("\nTotal: {} {}", secure_rentals_to_display.len(), label);

                display_ps_quick_start_commands();
            }
        }
    }

    Ok(())
}

/// Handle the `status` command - check rental status
pub async fn handle_status(
    target: Option<String>,
    json: bool,
    config: &CliConfig,
) -> Result<(), CliError> {
    use basilica_common::types::ComputeCategory;

    let api_client = create_authenticated_client(config).await?;

    // Determine rental ID and compute type
    let (rental_id, compute_type) = if let Some(target_id) = target {
        // Rental ID provided - check both types to find it
        let spinner = create_spinner("Looking up rental...");

        let (community_result, secure_result) = tokio::join!(
            api_client.list_rentals(Some(basilica_sdk::types::ListRentalsQuery {
                status: Some(basilica_sdk::types::RentalState::Active),
                gpu_type: None,
                min_gpu_count: None,
            })),
            api_client.list_secure_cloud_rentals()
        );

        complete_spinner_and_clear(spinner);

        // Check community cloud first
        if let Ok(community) = community_result {
            if community.rentals.iter().any(|r| r.rental_id == target_id) {
                (target_id.clone(), ComputeCategory::CommunityCloud)
            } else if let Ok(secure) = secure_result {
                // Check secure cloud
                if secure.rentals.iter().any(|r| r.rental_id == target_id) {
                    (target_id.clone(), ComputeCategory::SecureCloud)
                } else {
                    return Err(CliError::Internal(
                        eyre!("Rental '{}' not found", target_id)
                            .suggestion("Try 'basilica ps' to see your active rentals")
                            .note("The rental may have expired or been terminated"),
                    ));
                }
            } else {
                return Err(CliError::Internal(
                    eyre!("Rental '{}' not found", target_id)
                        .suggestion("Try 'basilica ps' to see your active rentals")
                        .note("The rental may have expired or been terminated"),
                ));
            }
        } else if let Ok(secure) = secure_result {
            // Community query failed, check secure only
            if secure.rentals.iter().any(|r| r.rental_id == target_id) {
                (target_id.clone(), ComputeCategory::SecureCloud)
            } else {
                return Err(CliError::Internal(
                    eyre!("Rental '{}' not found", target_id)
                        .suggestion("Try 'basilica ps' to see your active rentals"),
                ));
            }
        } else {
            return Err(CliError::Internal(
                eyre!("Failed to fetch rentals")
                    .suggestion("Check your internet connection and try again"),
            ));
        }
    } else {
        // No rental ID provided - use unified selector
        resolve_target_rental_unified(None, None, &api_client).await?
    };

    let spinner = create_spinner("Fetching rental status...");

    match compute_type {
        ComputeCategory::CommunityCloud => {
            // Fetch community cloud status
            let status =
                api_client
                    .get_rental_status(&rental_id)
                    .await
                    .map_err(|e| -> CliError {
                        complete_spinner_error(spinner.clone(), "Failed to get status");
                        let report = match e {
                            ApiError::NotFound { .. } => eyre!("Rental '{}' not found", rental_id)
                                .suggestion("Try 'basilica ps' to see your active rentals")
                                .note("The rental may have expired or been terminated"),
                            _ => {
                                eyre!(e).suggestion("Check your internet connection and try again")
                            }
                        };
                        CliError::Internal(report)
                    })?;

            complete_spinner_and_clear(spinner);

            if json {
                json_output(&status)?;
            } else {
                display_rental_status_with_details(&status, config);
            }
        }
        ComputeCategory::SecureCloud => {
            // Fetch secure cloud status
            let rentals = api_client.list_secure_cloud_rentals().await.map_err(|e| {
                complete_spinner_error(spinner.clone(), "Failed to get status");
                CliError::Internal(
                    eyre!(e).suggestion("Check your internet connection and try again"),
                )
            })?;

            let rental = rentals
                .rentals
                .iter()
                .find(|r| r.rental_id == rental_id)
                .ok_or_else(|| {
                    complete_spinner_error(spinner.clone(), "Rental not found");
                    CliError::Internal(
                        eyre!("Rental '{}' not found", rental_id)
                            .suggestion("Try 'basilica ps' to see your active rentals")
                            .note("The rental may have expired or been terminated"),
                    )
                })?;

            complete_spinner_and_clear(spinner);

            if json {
                json_output(&rental)?;
            } else {
                // Display secure cloud rental details
                println!("Rental Status: {}", rental.rental_id);
                println!("  Provider: {}", rental.provider);
                println!("  Status: {}", rental.status);
                println!("  GPU: {}x {}", rental.gpu_count, rental.gpu_type);
                if let Some(ip) = &rental.ip_address {
                    println!("  IP Address: {}", ip);
                }
                println!("  Hourly Cost: ${:.2}/hr", rental.hourly_cost);
                println!("  Created: {}", rental.created_at);
                if let Some(stopped_at) = &rental.stopped_at {
                    println!("  Stopped: {}", stopped_at);
                }
                if let Some(ssh_cmd) = &rental.ssh_command {
                    println!("  SSH: {}", ssh_cmd);
                }
            }
        }
    }

    Ok(())
}

/// Handle the `logs` command - view rental logs
pub async fn handle_logs(
    target: Option<String>,
    options: LogsOptions,
    config: &CliConfig,
) -> Result<(), CliError> {
    use basilica_common::types::ComputeCategory;

    // Create API client
    let api_client = create_authenticated_client(config).await?;

    // Determine rental ID and compute type
    let (rental_id, compute_type) = if let Some(target_id) = target {
        // Rental ID provided - check both types to find it
        let spinner = create_spinner("Looking up rental...");

        let (community_result, secure_result) = tokio::join!(
            api_client.list_rentals(Some(basilica_sdk::types::ListRentalsQuery {
                status: Some(basilica_sdk::types::RentalState::Active),
                gpu_type: None,
                min_gpu_count: None,
            })),
            api_client.list_secure_cloud_rentals()
        );

        complete_spinner_and_clear(spinner);

        // Check community cloud first
        if let Ok(community) = community_result {
            if community.rentals.iter().any(|r| r.rental_id == target_id) {
                (target_id.clone(), ComputeCategory::CommunityCloud)
            } else if let Ok(secure) = secure_result {
                // Check secure cloud
                if secure.rentals.iter().any(|r| r.rental_id == target_id) {
                    (target_id.clone(), ComputeCategory::SecureCloud)
                } else {
                    return Err(CliError::Internal(
                        eyre!("Rental '{}' not found", target_id)
                            .suggestion("Try 'basilica ps' to see your active rentals"),
                    ));
                }
            } else {
                return Err(CliError::Internal(
                    eyre!("Rental '{}' not found", target_id)
                        .suggestion("Try 'basilica ps' to see your active rentals"),
                ));
            }
        } else if let Ok(secure) = secure_result {
            // Community query failed, check secure only
            if secure.rentals.iter().any(|r| r.rental_id == target_id) {
                (target_id.clone(), ComputeCategory::SecureCloud)
            } else {
                return Err(CliError::Internal(
                    eyre!("Rental '{}' not found", target_id)
                        .suggestion("Try 'basilica ps' to see your active rentals"),
                ));
            }
        } else {
            return Err(CliError::Internal(
                eyre!("Failed to fetch rentals")
                    .suggestion("Check your internet connection and try again"),
            ));
        }
    } else {
        // No rental ID provided - use unified selector
        resolve_target_rental_unified(None, None, &api_client).await?
    };

    // Check if this is a secure cloud rental
    if matches!(compute_type, ComputeCategory::SecureCloud) {
        return Err(CliError::Internal(
            eyre!("Log streaming is not yet available for secure cloud rentals")
                .note("Secure cloud logs support is coming soon")
                .suggestion(format!(
                    "For now, use SSH to access logs manually: basilica ssh {}",
                    rental_id
                )),
        ));
    }

    let target = rental_id;

    let spinner = create_spinner("Connecting to log stream...");

    // Get log stream from API
    let response = api_client
        .get_rental_logs(&target, options.follow, options.tail)
        .await
        .inspect_err(|_| complete_spinner_error(spinner.clone(), "Failed to connect to logs"))?;

    // Check content type
    let content_type = response
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");

    if !content_type.contains("text/event-stream") {
        // Not an SSE stream, try to get error message
        let status = response.status();
        let body = response
            .text()
            .await
            .unwrap_or_else(|_| "Unknown error".to_string());

        complete_spinner_error(spinner, "Failed to get logs");

        if status == StatusCode::NOT_FOUND {
            return Err(eyre!(
                "Rental '{}' not found. Run 'basilica ps' to see active rentals",
                target
            )
            .into());
        } else {
            return Err(eyre!(
                "API request failed for get logs: status {}: {}",
                status,
                body
            )
            .into());
        }
    }

    // Parse and display SSE stream
    use eventsource_stream::Eventsource;
    use futures_util::StreamExt;
    use serde::Deserialize;

    #[derive(Debug, Deserialize)]
    struct LogEntry {
        timestamp: chrono::DateTime<chrono::Utc>,
        stream: String,
        message: String,
    }

    complete_spinner_and_clear(spinner);

    let stream = response.bytes_stream().eventsource();

    println!("Streaming logs for rental {}...", target);
    if options.follow {
        println!("Following log output - press Ctrl+C to stop");
    }

    futures_util::pin_mut!(stream);

    while let Some(event) = stream.next().await {
        match event {
            Ok(sse_event) => {
                // Parse the data field as JSON
                match serde_json::from_str::<LogEntry>(&sse_event.data) {
                    Ok(entry) => {
                        let timestamp = entry.timestamp.format("%Y-%m-%d %H:%M:%S%.3f");
                        let stream_indicator = match entry.stream.as_str() {
                            "stdout" => "OUT",
                            "stderr" => "ERR",
                            "error" => "ERR",
                            _ => &entry.stream,
                        };
                        println!("[{} {}] {}", timestamp, stream_indicator, entry.message);
                    }
                    Err(e) => {
                        debug!("Failed to parse log event: {}, data: {}", e, sse_event.data);
                    }
                }
            }
            Err(e) => {
                eprintln!("Error reading log stream: {}", e);
                break;
            }
        }
    }

    Ok(())
}

/// Handle the `down` command - terminate rental
pub async fn handle_down(
    target: Option<String>,
    compute: Option<ComputeCategoryArg>,
    all: bool,
    config: &CliConfig,
) -> Result<(), CliError> {
    use super::gpu_rental_helpers::resolve_target_rental_unified;
    use basilica_common::types::ComputeCategory;

    let api_client = create_authenticated_client(config).await?;
    let compute_filter = compute.map(ComputeCategory::from);

    if all {
        // Stop all active rentals based on compute filter
        let spinner = create_spinner("Fetching active rentals...");

        // Determine what to fetch based on filter
        let (community_rentals, secure_rentals) = match compute_filter {
            Some(ComputeCategory::CommunityCloud) => {
                // Fetch only community cloud
                let query = Some(ListRentalsQuery {
                    status: Some(RentalState::Active),
                    gpu_type: None,
                    min_gpu_count: None,
                });
                let rentals = api_client.list_rentals(query).await.map_err(|e| {
                    complete_spinner_error(spinner.clone(), "Failed to fetch rentals");
                    CliError::Internal(eyre!(e).wrap_err("Failed to fetch active rentals"))
                })?;
                (Some(rentals), None)
            }
            Some(ComputeCategory::SecureCloud) => {
                // Fetch only secure cloud
                let rentals = api_client.list_secure_cloud_rentals().await.map_err(|e| {
                    complete_spinner_error(spinner.clone(), "Failed to fetch secure cloud rentals");
                    CliError::Internal(eyre!(e).wrap_err("Failed to fetch secure cloud rentals"))
                })?;
                (None, Some(rentals))
            }
            None => {
                // Fetch both types
                let (community_result, secure_result) = tokio::join!(
                    api_client.list_rentals(Some(ListRentalsQuery {
                        status: Some(RentalState::Active),
                        gpu_type: None,
                        min_gpu_count: None,
                    })),
                    api_client.list_secure_cloud_rentals()
                );
                (community_result.ok(), secure_result.ok())
            }
        };

        complete_spinner_and_clear(spinner);

        let mut total_count = 0;
        let mut success_count = 0;
        let mut failed_rentals = Vec::new();

        // Stop community cloud rentals
        if let Some(community) = community_rentals {
            for rental in community.rentals {
                total_count += 1;
                let rental_id = &rental.rental_id;
                let spinner = create_spinner(&format!("Stopping rental: {}", rental_id));

                match api_client.stop_rental(rental_id).await {
                    Ok(_) => {
                        complete_spinner_and_clear(spinner);
                        print_success(&format!(
                            "Successfully stopped community cloud rental {}",
                            rental_id
                        ));
                        success_count += 1;
                    }
                    Err(e) => {
                        complete_spinner_error(
                            spinner,
                            &format!("Failed to stop rental: {}", rental_id),
                        );
                        failed_rentals.push((rental_id.clone(), "community".to_string(), e));
                    }
                }
            }
        }

        // Stop secure cloud rentals (only active ones)
        if let Some(secure) = secure_rentals {
            for rental in secure.rentals {
                // Skip stopped rentals
                if rental.stopped_at.is_some() {
                    continue;
                }

                total_count += 1;
                let rental_id = &rental.rental_id;
                let spinner = create_spinner(&format!("Stopping rental: {}", rental_id));

                match api_client.stop_secure_cloud_rental(rental_id).await {
                    Ok(_) => {
                        complete_spinner_and_clear(spinner);
                        print_success(&format!(
                            "Successfully stopped secure cloud rental {}",
                            rental_id
                        ));
                        success_count += 1;
                    }
                    Err(e) => {
                        complete_spinner_error(
                            spinner,
                            &format!("Failed to stop rental: {}", rental_id),
                        );
                        failed_rentals.push((rental_id.clone(), "secure".to_string(), e));
                    }
                }
            }
        }

        if total_count == 0 {
            println!("No active rentals found.");
            return Ok(());
        }

        // Print summary
        println!();
        if failed_rentals.is_empty() {
            print_success(&format!(
                "Successfully stopped all {} rental{}",
                success_count,
                if success_count == 1 { "" } else { "s" }
            ));
        } else {
            print_success(&format!(
                "Successfully stopped {} out of {} rental{}",
                success_count,
                total_count,
                if total_count == 1 { "" } else { "s" }
            ));

            if !failed_rentals.is_empty() {
                println!("\nFailed to stop the following rentals:");
                for (rental_id, rental_type, error) in failed_rentals {
                    println!("  - {} ({}): {}", rental_id, rental_type, error);
                }
            }
        }
    } else {
        // Single rental termination using unified resolution
        let (rental_id, compute_type) =
            resolve_target_rental_unified(target, compute_filter, &api_client).await?;

        let spinner = create_spinner(&format!("Stopping rental: {}", rental_id));

        match compute_type {
            ComputeCategory::CommunityCloud => {
                // Stop community cloud rental
                api_client
                    .stop_rental(&rental_id)
                    .await
                    .map_err(|e| -> CliError {
                        complete_spinner_error(spinner.clone(), "Failed to stop rental");
                        let report = match e {
                            ApiError::NotFound { .. } => eyre!("Rental '{}' not found", rental_id)
                                .suggestion("Try 'basilica ps' to see your active rentals")
                                .note("The rental may have already been stopped"),
                            _ => {
                                eyre!(e).suggestion("Check your internet connection and try again")
                            }
                        };
                        CliError::Internal(report)
                    })?;

                complete_spinner_and_clear(spinner);
                print_success(&format!(
                    "Successfully stopped community cloud rental {}",
                    rental_id
                ));
            }
            ComputeCategory::SecureCloud => {
                // Stop secure cloud rental
                api_client
                    .stop_secure_cloud_rental(&rental_id)
                    .await
                    .map_err(|e| -> CliError {
                        complete_spinner_error(spinner.clone(), "Failed to stop rental");
                        let report = match e {
                            ApiError::NotFound { .. } => eyre!("Rental '{}' not found", rental_id)
                                .suggestion(
                                    "Try 'basilica ps --compute secure-cloud' to see your rentals",
                                )
                                .note("The rental may have already been stopped"),
                            _ => {
                                eyre!(e).suggestion("Check your internet connection and try again")
                            }
                        };
                        CliError::Internal(report)
                    })?;

                complete_spinner_and_clear(spinner);
                print_success(&format!(
                    "Successfully stopped secure cloud rental {}",
                    rental_id
                ));
            }
        }
    }

    Ok(())
}

/// Handle the `restart` command - restart rental container
pub async fn handle_restart(target: Option<String>, config: &CliConfig) -> Result<(), CliError> {
    let api_client = create_authenticated_client(config).await?;

    // Single rental restart (no --all flag as per requirements)
    let (rental_id, _compute_type) =
        resolve_target_rental_unified(target, None, &api_client).await?;
    let spinner = create_spinner(&format!("Restarting rental: {}", rental_id));

    api_client
        .restart_rental(&rental_id)
        .await
        .map_err(|e| -> CliError {
            complete_spinner_error(spinner.clone(), "Failed to restart rental");
            let report = match e {
                ApiError::NotFound { .. } => eyre!("Rental '{}' not found", rental_id)
                    .suggestion("Try 'basilica ps' to see your active rentals"),
                ApiError::Conflict { message } => {
                    eyre!("Cannot restart rental: {}", message).suggestion(
                        "Only Active rentals can be restarted. Check rental status with 'basilica status'",
                    )
                }
                _ => eyre!(e).suggestion("Check your internet connection and try again"),
            };
            CliError::Internal(report)
        })?;

    complete_spinner_and_clear(spinner);
    print_success(&format!("Successfully restarted rental: {}", rental_id));

    Ok(())
}

/// Handle the `exec` command - execute commands via SSH
pub async fn handle_exec(
    target: Option<String>,
    command: String,
    config: &CliConfig,
) -> Result<(), CliError> {
    use basilica_common::types::ComputeCategory;

    // Create API client to verify rental status
    let api_client = create_authenticated_client(config).await?;

    // Determine rental ID and compute type
    let (rental_id, compute_type, ssh_command) = if let Some(target_id) = target {
        // Rental ID provided - check both types to find it
        let spinner = create_spinner("Looking up rental...");

        let (community_result, secure_result) = tokio::join!(
            api_client.list_rentals(Some(basilica_sdk::types::ListRentalsQuery {
                status: Some(basilica_sdk::types::RentalState::Active),
                gpu_type: None,
                min_gpu_count: None,
            })),
            api_client.list_secure_cloud_rentals()
        );

        complete_spinner_and_clear(spinner);

        // Check community cloud first
        if let Ok(community) = community_result {
            if let Some(_rental) = community.rentals.iter().find(|r| r.rental_id == target_id) {
                // Found in community cloud - fetch SSH credentials
                let rental_status = api_client
                    .get_rental_status(&target_id)
                    .await
                    .map_err(|e| CliError::Internal(eyre!(e)))?;

                let ssh_creds = rental_status.ssh_credentials.ok_or_else(|| {
                    CliError::Internal(
                        eyre!("SSH credentials not available")
                            .wrap_err(format!(
                                "The rental '{}' was created without SSH access",
                                target_id
                            ))
                            .note("Rentals created with --no-ssh flag cannot be accessed via SSH")
                            .note("Create a new rental without --no-ssh to enable SSH access"),
                    )
                })?;

                (
                    target_id.clone(),
                    ComputeCategory::CommunityCloud,
                    ssh_creds,
                )
            } else if let Ok(secure) = secure_result {
                // Not in community, check secure cloud
                if let Some(rental) = secure.rentals.iter().find(|r| r.rental_id == target_id) {
                    let ssh_cmd = rental.ssh_command.clone().ok_or_else(|| {
                        CliError::Internal(
                            eyre!("SSH command not available")
                                .wrap_err(format!(
                                    "The rental '{}' does not have SSH access configured",
                                    target_id
                                ))
                                .note("The rental may still be provisioning or SSH may not be enabled"),
                        )
                    })?;

                    (target_id.clone(), ComputeCategory::SecureCloud, ssh_cmd)
                } else {
                    return Err(CliError::Internal(
                        eyre!("Rental '{}' not found", target_id)
                            .suggestion("Try 'basilica ps' to see your active rentals"),
                    ));
                }
            } else {
                return Err(CliError::Internal(
                    eyre!("Rental '{}' not found", target_id)
                        .suggestion("Try 'basilica ps' to see your active rentals"),
                ));
            }
        } else if let Ok(secure) = secure_result {
            // Community cloud query failed, check secure cloud only
            if let Some(rental) = secure.rentals.iter().find(|r| r.rental_id == target_id) {
                let ssh_cmd = rental.ssh_command.clone().ok_or_else(|| {
                    CliError::Internal(
                        eyre!("SSH command not available")
                            .wrap_err(format!(
                                "The rental '{}' does not have SSH access configured",
                                target_id
                            ))
                            .note("The rental may still be provisioning or SSH may not be enabled"),
                    )
                })?;

                (target_id.clone(), ComputeCategory::SecureCloud, ssh_cmd)
            } else {
                return Err(CliError::Internal(
                    eyre!("Rental '{}' not found", target_id)
                        .suggestion("Try 'basilica ps' to see your active rentals"),
                ));
            }
        } else {
            return Err(CliError::Internal(
                eyre!("Failed to fetch rentals")
                    .suggestion("Check your internet connection and try again"),
            ));
        }
    } else {
        // No rental ID provided - use unified selector
        let (rental_id, compute_type) =
            resolve_target_rental_unified(None, None, &api_client).await?;

        // Fetch SSH credentials based on type
        let ssh_cmd = match compute_type {
            ComputeCategory::CommunityCloud => {
                let rental_status = api_client
                    .get_rental_status(&rental_id)
                    .await
                    .map_err(|e| CliError::Internal(eyre!(e)))?;

                rental_status.ssh_credentials.ok_or_else(|| {
                    CliError::Internal(
                        eyre!("SSH credentials not available")
                            .wrap_err(format!(
                                "The rental '{}' was created without SSH access",
                                rental_id
                            ))
                            .note("Rentals created with --no-ssh flag cannot be accessed via SSH"),
                    )
                })?
            }
            ComputeCategory::SecureCloud => {
                let secure_rentals = api_client
                    .list_secure_cloud_rentals()
                    .await
                    .map_err(|e| CliError::Internal(eyre!(e)))?;

                let rental = secure_rentals
                    .rentals
                    .iter()
                    .find(|r| r.rental_id == rental_id)
                    .ok_or_else(|| CliError::Internal(eyre!("Rental '{}' not found", rental_id)))?;

                rental.ssh_command.clone().ok_or_else(|| {
                    CliError::Internal(
                        eyre!("SSH command not available")
                            .wrap_err(format!(
                                "The rental '{}' does not have SSH access configured",
                                rental_id
                            ))
                            .note("The rental may still be provisioning or SSH may not be enabled"),
                    )
                })?
            }
        };

        (rental_id, compute_type, ssh_cmd)
    };

    debug!(
        "Executing command on {} rental: {}",
        match compute_type {
            ComputeCategory::CommunityCloud => "community cloud",
            ComputeCategory::SecureCloud => "secure cloud",
        },
        rental_id
    );

    // Parse SSH credentials (format is the same for both types)
    let (host, port, username) = parse_ssh_credentials(&ssh_command)?;
    let ssh_access = SshAccess {
        host,
        port,
        username,
    };

    // Fetch API-registered SSH key and find matching private key
    let private_key_path = {
        let ssh_key = api_client
            .get_ssh_key()
            .await
            .map_err(|e| CliError::Internal(eyre!(e)))?
            .ok_or_else(|| {
                CliError::Internal(
                    eyre!("No SSH key registered with Basilica")
                        .suggestion("Run 'basilica ssh-keys add' to register your SSH key")
                        .note("SSH keys are required to connect to rentals"),
                )
            })?;

        crate::ssh::find_private_key_for_public_key(&ssh_key.public_key)
            .map_err(CliError::Internal)?
    };

    debug!("Using private key for exec: {}", private_key_path.display());

    // Use SSH client to execute command
    let ssh_client = SshClient::new(&config.ssh)?;
    ssh_client
        .execute_command(&ssh_access, &command, Some(private_key_path))
        .await?;
    Ok(())
}

/// Handle the `ssh` command - SSH into instances
pub async fn handle_ssh(
    target: Option<String>,
    options: crate::cli::commands::SshOptions,
    config: &CliConfig,
) -> Result<(), CliError> {
    use basilica_common::types::ComputeCategory;

    // Create API client to verify rental status
    let api_client = create_authenticated_client(config).await?;

    // Determine rental ID and compute type
    let (rental_id, compute_type, ssh_command) = if let Some(target_id) = target {
        // Rental ID provided - check both types to find it
        let spinner = create_spinner("Looking up rental...");

        let (community_result, secure_result) = tokio::join!(
            api_client.list_rentals(Some(basilica_sdk::types::ListRentalsQuery {
                status: Some(basilica_sdk::types::RentalState::Active),
                gpu_type: None,
                min_gpu_count: None,
            })),
            api_client.list_secure_cloud_rentals()
        );

        complete_spinner_and_clear(spinner);

        // Check community cloud first
        if let Ok(community) = community_result {
            if let Some(_rental) = community.rentals.iter().find(|r| r.rental_id == target_id) {
                // Found in community cloud - need to fetch SSH credentials
                let rental_status = api_client
                    .get_rental_status(&target_id)
                    .await
                    .map_err(|e| CliError::Internal(eyre!(e)))?;

                let ssh_creds = rental_status.ssh_credentials.ok_or_else(|| {
                    CliError::Internal(
                        eyre!("SSH credentials not available")
                            .wrap_err(format!(
                                "The rental '{}' was created without SSH access",
                                target_id
                            ))
                            .note("Rentals created with --no-ssh flag cannot be accessed via SSH")
                            .note("Create a new rental without --no-ssh to enable SSH access"),
                    )
                })?;

                (
                    target_id.clone(),
                    ComputeCategory::CommunityCloud,
                    ssh_creds,
                )
            } else if let Ok(secure) = secure_result {
                // Not in community, check secure cloud
                if let Some(rental) = secure.rentals.iter().find(|r| r.rental_id == target_id) {
                    let ssh_cmd = rental.ssh_command.clone().ok_or_else(|| {
                        CliError::Internal(
                            eyre!("SSH command not available")
                                .wrap_err(format!(
                                    "The rental '{}' does not have SSH access configured",
                                    target_id
                                ))
                                .note("The rental may still be provisioning or SSH may not be enabled"),
                        )
                    })?;

                    (target_id.clone(), ComputeCategory::SecureCloud, ssh_cmd)
                } else {
                    return Err(CliError::Internal(
                        eyre!("Rental '{}' not found", target_id)
                            .suggestion("Try 'basilica ps' to see your active rentals"),
                    ));
                }
            } else {
                return Err(CliError::Internal(
                    eyre!("Rental '{}' not found", target_id)
                        .suggestion("Try 'basilica ps' to see your active rentals"),
                ));
            }
        } else if let Ok(secure) = secure_result {
            // Community cloud query failed, check secure cloud only
            if let Some(rental) = secure.rentals.iter().find(|r| r.rental_id == target_id) {
                let ssh_cmd = rental.ssh_command.clone().ok_or_else(|| {
                    CliError::Internal(
                        eyre!("SSH command not available")
                            .wrap_err(format!(
                                "The rental '{}' does not have SSH access configured",
                                target_id
                            ))
                            .note("The rental may still be provisioning or SSH may not be enabled"),
                    )
                })?;

                (target_id.clone(), ComputeCategory::SecureCloud, ssh_cmd)
            } else {
                return Err(CliError::Internal(
                    eyre!("Rental '{}' not found", target_id)
                        .suggestion("Try 'basilica ps' to see your active rentals"),
                ));
            }
        } else {
            return Err(CliError::Internal(
                eyre!("Failed to fetch rentals")
                    .suggestion("Check your internet connection and try again"),
            ));
        }
    } else {
        // No rental ID provided - show selector with SSH-enabled rentals only
        let (rental_id, compute_type) =
            resolve_target_rental_unified(None, None, &api_client).await?;

        // Fetch SSH credentials based on type
        let ssh_cmd = match compute_type {
            ComputeCategory::CommunityCloud => {
                let rental_status = api_client
                    .get_rental_status(&rental_id)
                    .await
                    .map_err(|e| CliError::Internal(eyre!(e)))?;

                rental_status.ssh_credentials.ok_or_else(|| {
                    CliError::Internal(
                        eyre!("SSH credentials not available")
                            .wrap_err(format!(
                                "The rental '{}' was created without SSH access",
                                rental_id
                            ))
                            .note("Rentals created with --no-ssh flag cannot be accessed via SSH"),
                    )
                })?
            }
            ComputeCategory::SecureCloud => {
                let secure_rentals = api_client
                    .list_secure_cloud_rentals()
                    .await
                    .map_err(|e| CliError::Internal(eyre!(e)))?;

                let rental = secure_rentals
                    .rentals
                    .iter()
                    .find(|r| r.rental_id == rental_id)
                    .ok_or_else(|| CliError::Internal(eyre!("Rental '{}' not found", rental_id)))?;

                rental.ssh_command.clone().ok_or_else(|| {
                    CliError::Internal(
                        eyre!("SSH command not available")
                            .wrap_err(format!(
                                "The rental '{}' does not have SSH access configured",
                                rental_id
                            ))
                            .note("The rental may still be provisioning or SSH may not be enabled"),
                    )
                })?
            }
        };

        (rental_id, compute_type, ssh_cmd)
    };

    debug!(
        "Opening SSH connection to {} rental: {}",
        match compute_type {
            ComputeCategory::CommunityCloud => "community cloud",
            ComputeCategory::SecureCloud => "secure cloud",
        },
        rental_id
    );

    // Parse SSH credentials (format is the same for both types)
    debug!("Raw ssh_command: {}", ssh_command);
    let (host, port, username) = parse_ssh_credentials(&ssh_command)?;
    debug!(
        "Parsed credentials - host: {}, port: {}, username: {}",
        host, port, username
    );
    let ssh_access = SshAccess {
        host,
        port,
        username,
    };

    // Fetch API-registered SSH key and find matching private key
    let private_key_path = {
        let ssh_key = api_client
            .get_ssh_key()
            .await
            .map_err(|e| CliError::Internal(eyre!(e)))?
            .ok_or_else(|| {
                CliError::Internal(
                    eyre!("No SSH key registered with Basilica")
                        .suggestion("Run 'basilica ssh-keys add' to register your SSH key")
                        .note("SSH keys are required to connect to rentals"),
                )
            })?;

        crate::ssh::find_private_key_for_public_key(&ssh_key.public_key)
            .map_err(CliError::Internal)?
    };

    debug!("Using private key: {}", private_key_path.display());

    // Use SSH client to handle connection with options
    let ssh_client = SshClient::new(&config.ssh)?;

    // Open interactive session with port forwarding options
    ssh_client
        .interactive_session_with_options(&ssh_access, &options, Some(private_key_path))
        .await?;
    Ok(())
}

/// Handle the `cp` command - copy files via SSH
pub async fn handle_cp(
    source: String,
    destination: String,
    config: &CliConfig,
) -> Result<(), CliError> {
    debug!("Copying files from {} to {}", source, destination);

    // Create API client
    let api_client = create_authenticated_client(config).await?;

    // Parse source and destination to check if rental ID is provided
    let (source_rental, source_path) = split_remote_path(&source);
    let (dest_rental, dest_path) = split_remote_path(&destination);

    // Determine rental_id, handling interactive selection if needed
    let (rental_id, is_upload, local_path, remote_path) = match (source_rental, dest_rental) {
        (Some(rental), None) => {
            // Download: remote -> local
            (rental, false, dest_path, source_path)
        }
        (None, Some(rental)) => {
            // Upload: local -> remote
            (rental, true, source_path, dest_path)
        }
        (Some(_), Some(_)) => {
            return Err(CliError::Internal(eyre!(
                "Remote-to-remote copy not supported"
            )));
        }
        (None, None) => {
            // No rental ID provided, need to prompt user
            // First determine if this looks like an upload or download based on path existence
            let source_exists = std::path::Path::new(&source).exists();

            // Resolve target rental with unified selection
            let (selected_rental, _compute_type) = resolve_target_rental_unified(None, None, &api_client).await
                .map_err(|_| eyre!("No rental ID provided. Specify rental ID explicitly: 'basilica cp <rental_id>:<path> <local_path>' or vice versa"))?;

            // Determine direction based on source file existence
            if source_exists {
                // Upload: local file exists, so source is local
                (selected_rental, true, source, destination)
            } else {
                // Download: assume source is remote path
                (selected_rental, false, destination, source)
            }
        }
    };

    // Fetch SSH credentials from appropriate source based on rental type
    // Check both community and secure cloud rentals
    let spinner = create_spinner("Looking up rental...");

    let (community_result, secure_result) = tokio::join!(
        api_client.list_rentals(Some(basilica_sdk::types::ListRentalsQuery {
            status: Some(basilica_sdk::types::RentalState::Active),
            gpu_type: None,
            min_gpu_count: None,
        })),
        api_client.list_secure_cloud_rentals()
    );

    complete_spinner_and_clear(spinner);

    let ssh_credentials = if let Ok(community) = community_result {
        if let Some(_rental) = community.rentals.iter().find(|r| r.rental_id == rental_id) {
            // Found in community cloud - fetch SSH credentials
            let rental_status = api_client
                .get_rental_status(&rental_id)
                .await
                .map_err(|e| CliError::Internal(eyre!(e)))?;

            rental_status.ssh_credentials.ok_or_else(|| {
                CliError::Internal(
                    eyre!("SSH credentials not available")
                        .wrap_err(format!(
                            "The rental '{}' was created without SSH access",
                            rental_id
                        ))
                        .note("Rentals created with --no-ssh flag cannot be accessed via SSH")
                        .note("Create a new rental without --no-ssh to enable SSH access"),
                )
            })?
        } else if let Ok(secure) = secure_result {
            // Not in community, check secure cloud
            if let Some(rental) = secure.rentals.iter().find(|r| r.rental_id == rental_id) {
                rental.ssh_command.clone().ok_or_else(|| {
                    CliError::Internal(
                        eyre!("SSH command not available")
                            .wrap_err(format!(
                                "The rental '{}' does not have SSH access configured",
                                rental_id
                            ))
                            .note("The rental may still be provisioning or SSH may not be enabled"),
                    )
                })?
            } else {
                return Err(CliError::Internal(
                    eyre!("Rental '{}' not found", rental_id)
                        .suggestion("Try 'basilica ps' to see your active rentals"),
                ));
            }
        } else {
            return Err(CliError::Internal(
                eyre!("Rental '{}' not found", rental_id)
                    .suggestion("Try 'basilica ps' to see your active rentals"),
            ));
        }
    } else if let Ok(secure) = secure_result {
        // Community cloud query failed, check secure cloud only
        if let Some(rental) = secure.rentals.iter().find(|r| r.rental_id == rental_id) {
            rental.ssh_command.clone().ok_or_else(|| {
                CliError::Internal(
                    eyre!("SSH command not available")
                        .wrap_err(format!(
                            "The rental '{}' does not have SSH access configured",
                            rental_id
                        ))
                        .note("The rental may still be provisioning or SSH may not be enabled"),
                )
            })?
        } else {
            return Err(CliError::Internal(
                eyre!("Rental '{}' not found", rental_id)
                    .suggestion("Try 'basilica ps' to see your active rentals"),
            ));
        }
    } else {
        return Err(CliError::Internal(
            eyre!("Failed to fetch rentals")
                .suggestion("Check your internet connection and try again"),
        ));
    };

    // Parse SSH credentials
    let (host, port, username) = parse_ssh_credentials(&ssh_credentials)?;
    let ssh_access = SshAccess {
        host,
        port,
        username,
    };

    // Fetch API-registered SSH key and find matching private key
    let private_key_path = {
        let ssh_key = api_client
            .get_ssh_key()
            .await
            .map_err(|e| CliError::Internal(eyre!(e)))?
            .ok_or_else(|| {
                CliError::Internal(
                    eyre!("No SSH key registered with Basilica")
                        .suggestion("Run 'basilica ssh-keys add' to register your SSH key")
                        .note("SSH keys are required to connect to rentals"),
                )
            })?;

        crate::ssh::find_private_key_for_public_key(&ssh_key.public_key)
            .map_err(CliError::Internal)?
    };

    debug!(
        "Using private key for file transfer: {}",
        private_key_path.display()
    );

    // Use SSH client for file transfer
    let ssh_client = SshClient::new(&config.ssh).map_err(|e| eyre!(e))?;

    if is_upload {
        ssh_client
            .upload_file(
                &ssh_access,
                &local_path,
                &remote_path,
                Some(private_key_path),
            )
            .await?;
        Ok(())
    } else {
        ssh_client
            .download_file(
                &ssh_access,
                &remote_path,
                &local_path,
                Some(private_key_path),
            )
            .await?;
        Ok(())
    }
}

// Helper functions

/// Poll rental status until it becomes active or timeout
async fn poll_rental_status(
    rental_id: &str,
    api_client: &basilica_sdk::BasilicaClient,
) -> Result<bool, CliError> {
    const MAX_WAIT_TIME: Duration = Duration::from_secs(300);
    const INITIAL_INTERVAL: Duration = Duration::from_secs(2);
    const MAX_INTERVAL: Duration = Duration::from_secs(10);

    let spinner = create_spinner("Waiting for rental to become active...");
    let start_time = std::time::Instant::now();
    let mut interval = INITIAL_INTERVAL;
    let mut attempt = 0;

    loop {
        // Check if we've exceeded the maximum wait time
        if start_time.elapsed() > MAX_WAIT_TIME {
            complete_spinner_error(spinner, "Timeout waiting for rental to become active");
            return Ok(false);
        }

        attempt += 1;
        spinner.set_message(format!("Checking rental status... (attempt {})", attempt));

        // Check rental status
        match api_client.get_rental_status(rental_id).await {
            Ok(status) => {
                use basilica_sdk::types::RentalStatus;
                match status.status {
                    RentalStatus::Active => {
                        complete_spinner_and_clear(spinner);
                        return Ok(true);
                    }
                    RentalStatus::Failed => {
                        complete_spinner_error(spinner, "Rental failed to start");
                        return Err(CliError::Internal(eyre!(
                            "Rental failed: {}",
                            "Rental failed during initialization",
                        )));
                    }
                    RentalStatus::Terminated => {
                        complete_spinner_error(spinner, "Rental was terminated");
                        return Err(CliError::Internal(eyre!(
                            "Rental failed: {}",
                            "Rental was terminated before becoming active",
                        )));
                    }
                    RentalStatus::Pending => {
                        // Still pending, continue polling
                        spinner.set_message("Rental is pending...");
                    }
                }
            }
            Err(e) => {
                // Log the error but continue polling
                debug!("Error checking rental status: {}", e);
                spinner.set_message("Retrying status check...");
            }
        }

        // Wait before next check with exponential backoff
        tokio::time::sleep(interval).await;

        // Increase interval up to maximum
        interval = std::cmp::min(interval * 2, MAX_INTERVAL);
    }
}

/// Check if an IP address is private (RFC 1918)
fn is_private_ip(ip: &str) -> bool {
    if let Ok(addr) = ip.parse::<std::net::IpAddr>() {
        match addr {
            std::net::IpAddr::V4(ipv4) => {
                let octets = ipv4.octets();
                // 10.0.0.0/8
                octets[0] == 10 ||
                // 172.16.0.0/12
                (octets[0] == 172 && (16..=31).contains(&octets[1])) ||
                // 192.168.0.0/16
                (octets[0] == 192 && octets[1] == 168)
            }
            std::net::IpAddr::V6(_) => false, // IPv6 private ranges not relevant here
        }
    } else {
        false // If we can't parse it, assume it's not private
    }
}

/// Poll secure cloud rental status until it becomes running or timeout
async fn poll_secure_cloud_rental_status(
    rental_id: &str,
    api_client: &basilica_sdk::BasilicaClient,
) -> Result<Option<basilica_sdk::types::SecureCloudRentalListItem>, CliError> {
    const MAX_WAIT_TIME: Duration = Duration::from_secs(180);
    const INITIAL_INTERVAL: Duration = Duration::from_secs(5);
    const MAX_INTERVAL: Duration = Duration::from_secs(15);

    let spinner = create_spinner("Waiting for rental to become active...");
    let start_time = std::time::Instant::now();
    let mut interval = INITIAL_INTERVAL;
    let mut attempt = 0;

    loop {
        // Check if we've exceeded the maximum wait time
        if start_time.elapsed() > MAX_WAIT_TIME {
            complete_spinner_error(spinner, "Timeout waiting for rental to become active");
            return Ok(None);
        }

        attempt += 1;
        spinner.set_message(format!("Checking rental status... (attempt {})", attempt));

        // Get rental from list
        match api_client.list_secure_cloud_rentals().await {
            Ok(response) => {
                // Find our rental
                if let Some(rental) = response.rentals.iter().find(|r| r.rental_id == rental_id) {
                    match rental.status.as_str() {
                        "running" => {
                            // Check if SSH command has a public IP
                            if let Some(ssh_cmd) = &rental.ssh_command {
                                // Parse the SSH command to extract the host
                                if let Ok((host, _, _)) = parse_ssh_credentials(ssh_cmd) {
                                    if is_private_ip(&host) {
                                        // Still has private IP, continue polling
                                        spinner.set_message(format!(
                                            "Rental running but waiting for public IP... ({}s elapsed)",
                                            start_time.elapsed().as_secs()
                                        ));
                                        // Continue to next iteration
                                    } else {
                                        // Has public IP, return success
                                        complete_spinner_and_clear(spinner);
                                        return Ok(Some(rental.clone()));
                                    }
                                } else {
                                    // Can't parse SSH command, return anyway
                                    complete_spinner_and_clear(spinner);
                                    return Ok(Some(rental.clone()));
                                }
                            } else {
                                // No SSH command yet, return success anyway
                                complete_spinner_and_clear(spinner);
                                return Ok(Some(rental.clone()));
                            }
                        }
                        "error" => {
                            complete_spinner_error(spinner, "Rental failed to start");
                            return Err(CliError::Internal(eyre!(
                                "Rental failed during provisioning"
                            )));
                        }
                        "deleted" => {
                            complete_spinner_error(spinner, "Rental was deleted");
                            return Err(CliError::Internal(eyre!(
                                "Rental was deleted before becoming active"
                            )));
                        }
                        "pending" | "provisioning" => {
                            // Still starting, continue polling
                            spinner.set_message(format!(
                                "Rental is {}... ({}s elapsed)",
                                rental.status,
                                start_time.elapsed().as_secs()
                            ));
                        }
                        _ => {
                            // Unknown status, continue polling
                            spinner.set_message(format!(
                                "Rental status: {}... ({}s elapsed)",
                                rental.status,
                                start_time.elapsed().as_secs()
                            ));
                        }
                    }
                } else {
                    // Rental not found in list
                    complete_spinner_error(spinner, "Rental not found");
                    return Err(CliError::Internal(eyre!(
                        "Rental {} not found in rental list",
                        rental_id
                    )));
                }
            }
            Err(e) => {
                // Log the error but continue polling
                debug!("Error checking rental status: {}", e);
                spinner.set_message("Retrying status check...");
            }
        }

        // Wait before next check with exponential backoff
        tokio::time::sleep(interval).await;

        // Increase interval up to maximum
        interval = std::cmp::min(interval * 2, MAX_INTERVAL);
    }
}

/// Retry SSH connection with exponential backoff
///
/// SSH services may not be immediately available after a rental becomes active.
/// This function retries the connection for up to `max_wait` duration with exponential backoff.
async fn retry_ssh_connection(
    ssh_client: &SshClient,
    ssh_access: &SshAccess,
    private_key_override: Option<PathBuf>,
    max_wait: Duration,
) -> Result<(), CliError> {
    const INITIAL_INTERVAL: Duration = Duration::from_secs(2);
    const MAX_INTERVAL: Duration = Duration::from_secs(10);

    let start_time = std::time::Instant::now();
    let mut interval = INITIAL_INTERVAL;
    let mut attempt = 0;

    // Use a spinner to show progress and avoid cluttering the terminal
    let spinner = create_spinner("Waiting for SSH to become available...");

    loop {
        attempt += 1;
        spinner.set_message("SSH not ready yet, retrying...");

        // First test connectivity without starting an interactive session
        // This captures stderr so we don't print raw SSH errors
        match ssh_client
            .test_connection(ssh_access, private_key_override.clone())
            .await
        {
            Ok(_) => {
                // Connection test succeeded, now start the actual interactive session
                complete_spinner_and_clear(spinner);
                return ssh_client
                    .interactive_session(ssh_access, private_key_override)
                    .await
                    .map_err(|e| CliError::Internal(eyre!("SSH session failed: {}", e)));
            }
            Err(e) => {
                // Check if we've exceeded the maximum wait time
                if start_time.elapsed() >= max_wait {
                    // Final attempt failed, return error
                    complete_spinner_error(spinner, "SSH connection failed");
                    return Err(CliError::Internal(
                        eyre!(
                            "SSH connection failed after {} attempts over {}s: {}",
                            attempt,
                            start_time.elapsed().as_secs(),
                            e
                        )
                        .suggestion("The SSH service may still be starting up")
                        .note("Try connecting manually in a few moments"),
                    ));
                }

                // Log the retry attempt
                debug!(
                    "SSH connection attempt {} failed ({}s elapsed): {}. Retrying in {}s...",
                    attempt,
                    start_time.elapsed().as_secs(),
                    e,
                    interval.as_secs()
                );

                // Wait before next attempt
                tokio::time::sleep(interval).await;

                // Increase interval up to maximum (exponential backoff)
                interval = std::cmp::min(interval * 2, MAX_INTERVAL);
            }
        }
    }
}

/// Display SSH connection instructions after rental creation
fn display_ssh_connection_instructions(
    rental_id: &str,
    ssh_credentials: &str,
    config: &CliConfig,
    message: &str,
) -> Result<(), CliError> {
    // Parse SSH credentials to get components
    let (host, port, username) = parse_ssh_credentials(ssh_credentials)?;

    // Get the private key path from config
    let private_key_path = &config.ssh.private_key_path;

    println!();
    print_info(message);
    println!();

    // Option 1: Using basilica CLI (simplest)
    println!("  1. Using Basilica CLI:");
    println!(
        "     {}",
        console::style(format!("basilica ssh {}", rental_id))
            .cyan()
            .bold()
    );
    println!();

    // Option 2: Using standard SSH command
    println!("  2. Using standard SSH:");
    println!(
        "     {}",
        console::style(format!(
            "ssh -i {} -p {} {}@{}",
            compress_path(private_key_path),
            port,
            username,
            host
        ))
        .cyan()
        .bold()
    );

    Ok(())
}

/// Display SSH connection instructions for secure cloud rentals
fn display_secure_cloud_reconnection_instructions(
    rental_id: &str,
    ssh_command: &str,
    config: &CliConfig,
    message: &str,
) -> Result<(), CliError> {
    // Parse SSH command to get components
    let (host, port, username) = parse_ssh_credentials(ssh_command)?;

    // Get the private key path from config
    let private_key_path = &config.ssh.private_key_path;

    println!();
    print_info(message);
    println!();

    // Option 1: Using basilica CLI (simplest)
    println!("  1. Using Basilica CLI:");
    println!(
        "     {}",
        console::style(format!("basilica ssh {}", rental_id))
            .cyan()
            .bold()
    );
    println!();

    // Option 2: Using standard SSH command
    println!("  2. Using standard SSH:");
    println!(
        "     {}",
        console::style(format!(
            "ssh -i {} -p {} {}@{}",
            compress_path(private_key_path),
            port,
            username,
            host
        ))
        .cyan()
        .bold()
    );

    Ok(())
}

fn split_remote_path(path: &str) -> (Option<String>, String) {
    if let Some((rental_id, remote_path)) = path.split_once(':') {
        (Some(rental_id.to_string()), remote_path.to_string())
    } else {
        (None, path.to_string())
    }
}

fn display_rental_status_with_details(
    status: &basilica_sdk::types::RentalStatusWithSshResponse,
    config: &CliConfig,
) {
    println!("Rental Status: {}", status.rental_id);
    println!("  Status: {:?}", status.status);
    println!("  Node: {}", status.node.id);
    println!(
        "  Created: {}",
        status.created_at.format("%Y-%m-%d %H:%M:%S UTC")
    );
    println!(
        "  Updated: {}",
        status.updated_at.format("%Y-%m-%d %H:%M:%S UTC")
    );

    // Display port mappings if available
    if let Some(ref port_mappings) = status.port_mappings {
        if !port_mappings.is_empty() {
            println!("\nPort Mappings (Host → Container):");
            let port_strings: Vec<String> = port_mappings
                .iter()
                .map(|p| format!("{}→{}", p.host_port, p.container_port))
                .collect();
            println!("  {}", port_strings.join(", "));
        }
    }

    // Display SSH connection instructions if available
    if let Some(ref ssh_credentials) = status.ssh_credentials {
        if let Ok((host, port, username)) = parse_ssh_credentials(ssh_credentials) {
            let private_key_path = &config.ssh.private_key_path;

            println!();
            print_info("SSH Connection:");
            println!();

            // Option 1: Using basilica CLI (simplest)
            println!("  1. Using Basilica CLI:");
            println!(
                "     {}",
                console::style(format!("basilica ssh {}", status.rental_id))
                    .cyan()
                    .bold()
            );
            println!();

            // Option 2: Using standard SSH command
            println!("  2. Using standard SSH:");
            println!(
                "     {}",
                console::style(format!(
                    "ssh -i {} -p {} {}@{}",
                    compress_path(private_key_path),
                    port,
                    username,
                    host
                ))
                .cyan()
                .bold()
            );
        }
    }
}

/// Display quick start commands after ps output
fn display_ps_quick_start_commands() {
    println!();
    println!("{}", style("Quick Commands:").cyan().bold());

    println!(
        "  {} {}",
        style("basilica ssh").yellow().bold(),
        style("- Connect to your rental").dim()
    );

    println!(
        "  {} {}",
        style("basilica exec").yellow().bold(),
        style("- Run commands on your rental").dim()
    );

    println!(
        "  {} {}",
        style("basilica logs").yellow().bold(),
        style("- Stream container logs").dim()
    );

    println!(
        "  {} {}",
        style("basilica status").yellow().bold(),
        style("- Check status of a specific rental").dim()
    );

    println!(
        "  {} {}",
        style("basilica down").yellow().bold(),
        style("- Stop a GPU rental").dim()
    );
}
