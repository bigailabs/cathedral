use basilica_operator::crd::{
    basilica_job::BasilicaJob, basilica_node_profile::BasilicaNodeProfile,
    basilica_queue::BasilicaQueue, gpu_rental::GpuRental, user_deployment::UserDeployment,
};
use kube::core::CustomResourceExt;

fn main() {
    let docs = [
        serde_yaml::to_string(&BasilicaJob::crd()).expect("serialize BasilicaJob CRD"),
        serde_yaml::to_string(&GpuRental::crd()).expect("serialize GpuRental CRD"),
        serde_yaml::to_string(&BasilicaNodeProfile::crd()).expect("serialize BasilicaNodeProfile CRD"),
        serde_yaml::to_string(&BasilicaQueue::crd()).expect("serialize BasilicaQueue CRD"),
        serde_yaml::to_string(&UserDeployment::crd()).expect("serialize UserDeployment CRD"),
    ];
    println!("{}", docs.join("\n---\n"));
}
