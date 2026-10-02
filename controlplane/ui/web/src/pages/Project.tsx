import { useState } from 'react';
import { api, ApiError, enc, type S } from '../api/client';
import { Badge, CopyButton, Empty, Section, Table, Time } from '../components/bits';
import { useOverlays } from '../components/overlays';
import { useCrumbs } from '../lib/chrome';
import { fmtDuration, go, routes, shortId } from '../lib/format';
import { QueryView, useLiveQuery } from '../lib/query';

const ACTIVE = new Set(['PENDING', 'SUBMITTED', 'RUNNING', 'PROGRESSING', 'DEPLOYING']);
const PAGE = 8;
const RUN_FILTERS: [string, string, (r: { status: string }) => boolean][] = [
  ['all', 'All', () => true],
  ['active', 'Active', (r) => ACTIVE.has(r.status)],
  ['failed', 'Failed', (r) => r.status === 'FAILED'],
];

export function ProjectPage({ name }: { name: string }) {
  const p = enc(name);
  const { form, toast } = useOverlays();
  const [limit, setLimit] = useState(PAGE);
  const [runFilter, setRunFilter] = useState('all');
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: name }]);

  const query = useLiveQuery(['project', name, limit], async () => {
    const [projects, summary, pruns, runs, models, deployments, audit] = await Promise.all([
      api.get<S['ProjectList']>('/projects?limit=200'),
      api.get<S['SummaryOut']>(`/projects/${p}/summary`),
      api.get<S['PipelineRunList']>(`/projects/${p}/pipeline-runs?limit=${limit}`),
      api.get<S['RunList']>(`/projects/${p}/runs?limit=${limit}`),
      api.get<S['ModelList']>(`/projects/${p}/models`),
      api.get<S['DeploymentList']>(`/projects/${p}/deployments`),
      api.get<S['AuditOut']>(`/projects/${p}/audit?limit=15`).catch(() => ({ items: [] as S['AuditEventOut'][] })),
    ]);
    const project = projects.items.find((x) => x.name === name);
    if (!project) throw new ApiError(404, 'not_found', `project ${name}`);
    return { project, summary, pruns: pruns.items, runs: runs.items, models: models.items, deployments: deployments.items, audit: audit.items };
  }, (d) => d.pruns.some((r) => ACTIVE.has(r.status)) || d.runs.some((r) => ACTIVE.has(r.status))
    || d.deployments.some((x) => x.status === 'DEPLOYING') || d.summary.active_rollouts > 0);

  async function runPipeline() {
    const { items } = await api.get<S['PipelineList']>(`/projects/${p}/pipelines`);
    if (!items.length) { toast('This project has no pipelines yet', 'bad'); return; }
    const run = await form<S['PipelineRunOut']>({
      title: 'Run a pipeline', submitLabel: 'Start run',
      intro: 'Runs the latest version of the pipeline. You can follow it live on the next page.',
      fields: [
        { name: 'pipeline', label: 'Pipeline', required: true, options: items.map((x) => ({ value: x.name, label: `${x.name} (v${x.version})` })) },
        { name: 'commit_sha', label: 'Commit', placeholder: 'a83d2c1', hint: 'Optional. Recorded on the run and on every model it registers.' },
      ],
      submit: (v) => {
        const pipeline = v.pipeline ?? '';
        return api.post<S['PipelineRunOut']>(`/projects/${p}/pipelines/${enc(pipeline)}/runs`, v.commit_sha ? { commit_sha: v.commit_sha } : {});
      },
    });
    if (run) { toast('Run started'); go(routes.pipelineRun(name, run.id)); }
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

  const test = RUN_FILTERS.find(([key]) => key === runFilter)?.[2] ?? (() => true);

  return (
    <QueryView query={query}>
      {(d) => {
        const s = d.summary;
        const ready = d.project.status === 'READY';
        const filters = (
          <div className="toolbar">
            <div className="seg" role="group" aria-label="Run filter">
              {RUN_FILTERS.map(([key, label]) => (
                <button key={key} type="button" aria-pressed={key === runFilter} data-run-filter={key} onClick={() => setRunFilter(key)}>{label}</button>))}
            </div>
          </div>);
        const more = (rows: unknown[]) => rows.length >= limit && (
          <div className="more"><button className="btn" type="button" data-testid="show-more" onClick={() => setLimit(limit + 20)}>Show more</button></div>);
        return (
          <>
            <div className="page-head">
              <h1>{d.project.display_name}</h1><Badge status={d.project.status} />
              <div className="actions">
                <button className="btn primary" type="button" data-testid="run-pipeline" disabled={!ready}
                  title={ready ? '' : 'The project is not ready yet'} onClick={runPipeline}>Run pipeline</button>
                <button className="btn" type="button" data-testid="start-job" disabled={!ready} onClick={startJob}>Start job</button>
              </div>
            </div>
            <p className="sub">{d.project.description || d.project.name}</p>
            {d.project.status_reason && <div className="alert">{d.project.status_reason}</div>}
            <div className="tiles">
              <Tile n={s.pipelines} label="Pipelines" /><Tile n={s.pipeline_runs + s.runs} label="Runs" />
              <Tile n={`${s.champions}/${s.models}`} label="Models with champion" />
              <Tile n={`${s.deployments_ready}/${s.deployments}`} label="Deployments ready" />
              <Tile n={s.endpoints} label="Endpoints" /><Tile n={s.active_rollouts} label="Rollouts in progress" />
            </div>
            <Section title="Pipeline runs" testid="pipeline-runs">
              {filters}
              <PipelineRuns project={name} rows={d.pruns.filter(test)} />
              {more(d.pruns)}
            </Section>
            <Section title="Job runs" testid="job-runs">
              <JobRuns project={name} rows={d.runs.filter(test)} />
              {more(d.runs)}
            </Section>
            <div className="cols">
              <Section title="Models" testid="models"><Models project={name} rows={d.models} /></Section>
              <Section title="Deployments" testid="deployments"><Deployments project={name} rows={d.deployments} /></Section>
            </div>
            <Section title="Recent activity" testid="activity"><Activity events={d.audit} /></Section>
          </>
        );
      }}
    </QueryView>
  );
}

