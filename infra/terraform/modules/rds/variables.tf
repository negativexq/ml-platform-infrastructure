variable "name" {
  description = "Name prefix and DB identifier."
  type        = string
}

variable "vpc_id" {
  description = "VPC to place the security group in."
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnets for the DB subnet group."
  type        = list(string)
}

variable "allowed_security_group_ids" {
  description = "Security groups permitted to reach PostgreSQL. Empty means nothing can."
  type        = list(string)
  default     = []
}

variable "engine_version" {
  description = "PostgreSQL major version."
  type        = string
  default     = "16"
}

variable "instance_class" {
  description = "Instance class. db.t4g.micro is the cheapest usable option and is enough for MLflow metadata."
  type        = string
  default     = "db.t4g.micro"
}

variable "allocated_storage" {
  description = "Initial storage in GB."
  type        = number
  default     = 20
}

variable "max_allocated_storage" {
  description = "Storage autoscaling ceiling in GB."
  type        = number
  default     = 50
}

variable "database_name" {
  description = "Initial database name."
  type        = string
  default     = "mlflow"
}

variable "master_username" {
  description = "Master username. The password is managed by AWS Secrets Manager, not by Terraform input."
  type        = string
  default     = "mlflow"
}

variable "multi_az" {
  description = "Multi-AZ doubles the bill; off for a lab."
  type        = bool
  default     = false
}

variable "backup_retention_period" {
  description = "Days of automated backups. 0 disables them."
  type        = number
  default     = 1
}

variable "deletion_protection" {
  description = "Block accidental deletion. False for ephemeral labs so destroy works."
  type        = bool
  default     = false
}

variable "skip_final_snapshot" {
  description = "Skip the final snapshot on destroy. True for ephemeral labs."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
