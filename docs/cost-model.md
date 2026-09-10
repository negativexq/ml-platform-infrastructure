# Cost model

This is a learning lab, not a product. The design target is "cheap enough to
tear down and rebuild without thinking about it", which shapes real
architectural choices — they are listed here rather than buried in Terraform
defaults.

## Estimates

**These are list-price estimates for `eu-central-1`, not measured spend.**
Nothing has been applied, so no bill exists to check them against. Prices
change; treat the ranking as more reliable than the absolute numbers.

| Component | Configuration | Est. USD/month |
| --- | --- | --- |
| EKS control plane | one cluster, flat fee | ~73 |
| NAT gateway | one, hourly + data processing | ~35 + traffic |
| EKS nodes | 2 × `t3a.large`, SPOT | ~25–35 |
| RDS PostgreSQL | `db.t4g.micro`, single-AZ, 20 GB gp3 | ~15 |
| S3 | artifacts, a few GB | ~1 |
| ECR | 10 images retained | ~1 |
| CloudWatch | `api` + `audit` control-plane logs | ~5–15 |
| **Total, running continuously** | | **~155–175** |

The EKS control plane and the NAT gateway together are roughly two thirds of
the bill, and neither scales down with usage. That is the single most important
fact in this document: **an idle cluster costs nearly as much as a busy one.**

## Which is why: ephemeral by default

The cluster is meant to be created for a session and destroyed after:

```bash
cd infra/terraform/environments/aws-dev
terraform apply     # ~15 min, mostly EKS
# ... work ...
terraform destroy   # everything, including the bucket
```

A day of use costs roughly $5–6. Leaving it up for a month costs ~$165 for the
same amount of learning. Every module is configured so `destroy` actually
works, which is a deliberate cost feature:

- `force_destroy = true` on the artifact bucket — otherwise destroy fails on a
  non-empty bucket and the operator walks away leaving it running.
- `deletion_protection = false` and `skip_final_snapshot = true` on RDS.
- No resource keeps a retention lock.

For anything that matters, all three of these should be inverted.

## Choices made for cost, and what each gives up

| Choice | Saves | Gives up |
| --- | --- | --- |
| 2 AZs, not 3 | ~$0 directly; smaller blast radius of mistakes | one less failure domain |
| **1 NAT gateway, not per-AZ** | ~$35/mo per gateway avoided | losing that AZ kills outbound traffic |
| **SPOT nodes** | ~60–70% off on-demand | nodes can vanish with 2 min notice |
| `db.t4g.micro`, single-AZ | ~$15/mo vs ~$60 for Multi-AZ `t4g.small` | no automatic failover |
| Backups retained 1 day | a few dollars | one day of recovery window |
| **No GPU node group** | a `g4dn.xlarge` alone is ~$380/mo | no GPU inference |
| `max_size = 3` | caps runaway autoscaling | hard ceiling on scale |
| S3 gateway endpoint | NAT data-processing charges on artifact pulls | nothing — it is free |
| Only `api` + `audit` logs | ~$20/mo vs all five log types | less control-plane forensics |

SPOT is worth singling out: it is safe here *because* [M2](evidence/m2/pod-recovery.md)
measured what happens when a node's pod disappears — replacement ready in 13s,
Service never non-200. The cheap option is defensible because the behaviour
under it was tested, not assumed.

## GPU

Disabled, and it should stay disabled until a workload needs it. The current
model is a `Ridge` regressor whose prediction latency is **2.78 ms p95 on
CPU** ([M5](evidence/m5/gate.md)). A GPU node would cost more than the entire
rest of the platform and make that number worse, not better, once transfer
overhead is counted.

When a real model arrives, GPU capacity belongs in a **separate node group
with a taint**, so CPU workloads cannot land on it and quietly burn the
expensive instances.

## Guardrails

**Tags.** Every resource carries `Project`, `Environment`, `Owner`, `ManagedBy`
and `Repository` through the provider's `default_tags`. Untagged spend is
unattributable spend; Cost Explorer cannot break down what has no labels.

**Budget before apply.** `aws_budgets_budget` is created with the
infrastructure, filtered to this project's tag, alerting at:

- **80% of budget, forecast** — early enough to act
- **100% of budget, actual** — the backstop

Set `budget_alert_emails` in `terraform.tfvars` before the first apply. Leave
it empty and no budget resource is created — which is the wrong default for
anyone with a real card attached, and is called out in the tfvars example.

**No automatic apply.** CI runs `fmt`, `validate` and `tflint` only. Nothing in
this repository can create billable infrastructure without a human running
`terraform apply` at a terminal.

## Before the first apply

- [ ] Set `owner` and `budget_alert_emails` in `terraform.tfvars`
- [ ] Set a globally unique `artifact_bucket_name`
- [ ] Narrow `public_access_cidrs` from `0.0.0.0/0`
- [ ] Confirm `create_github_oidc_provider` matches the account's actual state
- [ ] Know the destroy command before running the apply command
