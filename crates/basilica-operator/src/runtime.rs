use std::sync::Arc;
use std::time::Duration;

use anyhow::Result as AnyResult;
use futures::StreamExt;
use kube::{Api, Client, ResourceExt};
use kube_runtime::controller::{Action, Controller};
use tracing::{error, info, warn};

use crate::billing::{BillingClient, MockBillingClient, HttpBillingClient};
use crate::controllers::job_controller::JobController;
use crate::controllers::rental_controller::RentalController;
use crate::crd::basilica_job::BasilicaJob;
use crate::crd::gpu_rental::GpuRental;
use crate::k8s_client::{K8sClient, KubeClient};

#[derive(Clone)]
struct JobCtx<C: K8sClient + Clone + 'static> {
    ctrl: JobController<C>,
}

#[derive(Clone)]
struct RentalCtx<C: K8sClient + Clone + 'static> {
    ctrl: RentalController<C>,
}

#[derive(Debug)]
struct ReconcileError(anyhow::Error);

impl std::fmt::Display for ReconcileError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for ReconcileError {}

async fn reconcile_job<C: K8sClient + Clone + 'static>(obj: Arc<BasilicaJob>, ctx: Arc<JobCtx<C>>) -> std::result::Result<Action, ReconcileError> {
    let ns = obj.namespace().unwrap_or_else(|| "default".into());
    if let Err(e) = ctx.ctrl.reconcile(&ns, &obj).await { return Err(ReconcileError(e)); }
    Ok(Action::requeue(Duration::from_secs(30)))
}

fn error_policy_job<C: K8sClient + Clone + 'static>(_obj: Arc<BasilicaJob>, err: &ReconcileError, _ctx: Arc<JobCtx<C>>) -> Action {
    warn!("job reconcile error: {}", err);
    Action::requeue(Duration::from_secs(10))
}

async fn reconcile_rental<C: K8sClient + Clone + 'static>(
    obj: Arc<GpuRental>,
    ctx: Arc<RentalCtx<C>>,
) -> std::result::Result<Action, ReconcileError> {
    let ns = obj.namespace().unwrap_or_else(|| "default".into());
    if let Err(e) = ctx.ctrl.reconcile(&ns, &obj).await { return Err(ReconcileError(e)); }
    Ok(Action::requeue(Duration::from_secs(30)))
}

fn error_policy_rental<C: K8sClient + Clone + 'static>(
    _obj: Arc<GpuRental>,
    err: &ReconcileError,
    _ctx: Arc<RentalCtx<C>>,
) -> Action {
    warn!("rental reconcile error: {}", err);
    Action::requeue(Duration::from_secs(10))
}

pub async fn run() -> AnyResult<()> {
    let client = Client::try_default().await?;
    let kube_client = KubeClient { client: client.clone() };

    // Choose billing client based on env var BASILICA_BILLING_URL
    let billing_arc: std::sync::Arc<dyn BillingClient + Send + Sync> = match std::env::var("BASILICA_BILLING_URL") {
        Ok(url) if !url.is_empty() => std::sync::Arc::new(HttpBillingClient::new(url)),
        _ => std::sync::Arc::new(MockBillingClient::default()),
    };
    // Build controllers
    let job_ctrl = JobController::new_with_billing(kube_client.clone(), billing_arc.clone());
    let rent_ctrl = RentalController::new_with_arc(kube_client.clone(), billing_arc);

    // Set up APIs
    let bj_api: Api<BasilicaJob> = Api::all(client.clone());
    let gr_api: Api<GpuRental> = Api::all(client.clone());

    // Run controllers as streams
    let jobs = Controller::new(bj_api, Default::default())
        .run(reconcile_job, error_policy_job, Arc::new(JobCtx { ctrl: job_ctrl }))
        .for_each(|res| async move {
            match res {
                Ok(_o) => {}
                Err(e) => error!("job controller stream error: {}", e),
            }
        });

    let rentals = Controller::new(gr_api, Default::default())
        .run(reconcile_rental, error_policy_rental, Arc::new(RentalCtx { ctrl: rent_ctrl }))
        .for_each(|res| async move {
            match res {
                Ok(_o) => {}
                Err(e) => error!("rental controller stream error: {}", e),
            }
        });

    info!("operator controllers started");
    futures::future::join(jobs, rentals).await;
    Ok(())
}
