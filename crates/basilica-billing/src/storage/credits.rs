use crate::domain::audit::CreditTransaction;
use crate::domain::{
    credits::CreditAccount,
    types::{CreditBalance, UserId},
};
use crate::error::{BillingError, Result};
use crate::storage::audit::AuditRepository;
use crate::storage::rds::RdsConnection;
use async_trait::async_trait;
use sqlx::{Postgres, Row, Transaction};
use std::sync::Arc;
use uuid::Uuid;

#[async_trait]
pub trait CreditRepository: Send + Sync {
    async fn get_account(&self, user_id: &UserId) -> Result<Option<CreditAccount>>;
    async fn create_account(&self, account: &CreditAccount) -> Result<()>;
    async fn update_account(&self, account: &CreditAccount) -> Result<()>;
    async fn update_balance(&self, user_id: &UserId, balance: CreditBalance) -> Result<()>;
    async fn deduct_credits(&self, user_id: &UserId, amount: CreditBalance) -> Result<()>;
}

pub struct SqlCreditRepository {
    connection: Arc<RdsConnection>,
    audit: Arc<dyn AuditRepository>,
}

impl SqlCreditRepository {
    pub fn new(connection: Arc<RdsConnection>, audit: Arc<dyn AuditRepository>) -> Self {
        Self { connection, audit }
    }

    pub fn pool(&self) -> &sqlx::PgPool {
        self.connection.pool()
    }

