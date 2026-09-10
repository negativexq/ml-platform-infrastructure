# AWS architecture (M6 design)

Nothing here has been applied. This document and
[`infra/terraform/`](../infra/terraform/) describe the AWS counterpart of the
platform that M0–M5 built and measured locally. The point of writing it before
spending anything is that every decision below can be checked against a local
behaviour that was actually observed.

## Local → AWS mapping (frozen contract)

| Local (M0–M5) | AWS | Terraform module |
| --- | --- | --- |
| kind cluster | EKS | [`modules/eks`](../infra/terraform/modules/eks/) |
| Docker image on the host | ECR | [`modules/ecr`](../infra/terraform/modules/ecr/) |
| MinIO | S3 | [`modules/s3`](../infra/terraform/modules/s3/) |
| PostgreSQL container | RDS PostgreSQL | [`modules/rds`](../infra/terraform/modules/rds/) |
| NodePort Service on `localhost:30080` | ClusterIP + ALB | Helm `service.type` |
| `Secret` with MinIO credentials | IAM role via IRSA | [`modules/iam`](../infra/terraform/modules/iam/) |
| `values-local.yaml` | `values-aws-dev.yaml` | same chart |
| Argo `Application` → local cluster | Argo `Application` → EKS | same manifest shape |

The Helm chart already supports the right-hand column: `artifactStore.mode=irsa`
renders no Secret and drops `envFrom.secretRef`, and the ServiceAccount takes
an `eks.amazonaws.com/role-arn` annotation. That was verified in
[M3's gate](evidence/m3/gate.md) and is asserted in CI, so the migration does
not require touching a template.

## Why each service

**EKS, not ECS or plain EC2.** M2–M5 depend on Kubernetes primitives that were
individually load-bearing, not incidental: readiness gating traffic separately
from liveness ([M2](evidence/m2/readiness-failure.md)), `maxUnavailable: 0`
holding capacity during a bad rollout ([M5](evidence/m5/bad-rollout.md)), and
Argo CD reconciling live resources. Moving to a different orchestrator would
discard the behaviour this project spent five milestones proving.

**S3, not EFS or a volume.** Artifacts are write-once, read-many, and read at
pod start-up. Measured cold-start load from MinIO was 3.98s and 5.26s
([M5](evidence/m5/gate.md)); S3 with a gateway VPC endpoint is the same access
pattern with less to operate.

**RDS, not PostgreSQL on a node.** MLflow metadata is the one piece of state
whose loss cannot be reconstructed from Git or S3. It is small — the local
database held a handful of runs — so `db.t4g.micro` is genuinely sufficient,
and what is being bought is managed backups, not throughput.

**ECR, not Docker Hub.** Tag immutability. GitOps rolls forward by committing a
new tag; if a tag could be repointed, the chain from commit to running bytes
would break silently. `image_tag_mutability = "IMMUTABLE"` enforces it.

## Identity boundaries

Four separate identities. No shared admin role.

```
GitHub Actions ──OIDC──> ml-platform-aws-dev-github-ci
                          └─ ecr:PutImage on ONE repository
                          └─ no cluster access, no S3

inference pod ──IRSA──> ml-platform-aws-dev-inference
                          └─ s3:GetObject on the artifact bucket (READ ONLY)
                          └─ no write, no ECR, no RDS, no secrets

MLflow server ──IRSA──> ml-platform-aws-dev-mlflow
                          └─ s3:GetObject/PutObject/DeleteObject on the bucket
                          └─ secretsmanager:GetSecretValue on the RDS secret

EKS nodes ──instance profile──> ml-platform-aws-dev-node
                          └─ the three managed EKS policies, and nothing else
                          └─ deliberately NO S3 access
```

Three properties worth stating explicitly, because each is a decision rather
than a default:

1. **The inference role is read-only on S3.** A serving pod has no reason to
   write to the artifact store. A compromised pod cannot poison the model
   registry.
2. **The node role has no S3 access.** If workload permissions rode on the node
   instance profile — the easy way — then every pod on that node would inherit
   them, and the separation above would be decorative.
3. **The CI role is bound to one repository and one ref** via the OIDC subject
   claim, and a variable validation rejects any claim that does not start with
   `repo:`. Without that condition, any GitHub repository could assume the role.

The database password is never a Terraform input. RDS generates it with
`manage_master_user_password` and stores it in Secrets Manager; MLflow reads it
at start-up with the one permission granted for that ARN. No password reaches
tfvars, state as plaintext input, or a Kubernetes manifest.

## Network boundaries

```
                    Internet
                        │
                   ┌────┴─────┐
                   │   IGW    │
                   └────┬─────┘
     ┌──────────────────┴──────────────────┐
     │  public subnets (2 AZ)              │   ALB, NAT gateway
     └──────────────────┬──────────────────┘
                        │ NAT (one, not one per AZ)
     ┌──────────────────┴──────────────────┐
     │  private subnets (2 AZ)             │   EKS nodes, RDS
     └──────────────────┬──────────────────┘
                        │
                 S3 gateway endpoint  ──> S3 (never traverses NAT)
```

- **RDS is not publicly accessible**, and its security group takes ingress from
  *named security groups only* — the EKS cluster security group — never from a
  CIDR and never from the VPC range. Being inside the VPC is not sufficient.
- **The S3 gateway endpoint** keeps artifact traffic on the AWS network. It is
  both a cost decision (NAT charges per GB, and model artifacts are the
  bulkiest traffic this platform moves) and a security one.
- **The EKS public API endpoint defaults to `0.0.0.0/0`** so `kubectl` works
  from a laptop on day one. This is the weakest setting in the design and is
  called out in the variable description and the tfvars example; narrow
  `public_access_cidrs` before the cluster holds anything real.

## Cost posture

See [cost-model.md](cost-model.md). The short version: two AZs not three, one
NAT gateway not two, SPOT nodes, `db.t4g.micro`, no Multi-AZ, no GPU, a node
`max_size` ceiling, and an AWS Budget created alongside the infrastructure
rather than after the first surprising bill.

## Known limitations

- **Never applied.** `fmt`, `validate` and `tflint` pass; `terraform plan` has
  not run, because that needs AWS credentials. Plan-time and apply-time errors
  that validation cannot catch are still ahead.
- **Local state.** The S3 backend is written and commented out. Fine for one
  operator, wrong for two.
- **No ALB / Ingress controller.** The chart renders a ClusterIP Service for
  AWS; putting an ALB in front needs the AWS Load Balancer Controller, which
  is not in the Terraform.
- **No MLflow deployment for AWS.** RDS and S3 are provisioned and the MLflow
  IRSA role exists, but the tracking server itself still runs in Docker
  Compose locally. Moving it into the cluster is not part of M6.
- **Single NAT gateway is a single point of failure.** Losing that AZ takes
  outbound connectivity with it. Accepted deliberately for a lab; not
  acceptable for production.
- **`create_github_oidc_provider` defaults to true**, which fails if the
  account already has one — it is account-global. The tfvars example says so.
