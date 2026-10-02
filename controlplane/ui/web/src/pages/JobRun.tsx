import { useQuery } from '@tanstack/react-query';
import { api, type S } from '../api/client';
import { Alert, Badge, CopyButton, Time } from '../components/bits';
import { Logs } from '../components/Logs';
import { useOverlays } from '../components/overlays';
import { useCrumbs } from '../lib/chrome';
import { fmtDuration, go, routes, shortId } from '../lib/format';
import { QueryView, useAct, useLiveQuery } from '../lib/query';

const ACTIVE = new Set(['PENDING', 'SUBMITTED', 'RUNNING']);

export function JobRunPage({ project, id }: { project: string; id: string }) {
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) },
    { label: 'Runs', href: `${routes.project(project)}/runs?kind=job` }, { label: `job run ${shortId(id)}` }]);
  const { confirm, toast } = useOverlays();
  const act = useAct();
  const run = useLiveQuery(['job-run', id], () => api.get<S['RunOut']>(`/runs/${id}`), (r) => ACTIVE.has(r.status));
  const active = run.data ? ACTIVE.has(run.data.status) : false;
  const logs = useQuery({
    queryKey: ['job-run-logs', id], enabled: run.data !== undefined,
    queryFn: async () => { try { return await api.text(`/runs/${id}/logs`); } catch { return '(logs are not available)'; } },
    refetchInterval: active ? 3000 : false,
  });

  return (
    <QueryView query={run}>
      {(r) => (
        <>
          <div className="page-head">
            <h1>{r.job || 'Job run'}</h1><Badge status={r.status} />
            <div className="actions">
              {ACTIVE.has(r.status) && !r.cancel_requested && (
                <button className="btn danger" data-testid="cancel-run" onClick={async () => {
                  if (await confirm({ title: 'Cancel this run?', body: 'The workload is stopped.', confirmLabel: 'Cancel run', danger: true }))
                    await act(() => api.post(`/runs/${id}/cancel`), 'Cancellation requested');
                }}>Cancel run</button>)}
              {['FAILED', 'CANCELLED', 'SUCCEEDED'].includes(r.status) && (
                <button className={`btn${r.status === 'FAILED' ? ' primary' : ''}`} type="button" data-testid="retry"
                  title="Run it again as a new run; this one stays in the history" onClick={async () => {
                    try {
                      const again = await api.post<S['RunOut']>(`/runs/${id}/retry`, {});
                      toast('Retry started');
                      go(routes.jobRun(project, again.id));
                    } catch (error) { toast(error instanceof Error ? error.message : 'Could not retry', 'bad'); }
                  }}>{r.status === 'FAILED' ? 'Retry' : 'Run again'}</button>)}
            </div>
          </div>
          <p className="sub">
            <span className="mono" title={r.id}>{shortId(r.id)}</span><CopyButton text={r.id} what="run id" />
            {' · started '}<Time iso={r.started_at || r.created_at} />{' · '}{fmtDuration(r.duration_seconds)}
            {' · exit code '}<span className="mono">{r.exit_code ?? '—'}</span>
          </p>
          {r.status_reason && <Alert bad={r.status === 'FAILED'}>{r.status_reason}</Alert>}
          {r.retry_of && <p className="small muted">{'Retry of '}<a href={routes.jobRun(project, r.retry_of)}>{shortId(r.retry_of)}</a></p>}
          <Logs title="Logs" text={logs.data ?? ''} live={ACTIVE.has(r.status)} filename={`${r.job || 'job'}-${shortId(r.id)}.log`} />
        </>)}
    </QueryView>
  );
}
