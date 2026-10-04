import { api, enc, type S } from '../api/client';
import { Badge, Empty, Section, Table, Time } from '../components/bits';
import { useOverlays } from '../components/overlays';
import { describeAction, isRoutine } from '../lib/audit';
import { useCrumbs } from '../lib/chrome';
import { fmtDuration, routes } from '../lib/format';
import { usePlatformHealth } from '../lib/health';
import { roleIn, useMe } from '../lib/me';
import { notificationHref, useNotifications } from '../lib/notifications';
import { useNow } from '../lib/now';
import { QueryView, useLiveQuery } from '../lib/query';
import { useRunPipeline } from './Pipelines';
import { useNewProject } from './Projects';
import { StatusChip } from './Monitor';

type Workspace = {
  project: S['ProjectOut']; summary: S['SummaryOut']; models: S['ModelOut'][];
  deployments: S['DeploymentOut'][]; pipelines: S['PipelineRunSummary'][]; jobs: S['RunOut'][];
  audit: S['AuditEventOut'][];
};
type Snapshot = { projects: S['ProjectOut'][]; workspaces: Workspace[]; errors: { project: string; message: string }[] };
const activeFilter = 'status=PENDING&status=SUBMITTED&status=RUNNING&limit=200';

async function loadHome(): Promise<Snapshot> {
  const { items: projects } = await api.get<S['ProjectList']>('/projects?limit=200');
  const results = await Promise.allSettled(projects.map(async (project): Promise<Workspace> => {
    const base = `/projects/${enc(project.name)}`;
    const [summary, models, deployments, pipelines, jobs, audit] = await Promise.all([
      api.get<S['SummaryOut']>(`${base}/summary`), api.get<S['ModelList']>(`${base}/models`),
      api.get<S['DeploymentList']>(`${base}/deployments`),
      api.get<S['PipelineRunList']>(`${base}/pipeline-runs?${activeFilter}`), api.get<S['RunList']>(`${base}/runs?${activeFilter}`),
      api.get<S['AuditOut']>(`${base}/audit?limit=25`),
    ]);
    return { project, summary, models: models.items, deployments: deployments.items, pipelines: pipelines.items, jobs: jobs.items, audit: audit.items };
  }));
  return {
    projects,
    workspaces: results.flatMap((result) => result.status === 'fulfilled' ? [result.value] : []),
    errors: results.flatMap((result, i) => result.status === 'rejected' ? [{ project: projects[i]!.display_name, message: result.reason instanceof Error ? result.reason.message : 'Could not load project overview' }] : []),
  };
}

