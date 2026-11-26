variable "channel" {
  description = "Deployment channel identifier (for example dev)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.channel)
    error_message = "channel must be one of dev, staging, or prod."
  }
}

variable "cluster_name" {
  description = "Logical VM cluster label prefix."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.cluster_name))
    error_message = "cluster_name must be a valid DNS label."
  }
}

variable "region" {
  description = "Vultr region code."
  type        = string
  default     = "ewr"
}

variable "vm_plan" {
  description = "Vultr Cloud Compute instance plan."
  type        = string
  default     = "vc2-2c-4gb"
}

variable "os_id" {
  description = "Vultr OS image ID (pinned numeric ID)."
  type        = number
  default     = 2284
}

variable "node_count" {
  description = "Number of worker VMs to provision."
  type        = number
  default     = 1

  validation {
    condition     = var.node_count >= 1 && var.node_count <= 9
    error_message = "node_count must be between 1 and 9."
  }
}

variable "vpc_id" {
  description = "Vultr private VPC ID created by platform-network."
  type        = string
}

variable "firewall_group_id" {
  description = "Vultr firewall group ID enforcing gateway-only ingress."
  type        = string
}

variable "ssh_key_ids" {
  description = "Caller-supplied Vultr SSH key IDs (never hardcoded in repository)."
  type        = list(string)
  default     = []
}
