import { h, badge, timeEl, num, shortId } from '../dom.js';
import { api, enc } from '../api.js';

const EVALUATABLE = new Set(['REGISTERED', 'EVALUATING']);

export function modelView(ctx, project, name) {
  ctx.setCrumbs([{ label: 'Projects', href: '#/projects' }, { label: project, href: `#/projects/${enc(project)}` },
    { label: name }]);
  const base = `/projects/${enc(project)}/models/${enc(name)}`;
  let selected = null;
  let view;

  view = ctx.mount({
    async load() {
      const [model, list] = await Promise.all([api.get(base), api.get(`${base}/versions`)]);
      const versions = await Promise.all(list.items.map((v) => api.get(`/model-versions/${v.id}`)));
      return { model, versions: versions.sort((a, b) => b.version - a.version) };
    },
    paint({ model, versions }) {
      if (selected === null && versions.length) selected = (versions.find((v) => v.status === 'CHAMPION') || versions[0]).id;
      const champion = versions.find((v) => v.status === 'CHAMPION');
      const detail = versions.find((v) => v.id === selected);
      ctx.render(
        h('div', { class: 'page-head' }, h('h1', {}, model.name),
          model.champion ? badge('CHAMPION') : null,
          h('div', { class: 'actions' },
            h('button', { class: 'btn', 'data-testid': 'discover', onclick: () => ctx.act(async () => {
              const r = await api.post(`${base}/discover`);
              ctx.toast(r.created.length ? `Registered ${r.created.length} new version(s)` : 'No new versions found');
            }, 'Checked the registry', view) }, 'Discover versions'))),
        h('p', { class: 'sub' }, 'Registry name ', h('span', { class: 'mono' }, model.registry_name), ' · acceptance thresholds ',
          Object.keys(model.thresholds).length
            ? Object.entries(model.thresholds).map(([m, t]) => h('span', { class: 'chip mono' }, `${m} ${t.min != null ? '≥ ' + t.min : ''}${t.min != null && t.max != null ? ', ' : ''}${t.max != null ? '≤ ' + t.max : ''}`))
            : h('span', { class: 'muted' }, 'none set')),
        model.alias_drift ? h('div', { class: 'alert' }, `Registry alias drift: ${model.alias_drift}`) : null,
        versions.length ? h('table', { class: 't', 'data-testid': 'versions' },
          h('thead', {}, h('tr', {}, ['Version', 'Status', ...metricNames(versions), 'Source run', ''].map((c) => h('th', {}, c)))),
          h('tbody', {}, versions.map((v) => versionRow(v, metricNames(versions), project, champion, view, ctx, () => { selected = v.id; view.refresh(); }, selected))))
          : h('div', { class: 'empty' }, 'No versions yet. Train and register one, then press “Discover versions”.'),
        detail ? detailPanel(detail) : null);
    },
    isActive: (d) => d.versions.some((v) => v.status === 'EVALUATING'),
  });
  return view;
}

const metricNames = (versions) => [...new Set(versions.flatMap((v) => v.evaluations.flatMap((e) => Object.keys(e.metrics))))].sort();
const latest = (v) => v.evaluations[v.evaluations.length - 1];

function versionRow(v, metrics, project, champion, view, ctx, onSelect, selected) {
  const ev = latest(v);
  return h('tr', { class: `click${v.id === selected ? ' sel' : ''}`, 'data-testid': 'version-row', 'data-version': v.version, onclick: onSelect },
    h('td', { class: 'mono' }, `v${v.version}`),
    h('td', {}, badge(v.status)),
    metrics.map((m) => h('td', { class: 'num mono' }, ev && ev.metrics[m] != null ? num(ev.metrics[m], 3) : '—')),
    h('td', {}, v.source_pipeline_run_id
      ? h('a', { href: `#/projects/${enc(project)}/pipeline-runs/${v.source_pipeline_run_id}`, onclick: (e) => e.stopPropagation() }, shortId(v.source_pipeline_run_id))
      : '—'),
    h('td', { class: 'num' },
      EVALUATABLE.has(v.status) ? h('button', { class: 'btn small', 'data-testid': 'evaluate', onclick: (e) => {
        e.stopPropagation();
        ctx.act(() => api.post(`/model-versions/${v.id}/evaluate`), `Evaluated v${v.version}`, view);
      } }, 'Evaluate') : null,
      v.status === 'CANDIDATE' ? h('button', { class: 'btn small primary', 'data-testid': 'promote', onclick: async (e) => {
        e.stopPropagation();
        const body = champion ? `v${champion.version} (current champion) will be archived and v${v.version} becomes the champion.`
          : `v${v.version} becomes the first champion.`;
        if (await ctx.confirm({ title: `Promote v${v.version}?`, body: `${body} The registry alias follows shortly.`, confirmLabel: 'Promote' }))
          await ctx.act(() => api.post(`/model-versions/${v.id}/promote`), `v${v.version} is now the champion`, view);
      } }, 'Promote') : null));
}

function detailPanel(v) {
  return h('div', { class: 'cols section', 'data-testid': 'version-detail' },
    h('div', { class: 'card' }, h('h2', {}, `v${v.version} · evaluations`),
      v.evaluations.length ? v.evaluations.map((e) => h('div', {},
        h('div', {}, badge(e.status), ' ', timeEl(e.created_at)),
        h('table', { class: 't', style: { margin: '8px 0' } },
          h('thead', {}, h('tr', {}, ['Metric', 'Value', 'Required', ''].map((c) => h('th', {}, c)))),
          h('tbody', {}, e.checks.map((c) => h('tr', {},
            h('td', {}, c.metric), h('td', { class: 'mono' }, c.value == null ? 'not reported' : num(c.value, 3)),
            h('td', { class: 'mono' }, [c.min != null ? `≥ ${c.min}` : null, c.max != null ? `≤ ${c.max}` : null].filter(Boolean).join(', ')),
            h('td', {}, badge(c.passed ? 'PASSED' : 'FAILED'))))))))
        : h('p', { class: 'muted' }, 'Not evaluated yet.')),
    h('div', { class: 'card' }, h('h2', {}, 'Promotion history'),
      v.promotions.length ? h('ul', { class: 'timeline' }, v.promotions.map((p) => h('li', {},
        timeEl(p.created_at), h('span', {}, 'Promoted to champion', p.previous_champion_id ? ' (replaced the previous champion)' : ' (first champion)'),
        badge(p.status))))
        : h('p', { class: 'muted' }, 'Never promoted.')));
}
