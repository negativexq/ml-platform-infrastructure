import { h, badge, timeEl, fmtDuration, shortId } from '../dom.js';
import { api, enc, ApiError } from '../api.js';

const ACTIVE = new Set(['PENDING', 'SUBMITTED', 'RUNNING', 'PROGRESSING', 'DEPLOYING']);

export function projectView(ctx, name) {
  ctx.setCrumbs([{ label: 'Projects', href: '#/projects' }, { label: name }]);
  const p = enc(name);
  return ctx.mount({
    async load() {
      const [projects, summary, pruns, runs, models, deployments] = await Promise.all([
        api.get('/projects?limit=200'),
        api.get(`/projects/${p}/summary`),
        api.get(`/projects/${p}/pipeline-runs?limit=8`),
        api.get(`/projects/${p}/runs?limit=8`),
        api.get(`/projects/${p}/models`),
        api.get(`/projects/${p}/deployments`),
      ]);
      const project = projects.items.find((x) => x.name === name);
      if (!project) throw new ApiError(404, 'not_found', `project ${name}`);
      return { project, summary, pruns: pruns.items, runs: runs.items, models: models.items, deployments: deployments.items };
    },
    paint(d) {
      const s = d.summary;
      ctx.setCrumbs([{ label: 'Projects', href: '#/projects' }, { label: d.project.display_name }]);
      ctx.render(
        h('div', { class: 'page-head' }, h('h1', {}, d.project.display_name), badge(d.project.status)),
        h('p', { class: 'sub' }, d.project.description || d.project.name),
        d.project.status_reason ? h('div', { class: 'alert' }, d.project.status_reason) : null,
        h('div', { class: 'tiles' },
          tile(s.pipelines, 'Pipelines'), tile(s.pipeline_runs + s.runs, 'Runs'),
          tile(`${s.champions}/${s.models}`, 'Models with champion'),
          tile(`${s.deployments_ready}/${s.deployments}`, 'Deployments ready'),
          tile(s.endpoints, 'Endpoints'), tile(s.active_rollouts, 'Rollouts in progress')),
        section('Pipeline runs', pipelineRuns(name, d.pruns), 'pipeline-runs'),
        section('Job runs', jobRuns(name, d.runs), 'job-runs'),
        h('div', { class: 'cols' },
          section('Models', models(name, d.models), 'models'),
          section('Deployments', deployments(name, d.deployments), 'deployments')));
    },
    isActive: (d) => d.pruns.some((r) => ACTIVE.has(r.status)) || d.runs.some((r) => ACTIVE.has(r.status))
      || d.deployments.some((x) => x.status === 'DEPLOYING') || d.summary.active_rollouts > 0,
  });
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
