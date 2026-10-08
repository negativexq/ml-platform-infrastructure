import { MonitoringRules } from './MonitoringRules';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, enc, type S } from '../api/client';
import { Alert, Badge, Empty, Kv, Table, Time } from '../components/bits';
import { useOverlays } from '../components/overlays';
import { useAccess } from '../lib/me';
import { useCrumbs } from '../lib/chrome';
import { go, pct, routes } from '../lib/format';
import { QueryView } from '../lib/query';
import { useSearchState } from '../lib/search';

type Report = S['MonitoringReportOut'];
const reportUrl = (project: string, id: string) => `${routes.project(project)}/model-monitoring/reports/${id}`;
const score = (value: number | null | undefined) => value == null ? 'Not measured' : value.toFixed(3);

export function ModelMonitoringPage({ project: fixedProject }: { project?: string }) {
  const [filters, setFilters] = useSearchState({ project: '', offset: '0' });
  const project = fixedProject ?? filters.project;
  const offset = Math.max(0, Number(filters.offset) || 0);
  useCrumbs([{ label: 'Model Monitoring' }]);
  const projects = useQuery({ queryKey: ['projects'], queryFn: () => api.get<S['ProjectList']>('/projects?limit=200') });
  const checks = useQuery({ queryKey: ['monitoring-checks', project], enabled: !!project, queryFn: () => api.get<S['JobList']>(`/projects/${enc(project)}/model-monitoring/checks`) });
  const reports = useQuery({ queryKey: ['monitoring-reports', project, offset], enabled: !!project, queryFn: () => api.get<S['MonitoringReportList']>(`/projects/${enc(project)}/model-monitoring/reports?limit=50&offset=${offset}`), refetchInterval: 10000 });
  const queryClient = useQueryClient();
  const access = useAccess(project);
  const { form, toast } = useOverlays();
  async function create(automatic = false) {
    try {
      const [datasets, models] = await Promise.all([
        api.get<S['DatasetList']>(`/projects/${enc(project)}/datasets?limit=200`),
        api.get<S['ModelList']>(`/projects/${enc(project)}/models`),
      ]);
      const versions = (await Promise.all(models.items.filter(m => m.kind === 'classic').map(async model => {
        const list = await api.get<{ items: S['ModelVersionOut'][] }>(`/projects/${enc(project)}/models/${enc(model.name)}/versions`);
        return list.items.map(v => ({ value: v.id, label: `${model.name} · v${v.version}` }));
      }))).flat();
      if (!datasets.items.length || !versions.length) { toast('Register datasets and a classic model version first.', 'bad'); return; }
      const options = datasets.items.map(d => ({ value: d.id, label: `${d.name} · v${d.version} · ${d.format}` }));
      const job = await form<S['JobOut'] | S['RuleOut']>({ title: automatic ? 'Create dataset-triggered rule' : 'Create monitoring check', submitLabel: automatic ? 'Create rule' : 'Create check',
        intro: 'Pin reference and observed dataset versions. Drift measures distribution changes; delayed feedback measures predictive quality.',
        fields: [
          { name: 'name', label: 'Check name', required: true, placeholder: 'credit-quality-october' },
          { name: 'model_version_id', label: 'Model version', required: true, options: versions },
          { name: 'reference_dataset_id', label: 'Reference dataset', required: true, options },
          automatic ? { name: 'observed_dataset_name', label: 'Observed dataset catalog', required: true, options: [...new Set(datasets.items.map(d => d.name))].map(name => ({ value: name, label: name })) } : { name: 'observed_dataset_id', label: 'Observed dataset', required: true, options },
          { name: 'features', label: 'Feature columns', required: true, placeholder: 'income, age', hint: 'Up to 32 matching columns, separated by commas.' },
          { name: 'psi_threshold', label: 'PSI threshold', value: '0.2', hint: 'Configurable distribution-change threshold; not a statistical significance test.' },
          { name: 'missing_rate_threshold', label: 'Missing-rate change', value: '0.1', hint: 'Absolute fraction change; 0.1 means 10 percentage points.' },
          { name: 'minimum_rows', label: 'Minimum rows', value: '100', pattern: '^[1-9][0-9]*$', hint: 'Smaller samples are labelled insufficient data.' },
          automatic ? { name: 'feedback_dataset_name', label: 'Ground-truth catalog', value: '', options: [{ value: '', label: 'No feedback — drift only' }, ...[...new Set(datasets.items.map(d => d.name))].map(name => ({ value: name, label: name }))], hint: 'Resolve a version with the same processing date; wait up to 24 hours.' } : { name: 'feedback_dataset_id', label: 'Ground-truth dataset', value: '', options: [{ value: '', label: 'No feedback — drift only' }, ...options] },
          { name: 'task', label: 'Prediction task', value: 'REGRESSION', options: [{ value: 'REGRESSION', label: 'Regression' }, { value: 'CLASSIFICATION', label: 'Classification (predicted labels)' }] },
          { name: 'entity_key', label: 'Entity / request ID column', placeholder: 'request_id', hint: 'Required with feedback. Keys must be unique in both datasets.' },
          { name: 'prediction_column', label: 'Prediction column', value: 'prediction' },
          { name: 'label_column', label: 'Ground-truth column', value: 'actual' },
          { name: 'cpu', label: 'CPU', value: '1' },
          { name: 'memory', label: 'Memory', value: '2Gi' },
        ], submit: values => {
          const { cpu, memory, ...fields } = values;
          return api.post<S['JobOut'] | S['RuleOut']>(`/projects/${enc(project)}/model-monitoring/${automatic ? 'rules' : 'checks'}`, { ...fields, resources: { cpu, memory }, ...(automatic ? { feedback_dataset_name: values.feedback_dataset_name || null } : { feedback_dataset_id: values.feedback_dataset_id || null }), features: (values.features ?? '').split(',').map(s => s.trim()).filter(Boolean), psi_threshold: Number(values.psi_threshold), missing_rate_threshold: Number(values.missing_rate_threshold), minimum_rows: Number(values.minimum_rows) });
        },
      });
      if (job) { toast(automatic ? 'Monitoring rule created' : 'Monitoring check created'); checks.refetch(); if (automatic) queryClient.invalidateQueries({ queryKey: ['monitoring-rules', project] }); }
    } catch (error) { toast(error instanceof Error ? error.message : 'Could not create monitoring check', 'bad'); }
  }
  return <>
    <div className="page-head"><h1>Model Monitoring</h1><button className="btn primary" disabled={!project || !access.may('operator')} onClick={() => create()}>Create monitoring check</button></div>
    <p className="sub">Feature drift and delayed ground-truth measurements for classic models. Each check preserves its dataset and model versions.</p>
    {!fixedProject && <div className="toolbar"><label>Project <select aria-label="Project" value={project} onChange={e => setFilters({ project: e.target.value, offset: '0' })}><option value="">Select a project</option>{projects.data?.items.map(p => <option key={p.id} value={p.name}>{p.display_name}</option>)}</select></label></div>}
    {!project ? <Empty>Select a project to browse model-quality checks.</Empty> : <>
      <MonitoringRules project={project} create={() => create(true)} />
      <section className="card"><h2>Checks</h2><QueryView query={checks}>{list => !list.items.length ? <Empty>No monitoring checks yet.</Empty> : <Table head={['Check', 'Model', 'Reference → Observed', 'Features', '']} testid="monitoring-checks">{list.items.map(job => {
        const spec = job.monitoring_spec as { model_name: string; model_version: number; reference: { name: string; version: number }; observed: { name: string; version: number }; features: string[] };
        return <tr key={job.id}><td><a href={`${routes.project(project)}/jobs/${enc(job.name)}`}>{job.name}</a></td><td>{spec.model_name} · v{spec.model_version}</td><td>{spec.reference.name} · v{spec.reference.version} → {spec.observed.name} · v{spec.observed.version}</td><td>{spec.features.join(', ')}</td><td><button className="btn small" disabled={!access.may('operator')} onClick={async () => { try { const run = await api.post<S['RunOut']>(`/projects/${enc(project)}/jobs/${enc(job.name)}/runs`, {}); go(routes.jobRun(project, run.id)); } catch (error) { toast(error instanceof Error ? error.message : 'Could not start monitoring', 'bad'); } }}>Run now</button></td></tr>;
      })}</Table>}</QueryView></section>
      <section className="section card"><h2>Report history</h2><QueryView query={reports}>{list => <>
        {!list.items.length ? <Empty>No completed measurements. Start a check to create a report.</Empty> : <Table head={['Check', 'Model', 'Feature drift', 'Reference / Observed rows', 'Feedback coverage', 'Measured']} testid="monitoring-reports">{list.items.map(report => <tr key={report.id}><td><a href={reportUrl(project, report.id)}>{report.check_name}</a></td><td><a href={`${routes.model(project, report.model_name)}?version=${report.model_version_id}`}>{report.model_name} · v{report.model_version}</a></td><td><Badge status={report.status} /></td><td>{report.result.reference_rows.toLocaleString()} / {report.result.observed_rows.toLocaleString()}</td><td>{report.result.performance ? pct(report.result.performance.coverage, 1) : 'No feedback'}</td><td><Time iso={report.created_at} /></td></tr>)}</Table>}
        <div className="actions section"><button className="btn" disabled={!offset} onClick={() => setFilters({ offset: String(Math.max(0, offset - 50)) })}>Previous</button><button className="btn" disabled={list.items.length < 50} onClick={() => setFilters({ offset: String(offset + 50) })}>Next</button></div>
      </>}</QueryView></section>
    </>}
  </>;
}

