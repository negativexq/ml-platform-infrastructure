import { useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { api, enc, type S } from '../api/client';
import { Badge, Empty, Time } from '../components/bits';
import { useOverlays } from '../components/overlays';
import { useCrumbs } from '../lib/chrome';
import { go, routes } from '../lib/format';
import { PROJECT_NAV } from '../lib/navigation';
import { useSearchState } from '../lib/search';
import { QueryView, useLiveQuery } from '../lib/query';

type Problem = { key: string; project: string; href: string; what: string; why?: string | null; at: string };
type Row = { project: S['ProjectOut']; summary: S['SummaryOut'] | null; problems: Problem[] };
const DAY = 24 * 3600 * 1000;

/** The last day's failures and anything not serving well, for one project. */
async function problemsOf(p: S['ProjectOut']): Promise<Problem[]> {
  if (p.status !== 'READY' && p.status !== 'DRIFTED') return [];
  const base = `/projects/${enc(p.name)}`;
  const [pruns, runs, deployments] = await Promise.all([
    api.get<S['PipelineRunList']>(`${base}/pipeline-runs?status=FAILED&limit=5`).catch(() => ({ items: [] as S['PipelineRunSummary'][] })),
    api.get<S['RunList']>(`${base}/runs?status=FAILED&limit=5`).catch(() => ({ items: [] as S['RunOut'][] })),
    api.get<S['DeploymentList']>(`${base}/deployments`).catch(() => ({ items: [] as S['DeploymentOut'][] })),
  ]);
  const recent = (iso: string) => Date.now() - Date.parse(iso) < DAY;
  return [
    ...pruns.items.filter((r) => recent(r.created_at)).map((r) => ({ key: r.id, project: p.name, href: routes.pipelineRun(p.name, r.id), what: `${r.pipeline ?? 'pipeline'} run failed`, why: r.status_reason, at: r.created_at })),
    ...runs.items.filter((r) => recent(r.created_at)).map((r) => ({ key: r.id, project: p.name, href: routes.jobRun(p.name, r.id), what: `${r.job ?? 'job'} run failed`, why: r.status_reason, at: r.created_at })),
    ...deployments.items.filter((d) => ['FAILED', 'DEGRADED', 'DRIFTED'].includes(d.status) || d.endpoint.status === 'UNAVAILABLE')
      .map((d) => ({ key: d.id, project: p.name, href: routes.deployment(p.name, d.name), what: `${d.name} is ${d.status.toLowerCase()}`, why: d.status_reason, at: d.updated_at })),
  ];
}

function Attention({ rows }: { rows: Row[] }) {
  const items = rows.flatMap((r) => r.problems).sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  const rollouts = rows.filter((r) => (r.summary?.active_rollouts ?? 0) > 0);
  if (!items.length && !rollouts.length) return null;
  return (
    <div className="card attention section-gap project-attention" data-testid="attention" role="region" aria-label="Needs attention">
      <h2>{items.length ? 'Needs attention' : 'In progress'} {items.length > 0 && <span className="count">{items.length}</span>}</h2>
      <ul className="timeline">
        {items.slice(0, 8).map((i) => (
          <li key={i.key}><Time iso={i.at} /><span className="chip">{i.project}</span><a href={i.href}>{i.what}</a>
            {i.why && <span className="muted small clip" title={i.why}>{i.why}</span>}</li>))}
        {rollouts.map((r) => (
          <li key={`ro-${r.project.id}`}><span className="muted small">live</span><span className="chip">{r.project.name}</span>
            <a href={`${routes.project(r.project.name)}/deployments`}>{`${r.summary?.active_rollouts} canary rollout${r.summary?.active_rollouts === 1 ? '' : 's'} in progress`}</a></li>))}
      </ul>
    </div>
  );
}

const FILTERS: [string, string, (p: S['ProjectOut']) => boolean][] = [
  ['all', 'All', () => true],
  ['attention', 'Needs attention', (p) => ['FAILED', 'DRIFTED', 'DELETING'].includes(p.status)],  // or has problems, see below
  ['ready', 'Ready', (p) => p.status === 'READY'],
];
const BUSY = ['PENDING', 'PROVISIONING', 'DRIFTED', 'DELETING'];

export function ProjectsPage() {
  useCrumbs([{ label: 'Projects' }]);
  const newProject = useNewProject();
  const [{ section: requestedSection }] = useSearchState({ section: '' });
  const validSections = PROJECT_NAV.flatMap((g) => g.items ? g.items.map(([key]) => key) : [g.key]);
  const section = validSections.includes(requestedSection) ? requestedSection : '';
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('all');

  const rows = useLiveQuery<Row[]>(['projects'], async () => {
    const { items } = await api.get<S['ProjectList']>('/projects?limit=200');
    const [summaries, problems] = await Promise.all([
      Promise.all(items.map((p) => api.get<S['SummaryOut']>(`/projects/${enc(p.name)}/summary`).catch(() => null))),
      Promise.all(items.map(problemsOf)),
    ]);
    return items.map((project, i) => ({ project, summary: summaries[i] ?? null, problems: problems[i] ?? [] }));
  }, (data) => data.some((r) => BUSY.includes(r.project.status)));

  const test = FILTERS.find(([key]) => key === filter)?.[2] ?? (() => true);
  const shown = useMemo(() => (rows.data ?? []).filter(({ project: p, problems }) => (test(p) || (filter === 'attention' && problems.length > 0))
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
          <p className="sub">{section ? `Choose a project to continue in ${section}.` : 'Everything the platform runs, grouped by project.'}</p>
          <Attention rows={all} />
          <div className="toolbar">
            <input type="search" className="grow" placeholder="Filter projects…  ( / )" aria-label="Filter projects" data-search
              data-testid="projects-search" value={query} onChange={(e) => setQuery(e.target.value.trim())} />
            <div className="seg" role="group" aria-label="Status filter">
              {FILTERS.map(([key, label]) => (
                <button key={key} type="button" aria-pressed={key === filter} data-filter={key} onClick={() => setFilter(key)}>{label}</button>))}
            </div>
          </div>
          <div data-testid="projects-list">
            {shown.length ? <div className="grid">{shown.map((r) => <Card key={r.project.id} row={r} section={section} />)}</div>
              : <Empty actions={<>{all.length ? <button className="btn" type="button" onClick={() => { setQuery(''); setFilter('all'); }}>Clear filters</button> : <button className="btn primary" type="button" onClick={newProject}>New project</button>}<a href="#/help?topic=start">Getting started</a></>}>{all.length ? 'No project matches this filter.' : 'No projects yet. Create the first one with “New project”.'}</Empty>}
          </div>
        </>
      )}
    </QueryView>
  );
}

/** Shared project creation action for the directory and Home. */
export function useNewProject() {
  const { form, toast } = useOverlays();
  const client = useQueryClient();
  return async function newProject() {
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
    if (created) {
      await client.invalidateQueries({ queryKey: ['me'] }); // the creator is the new project's admin
      toast(`Project ${created.name} created`);
      go(routes.project(created.name));
    }
  }

}

const Count = ({ label, n }: { label: string; n: number }) => (
  <div data-count={label.toLowerCase()}><b>{n}</b><span>{label}</span></div>
);

function Card({ row: { project, summary: s, problems }, section }: { row: Row; section: string }) {
  return (
    <a className="card" href={`${routes.project(project.name)}${section ? `/${section}` : ''}`} data-testid={`project-${project.name}`}>
      <h2>{project.display_name}<Badge status={project.status} />{problems.length > 0 && <span className="count bad" title="Needs attention">{problems.length}</span>}</h2>
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
