use super::types::*;
use crate::error::Result;
use async_trait::async_trait;

#[async_trait]
pub trait ApiK8sClient {
    fn kube_client(&self) -> kube::Client;

    // Job Operations
    async fn create_job(&self, ns: &str, name: &str, spec: JobSpecDto) -> Result<String>;
    async fn get_job_status(&self, ns: &str, name: &str) -> Result<JobStatusDto>;
    async fn delete_job(&self, ns: &str, name: &str) -> Result<()>;
    async fn get_job_logs(&self, ns: &str, name: &str) -> Result<String>;
    async fn exec_job(
        &self,
        ns: &str,
        name: &str,
        command: Vec<String>,
        stdin: Option<String>,
        tty: bool,
    ) -> Result<ExecResultDto>;
    async fn suspend_job(&self, ns: &str, name: &str) -> Result<()>;
    async fn resume_job(&self, ns: &str, name: &str) -> Result<()>;

    // Rental Operations
    async fn create_rental(&self, ns: &str, name: &str, spec: RentalSpecDto) -> Result<String>;
    async fn get_rental_status(&self, ns: &str, name: &str) -> Result<RentalStatusDto>;
    async fn delete_rental(&self, ns: &str, name: &str) -> Result<()>;
    async fn get_rental_logs(
        &self,
        ns: &str,
        name: &str,
        tail: Option<u32>,
        since_seconds: Option<u32>,
    ) -> Result<String>;
    async fn exec_rental(
        &self,
        ns: &str,
        name: &str,
        command: Vec<String>,
        stdin: Option<String>,
        tty: bool,
    ) -> Result<ExecResultDto>;
    async fn extend_rental(
        &self,
        ns: &str,
        name: &str,
        additional_hours: u32,
    ) -> Result<RentalStatusDto>;
    async fn list_rentals(&self, ns: &str) -> Result<Vec<RentalListItemDto>>;

    // Namespace Management
    async fn create_namespace(&self, name: &str) -> Result<()>;
    async fn get_namespace(&self, name: &str) -> Result<()>;

    // ConfigMap Management
    async fn get_configmap(
        &self,
        ns: &str,
        name: &str,
    ) -> Result<std::collections::BTreeMap<String, String>>;
    async fn patch_configmap(
        &self,
        ns: &str,
        name: &str,
        data: std::collections::BTreeMap<String, String>,
    ) -> Result<()>;

    // Deployment Management
    async fn restart_deployment(&self, ns: &str, name: &str) -> Result<()>;
    async fn list_pods(&self, ns: &str, label_selector: &str) -> Result<Vec<String>>;

    // User Deployment Management
    async fn create_user_deployment(
        &self,
        ns: &str,
        name: &str,
        user_id: &str,
        instance_name: &str,
        req: &crate::api::routes::deployments::types::CreateDeploymentRequest,
        path_prefix: &str,
    ) -> Result<()>;
    async fn delete_user_deployment(&self, ns: &str, name: &str) -> Result<()>;
    async fn delete_deployment(&self, ns: &str, name: &str) -> Result<()>;
    async fn delete_service(&self, ns: &str, name: &str) -> Result<()>;
    async fn delete_network_policy(&self, ns: &str, name: &str) -> Result<()>;

    async fn user_deployment_exists(&self, ns: &str, name: &str) -> Result<bool>;

    async fn get_user_deployment_status(&self, ns: &str, name: &str) -> Result<(u32, u32)>;

    async fn get_user_deployment_logs(
        &self,
        ns: &str,
        name: &str,
        tail: Option<u32>,
        since_seconds: Option<u32>,
    ) -> Result<String>;
}
