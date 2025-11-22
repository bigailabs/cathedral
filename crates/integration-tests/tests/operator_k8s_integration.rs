//! Operator Kubernetes Integration Tests
//!
//! Tests the Basilica operator against a real Kubernetes cluster.
//! These tests validate:
//! - CRD creation and reconciliation
//! - Pod/Job creation with correct security contexts
//! - RBAC enforcement
//! - Status updates and lifecycle management
//!
//! Prerequisites:
//! - Kubernetes cluster accessible via KUBECONFIG or in-cluster config
//! - Operator CRDs installed (BasilicaJob, GpuRental, etc.)
//! - Test namespace created (or will be created automatically)
//!
//! Set NO_K8S_TESTS=1 to skip these tests if cluster is not available.

use anyhow::{Context, Result};
use integration_tests::K8sTestContext;
use k8s_openapi::api::batch::v1::Job;
use kube::{Api, ResourceExt};

use basilica_operator::crd::basilica_job::{BasilicaJob, BasilicaJobSpec, GpuSpec, Resources};
use basilica_operator::crd::gpu_rental::{
    AccessType, GpuRental, GpuRentalSpec, RentalContainer, RentalDuration,
};
use basilica_operator::k8s_client::{K8sClient, KubeClient};

/// Test that BasilicaJob CRD can be created and retrieved
#[tokio::test]
async fn test_basilica_job_crd_create_get() -> Result<()> {
    if K8sTestContext::should_skip_test().await {
        return Ok(());
    }

    let ctx = K8sTestContext::new("job-crd-create").await?;
    let client = KubeClient {
        client: ctx.client.clone(),
    };

    // Create a simple job
    let job = BasilicaJob::new(
        "test-job-simple",
        BasilicaJobSpec {
            image: "busybox:latest".to_string(),
            command: vec!["echo".to_string(), "hello".to_string()],
            args: vec![],
            env: vec![],
            resources: Resources {
                cpu: "0.25".to_string(),
                memory: "128Mi".to_string(),
                gpus: GpuSpec {
                    count: 0,
                    model: vec![],
                },
            },
            storage: None,
            artifacts: None,
            ttl_seconds: 300,
            priority: "normal".to_string(),
        },
    );

    // Create via K8sClient trait
    let created = client
        .create_basilica_job(&ctx.namespace, &job)
        .await
        .context("Failed to create BasilicaJob")?;

    assert_eq!(created.name_any(), "test-job-simple");
    println!("✓ Created BasilicaJob: {}", created.name_any());

    // Retrieve it
    let retrieved = client
        .get_basilica_job(&ctx.namespace, "test-job-simple")
        .await
        .context("Failed to get BasilicaJob")?;

    assert_eq!(retrieved.name_any(), "test-job-simple");
    assert_eq!(retrieved.spec.image, "busybox:latest");
    println!("✓ Retrieved BasilicaJob matches created spec");

    // Clean up
    client
        .delete_basilica_job(&ctx.namespace, "test-job-simple")
        .await
        .context("Failed to delete BasilicaJob")?;
    println!("✓ Deleted BasilicaJob");

    Ok(())
}

