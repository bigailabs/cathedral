use crate::error::ValidationError;

const MAX_NAME_LENGTH: usize = 255;
const MAX_USER_ID_LENGTH: usize = 128;
const MAX_NAMESPACE_LENGTH: usize = 63;
const MAX_DESCRIPTION_LENGTH: usize = 1024;
const MIN_NAME_LENGTH: usize = 1;

/// Validates a resource name (API keys, deployments, etc.)
///
/// Names must:
/// - Be 1-255 characters
/// - Contain only alphanumeric, dash, underscore, or space
/// - Not start or end with whitespace
/// - Not contain control characters
pub fn validate_name(name: &str, field: &str) -> Result<(), ValidationError> {
    if name.is_empty() {
        return Err(ValidationError::InvalidFormat {
            field: field.to_string(),
            value: "(empty)".to_string(),
        });
    }

    if name.len() > MAX_NAME_LENGTH {
        return Err(ValidationError::OutOfRange {
            field: field.to_string(),
            value: name.len().to_string(),
            min: MIN_NAME_LENGTH.to_string(),
            max: MAX_NAME_LENGTH.to_string(),
        });
    }

    if name.starts_with(char::is_whitespace) || name.ends_with(char::is_whitespace) {
        return Err(ValidationError::InvalidFormat {
            field: field.to_string(),
            value: "cannot start or end with whitespace".to_string(),
        });
    }

    if name.chars().any(|c| c.is_control()) {
        return Err(ValidationError::InvalidFormat {
            field: field.to_string(),
            value: "contains control characters".to_string(),
        });
    }

    let valid = name
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | ' ' | '.'));

    if !valid {
        return Err(ValidationError::InvalidFormat {
            field: field.to_string(),
            value:
                "contains invalid characters (allowed: alphanumeric, dash, underscore, space, dot)"
                    .to_string(),
        });
    }

    Ok(())
}

/// Validates a user ID
///
/// User IDs must:
/// - Be 1-128 characters
/// - Contain only alphanumeric, dash, underscore, pipe, or at-sign
/// - Not contain control characters
pub fn validate_user_id(user_id: &str) -> Result<(), ValidationError> {
    if user_id.is_empty() {
        return Err(ValidationError::InvalidFormat {
            field: "user_id".to_string(),
            value: "(empty)".to_string(),
        });
    }

    if user_id.len() > MAX_USER_ID_LENGTH {
        return Err(ValidationError::OutOfRange {
            field: "user_id".to_string(),
            value: user_id.len().to_string(),
            min: "1".to_string(),
            max: MAX_USER_ID_LENGTH.to_string(),
        });
    }

    if user_id.chars().any(|c| c.is_control()) {
        return Err(ValidationError::InvalidFormat {
            field: "user_id".to_string(),
            value: "contains control characters".to_string(),
        });
    }

    let valid = user_id
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '|' | '@'));

    if !valid {
        return Err(ValidationError::InvalidFormat {
            field: "user_id".to_string(),
            value: "contains invalid characters (allowed: alphanumeric, dash, underscore, pipe, at-sign)".to_string(),
        });
    }

    Ok(())
}

/// Validates a Kubernetes namespace name
///
/// Namespace names must:
/// - Be 1-63 characters
/// - Contain only lowercase alphanumeric or dash
/// - Start with a letter
/// - Not end with a dash
pub fn validate_namespace(namespace: &str) -> Result<(), ValidationError> {
    if namespace.is_empty() {
        return Err(ValidationError::InvalidFormat {
            field: "namespace".to_string(),
            value: "(empty)".to_string(),
        });
    }

    if namespace.len() > MAX_NAMESPACE_LENGTH {
        return Err(ValidationError::OutOfRange {
            field: "namespace".to_string(),
            value: namespace.len().to_string(),
            min: "1".to_string(),
            max: MAX_NAMESPACE_LENGTH.to_string(),
        });
    }

    let first_char = namespace.chars().next().unwrap();
    if !first_char.is_ascii_lowercase() {
        return Err(ValidationError::InvalidFormat {
            field: "namespace".to_string(),
            value: "must start with a lowercase letter".to_string(),
        });
    }

    if namespace.ends_with('-') {
        return Err(ValidationError::InvalidFormat {
            field: "namespace".to_string(),
            value: "cannot end with a dash".to_string(),
        });
    }

    let valid = namespace
        .chars()
        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-');

    if !valid {
        return Err(ValidationError::InvalidFormat {
            field: "namespace".to_string(),
            value: "contains invalid characters (allowed: lowercase alphanumeric, dash)"
                .to_string(),
        });
    }

    if namespace.contains("--") {
        return Err(ValidationError::InvalidFormat {
            field: "namespace".to_string(),
            value: "cannot contain consecutive dashes".to_string(),
        });
    }

    Ok(())
}

