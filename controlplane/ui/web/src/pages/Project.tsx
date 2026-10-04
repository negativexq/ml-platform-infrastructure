import { api, ApiError, enc, type S } from '../api/client';
import { Badge, CopyButton, Empty, Section, Table, Time } from '../components/bits';
import { useOverlays } from '../components/overlays';
import { describeAction, isRoutine } from '../lib/audit';
import { useAccess } from '../lib/me';
import { useCrumbs } from '../lib/chrome';
import { go, routes } from '../lib/format';
import { functionStatus, servingFor } from '../lib/resources';
import { resourceType } from '../lib/navigation';
import { QueryView, useLiveQuery } from '../lib/query';
import { useRunPipeline } from './Pipelines';
import { JobRunTable, PipelineRunTable } from './Runs';

const ACTIVE = new Set(['PENDING', 'SUBMITTED', 'RUNNING', 'PROGRESSING', 'DEPLOYING']);
const RECENT = 5;
const DAY = 24 * 3600 * 1000;

/** The project at a glance: what needs attention, what ran lately, what is serving. */
export function ProjectPage({ name }: { name: string }) {
  const access = useAccess(name);
  const p = enc(name);
  const { form, toast } = useOverlays();
  const runPipeline = useRunPipeline(name);
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: name }]);

  const query = useLiveQuery(['project', name], async () => {
    const [projects, summary, pruns, runs, models, deployments, audit, failedPipelines, failedJobs] = await Promise.all([
      api.get<S['ProjectList']>('/projects?limit=200'),
      api.get<S['SummaryOut']>(`/projects/${p}/summary`),
      api.get<S['PipelineRunList']>(`/projects/${p}/pipeline-runs?limit=${RECENT}`),
      api.get<S['RunList']>(`/projects/${p}/runs?limit=${RECENT}`),
      api.get<S['ModelList']>(`/projects/${p}/models`),
      api.get<S['DeploymentList']>(`/projects/${p}/deployments`),
      api.get<S['AuditOut']>(`/projects/${p}/audit?limit=60`).catch(() => ({ items: [] as S['AuditEventOut'][] })),
      // Failures of the last day, so a problem is visible before anyone goes looking for it.
      api.get<S['PipelineRunList']>(`/projects/${p}/pipeline-runs?status=FAILED&limit=10`),
      api.get<S['RunList']>(`/projects/${p}/runs?status=FAILED&limit=10`),
    ]);
    const versions = Object.fromEntries(await Promise.all(models.items.map(async (model) => {
      const list = await api.get<S['VersionList']>(`/projects/${p}/models/${enc(model.name)}/versions`);
      return [model.id, list.items];
    })));
    const project = projects.items.find((x) => x.name === name);
    if (!project) throw new ApiError(404, 'not_found', `project ${name}`);
    const recent = (r: { created_at: string }) => Date.now() - Date.parse(r.created_at) < DAY;
    return {
      project, summary, versions, pruns: pruns.items, runs: runs.items, models: models.items, deployments: deployments.items,
      audit: audit.items.filter((e) => !isRoutine(e)).slice(0, 8), // key events, as on the Activity page
      failed: { pipelines: failedPipelines.items.filter(recent), jobs: failedJobs.items.filter(recent) },
    };
  }, (d) => d.pruns.some((r) => ACTIVE.has(r.status)) || d.runs.some((r) => ACTIVE.has(r.status))
    || d.deployments.some((x) => x.status === 'DEPLOYING') || d.summary.active_rollouts > 0 || ['PENDING', 'PROVISIONING'].includes(d.project.status));

  async function startRun() {
    const { items } = await api.get<S['PipelineList']>(`/projects/${p}/pipelines`);
    if (!items.length) { toast('This project has no pipelines yet', 'bad'); return; }
    await runPipeline(items);
  }

  async function startJob() {
    const { items } = await api.get<S['JobList']>(`/projects/${p}/jobs`);
    if (!items.length) { toast('This project has no jobs yet', 'bad'); return; }
    const run = await form<S['RunOut']>({
      title: 'Start a job', submitLabel: 'Start job',
      fields: [{ name: 'job', label: 'Job', required: true, options: items.map((j) => ({ value: j.name, label: j.name })) }],
      submit: (v) => {
        const job = v.job ?? '';
        return api.post<S['RunOut']>(`/projects/${p}/jobs/${enc(job)}/runs`, {});
      },
    });
    if (run) { toast('Job started'); go(routes.jobRun(name, run.id)); }
  }

  return (
    <QueryView query={query}>
      {(d) => {
        const s = d.summary;
        const ready = d.project.status === 'READY';
        const base = routes.project(name);
        return (
          <>
            <div className="page-head">
              <h1>{d.project.display_name}</h1><Badge status={d.project.status} />
              <div className="actions">
                <button className="btn primary" type="button" data-testid="run-pipeline" disabled={!ready || !access.may('operator')}
                  title={access.why('operator') ?? (ready ? '' : 'The project is not ready yet')} onClick={startRun}>Run pipeline</button>
                <button className="btn" type="button" data-testid="start-job" disabled={!ready || !access.may('operator')} title={access.why('operator')} onClick={startJob}>Start job</button>
              </div>
            </div>
            <p className="sub">{d.project.description || d.project.name}</p>
            {d.project.status_reason && <div className="alert">{d.project.status_reason}</div>}
            <div className="tiles">
              <Tile n={s.pipelines} label="Pipelines" href={`${base}/pipelines`} /><Tile n={s.pipeline_runs + s.runs} label="Runs" href={`${base}/runs`} />
              <Tile n={`${d.models.filter((m) => m.kind !== 'function' && m.champion).length}/${d.models.filter((m) => m.kind !== 'function').length}`} label="Models with champion" href={`${base}/models`} />
              <Tile n={d.models.filter((m) => m.kind === 'function').length} label="Functions" href={`${base}/functions`} />
              <Tile n={`${s.deployments_ready}/${s.deployments}`} label="Deployments ready" href={`${base}/deployments`} />
              <Tile n={s.endpoints} label="Endpoints" href={`${base}/endpoints`} /><Tile n={s.active_rollouts} label="Rollouts in progress" href={`${base}/deployments`} />
            </div>
            <Attention project={name} failed={d.failed} deployments={d.deployments} />
            <Section title="Recent pipeline runs" testid="pipeline-runs" more={[`${base}/runs`, 'All runs']}>
              {d.pruns.length ? <PipelineRunTable project={name} rows={d.pruns} /> : <Empty>No pipeline runs yet.</Empty>}
            </Section>
            <Section title="Recent job runs" testid="job-runs" more={[`${base}/runs?kind=job`, 'All job runs']}>
              {d.runs.length ? <JobRunTable project={name} rows={d.runs} /> : <Empty>No job runs yet.</Empty>}
            </Section>
            <div className="cols">
              <Section title="Resources" testid="models" more={[`${base}/models`, 'Models']}><Resources project={name} rows={d.models} versions={d.versions} deployments={d.deployments} /></Section>
              <Section title="Deployments" testid="deployments" more={[`${base}/deployments`, 'Deployments']}><Deployments project={name} rows={d.deployments} /></Section>
            </div>
            <Section title="Recent activity" testid="activity" more={[`${base}/activity`, 'All activity']}><Activity events={d.audit} /></Section>
          </>
        );
      }}
    </QueryView>
  );
}

