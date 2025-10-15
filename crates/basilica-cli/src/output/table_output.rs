//! Table formatting for CLI output

use crate::error::Result;
use basilica_api::country_mapping::get_country_name_from_code;
use basilica_common::LocationProfile;
use basilica_sdk::{
    types::{
        ApiKeyInfo, ApiRentalListItem, BalanceResponse, BillingPackageInfo, GpuSpec,
        ListDepositsResponse, NodeDetails, PackagesResponse, RentalStatusResponse,
        RentalUsageResponse, UsageHistoryResponse,
    },
    AvailableNode,
};
use basilica_validator::gpu::GpuCategory;
use chrono::{DateTime, Local};
use console::style;
use rust_decimal::Decimal;
use std::{collections::HashMap, str::FromStr};
use tabled::{builder::Builder, settings::Style, Table, Tabled};

/// Format RFC3339 timestamp to YY-MM-DD HH:MM:SS format
fn format_timestamp(timestamp: &str) -> String {
    DateTime::parse_from_rfc3339(timestamp)
        .ok()
        .map(|dt| {
            let local_dt = dt.with_timezone(&Local);
            local_dt.format("%y-%m-%d %H:%M:%S").to_string()
        })
        .unwrap_or_else(|| timestamp.to_string())
}

/// Display nodes in table format
pub fn display_nodes(nodes: &[NodeDetails]) -> Result<()> {
    #[derive(Tabled)]
    struct NodeRow {
        #[tabled(rename = "ID")]
        id: String,
        // #[tabled(rename = "GPUs")]
        // gpus: String,
        // #[tabled(rename = "CPU")]
        // cpu: String,
        // #[tabled(rename = "Memory")]
        // memory: String,
        #[tabled(rename = "Location")]
        location: String,
    }

    let rows: Vec<NodeRow> = nodes
        .iter()
        .map(|node| {
            // let gpu_info = if node.gpu_specs.is_empty() {
            //     "None".to_string()
            // } else {
            //     format!(
            //         "{} x {} ({}GB)",
            //         node.gpu_specs.len(),
            //         node.gpu_specs[0].name,
            //         node.gpu_specs[0].memory_gb
            //     )
            // };

            NodeRow {
                id: node.id.clone(),
                // gpus: gpu_info,
                // cpu: format!("{} cores", node.cpu_specs.cores),
                // memory: format!("{}GB", node.cpu_specs.memory_gb),
                location: node
                    .location
                    .clone()
                    .unwrap_or_else(|| "Unknown".to_string()),
            }
        })
        .collect();

    let mut table = Table::new(rows);
    table.with(Style::modern());
    println!("{table}");

    Ok(())
}

/// Display active rentals in table format (for RentalStatusResponse - legacy)
pub fn display_rentals(rentals: &[RentalStatusResponse]) -> Result<()> {
    #[derive(Tabled)]
    struct RentalRow {
        #[tabled(rename = "Rental ID")]
        rental_id: String,
        #[tabled(rename = "Status")]
        status: String,
        #[tabled(rename = "Node")]
        node: String,
        #[tabled(rename = "Created")]
        created: String,
    }

    let rows: Vec<RentalRow> = rentals
        .iter()
        .map(|rental| RentalRow {
            rental_id: rental.rental_id.clone(),
            status: format!("{:?}", rental.status),
            node: rental.node.id.clone(),
            created: rental.created_at.format("%y-%m-%d %H:%M:%S").to_string(),
        })
        .collect();

    let mut table = Table::new(rows);
    table.with(Style::modern());
    println!("{table}");

    Ok(())
}

