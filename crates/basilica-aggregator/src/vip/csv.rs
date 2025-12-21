use crate::vip::types::VipCsvRow;
use async_trait::async_trait;
use aws_sdk_s3::Client as S3Client;
use rust_decimal::Decimal;
use std::sync::Arc;
use thiserror::Error;
use tokio::sync::RwLock;

#[derive(Debug, Error)]
pub enum DataSourceError {
    #[error("Failed to read file: {0}")]
    FileRead(#[from] std::io::Error),
    #[error("Failed to parse CSV: {0}")]
    CsvParse(String),
    #[error("Failed to parse row {row}: {message}")]
    RowParse { row: usize, message: String },
    #[error("S3 error: {0}")]
    S3(String),
    #[error("UTF-8 encoding error: {0}")]
    Utf8(#[from] std::string::FromUtf8Error),
}

/// Trait for fetching VIP machine data (allows mocking for tests)
#[async_trait]
pub trait VipDataSource: Send + Sync {
    async fn fetch_vip_rows(&self) -> Result<Vec<VipCsvRow>, DataSourceError>;
}

/// Source type for CSV data
enum CsvSource {
    Local {
        file_path: String,
    },
    S3 {
        bucket: String,
        key: String,
        client: S3Client,
    },
}

/// CSV data source that can read from local files or S3
pub struct CsvDataSource {
    source: CsvSource,
}

impl CsvDataSource {
    /// Create a data source from a local CSV file path
    pub fn from_local(file_path: String) -> Self {
        Self {
            source: CsvSource::Local { file_path },
        }
    }

    /// Create a data source from S3 bucket/key
    /// Uses AWS SDK default credential chain for authentication
    /// If region is provided, it overrides the default region from the credential chain
    pub async fn from_s3(
        bucket: String,
        key: String,
        region: Option<String>,
    ) -> Result<Self, DataSourceError> {
        let mut config_loader = aws_config::defaults(aws_config::BehaviorVersion::latest());

        if let Some(region) = region {
            config_loader = config_loader.region(aws_sdk_s3::config::Region::new(region));
        }

        let config = config_loader.load().await;
        let client = S3Client::new(&config);
        Ok(Self {
            source: CsvSource::S3 {
                bucket,
                key,
                client,
            },
        })
    }

    /// Read the raw CSV content from the configured source
    async fn read_csv_content(&self) -> Result<String, DataSourceError> {
        match &self.source {
            CsvSource::Local { file_path } => tokio::fs::read_to_string(file_path)
                .await
                .map_err(DataSourceError::FileRead),
            CsvSource::S3 {
                bucket,
                key,
                client,
            } => {
                let resp = client
                    .get_object()
                    .bucket(bucket)
                    .key(key)
                    .send()
                    .await
                    .map_err(|e| DataSourceError::S3(e.to_string()))?;

                let bytes = resp
                    .body
                    .collect()
                    .await
                    .map_err(|e| DataSourceError::S3(e.to_string()))?
                    .into_bytes();

                String::from_utf8(bytes.to_vec()).map_err(DataSourceError::Utf8)
            }
        }
    }

    /// Parse CSV content into VipCsvRow records
    /// Expects header row with columns: vip_machine_id, assigned_user, active, ssh_host,
    /// ssh_port, ssh_user, gpu_type, gpu_count, region, hourly_rate, vcpu_count, system_memory_gb, notes
    fn parse_csv(&self, content: &str) -> Result<Vec<VipCsvRow>, DataSourceError> {
        let mut reader = csv::Reader::from_reader(content.as_bytes());
        let mut rows = Vec::new();

        for (idx, result) in reader.records().enumerate() {
            let record = result.map_err(|e| DataSourceError::CsvParse(e.to_string()))?;
            let row_num = idx + 2; // +2 because: +1 for 0-index, +1 for header row

            match Self::parse_record(row_num, &record) {
                Ok(csv_row) => rows.push(csv_row),
                Err(e) => {
                    tracing::warn!(row = row_num, error = %e, "Skipping invalid row");
                    // Continue processing other rows
                }
            }
        }

        Ok(rows)
    }

    /// Parse a single CSV record into a VipCsvRow
    /// Expected columns (0-indexed):
    /// 0: vip_machine_id, 1: assigned_user, 2: active, 3: ssh_host, 4: ssh_port,
    /// 5: ssh_user, 6: gpu_type, 7: gpu_count, 8: region, 9: hourly_rate,
    /// 10: vcpu_count, 11: system_memory_gb, 12: notes (optional)
    fn parse_record(
        row_num: usize,
        record: &csv::StringRecord,
    ) -> Result<VipCsvRow, DataSourceError> {
        let get_col = |idx: usize, name: &str| -> Result<String, DataSourceError> {
            record
                .get(idx)
                .filter(|s| !s.is_empty())
                .map(|s| s.to_string())
                .ok_or_else(|| DataSourceError::RowParse {
                    row: row_num,
                    message: format!("Missing required column {}", name),
                })
        };

        let vip_machine_id = get_col(0, "vip_machine_id")?;
        let assigned_user = get_col(1, "assigned_user")?;
        let active_str = get_col(2, "active")?;
        let active = match active_str.as_str() {
            "1" | "true" => true,
            "0" | "false" => false,
            other => {
                return Err(DataSourceError::RowParse {
                    row: row_num,
                    message: format!("Invalid 'active' value '{}', expected 0/1", other),
                })
            }
        };
        let ssh_host = get_col(3, "ssh_host")?;
        let ssh_port_str = get_col(4, "ssh_port")?;
        let ssh_user = get_col(5, "ssh_user")?;
        let gpu_type = get_col(6, "gpu_type")?;
        let gpu_count_str = get_col(7, "gpu_count")?;
        let region = get_col(8, "region")?;
        let hourly_rate_str = get_col(9, "hourly_rate")?;

        let vcpu_count_str = get_col(10, "vcpu_count")?;
        let system_memory_gb_str = get_col(11, "system_memory_gb")?;

        let notes = record
            .get(12)
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string());

        let ssh_port: u16 = ssh_port_str
            .parse()
            .map_err(|_| DataSourceError::RowParse {
                row: row_num,
                message: format!("Invalid ssh_port: {}", ssh_port_str),
            })?;

        let gpu_count: u32 = gpu_count_str
            .parse()
            .map_err(|_| DataSourceError::RowParse {
                row: row_num,
                message: format!("Invalid gpu_count: {}", gpu_count_str),
            })?;

        let hourly_rate: Decimal =
            hourly_rate_str
                .parse()
                .map_err(|_| DataSourceError::RowParse {
                    row: row_num,
                    message: format!("Invalid hourly_rate: {}", hourly_rate_str),
                })?;

        let vcpu_count: u32 = vcpu_count_str
            .parse()
            .map_err(|_| DataSourceError::RowParse {
                row: row_num,
                message: format!("Invalid vcpu_count: {}", vcpu_count_str),
            })?;

        let system_memory_gb: u32 =
            system_memory_gb_str
                .parse()
                .map_err(|_| DataSourceError::RowParse {
                    row: row_num,
                    message: format!("Invalid system_memory_gb: {}", system_memory_gb_str),
                })?;

        Ok(VipCsvRow {
            vip_machine_id,
            assigned_user,
            active,
            ssh_host,
            ssh_port,
            ssh_user,
            gpu_type,
            gpu_count,
            region,
            hourly_rate,
            vcpu_count,
            system_memory_gb,
            notes,
        })
    }
}

#[async_trait]
impl VipDataSource for CsvDataSource {
    async fn fetch_vip_rows(&self) -> Result<Vec<VipCsvRow>, DataSourceError> {
        let content = self.read_csv_content().await?;
        self.parse_csv(&content)
    }
}

/// Mock data source for testing - returns configurable rows
#[derive(Clone)]
pub struct MockVipDataSource {
    rows: Arc<RwLock<Vec<VipCsvRow>>>,
}

impl MockVipDataSource {
    /// Create a new mock with initial rows
    pub fn new(rows: Vec<VipCsvRow>) -> Self {
        Self {
            rows: Arc::new(RwLock::new(rows)),
        }
    }

