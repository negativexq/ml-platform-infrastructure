import { h, badge, timeEl, fmtDuration, pct, num } from '../dom.js';
import { api, enc } from '../api.js';

const ROLLING = new Set(['PENDING', 'PROGRESSING']);

export function deploymentView(ctx, project, name) {
  ctx.setCrumbs([{ label: 'Projects', href: '#/projects' }, { label: project, href: `#/projects/${enc(project)}` },
    { label: name }]);
  const base = `/projects/${enc(project)}`;
  let view;

  view = ctx.mount({
    async load() {
      const deployment = await api.get(`${base}/deployments/${enc(name)}`);
      const [rollouts, metrics, audit] = await Promise.all([
        api.get(`${base}/deployments/${enc(name)}/rollouts`),
        api.get(`${base}/endpoints/${enc(deployment.endpoint.name)}/metrics`).catch(() => null),
        api.get(`${base}/audit?entity_id=${deployment.id}&limit=10`).catch(() => ({ items: [] })),
      ]);
      return { deployment, rollouts: rollouts.items, metrics, audit: audit.items };
    },
    paint({ deployment: d, rollouts, metrics, audit }) {
      const live = rollouts.find((r) => ROLLING.has(r.status));
      ctx.render(
        h('div', { class: 'page-head' }, h('h1', {}, d.name), badge(d.status),
          h('div', { class: 'actions' },
            h('button', { class: 'btn danger', 'data-testid': 'rollback', disabled: Boolean(live) || d.revisions.length < 2 || d.active_revision == null,
              title: live ? 'A rollout is in progress; abort it instead' : d.revisions.length < 2 ? 'There is no earlier revision' : 'Serve the previous revision again',
              onclick: async () => {
                const prev = Math.max(...d.revisions.map((r) => r.revision).filter((n) => n < d.active_revision));
                if (await ctx.confirm({ title: 'Roll back this deployment?',
                  body: `Revision r${d.active_revision} is replaced by r${prev}. The model served by r${prev} becomes the champion again and the current champion is archived.`,
                  confirmLabel: 'Roll back', danger: true }))
                  await ctx.act(() => api.post(`${base}/deployments/${enc(name)}/rollback`), `Rolling back to r${prev}`, view);
              } }, 'Roll back'))),
        h('p', { class: 'sub' }, 'Active revision ', h('b', {}, d.active_revision != null ? `r${d.active_revision}` : 'none'),
          d.desired_revision !== d.active_revision ? [' · desired ', h('b', {}, `r${d.desired_revision}`)] : null),
        d.status_reason ? h('div', { class: `alert${d.status === 'FAILED' ? ' bad' : ''}` }, d.status_reason) : null,
        live ? rolloutCard(live, project, view, ctx) : null,
        h('div', { class: 'cols' }, endpointCard(d.endpoint, metrics, live), revisionsCard(d)),
        rollouts.filter((r) => r !== live).length ? h('div', { class: 'section' }, h('h2', {}, 'Rollout history'), rolloutHistory(rollouts.filter((r) => r !== live))) : null,
        h('div', { class: 'section card' }, h('h2', {}, 'Recent activity'), activity(audit)));
    },
    isActive: () => true, // metrics are live; keep polling while the page is open
    interval: 3000,
  });
  return view;
}

function rolloutCard(r, project, view, ctx) {
  return h('div', { class: 'card section', 'data-testid': 'rollout' },
    h('h2', {}, `Canary rollout: r${r.from_revision} → r${r.to_revision}`, badge(r.status),
      h('span', { class: 'muted small' }, ` ${r.model} v${r.model_version}`)),
    h('div', { class: 'bar', role: 'img', 'aria-label': `Traffic: stable ${100 - r.canary_percent}%, canary ${r.canary_percent}%` },
      h('div', { class: 'stable', style: { width: `${100 - r.canary_percent}%` }, 'data-testid': 'stable-share' }, `r${r.from_revision} stable ${100 - r.canary_percent}%`),
      r.canary_percent ? h('div', { class: 'canary', style: { width: `${r.canary_percent}%` }, 'data-testid': 'canary-share' }, `r${r.to_revision} ${r.canary_percent}%`) : null),
    h('div', { class: 'steps', 'aria-label': 'Rollout steps' }, r.steps.map((s, i) =>
      h('span', { class: i < r.current_step ? 'done' : i === r.current_step ? 'cur' : '' }, `${s}%`))),
    h('p', { class: 'muted small' }, `Gate: error rate ≤ ${pct(r.gate.max_error_rate)}, p95 ≤ ${r.gate.max_p95_latency_ms} ms, ≥ ${r.gate.min_requests} requests, ${fmtDuration(r.gate.step_seconds)} per step`),
    r.abort_requested ? h('div', { class: 'alert' }, 'Abort requested — returning traffic to the stable revision.') : null,
    h('button', { class: 'btn danger', 'data-testid': 'abort', disabled: r.abort_requested, onclick: async () => {
      if (await ctx.confirm({ title: 'Abort this rollout?', body: `All traffic returns to r${r.from_revision}. The canary is not promoted.`, confirmLabel: 'Abort rollout', danger: true }))
        await ctx.act(() => api.post(`/rollouts/${r.id}/abort`), 'Abort requested', view);
    } }, 'Abort rollout'));
}

