# M2 — Pod crash self-recovery

**Question:** when a pod disappears, does the platform replace it without the
Service ever going down?

## Method

Cluster: kind `ml-platform`, 1 control-plane + 2 workers. Deployment
`inference`, 2 replicas, exposed through a NodePort Service on
`localhost:30080`. One pod deleted; deployment status and the Service polled
every 3s.

## Observed

```
=== before ===
inference-7546c7d5b4-fz9g5   1/1   Running   10.244.1.3   ml-platform-worker2
inference-7546c7d5b4-vt8nh   1/1   Running   10.244.2.3   ml-platform-worker

=== deleting pod inference-7546c7d5b4-fz9g5 ===
t+0s   readyReplicas=1  service/ready=HTTP 200
t+3s   readyReplicas=1  service/ready=HTTP 200
t+6s   readyReplicas=1  service/ready=HTTP 200
t+9s   readyReplicas=1  service/ready=HTTP 200
t+13s  readyReplicas=2  service/ready=HTTP 200

=== after ===
inference-7546c7d5b4-tv62w   1/1   Running   0   13s   10.244.1.4   ml-platform-worker2
inference-7546c7d5b4-vt8nh   1/1   Running   0   44s   10.244.2.3   ml-platform-worker
```

## Result

| | |
| --- | --- |
| Detection | ReplicaSet controller — observed replica count fell below desired |
| Containment | Surviving replica kept serving; Service never returned non-200 |
| Recovery | New pod scheduled, model pulled from MLflow, ready at **t+13s** |
| Evidence | poll output above |

The replacement pod had to download the artifact from MLflow before passing
readiness, so 13s is the real cold-start cost of this workload, not just
scheduling latency.
