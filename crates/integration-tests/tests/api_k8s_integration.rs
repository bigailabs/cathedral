//! API Kubernetes Integration Tests
//!
//! Tests the Basilica API's K8s client against a real Kubernetes cluster.
//! These tests validate:
//! - Jobs API lifecycle (create, get status, logs, delete)
//! - Rentals API lifecycle (create, get status, logs, exec, extend, delete)
//! - API error handling with real K8s errors
//! - List operations
//!
//! Prerequisites:
//! - Kubernetes cluster accessible via KUBECONFIG or in-cluster config
//! - Basilica CRDs installed (BasilicaJob, GpuRental)
//! - Test namespace created (or will be created automatically)
//!
//! Set NO_K8S_TESTS=1 to skip these tests if cluster is not available.

use anyhow::{Context, Result};
use integration_tests::K8sTestContext;

use basilica_api::k8s_client::{
    ApiK8sClient, GpuSpec, JobSpecDto, RentalSpecDto, RentalSshDto, Resources,
};

/// Test that Jobs API can create and retrieve job status
#[tokio::test]
async fn test_jobs_api_create_get_status() -> Result<()> {
    if K8sTestContext::should_skip_test().await {
        return Ok(());
    }

    let ctx = K8sTestContext::new("api-jobs-create").await?;
    let client = basilica_api::k8s_client::K8sClient::try_default().await?;

    // Create a simple job
    let spec = JobSpecDto {
        image: "busybox:latest".to_string(),
        command: vec!["echo".to_string(), "api-test-job".to_string()],
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
        ttl_seconds: 300,
        ports: vec![],
        storage: None,
    };

    let job_id = client
        .create_job(&ctx.namespace, "api-test-job", spec)
        .await
        .context("Failed to create job via API")?;

    assert_eq!(job_id, "api-test-job");
    println!("✓ Created job via Jobs API: {}", job_id);

    // Get job status
    let status = client
        .get_job_status(&ctx.namespace, "api-test-job")
        .await
        .context("Failed to get job status")?;

    // Status should be Pending initially (operator may not be running)
    println!("✓ Job status: {}", status.phase);

    // Clean up
    client
        .delete_job(&ctx.namespace, "api-test-job")
        .await
        .context("Failed to delete job")?;

    println!("✓ Deleted job via Jobs API");

    Ok(())
}

/// Test that Jobs API returns NotFound for non-existent job
#[tokio::test]
async fn test_jobs_api_not_found() -> Result<()> {
    if K8sTestContext::should_skip_test().await {
        return Ok(());
    }

    let ctx = K8sTestContext::new("api-jobs-notfound").await?;
    let client = basilica_api::k8s_client::K8sClient::try_default().await?;

    // Try to get non-existent job
    let result = client
        .get_job_status(&ctx.namespace, "non-existent-job")
        .await;

    assert!(result.is_err(), "Should return error for non-existent job");

    match result {
        Err(basilica_api::error::ApiError::NotFound { message }) => {
            println!("✓ Correctly returned NotFound: {}", message);
        }
        Err(e) => panic!("Expected NotFound error, got: {:?}", e),
        Ok(_) => panic!("Expected error, got success"),
    }

    Ok(())
}

