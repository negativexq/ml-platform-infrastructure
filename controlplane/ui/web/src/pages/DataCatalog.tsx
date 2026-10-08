import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, enc, type S } from '../api/client';
import { Alert, Empty, Kv, Table, Time } from '../components/bits';
import { Dag } from '../components/Dag';
import { Modal, useOverlays } from '../components/overlays';
import { useAccess } from '../lib/me';
import { useCrumbs } from '../lib/chrome';
import { go, routes, shortId } from '../lib/format';
import { QueryView } from '../lib/query';
import { useSearchState } from '../lib/search';

type Dataset = S['DatasetOut'];
type Connection = S['ConnectionOut'];
const datasetUrl = (project: string, name: string, version?: number) => `${routes.project(project)}/datasets/${enc(name)}${version ? `?version=${version}` : ''}`;
const connectionUrl = (project: string, id: string) => `${routes.project(project)}/connections/${id}`;

function ProjectSelect({ value, change }: { value: string; change: (value: string) => void }) {
  const projects = useQuery({ queryKey: ['projects'], queryFn: () => api.get<S['ProjectList']>('/projects?limit=200') });
  return <label>Project <select aria-label="Project" value={value} onChange={event => change(event.target.value)}><option value="">Select a project</option>{projects.data?.items.map(p => <option key={p.id} value={p.name}>{p.display_name}</option>)}</select></label>;
}

function Pagination({ offset, count, change }: { offset: number; count: number; change: (offset: number) => void }) {
  return <div className="actions section"><button className="btn" disabled={!offset} onClick={() => change(Math.max(0, offset - 50))}>Previous</button><button className="btn" disabled={count < 50} onClick={() => change(offset + 50)}>Next</button></div>;
}

