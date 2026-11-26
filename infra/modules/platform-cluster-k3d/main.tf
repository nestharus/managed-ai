terraform {
  required_version = ">= 1.8.0, < 2.0.0"

  required_providers {
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2.3"
    }
  }
}

locals {
  k3s_image = "rancher/k3s:${var.k3s_version}"
  cluster_args = join(" ", [
    "k3d cluster create ${var.cluster_name}",
    "--image ${local.k3s_image}",
    "--registry-create ${var.registry_name}:${var.registry_port}",
    "--port 127.0.0.1:${var.gateway_port}:8080@loadbalancer",
    "--kubeconfig-update-default=false",
    "--wait",
  ])
}

resource "null_resource" "k3d_cluster" {
  triggers = {
    cluster_name  = var.cluster_name
    k3s_version   = var.k3s_version
    registry_name = var.registry_name
    registry_port = tostring(var.registry_port)
    gateway_port  = tostring(var.gateway_port)
    kubeconfig    = var.kubeconfig_path
  }

  provisioner "local-exec" {
    command = "echo 'Planned k3d bootstrap: ${local.cluster_args}'"
  }
}
