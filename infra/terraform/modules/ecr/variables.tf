variable "name" {
  description = "ECR repository name."
  type        = string
}

variable "keep_last_images" {
  description = "How many images to retain. Storage is billed per GB-month."
  type        = number
  default     = 10
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
