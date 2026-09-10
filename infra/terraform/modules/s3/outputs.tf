output "bucket_name" {
  description = "Bucket name, used as the MLflow default artifact root."
  value       = aws_s3_bucket.artifacts.id
}

output "bucket_arn" {
  description = "Bucket ARN, for scoping the workload IAM policy."
  value       = aws_s3_bucket.artifacts.arn
}
