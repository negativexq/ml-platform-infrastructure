import { api, enc, type S } from '../api/client';
import { ApiAccessCard } from '../components/ApiAccess';
import { Badge, Empty, Table } from '../components/bits';
import { MetricTrends } from '../components/MetricTrends';
import { Playground } from '../components/Playground';
import { TryIt } from '../components/TryIt';
import { useCrumbs } from '../lib/chrome';
import { routes } from '../lib/format';
import { useAccess } from '../lib/me';
import { resourceType } from '../lib/navigation';
import { QueryView, useLiveQuery } from '../lib/query';

export function EndpointsPage({ project }: { project: string }) {
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) }, { label: 'Endpoints' }]);
  const query = useLiveQuery(['endpoints', project], () => api.get<S['EndpointListOut']>(`/projects/${enc(project)}/endpoints`), () => true, 15_000);
  return <QueryView query={query}>{({ items }) => <>
    <div className="page-head"><h1>Endpoints</h1></div><p className="sub">Callable resources in this project. Manage API access and test requests from an endpoint.</p>
    {items.length ? <Table head={['Endpoint', 'Status', 'Deployment status', 'Revision', 'URL']} testid="endpoints">{items.map((e) => <tr key={e.id}><td><a href={routes.endpoint(project, e.name)}>{e.name}</a></td><td><Badge status={e.status} /></td><td><Badge status={e.deployment_status} /></td><td>{e.active_revision == null ? '—' : `r${e.active_revision}`}</td><td className="mono">{e.url ?? '—'}</td></tr>)}</Table> : <Empty actions={<><a className="btn" href={`${routes.project(project)}/deployments`}>Open deployments</a><a href="#/help?topic=guides">Deployment guide</a></>}>No endpoints yet. Deploy a model or function to create one.</Empty>}
  </>}</QueryView>;
}

export function EndpointPage({ project, name }: { project: string; name: string }) {
  const access = useAccess(project);
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) }, { label: 'Endpoints', href: `${routes.project(project)}/endpoints` }, { label: name }]);
  const query = useLiveQuery(['endpoint', project, name], async () => {
    const endpoint = await api.get<S['EndpointOut']>(`/projects/${enc(project)}/endpoints/${enc(name)}`);
    const { items } = await api.get<S['DeploymentList']>(`/projects/${enc(project)}/deployments`);
    return { endpoint, deployment: items.find((d) => d.endpoint.id === endpoint.id) };
  }, () => true, 15_000);
  return <QueryView query={query}>{({ endpoint, deployment }) => <>
    <div className="page-head"><h1>{endpoint.name}</h1><Badge status={endpoint.status} /><span className="chip">{resourceType(endpoint.kind)}</span></div>
    <p className="sub">{endpoint.protocol} · {endpoint.exposure}{deployment && <> · <a href={routes.deployment(project, deployment.name)}>Deployment: {deployment.name}</a></>}</p>
    <MetricTrends project={project} endpoint={name} />
    <ApiAccessCard project={project} endpoint={endpoint} />
    {endpoint.kind === 'llm' ? <Playground project={project} endpoint={endpoint} allowed={access.may('invoker')} /> : <TryIt project={project} endpoint={endpoint} allowed={access.may('invoker')} />}
  </>}</QueryView>;
}