/// Validates a description field
///
/// Descriptions must:
/// - Be at most 1024 characters
/// - Not contain control characters except newline and tab
pub fn validate_description(description: &str) -> Result<(), ValidationError> {
    if description.len() > MAX_DESCRIPTION_LENGTH {
        return Err(ValidationError::OutOfRange {
            field: "description".to_string(),
            value: description.len().to_string(),
            min: "0".to_string(),
            max: MAX_DESCRIPTION_LENGTH.to_string(),
        });
    }

    let has_invalid_control = description
        .chars()
        .any(|c| c.is_control() && !matches!(c, '\n' | '\t' | '\r'));

    if has_invalid_control {
        return Err(ValidationError::InvalidFormat {
            field: "description".to_string(),
            value: "contains invalid control characters".to_string(),
        });
    }

    Ok(())
}

/// Validates a container image reference for safe use in shell commands
///
/// Image references must:
/// - Be non-empty
/// - Not exceed 512 characters
/// - Not contain shell metacharacters ($, `, ;, |, &, >, <, newline, carriage return, null)
/// - Contain only alphanumeric characters and allowed punctuation (/, :, -, _, ., @)
///
/// Note: This provides basic injection prevention validation, not strict Docker
/// image format validation. For strict OCI image reference parsing, use
/// `validate_docker_image()` from `basilica_common::utils` instead.
pub fn validate_image_reference(image: &str) -> Result<(), ValidationError> {
    if image.is_empty() {
        return Err(ValidationError::InvalidFormat {
            field: "image".to_string(),
            value: "(empty)".to_string(),
        });
    }

    if image.len() > 512 {
        return Err(ValidationError::OutOfRange {
            field: "image".to_string(),
            value: image.len().to_string(),
            min: "1".to_string(),
            max: "512".to_string(),
        });
    }

    // Block shell metacharacters
    let dangerous_chars = ['$', '`', ';', '|', '&', '>', '<', '\n', '\r', '\0'];
    for c in dangerous_chars {
        if image.contains(c) {
            return Err(ValidationError::InvalidFormat {
                field: "image".to_string(),
                value: format!("contains forbidden character: {:?}", c),
            });
        }
    }

    // Basic format check: must have at least one valid character sequence
    let valid = image
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '/' | ':' | '-' | '_' | '.' | '@'));

    if !valid {
        return Err(ValidationError::InvalidFormat {
            field: "image".to_string(),
            value: "contains invalid characters".to_string(),
        });
    }

    Ok(())
}

/// Validates an environment variable name
///
/// Must follow POSIX conventions:
/// - Start with letter or underscore
/// - Contain only alphanumeric and underscore
pub fn validate_env_var_name(name: &str) -> Result<(), ValidationError> {
    if name.is_empty() {
        return Err(ValidationError::InvalidFormat {
            field: "env_var_name".to_string(),
            value: "(empty)".to_string(),
        });
    }

    if name.len() > 255 {
        return Err(ValidationError::OutOfRange {
            field: "env_var_name".to_string(),
            value: name.len().to_string(),
            min: "1".to_string(),
            max: "255".to_string(),
        });
    }

    let first_char = name.chars().next().unwrap();
    if !first_char.is_ascii_alphabetic() && first_char != '_' {
        return Err(ValidationError::InvalidFormat {
            field: "env_var_name".to_string(),
            value: "must start with letter or underscore".to_string(),
        });
    }

    let valid = name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_');

    if !valid {
        return Err(ValidationError::InvalidFormat {
            field: "env_var_name".to_string(),
            value: "contains invalid characters (allowed: alphanumeric, underscore)".to_string(),
        });
    }

    Ok(())
}

