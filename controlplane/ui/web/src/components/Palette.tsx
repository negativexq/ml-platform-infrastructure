import { useEffect, useMemo, useRef, useState } from 'react';
import { api, enc, type S } from '../api/client';
import { go, routes, score } from '../lib/format';

type Entry = { label: string; detail?: string; kind: string; href: string };

async function loadIndex(): Promise<Entry[]> {
  const { items: projects } = await api.get<S['ProjectList']>('/projects?limit=200');
  const entries: Entry[] = [{ label: 'All projects', kind: 'page', href: routes.projects() }];
  await Promise.all(projects.map(async (p) => {
    const base = `/projects/${enc(p.name)}`;
    entries.push({ label: p.display_name, detail: p.name, kind: 'project', href: routes.project(p.name) });
    const [models, deployments] = await Promise.all([
      api.get<S['ModelList']>(`${base}/models`).catch(() => ({ items: [] as S['ModelOut'][] })),
      api.get<S['DeploymentList']>(`${base}/deployments`).catch(() => ({ items: [] as S['DeploymentOut'][] })),
    ]);
    models.items.forEach((m) => entries.push({ label: m.name, detail: p.name, kind: 'model', href: routes.model(p.name, m.name) }));
    deployments.items.forEach((d) => entries.push({ label: d.name, detail: p.name, kind: 'deployment', href: routes.deployment(p.name, d.name) }));
  }));
  return entries;
}

/** Ctrl/⌘+K: jump to any project, model or deployment by typing a few letters. */
export function Palette({ onDone }: { onDone: (el: Element) => void }) {
  const [entries, setEntries] = useState<Entry[] | null>(null);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    loadIndex().then((all) => !cancelled && setEntries(all)).catch(() => !cancelled && setEntries([]));
    return () => { cancelled = true; };
  }, []);

  const shown = useMemo(() => (entries ?? [])
    .map((e) => ({ e, s: score(query.trim(), `${e.label} ${e.detail ?? ''}`) }))
    .filter((x): x is { e: Entry; s: number } => x.s !== null)
    .sort((a, b) => a.s - b.s).slice(0, 12).map((x) => x.e), [entries, query]);
  const current = Math.min(active, Math.max(0, shown.length - 1));

  const pick = (entry: Entry) => { if (root.current) onDone(root.current); go(entry.href); };

  return (
    <div ref={root}>
      <input type="search" placeholder="Jump to a project, model or deployment…" aria-label="Search" autoComplete="off" spellCheck={false}
        data-testid="palette-input" role="combobox" aria-expanded="true" aria-controls="palette-list" autoFocus value={query}
        onChange={(e) => { setQuery(e.target.value); setActive(0); }}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown') { setActive(Math.min(shown.length - 1, current + 1)); e.preventDefault(); }
          else if (e.key === 'ArrowUp') { setActive(Math.max(0, current - 1)); e.preventDefault(); }
          else if (e.key === 'Enter' && shown[current]) { pick(shown[current]); e.preventDefault(); }
        }} />
      <ul id="palette-list" className="palette-list" role="listbox">
        {entries === null ? <li className="muted palette-note">Loading…</li>
          : shown.length === 0 ? <li className="muted palette-note">Nothing matches.</li>
          : shown.map((e, i) => (
            <li key={e.href} role="option" aria-selected={i === current} className={i === current ? 'on' : ''}
              data-testid="palette-item" onClick={() => pick(e)}>
              <span className="kind">{e.kind}</span><span>{e.label}</span>
              {e.detail && <span className="muted small">{e.detail}</span>}
            </li>))}
      </ul>
    </div>
  );
}
