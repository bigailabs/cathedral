use std::sync::Once;
use std::time::Instant;

use metrics::{counter, describe_counter, describe_gauge, describe_histogram, gauge, histogram};

static INIT: Once = Once::new();

fn ensure_init() {
    INIT.call_once(|| {
        // Jobs
        describe_counter!(
            "basilica_operator_jobs_reconciles_total",
            "Total job reconciles"
        );
        describe_counter!(
            "basilica_operator_jobs_created_total",
            "Jobs created by controller"
        );
        describe_counter!(
            "basilica_operator_jobs_status_transitions_total",
            "Job status transitions"
        );
        describe_counter!("basilica_operator_jobs_succeeded_total", "Jobs succeeded");
        describe_counter!("basilica_operator_jobs_failed_total", "Jobs failed");
        describe_histogram!(
            "basilica_operator_job_reconcile_duration_seconds",
            "Job reconcile duration in seconds"
        );
        describe_gauge!(
            "basilica_operator_active_jobs_total",
            "Active running jobs per namespace"
        );

        // Rentals
        describe_counter!(
            "basilica_operator_rentals_reconciles_total",
            "Total rental reconciles"
        );
        describe_counter!(
            "basilica_operator_rentals_created_total",
            "Rental pods created by controller"
        );
        describe_counter!(
            "basilica_operator_rentals_status_transitions_total",
            "Rental status transitions"
        );
        describe_counter!(
            "basilica_operator_rentals_extension_approved_total",
            "Auto-extension approvals"
        );
        describe_counter!(
            "basilica_operator_rentals_extension_denied_total",
            "Auto-extension denials"
        );
        describe_counter!(
            "basilica_operator_rentals_netpol_applied_total",
            "NetworkPolicy applied for rental egress mode"
        );
        describe_counter!(
            "basilica_operator_rentals_terminated_total",
            "Rentals terminated (by reason)"
        );
        describe_histogram!(
            "basilica_operator_rental_reconcile_duration_seconds",
            "Rental reconcile duration in seconds"
        );
        describe_gauge!(
            "basilica_operator_active_rentals_total",
            "Active rentals per namespace"
        );
        describe_histogram!(
            "basilica_operator_rental_active_duration_seconds",
            "Rental active duration in seconds"
        );

        // UserDeployments
        describe_counter!(
            "basilica_operator_deployments_reconciles_total",
            "Total deployment reconciles"
        );
        describe_counter!(
            "basilica_operator_deployments_created_total",
            "Deployments created by controller"
        );
        describe_histogram!(
            "basilica_operator_deployment_phase_duration_seconds",
            "Time spent in each deployment phase"
        );
        describe_gauge!(
            "basilica_operator_deployments_by_phase",
            "Number of deployments in each phase"
        );
        describe_counter!(
            "basilica_operator_deployment_phase_transitions_total",
            "Total deployment phase transitions"
        );
        describe_counter!(
            "basilica_operator_storage_sync_bytes_total",
            "Total bytes synced for storage mounts"
        );
        describe_histogram!(
            "basilica_operator_deployment_reconcile_duration_seconds",
            "Deployment reconcile duration in seconds"
        );

        // Node Profile Controller
        describe_counter!(
            "basilica_operator_nodes_validated_total",
            "Total nodes validated by datacenter"
        );
        describe_counter!(
            "basilica_operator_nfd_conversions_total",
            "Total NFD label conversions performed"
        );
    });
}

pub fn record_job_reconcile(
    ns: &str,
    name: &str,
    created: bool,
    from: &str,
    to: &str,
    start: Instant,
) {
    ensure_init();
    counter!("basilica_operator_jobs_reconciles_total", "namespace" => ns.to_string()).increment(1);
    if created {
        counter!("basilica_operator_jobs_created_total", "namespace" => ns.to_string())
            .increment(1);
    }
    if from != to {
        counter!(
            "basilica_operator_jobs_status_transitions_total",
            "namespace" => ns.to_string(),
            "job" => name.to_string(),
            "from" => from.to_string(),
            "to" => to.to_string()
        )
        .increment(1);
    }
    let secs = start.elapsed().as_secs_f64();
    histogram!("basilica_operator_job_reconcile_duration_seconds", "namespace" => ns.to_string())
        .record(secs);
}

pub fn record_rental_netpol(mode: &str, ns: &str) {
    ensure_init();
    counter!("basilica_operator_rentals_netpol_applied_total", "namespace" => ns.to_string(), "mode" => mode.to_string()).increment(1);
}

pub fn record_rental_reconcile(
    ns: &str,
    name: &str,
    created: bool,
    from: &str,
    to: &str,
    start: Instant,
) {
    ensure_init();
    counter!("basilica_operator_rentals_reconciles_total", "namespace" => ns.to_string())
        .increment(1);
    if created {
        counter!("basilica_operator_rentals_created_total", "namespace" => ns.to_string())
            .increment(1);
    }
    if from != to {
        counter!(
            "basilica_operator_rentals_status_transitions_total",
            "namespace" => ns.to_string(),
            "rental" => name.to_string(),
            "from" => from.to_string(),
            "to" => to.to_string()
        )
        .increment(1);
    }
    let secs = start.elapsed().as_secs_f64();
    histogram!("basilica_operator_rental_reconcile_duration_seconds", "namespace" => ns.to_string()).record(secs);
}

