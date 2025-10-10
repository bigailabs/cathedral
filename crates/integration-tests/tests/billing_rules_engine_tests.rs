use async_trait::async_trait;
use basilica_billing::domain::packages::BillingPackage;
use basilica_billing::domain::rules_engine::{BillingRule, RulesEngine, RulesEvaluator};
use basilica_billing::domain::types::{
    CostBreakdown, CreditBalance, DiscountType, PackageId, UsageMetrics, UserId, UserMetadata,
    UserTier,
};
use basilica_billing::error::{BillingError, Result};
use basilica_billing::storage::{
    PackageRepository, PromoCode, PromoCodeRepository, RulesRepository, UserMetadataRepository,
};
use chrono::Utc;
use rust_decimal::Decimal;
use std::collections::HashMap;
use std::str::FromStr;
use std::sync::Arc;
use tokio::sync::Mutex;

struct MockPackageRepository {
    packages: Mutex<HashMap<PackageId, BillingPackage>>,
}

impl MockPackageRepository {
    fn with_package(package: BillingPackage) -> Self {
        let mut packages = HashMap::new();
        packages.insert(package.id.clone(), package);
        Self {
            packages: Mutex::new(packages),
        }
    }
}

#[async_trait]
impl PackageRepository for MockPackageRepository {
    async fn get_package(&self, package_id: &PackageId) -> Result<BillingPackage> {
        self.packages
            .lock()
            .await
            .get(package_id)
            .cloned()
            .ok_or_else(|| BillingError::PackageNotFound {
                id: package_id.to_string(),
            })
    }

    async fn list_packages(&self) -> Result<Vec<BillingPackage>> {
        Ok(self.packages.lock().await.values().cloned().collect())
    }

    async fn find_package_for_gpu_model(&self, _gpu_model: &str) -> Result<BillingPackage> {
        unimplemented!()
    }

    async fn is_package_compatible_with_gpu(
        &self,
        _package_id: &PackageId,
        _gpu_model: &str,
    ) -> Result<bool> {
        unimplemented!()
    }

    async fn create_package(&self, package: BillingPackage) -> Result<()> {
        self.packages
            .lock()
            .await
            .insert(package.id.clone(), package);
        Ok(())
    }

    async fn update_package(&self, package: BillingPackage) -> Result<()> {
        self.packages
            .lock()
            .await
            .insert(package.id.clone(), package);
        Ok(())
    }

    async fn delete_package(&self, package_id: &PackageId) -> Result<()> {
        self.packages.lock().await.remove(package_id);
        Ok(())
    }

    async fn activate_package(&self, _package_id: &PackageId) -> Result<()> {
        unimplemented!()
    }

    async fn deactivate_package(&self, _package_id: &PackageId) -> Result<()> {
        unimplemented!()
    }

    async fn evaluate_package_cost(
        &self,
        package_id: &PackageId,
        usage: &UsageMetrics,
    ) -> Result<CostBreakdown> {
        let package = self.get_package(package_id).await?;
        Ok(package.calculate_cost(usage))
    }
}

struct MockRulesRepository {
    rules: Mutex<Vec<BillingRule>>,
}

impl MockRulesRepository {
    fn new() -> Self {
        Self {
            rules: Mutex::new(Vec::new()),
        }
    }
}

#[async_trait]
impl RulesRepository for MockRulesRepository {
    async fn create_rule(&self, rule: &BillingRule) -> Result<()> {
        self.rules.lock().await.push(rule.clone());
        Ok(())
    }

    async fn list_rules(&self) -> Result<Vec<BillingRule>> {
        Ok(self.rules.lock().await.clone())
    }

    async fn list_active_rules(&self) -> Result<Vec<BillingRule>> {
        Ok(self
            .rules
            .lock()
            .await
            .iter()
            .filter(|r| r.active)
            .cloned()
            .collect())
    }

    async fn get_rule(&self, _id: &str) -> Result<Option<BillingRule>> {
        Ok(None)
    }

    async fn update_rule(&self, _rule: &BillingRule) -> Result<()> {
        unimplemented!()
    }

    async fn delete_rule(&self, _id: &str) -> Result<()> {
        unimplemented!()
    }
}

struct MockUserMetadataRepository {
    users: Mutex<HashMap<UserId, UserMetadata>>,
}

