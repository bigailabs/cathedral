#[cfg(test)]
mod tests {
    use crate::config::{emission::DEFAULT_BURN_UID, ValidatorConfig};
    use basilica_common::config::ConfigValidation;

    #[test]
    fn test_validator_config_includes_emission_config() {
        let config = ValidatorConfig::default();

        // Verify emission config is included
        assert_eq!(config.emission.forced_burn_percentage, None);
        assert_eq!(config.emission.burn_uid, DEFAULT_BURN_UID);
        assert_eq!(config.emission.weight_set_interval_blocks, 360);

        // Default config should now pass emission validation (no gpu_allocations constraint),
        // but may still fail on other fields like binary_validation
    }

    #[test]
    fn test_validator_config_emission_validation() {
        let mut config = ValidatorConfig::default();

        // Modify emission config to be invalid
        config.emission.forced_burn_percentage = Some(150.0); // Invalid

        // Should fail validation
        assert!(config.validate().is_err());

        // Set valid burn percentage
        config.emission.forced_burn_percentage = Some(10.0);
        // Disable binary validation since we don't have the binaries in test
        config.verification.binary_validation = None;
        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_validator_config_serialization_with_emission() {
        let mut config = ValidatorConfig::default();
        // Disable binary validation since we don't have the binaries in test (None = disabled)
        config.verification.binary_validation = None;

        config.emission.forced_burn_percentage = Some(10.0);

        // Test TOML serialization includes emission config
        let toml_str = toml::to_string(&config).expect("Failed to serialize to TOML");
        assert!(toml_str.contains("[emission]"));
        assert!(toml_str.contains("forced_burn_percentage"));

        // Test deserialization
        let deserialized: ValidatorConfig =
            toml::from_str(&toml_str).expect("Failed to deserialize from TOML");

        assert_eq!(
            config.emission.forced_burn_percentage,
            deserialized.emission.forced_burn_percentage
        );

        // Verify deserialized config is valid
        assert!(deserialized.validate().is_ok());
    }

    #[test]
    fn test_billing_api_endpoint_requires_gateway() {
        let mut config = ValidatorConfig::default();
        config.verification.binary_validation = None;
        config.emission.forced_burn_percentage = Some(10.0);

        config.billing.enabled = true;
        config.api_endpoint = config.billing.billing_endpoint.clone();

        assert!(config.validate().is_err());
    }

    #[test]
    fn test_default_endpoints_use_api_gateway() {
        let config = ValidatorConfig::default();
        assert_ne!(config.api_endpoint, config.billing.billing_endpoint);
        assert!(config.api_endpoint.contains("basilica"));
    }
}
