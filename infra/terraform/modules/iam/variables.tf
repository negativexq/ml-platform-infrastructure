variable "name_prefix" {
  description = "Prefix for role names."
  type        = string
}

variable "create_github_oidc_provider" {
  description = "Create the GitHub OIDC provider. False if the account already has one — it is account-global."
  type        = bool
  default     = true
}

variable "github_subject_claims" {
  description = "Allowed GitHub OIDC subject claims, e.g. repo:owner/name:ref:refs/heads/main. Never leave this as a wildcard."
  type        = list(string)

  validation {
    condition     = alltrue([for claim in var.github_subject_claims : startswith(claim, "repo:")])
    error_message = "Every subject claim must start with 'repo:' so the role is bound to a specific repository."
  }
}

variable "ecr_repository_arn" {
  description = "ECR repository the CI role may push to."
  type        = string
}

variable "artifact_bucket_arn" {
  description = "S3 bucket holding model artifacts."
  type        = string
}

variable "eks_oidc_provider_arn" {
  description = "EKS OIDC provider ARN, for IRSA trust policies."
  type        = string
}

variable "eks_oidc_provider_url" {
  description = "EKS OIDC issuer URL without scheme."
  type        = string
}

variable "workload_namespace" {
  description = "Kubernetes namespace the workloads run in."
  type        = string
  default     = "ml-platform"
}

variable "inference_service_account" {
  description = "ServiceAccount name the inference pods use. Must match the Helm chart's fullname."
  type        = string
  default     = "ml-platform-inference"
}

variable "mlflow_service_account" {
  description = "ServiceAccount name the MLflow tracking server uses."
  type        = string
  default     = "mlflow"
}

variable "rds_master_secret_arn" {
  description = "Secrets Manager ARN of the RDS master password. Null omits the permission."
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
