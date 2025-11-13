use crate::error::ApiError;
use crate::k8s::{helpers, mock::MockK8sClient, r#trait::ApiK8sClient, types::*};

#[test]
fn parses_endpoints_from_status_value() {
    let val = serde_json::json!({
        "status": {
            "state": "Active",
            "podName": "rental-pod-1",
            "endpoints": ["NodePort:8080", "LoadBalancer:443"]
        }
    });
    let eps = helpers::parse_status_endpoints(&val);
    assert_eq!(eps, vec!["NodePort:8080", "LoadBalancer:443"]);
}

#[test]
fn endpoints_absent_defaults_empty() {
    let val = serde_json::json!({ "status": { "state": "Provisioning" } });
    let eps = helpers::parse_status_endpoints(&val);
    assert!(eps.is_empty());
}

#[tokio::test]
async fn mock_k8s_create_get_delete() {
    let c = MockK8sClient::default();
    let name = c
        .create_job(
            "ns",
            "job1",
            JobSpecDto {
                image: "img".into(),
                command: vec![],
                args: vec![],
                env: vec![],
                resources: Resources {
                    cpu: "1".into(),
                    memory: "512Mi".into(),
                    gpus: GpuSpec {
                        count: 0,
                        model: vec![],
                    },
                },
                ttl_seconds: 0,
                ports: vec![],
                storage: None,
            },
        )
        .await
        .unwrap();
    assert_eq!(name, "job1");
    let st = c.get_job_status("ns", "job1").await.unwrap();
    assert_eq!(st.phase, "Pending");
    assert!(st.endpoints.is_empty());
    c.delete_job("ns", "job1").await.unwrap();
    assert!(matches!(
        c.get_job_status("ns", "job1").await,
        Err(ApiError::NotFound { message: _ })
    ));
}
