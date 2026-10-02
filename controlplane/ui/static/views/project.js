import { h, badge, timeEl, fmtDuration, shortId, copyButton } from '../dom.js';
import { api, enc, ApiError } from '../api.js';

const ACTIVE = new Set(['PENDING', 'SUBMITTED', 'RUNNING', 'PROGRESSING', 'DEPLOYING']);

const PAGE = 8;
const RUN_FILTERS = [['all', 'All', () => true], ['active', 'Active', (r) => ACTIVE.has(r.status)],
  ['failed', 'Failed', (r) => r.status === 'FAILED']];

export function projectView(ctx, name) {
  ctx.setCrumbs([{ label: 'Projects', href: '#/projects' }, { label: name }]);
  const p = enc(name);
  let view;
  let limit = PAGE, runFilter = 'all';

  async function runPipeline() {
    const { items } = await api.get(`/projects/${p}/pipelines`);
    if (!items.length) { ctx.toast('This project has no pipelines yet', 'bad'); return; }
    const run = await ctx.form({
      title: 'Run a pipeline', submitLabel: 'Start run',
      intro: 'Runs the latest version of the pipeline. You can follow it live on the next page.',
      fields: [
        { name: 'pipeline', label: 'Pipeline', required: true, options: items.map((x) => ({ value: x.name, label: `${x.name} (v${x.version})` })) },
        { name: 'commit_sha', label: 'Commit', placeholder: 'a83d2c1', hint: 'Optional. Recorded on the run and on every model it registers.' },
      ],
      submit: (v) => api.post(`/projects/${p}/pipelines/${enc(v.pipeline)}/runs`, v.commit_sha ? { commit_sha: v.commit_sha } : {}),
    });
    if (run) { ctx.toast('Run started'); location.hash = `#/projects/${p}/pipeline-runs/${run.id}`; }
  }

  async function startJob() {
    const { items } = await api.get(`/projects/${p}/jobs`);
    if (!items.length) { ctx.toast('This project has no jobs yet', 'bad'); return; }
    const run = await ctx.form({
      title: 'Start a job', submitLabel: 'Start job',
      fields: [{ name: 'job', label: 'Job', required: true, options: items.map((j) => ({ value: j.name, label: j.name })) }],
      submit: (v) => api.post(`/projects/${p}/jobs/${enc(v.job)}/runs`, {}),
    });
    if (run) { ctx.toast('Job started'); location.hash = `#/projects/${p}/runs/${run.id}`; }
  }

  view = ctx.mount({
    async load() {
      const [projects, summary, pruns, runs, models, deployments, audit] = await Promise.all([
        api.get('/projects?limit=200'),
        api.get(`/projects/${p}/summary`),
        api.get(`/projects/${p}/pipeline-runs?limit=${limit}`),
        api.get(`/projects/${p}/runs?limit=${limit}`),
        api.get(`/projects/${p}/models`),
        api.get(`/projects/${p}/deployments`),
        api.get(`/projects/${p}/audit?limit=15`).catch(() => ({ items: [] })),
      ]);
      const project = projects.items.find((x) => x.name === name);
      if (!project) throw new ApiError(404, 'not_found', `project ${name}`);
      return { project, summary, pruns: pruns.items, runs: runs.items, models: models.items, deployments: deployments.items, audit: audit.items };
    },
    paint(d) {
      const s = d.summary;
      ctx.setCrumbs([{ label: 'Projects', href: '#/projects' }, { label: d.project.display_name }]);
      ctx.render(
        h('div', { class: 'page-head' }, h('h1', {}, d.project.display_name), badge(d.project.status),
          h('div', { class: 'actions' },
            h('button', { class: 'btn primary', type: 'button', 'data-testid': 'run-pipeline', disabled: d.project.status !== 'READY',
              title: d.project.status === 'READY' ? '' : 'The project is not ready yet', onclick: runPipeline }, 'Run pipeline'),
            h('button', { class: 'btn', type: 'button', 'data-testid': 'start-job', disabled: d.project.status !== 'READY', onclick: startJob }, 'Start job'))),
        h('p', { class: 'sub' }, d.project.description || d.project.name),
        d.project.status_reason ? h('div', { class: 'alert' }, d.project.status_reason) : null,
        h('div', { class: 'tiles' },
          tile(s.pipelines, 'Pipelines'), tile(s.pipeline_runs + s.runs, 'Runs'),
          tile(`${s.champions}/${s.models}`, 'Models with champion'),
          tile(`${s.deployments_ready}/${s.deployments}`, 'Deployments ready'),
          tile(s.endpoints, 'Endpoints'), tile(s.active_rollouts, 'Rollouts in progress')),
        section('Pipeline runs', [runFilters(), pipelineRuns(name, d.pruns.filter(runTest())), more(d.pruns)], 'pipeline-runs'),
        section('Job runs', [jobRuns(name, d.runs.filter(runTest())), more(d.runs)], 'job-runs'),
        h('div', { class: 'cols' },
          section('Models', models(name, d.models), 'models'),
          section('Deployments', deployments(name, d.deployments), 'deployments')),
        section('Recent activity', activity(d.audit), 'activity'));
    },
    isActive: (d) => d.pruns.some((r) => ACTIVE.has(r.status)) || d.runs.some((r) => ACTIVE.has(r.status))
      || d.deployments.some((x) => x.status === 'DEPLOYING') || d.summary.active_rollouts > 0,
  });

  const runTest = () => RUN_FILTERS.find(([key]) => key === runFilter)[2];
  function runFilters() {
    return h('div', { class: 'toolbar' }, h('div', { class: 'seg', role: 'group', 'aria-label': 'Run filter' }, RUN_FILTERS.map(([key, label]) =>
      h('button', { type: 'button', 'aria-pressed': String(key === runFilter), 'data-run-filter': key,
        onclick: () => { runFilter = key; view.refresh(); } }, label))));
  }
  function more(rows) {
    return rows.length >= limit
      ? h('div', { class: 'more' }, h('button', { class: 'btn', type: 'button', 'data-testid': 'show-more',
        onclick: () => { limit += 20; view.refresh(); } }, 'Show more'))
      : null;
  }
  return view;
}

