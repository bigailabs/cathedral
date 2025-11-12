use kube::{
    api::{Api, PostParams},
    core::{ApiResource, DynamicObject, GroupVersionKind},
    Client,
};
use serde_json::json;

use crate::error::{ApiError, Result};

pub struct HTTPRoute {
    pub instance_name: String,
    pub namespace: String,
    pub service_name: String,
    pub service_port: i32,
    pub path_prefix: Option<String>,
    pub hostname: Option<String>,
}

impl HTTPRoute {
    pub fn new_path_based(
        instance_name: String,
        namespace: String,
        service_name: String,
        service_port: i32,
    ) -> Self {
        Self {
            instance_name: instance_name.clone(),
            namespace,
            service_name,
            service_port,
            path_prefix: Some(format!("/deployments/{}/", instance_name)),
            hostname: None,
        }
    }

    pub fn new_host_based(
        instance_name: String,
        namespace: String,
        service_name: String,
        service_port: i32,
        hostname: String,
    ) -> Self {
        Self {
            instance_name,
            namespace,
            service_name,
            service_port,
            path_prefix: None,
            hostname: Some(hostname),
        }
    }

    pub async fn create(&self, client: &Client) -> Result<()> {
        let gvk = GroupVersionKind::gvk("gateway.networking.k8s.io", "v1", "HTTPRoute");
        let ar = ApiResource::from_gvk(&gvk);
        let api: Api<DynamicObject> = Api::namespaced_with(client.clone(), &self.namespace, &ar);

        let httproute = self.build_httproute_object();

        match api.create(&PostParams::default(), &httproute).await {
            Ok(_) => {
                tracing::info!(
                    instance_name = %self.instance_name,
                    namespace = %self.namespace,
                    "HTTPRoute created successfully"
                );
                Ok(())
            }
            Err(kube::Error::Api(ae)) if ae.code == 409 => {
                tracing::warn!(
                    instance_name = %self.instance_name,
                    namespace = %self.namespace,
                    "HTTPRoute already exists, skipping"
                );
                Ok(())
            }
            Err(e) => {
                tracing::error!(
                    error = %e,
                    instance_name = %self.instance_name,
                    namespace = %self.namespace,
                    "Failed to create HTTPRoute"
                );
                Err(ApiError::Internal {
                    message: format!("Failed to create HTTPRoute: {}", e),
                })
            }
        }
    }

    pub async fn delete(&self, client: &Client) -> Result<()> {
        let gvk = GroupVersionKind::gvk("gateway.networking.k8s.io", "v1", "HTTPRoute");
        let ar = ApiResource::from_gvk(&gvk);
        let api: Api<DynamicObject> = Api::namespaced_with(client.clone(), &self.namespace, &ar);

        let name = format!("ud-{}", self.instance_name);

        match api.delete(&name, &Default::default()).await {
            Ok(_) => {
                tracing::info!(
                    instance_name = %self.instance_name,
                    namespace = %self.namespace,
                    "HTTPRoute deleted successfully"
                );
                Ok(())
            }
            Err(kube::Error::Api(ae)) if ae.code == 404 => {
                tracing::warn!(
                    instance_name = %self.instance_name,
                    namespace = %self.namespace,
                    "HTTPRoute not found, skipping deletion"
                );
                Ok(())
            }
            Err(e) => {
                tracing::error!(
                    error = %e,
                    instance_name = %self.instance_name,
                    namespace = %self.namespace,
                    "Failed to delete HTTPRoute"
                );
                Err(ApiError::Internal {
                    message: format!("Failed to delete HTTPRoute: {}", e),
                })
            }
        }
    }

    fn build_httproute_object(&self) -> DynamicObject {
        let name = format!("ud-{}", self.instance_name);

        let spec = if let Some(ref hostname) = self.hostname {
            self.build_host_based_spec(hostname)
        } else {
            self.build_path_based_spec()
        };

        DynamicObject {
            types: Some(kube::api::TypeMeta {
                api_version: "gateway.networking.k8s.io/v1".to_string(),
                kind: "HTTPRoute".to_string(),
            }),
            metadata: kube::api::ObjectMeta {
                name: Some(name),
                namespace: Some(self.namespace.clone()),
                ..Default::default()
            },
            data: json!({ "spec": spec }),
        }
    }

