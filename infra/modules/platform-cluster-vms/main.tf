terraform {
  required_version = ">= 1.8.0, < 2.0.0"

  required_providers {
    vultr = {
      source  = "vultr/vultr"
      version = "~> 2.23.1"
    }
  }
}

resource "vultr_instance" "node" {
  count = var.node_count

  label             = "${var.cluster_name}-node-${count.index + 1}"
  hostname          = "${var.cluster_name}-node-${count.index + 1}"
  region            = var.region
  plan              = var.vm_plan
  os_id             = var.os_id
  vpc_ids           = [var.vpc_id]
  firewall_group_id = var.firewall_group_id
  ssh_key_ids       = var.ssh_key_ids
  enable_ipv6       = false
  backups           = "disabled"
  tags              = ["managed-ai", "channel:${var.channel}"]
}
