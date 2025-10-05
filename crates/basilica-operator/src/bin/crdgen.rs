use basilica_operator::crd::{
    basilica_job::BasilicaJob, basilica_node_profile::BasilicaNodeProfile, gpu_rental::GpuRental,
};
use kube::core::CustomResourceExt;

fn main() {
    let mut docs = Vec::new();
    let bj = BasilicaJob::crd();
    let gr = GpuRental::crd();
    let np = BasilicaNodeProfile::crd();
    docs.push(serde_yaml::to_string(&bj).expect("serialize BasilicaJob CRD"));
    docs.push(serde_yaml::to_string(&gr).expect("serialize GpuRental CRD"));
    docs.push(serde_yaml::to_string(&np).expect("serialize BasilicaNodeProfile CRD"));
    println!("{}\n---\n{}\n---\n{}", docs[0], docs[1], docs[2]);
}
