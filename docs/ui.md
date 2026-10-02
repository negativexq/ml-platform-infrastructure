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

An application shell, the same on every page: a top bar (breadcrumbs, search, theme, help,
account) and a sidebar. The sidebar lists the platform pages (**Projects**, **Monitor**) and,
inside a project, a project switcher and that project's sections. Switching project keeps the
section you are on. Content uses the full width (up to 1480px) with dense tables. On a phone
the sidebar is a drawer behind the menu button and closes once you have navigated.

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
