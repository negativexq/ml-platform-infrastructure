output "region" {
  description = "AWS region."
  value       = var.region
}

output "cluster_name" {
  description = "EKS cluster name; use with `aws eks update-kubeconfig`."
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "Kubernetes API endpoint."
  value       = module.eks.cluster_endpoint
}

output "ecr_repository_url" {
  description = "Set as image.repository in the Helm values for this environment."
  value       = module.ecr.repository_url
}

output "artifact_bucket_name" {
  description = "MLflow default artifact root (s3://<this>/)."
  value       = module.s3.bucket_name
}

output "rds_endpoint" {
  description = "PostgreSQL endpoint for the MLflow backend store."
  value       = module.rds.endpoint
}

output "rds_master_secret_arn" {
  description = "Secrets Manager ARN holding the database password."
  value       = module.rds.master_user_secret_arn
}

output "github_ci_role_arn" {
  description = "Configure this as the role-to-assume in the GitHub Actions workflow."
  value       = module.iam.github_ci_role_arn
}

output "inference_role_arn" {
  description = "Annotate the inference ServiceAccount with this (eks.amazonaws.com/role-arn)."
  value       = module.iam.inference_role_arn
}

output "mlflow_role_arn" {
  description = "Annotate the MLflow ServiceAccount with this."
  value       = module.iam.mlflow_role_arn
}

output "helm_values_hint" {
  description = "The values this environment implies for the Helm chart."
  value = {
    "image.repository"                                          = module.ecr.repository_url
    "artifactStore.mode"                                        = "irsa"
    "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn" = module.iam.inference_role_arn
    "service.type"                                              = "ClusterIP"
    "mlflow.s3EndpointUrl"                                      = ""
  }
}
