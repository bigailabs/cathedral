use super::{PgRepos, PgTx};
use crate::reconciliation::{ReconciliationSweep, SweepStatus};
use sqlx::{types::Decimal, Result, Row};
use std::str::FromStr;

#[async_trait::async_trait]
pub trait ReconciliationRepo {
    #[allow(clippy::too_many_arguments)]
    async fn insert_sweep_tx(
        &self,
        tx: &mut PgTx<'_>,
        account_hex: &str,
        hotwallet_ss58: &str,
        coldwallet_ss58: &str,
        balance_before: &str,
        sweep_amount: &str,
        estimated_fee: &str,
        dry_run: bool,
    ) -> Result<i64>;

    #[allow(clippy::too_many_arguments)]
    async fn update_sweep_status_tx(
        &self,
        tx: &mut PgTx<'_>,
        sweep_id: i64,
        status: SweepStatus,
        tx_hash: Option<&str>,
        block_number: Option<i64>,
        balance_after: Option<&str>,
        error_message: Option<&str>,
    ) -> Result<()>;

    async fn get_recent_sweep(&self, account_hex: &str, within_seconds: i64) -> Result<bool>;

    async fn get_pending_sweep(&self, account_hex: &str) -> Result<Option<i64>>;

    async fn list_recent_sweeps(&self, limit: i64) -> Result<Vec<ReconciliationSweep>>;

    /// List sweeps stuck in pending/submitted state older than threshold
    async fn list_stale_sweeps(&self, stale_seconds: i64) -> Result<Vec<ReconciliationSweep>>;
}

#[async_trait::async_trait]
impl ReconciliationRepo for PgRepos {
    async fn insert_sweep_tx(
        &self,
        tx: &mut PgTx<'_>,
        account_hex: &str,
        hotwallet_ss58: &str,
        coldwallet_ss58: &str,
        balance_before: &str,
        sweep_amount: &str,
        estimated_fee: &str,
        dry_run: bool,
    ) -> Result<i64> {
        let balance_before_dec =
            Decimal::from_str(balance_before).map_err(|e| sqlx::Error::Decode(Box::new(e)))?;
        let sweep_amount_dec =
            Decimal::from_str(sweep_amount).map_err(|e| sqlx::Error::Decode(Box::new(e)))?;
        let estimated_fee_dec =
            Decimal::from_str(estimated_fee).map_err(|e| sqlx::Error::Decode(Box::new(e)))?;

        let row = sqlx::query(
            r#"INSERT INTO reconciliation_sweeps
               (account_hex, hotwallet_address_ss58, coldwallet_address_ss58,
                balance_before_plancks, sweep_amount_plancks, estimated_fee_plancks,
                status, dry_run)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               RETURNING id"#,
        )
        .bind(account_hex)
        .bind(hotwallet_ss58)
        .bind(coldwallet_ss58)
        .bind(balance_before_dec)
        .bind(sweep_amount_dec)
        .bind(estimated_fee_dec)
        .bind("pending")
        .bind(dry_run)
        .fetch_one(&mut **tx)
        .await?;

        Ok(row.get("id"))
    }

    async fn update_sweep_status_tx(
        &self,
        tx: &mut PgTx<'_>,
        sweep_id: i64,
        status: SweepStatus,
        tx_hash: Option<&str>,
        block_number: Option<i64>,
        balance_after: Option<&str>,
        error_message: Option<&str>,
    ) -> Result<()> {
        let balance_after_dec = balance_after
            .map(Decimal::from_str)
            .transpose()
            .map_err(|e| sqlx::Error::Decode(Box::new(e)))?;

        sqlx::query(
            r#"UPDATE reconciliation_sweeps
               SET status = $1, tx_hash = $2, block_number = $3,
                   balance_after_plancks = $4, error_message = $5,
                   completed_at = CASE WHEN $1 IN ('confirmed', 'failed') THEN now() ELSE completed_at END
               WHERE id = $6"#,
        )
        .bind(status.as_str())
        .bind(tx_hash)
        .bind(block_number)
        .bind(balance_after_dec)
        .bind(error_message)
        .bind(sweep_id)
        .execute(&mut **tx)
        .await?;

        Ok(())
    }

    async fn get_recent_sweep(&self, account_hex: &str, within_seconds: i64) -> Result<bool> {
        let row = sqlx::query(
            r#"SELECT EXISTS(
                 SELECT 1 FROM reconciliation_sweeps
                 WHERE account_hex = $1
                 AND initiated_at > now() - interval '1 second' * $2
                 AND status != 'failed'
               ) as exists"#,
        )
        .bind(account_hex)
        .bind(within_seconds)
        .fetch_one(&self.pool)
        .await?;

