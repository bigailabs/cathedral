//! ASCII art banners and welcome messages

use console::style;

/// Display post-login welcome message with helpful commands
pub fn print_post_login_welcome() {
    println!(
        "{}",
        style("First time setup detected. Let's get you authenticated.").dim()
    );
    println!();

    print_command_suggestions();
}

/// Print helpful command suggestions
pub fn print_command_suggestions() {
    println!("{}", style("Quick Commands:").cyan().bold());
    println!();

    // List available GPUs
    println!(
        "  {} {}",
        style("cathedral ls").yellow().bold(),
        style("- View available GPUs for rental").dim()
    );

    // Start a rental
    println!(
        "  {} {}",
        style("cathedral up").yellow().bold(),
        style("- Start a GPU rental session").dim()
    );

    // List active rentals
    println!(
        "  {} {}",
        style("cathedral ps").yellow().bold(),
        style("- List active rentals").dim()
    );

    // Check specific rental status
    println!(
        "  {} {}",
        style("cathedral status").yellow().bold(),
        style("- Check status of a specific rental").dim()
    );

    // SSH into rental
    println!(
        "  {} {}",
        style("cathedral ssh").yellow().bold(),
        style("- Connect to your rental").dim()
    );

    // Stop a rental
    println!(
        "  {} {}",
        style("cathedral down").yellow().bold(),
        style("- Stop a GPU rental").dim()
    );

    println!();
    println!(
        "For more information, run {}",
        style("cathedral --help").green(),
    );
}