    async fn resolve_user_uuid(&self, user_id: &UserId) -> Result<Option<Uuid>> {
        if let Ok(uuid) = user_id.as_uuid() {
            return Ok(Some(uuid));
        }

        sqlx::query_scalar::<_, uuid::Uuid>(
            "SELECT user_id FROM billing.users WHERE external_id = $1",
        )
        .bind(user_id.as_str())
        .fetch_optional(self.connection.pool())
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "resolve_user_uuid".to_string(),
            source: Box::new(e),
        })
    }

    async fn require_user_uuid(&self, user_id: &UserId) -> Result<Uuid> {
        self.resolve_user_uuid(user_id)
            .await?
            .ok_or_else(|| BillingError::UserNotFound {
                id: user_id.to_string(),
            })
    }

    async fn ensure_user_uuid(&self, user_id: &UserId) -> Result<Uuid> {
        if let Ok(uuid) = user_id.as_uuid() {
            return Ok(uuid);
        }

        sqlx::query_scalar::<_, uuid::Uuid>(
            r#"
            INSERT INTO billing.users (external_id)
            VALUES ($1)
            ON CONFLICT (external_id) DO UPDATE SET updated_at = NOW()
            RETURNING user_id
            "#,
        )
        .bind(user_id.as_str())
        .fetch_one(self.connection.pool())
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "ensure_user_uuid".to_string(),
            source: Box::new(e),
        })
    }

    async fn record_deduction_event(
        &self,
        executor: impl sqlx::Executor<'_, Database = Postgres>,
        user_id: &UserId,
        user_uuid: Uuid,
        amount: CreditBalance,
    ) -> Result<()> {
        sqlx::query(
            r#"
            INSERT INTO billing.billing_events
                (event_id, event_type, entity_type, entity_id, user_id,
                 event_data, created_by, created_at)
            VALUES ($1, 'credit_deduction', 'user', $2, $3, $4, 'credit_repository', NOW())
            "#,
        )
        .bind(Uuid::new_v4())
        .bind(user_id.as_str())
        .bind(user_uuid)
        .bind(serde_json::json!({ "amount": amount.to_string() }))
        .execute(executor)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "record_deduction_event".to_string(),
            source: Box::new(e),
        })?;

        Ok(())
    }

    async fn deduct_credits_atomic(
        &self,
        tx: &mut Transaction<'_, Postgres>,
        user_uuid: Uuid,
        amount: CreditBalance,
    ) -> Result<(CreditBalance, CreditBalance)> {
        let row = sqlx::query(
            r#"
            UPDATE billing.credits
            SET balance = balance - $2,
                lifetime_spent = lifetime_spent + $2,
                updated_at = NOW()
            WHERE user_id = $1 AND balance >= $2
            RETURNING (balance + $2) as balance_before, balance as balance_after
            "#,
        )
        .bind(user_uuid)
        .bind(amount.as_decimal())
        .fetch_optional(&mut **tx)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "deduct_credits_atomic".to_string(),
            source: Box::new(e),
        })?;

        match row {
            Some(r) => {
                let balance_before = CreditBalance::from_decimal(r.get("balance_before"));
                let balance_after = CreditBalance::from_decimal(r.get("balance_after"));
                Ok((balance_before, balance_after))
            }
            None => {
                let current = sqlx::query_scalar::<_, rust_decimal::Decimal>(
                    "SELECT balance FROM billing.credits WHERE user_id = $1",
                )
                .bind(user_uuid)
                .fetch_optional(&mut **tx)
                .await
                .map_err(|e| BillingError::DatabaseError {
                    operation: "fetch_balance_for_error".to_string(),
                    source: Box::new(e),
                })?
                .unwrap_or(rust_decimal::Decimal::ZERO);

                Err(BillingError::InsufficientCredits {
                    available: current,
                    required: amount.as_decimal(),
                })
            }
        }
    }

    // Transaction history for testing - returns raw transaction data
    pub async fn get_transaction_history(
        &self,
        user_id: &UserId,
        limit: Option<i64>,
    ) -> Result<Vec<CreditTransactionRecord>> {
        let limit = limit.unwrap_or(100);

        let rows = sqlx::query(
            r#"
            SELECT id, user_id, amount, transaction_type, balance_before, balance_after,
                   reference_id, reference_type, description, metadata, created_at
            FROM billing.credit_transactions
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            "#,
        )
        .bind(user_id.as_str())
        .bind(limit)
        .fetch_all(self.connection.pool())
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "get_transaction_history".to_string(),
            source: Box::new(e),
        })?;

        Ok(rows
            .into_iter()
            .map(|row| CreditTransactionRecord {
                id: row.get("id"),
                user_id: UserId::new(row.get("user_id")),
                amount: CreditBalance::from_decimal(row.get("amount")),
                transaction_type: row.get("transaction_type"),
                balance_before: CreditBalance::from_decimal(row.get("balance_before")),
                balance_after: CreditBalance::from_decimal(row.get("balance_after")),
                reference_id: row.get("reference_id"),
                reference_type: row.get("reference_type"),
                description: row.get("description"),
                metadata: row.get("metadata"),
                created_at: row.get("created_at"),
            })
            .collect())
    }
}

// Simple transaction record for querying history
#[derive(Debug, Clone)]
pub struct CreditTransactionRecord {
    pub id: uuid::Uuid,
    pub user_id: UserId,
    pub amount: CreditBalance,
    pub transaction_type: String,
    pub balance_before: CreditBalance,
    pub balance_after: CreditBalance,
    pub reference_id: Option<String>,
    pub reference_type: Option<String>,
    pub description: String,
    pub metadata: Option<serde_json::Value>,
    pub created_at: chrono::DateTime<chrono::Utc>,
}

