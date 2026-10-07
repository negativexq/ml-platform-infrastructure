import { parseParameterObject } from '../components/ParameterFields';
import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, enc, type S } from '../api/client';
import { Alert, Badge, Empty, Table, Time } from '../components/bits';
import { Modal } from '../components/overlays';
import { useCrumbs } from '../lib/chrome';
import { roleIn, useAccess, useMe } from '../lib/me';
import { go, routes, shortId } from '../lib/format';
import { QueryView, useAct } from '../lib/query';
import { useSearchState } from '../lib/search';

type Schedule = S['ScheduleOut'];
type Execution = S['ExecutionOut'];
const url = (id: string) => `#/schedules/${id}`;

export function SchedulesPage({ project }: { project?: string }) {
  useCrumbs(project ? [{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) }, { label: 'Schedules' }] : [{ label: 'Schedules' }]);
  const [filters, setFilters] = useSearchState({ project: project ?? '', target_kind: '', status: '', target_name: '', offset: '0' });
  const [creating, setCreating] = useState(false);
  const me = useMe();
  const projects = useQuery({ queryKey: ['schedule-projects'], queryFn: () => api.get<S['ProjectList']>('/projects?limit=200') });
  const selectedProject = project ?? filters.project;
  const parameters = new URLSearchParams({ limit: '50', offset: filters.offset });
  if (selectedProject) parameters.set('project', selectedProject);
  if (filters.target_kind) parameters.set('target_kind', filters.target_kind);
  if (filters.target_name) parameters.set('target_name', filters.target_name);
  if (filters.status) parameters.set('paused', String(filters.status === 'paused'));
  const query = useQuery({ queryKey: ['schedules', parameters.toString()], queryFn: () => api.get<S['ScheduleList']>(`/schedules?${parameters}`), refetchInterval: 5000 });
  const available = (projects.data?.items ?? []).filter((p) => p.status === 'READY' && ['operator', 'admin'].includes(roleIn(me.data, p.name) ?? ''));
  const mayCreate = available.some((p) => !selectedProject || p.name === selectedProject);
  return <>
    <div className="page-head"><h1>Schedules</h1><button className="btn primary" disabled={!mayCreate} onClick={() => setCreating(true)} data-testid="create-schedule">Create schedule</button></div>
    <p className="sub">Recurring jobs and pipelines. View execution history, pause future work and follow each run.</p>
    <div className="toolbar" aria-label="Schedule filters">
      {!project && <label>Project <select aria-label="Project" value={filters.project} onChange={(e) => setFilters({ project: e.target.value, offset: '0' })}><option value="">All projects</option>{projects.data?.items.map((p) => <option key={p.id} value={p.name}>{p.display_name}</option>)}</select></label>}
      <label>Target <select aria-label="Target" value={filters.target_kind} onChange={(e) => setFilters({ target_kind: e.target.value, offset: '0' })}><option value="">All targets</option><option value="PIPELINE">Pipelines</option><option value="JOB">Jobs</option></select></label>
      <label>Status <select aria-label="Status" value={filters.status} onChange={(e) => setFilters({ status: e.target.value, offset: '0' })}><option value="">All statuses</option><option value="enabled">Enabled</option><option value="paused">Paused</option></select></label>
      <label>Name <input aria-label="Target name" value={filters.target_name} placeholder="Job or pipeline" onChange={(e) => setFilters({ target_name: e.target.value, offset: '0' })} /></label>
    </div>
    <QueryView query={query}>{(list) => <>
      {list.items.length === 0 ? <Empty>No schedules match these filters.</Empty> : <Table head={['Schedule', 'Project / Target', 'Status', 'Timing', 'Next execution', 'Last execution']} testid="schedules">
        {list.items.map((s) => <tr key={s.id}>
          <td><a href={url(s.id)}>{s.name}</a></td>
          <td><a href={`${routes.project(s.project_name)}/${s.target_kind === 'PIPELINE' ? 'pipelines' : 'jobs'}/${enc(s.target_name)}`}>{s.project_name} / {s.target_name}</a><div className="muted small">{s.target_kind.toLowerCase()} · {s.version_policy === 'LATEST' ? 'latest at occurrence' : s.version ? `v${s.version}` : 'immutable job'}</div></td>
          <td><span className={`badge ${s.paused ? '' : 'ok'}`}>{s.paused ? 'Paused' : 'Enabled'}</span></td>
          <td><span className="mono">{s.cron}</span><div className="muted small">{s.timezone}</div></td>
          <td>{s.paused ? 'Paused' : <Time iso={s.next_run_at} />}</td>
          <td>{s.last_execution ? <><ExecutionState execution={s.last_execution} /><div className="muted small"><Time iso={s.last_execution.scheduled_for_utc} /></div></> : '—'}</td>
        </tr>)}
      </Table>}
      <div className="actions section"><button className="btn" disabled={list.offset === 0} onClick={() => setFilters({ offset: String(Math.max(0, list.offset - list.limit)) })}>Previous</button><button className="btn" disabled={list.items.length < list.limit} onClick={() => setFilters({ offset: String(list.offset + list.limit) })}>Next</button></div>
    </>}</QueryView>
    {creating && <ScheduleEditor project={selectedProject || available[0]?.name || ''} projects={available} onClose={() => setCreating(false)} onSaved={(s) => { setCreating(false); go(url(s.id)); }} />}
  </>;
}