/// Display rental items in table format
pub fn display_rental_items(
    rentals: &[ApiRentalListItem],
    show_standard: bool,
    show_ids: bool,
) -> Result<()> {
    if show_ids {
        // Detailed view with IDs
        #[derive(Tabled)]
        struct DetailedRentalRowWithIds {
            #[tabled(rename = "RENTAL ID")]
            rental_id: String,
            #[tabled(rename = "NODE ID")]
            node_id: String,
            #[tabled(rename = "GPU")]
            gpu: String,
            #[tabled(rename = "State")]
            state: String,
            #[tabled(rename = "SSH")]
            ssh: String,
            #[tabled(rename = "Ports (Host → Container)")]
            ports: String,
            #[tabled(rename = "Image")]
            image: String,
            #[tabled(rename = "CPU")]
            cpu: String,
            #[tabled(rename = "RAM")]
            ram: String,
            #[tabled(rename = "Location")]
            location: String,
            #[tabled(rename = "Created")]
            created: String,
        }

        let rows: Vec<DetailedRentalRowWithIds> = rentals
            .iter()
            .map(|rental| {
                // Extract the node ID (remove miner prefix if present)
                let node_id = rental
                    .node_id
                    .split_once("__")
                    .map(|(_, id)| id)
                    .unwrap_or(&rental.node_id)
                    .to_string();

                // Format GPU info from specs
                let gpu = format_gpu_info(&rental.gpu_specs, true);

                // Format CPU info
                let cpu = rental
                    .cpu_specs
                    .as_ref()
                    .map(|cpu| format!("{} ({} cores)", cpu.model, cpu.cores))
                    .unwrap_or_else(|| "Unknown".to_string());

                // Format RAM info
                let ram = rental
                    .cpu_specs
                    .as_ref()
                    .map(|cpu| format!("{}GB", cpu.memory_gb))
                    .unwrap_or_else(|| "Unknown".to_string());

                // Format location
                let location = rental
                    .location
                    .as_ref()
                    .and_then(|loc| LocationProfile::from_str(loc).ok())
                    .map(|profile| profile.to_string())
                    .unwrap_or_else(|| "Unknown".to_string());

                // Format SSH availability
                let ssh = if rental.has_ssh { "✓" } else { "✗" };

                // Format port mappings (show all ports in detailed view)
                let ports = format_port_mappings(&rental.port_mappings, None);

                DetailedRentalRowWithIds {
                    rental_id: rental.rental_id.clone(),
                    node_id,
                    gpu,
                    state: rental.state.to_string(),
                    ssh: ssh.to_string(),
                    ports,
                    image: rental.container_image.clone(),
                    cpu,
                    ram,
                    location,
                    created: format_timestamp(&rental.created_at),
                }
            })
            .collect();

        let mut table = Table::new(rows);
        table.with(Style::modern());
        println!("{table}");
    } else if show_standard {
        // Standard view with full information (no IDs)
        #[derive(Tabled)]
        struct DetailedRentalRow {
            #[tabled(rename = "GPU")]
            gpu: String,
            #[tabled(rename = "State")]
            state: String,
            #[tabled(rename = "SSH")]
            ssh: String,
            #[tabled(rename = "Ports (Host → Container)")]
            ports: String,
            #[tabled(rename = "Image")]
            image: String,
            #[tabled(rename = "CPU")]
            cpu: String,
            #[tabled(rename = "RAM")]
            ram: String,
            #[tabled(rename = "Location")]
            location: String,
            #[tabled(rename = "Created")]
            created: String,
        }

        let rows: Vec<DetailedRentalRow> = rentals
            .iter()
            .map(|rental| {
                // Format GPU info from specs
                let gpu = format_gpu_info(&rental.gpu_specs, true);

                // Format CPU info
                let cpu = rental
                    .cpu_specs
                    .as_ref()
                    .map(|cpu| format!("{} ({} cores)", cpu.model, cpu.cores))
                    .unwrap_or_else(|| "Unknown".to_string());

                // Format RAM info
                let ram = rental
                    .cpu_specs
                    .as_ref()
                    .map(|cpu| format!("{}GB", cpu.memory_gb))
                    .unwrap_or_else(|| "Unknown".to_string());

                // Format location
                let location = rental
                    .location
                    .as_ref()
                    .and_then(|loc| LocationProfile::from_str(loc).ok())
                    .map(|profile| profile.to_string())
                    .unwrap_or_else(|| "Unknown".to_string());

                // Format SSH availability
                let ssh = if rental.has_ssh { "✓" } else { "✗" };

                // Format port mappings (show up to 2-3 ports)
                let ports = format_port_mappings(&rental.port_mappings, Some(2));

                DetailedRentalRow {
                    gpu,
                    state: rental.state.to_string(),
                    ssh: ssh.to_string(),
                    ports,
                    image: rental.container_image.clone(),
                    cpu,
                    ram,
                    location,
                    created: format_timestamp(&rental.created_at),
                }
            })
            .collect();

        let mut table = Table::new(rows);
        table.with(Style::modern());
        println!("{table}");
    } else {
        // Compact view with essential information
        #[derive(Tabled)]
        struct CompactRentalRow {
            #[tabled(rename = "GPU")]
            gpu: String,
            #[tabled(rename = "State")]
            state: String,
            #[tabled(rename = "SSH")]
            ssh: String,
            #[tabled(rename = "Created")]
            created: String,
        }

        let rows: Vec<CompactRentalRow> = rentals
            .iter()
            .map(|rental| {
                // Format GPU info from specs
                let gpu = format_gpu_info(&rental.gpu_specs, false);

                // Format SSH availability
                let ssh = if rental.has_ssh { "✓" } else { "✗" };

                CompactRentalRow {
                    gpu,
                    state: rental.state.to_string(),
                    ssh: ssh.to_string(),
                    created: format_timestamp(&rental.created_at),
                }
            })
            .collect();

        let mut table = Table::new(rows);
        table.with(Style::modern());
        println!("{table}");
    }

    Ok(())
}

