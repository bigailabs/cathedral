use crate::bdd::TestContext;
use basilica_protocol::billing::{ApplyCreditsRequest, GetBalanceRequest};
use uuid::Uuid;

#[tokio::test]
async fn test_apply_credits_increases_balance() {
    let mut context = TestContext::new().await;
    let user_id = "test_apply_credits_001";

    context.create_test_user(user_id, "100.0").await;

    let initial_balance = context.get_user_balance(user_id).await;
    assert_eq!(initial_balance, rust_decimal::Decimal::from(100));

    let transaction_id = Uuid::new_v4().to_string();
    let request = ApplyCreditsRequest {
        payment_method: String::new(),
        user_id: user_id.to_string(),
        amount: "50.0".to_string(),
        transaction_id: transaction_id.clone(),
        metadata: std::collections::HashMap::new(),
    };

    let response = context
        .client
        .apply_credits(request)
        .await
        .expect("Failed to apply credits")
        .into_inner();

    assert!(response.success, "Applying credits should succeed");
    assert_eq!(
        response.new_balance, "150",
        "New balance should be 100 + 50"
    );
    assert_eq!(
        response.credit_id, transaction_id,
        "Transaction ID should match"
    );
    assert!(response.applied_at.is_some(), "Should return timestamp");

    let final_balance = context.get_user_balance(user_id).await;
    assert_eq!(
        final_balance,
        rust_decimal::Decimal::from(150),
        "Database balance should be updated"
    );

    context.cleanup().await;
}

#[tokio::test]
async fn test_apply_negative_credits_reduces_balance() {
    let mut context = TestContext::new().await;
    let user_id = "test_negative_credits";

    context.create_test_user(user_id, "200.0").await;

    let request = ApplyCreditsRequest {
        payment_method: String::new(),
        user_id: user_id.to_string(),
        amount: "-30.0".to_string(),
        transaction_id: Uuid::new_v4().to_string(),
        metadata: std::collections::HashMap::new(),
    };

    let response = context
        .client
        .apply_credits(request)
        .await
        .expect("Failed to apply negative credits")
        .into_inner();

    assert!(response.success);
    assert_eq!(response.new_balance, "170", "Balance should be 200 - 30");

    let final_balance = context.get_user_balance(user_id).await;
    assert_eq!(final_balance, rust_decimal::Decimal::from(170));

    context.cleanup().await;
}

#[tokio::test]
async fn test_get_balance_returns_available_and_total() {
    let mut context = TestContext::new().await;
    let user_id = "test_get_balance";

    context.create_test_user(user_id, "500.0").await;

    let request = GetBalanceRequest {
        user_id: user_id.to_string(),
    };

    let response = context
        .client
        .get_balance(request)
        .await
        .expect("Failed to get balance")
        .into_inner();

    assert_eq!(
        response.available_balance, "500",
        "Available balance should match"
    );
    assert_eq!(
        response.total_balance, "500",
        "Total balance should equal available balance in pay-as-you-go model"
    );
    assert!(response.last_updated.is_some(), "Should return timestamp");

    context.cleanup().await;
}
