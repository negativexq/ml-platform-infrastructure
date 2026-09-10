variable "region" {
  description = "AWS region."
  type        = string
  default     = "eu-central-1"
}

variable "environment" {
  description = "Environment name; becomes part of every resource name and tag."
  type        = string
  default     = "aws-dev"
}

variable "project" {
  description = "Project name used for naming and cost allocation."
  type        = string
  default     = "ml-platform"
}

variable "owner" {
  description = "Owner tag. Cost reports are only useful if spend has a name on it."
  type        = string
}

variable "azs" {
  description = "Availability zones. Two keeps EKS happy without paying for a third."
  type        = list(string)
  default     = ["eu-central-1a", "eu-central-1b"]
}

variable "vpc_cidr" {
  description = "VPC CIDR block."
  type        = string
  default     = "10.20.0.0/16"
}

variable "artifact_bucket_name" {
  description = "S3 bucket for MLflow artifacts. Must be globally unique."
  type        = string
}

variable "github_subject_claims" {
  description = "GitHub OIDC subject claims allowed to assume the CI role."
  type        = list(string)
  default     = ["repo:negativexq/ml-platform-infrastructure:ref:refs/heads/main"]
}

variable "create_github_oidc_provider" {
  description = "False if this AWS account already has the GitHub OIDC provider."
  type        = bool
  default     = true
}

variable "kubernetes_version" {
  description = "EKS Kubernetes version."
  type        = string
  default     = "1.33"
}

variable "node_instance_types" {
  description = "Node instance types."
  type        = list(string)
  default     = ["t3a.large"]
}

variable "node_capacity_type" {
  description = "SPOT keeps the lab affordable; the workload tolerates node loss."
  type        = string
  default     = "SPOT"
}

variable "node_desired_size" {
  description = "Initial node count."
  type        = number
  default     = 2
}

variable "node_max_size" {
  description = "Node ceiling — a spend limit as much as a scaling limit."
  type        = number
  default     = 3
}

variable "public_access_cidrs" {
  description = "CIDRs allowed to reach the EKS public API endpoint. Narrow to an office or home IP before real use."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "monthly_budget_usd" {
  description = "Budget threshold that triggers alerts before the bill surprises anyone."
  type        = number
  default     = 150
}

variable "budget_alert_emails" {
  description = "Addresses notified when forecast spend crosses the budget."
  type        = list(string)
  default     = []
}