function ExecutionState({ execution: e }: { execution: Execution }) {
  return <>{e.run_status ? <Badge status={e.run_status} /> : <span className="badge">{e.status.toLowerCase()}</span>}</>;
}

export function SchedulePage({ id }: { id: string }) {
  const query = useQuery({ queryKey: ['schedule', id], queryFn: () => api.get<Schedule>(`/schedules/${id}`), refetchInterval: 5000 });
  useCrumbs([{ label: 'Schedules', href: '#/schedules' }, { label: query.data?.name ?? shortId(id) }]);
  const [editing, setEditing] = useState(false);
  const [offset, setOffset] = useState(0);
  const access = useAccess(query.data?.project_name ?? '');
  const act = useAct();
  const history = useQuery({ queryKey: ['schedule-executions', id, offset], queryFn: () => api.get<S['ExecutionList']>(`/schedules/${id}/executions?limit=50&offset=${offset}`), refetchInterval: 5000 });
  return <QueryView query={query}>{(s) => <>
    <div className="page-head"><h1>{s.name}</h1><span className="badge">{s.paused ? 'Paused' : 'Enabled'}</span><div className="actions">
      <button className="btn" disabled={!access.may('operator')} title={access.why('operator')} onClick={() => setEditing(true)}>Edit schedule</button>
      <button className="btn primary" disabled={!access.may('operator')} title={access.why('operator')} data-testid="pause-schedule" onClick={() => act(() => api.patch(`/schedules/${id}`, { expected_revision: s.revision, paused: !s.paused }), s.paused ? 'Schedule resumed' : 'Schedule paused')}>{s.paused ? 'Resume' : 'Pause'}</button>
    </div></div>
    <p className="sub"><a href={`${routes.project(s.project_name)}/${s.target_kind === 'PIPELINE' ? 'pipelines' : 'jobs'}/${enc(s.target_name)}`}>{s.project_name} / {s.target_name}</a> · {s.version_policy === 'LATEST' ? 'Latest, frozen at each occurrence' : s.version ? `Pinned v${s.version}` : 'Immutable job'} · revision {s.revision}</p>
    {s.paused && <Alert>New occurrences and queued dispatch are paused. Started runs continue. Queue deadlines apply when resumed.</Alert>}
    <div className="schedule-summary">
      <section className="card"><h2>Timing</h2><dl className="schedule-facts"><dt>Cron</dt><dd className="mono">{s.cron}</dd><dt>Timezone</dt><dd>{s.timezone}</dd><dt>Concurrency</dt><dd>{s.concurrency_policy} · {s.concurrency_scope}</dd><dt>Missed runs</dt><dd>{s.missed_run_policy} · {s.deadline_seconds}s deadline</dd><dt>Queue</dt><dd>{s.max_queue_size} occurrences · {s.queue_ttl_seconds}s lifetime</dd><dt>Run timeout</dt><dd>{s.timeout_seconds}s</dd></dl></section>
      <section className="card"><h2>{s.paused ? 'Timing preview (paused)' : 'Next executions'}</h2><ol>{s.next_executions.map((date) => <li key={date}><span title={date}>{new Date(date).toLocaleString(undefined, { timeZone: s.timezone })}</span> <span className="muted small">{s.timezone}</span></li>)}</ol><p className="muted small">DST: nonexistent local times are skipped; repeated times run once, at their first occurrence.</p></section>
    </div>
    <section className="section card"><h2>Execution history</h2><p className="muted small">Queued occurrences have no run until capacity opens. TARGET concurrency counts scheduled runs; manual runs are independent.</p>
      <QueryView query={history}>{(list) => list.items.length === 0 ? <Empty>No executions yet.</Empty> : <>
        <Table head={['Scheduled for', 'Intent', 'Run status', 'Run', 'Definition / Revision', 'Reason']} testid="schedule-executions">
          {list.items.map((e) => <tr key={e.id}><td><Time iso={e.scheduled_for_utc} /></td><td><span className="badge">{e.status.toLowerCase()}</span></td><td>{e.run_status ? <Badge status={e.run_status} /> : '—'}</td><td>{e.pipeline_run_id ? <a href={routes.pipelineRun(s.project_name, e.pipeline_run_id)}>{shortId(e.pipeline_run_id)}</a> : e.job_run_id ? <a href={routes.jobRun(s.project_name, e.job_run_id)}>{shortId(e.job_run_id)}</a> : 'Waiting / no run'}</td><td><span className="mono" title={e.resolved_definition_id ?? undefined}>{e.resolved_definition_id ? shortId(e.resolved_definition_id) : '—'}</span> · r{e.schedule_revision}</td><td>{e.reason ?? '—'}</td></tr>)}
        </Table><div className="actions section"><button className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous</button><button className="btn" disabled={list.items.length < 50} onClick={() => setOffset(offset + 50)}>Next</button></div>
      </>}</QueryView>
    </section>
    {editing && <ScheduleEditor project={s.project_name} schedule={s} onClose={() => setEditing(false)} onSaved={() => { setEditing(false); query.refetch(); }} />}
  </>}</QueryView>;
}