impl MockUserMetadataRepository {
    fn with_user(user_id: UserId, user_metadata: UserMetadata) -> Self {
        let mut users = HashMap::new();
        users.insert(user_id, user_metadata);
        Self {
            users: Mutex::new(users),
        }
    }
}

#[async_trait]
impl UserMetadataRepository for MockUserMetadataRepository {
    async fn get_user_metadata(&self, user_id: &UserId) -> Result<UserMetadata> {
        Ok(self
            .users
            .lock()
            .await
            .get(user_id)
            .cloned()
            .unwrap_or_else(|| UserMetadata {
                user_id: user_id.clone(),
                user_tier: UserTier::Standard,
                discount_percentage: None,
                promo_codes: vec![],
                tier_updated_at: Utc::now(),
                custom_attributes: HashMap::new(),
            }))
    }

    async fn update_user_tier(&self, user_id: &UserId, tier: UserTier) -> Result<()> {
        let mut users = self.users.lock().await;
        if let Some(user) = users.get_mut(user_id) {
            user.user_tier = tier;
        }
        Ok(())
    }

    async fn set_custom_discount(&self, user_id: &UserId, percentage: Decimal) -> Result<()> {
        let mut users = self.users.lock().await;
        if let Some(user) = users.get_mut(user_id) {
            user.discount_percentage = Some(percentage);
        }
        Ok(())
    }
}

struct MockPromoCodeRepository {
    codes: Mutex<HashMap<String, PromoCode>>,
    usage_calls: Mutex<Vec<String>>,
}

impl MockPromoCodeRepository {
    fn new() -> Self {
        Self {
            codes: Mutex::new(HashMap::new()),
            usage_calls: Mutex::new(Vec::new()),
        }
    }

    fn with_promo_code(code: PromoCode) -> Self {
        let mut codes = HashMap::new();
        codes.insert(code.code.clone(), code);
        Self {
            codes: Mutex::new(codes),
            usage_calls: Mutex::new(Vec::new()),
        }
    }
}

#[async_trait]
impl PromoCodeRepository for MockPromoCodeRepository {
    async fn get_promo_code(&self, code: &str) -> Result<Option<PromoCode>> {
        Ok(self.codes.lock().await.get(code).cloned())
    }

    async fn validate_and_get(&self, code: &str) -> Result<PromoCode> {
        let promo = self.codes.lock().await.get(code).cloned().ok_or_else(|| {
            BillingError::ValidationError {
                field: "promo_code".to_string(),
                message: "Promo code not found".to_string(),
            }
        })?;

        if !promo.is_valid() {
            return Err(BillingError::ValidationError {
                field: "promo_code".to_string(),
                message: "Promo code invalid".to_string(),
            });
        }

        Ok(promo)
    }

    async fn increment_usage(&self, code: &str) -> Result<()> {
        self.usage_calls.lock().await.push(code.to_string());
        Ok(())
    }
}

fn create_test_package() -> BillingPackage {
    BillingPackage::new(
        PackageId::h100(),
        "H100 GPU".to_string(),
        "NVIDIA H100 GPU instances".to_string(),
        CreditBalance::from_f64(3.5).unwrap(),
        "H100".to_string(),
    )
}

fn create_test_usage() -> UsageMetrics {
    UsageMetrics {
        gpu_hours: Decimal::from(10),
        cpu_hours: Decimal::ZERO,
        memory_gb_hours: Decimal::ZERO,
        storage_gb_hours: Decimal::ZERO,
        network_gb: Decimal::ZERO,
        disk_io_gb: Decimal::ZERO,
    }
}

#[tokio::test]
async fn test_discount_application_standard_tier() {
    let package = create_test_package();
    let user_id = UserId::new("user1".to_string());
    let user_metadata = UserMetadata {
        user_id: user_id.clone(),
        user_tier: UserTier::Standard,
        discount_percentage: None,
        promo_codes: vec![],
        tier_updated_at: Utc::now(),
        custom_attributes: HashMap::new(),
    };

    let engine = RulesEngine::new(
        Arc::new(MockPackageRepository::with_package(package.clone())),
        Arc::new(MockRulesRepository::new()),
        Arc::new(MockUserMetadataRepository::with_user(
            user_id.clone(),
            user_metadata,
        )),
        Arc::new(MockPromoCodeRepository::new()),
    );

    let usage = create_test_usage();
    let cost = engine
        .evaluate_package(&user_id, &PackageId::h100(), &usage, None, &HashMap::new())
        .await
        .unwrap();

    let expected_total = CreditBalance::from_f64(35.0).unwrap();
    assert_eq!(cost.total_cost, expected_total);
    assert_eq!(cost.discounts, CreditBalance::zero());
}

