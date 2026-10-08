import { MonitoringOutputs } from './ModelMonitoring';
import { BatchOutputs } from './BatchInference';
import { RunScheduleOrigin } from './Schedules';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useRunLogs } from '../lib/logs';
import { api, enc, type S } from '../api/client';
import { Alert, Badge, CopyButton, Kv as ParameterKv, Table, Time } from '../components/bits';
import { Dag } from '../components/Dag';
import { niceTicks, statusOf, Tooltip, useTip, useWidth } from '../components/charts/base';
import { Logs } from '../components/Logs';
import { useOverlays } from '../components/overlays';
import { useAccess } from '../lib/me';
import { useCrumbs } from '../lib/chrome';
import { fmtDuration, go, num, routes, shortId } from '../lib/format';
import { QueryView, useAct, useLiveQuery } from '../lib/query';

const ACTIVE = new Set(['PENDING', 'SUBMITTED', 'RUNNING']);
type Step = S['StepRunOut'];

export function PipelineRunPage({ project, id }: { project: string; id: string }) {
  const access = useAccess(project);
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) },
    { label: 'Runs', href: `${routes.project(project)}/runs` }, { label: `run ${shortId(id)}` }]);
  const { confirm, toast } = useOverlays();
  const act = useAct();
  const [picked, setPicked] = useState<string | null>(null);

  const run = useLiveQuery(['pipeline-run', id], () => api.get<S['PipelineRunOut']>(`/pipeline-runs/${id}`), (r) => ACTIVE.has(r.status));
  // Default to the step most worth looking at: a failure, else what is running, else the last.
  const steps = run.data?.steps ?? [];
  const fallback = steps.find((s) => s.status === 'FAILED') ?? steps.find((s) => s.status === 'RUNNING') ?? steps[steps.length - 1];
  const selected = picked ?? fallback?.step ?? null;
  const running = run.data ? ACTIVE.has(run.data.status) : false;

  const logs = useRunLogs(selected ? `/pipeline-runs/${id}/steps/${enc(selected)}/logs` : null, running);
  const tracking = useQuery({
    queryKey: ['pipeline-run-tracking', id], retry: false,
    queryFn: () => api.get<S['TrackingOut']>(`/pipeline-runs/${id}/tracking`),
    refetchInterval: running ? 3000 : false,
  });

  async function rerun(r: S['PipelineRunOut']) {
    try {
      const again = await api.post<S['PipelineRunOut']>(
        `/projects/${enc(project)}/pipelines/${enc(r.pipeline)}/runs?version=${r.pipeline_version}`, { commit_sha: r.commit_sha, parameters: r.parameters ?? {} });
      toast('New run started');
      go(routes.pipelineRun(project, again.id));
    } catch (error) { toast(error instanceof Error ? error.message : 'Could not start the run', 'bad'); }
  }

  return (
    <QueryView query={run}>
      {(r) => {
        const cancellable = ACTIVE.has(r.status) && !r.cancel_requested;
        return (
          <>
            <div className="page-head">
              <h1>{`${r.pipeline} v${r.pipeline_version}`}</h1><Badge status={r.status} />
              <div className="actions">
                {cancellable ? (
                  <button className="btn danger" data-testid="cancel-run" disabled={!access.may('operator')} title={access.why('operator')} onClick={async () => {
                    if (await confirm({ title: 'Cancel this run?', body: 'Running steps are stopped and pending steps are cancelled.', confirmLabel: 'Cancel run', danger: true }))
                      await act(() => api.post(`/pipeline-runs/${id}/cancel`), 'Cancellation requested');
                  }}>Cancel run</button>
                ) : r.cancel_requested && ACTIVE.has(r.status) ? <Badge status="CANCELLED" /> : null}
                {!ACTIVE.has(r.status) && (
                  <button className="btn" type="button" data-testid="rerun" disabled={!access.may('operator')}
                    title={access.why('operator') ?? 'Start a new run of the same pipeline version'} onClick={() => rerun(r)}>Run again</button>)}
              </div>
            </div>
            <BatchOutputs project={project} runId={id} pipeline />
          <MonitoringOutputs project={project} runId={id} pipeline active={running} />
            <RunScheduleOrigin kind="pipeline-runs" id={id} />
            {Object.keys(r.parameters ?? {}).length > 0 && <div className="section card" data-testid="execution-parameters"><h2>Execution parameters</h2><ParameterKv entries={Object.entries(r.parameters ?? {}).map(([key, value]) => [key, typeof value === 'string' ? value : JSON.stringify(value)])} /></div>}
            <p className="sub meta">
              <span className="mono" title={r.id}>{shortId(r.id)}</span><CopyButton text={r.id} what="run id" />
              <span>{'Started '}<Time iso={r.started_at || r.created_at} /></span><span>{`Took ${fmtDuration(r.duration_seconds)}`}</span>
              {r.commit_sha && <span>{'Commit '}<span className="mono">{r.commit_sha}</span></span>}
            </p>
            {r.status_reason && <Alert bad={r.status === 'FAILED'}>{r.status_reason}</Alert>}
            <div className="card" data-testid="dag"><h2>Steps</h2><Dag steps={r.steps.map((s) => ({ ...s, reason: explain(s, r.steps) }))} selected={selected} onSelect={setPicked} /></div>
            <div className="section">
              <Table head={['Step', 'Status', 'Exit', 'Duration', 'Depends on']}>
                {r.steps.map((s) => (
                  <tr key={s.step} className={`click${s.step === selected ? ' sel' : ''}`} data-testid="step-row" onClick={() => setPicked(s.step)}>
                    <td>{s.step}</td>
                    <td><Badge status={s.status} />{explain(s, r.steps) && <div className="muted small" data-testid="step-reason">{explain(s, r.steps)}</div>}</td>
                    <td className="mono">{s.exit_code ?? '—'}</td>
                    <td>{fmtDuration(s.duration_seconds)}</td><td className="muted">{s.depends_on.join(', ') || '—'}</td>
                  </tr>))}
              </Table>
            </div>
            {r.steps.some((s) => s.started_at) && (
              <div className="section card" data-testid="timeline"><h2>Timeline</h2><Timeline steps={r.steps} onSelect={setPicked} /></div>)}
            <Logs title={selected ? `Logs: ${selected}` : 'Logs'} text={logs.data ?? ''} live={ACTIVE.has(r.status)}
              filename={`${r.pipeline}-${shortId(r.id)}-${selected ?? 'run'}.log`} />
            <Produced project={project} runId={r.id} />
            <div className="section card" data-testid="tracking"><h2>Experiment tracking</h2>
              <Tracking unavailable={tracking.isError} runs={tracking.data?.runs ?? []} />
            </div>
          </>
        );
      }}
    </QueryView>
  );
}