const tile = (n, label) => h('div', { class: 'tile' }, h('div', { class: 'n' }, n), h('div', { class: 'l' }, label));
const section = (title, body, testid) => h('div', { class: 'section', 'data-testid': testid }, h('h2', {}, title), body);
const empty = (text) => h('div', { class: 'empty' }, text);

function pipelineRuns(project, rows) {
  if (!rows.length) return empty('No pipeline runs yet.');
  return h('table', { class: 't' },
    h('thead', {}, h('tr', {}, ['Pipeline', 'Run', 'Commit', 'Status', 'Started', 'Duration'].map((c) => h('th', {}, c)))),
    h('tbody', {}, rows.map((r) => h('tr', { class: 'click', 'data-testid': 'pipeline-run-row',
      onclick: () => { location.hash = `#/projects/${enc(project)}/pipeline-runs/${r.id}`; } },
      h('td', {}, h('a', { href: `#/projects/${enc(project)}/pipeline-runs/${r.id}` }, `${r.pipeline || 'pipeline'} v${r.pipeline_version ?? '?'}`)),
      h('td', { class: 'mono' }, shortId(r.id)),
      h('td', { class: 'mono' }, r.commit_sha || '—'),
      h('td', {}, badge(r.status)),
      h('td', {}, timeEl(r.started_at || r.created_at)),
      h('td', {}, fmtDuration(r.duration_seconds))))));
}