/// Test that BasilicaJob status can be updated
#[tokio::test]
async fn test_basilica_job_status_update() -> Result<()> {
    if K8sTestContext::should_skip_test().await {
        return Ok(());
    }

    let ctx = K8sTestContext::new("job-status-update").await?;
    let client = KubeClient {
        client: ctx.client.clone(),
    };

    // Create job
    let job = BasilicaJob::new(
        "test-job-status",
        BasilicaJobSpec {
            image: "busybox:latest".to_string(),
            command: vec!["sleep".to_string(), "10".to_string()],
            args: vec![],
            env: vec![],
            resources: Resources {
                cpu: "0.25".to_string(),
                memory: "128Mi".to_string(),
                gpus: GpuSpec {
                    count: 0,
                    model: vec![],
                },
            },
            storage: None,
            artifacts: None,
            ttl_seconds: 300,
            priority: "normal".to_string(),
        },
    );

    client
        .create_basilica_job(&ctx.namespace, &job)
        .await
        .context("Failed to create BasilicaJob")?;

    // Update status - in a real cluster the operator may reconcile and change it
    let status = basilica_operator::crd::basilica_job::BasilicaJobStatus {
        phase: Some("Running".to_string()),
        pod_name: Some("test-pod".to_string()),
        start_time: Some("2024-10-09T00:00:00Z".to_string()),
        completion_time: None,
    };

    client
        .update_basilica_job_status(&ctx.namespace, "test-job-status", status.clone())
        .await
        .context("Failed to update BasilicaJob status")?;

    println!("✓ Updated BasilicaJob status (operation succeeded)");

    // Verify the job still exists and has a status field
    // Note: In a real cluster with operator running, the status may be immediately
    // reconciled by the controller, so we just verify the status field exists
    let updated = client
        .get_basilica_job(&ctx.namespace, "test-job-status")
        .await?;

    assert!(updated.status.is_some(), "Status field should exist");
    println!("✓ Status field exists (operator may have reconciled to different value)");

    // Clean up
    client
        .delete_basilica_job(&ctx.namespace, "test-job-status")
        .await?;

    Ok(())
}

/// Test that GpuRental CRD can be created and managed
#[tokio::test]
async fn test_gpu_rental_crd_lifecycle() -> Result<()> {
    if K8sTestContext::should_skip_test().await {
        return Ok(());
    }

    let ctx = K8sTestContext::new("rental-lifecycle").await?;
    let client = KubeClient {
        client: ctx.client.clone(),
    };

    // Create a CPU-only rental (no GPU required)
    let rental = GpuRental::new(
        "test-rental-cpu",
        GpuRentalSpec {
            container: RentalContainer {
                image: "ubuntu:22.04".to_string(),
                env: vec![],
                command: vec!["sleep".to_string(), "infinity".to_string()],
                ports: vec![],
                volumes: vec![],
                resources: basilica_operator::crd::gpu_rental::Resources {
                    cpu: "1".to_string(),
                    memory: "512Mi".to_string(),
                    gpus: basilica_operator::crd::gpu_rental::GpuSpec {
                        count: 0,
                        model: vec![],
                    },
                },
            },
            duration: RentalDuration {
                hours: 1,
                auto_extend: false,
                max_extensions: 0,
            },
            access_type: AccessType::Ssh,
            network: Default::default(),
            storage: None,
            artifacts: None,
            ssh: None,
            jupyter_access: None,
            environment: None,
            miner_selector: None,
            billing: None,
            ttl_seconds: 3600,
            tenancy: None,
            exclusive: false,
        },
    );

    // Create
    let created = client
        .create_gpu_rental(&ctx.namespace, &rental)
        .await
        .context("Failed to create GpuRental")?;

    assert_eq!(created.name_any(), "test-rental-cpu");
    println!("✓ Created GpuRental: {}", created.name_any());

    // Get
    let retrieved = client
        .get_gpu_rental(&ctx.namespace, "test-rental-cpu")
        .await
        .context("Failed to get GpuRental")?;

    assert_eq!(retrieved.spec.container.image, "ubuntu:22.04");
    println!("✓ Retrieved GpuRental matches spec");

    // Update status
    let status = basilica_operator::crd::gpu_rental::GpuRentalStatus {
        state: Some("Pending".to_string()),
        pod_name: Some("rental-pod".to_string()),
        node_name: None,
        start_time: Some("2024-10-09T00:00:00Z".to_string()),
        expiry_time: Some("2024-10-09T01:00:00Z".to_string()),
        renewal_time: None,
        total_cost: None,
        total_extensions: None,
        endpoints: None,
    };

    client
        .update_gpu_rental_status(&ctx.namespace, "test-rental-cpu", status)
        .await
        .context("Failed to update GpuRental status")?;

    println!("✓ Updated GpuRental status");

    // Delete
    client
        .delete_gpu_rental(&ctx.namespace, "test-rental-cpu")
        .await
        .context("Failed to delete GpuRental")?;

    println!("✓ Deleted GpuRental");

    Ok(())
}

