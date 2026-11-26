variable "cluster_name" {
  description = "Logical cluster prefix for VPC and firewall naming."
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

variable "vpc_cidr" {
  description = "RFC 1918 private IPv4 CIDR block for internal cluster traffic."
  type        = string
  default     = "10.42.0.0/24"

  validation {
    condition     = can(cidrnetmask(var.vpc_cidr)) && can(regex("^(10\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.|192\\.168\\.)", var.vpc_cidr))
    error_message = "vpc_cidr must be a valid RFC 1918 private IPv4 CIDR block."
  }
}

variable "admin_cidrs" {
  description = "Explicitly authorized admin CIDR allowlist (empty by default; never 0.0.0.0/0)."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for cidr in var.admin_cidrs : can(cidrnetmask(cidr)) && cidr != "0.0.0.0/0"])
    error_message = "admin_cidrs must contain valid CIDRs and never 0.0.0.0/0."
  }
}

variable "internal_control_port" {
  description = "Internal worker control port reachable only inside the private VPC."
  type        = number
  default     = 8081

  validation {
    condition     = var.internal_control_port >= 1024 && var.internal_control_port <= 65535
    error_message = "internal_control_port must be between 1024 and 65535."
  }
}
