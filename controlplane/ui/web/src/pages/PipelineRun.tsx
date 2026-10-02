import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api, enc, type S } from '../api/client';
import { Alert, Badge, CopyButton, Table, Time } from '../components/bits';
import { Dag } from '../components/Dag';
import { Logs } from '../components/Logs';
import { useOverlays } from '../components/overlays';
import { useCrumbs } from '../lib/chrome';
import { fmtDuration, go, num, routes, shortId } from '../lib/format';
import { QueryView, useAct, useLiveQuery } from '../lib/query';

const ACTIVE = new Set(['PENDING', 'SUBMITTED', 'RUNNING']);
type Step = S['StepRunOut'];

export function PipelineRunPage({ project, id }: { project: string; id: string }) {
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) }, { label: `run ${shortId(id)}` }]);
  const { confirm, toast } = useOverlays();
  const act = useAct();
  const [picked, setPicked] = useState<string | null>(null);

  const run = useLiveQuery(['pipeline-run', id], () => api.get<S['PipelineRunOut']>(`/pipeline-runs/${id}`), (r) => ACTIVE.has(r.status));
  // Default to the step most worth looking at: a failure, else what is running, else the last.
  const steps = run.data?.steps ?? [];
  const fallback = steps.find((s) => s.status === 'FAILED') ?? steps.find((s) => s.status === 'RUNNING') ?? steps[steps.length - 1];
  const selected = picked ?? fallback?.step ?? null;
  const running = run.data ? ACTIVE.has(run.data.status) : false;

  const logs = useQuery({
    queryKey: ['pipeline-run-logs', id, selected], enabled: selected !== null,
    queryFn: async () => {
      const step = selected ?? '';
      try { return await api.text(`/pipeline-runs/${id}/steps/${enc(step)}/logs`); } catch { return '(logs are not available)'; }
    },
    refetchInterval: running ? 3000 : false,
  });
  const tracking = useQuery({
    queryKey: ['pipeline-run-tracking', id], retry: false,
    queryFn: () => api.get<S['TrackingOut']>(`/pipeline-runs/${id}/tracking`),
    refetchInterval: running ? 3000 : false,
  });

  async function rerun(r: S['PipelineRunOut']) {
    try {
      const again = await api.post<S['PipelineRunOut']>(
        `/projects/${enc(project)}/pipelines/${enc(r.pipeline)}/runs?version=${r.pipeline_version}`, r.commit_sha ? { commit_sha: r.commit_sha } : {});
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
                  <button className="btn danger" data-testid="cancel-run" onClick={async () => {
                    if (await confirm({ title: 'Cancel this run?', body: 'Running steps are stopped and pending steps are cancelled.', confirmLabel: 'Cancel run', danger: true }))
                      await act(() => api.post(`/pipeline-runs/${id}/cancel`), 'Cancellation requested');
                  }}>Cancel run</button>
                ) : r.cancel_requested && ACTIVE.has(r.status) ? <Badge status="CANCELLED" /> : null}
                {!ACTIVE.has(r.status) && (
                  <button className="btn" type="button" data-testid="rerun" title="Start a new run of the same pipeline version" onClick={() => rerun(r)}>Run again</button>)}
              </div>
            </div>
            <p className="sub">
              <span className="mono" title={r.id}>{shortId(r.id)}</span><CopyButton text={r.id} what="run id" />
              {' · started '}<Time iso={r.started_at || r.created_at} />{' · '}{fmtDuration(r.duration_seconds)}
              {r.commit_sha && <>{' · commit '}<span className="mono">{r.commit_sha}</span></>}
            </p>
            {r.status_reason && <Alert bad={r.status === 'FAILED'}>{r.status_reason}</Alert>}
            <div className="card" data-testid="dag"><h2>Steps</h2><Dag steps={r.steps} selected={selected} onSelect={setPicked} /></div>
            <div className="section">
              <Table head={['Step', 'Status', 'Exit', 'Duration', 'Depends on']}>
                {r.steps.map((s) => (
                  <tr key={s.step} className={`click${s.step === selected ? ' sel' : ''}`} data-testid="step-row" onClick={() => setPicked(s.step)}>
                    <td>{s.step}</td><td><Badge status={s.status} /></td><td className="mono">{s.exit_code ?? '—'}</td>
                    <td>{fmtDuration(s.duration_seconds)}</td><td className="muted">{s.depends_on.join(', ') || '—'}</td>
                  </tr>))}
              </Table>
            </div>
            {r.steps.some((s) => s.started_at) && (
              <div className="section card" data-testid="timeline"><h2>Timeline</h2><Timeline steps={r.steps} onSelect={setPicked} /></div>)}
            <Logs title={selected ? `Logs: ${selected}` : 'Logs'} text={logs.data ?? ''} live={ACTIVE.has(r.status)}
              filename={`${r.pipeline}-${shortId(r.id)}-${selected ?? 'run'}.log`} />
            <div className="section card" data-testid="tracking"><h2>Experiment tracking</h2>
              <Tracking unavailable={tracking.isError} runs={tracking.data?.runs ?? []} />
            </div>
          </>
        );
      }}
    </QueryView>
  );
}

/** Steps on a shared time axis: what ran in parallel, where the time went, what was waiting. */
function Timeline({ steps, onSelect }: { steps: Step[]; onSelect: (step: string) => void }) {
  const stamped = steps.filter((s) => s.started_at);
  const start = Math.min(...stamped.map((s) => Date.parse(s.started_at as string)));
  const now = Date.now();
  const end = Math.max(...stamped.map((s) => (s.finished_at ? Date.parse(s.finished_at) : now)), start + 1000);
  const span = end - start;
  return (
    <div className="timeline-chart" role="list">
      {steps.map((s) => {
        const from = s.started_at ? Date.parse(s.started_at) : null;
        const to = s.finished_at ? Date.parse(s.finished_at) : from != null ? now : null;
        return (
          <span key={s.step} style={{ display: 'contents' }}>
            <button className="tl-label linklike" type="button" role="listitem" onClick={() => onSelect(s.step)}>{s.step}</button>
            <div className="tl-track" aria-label={`${s.step} ${s.status.toLowerCase()}`}>
              {from != null && to != null && (
                <div className={`tl-bar st-${s.status}`} title={`${s.step}: ${fmtDuration(s.duration_seconds ?? (to - from) / 1000)}`}
                  style={{ left: `${((from - start) / span) * 100}%`, width: `${Math.max(0.8, ((to - from) / span) * 100)}%` }} />)}
            </div>
          </span>);
      })}
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
