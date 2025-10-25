//! CLI upgrade handler using self_update crate

use crate::error::CliError;
use color_eyre::eyre::eyre;
use console::style;
use self_update::cargo_crate_version;
use std::env;
use std::fs;

/// Handle the upgrade command
/// Note: This function uses blocking operations from self_update crate
pub async fn handle_upgrade(version: Option<String>) -> Result<(), CliError> {
    // Run the blocking upgrade code in a tokio blocking task to avoid runtime conflicts
    tokio::task::spawn_blocking(move || handle_upgrade_blocking(version))
        .await
        .map_err(|e| CliError::Internal(eyre!("Failed to execute upgrade task: {}", e)))?
}

/// Blocking version of the upgrade handler
fn handle_upgrade_blocking(version: Option<String>) -> Result<(), CliError> {
    let current_version = cargo_crate_version!();

    // Determine target triple based on OS and architecture
    let target = get_target_triple()?;

    println!("Current version: {}", style(current_version).cyan());
    println!("Checking for updates...");

    // Fetch release information
    let releases = self_update::backends::github::ReleaseList::configure()
        .repo_owner("one-covenant")
        .repo_name("basilica")
        .build()
        .map_err(|e| CliError::Internal(eyre!("Failed to configure release list: {}", e)))?
        .fetch()
        .map_err(|e| CliError::Internal(eyre!("Failed to fetch releases from GitHub: {}", e)))?;

    // Filter CLI releases
    let cli_releases: Vec<_> = releases
        .iter()
        .filter(|r| r.version.starts_with("basilica-cli-v"))
        .collect();

    if cli_releases.is_empty() {
        return Err(CliError::Internal(eyre!("No CLI releases found")));
    }

    // Determine target version
    let target_release = if let Some(ref ver) = version {
        let target_tag = format!("basilica-cli-v{}", ver.trim_start_matches('v'));
        cli_releases
            .iter()
            .find(|r| r.version == target_tag)
            .ok_or_else(|| CliError::Internal(eyre!("Version {} not found", ver)))?
    } else {
        cli_releases[0] // Latest version
    };

    let target_version = target_release
        .version
        .trim_start_matches("basilica-cli-v")
        .trim_start_matches('v');

    // Check if already on target version
    if target_version == current_version {
        println!("{}", style("Already up to date!").green());
        return Ok(());
    }

    println!(
        "Updating from {} to {}...",
        style(current_version).cyan(),
        style(target_version).green().bold()
    );

    // Find the asset for our platform
    let asset_name = format!("basilica-{}", target);
    let asset = target_release
        .assets
        .iter()
        .find(|a| a.name == asset_name)
        .ok_or_else(|| {
            CliError::Internal(eyre!("No release asset found for platform: {}", asset_name))
        })?;

    // Download the binary
    println!("Downloading {}...", asset_name);
    let tmp_dir = std::env::temp_dir();
    let tmp_tarball = tmp_dir.join(format!("{}.download", asset_name));

    let tmp_file = fs::File::create(&tmp_tarball)
        .map_err(|e| CliError::Internal(eyre!("Failed to create temp file: {}", e)))?;

    self_update::Download::from_url(&asset.download_url)
        .show_progress(true)
        .download_to(tmp_file)
        .map_err(|e| CliError::Internal(eyre!("Download failed: {}", e)))?;

    // Get current binary path
    let current_exe = std::env::current_exe()
        .map_err(|e| CliError::Internal(eyre!("Failed to get current executable path: {}", e)))?;

    // Replace the current binary
    println!("Installing update...");
    // Make executable
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut perms = fs::metadata(&tmp_tarball)
            .map_err(|e| CliError::Internal(eyre!("Failed to get file metadata: {}", e)))?
            .permissions();
        perms.set_mode(0o755);
        fs::set_permissions(&tmp_tarball, perms)
            .map_err(|e| CliError::Internal(eyre!("Failed to set permissions: {}", e)))?;
    }

    // Copy the file to replace the current exe
    fs::copy(&tmp_tarball, &current_exe).map_err(|e| {
        CliError::Internal(eyre!(
            "Failed to replace binary: {}. You may need elevated permissions.",
            e
        ))
    })?;

    // Clean up
    let _ = fs::remove_file(&tmp_tarball);

    println!(
        "\n{} Updated to version {}",
        style("✓").green().bold(),
        style(target_version).green().bold()
    );
    println!(
        "\nRun {} to verify the new version",
        style("basilica --version").cyan()
    );

    Ok(())
}

/// Get the target triple for the current platform
fn get_target_triple() -> Result<String, CliError> {
    let os = env::consts::OS;
    let arch = env::consts::ARCH;

    // Map Rust's arch names to our release naming convention
    let target_arch = match arch {
        "x86_64" => "amd64",
        "aarch64" => "arm64",
        other => {
            return Err(CliError::Internal(eyre!(
                "Unsupported architecture: {}",
                other
            )))
        }
    };

    // Map Rust's OS names to our release naming convention
    let target_os = match os {
        "linux" => "linux",
        "macos" => "darwin",
        other => {
            return Err(CliError::Internal(eyre!(
                "Unsupported operating system: {}",
                other
            )))
        }
    };

    // Our binaries are named: basilica-{os}-{arch}
    Ok(format!("{}-{}", target_os, target_arch))
}
