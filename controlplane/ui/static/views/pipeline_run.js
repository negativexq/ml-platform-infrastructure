import { h, badge, timeEl, fmtDuration, num, shortId, copyButton } from '../dom.js';
import { logsPanel, newLogPrefs } from '../logs.js';
import { api, enc } from '../api.js';
import { dagSvg } from '../dag.js';

const ACTIVE = new Set(['PENDING', 'SUBMITTED', 'RUNNING']);

export function pipelineRunView(ctx, project, id) {
  ctx.setCrumbs([{ label: 'Projects', href: '#/projects' }, { label: project, href: `#/projects/${enc(project)}` },
    { label: `run ${shortId(id)}` }]);
  let selected = null;
  let logs = { step: null, text: '' };
  let tracking = { state: 'loading', runs: [] };
  let view;
  const logPrefs = newLogPrefs();

  async function loadLogs(step) {
    try { logs = { step, text: await api.text(`/pipeline-runs/${id}/steps/${enc(step)}/logs`) }; }
    catch { logs = { step, text: '(logs are not available)' }; }
  }
  const select = async (step) => { selected = step; await loadLogs(step); view.refresh(); };

  view = ctx.mount({
    async load() {
      const run = await api.get(`/pipeline-runs/${id}`);
      if (selected === null) {
        // Default to the step most worth looking at: a failure, else what is running, else the last.
        const pick = run.steps.find((s) => s.status === 'FAILED') || run.steps.find((s) => s.status === 'RUNNING')
          || run.steps[run.steps.length - 1];
        selected = pick ? pick.step : null;
      }
      if (selected) await loadLogs(selected);
      try {
        tracking = { state: 'ok', runs: (await api.get(`/pipeline-runs/${id}/tracking`)).runs };
      } catch { tracking = { state: 'unavailable', runs: [] }; }
      return run;
    },
    paint(run) {
      const cancellable = ACTIVE.has(run.status) && !run.cancel_requested;
      ctx.render(
        h('div', { class: 'page-head' },
          h('h1', {}, `${run.pipeline} v${run.pipeline_version}`), badge(run.status),
          h('div', { class: 'actions' }, cancellable
            ? h('button', { class: 'btn danger', 'data-testid': 'cancel-run', onclick: async () => {
              if (await ctx.confirm({ title: 'Cancel this run?', body: 'Running steps are stopped and pending steps are cancelled.',
                confirmLabel: 'Cancel run', danger: true })) await ctx.act(() => api.post(`/pipeline-runs/${id}/cancel`), 'Cancellation requested', view);
            } }, 'Cancel run')
            : run.cancel_requested && ACTIVE.has(run.status) ? badge('CANCELLED') : null,
          !ACTIVE.has(run.status) ? h('button', { class: 'btn', type: 'button', 'data-testid': 'rerun', title: 'Start a new run of the same pipeline version',
            onclick: () => rerun(run) }, 'Run again') : null)),
        h('p', { class: 'sub' }, h('span', { class: 'mono', title: run.id }, shortId(run.id)), copyButton(run.id, 'run id'), ' · started ', timeEl(run.started_at || run.created_at),
          ' · ', fmtDuration(run.duration_seconds), run.commit_sha ? [' · commit ', h('span', { class: 'mono' }, run.commit_sha)] : null),
        run.status_reason ? h('div', { class: `alert${run.status === 'FAILED' ? ' bad' : ''}` }, run.status_reason) : null,
        h('div', { class: 'card', 'data-testid': 'dag' }, h('h2', {}, 'Steps'), dagSvg(run.steps, selected, select)),
        h('div', { class: 'section' },
          h('table', { class: 't' },
            h('thead', {}, h('tr', {}, ['Step', 'Status', 'Exit', 'Duration', 'Depends on'].map((c) => h('th', {}, c)))),
            h('tbody', {}, run.steps.map((s) => h('tr', { class: `click${s.step === selected ? ' sel' : ''}`, 'data-testid': 'step-row',
              onclick: () => select(s.step) },
              h('td', {}, s.step), h('td', {}, badge(s.status)), h('td', { class: 'mono' }, s.exit_code ?? '—'),
              h('td', {}, fmtDuration(s.duration_seconds)), h('td', { class: 'muted' }, s.depends_on.join(', ') || '—')))))),
        run.steps.some((s) => s.started_at) ? h('div', { class: 'section card', 'data-testid': 'timeline' }, h('h2', {}, 'Timeline'), timeline(run, select)) : null,
        logsPanel(logPrefs, { title: selected ? `Logs: ${selected}` : 'Logs', text: logs.text, live: ACTIVE.has(run.status),
          filename: `${run.pipeline}-${shortId(run.id)}-${selected || 'run'}.log` }),
        h('div', { class: 'section card', 'data-testid': 'tracking' }, h('h2', {}, 'Experiment tracking'), trackingPanel(tracking)));
    },
    isActive: (run) => ACTIVE.has(run.status),
  });
  async function rerun(run) {
    try {
      const again = await api.post(`/projects/${enc(project)}/pipelines/${enc(run.pipeline)}/runs?version=${run.pipeline_version}`,
        run.commit_sha ? { commit_sha: run.commit_sha } : {});
      ctx.toast('New run started');
      location.hash = `#/projects/${enc(project)}/pipeline-runs/${again.id}`;
    } catch (error) { ctx.toast(error.message || 'Could not start the run', 'bad'); }
  }
  return view;
}