const Tile = ({ n, label, href }: { n: number | string; label: string; href: string }) => (
  <a className="tile" href={href}><div className="n">{n}</div><div className="l">{label}</div></a>
);

/** What needs a human, right now: recent failures and anything not serving as it should. */
function Attention({ project, failed, deployments }: {
  project: string; failed: { pipelines: S['PipelineRunSummary'][]; jobs: S['RunOut'][] }; deployments: S['DeploymentOut'][];
}) {
  const unhealthy = deployments.filter((d) => ['FAILED', 'DEGRADED', 'DRIFTED'].includes(d.status) || d.endpoint.status === 'UNAVAILABLE');
  const items = [
    ...failed.pipelines.map((r) => ({ key: r.id, href: routes.pipelineRun(project, r.id), what: `${r.pipeline ?? 'pipeline'} run failed`, why: r.status_reason, at: r.created_at })),
    ...failed.jobs.map((r) => ({ key: r.id, href: routes.jobRun(project, r.id), what: `${r.job ?? 'job'} run failed`, why: r.status_reason, at: r.created_at })),
    ...unhealthy.map((d) => ({ key: d.id, href: routes.deployment(project, d.name), what: `${d.name} is ${d.status.toLowerCase()}`, why: d.status_reason, at: d.updated_at })),
  ].sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  if (!items.length) return null;
  return (
    <div className="section card attention" data-testid="attention" role="region" aria-label="Needs attention">
      <h2>Needs attention <span className="count">{items.length}</span></h2>
      <ul className="timeline">
        {items.map((i) => (
          <li key={i.key}><Time iso={i.at} /><a href={i.href}>{i.what}</a>{i.why && <span className="muted small clip" title={i.why}>{i.why}</span>}</li>))}
      </ul>
    </div>
  );
}

