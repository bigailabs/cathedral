use anyhow::{anyhow, Result};
use async_trait::async_trait;
use k8s_openapi::api::batch::v1::Job;
use k8s_openapi::api::core::v1::{PersistentVolumeClaim, Pod, Secret, Service};
use k8s_openapi::api::networking::v1::NetworkPolicy;
use kube::ResourceExt;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;

use crate::crd::basilica_job::BasilicaJob;
use crate::crd::gpu_rental::GpuRental;

#[async_trait]
pub trait K8sClient: Send + Sync {
    // CRDs
    async fn create_basilica_job(&self, ns: &str, obj: &BasilicaJob) -> Result<BasilicaJob>;
    async fn get_basilica_job(&self, ns: &str, name: &str) -> Result<BasilicaJob>;
    async fn delete_basilica_job(&self, ns: &str, name: &str) -> Result<()>;
    async fn update_basilica_job_status(&self, ns: &str, name: &str, status: crate::crd::basilica_job::BasilicaJobStatus) -> Result<()>;

    async fn create_gpu_rental(&self, ns: &str, obj: &GpuRental) -> Result<GpuRental>;
    async fn get_gpu_rental(&self, ns: &str, name: &str) -> Result<GpuRental>;
    async fn delete_gpu_rental(&self, ns: &str, name: &str) -> Result<()>;
    async fn update_gpu_rental_status(&self, ns: &str, name: &str, status: crate::crd::gpu_rental::GpuRentalStatus) -> Result<()>;

    // Core
    async fn create_pod(&self, ns: &str, pod: &Pod) -> Result<Pod>;
    async fn get_pod(&self, ns: &str, name: &str) -> Result<Pod>;
    async fn delete_pod(&self, ns: &str, name: &str) -> Result<()>;
    async fn list_pods_with_label(&self, ns: &str, key: &str, value: &str) -> Result<Vec<Pod>>;

    async fn create_service(&self, ns: &str, svc: &Service) -> Result<Service>;
    async fn get_service(&self, ns: &str, name: &str) -> Result<Service>;
    async fn delete_service(&self, ns: &str, name: &str) -> Result<()>;
    async fn list_services_with_label(&self, ns: &str, key: &str, value: &str) -> Result<Vec<Service>>;

    async fn create_network_policy(&self, ns: &str, np: &NetworkPolicy) -> Result<NetworkPolicy>;
    async fn create_pvc(&self, ns: &str, pvc: &PersistentVolumeClaim) -> Result<PersistentVolumeClaim>;
    async fn create_secret(&self, ns: &str, secret: &Secret) -> Result<Secret>;

    async fn create_job(&self, ns: &str, job: &Job) -> Result<Job>;
    async fn get_job(&self, ns: &str, name: &str) -> Result<Job>;
}

/// In-memory mock client implementing a subset of the Kubernetes API for tests.
#[derive(Default, Clone)]
pub struct MockK8sClient {
    // namespace -> (name -> obj)
    jobs: Arc<RwLock<HashMap<String, HashMap<String, Job>>>>,
    pods: Arc<RwLock<HashMap<String, HashMap<String, Pod>>>>,
    services: Arc<RwLock<HashMap<String, HashMap<String, Service>>>>,
    network_policies: Arc<RwLock<HashMap<String, HashMap<String, NetworkPolicy>>>>,
    pvcs: Arc<RwLock<HashMap<String, HashMap<String, PersistentVolumeClaim>>>>,
    secrets: Arc<RwLock<HashMap<String, HashMap<String, Secret>>>>,
    rent_crds: Arc<RwLock<HashMap<String, HashMap<String, GpuRental>>>>,
    job_crds: Arc<RwLock<HashMap<String, HashMap<String, BasilicaJob>>>>,
}

fn key(ns: &str) -> String { ns.to_string() }

#[async_trait]
impl K8sClient for MockK8sClient {
    async fn create_basilica_job(&self, ns: &str, obj: &BasilicaJob) -> Result<BasilicaJob> {
        let name = obj.name_any();
        if name.is_empty() { return Err(anyhow!("BasilicaJob missing metadata.name")); }
        let mut map = self.job_crds.write().await;
        map.entry(key(ns)).or_default().insert(name.clone(), obj.clone());
        Ok(obj.clone())
    }

    async fn get_basilica_job(&self, ns: &str, name: &str) -> Result<BasilicaJob> {
        let map = self.job_crds.read().await;
        map.get(ns)
            .and_then(|m| m.get(name))
            .cloned()
            .ok_or_else(|| anyhow!("BasilicaJob not found: {}/{}", ns, name))
    }

    async fn delete_basilica_job(&self, ns: &str, name: &str) -> Result<()> {
        let mut map = self.job_crds.write().await;
        map.get_mut(ns).and_then(|m| m.remove(name));
        Ok(())
    }

