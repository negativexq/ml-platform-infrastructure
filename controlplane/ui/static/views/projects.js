import { h, badge } from '../dom.js';
import { api, enc } from '../api.js';

export function projectsView(ctx) {
  ctx.setCrumbs([{ label: 'Projects' }]);
  return ctx.mount({
    async load() {
      const { items } = await api.get('/projects?limit=200');
      const summaries = await Promise.all(items.map((p) => api.get(`/projects/${enc(p.name)}/summary`).catch(() => null)));
      return items.map((project, i) => ({ project, summary: summaries[i] }));
    },
    paint(rows) {
      ctx.render(
        h('div', { class: 'page-head' }, h('h1', {}, 'Projects')),
        h('p', { class: 'sub' }, 'Everything the platform runs, grouped by project.'),
        rows.length
          ? h('div', { class: 'grid' }, rows.map(card))
          : h('div', { class: 'empty' }, 'No projects yet. Create one with POST /projects.'));
    },
    isActive: (rows) => rows.some((r) => ['PENDING', 'PROVISIONING', 'DRIFTED', 'DELETING'].includes(r.project.status)),
  });
}

function card({ project, summary: s }) {
  return h('a', { class: 'card', href: `#/projects/${enc(project.name)}`, 'data-testid': `project-${project.name}` },
    h('h2', {}, project.display_name, badge(project.status)),
    h('div', { class: 'muted mono small' }, project.name),
    h('p', { class: 'muted', style: { minHeight: '2.6em' } }, project.description || ''),
    project.status_reason ? h('div', { class: 'alert' }, project.status_reason) : null,
    s ? h('div', { class: 'counts' },
      count('Runs', s.runs + s.pipeline_runs), count('Models', s.models),
      count('Deployments', s.deployments), count('Endpoints', s.endpoints),
      s.active_rollouts ? h('div', {}, h('b', {}, s.active_rollouts), h('span', {}, 'Rollouts live')) : null)
      : null);
}

const count = (label, n) => h('div', { 'data-count': label.toLowerCase() }, h('b', {}, n), h('span', {}, label));
