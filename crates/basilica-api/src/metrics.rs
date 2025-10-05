use std::sync::Once;
use std::time::Instant;

use metrics::{counter, describe_counter, describe_histogram, histogram};

static INIT: Once = Once::new();

fn ensure_init() {
    INIT.call_once(|| {
        describe_counter!(
            "basilica_api_http_requests_total",
            "HTTP requests processed by API routes"
        );
        describe_histogram!(
            "basilica_api_http_request_duration_seconds",
            "Duration of API requests"
        );
        describe_counter!("basilica_api_jobs_created_total", "Jobs created via API");
        describe_counter!(
            "basilica_api_rentals_created_total",
            "Rentals created via API"
        );
    });
}

pub fn record_request(route: &str, method: &str, start: Instant, ok: bool) {
    ensure_init();
    let secs = start.elapsed().as_secs_f64();
    histogram!("basilica_api_http_request_duration_seconds", "route" => route.to_string(), "method" => method.to_string()).record(secs);
    counter!("basilica_api_http_requests_total", "route" => route.to_string(), "method" => method.to_string(), "outcome" => if ok {"ok"} else {"error"}.to_string()).increment(1);
}

pub fn record_job_created(namespace: &str) {
    ensure_init();
    counter!("basilica_api_jobs_created_total", "namespace" => namespace.to_string()).increment(1);
}

pub fn record_rental_created(namespace: &str) {
    ensure_init();
    counter!("basilica_api_rentals_created_total", "namespace" => namespace.to_string())
        .increment(1);
}