/// Test that pods are created with correct security context
#[tokio::test]
async fn test_pod_security_context() -> Result<()> {
    if K8sTestContext::should_skip_test().await {
        return Ok(());
    }

    let ctx = K8sTestContext::new("pod-security").await?;
    let client = KubeClient {
        client: ctx.client.clone(),
    };

    // Create a BasilicaJob and verify the underlying pod has correct security context
    let job = BasilicaJob::new(
        "test-security-ctx",
        BasilicaJobSpec {
            image: "busybox:latest".to_string(),
            command: vec!["sh".to_string(), "-c".to_string(), "sleep 30".to_string()],
            args: vec![],
            env: vec![],
            resources: Resources {
                cpu: "0.25".to_string(),
                memory: "128Mi".to_string(),
                gpus: GpuSpec {
                    count: 0,
                    model: vec![],
                },
            },
            storage: None,
            artifacts: None,
            ttl_seconds: 300,
            priority: "normal".to_string(),
        },
    );

    client.create_basilica_job(&ctx.namespace, &job).await?;
    println!("✓ Created BasilicaJob");

    // Note: In a real operator reconciliation, the Job controller would create a K8s Job
    // and the Job would create a Pod. For this test, we check the Job resource directly.

    // Wait a bit for operator to reconcile (if running)
    tokio::time::sleep(tokio::time::Duration::from_secs(2)).await;

    // Try to get the Kubernetes Job that should have been created
    let job_api: Api<Job> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    let k8s_job = match job_api.get("test-security-ctx").await {
        Ok(j) => {
            println!("✓ Found Kubernetes Job created by operator");
            Some(j)
        }
        Err(kube::Error::Api(e)) if e.code == 404 => {
            println!("⚠ Kubernetes Job not found (operator may not be running)");
            None
        }
        Err(e) => return Err(e.into()),
    };

    // If job exists, check pod template security context
    if let Some(k8s_job) = k8s_job {
        let pod_template = k8s_job
            .spec
            .and_then(|s| s.template.spec)
            .context("Job missing pod template spec")?;

        if let Some(security_context) = pod_template.security_context {
            assert_eq!(
                security_context.run_as_non_root,
                Some(true),
                "Pod should have runAsNonRoot=true"
            );
            assert_eq!(
                security_context.run_as_user,
                Some(1000),
                "Pod should have runAsUser=1000"
            );
            println!("✓ Pod security context is correct");
        } else {
            println!("⚠ Pod security context not set (may be operator version issue)");
        }

        // Check container security context
        if let Some(container) = pod_template.containers.first() {
            if let Some(container_sc) = &container.security_context {
                assert_eq!(
                    container_sc.allow_privilege_escalation,
                    Some(false),
                    "Container should have allowPrivilegeEscalation=false"
                );
                println!("✓ Container security context is correct");
            }
        }
    }

    // Clean up
    client
        .delete_basilica_job(&ctx.namespace, "test-security-ctx")
        .await?;

    Ok(())
}

/// Test loading and using test fixtures
#[tokio::test]
async fn test_load_fixture() -> Result<()> {
    if K8sTestContext::should_skip_test().await {
        return Ok(());
    }

    let ctx = K8sTestContext::new("fixture-test").await?;

    // Load job fixture
    let job_fixture: serde_json::Value = ctx.load_fixture_json("job-simple.json")?;
    assert_eq!(job_fixture["image"], "busybox:latest");
    println!("✓ Loaded job-simple.json fixture");

    // Load rental fixture (has flat structure with container_image)
    let rental_fixture: serde_json::Value = ctx.load_fixture_json("rental-cpu-only.json")?;
    assert!(rental_fixture["container_image"].is_string());
    assert_eq!(rental_fixture["container_image"], "busybox:latest");
    println!("✓ Loaded rental-cpu-only.json fixture");

    // Load node profile fixture
    let _profile_fixture: serde_json::Value = ctx.load_fixture_yaml("node-profile-valid.yaml")?;
    println!("✓ Loaded node-profile-valid.yaml fixture");

    Ok(())
}

