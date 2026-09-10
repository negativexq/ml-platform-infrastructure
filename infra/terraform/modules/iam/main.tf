terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

# Four distinct identities, no shared admin role:
#
#   1. GitHub CI        — pushes images to ECR. No cluster access, no S3.
#   2. Inference pods   — read artifacts from S3. No write, no ECR, no RDS.
#   3. MLflow server    — read/write artifacts in S3, read the RDS secret.
#   4. Node instances   — defined in the EKS module; deliberately has no S3.
#
# Each is assumed through a different mechanism (OIDC federation for CI,
# IRSA for pods), so compromising one does not yield the others.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  github_oidc_provider_arn = var.create_github_oidc_provider ? one(aws_iam_openid_connect_provider.github[*].arn) : "arn:${local.partition}:iam::${local.account_id}:oidc-provider/token.actions.githubusercontent.com"
}

# -- 1. GitHub Actions CI identity -------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # GitHub's OIDC certificate chain is verified by AWS; the thumbprint list is
  # retained for API compatibility.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]

  tags = var.tags
}

resource "aws_iam_role" "github_ci" {
  name        = "${var.name_prefix}-github-ci"
  description = "Pushes container images to ECR from GitHub Actions"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = local.github_oidc_provider_arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        # Scoped to one repository and one ref. Without this condition any
        # GitHub repository in the world could assume the role.
        StringLike = {
          "token.actions.githubusercontent.com:sub" = var.github_subject_claims
        }
      }
    }]
  })

  tags = var.tags
}

data "aws_iam_policy_document" "github_ci" {
  statement {
    sid       = "ECRAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # This action does not support resource scoping.
  }

  statement {
    sid    = "ECRPush"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [var.ecr_repository_arn]
  }
}

resource "aws_iam_role_policy" "github_ci" {
  name   = "ecr-push"
  role   = aws_iam_role.github_ci.id
  policy = data.aws_iam_policy_document.github_ci.json
}

# -- 2. Inference workload identity (IRSA) -----------------------------------

data "aws_iam_policy_document" "inference_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.eks_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.eks_oidc_provider_url}:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Bound to one namespace and one ServiceAccount — the same
    # ml-platform/ml-platform-inference identity the Helm chart creates.
    condition {
      test     = "StringEquals"
      variable = "${var.eks_oidc_provider_url}:sub"
      values   = ["system:serviceaccount:${var.workload_namespace}:${var.inference_service_account}"]
    }
  }
}

resource "aws_iam_role" "inference" {
  name               = "${var.name_prefix}-inference"
  description        = "Read-only access to model artifacts for inference pods"
  assume_role_policy = data.aws_iam_policy_document.inference_assume.json

  tags = var.tags
}

data "aws_iam_policy_document" "inference" {
  statement {
    sid       = "ListArtifactBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [var.artifact_bucket_arn]
  }

  # Read only. A serving pod has no reason to write to the artifact store, and
  # denying it means a compromised pod cannot poison the model registry.
  statement {
    sid       = "ReadArtifacts"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${var.artifact_bucket_arn}/*"]
  }
}

resource "aws_iam_role_policy" "inference" {
  name   = "read-artifacts"
  role   = aws_iam_role.inference.id
  policy = data.aws_iam_policy_document.inference.json
}

# -- 3. MLflow tracking server identity (IRSA) -------------------------------

data "aws_iam_policy_document" "mlflow_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.eks_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.eks_oidc_provider_url}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.eks_oidc_provider_url}:sub"
      values   = ["system:serviceaccount:${var.workload_namespace}:${var.mlflow_service_account}"]
    }
  }
}

resource "aws_iam_role" "mlflow" {
  name               = "${var.name_prefix}-mlflow"
  description        = "Read/write model artifacts and read the RDS master secret"
  assume_role_policy = data.aws_iam_policy_document.mlflow_assume.json

  tags = var.tags
}

data "aws_iam_policy_document" "mlflow" {
  statement {
    sid       = "ListArtifactBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [var.artifact_bucket_arn]
  }

  statement {
    sid    = "WriteArtifacts"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
    ]
    resources = ["${var.artifact_bucket_arn}/*"]
  }

  # The database password is never templated into a manifest; MLflow reads it
  # from Secrets Manager at start-up using this permission.
  dynamic "statement" {
    for_each = var.rds_master_secret_arn == null ? [] : [var.rds_master_secret_arn]

    content {
      sid       = "ReadDatabaseCredentials"
      effect    = "Allow"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [statement.value]
    }
  }
}

resource "aws_iam_role_policy" "mlflow" {
  name   = "artifacts-and-credentials"
  role   = aws_iam_role.mlflow.id
  policy = data.aws_iam_policy_document.mlflow.json
}
