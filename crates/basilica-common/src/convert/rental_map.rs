use serde::{Deserialize, Serialize};

use crate::{
    compute::{GpuSpec, Resources},
    rental::{
        AccessType, RentalContainer, RentalDuration, RentalEnvironment, RentalNetwork, RentalPort,
        RentalSpec, VolumeMount,
    },
};

/// A DTO that mirrors the validator's ContainerSpec without creating a crate dependency.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ValidatorContainerSpecDto {
    pub image: String,
    pub environment: Vec<(String, String)>,
    pub ports: Vec<ValidatorPortMappingDto>,
    pub resources: ValidatorResourceRequirementsDto,
    pub entrypoint: Vec<String>,
    pub command: Vec<String>,
    pub volumes: Vec<ValidatorVolumeMountDto>,
    pub labels: Vec<(String, String)>,
    pub capabilities: Vec<String>,
    pub network: ValidatorNetworkConfigDto,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ValidatorPortMappingDto {
    pub container_port: u32,
    pub host_port: u32,
    pub protocol: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ValidatorResourceRequirementsDto {
    pub cpu_cores: f64,
    pub memory_mb: i64,
    pub storage_mb: i64,
    pub gpu_count: u32,
    pub gpu_types: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ValidatorVolumeMountDto {
    pub host_path: String,
    pub container_path: String,
    pub read_only: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ValidatorNetworkConfigDto {
    pub mode: String,
    pub dns: Vec<String>,
    pub extra_hosts: Vec<(String, String)>,
}

/// Map a validator-style ContainerSpec DTO to a RentalSpec skeleton.
/// The caller is expected to provide additional RentalSpec fields (duration, access/network/billing),
/// or use defaults provided here.
pub fn map_validator_container_to_rental(
    node_region_hint: Option<String>,
    dto: &ValidatorContainerSpecDto,
) -> RentalSpec {
    // Convert resources to k8s-style strings
    let cpu = if dto.resources.cpu_cores > 0.0 {
        // Use raw core count as integer or decimal string
        if (dto.resources.cpu_cores - dto.resources.cpu_cores.trunc()).abs() < f64::EPSILON {
            format!("{}", dto.resources.cpu_cores as u32)
        } else {
            format!("{:.3}", dto.resources.cpu_cores)
        }
    } else {
        "1".into()
    };
    let memory = if dto.resources.memory_mb > 0 {
        format!("{}Mi", dto.resources.memory_mb)
    } else {
        "1024Mi".into()
    };

    let container = RentalContainer {
        image: dto.image.clone(),
        env: dto.environment.clone(),
        command: if !dto.entrypoint.is_empty() {
            // Join entrypoint and command for simplicity; operator can split if needed
            let mut v = dto.entrypoint.clone();
            v.extend(dto.command.clone());
            v
        } else {
            dto.command.clone()
        },
        ports: dto
            .ports
            .iter()
            .map(|p| RentalPort {
                container_port: p.container_port as u16,
                protocol: p.protocol.clone(),
            })
            .collect(),
        volumes: dto
            .volumes
            .iter()
            .map(|v| VolumeMount {
                host_path: Some(v.host_path.clone()),
                container_path: v.container_path.clone(),
                read_only: v.read_only,
            })
            .collect(),
        resources: Resources {
            cpu,
            memory,
            gpus: GpuSpec {
                count: dto.resources.gpu_count,
                model: dto.resources.gpu_types.clone(),
            },
        },
    };

    // Default network policies derived from validator dto.network
    // Host networking is not allowed by default in K8s mode; translate to exposure rules instead.
    let network = RentalNetwork {
        ingress: vec![],
        egress_policy: "restricted".into(),
        allowed_egress: vec![],
        public_ip_required: false,
        bandwidth_mbps: None,
    };

    RentalSpec {
        container,
        duration: RentalDuration {
            hours: 24,
            auto_extend: false,
            max_extensions: 0,
        },
        access_type: AccessType::Ssh,
        network,
        storage: None,
        ssh: None,
        jupyter: None,
        environment: Some(RentalEnvironment {
            base_image: None,
            pre_install_script: None,
            environment_variables: vec![],
        }),
        miner_selector: node_region_hint.map(|region| crate::rental::MinerSelector {
            id: None,
            region: Some(region),
            tier: None,
        }),
        billing: None,
        ttl_seconds: 0,
        tenancy: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn map_from_validator_container_dto() {
        let dto = ValidatorContainerSpecDto {
            image: "image:tag".into(),
            environment: vec![("K".into(), "V".into())],
            ports: vec![ValidatorPortMappingDto {
                container_port: 7860,
                host_port: 17860,
                protocol: "TCP".into(),
            }],
            resources: ValidatorResourceRequirementsDto {
                cpu_cores: 4.0,
                memory_mb: 16384,
                storage_mb: 0,
                gpu_count: 1,
                gpu_types: vec!["A100".into()],
            },
            entrypoint: vec!["bash".into(), "-lc".into()],
            command: vec!["python".into(), "main.py".into()],
            volumes: vec![ValidatorVolumeMountDto {
                host_path: "/var/scratch".into(),
                container_path: "/data".into(),
                read_only: false,
            }],
            labels: vec![("a".into(), "b".into())],
            capabilities: vec!["SYS_PTRACE".into()],
            network: ValidatorNetworkConfigDto {
                mode: "bridge".into(),
                dns: vec![],
                extra_hosts: vec![],
            },
        };

        let spec = map_validator_container_to_rental(Some("us-east-1".into()), &dto);
        assert_eq!(spec.container.image, "image:tag");
        assert_eq!(spec.container.env.len(), 1);
        assert_eq!(
            spec.container.command,
            vec!["bash", "-lc", "python", "main.py"]
        );
        assert_eq!(spec.container.ports.len(), 1);
        assert_eq!(spec.container.resources.cpu, "4");
        assert_eq!(spec.container.resources.memory, "16384Mi");
        assert_eq!(spec.container.resources.gpus.count, 1);
        assert_eq!(spec.container.resources.gpus.model, vec!["A100"]);
        assert!(
            spec.miner_selector
                .as_ref()
                .unwrap()
                .region
                .as_ref()
                .unwrap()
                == "us-east-1"
        );
        // No default SSH unless requested
        assert!(spec.ssh.is_none());
    }

    #[test]
    fn fractional_cpu_is_formatted() {
        let mut dto = ValidatorContainerSpecDto {
            image: "image".into(),
            environment: vec![],
            ports: vec![],
            resources: ValidatorResourceRequirementsDto {
                cpu_cores: 1.5,
                memory_mb: 2048,
                storage_mb: 0,
                gpu_count: 0,
                gpu_types: vec![],
            },
            entrypoint: vec![],
            command: vec![],
            volumes: vec![],
            labels: vec![],
            capabilities: vec![],
            network: ValidatorNetworkConfigDto {
                mode: "bridge".into(),
                dns: vec![],
                extra_hosts: vec![],
            },
        };
        let spec = map_validator_container_to_rental(None, &dto);
        assert_eq!(spec.container.resources.cpu, "1.500");
        assert_eq!(spec.container.resources.memory, "2048Mi");

        dto.resources.cpu_cores = 0.0;
        let spec2 = map_validator_container_to_rental(None, &dto);
        assert_eq!(spec2.container.resources.cpu, "1");
    }
}
