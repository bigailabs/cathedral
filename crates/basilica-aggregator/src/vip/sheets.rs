use crate::vip::types::VipSheetRow;
use google_sheets4::{hyper, hyper_rustls, Sheets};
use rust_decimal::Decimal;
use serde_json::Value as JsonValue;
use std::path::Path;
use thiserror::Error;
use yup_oauth2;

#[derive(Debug, Error)]
pub enum SheetsError {
    #[error("Failed to read credentials file: {0}")]
    CredentialsRead(#[from] std::io::Error),
    #[error("Failed to parse credentials: {0}")]
    CredentialsParse(#[from] serde_json::Error),
    #[error("Authentication failed: {0}")]
    Auth(String),
    #[error("API error: {0}")]
    Api(String),
    #[error("Failed to parse row {row}: {message}")]
    RowParse { row: usize, message: String },
}

pub struct GoogleSheetsClient {
    hub: Sheets<hyper_rustls::HttpsConnector<hyper::client::HttpConnector>>,
    sheet_id: String,
}

impl GoogleSheetsClient {
    /// Create a new client with service account authentication
    pub async fn new(credentials_path: &Path, sheet_id: String) -> Result<Self, SheetsError> {
        let secret = yup_oauth2::read_service_account_key(credentials_path)
            .await
            .map_err(|e| SheetsError::Auth(e.to_string()))?;

        let auth = yup_oauth2::ServiceAccountAuthenticator::builder(secret)
            .build()
            .await
            .map_err(|e| SheetsError::Auth(e.to_string()))?;

        let https = hyper_rustls::HttpsConnectorBuilder::new()
            .with_native_roots()
            .map_err(|e| SheetsError::Auth(e.to_string()))?
            .https_or_http()
            .enable_http1()
            .enable_http2()
            .build();

        let client = hyper::Client::builder().build(https);

        let hub = Sheets::new(client, auth);

        Ok(Self { hub, sheet_id })
    }

    /// Fetch all VIP rows from the sheet (skips header row)
    pub async fn fetch_vip_rows(&self) -> Result<Vec<VipSheetRow>, SheetsError> {
        // Fetch columns A through K (all data columns)
        let range = "A:K";

        let result = self.hub
            .spreadsheets()
            .values_get(&self.sheet_id, range)
            .doit()
            .await
            .map_err(|e| SheetsError::Api(e.to_string()))?;

        let values = result.1.values.unwrap_or_default();

        // Skip header row (row 0), parse remaining rows
        let mut rows = Vec::new();
        for (idx, row) in values.iter().enumerate().skip(1) {
            match Self::parse_row(idx + 1, row) {
                Ok(sheet_row) => rows.push(sheet_row),
                Err(e) => {
                    tracing::warn!(row = idx + 1, error = %e, "Skipping invalid row");
                    // Continue processing other rows
                }
            }
        }

        Ok(rows)
    }

    /// Parse a single row from the sheet
    /// Column positions: A=0 vip_machine_id, B=1 assigned_user, C=2 ready,
    /// D=3 ssh_host, E=4 ssh_port, F=5 ssh_user, G=6 gpu_type, H=7 gpu_count,
    /// I=8 region, J=9 hourly_rate, K=10 notes (optional)
    fn parse_row(row_num: usize, row: &[JsonValue]) -> Result<VipSheetRow, SheetsError> {
        let get_col = |idx: usize, name: &str| -> Result<String, SheetsError> {
            row.get(idx)
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
                .map(|s| s.to_string())
                .ok_or_else(|| SheetsError::RowParse {
                    row: row_num,
                    message: format!("Missing required column {}", name),
                })
        };

        let vip_machine_id = get_col(0, "vip_machine_id")?;
        let assigned_user = get_col(1, "assigned_user")?;
        let ready = get_col(2, "ready")?;
        let ssh_host = get_col(3, "ssh_host")?;
        let ssh_port_str = get_col(4, "ssh_port")?;
        let ssh_user = get_col(5, "ssh_user")?;
        let gpu_type = get_col(6, "gpu_type")?;
        let gpu_count_str = get_col(7, "gpu_count")?;
        let region = get_col(8, "region")?;
        let hourly_rate_str = get_col(9, "hourly_rate")?;
        let notes = row.get(10).and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(|s| s.to_string());

        let ssh_port: u16 = ssh_port_str.parse().map_err(|_| SheetsError::RowParse {
            row: row_num,
            message: format!("Invalid ssh_port: {}", ssh_port_str),
        })?;

        let gpu_count: u32 = gpu_count_str.parse().map_err(|_| SheetsError::RowParse {
            row: row_num,
            message: format!("Invalid gpu_count: {}", gpu_count_str),
        })?;

        let hourly_rate: Decimal = hourly_rate_str.parse().map_err(|_| SheetsError::RowParse {
            row: row_num,
            message: format!("Invalid hourly_rate: {}", hourly_rate_str),
        })?;

        Ok(VipSheetRow {
            vip_machine_id,
            assigned_user,
            ready,
            ssh_host,
            ssh_port,
            ssh_user,
            gpu_type,
            gpu_count,
            region,
            hourly_rate,
            notes,
        })
    }
}
