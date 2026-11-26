variable "cluster_name" {
  description = "Local k3d cluster name."
  type        = string
  default     = "managed-ai-information-local"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.cluster_name))
    error_message = "cluster_name must be a valid DNS label."
  }
}

variable "k3s_version" {
  description = "Pinned k3s release tag (never latest)."
  type        = string
  default     = "v1.31.4-k3s1"

  validation {
    condition     = can(regex("^v[0-9]+\\.[0-9]+\\.[0-9]+-k3s[0-9]+$", var.k3s_version))
    error_message = "k3s_version must be an exact pinned k3s tag (for example v1.31.4-k3s1)."
  }
}

variable "registry_name" {
  description = "Ephemeral local OCI registry container name."
  type        = string
  default     = "managed-ai-information-local"
}

variable "registry_port" {
  description = "Loopback port for the ephemeral local registry."
  type        = number
  default     = 5000

  validation {
    condition     = var.registry_port >= 1024 && var.registry_port <= 65535
    error_message = "registry_port must be an unprivileged port between 1024 and 65535."
  }
}

variable "gateway_port" {
  description = "Loopback control surface port exposed on 127.0.0.1 only."
  type        = number
  default     = 8480

  validation {
    condition     = var.gateway_port >= 1024 && var.gateway_port <= 65535
    error_message = "gateway_port must be an unprivileged port between 1024 and 65535."
  }
}

variable "kubeconfig_path" {
  description = "Dedicated kubeconfig path for the local k3d cluster."
  type        = string
  default     = "~/.kube/managed-ai-information-local.yaml"
}