/** Steps on a shared time axis: what ran in parallel, where the time went, what was waiting.
 * Bars are the status palette with symbol and word in the tooltip; the steps table is its twin. */
function Timeline({ steps, onSelect }: { steps: Step[]; onSelect: (step: string) => void }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const { tip, bind } = useTip();
  const stamped = steps.filter((s) => s.started_at);
  const start = Math.min(...stamped.map((s) => Date.parse(s.started_at as string)));
  const now = Date.now();
  const end = Math.max(...stamped.map((s) => (s.finished_at ? Date.parse(s.finished_at) : now)), start + 1000);
  const ticks = niceTicks((end - start) / 1000, 5, 'seconds');
  const span = ticks[ticks.length - 1]! * 1000;
  const LABEL = 110, ROW = 26, BAR = 12, AXIS = 18;
  const x = (ms: number) => LABEL + ((ms - start) / span) * (width - LABEL - 8);
  const height = steps.length * ROW + AXIS;
  return (
    <div className="chart" ref={ref}>
      <svg width={width} height={height} role="img" aria-label="Steps over time. The steps table lists the same steps.">
        <g className="grid">{ticks.map((t) => <line key={t} x1={x(start + t * 1000)} x2={x(start + t * 1000)} y1={0} y2={steps.length * ROW} />)}</g>
        {ticks.map((t, i) => (
          <text key={t} className="tick" x={x(start + t * 1000)} y={height - 4} textAnchor={i === 0 ? 'start' : i === ticks.length - 1 ? 'end' : 'middle'}>
            {t === 0 ? 'start' : `+${fmtDuration(t)}`}
          </text>))}
        {steps.map((s, i) => {
          const st = statusOf(s.status);
          const cy = i * ROW + ROW / 2;
          const from = s.started_at ? Date.parse(s.started_at) : null;
          const to = s.finished_at ? Date.parse(s.finished_at) : from != null ? now : null;
          const took = s.duration_seconds ?? (from != null && to != null ? (to - from) / 1000 : null);
          return (
            <g key={s.step}>
              <text className="tl-label tick" x={0} y={cy + 4} style={{ fill: 'var(--text)', cursor: 'pointer' }} onClick={() => onSelect(s.step)}>
                {`${st.symbol} ${s.step}`}
              </text>
              {from != null && to != null && (
                <g className="mark" tabIndex={0} role="button" aria-label={`${s.step} ${st.word}, ${fmtDuration(took)}`}
                  onClick={() => onSelect(s.step)} onKeyDown={(e) => { if (e.key === 'Enter') onSelect(s.step); }}
                  {...bind({ head: `${s.step} · started +${fmtDuration((from - start) / 1000)}`, rows: [{ value: fmtDuration(took), label: `${st.symbol} ${st.word}`, color: st.color }] },
                    { x: x(from), y: cy - 10 })}>
                  <rect className="hit" x={x(from) - 4} y={i * ROW} width={Math.max(24, x(to) - x(from) + 8)} height={ROW} />
                  <rect className={`tl-bar st-${s.status}`} x={x(from)} y={cy - BAR / 2} width={Math.max(3, x(to) - x(from))} height={BAR} rx={4} fill={st.color} />
                </g>)}
            </g>);
        })}
      </svg>
      <Tooltip tip={tip} width={width} />
    </div>
  );
}

