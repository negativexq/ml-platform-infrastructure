# F2 — Invalid model artifact

**Question:** a model version that does not exist is committed to `main`. Does
it reach users?

## Method

`model.uri: "models:/ml-platform-model/999"` committed to `values-local.yaml`
and pushed; Argo CD applied it. Requests fired at the Service throughout.

M2 ran this same drill against raw manifests
([../m2/readiness-failure.md](../m2/readiness-failure.md)); this run adds the
metric view and drives the change through GitOps.

## Observed

```
t+0s    availableReplicas=2  service/predict=200
t+9s    availableReplicas=2  service/predict=200
...
t+118s  availableReplicas=2  service/predict=200

ml-platform-inference-765d5dd7f5-689cs   0/1   Running   0   2m5s    <- new, never ready
ml-platform-inference-86bf967968-9mbrr   1/1   Running   0   7m56s
ml-platform-inference-86bf967968-sk6x9   1/1   Running   0   9m58s
```

```
model_ready
  ml-platform-inference-765d5dd7f5-689cs   0
  ml-platform-inference-86bf967968-9mbrr   1
  ml-platform-inference-86bf967968-sk6x9   1

model_load_failures_total
  ml-platform-inference-765d5dd7f5-689cs   6
  ml-platform-inference-86bf967968-9mbrr   3
  ml-platform-inference-86bf967968-sk6x9   0

kube_pod_container_status_restarts_total
  all pods                                 0
```

The failing pod's `/ready` carries the reason verbatim:

```
{"ready":false,"state":"failed","model_version":null,
 "detail":"RESOURCE_DOES_NOT_EXIST: Model Version
           (name=ml-platform-model, version=999) not found"}
```

## Reading the numbers carefully

`model_load_failures_total=3` on a **serving** pod (`9mbrr`) looks alarming
until you check when: that pod is the one that came up during the F3 MinIO
outage earlier in the session and retried three times before succeeding. The
counter is cumulative for the pod's lifetime, so a non-zero value does not mean
"currently broken" — `model_ready` does. Alerting on the counter alone would
page for an incident that already resolved itself.

## Recovery

```
$ git revert --no-edit HEAD && git push
restored: models:/ml-platform-model/1  argo=Healthy
smoke test PASSED
```

| | |
| --- | --- |
| Detection | readiness 503 with the MLflow error inline; `model_ready=0` |
| Containment | pod excluded from endpoints; `maxUnavailable: 0` stalls the rollout; 0 restarts |
| Recovery | Git revert → Argo sync → smoke green |
| Evidence | poll output and metric dumps above |
