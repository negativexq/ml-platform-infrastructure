import { api, enc, type S } from '../api/client';
import { Badge, Empty, Table } from '../components/bits';
import { useDeploy } from '../components/Deploy';
import { useOverlays } from '../components/overlays';
import { useAccess } from '../lib/me';
import { useCrumbs } from '../lib/chrome';
import { go, num, pct, routes } from '../lib/format';
import { QueryView, useLiveQuery } from '../lib/query';

export function DeploymentsPage({ project }: { project: string }) {
  const access = useAccess(project);
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) }, { label: 'Deployments' }]);
  const p = enc(project);
  const { form, toast } = useOverlays();
  const deploy = useDeploy(project);
  const query = useLiveQuery(['deployments', project], async () => {
    const { items } = await api.get<S['DeploymentList']>(`/projects/${p}/deployments`);
    return Promise.all(items.map(async (d) => {
      const [rollouts, metrics] = await Promise.all([
        api.get<S['RolloutList']>(`/projects/${p}/deployments/${enc(d.name)}/rollouts`),
        api.get<S['EndpointMetricsOut']>(`/projects/${p}/endpoints/${enc(d.endpoint.name)}/metrics`).catch(() => null),
      ]);
      return { d, live: rollouts.items.find((r) => ['PENDING', 'PROGRESSING'].includes(r.status)), metrics };
    }));
  }, () => true);

  async function create() {
    const d = await form<S['DeploymentOut']>({
      title: 'New deployment', submitLabel: 'Create',
      intro: 'A deployment is a named place that serves one model version (two during a canary), with one stable endpoint.',
      fields: [{ name: 'name', label: 'Name', required: true, pattern: '^[a-z][a-z0-9]*(-[a-z0-9]+)*$', placeholder: `${project}-prod`, hint: 'Lowercase letters, digits and dashes.' }],
      submit: (v) => api.post<S['DeploymentOut']>(`/projects/${p}/deployments`, { name: v.name }),
    });
    if (d) { toast(`Deployment ${d.name} created; now deploy a model version to it`); go(routes.deployment(project, d.name)); }
  }

  return (
    <QueryView query={query}>
      {(rows) => (
        <>
          <div className="page-head">
            <h1>Deployments</h1>
            <div className="actions">
              <button className="btn" type="button" data-testid="new-deployment" disabled={!access.may('operator')} title={access.why('operator')} onClick={create}>New deployment</button>
              <button className="btn primary" type="button" data-testid="deploy" disabled={!access.may('operator')} title={access.why('operator')} onClick={() => deploy()}>Deploy a version</button>
            </div>
          </div>
          <p className="sub">What serves traffic, how it is split, and how it is doing right now.</p>
          {rows.length === 0 ? <Empty>No deployments yet.</Empty> : (
            <Table testid="deployments" head={['Deployment', 'Status', 'Serving', 'Traffic', 'p95', '5xx', 'Endpoint']}>
              {rows.map(({ d, live, metrics }) => {
                const active = d.revisions.find((r) => r.revision === d.active_revision);
                const revs = metrics?.available ? metrics.revisions : [];
                const worstP95 = revs.length ? Math.max(...revs.map((r) => r.p95_latency_ms ?? 0)) : null;
                const worstErr = revs.length ? Math.max(...revs.map((r) => r.error_rate ?? 0)) : null;
                return (
                  <tr key={d.id} className="click" data-testid="deployment-row" onClick={() => go(routes.deployment(project, d.name))}>
                    <td><a href={routes.deployment(project, d.name)}>{d.name}</a></td>
                    <td><Badge status={d.status} /></td>
                    <td>{active ? `${active.model} v${active.model_version}` : <span className="muted">nothing yet</span>}</td>
                    <td>{live ? <span className="chip" title="Canary in progress">{`canary ${live.canary_percent}% → v${live.model_version}`}</span> : active ? '100%' : '—'}</td>
                    <td className="num">{worstP95 == null ? '—' : `${num(worstP95, 0)} ms`}</td>
                    <td className="num">{worstErr == null ? '—' : pct(worstErr)}</td>
                    <td><Badge status={d.endpoint.status} /></td>
                  </tr>);
              })}
            </Table>
          )}
        </>
      )}
    </QueryView>
  );
}