/// Helper function to format port mappings
fn format_port_mappings(
    port_mappings: &Option<Vec<basilica_validator::rental::PortMapping>>,
    max_count: Option<usize>,
) -> String {
    match port_mappings {
        None => "-".to_string(),
        Some(ports) if ports.is_empty() => "-".to_string(),
        Some(ports) => {
            let formatted_ports: Vec<String> = ports
                .iter()
                .map(|p| format!("{}→{}", p.host_port, p.container_port))
                .collect();

            match max_count {
                Some(max) if formatted_ports.len() > max => {
                    let shown = &formatted_ports[..max];
                    let remaining = formatted_ports.len() - max;
                    format!("{}, +{} more", shown.join(", "), remaining)
                }
                _ => formatted_ports.join(", "),
            }
        }
    }
}

/// Helper function to format GPU info
fn format_gpu_info(gpu_specs: &[GpuSpec], detailed: bool) -> String {
    if gpu_specs.is_empty() {
        return "Unknown".to_string();
    }

    // Check if all GPUs are the same
    let first_gpu = &gpu_specs[0];
    let all_same = gpu_specs
        .iter()
        .all(|g| g.name == first_gpu.name && g.memory_gb == first_gpu.memory_gb);

    if all_same {
        let gpu_display_name = if detailed {
            // Detailed mode: show full GPU name
            first_gpu.name.clone()
        } else {
            // Compact mode: show categorized name
            GpuCategory::from_str(&first_gpu.name).unwrap().to_string()
        };

        if gpu_specs.len() > 1 {
            format!("{}x {}", gpu_specs.len(), gpu_display_name)
        } else {
            format!("1x {}", gpu_display_name)
        }
    } else {
        // List each GPU
        gpu_specs
            .iter()
            .map(|g| {
                if detailed {
                    g.name.clone()
                } else {
                    GpuCategory::from_str(&g.name).unwrap().to_string()
                }
            })
            .collect::<Vec<_>>()
            .join(", ")
    }
}

/// Display configuration in table format
pub fn display_config(config: &HashMap<String, String>) -> Result<()> {
    #[derive(Tabled)]
    struct ConfigRow {
        #[tabled(rename = "Key")]
        key: String,
        #[tabled(rename = "Value")]
        value: String,
    }

    let mut rows: Vec<ConfigRow> = config
        .iter()
        .map(|(key, value)| ConfigRow {
            key: key.clone(),
            value: value.clone(),
        })
        .collect();

    rows.sort_by(|a, b| a.key.cmp(&b.key));

    let mut table = Table::new(rows);
    table.with(Style::modern());
    println!("{table}");

    Ok(())
}

/// Display available nodes in compact format (grouped by location and GPU type)
pub fn display_available_nodes_compact(nodes: &[AvailableNode]) -> Result<()> {
    if nodes.is_empty() {
        println!("No available nodes found matching the specified criteria.");
        return Ok(());
    }

    // Group nodes by country (extracted from location) and GPU configuration
    let mut country_groups: HashMap<String, HashMap<String, Vec<&AvailableNode>>> = HashMap::new();

    for node in nodes {
        // Parse location string using LocationProfile to extract country
        let country = node
            .node
            .location
            .as_ref()
            .and_then(|loc| {
                LocationProfile::from_str(loc)
                    .ok()
                    .and_then(|profile| profile.country)
            })
            .unwrap_or_else(|| "Unknown".to_string());

        let gpu_key = if node.node.gpu_specs.is_empty() {
            "No GPU".to_string()
        } else {
            let gpu = &node.node.gpu_specs[0];
            let category = GpuCategory::from_str(&gpu.name).unwrap();
            let gpu_count = node.node.gpu_specs.len();
            format!("{}x {}", gpu_count, category)
        };

        country_groups
            .entry(country)
            .or_default()
            .entry(gpu_key)
            .or_default()
            .push(node);
    }

    // Sort countries for consistent display
    let mut sorted_countries: Vec<_> = country_groups.keys().cloned().collect();
    sorted_countries.sort();

    println!("Available GPU Instances by Country\n");

    for country in sorted_countries {
        // Print country header with full name
        let country_display = get_country_name_from_code(&country);
        println!("{}", country_display);

        #[derive(Tabled)]
        struct CompactRow {
            #[tabled(rename = "GPU TYPE")]
            gpu_type: String,
            #[tabled(rename = "AVAILABLE")]
            available: String,
        }

        let gpu_groups = country_groups.get(&country).unwrap();
        let mut rows: Vec<CompactRow> = Vec::new();

        // Sort GPU configurations for consistent display
        let mut sorted_gpu_configs: Vec<_> = gpu_groups.keys().cloned().collect();
        sorted_gpu_configs.sort();

        for gpu_config in sorted_gpu_configs {
            let nodes_in_group = gpu_groups.get(&gpu_config).unwrap();
            let count = nodes_in_group.len();

            rows.push(CompactRow {
                gpu_type: gpu_config.clone(),
                available: count.to_string(),
            });
        }

        let mut table = Table::new(rows);
        table.with(Style::modern());
        println!("{}", table);
        println!();
    }

    let total_count = nodes.len();
    println!("Total available nodes: {}", total_count);

    Ok(())
}

