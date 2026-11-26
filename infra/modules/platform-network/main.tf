terraform {
  required_version = ">= 1.8.0, < 2.0.0"

  required_providers {
    vultr = {
      source  = "vultr/vultr"
      version = "~> 2.23.1"
    }
  }
}

resource "vultr_vpc" "private" {
  description    = "${var.cluster_name}-private-vpc"
  region         = var.region
  v4_subnet      = split("/", var.vpc_cidr)[0]
  v4_subnet_mask = tonumber(split("/", var.vpc_cidr)[1])
}

resource "vultr_firewall_group" "gateway_only" {
  description = "${var.cluster_name}-gateway-only"
}

resource "vultr_firewall_rule" "admin_ssh" {
  for_each = toset(var.admin_cidrs)

  firewall_group_id = vultr_firewall_group.gateway_only.id
  protocol          = "tcp"
  ip_type           = "v4"
  subnet            = split("/", each.value)[0]
  subnet_size       = tonumber(split("/", each.value)[1])
  port              = "22"
  notes             = "Explicitly authorized admin CIDR SSH access"
}

resource "vultr_firewall_rule" "vpc_internal_control" {
  firewall_group_id = vultr_firewall_group.gateway_only.id
  protocol          = "tcp"
  ip_type           = "v4"
  subnet            = split("/", var.vpc_cidr)[0]
  subnet_size       = tonumber(split("/", var.vpc_cidr)[1])
  port              = tostring(var.internal_control_port)
  notes             = "Internal VPC gateway-to-worker traffic only"
}