function endpointCard(endpoint, metrics, live) {
  const rows = metrics && metrics.available ? metrics.revisions : [];
  return h('div', { class: 'card', 'data-testid': 'endpoint' },
    h('h2', {}, 'Endpoint', badge(endpoint.status)),
    h('p', { class: 'small' }, h('span', { class: 'mono' }, endpoint.name), endpoint.url ? [' · ', h('span', { class: 'mono muted' }, endpoint.url)] : null),
    metrics && !metrics.available ? h('div', { class: 'alert' }, `Metrics unavailable: ${metrics.error}`) : null,
    rows.length ? rows.map((m) => h('div', { class: 'rev', 'data-testid': 'revision-metrics', 'data-revision': m.revision },
      h('h3', {}, `r${m.revision}`, live ? (m.revision === live.to_revision ? ' · canary' : ' · stable') : '', ` · ${m.model} v${m.model_version} · ${m.traffic_percent}% of traffic`),
      h('div', { class: 'metric' },
        h('div', {}, h('b', {}, m.p95_latency_ms == null ? '—' : `${num(m.p95_latency_ms, 0)} ms`), h('span', {}, 'p95 latency')),
        h('div', {}, h('b', {}, pct(m.error_rate)), h('span', {}, '5xx rate')),
        h('div', {}, h('b', {}, num(m.requests_per_second, 1)), h('span', {}, 'requests / s')))))
      : metrics && metrics.available ? h('p', { class: 'muted' }, 'No traffic data yet.') : null);
}

function revisionsCard(d) {
  return h('div', { class: 'card', 'data-testid': 'revisions' }, h('h2', {}, 'Revisions'),
    h('table', { class: 't' },
      h('thead', {}, h('tr', {}, ['Revision', 'Model', 'Created', ''].map((c) => h('th', {}, c)))),
      h('tbody', {}, [...d.revisions].reverse().map((r) => h('tr', {},
        h('td', { class: 'mono' }, `r${r.revision}`), h('td', {}, `${r.model} v${r.model_version}`), h('td', {}, timeEl(r.created_at)),
        h('td', {}, r.revision === d.active_revision ? badge('READY') : r.revision === d.desired_revision ? badge('DEPLOYING') : null))))));
}

function rolloutHistory(rows) {
  return h('table', { class: 't', 'data-testid': 'rollout-history' },
    h('thead', {}, h('tr', {}, ['Rollout', 'Model', 'Outcome', 'Reason', 'Finished'].map((c) => h('th', {}, c)))),
    h('tbody', {}, rows.map((r) => h('tr', {},
      h('td', { class: 'mono' }, `r${r.from_revision} → r${r.to_revision}`), h('td', {}, `${r.model} v${r.model_version}`),
      h('td', {}, badge(r.status)), h('td', { class: 'muted' }, r.status_reason || '—'), h('td', {}, timeEl(r.finished_at))))));
}

const LABELS = {
  'deployment.created': 'Deployment created', 'deployment.revision_created': 'New revision', 'deployment.ready': 'Became ready',
  'deployment.drift_detected': 'Serving drift detected', 'deployment.redeploying': 'Recreating serving resource',
  'deployment.failed': 'Failed', 'deployment.rolled_back': 'Rolled back', 'endpoint.ready': 'Endpoint ready',
};
function activity(events) {
  if (!events.length) return h('p', { class: 'muted' }, 'No activity recorded.');
  return h('ul', { class: 'timeline' }, events.map((e) => h('li', {}, timeEl(e.occurred_at),
    h('span', {}, LABELS[e.action] || e.action), h('span', { class: 'muted small' }, e.actor))));
}
