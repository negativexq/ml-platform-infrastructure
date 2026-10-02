import { h } from './dom.js';
import { api, enc } from './api.js';

/** Ctrl/⌘+K: jump to any project, pipeline run view, model or deployment by typing a few letters. */

async function loadIndex() {
  const { items: projects } = await api.get('/projects?limit=200');
  const entries = [{ label: 'All projects', kind: 'page', href: '#/projects' }];
  await Promise.all(projects.map(async (p) => {
    const base = `/projects/${enc(p.name)}`;
    entries.push({ label: p.display_name, detail: p.name, kind: 'project', href: `#/projects/${enc(p.name)}` });
    const [models, deployments] = await Promise.all([
      api.get(`${base}/models`).catch(() => ({ items: [] })),
      api.get(`${base}/deployments`).catch(() => ({ items: [] })),
    ]);
    models.items.forEach((m) => entries.push({ label: m.name, detail: p.name, kind: 'model', href: `#/projects/${enc(p.name)}/models/${enc(m.name)}` }));
    deployments.items.forEach((d) => entries.push({ label: d.name, detail: p.name, kind: 'deployment', href: `#/projects/${enc(p.name)}/deployments/${enc(d.name)}` }));
  }));
  return entries;
}

/** Subsequence match, so "crprod" finds "credit-risk-prod". Lower score is better; null is no match. */
export function score(query, text) {
  const q = query.toLowerCase(), t = text.toLowerCase();
  if (!q) return 0;
  const at = t.indexOf(q);
  if (at >= 0) return at;
  let i = 0, gaps = 0, last = -1;
  for (const ch of q) {
    const found = t.indexOf(ch, last + 1);
    if (found < 0) return null;
    gaps += found - last - 1;
    last = found;
    i += 1;
  }
  return 100 + gaps + (q.length - i);
}

export function openPalette(dialog) {
  let entries = null, shown = [], active = 0;
  const input = h('input', { type: 'search', placeholder: 'Jump to a project, model or deployment…', 'aria-label': 'Search',
    autocomplete: 'off', spellcheck: 'false', 'data-testid': 'palette-input', role: 'combobox', 'aria-expanded': 'true', 'aria-controls': 'palette-list' });
  const list = h('ul', { id: 'palette-list', class: 'palette-list', role: 'listbox' });

  function go(entry) {
    dialog.close('ok');
    location.hash = entry.href;
  }
  function draw() {
    const q = input.value.trim();
    shown = (entries || []).map((e) => ({ e, s: score(q, `${e.label} ${e.detail || ''}`) })).filter((x) => x.s !== null)
      .sort((a, b) => a.s - b.s).slice(0, 12).map((x) => x.e);
    active = Math.min(active, Math.max(0, shown.length - 1));
    list.replaceChildren(...(entries === null ? [h('li', { class: 'muted palette-note' }, 'Loading…')]
      : shown.length ? shown.map((e, i) => h('li', { role: 'option', 'aria-selected': String(i === active), class: i === active ? 'on' : '',
        'data-testid': 'palette-item', onclick: () => go(e) },
      h('span', { class: 'kind' }, e.kind), h('span', {}, e.label), e.detail ? h('span', { class: 'muted small' }, e.detail) : null))
        : [h('li', { class: 'muted palette-note' }, 'Nothing matches.')]));
  }
  input.addEventListener('input', () => { active = 0; draw(); });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown') { active = Math.min(shown.length - 1, active + 1); draw(); event.preventDefault(); }
    else if (event.key === 'ArrowUp') { active = Math.max(0, active - 1); draw(); event.preventDefault(); }
    else if (event.key === 'Enter' && shown[active]) { go(shown[active]); event.preventDefault(); }
  });
  dialog.className = 'palette';
  dialog.replaceChildren(input, list);
  dialog.addEventListener('close', () => { dialog.className = ''; }, { once: true });
  draw();
  dialog.showModal();
  input.focus();
  loadIndex().then((all) => { entries = all; draw(); }).catch(() => { entries = []; draw(); });
}
