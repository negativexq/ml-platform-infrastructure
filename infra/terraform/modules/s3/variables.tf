variable "bucket_name" {
  description = "Bucket name. Must be globally unique."
  type        = string
}

variable "force_destroy" {
  description = "Allow terraform destroy to delete a non-empty bucket. True for ephemeral lab environments only."
  type        = bool
  default     = true
}

variable "noncurrent_version_expiration_days" {
  description = "How long superseded object versions are retained."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