#[tokio::test]
async fn test_discount_application_student_tier() {
    let package = create_test_package();
    let user_id = UserId::new("student1".to_string());
    let user_metadata = UserMetadata {
        user_id: user_id.clone(),
        user_tier: UserTier::Student,
        discount_percentage: None,
        promo_codes: vec![],
        tier_updated_at: Utc::now(),
        custom_attributes: HashMap::new(),
    };

    let engine = RulesEngine::new(
        Arc::new(MockPackageRepository::with_package(package.clone())),
        Arc::new(MockRulesRepository::new()),
        Arc::new(MockUserMetadataRepository::with_user(
            user_id.clone(),
            user_metadata,
        )),
        Arc::new(MockPromoCodeRepository::new()),
    );

    let usage = create_test_usage();
    let cost = engine
        .evaluate_package(&user_id, &PackageId::h100(), &usage, None, &HashMap::new())
        .await
        .unwrap();

    let expected_discount = CreditBalance::from_f64(7.0).unwrap();
    let expected_total = CreditBalance::from_f64(28.0).unwrap();
    assert_eq!(cost.discounts, expected_discount);
    assert_eq!(cost.total_cost, expected_total);
}

#[tokio::test]
async fn test_discount_application_enterprise_tier() {
    let package = create_test_package();
    let user_id = UserId::new("enterprise1".to_string());
    let user_metadata = UserMetadata {
        user_id: user_id.clone(),
        user_tier: UserTier::Enterprise,
        discount_percentage: None,
        promo_codes: vec![],
        tier_updated_at: Utc::now(),
        custom_attributes: HashMap::new(),
    };

    let engine = RulesEngine::new(
        Arc::new(MockPackageRepository::with_package(package.clone())),
        Arc::new(MockRulesRepository::new()),
        Arc::new(MockUserMetadataRepository::with_user(
            user_id.clone(),
            user_metadata,
        )),
        Arc::new(MockPromoCodeRepository::new()),
    );

    let usage = create_test_usage();
    let cost = engine
        .evaluate_package(&user_id, &PackageId::h100(), &usage, None, &HashMap::new())
        .await
        .unwrap();

    let expected_discount = CreditBalance::from_f64(5.25).unwrap();
    let expected_total = CreditBalance::from_f64(29.75).unwrap();
    assert_eq!(cost.discounts, expected_discount);
    assert_eq!(cost.total_cost, expected_total);
}

#[tokio::test]
async fn test_discount_application_percentage_promo_code() {
    let package = create_test_package();
    let user_id = UserId::new("user1".to_string());
    let user_metadata = UserMetadata {
        user_id: user_id.clone(),
        user_tier: UserTier::Standard,
        discount_percentage: None,
        promo_codes: vec![],
        tier_updated_at: Utc::now(),
        custom_attributes: HashMap::new(),
    };

    let promo_code = PromoCode {
        code: "SAVE25".to_string(),
        discount_type: DiscountType::Percentage,
        discount_value: Decimal::from_str("0.25").unwrap(),
        max_uses: None,
        current_uses: 0,
        valid_from: Utc::now() - chrono::Duration::days(1),
        valid_until: Some(Utc::now() + chrono::Duration::days(30)),
        active: true,
        applicable_packages: vec![],
        description: "25% off promo".to_string(),
    };

    let engine = RulesEngine::new(
        Arc::new(MockPackageRepository::with_package(package.clone())),
        Arc::new(MockRulesRepository::new()),
        Arc::new(MockUserMetadataRepository::with_user(
            user_id.clone(),
            user_metadata,
        )),
        Arc::new(MockPromoCodeRepository::with_promo_code(promo_code)),
    );

    let usage = create_test_usage();
    let cost = engine
        .evaluate_package(
            &user_id,
            &PackageId::h100(),
            &usage,
            Some("SAVE25"),
            &HashMap::new(),
        )
        .await
        .unwrap();

    let expected_discount = CreditBalance::from_f64(8.75).unwrap();
    let expected_total = CreditBalance::from_f64(26.25).unwrap();
    assert_eq!(cost.discounts, expected_discount);
    assert_eq!(cost.total_cost, expected_total);
}