const Tile = ({ n, label }: { n: number | string; label: string }) => (
  <div className="tile"><div className="n">{n}</div><div className="l">{label}</div></div>
);

function PipelineRuns({ project, rows }: { project: string; rows: S['PipelineRunSummary'][] }) {
  if (!rows.length) return <Empty>No pipeline runs yet.</Empty>;
  return (
    <Table head={['Pipeline', 'Run', 'Commit', 'Status', 'Started', 'Duration']}>
      {rows.map((r) => (
        <tr key={r.id} className="click" data-testid="pipeline-run-row" onClick={() => go(routes.pipelineRun(project, r.id))}>
          <td><a href={routes.pipelineRun(project, r.id)}>{`${r.pipeline || 'pipeline'} v${r.pipeline_version ?? '?'}`}</a></td>
          <td className="mono">{shortId(r.id)}</td><td className="mono">{r.commit_sha || '—'}</td>
          <td><Badge status={r.status} /></td><td><Time iso={r.started_at || r.created_at} /></td><td>{fmtDuration(r.duration_seconds)}</td>
        </tr>))}
    </Table>
  );
}

function JobRuns({ project, rows }: { project: string; rows: S['RunOut'][] }) {
  if (!rows.length) return <Empty>No job runs yet.</Empty>;
  return (
    <Table head={['Job', 'Run', 'Status', 'Exit', 'Started', 'Duration']}>
      {rows.map((r) => (
        <tr key={r.id} className="click" data-testid="job-run-row" onClick={() => go(routes.jobRun(project, r.id))}>
          <td><a href={routes.jobRun(project, r.id)}>{r.job || 'job'}</a></td><td className="mono">{shortId(r.id)}</td>
          <td><Badge status={r.status} /></td><td className="mono">{r.exit_code ?? '—'}</td>
          <td><Time iso={r.started_at || r.created_at} /></td><td>{fmtDuration(r.duration_seconds)}</td>
        </tr>))}
    </Table>
  );
}

function Models({ project, rows }: { project: string; rows: S['ModelOut'][] }) {
  if (!rows.length) return <Empty>No models yet.</Empty>;
  return (
    <Table head={['Model', 'Champion', 'Versions', '']}>
      {rows.map((m) => (
        <tr key={m.id} className="click" data-testid="model-row" onClick={() => go(routes.model(project, m.name))}>
          <td><a href={routes.model(project, m.name)}>{m.name}</a></td>
          <td>{m.champion ? <><Badge status="CHAMPION" />{` v${m.champion.version}`}</> : '—'}</td>
          <td className="num">{m.versions}</td><td>{m.alias_drift ? <Badge status="DRIFTED" /> : null}</td>
        </tr>))}
    </Table>
  );
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

const AUDIT_LABELS: Record<string, string> = {
  'project.created': 'Project created', 'project.provisioned': 'Namespace provisioned', 'project.failed': 'Provisioning failed',
  'pipeline_run.created': 'Pipeline run created', 'pipeline_run.submitted': 'Pipeline run submitted', 'pipeline_run.succeeded': 'Pipeline run succeeded',
  'pipeline_run.failed': 'Pipeline run failed', 'run.created': 'Job run created', 'run.succeeded': 'Job run succeeded', 'run.failed': 'Job run failed',
  'deployment.ready': 'Deployment ready', 'deployment.failed': 'Deployment failed', 'deployment.rolled_back': 'Deployment rolled back',
  'rollout.started': 'Rollout started', 'rollout.succeeded': 'Rollout succeeded', 'rollout.rolled_back': 'Rollout rolled back',
};

function Activity({ events }: { events: S['AuditEventOut'][] }) {
  if (!events.length) return <Empty>No activity recorded yet.</Empty>;
  return (
    <ul className="timeline">
      {events.map((e) => (
        <li key={e.id}>
          <Time iso={e.occurred_at} />
          <span>{AUDIT_LABELS[e.action] ?? e.action.replace(/[._]/g, ' ')}</span>
          <span className="muted small">{e.actor}</span>
          {e.trace_id && <span className="trace" title="Trace id: look it up in your tracing tool">{e.trace_id.slice(0, 8)}<CopyButton text={e.trace_id} what="trace id" /></span>}
        </li>))}
    </ul>
  );
}
