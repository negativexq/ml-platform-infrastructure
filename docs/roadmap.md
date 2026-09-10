# ML Platform Infrastructure Roadmap — frozen v2

Status: **frozen v2** (2026-09-11). Milestones are closed in order M0 → M12,
then AWS from M13. The goal through M12 is to extract every piece of real
engineering evidence that can be produced locally — before spending a cent of
AWS credit. After M12 the local architecture is frozen; the AWS work swaps
each local dependency for its managed counterpart and re-runs the same gates.

| Milestone | Amaç | Ana çıktı | Status |
| --- | --- | --- | --- |
| M0 | Deploy edilecek workload contract'ını kurmak | Containerized inference service | ✅ |
| M1 | Local ML lifecycle kurmak | MLflow + PostgreSQL + MinIO + training/serving | ✅ |
| M2 | Kubernetes'e taşımak | kind üzerinde çalışan workload | ✅ |
| M3 | Deployment'ı standardize etmek | Helm chart | ✅ |
| M4 | GitOps kurmak | Argo CD + self-heal | ✅ |
| M5 | Production davranışını kanıtlamak | Observability + failure tests | ✅ |
| M6 | AWS migration'ı IaC olarak tasarlamak | Terraform foundation | ✅ |
| M7 | Full local Kubernetes platform | Bütün lifecycle kind üzerinde, Compose'suz | ✅ |
| M8 | Stateful persistence & recovery | Restart/restore sonrası veri kaybı yok | ⬜ |
| M9 | Security hardening | RBAC + NetworkPolicy allow/deny + image scan gates | ⬜ |
| M10 | Scaling, SLO & alerting | HPA + load + PDB + tested alert rules | ⬜ |
| M11 | End-to-end reproducibility | Fresh cluster tek komutla acceptance PASS | ⬜ |
| M12 | Local Release Candidate | `local-v1.0.0` freeze | ⬜ |
| M13+ | AWS | terraform apply, gerçek EKS/RDS/S3/ECR | ⬜ |

## Local → AWS dependency swap (the M13 contract)

| Local (M7–M12) | AWS (M13+) |
| --- | --- |
| PostgreSQL StatefulSet + PVC | RDS PostgreSQL |
| MinIO StatefulSet + PVC | S3 |
| kind | EKS |
| locally built image | ECR |
| kindnet NetworkPolicy | VPC security groups + NetworkPolicy |
| local PV backup/restore drill | RDS snapshots + S3 versioning |

Postgres and MinIO are **local environment dependencies**, deployed by a
separate `platform-local` chart — never bundled into the application chart,
because in AWS they are managed services the app does not deploy.

## M0 — Application & Repository Foundation

**Amaç:** Deploy edilecek servisin düzgün bir contract'ı olsun; uygulamanın
kendisini stabilize et.

**Yapılacaklar:** repo yapısı · Python env / dependency management · FastAPI
inference servisi · typed request/response · model inference katmanı ·
`GET /health` · `GET /ready` · `POST /predict` · config management ·
structured logging · Dockerfile · `.dockerignore` · Makefile · pytest · Ruff ·
mypy · GitHub Actions basic CI · README · architecture/roadmap docs.

**Özellikle test et:**

```
process alive        → /health 200
model hazır değil     → /ready 503
model hazır           → /ready 200
valid request         → /predict 200
invalid request       → typed validation error
```

**M0 Gate:** `pytest PASS` · `ruff PASS` · `mypy PASS` · `docker build PASS` ·
`container startup PASS` · `/health` · `/ready` · `/predict` PASS.

**Commit:** `M0: establish containerized inference service contract`

## M1 — Local ML Lifecycle

**Amaç:** Modeli repo içine gömülü `.pkl` yerine gerçek bir ML lifecycle ile
servis et.

**Local servisler:** PostgreSQL · MLflow Tracking Server · MinIO · training
workload · inference workload.

**Yapılacaklar:** Docker Compose · PostgreSQL/MinIO/MLflow container'ları ·
MLflow backend store → PostgreSQL · artifact store → MinIO · sample dataset ·
deterministic training script · parameter/metric/model logging · model
version/tag convention · inference `MODEL_URI` config · startup'ta artifact
download · load başarısızsa readiness false · bounded startup · integration
tests.

**Özellikle test et:** MinIO yokken? · MLflow erişilemiyorsa? · artifact
yanlışsa readiness? · doğru artifact gelince toparlıyor mu? · version değişince
yeni artifact yükleniyor mu?

**M1 Gate:** training PASS · MLflow experiment visible · metadata in PostgreSQL ·
artifact in MinIO · inference downloads artifact · `/ready` healthy · `/predict`
succeeds.

**Commit:** `M1: establish local MLflow-backed model lifecycle`

## M2 — Local Kubernetes with kind

