locals {
  name = "${var.project}-${var.environment}"

  # Applied to every resource through the provider's default_tags. Without a
  # consistent tag set, Cost Explorer cannot attribute a single dollar of this
  # environment's spend.
  tags = {
    Project     = var.project
    Environment = var.environment
    Owner       = var.owner
    ManagedBy   = "terraform"
    Repository  = "ml-platform-infrastructure"
  }
}

module "vpc" {
  source = "../../modules/vpc"

  name         = local.name
  cidr_block   = var.vpc_cidr
  azs          = var.azs
  region       = var.region
  cluster_name = local.name
  tags         = local.tags
}

module "ecr" {
  source = "../../modules/ecr"

  name = "${var.project}-inference"
  tags = local.tags
}

module "s3" {
  source = "../../modules/s3"

  bucket_name = var.artifact_bucket_name
  # Ephemeral lab: `terraform destroy` must actually work.
  force_destroy = true
  tags          = local.tags
}

module "eks" {
  source = "../../modules/eks"

  cluster_name       = local.name
  kubernetes_version = var.kubernetes_version

  private_subnet_ids  = module.vpc.private_subnet_ids
  public_subnet_ids   = module.vpc.public_subnet_ids
  public_access_cidrs = var.public_access_cidrs

  node_instance_types = var.node_instance_types
  node_capacity_type  = var.node_capacity_type
  node_desired_size   = var.node_desired_size
  node_max_size       = var.node_max_size

  tags = local.tags
}

module "rds" {
  source = "../../modules/rds"

  name               = local.name
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids

  # Only the cluster's own security group may reach PostgreSQL — not the VPC
  # CIDR, and certainly not the internet.
  allowed_security_group_ids = [module.eks.cluster_security_group_id]

  tags = local.tags
}

module "iam" {
  source = "../../modules/iam"

  name_prefix = local.name

  create_github_oidc_provider = var.create_github_oidc_provider
  github_subject_claims       = var.github_subject_claims

  ecr_repository_arn  = module.ecr.repository_arn
  artifact_bucket_arn = module.s3.bucket_arn

  eks_oidc_provider_arn = module.eks.oidc_provider_arn
  eks_oidc_provider_url = module.eks.oidc_provider_url

  rds_master_secret_arn = module.rds.master_user_secret_arn

  tags = local.tags
}

# A budget is created with the infrastructure, not after the first surprise
# bill. Alerts fire on forecast, so there is time to react.
resource "aws_budgets_budget" "monthly" {
  count = length(var.budget_alert_emails) > 0 ? 1 : 0

  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:Project$${var.project}"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = var.budget_alert_emails
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = var.budget_alert_emails
  }
}
