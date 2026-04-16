#[cfg(test)]
mod tests {
    use crate::config::emission::{EmissionConfig, DEFAULT_BURN_UID};
    use std::io::Write;
    use std::path::Path;
    use tempfile::NamedTempFile;

    #[test]
    fn test_default_config() {
        let config = EmissionConfig::default();

        // Verify default values
        assert_eq!(config.forced_burn_percentage, None);
        assert_eq!(config.burn_uid, DEFAULT_BURN_UID);
        assert_eq!(config.weight_set_interval_blocks, 360);

        // Default config should be valid (no gpu_allocations to fail)
        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_forced_burn_percentage_validation() {
        let mut config = EmissionConfig::for_testing();

        // None is always valid
        config.forced_burn_percentage = None;
        assert!(config.validate().is_ok());

        // Valid ranges
        config.forced_burn_percentage = Some(0.0);
        assert!(config.validate().is_ok());

        config.forced_burn_percentage = Some(50.0);
        assert!(config.validate().is_ok());

        config.forced_burn_percentage = Some(99.9);
        assert!(config.validate().is_ok());

        // Invalid: >= 100.0
        config.forced_burn_percentage = Some(100.0);
        assert!(config.validate().is_err());

        config.forced_burn_percentage = Some(100.1);
        assert!(config.validate().is_err());

        // Invalid: negative
        config.forced_burn_percentage = Some(-0.1);
        assert!(config.validate().is_err());

        config.forced_burn_percentage = Some(-50.0);
        assert!(config.validate().is_err());

        config.forced_burn_percentage = Some(150.0);
        assert!(config.validate().is_err());
    }

    #[test]
    fn test_weight_interval_validation() {
        let mut config = EmissionConfig::for_testing();
        config.weight_set_interval_blocks = 1;
        assert!(config.validate().is_ok());

        config.weight_set_interval_blocks = 360;
        assert!(config.validate().is_ok());

        config.weight_set_interval_blocks = 1000;
        assert!(config.validate().is_ok());

        // Test zero interval (should fail)
        config.weight_set_interval_blocks = 0;
        assert!(config.validate().is_err());
    }

    #[test]
    fn test_config_serialization() {
        let config = EmissionConfig::for_testing();

        // Test TOML serialization/deserialization
        let toml_str = toml::to_string(&config).expect("Failed to serialize to TOML");
        let deserialized: EmissionConfig =
            toml::from_str(&toml_str).expect("Failed to deserialize from TOML");
        assert_eq!(config, deserialized);

        // Test JSON serialization/deserialization
        let json_str = serde_json::to_string(&config).expect("Failed to serialize to JSON");
        let deserialized: EmissionConfig =
            serde_json::from_str(&json_str).expect("Failed to deserialize from JSON");
        assert_eq!(config, deserialized);

        // Test that serialized config is valid
        assert!(deserialized.validate().is_ok());
    }

    #[test]
    fn test_config_from_toml_file() {
        let toml_content = r#"
forced_burn_percentage = 15.0
burn_uid = 123
weight_set_interval_blocks = 720
weight_version_key = 0
"#;

        let mut temp_file = NamedTempFile::new().expect("Failed to create temp file");
        temp_file
            .write_all(toml_content.as_bytes())
            .expect("Failed to write temp file");

        let config = EmissionConfig::from_toml_file(temp_file.path())
            .expect("Failed to load from TOML file");

        assert_eq!(config.forced_burn_percentage, Some(15.0));
        assert_eq!(config.burn_uid, 123);
        assert_eq!(config.weight_set_interval_blocks, 720);

        // Test loading from non-existent file
        let result = EmissionConfig::from_toml_file(Path::new("/non/existent/file.toml"));
        assert!(result.is_err());
    }

    #[test]
    fn test_config_merge_with_defaults() {
        let partial_config = EmissionConfig {
            forced_burn_percentage: Some(20.0),
            burn_uid: 456,
            weight_set_interval_blocks: 0, // Invalid - should use default
            weight_version_key: 0,
        };

        let merged = partial_config.merge_with_defaults();

        assert_eq!(merged.forced_burn_percentage, Some(20.0)); // Preserved
        assert_eq!(merged.burn_uid, 456); // Preserved
        assert_eq!(merged.weight_set_interval_blocks, 360); // Default

        // Test complete config override (no merging needed)
        let complete_config = EmissionConfig::for_testing();
        let merged = complete_config.clone().merge_with_defaults();
        assert_eq!(merged, complete_config);
    }

    #[test]
    fn test_edge_cases() {
        let mut config = EmissionConfig::for_testing();
        config.forced_burn_percentage = Some(99.9);
        config.burn_uid = u16::MAX;
        config.weight_set_interval_blocks = u64::MAX;
        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_for_testing_config() {
        let config = EmissionConfig::for_testing();
        assert!(config.validate().is_ok());

        assert_eq!(config.forced_burn_percentage, Some(10.0));
        assert_eq!(config.burn_uid, 999);
        assert_eq!(config.weight_set_interval_blocks, 360);
    }

    #[test]
    fn test_old_config_with_removed_fields_still_parses() {
        // Simulates an operator whose config file still has gpu_allocations,
        // burn_percentage, and min_miners_per_category from before removal.
        let toml_content = r#"
burn_percentage = 95.0
forced_burn_percentage = 15.0
burn_uid = 123
weight_set_interval_blocks = 720
weight_version_key = 0
min_miners_per_category = 2
some_totally_unknown_field = "hello"

[gpu_allocations]
A100 = 25.0
H100 = 50.0
B200 = 25.0
"#;

        let mut temp_file = NamedTempFile::new().expect("Failed to create temp file");
        temp_file
            .write_all(toml_content.as_bytes())
            .expect("Failed to write temp file");

        let config = EmissionConfig::from_toml_file(temp_file.path())
            .expect("Old config with removed fields should still parse");

        assert_eq!(config.forced_burn_percentage, Some(15.0));
        assert_eq!(config.burn_uid, 123);
        assert!(config.validate().is_ok());
    }
}