pub fn record_rental_extension(ns: &str, approved: bool) {
    ensure_init();
    if approved {
        counter!("basilica_operator_rentals_extension_approved_total", "namespace" => ns.to_string()).increment(1);
    } else {
        counter!("basilica_operator_rentals_extension_denied_total", "namespace" => ns.to_string())
            .increment(1);
    }
}

pub fn record_job_active_change(ns: &str, prev_active: bool, new_active: bool) {
    ensure_init();
    match (prev_active, new_active) {
        (false, true) => {
            gauge!("basilica_operator_active_jobs_total", "namespace" => ns.to_string())
                .increment(1.0)
        }
        (true, false) => {
            gauge!("basilica_operator_active_jobs_total", "namespace" => ns.to_string())
                .decrement(1.0)
        }
        _ => {}
    }
}

pub fn record_rental_active_change(ns: &str, prev_active: bool, new_active: bool) {
    ensure_init();
    match (prev_active, new_active) {
        (false, true) => {
            gauge!("basilica_operator_active_rentals_total", "namespace" => ns.to_string())
                .increment(1.0)
        }
        (true, false) => {
            gauge!("basilica_operator_active_rentals_total", "namespace" => ns.to_string())
                .decrement(1.0)
        }
        _ => {}
    }
}

pub fn record_job_outcome(ns: &str, phase: &str) {
    ensure_init();
    match phase {
        "Succeeded" => {
            counter!("basilica_operator_jobs_succeeded_total", "namespace" => ns.to_string())
                .increment(1)
        }
        "Failed" => counter!("basilica_operator_jobs_failed_total", "namespace" => ns.to_string())
            .increment(1),
        _ => {}
    }
}

pub fn record_rental_termination(ns: &str, reason: &str) {
    ensure_init();
    counter!("basilica_operator_rentals_terminated_total", "namespace" => ns.to_string(), "reason" => reason.to_string()).increment(1);
}

pub fn record_rental_active_duration(ns: &str, seconds: f64) {
    ensure_init();
    histogram!("basilica_operator_rental_active_duration_seconds", "namespace" => ns.to_string())
        .record(seconds);
}

pub fn record_deployment_reconcile(ns: &str, created: bool, start: Instant) {
    ensure_init();
    counter!("basilica_operator_deployments_reconciles_total", "namespace" => ns.to_string())
        .increment(1);
    if created {
        counter!("basilica_operator_deployments_created_total", "namespace" => ns.to_string())
            .increment(1);
    }
    let secs = start.elapsed().as_secs_f64();
    histogram!(
        "basilica_operator_deployment_reconcile_duration_seconds",
        "namespace" => ns.to_string()
    )
    .record(secs);
}

pub fn record_deployment_phase_transition(
    namespace: &str,
    from_phase: &str,
    to_phase: &str,
    duration_secs: f64,
) {
    ensure_init();

    // Record duration of previous phase
    histogram!(
        "basilica_operator_deployment_phase_duration_seconds",
        "phase" => from_phase.to_string()
    )
    .record(duration_secs);

    // Decrement old phase count
    gauge!(
        "basilica_operator_deployments_by_phase",
        "namespace" => namespace.to_string(),
        "phase" => from_phase.to_string()
    )
    .decrement(1.0);

    // Increment new phase count
    gauge!(
        "basilica_operator_deployments_by_phase",
        "namespace" => namespace.to_string(),
        "phase" => to_phase.to_string()
    )
    .increment(1.0);

    // Count transition (no namespace label to prevent cardinality explosion)
    counter!(
        "basilica_operator_deployment_phase_transitions_total",
        "from" => from_phase.to_string(),
        "to" => to_phase.to_string()
    )
    .increment(1);
}

pub fn record_storage_sync_bytes(namespace: &str, bytes: u64) {
    ensure_init();
    counter!(
        "basilica_operator_storage_sync_bytes_total",
        "namespace" => namespace.to_string()
    )
    .increment(bytes);
}

pub fn record_deployment_new(namespace: &str, initial_phase: &str) {
    ensure_init();
    gauge!(
        "basilica_operator_deployments_by_phase",
        "namespace" => namespace.to_string(),
        "phase" => initial_phase.to_string()
    )
    .increment(1.0);
}

pub fn cleanup_deployment_metrics(namespace: &str, phase: &str) {
    ensure_init();
    gauge!(
        "basilica_operator_deployments_by_phase",
        "namespace" => namespace.to_string(),
        "phase" => phase.to_string()
    )
    .decrement(1.0);
}

// Node Profile Controller metrics

/// Record a successful node validation.
pub fn record_node_validation(node_name: &str, datacenter: &str) {
    ensure_init();
    counter!(
        "basilica_operator_nodes_validated_total",
        "datacenter" => datacenter.to_string()
    )
    .increment(1);
    tracing::debug!(node = %node_name, datacenter = %datacenter, "Node validation metric recorded");
}

/// Record NFD label conversion for a node.
pub fn record_node_nfd_conversion(node_name: &str) {
    ensure_init();
    counter!("basilica_operator_nfd_conversions_total").increment(1);
    tracing::debug!(node = %node_name, "NFD conversion metric recorded");
}
