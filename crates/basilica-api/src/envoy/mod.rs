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

        let full_config = self.build_full_envoy_config(&cm_data);
        cm_data.insert("envoy.yaml".to_string(), full_config);

        self.k8s_client
            .patch_configmap(&self.namespace, &self.configmap_name, cm_data)
            .await?;

        tracing::info!(
            prefix = %route.prefix,
            cluster = %route.cluster_name,
            "Added Envoy route and rebuilt envoy.yaml"
        );

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

        let full_config = self.build_full_envoy_config(&cm_data);
        cm_data.insert("envoy.yaml".to_string(), full_config);

        self.k8s_client
            .patch_configmap(&self.namespace, &self.configmap_name, cm_data)
            .await?;

        tracing::info!(
            prefix = %prefix,
            cluster = %cluster_name,
            "Removed Envoy route and rebuilt envoy.yaml"
        );

        Ok(())
    }

    fn build_full_envoy_config(&self, cm_data: &std::collections::BTreeMap<String, String>) -> String {
        let mut routes = Vec::new();
        let mut clusters = Vec::new();

        for (key, value) in cm_data.iter() {
            if key.starts_with("route_") {
                let indented = indent_lines(value, 18);
                routes.push(indented);
            } else if key.starts_with("cluster_") {
                let indented = indent_lines(value, 6);
                clusters.push(indented);
            }
        }

        routes.sort();
        clusters.sort();

        let routes_section = if routes.is_empty() {
            String::new()
        } else {
            format!("\n{}\n                  ", routes.join("\n"))
        };

        let clusters_section = if clusters.is_empty() {
            String::new()
        } else {
            format!("{}\n      ", clusters.join("\n"))
        };

        format!(
            r#"static_resources:
  listeners:
  - name: listener_http
    address:
      socket_address:
        address: 0.0.0.0
        port_value: 8080
    filter_chains:
    - filters:
      - name: envoy.filters.network.http_connection_manager
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
          stat_prefix: ingress_http
          codec_type: AUTO
          route_config:
            name: local_route
            virtual_hosts:
            - name: user_deployments
              domains: ["*"]
              routes:
                  - match:
                      prefix: "/health"
                    direct_response:
                      status: 200
                      body:
                        inline_string: "healthy"
{}
                  - match:
                      prefix: "/"
                    route:
                      cluster: dynamic_forward_proxy_cluster
                      timeout: 0s
          http_filters:
          - name: envoy.filters.http.dynamic_forward_proxy
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.http.dynamic_forward_proxy.v3.FilterConfig
              dns_cache_config:
                name: dynamic_forward_proxy_cache_config
                dns_lookup_family: V4_ONLY
          - name: envoy.filters.http.router
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router

  clusters:
{}
      - name: dynamic_forward_proxy_cluster
        connect_timeout: 5s
        lb_policy: CLUSTER_PROVIDED
        cluster_type:
          name: envoy.clusters.dynamic_forward_proxy
          typed_config:
            "@type": type.googleapis.com/envoy.extensions.clusters.dynamic_forward_proxy.v3.ClusterConfig
            dns_cache_config:
              name: dynamic_forward_proxy_cache_config
              dns_lookup_family: V4_ONLY

admin:
  address:
    socket_address:
      address: 0.0.0.0
      port_value: 9901
"#,
            routes_section, clusters_section
        )
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

fn indent_lines(text: &str, spaces: usize) -> String {
    let indent = " ".repeat(spaces);
    text.lines()
        .map(|line| {
            if line.is_empty() {
                line.to_string()
            } else {
                format!("{}{}", indent, line)
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
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

    #[test]
    fn test_build_full_envoy_config() {
        let manager = EnvoyConfigManager {
            k8s_client: Arc::new(crate::k8s_client::MockK8sClient::default()),
            namespace: "test".to_string(),
            configmap_name: "test-cm".to_string(),
        };

        let mut cm_data = std::collections::BTreeMap::new();

        let route1 = EnvoyRoute {
            prefix: "/deployments/app1/".to_string(),
            cluster_name: "user_deployment_app1".to_string(),
            target_service: "app1-service.u-user1:8080".to_string(),
            rewrite: "/".to_string(),
        };

        let route2 = EnvoyRoute {
            prefix: "/deployments/app2/".to_string(),
            cluster_name: "user_deployment_app2".to_string(),
            target_service: "app2-service.u-user2:9000".to_string(),
            rewrite: "/".to_string(),
        };

        cm_data.insert("route_deployments_app1".to_string(), manager.render_route_snippet(&route1));
        cm_data.insert("cluster_user_deployment_app1".to_string(), manager.render_cluster_snippet(&route1));
        cm_data.insert("route_deployments_app2".to_string(), manager.render_route_snippet(&route2));
        cm_data.insert("cluster_user_deployment_app2".to_string(), manager.render_cluster_snippet(&route2));

        let config = manager.build_full_envoy_config(&cm_data);

        assert!(config.contains("static_resources:"));
        assert!(config.contains("listener_http"));
        assert!(config.contains("user_deployments"));
        assert!(config.contains("/health"));
        assert!(config.contains("healthy"));
        assert!(config.contains("/deployments/app1/"));
        assert!(config.contains("/deployments/app2/"));
        assert!(config.contains("user_deployment_app1"));
        assert!(config.contains("user_deployment_app2"));
        assert!(config.contains("app1-service.u-user1"));
        assert!(config.contains("app2-service.u-user2"));
        assert!(config.contains("port_value: 8080"));
        assert!(config.contains("port_value: 9000"));
        assert!(config.contains("dynamic_forward_proxy_cluster"));
        assert!(config.contains("admin:"));
        assert!(config.contains("9901"));
    }

    #[test]
    fn test_build_full_envoy_config_empty() {
        let manager = EnvoyConfigManager {
            k8s_client: Arc::new(crate::k8s_client::MockK8sClient::default()),
            namespace: "test".to_string(),
            configmap_name: "test-cm".to_string(),
        };

        let cm_data = std::collections::BTreeMap::new();
        let config = manager.build_full_envoy_config(&cm_data);

        assert!(config.contains("static_resources:"));
        assert!(config.contains("/health"));
        assert!(config.contains("dynamic_forward_proxy_cluster"));
        assert!(!config.contains("user_deployment_"));
        assert!(!config.contains("/deployments/"));
    }
}
