import { h, badge, timeEl, fmtDuration, num, shortId } from '../dom.js';
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
            : run.cancel_requested && ACTIVE.has(run.status) ? badge('CANCELLED') : null)),
        h('p', { class: 'sub' }, h('span', { class: 'mono' }, shortId(run.id)), ' · started ', timeEl(run.started_at || run.created_at),
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
        h('div', { class: 'section card' }, h('h2', {}, selected ? `Logs: ${selected}` : 'Logs'),
          h('pre', { class: 'logs', 'data-testid': 'logs' }, logs.text || '(no output yet)')),
        h('div', { class: 'section card', 'data-testid': 'tracking' }, h('h2', {}, 'Experiment tracking'), trackingPanel(tracking)));
    },
    isActive: (run) => ACTIVE.has(run.status),
  });
  return view;
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
