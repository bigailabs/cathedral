use thiserror::Error;

#[derive(Error, Debug)]
pub enum AggregatorError {
    #[error("Database error: {0}")]
    Database(#[from] sqlx::Error),

    #[error("HTTP client error: {0}")]
    Http(#[from] reqwest::Error),

    #[error("Configuration error: {0}")]
    Config(String),

    #[error("Provider error ({provider}): {message}")]
    Provider { provider: String, message: String },

    #[error("Normalization error: {0}")]
    Normalization(String),

    #[error("No providers available")]
    NoProvidersAvailable,

    #[error("Resource not found: {0}")]
    NotFound(String),

    #[error("User already has an SSH key registered")]
    SshKeyAlreadyExists,

    #[error("No SSH key found for user")]
    SshKeyNotFound,

    #[error("Invalid SSH public key format")]
    InvalidSshKey,
}

pub type Result<T> = std::result::Result<T, AggregatorError>;