#[tokio::test]
async fn test_discount_application_fixed_amount_promo_code() {
    let package = create_test_package();
    let user_id = UserId::new("user1".to_string());
    let user_metadata = UserMetadata {
        user_id: user_id.clone(),
        user_tier: UserTier::Standard,
        discount_percentage: None,
        promo_codes: vec![],
        tier_updated_at: Utc::now(),
        custom_attributes: HashMap::new(),
    };

    let promo_code = PromoCode {
        code: "FIXED10".to_string(),
        discount_type: DiscountType::FixedAmount,
        discount_value: Decimal::from_str("10.0").unwrap(),
        max_uses: None,
        current_uses: 0,
        valid_from: Utc::now() - chrono::Duration::days(1),
        valid_until: Some(Utc::now() + chrono::Duration::days(30)),
        active: true,
        applicable_packages: vec![],
        description: "$10 off promo".to_string(),
    };

    let engine = RulesEngine::new(
        Arc::new(MockPackageRepository::with_package(package.clone())),
        Arc::new(MockRulesRepository::new()),
        Arc::new(MockUserMetadataRepository::with_user(
            user_id.clone(),
            user_metadata,
        )),
        Arc::new(MockPromoCodeRepository::with_promo_code(promo_code)),
    );

    let usage = create_test_usage();
    let cost = engine
        .evaluate_package(
            &user_id,
            &PackageId::h100(),
            &usage,
            Some("FIXED10"),
            &HashMap::new(),
        )
        .await
        .unwrap();

    let expected_discount = CreditBalance::from_f64(10.0).unwrap();
    let expected_total = CreditBalance::from_f64(25.0).unwrap();
    assert_eq!(cost.discounts, expected_discount);
    assert_eq!(cost.total_cost, expected_total);
}

#[tokio::test]
async fn test_discount_application_best_discount_only() {
    let package = create_test_package();
    let user_id = UserId::new("student1".to_string());
    let user_metadata = UserMetadata {
        user_id: user_id.clone(),
        user_tier: UserTier::Student,
        discount_percentage: None,
        promo_codes: vec![],
        tier_updated_at: Utc::now(),
        custom_attributes: HashMap::new(),
    };

    let promo_code = PromoCode {
        code: "SAVE15".to_string(),
        discount_type: DiscountType::Percentage,
        discount_value: Decimal::from_str("0.15").unwrap(),
        max_uses: None,
        current_uses: 0,
        valid_from: Utc::now() - chrono::Duration::days(1),
        valid_until: Some(Utc::now() + chrono::Duration::days(30)),
        active: true,
        applicable_packages: vec![],
        description: "15% off promo".to_string(),
    };

    let engine = RulesEngine::new(
        Arc::new(MockPackageRepository::with_package(package.clone())),
        Arc::new(MockRulesRepository::new()),
        Arc::new(MockUserMetadataRepository::with_user(
            user_id.clone(),
            user_metadata,
        )),
        Arc::new(MockPromoCodeRepository::with_promo_code(promo_code)),
    );

    let usage = create_test_usage();
    let cost = engine
        .evaluate_package(
            &user_id,
            &PackageId::h100(),
            &usage,
            Some("SAVE15"),
            &HashMap::new(),
        )
        .await
        .unwrap();

    let expected_discount = CreditBalance::from_f64(7.0).unwrap();
    let expected_total = CreditBalance::from_f64(28.0).unwrap();
    assert_eq!(cost.discounts, expected_discount);
    assert_eq!(cost.total_cost, expected_total);
}

