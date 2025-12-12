use basilica_autoscaler::crd::{NodePool, ScalingPolicy};
use kube::core::CustomResourceExt;

fn main() {
    let docs = [
        serde_yaml::to_string(&NodePool::crd()).expect("serialize NodePool CRD"),
        serde_yaml::to_string(&ScalingPolicy::crd()).expect("serialize ScalingPolicy CRD"),
    ];
    println!("{}", docs.join("\n---\n"));
}
