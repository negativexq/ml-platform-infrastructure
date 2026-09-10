terraform {
  # Pinned, not floating: an unpinned Terraform or provider version means the
  # same code can produce a different plan tomorrow.
  required_version = ">= 1.9, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }

  # State stays local until there is a second operator. An S3 + DynamoDB
  # backend is the next step, and is deliberately not configured while this
  # environment has never been applied.
  #
  # backend "s3" {
  #   bucket       = "<state-bucket>"
  #   key          = "aws-dev/terraform.tfstate"
  #   region       = "eu-central-1"
  #   use_lockfile = true
  #   encrypt      = true
  # }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.tags
  }
}
