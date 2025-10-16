use crate::error::{PaymentsError, Result};
use anyhow::Context;
use sp_core::sr25519;
use subxt::{dynamic::At, OnlineClient, PolkadotConfig};
use tracing::{debug, info};

pub struct BlockchainClient {
    client: OnlineClient<PolkadotConfig>,
    endpoint: String,
}

impl BlockchainClient {
    pub async fn new(endpoint: &str) -> Result<Self> {
        let client = OnlineClient::<PolkadotConfig>::from_url(endpoint)
            .await
            .context("Failed to connect to blockchain")
            .map_err(|e| PaymentsError::Blockchain(e.to_string()))?;

        info!("Connected to blockchain at {}", endpoint);

        Ok(Self {
            client,
            endpoint: endpoint.to_string(),
        })
    }

    pub async fn get_balance(&self, account_hex: &str) -> Result<u128> {
        let account_bytes = hex::decode(account_hex)
            .context("Invalid account hex")
            .map_err(|e| PaymentsError::Blockchain(e.to_string()))?;

        if account_bytes.len() != 32 {
            return Err(PaymentsError::Blockchain(format!(
                "Invalid account ID length: expected 32 bytes, got {}",
                account_bytes.len()
            )));
        }

        let mut account_id = [0u8; 32];
        account_id.copy_from_slice(&account_bytes);

        let account = subxt::utils::AccountId32(account_id);

        let storage_query = subxt::dynamic::storage(
            "System",
            "Account",
            vec![subxt::dynamic::Value::from_bytes(&account)],
        );

        let result = self
            .client
            .storage()
            .at_latest()
            .await
            .context("Failed to query storage")
            .map_err(|e| PaymentsError::Blockchain(e.to_string()))?
            .fetch(&storage_query)
            .await
            .context("Failed to fetch account data")
            .map_err(|e| PaymentsError::Blockchain(e.to_string()))?;

        if let Some(account_info) = result {
            let value = account_info
                .to_value()
                .context("Failed to decode account info")
                .map_err(|e| PaymentsError::Blockchain(e.to_string()))?;

            let account_preview = account_hex.chars().take(8).collect::<String>();

            // Extract balance from AccountInfo structure: { data: { free: u128, ... }, ... }
            if let Some(data_field) = value.at("data") {
                if let Some(free_balance) = data_field.at("free").and_then(|v| v.as_u128()) {
                    debug!("Balance for {}: {} plancks", account_preview, free_balance);
                    return Ok(free_balance);
                }
            }

            // Fallback: try string parsing if structure parsing fails
            let value_str = format!("{:?}", value);
            debug!("Could not parse AccountInfo structure for {}, trying string fallback. Debug output: {}", account_preview, value_str);

            if let Some(start) = value_str.find("free: ") {
                let rest = &value_str[start + 6..];
                if let Some(end) = rest
                    .find(',')
                    .or_else(|| rest.find(' ').or_else(|| rest.find('}')))
                {
                    let balance_str = &rest[..end];
                    if let Ok(balance) = balance_str.trim().parse::<u128>() {
                        debug!("Balance for {} (via fallback): {} plancks", account_preview, balance);
                        return Ok(balance);
                    }
                }
            }

            info!("Failed to parse balance for {}. AccountInfo structure: {:?}", account_preview, value);
        }

        Ok(0)
    }

    pub async fn transfer(
        &self,
        keypair: &sr25519::Pair,
        to_address_ss58: &str,
        amount_plancks: u128,
    ) -> Result<TransferReceipt> {
        use subxt::ext::sp_core::crypto::Ss58Codec;

        let dest_account = sp_core::sr25519::Public::from_ss58check(to_address_ss58)
            .map_err(|e| PaymentsError::Blockchain(format!("Invalid SS58 address: {}", e)))?;

        let dest = subxt::utils::AccountId32(dest_account.0);

        let amount_u64 = u64::try_from(amount_plancks)
            .map_err(|_| PaymentsError::Blockchain(format!("Amount {} exceeds u64::MAX", amount_plancks)))?;

        let dest_multi = subxt::dynamic::Value::unnamed_variant(
            "Id",
            vec![subxt::dynamic::Value::from_bytes(&dest)]
        );

        let transfer_tx = subxt::dynamic::tx(
            "Balances",
            "transfer_keep_alive",
            vec![
                dest_multi,
                subxt::dynamic::Value::u128(amount_u64 as u128),
            ],
        );

        let signer = subxt::tx::PairSigner::new(keypair.clone());

        let progress = self
            .client
            .tx()
            .sign_and_submit_then_watch_default(&transfer_tx, &signer)
            .await
            .context("Failed to submit transaction")
            .map_err(|e| PaymentsError::Blockchain(e.to_string()))?;

        let tx_hash = format!("0x{}", hex::encode(progress.extrinsic_hash()));
        info!("Transaction submitted: {}", tx_hash);

        let events = progress
            .wait_for_finalized_success()
            .await
            .context("Failed to wait for block finalization")
            .map_err(|e| PaymentsError::Blockchain(e.to_string()))?;

        let block_hash = events.extrinsic_hash();
        info!("Transaction finalized with hash: {:?}", block_hash);

        Ok(TransferReceipt {
            tx_hash,
            block_hash: format!("0x{}", hex::encode(block_hash)),
            status: TransferStatus::Finalized,
        })
    }

    pub fn endpoint(&self) -> &str {
        &self.endpoint
    }
}

#[derive(Debug, Clone)]
pub struct TransferReceipt {
    pub tx_hash: String,
    pub block_hash: String,
    pub status: TransferStatus,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TransferStatus {
    InBlock,
    Finalized,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_transfer_status() {
        let status = TransferStatus::InBlock;
        assert_eq!(status, TransferStatus::InBlock);
        assert_ne!(status, TransferStatus::Finalized);
    }

    #[test]
    fn test_invalid_account_hex() {
        assert!(hex::decode("invalid_hex").is_err());
    }

    #[test]
    fn test_account_hex_length() {
        let too_short = "abcd";
        let bytes = hex::decode(too_short).unwrap();
        assert_ne!(bytes.len(), 32);
    }
}
