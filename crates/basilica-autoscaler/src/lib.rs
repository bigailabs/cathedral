pub mod api;
pub mod config;
pub mod controllers;
pub mod crd;
pub mod error;
pub mod health;
pub mod leader_election;
pub mod metrics;
pub mod offering_matcher;
pub mod provisioner;
pub mod runtime;

pub use config::AutoscalerConfig;
pub use error::{AutoscalerError, Result};

// Re-export commonly used types
pub use controllers::{AutoscalerK8sClient, KubeClient};
pub use offering_matcher::{
    GpuRequirements, MaybeOfferingSelector, OfferingConstraints, OfferingMatcher,
    OfferingMatcherConfig, OfferingSelector, PendingGpuPod,
};
pub use provisioner::{NodeProvisioner, SshProvisioner};
