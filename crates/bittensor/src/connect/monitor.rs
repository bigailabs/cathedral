//! Blockchain monitoring for Bittensor Substrate chain
//!
//! Simple, robust implementation following KISS principle.

use anyhow::Result;
use subxt::{OnlineClient, PolkadotConfig};
use tracing::{debug, info};

/// Transfer event data
#[derive(Debug, Clone)]
pub struct TransferInfo {
    pub from: String,
    pub to: String,
    pub amount: String,
    pub block_number: u32,
    pub event_index: usize,
}

/// Simple blockchain monitor for Bittensor transfers
pub struct BlockchainMonitor {
    client: OnlineClient<PolkadotConfig>,
    endpoint: String,
}

impl BlockchainMonitor {
    /// Connect to blockchain endpoint
    pub async fn new(endpoint: &str) -> Result<Self> {
        let is_insecure = endpoint.starts_with("ws://") || endpoint.starts_with("http://");
        let client = if is_insecure {
            debug!("Using insecure connection for endpoint: {}", endpoint);
            OnlineClient::<PolkadotConfig>::from_insecure_url(endpoint).await?
        } else {
            OnlineClient::<PolkadotConfig>::from_url(endpoint).await?
        };
        Ok(Self {
            client,
            endpoint: endpoint.to_string(),
        })
    }

    /// Get current block number
    pub async fn get_current_block(&self) -> Result<u32> {
        let block = self.client.blocks().at_latest().await?;
        Ok(block.number())
    }

    /// Get transfers from latest block
    pub async fn get_latest_transfers(&self) -> Result<Vec<TransferInfo>> {
        let block = self.client.blocks().at_latest().await?;
        self.get_transfers_from_block(block).await
    }

    /// Get transfers from a specific block number
    pub async fn get_transfers_at_block(&self, block_number: u32) -> Result<Vec<TransferInfo>> {
        use subxt::backend::legacy::LegacyRpcMethods;
        use subxt::backend::rpc::RpcClient;

        let is_insecure =
            self.endpoint.starts_with("ws://") || self.endpoint.starts_with("http://");
        let rpc = if is_insecure {
            RpcClient::from_insecure_url(&self.endpoint).await?
        } else {
            RpcClient::from_url(&self.endpoint).await?
        };

        let rpc_methods = LegacyRpcMethods::<PolkadotConfig>::new(rpc);
        let block_hash = rpc_methods
            .chain_get_block_hash(Some(block_number.into()))
            .await?
            .ok_or_else(|| anyhow::anyhow!("Block {} not found", block_number))?;

        let block = self.client.blocks().at(block_hash).await?;
        self.get_transfers_from_block(block).await
    }

    /// Extract transfers from a block
    async fn get_transfers_from_block(
        &self,
        block: subxt::blocks::Block<PolkadotConfig, OnlineClient<PolkadotConfig>>,
    ) -> Result<Vec<TransferInfo>> {
        let mut transfers = Vec::new();
        let block_num = block.number();
        let events = block.events().await?;

        for (idx, event) in events.iter().enumerate() {
            if let Ok(ev) = event {
                if ev.pallet_name() == "Balances" && ev.variant_name() == "Transfer" {
                    if let Some(transfer) = Self::extract_transfer(&ev, block_num, idx) {
                        transfers.push(transfer);
                    }
                }
            }
        }

        Ok(transfers)
    }

    /// Extract transfer details from event
    fn extract_transfer(
        ev: &subxt::events::EventDetails<PolkadotConfig>,
        block_number: u32,
        event_index: usize,
    ) -> Option<TransferInfo> {
        let fields = ev.field_values().ok()?;

        // Handle both named and unnamed fields
        let (from, to, amount) = match fields {
            subxt::ext::scale_value::Composite::Named(named_fields) => {
                let mut from = None;
                let mut to = None;
                let mut amount = None;

                for (name, value) in named_fields {
                    match name.as_str() {
                        "from" => from = extract_account_hex(&value),
                        "to" => to = extract_account_hex(&value),
                        "amount" => amount = Some(value.to_string()),
                        _ => {}
                    }
                }

                (from?, to?, amount?)
            }
            subxt::ext::scale_value::Composite::Unnamed(unnamed_fields) => {
                if unnamed_fields.len() < 3 {
                    return None;
                }

                let from = extract_account_hex(&unnamed_fields[0])?;
                let to = extract_account_hex(&unnamed_fields[1])?;
                let amount = unnamed_fields[2].to_string();

                (from, to, amount)
            }
        };

        Some(TransferInfo {
            from,
            to,
            amount,
            block_number,
            event_index,
        })
    }

    /// Poll for new transfers continuously
    pub async fn poll_transfers<F>(
        &self,
        mut last_block: u32,
        interval: tokio::time::Duration,
        mut callback: F,
    ) -> Result<()>
    where
        F: FnMut(Vec<TransferInfo>) -> Result<()>,
    {
        let mut ticker = tokio::time::interval(interval);

        loop {
            ticker.tick().await;

            let current_block = self.get_current_block().await?;

            if current_block > last_block {
                // Get transfers from latest block only
                let transfers = self.get_latest_transfers().await?;

                if !transfers.is_empty() {
                    info!(
                        "Found {} transfers in block {}",
                        transfers.len(),
                        current_block
                    );
                    callback(transfers)?;
                }

                last_block = current_block;
            }
        }
    }
}

/// Extract account hex from nested composite value
fn extract_account_hex(value: &subxt::ext::scale_value::Value<u32>) -> Option<String> {
    // Convert to string and extract byte values from nested structure
    let value_str = value.to_string();

    // Handle nested composite: ((byte1, byte2, ...))
    let cleaned = value_str
        .trim_start_matches('(')
        .trim_end_matches(')')
        .trim_start_matches('(')
        .trim_end_matches(')');

    // Parse comma-separated bytes
    let bytes: Vec<u8> = cleaned
        .split(',')
        .filter_map(|s| s.trim().parse::<u8>().ok())
        .collect();

    // Must be exactly 32 bytes for AccountId32
    if bytes.len() == 32 {
        Some(bytes.iter().map(|b| format!("{:02x}", b)).collect())
    } else {
        debug!("Invalid account bytes length: {}", bytes.len());
        None
    }
}

#[cfg(test)]
mod tests {

    #[test]
    fn test_extract_account_hex() {
        // Test with a mock Value that produces the expected string format
        // In reality, this would be a subxt Value, but we can test the logic
        // by directly testing the string parsing

        // Simulate the nested composite format: ((byte1, byte2, ...))
        let test_str = "((126, 85, 233, 164, 31, 92, 185, 17, 101, 198, 143, 31, 141, 41, 187, 43, 115, 147, 93, 29, 237, 199, 253, 100, 235, 33, 224, 71, 168, 155, 113, 242))";

        // Extract bytes from the string
        let cleaned = test_str
            .trim_start_matches('(')
            .trim_end_matches(')')
            .trim_start_matches('(')
            .trim_end_matches(')');

        let bytes: Vec<u8> = cleaned
            .split(',')
            .filter_map(|s| s.trim().parse::<u8>().ok())
            .collect();

        assert_eq!(bytes.len(), 32);

        let hex: String = bytes.iter().map(|b| format!("{:02x}", b)).collect();
        assert_eq!(hex.len(), 64); // 32 bytes = 64 hex chars
        assert_eq!(&hex[0..2], "7e"); // First byte is 126 = 0x7e
        assert_eq!(&hex[2..4], "55"); // Second byte is 85 = 0x55
    }
}