        Ok(row.get("exists"))
    }

    async fn get_pending_sweep(&self, account_hex: &str) -> Result<Option<i64>> {
        let row = sqlx::query(
            r#"SELECT id FROM reconciliation_sweeps
               WHERE account_hex = $1
               AND status IN ('pending', 'submitted')
               ORDER BY initiated_at DESC
               LIMIT 1"#,
        )
        .bind(account_hex)
        .fetch_optional(&self.pool)
        .await?;

        Ok(row.map(|r| r.get("id")))
    }

    async fn list_recent_sweeps(&self, limit: i64) -> Result<Vec<ReconciliationSweep>> {
        let rows = sqlx::query(
            r#"SELECT id, account_hex, hotwallet_address_ss58, coldwallet_address_ss58,
                      balance_before_plancks, sweep_amount_plancks, estimated_fee_plancks,
                      balance_after_plancks, status, dry_run, tx_hash, block_number,
                      error_message, initiated_at, completed_at
               FROM reconciliation_sweeps
               ORDER BY initiated_at DESC
               LIMIT $1"#,
        )
        .bind(limit)
        .fetch_all(&self.pool)
        .await?;

        let sweeps = rows
            .into_iter()
            .map(|row| {
                let status_str: String = row.get("status");
                let status = match status_str.as_str() {
                    "pending" => SweepStatus::Pending,
                    "submitted" => SweepStatus::Submitted,
                    "confirmed" => SweepStatus::Confirmed,
                    "failed" => SweepStatus::Failed,
                    _ => SweepStatus::Pending,
                };

                ReconciliationSweep {
                    id: row.get("id"),
                    account_hex: row.get("account_hex"),
                    hotwallet_address_ss58: row.get("hotwallet_address_ss58"),
                    coldwallet_address_ss58: row.get("coldwallet_address_ss58"),
                    balance_before_plancks: row.get("balance_before_plancks"),
                    sweep_amount_plancks: row.get("sweep_amount_plancks"),
                    estimated_fee_plancks: row.get("estimated_fee_plancks"),
                    balance_after_plancks: row.get("balance_after_plancks"),
                    status,
                    dry_run: row.get("dry_run"),
                    tx_hash: row.get("tx_hash"),
                    block_number: row.get("block_number"),
                    error_message: row.get("error_message"),
                    initiated_at: row.get("initiated_at"),
                    completed_at: row.get("completed_at"),
                }
            })
            .collect();

        Ok(sweeps)
    }

    async fn list_stale_sweeps(&self, stale_seconds: i64) -> Result<Vec<ReconciliationSweep>> {
        let rows = sqlx::query(
            r#"SELECT id, account_hex, hotwallet_address_ss58, coldwallet_address_ss58,
                      balance_before_plancks, sweep_amount_plancks, estimated_fee_plancks,
                      balance_after_plancks, status, dry_run, tx_hash, block_number,
                      error_message, initiated_at, completed_at
               FROM reconciliation_sweeps
               WHERE status IN ('pending', 'submitted')
               AND initiated_at < now() - interval '1 second' * $1
               AND dry_run = false
               ORDER BY initiated_at ASC
               LIMIT 100"#,
        )
        .bind(stale_seconds)
        .fetch_all(&self.pool)
        .await?;

        let sweeps = rows
            .into_iter()
            .map(|row| {
                let status_str: String = row.get("status");
                let status = match status_str.as_str() {
                    "pending" => SweepStatus::Pending,
                    "submitted" => SweepStatus::Submitted,
                    "confirmed" => SweepStatus::Confirmed,
                    "failed" => SweepStatus::Failed,
                    _ => SweepStatus::Pending,
                };

                ReconciliationSweep {
                    id: row.get("id"),
                    account_hex: row.get("account_hex"),
                    hotwallet_address_ss58: row.get("hotwallet_address_ss58"),
                    coldwallet_address_ss58: row.get("coldwallet_address_ss58"),
                    balance_before_plancks: row.get("balance_before_plancks"),
                    sweep_amount_plancks: row.get("sweep_amount_plancks"),
                    estimated_fee_plancks: row.get("estimated_fee_plancks"),
                    balance_after_plancks: row.get("balance_after_plancks"),
                    status,
                    dry_run: row.get("dry_run"),
                    tx_hash: row.get("tx_hash"),
                    block_number: row.get("block_number"),
                    error_message: row.get("error_message"),
                    initiated_at: row.get("initiated_at"),
                    completed_at: row.get("completed_at"),
                }
            })
            .collect();

        Ok(sweeps)
    }
}
