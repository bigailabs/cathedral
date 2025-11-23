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
