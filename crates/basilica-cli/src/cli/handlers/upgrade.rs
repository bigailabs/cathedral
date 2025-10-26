//! CLI upgrade handler using self_update crate

use crate::error::CliError;
use color_eyre::eyre::eyre;
use console::style;
use self_update::cargo_crate_version;
use semver::Version;

/// Minimum supported version for auto-updates (first release with new CI binary format)
pub const MIN_SUPPORTED_VERSION: &str = "0.5.5";

/// Handle the upgrade command
/// Note: This function uses blocking operations from self_update crate
pub fn handle_upgrade(version: Option<String>, dry_run: bool) -> Result<(), CliError> {
    let current_version = cargo_crate_version!();

    // Validate version if specified
    if let Some(ref ver) = version {
        let target_version = ver.trim_start_matches('v');

        // Check if the version is supported
        let min_version =
            Version::parse(MIN_SUPPORTED_VERSION).expect("MIN_SUPPORTED_VERSION is valid");
        let requested_version = Version::parse(target_version).map_err(|e| {
            CliError::Internal(eyre!("Invalid version format '{}': {}", target_version, e))
        })?;

        if requested_version < min_version {
            return Err(CliError::Internal(eyre!(
                "Version {} is not supported for auto-updates.",
                target_version
            )));
        }
    }

    // Handle dry-run mode: check for updates without installing
    if dry_run {
        return handle_dry_run(current_version);
    }

    println!("Current version: {}", style(current_version).cyan());
    println!("Checking for updates...");

    let target_tag = if let Some(ref ver) = version {
        format!("basilica-cli-v{}", ver.trim_start_matches('v'))
    } else {
        let releases = self_update::backends::github::ReleaseList::configure()
            .repo_owner("one-covenant")
            .repo_name("basilica")
            .build()
            .map_err(|e| CliError::Internal(eyre!("Failed to configure release list: {}", e)))?
            .fetch()
            .map_err(|e| {
                CliError::Internal(eyre!("Failed to fetch releases from GitHub: {}", e))
            })?;

        let min_version =
            Version::parse(MIN_SUPPORTED_VERSION).expect("MIN_SUPPORTED_VERSION is valid");
        let current = Version::parse(current_version)
            .map_err(|e| CliError::Internal(eyre!("Invalid current version: {}", e)))?;

        let latest_tag = releases
            .iter()
            .filter_map(|r| {
                if !r.version.starts_with("basilica-cli-v") {
                    return None;
                }

                let version_str = r
                    .version
                    .trim_start_matches("basilica-cli-v")
                    .trim_start_matches('v');

                if let Ok(v) = Version::parse(version_str) {
                    if v >= min_version && v > current {
                        Some(r.version.clone())
                    } else {
                        None
                    }
                } else {
                    None
                }
            })
            .next();

        match latest_tag {
            Some(tag) => tag,
            None => {
                println!("{}", style("Already up to date!").green());
                println!("Current version: {}", style(current_version).cyan());
                return Ok(());
            }
        }
    };

    let mut update_builder = self_update::backends::github::Update::configure();

    update_builder
        .repo_owner("one-covenant")
        .repo_name("basilica")
        .bin_name("basilica")
        .current_version(current_version)
        .show_download_progress(true)
        .no_confirm(true)
        .target_version_tag(&target_tag);

    // Build and execute the update
    let status = update_builder
        .build()
        .map_err(|e| CliError::Internal(eyre!("Failed to configure updater: {}", e)))?
        .update()
        .map_err(|e| {
            // Provide helpful error messages for common failures
            let error_msg = format!("{}", e);
            if error_msg.contains("permission") || error_msg.contains("Permission") {
                CliError::Internal(eyre!(
                    "Failed to replace binary: {}. You may need elevated permissions.\n\
                     Try running: sudo -E basilica upgrade",
                    e
                ))
            } else if error_msg.contains("not found") || error_msg.contains("404") {
                CliError::Internal(eyre!(
                    "Release not found. Please check that the version exists.\n\
                     View available releases: https://github.com/one-covenant/basilica/releases"
                ))
            } else if error_msg.contains("target") || error_msg.contains("asset") {
                CliError::Internal(eyre!(
                    "No binary available for your platform.\n\
                     Supported platforms: Linux (x86_64, aarch64), macOS (x86_64, aarch64)\n\
                     Error: {}",
                    e
                ))
            } else {
                CliError::Internal(eyre!("Update failed: {}", e))
            }
        })?;

    // Display results
    match status {
        self_update::Status::UpToDate(v) => {
            println!("{}", style("Already up to date!").green());
            println!("Current version: {}", style(v).cyan());
        }
        self_update::Status::Updated(v) => {
            println!(
                "\n{} Updated to version {}",
                style("✓").green().bold(),
                style(v).green().bold()
            );
            println!(
                "\nRun {} to verify the new version",
                style("basilica --version").cyan()
            );
        }
    }

    Ok(())
}

/// Handle dry-run mode: check for updates without installing
fn handle_dry_run(current_version: &str) -> Result<(), CliError> {
    println!("Current version: {}", style(current_version).cyan());
    println!("Checking for updates...");

    let releases = self_update::backends::github::ReleaseList::configure()
        .repo_owner("one-covenant")
        .repo_name("basilica")
        .build()
        .map_err(|e| CliError::Internal(eyre!("Failed to configure release list: {}", e)))?
        .fetch()
        .map_err(|e| CliError::Internal(eyre!("Failed to fetch releases from GitHub: {}", e)))?;

    let min_version =
        Version::parse(MIN_SUPPORTED_VERSION).expect("MIN_SUPPORTED_VERSION is valid");
    let current = Version::parse(current_version).ok();

    // Filter releases that match our tag pattern (basilica-cli-v*) and are supported
    let cli_releases: Vec<_> = releases
        .iter()
        .filter(|r| {
            if !r.version.starts_with("basilica-cli-v") {
                return false;
            }

            let version = r
                .version
                .trim_start_matches("basilica-cli-v")
                .trim_start_matches('v');

            if let Ok(v) = Version::parse(version) {
                if v < min_version {
                    return false;
                }

                // Only include versions newer than or equal to current
                if let Some(ref cur) = current {
                    v >= *cur
                } else {
                    true
                }
            } else {
                false
            }
        })
        .collect();

    if cli_releases.is_empty() {
        println!("No newer CLI releases found");
        return Ok(());
    }

    // Get latest release
    let latest = cli_releases[0];
    let latest_version = latest
        .version
        .trim_start_matches("basilica-cli-v")
        .trim_start_matches('v');

    if latest_version == current_version {
        println!("{}", style("Already up to date!").green());
    } else {
        println!(
            "Latest version available: {}",
            style(latest_version).green()
        );
        println!(
            "\nRun {} to upgrade",
            style("basilica upgrade").cyan().bold()
        );
    }

    Ok(())
}
