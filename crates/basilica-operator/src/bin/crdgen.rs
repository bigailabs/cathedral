use basilica_operator::crd::{basilica_job::BasilicaJob, gpu_rental::GpuRental};
use kube::core::CustomResourceExt;

fn main() {
    let mut docs = Vec::new();
    let bj = BasilicaJob::crd();
    let gr = GpuRental::crd();
    docs.push(serde_yaml::to_string(&bj).expect("serialize BasilicaJob CRD"));
    docs.push(serde_yaml::to_string(&gr).expect("serialize GpuRental CRD"));
    println!("{}\n---\n{}", docs[0], docs[1]);
}

