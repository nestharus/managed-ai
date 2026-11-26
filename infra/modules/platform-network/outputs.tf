output "vpc_id" {
  description = "Vultr private VPC identifier."
  value       = vultr_vpc.private.id
}

output "firewall_group_id" {
  description = "Vultr firewall group identifier enforcing gateway-only ingress."
  value       = vultr_firewall_group.gateway_only.id
}
