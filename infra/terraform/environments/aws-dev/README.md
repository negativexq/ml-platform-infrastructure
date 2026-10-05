# Historical AWS infrastructure lab

This environment provisions the earlier EKS/MLflow/RDS/S3/ECR infrastructure. It is not a
complete AWS deployment of the current API/gateway/reconciler platform. The environment
has not been applied here. See [security hardening and pending AWS work](../../../../docs/security-hardening.md).

`public_access_cidrs` is required and must contain valid non-empty ranges narrower than
`/0`. Replace the documentation-only address in `terraform.tfvars.example` with actual
trusted office/VPN ranges.

`database_client_security_group_ids` is required. Supply existing dedicated MLflow pod
security groups and configure AWS VPC CNI `SecurityGroupPolicy` for those workloads. The
shared EKS/node security group is rejected by a lifecycle precondition. Supplying a SG ID
alone does not attach it to pods or prove packet isolation. A production AWS phase must
provision and verify this policy and all current control-plane dependencies.
