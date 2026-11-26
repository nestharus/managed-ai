output "instance_ids" {
  description = "Vultr instance IDs for the channel nodes."
  value       = vultr_instance.node[*].id
}

output "internal_ips" {
  description = "Private VPC IPv4 addresses (marked sensitive; never exposed directly)."
  value       = vultr_instance.node[*].internal_ip
  sensitive   = true
}