/// Display API keys in table format
pub fn display_api_keys(keys: &[ApiKeyInfo]) -> Result<()> {
    #[derive(Tabled)]
    struct ApiKeyRow {
        #[tabled(rename = "Name")]
        name: String,
        #[tabled(rename = "Created")]
        created: String,
        #[tabled(rename = "Last Used")]
        last_used: String,
    }

    let rows: Vec<ApiKeyRow> = keys
        .iter()
        .map(|key| ApiKeyRow {
            name: key.name.clone(),
            created: format_timestamp(&key.created_at.to_rfc3339()),
            last_used: key
                .last_used_at
                .map(|dt| format_timestamp(&dt.to_rfc3339()))
                .unwrap_or_else(|| "Never".to_string()),
        })
        .collect();

    let mut table = Table::new(rows);
    table.with(Style::modern());
    println!("{table}");

    Ok(())
}

/// Helper function to format GPU info for an node
fn format_node_gpu_info(node: &AvailableNode, show_full_gpu_names: bool) -> String {
    if node.node.gpu_specs.is_empty() {
        "No GPU".to_string()
    } else if node.node.gpu_specs.len() == 1 {
        // Single GPU
        let gpu = &node.node.gpu_specs[0];
        let gpu_display_name = if show_full_gpu_names {
            gpu.name.clone()
        } else {
            GpuCategory::from_str(&gpu.name).unwrap().to_string()
        };
        format!("1x {}", gpu_display_name)
    } else {
        // Multiple GPUs - check if they're all the same model
        let first_gpu = &node.node.gpu_specs[0];
        let all_same = node
            .node
            .gpu_specs
            .iter()
            .all(|g| g.name == first_gpu.name && g.memory_gb == first_gpu.memory_gb);

        if all_same {
            // All GPUs are identical - use count prefix format
            let gpu_display_name = if show_full_gpu_names {
                first_gpu.name.clone()
            } else {
                GpuCategory::from_str(&first_gpu.name).unwrap().to_string()
            };
            format!("{}x {}", node.node.gpu_specs.len(), gpu_display_name)
        } else {
            // Different GPU models - list them individually
            let gpu_names: Vec<String> = node
                .node
                .gpu_specs
                .iter()
                .map(|g| {
                    if show_full_gpu_names {
                        g.name.clone()
                    } else {
                        GpuCategory::from_str(&g.name).unwrap().to_string()
                    }
                })
                .collect();
            gpu_names.join(", ")
        }
    }
}

/// Helper function to format location
fn format_node_location(location: &Option<String>) -> String {
    location
        .as_ref()
        .map(|loc| {
            LocationProfile::from_str(loc)
                .ok()
                .map(|profile| profile.to_string())
                .unwrap_or_else(|| loc.clone())
        })
        .unwrap_or_else(|| "Unknown".to_string())
}

