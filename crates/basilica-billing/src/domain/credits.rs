use crate::domain::types::{CreditBalance, UserId};
use crate::error::{BillingError, Result};
use crate::storage::CreditRepository;
use async_trait::async_trait;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CreditAccount {
    pub user_id: UserId,
    pub balance: CreditBalance,
    pub lifetime_spent: CreditBalance,
    pub last_updated: DateTime<Utc>,
}

impl CreditAccount {
    pub fn new(user_id: UserId) -> Self {
        Self {
            user_id,
            balance: CreditBalance::zero(),
            lifetime_spent: CreditBalance::zero(),
            last_updated: Utc::now(),
        }
    }

    pub fn apply_credits(&mut self, amount: CreditBalance) {
        self.balance = self.balance.add(amount);
        self.last_updated = Utc::now();
    }

    pub fn charge(&mut self, amount: CreditBalance) -> Result<()> {
        let new_balance =
            self.balance
                .subtract(amount)
                .ok_or_else(|| BillingError::InsufficientBalance {
                    available: self.balance.as_decimal(),
                    required: amount.as_decimal(),
                })?;
        self.balance = new_balance;
        self.lifetime_spent = self.lifetime_spent.add(amount);
        self.last_updated = Utc::now();
        Ok(())
    }
}

#[async_trait]
pub trait CreditOperations: Send + Sync {
    async fn get_balance(&self, user_id: &UserId) -> Result<CreditBalance>;
    async fn get_account(&self, user_id: &UserId) -> Result<CreditAccount>;
    async fn apply_credits(&self, user_id: &UserId, amount: CreditBalance)
        -> Result<CreditBalance>;
    async fn charge_credits(
        &self,
        user_id: &UserId,
        amount: CreditBalance,
    ) -> Result<CreditBalance>;
}

pub struct CreditManager {
    repository: Arc<dyn CreditRepository + Send + Sync>,
}

impl CreditManager {
    pub fn new(repository: Arc<dyn CreditRepository + Send + Sync>) -> Self {
        Self { repository }
    }

    async fn get_or_create_account(&self, user_id: &UserId) -> Result<CreditAccount> {
        if let Some(account) = self.repository.get_account(user_id).await? {
            return Ok(account);
        }

        let new_account = CreditAccount::new(user_id.clone());
        self.repository.create_account(&new_account).await?;

        self.repository
            .get_account(user_id)
            .await?
            .ok_or_else(|| BillingError::DatabaseError {
                operation: "get_or_create_account".to_string(),
                source: "Failed to fetch account after creation".into(),
            })
    }
}

#[async_trait]
impl CreditOperations for CreditManager {
    async fn get_balance(&self, user_id: &UserId) -> Result<CreditBalance> {
        let account = self.get_or_create_account(user_id).await?;
        Ok(account.balance)
    }

    async fn get_account(&self, user_id: &UserId) -> Result<CreditAccount> {
        self.get_or_create_account(user_id).await
    }

    async fn apply_credits(
        &self,
        user_id: &UserId,
        amount: CreditBalance,
    ) -> Result<CreditBalance> {
        let mut account = self.get_or_create_account(user_id).await?;

        account.apply_credits(amount);

        self.repository.update_account(&account).await?;

        Ok(account.balance)
    }

    async fn charge_credits(
        &self,
        user_id: &UserId,
        amount: CreditBalance,
    ) -> Result<CreditBalance> {
        let mut account = self.repository.get_account(user_id).await?.ok_or_else(|| {
            BillingError::UserNotFound {
                id: user_id.to_string(),
            }
        })?;

        account.charge(amount)?;

        self.repository.update_account(&account).await?;

        Ok(account.balance)
    }
}
