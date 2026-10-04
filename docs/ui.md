# The platform UI: how it is designed

React + TypeScript (`controlplane/ui/web`), built into `controlplane/ui/static`, served at `/ui`.
This page is for whoever changes it next.

## What it is for

Operators of an ML platform answering a question or making a change with consequences: what
is broken, why did this run fail, is the canary healthy, can this version ship, who may do
what. Each surface is shaped by the question it answers, following the control-plane design
rules we adopted (from the `Admin-ui-design` skill):

* **Explain, don't just expose.** A failed step says why (exit code, or which upstream step
  stopped it). A canary shows each gate's value against its limit. A model version shows
  the run and commit that produced it, and a run shows the versions it registered.
* **Preview before commit** for anything that moves traffic or changes policy: the deploy
  dialog states the canary plan and the rollback; editing thresholds lists which existing
  versions would pass or fail.
* **Permissions with their source.** Settings shows your role here and where it comes from
  (direct, a group, platform admin), and what each role can do. Disabled controls say which
  role they need.
* **Friction proportional to blast radius**: confirmations name the consequence; deleting
  a project needs its name typed.

## Layout

The top bar contains breadcrumbs, global search, theme, help and account controls. The
sidebar stays a global capability directory: Overview (Home), Work (Projects, Runs,
Pipelines), AI (Models, Functions), Serving (Deployments, Endpoints), Platform (Services),
Operations (Monitor, Activity), and Admin (Identity, Settings). Data and Infrastructure
are withheld until dataset/object storage and cluster/compute/storage inventory APIs are
available. GPU quota management remains in project Settings. No Coming later surfaces.

`#/home` is the operational landing page. It shows visible projects, active runs, model
counts excluding functions, deployments, endpoints, platform alerts, recent failures,
currently running jobs/pipelines, recently updated deployments and audit activity. Data
refreshes every 15 seconds, platform checks every 30 seconds. Per-project load errors are
shown explicitly alongside partial totals; missing health telemetry never implies healthy.
Attention scans the latest 25 failed pipeline and 25 failed job runs per project within
24 hours; active lists show up to 200 runs of each kind per project. New project reuses the
existing creation action. Run pipeline reuses the existing pipeline/commit form, with project-qualified choices
restricted to ready projects where the user is an operator/admin.

Global resource pages aggregate visible projects without workspace navigation cards.
Models expose name, type (ML Model / LLM), project, champion, version count, active serving
deployments and lifecycle status. Functions expose name, project, version, deployment and
status; their project list uses the same columns without the redundant project column. Search, project, model type and status filters operate on these inventories.
Serving associations come from each deployment's active revision, never deployment naming
conventions; no Production/Staging label is invented. Global Runs shows the latest 25
pipeline and 25 job runs per project, Activity the latest 25 events.

Entering a project keeps Projects selected in the global sidebar. The content header shows
the active project name and grouped lifecycle navigation: Overview, Build (Runs, Pipelines,
Jobs), Assets (Models, Functions), Serve (Deployments, Endpoints), Activity and Settings.
Dropdowns close on navigation, outside click and Escape, and highlight the active group.
Projects opens the project cards for selection. When leaving a project through the global
Projects link, a section query parameter carries its lifecycle section to the cards; picking
a card resumes that section in the new project. There is no header project dropdown.
Resource details select their owning section.
The Overview Resources table explicitly labels ML Model, LLM and Function and shows the
selected/served version and status. Existing project URLs remain valid; functions also
have `#/projects/<project>/functions/<name>`.

Endpoints have their own global list, project list and detail URL. Endpoint details reuse
the serving trends, API access and Try it / Playground components; they link to their
associated deployment for rollout operations.

Services shows the components with an existing provider telemetry contract: MLflow, Argo
Workflows, KServe and Prometheus, with measured error rate, p95 latency and judged status.
This measures recent control-plane provider calls, not direct component readiness probes.
Grafana, MinIO and PostgreSQL are not listed until component telemetry is available, and
component versions are not invented. Missing samples from a monitored provider still
show No data. Identity shows the account and links to project memberships; global Settings
links to existing project settings without introducing new policy APIs.