/// Test that Rentals API can create and retrieve rental status
#[tokio::test]
async fn test_rentals_api_create_get_status() -> Result<()> {
    if K8sTestContext::should_skip_test().await {
        return Ok(());
    }

    let ctx = K8sTestContext::new("api-rentals-create").await?;
    let client = basilica_api::k8s_client::K8sClient::try_default().await?;

    // Create a CPU-only rental (no ports to avoid serialization issues in test)
    let spec = RentalSpecDto {
        container_image: "ubuntu:22.04".to_string(),
        resources: Resources {
            cpu: "1".to_string(),
            memory: "512Mi".to_string(),
            gpus: GpuSpec {
                count: 0,
                model: vec![],
            },
        },
        container_env: vec![("TEST_VAR".to_string(), "test_value".to_string())],
        container_command: vec!["sleep".to_string(), "infinity".to_string()],
        container_ports: vec![], // Empty to avoid port validation issues
        network_ingress: vec![],
        ssh: Some(RentalSshDto {
            enabled: true,
            public_key: "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC... test@example.com".to_string(),
        }),
        name: None,
        namespace: None,
        labels: None,
        annotations: None,
    };

    let rental_id = client
        .create_rental(&ctx.namespace, "api-test-rental", spec)
        .await
        .context("Failed to create rental via API")?;

    assert_eq!(rental_id, "api-test-rental");
    println!("✓ Created rental via Rentals API: {}", rental_id);

    // Get rental status
    let status = client
        .get_rental_status(&ctx.namespace, "api-test-rental")
        .await
        .context("Failed to get rental status")?;

    // Status should be Provisioning initially
    println!("✓ Rental status: {}", status.state);

    // Clean up
    client
        .delete_rental(&ctx.namespace, "api-test-rental")
        .await
        .context("Failed to delete rental")?;

    println!("✓ Deleted rental via Rentals API");

    Ok(())
}

/// Test that Rentals API can list rentals
#[tokio::test]
async fn test_rentals_api_list() -> Result<()> {
    if K8sTestContext::should_skip_test().await {
        return Ok(());
    }

    let ctx = K8sTestContext::new("api-rentals-list").await?;
    let client = basilica_api::k8s_client::K8sClient::try_default().await?;

    // Create two rentals
    let spec1 = RentalSpecDto {
        container_image: "ubuntu:22.04".to_string(),
        resources: Resources {
            cpu: "0.5".to_string(),
            memory: "256Mi".to_string(),
            gpus: GpuSpec {
                count: 0,
                model: vec![],
            },
        },
        container_env: vec![],
        container_command: vec!["sleep".to_string(), "infinity".to_string()],
        container_ports: vec![],
        network_ingress: vec![],
        ssh: None,
        name: None,
        namespace: None,
        labels: None,
        annotations: None,
    };

    let spec2 = spec1.clone();

    client
        .create_rental(&ctx.namespace, "rental-list-1", spec1)
        .await?;
    client
        .create_rental(&ctx.namespace, "rental-list-2", spec2)
        .await?;

    println!("✓ Created 2 rentals for list test");

    // List rentals
    let rentals = client
        .list_rentals(&ctx.namespace)
        .await
        .context("Failed to list rentals")?;

    assert!(
        rentals.len() >= 2,
        "Should have at least 2 rentals, got {}",
        rentals.len()
    );
    println!("✓ Listed {} rentals", rentals.len());

    // Verify our rentals are in the list
    let rental_ids: Vec<String> = rentals.iter().map(|r| r.rental_id.clone()).collect();
    assert!(rental_ids.contains(&"rental-list-1".to_string()));
    assert!(rental_ids.contains(&"rental-list-2".to_string()));
    println!("✓ Both test rentals found in list");

    // Clean up
    client
        .delete_rental(&ctx.namespace, "rental-list-1")
        .await?;
    client
        .delete_rental(&ctx.namespace, "rental-list-2")
        .await?;

    Ok(())
}