function DatasetLink({ project, id }: { project: string; id: string }) {
  const query = useQuery({ queryKey: ['dataset-identity', project, id], queryFn: () => api.get<S['DatasetOut']>(`/projects/${enc(project)}/dataset-versions/${id}`) });
  if (query.isPending) return <span className="muted small">Loading dataset…</span>;
  if (query.isError) return <span className="muted small">Dataset unavailable</span>;
  const d = query.data;
  return <a href={`${routes.project(project)}/datasets/${enc(d.name)}?version=${d.version}`}>{d.name} · v{d.version}</a>;
}

export function MonitoringReportPage({ project, id }: { project: string; id: string }) {
  useCrumbs([{ label: 'Model Monitoring', href: `${routes.project(project)}/model-monitoring` }, { label: 'Report' }]);
  const report = useQuery({ queryKey: ['monitoring-report', project, id], queryFn: () => api.get<Report>(`/projects/${enc(project)}/model-monitoring/reports/${id}`) });
  const check = useQuery({ queryKey: ['monitoring-report-check', project, report.data?.check_name], enabled: !!report.data, queryFn: () => api.get<S['JobOut']>(`/projects/${enc(project)}/model-monitoring/checks/${enc(report.data!.check_name)}`) });
  return <QueryView query={report}>{r => <>
    <div className="page-head"><h1><a href={`${routes.model(project, r.model_name)}?version=${r.model_version_id}`}>{r.model_name} · v{r.model_version}</a></h1><span className="muted small">Feature drift</span><Badge status={r.status} /></div>
    <p className="sub">{r.check_name} · measured <Time iso={r.created_at} />. Drift measures distribution change; predictive quality requires ground truth.</p>
    {r.status === 'INSUFFICIENT_DATA' && <Alert>Too few rows or an unavailable reference distribution. This report does not establish stability.</Alert>}
    {r.status === 'DRIFTED' && <Alert>Feature distributions or missing rates exceeded this check’s thresholds. Review the affected features before deciding on retraining.</Alert>}
    <section className="section card"><Kv entries={[["Reference", <DatasetLink project={project} id={r.reference_dataset_id} />], ["Observed", <DatasetLink project={project} id={r.observed_dataset_id} />], ["Ground truth", r.feedback_dataset_id ? <DatasetLink project={project} id={r.feedback_dataset_id} /> : 'Not supplied'], ["Sample rows", `${r.result.reference_rows.toLocaleString()} reference / ${r.result.observed_rows.toLocaleString()} observed`], ["Execution", <a href={r.job_run_id ? routes.jobRun(project, r.job_run_id) : routes.pipelineRun(project, r.pipeline_run_id!)}>{r.job_run_id ? 'Job run' : `Pipeline step: ${r.step}`}</a>]]} /></section>
    <section className="section card"><h2>Feature drift</h2><p className="muted small">PSI uses fixed reference bins and 0.5 smoothing per bin. Missing-rate change is measured in percentage points.</p><QueryView query={check}>{job => {
      const spec = job.monitoring_spec as { psi_threshold: number; missing_rate_threshold: number; minimum_rows: number };
      return <p className="muted small">Thresholds: PSI ≥ {spec.psi_threshold}, missing-rate change ≥ {pct(spec.missing_rate_threshold, 1)}; at least {spec.minimum_rows.toLocaleString()} rows in each dataset.</p>;
    }}</QueryView><div className="table-scroll" tabIndex={0} role="region" aria-label="Feature drift scores"><Table head={['Feature', 'PSI', 'Reference missing', 'Observed missing', 'Change', 'Assessment']} testid="monitoring-features">{r.result.features.map(f => <tr key={f.name}><td>{f.name}<div className="muted small">{f.dtype}</div></td><td>{score(f.psi)}</td><td>{pct(f.reference_missing_rate, 1)}</td><td>{pct(f.observed_missing_rate, 1)}</td><td>{f.missing_rate_change == null ? 'Not measured' : `${(f.missing_rate_change * 100).toFixed(1)} pp`}</td><td><Badge status={f.status} /></td></tr>)}</Table></div></section>
    <section className="section card"><h2>Predictive quality</h2>{!r.result.performance ? <Empty>No ground truth supplied. Feature drift alone does not measure prediction errors.</Empty> : <>
      <Kv entries={[["Task", r.result.performance.task], ["Matched rows", r.result.performance.matched_rows.toLocaleString()], ["Prediction coverage", pct(r.result.performance.coverage, 1)], ["Predictions without labels", r.result.performance.unmatched_predictions.toLocaleString()], ["Labels without predictions", r.result.performance.unmatched_truth.toLocaleString()]]} />
      {r.result.performance.status === 'INSUFFICIENT_DATA' && <Alert>Too few matched rows to report predictive quality.</Alert>}
      <Table head={['Metric', 'Value']} testid="monitoring-performance">{Object.entries(r.result.performance.metrics).map(([metric, value]) => <tr key={metric}><td>{metric.toUpperCase().replace('_', ' ')}</td><td>{score(value)}</td></tr>)}</Table>
      <p className="muted small">Constant ground truth leaves R² undefined. Classification uses predicted labels; no probability-based AUC is inferred.</p>
    </>}</section>
  </>}</QueryView>;
}

export function MonitoringOutputs({ project, runId, pipeline = false, active = false }: { project: string; runId: string; pipeline?: boolean; active?: boolean }) {
  const query = useQuery({ queryKey: ['monitoring-outputs', project, runId, pipeline, active], queryFn: () => api.get<S['MonitoringReportList']>(`/projects/${enc(project)}/model-monitoring/reports?${pipeline ? 'pipeline_run_id' : 'job_run_id'}=${enc(runId)}`), refetchInterval: active ? 5000 : false });
  if (!query.data?.items.length) return null;
  return <section className="section card" data-testid="monitoring-outputs"><h2>Model-quality reports</h2><Table head={['Check', 'Model', 'Feature drift', 'Observed rows']}>{query.data.items.map(report => <tr key={report.id}><td><a href={reportUrl(project, report.id)}>{report.check_name}</a></td><td><a href={`${routes.model(project, report.model_name)}?version=${report.model_version_id}`}>{report.model_name} · v{report.model_version}</a></td><td><Badge status={report.status} /></td><td>{report.result.observed_rows.toLocaleString()}</td></tr>)}</Table></section>;
}
