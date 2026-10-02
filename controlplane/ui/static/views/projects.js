import { h, badge } from '../dom.js';
import { api, enc } from '../api.js';

const FILTERS = [['all', 'All', () => true], ['attention', 'Needs attention', (p) => ['FAILED', 'DRIFTED', 'DELETING'].includes(p.status)],
  ['ready', 'Ready', (p) => p.status === 'READY']];

export function projectsView(ctx) {
  ctx.setCrumbs([{ label: 'Projects' }]);
  let rows = [];
  let query = '', filter = 'all';

  // The toolbar and list container live across polling repaints, so typing is never interrupted.
  const list = h('div', { 'data-testid': 'projects-list' });
  const search = h('input', { type: 'search', placeholder: 'Filter projects…  ( / )', 'aria-label': 'Filter projects',
    'data-search': true, 'data-testid': 'projects-search', class: 'grow',
    oninput: (e) => { query = e.target.value.trim().toLowerCase(); drawList(); } });
  const segment = h('div', { class: 'seg', role: 'group', 'aria-label': 'Status filter' }, FILTERS.map(([key, label]) =>
    h('button', { type: 'button', 'aria-pressed': String(key === filter), 'data-filter': key,
      onclick: () => { filter = key; segment.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.filter === key))); drawList(); } }, label)));
  const create = h('button', { class: 'btn primary', type: 'button', 'data-testid': 'create-project', onclick: newProject }, 'New project');
  const toolbar = h('div', { class: 'toolbar' }, search, segment);

  async function newProject() {
    const created = await ctx.form({
      title: 'New project', intro: 'A project is a namespace on the cluster plus everything that runs in it.', submitLabel: 'Create project',
      fields: [
        { name: 'name', label: 'Name', required: true, pattern: '^[a-z][a-z0-9]*(-[a-z0-9]+)*$', placeholder: 'credit-risk',
          hint: 'Lowercase letters, digits and dashes; starts with a letter. It cannot be changed later.' },
        { name: 'display_name', label: 'Display name', placeholder: 'Credit Risk' },
        { name: 'description', label: 'Description', type: 'textarea' },
      ],
      submit: (v) => api.post('/projects', { name: v.name, display_name: v.display_name || undefined, description: v.description || undefined }),
    });
    if (created) { ctx.toast(`Project ${created.name} created`); location.hash = `#/projects/${enc(created.name)}`; }
  }

  function drawList() {
    const test = FILTERS.find(([key]) => key === filter)[2];
    const shown = rows.filter(({ project: p }) => test(p)
      && (!query || `${p.name} ${p.display_name} ${p.description || ''}`.toLowerCase().includes(query)));
    list.replaceChildren(shown.length ? h('div', { class: 'grid' }, shown.map(card))
      : h('div', { class: 'empty' }, rows.length ? 'No project matches this filter.' : 'No projects yet. Create the first one with “New project”.'));
  }

  return ctx.mount({
    async load() {
      const { items } = await api.get('/projects?limit=200');
      const summaries = await Promise.all(items.map((p) => api.get(`/projects/${enc(p.name)}/summary`).catch(() => null)));
      return items.map((project, i) => ({ project, summary: summaries[i] }));
    },
    paint(data) {
      rows = data;
      if (!toolbar.isConnected) {
        ctx.render(
          h('div', { class: 'page-head' }, h('h1', {}, 'Projects'), h('div', { class: 'actions' }, create)),
          h('p', { class: 'sub' }, 'Everything the platform runs, grouped by project.'),
          toolbar, list);
      }
      drawList();
    },
    isActive: (data) => data.some((r) => ['PENDING', 'PROVISIONING', 'DRIFTED', 'DELETING'].includes(r.project.status)),
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
