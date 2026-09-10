# M2 — Readiness failure keeps traffic away

**Question:** when a pod cannot load its model, is it kept out of the Service
without being restarted, and does the rollout stall instead of taking the
service down?

## Method

The ConfigMap was patched to a model version that does not exist
(`models:/ml-platform-model/999`) and the Deployment restarted. The Service was
polled for two minutes.

## Observed

Service stayed fully available for the entire window — 20 consecutive polls:

```
t+0s   readyReplicas=2  service/ready=200  service/predict=200
...
t+116s readyReplicas=2  service/ready=200  service/predict=200
```

Pods — the new one never became ready, the two old ones kept serving:

```
inference-7546c7d5b4-tv62w   1/1   Running   0   2m26s
inference-7546c7d5b4-vt8nh   1/1   Running   0   2m57s
inference-754c47fb6b-xxdwr   0/1   Running   0   2m2s     <- new, not ready
```

The EndpointSlice marks it not-ready, so kube-proxy does not route to it:

```
10.244.2.3  ready=true   pod=inference-7546c7d5b4-vt8nh
10.244.1.4  ready=true   pod=inference-7546c7d5b4-tv62w
10.244.2.4  ready=false  pod=inference-754c47fb6b-xxdwr
```

From inside the failing pod:

```
/health HTTP 200 {"status":"ok"}
/ready  HTTP 503 {"ready":false,"state":"failed","model_version":null,
                  "detail":"RESOURCE_DOES_NOT_EXIST: Model Version
                  (name=ml-platform-model, version=999) not found"}
```

**Restart count on the failing pod: `0`.** Liveness probes `/health`, which
does not depend on the artifact store, so Kubernetes did not kill a process
that was working correctly — it simply withheld traffic. This is the payoff of
the M1 fix in
[`../m1/blocking-startup-defect.md`](../m1/blocking-startup-defect.md); before
that change this pod would have CrashLoopBackOff'd.

## Recovery

ConfigMap reverted to `models:/ml-platform-model/1`, Deployment restarted:

```
deployment "inference" successfully rolled out
smoke test PASSED (6/6)
```

## Result

| | |
| --- | --- |
| Detection | readiness probe → `/ready` 503 |
| Containment | EndpointSlice `ready=false`; `maxUnavailable: 0` stalls the rollout |
| Recovery | config reverted, rollout completed, smoke test green |
| Evidence | endpointslice dump + in-pod probe output above |
