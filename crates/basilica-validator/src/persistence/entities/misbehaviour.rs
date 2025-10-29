use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::fmt;
use std::str::FromStr;

/// Type of misbehaviour that can trigger a ban
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum MisbehaviourType {
    /// Failed rental request
    BadRental,
    /// Rejected rental request
    RejectedRental,
    /// Rental halted unexpectedly
    HaltedRental,
    /// Provided malicious or incorrect results
    MaliciousResult,
}

impl MisbehaviourType {
    /// Convert to database string representation
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::BadRental => "bad_rental",
            Self::RejectedRental => "rejected_rental",
            Self::HaltedRental => "halted_rental",
            Self::MaliciousResult => "malicious_result",
        }
    }
}

impl FromStr for MisbehaviourType {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "bad_rental" => Ok(Self::BadRental),
            "rejected_rental" => Ok(Self::RejectedRental),
            "halted_rental" => Ok(Self::HaltedRental),
            "malicious_result" => Ok(Self::MaliciousResult),
            _ => Err(format!("Unknown misbehaviour type: {}", s)),
        }
    }
}

impl fmt::Display for MisbehaviourType {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.as_str())
    }
}

/// Log entry for executor misbehaviour
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MisbehaviourLog {
    /// Miner UID
    pub miner_uid: u16,
    /// Executor ID
    pub executor_id: String,
    /// GPU UUID that misbehaved
    pub gpu_uuid: String,
    /// When the misbehaviour was recorded
    pub recorded_at: DateTime<Utc>,
    /// Executor endpoint
    pub endpoint_executor: String,
    /// Type of misbehaviour
    pub type_of_misbehaviour: MisbehaviourType,
    /// JSON details of the misbehaviour
    pub details: String,
    /// When the record was created
    pub created_at: DateTime<Utc>,
    /// When the record was last updated
    pub updated_at: DateTime<Utc>,
}

impl MisbehaviourLog {
    /// Create a new misbehaviour log entry
    pub fn new(
        miner_uid: u16,
        executor_id: String,
        gpu_uuid: String,
        endpoint_executor: String,
        type_of_misbehaviour: MisbehaviourType,
        details: String,
    ) -> Self {
        let now = Utc::now();
        Self {
            miner_uid,
            executor_id,
            gpu_uuid,
            recorded_at: now,
            endpoint_executor,
            type_of_misbehaviour,
            details,
            created_at: now,
            updated_at: now,
        }
    }
}
