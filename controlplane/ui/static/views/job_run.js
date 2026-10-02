import { h, badge, timeEl, fmtDuration, shortId } from '../dom.js';
import { api, enc } from '../api.js';

const ACTIVE = new Set(['PENDING', 'SUBMITTED', 'RUNNING']);

export function jobRunView(ctx, project, id) {
  ctx.setCrumbs([{ label: 'Projects', href: '#/projects' }, { label: project, href: `#/projects/${enc(project)}` },
    { label: `job run ${shortId(id)}` }]);
  let logs = '';
  const view = ctx.mount({
    async load() {
      const run = await api.get(`/runs/${id}`);
      try { logs = await api.text(`/runs/${id}/logs`); } catch { logs = '(logs are not available)'; }
      return run;
    },
    paint(run) {
      const cancellable = ACTIVE.has(run.status) && !run.cancel_requested;
      ctx.render(
        h('div', { class: 'page-head' }, h('h1', {}, run.job || 'Job run'), badge(run.status),
          h('div', { class: 'actions' }, cancellable
            ? h('button', { class: 'btn danger', 'data-testid': 'cancel-run', onclick: async () => {
              if (await ctx.confirm({ title: 'Cancel this run?', body: 'The workload is stopped.', confirmLabel: 'Cancel run', danger: true }))
                await ctx.act(() => api.post(`/runs/${id}/cancel`), 'Cancellation requested', view);
            } }, 'Cancel run') : null)),
        h('p', { class: 'sub' }, h('span', { class: 'mono' }, shortId(run.id)), ' · started ', timeEl(run.started_at || run.created_at),
          ' · ', fmtDuration(run.duration_seconds), ' · exit code ', h('span', { class: 'mono' }, run.exit_code ?? '—')),
        run.status_reason ? h('div', { class: `alert${run.status === 'FAILED' ? ' bad' : ''}` }, run.status_reason) : null,
        h('div', { class: 'card' }, h('h2', {}, 'Logs'), h('pre', { class: 'logs', 'data-testid': 'logs' }, logs || '(no output yet)')));
    },
    isActive: (run) => ACTIVE.has(run.status),
  });
  return view;
}