function ScheduleEditor({ project: initialProject, projects = [], schedule, onClose, onSaved }: { project: string; projects?: S['ProjectOut'][]; schedule?: Schedule; onClose: () => void; onSaved: (s: Schedule) => void }) {
  const [project, setProject] = useState(initialProject);
  const [kind, setKind] = useState<'PIPELINE' | 'JOB'>(schedule?.target_kind ?? 'PIPELINE');
  const [target, setTarget] = useState(schedule?.target_name ?? '');
  const [name, setName] = useState(schedule?.name ?? '');
  const [frequency, setFrequency] = useState(schedule ? 'advanced' : 'daily');
  const [time, setTime] = useState('03:00');
  const [weekday, setWeekday] = useState('1');
  const [minute, setMinute] = useState('0');
  const [customCron, setCustomCron] = useState(schedule?.cron ?? '0 3 * * *');
  const [timezone, setTimezone] = useState(schedule?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone ?? 'UTC');
  const [latest, setLatest] = useState(schedule?.version_policy === 'LATEST');
  const [version, setVersion] = useState(String(schedule?.version ?? 1));
  const [concurrency, setConcurrency] = useState<Schedule['concurrency_policy']>(schedule?.concurrency_policy ?? 'FORBID');
  const [scope, setScope] = useState<Schedule['concurrency_scope']>(schedule?.concurrency_scope ?? 'TARGET');
  const [missed, setMissed] = useState<Schedule['missed_run_policy']>(schedule?.missed_run_policy ?? 'SKIP');
  const [deadline, setDeadline] = useState(String(schedule?.deadline_seconds ?? 300));
  const [queueTTL, setQueueTTL] = useState(String(schedule?.queue_ttl_seconds ?? 86400));
  const [queueSize, setQueueSize] = useState(String(schedule?.max_queue_size ?? 100));
  const [timeout, setRunTimeout] = useState(String(schedule?.timeout_seconds ?? 3600));
  const [parameterText, setParameterText] = useState(JSON.stringify(schedule?.parameters ?? {}, null, 2));
  const [bindingText, setBindingText] = useState(JSON.stringify(schedule?.parameter_bindings ?? {}, null, 2));
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [hour, dailyMinute] = time.split(':').map(Number);
  const cron = frequency === 'daily' ? `${dailyMinute} ${hour} * * *` : frequency === 'weekly' ? `${dailyMinute} ${hour} * * ${weekday}` : frequency === 'hourly' ? `${Number(minute)} * * * *` : customCron;
  const [previewInput, setPreviewInput] = useState({ cron, timezone });
  useEffect(() => { const timer = setTimeout(() => setPreviewInput({ cron, timezone }), 300); return () => clearTimeout(timer); }, [cron, timezone]);
  const previewCurrent = cron === previewInput.cron && timezone === previewInput.timezone;
  const next = useQuery({ queryKey: ['schedule-preview', previewInput], queryFn: () => api.post<S['PreviewOut']>('/schedule-preview', previewInput), retry: false });
  const targets = useQuery({ queryKey: ['schedule-targets', project, kind], enabled: Boolean(project), queryFn: async () => {
    if (kind === 'PIPELINE') return (await api.get<S['PipelineList']>(`/projects/${enc(project)}/pipelines`)).items.map((p) => ({ name: p.name, version: p.version }));
    return (await api.get<S['JobList']>(`/projects/${enc(project)}/jobs`)).items.map((j) => ({ name: j.name, version: 1 }));
  }});
  return <Modal open label={schedule ? 'Edit schedule' : 'Create schedule'} onClose={onClose} className="schedule-modal"><form onSubmit={async (event) => {
    event.preventDefault(); setSaving(true); setError('');
    try {
      const spec = { parameters: parseParameterObject(parameterText), parameter_bindings: parseParameterObject(bindingText), cron, timezone, version_policy: kind === 'PIPELINE' && latest ? 'LATEST' : 'PINNED', version: kind === 'PIPELINE' && !latest ? Number(version) : null, concurrency_policy: concurrency, concurrency_scope: scope, missed_run_policy: missed, deadline_seconds: Number(deadline), queue_ttl_seconds: Number(queueTTL), max_queue_size: Number(queueSize), timeout_seconds: Number(timeout) };
      const saved = schedule ? await api.patch<Schedule>(`/schedules/${schedule.id}`, { expected_revision: schedule.revision, ...spec }) : await api.post<Schedule>(`/projects/${enc(project)}/schedules`, { name, target_kind: kind, target_name: target, ...spec });
      onSaved(saved);
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not save schedule'); } finally { setSaving(false); }
  }}>
    <h2>{schedule ? 'Edit schedule' : 'Create schedule'}</h2>
    {targets.isError && <Alert>Could not load job/pipeline targets. Close and retry.</Alert>}
    {!schedule && <div className="schedule-fields">
      <label>Project<select aria-label="Project" required value={project} onChange={(e) => { setProject(e.target.value); setTarget(''); }}>{projects.map((p) => <option key={p.id} value={p.name}>{p.display_name}</option>)}</select></label>
      <label>Name<input aria-label="Name" required pattern="[a-z][a-z0-9]*(-[a-z0-9]+)*" minLength={3} maxLength={40} placeholder="daily-credit-scoring" value={name} onChange={(e) => setName(e.target.value)} /></label>
      <label>Target type<select aria-label="Target type" value={kind} onChange={(e) => { setKind(e.target.value as 'PIPELINE' | 'JOB'); setTarget(''); }}><option value="PIPELINE">Pipeline</option><option value="JOB">Job</option></select></label>
      <label>Target<select aria-label="Target" required value={target} onChange={(e) => { setTarget(e.target.value); setVersion(String(targets.data?.find((t) => t.name === e.target.value)?.version ?? 1)); }}><option value="">Choose target</option>{targets.data?.map((t) => <option key={t.name} value={t.name}>{t.name}</option>)}</select></label>
    </div>}
    {kind === 'PIPELINE' && <div className="schedule-fields"><label>Version policy<select aria-label="Version policy" value={latest ? 'LATEST' : 'PINNED'} onChange={(e) => setLatest(e.target.value === 'LATEST')}><option value="PINNED">Pinned version</option><option value="LATEST">Latest at occurrence</option></select></label>{!latest && <label>Pipeline version<input aria-label="Pipeline version" type="number" min={1} required value={version} onChange={(e) => setVersion(e.target.value)} /></label>}</div>}
    <div className="schedule-fields">
      <label>Frequency<select aria-label="Frequency" value={frequency} onChange={(e) => setFrequency(e.target.value)}><option value="hourly">Hourly</option><option value="daily">Daily</option><option value="weekly">Weekly</option><option value="advanced">Advanced cron</option></select></label>
      {frequency === 'hourly' ? <label>Minute<input aria-label="Minute" type="number" min={0} max={59} required value={minute} onChange={(e) => setMinute(e.target.value)} /></label> : frequency !== 'advanced' ? <label>Local time<input aria-label="Local time" type="time" required value={time} onChange={(e) => setTime(e.target.value)} /></label> : <label>Cron<input aria-label="Cron" required value={customCron} onChange={(e) => setCustomCron(e.target.value)} placeholder="0 3 * * *" /></label>}
      {frequency === 'weekly' && <label>Day<select aria-label="Day" value={weekday} onChange={(e) => setWeekday(e.target.value)}>{['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'].map((d, i) => <option key={d} value={i}>{d}</option>)}</select></label>}
      <label>Timezone<input aria-label="Timezone" required list="schedule-timezones" value={timezone} onChange={(e) => setTimezone(e.target.value)} /><datalist id="schedule-timezones">{['UTC', 'Europe/Istanbul', 'Europe/London', 'America/New_York', 'Asia/Tokyo'].map((z) => <option key={z} value={z} />)}</datalist></label>
    </div>
    <div className="schedule-preview" aria-live="polite"><strong>Next executions</strong>{!previewCurrent || next.isPending ? <p>Calculating…</p> : next.isError ? <p className="bad">{next.error.message}</p> : <ul>{next.data?.executions.map((d) => <li key={d}>{new Date(d).toLocaleString(undefined, { timeZone: previewInput.timezone })} · {previewInput.timezone}</li>)}</ul>}</div>
    <details><summary>Run parameters</summary><div className="schedule-fields section"><label>Static parameters<textarea aria-label="Static parameters" value={parameterText} onChange={e => setParameterText(e.target.value)} /></label><label>Scheduled values<textarea aria-label="Scheduled values" value={bindingText} onChange={e => setBindingText(e.target.value)} placeholder={'{"processing_date": "processing_date"}'} /></label></div><p className="muted small">Bind a parameter to processing_date (local date) or scheduled_for (UTC timestamp). Values are frozen when the occurrence is recorded.</p></details>
    <details><summary>Execution policies</summary><div className="schedule-fields section">
      <label>Concurrency<select aria-label="Concurrency" value={concurrency} onChange={(e) => setConcurrency(e.target.value as Schedule['concurrency_policy'])}><option value="FORBID">Skip while busy</option><option value="QUEUE">Queue while busy</option><option value="ALLOW">Allow overlapping runs</option></select></label>
      <label>Concurrency scope<select aria-label="Concurrency scope" value={scope} onChange={(e) => setScope(e.target.value as Schedule['concurrency_scope'])}><option value="TARGET">All schedules for this target</option><option value="SCHEDULE">This schedule only</option></select></label>
      <label>Missed runs<select aria-label="Missed runs" value={missed} onChange={(e) => setMissed(e.target.value as Schedule['missed_run_policy'])}><option value="SKIP">Skip older occurrences</option><option value="CATCH_UP">Catch up within deadline</option></select></label>
      <label>Start deadline (seconds)<input aria-label="Start deadline (seconds)" type="number" min={1} max={604800} required value={deadline} onChange={(e) => setDeadline(e.target.value)} /></label>
      <label>Queue lifetime (seconds)<input aria-label="Queue lifetime (seconds)" type="number" min={1} max={604800} required value={queueTTL} onChange={(e) => setQueueTTL(e.target.value)} /></label>
      <label>Queue capacity<input aria-label="Queue capacity" type="number" min={1} max={1000} required value={queueSize} onChange={(e) => setQueueSize(e.target.value)} /></label>
      <label>Run timeout (seconds)<input aria-label="Run timeout (seconds)" type="number" min={1} max={604800} required value={timeout} onChange={(e) => setRunTimeout(e.target.value)} /></label>
    </div><p className="muted small">Manual runs are outside schedule concurrency. Pausing preserves queued work and lets started runs finish.</p></details>
    {error && <Alert bad>{error}</Alert>}
    <div className="actions section"><button className="btn" type="button" onClick={onClose}>Cancel</button><button className="btn primary" disabled={saving || !previewCurrent || next.isError || !next.data}>{saving ? 'Saving…' : 'Save schedule'}</button></div>
  </form></Modal>;
}

export function RunScheduleOrigin({ kind, id }: { kind: 'runs' | 'pipeline-runs'; id: string }) {
  const query = useQuery({ queryKey: ['run-schedule', kind, id], queryFn: () => api.get<Execution | null>(`/${kind}/${id}/schedule`), retry: false });
  if (!query.data) return null;
  return <p className="sub" data-testid="run-schedule-origin">Scheduled by <a href={url(query.data.schedule_id)}>schedule {shortId(query.data.schedule_id)}</a> for <Time iso={query.data.scheduled_for_utc} /> · revision {query.data.schedule_revision}</p>;
}
