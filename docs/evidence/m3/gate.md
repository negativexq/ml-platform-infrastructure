# M3 Gate — evidence

Run date: 2026-09-10 · chart `helm/ml-platform` 0.1.0 · release `inference` in
namespace `ml-platform` on the kind cluster from M2.

## Lint and template

```
$ make helm-lint
==> Linting helm/ml-platform
[INFO] Chart.yaml: icon is recommended
1 chart(s) linted, 0 chart(s) failed

$ helm template inference helm/ml-platform --values helm/ml-platform/values-local.yaml
rendered OK: 5 objects
kind: ServiceAccount
kind: Secret
kind: ConfigMap
kind: Service
kind: Deployment
```

## Install → healthy → predict works

```
$ make helm-deploy
platform host: 172.21.0.1
Release "inference" has been upgraded. Happy Helming!
STATUS: deployed

$ make smoke
Smoke test against http://localhost:30080 (deployment: ml-platform-inference)
  PASS  GET /health                        200
  PASS  GET /ready                         200
  PASS  POST /predict (valid)              200
  PASS  POST /predict (invalid)            422
  PASS  ready replicas                     2
  PASS  ready Service endpoints            2
smoke test PASSED
```

## Upgrade

Image `dev` → `v2` **and** replicas 2 → 3 in one upgrade:

```
$ ./scripts/helm-deploy.sh --set image.tag=v2 --set replicaCount=3
Release "inference" has been upgraded. Happy Helming!
STATUS: deployed
  image    : ml-platform-inference:v2
  replicas : 3

image=ml-platform-inference:v2  replicas=3  ready=3
  PASS  ready replicas             3
  PASS  ready Service endpoints    3
smoke test PASSED
```

## Rollback

```
$ helm -n ml-platform rollback inference 2 --wait
Rollback was a success! Happy Helming!

REVISION  STATUS      DESCRIPTION
2         superseded  Upgrade complete
3         superseded  Upgrade complete
4         deployed    Rollback to 2

image=ml-platform-inference:dev  replicas=2  ready=2
smoke test PASSED
```

Both the image tag and the replica count returned to the previous stable
state — the rollback restored the whole release, not just one field.

## One chart, many environments

The same chart rendered for a hypothetical AWS environment, with no template
edits — only values:

```
$ helm template inference helm/ml-platform \
    --set environment=aws-dev \
    --set image.repository=123456789012.dkr.ecr.eu-central-1.amazonaws.com/ml-platform-inference \
    --set image.tag=sha-abc1234 \
    --set service.type=ClusterIP --set service.nodePort=null \
    --set artifactStore.mode=irsa \
    --set 'serviceAccount.annotations.eks\.amazonaws\.com/role-arn=arn:aws:iam::123456789012:role/ml-platform-inference' \
    --set mlflow.trackingUri=http://mlflow.ml-platform.svc.cluster.local:5000

objects rendered: 4
kind: ServiceAccount
kind: ConfigMap
kind: Service
kind: Deployment

Secret present? 0 (expected 0 in irsa mode)
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/ml-platform-inference
  type: ClusterIP
  ML_MLFLOW_TRACKING_URI: "http://mlflow.ml-platform.svc.cluster.local:5000"
  image: "123456789012.dkr.ecr.eu-central-1.amazonaws.com/ml-platform-inference:sha-abc1234"
```

`artifactStore.mode` switches the credential strategy structurally: `local`
renders a Secret from values (lab only), `existing` references a Secret managed
outside the chart, `irsa` renders **no Secret at all** and drops the
`envFrom.secretRef` — the identity comes from the ServiceAccount annotation.
That is the M6 migration path already expressed in the chart, and CI asserts it.

## What the chart deliberately does not do

- No separate dev/prod chart — one chart, `values-<env>.yaml`.
- No hardcoded image tag, endpoint, or credential in a template.
- The one machine-specific value (the kind bridge gateway) is discovered at
  deploy time by [`scripts/helm-deploy.sh`](../../../scripts/helm-deploy.sh)
  and passed with `--set`, replacing M2's `sed` placeholder substitution.

## Gate result

| Check | Result |
| --- | --- |
| `helm lint` | PASS |
| `helm template` | PASS (5 objects local, 4 in irsa mode) |
| `helm upgrade --install` → healthy → predict works | PASS |
| new image/config → `helm upgrade` works | PASS (image + replicas together) |
| `helm rollback` → previous stable state restored | PASS |
| no hardcoded env/image/credential in templates | PASS (asserted in CI) |
