use super::{MonitorStateRepo, PgRepos};
use sqlx::Row;

#[async_trait::async_trait]
impl MonitorStateRepo for PgRepos {
    async fn get_last_scanned_block(&self, monitor_id: &str) -> sqlx::Result<Option<u32>> {
        let row =
            sqlx::query(r#"SELECT last_scanned_block FROM monitor_state WHERE monitor_id = $1"#)
                .bind(monitor_id)
                .fetch_optional(&self.pool)
                .await?;

        Ok(row.map(|r| {
            let block: i64 = r.get("last_scanned_block");
            block as u32
        }))
    }

    async fn update_last_scanned_block(
        &self,
        monitor_id: &str,
        block_number: u32,
    ) -> sqlx::Result<()> {
        sqlx::query(
            r#"INSERT INTO monitor_state (monitor_id, last_scanned_block, updated_at)
               VALUES ($1, $2, now())
               ON CONFLICT (monitor_id)
               DO UPDATE SET last_scanned_block = $2, updated_at = now()"#,
        )
        .bind(monitor_id)
        .bind(block_number as i64)
        .execute(&self.pool)
        .await?;

        Ok(())
    }
}
