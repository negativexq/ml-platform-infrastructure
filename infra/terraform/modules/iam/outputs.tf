output "github_ci_role_arn" {
  description = "Role ARN for the GitHub Actions image-push workflow."
  value       = aws_iam_role.github_ci.arn
}

output "inference_role_arn" {
  description = "Role ARN to annotate on the inference ServiceAccount (eks.amazonaws.com/role-arn)."
  value       = aws_iam_role.inference.arn
}

output "mlflow_role_arn" {
  description = "Role ARN to annotate on the MLflow ServiceAccount."
  value       = aws_iam_role.mlflow.arn
}
