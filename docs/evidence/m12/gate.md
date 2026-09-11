# M12 Gate — Local Release Candidate

Run date: 2026-09-11. This milestone adds no feature. It is the freeze point:
confirm every gate still holds, publish the evidence matrix, tag.

## Full gate sweep, run immediately before freezing

```
$ ruff check .                          PASS
$ mypy app                              PASS
$ pytest                                9 passed
$ helm lint helm/ml-platform helm/platform-local --values .../values-local.yaml
                                         2 chart(s) linted, 0 failed
$ helm template ... --values values-aws-dev.yaml    PASS
$ helm template ... --values values-local.yaml      PASS
$ terraform fmt -check -recursive infra/terraform   PASS
$ tflint --recursive --chdir=infra/terraform        PASS (0 issues)
$ terraform validate (aws-dev)                      Success! The configuration is valid.
$ ./scripts/security-scan.sh
    kubeconform: 7+13 resources, Valid, 0 Invalid
    trivy (fixable HIGH/CRITICAL, all 3 images): 0
    trivy (secrets, all 3 images): clean
```

Every check this project has ever added is green at the freeze point — none
of it was left broken and unnoticed for a later milestone to trip over.

## The acceptance sequence the roadmap asked for

> fresh clone → fresh kind → bootstrap → train → track → store → serve →
> observe → scale → break → recover → destroy

| Step | Where it's proven |
| --- | --- |
| fresh clone → fresh kind → bootstrap | [M11](../m11/gate.md) — cluster, images, build cache destroyed first, `make local-up` from the repo alone |
| train → track → store | [M7](../m7/gate.md) — training Job → MLflow → PostgreSQL (metadata) + MinIO (artifact), in-cluster |
| serve | [M7](../m7/gate.md), [M1](../m1/gate.md) — inference pulls the artifact, `/predict` 200 |
| observe | [M5](../m5/gate.md), [M10](../m10/gate.md) — 12-panel dashboard, every query verified against live data; 5 alert rules loaded with `health: ok` |
| scale | [M10](../m10/gate.md) — HPA 2→6→2 measured under a real k6 load, 0 failures across 645,809 requests |
| break | [M5](../m5/gate.md) 6 drills, [M9](../m9/gate.md) NetworkPolicy deny edges, [M10](../m10/gate.md) node drain |
| recover | [M2](../m2/pod-recovery.md), [M8](../m8/gate.md) backup/restore, [M10](../m10/gate.md) drain recovery |
| destroy | `make local-down` — proven as the teardown half of the M11 cycle |

Every link above is a distinct evidence file with real command output, not a
restatement of this table.

## Evidence matrix

Published in the [README](../../../README.md#evidence-matrix) — thirteen
capability rows, each linking to the milestone gate that measured it. Not
duplicated here to avoid the two copies drifting apart.

## What "frozen" means from here

- No new local features land under an M0–M12 label. A defect found in the
  frozen surface gets fixed and noted as a patch to the milestone it belongs
  to, not folded into new scope.
- AWS work starts a new phase (M13+) with its own gates — it consumes this
  local platform's contract (the local→AWS mapping frozen in
  [M6](../../aws-architecture.md)) rather than re-litigating it.
- The tag `local-v1.0.0` marks the exact commit this file describes.

## Gate result

| Check | Result |
| --- | --- |
| every existing quality/security/IaC gate green at freeze time | PASS |
| full acceptance sequence (clone→...→destroy) evidenced | PASS |
| evidence matrix published | PASS ([README](../../../README.md)) |
| tag `local-v1.0.0` created | PASS |
