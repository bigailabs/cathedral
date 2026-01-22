use crate::persistence::SimplePersistence;
use anyhow::Result;
use chrono::{DateTime, Utc};
use sqlx::Row;

impl SimplePersistence {
    pub async fn get_undercollateralized_since(
        &self,
        hotkey: &str,
        node_id: &str,
    ) -> Result<Option<DateTime<Utc>>> {
        let row = sqlx::query(
            "SELECT undercollateralized_since FROM collateral_grace_periods WHERE hotkey = ? AND node_id = ?",
        )
        .bind(hotkey)
        .bind(node_id)
        .fetch_optional(self.pool())
        .await?;

        if let Some(row) = row {
            let since: String = row.get(0);
            let since = DateTime::parse_from_rfc3339(&since)
                .map_err(|e| anyhow::anyhow!("Invalid undercollateralized_since: {e}"))?
                .with_timezone(&Utc);
            Ok(Some(since))
        } else {
            Ok(None)
        }
    }

    pub async fn mark_undercollateralized(
        &self,
        hotkey: &str,
        node_id: &str,
        since: DateTime<Utc>,
    ) -> Result<()> {
        let now = Utc::now().to_rfc3339();
        sqlx::query(
            r#"
            INSERT INTO collateral_grace_periods (hotkey, node_id, undercollateralized_since, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(hotkey, node_id)
            DO UPDATE SET undercollateralized_since = excluded.undercollateralized_since, updated_at = excluded.updated_at
            "#,
        )
        .bind(hotkey)
        .bind(node_id)
        .bind(since.to_rfc3339())
        .bind(now)
        .execute(self.pool())
        .await?;
        Ok(())
    }

    pub async fn clear_undercollateralized(&self, hotkey: &str, node_id: &str) -> Result<()> {
        sqlx::query(
            "DELETE FROM collateral_grace_periods WHERE hotkey = ? AND node_id = ?",
        )
        .bind(hotkey)
        .bind(node_id)
        .execute(self.pool())
        .await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::persistence::SimplePersistence;
    use chrono::Duration;

    #[tokio::test]
    async fn test_grace_period_roundtrip() {
        let persistence = SimplePersistence::for_testing().await.unwrap();
        let hotkey = "hotkey-1";
        let node_id = "node-1";
        let since = Utc::now() - Duration::minutes(5);

        persistence
            .mark_undercollateralized(hotkey, node_id, since)
            .await
            .unwrap();

        let fetched = persistence
            .get_undercollateralized_since(hotkey, node_id)
            .await
            .unwrap();
        assert!(fetched.is_some());
        assert_eq!(fetched.unwrap().timestamp(), since.timestamp());

        persistence
            .clear_undercollateralized(hotkey, node_id)
            .await
            .unwrap();
        let cleared = persistence
            .get_undercollateralized_since(hotkey, node_id)
            .await
            .unwrap();
        assert!(cleared.is_none());
    }
}

