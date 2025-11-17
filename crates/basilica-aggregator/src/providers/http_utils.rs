use crate::error::{AggregatorError, Result};
use reqwest::{Client, Response};
use std::time::Duration;

/// Builder for creating HTTP clients with consistent configuration across providers
pub struct HttpClientBuilder {
    timeout_seconds: u64,
}

impl HttpClientBuilder {
    /// Create a new HTTP client builder with specified timeout
    pub fn new(timeout_seconds: u64) -> Self {
        Self { timeout_seconds }
    }

    /// Build an HTTP client for the specified provider
    ///
    /// # Arguments
    /// * `provider_name` - Name of the provider (used in error messages)
    ///
    /// # Returns
    /// A configured `reqwest::Client` or an error if client creation fails
    pub fn build(&self, provider_name: &str) -> Result<Client> {
        Client::builder()
            .timeout(Duration::from_secs(self.timeout_seconds))
            .build()
            .map_err(|e| AggregatorError::Provider {
                provider: provider_name.to_string(),
                message: format!("Failed to create HTTP client: {}", e),
            })
    }
}

/// Handle HTTP response errors with consistent error reporting
///
/// # Arguments
/// * `response` - The HTTP response to check
/// * `provider_name` - Name of the provider (used in error messages)
///
/// # Returns
/// The original response if successful, or an error if the status indicates failure
pub async fn handle_error_response(response: Response, provider_name: &str) -> Result<Response> {
    if !response.status().is_success() {
        let status = response.status();
        let error_text = response.text().await.unwrap_or_default();

        // Check for 404 Not Found - instance was deleted externally
        if status == reqwest::StatusCode::NOT_FOUND {
            tracing::warn!("{} instance not found (404): {}", provider_name, error_text);
            return Err(AggregatorError::NotFound(format!(
                "{} instance not found (may have been deleted externally)",
                provider_name
            )));
        }

        tracing::error!(
            "{} API returned error: {} - {}",
            provider_name,
            status,
            error_text
        );
        return Err(AggregatorError::Provider {
            provider: provider_name.to_string(),
            message: format!("API returned status: {} - {}", status, error_text),
        });
    }
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_http_client_builder_creates_client() {
        let builder = HttpClientBuilder::new(30);
        let result = builder.build("test-provider");
        assert!(result.is_ok());
    }

    #[test]
    fn test_http_client_builder_with_different_timeouts() {
        let builder_short = HttpClientBuilder::new(10);
        let builder_long = HttpClientBuilder::new(60);

        assert!(builder_short.build("test").is_ok());
        assert!(builder_long.build("test").is_ok());
    }
}