**Amaç:** Container'ın gerçek orchestration ortamındaki davranışını gör.

**Yapılacaklar:** kind kurulumu + cluster config · namespace · Deployment ·
Service · ServiceAccount · ConfigMap · Secret · resource requests/limits ·
liveness/readiness probe · startup davranışı · replicas · rolling update
strategy · image load into kind · smoke-test script.

İlk adımda MLflow/Postgres/MinIO Docker Compose'da kalabilir; sadece inference
K8s'e geçer.

**Failure testleri:** pod crash → self-recovery · model load failure →
`/health 200` + `/ready 503` + no traffic · rolling update → availability
maintained.

**M2 Gate:** kind cluster reproducible · deployment healthy · 2 replicas ·
service routing works · pod deletion self-recovers · readiness failure prevents
traffic · rolling update works.

**Commit:** `M2: validate inference workload on Kubernetes`

## M3 — Helm Packaging

**Amaç:** K8s YAML'larını environment-specific copy/paste olmaktan çıkar.

**Yapılacaklar:** Helm chart · `Chart.yaml` · `values.yaml` ·
`values-local.yaml` · Deployment/Service/ConfigMap/ServiceAccount template ·
optional Secret refs · probes / resources / replicas / image repo+tag /
artifact endpoint / model URI configurable · environment labels/annotations ·
helm lint · template validation · upgrade + rollback test.

**Kaçın:** dev/prod ayrı chart · hardcoded endpoint / image tag / credentials ·
dev YAML copy-paste.

**M3 Gate:** `helm lint PASS` · `helm template PASS` · `helm upgrade --install`
→ healthy → predict works · yeni image/config → `helm upgrade` works ·
`helm rollback` → previous stable state restored.

**Commit:** `M3: package Kubernetes deployment as reusable Helm chart`

## M4 — GitOps with Argo CD

**Amaç:** Deployment authority'yi developer laptop'tan Git'e taşı.

**Yapılacaklar:** Argo CD local cluster'a kur · GitOps directory · Argo
Application manifest · auto-sync · prune · self-heal · Helm chart Git'ten
consume · environment values Git'te · manual drift scenario · Git change →
rollout · rollback/revert.

**Kritik testler:** drift (elle `replicas=1` → Argo self-heal → `replicas=2`) ·
Git-controlled rollout (image tag v1→v2 → Argo → rollout) · Git revert restores
previous state.

**M4 Gate:** Argo Application Healthy + Synced · Git-controlled deployment
works · manual drift detected + healed · Git revert restores previous state.

**Evidence:** README'ye `OutOfSync → Synced` ekran görüntüsü + kısa failure
timeline.

**Commit:** `M4: establish GitOps reconciliation with Argo CD`

## M5 — Observability & Failure Engineering

**Amaç:** "Deploy oluyor"dan "production altında davranışını biliyorum"a geç.

**Observability:** Prometheus · Grafana · application metrics · Kubernetes
metrics · structured logs · model/deployment version labels. Opsiyonel:
OpenTelemetry tracing (önce Prometheus/Grafana bitsin).

**Application metrics:** `http_requests_total` · `http_request_duration_seconds`
· `prediction_requests_total` · `prediction_errors_total` ·
`prediction_duration_seconds` · `model_load_duration_seconds` · `model_info`.

**Platform metrics:** pod count · pod restarts · CPU · memory · desired /
available replicas · readiness state.

**Dashboards:** Service health (RPS, p50, p95, error rate) · Model serving
(model version, prediction latency, model load time, prediction errors) ·
Kubernetes health (pod restarts, replicas, CPU, memory).

**Failure scenarios:** F1 pod crash · F2 invalid model artifact · F3 artifact
store outage · F4 config drift · F5 latency regression (+500 ms fault
injection) · F6 bad rollout (intentional failure image).

**Failure evidence formatı** (README tablosu): Failure · Detection ·
Containment · Recovery · Evidence.

**M5 Gate:** 5 failure scenarios · 5 detection mechanisms · 5 recovery paths ·
Prometheus metrics · Grafana dashboards · documented evidence.
**Hiçbir metric uydurulmayacak** — README'de yalnız gerçekten ölçülen şeyler.

**Commit:** `M5: add observability and deterministic failure evidence`

## M6 — Terraform & AWS Migration Design

**Amaç:** AWS resource'larını açmak değil — localde doğruladığımız platformun
AWS karşılığını kodla tanımlamak.

