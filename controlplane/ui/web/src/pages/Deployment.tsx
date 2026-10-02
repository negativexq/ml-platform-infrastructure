import { api, enc, type S } from '../api/client';
import { Alert, Badge, CopyButton, Section, Table, Time } from '../components/bits';
import { useOverlays } from '../components/overlays';
import { useCrumbs } from '../lib/chrome';
import { fmtDuration, num, pct, routes } from '../lib/format';
import { QueryView, useAct, useLiveQuery } from '../lib/query';

const ROLLING = new Set(['PENDING', 'PROGRESSING']);
type Rollout = S['RolloutOut'];

const LABELS: Record<string, string> = {
  'deployment.created': 'Deployment created', 'deployment.revision_created': 'New revision', 'deployment.ready': 'Became ready',
  'deployment.drift_detected': 'Serving drift detected', 'deployment.redeploying': 'Recreating serving resource',
  'deployment.failed': 'Failed', 'deployment.rolled_back': 'Rolled back', 'endpoint.ready': 'Endpoint ready',
};

export function DeploymentPage({ project, name }: { project: string; name: string }) {
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) }, { label: name }]);
  const base = `/projects/${enc(project)}`;
  const { confirm } = useOverlays();
  const act = useAct();

  const query = useLiveQuery(['deployment', project, name], async () => {
    const deployment = await api.get<S['DeploymentOut']>(`${base}/deployments/${enc(name)}`);
    const [rollouts, metrics, audit] = await Promise.all([
      api.get<S['RolloutList']>(`${base}/deployments/${enc(name)}/rollouts`),
      api.get<S['EndpointMetricsOut']>(`${base}/endpoints/${enc(deployment.endpoint.name)}/metrics`).catch(() => null),
      api.get<S['AuditOut']>(`${base}/audit?entity_id=${deployment.id}&limit=10`).catch(() => ({ items: [] as S['AuditEventOut'][] })),
    ]);
    return { deployment, rollouts: rollouts.items, metrics, audit: audit.items };
  }, () => true); // metrics are live; keep polling while the page is open

  return (
    <QueryView query={query}>
      {({ deployment: d, rollouts, metrics, audit }) => {
        const live = rollouts.find((r) => ROLLING.has(r.status));
        const noRollback = Boolean(live) || d.revisions.length < 2 || d.active_revision == null;
        return (
          <>
            <div className="page-head">
              <h1>{d.name}</h1><Badge status={d.status} />
              <div className="actions">
                <button className="btn danger" data-testid="rollback" disabled={noRollback}
                  title={live ? 'A rollout is in progress; abort it instead' : d.revisions.length < 2 ? 'There is no earlier revision' : 'Serve the previous revision again'}
                  onClick={async () => {
                    const prev = Math.max(...d.revisions.map((r) => r.revision).filter((n) => n < (d.active_revision ?? 0)));
                    if (await confirm({ title: 'Roll back this deployment?', confirmLabel: 'Roll back', danger: true,
                      body: `Revision r${d.active_revision} is replaced by r${prev}. The model served by r${prev} becomes the champion again and the current champion is archived.` }))
                      await act(() => api.post(`${base}/deployments/${enc(name)}/rollback`), `Rolling back to r${prev}`);
                  }}>Roll back</button>
              </div>
            </div>
            <p className="sub">
              Active revision <b>{d.active_revision != null ? `r${d.active_revision}` : 'none'}</b>
              {d.desired_revision !== d.active_revision && <>{' · desired '}<b>{`r${d.desired_revision}`}</b></>}
            </p>
            {d.status_reason && <Alert bad={d.status === 'FAILED'}>{d.status_reason}</Alert>}
            {live && <RolloutCard r={live} onAbort={async () => {
              if (await confirm({ title: 'Abort this rollout?', body: `All traffic returns to r${live.from_revision}. The canary is not promoted.`, confirmLabel: 'Abort rollout', danger: true }))
                await act(() => api.post(`/rollouts/${live.id}/abort`), 'Abort requested');
            }} />}
            <div className="cols"><EndpointCard endpoint={d.endpoint} metrics={metrics} live={live} /><Revisions d={d} /></div>
            {rollouts.some((r) => r !== live) && (
              <Section title="Rollout history"><History rows={rollouts.filter((r) => r !== live)} /></Section>)}
            <div className="section card"><h2>Recent activity</h2>
              {audit.length ? (
                <ul className="timeline">{audit.map((e) => (
                  <li key={e.id}><Time iso={e.occurred_at} /><span>{LABELS[e.action] ?? e.action}</span><span className="muted small">{e.actor}</span></li>))}</ul>
              ) : <p className="muted">No activity recorded.</p>}
            </div>
          </>
        );
      }}
    </QueryView>
  );
}