function jobRuns(project, rows) {
  if (!rows.length) return empty('No job runs yet.');
  return h('table', { class: 't' },
    h('thead', {}, h('tr', {}, ['Job', 'Run', 'Status', 'Exit', 'Started', 'Duration'].map((c) => h('th', {}, c)))),
    h('tbody', {}, rows.map((r) => h('tr', { class: 'click', 'data-testid': 'job-run-row',
      onclick: () => { location.hash = `#/projects/${enc(project)}/runs/${r.id}`; } },
      h('td', {}, h('a', { href: `#/projects/${enc(project)}/runs/${r.id}` }, r.job || 'job')),
      h('td', { class: 'mono' }, shortId(r.id)),
      h('td', {}, badge(r.status)),
      h('td', { class: 'mono' }, r.exit_code ?? '—'),
      h('td', {}, timeEl(r.started_at || r.created_at)),
      h('td', {}, fmtDuration(r.duration_seconds))))));
}

function models(project, rows) {
  if (!rows.length) return empty('No models yet.');
  return h('table', { class: 't' },
    h('thead', {}, h('tr', {}, ['Model', 'Champion', 'Versions', ''].map((c) => h('th', {}, c)))),
    h('tbody', {}, rows.map((m) => h('tr', { class: 'click', 'data-testid': 'model-row',
      onclick: () => { location.hash = `#/projects/${enc(project)}/models/${enc(m.name)}`; } },
      h('td', {}, h('a', { href: `#/projects/${enc(project)}/models/${enc(m.name)}` }, m.name)),
      h('td', {}, m.champion ? badge('CHAMPION') : '—', m.champion ? ` v${m.champion.version}` : ''),
      h('td', { class: 'num' }, m.versions),
      h('td', {}, m.alias_drift ? badge('DRIFTED') : '')))));
}

function deployments(project, rows) {
  if (!rows.length) return empty('No deployments yet.');
  return h('table', { class: 't' },
    h('thead', {}, h('tr', {}, ['Deployment', 'Status', 'Revision', 'Endpoint'].map((c) => h('th', {}, c)))),
    h('tbody', {}, rows.map((d) => h('tr', { class: 'click', 'data-testid': 'deployment-row',
      onclick: () => { location.hash = `#/projects/${enc(project)}/deployments/${enc(d.name)}`; } },
      h('td', {}, h('a', { href: `#/projects/${enc(project)}/deployments/${enc(d.name)}` }, d.name)),
      h('td', {}, badge(d.status)),
      h('td', { class: 'mono' }, d.active_revision != null ? `r${d.active_revision}` : '—'),
      h('td', {}, badge(d.endpoint.status))))));
}

const AUDIT_LABELS = {
  'project.created': 'Project created', 'project.provisioned': 'Namespace provisioned', 'project.failed': 'Provisioning failed',
  'pipeline_run.created': 'Pipeline run created', 'pipeline_run.submitted': 'Pipeline run submitted', 'pipeline_run.succeeded': 'Pipeline run succeeded',
  'pipeline_run.failed': 'Pipeline run failed', 'run.created': 'Job run created', 'run.succeeded': 'Job run succeeded', 'run.failed': 'Job run failed',
  'deployment.ready': 'Deployment ready', 'deployment.failed': 'Deployment failed', 'deployment.rolled_back': 'Deployment rolled back',
  'rollout.started': 'Rollout started', 'rollout.succeeded': 'Rollout succeeded', 'rollout.rolled_back': 'Rollout rolled back',
};
const humanise = (action) => AUDIT_LABELS[action] || action.replace(/[._]/g, ' ');

function activity(events) {
  if (!events.length) return empty('No activity recorded yet.');
  return h('ul', { class: 'timeline' }, events.map((e) => h('li', {}, timeEl(e.occurred_at),
    h('span', {}, humanise(e.action)), h('span', { class: 'muted small' }, e.actor),
    e.trace_id ? h('span', { class: 'trace', title: 'Trace id: look it up in your tracing tool' }, e.trace_id.slice(0, 8), copyButton(e.trace_id, 'trace id')) : null)));
}
