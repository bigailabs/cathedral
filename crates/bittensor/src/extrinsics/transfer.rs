//! # Transfer Extrinsics
//!
//! Extrinsics for TAO transfers on the Bittensor network:
//! - `transfer`: Transfer TAO to another account
//! - `transfer_keep_alive`: Transfer TAO while keeping the sender account alive
//! - `transfer_all`: Transfer all TAO to another account

use crate::api::api;
use crate::error::BittensorError;
use crate::extrinsics::ExtrinsicResponse;
use crate::types::Balance;
use crate::AccountId;
use std::str::FromStr;
use subxt::OnlineClient;
use subxt::PolkadotConfig;

/// Parameters for transfer operations
#[derive(Debug, Clone)]
pub struct TransferParams {
    /// Destination account (SS58 address)
    pub dest: String,
    /// Amount to transfer in RAO
    pub amount_rao: u64,
    /// Keep the sender account alive (minimum balance)
    pub keep_alive: bool,
}

impl TransferParams {
    /// Create new transfer params with amount in TAO
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::extrinsics::TransferParams;
    ///
    /// let params = TransferParams::new_tao(
    ///     "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
    ///     1.5
    /// );
    /// assert_eq!(params.amount_rao, 1_500_000_000);
    /// ```
    pub fn new_tao(dest: &str, amount_tao: f64) -> Self {
        Self {
            dest: dest.to_string(),
            amount_rao: (amount_tao * 1_000_000_000.0) as u64,
            keep_alive: true,
        }
    }

    /// Create new transfer params with amount in RAO
    pub fn new_rao(dest: &str, amount_rao: u64) -> Self {
        Self {
            dest: dest.to_string(),
            amount_rao,
            keep_alive: true,
        }
    }

    /// Set whether to keep the sender account alive
    pub fn keep_alive(mut self, keep_alive: bool) -> Self {
        self.keep_alive = keep_alive;
        self
    }
}

/// Transfer TAO to another account
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `signer` - The account signer (must have sufficient balance)
/// * `params` - Transfer parameters
///
/// # Returns
///
/// An `ExtrinsicResponse` with the transfer result
///
/// # Example
///
/// ```rust,no_run
/// use bittensor::extrinsics::{transfer, TransferParams};
///
/// # async fn example() -> Result<(), Box<dyn std::error::Error>> {
/// # let client: subxt::OnlineClient<subxt::PolkadotConfig> = todo!();
/// # let signer: bittensor::WalletSigner = todo!();
/// let params = TransferParams::new_tao(
///     "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
///     1.0
/// );
/// let result = transfer(&client, &signer, params).await?;
/// # Ok(())
/// # }
/// ```
pub async fn transfer<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    params: TransferParams,
) -> Result<ExtrinsicResponse<Balance>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    if params.keep_alive {
        transfer_keep_alive(client, signer, params).await
    } else {
        transfer_allow_death(client, signer, params).await
    }
}

/// Transfer TAO while keeping the sender account alive
///
/// This ensures the sender's account maintains the minimum existential deposit.
pub async fn transfer_keep_alive<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    params: TransferParams,
) -> Result<ExtrinsicResponse<Balance>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    let dest_account =
        AccountId::from_str(&params.dest).map_err(|_| BittensorError::InvalidHotkey {
            hotkey: params.dest.clone(),
        })?;

    let dest_multi: subxt::utils::MultiAddress<AccountId, ()> =
        subxt::utils::MultiAddress::Id(dest_account);

    let call = api::tx()
        .balances()
        .transfer_keep_alive(dest_multi, params.amount_rao);

    let tx_hash = client
        .tx()
        .sign_and_submit_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to submit transfer: {}", e),
        })?;

    Ok(ExtrinsicResponse::success()
        .with_message("Transfer completed successfully")
        .with_extrinsic_hash(&format!("{:?}", tx_hash))
        .with_data(Balance::from_rao(params.amount_rao)))
}

/// Transfer TAO allowing the sender account to be reaped
async fn transfer_allow_death<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    params: TransferParams,
) -> Result<ExtrinsicResponse<Balance>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    let dest_account =
        AccountId::from_str(&params.dest).map_err(|_| BittensorError::InvalidHotkey {
            hotkey: params.dest.clone(),
        })?;

    let dest_multi: subxt::utils::MultiAddress<AccountId, ()> =
        subxt::utils::MultiAddress::Id(dest_account);

    let call = api::tx()
        .balances()
        .transfer_allow_death(dest_multi, params.amount_rao);

    let tx_hash = client
        .tx()
        .sign_and_submit_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to submit transfer: {}", e),
        })?;

    Ok(ExtrinsicResponse::success()
        .with_message("Transfer completed successfully")
        .with_extrinsic_hash(&format!("{:?}", tx_hash))
        .with_data(Balance::from_rao(params.amount_rao)))
}

/// Transfer all TAO to another account
///
/// Transfers the entire balance, optionally keeping the account alive.
pub async fn transfer_all<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    dest: &str,
    keep_alive: bool,
) -> Result<ExtrinsicResponse<()>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    let dest_account = AccountId::from_str(dest).map_err(|_| BittensorError::InvalidHotkey {
        hotkey: dest.to_string(),
    })?;

    let dest_multi: subxt::utils::MultiAddress<AccountId, ()> =
        subxt::utils::MultiAddress::Id(dest_account);

    let call = api::tx().balances().transfer_all(dest_multi, keep_alive);

    let tx_hash = client
        .tx()
        .sign_and_submit_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to submit transfer_all: {}", e),
        })?;

    Ok(ExtrinsicResponse::success()
        .with_message("Transfer all completed")
        .with_extrinsic_hash(&format!("{:?}", tx_hash))
        .with_data(()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_transfer_params_tao() {
        let params =
            TransferParams::new_tao("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY", 1.5);
        assert_eq!(params.amount_rao, 1_500_000_000);
        assert!(params.keep_alive);
    }

    #[test]
    fn test_transfer_params_rao() {
        let params =
            TransferParams::new_rao("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY", 1000);
        assert_eq!(params.amount_rao, 1000);
    }

    #[test]
    fn test_transfer_params_builder() {
        let params =
            TransferParams::new_tao("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY", 1.0)
                .keep_alive(false);

        assert!(!params.keep_alive);
    }
}
