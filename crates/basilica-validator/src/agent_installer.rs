//! K3s Agent Installer and Prober
//!
//! Pure helpers to construct install/upgrade/enable commands for k3s-agent and
//! parsers to interpret common outputs from `systemctl status` and `k3s -v`.
//! These are designed to be used over SSH but contain no network IO themselves,
//! enabling fast unit tests and offline validation.

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct K3sAgentConfig {
    pub server_url: String,
    pub token: String,
    pub node_name: Option<String>,
    pub extra_args: Vec<String>,
    /// Optional k3s channel (e.g. "stable", "v1.29"). If None, defaults upstream.
    pub channel: Option<String>,
}

impl K3sAgentConfig {
    pub fn new<S1: Into<String>, S2: Into<String>>(server_url: S1, token: S2) -> Self {
        Self {
            server_url: server_url.into(),
            token: token.into(),
            node_name: None,
            extra_args: vec![],
            channel: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ServiceState {
    Active,
    Inactive,
    Failed,
    Unknown,
}

/// Build shell commands to install/start the k3s agent.
/// Commands assume systemd is present and `curl` is available on the remote host.
pub fn build_install_commands(cfg: &K3sAgentConfig) -> Vec<String> {
    let mut cmds = Vec::new();
    // Ensure curl and system tools (best-effort, distro-agnostic attempt)
    cmds.push("which curl || (which apt && sudo apt update && sudo apt install -y curl) || (which yum && sudo yum install -y curl) || true".to_string());
    // Compose installer envs
    let mut envs = vec![
        format!("K3S_URL={}", cfg.server_url),
        format!("K3S_TOKEN={}", cfg.token),
    ];
    if let Some(ch) = &cfg.channel {
        envs.push(format!("INSTALL_K3S_CHANNEL={}", ch));
    }

    // Additional agent args
    let mut agent_args: Vec<String> = Vec::new();
    if let Some(n) = &cfg.node_name {
        agent_args.push(format!("--node-name {}", shell_quote(n)));
    }
    if !cfg.extra_args.is_empty() {
        agent_args.extend(cfg.extra_args.iter().cloned());
    }

    // Use upstream installer with agent subcommand
    let arg_tail = if agent_args.is_empty() {
        "".into()
    } else {
        format!(" -- {}", agent_args.join(" "))
    };
    cmds.push(format!(
        "curl -sfL https://get.k3s.io | {} sh -s - agent{} <(true)",
        envs.join(" "),
        arg_tail
    ));

    // Ensure enabled and started
    cmds.push("sudo systemctl enable k3s-agent".into());
    cmds.push("sudo systemctl restart k3s-agent".into());
    cmds
}

/// Build shell commands to upgrade the k3s agent.
pub fn build_upgrade_commands(channel: Option<&str>) -> Vec<String> {
    let mut cmds = Vec::new();
    if let Some(ch) = channel {
        cmds.push(format!(
            "INSTALL_K3S_CHANNEL={} sh -c 'curl -sfL https://get.k3s.io | sh -'",
            ch
        ));
    } else {
        cmds.push("sh -c 'curl -sfL https://get.k3s.io | sh -'".into());
    }
    cmds.push("sudo systemctl restart k3s-agent".into());
    cmds
}

/// Build shell commands to uninstall the k3s agent.
pub fn build_uninstall_commands() -> Vec<String> {
    vec![
        "sudo /usr/local/bin/k3s-agent-uninstall.sh || sudo /usr/local/bin/k3s-uninstall.sh || true".into(),
    ]
}

/// Parse `systemctl status k3s-agent` output to a simplified state.
pub fn parse_systemctl_status(output: &str) -> ServiceState {
    let lower = output.to_lowercase();
    if lower.contains("active: active (running)") {
        ServiceState::Active
    } else if lower.contains("active: inactive") {
        ServiceState::Inactive
    } else if lower.contains("active: failed") || lower.contains("failed; ") {
        ServiceState::Failed
    } else {
        ServiceState::Unknown
    }
}

/// Parse `k3s -v` (or `k3s --version`) output to extract the version string.
/// Examples:
/// - "k3s version v1.29.4+k3s1 (hash)" -> Some("v1.29.4+k3s1")
/// - "k3s version v1.28.5+k3s2" -> Some("v1.28.5+k3s2")
pub fn parse_k3s_version(output: &str) -> Option<String> {
    // Look for token starting with 'v' and containing '+'
    for tok in output.split_whitespace() {
        if tok.starts_with('v') && tok.contains("+k3s") {
            return Some(tok.trim().to_string());
        }
    }
    None
}

/// Shell-escape a simple argument by quoting if needed.
fn shell_quote(s: &str) -> String {
    if s.chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.')
    {
        s.to_string()
    } else {
        // Replace single quotes with the shell-safe pattern: ' -> '\''
        format!("'{}'", s.replace('\'', "'\\''"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn install_commands_include_server_token_and_args() {
        let mut cfg = K3sAgentConfig::new("https://10.0.0.1:6443", "TOKEN123");
        cfg.node_name = Some("gpu-node-01".into());
        cfg.extra_args = vec!["--node-taint basilica.ai/workloads-only=true:NoSchedule".into()];
        cfg.channel = Some("v1.29".into());
        let cmds = build_install_commands(&cfg);
        let all = cmds.join("\n");
        assert!(all.contains("K3S_URL=https://10.0.0.1:6443"));
        assert!(all.contains("K3S_TOKEN=TOKEN123"));
        assert!(all.contains("INSTALL_K3S_CHANNEL=v1.29"));
        assert!(all.contains("agent -- --node-name gpu-node-01 --node-taint basilica.ai/workloads-only=true:NoSchedule"));
        assert!(all.contains("systemctl enable k3s-agent"));
        assert!(all.contains("systemctl restart k3s-agent"));
    }

    #[test]
    fn parse_systemctl_status_variants() {
        let active = "\n   Active: active (running) since Thu 2024-10-03 12:00:00 UTC; 1h ago\n";
        let inactive = "\n   Active: inactive (dead) since Thu 2024-10-03 12:00:00 UTC; 1h ago\n";
        let failed =
            "\n   Active: failed (Result: exit-code) since Thu 2024-10-03 12:00:00 UTC; 1h ago\n";
        assert_eq!(parse_systemctl_status(active), ServiceState::Active);
        assert_eq!(parse_systemctl_status(inactive), ServiceState::Inactive);
        assert_eq!(parse_systemctl_status(failed), ServiceState::Failed);
        assert_eq!(
            parse_systemctl_status("something else"),
            ServiceState::Unknown
        );
    }

    #[test]
    fn parse_k3s_version_variants() {
        let v1 = "k3s version v1.29.4+k3s1 (hash)";
        let v2 = "k3s version v1.28.5+k3s2";
        let bad = "k3s version commit abcdef";
        assert_eq!(parse_k3s_version(v1), Some("v1.29.4+k3s1".into()));
        assert_eq!(parse_k3s_version(v2), Some("v1.28.5+k3s2".into()));
        assert_eq!(parse_k3s_version(bad), None);
    }
}