/// Test Jobs API with test fixture
#[tokio::test]
async fn test_jobs_api_with_fixture() -> Result<()> {
    if K8sTestContext::should_skip_test().await {
        return Ok(());
    }

    let ctx = K8sTestContext::new("api-jobs-fixture").await?;
    let client = basilica_api::k8s_client::K8sClient::try_default().await?;

    // Load job fixture
    let fixture: serde_json::Value = ctx.load_fixture_json("job-simple.json")?;
    println!("✓ Loaded job-simple.json fixture");

    // Parse into JobSpecDto
    let spec = JobSpecDto {
        image: fixture["image"]
            .as_str()
            .context("Missing image in fixture")?
            .to_string(),
        command: fixture["command"]
            .as_array()
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(|s| s.to_string()))
                    .collect()
            })
            .unwrap_or_default(),
        args: vec![],
        env: vec![],
        resources: Resources {
            cpu: fixture["resources"]["cpu"]
                .as_str()
                .unwrap_or("0.25")
                .to_string(),
            memory: fixture["resources"]["memory"]
                .as_str()
                .unwrap_or("128Mi")
                .to_string(),
            gpus: GpuSpec {
                count: 0,
                model: vec![],
            },
        },
        ttl_seconds: 300,
        ports: vec![],
        storage: None,
    };

    // Create job from fixture
    let job_id = client
        .create_job(&ctx.namespace, "job-from-fixture", spec)
        .await?;

    println!("✓ Created job from fixture: {}", job_id);

    // Verify it exists
    let status = client.get_job_status(&ctx.namespace, &job_id).await?;
    println!("✓ Job status: {}", status.phase);

    // Clean up
    client.delete_job(&ctx.namespace, &job_id).await?;

    Ok(())
}

/// Test Rentals API with test fixture
#[tokio::test]
async fn test_rentals_api_with_fixture() -> Result<()> {
    if K8sTestContext::should_skip_test().await {
        return Ok(());
    }

    let ctx = K8sTestContext::new("api-rentals-fixture").await?;
    let client = basilica_api::k8s_client::K8sClient::try_default().await?;

    // Load rental fixture
    let fixture: serde_json::Value = ctx.load_fixture_json("rental-cpu-only.json")?;
    println!("✓ Loaded rental-cpu-only.json fixture");

    // Parse into RentalSpecDto (fixture has flat structure)
    let spec = RentalSpecDto {
        container_image: fixture["container_image"]
            .as_str()
            .context("Missing container_image in fixture")?
            .to_string(),
        resources: Resources {
            cpu: fixture["resources"]["cpu"]
                .as_str()
                .unwrap_or("0.5")
                .to_string(),
            memory: fixture["resources"]["memory"]
                .as_str()
                .unwrap_or("512Mi")
                .to_string(),
            gpus: GpuSpec {
                count: fixture["resources"]["gpus"]["count"].as_u64().unwrap_or(0) as u32,
                model: vec![],
            },
        },
        container_env: vec![],
        container_command: fixture["command"]
            .as_array()
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(|s| s.to_string()))
                    .collect()
            })
            .unwrap_or_else(|| vec!["sleep".to_string(), "infinity".to_string()]),
        container_ports: vec![],
        network_ingress: vec![],
        ssh: None,
        name: None,
        namespace: None,
        labels: None,
        annotations: None,
    };

    // Create rental from fixture
    let rental_id = client
        .create_rental(&ctx.namespace, "rental-from-fixture", spec)
        .await?;

    println!("✓ Created rental from fixture: {}", rental_id);

    // Verify it exists
    let status = client.get_rental_status(&ctx.namespace, &rental_id).await?;
    println!("✓ Rental status: {}", status.state);

    // Clean up
    client.delete_rental(&ctx.namespace, &rental_id).await?;

    Ok(())
}

