use crate::error::Result;
use crate::k8s_client::ApiK8sClient;
use std::sync::Arc;

#[derive(Debug, Clone)]
pub struct EnvoyRoute {
    pub prefix: String,
    pub cluster_name: String,
    pub target_service: String,
    pub rewrite: String,
}

pub struct EnvoyConfigManager {
    k8s_client: Arc<dyn ApiK8sClient + Send + Sync>,
    namespace: String,
    configmap_name: String,
}

impl EnvoyConfigManager {
    pub fn new(
        k8s_client: Arc<dyn ApiK8sClient + Send + Sync>,
        namespace: String,
        configmap_name: String,
    ) -> Self {
        Self {
            k8s_client,
            namespace,
            configmap_name,
        }
    }

    pub async fn add_route(&self, route: EnvoyRoute) -> Result<()> {
        let mut cm_data = self
            .k8s_client
            .get_configmap(&self.namespace, &self.configmap_name)
            .await?;

        let route_key = format!("route_{}", sanitize_key(&route.prefix));
        let cluster_key = format!("cluster_{}", sanitize_key(&route.cluster_name));

        if cm_data.contains_key(&route_key) && cm_data.contains_key(&cluster_key) {
            tracing::debug!(
                prefix = %route.prefix,
                cluster = %route.cluster_name,
                "Envoy route and cluster already exist, skipping"
            );
            return Ok(());
        }

        let route_yaml = self.render_route_snippet(&route);
        let cluster_yaml = self.render_cluster_snippet(&route);

        cm_data.insert(route_key, route_yaml);
        cm_data.insert(cluster_key, cluster_yaml);

        self.k8s_client
            .patch_configmap(&self.namespace, &self.configmap_name, cm_data)
            .await?;

        Ok(())
    }

    pub async fn remove_route(&self, prefix: &str, cluster_name: &str) -> Result<()> {
        let mut cm_data = self
            .k8s_client
            .get_configmap(&self.namespace, &self.configmap_name)
            .await?;

        let route_key = format!("route_{}", sanitize_key(prefix));
        let cluster_key = format!("cluster_{}", sanitize_key(cluster_name));

        cm_data.remove(&route_key);
        cm_data.remove(&cluster_key);

        self.k8s_client
            .patch_configmap(&self.namespace, &self.configmap_name, cm_data)
            .await?;

        Ok(())
    }

    fn render_route_snippet(&self, route: &EnvoyRoute) -> String {
        format!(
            r#"- match:
    prefix: "{}"
  route:
    cluster: {}
    prefix_rewrite: "{}"
    timeout: 300s
    retry_policy:
      retry_on: 5xx,connect-failure
      num_retries: 2"#,
            route.prefix, route.cluster_name, route.rewrite
        )
    }

    fn render_cluster_snippet(&self, route: &EnvoyRoute) -> String {
        let (host, port) = route
            .target_service
            .split_once(':')
            .unwrap_or((&route.target_service, "80"));

        format!(
            r#"- name: {}
  type: STRICT_DNS
  connect_timeout: 5s
  lb_policy: ROUND_ROBIN
  load_assignment:
    cluster_name: {}
    endpoints:
    - lb_endpoints:
      - endpoint:
          address:
            socket_address:
              address: {}
              port_value: {}
  health_checks:
  - timeout: 5s
    interval: 10s
    unhealthy_threshold: 2
    healthy_threshold: 2
    http_health_check:
      path: /health"#,
            route.cluster_name, route.cluster_name, host, port
        )
    }
}

fn sanitize_key(s: &str) -> String {
    s.replace(['/', '.', ':'], "_")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sanitize_key() {
        assert_eq!(sanitize_key("/deployments/my-app"), "_deployments_my-app");
        assert_eq!(
            sanitize_key("service.namespace:8080"),
            "service_namespace_8080"
        );
    }

    #[test]
    fn test_render_route_snippet() {
        let manager = EnvoyConfigManager {
            k8s_client: Arc::new(crate::k8s_client::MockK8sClient::default()),
            namespace: "test".to_string(),
            configmap_name: "test-cm".to_string(),
        };

        let route = EnvoyRoute {
            prefix: "/deployments/my-app".to_string(),
            cluster_name: "user_deployment_my-app".to_string(),
            target_service: "my-app-service.u-user123:8080".to_string(),
            rewrite: "/".to_string(),
        };

        let snippet = manager.render_route_snippet(&route);
        assert!(snippet.contains("/deployments/my-app"));
        assert!(snippet.contains("user_deployment_my-app"));
        assert!(snippet.contains("prefix_rewrite: \"/\""));
    }

    #[test]
    fn test_render_cluster_snippet() {
        let manager = EnvoyConfigManager {
            k8s_client: Arc::new(crate::k8s_client::MockK8sClient::default()),
            namespace: "test".to_string(),
            configmap_name: "test-cm".to_string(),
        };

        let route = EnvoyRoute {
            prefix: "/deployments/my-app".to_string(),
            cluster_name: "user_deployment_my-app".to_string(),
            target_service: "my-app-service.u-user123:8080".to_string(),
            rewrite: "/".to_string(),
        };

        let snippet = manager.render_cluster_snippet(&route);
        assert!(snippet.contains("name: user_deployment_my-app"));
        assert!(snippet.contains("address: my-app-service.u-user123"));
        assert!(snippet.contains("port_value: 8080"));
        assert!(snippet.contains("STRICT_DNS"));
    }
}