    async fn update_basilica_job_status(&self, ns: &str, name: &str, status: crate::crd::basilica_job::BasilicaJobStatus) -> Result<()> {
        let mut map = self.job_crds.write().await;
        let ns_map = map.get_mut(ns).ok_or_else(|| anyhow!("namespace not found: {}", ns))?;
        let bj = ns_map.get_mut(name).ok_or_else(|| anyhow!("BasilicaJob not found: {}/{}", ns, name))?;
        bj.status = Some(status);
        Ok(())
    }

    async fn create_gpu_rental(&self, ns: &str, obj: &GpuRental) -> Result<GpuRental> {
        let name = obj.name_any();
        if name.is_empty() { return Err(anyhow!("GpuRental missing metadata.name")); }
        let mut map = self.rent_crds.write().await;
        map.entry(key(ns)).or_default().insert(name.clone(), obj.clone());
        Ok(obj.clone())
    }

    async fn get_gpu_rental(&self, ns: &str, name: &str) -> Result<GpuRental> {
        let map = self.rent_crds.read().await;
        map.get(ns)
            .and_then(|m| m.get(name))
            .cloned()
            .ok_or_else(|| anyhow!("GpuRental not found: {}/{}", ns, name))
    }

    async fn delete_gpu_rental(&self, ns: &str, name: &str) -> Result<()> {
        let mut map = self.rent_crds.write().await;
        map.get_mut(ns).and_then(|m| m.remove(name));
        Ok(())
    }

    async fn update_gpu_rental_status(&self, ns: &str, name: &str, status: crate::crd::gpu_rental::GpuRentalStatus) -> Result<()> {
        let mut map = self.rent_crds.write().await;
        let ns_map = map.get_mut(ns).ok_or_else(|| anyhow!("namespace not found: {}", ns))?;
        let gr = ns_map.get_mut(name).ok_or_else(|| anyhow!("GpuRental not found: {}/{}", ns, name))?;
        gr.status = Some(status);
        Ok(())
    }

    async fn create_pod(&self, ns: &str, pod: &Pod) -> Result<Pod> {
        let name = pod.name_any();
        if name.is_empty() { return Err(anyhow!("Pod missing metadata.name")); }
        let mut map = self.pods.write().await;
        map.entry(key(ns)).or_default().insert(name.clone(), pod.clone());
        Ok(pod.clone())
    }

    async fn get_pod(&self, ns: &str, name: &str) -> Result<Pod> {
        let map = self.pods.read().await;
        map.get(ns)
            .and_then(|m| m.get(name))
            .cloned()
            .ok_or_else(|| anyhow!("Pod not found: {}/{}", ns, name))
    }

    async fn delete_pod(&self, ns: &str, name: &str) -> Result<()> {
        let mut map = self.pods.write().await;
        map.get_mut(ns).and_then(|m| m.remove(name));
        Ok(())
    }

