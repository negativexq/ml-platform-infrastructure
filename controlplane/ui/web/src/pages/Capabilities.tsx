import { useState } from 'react';
import { api, enc, type S } from '../api/client';
import { Badge, Empty, Table, Time } from '../components/bits';
import { resourceType } from '../lib/navigation';
import { useCrumbs } from '../lib/chrome';
import { describeAction } from '../lib/audit';
import { routes } from '../lib/format';
import { functionStatus, servingFor } from '../lib/resources';
import { useMe } from '../lib/me';
import { QueryView, useLiveQuery } from '../lib/query';

export type Capability = 'runs' | 'pipelines' | 'models' | 'functions' | 'deployments' | 'endpoints' | 'activity';
type Entry = { model?: S['ModelOut']; version?: number; serving?: ReturnType<typeof servingFor>; id: string; name: string; type: string; status?: string; href: string; at?: string };
type Group = { project: S['ProjectOut']; items: Entry[]; error?: string };
const LABELS: Record<Capability, string> = { runs: 'Runs', pipelines: 'Pipelines', models: 'Models', functions: 'Functions', deployments: 'Deployments', endpoints: 'Endpoints', activity: 'Activity' };

async function entries(project: string, capability: Capability): Promise<Entry[]> {
  const base = `/projects/${enc(project)}`;
  const section = `${routes.project(project)}/${capability}`;
  if (capability === 'models' || capability === 'functions') {
    const [models, deployments] = await Promise.all([
      api.get<S['ModelList']>(`${base}/models`), api.get<S['DeploymentList']>(`${base}/deployments`),
    ]);
    return Promise.all(models.items.filter((m) => (m.kind === 'function') === (capability === 'functions')).map(async (model) => {
      const { items: versions } = await api.get<S['VersionList']>(`${base}/models/${enc(model.name)}/versions`);
      const latest = versions.reduce<S['ModelVersionSummary'] | undefined>((last, v) => !last || v.version > last.version ? v : last, undefined);
      const serving = servingFor(model, deployments.items);
      const active = serving.reduce<number | undefined>((last, s) => Math.max(last ?? 0, s.revision.model_version), undefined);
      return {
        id: model.id, name: model.name, type: resourceType(model.kind), model, serving,
        status: model.kind === 'function' ? functionStatus(serving, latest?.status ?? 'REGISTERED') : model.champion ? 'CHAMPION' : latest?.status ?? 'REGISTERED',
        version: active ?? latest?.version,
        href: model.kind === 'function' ? routes.function(project, model.name) : routes.model(project, model.name), at: latest?.updated_at ?? model.created_at,
      };
    }));
  }
  if (capability === 'runs') {
    const [pipelines, jobs] = await Promise.all([
      api.get<S['PipelineRunList']>(`${base}/pipeline-runs?limit=25`), api.get<S['RunList']>(`${base}/runs?limit=25`),
    ]);
    return [
      ...pipelines.items.map((r) => ({ id: r.id, name: r.pipeline ?? r.id, type: 'Pipeline run', status: r.status, href: routes.pipelineRun(project, r.id), at: r.created_at })),
      ...jobs.items.map((r) => ({ id: r.id, name: r.job ?? r.id, type: 'Job run', status: r.status, href: routes.jobRun(project, r.id), at: r.created_at })),
    ].sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  }
  if (capability === 'pipelines') {
    const { items } = await api.get<S['PipelineList']>(`${base}/pipelines`);
    return items.map((r) => ({ id: r.id, name: r.name, type: `Pipeline v${r.version}`, href: `${section}/${enc(r.name)}` }));
  }
  if (capability === 'deployments') {
    const { items } = await api.get<S['DeploymentList']>(`${base}/deployments`);
    return items.map((r) => ({ id: r.id, name: r.name, type: 'Deployment', status: r.status, href: routes.deployment(project, r.name), at: r.updated_at }));
  }
  if (capability === 'endpoints') {
    const { items } = await api.get<S['EndpointListOut']>(`${base}/endpoints`);
    return items.map((r) => ({ id: r.id, name: r.name, type: 'Endpoint', status: r.status, href: routes.endpoint(project, r.name) }));
  }
  const { items } = await api.get<S['AuditOut']>(`${base}/audit?limit=25`);
  return items.map((r) => ({ id: r.id, name: describeAction(r.action), type: r.actor, href: section, at: r.occurred_at }));
}