/// Validates a port number
pub fn validate_port(port: u16, field: &str) -> Result<(), ValidationError> {
    if port == 0 {
        return Err(ValidationError::OutOfRange {
            field: field.to_string(),
            value: "0".to_string(),
            min: "1".to_string(),
            max: "65535".to_string(),
        });
    }
    Ok(())
}

/// Validates a hostname
///
/// Must be valid DNS hostname:
/// - Labels separated by dots
/// - Each label 1-63 chars
/// - Only alphanumeric and hyphens (no leading/trailing hyphens)
pub fn validate_hostname(hostname: &str) -> Result<(), ValidationError> {
    if hostname.is_empty() {
        return Err(ValidationError::InvalidFormat {
            field: "hostname".to_string(),
            value: "(empty)".to_string(),
        });
    }

    if hostname.len() > 253 {
        return Err(ValidationError::OutOfRange {
            field: "hostname".to_string(),
            value: hostname.len().to_string(),
            min: "1".to_string(),
            max: "253".to_string(),
        });
    }

    for label in hostname.split('.') {
        if label.is_empty() {
            return Err(ValidationError::InvalidFormat {
                field: "hostname".to_string(),
                value: "contains empty label".to_string(),
            });
        }

        if label.len() > 63 {
            return Err(ValidationError::OutOfRange {
                field: "hostname_label".to_string(),
                value: label.len().to_string(),
                min: "1".to_string(),
                max: "63".to_string(),
            });
        }

        if label.starts_with('-') || label.ends_with('-') {
            return Err(ValidationError::InvalidFormat {
                field: "hostname".to_string(),
                value: "label cannot start or end with hyphen".to_string(),
            });
        }

        let valid = label.chars().all(|c| c.is_ascii_alphanumeric() || c == '-');
        if !valid {
            return Err(ValidationError::InvalidFormat {
                field: "hostname".to_string(),
                value: "contains invalid characters".to_string(),
            });
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    // Name validation tests
    #[test]
    fn test_validate_name_valid() {
        assert!(validate_name("my-api-key", "name").is_ok());
        assert!(validate_name("API Key 1", "name").is_ok());
        assert!(validate_name("test_key.v1", "name").is_ok());
        assert!(validate_name("a", "name").is_ok());
    }

    #[test]
    fn test_validate_name_empty() {
        assert!(validate_name("", "name").is_err());
    }

    #[test]
    fn test_validate_name_too_long() {
        let long_name = "a".repeat(256);
        assert!(validate_name(&long_name, "name").is_err());
    }

    #[test]
    fn test_validate_name_whitespace() {
        assert!(validate_name(" leading", "name").is_err());
        assert!(validate_name("trailing ", "name").is_err());
        assert!(validate_name("middle space", "name").is_ok());
    }

    #[test]
    fn test_validate_name_control_chars() {
        assert!(validate_name("with\x00null", "name").is_err());
        assert!(validate_name("with\nnewline", "name").is_err());
    }

    #[test]
    fn test_validate_name_invalid_chars() {
        assert!(validate_name("with;semicolon", "name").is_err());
        assert!(validate_name("with$dollar", "name").is_err());
        assert!(validate_name("with`backtick", "name").is_err());
    }

    // User ID validation tests
    #[test]
    fn test_validate_user_id_valid() {
        assert!(validate_user_id("user-123").is_ok());
        assert!(validate_user_id("github|12345").is_ok());
        assert!(validate_user_id("user@domain").is_ok());
        assert!(validate_user_id("user_name").is_ok());
    }

    #[test]
    fn test_validate_user_id_empty() {
        assert!(validate_user_id("").is_err());
    }

    #[test]
    fn test_validate_user_id_too_long() {
        let long_id = "a".repeat(129);
        assert!(validate_user_id(&long_id).is_err());
    }

    #[test]
    fn test_validate_user_id_invalid_chars() {
        assert!(validate_user_id("user;evil").is_err());
        assert!(validate_user_id("user$evil").is_err());
        assert!(validate_user_id("user\nevil").is_err());
    }

    // Namespace validation tests
    #[test]
    fn test_validate_namespace_valid() {
        assert!(validate_namespace("u-user-123").is_ok());
        assert!(validate_namespace("basilica-system").is_ok());
        assert!(validate_namespace("default").is_ok());
    }

    #[test]
    fn test_validate_namespace_empty() {
        assert!(validate_namespace("").is_err());
    }

    #[test]
    fn test_validate_namespace_invalid_start() {
        assert!(validate_namespace("-invalid").is_err());
        assert!(validate_namespace("123invalid").is_err());
        assert!(validate_namespace("Uppercase").is_err());
    }

    #[test]
    fn test_validate_namespace_invalid_end() {
        assert!(validate_namespace("invalid-").is_err());
    }

    #[test]
    fn test_validate_namespace_consecutive_dashes() {
        assert!(validate_namespace("invalid--name").is_err());
    }

    #[test]
    fn test_validate_namespace_too_long() {
        let long_ns = "a".repeat(64);
        assert!(validate_namespace(&long_ns).is_err());
    }

    // Description validation tests
    #[test]
    fn test_validate_description_valid() {
        assert!(validate_description("A valid description").is_ok());
        assert!(validate_description("With\nnewlines\nallowed").is_ok());
        assert!(validate_description("With\ttabs\tallowed").is_ok());
        assert!(validate_description("").is_ok());
    }

    #[test]
    fn test_validate_description_too_long() {
        let long_desc = "a".repeat(1025);
        assert!(validate_description(&long_desc).is_err());
    }

    #[test]
    fn test_validate_description_control_chars() {
        assert!(validate_description("with\x00null").is_err());
        assert!(validate_description("with\x07bell").is_err());
    }

    // Image reference validation tests
    #[test]
    fn test_validate_image_reference_valid() {
        assert!(validate_image_reference("nginx:latest").is_ok());
        assert!(validate_image_reference("docker.io/library/nginx:1.21").is_ok());
        assert!(validate_image_reference("ghcr.io/org/image:v1.0.0").is_ok());
        assert!(validate_image_reference("image@sha256:abc123").is_ok());
    }

    #[test]
    fn test_validate_image_reference_empty() {
        assert!(validate_image_reference("").is_err());
    }

    #[test]
    fn test_validate_image_reference_injection() {
        assert!(validate_image_reference("nginx; rm -rf /").is_err());
        assert!(validate_image_reference("$(evil)").is_err());
        assert!(validate_image_reference("`evil`").is_err());
        assert!(validate_image_reference("image | cat /etc/passwd").is_err());
    }

    // Env var name validation tests
    #[test]
    fn test_validate_env_var_name_valid() {
        assert!(validate_env_var_name("PATH").is_ok());
        assert!(validate_env_var_name("_PRIVATE").is_ok());
        assert!(validate_env_var_name("MY_VAR_123").is_ok());
    }

    #[test]
    fn test_validate_env_var_name_invalid_start() {
        assert!(validate_env_var_name("123VAR").is_err());
        assert!(validate_env_var_name("-VAR").is_err());
    }

    #[test]
    fn test_validate_env_var_name_invalid_chars() {
        assert!(validate_env_var_name("MY-VAR").is_err());
        assert!(validate_env_var_name("MY.VAR").is_err());
        assert!(validate_env_var_name("MY VAR").is_err());
    }

    // Port validation tests
    #[test]
    fn test_validate_port_valid() {
        assert!(validate_port(80, "port").is_ok());
        assert!(validate_port(8080, "port").is_ok());
        assert!(validate_port(65535, "port").is_ok());
        assert!(validate_port(1, "port").is_ok());
    }

    #[test]
    fn test_validate_port_zero() {
        assert!(validate_port(0, "port").is_err());
    }

    // Hostname validation tests
    #[test]
    fn test_validate_hostname_valid() {
        assert!(validate_hostname("example.com").is_ok());
        assert!(validate_hostname("sub.example.com").is_ok());
        assert!(validate_hostname("my-host").is_ok());
        assert!(validate_hostname("localhost").is_ok());
    }

    #[test]
    fn test_validate_hostname_empty() {
        assert!(validate_hostname("").is_err());
    }

    #[test]
    fn test_validate_hostname_invalid_label() {
        assert!(validate_hostname("-invalid.com").is_err());
        assert!(validate_hostname("invalid-.com").is_err());
        assert!(validate_hostname("invalid..com").is_err());
    }

    #[test]
    fn test_validate_hostname_too_long() {
        let long_label = "a".repeat(64);
        let hostname = format!("{}.example.com", long_label);
        assert!(validate_hostname(&hostname).is_err());
    }
}
