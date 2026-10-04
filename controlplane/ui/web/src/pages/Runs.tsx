import { api, enc, type S } from '../api/client';
import { Badge, Empty, Pager, Table, Time } from '../components/bits';
import { useCrumbs } from '../lib/chrome';
import { fmtDuration, go, routes, shortId } from '../lib/format';
import { QueryView, useLiveQuery } from '../lib/query';
import { useSearchState } from '../lib/search';

const ACTIVE = new Set(['PENDING', 'SUBMITTED', 'RUNNING']);
const LIMIT = 25;
export const STATUS_FILTERS: [string, string, string[]][] = [
  ['', 'Any status', []],
  ['active', 'In progress', ['PENDING', 'SUBMITTED', 'RUNNING']],
  ['failed', 'Failed', ['FAILED']],
  ['succeeded', 'Succeeded', ['SUCCEEDED']],
  ['cancelled', 'Cancelled', ['CANCELLED']],
];
export const statusQuery = (key: string) =>
  (STATUS_FILTERS.find(([k]) => k === key)?.[2] ?? []).map((s) => `&status=${s}`).join('');

/** Every run of the project, newest first: filter by kind, pipeline or job and status; page through. */
export function RunsPage({ project }: { project: string }) {
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) }, { label: 'Runs' }]);
  const p = enc(project);
  const [{ kind, name, status, offset: offsetRaw }, update] = useSearchState({ kind: 'pipeline', name: '', status: '', offset: '0' });
  const offset = Number(offsetRaw) || 0;

  const options = useLiveQuery(['run-filter-options', project], async () => {
    const [pipelines, jobs] = await Promise.all([
      api.get<S['PipelineList']>(`/projects/${p}/pipelines`), api.get<S['JobList']>(`/projects/${p}/jobs`)]);
    return { pipelines: pipelines.items.map((x) => x.name), jobs: jobs.items.map((x) => x.name) };
  });
  const query = useLiveQuery(['runs', project, kind, name, status, offset], async () => {
    const filter = `${name ? `&${kind === 'job' ? 'job' : 'pipeline'}=${enc(name)}` : ''}${statusQuery(status)}`;
    if (kind === 'job') {
      const { items } = await api.get<S['RunList']>(`/projects/${p}/runs?limit=${LIMIT}&offset=${offset}${filter}`);
      return { kind: 'job' as const, items };
    }
    const { items } = await api.get<S['PipelineRunList']>(`/projects/${p}/pipeline-runs?limit=${LIMIT}&offset=${offset}${filter}`);
    return { kind: 'pipeline' as const, items };
  }, (d) => d.items.some((r) => ACTIVE.has(r.status)));

  const names = kind === 'job' ? options.data?.jobs ?? [] : options.data?.pipelines ?? [];

  return (
    <>
      <div className="page-head"><h1>Runs</h1></div>
      <p className="sub">Every run in this project, newest first. Filters are kept in the address, so a view can be shared.</p>
      <div className="toolbar">
        <div className="seg" role="group" aria-label="Kind of run">
          {[['pipeline', 'Pipeline runs'], ['job', 'Job runs']].map(([key, label]) => (
            <button key={key} type="button" aria-pressed={kind === key} data-kind={key}
              onClick={() => update({ kind: key!, name: '', offset: '0' })}>{label}</button>))}
        </div>
        <label className="inline">{kind === 'job' ? 'Job' : 'Pipeline'}
          <select value={name} data-testid="run-name-filter" onChange={(e) => update({ name: e.target.value, offset: '0' })}>
            <option value="">All</option>
            {names.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <label className="inline">Status
          <select value={status} data-testid="run-status-filter" onChange={(e) => update({ status: e.target.value, offset: '0' })}>
            {STATUS_FILTERS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
          </select>
        </label>
      </div>
      <QueryView query={query}>
        {(d) => (
          <>
            {d.items.length === 0 ? <Empty actions={<>{name || status || offset ? <button className="btn" type="button" onClick={() => update({ name: '', status: '', offset: '0' })}>Clear filters</button> : <a className="btn" href={`${routes.project(project)}/${kind === 'job' ? 'jobs' : 'pipelines'}`}>Open {kind === 'job' ? 'jobs' : 'pipelines'}</a>}<a href="#/help?topic=guides">Workflow guide</a></>}>{name || status ? 'No run matches these filters.' : 'No runs yet.'}</Empty>
              : d.kind === 'pipeline' ? <PipelineRunTable project={project} rows={d.items} />
              : <JobRunTable project={project} rows={d.items} />}
            <Pager offset={offset} limit={LIMIT} count={d.items.length} onChange={(o) => update({ offset: String(o) })} />
          </>)}
      </QueryView>
    </>
  );
}

export function PipelineRunTable({ project, rows }: { project: string; rows: S['PipelineRunSummary'][] }) {
  return (
    <Table head={['Pipeline', 'Run', 'Commit', 'Status', 'Started', 'Duration']}>
      {rows.map((r) => (
        <tr key={r.id} className="click" data-testid="pipeline-run-row" onClick={() => go(routes.pipelineRun(project, r.id))}>
          <td><a href={routes.pipelineRun(project, r.id)}>{`${r.pipeline || 'pipeline'} v${r.pipeline_version ?? '?'}`}</a></td>
          <td className="mono">{shortId(r.id)}</td><td className="mono">{r.commit_sha || '—'}</td>
          <td><Badge status={r.status} />{r.status === 'FAILED' && r.status_reason && <div className="muted small clip" title={r.status_reason}>{r.status_reason}</div>}</td>
          <td><Time iso={r.started_at || r.created_at} /></td><td>{fmtDuration(r.duration_seconds)}</td>
        </tr>))}
    </Table>
  );
}

export function JobRunTable({ project, rows }: { project: string; rows: S['RunOut'][] }) {
  return (
    <Table head={['Job', 'Run', 'Status', 'Exit', 'Started', 'Duration']}>
      {rows.map((r) => (
        <tr key={r.id} className="click" data-testid="job-run-row" onClick={() => go(routes.jobRun(project, r.id))}>
          <td><a href={routes.jobRun(project, r.id)}>{r.job || 'job'}</a>{r.retry_of && <span className="muted small"> · retry</span>}</td>
          <td className="mono">{shortId(r.id)}</td>
          <td><Badge status={r.status} />{r.status === 'FAILED' && r.status_reason && <div className="muted small clip" title={r.status_reason}>{r.status_reason}</div>}</td>
          <td className="mono">{r.exit_code ?? '—'}</td>
          <td><Time iso={r.started_at || r.created_at} /></td><td>{fmtDuration(r.duration_seconds)}</td>
        </tr>))}
    </Table>
  );
}