function Tracking({ unavailable, runs }: { unavailable: boolean; runs: S['TrackedRunOut'][] }) {
  if (unavailable) return <p className="muted">Experiment tracking is not available.</p>;
  if (!runs.length) return <p className="muted">This run has not logged any tracked results yet.</p>;
  return (
    <div>
      {runs.map((r, i) => (
        <div className="cols" style={{ marginBottom: '10px' }} key={i}>
          <div><h3>{`${r.step || 'step'} · parameters`}</h3><Kv obj={r.params} /></div>
          <div>
            <h3>metrics</h3>
            <Kv obj={Object.fromEntries(Object.entries(r.metrics).map(([k, v]) => [k, num(v, 3)]))} />
            {r.artifact_uri && <p className="small">Artifact: <span className="mono" data-testid="artifact">{r.artifact_uri}</span></p>}
          </div>
        </div>))}
    </div>
  );
}

function Kv({ obj }: { obj: Record<string, string> }) {
  const entries = Object.entries(obj);
  if (!entries.length) return <p className="muted">—</p>;
  return <dl className="kv">{entries.map(([k, v]) => <span key={k} style={{ display: 'contents' }}><dt>{k}</dt><dd className="mono">{v}</dd></span>)}</dl>;
}

/** Why a step ended up the way it did, in one line: the platform's reason, an exit code, or the
 * upstream failure that meant it never ran. */
export function explain(step: Step, steps: Step[]): string | null {
  if (step.status === 'SUCCEEDED' || step.status === 'RUNNING' || step.status === 'PENDING') return step.status_reason ?? null;
  if (step.status_reason) return step.status_reason;
  if (step.status === 'FAILED') return step.exit_code != null ? `exited with code ${step.exit_code}` : null;
  if (step.status === 'SKIPPED') {
    const byName = new Map(steps.map((s) => [s.step, s]));
    const seen = new Set<string>();
    const queue = [...step.depends_on];
    while (queue.length) {
      const name = queue.shift() as string;
      if (seen.has(name)) continue;
      seen.add(name);
      const dep = byName.get(name);
      if (dep?.status === 'FAILED' || dep?.status === 'CANCELLED') return `not run: ${dep.step} ${dep.status.toLowerCase()}`;
      if (dep) queue.push(...dep.depends_on);
    }
    return 'not run';
  }
  return null;
}

/** The model versions this run registered, so a result can be followed into the registry. */
function Produced({ project, runId }: { project: string; runId: string }) {
  const p = enc(project);
  const produced = useQuery({
    queryKey: ['produced', runId],
    queryFn: async () => {
      const { items } = await api.get<S['ModelList']>(`/projects/${p}/models`);
      const lists = await Promise.all(items.map((m) => api.get<{ items: { id: string; version: number; status: string; source_pipeline_run_id: string | null }[] }>(`/projects/${p}/models/${enc(m.name)}/versions`)
        .then((l) => l.items.filter((v) => v.source_pipeline_run_id === runId).map((v) => ({ model: m.name, ...v })))));
      return lists.flat();
    },
  });
  if (!produced.data?.length) return null;
  return (
    <div className="section card" data-testid="produced">
      <h2>Models from this run</h2>
      <ul className="timeline">
        {produced.data.map((v) => (
          <li key={v.id}><a href={routes.model(project, v.model)}>{`${v.model} v${v.version}`}</a><Badge status={v.status} /></li>))}
      </ul>
    </div>
  );
}