/// Display available nodes in detailed format (individual nodes)
pub fn display_available_nodes_detailed(
    nodes: &[AvailableNode],
    show_full_gpu_names: bool,
    show_ids: bool,
) -> Result<()> {
    if nodes.is_empty() {
        println!("No available nodes found matching the specified criteria.");
        return Ok(());
    }

    // Different structs based on whether we show IDs
    if show_ids {
        #[derive(Tabled)]
        struct DetailedNodeRowWithId {
            #[tabled(rename = "NODE ID")]
            node_id: String,
            #[tabled(rename = "GPU")]
            gpu_info: String,
            #[tabled(rename = "CPU")]
            cpu: String,
            #[tabled(rename = "RAM")]
            ram: String,
            #[tabled(rename = "Location")]
            location: String,
        }

        let rows: Vec<DetailedNodeRowWithId> = nodes
            .iter()
            .map(|node| {
                // Extract the node ID (remove miner prefix if present)
                let node_id = node
                    .node
                    .id
                    .split_once("__")
                    .map(|(_, id)| id)
                    .unwrap_or(&node.node.id)
                    .to_string();

                DetailedNodeRowWithId {
                    node_id,
                    gpu_info: format_node_gpu_info(node, show_full_gpu_names),
                    cpu: format!(
                        "{} ({} cores)",
                        node.node.cpu_specs.model, node.node.cpu_specs.cores
                    ),
                    ram: format!("{}GB", node.node.cpu_specs.memory_gb),
                    location: format_node_location(&node.node.location),
                }
            })
            .collect();

        let mut table = Table::new(rows);
        table.with(Style::modern());
        println!("{}", table);
    } else {
        #[derive(Tabled)]
        struct DetailedNodeRow {
            #[tabled(rename = "GPU")]
            gpu_info: String,
            #[tabled(rename = "CPU")]
            cpu: String,
            #[tabled(rename = "RAM")]
            ram: String,
            #[tabled(rename = "Location")]
            location: String,
        }

        let rows: Vec<DetailedNodeRow> = nodes
            .iter()
            .map(|node| DetailedNodeRow {
                gpu_info: format_node_gpu_info(node, show_full_gpu_names),
                cpu: format!(
                    "{} ({} cores)",
                    node.node.cpu_specs.model, node.node.cpu_specs.cores
                ),
                ram: format!("{}GB", node.node.cpu_specs.memory_gb),
                location: format_node_location(&node.node.location),
            })
            .collect();

        let mut table = Table::new(rows);
        table.with(Style::modern());
        println!("{}", table);
    }

    println!("\nTotal available nodes: {}", nodes.len());

    Ok(())
}

/// Display deposits history in table format
pub fn display_deposits(response: &ListDepositsResponse) -> Result<()> {
    println!();
    println!("{}", style("# Deposit History").dim());
    println!();

    let mut builder = Builder::default();

    // Add header
    builder.push_record(["Date (UTC)", "TAO", "Tx Hash", "Conf", "Block", "Status"]);

    let mut total_tao = 0.0;

    for deposit in &response.deposits {
        let amount_tao: f64 = deposit.amount_tao.parse().unwrap_or(0.0);
        total_tao += amount_tao;

        // Format date
        let date = deposit.observed_at.format("%Y-%m-%d %H:%M:%S").to_string();

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

        // Format status
        let status = if deposit.credited_at.is_some() {
            "Credited"
        } else if deposit.finalized_at.is_some() {
            "Finalized"
        } else {
            "Pending"
        };

        builder.push_record([
            date.as_str(),
            &format!("{:.3}", amount_tao),
            tx_hash.as_str(),
            confirmations.as_str(),
            &deposit.block_number.to_string(),
            status,
        ]);
    }

    let mut table = builder.build();
    table.with(Style::modern());
    println!("{}", table);

    // Display totals
    println!();
    println!("{}:", style("Total Deposits").bold());
    println!("  {} TAO", style(format!("{:.3}", total_tao)).green());

    Ok(())
}

/// Display pricing table for all GPU types
pub fn display_pricing_table(
    packages: &PackagesResponse,
    balance: Option<&BalanceResponse>,
) -> Result<()> {
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
            .ok()
            .unwrap_or_default();

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
    let mut table = Table::new(&rows);
    table.with(Style::modern());
    println!("{}", table);
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
pub fn display_gpu_pricing(
    package: &BillingPackageInfo,
    hours: Option<u32>,
    balance: Option<&BalanceResponse>,
) -> Result<()> {
    let hourly_rate = package
        .hourly_rate
        .parse::<Decimal>()
        .ok()
        .unwrap_or_default();

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
            .ok()
            .unwrap_or_default();

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

/// Display detailed usage for a specific rental
pub fn display_rental_usage_detail(usage: &RentalUsageResponse) -> Result<()> {
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
        let mut table = Table::new(&rows);
        table.with(Style::modern());
        println!("{}", table);
        println!();
    } else {
        println!("{}", style("No usage data points available").yellow());
        println!();
    }

    Ok(())
}

/// Display usage history list
pub fn display_usage_history(history: &UsageHistoryResponse) -> Result<()> {
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
    let mut table = Table::new(&rows);
    table.with(Style::modern());
    println!("{}", table);
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