#[async_trait]
impl CreditRepository for SqlCreditRepository {
    async fn get_account(&self, user_id: &UserId) -> Result<Option<CreditAccount>> {
        let user_uuid = match self.resolve_user_uuid(user_id).await? {
            Some(uuid) => uuid,
            None => return Ok(None),
        };

        let row = sqlx::query(
            r#"
            SELECT c.user_id, c.balance, c.lifetime_spent, c.updated_at
            FROM billing.credits c
            WHERE c.user_id = $1
            "#,
        )
        .bind(user_uuid)
        .fetch_optional(self.connection.pool())
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "get_account".to_string(),
            source: Box::new(e),
        })?;

        Ok(row.map(|r| {
            let uuid: uuid::Uuid = r.get("user_id");
            CreditAccount {
                user_id: UserId::from_uuid(uuid),
                balance: CreditBalance::from_decimal(r.get("balance")),
                lifetime_spent: CreditBalance::from_decimal(r.get("lifetime_spent")),
                last_updated: r.get("updated_at"),
            }
        }))
    }

    async fn create_account(&self, account: &CreditAccount) -> Result<()> {
        let user_uuid = self.ensure_user_uuid(&account.user_id).await?;

        sqlx::query(
            r#"
            INSERT INTO billing.credits (user_id, balance, lifetime_spent, updated_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO NOTHING
            "#,
        )
        .bind(user_uuid)
        .bind(account.balance.as_decimal())
        .bind(account.lifetime_spent.as_decimal())
        .bind(account.last_updated)
        .execute(self.connection.pool())
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "create_account".to_string(),
            source: Box::new(e),
        })?;

        Ok(())
    }

    async fn update_account(&self, account: &CreditAccount) -> Result<()> {
        let user_uuid = self.require_user_uuid(&account.user_id).await?;

        let result = sqlx::query(
            r#"
            UPDATE billing.credits
            SET balance = $2, lifetime_spent = $3, updated_at = $4
            WHERE user_id = $1
            "#,
        )
        .bind(user_uuid)
        .bind(account.balance.as_decimal())
        .bind(account.lifetime_spent.as_decimal())
        .bind(account.last_updated)
        .execute(self.connection.pool())
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "update_account".to_string(),
            source: Box::new(e),
        })?;

        if result.rows_affected() == 0 {
            return Err(BillingError::UserNotFound {
                id: account.user_id.to_string(),
            });
        }

        Ok(())
    }

    async fn update_balance(&self, user_id: &UserId, balance: CreditBalance) -> Result<()> {
        let user_uuid = self.require_user_uuid(user_id).await?;

        sqlx::query(
            r#"
            UPDATE billing.credits
            SET balance = $2, updated_at = NOW()
            WHERE user_id = $1
            "#,
        )
        .bind(user_uuid)
        .bind(balance.as_decimal())
        .execute(self.connection.pool())
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "update_balance".to_string(),
            source: Box::new(e),
        })?;

        Ok(())
    }

    async fn deduct_credits(&self, user_id: &UserId, amount: CreditBalance) -> Result<()> {
        let mut tx =
            self.connection
                .pool()
                .begin()
                .await
                .map_err(|e| BillingError::DatabaseError {
                    operation: "begin_transaction".to_string(),
                    source: Box::new(e),
                })?;

        self.deduct_credits_tx(
            &mut tx,
            user_id,
            amount,
            None,
            Some("system"),
            Some("Credit deduction"),
        )
        .await?;

        tx.commit().await.map_err(|e| BillingError::DatabaseError {
            operation: "commit_transaction".to_string(),
            source: Box::new(e),
        })?;

        Ok(())
    }
}

impl SqlCreditRepository {
    pub async fn deduct_credits_tx(
        &self,
        tx: &mut Transaction<'_, Postgres>,
        user_id: &UserId,
        amount: CreditBalance,
        reference_id: Option<&str>,
        reference_type: Option<&str>,
        description: Option<&str>,
    ) -> Result<()> {
        let user_uuid = self.require_user_uuid(user_id).await?;

        let (balance_before, balance_after) = self
            .deduct_credits_atomic(&mut *tx, user_uuid, amount)
            .await?;

        let mut transaction =
            CreditTransaction::new_debit(user_uuid, amount, balance_before, balance_after);

        if let Some(ref_id) = reference_id {
            transaction = transaction.with_reference(ref_id, reference_type.unwrap_or("rental"));
        }

        if let Some(desc) = description {
            transaction = transaction.with_description(desc);
        }

        self.audit
            .record_transaction_tx(&mut *tx, &transaction)
            .await?;

        self.record_deduction_event(&mut **tx, user_id, user_uuid, amount)
            .await?;

        Ok(())
    }
}
