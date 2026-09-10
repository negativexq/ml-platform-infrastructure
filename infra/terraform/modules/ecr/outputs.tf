output "repository_url" {
  description = "Repository URL, used as image.repository in the Helm values."
  value       = aws_ecr_repository.this.repository_url
}

output "repository_arn" {
  description = "Repository ARN, for scoping CI push permissions."
  value       = aws_ecr_repository.this.arn
}