**Architecture mapping (M6 contract'ı):**

| Local | AWS |
| --- | --- |
| kind | EKS |
| Docker image | ECR |
| MinIO | S3 |
| PostgreSQL | RDS PostgreSQL |
| local Service | EKS networking / ALB |
| local identity | IAM workload identity |
| Helm local values | Helm AWS values |
| local Argo Application | AWS Argo environment |

**Terraform yapısı:** `infra/terraform/modules/{vpc,ecr,s3,rds,eks,iam}` +
`infra/terraform/environments/aws-dev/{providers,main,variables,outputs}.tf` +
`terraform.tfvars.example`.

**Foundations:** provider + Terraform version pinning · variables · outputs ·
locals · tagging + naming convention · module boundaries · environment
boundary · `.terraform.lock.hcl` · secrets git'e girmez.

**Modules:**
- **VPC:** public/private subnet(s) · route tables · security boundaries. Lab
  için 3 AZ / redundant NAT / multi-region şart değil — bütçe odaklı.
- **ECR:** repository · lifecycle policy · immutable/reasonable tagging.
- **S3:** bucket · encryption · public access blocked · lifecycle · scoped IAM.
- **RDS:** private networking · no public DB · security group · small lab
  instance · credentials externalized.
- **EKS:** cluster · one CPU node group (GPU sonra) · subnet selection · IAM
  roles · outputs.
- **IAM:** GitHub CI identity / EKS node identity / workload identity / MLflow
  S3 access ayrı — least privilege başlasın.

**M6 CI:** `terraform fmt -check` · `terraform validate` · `tflint`. AWS auth
olunca `terraform plan` eklenir. Automatic apply yok.

**M6 docs:** `docs/aws-architecture.md` (why EKS/S3/RDS/ECR · identity +
network boundaries · local → AWS mapping · cost assumptions · known
limitations) · `docs/cost-model.md` (ephemeral cluster · destroy after tests ·
GPU disabled by default · cost tags · budget alerts before apply).

**M6 Gate:** local→AWS mapping frozen · Terraform structure complete · module
boundaries defined · `fmt` / `validate` / `tflint` / CI PASS · IAM + network
design documented · cost model documented · no AWS secrets in repo · no actual
expensive infrastructure required.

**Commit:** `M6: establish Terraform foundation for AWS migration`

## M7 — Full local Kubernetes platform

**Amaç:** M2'den kalan split yapıyı bitir — inference K8s'te ama
MLflow/Postgres/MinIO Compose'da. Kubernetes dışında çalışan hiçbir dependency
kalmayacak.

**Mimari karar:** Postgres ve MinIO ayrı bir `platform-local` chart'ında
deploy edilir, uygulama chart'ına gömülmez. AWS'de bunlar RDS/S3 olacak; app
onları deploy etmez.

**Yapılacaklar:** `helm/platform-local/` chart (PostgreSQL, MinIO, MLflow) ·
kind cluster config'i genişlet · MLflow `platform-local` içine · inference
`ML_MLFLOW_TRACKING_URI` → in-cluster service DNS · `scripts/kind-up.sh`
Compose'a hiç dokunmayacak · Argo CD platform-local'i de yönetsin (app-of-apps
veya ikinci Application) · smoke-test full lifecycle'ı cluster içinde koşsun.

**M7 Gate:** fresh kind cluster → platform bootstrap → train → MLflow metadata
PostgreSQL pod'unda + artifact MinIO pod'unda → inference artifact indiriyor →
`/ready` 200 → `/predict` 200 — **hiçbir Docker Compose servisi çalışmadan.**

**Commit:** `M7: run the full ML lifecycle on Kubernetes`

## M8 — Persistence & recovery

**Amaç:** Kubernetes'in stateful tarafını çalıştır ve state kaybı / restore
contract'ını kanıtla.

**Yapılacaklar:** PostgreSQL ve MinIO → StatefulSet + PVC + PersistentVolume ·
`storageClassName` explicit · pod delete drill (Postgres, MinIO, MLflow) ·
backup/restore drill (`pg_dump` + MinIO artifact kopyası → delete/recreate →
restore → MLflow metadata↔artifact ilişkisi geçerli).

Production-grade PostgreSQL HA kurulmaz — AWS'de RDS var. Amaç veri kaybı
davranışını öğrenmek.

**M8 Gate:** pod recreation veri kaybettirmiyor (eski MLflow run'ları + artifact
duruyor, inference yüklenebiliyor) · kontrollü backup/restore PASS.

**Commit:** `M8: prove stateful persistence and restore`

## M9 — Security hardening

**Container:** `runAsNonRoot` · `readOnlyRootFilesystem` ·
`allowPrivilegeEscalation: false` · `capabilities.drop: [ALL]` ·
`seccompProfile: RuntimeDefault` — her workload için.

**Kubernetes:** dedicated ServiceAccount · RBAC (en dar) · NetworkPolicy ·
Secret separation · namespace isolation.

**NetworkPolicy — CNI'a güvenme, testle kanıtla:** kindnet'in NetworkPolicy'yi
gerçekten enforce edip etmediği önce ampirik doğrulanır (M7'de smoke). Sonra
explicit allow/deny integration testleri:

```
inference → MLflow        : ALLOWED
inference → PostgreSQL     : BLOCKED
MLflow    → PostgreSQL     : ALLOWED
MLflow    → MinIO          : ALLOWED
any       → inference:8000 : ALLOWED (Service üzerinden)
```

Her satır hem pozitif hem negatif olarak test edilir. Enforce edilmiyorsa o
noktada Calico'ya geçilir — ama önce ölç.

**CI/local security gates:** Trivy image scan · dependency scan · SBOM ·
`helm lint` · `kubeconform` · Terraform static checks.

**M9 Gate:** workload privilege sınırları ve network boundary'leri her biri
allow+deny testiyle kanıtlanmış · image scan CI'da.

**Commit:** `M9: enforce and prove workload security boundaries`

## M10 — Scaling, SLO & alerting

**Yapılacaklar:** `metrics-server` (kind'da `--kubelet-insecure-tls`) · HPA
(CPU + istek bazlı) · k6 ile load · ölç: RPS/p50/p95/error rate/replica
count/CPU/mem/scale-up süresi/scale-down süresi · `PodDisruptionBudget`
(`minAvailable: 1`) · `kubectl drain` ile node maintenance testi.

**Alerting (M5'ten eksik kalan):** PrometheusRule — `HighErrorRate` ·
`HighLatency` · `InferenceUnavailable` · `ModelNotReady` · `ReplicaUnavailable`.
Sadece YAML değil: `promtool test rules` ile given-series → condition →
expected-alert testi.

**SLO:** Availability / Latency / Error-rate SLI — rakamlar benchmark
görülmeden dondurulmaz.

**M10 Gate:** load altında autoscaling ölçülmüş · disruption budget korunmuş ·
alert rule'ları `promtool` ile test edilmiş.

**Commit:** `M10: measure autoscaling and test alert rules`

## M11 — End-to-end reproducibility

**Amaç:** "Benim makinemde çalışıyor" yetmez. Repo dışında hiçbir manuel state
gerektirmeden fresh cluster'da acceptance PASS.

**Yapılacaklar:**

```
make local-up     # kind + platform-local + Argo CD + observability + app + seed/train
make local-test   # health/ready/predict + MLflow persistence + pod recovery +
                  # readiness isolation + GitOps sync + NetworkPolicy allow/deny +
                  # Prometheus scrape + alert rules + HPA smoke
make local-down    # her şeyi kaldır
```

**Kritik test:** brand-new kind cluster → repo dışında elle oluşturulmuş
kaynak yok → bootstrap → acceptance PASS.

**CI:** ucuz olanlar her PR'da (pytest, ruff, mypy, helm lint, kubeconform,
terraform validate, tflint, trivy, promtool) · pahalı olan (kind → deploy →
smoke) opsiyonel/manuel.

**M11 Gate:** fresh environment'da repo dışında manuel state gerektirmeden
`make local-up && make local-test` PASS.

**Commit:** `M11: one-command reproducible local environment`

## M12 — Local Release Candidate

**Amaç:** Local geliştirmeyi freeze et. Yeni feature yok.

**Son acceptance:** fresh clone → fresh kind → bootstrap → train → track →
store → serve → observe → scale → break → recover → destroy.

**README evidence matrix** — her capability'nin altında gerçek kanıt:

| Capability | Evidence |
| --- | --- |
| ML lifecycle | Postgres metadata + MinIO artifact (pod'larda) |
| Serving | MLflow artifact → inference |
| Health semantics | non-blocking health/readiness |
| Kubernetes recovery | measured pod recovery |
| Rolling update | measured zero-drop |
| GitOps | OutOfSync → Synced |
| Observability | live Prometheus/Grafana queries |
| Failure engineering | 6 drills |
| Persistence | restart/restore drill |
| Security | RBAC + NetworkPolicy allow/deny |
| Scaling | measured HPA behavior |
| Alerting | tested Prometheus rules |
| Reproducibility | fresh-cluster acceptance |

Sonra: `git tag local-v1.0.0`, local architecture frozen.

**Commit:** `M12: freeze local release candidate` + tag `local-v1.0.0`

## Explicitly out of scope (local)

- **LocalStack ile AWS taklidi yok.** MinIO object-store contract'ı zaten
  veriyor. IAM/SG/EKS/IRSA/OIDC/RDS/ALB'yi localde taklit etmek sahte güven
  verir, gerçek AWS davranışını kanıtlamaz.
- **Production PostgreSQL HA / distributed MinIO / 15-tool security stack yok.**
  Scope'u büyütür, CV açığını kapatmaz.

## Evidence

Her milestone'un kanıtı `docs/evidence/m<N>/` altında tutulur — README'deki her
claim'in altında gerçek kanıt gösterilebilsin diye.
