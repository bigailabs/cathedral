//! # Extrinsic Response
//!
//! Standardized response type for all blockchain extrinsics.

use crate::error::BittensorError;
use crate::types::Balance;
use serde::{Deserialize, Serialize};

/// Status of an extrinsic submission
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ExtrinsicStatus {
    /// Transaction was successfully included in a block
    Success,
    /// Transaction failed with an error
    Failed,
    /// Transaction is pending/in progress
    Pending,
    /// Transaction was dropped from the mempool
    Dropped,
}

impl std::fmt::Display for ExtrinsicStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Success => write!(f, "Success"),
            Self::Failed => write!(f, "Failed"),
            Self::Pending => write!(f, "Pending"),
            Self::Dropped => write!(f, "Dropped"),
        }
    }
}

/// Standardized response from an extrinsic submission
///
/// This type provides consistent access to transaction results including:
/// - Success/failure status
/// - Block and extrinsic hashes
/// - Fee information
/// - Error details if failed
/// - Optional typed return data
///
/// # Example
///
/// ```
/// use bittensor::extrinsics::{ExtrinsicResponse, ExtrinsicStatus};
///
/// // Create a successful response
/// let response = ExtrinsicResponse::<u64>::success()
///     .with_message("Transfer completed")
///     .with_block_hash("0x1234...")
///     .with_data(100);
///
/// assert!(response.is_success());
/// assert_eq!(response.data, Some(100));
/// ```
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExtrinsicResponse<T = ()> {
    /// Whether the extrinsic succeeded
    pub status: ExtrinsicStatus,
    /// Human-readable message about the result
    pub message: String,
    /// Block hash where the extrinsic was included
    pub block_hash: Option<String>,
    /// Extrinsic hash for tracking
    pub extrinsic_hash: Option<String>,
    /// Block number where the extrinsic was included
    pub block_number: Option<u64>,
    /// Transaction fee paid
    pub fee: Option<Balance>,
    /// Optional typed return data
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<T>,
    /// Error details if the extrinsic failed
    #[serde(skip)]
    pub error: Option<BittensorError>,
}

impl<T> ExtrinsicResponse<T> {
    /// Create a new successful response
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::extrinsics::ExtrinsicResponse;
    ///
    /// let response: ExtrinsicResponse<()> = ExtrinsicResponse::success();
    /// assert!(response.is_success());
    /// ```
    pub fn success() -> Self {
        Self {
            status: ExtrinsicStatus::Success,
            message: "Extrinsic succeeded".to_string(),
            block_hash: None,
            extrinsic_hash: None,
            block_number: None,
            fee: None,
            data: None,
            error: None,
        }
    }

    /// Create a new failed response
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::extrinsics::ExtrinsicResponse;
    ///
    /// let response: ExtrinsicResponse<()> = ExtrinsicResponse::failed("Something went wrong");
    /// assert!(!response.is_success());
    /// ```
    pub fn failed(message: &str) -> Self {
        Self {
            status: ExtrinsicStatus::Failed,
            message: message.to_string(),
            block_hash: None,
            extrinsic_hash: None,
            block_number: None,
            fee: None,
            data: None,
            error: None,
        }
    }

    /// Create a response from an error
    pub fn from_error(error: BittensorError) -> Self {
        Self {
            status: ExtrinsicStatus::Failed,
            message: error.to_string(),
            block_hash: None,
            extrinsic_hash: None,
            block_number: None,
            fee: None,
            data: None,
            error: Some(error),
        }
    }

    /// Check if the extrinsic succeeded
    pub fn is_success(&self) -> bool {
        self.status == ExtrinsicStatus::Success
    }

    /// Check if the extrinsic failed
    pub fn is_failed(&self) -> bool {
        self.status == ExtrinsicStatus::Failed
    }

    /// Set the message
    pub fn with_message(mut self, message: &str) -> Self {
        self.message = message.to_string();
        self
    }

    /// Set the block hash
    pub fn with_block_hash(mut self, hash: &str) -> Self {
        self.block_hash = Some(hash.to_string());
        self
    }

    /// Set the extrinsic hash
    pub fn with_extrinsic_hash(mut self, hash: &str) -> Self {
        self.extrinsic_hash = Some(hash.to_string());
        self
    }

    /// Set the block number
    pub fn with_block_number(mut self, number: u64) -> Self {
        self.block_number = Some(number);
        self
    }

    /// Set the fee
    pub fn with_fee(mut self, fee: Balance) -> Self {
        self.fee = Some(fee);
        self
    }

    /// Set the return data
    pub fn with_data(mut self, data: T) -> Self {
        self.data = Some(data);
        self
    }

    /// Set the error
    pub fn with_error(mut self, error: BittensorError) -> Self {
        self.error = Some(error);
        self
    }

