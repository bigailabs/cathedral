use anyhow::Result;
use basilica_protocol::billing::MinerDelivery;
use chrono::{DateTime, Utc};
use sqlx::{QueryBuilder, Row, Sqlite};
use std::sync::Arc;

use crate::persistence::SimplePersistence;

#[derive(Clone)]
pub struct MinerDeliveryRepository {
    persistence: Arc<SimplePersistence>,
}

impl MinerDeliveryRepository {
    pub fn new(persistence: Arc<SimplePersistence>) -> Self {
        Self { persistence }
    }

    pub async fn store_deliveries(
        &self,
        period_start: DateTime<Utc>,
        period_end: DateTime<Utc>,
        deliveries: &[MinerDelivery],
    ) -> Result<()> {
        if deliveries.is_empty() {
            return Ok(());
        }

        let period_start_ts = period_start.timestamp();
        let period_end_ts = period_end.timestamp();
        let received_at = Utc::now().timestamp();

        let mut tx = self.persistence.pool().begin().await?;

        for delivery in deliveries {
            sqlx::query(
                r#"
                INSERT INTO miner_delivery_cache (
                    miner_hotkey,
                    miner_uid,
                    gpu_category,
                    period_start,
                    period_end,
                    total_hours,
                    revenue_usd,
                    received_at,
                    node_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(miner_hotkey, node_id, gpu_category, period_start, period_end)
                DO UPDATE SET
                    miner_uid = excluded.miner_uid,
                    gpu_category = excluded.gpu_category,
                    total_hours = excluded.total_hours,
                    revenue_usd = excluded.revenue_usd,
                    received_at = excluded.received_at,
                    node_id = excluded.node_id
                "#,
            )
            .bind(&delivery.miner_hotkey)
            .bind(delivery.miner_uid as i64)
            .bind(&delivery.gpu_category)
            .bind(period_start_ts)
            .bind(period_end_ts)
            .bind(delivery.total_hours)
            .bind(delivery.revenue_usd)
            .bind(received_at)
            .bind(&delivery.node_id)
            .execute(&mut *tx)
            .await?;
        }

        tx.commit().await?;
        Ok(())
    }

    pub async fn get_deliveries(
        &self,
        since: DateTime<Utc>,
        until: DateTime<Utc>,
        miner_hotkeys: Option<Vec<String>>,
    ) -> Result<Vec<MinerDelivery>> {
        let since_ts = since.timestamp();
        let until_ts = until.timestamp();

        let mut qb = QueryBuilder::<Sqlite>::new(
            r#"
            SELECT
                miner_hotkey,
                miner_uid,
                total_hours,
                revenue_usd,
                gpu_category,
                node_id
            FROM miner_delivery_cache
            WHERE period_end >=
            "#,
        );
        qb.push_bind(since_ts);
        qb.push(" AND period_start <= ");
        qb.push_bind(until_ts);

        if let Some(hotkeys) = miner_hotkeys {
            if !hotkeys.is_empty() {
                qb.push(" AND miner_hotkey IN (");
                let mut separated = qb.separated(", ");
                for hotkey in hotkeys {
                    separated.push_bind(hotkey);
                }
                qb.push(")");
            }
        }

        let rows = qb.build().fetch_all(self.persistence.pool()).await?;

        Ok(rows
            .into_iter()
            .map(|row| MinerDelivery {
                miner_hotkey: row.get("miner_hotkey"),
                miner_uid: row.get::<i64, _>("miner_uid") as u32,
                total_hours: row.get("total_hours"),
                revenue_usd: row.get("revenue_usd"),
                gpu_category: row.get("gpu_category"),
                node_id: row.get("node_id"),
            })
            .collect())
    }

    pub async fn get_deliveries_for_window(
        &self,
        period_start: DateTime<Utc>,
        period_end: DateTime<Utc>,
        miner_hotkeys: Option<Vec<String>>,
    ) -> Result<Vec<MinerDelivery>> {
        let period_start_ts = period_start.timestamp();
        let period_end_ts = period_end.timestamp();

        let mut qb = QueryBuilder::<Sqlite>::new(
            r#"
            SELECT
                miner_hotkey,
                miner_uid,
                total_hours,
                revenue_usd,
                gpu_category,
                node_id
            FROM miner_delivery_cache
            WHERE period_start =
            "#,
        );
        qb.push_bind(period_start_ts);
        qb.push(" AND period_end = ");
        qb.push_bind(period_end_ts);

        if let Some(hotkeys) = miner_hotkeys {
            if !hotkeys.is_empty() {
                qb.push(" AND miner_hotkey IN (");
                let mut separated = qb.separated(", ");
                for hotkey in hotkeys {
                    separated.push_bind(hotkey);
                }
                qb.push(")");
            }
        }

        let rows = qb.build().fetch_all(self.persistence.pool()).await?;

        Ok(rows
            .into_iter()
            .map(|row| MinerDelivery {
                miner_hotkey: row.get("miner_hotkey"),
                miner_uid: row.get::<i64, _>("miner_uid") as u32,
                total_hours: row.get("total_hours"),
                revenue_usd: row.get("revenue_usd"),
                gpu_category: row.get("gpu_category"),
                node_id: row.get("node_id"),
            })
            .collect())
    }
}
