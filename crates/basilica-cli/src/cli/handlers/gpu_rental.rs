//! GPU rental command handlers

use crate::cli::commands::{ComputeCategoryArg, ListFilters, LogsOptions, PsFilters, UpOptions};
use crate::cli::handlers::gpu_rental_helpers::resolve_target_rental;
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

/// Handle the `ls` command - list available nodes for rental
pub async fn handle_ls(
    gpu_category: Option<GpuCategory>,
    filters: ListFilters,
    compute: Option<ComputeCategoryArg>,
    json: bool,
    config: &CliConfig,
) -> Result<(), CliError> {
    let api_client = create_authenticated_client(config).await?;

    // Determine compute category (default to secure cloud)
    let compute_category = compute
        .map(|c| match c {
            ComputeCategoryArg::SecureCloud => ComputeCategory::SecureCloud,
            ComputeCategoryArg::CommunityCloud => ComputeCategory::CommunityCloud,
        })
        .unwrap_or(ComputeCategory::SecureCloud);

    // Branch based on compute type
    match compute_category {
        ComputeCategory::SecureCloud => {
            // Fetch datacenter GPU offerings
            let spinner = create_spinner("Scanning datacenter GPU availability...");
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

            if filtered_gpus.is_empty() {
                print_info("No GPUs available matching your criteria");
                return Ok(());
            }

            // Sort by price (ascending)
            filtered_gpus.sort_by(|a, b| a.hourly_rate.partial_cmp(&b.hourly_rate).unwrap());

            if json {
                json_output(&filtered_gpus)?;
            } else {
                // Use table_output module for consistent styling
                if filters.compact {
                    table_output::display_secure_cloud_offerings_compact(&filtered_gpus)?;
                } else {
                    table_output::display_secure_cloud_offerings_detailed(
                        &filtered_gpus,
                        filters.detailed, // show_ids
                    )?;
                }
            }
        }
        ComputeCategory::CommunityCloud => {
            // Convert GPU category to string if provided
            let gpu_type = gpu_category.map(|gc| gc.as_str());

            // Build query from filters
            let query = ListAvailableNodesQuery {
                available: Some(true), // Filter for available nodes only
                min_gpu_memory: filters.memory_min,
                gpu_type,
                min_gpu_count: Some(filters.gpu_min.unwrap_or(0)),
                location: filters.country.map(|country| LocationProfile {
                    city: None,
                    region: None,
                    country: Some(country),
                }),
            };

            let spinner = create_spinner("Scanning global GPU availability...");

            // Fetch both available nodes and pricing data in parallel
            let (response, packages_result) = tokio::join!(
                api_client.list_available_nodes(Some(query)),
                api_client.get_packages()
            );

            let response = response.map_err(|e| -> CliError {
                complete_spinner_error(spinner.clone(), "Failed to fetch nodes");
                CliError::Internal(
                    eyre!(e)
                        .suggestion("Check your internet connection and try again")
                        .note("If this persists, nodes may be temporarily unavailable"),
                )
            })?;

            // Build pricing map: GPU type -> hourly rate
            let pricing_map: HashMap<String, String> = match packages_result {
                Ok(packages) => {
                    packages
                        .packages
                        .into_iter()
                        .filter(|p| p.is_active)
                        .filter_map(|p| {
                            // Extract GPU type from package name (e.g., "H100 GPU Package" -> "h100")
                            let gpu_type = p.name.split_whitespace().next().map(|s| s.to_lowercase());
                            gpu_type.map(|t| (t, p.hourly_rate))
                        })
                        .collect()
                }
                Err(_e) => HashMap::new(),
            };

            complete_spinner_and_clear(spinner);

            if json {
                json_output(&response)?;
            } else {
                // Use table_output module for consistent styling
                if filters.compact {
                    // Compact view: grouped by country and GPU type
                    table_output::display_available_nodes_compact(&response.available_nodes, &pricing_map)?;
                } else {
                    // Default or detailed view: show individual nodes
                    table_output::display_available_nodes_detailed(
                        &response.available_nodes,
                        true,  // show_full_gpu_names
                        filters.detailed,  // show_ids
                        &pricing_map,
                    )?;
                }
            }
        }
    }

    Ok(())
}

