use crate::domain::audit::{CreditTransaction, TransactionType};
use crate::domain::types::{CreditBalance, UserId};
use crate::error::{BillingError, Result};
use crate::storage::rds::RdsConnection;
use async_trait::async_trait;
use sqlx::{Postgres, Row, Transaction};
use std::sync::Arc;

#[async_trait]
pub trait AuditRepository: Send + Sync {
    async fn record_transaction(&self, transaction: &CreditTransaction) -> Result<()>;

    async fn record_transaction_tx(
        &self,
        tx: &mut Transaction<'_, Postgres>,
        transaction: &CreditTransaction,
    ) -> Result<()>;

    async fn get_transaction_history(
        &self,
        user_id: &UserId,
        limit: Option<i64>,
    ) -> Result<Vec<CreditTransaction>>;

    async fn get_transactions_by_reference(
        &self,
        reference_id: &str,
        reference_type: Option<&str>,
    ) -> Result<Vec<CreditTransaction>>;
}

pub struct SqlAuditRepository {
    connection: Arc<RdsConnection>,
}

impl SqlAuditRepository {
    pub fn new(connection: Arc<RdsConnection>) -> Self {
        Self { connection }
    }

    pub fn pool(&self) -> &sqlx::PgPool {
        self.connection.pool()
    }

    fn transaction_from_row(row: &sqlx::postgres::PgRow) -> CreditTransaction {
        CreditTransaction {
            id: row.get("id"),
            user_id: row.get("user_id"),
            transaction_type: match row.get::<String, _>("transaction_type").as_str() {
                "credit" => TransactionType::Credit,
                "debit" => TransactionType::Debit,
                "reserve" => TransactionType::Reserve,
                "release" => TransactionType::Release,
                _ => TransactionType::Debit,
            },
            amount: CreditBalance::from_decimal(row.get("amount")),
            balance_before: CreditBalance::from_decimal(row.get("balance_before")),
            balance_after: CreditBalance::from_decimal(row.get("balance_after")),
            reference_id: row.get("reference_id"),
            reference_type: row.get("reference_type"),
            description: row.get("description"),
            metadata: row.get("metadata"),
            created_at: row.get("created_at"),
        }
    }
}

#[async_trait]
impl AuditRepository for SqlAuditRepository {
    async fn record_transaction(&self, transaction: &CreditTransaction) -> Result<()> {
        sqlx::query(
            r#"
            INSERT INTO billing.credit_transactions
                (id, user_id, transaction_type, amount, balance_before, balance_after,
                 reference_id, reference_type, description, metadata, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            "#,
        )
        .bind(transaction.id)
        .bind(transaction.user_id)
        .bind(transaction.transaction_type.as_str())
        .bind(transaction.amount.as_decimal())
        .bind(transaction.balance_before.as_decimal())
        .bind(transaction.balance_after.as_decimal())
        .bind(&transaction.reference_id)
        .bind(&transaction.reference_type)
        .bind(&transaction.description)
        .bind(&transaction.metadata)
        .bind(transaction.created_at)
        .execute(self.connection.pool())
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "record_transaction".to_string(),
            source: Box::new(e),
        })?;

        Ok(())
    }

    async fn record_transaction_tx(
        &self,
        tx: &mut Transaction<'_, Postgres>,
        transaction: &CreditTransaction,
    ) -> Result<()> {
        sqlx::query(
            r#"
            INSERT INTO billing.credit_transactions
                (id, user_id, transaction_type, amount, balance_before, balance_after,
                 reference_id, reference_type, description, metadata, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            "#,
        )
        .bind(transaction.id)
        .bind(transaction.user_id)
        .bind(transaction.transaction_type.as_str())
        .bind(transaction.amount.as_decimal())
        .bind(transaction.balance_before.as_decimal())
        .bind(transaction.balance_after.as_decimal())
        .bind(&transaction.reference_id)
        .bind(&transaction.reference_type)
        .bind(&transaction.description)
        .bind(&transaction.metadata)
        .bind(transaction.created_at)
        .execute(&mut **tx)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "record_transaction_tx".to_string(),
            source: Box::new(e),
        })?;

        Ok(())
    }

    async fn get_transaction_history(
        &self,
        user_id: &UserId,
        limit: Option<i64>,
    ) -> Result<Vec<CreditTransaction>> {
        let limit = limit.unwrap_or(100);

        let rows = sqlx::query(
            r#"
            SELECT id, user_id, transaction_type, amount, balance_before, balance_after,
                   reference_id, reference_type, description, metadata, created_at
            FROM billing.credit_transactions
            WHERE user_id = (SELECT user_id FROM billing.users WHERE external_id = $1)
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

        Ok(rows.iter().map(Self::transaction_from_row).collect())
    }

    async fn get_transactions_by_reference(
        &self,
        reference_id: &str,
        reference_type: Option<&str>,
    ) -> Result<Vec<CreditTransaction>> {
        let query = match reference_type {
            Some(ref_type) => sqlx::query(
                r#"
                SELECT id, user_id, transaction_type, amount, balance_before, balance_after,
                       reference_id, reference_type, description, metadata, created_at
                FROM billing.credit_transactions
                WHERE reference_id = $1 AND reference_type = $2
                ORDER BY created_at ASC
                "#,
            )
            .bind(reference_id)
            .bind(ref_type),
            None => sqlx::query(
                r#"
                SELECT id, user_id, transaction_type, amount, balance_before, balance_after,
                       reference_id, reference_type, description, metadata, created_at
                FROM billing.credit_transactions
                WHERE reference_id = $1
                ORDER BY created_at ASC
                "#,
            )
            .bind(reference_id),
        };

        let rows = query.fetch_all(self.connection.pool()).await.map_err(|e| {
            BillingError::DatabaseError {
                operation: "get_transactions_by_reference".to_string(),
                source: Box::new(e),
            }
        })?;

        Ok(rows.iter().map(Self::transaction_from_row).collect())
    }
}
