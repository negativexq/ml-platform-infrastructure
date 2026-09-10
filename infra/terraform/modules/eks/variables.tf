variable "cluster_name" {
  description = "EKS cluster name."
  type        = string
}

variable "kubernetes_version" {
  description = "Kubernetes minor version."
  type        = string
  default     = "1.33"
}

variable "private_subnet_ids" {
  description = "Private subnets for nodes."
  type        = list(string)
}

variable "public_subnet_ids" {
  description = "Public subnets for internet-facing load balancers."
  type        = list(string)
}

variable "public_access_cidrs" {
  description = "CIDRs allowed to reach the public API endpoint. Narrow this before using the cluster for anything real."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "enabled_cluster_log_types" {
  description = "Control-plane logs to ship to CloudWatch. Each type costs ingestion; audit is the one worth paying for."
  type        = list(string)
  default     = ["api", "audit"]
}

variable "node_instance_types" {
  description = "Node instance types. Graviton (t4g/m7g) is materially cheaper per vCPU."
  type        = list(string)
  default     = ["t3a.large"]
}

variable "node_capacity_type" {
  description = "ON_DEMAND or SPOT. SPOT is far cheaper and fine for a lab that can lose a node."
  type        = string
  default     = "SPOT"

  validation {
    condition     = contains(["ON_DEMAND", "SPOT"], var.node_capacity_type)
    error_message = "node_capacity_type must be ON_DEMAND or SPOT."
  }
}

variable "node_disk_size" {
  description = "Node root volume size in GB."
  type        = number
  default     = 30
}

variable "node_desired_size" {
  description = "Initial node count."
  type        = number
  default     = 2
}

variable "node_min_size" {
  description = "Minimum node count."
  type        = number
  default     = 1
}

variable "node_max_size" {
  description = "Maximum node count. A ceiling is a cost control, not just a scaling limit."
  type        = number
  default     = 3
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
