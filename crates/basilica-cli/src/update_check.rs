//! Background update checker for the Basilica CLI
//!
//! Checks for new versions once per day and displays a notification if available.
//! Inspired by Deno's upgrade notification system.

use crate::cli::handlers::upgrade::MIN_SUPPORTED_VERSION;
use chrono::{DateTime, Duration, Utc};
use console::style;
use etcetera::{choose_base_strategy, BaseStrategy};
use self_update::cargo_crate_version;
use semver::Version;
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::IsTerminal;
use std::path::PathBuf;

const UPDATE_CHECK_FILE: &str = "update_check.json";
const CHECK_INTERVAL_HOURS: i64 = 24;

/// Check if a version is supported for auto-updates
fn is_version_supported(version: &str) -> bool {
    let min_version = match Version::parse(MIN_SUPPORTED_VERSION) {
        Ok(v) => v,
        Err(_) => return false,
    };

    let check_version = match Version::parse(version) {
        Ok(v) => v,
        Err(_) => return false,
    };

    check_version >= min_version
}

#[derive(Debug, Serialize, Deserialize)]
struct UpdateCheckCache {
    last_check: DateTime<Utc>,
    latest_version: Option<String>,
    last_prompt: Option<DateTime<Utc>>,
}

/// Check the cache and show update notification if appropriate.
/// This should be called at CLI startup, before executing the command.
pub fn check_cache_and_show_notification() {
    // Skip if explicitly disabled
    if std::env::var("BASILICA_NO_UPDATE_CHECK").is_ok() {
        return;
    }

    // Skip if not running in a TTY (avoid polluting scripts/CI output)
    if !std::io::stdout().is_terminal() {
        return;
    }

    let cache_path = match get_cache_path() {
        Ok(path) => path,
        Err(_) => return, // Silently fail if we can't determine cache path
    };

    let mut cache = match load_cache(&cache_path) {
        Ok(Some(c)) => c,
        _ => return, // No cache or error reading it
    };

    // Check if we have a newer version available
    if let Some(latest_version) = &cache.latest_version {
        let current_version = cargo_crate_version!();

        // Only show if the latest version is different from current
        if latest_version != current_version {
            // Show the notification
            eprintln!(
                "{} {} → {}",
                style("A new version of Basilica is available:").yellow(),
                style(current_version).cyan(),
                style(latest_version).green().bold()
            );
            eprintln!(
                "{} {}",
                style("Run").dim(),
                style("basilica upgrade").cyan().bold()
            );
            eprintln!(); // Empty line for spacing

            // Update last_prompt time
            cache.last_prompt = Some(Utc::now());
            let _ = save_cache(&cache_path, &cache);
        }
    }
}

/// Check for updates and display a notification if a new version is available.
/// This runs at most once per day and respects the BASILICA_NO_UPDATE_CHECK env var.
pub fn check_and_notify_update() {
    // Skip if explicitly disabled
    if std::env::var("BASILICA_NO_UPDATE_CHECK").is_ok() {
        return;
    }

    // Skip if not running in a TTY (avoid polluting scripts/CI output)
    if !std::io::stdout().is_terminal() {
        return;
    }

    // Check if we should run the update check (once per day)
    let cache_path = match get_cache_path() {
        Ok(path) => path,
        Err(_) => return, // Silently fail if we can't determine cache path
    };

    let should_check = match load_cache(&cache_path) {
        Ok(Some(cache)) => {
            let now = Utc::now();
            let elapsed = now.signed_duration_since(cache.last_check);
            elapsed > Duration::hours(CHECK_INTERVAL_HOURS)
        }
        Ok(None) => true, // No cache file, first run
        Err(_) => true,   // Error reading cache, try checking
    };

    if !should_check {
        // Already checked recently, skip background check
        return;
    }

    // Perform the check in a non-blocking way using std::thread
    // We don't want to block CLI startup
    std::thread::spawn(move || {
        // Create a new runtime for this thread
        let rt = match tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
        {
            Ok(rt) => rt,
            Err(_) => return,
        };

        rt.block_on(async {
            if let Ok(latest_version) = fetch_latest_version().await {
                let cache = UpdateCheckCache {
                    last_check: Utc::now(),
                    latest_version: Some(latest_version.clone()),
                    last_prompt: None, // Will be set when notification is shown
                };

                // Save cache (notification will be shown on next CLI invocation)
                let _ = save_cache(&cache_path, &cache);
            } else {
                // Even if fetch fails, update the last check time to avoid spamming
                let cache = UpdateCheckCache {
                    last_check: Utc::now(),
                    latest_version: None,
                    last_prompt: None,
                };
                let _ = save_cache(&cache_path, &cache);
            }
        });
    });
}

/// Fetch the latest version from GitHub
async fn fetch_latest_version() -> Result<String, Box<dyn std::error::Error>> {
    let releases = self_update::backends::github::ReleaseList::configure()
        .repo_owner("one-covenant")
        .repo_name("basilica")
        .build()?
        .fetch()?;

    let current_version = cargo_crate_version!();

    // Filter releases that match our tag pattern (basilica-cli-v*)
    // Note: r.version contains the tag name, r.name contains the release title
    let cli_releases: Vec<_> = releases
        .iter()
        .filter(|r| {
            if !r.version.starts_with("basilica-cli-v") {
                return false;
            }

            // Extract version and check if it's supported
            let version = r
                .version
                .trim_start_matches("basilica-cli-v")
                .trim_start_matches('v');

            // Filter out unsupported versions (< 0.5.4)
            if !is_version_supported(version) {
                return false;
            }

            // Only include versions newer than current
            match (Version::parse(version), Version::parse(current_version)) {
                (Ok(v), Ok(current)) => v > current,
                _ => false,
            }
        })
        .collect();

    if cli_releases.is_empty() {
        return Err("No newer CLI releases found".into());
    }

    // Get latest release version
    let latest = cli_releases[0];
    let version = latest
        .version
        .trim_start_matches("basilica-cli-v")
        .trim_start_matches('v')
        .to_string();

    Ok(version)
}

/// Get the path to the update check cache file
fn get_cache_path() -> Result<PathBuf, Box<dyn std::error::Error>> {
    let strategy = choose_base_strategy()?;
    let cache_dir = strategy.cache_dir().join("basilica");

    // Ensure directory exists
    fs::create_dir_all(&cache_dir)?;

    Ok(cache_dir.join(UPDATE_CHECK_FILE))
}

/// Load the update check cache
fn load_cache(path: &PathBuf) -> Result<Option<UpdateCheckCache>, Box<dyn std::error::Error>> {
    if !path.exists() {
        return Ok(None);
    }

    let content = fs::read_to_string(path)?;
    let cache: UpdateCheckCache = serde_json::from_str(&content)?;
    Ok(Some(cache))
}

/// Save the update check cache
fn save_cache(
    path: &PathBuf,
    cache: &UpdateCheckCache,
) -> Result<(), Box<dyn std::error::Error>> {
    let content = serde_json::to_string_pretty(cache)?;
    fs::write(path, content)?;
    Ok(())
}