/// Ensure user has SSH key registered for secure cloud
///
/// Auto-registers default SSH key (~/.ssh/id_rsa.pub) with user confirmation.
/// Returns the SSH key ID if successful.
async fn ensure_ssh_key_registered(
    api_client: &basilica_sdk::BasilicaClient,
) -> Result<String, CliError> {
    // Check if user already has SSH key
    if let Some(key) = api_client.get_user_ssh_key().await? {
        return Ok(key.id);
    }

    // Find default SSH public key
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .map_err(|_| CliError::Internal(eyre!("Could not determine home directory")))?;

    let ssh_key_path = std::path::PathBuf::from(home)
        .join(".ssh")
        .join("id_rsa.pub");

    if !ssh_key_path.exists() {
        return Err(CliError::Internal(eyre!(
            "No SSH public key found at {}\n\
                 Please generate one with: ssh-keygen -t rsa -b 4096",
            ssh_key_path.display()
        )));
    }

    // Read public key
    let public_key = std::fs::read_to_string(&ssh_key_path)
        .map_err(|e| CliError::Internal(eyre!(e).wrap_err("Failed to read SSH public key")))?;

    // Prompt user for confirmation
    use dialoguer::Confirm;

    println!("\n🔑 SSH Key Registration Required");
    println!("─────────────────────────────────");
    println!("Secure cloud deployments require SSH key registration.");
    println!("Key path: {}", ssh_key_path.display());
    println!(
        "Key type: {}",
        public_key.split_whitespace().next().unwrap_or("unknown")
    );
    println!();

    let confirmed = Confirm::new()
        .with_prompt("Register this SSH key for secure cloud?")
        .default(true)
        .interact()
        .map_err(|e| CliError::Internal(eyre!(e).wrap_err("Failed to show confirmation prompt")))?;

    if !confirmed {
        return Err(CliError::Internal(eyre!(
            "SSH key registration required for secure cloud rentals"
        )));
    }

    // Register key
    let spinner = create_spinner("Registering SSH key...");
    let result = api_client
        .register_ssh_key("default", public_key.trim())
        .await;

    match result {
        Ok(key) => {
            complete_spinner_and_clear(spinner);
            print_success("SSH key registered successfully");
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
            let base_price = o.hourly_rate.to_f64().unwrap_or(0.0);
            let total_price = base_price * (1.0 + markup_percent / 100.0);

            let memory_str = if let Some(mem_per_gpu) = o.gpu_memory_gb_per_gpu {
                format!("{}GB", mem_per_gpu * o.gpu_count)
            } else {
                "N/A".to_string()
            };

            format!(
                "{:<12} {:>8} {:>4}x {:<8} {:>6} | ${:>6.2}/hr",
                o.provider.as_str(),
                o.region,
                o.gpu_count,
                o.gpu_type.as_str(),
                memory_str,
                total_price
            )
        })
        .collect();

    // Show header
    println!("\n📊 Available GPU Offerings");
    println!("─────────────────────────────────────────────────────────────────────────────");
    println!(
        "{:<12} {:<8} {:<15} {:<8} {:<20}",
        "Provider", "Region", "GPU", "Memory", "Price/hr"
    );
    println!("─────────────────────────────────────────────────────────────────────────────");

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

/// Handle secure cloud rental workflow
async fn handle_secure_cloud_rental(
    api_client: basilica_sdk::BasilicaClient,
    target: Option<TargetType>,
    options: UpOptions,
) -> Result<(), CliError> {
    // Step 1: Ensure SSH key registered
    let ssh_key_id = ensure_ssh_key_registered(&api_client).await?;

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

    // Step 5: Show rental summary
    let markup_percent = 15.0;
    let base_price = selected.hourly_rate.to_f64().unwrap_or(0.0);
    let total_price = base_price * (1.0 + markup_percent / 100.0);

    println!("\n🚀 Starting Secure Cloud Rental");
    println!("─────────────────────────────────");
    println!("Provider:    {}", selected.provider.as_str());
    println!(
        "GPU:         {}x {}",
        selected.gpu_count,
        selected.gpu_type.as_str()
    );
    if let Some(mem_per_gpu) = selected.gpu_memory_gb_per_gpu {
        println!("Memory:      {}GB total", mem_per_gpu * selected.gpu_count);
    }
    println!("Region:      {}", selected.region);
    println!("Price:       ${:.2}/hr", total_price);
    println!();

    // Step 6: Confirm
    use dialoguer::Confirm;
    let confirmed = Confirm::new()
        .with_prompt("Proceed with rental?")
        .default(true)
        .interact()
        .map_err(|e| CliError::Internal(eyre!(e).wrap_err("Failed to show confirmation")))?;

    if !confirmed {
        print_info("Rental cancelled");
        return Ok(());
    }

    // Step 7: Start rental
    let spinner = create_spinner("Provisioning GPU instance...");

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
        offering_id: selected.id.clone(),
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

    // Step 8: Display rental info
    print_success("Secure cloud rental started successfully!");
    println!();
    println!("Rental Details:");
    println!("─────────────────────────────────");
    println!("Rental ID:   {}", response.rental_id);
    println!("Provider:    {}", response.provider);
    println!("Status:      {}", response.status);

    if let Some(ip) = &response.ip_address {
        println!("IP Address:  {}", ip);
    }

    println!("Hourly Cost: ${:.2}/hr", response.hourly_cost);
    println!();

    if let Some(ssh_cmd) = &response.ssh_command {
        println!("SSH Command:");
        println!("  {}", ssh_cmd);
    } else {
        println!("⏳ Instance is starting up. Use 'basilica ps' to check status.");
    }

    println!();
    print_info("Monitor with: basilica ps");
    print_info(&format!("Stop with: basilica down {}", response.rental_id));

    Ok(())
}

/// Handle the `up` command - provision GPU instances
pub async fn handle_up(
    target: Option<TargetType>,
    options: UpOptions,
    compute: Option<ComputeCategoryArg>,
    config: &CliConfig,
) -> Result<(), CliError> {
    let api_client = create_authenticated_client(config).await?;

    // Determine compute category (default to secure cloud)
    let compute_category = compute
        .map(|c| match c {
            ComputeCategoryArg::SecureCloud => ComputeCategory::SecureCloud,
            ComputeCategoryArg::CommunityCloud => ComputeCategory::CommunityCloud,
        })
        .unwrap_or(ComputeCategory::SecureCloud);

    // Branch based on compute type
    match compute_category {
        ComputeCategory::SecureCloud => {
            return handle_secure_cloud_rental(api_client, target, options).await;
        }
        ComputeCategory::CommunityCloud => {
            // Fall through to existing community cloud implementation
        }
    }

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
                    create_spinner(&format!("Finding available {} nodes...", gpu_category));
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
        let spinner = create_spinner("Fetching available nodes...");

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
    spinner.set_message("Validating SSH key...");
    let ssh_public_key = load_ssh_public_key(&options.ssh_key, config).inspect_err(|_e| {
        complete_spinner_error(spinner.clone(), "SSH key validation failed");
    })?;

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
        ssh_public_key,
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
        "Successfully created rental: {}",
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

            // Use SSH client to open interactive session
            let ssh_client = SshClient::new(&config.ssh)?;
            match ssh_client.interactive_session(&ssh_access).await {
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

    // Default to SecureCloud (same as `up` command)
    let compute_category = compute
        .map(ComputeCategory::from)
        .unwrap_or(ComputeCategory::SecureCloud);

    let api_client = create_authenticated_client(config).await?;

    // Branch based on compute category
    match compute_category {
        ComputeCategory::CommunityCloud => {
            // Existing community cloud logic
            let spinner = if filters.history {
                create_spinner("Loading rental history...")
            } else {
                create_spinner("Loading active rentals...")
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
        ComputeCategory::SecureCloud => {
            // Secure cloud rentals logic
            let spinner = if filters.history {
                create_spinner("Loading secure cloud rental history...")
            } else {
                create_spinner("Loading secure cloud rentals...")
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
    }

    Ok(())
}

/// Handle the `status` command - check rental status
pub async fn handle_status(
    target: Option<String>,
    json: bool,
    config: &CliConfig,
) -> Result<(), CliError> {
    let api_client = create_authenticated_client(config).await?;

    // Resolve target rental (fetch and prompt if not provided)
    let target = resolve_target_rental(target, &api_client, false).await?;

    let spinner = create_spinner("Checking rental status...");

    let status = api_client
        .get_rental_status(&target)
        .await
        .map_err(|e| -> CliError {
            complete_spinner_error(spinner.clone(), "Failed to get status");
            let report = match e {
                ApiError::NotFound { .. } => eyre!("Rental '{}' not found", target)
                    .suggestion("Try 'basilica ps' to see your active rentals")
                    .note("The rental may have expired or been terminated"),
                _ => eyre!(e).suggestion("Check your internet connection and try again"),
            };
            CliError::Internal(report)
        })?;

    complete_spinner_and_clear(spinner);

    if json {
        json_output(&status)?;
    } else {
        display_rental_status_with_details(&status, config);
    }

    Ok(())
}

/// Handle the `logs` command - view rental logs
pub async fn handle_logs(
    target: Option<String>,
    options: LogsOptions,
    config: &CliConfig,
) -> Result<(), CliError> {
    // Create API client
    let api_client = create_authenticated_client(config).await?;

    // Resolve target rental (fetch and prompt if not provided)
    let target = resolve_target_rental(target, &api_client, false).await?;

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
                let spinner =
                    create_spinner(&format!("Stopping community cloud rental: {}", rental_id));

                match api_client.stop_rental(rental_id).await {
                    Ok(_) => {
                        complete_spinner_and_clear(spinner);
                        print_success(&format!("✓ Community cloud rental {} stopped", rental_id));
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
                let spinner =
                    create_spinner(&format!("Stopping secure cloud rental: {}", rental_id));

                match api_client.stop_secure_cloud_rental(rental_id).await {
                    Ok(response) => {
                        complete_spinner_and_clear(spinner);
                        print_success(&format!(
                            "✓ Secure cloud rental {} stopped | Duration: {:.2}h | Cost: ${:.2}",
                            rental_id, response.duration_hours, response.total_cost
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
                "Successfully stopped all {} rental{}.",
                success_count,
                if success_count == 1 { "" } else { "s" }
            ));
        } else {
            print_success(&format!(
                "Successfully stopped {} out of {} rental{}.",
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
                    "✓ Community cloud rental {} stopped successfully",
                    rental_id
                ));
            }
            ComputeCategory::SecureCloud => {
                // Stop secure cloud rental
                let response = api_client
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
                    "✓ Secure cloud rental {} stopped successfully",
                    rental_id
                ));
                println!();
                println!("  Duration:   {:.2} hours", response.duration_hours);
                println!("  Total cost: ${:.2}", response.total_cost);
            }
        }
    }

    Ok(())
}

/// Handle the `exec` command - execute commands via SSH
pub async fn handle_exec(
    target: Option<String>,
    command: String,
    config: &CliConfig,
) -> Result<(), CliError> {
    // Create API client to verify rental status
    let api_client = create_authenticated_client(config).await?;

    // Resolve target rental with SSH requirement
    let target = resolve_target_rental(target, &api_client, true).await?;

    debug!("Executing command on rental: {}", target);

    // Get rental status from API which includes SSH credentials
    let rental_status = api_client
        .get_rental_status(&target)
        .await
        .map_err(|e| -> CliError {
            let report = match e {
                ApiError::NotFound { .. } => eyre!("Rental '{}' not found", target)
                    .suggestion("Try 'basilica ps' to see your active rentals"),
                _ => eyre!(e).suggestion("Check your internet connection and try again"),
            };
            CliError::Internal(report)
        })?;

    // Extract SSH credentials from response
    let ssh_credentials = rental_status.ssh_credentials.ok_or_else(|| {
        eyre!("SSH credentials not available")
            .wrap_err(format!(
                "The rental '{}' was created without SSH access",
                target
            ))
            .note("Rentals created with --no-ssh flag cannot be accessed via SSH")
            .note("Create a new rental without --no-ssh to enable SSH access")
    })?;

    // Parse SSH credentials
    let (host, port, username) = parse_ssh_credentials(&ssh_credentials)?;
    let ssh_access = SshAccess {
        host,
        port,
        username,
    };

    // Use SSH client to execute command
    let ssh_client = SshClient::new(&config.ssh)?;
    ssh_client.execute_command(&ssh_access, &command).await?;
    Ok(())
}

/// Handle the `ssh` command - SSH into instances
pub async fn handle_ssh(
    target: Option<String>,
    options: crate::cli::commands::SshOptions,
    config: &CliConfig,
) -> Result<(), CliError> {
    // Create API client to verify rental status
    let api_client = create_authenticated_client(config).await?;

    // Resolve target rental with SSH requirement
    let target = resolve_target_rental(target, &api_client, true).await?;

    debug!("Opening SSH connection to rental: {}", target);

    // Get rental status from API which includes SSH credentials
    let rental_status = api_client
        .get_rental_status(&target)
        .await
        .map_err(|e| -> CliError {
            let report = match e {
                ApiError::NotFound { .. } => eyre!("Rental '{}' not found", target)
                    .suggestion("Try 'basilica ps' to see your active rentals"),
                _ => eyre!(e).suggestion("Check your internet connection and try again"),
            };
            CliError::Internal(report)
        })?;

    // Extract SSH credentials from response
    let ssh_credentials = rental_status.ssh_credentials.ok_or_else(|| {
        eyre!("SSH credentials not available")
            .wrap_err(format!(
                "The rental '{}' was created without SSH access",
                target
            ))
            .note("Rentals created with --no-ssh flag cannot be accessed via SSH")
            .note("Create a new rental without --no-ssh to enable SSH access")
    })?;

    // Parse SSH credentials
    let (host, port, username) = parse_ssh_credentials(&ssh_credentials)?;
    let ssh_access = SshAccess {
        host,
        port,
        username,
    };

    // Use SSH client to handle connection with options
    let ssh_client = SshClient::new(&config.ssh)?;

    // Open interactive session with port forwarding options
    ssh_client
        .interactive_session_with_options(&ssh_access, &options)
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

            // Resolve target rental with SSH requirement
            let selected_rental = resolve_target_rental(None, &api_client, true).await
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

    // Get rental status from API which includes SSH credentials
    let rental_status =
        api_client
            .get_rental_status(&rental_id)
            .await
            .map_err(|e| -> CliError {
                let report = match e {
                    ApiError::NotFound { .. } => eyre!("Rental '{}' not found", rental_id)
                        .suggestion("Try 'basilica ps' to see your active rentals"),
                    _ => eyre!(e).suggestion("Check your internet connection and try again"),
                };
                CliError::Internal(report)
            })?;

    // Extract SSH credentials from response
    let ssh_credentials = rental_status.ssh_credentials.ok_or_else(|| {
        eyre!("SSH credentials not available")
            .wrap_err(format!(
                "The rental '{}' was created without SSH access",
                rental_id
            ))
            .note("Rentals created with --no-ssh flag cannot be accessed via SSH")
            .note("Create a new rental without --no-ssh to enable SSH access")
    })?;

    // Parse SSH credentials
    let (host, port, username) = parse_ssh_credentials(&ssh_credentials)?;
    let ssh_access = SshAccess {
        host,
        port,
        username,
    };

    // Use SSH client for file transfer
    let ssh_client = SshClient::new(&config.ssh).map_err(|e| eyre!(e))?;

    if is_upload {
        ssh_client
            .upload_file(&ssh_access, &local_path, &remote_path)
            .await?;
        Ok(())
    } else {
        ssh_client
            .download_file(&ssh_access, &remote_path, &local_path)
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
    const MAX_WAIT_TIME: Duration = Duration::from_secs(60);
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
                        spinner.set_message(format!(
                            "Rental is pending... ({}s elapsed)",
                            start_time.elapsed().as_secs()
                        ));
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

fn load_ssh_public_key(key_path: &Option<PathBuf>, config: &CliConfig) -> Result<String, CliError> {
    let path = key_path.as_ref().unwrap_or(&config.ssh.key_path);

    std::fs::read_to_string(path).map_err(|_| {
        eyre!(
            "SSH key not found at: {}. Run \'basilica login\' to generate keys",
            path.display().to_string()
        )
        .into()
    })
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