function RolloutCard({ r, onAbort }: { r: Rollout; onAbort: () => void }) {
  return (
    <div className="card section" data-testid="rollout">
      <h2>{`Canary rollout: r${r.from_revision} → r${r.to_revision}`}<Badge status={r.status} /><span className="muted small">{` ${r.model} v${r.model_version}`}</span></h2>
      <div className="bar" role="img" aria-label={`Traffic: stable ${100 - r.canary_percent}%, canary ${r.canary_percent}%`}>
        <div className="stable" style={{ width: `${100 - r.canary_percent}%` }} data-testid="stable-share">{`r${r.from_revision} stable ${100 - r.canary_percent}%`}</div>
        {r.canary_percent > 0 && <div className="canary" style={{ width: `${r.canary_percent}%` }} data-testid="canary-share">{`r${r.to_revision} ${r.canary_percent}%`}</div>}
      </div>
      <div className="steps" aria-label="Rollout steps">
        {r.steps.map((s, i) => <span key={i} className={i < r.current_step ? 'done' : i === r.current_step ? 'cur' : ''}>{`${s}%`}</span>)}
      </div>
      <p className="muted small">{`Gate: error rate ≤ ${pct(r.gate.max_error_rate)}, p95 ≤ ${r.gate.max_p95_latency_ms} ms, ≥ ${r.gate.min_requests} requests, ${fmtDuration(r.gate.step_seconds)} per step`}</p>
      {r.abort_requested && <Alert>Abort requested — returning traffic to the stable revision.</Alert>}
      <button className="btn danger" data-testid="abort" disabled={r.abort_requested} onClick={onAbort}>Abort rollout</button>
    </div>
  );
}

function EndpointCard({ endpoint, metrics, live }: { endpoint: S['EndpointOut']; metrics: S['EndpointMetricsOut'] | null; live: Rollout | undefined }) {
  const rows = metrics?.available ? metrics.revisions : [];
  return (
    <div className="card" data-testid="endpoint">
      <h2>Endpoint<Badge status={endpoint.status} /></h2>
      <p className="small">
        <span className="mono">{endpoint.name}</span>
        {endpoint.url && <>{' · '}<span className="mono muted">{endpoint.url}</span><CopyButton text={endpoint.url} what="endpoint URL" /></>}
      </p>
      {metrics && !metrics.available && <Alert>{`Metrics unavailable: ${metrics.error}`}</Alert>}
      {rows.length ? rows.map((m) => (
        <div className="rev" key={m.revision} data-testid="revision-metrics" data-revision={m.revision}>
          <h3>{`r${m.revision}`}{live ? (m.revision === live.to_revision ? ' · canary' : ' · stable') : ''}{` · ${m.model} v${m.model_version} · ${m.traffic_percent}% of traffic`}</h3>
          <div className="metric">
            <div><b>{m.p95_latency_ms == null ? '—' : `${num(m.p95_latency_ms, 0)} ms`}</b><span>p95 latency</span></div>
            <div><b>{pct(m.error_rate)}</b><span>5xx rate</span></div>
            <div><b>{num(m.requests_per_second, 1)}</b><span>requests / s</span></div>
          </div>
        </div>)) : metrics?.available ? <p className="muted">No traffic data yet.</p> : null}
    </div>
  );
}

function Revisions({ d }: { d: S['DeploymentOut'] }) {
  return (
    <div className="card" data-testid="revisions"><h2>Revisions</h2>
      <Table head={['Revision', 'Model', 'Created', '']}>
        {[...d.revisions].reverse().map((r) => (
          <tr key={r.revision}>
            <td className="mono">{`r${r.revision}`}</td><td>{`${r.model} v${r.model_version}`}</td><td><Time iso={r.created_at} /></td>
            <td>{r.revision === d.active_revision ? <Badge status="READY" /> : r.revision === d.desired_revision ? <Badge status="DEPLOYING" /> : null}</td>
          </tr>))}
      </Table>
    </div>
  );
}

function History({ rows }: { rows: Rollout[] }) {
  return (
    <Table testid="rollout-history" head={['Rollout', 'Model', 'Outcome', 'Reason', 'Finished']}>
      {rows.map((r) => (
        <tr key={r.id}>
          <td className="mono">{`r${r.from_revision} → r${r.to_revision}`}</td><td>{`${r.model} v${r.model_version}`}</td>
          <td><Badge status={r.status} /></td><td className="muted">{r.status_reason || '—'}</td><td><Time iso={r.finished_at} /></td>
        </tr>))}
    </Table>
  );
}
