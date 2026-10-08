import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, enc, type S } from '../api/client';
import { Empty, Table, Time } from '../components/bits';
import { useOverlays } from '../components/overlays';
import { useAccess } from '../lib/me';
import { useCrumbs } from '../lib/chrome';
import { go, routes } from '../lib/format';
import { QueryView } from '../lib/query';

export function BatchInferencePage({ project: initialProject }: { project?: string }) {
  const [project, setProject] = useState(initialProject ?? '');
  useCrumbs([{ label: 'Batch Inference' }]);
  const projects = useQuery({ queryKey: ['projects'], queryFn: () => api.get<S['ProjectList']>('/projects?limit=200') });
  const access = useAccess(project);
  const { form, toast } = useOverlays();
  const query = useQuery({ queryKey: ['batch-definitions', project], enabled: !!project, queryFn: () => api.get<S['JobList']>(`/projects/${enc(project)}/batch-inference`) });
  async function create() {
    try {
      const [datasets, connections, models] = await Promise.all([
        api.get<S['DatasetList']>(`/projects/${enc(project)}/datasets?limit=200`),
        api.get<S['ConnectionList']>(`/projects/${enc(project)}/data-connections?limit=200`),
        api.get<S['ModelList']>(`/projects/${enc(project)}/models`),
      ]);
      const versions = (await Promise.all(models.items.filter(m => m.kind === 'classic').map(async m => {
        const list = await api.get<{ items: S['ModelVersionOut'][] }>(`/projects/${enc(project)}/models/${enc(m.name)}/versions`);
        return list.items.map(v => ({ value: v.id, label: `${m.name} · v${v.version}` }));
      }))).flat();
      if (!datasets.items.length || !connections.items.length || !versions.length) { toast('Register a dataset, S3 connections and a classic model version first.', 'bad'); return; }
      const options = connections.items.map(c => ({ value: c.id, label: `${c.name} · ${c.bucket}/${c.prefix}` }));
      const job = await form<S['JobOut']>({ title: 'Create batch inference', submitLabel: 'Create batch',
        intro: 'The input dataset and model version are pinned. Runs preserve input columns and add one prediction per row.',
        fields: [
          { name: 'name', label: 'Name', required: true, pattern: '^[a-z][a-z0-9]*(-[a-z0-9]+)*$', placeholder: 'daily-scoring' },
          { name: 'input_dataset_id', label: 'Input dataset version', required: true, options: datasets.items.map(d => ({ value: d.id, label: `${d.name} · v${d.version} · ${d.format}` })) },
          { name: 'model_version_id', label: 'Model version', required: true, options: versions },
          { name: 'model_connection_id', label: 'Model artifact connection', required: true, options },
          { name: 'output_connection_id', label: 'Output connection', required: true, options },
          { name: 'output_dataset', label: 'Output dataset name', required: true, placeholder: 'daily-predictions' },
          { name: 'features', label: 'Model feature columns', required: true, placeholder: 'income, age', hint: 'Comma-separated names in model input order.' },
          { name: 'output_format', label: 'Output format', value: 'PARQUET', options: [{ value: 'PARQUET', label: 'Parquet' }, { value: 'CSV', label: 'CSV' }] },
          { name: 'prediction_dtype', label: 'Prediction type', value: 'number', options: ['number', 'integer', 'string', 'boolean'].map(value => ({ value, label: value })) },
          { name: 'cpu', label: 'CPU', value: '1', hint: 'CPU request and limit, for example 1 or 500m.' },
          { name: 'memory', label: 'Memory', value: '2Gi', hint: 'Memory request and limit. Small local acceptance datasets can use 1Gi.' },
          { name: 'batch_size', label: 'Rows per prediction batch', value: '1000', pattern: '^[1-9][0-9]*$' },
        ], submit: values => { const { cpu, memory, ...fields } = values; return api.post<S['JobOut']>(`/projects/${enc(project)}/batch-inference`, { ...fields, resources: { cpu, memory }, features: (values.features ?? '').split(',').map(s => s.trim()).filter(Boolean), batch_size: Number(values.batch_size) }); },
      });
      if (job) { toast('Batch definition created'); query.refetch(); }
    } catch (error) { toast(error instanceof Error ? error.message : 'Could not create batch inference', 'bad'); }
  }
  return <>
    <div className="page-head"><h1>Batch Inference</h1><button className="btn primary" disabled={!project || !access.may('operator')} onClick={create}>Create batch inference</button></div>
    <p className="sub">Score versioned CSV or Parquet datasets with classic ML models. Run now, use in a pipeline, or schedule recurring execution.</p>
    {!initialProject && <div className="toolbar"><label>Project <select aria-label="Project" value={project} onChange={e => setProject(e.target.value)}><option value="">Select a project</option>{projects.data?.items.map(p => <option key={p.id} value={p.name}>{p.display_name}</option>)}</select></label></div>}
    {!project ? <Empty>Select a project to view its batch definitions.</Empty> : <QueryView query={query}>{list => list.items.length === 0 ? <Empty>No batch definitions yet.</Empty> : <Table head={['Definition', 'Input', 'Output dataset', 'Format', 'Created', '']} testid="batch-definitions">{list.items.map(job => {
      const spec = job.batch_spec as Record<string, unknown>;
      const input = spec.input as { name: string; version: number };
      return <tr key={job.id}><td><a href={`${routes.project(project)}/jobs/${enc(job.name)}`}>{job.name}</a></td><td>{input.name} · v{input.version}</td><td>{String(spec.output_dataset)}</td><td>{String(spec.output_format)}</td><td><Time iso={job.created_at} /></td><td><button className="btn small" disabled={!access.may('operator')} onClick={async () => { try { const run = await api.post<S['RunOut']>(`/projects/${enc(project)}/jobs/${enc(job.name)}/runs`, {}); go(routes.jobRun(project, run.id)); } catch (error) { toast(error instanceof Error ? error.message : 'Could not start batch', 'bad'); } }}>Run now</button></td></tr>;
    })}</Table>}</QueryView>}
  </>;
}

export function BatchOutputs({ project, runId, pipeline = false }: { project: string; runId: string; pipeline?: boolean }) {
  const query = useQuery({ queryKey: ['batch-outputs', runId, pipeline], queryFn: () => api.get<S['DatasetList']>(`/projects/${enc(project)}/datasets?${pipeline ? 'producer_pipeline_run_id' : 'producer_run_id'}=${enc(runId)}`), refetchInterval: 5000 });
  if (!query.data?.items.length) return null;
  return <section className="section card" data-testid="batch-outputs"><h2>Output datasets</h2><Table head={['Dataset', 'Rows', 'Format', 'Location', 'Integrity']}>{query.data.items.map(d => <tr key={d.id}><td><a href={`${routes.project(project)}/datasets/${enc(d.name)}?version=${d.version}`}>{d.name} · v{d.version}</a></td><td>{d.row_count?.toLocaleString() ?? '—'}</td><td>{d.format}</td><td className="mono small">{d.uri}</td><td className="mono small" title={d.checksum_sha256 ?? ''}>{d.checksum_sha256?.slice(0, 12)}…</td></tr>)}</Table></section>;
}
