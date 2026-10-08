import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, enc, type S } from '../api/client';
import { Badge, Empty, Table, Time } from '../components/bits';
import { useAccess } from '../lib/me';
import { routes } from '../lib/format';
import { QueryView } from '../lib/query';
import { useOverlays } from '../components/overlays';

type Rule = S['RuleOut'];
type Execution = S['MonitoringExecutionOut'];

export function MonitoringRules({ project, create }: { project: string; create: () => void }) {
  const [selected, select] = useState('');
  const access = useAccess(project);
  const { toast } = useOverlays();
  const base = `/projects/${enc(project)}/model-monitoring/rules`;
  const rules = useQuery({ queryKey: ['monitoring-rules', project], queryFn: () => api.get<Rule[]>(base), refetchInterval: 10000 });
  const executions = useQuery({ queryKey: ['monitoring-executions', project, selected], enabled: !!selected, queryFn: () => api.get<Execution[]>(`${base}/${selected}/executions`), refetchInterval: 5000 });
  return <section className="section card" data-testid="monitoring-rules">
    <div className="page-head"><h2>Dataset-triggered rules</h2><button className="btn" disabled={!access.may('operator')} onClick={create}>Create monitoring rule</button></div>
    <p className="muted small">Every new observed dataset version creates one immutable monitoring execution. Ground truth is selected for the same processing date. Pausing stops new triggers; existing executions continue.</p>
    <QueryView query={rules}>{items => !items.length ? <Empty>No automatic monitoring rules yet.</Empty> : <Table head={['Rule', 'Observed catalog', 'Ground truth', 'State', '']}>
      {items.map(rule => <tr key={rule.id}><td><button className="link" onClick={() => select(rule.id)}>{rule.name}</button></td><td><a href={`${routes.project(project)}/datasets/${enc(rule.observed_dataset_name)}`}>{rule.observed_dataset_name}</a></td><td>{rule.feedback_dataset_name ? `${rule.feedback_dataset_name} · same processing date` : 'Drift only'}</td><td><Badge status={rule.enabled ? 'ACTIVE' : 'PAUSED'} /></td><td><button className="btn small" disabled={!access.may('operator')} onClick={async () => { try { await api.patch(`${base}/${rule.id}`, { enabled: !rule.enabled, revision: rule.revision }); rules.refetch(); } catch (error) { toast(error instanceof Error ? error.message : 'Rule update failed', 'bad'); } }}>{rule.enabled ? 'Pause' : 'Resume'}</button></td></tr>)}
    </Table>}</QueryView>
    {selected && <><h3>Trigger history · {rules.data?.find(r => r.id === selected)?.name}</h3><QueryView query={executions}>{items => !items.length ? <Empty>Waiting for a new dataset version.</Empty> : <Table head={['Observed version', 'Execution', 'Run / report', 'Created']}>
      {items.map(item => <tr key={item.id}><td><ObservedVersion project={project} id={item.observed_dataset_id} /></td><td><Badge status={item.status} />{item.reason && <div className="muted small">{item.reason}</div>}{item.status === 'WAITING_FEEDBACK' && <div className="muted small">Waiting until <Time iso={item.deadline_at} /></div>}</td><td>{item.job_run_id ? <ExecutionLinks project={project} runId={item.job_run_id} /> : 'No run created'}</td><td><Time iso={item.created_at} /></td></tr>)}
    </Table>}</QueryView></>}
  </section>;
}

function ObservedVersion({ project, id }: { project: string; id: string }) {
  const query = useQuery({ queryKey: ['dataset-identity', project, id], queryFn: () => api.get<S['DatasetOut']>(`/projects/${enc(project)}/dataset-versions/${id}`) });
  return query.data ? <a href={`${routes.project(project)}/datasets/${enc(query.data.name)}?version=${query.data.version}`}>{query.data.name} · v{query.data.version}</a> : <span>Loading dataset…</span>;
}

function ExecutionLinks({ project, runId }: { project: string; runId: string }) {
  const run = useQuery({ queryKey: ['automation-run', project, runId], queryFn: () => api.get<S['RunOut']>(`/runs/${runId}`), refetchInterval: 5000 });
  const reports = useQuery({ queryKey: ['automation-report', project, runId], queryFn: () => api.get<S['MonitoringReportList']>(`/projects/${enc(project)}/model-monitoring/reports?job_run_id=${runId}`), refetchInterval: 5000 });
  return <><a href={routes.jobRun(project, runId)}>Run {run.data?.status ?? ''}</a>{reports.data?.items.map(report => <div key={report.id}><a href={`${routes.project(project)}/model-monitoring/reports/${report.id}`}>Report · {report.status}</a></div>)}</>;
}