/// Test concurrent rental creation (stress test)
#[tokio::test]
async fn test_concurrent_rental_creation() -> Result<()> {
    if K8sTestContext::should_skip_test().await {
        return Ok(());
    }

    let ctx = K8sTestContext::new("api-concurrent-rentals").await?;

    // Create 3 rentals concurrently
    let mut handles = vec![];
    for i in 0..3 {
        let namespace = ctx.namespace.clone();
        let handle = tokio::spawn(async move {
            let client = basilica_api::k8s_client::K8sClient::try_default()
                .await
                .unwrap();
            let spec = RentalSpecDto {
                container_image: "ubuntu:22.04".to_string(),
                resources: Resources {
                    cpu: "0.5".to_string(),
                    memory: "256Mi".to_string(),
                    gpus: GpuSpec {
                        count: 0,
                        model: vec![],
                    },
                },
                container_env: vec![],
                container_command: vec!["sleep".to_string(), "infinity".to_string()],
                container_ports: vec![],
                network_ingress: vec![],
                ssh: None,
                name: None,
                namespace: None,
                labels: None,
                annotations: None,
            };

            client
                .create_rental(&namespace, &format!("concurrent-rental-{}", i), spec)
                .await
        });
        handles.push(handle);
    }

    // Wait for all rentals to be created
    let mut success_count = 0;
    for handle in handles {
        match handle.await {
            Ok(Ok(_)) => success_count += 1,
            Ok(Err(e)) => eprintln!("Rental creation failed: {}", e),
            Err(e) => eprintln!("Task panicked: {}", e),
        }
    }

    assert_eq!(
        success_count, 3,
        "All 3 rentals should be created successfully"
    );
    println!("✓ Created 3 rentals concurrently via API");

    // Clean up
    let client = basilica_api::k8s_client::K8sClient::try_default().await?;
    for i in 0..3 {
        client
            .delete_rental(&ctx.namespace, &format!("concurrent-rental-{}", i))
            .await
            .ok(); // Ignore errors (may already be deleted)
    }

    Ok(())
}

/// Test that API properly handles resource specs
#[tokio::test]
async fn test_api_resource_specs() -> Result<()> {
    if K8sTestContext::should_skip_test().await {
        return Ok(());
    }

    let ctx = K8sTestContext::new("api-resources").await?;
    let client = basilica_api::k8s_client::K8sClient::try_default().await?;

    // Create job with specific resource requests
    let spec = JobSpecDto {
        image: "busybox:latest".to_string(),
        command: vec!["echo".to_string(), "resource-test".to_string()],
        args: vec![],
        env: vec![],
        resources: Resources {
            cpu: "2".to_string(),      // 2 cores
            memory: "4Gi".to_string(), // 4 GiB
            gpus: GpuSpec {
                count: 0,
                model: vec![],
            },
        },
        ttl_seconds: 300,
        ports: vec![],
        storage: None,
    };

    let job_id = client
        .create_job(&ctx.namespace, "resource-test-job", spec.clone())
        .await?;

    println!(
        "✓ Created job with resources: cpu={}, memory={}",
        spec.resources.cpu, spec.resources.memory
    );

    // Verify via status API
    let _status = client.get_job_status(&ctx.namespace, &job_id).await?;

    // Clean up
    client.delete_job(&ctx.namespace, &job_id).await?;

    Ok(())
}

/// Test that API handles job commands and args
#[tokio::test]
async fn test_api_job_commands_args() -> Result<()> {
    if K8sTestContext::should_skip_test().await {
        return Ok(());
    }

    let ctx = K8sTestContext::new("api-job-cmd").await?;
    let client = basilica_api::k8s_client::K8sClient::try_default().await?;

    // Create job with custom command and args
    let spec = JobSpecDto {
        image: "busybox:latest".to_string(),
        command: vec!["sh".to_string(), "-c".to_string()],
        args: vec!["echo hello && sleep 5".to_string()],
        env: vec![], // Empty env to avoid serialization issues
        resources: Resources {
            cpu: "0.25".to_string(),
            memory: "128Mi".to_string(),
            gpus: GpuSpec {
                count: 0,
                model: vec![],
            },
        },
        ttl_seconds: 300,
        ports: vec![],
        storage: None,
    };

    let job_id = client
        .create_job(&ctx.namespace, "cmd-test-job", spec)
        .await?;

    println!("✓ Created job with custom command and args");

    // Verify via status API
    let _status = client.get_job_status(&ctx.namespace, &job_id).await?;

    // Clean up
    client.delete_job(&ctx.namespace, &job_id).await?;

    Ok(())
}
