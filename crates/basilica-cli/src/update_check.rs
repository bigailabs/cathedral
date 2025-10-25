//! Background update checker for the Basilica CLI
//!
//! Checks for new versions once per day and displays a notification if available.
//! Inspired by Deno's upgrade notification system.

use chrono::{DateTime, Duration, Utc};
use console::style;
use etcetera::{choose_base_strategy, BaseStrategy};
use self_update::cargo_crate_version;
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::IsTerminal;
use std::path::PathBuf;

const UPDATE_CHECK_FILE: &str = "update_check.json";
const CHECK_INTERVAL_HOURS: i64 = 24;

#[derive(Debug, Serialize, Deserialize)]
struct UpdateCheckCache {
    last_check: DateTime<Utc>,
    latest_version: Option<String>,
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
        // Already checked recently, try to show notification from cache
        if let Ok(Some(cache)) = load_cache(&cache_path) {
            if let Some(latest) = cache.latest_version {
                show_update_notification(&latest);
            }
        }
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
                };

                // Save cache
                let _ = save_cache(&cache_path, &cache);

                // Show notification
                show_update_notification(&latest_version);
            } else {
                // Even if fetch fails, update the last check time to avoid spamming
                let cache = UpdateCheckCache {
                    last_check: Utc::now(),
                    latest_version: None,
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

    // Filter releases that match our tag pattern (basilica-cli-v*)
    // Note: r.version contains the tag name, r.name contains the release title
    let cli_releases: Vec<_> = releases
        .iter()
        .filter(|r| r.version.starts_with("basilica-cli-v"))
        .collect();

    if cli_releases.is_empty() {
        return Err("No CLI releases found".into());
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

/// Display update notification if a newer version is available
fn show_update_notification(latest_version: &str) {
    let current_version = cargo_crate_version!();

    // Only show if the latest version is different from current
    if latest_version != current_version {
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
    }
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