    fn build_path_based_spec(&self) -> serde_json::Value {
        json!({
            "parentRefs": [{
                "name": "basilica-gateway",
                "namespace": "basilica-system"
            }],
            "rules": [{
                "matches": [{
                    "path": {
                        "type": "PathPrefix",
                        "value": self.path_prefix.as_ref().unwrap()
                    }
                }],
                "filters": [{
                    "type": "URLRewrite",
                    "urlRewrite": {
                        "path": {
                            "type": "ReplacePrefixMatch",
                            "replacePrefixMatch": "/"
                        }
                    }
                }],
                "backendRefs": [{
                    "name": self.service_name.clone(),
                    "port": self.service_port
                }]
            }]
        })
    }

    fn build_host_based_spec(&self, hostname: &str) -> serde_json::Value {
        json!({
            "parentRefs": [{
                "name": "basilica-gateway",
                "namespace": "basilica-system"
            }],
            "hostnames": [hostname],
            "rules": [{
                "matches": [{
                    "path": {
                        "type": "PathPrefix",
                        "value": "/"
                    }
                }],
                "backendRefs": [{
                    "name": self.service_name.clone(),
                    "port": self.service_port
                }]
            }]
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_new_path_based() {
        let route = HTTPRoute::new_path_based(
            "test-app".to_string(),
            "u-testuser".to_string(),
            "test-app-service".to_string(),
            8080,
        );

        assert_eq!(route.instance_name, "test-app");
        assert_eq!(route.namespace, "u-testuser");
        assert_eq!(route.service_name, "test-app-service");
        assert_eq!(route.service_port, 8080);
        assert_eq!(
            route.path_prefix,
            Some("/deployments/test-app/".to_string())
        );
        assert_eq!(route.hostname, None);
    }

    #[test]
    fn test_new_host_based() {
        let route = HTTPRoute::new_host_based(
            "test-app".to_string(),
            "u-testuser".to_string(),
            "test-app-service".to_string(),
            8080,
            "test-app.deployments.basilica.ai".to_string(),
        );

        assert_eq!(route.instance_name, "test-app");
        assert_eq!(
            route.hostname,
            Some("test-app.deployments.basilica.ai".to_string())
        );
        assert_eq!(route.path_prefix, None);
    }

    #[test]
    fn test_build_path_based_spec() {
        let route = HTTPRoute::new_path_based(
            "test-app".to_string(),
            "u-testuser".to_string(),
            "test-app-service".to_string(),
            8080,
        );

        let spec = route.build_path_based_spec();

        assert_eq!(spec["parentRefs"][0]["name"], "basilica-gateway");
        assert_eq!(spec["parentRefs"][0]["namespace"], "basilica-system");
        assert_eq!(spec["rules"][0]["matches"][0]["path"]["type"], "PathPrefix");
        assert_eq!(
            spec["rules"][0]["matches"][0]["path"]["value"],
            "/deployments/test-app/"
        );
        assert_eq!(
            spec["rules"][0]["backendRefs"][0]["name"],
            "test-app-service"
        );
        assert_eq!(spec["rules"][0]["backendRefs"][0]["port"], 8080);
    }

    #[test]
    fn test_build_host_based_spec() {
        let route = HTTPRoute::new_host_based(
            "test-app".to_string(),
            "u-testuser".to_string(),
            "test-app-service".to_string(),
            8080,
            "test-app.deployments.basilica.ai".to_string(),
        );

        let spec = route.build_host_based_spec("test-app.deployments.basilica.ai");

        assert_eq!(spec["hostnames"][0], "test-app.deployments.basilica.ai");
        assert_eq!(spec["rules"][0]["matches"][0]["path"]["value"], "/");
        assert_eq!(
            spec["rules"][0]["backendRefs"][0]["name"],
            "test-app-service"
        );
    }
}