On a phone, the global sidebar is a drawer; grouped project lifecycle navigation remains
in the main content and wraps to the available width.

## Monitor (platform health)

`#/monitor` answers "is the platform itself doing its job?" for whoever is on call for it,
from `GET /platform/health` (any signed-in user; workload counts cover only their projects).

* **Status**: the worst of all checks that have data, also shown next to *Monitor* in the
  sidebar when it is a warning or critical, so a problem is visible from any page.
* **Needs attention**: only the checks over a threshold, with the value, the rule and what
  it means.
* **Workload**, from the database: projects not ready, runs in flight, the longest wait to
  start (a stuck reconciler or a full cluster), failed runs in 24h, deployments, rollouts.
* **Platform API** charts: traffic, 5xx rate and p95, with the warning threshold drawn.
* **Checks**: every measure, its scope (a reconciler, an external system), its value now,
  the rule it is judged by, its status and a sparkline. Signals with many series are table
  rows with sparklines (small multiples), never a many-coloured chart.

The checks and thresholds live in `controlplane/application/platform.py` and match the alert
rules in `observability/controlplane-alert-rules.yaml`. A reconciler heartbeat that goes
silent is critical (it stopped), not "no data". Without `CP_PROMETHEUS_URL` the page says so
and still shows the workload counts.

## API access (public endpoints)

The deployment page's **API access** card answers "can someone outside call this, how, and
how much are they?":
* Exposure (Public or Internal), the public URL, limits, and curl and Python snippets.
* The keys that can call it, and usage by caller against the endpoint's limit.
* Opening, closing, limits and keys are admin actions with previews:
  * closing says how many keys lose access;
  * a new limit is checked against the busiest minute of the last hour;
  * a new key's secret is shown once, in a dialog that says so.

Settings has the project's **API keys** list. See `docs/gateway.md`.

## LLMs

* **Models:** creating a model offers its kind. An LLM also takes its GPUs per replica and
  its context length; the preview says what that holds of the project's quota.
* **Model page:** an LLM has **Register from hub**. It takes a pinned `hf://` source and the
  results of an offline evaluation, and previews whether the version would become a
  candidate or be rejected. Hub versions show their source in the versions table.
* **Deployment page:**
  * **Playground:** an LLM deployment has a playground instead of "Try it". It is a
    conversation with a system prompt, max tokens and temperature, and every reply shows
    the prompt and completion tokens it cost.
  * **Revisions** say what serves them (LLM runtime and GPUs).
  * **API access** shows the chat path, a streamed curl and an OpenAI SDK snippet. Its
    limits and usage are in tokens, with prompt and completion per caller.
* **Settings:** **GPUs** shows the quota and what is in use. Only platform admins can
  change the quota, and the preview refuses a quota below current use.

## Charts

Built to the `dataviz` method (form first, colour last, computed not eyeballed):

| Chart | Question | Form | Colour job |
| --- | --- | --- | --- |
| Run history (pipeline, job) | Is it getting slower, is it flaky? | columns over time + median reference line | status (with symbol and word) |
| Gate meters (canary) | How close is each gate to its limit? | meter with the limit marked | status: within / near / over |
| Version comparison (model) | Is the candidate better, against the bar? | dot strip per metric + threshold line | emphasis: champion in the accent, the rest grey |
| Traffic split (canary) | Where does traffic go now? | stacked bar, 2px gaps, legend | categorical: stable slot 1, canary slot 2 |
| Step timeline (run) | What ran in parallel, where did the time go? | bars on a shared time axis | status |
| Serving trends (deployment) | How has each revision served over time, against the gate? | lines, one axis per chart, gate line, event markers | categorical by role |
| Platform API (monitor) | Is the API answering, fast, without errors? | line per measure, warning threshold line | one series, no legend |
| Check sparklines (monitor) | Which way is each check going? | 1.5px line in a table cell, threshold dashed | one series |
| Gateway usage (deployment) | How close are callers to the limit, who uses it? | total line with the limit drawn only when near; per-caller sparklines in a table | one series; callers are rows, never colours |

