# Platform connectivity

This is the proposed local topology and policy work prepared on 2026-10-05.
No manifests were applied and no CNI connectivity tests were run for this change.

## Selected namespace layout

| Namespace | Components |
| --- | --- |
| `mlp-system` | Control-plane API, gateway, reconciler, migration Job |
| `mlp-*` | Project training/Argo executor pods and KServe predictors |
| `ml-platform` | Existing PostgreSQL server, MLflow, MinIO |
| `argo` | Argo Workflows controller |
| `knative-serving`, `kourier-system` or `istio-system` | Activator, serving controller and routing |
| `identity` | Local Keycloak; shared installations may use an external OIDC issuer |
| `observability` | Prometheus, Collector, Tempo, Grafana |
| `ingress-nginx` | Public ingress controller |

Use `networkPolicy.controlplaneNamespace` in the platform-local chart if the control plane
uses a different namespace. The standalone gateway policy uses the concrete names above
and must be edited for another topology.

## Connection matrix

Both source egress and destination ingress must permit a connection when isolated.
Return packets on an admitted connection do not need an independent reverse rule.

| Source | Destination | Port / purpose |
| --- | --- | --- |
| Browser / external caller | Public ingress | TCP 443, UI/API and gateway requests |
| Ingress controller | API / gateway | TCP 8080 / 8081 |
| API, gateway, reconciler, migration Job | Control-plane database | TCP 5432 |
| API and reconciler | Kubernetes API | Cluster-specific API endpoint/port; resources, status and logs |
| Argo and KServe/Knative controllers | Kubernetes API | Controller operations; not arbitrary project inbound traffic |
| API and gateway | OIDC issuer | TCP 443 externally or 8080 for local Keycloak; discovery/JWKS and auth flows |
| API and reconciler | MLflow | TCP 5000, registry and experiment operations |
| Project training pods | MLflow / MinIO / approved storage | TCP 5000 / 9000 / storage-specific TLS; tracking and artifacts |
| KServe storage initializer | Artifact storage | TCP 9000 or approved external TLS endpoints; scoped credentials |
| API / gateway | Knative local gateway / predictor service | TCP 80 or configured serving ingress port |
| Knative routers / activator | Predictor queue-proxy / container | Configured queue-proxy and application ports; verify the actual routing implementation |
| Knative queue-proxy | Autoscaler | Actual Knative deployment's metric/control ports |
| API / reconciler | Prometheus | TCP 9090, Monitor and canary gates |
| Instrumented processes | OTEL Collector | TCP 4318 (HTTP) or 4317 if configured for gRPC |
| Prometheus | Scrape targets | Actual ServiceMonitor/PodMonitor target ports |
| Isolated application pods | Cluster DNS | UDP and TCP 53 |
| Project A | Project B workloads | Deny direct access unless an explicit cross-project contract permits it |

Image pulls are performed by the node/container runtime. Pod egress rules do not grant
registry reachability to the node; private registry credentials and node network access
must be handled separately. Training containers that download code, packages or weights
need a deliberate outbound policy as well as storage access.

## Prepared policy corrections

- The standalone gateway's PostgreSQL peer now combines namespace `ml-platform` with
  the existing chart's pod label `app.kubernetes.io/name=postgres`. Its earlier
  namespace-local `postgresql` selector could not match that database.
- The gateway has an identity-namespace Keycloak rule and a public IPv4 TCP 443 rule for
  the external issuer configured in the example manifest. Standard NetworkPolicy has no
  DNS-name selector: this rule allows other public HTTPS destinations too. For a shared
  installation, restrict it to maintained issuer CIDRs or use the CNI's FQDN policy.
  IPv6, redirects to other ports and an issuer routed through another namespace require
  site-specific rules. The local Keycloak's `localhost:8180` issuer is for host development
  and cannot be used unchanged by an in-cluster gateway.
- The platform-local chart permits database ingress from the labelled API, gateway,
  reconciler and migration pods in the selected control-plane namespace. MLflow remains
  permitted. It also permits MLflow access from API/reconciler and managed project
  namespaces, and MinIO access from managed project namespaces. Artifact authorization
  still depends on credentials and storage permissions.

## Configured project and control-plane policies

The provisioner keeps same-namespace baseline ingress and adds `mlp-serving-ingress`
only to pods with `serving.kserve.io/inferenceservice`. It admits labelled API/gateway
pods from the configured system namespace and traffic from configured serving namespaces.
Training pods are not opened to these callers. Serving namespaces are trusted infrastructure;
their rules permit application/queue-proxy/control ports across supported deployments.

Project egress is opt-in (`CP_PROJECT_EGRESS_ENABLED`), with DNS, same-namespace traffic,
MLflow/MinIO, serving control paths, collector export, explicit Kubernetes API CIDRs and
optional external HTTPS CIDRs. Disabling it removes only the provisioner's owned egress
policy, so other site policies still apply. Namespace names are configured through
`CP_SYSTEM_NAMESPACE`, `CP_PLATFORM_NAMESPACE`, `CP_OBSERVABILITY_NAMESPACE` and
`CP_SERVING_NAMESPACES`; CIDRs use `CP_CLUSTER_API_CIDRS` and `CP_EXTERNAL_HTTPS_CIDRS`.

The chart generates per-component API/gateway/reconciler/migration policies when
`networkPolicy.enabled=true`. Its migration policy is a pre-install/pre-upgrade hook that
runs before the migration Job. `networkPolicy.projectEgressEnabled` configures project
isolation separately. Either isolation toggle requires `networkPolicy.apiCIDRs`; neither
guesses an API address. Site values must include actual API destinations (Service/backend
addresses depend on CNI DNAT) and required issuer/artifact/weight HTTPS destinations.
`networkPolicy.servingNamespaces` must match the installed routing implementation.

The chart defaults keep egress isolation disabled until these site values are verified.
The standalone gateway policy selects its legacy `mlp-gateway` label. Standard policies
use CIDRs, not DNS names; external RDS/storage, NodeLocal DNS, IPv6 and alternate ports need
site-specific rules. The examples assume platform-local PostgreSQL/MLflow/MinIO.

## Remaining acceptance gates

With an enforcing CNI, prove every required matrix path and a denied direct project-A →
project-B connection. Check DNS over UDP/TCP, database access from the migration hook,
OIDC discovery/JWKS refresh, training artifact upload/download, canary traffic and metric
labels, scale-to-zero/reactivation, streamed responses and collector export. Record the
namespace/pod labels, destination ports, dependency/CNI versions, commands and results.
Rendering/schema validation alone does not close these gates.
