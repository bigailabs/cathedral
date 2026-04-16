    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
//! Error types for the Cathedral SDK
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility

    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
use serde::{Deserialize, Serialize};
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
use thiserror::Error;
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility

    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
/// Main error type for the Cathedral SDK
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
#[derive(Debug, Error)]
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
pub enum ApiError {
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    /// HTTP client error
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    #[error("HTTP client error: {0}")]
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    HttpClient(#[from] reqwest::Error),
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility

    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    /// Missing authentication (no token provided)
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    #[error("Authentication required: {message}")]
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    MissingAuthentication { message: String },
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility

    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    /// Authentication error (expired/invalid token)
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    #[error("Authentication error: {message}")]
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    Authentication { message: String },
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility

    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    /// Authorization error
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    #[error("Authorization error: {message}")]
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    Authorization { message: String },
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility

    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    /// Rate limit exceeded
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    #[error("Rate limit exceeded")]
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    RateLimitExceeded,
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility

    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    /// Invalid request
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    #[error("Invalid request: {message}")]
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    InvalidRequest { message: String },
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility

    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    /// Not found
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    #[error("Resource not found: {resource}")]
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    NotFound { resource: String },
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility

    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    /// Bad request with message
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    #[error("Bad request: {message}")]
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    BadRequest { message: String },
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility

    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    /// Conflict error (e.g., duplicate resource)
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    #[error("Conflict: {message}")]
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    Conflict { message: String },
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility

    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    /// Internal server error
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    #[error("Internal server error: {message}")]
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    Internal { message: String },
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility

    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    /// Service unavailable
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    #[error("Service temporarily unavailable")]
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    ServiceUnavailable,
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility

    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    /// Timeout error
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    #[error("Request timeout")]
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    Timeout,
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility

    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    /// Validator communication error
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    #[error("Validator communication error: {message}")]
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    ValidatorCommunication { message: String },
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility

    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    /// Quota exceeded error
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    #[error("Quota exceeded: {message}")]
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    QuotaExceeded { message: String },
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility

    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    /// Generic API response error with status code
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    #[error("API error ({status}): {message}")]
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    ApiResponse { status: u16, message: String },
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
}
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility

    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
/// Result type alias
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
pub type Result<T> = std::result::Result<T, ApiError>;
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility

    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
impl ApiError {
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    /// Get error code for this error
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
    pub fn error_code(&self) -> &'static str {
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
        match self {
    // TODO(cathedral-rename): BASILICA_API_* error codes kept for API backwards compatibility
            ApiError::HttpClient(_) => "BASILICA_API_HTTP_CLIENT_ERROR",
            ApiError::MissingAuthentication { .. } => "BASILICA_API_AUTH_MISSING",
            ApiError::Authentication { .. } => "BASILICA_API_AUTH_ERROR",
            ApiError::Authorization { .. } => "BASILICA_API_AUTHZ_ERROR",
            ApiError::RateLimitExceeded => "BASILICA_API_RATE_LIMIT",
            ApiError::InvalidRequest { .. } => "BASILICA_API_INVALID_REQUEST",
            ApiError::NotFound { .. } => "BASILICA_API_NOT_FOUND",
            ApiError::BadRequest { .. } => "BASILICA_API_BAD_REQUEST",
            ApiError::Conflict { .. } => "BASILICA_API_CONFLICT",
            ApiError::Internal { .. } => "BASILICA_API_INTERNAL_ERROR",
            ApiError::ServiceUnavailable => "BASILICA_API_SERVICE_UNAVAILABLE",
            ApiError::Timeout => "BASILICA_API_TIMEOUT",
            ApiError::ValidatorCommunication { .. } => "BASILICA_API_VALIDATOR_COMM_ERROR",
            ApiError::QuotaExceeded { .. } => "BASILICA_API_QUOTA_EXCEEDED",
            ApiError::ApiResponse { .. } => "BASILICA_API_RESPONSE_ERROR",
        }
    }

    /// Check if error is retryable
    pub fn is_retryable(&self) -> bool {
        matches!(
            self,
            ApiError::HttpClient(_)
                | ApiError::ValidatorCommunication { .. }
                | ApiError::Timeout
                | ApiError::ServiceUnavailable
        )
    }

    /// Check if error is a client error
    pub fn is_client_error(&self) -> bool {
        matches!(
            self,
            ApiError::MissingAuthentication { .. }
                | ApiError::Authentication { .. }
                | ApiError::Authorization { .. }
                | ApiError::RateLimitExceeded
                | ApiError::InvalidRequest { .. }
                | ApiError::NotFound { .. }
                | ApiError::BadRequest { .. }
                | ApiError::Conflict { .. }
                | ApiError::QuotaExceeded { .. }
        )
    }
}

/// Error response structure from API
#[derive(Debug, Serialize, Deserialize)]
pub struct ErrorResponse {
    /// Error details
    pub error: ErrorDetails,
}

/// Error details structure
#[derive(Debug, Serialize, Deserialize)]
pub struct ErrorDetails {
    /// Error code
    pub code: String,

    /// Human-readable error message
    pub message: String,

    /// ISO 8601 timestamp
    pub timestamp: chrono::DateTime<chrono::Utc>,

    /// Whether the error is retryable
    pub retryable: bool,
}
