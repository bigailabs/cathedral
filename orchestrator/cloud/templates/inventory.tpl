[k3s_server]
%{ for idx, ip in k3s_server_public_ips ~}
server${idx + 1} ansible_host=${ip} ansible_user=${ssh_user} ansible_ssh_private_key_file=${ssh_key_file} ansible_become=true server_private_ip=${k3s_server_private_ips[idx]}
%{ endfor ~}

[k3s_agents]
%{ for idx, ip in k3s_agent_public_ips ~}
agent${idx + 1} ansible_host=${ip} ansible_user=${ssh_user} ansible_ssh_private_key_file=${ssh_key_file} ansible_become=true agent_private_ip=${k3s_agent_private_ips[idx]}
%{ endfor ~}

[k3s_cluster:children]
k3s_server
k3s_agents

[k3s_cluster:vars]
deployment_public_ip=${deployment_public_ip}
deployment_public_port=8080
k3s_nlb_dns=${nlb_dns_name}
