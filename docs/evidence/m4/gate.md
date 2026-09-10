# M4 Gate — evidence

Run date: 2026-09-10 · Argo CD v3.5.2 · Application `inference-local` ·
repo `github.com/negativexq/ml-platform-infrastructure` (private), branch `main`

The private repo's read credentials are taken from the GitHub CLI at bootstrap
time and written directly into the cluster by
[`scripts/argocd-up.sh`](../../../scripts/argocd-up.sh). **No token is
committed** — the Application manifest in Git holds only the repo URL.

## Application Healthy and Synced

```
$ ./scripts/argocd-up.sh
==> Applying the Application manifest
application.argoproj.io/inference-local created

==> Waiting for the Application to become Healthy
sync=?          health=?
sync=OutOfSync  health=Healthy
sync=Synced     health=Progressing
sync=Synced     health=Healthy
```

```
$ kubectl -n argocd get application inference-local \
    -o jsonpath='sync={.status.sync.status} health={.status.health.status} revision={.status.sync.revision}'
sync=Synced  health=Healthy  revision=830635a68a6bbaa8e496a3ed5bfabcbf6ad8363c
```

```
$ make smoke
  PASS  GET /health                        200
  PASS  GET /ready                         200
  PASS  POST /predict (valid)              200
  PASS  POST /predict (invalid)            422
  PASS  ready replicas                     2
  PASS  ready Service endpoints            2
smoke test PASSED
```

The workload is the same one M3 deployed, but nothing was installed from this
machine — Argo built it from the chart in Git.

## Git-controlled rollout

`image.tag: dev` → `v2` committed to `values-local.yaml` and pushed. No
`kubectl`, no `helm`, no forced refresh — Argo's own repo polling:

```
pushed dab0051 with image.tag=v2
t+1s    argo=Synced  revision=830635a  image=dev
t+42s   argo=Synced  revision=830635a  image=dev
t+105s  argo=Synced  revision=830635a  image=dev
t+146s  argo=Synced  revision=830635a  image=dev
t+163s  ROLLED OUT   revision=dab0051  image=v2
```

**163 seconds** to pick up the commit — Argo's default repo poll interval is
3 minutes, and this is what that actually feels like. Live-resource drift is
reconciled in ~1s (see below) because that path is watch-based, not polled.
Worth knowing before quoting a single "reconciliation time" for GitOps.

## Manual drift detected and healed

Full detail in
[argocd-drift-reconciliation.md](argocd-drift-reconciliation.md):

```
t+362ms   argo=Synced     image=v2     <- manual `kubectl set image`
t+914ms   argo=OutOfSync  image=dev    <- detected, reverting
t+1427ms  argo=Synced     image=dev    <- reconciled to Git
```

## Git revert restores the previous state

```
$ git revert --no-edit HEAD && git push
reverted: 6414b05 Revert "chore: roll local environment to image tag v2"

t+0s    argo=Synced  revision=dab0051  image=v2
t+167s  argo=Synced  revision=dab0051  image=v2
t+333s  argo=Synced  revision=dab0051  image=v2
t+350s  RESTORED     revision=6414b05  image=dev

smoke test PASSED
```

350s here — the push landed just after a poll, so it waited out two cycles.

## Gate result

| Check | Result |
| --- | --- |
| Argo Application Healthy | PASS |
| Argo Application Synced | PASS |
| Git-controlled deployment works | PASS (163s, poll-bound) |
| manual drift detected | PASS (`OutOfSync` at t+914ms) |
| manual drift healed | PASS (reconciled at t+1427ms) |
| Git revert restores previous state | PASS (350s, poll-bound) |

## Evidence note

The roadmap asked for an `OutOfSync → Synced` screenshot. This machine's Argo
UI was reached only by `kubectl port-forward`, and the state transitions are
sub-second, so the polled `kubectl` output above is the record instead — it
carries the same information with timestamps a screenshot would not have.