    /// Replace all rows
    pub async fn set_rows(&self, rows: Vec<VipCsvRow>) {
        let mut guard = self.rows.write().await;
        *guard = rows;
    }

    /// Add a single row
    pub async fn add_row(&self, row: VipCsvRow) {
        let mut guard = self.rows.write().await;
        guard.push(row);
    }

    /// Remove a row by vip_machine_id
    pub async fn remove_row(&self, vip_machine_id: &str) {
        let mut guard = self.rows.write().await;
        guard.retain(|r| r.vip_machine_id != vip_machine_id);
    }

    /// Update a row by vip_machine_id
    pub async fn update_row<F>(&self, vip_machine_id: &str, f: F)
    where
        F: FnOnce(&mut VipCsvRow),
    {
        let mut guard = self.rows.write().await;
        if let Some(row) = guard
            .iter_mut()
            .find(|r| r.vip_machine_id == vip_machine_id)
        {
            f(row);
        }
    }
}

#[async_trait]
impl VipDataSource for MockVipDataSource {
    async fn fetch_vip_rows(&self) -> Result<Vec<VipCsvRow>, DataSourceError> {
        let guard = self.rows.read().await;
        Ok(guard.clone())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal::Decimal;

    #[test]
    fn test_parse_csv_valid() {
        let csv_content = r#"vip_machine_id,assigned_user,active,ssh_host,ssh_port,ssh_user,gpu_type,gpu_count,region,hourly_rate,vcpu_count,system_memory_gb,notes
machine-001,auth0|user123,1,10.0.0.1,22,ubuntu,H100,8,us-east-1,25.00,128,500,Production machine
machine-002,auth0|user456,1,10.0.0.2,22,ubuntu,A100,4,us-west-2,12.00,64,256,
"#;

        let source = CsvDataSource::from_local("/tmp/test.csv".to_string());
        let rows = source.parse_csv(csv_content).unwrap();

        assert_eq!(rows.len(), 2);

        assert_eq!(rows[0].vip_machine_id, "machine-001");
        assert_eq!(rows[0].assigned_user, "auth0|user123");
        assert!(rows[0].active);
        assert_eq!(rows[0].ssh_host, "10.0.0.1");
        assert_eq!(rows[0].ssh_port, 22);
        assert_eq!(rows[0].ssh_user, "ubuntu");
        assert_eq!(rows[0].gpu_type, "H100");
        assert_eq!(rows[0].gpu_count, 8);
        assert_eq!(rows[0].region, "us-east-1");
        assert_eq!(rows[0].hourly_rate, Decimal::new(2500, 2));
        assert_eq!(rows[0].vcpu_count, 128);
        assert_eq!(rows[0].system_memory_gb, 500);
        assert_eq!(rows[0].notes, Some("Production machine".to_string()));

        assert_eq!(rows[1].vip_machine_id, "machine-002");
        assert_eq!(rows[1].vcpu_count, 64);
        assert_eq!(rows[1].system_memory_gb, 256);
        assert_eq!(rows[1].notes, None);
    }

    #[test]
    fn test_parse_csv_skips_invalid_rows() {
        let csv_content = r#"vip_machine_id,assigned_user,active,ssh_host,ssh_port,ssh_user,gpu_type,gpu_count,region,hourly_rate,vcpu_count,system_memory_gb,notes
machine-001,auth0|user123,1,10.0.0.1,22,ubuntu,H100,8,us-east-1,25.00,128,500,
,auth0|user456,1,10.0.0.2,22,ubuntu,A100,4,us-west-2,12.00,64,256,
machine-003,auth0|user789,1,10.0.0.3,invalid_port,ubuntu,H100,4,us-east-1,15.00,64,256,
machine-004,auth0|user000,INVALID,10.0.0.4,22,ubuntu,H100,4,us-east-1,15.00,64,256,
"#;

        let source = CsvDataSource::from_local("/tmp/test.csv".to_string());
        let rows = source.parse_csv(csv_content).unwrap();

        // Only the first valid row should be parsed (others have missing id, invalid port, or invalid active)
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].vip_machine_id, "machine-001");
    }

    #[tokio::test]
    async fn test_mock_data_source() {
        let mock = MockVipDataSource::new(vec![
            VipCsvRow::test_machine("m1", "user1"),
            VipCsvRow::test_machine("m2", "user2"),
        ]);

        let rows = mock.fetch_vip_rows().await.unwrap();
        assert_eq!(rows.len(), 2);

        // Test add
        mock.add_row(VipCsvRow::test_machine("m3", "user3")).await;
        let rows = mock.fetch_vip_rows().await.unwrap();
        assert_eq!(rows.len(), 3);

        // Test remove
        mock.remove_row("m2").await;
        let rows = mock.fetch_vip_rows().await.unwrap();
        assert_eq!(rows.len(), 2);
        assert!(rows.iter().all(|r| r.vip_machine_id != "m2"));

        // Test update
        mock.update_row("m1", |r| r.active = false).await;
        let rows = mock.fetch_vip_rows().await.unwrap();
        let m1 = rows.iter().find(|r| r.vip_machine_id == "m1").unwrap();
        assert!(!m1.active);
    }
}