    async fn list_pods_with_label(&self, ns: &str, key: &str, value: &str) -> Result<Vec<Pod>> {
        let map = self.pods.read().await;
        let list = map
            .get(ns)
            .map(|m| {
                m.values()
                    .filter(|p| p.labels().get(key).map(|v| v.as_str() == value).unwrap_or(false))
                    .cloned()
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        Ok(list)
    }

    async fn create_service(&self, ns: &str, svc: &Service) -> Result<Service> {
        let name = svc.metadata.name.clone().unwrap_or_default();
        if name.is_empty() { return Err(anyhow!("Service missing metadata.name")); }
        let mut map = self.services.write().await;
        map.entry(key(ns)).or_default().insert(name.clone(), svc.clone());
        Ok(svc.clone())
    }

    async fn get_service(&self, ns: &str, name: &str) -> Result<Service> {
        let map = self.services.read().await;
        map.get(ns)
            .and_then(|m| m.get(name))
            .cloned()
            .ok_or_else(|| anyhow!("Service not found: {}/{}", ns, name))
    }

    async fn delete_service(&self, ns: &str, name: &str) -> Result<()> {
        let mut map = self.services.write().await;
        map.get_mut(ns).and_then(|m| m.remove(name));
        Ok(())
    }

    async fn list_services_with_label(&self, ns: &str, key: &str, value: &str) -> Result<Vec<Service>> {
        let map = self.services.read().await;
        let list = map
            .get(ns)
            .map(|m| {
                m.values()
                    .filter(|s| s.metadata.labels.as_ref().and_then(|l| l.get(key)).map(|v| v == value).unwrap_or(false))
                    .cloned()
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        Ok(list)
    }

    async fn create_network_policy(&self, ns: &str, np: &NetworkPolicy) -> Result<NetworkPolicy> {
        let name = np.metadata.name.clone().unwrap_or_default();
        if name.is_empty() { return Err(anyhow!("NetworkPolicy missing metadata.name")); }
        let mut map = self.network_policies.write().await;
        map.entry(key(ns)).or_default().insert(name.clone(), np.clone());
        Ok(np.clone())
    }

    async fn create_pvc(&self, ns: &str, pvc: &PersistentVolumeClaim) -> Result<PersistentVolumeClaim> {
        let name = pvc.metadata.name.clone().unwrap_or_default();
        if name.is_empty() { return Err(anyhow!("PVC missing metadata.name")); }
        let mut map = self.pvcs.write().await;
        map.entry(key(ns)).or_default().insert(name.clone(), pvc.clone());
        Ok(pvc.clone())
    }

    async fn create_secret(&self, ns: &str, secret: &Secret) -> Result<Secret> {
        let name = secret.metadata.name.clone().unwrap_or_default();
        if name.is_empty() { return Err(anyhow!("Secret missing metadata.name")); }
        let mut map = self.secrets.write().await;
        map.entry(key(ns)).or_default().insert(name.clone(), secret.clone());
        Ok(secret.clone())
    }

    async fn create_job(&self, ns: &str, job: &Job) -> Result<Job> {
        let name = job.metadata.name.clone().unwrap_or_default();
        if name.is_empty() { return Err(anyhow!("Job missing metadata.name")); }
        let mut map = self.jobs.write().await;
        map.entry(key(ns)).or_default().insert(name.clone(), job.clone());
        Ok(job.clone())
    }

    async fn get_job(&self, ns: &str, name: &str) -> Result<Job> {
        let map = self.jobs.read().await;
        map.get(ns)
            .and_then(|m| m.get(name))
            .cloned()
            .ok_or_else(|| anyhow!("Job not found: {}/{}", ns, name))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;

    fn pod_with_labels(name: &str, labels: &[(&str, &str)]) -> Pod {
        let mut meta = ObjectMeta { name: Some(name.to_string()), ..Default::default() };
        if !labels.is_empty() {
            meta.labels = Some(labels.iter().map(|(k,v)| (k.to_string(), v.to_string())).collect());
        }
        Pod { metadata: meta, ..Default::default() }
    }

    #[tokio::test]
    async fn mock_client_crd_and_core_crud() {
        let client = MockK8sClient::default();

        // CRDs
        let bj = BasilicaJob::new("job1", crate::crd::basilica_job::BasilicaJobSpec {
            image: "img".into(), command: vec![], args: vec![], env: vec![],
            resources: crate::crd::basilica_job::Resources { cpu: "1".into(), memory: "512Mi".into(), gpus: crate::crd::basilica_job::GpuSpec { count: 0, model: vec![] } },
            storage: None, ttl_seconds: 0, priority: "normal".into(),
        });
        client.create_basilica_job("ns", &bj).await.unwrap();
        let bj_get = client.get_basilica_job("ns", "job1").await.unwrap();
        assert_eq!(bj_get.name_any(), "job1");
        client.delete_basilica_job("ns", "job1").await.unwrap();
        assert!(client.get_basilica_job("ns", "job1").await.is_err());

        let gr = GpuRental::new("rent1", crate::crd::gpu_rental::GpuRentalSpec {
            container: crate::crd::gpu_rental::RentalContainer {
                image: "img".into(), env: vec![], command: vec![], ports: vec![], volumes: vec![],
                resources: crate::crd::gpu_rental::Resources { cpu: "1".into(), memory: "1024Mi".into(), gpus: crate::crd::gpu_rental::GpuSpec { count: 1, model: vec!["A100".into()] } },
            },
            duration: crate::crd::gpu_rental::RentalDuration { hours: 24, auto_extend: false, max_extensions: 0 },
            access_type: crate::crd::gpu_rental::AccessType::Ssh,
            network: Default::default(), storage: None, ssh: None, jupyter_access: None, environment: None, miner_selector: None, billing: None,
            ttl_seconds: 0, tenancy: None,
        });
        client.create_gpu_rental("ns", &gr).await.unwrap();
        let gr_get = client.get_gpu_rental("ns", "rent1").await.unwrap();
        assert_eq!(gr_get.name_any(), "rent1");
        client.delete_gpu_rental("ns", "rent1").await.unwrap();
        assert!(client.get_gpu_rental("ns", "rent1").await.is_err());

        // Core
        let p1 = pod_with_labels("p1", &[("a","1"),("b","2")]);
        let p2 = pod_with_labels("p2", &[("a","1"),("b","3")]);
        client.create_pod("ns", &p1).await.unwrap();
        client.create_pod("ns", &p2).await.unwrap();
        let got = client.get_pod("ns", "p1").await.unwrap();
        assert_eq!(got.name_any(), "p1");
        let list = client.list_pods_with_label("ns", "b", "2").await.unwrap();
        assert_eq!(list.len(), 1);
        assert_eq!(list[0].name_any(), "p1");
        client.delete_pod("ns", "p2").await.unwrap();
        assert!(client.get_pod("ns", "p2").await.is_err());
    }
}
