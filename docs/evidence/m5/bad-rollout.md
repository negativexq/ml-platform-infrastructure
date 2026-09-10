# F6 — Bad rollout

**Question:** a broken image reaches `main`. How far does it get?

## Method

`image.tag: v99-does-not-exist` committed and pushed. Argo CD applied it.
Requests fired at the Service throughout.

## Observed — the bad version never carried traffic

```
t+0s    image=v4                  availableReplicas=2  service/predict=200
t+8s    image=v99-does-not-exist  availableReplicas=2  service/predict=200
...
t+122s  image=v99-does-not-exist  availableReplicas=2  service/predict=200

ml-platform-inference-67486cc94b-f8xqj   0/1   ImagePullBackOff   0   2m8s
ml-platform-inference-86bf967968-9mbrr   1/1   Running            0   5m14s
ml-platform-inference-86bf967968-sk6x9   1/1   Running            0   7m16s

argo health: Progressing
```

The Deployment `spec` moved to the broken tag immediately — Git is the source
of truth and Argo did its job. What did **not** happen is the broken version
receiving a single request: `maxUnavailable: 0` means the old ReplicaSet is
not scaled down until a new pod is ready, and the new pod never was.

`availableReplicas` held at 2 for the full two minutes, and every one of the
16 probe requests returned 200.

## The signal to alert on

`argo health: Progressing` — not `Healthy`, not `Degraded`. A stalled rollout
looks like *slow progress*, and it will sit there indefinitely without
complaining. In the dashboard the same state reads as
`kube_deployment_spec_replicas` above `kube_deployment_status_replicas_available`
for longer than a rollout should take. That gap, sustained, is the alert worth
writing — not the pod phase.

## Recovery

```
$ git revert --no-edit HEAD && git push
restored: ml-platform-inference:v4  argo=Healthy

ml-platform-inference-86bf967968-9mbrr   1/1   Running   0   5m32s
ml-platform-inference-86bf967968-sk6x9   1/1   Running   0   7m34s
smoke test PASSED
```

The two serving pods have the same names and continuous ages across the whole
incident — they were never touched. The failed ReplicaSet was pruned.

| | |
| --- | --- |
| Detection | readiness never satisfied → `ImagePullBackOff`; Argo health stuck `Progressing` |
| Containment | `maxUnavailable: 0` — old ReplicaSet kept at full capacity |
| Recovery | `git revert` → Argo sync → broken ReplicaSet pruned |
| Evidence | request outcomes + pod ages above |