export function ConnectionsPage({ project: fixedProject }: { project?: string }) {
  const [filters, setFilters] = useSearchState({ project: '', offset: '0' });
  const project = fixedProject ?? filters.project;
  const offset = Math.max(0, Number(filters.offset) || 0);
  const access = useAccess(project);
  useCrumbs([{ label: 'Connections' }]);
  const { form, toast } = useOverlays();
  const query = useQuery({ queryKey: ['connections', project, offset], enabled: !!project, queryFn: () => api.get<S['ConnectionList']>(`/projects/${enc(project)}/data-connections?limit=50&offset=${offset}`) });
  async function create() {
    try {
      const secrets = await api.get<S['SecretList']>(`/projects/${enc(project)}/secret-references`);
      const eligible = secrets.items.filter(s => s.kind === 'Opaque' && ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'].every(key => s.keys.includes(key)));
      if (!eligible.length) { toast('A project admin must add an S3 credential secret in Settings first.', 'bad'); return; }
      const connection = await form<Connection>({ title: 'Register S3 connection', submitLabel: 'Register connection',
        intro: 'Connections are immutable. Register a new name to change storage settings; rotate credentials in Settings.',
        fields: [
          { name: 'name', label: 'Name', required: true, placeholder: 'customer-data' },
          { name: 'endpoint', label: 'S3 endpoint', required: true, placeholder: 'https://s3.eu-west-1.amazonaws.com', hint: 'HTTP(S) origin, including the port for MinIO.' },
          { name: 'region', label: 'Region', value: 'us-east-1', required: true },
          { name: 'bucket', label: 'Bucket', required: true, placeholder: 'ml-data' },
          { name: 'prefix', label: 'Allowed prefix', placeholder: 'credit-risk', hint: 'Datasets and model artifacts must stay within this path.' },
          { name: 'credential_secret', label: 'Credential secret', required: true, options: eligible.map(s => ({ value: s.name, label: s.name })) },
        ], submit: values => api.post<Connection>(`/projects/${enc(project)}/data-connections`, values),
      });
      if (connection) { toast('Connection registered'); go(connectionUrl(project, connection.id)); }
    } catch (error) { toast(error instanceof Error ? error.message : 'Could not register connection', 'bad'); }
  }
  return <>
    <div className="page-head"><h1>Connections</h1><button className="btn primary" disabled={!project || !access.may('operator')} onClick={create}>Register connection</button></div>
    <p className="sub">Reusable S3/MinIO locations and project credential references.</p>
    {!fixedProject && <div className="toolbar"><ProjectSelect value={project} change={value => setFilters({ project: value, offset: '0' })} /></div>}
    {!project ? <Empty>Select a project to browse its connections.</Empty> : <QueryView query={query}>{list => <>
      {!list.items.length ? <Empty>No connections registered.</Empty> : <Table head={['Connection', 'Location', 'Endpoint / Region', 'Credentials', 'Registered']} testid="data-connections">{list.items.map(c => <tr key={c.id}>
        <td><a href={connectionUrl(project, c.id)}>{c.name}</a></td><td className="mono small">s3://{c.bucket}/{c.prefix}</td><td>{c.endpoint}<div className="muted small">{c.region}</div></td><td>{c.credential_secret}</td><td><Time iso={c.created_at} /></td>
      </tr>)}</Table>}
      <Pagination offset={offset} count={list.items.length} change={value => setFilters({ offset: String(value) })} />
    </>}</QueryView>}
  </>;
}

export function ConnectionPage({ project, id }: { project: string; id: string }) {
  useCrumbs([{ label: 'Connections', href: `${routes.project(project)}/connections` }, { label: 'Connection' }]);
  const query = useQuery({ queryKey: ['connection', project, id], queryFn: () => api.get<Connection>(`/projects/${enc(project)}/data-connections/${id}`) });
  return <QueryView query={query}>{c => <>
    <div className="page-head"><h1>{c.name}</h1><span className="badge">Immutable connection</span></div>
    <section className="card"><Kv entries={[["Endpoint", c.endpoint], ["Region", c.region], ["Bucket", c.bucket], ["Allowed prefix", c.prefix || 'Bucket root'], ["Credential secret", c.credential_secret], ["Registered", <Time iso={c.created_at} />]]} /></section>
    <p className="sub section">Registration stores settings. Storage access and object integrity are checked by the workload that uses this connection.</p>
    <div className="actions"><a className="btn" href={`${routes.project(project)}/datasets`}>Browse datasets</a><a className="btn" href={`${routes.project(project)}/settings`}>Manage credentials</a></div>
  </>}</QueryView>;
}

export function DatasetsPage({ project: fixedProject }: { project?: string }) {
  const [filters, setFilters] = useSearchState({ project: '', name: '', offset: '0' });
  const project = fixedProject ?? filters.project;
  const offset = Math.max(0, Number(filters.offset) || 0);
  const [editing, setEditing] = useState(false);
  const access = useAccess(project);
  useCrumbs([{ label: 'Datasets' }]);
  const query = useQuery({ queryKey: ['datasets', project, filters.name, offset], enabled: !!project, queryFn: () => api.get<S['DatasetList']>(`/projects/${enc(project)}/datasets?limit=50&offset=${offset}${filters.name ? `&name=${enc(filters.name)}` : ''}`) });
  return <>
    <div className="page-head"><h1>Datasets</h1><button className="btn primary" disabled={!project || !access.may('operator')} onClick={() => setEditing(true)}>Register dataset</button></div>
    <p className="sub">Immutable CSV/Parquet versions, schema, integrity identity and recorded lineage.</p>
    <div className="toolbar">{!fixedProject && <ProjectSelect value={project} change={value => setFilters({ project: value, offset: '0' })} />}<label>Dataset name <input aria-label="Dataset name filter" value={filters.name} placeholder="Exact name" onChange={event => setFilters({ name: event.target.value, offset: '0' })} /></label></div>
    {!project ? <Empty>Select a project to browse its datasets.</Empty> : <QueryView query={query}>{list => <>
      {!list.items.length ? <Empty>No dataset versions match.</Empty> : <Table head={['Dataset', 'Version', 'Format', 'Rows', 'Location', 'Producer', 'Registered']} testid="datasets">{list.items.map(d => <tr key={d.id}>
        <td><a href={datasetUrl(project, d.name, d.version)}>{d.name}</a></td><td>v{d.version}</td><td>{d.format}</td><td>{d.row_count?.toLocaleString() ?? 'Unknown'}</td><td className="mono small">{d.uri}</td><td><Producer project={project} dataset={d} /></td><td><Time iso={d.created_at} /></td>
      </tr>)}</Table>}
      <Pagination offset={offset} count={list.items.length} change={value => setFilters({ offset: String(value) })} />
    </>}</QueryView>}
    {editing && <DatasetEditor project={project} onClose={() => setEditing(false)} onSaved={dataset => { setEditing(false); go(datasetUrl(project, dataset.name, dataset.version)); }} />}
  </>;
}

function Producer({ project, dataset }: { project: string; dataset: Dataset }) {
  return dataset.producer_run_id ? <a href={routes.jobRun(project, dataset.producer_run_id)}>Job {shortId(dataset.producer_run_id)}</a> : dataset.producer_pipeline_run_id ? <a href={routes.pipelineRun(project, dataset.producer_pipeline_run_id)}>Pipeline {shortId(dataset.producer_pipeline_run_id)}</a> : <span className="muted">External / no run recorded</span>;
}

export function DatasetPage({ project, name }: { project: string; name: string }) {
  const [filter, setFilter] = useSearchState({ version: '', offset: '0' });
  const [editing, setEditing] = useState<Dataset | null>(null);
  const [editorLatest, setEditorLatest] = useState(0);
  const access = useAccess(project);
  const { toast } = useOverlays();
  const query = useQuery({ queryKey: ['dataset', project, name, filter.version], queryFn: () => { const path = `/projects/${enc(project)}/datasets/${enc(name)}`; return api.get<Dataset>(path + (filter.version ? `?version=${enc(filter.version)}` : '')); } });
  const offset = Math.max(0, Number(filter.offset) || 0);
  const versions = useQuery({ queryKey: ['dataset-versions', project, name, offset], queryFn: () => api.get<S['DatasetList']>(`/projects/${enc(project)}/datasets?name=${enc(name)}&limit=50&offset=${offset}`) });
  useCrumbs([{ label: 'Datasets', href: `${routes.project(project)}/datasets` }, { label: name }]);
  async function newVersion(dataset: Dataset) {
    try { const latest = await api.get<Dataset>(`/projects/${enc(project)}/datasets/${enc(name)}`); setEditorLatest(latest.version); setEditing(dataset); }
    catch (error) { toast(error instanceof Error ? error.message : 'Could not read latest version', 'bad'); }
  }
  return <QueryView query={query}>{d => <>
    <div className="page-head"><h1>{d.name}</h1><span className="badge">v{d.version}</span><button className="btn primary" disabled={!access.may('operator')} onClick={() => newVersion(d)}>Register new version</button></div>
    <div className="toolbar"><label>Version <select aria-label="Dataset version" value={String(d.version)} onChange={event => setFilter({ version: event.target.value })}>{!versions.data?.items.some(v => v.version === d.version) && <option value={d.version}>v{d.version}</option>}{versions.data?.items.map(v => <option key={v.id} value={v.version}>v{v.version}</option>)}</select></label><a href={datasetUrl(project, name)}>Latest version</a></div>
    <section className="card"><Kv entries={[["Location", <span className="mono">{d.uri}</span>], ["Format", d.format], ["Rows", d.row_count?.toLocaleString() ?? 'Unknown'], ["Connection", <a href={connectionUrl(project, d.connection_id)}>Storage connection</a>], ["SHA-256", <span className="mono">{d.checksum_sha256 ?? 'Not supplied'}</span>], ["S3 object version", d.object_version_id ?? 'Not supplied'], ["Producer", <Producer project={project} dataset={d} />], ["Registered", <Time iso={d.created_at} />]]} /></section>
    <section className="section card"><h2>Schema</h2><Table head={['Column', 'Type', 'Nullable']} testid="dataset-schema">{d.columns.map(c => <tr key={c.name}><td>{c.name}</td><td>{c.dtype}</td><td>{c.nullable ? 'Yes' : 'No'}</td></tr>)}</Table></section>
    <section className="section card"><h2>Version history</h2><QueryView query={versions}>{list => <><Table head={['Version', 'Format', 'Rows', 'Registered']} testid="dataset-version-history">{list.items.map(version => <tr key={version.id}><td><a href={datasetUrl(project, name, version.version)}>v{version.version}</a></td><td>{version.format}</td><td>{version.row_count?.toLocaleString() ?? 'Unknown'}</td><td><Time iso={version.created_at} /></td></tr>)}</Table><Pagination offset={offset} count={list.items.length} change={value => setFilter({ offset: String(value) })} /></>}</QueryView></section>
    <DatasetLineage project={project} dataset={d} />
    {editing && <DatasetEditor project={project} existing={editing} expectedVersion={editorLatest} onClose={() => setEditing(null)} onSaved={next => { setEditing(null); versions.refetch(); setFilter({ version: String(next.version) }); }} />}
  </>}</QueryView>;
}

function DatasetLineage({ project, dataset }: { project: string; dataset: Dataset }) {
  const [selected, setSelected] = useState<string | null>(null);
  const query = useQuery({ queryKey: ['dataset-lineage', project, dataset.id], queryFn: () => api.get<S['DatasetLineage']>(`/projects/${enc(project)}/datasets/${enc(dataset.name)}/lineage?version=${dataset.version}`) });
  async function visit(node: S['LineageNode']) {
    setSelected(node.id);
    if (node.kind === 'DATASET') { const target = await api.get<Dataset>(`/projects/${enc(project)}/dataset-versions/${node.ref_id}`); go(datasetUrl(project, target.name, target.version)); }
    else if (node.kind === 'MODEL_VERSION') go(routes.model(project, node.name));
    else go(node.kind === 'JOB_RUN' ? routes.jobRun(project, node.ref_id) : routes.pipelineRun(project, node.ref_id));
  }
  const { toast } = useOverlays();
  return <section className="section card" data-testid="dataset-lineage"><h2>Lineage</h2><p className="muted small">Recorded dataset and model versions, producer runs and model-training pipelines. Select a node to open it.</p><QueryView query={query}>{graph => <>
    <Dag label="Dataset lineage" selected={selected} onSelect={id => { const node = graph.nodes.find(n => n.id === id); if (node) visit(node).catch(error => toast(error instanceof Error ? error.message : 'Could not open lineage', 'bad')); }} steps={graph.nodes.map(node => ({ step: node.id, label: node.name, status: node.status ?? 'REGISTERED', detail: `${node.kind.toLowerCase().replaceAll('_', ' ')}${node.version ? ` · v${node.version}` : ''}`, depends_on: graph.edges.filter(edge => edge.target === node.id).map(edge => edge.source) }))} />
    {graph.edges.length === 0 && <p className="muted small">No producer or upstream relationship is recorded for this version.</p>}{graph.truncated && <Alert>Open an upstream dataset to explore earlier lineage.</Alert>}
  </>}</QueryView></section>;
}

function DatasetEditor({ project, existing, expectedVersion = 0, onClose, onSaved }: { project: string; existing?: Dataset; expectedVersion?: number; onClose: () => void; onSaved: (dataset: Dataset) => void }) {
  const [name, setName] = useState(existing?.name ?? '');
  const [connection, setConnection] = useState(existing?.connection_id ?? '');
  const [uri, setUri] = useState(existing?.uri ?? '');
  const [format, setFormat] = useState<'CSV' | 'PARQUET'>(existing?.format ?? 'PARQUET');
  const [identity, setIdentity] = useState(existing?.object_version_id && !existing.checksum_sha256 ? 'version' : 'checksum');
  const [checksum, setChecksum] = useState(existing?.checksum_sha256 ?? '');
  const [objectVersion, setObjectVersion] = useState(existing?.object_version_id ?? '');
  const [rows, setRows] = useState(existing?.row_count?.toString() ?? '');
  const [columns, setColumns] = useState<S['ColumnIn'][]>(existing?.columns ?? [{ name: '', dtype: 'number', nullable: false }]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const connections = useQuery({ queryKey: ['connection-choices', project, existing?.connection_id], queryFn: async () => {
    const list = await api.get<S['ConnectionList']>(`/projects/${enc(project)}/data-connections?limit=200`);
    if (existing && !list.items.some(c => c.id === existing.connection_id)) list.items.push(await api.get<Connection>(`/projects/${enc(project)}/data-connections/${existing.connection_id}`));
    return list;
  } });
  function edit(index: number, patch: Partial<S['ColumnIn']>) { setColumns(values => values.map((value, i) => i === index ? { ...value, ...patch } : value)); }
  return <Modal open label={existing ? 'Register dataset version' : 'Register dataset'} onClose={onClose}><form onSubmit={async event => {
    event.preventDefault(); setError(''); setBusy(true);
    try {
      const count = rows === '' ? null : Number(rows);
      if (count !== null && (!Number.isSafeInteger(count) || count < 0)) throw new Error('Row count must be a nonnegative whole number.');
      const selectedConnection = connection || connections.data?.items[0]?.id;
      const dataset = await api.post<Dataset>(`/projects/${enc(project)}/datasets`, { name, connection_id: selectedConnection, uri, format, columns, row_count: count, checksum_sha256: identity === 'checksum' ? checksum : null, object_version_id: identity === 'version' ? objectVersion : null, expected_latest_version: expectedVersion });
      onSaved(dataset);
    } catch (failure) { setError(failure instanceof Error ? failure.message : 'Could not register dataset'); }
    finally { setBusy(false); }
  }}><h2>{existing ? 'Register dataset version' : 'Register dataset'}</h2><p className="muted">Register metadata for an existing object. Workloads verify its identity and schema before use.</p>
    {existing && <p className="muted small">Based on v{existing.version}; publishing against latest v{expectedVersion}. Older versions remain unchanged.</p>}
    <div className="field"><label>Name <input aria-label="Dataset name" value={name} required maxLength={40} readOnly={!!existing} onChange={event => setName(event.target.value)} /></label></div>
    <div className="field"><label>Connection <select aria-label="Dataset connection" value={connection || connections.data?.items[0]?.id || ''} required onChange={event => setConnection(event.target.value)}>{connections.data?.items.map(c => <option key={c.id} value={c.id}>{c.name} · {c.bucket}/{c.prefix}</option>)}</select></label></div>
    <div className="field"><label>S3 object URI <input aria-label="S3 object URI" value={uri} required placeholder="s3://ml-data/customers.parquet" onChange={event => setUri(event.target.value)} /></label></div>
    <div className="field"><label>Format <select aria-label="Dataset format" value={format} onChange={event => setFormat(event.target.value as 'CSV' | 'PARQUET')}><option value="PARQUET">Parquet</option><option value="CSV">CSV (UTF-8)</option></select></label></div>
    <div className="field"><label>Integrity identity <select aria-label="Integrity identity" value={identity} onChange={event => setIdentity(event.target.value)}><option value="checksum">SHA-256 checksum</option><option value="version">S3 object version</option></select></label></div>
    {identity === 'checksum' ? <div className="field"><label>SHA-256 checksum <input aria-label="SHA-256 checksum" value={checksum} required pattern="[a-f0-9]{64}" onChange={event => setChecksum(event.target.value)} /></label></div> : <div className="field"><label>S3 object version <input aria-label="S3 object version" value={objectVersion} required onChange={event => setObjectVersion(event.target.value)} /></label></div>}
    <div className="field"><label>Row count (optional) <input aria-label="Row count" value={rows} type="number" min="0" step="1" onChange={event => setRows(event.target.value)} /></label></div>
    <fieldset><legend>Columns</legend>{columns.map((column, index) => <div className="data-column" key={index}><input aria-label={`Column name ${index + 1}`} value={column.name} required maxLength={128} placeholder="income" onChange={event => edit(index, { name: event.target.value })} /><select aria-label={`Column type ${index + 1}`} value={column.dtype} onChange={event => edit(index, { dtype: event.target.value })}>{['string', 'integer', 'number', 'boolean'].map(type => <option key={type} value={type}>{type}</option>)}</select><label><input type="checkbox" checked={column.nullable ?? false} onChange={event => edit(index, { nullable: event.target.checked })} /> Nullable</label><button type="button" className="btn small" disabled={columns.length === 1} onClick={() => setColumns(values => values.filter((_, i) => i !== index))}>Remove</button></div>)}<button type="button" className="btn small" disabled={columns.length >= 512} onClick={() => setColumns(values => [...values, { name: '', dtype: 'number', nullable: false }])}>Add column</button></fieldset>
    {connections.isError && <Alert bad>Could not read project connections.</Alert>}{connections.data && !connections.data.items.length && <Alert>Register a connection first.</Alert>}{error && <Alert bad>{error}</Alert>}
    <div className="dlg-foot"><button className="btn" type="button" onClick={onClose}>Cancel</button><button className="btn primary" type="submit" data-testid="register-dataset-submit" disabled={busy || !connections.data?.items.length}>{busy ? 'Registering…' : 'Register version'}</button></div>
  </form></Modal>;
}
