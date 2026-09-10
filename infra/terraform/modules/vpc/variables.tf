variable "name" {
  description = "Name prefix for VPC resources."
  type        = string
}

variable "cidr_block" {
  description = "VPC CIDR block."
  type        = string
  default     = "10.20.0.0/16"
}

variable "azs" {
  description = "Availability zones to spread subnets across. Two is enough for a lab; EKS requires at least two."
  type        = list(string)

  validation {
    condition     = length(var.azs) >= 2
    error_message = "EKS requires subnets in at least two availability zones."
  }
}

variable "region" {
  description = "AWS region, used for the S3 gateway endpoint service name."
  type        = string
}

variable "cluster_name" {
  description = "EKS cluster name, used for subnet discovery tags."
  type        = string
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