/** The landing page answers what is happening across the visible project fleet. */
export function HomePage() {
  useCrumbs([{ label: 'Home' }]);
  const query = useLiveQuery(['home'], loadHome, () => true, 15_000);
  const health = usePlatformHealth(60);
  const inbox = useNotifications();
  const now = useNow();
  const me = useMe();
  const newProject = useNewProject();
  const runPipeline = useRunPipeline('');
  const { toast } = useOverlays();

  async function startPipeline(projects: S['ProjectOut'][]) {
    const eligible = projects.filter((p) => p.status === 'READY' && ['operator', 'admin'].includes(roleIn(me.data, p.name) ?? ''));
    if (!eligible.length) { toast('No ready projects where you can run a pipeline', 'bad'); return; }
    const results = await Promise.allSettled(eligible.map(async (project) => {
      const { items } = await api.get<S['PipelineList']>(`/projects/${enc(project.name)}/pipelines`);
      return items.map((pipeline) => ({ name: pipeline.name, version: pipeline.version, project: project.name, projectLabel: project.display_name }));
    }));
    const pipelines = results.flatMap((r) => r.status === 'fulfilled' ? r.value : []);
    const failed = results.flatMap((r, i) => r.status === 'rejected' ? [eligible[i]!.display_name] : []);
    if (failed.length) toast(`Could not load pipelines for ${failed.join(', ')}`, 'bad');
    if (!pipelines.length) { toast('No pipelines are available in your ready projects', 'bad'); return; }
    await runPipeline(pipelines);
  }

  return <QueryView query={query}>{({ projects, workspaces, errors }) => {
    const hour = new Date(now).getHours();
    const greeting = hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
    const checks = health.data?.signals.flatMap((signal) => signal.series.length
      ? signal.series.map((series) => ({ key: `${signal.key}:${series.name}`, name: `${signal.title}${series.name ? ` · ${series.name}` : ''}`, status: series.status }))
      : [{ key: signal.key, name: signal.title, status: signal.status }]) ?? [];
    const alerts = checks.filter((c) => c.status === 'warning' || c.status === 'critical');
    const problems = inbox.data?.items.filter((n) => n.needs_attention) ?? [];
    const running = workspaces.flatMap((w) => [
      ...w.pipelines.map((r) => ({ ...r, project: w.project, name: r.pipeline ?? 'Pipeline run', href: routes.pipelineRun(w.project.name, r.id) })),
      ...w.jobs.map((r) => ({ ...r, project: w.project, name: r.job ?? 'Job run', href: routes.jobRun(w.project.name, r.id) })),
    ]).sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
    const deployments = workspaces.flatMap((w) => w.deployments.map((d) => ({ ...d, project: w.project }))).sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at));
    const activity = workspaces.flatMap((w) => w.audit.filter((e) => !isRoutine(e)).map((e) => ({ ...e, project: w.project }))).sort((a, b) => Date.parse(b.occurred_at) - Date.parse(a.occurred_at));
    const issueCount = (inbox.data?.attention_count ?? problems.length) + alerts.length;
    const healthy = health.data?.available && health.data.status === 'ok' && checks.length > 0 && checks.every((c) => c.status !== 'no_data') && !health.isError && errors.length === 0 && !query.isError && Boolean(inbox.data) && !inbox.isError;
    const tiles: [string, number | string, string][] = [
      ['Projects', projects.length, '#/projects'], ['Active runs', health.data?.inventory.runs_active ?? running.length, '#/runs'],
      ['Models', workspaces.reduce((n, w) => n + w.models.filter((m) => m.kind !== 'function').length, 0), '#/models'],
      ['Deployments', workspaces.reduce((n, w) => n + w.summary.deployments, 0), '#/deployments'],
      ['Endpoints', workspaces.reduce((n, w) => n + w.summary.endpoints, 0), '#/endpoints'],
      ['Alerts', health.data?.available && !health.isError ? alerts.length : '—', '#/monitor'],
    ];
    return <>
      <div className="page-head"><h1>Home</h1>{health.data && <StatusChip status={health.data.status} />}</div>
      <p className="home-greeting">{greeting}</p><p className="sub">{issueCount ? `${issueCount} items need attention.` : healthy ? 'Everything looks healthy.' : 'Workload overview. Platform health or project data is incomplete.'}</p>
      {errors.map((e) => <div className="alert" key={e.project}>{e.project}: {e.message}. Totals below cover the projects that loaded.</div>)}
      {health.isError && <div className="alert">Platform health could not be refreshed: {health.error.message}</div>}
      {inbox.isError && <div className="alert">Attention items could not be refreshed: {inbox.error.message}</div>}
      <div className="tiles" data-testid="home-totals">{tiles.map(([label, count, href]) => <a className="tile" key={label} href={href} data-count={label.toLowerCase().replace(/ /g, '-')}><div className="n">{count}</div><div className="l">{label}</div></a>)}</div>
      <Section title="Needs attention" testid="home-attention">
        {problems.length || alerts.length ? <ul className="timeline">{problems.slice(0, 8).map((p) => <li key={p.id}><a href={notificationHref(p)}>{p.title}</a><a className="muted" href={routes.project(p.project)}>{p.project_label}</a><Badge status={p.status} />{p.reason && <span className="muted small">{p.reason}</span>}</li>)}{alerts.map((a) => <li key={a.key}><a href="#/monitor">{a.name}</a><StatusChip status={a.status} /></li>)}</ul> : <Empty>{healthy ? 'No issues need attention.' : inbox.isPending ? 'Loading attention items…' : !inbox.data || inbox.isError ? 'Attention data is unavailable. Review the loading or error state.' : 'No workload issues reported. Platform health is not fully available.'}</Empty>}
      </Section>
      <Section title="Currently running" testid="home-running" more={['#/runs', 'All runs']}>
        {running.length ? <Table head={['Run', 'Project', 'Status', 'Elapsed']}><>{running.slice(0, 10).map((r) => <tr key={r.id}><td><a href={r.href}>{r.name}</a></td><td><a href={routes.project(r.project.name)}>{r.project.display_name}</a></td><td><Badge status={r.status} /></td><td>{r.started_at ? fmtDuration(Math.max(0, (now - Date.parse(r.started_at)) / 1000)) : 'Waiting to start'}</td></tr>)}</></Table> : <Empty>No active runs.</Empty>}
      </Section>
      <Section title="Recent deployments" testid="home-deployments" more={['#/deployments', 'All deployments']}>
        {deployments.length ? <Table head={['Deployment', 'Serving', 'Project', 'Status', 'Updated']}><>{deployments.slice(0, 5).map((d) => {
          const revision = d.revisions.find((r) => r.revision === d.active_revision);
          return <tr key={d.id}><td><a href={routes.deployment(d.project.name, d.name)}>{d.name}</a></td><td>{revision ? `${revision.model}:v${revision.model_version}` : 'Not serving'}</td><td><a href={routes.project(d.project.name)}>{d.project.display_name}</a></td><td><Badge status={d.status} /></td><td><Time iso={d.updated_at} /></td></tr>;
        })}</></Table> : <Empty>No deployments yet.</Empty>}
      </Section>
      <Section title="Recent activity" testid="home-activity" more={['#/activity', 'All activity']}>
        {activity.length ? <ul className="timeline">{activity.slice(0, 8).map((e) => <li key={e.id}><Time iso={e.occurred_at} /><a href={`${routes.project(e.project.name)}/activity`}>{describeAction(e.action)}</a><span className="chip">{e.project.display_name}</span><span className="muted small">{e.actor}</span></li>)}</ul> : <Empty>No activity recorded yet.</Empty>}
      </Section>
      <div className="home-actions"><button className="btn primary" data-testid="create-project" type="button" onClick={newProject}>New project</button><button className="btn" data-testid="home-run-pipeline" type="button" disabled={!me.data || !projects.some((p) => p.status === 'READY' && ['operator', 'admin'].includes(roleIn(me.data, p.name) ?? ''))} onClick={() => startPipeline(projects)}>Run pipeline</button></div>
    </>;
  }}</QueryView>;
}
