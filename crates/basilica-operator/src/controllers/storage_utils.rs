/// Wrapped container command and args for FUSE wait integration.
pub type WrappedCommand = (Option<Vec<String>>, Option<Vec<String>>);

/// Error type for command wrapping failures
#[derive(Debug, thiserror::Error)]
pub enum CommandWrapError {
    #[error("Failed to shell-escape command: {0}")]
    ShellEscapeError(String),
}

/// Wraps user command with FUSE wait script.
///
/// Handles four input combinations:
/// - command Some, args Some: exec command with args
/// - command Some, args None: exec command alone
/// - command None, args Some: pass args to image entrypoint
/// - command None, args None: run image entrypoint after wait
///
/// Returns error if shell escaping fails (e.g., null bytes in strings).
pub fn wrap_command_with_fuse_wait(
    user_command: Option<Vec<String>>,
    user_args: Option<Vec<String>>,
    mount_path: &str,
) -> Result<WrappedCommand, CommandWrapError> {
    const WAIT_TIMEOUT_SECS: i32 = 60;

    let wait_script = format!(
        r#"echo 'Waiting for FUSE mount at {mount_path}...'
for i in $(seq 1 {timeout}); do
  if [ -f {mount_path}/.fuse_ready ]; then
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
}