/** Global discovery across visible projects; actions stay on the existing project pages. */
export function CapabilityPage({ capability }: { capability: Capability }) {
  const label = LABELS[capability];
  useCrumbs([{ label }]);
  const [search, setSearch] = useState('');
  const [project, setProject] = useState('');
  const [kind, setKind] = useState('');
  const [status, setStatus] = useState('');
  const query = useLiveQuery<Group[]>(['capability', capability], async () => {
    const { items } = await api.get<S['ProjectList']>('/projects?limit=200');
    return Promise.all(items.map(async (p) => {
      try { return { project: p, items: await entries(p.name, capability) }; }
      catch (error) { return { project: p, items: [], error: error instanceof Error ? error.message : 'Could not load resources' }; }
    }));
  }, () => true, 15_000);
  return <QueryView query={query}>{(groups) => {
    const rows = groups.filter((g) => !project || g.project.name === project).flatMap((g) => g.items.map((item) => ({ ...item, project: g.project })));
    const shown = rows.filter((r) => (!kind || r.type === kind) && (!status || r.status === status) && `${r.name} ${r.project.display_name} ${r.type}`.toLowerCase().includes(search.toLowerCase()));
    return <>
      <div className="page-head"><h1>{label}</h1></div>
      <p className="sub">{capability === 'runs' || capability === 'activity' ? 'Recent activity across your projects. Open a project for its full history.' : `${label} across your projects. Open a resource to work in its project context.`}</p>
      <div className="toolbar">
        <input className="grow" type="search" aria-label={`Filter ${label.toLowerCase()}`} placeholder={`Filter ${label.toLowerCase()}…`} data-search value={search} onChange={(e) => setSearch(e.target.value)} />
        <select aria-label="Project filter" value={project} onChange={(e) => setProject(e.target.value)}><option value="">All projects</option>{groups.map((g) => <option key={g.project.id} value={g.project.name}>{g.project.display_name}</option>)}</select>
        {capability === 'models' && <select aria-label="Model type" value={kind} onChange={(e) => setKind(e.target.value)}><option value="">All model types</option><option value="ML Model">ML Model</option><option value="LLM">LLM</option></select>}
        <select aria-label="Status filter" value={status} onChange={(e) => setStatus(e.target.value)}><option value="">All statuses</option>{[...new Set(rows.flatMap((r) => r.status ? [r.status] : []))].sort().map((value) => <option key={value} value={value}>{value.toLowerCase().replace(/_/g, ' ')}</option>)}</select>
      </div>
      {groups.filter((g) => g.error && (!project || project === g.project.name)).map((g) => <div className="alert" key={g.project.id}>{g.project.display_name}: {g.error}</div>)}
      {shown.length ? <Table head={capability === 'models' ? ['Name', 'Type', 'Project', 'Champion', 'Versions', 'Serving', 'Status'] : capability === 'functions' ? ['Name', 'Project', 'Version', 'Deployment', 'Status'] : ['Resource', 'Type', 'Project', 'Status', 'Time']} testid="global-resources">{shown.map((r) => <tr key={`${r.project.id}/${r.id}`}>
        <td><a href={r.href}>{r.name}</a></td>
        {capability !== 'functions' && <td>{r.type}</td>}
        <td><a href={`${routes.project(r.project.name)}/${capability}`}>{r.project.display_name}</a></td>
        {capability === 'models' && <><td>{r.model?.champion ? `v${r.model.champion.version}` : '—'}</td><td className="num">{r.model?.versions}</td></>}
        {capability === 'functions' && <td>{r.version == null ? '—' : `v${r.version}`}</td>}
        {(capability === 'models' || capability === 'functions') && <td>{r.serving?.length ? r.serving.map(({ deployment, revision }) => <div key={deployment.id}><a href={routes.deployment(r.project.name, deployment.name)}>{deployment.name}</a> <span className="muted">v{revision.model_version}</span></div>) : <span className="muted">Not deployed</span>}</td>}
        <td>{r.status ? <Badge status={r.status} /> : '—'}</td>
        {capability !== 'models' && capability !== 'functions' && <td><Time iso={r.at} /></td>}
      </tr>)}</Table> : <Empty actions={<>{search || project || kind || status ? <button className="btn" type="button" onClick={() => { setSearch(''); setProject(''); setKind(''); setStatus(''); }}>Clear filters</button> : <a className="btn" href={`#/projects?section=${capability}`}>Choose a project</a>}<a href="#/help?topic=map">Find your next step</a></>}>{search || project || kind || status ? `No ${label.toLowerCase()} match this view.` : groups.some((g) => g.error) ? `Could not load all ${label.toLowerCase()}. Review the errors above.` : `No ${label.toLowerCase()} yet. Choose a project to get started.`}</Empty>}
    </>;
  }}</QueryView>;
}

export function AdminPage({ section }: { section: 'identity' | 'settings' }) {
  const label = section[0]!.toUpperCase() + section.slice(1);
  useCrumbs([{ label }]);
  const me = useMe();
  const projects = useLiveQuery(['projects', 'admin-directory'], () => api.get<S['ProjectList']>('/projects?limit=200'));
  return <><div className="page-head"><h1>{label}</h1></div>
    {section === 'identity' && <><p className="sub">Your identity and project access. Manage project members in their workspace settings.</p>{me.isError && <div className="alert bad">{me.error.message}</div>}{me.data && <div className="card"><h2>{me.data.display_name || me.data.username}</h2><p>{me.data.platform_admin ? 'Platform admin' : 'Project member'}</p><p className="muted">Groups: {me.data.groups.join(', ') || 'None'}</p></div>}</>}
    {section === 'settings' && <p className="sub">Choose a project to manage its members, API keys and settings. Appearance and account controls are in the top bar.</p>}
    <QueryView query={projects}>{({ items }) => <div className="section grid">{items.map((p) => <a key={p.id} className="card" href={`${routes.project(p.name)}/settings`}><h2>{p.display_name}</h2><span className="muted">{section === 'identity' ? 'Members and access' : 'Project settings'}</span></a>)}</div>}</QueryView>
  </>;
}
