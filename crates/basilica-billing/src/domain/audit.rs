use crate::domain::types::CreditBalance;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TransactionType {
    Credit,
    Debit,
    Reserve,
    Release,
}

impl TransactionType {
    pub fn as_str(&self) -> &'static str {
        match self {
            TransactionType::Credit => "credit",
            TransactionType::Debit => "debit",
            TransactionType::Reserve => "reserve",
            TransactionType::Release => "release",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CreditTransaction {
    pub id: Uuid,
    pub user_id: Uuid,
    pub transaction_type: TransactionType,
    pub amount: CreditBalance,
    pub balance_before: CreditBalance,
    pub balance_after: CreditBalance,
    pub reference_id: Option<String>,
    pub reference_type: Option<String>,
    pub description: String,
    pub metadata: Option<serde_json::Value>,
    pub created_at: DateTime<Utc>,
}

impl CreditTransaction {
    pub fn new_debit(
        user_id: Uuid,
        amount: CreditBalance,
        balance_before: CreditBalance,
        balance_after: CreditBalance,
    ) -> Self {
        Self {
            id: Uuid::new_v4(),
            user_id,
            transaction_type: TransactionType::Debit,
            amount,
            balance_before,
            balance_after,
            reference_id: None,
            reference_type: None,
            description: String::from("Credit deduction"),
            metadata: None,
            created_at: Utc::now(),
        }
    }

    pub fn new_credit(
        user_id: Uuid,
        amount: CreditBalance,
        balance_before: CreditBalance,
        balance_after: CreditBalance,
    ) -> Self {
        Self {
            id: Uuid::new_v4(),
            user_id,
            transaction_type: TransactionType::Credit,
            amount,
            balance_before,
            balance_after,
            reference_id: None,
            reference_type: None,
            description: String::from("Credit addition"),
            metadata: None,
            created_at: Utc::now(),
        }
    }

    pub fn with_reference(mut self, id: impl Into<String>, ref_type: impl Into<String>) -> Self {
        self.reference_id = Some(id.into());
        self.reference_type = Some(ref_type.into());
        self
    }

    pub fn with_description(mut self, description: impl Into<String>) -> Self {
        self.description = description.into();
        self
    }

    pub fn with_metadata(mut self, metadata: serde_json::Value) -> Self {
        self.metadata = Some(metadata);
        self
    }
}