Every chart has a table twin on the same page, a tooltip on hover **and** keyboard focus, and
never relies on colour alone. The two categorical colours were validated against our light
and dark card surfaces with the skill's validator:

```bash
node validate_palette.js "#2a78d6,#eb6834" --mode light --surface "#ffffff"   # all checks pass
node validate_palette.js "#3987e5,#d95926" --mode dark  --surface "#171b23"   # all checks pass
```

Status colours (`--status-*`) are reserved for state and never used as a series.

## Visual rules

* System sans everywhere (the CSP allows no external fonts, and an operator console wants
  the platform's own UI face). Tabular figures only in columns.
* One accent (`--accent`); status colours carry meaning only.
* Flat surfaces: hairline borders carry the structure, no decorative shadows.
* One shape scale: containers 10px, controls 8px, chips 5-6px, status pills round, data
  marks 4px.
* No all-caps labels, no arrows on links, no em-dashes in copy, middle dots rationed.

All of it is covered by `controlplane/tests/test_ui.py` and `test_ui_auth.py` in a real
browser.

## Help and first-use guidance

The header’s **Help & learning** button (or `?`) opens an in-app guide with Getting
started, Platform map, help for the current page, how-to guides, troubleshooting and
keyboard shortcuts. The map links to global capability lists; guides opened from a
project link to that project's resources. Standalone `#/help?topic=guides` and
`#/help?topic=trouble` links support empty states and permission guidance. No external
support destination is shown until one is configured.

Project and resource lists distinguish an empty collection from unmatched filters.
Empty collections offer the existing creation action or the next lifecycle step;
unmatched filters offer **Clear filters**. Creation actions enforce the same role
restrictions as the page header. Access failures explain how to request the right
membership and link to permission guidance. Load failures remain visible as errors.

Model registration shows GPU and context settings only for LLMs. Hidden fields are
excluded from validation and submission; switching back to Classic ML cannot submit
LLM configuration. Function registration retains its separate scaling and environment
fields and does not show model thresholds.

## Notification center

The header bell opens a notification panel with All / Unread filters, individual read
controls and **Mark all read**. Opening a notification marks it read and navigates to its
run, deployment or endpoint. Read failures leave the item unread and show an error;
loading failures do not claim the inbox is empty. The bell shows the unread count and the
panel refreshes every 15 seconds, including while navigating between projects.

`GET /me/notifications` derives a personal feed from the caller's visible projects at
viewer level or above. It includes failed pipeline/job runs by **finish time** in the last
24 hours, current failed/drifted projects, current failed/degraded deployments and
unavailable endpoints, plus succeeded or rolled-back rollouts in the last 24 hours. A bad
deployment with an unavailable endpoint produces one incident with links to both resources.
Current issues are shown before successful rollout outcomes; up to 500 items are returned,
with total attention/unread counts and a truncation indicator.

Home's workload **Needs attention** entries use the same query and notification identities.
Platform telemetry alerts continue to link to Monitor. Reading an incident does not remove
it from Needs attention. Repeated polling and unrelated configuration changes do not create
new incidents: stateful issues use their immutable audit transition, and executions use
their terminal outcome. Recovery followed by another failure creates a new incident.

`POST /me/notifications/read` accepts selected IDs or `all: true`. The caller is taken from
the authenticated session, and only their accessible notifications can be marked. Receipts
are stored per user in PostgreSQL; they survive browser refresh and do not affect other
users. The in-memory demo uses the equivalent repository. Mutation requests retain the
normal same-origin CSRF protection. Apply migration **0013** with `make cp-migrate` before
running the updated control plane against an existing PostgreSQL database. No external
notification delivery is configured in this version.
