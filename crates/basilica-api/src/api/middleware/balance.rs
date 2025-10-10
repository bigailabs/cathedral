use crate::{api::middleware::AuthContext, error::ApiError, server::AppState};
use axum::{
    body::Body,
    extract::State,
    http::Request,
    middleware::Next,
    response::{IntoResponse, Response},
};
use rust_decimal::Decimal;
use std::str::FromStr;

const MIN_BALANCE_USD: f64 = 10.0;

pub async fn balance_validation_middleware(
    State(state): State<AppState>,
    req: Request<Body>,
    next: Next,
) -> Result<Response, Response> {
    let auth_context = req
        .extensions()
        .get::<AuthContext>()
        .ok_or_else(|| {
            ApiError::Internal {
                message: "Auth context not found in request extensions".to_string(),
            }
            .into_response()
        })?
        .clone();

    if let Some(billing_client) = &state.billing_client {
        match uuid::Uuid::from_str(&auth_context.user_id) {
            Ok(user_uuid) => match billing_client.get_balance(user_uuid).await {
                Ok(balance_response) => {
                    match Decimal::from_str(&balance_response.available_balance) {
                        Ok(available_balance) => {
                            let min_balance =
                                Decimal::from_f64_retain(MIN_BALANCE_USD).unwrap_or(Decimal::ZERO);

                            if available_balance < min_balance {
                                if state.config.billing.enforce_balance_checks {
                                    tracing::warn!(
                                        "ENFORCED: Blocking rental for user {} with insufficient balance: {} < {}",
                                        auth_context.user_id,
                                        available_balance,
                                        min_balance
                                    );
                                    return Err(ApiError::InsufficientBalance {
                                        message: "Your account balance is below the minimum required to create rentals".to_string(),
                                        current_balance: balance_response.available_balance.clone(),
                                        required: MIN_BALANCE_USD.to_string(),
                                    }
                                    .into_response());
                                } else {
                                    tracing::warn!(
                                        "SHADOW MODE: User {} has insufficient balance ({} < {}) but rental is allowed to proceed. Enforcement disabled.",
                                        auth_context.user_id,
                                        available_balance,
                                        min_balance
                                    );
                                }
                            } else {
                                tracing::debug!(
                                    "User {} has sufficient balance: {} >= {}",
                                    auth_context.user_id,
                                    available_balance,
                                    min_balance
                                );
                            }
                        }
                        Err(e) => {
                            tracing::warn!(
                            "Failed to parse balance as Decimal: {}. Allowing request to proceed.",
                            e
                        );
                        }
                    }
                }
                Err(e) => {
                    tracing::warn!(
                        "Balance check failed for user {}: {}. Allowing request to proceed (graceful degradation).",
                        auth_context.user_id,
                        e
                    );
                }
            },
            Err(e) => {
                tracing::warn!(
                    "Invalid user ID format for {}: {}. Skipping balance validation.",
                    auth_context.user_id,
                    e
                );
            }
        }
    } else {
        tracing::debug!(
            "Billing client not available. Skipping balance validation for user {}.",
            auth_context.user_id
        );
    }

    Ok(next.run(req).await)
}