    /// Convert to a Result type
    ///
    /// Returns `Ok(data)` if successful and data is present,
    /// otherwise returns `Err` with the error or a generic error.
    pub fn into_result(self) -> Result<T, BittensorError>
    where
        T: Default,
    {
        if self.is_success() {
            Ok(self.data.unwrap_or_default())
        } else {
            Err(self.error.unwrap_or(BittensorError::ChainError {
                message: self.message,
            }))
        }
    }

    /// Map the data to a different type
    pub fn map<U, F>(self, f: F) -> ExtrinsicResponse<U>
    where
        F: FnOnce(T) -> U,
    {
        ExtrinsicResponse {
            status: self.status,
            message: self.message,
            block_hash: self.block_hash,
            extrinsic_hash: self.extrinsic_hash,
            block_number: self.block_number,
            fee: self.fee,
            data: self.data.map(f),
            error: self.error,
        }
    }

    /// Discard the data, keeping only the metadata
    pub fn discard_data(self) -> ExtrinsicResponse<()> {
        ExtrinsicResponse {
            status: self.status,
            message: self.message,
            block_hash: self.block_hash,
            extrinsic_hash: self.extrinsic_hash,
            block_number: self.block_number,
            fee: self.fee,
            data: Some(()),
            error: self.error,
        }
    }
}

impl<T> Default for ExtrinsicResponse<T> {
    fn default() -> Self {
        Self::success()
    }
}

impl<T: std::fmt::Display> std::fmt::Display for ExtrinsicResponse<T> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "[{}] {}", self.status, self.message)?;
        if let Some(ref hash) = self.block_hash {
            write!(f, " (block: {})", hash)?;
        }
        if let Some(ref fee) = self.fee {
            write!(f, " (fee: {})", fee)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_success_response() {
        let response: ExtrinsicResponse<()> = ExtrinsicResponse::success();
        assert!(response.is_success());
        assert!(!response.is_failed());
        assert_eq!(response.status, ExtrinsicStatus::Success);
    }

    #[test]
    fn test_failed_response() {
        let response: ExtrinsicResponse<()> = ExtrinsicResponse::failed("test error");
        assert!(!response.is_success());
        assert!(response.is_failed());
        assert_eq!(response.message, "test error");
    }

    #[test]
    fn test_builder_pattern() {
        let response = ExtrinsicResponse::<u64>::success()
            .with_message("Transfer completed")
            .with_block_hash("0x1234")
            .with_extrinsic_hash("0x5678")
            .with_block_number(100)
            .with_fee(Balance::from_tao(0.001))
            .with_data(42);

        assert!(response.is_success());
        assert_eq!(response.message, "Transfer completed");
        assert_eq!(response.block_hash, Some("0x1234".to_string()));
        assert_eq!(response.extrinsic_hash, Some("0x5678".to_string()));
        assert_eq!(response.block_number, Some(100));
        assert!(response.fee.is_some());
        assert_eq!(response.data, Some(42));
    }

    #[test]
    fn test_from_error() {
        let error = BittensorError::ChainError {
            message: "test".to_string(),
        };
        let response: ExtrinsicResponse<()> = ExtrinsicResponse::from_error(error);
        assert!(response.is_failed());
        assert!(response.error.is_some());
    }

    #[test]
    fn test_into_result_success() {
        let response = ExtrinsicResponse::<u64>::success().with_data(42);
        let result = response.into_result();
        assert_eq!(result.unwrap(), 42);
    }

    #[test]
    fn test_into_result_failure() {
        let response: ExtrinsicResponse<u64> = ExtrinsicResponse::failed("test error");
        let result = response.into_result();
        assert!(result.is_err());
    }

    #[test]
    fn test_map() {
        let response = ExtrinsicResponse::<u64>::success()
            .with_data(42)
            .map(|x| x.to_string());

        assert_eq!(response.data, Some("42".to_string()));
    }

    #[test]
    fn test_discard_data() {
        let response = ExtrinsicResponse::<u64>::success()
            .with_data(42)
            .discard_data();

        assert_eq!(response.data, Some(()));
        assert!(response.is_success());
    }

    #[test]
    fn test_display() {
        let response = ExtrinsicResponse::<String>::success()
            .with_message("Done")
            .with_block_hash("0x1234");

        let display = format!("{}", response);
        assert!(display.contains("Success"));
        assert!(display.contains("Done"));
        assert!(display.contains("0x1234"));
    }

    #[test]
    fn test_serialization() {
        let response = ExtrinsicResponse::<u64>::success()
            .with_message("test")
            .with_data(42);

        let json = serde_json::to_string(&response).unwrap();
        assert!(json.contains("\"status\":\"Success\""));
        assert!(json.contains("\"data\":42"));
    }
}
