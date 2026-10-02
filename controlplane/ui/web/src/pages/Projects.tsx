import { useMemo, useState } from 'react';
import { api, enc, type S } from '../api/client';
import { Badge, Empty } from '../components/bits';
import { useOverlays } from '../components/overlays';
import { useCrumbs } from '../lib/chrome';
import { go, routes } from '../lib/format';
import { QueryView, useLiveQuery } from '../lib/query';

type Row = { project: S['ProjectOut']; summary: S['SummaryOut'] | null };

const FILTERS: [string, string, (p: S['ProjectOut']) => boolean][] = [
  ['all', 'All', () => true],
  ['attention', 'Needs attention', (p) => ['FAILED', 'DRIFTED', 'DELETING'].includes(p.status)],
  ['ready', 'Ready', (p) => p.status === 'READY'],
];
const BUSY = ['PENDING', 'PROVISIONING', 'DRIFTED', 'DELETING'];

export function ProjectsPage() {
  useCrumbs([{ label: 'Projects' }]);
  const { form, toast } = useOverlays();
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('all');

  const rows = useLiveQuery<Row[]>(['projects'], async () => {
    const { items } = await api.get<S['ProjectList']>('/projects?limit=200');
    const summaries = await Promise.all(items.map((p) => api.get<S['SummaryOut']>(`/projects/${enc(p.name)}/summary`).catch(() => null)));
    return items.map((project, i) => ({ project, summary: summaries[i] ?? null }));
  }, (data) => data.some((r) => BUSY.includes(r.project.status)));

  async function newProject() {
    const created = await form<S['ProjectOut']>({
      title: 'New project', intro: 'A project is a namespace on the cluster plus everything that runs in it.', submitLabel: 'Create project',
      fields: [
        { name: 'name', label: 'Name', required: true, pattern: '^[a-z][a-z0-9]*(-[a-z0-9]+)*$', placeholder: 'credit-risk',
          hint: 'Lowercase letters, digits and dashes; starts with a letter. It cannot be changed later.' },
        { name: 'display_name', label: 'Display name', placeholder: 'Credit Risk' },
        { name: 'description', label: 'Description', type: 'textarea' },
      ],
      submit: (v) => api.post<S['ProjectOut']>('/projects', {
        name: v.name, display_name: v.display_name || undefined, description: v.description || undefined,
      }),
    });
    if (created) { toast(`Project ${created.name} created`); go(routes.project(created.name)); }
  }

  const test = FILTERS.find(([key]) => key === filter)?.[2] ?? (() => true);
  const shown = useMemo(() => (rows.data ?? []).filter(({ project: p }) => test(p)
    && (!query || `${p.name} ${p.display_name} ${p.description ?? ''}`.toLowerCase().includes(query.toLowerCase()))),
  [rows.data, query, filter]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <QueryView query={rows}>
      {(all) => (
        <>
          <div className="page-head">
            <h1>Projects</h1>
            <div className="actions">
              <button className="btn primary" type="button" data-testid="create-project" onClick={newProject}>New project</button>
            </div>
          </div>
          <p className="sub">Everything the platform runs, grouped by project.</p>
          <div className="toolbar">
            <input type="search" className="grow" placeholder="Filter projects…  ( / )" aria-label="Filter projects" data-search
              data-testid="projects-search" value={query} onChange={(e) => setQuery(e.target.value.trim())} />
            <div className="seg" role="group" aria-label="Status filter">
              {FILTERS.map(([key, label]) => (
                <button key={key} type="button" aria-pressed={key === filter} data-filter={key} onClick={() => setFilter(key)}>{label}</button>))}
            </div>
          </div>
          <div data-testid="projects-list">
            {shown.length ? <div className="grid">{shown.map((r) => <Card key={r.project.id} row={r} />)}</div>
              : <Empty>{all.length ? 'No project matches this filter.' : 'No projects yet. Create the first one with “New project”.'}</Empty>}
          </div>
        </>
      )}
    </QueryView>
  );
}

const Count = ({ label, n }: { label: string; n: number }) => (
  <div data-count={label.toLowerCase()}><b>{n}</b><span>{label}</span></div>
);

function Card({ row: { project, summary: s } }: { row: Row }) {
  return (
    <a className="card" href={routes.project(project.name)} data-testid={`project-${project.name}`}>
      <h2>{project.display_name}<Badge status={project.status} /></h2>
      <div className="muted mono small">{project.name}</div>
      <p className="muted" style={{ minHeight: '2.6em' }}>{project.description || ''}</p>
      {project.status_reason && <div className="alert">{project.status_reason}</div>}
      {s && (
        <div className="counts">
          <Count label="Runs" n={s.runs + s.pipeline_runs} /><Count label="Models" n={s.models} />
          <Count label="Deployments" n={s.deployments} /><Count label="Endpoints" n={s.endpoints} />
          {s.active_rollouts > 0 && <div><b>{s.active_rollouts}</b><span>Rollouts live</span></div>}
        </div>
      )}
    </a>
  );
}
