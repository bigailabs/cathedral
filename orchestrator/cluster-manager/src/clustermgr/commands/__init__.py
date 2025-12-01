"""CLI commands for clustermgr."""

from clustermgr.commands.audit_pods import audit_pods
from clustermgr.commands.cert_check import cert_check
from clustermgr.commands.cleanup import cleanup
from clustermgr.commands.deployments import deployments
from clustermgr.commands.diagnose import diagnose
from clustermgr.commands.envoy import envoy
from clustermgr.commands.events import events
from clustermgr.commands.firewall import firewall
from clustermgr.commands.fix import fix
from clustermgr.commands.flannel import flannel
from clustermgr.commands.fuse_troubleshoot import fuse_troubleshoot
from clustermgr.commands.gateway import gateway
from clustermgr.commands.health import health
from clustermgr.commands.latency_matrix import latency_matrix
from clustermgr.commands.logs import logs
from clustermgr.commands.mesh_test import mesh_test
from clustermgr.commands.mtu import mtu
from clustermgr.commands.namespace import namespace
from clustermgr.commands.netpol import netpol
from clustermgr.commands.node_pressure import node_pressure
from clustermgr.commands.pod_troubleshoot import pod_troubleshoot
from clustermgr.commands.resources import resources
from clustermgr.commands.topology import topology
from clustermgr.commands.ud import ud
from clustermgr.commands.wg import wg

__all__ = [
    "audit_pods",
    "cert_check",
    "cleanup",
    "deployments",
    "diagnose",
    "envoy",
    "events",
    "firewall",
    "fix",
    "flannel",
    "fuse_troubleshoot",
    "gateway",
    "health",
    "latency_matrix",
    "logs",
    "mesh_test",
    "mtu",
    "namespace",
    "netpol",
    "node_pressure",
    "pod_troubleshoot",
    "resources",
    "topology",
    "ud",
    "wg",
]