/** Steps on a shared time axis: what ran in parallel, where the time went, what was waiting. */
function timeline(run, select) {
  const stamped = run.steps.filter((s) => s.started_at);
  const start = Math.min(...stamped.map((s) => Date.parse(s.started_at)));
  const now = Date.now();
  const end = Math.max(...stamped.map((s) => (s.finished_at ? Date.parse(s.finished_at) : now)), start + 1000);
  const span = end - start;
  return h('div', { class: 'timeline-chart', role: 'list' }, run.steps.flatMap((s) => {
    const from = s.started_at ? Date.parse(s.started_at) : null;
    const to = s.finished_at ? Date.parse(s.finished_at) : from != null ? now : null;
    const bar = from == null ? null : h('div', { class: `tl-bar st-${s.status}`,
      style: { left: `${((from - start) / span) * 100}%`, width: `${Math.max(0.8, ((to - from) / span) * 100)}%` },
      title: `${s.step}: ${fmtDuration(s.duration_seconds ?? (to - from) / 1000)}` });
    return [h('button', { class: 'tl-label linklike', type: 'button', role: 'listitem', onclick: () => select(s.step) }, s.step),
      h('div', { class: 'tl-track', 'aria-label': `${s.step} ${s.status.toLowerCase()}` }, bar)];
  }));
}

function trackingPanel(t) {
  if (t.state === 'unavailable') return h('p', { class: 'muted' }, 'Experiment tracking is not available.');
  if (!t.runs.length) return h('p', { class: 'muted' }, 'This run has not logged any tracked results yet.');
  return h('div', {}, t.runs.map((r) => h('div', { class: 'cols', style: { marginBottom: '10px' } },
    h('div', {}, h('h3', {}, `${r.step || 'step'} · parameters`),
      kv(r.params)),
    h('div', {}, h('h3', {}, 'metrics'), kv(Object.fromEntries(Object.entries(r.metrics).map(([k, v]) => [k, num(v, 3)]))),
      r.artifact_uri ? h('p', { class: 'small' }, 'Artifact: ', h('span', { class: 'mono', 'data-testid': 'artifact' }, r.artifact_uri)) : null))));
}

function kv(obj) {
  const entries = Object.entries(obj);
  return entries.length ? h('dl', { class: 'kv' }, entries.flatMap(([k, v]) => [h('dt', {}, k), h('dd', { class: 'mono' }, v)]))
    : h('p', { class: 'muted' }, '—');
}
