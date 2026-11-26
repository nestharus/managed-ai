output "cluster_name" {
  description = "Name of the local k3d cluster."
  value       = var.cluster_name
}

output "kubeconfig_path" {
  description = "Dedicated kubeconfig path for local channel operations."
  value       = var.kubeconfig_path
}

output "registry_endpoint" {
  description = "Ephemeral local registry host:port."
  value       = "${var.registry_name}:${var.registry_port}"
}

output "control_surface_url" {
  description = "Loopback-only local control surface endpoint."
  value       = "http://127.0.0.1:${var.gateway_port}"
}
