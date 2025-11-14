use crate::error::{ApiError, Result};
use serde_json::Value;

pub fn parse_status_endpoints(val: &Value) -> Vec<String> {
    val.get("status")
        .and_then(|s| s.get("endpoints"))
        .and_then(|e| e.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|x| x.as_str().map(|s| s.to_string()))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default()
}

pub async fn client_from_kubeconfig_content(content: &str) -> anyhow::Result<kube::Client> {
    use kube::config::Kubeconfig;

    let kubeconfig: Kubeconfig = serde_yaml::from_str(content)
        .map_err(|e| anyhow::anyhow!("Failed to parse kubeconfig YAML: {}", e))?;

    let config = kube::Config::from_custom_kubeconfig(
        kubeconfig,
        &kube::config::KubeConfigOptions {
            context: None,
            cluster: None,
            user: None,
        },
    )
    .await?;

    kube::Client::try_from(config).map_err(Into::into)
}

pub async fn create_client() -> anyhow::Result<kube::Client> {
    if let Ok(kubeconfig_content) = std::env::var("KUBECONFIG_CONTENT") {
        tracing::info!("Loading K8s config from KUBECONFIG_CONTENT environment variable");
        return client_from_kubeconfig_content(&kubeconfig_content).await;
    }

    if let Ok(kubeconfig_path) = std::env::var("KUBECONFIG") {
        tracing::info!("Loading K8s config from KUBECONFIG: {}", kubeconfig_path);
        let config = kube::Config::from_kubeconfig(&kube::config::KubeConfigOptions {
            context: None,
            cluster: None,
            user: None,
        })
        .await?;
        return Ok(kube::Client::try_from(config)?);
    }

    tracing::info!("Attempting default K8s client initialization");
    kube::Client::try_default().await.map_err(Into::into)
}

pub async fn create_reference_grant_for_namespace(
    client: &kube::Client,
    user_namespace: &str,
) -> Result<()> {
    use kube::{
        api::{Api, PostParams},
        core::{ApiResource, DynamicObject, GroupVersionKind},
    };
    use serde_json::json;

    let gvk = GroupVersionKind::gvk("gateway.networking.k8s.io", "v1beta1", "ReferenceGrant");
    let ar = ApiResource::from_gvk(&gvk);
    let api: Api<DynamicObject> = Api::namespaced_with(client.clone(), "basilica-system", &ar);

    let reference_grant_name = format!("allow-httproutes-{}", user_namespace);
    let reference_grant = DynamicObject::new(&reference_grant_name, &ar).data(json!({
        "apiVersion": "gateway.networking.k8s.io/v1beta1",
        "kind": "ReferenceGrant",
        "metadata": {
            "name": reference_grant_name,
            "namespace": "basilica-system"
        },
        "spec": {
            "from": [{
                "group": "gateway.networking.k8s.io",
                "kind": "HTTPRoute",
                "namespace": user_namespace
            }],
            "to": [{
                "group": "gateway.networking.k8s.io",
                "kind": "Gateway",
                "name": "basilica-gateway"
            }]
        }
    }));

    match api.create(&PostParams::default(), &reference_grant).await {
        Ok(_) => {
            tracing::info!(
                user_namespace = %user_namespace,
                reference_grant_name = %reference_grant_name,
                "ReferenceGrant created for user namespace"
            );
            Ok(())
        }
        Err(kube::Error::Api(ae)) if ae.code == 409 => {
            tracing::debug!(
                user_namespace = %user_namespace,
                "ReferenceGrant already exists for namespace"
            );
            Ok(())
        }
        Err(e) => {
            tracing::error!(
                error = %e,
                user_namespace = %user_namespace,
                "Failed to create ReferenceGrant"
            );
            Err(ApiError::Internal {
                message: format!(
                    "Failed to create ReferenceGrant for namespace {}: {}",
                    user_namespace, e
                ),
            })
        }
    }
}