function Resources({ project, rows, versions, deployments }: {
  project: string; rows: S['ModelOut'][]; versions: Record<string, S['ModelVersionSummary'][]>; deployments: S['DeploymentOut'][];
}) {
  if (!rows.length) return <Empty>No resources yet.</Empty>;
  return <Table head={['Resource', 'Type', 'Version', 'Status']}>
    {rows.map((model) => {
      const serving = servingFor(model, deployments);
      const latest = [...(versions[model.id] ?? [])].sort((a, b) => b.version - a.version)[0];
      const version = model.kind === 'function' ? serving.length ? Math.max(...serving.map((s) => s.revision.model_version)) : latest?.version : model.champion?.version ?? latest?.version;
      const status = model.kind === 'function' ? functionStatus(serving, latest?.status ?? 'REGISTERED') : model.champion ? 'CHAMPION' : latest?.status ?? 'REGISTERED';
      const href = model.kind === 'function' ? routes.function(project, model.name) : routes.model(project, model.name);
      return <tr key={model.id} className="click" data-testid="model-row" onClick={() => go(href)}>
        <td><a href={href}>{model.name}</a></td><td>{resourceType(model.kind)}</td>
        <td>{version == null ? '—' : `v${version}`}</td><td><Badge status={model.kind === 'function' && status === 'READY' ? 'DEPLOYED' : status} />{model.alias_drift && <Badge status="DRIFTED" />}</td>
      </tr>;
    })}
  </Table>;
}

function Deployments({ project, rows }: { project: string; rows: S['DeploymentOut'][] }) {
  if (!rows.length) return <Empty>No deployments yet.</Empty>;
  return (
    <Table head={['Deployment', 'Status', 'Revision', 'Endpoint']}>
      {rows.map((d) => (
        <tr key={d.id} className="click" data-testid="deployment-row" onClick={() => go(routes.deployment(project, d.name))}>
          <td><a href={routes.deployment(project, d.name)}>{d.name}</a></td><td><Badge status={d.status} /></td>
          <td className="mono">{d.active_revision != null ? `r${d.active_revision}` : '—'}</td><td><Badge status={d.endpoint.status} /></td>
        </tr>))}
    </Table>
  );
}

function Activity({ events }: { events: S['AuditEventOut'][] }) {
  if (!events.length) return <Empty>No activity recorded yet.</Empty>;
  return (
    <ul className="timeline">
      {events.map((e) => (
        <li key={e.id}>
          <Time iso={e.occurred_at} />
          <span>{describeAction(e.action)}</span>
          <span className="muted small">{e.actor}</span>
          {e.trace_id && <span className="trace" title="Trace id: look it up in your tracing tool">{e.trace_id.slice(0, 8)}<CopyButton text={e.trace_id} what="trace id" /></span>}
        </li>))}
    </ul>
  );
}