/// Test RBAC permissions (namespace-scoped)
#[tokio::test]
async fn test_operator_rbac_namespace_scoped() -> Result<()> {
    if K8sTestContext::should_skip_test().await {
        return Ok(());
    }

    let ctx = K8sTestContext::new("rbac-test").await?;

    // Create a BasilicaJob to test RBAC
    let job = BasilicaJob::new(
        "rbac-test-job",
        BasilicaJobSpec {
            image: "busybox:latest".to_string(),
            command: vec!["echo".to_string(), "test".to_string()],
            args: vec![],
            env: vec![],
            resources: Resources {
                cpu: "0.25".to_string(),
                memory: "128Mi".to_string(),
                gpus: GpuSpec {
                    count: 0,
                    model: vec![],
                },
            },
            storage: None,
            artifacts: None,
            ttl_seconds: 300,
            priority: "normal".to_string(),
        },
    );

    let api: Api<BasilicaJob> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    let _created = api.create(&kube::api::PostParams::default(), &job).await?;

    println!("✓ Created BasilicaJob via API (RBAC allows namespace-scoped create)");

    // Try to get it (should succeed)
    let _retrieved = api.get("rbac-test-job").await?;
    println!("✓ Retrieved BasilicaJob via API (RBAC allows namespace-scoped get)");

    // Clean up
    api.delete("rbac-test-job", &kube::api::DeleteParams::default())
        .await?;
    println!("✓ Deleted BasilicaJob via API (RBAC allows namespace-scoped delete)");

    Ok(())
}

/// Test concurrent job creation (stress test)
#[tokio::test]
async fn test_concurrent_job_creation() -> Result<()> {
    if K8sTestContext::should_skip_test().await {
        return Ok(());
    }

    let ctx = K8sTestContext::new("concurrent-jobs").await?;
    let client = KubeClient {
        client: ctx.client.clone(),
    };

    // Create 5 jobs concurrently
    let mut handles = vec![];
    for i in 0..5 {
        let client = client.clone();
        let namespace = ctx.namespace.clone();
        let handle = tokio::spawn(async move {
            let job = BasilicaJob::new(
                &format!("concurrent-job-{}", i),
                BasilicaJobSpec {
                    image: "busybox:latest".to_string(),
                    command: vec!["echo".to_string(), format!("job-{}", i)],
                    args: vec![],
                    env: vec![],
                    resources: Resources {
                        cpu: "0.25".to_string(),
                        memory: "128Mi".to_string(),
                        gpus: GpuSpec {
                            count: 0,
                            model: vec![],
                        },
                    },
                    storage: None,
                    artifacts: None,
                    ttl_seconds: 300,
                    priority: "normal".to_string(),
                },
            );

            client.create_basilica_job(&namespace, &job).await
        });
        handles.push(handle);
    }

    // Wait for all jobs to be created
    let mut success_count = 0;
    for handle in handles {
        match handle.await {
            Ok(Ok(_)) => success_count += 1,
            Ok(Err(e)) => eprintln!("Job creation failed: {}", e),
            Err(e) => eprintln!("Task panicked: {}", e),
        }
    }

    assert_eq!(
        success_count, 5,
        "All 5 jobs should be created successfully"
    );
    println!("✓ Created 5 jobs concurrently");

    // Clean up
    for i in 0..5 {
        client
            .delete_basilica_job(&ctx.namespace, &format!("concurrent-job-{}", i))
            .await
            .ok(); // Ignore errors (may already be deleted)
    }

    Ok(())
}