#[tokio::test]
async fn test_discount_application_promo_better_than_tier() {
    let package = create_test_package();
    let user_id = UserId::new("student1".to_string());
    let user_metadata = UserMetadata {
        user_id: user_id.clone(),
        user_tier: UserTier::Student,
        discount_percentage: None,
        promo_codes: vec![],
        tier_updated_at: Utc::now(),
        custom_attributes: HashMap::new(),
    };

    let promo_code = PromoCode {
        code: "SAVE30".to_string(),
        discount_type: DiscountType::Percentage,
        discount_value: Decimal::from_str("0.30").unwrap(),
        max_uses: None,
        current_uses: 0,
        valid_from: Utc::now() - chrono::Duration::days(1),
        valid_until: Some(Utc::now() + chrono::Duration::days(30)),
        active: true,
        applicable_packages: vec![],
        description: "30% off promo".to_string(),
    };

    let engine = RulesEngine::new(
        Arc::new(MockPackageRepository::with_package(package.clone())),
        Arc::new(MockRulesRepository::new()),
        Arc::new(MockUserMetadataRepository::with_user(
            user_id.clone(),
            user_metadata,
        )),
        Arc::new(MockPromoCodeRepository::with_promo_code(promo_code)),
    );

    let usage = create_test_usage();
    let cost = engine
        .evaluate_package(
            &user_id,
            &PackageId::h100(),
            &usage,
            Some("SAVE30"),
            &HashMap::new(),
        )
        .await
        .unwrap();

    let expected_discount = CreditBalance::from_f64(10.5).unwrap();
    let expected_total = CreditBalance::from_f64(24.5).unwrap();
    assert_eq!(cost.discounts, expected_discount);
    assert_eq!(cost.total_cost, expected_total);
}

#[tokio::test]
async fn test_discount_application_custom_tier() {
    let package = create_test_package();
    let user_id = UserId::new("custom1".to_string());
    let user_metadata = UserMetadata {
        user_id: user_id.clone(),
        user_tier: UserTier::Custom,
        discount_percentage: Some(Decimal::from_str("0.10").unwrap()),
        promo_codes: vec![],
        tier_updated_at: Utc::now(),
        custom_attributes: HashMap::new(),
    };

    let engine = RulesEngine::new(
        Arc::new(MockPackageRepository::with_package(package.clone())),
        Arc::new(MockRulesRepository::new()),
        Arc::new(MockUserMetadataRepository::with_user(
            user_id.clone(),
            user_metadata,
        )),
        Arc::new(MockPromoCodeRepository::new()),
    );

    let usage = create_test_usage();
    let cost = engine
        .evaluate_package(&user_id, &PackageId::h100(), &usage, None, &HashMap::new())
        .await
        .unwrap();

    let expected_discount = CreditBalance::from_f64(3.5).unwrap();
    let expected_total = CreditBalance::from_f64(31.5).unwrap();
    assert_eq!(cost.discounts, expected_discount);
    assert_eq!(cost.total_cost, expected_total);
}

#[tokio::test]
async fn test_discount_prevents_negative_cost() {
    let package = create_test_package();
    let user_id = UserId::new("user1".to_string());
    let user_metadata = UserMetadata {
        user_id: user_id.clone(),
        user_tier: UserTier::Standard,
        discount_percentage: None,
        promo_codes: vec![],
        tier_updated_at: Utc::now(),
        custom_attributes: HashMap::new(),
    };

    let promo_code = PromoCode {
        code: "HUGE_DISCOUNT".to_string(),
        discount_type: DiscountType::FixedAmount,
        discount_value: Decimal::from_str("100.0").unwrap(),
        max_uses: None,
        current_uses: 0,
        valid_from: Utc::now() - chrono::Duration::days(1),
        valid_until: Some(Utc::now() + chrono::Duration::days(30)),
        active: true,
        applicable_packages: vec![],
        description: "Huge discount".to_string(),
    };

    let engine = RulesEngine::new(
        Arc::new(MockPackageRepository::with_package(package.clone())),
        Arc::new(MockRulesRepository::new()),
        Arc::new(MockUserMetadataRepository::with_user(
            user_id.clone(),
            user_metadata,
        )),
        Arc::new(MockPromoCodeRepository::with_promo_code(promo_code)),
    );

    let usage = create_test_usage();
    let cost = engine
        .evaluate_package(
            &user_id,
            &PackageId::h100(),
            &usage,
            Some("HUGE_DISCOUNT"),
            &HashMap::new(),
        )
        .await
        .unwrap();

    assert_eq!(cost.total_cost, CreditBalance::zero());
}
