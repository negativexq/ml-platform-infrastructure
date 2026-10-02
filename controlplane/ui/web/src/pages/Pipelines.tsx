import { useState } from 'react';
import { api, enc, type S } from '../api/client';
import { Badge, Empty, Section, Snippet, Table, Time } from '../components/bits';
import { Dag } from '../components/Dag';
import { useOverlays } from '../components/overlays';
import { useAccess } from '../lib/me';
import { useCrumbs } from '../lib/chrome';
import { fmtDuration, go, pct, routes, shortId } from '../lib/format';
import { QueryView, useLiveQuery } from '../lib/query';
import { useSearchState } from '../lib/search';
import { runStats } from '../lib/stats';
import { PipelineRunTable } from './Runs';
import { RunHistory } from '../components/charts/RunHistory';

const ACTIVE = new Set(['PENDING', 'SUBMITTED', 'RUNNING']);
const HISTORY = 20;

/** Ask for a commit and start a run of `pipeline` (latest version unless one is given). */
export function useRunPipeline(project: string) {
  const { form, toast } = useOverlays();
  return async (pipelines: { name: string; version: number }[], preselect?: string, version?: number) => {
    const run = await form<S['PipelineRunOut']>({
      title: 'Run a pipeline', submitLabel: 'Start run',
      intro: version ? `Runs version ${version}. You can follow it live on the next page.` : 'Runs the latest version of the pipeline. You can follow it live on the next page.',
      fields: [
        { name: 'pipeline', label: 'Pipeline', required: true, value: preselect,
          options: pipelines.map((x) => ({ value: x.name, label: `${x.name} (v${version ?? x.version})` })) },
        { name: 'commit_sha', label: 'Commit', placeholder: 'a83d2c1', hint: 'Optional. Recorded on the run and on every model it registers.' },
      ],
      submit: (v) => {
        const pipeline = v.pipeline ?? '';
        const pinned = version ? `?version=${version}` : '';
        return api.post<S['PipelineRunOut']>(`/projects/${enc(project)}/pipelines/${enc(pipeline)}/runs` + pinned, v.commit_sha ? { commit_sha: v.commit_sha } : {});
      },
    });
    if (run) { toast('Run started'); go(routes.pipelineRun(project, run.id)); }
  };
}

export function PipelinesPage({ project }: { project: string }) {
  const access = useAccess(project);
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) }, { label: 'Pipelines' }]);
  const p = enc(project);
  const runPipeline = useRunPipeline(project);
  const query = useLiveQuery(['pipelines', project], async () => {
    const { items } = await api.get<S['PipelineList']>(`/projects/${p}/pipelines`);
    const runs = await Promise.all(items.map((x) =>
      api.get<S['PipelineRunList']>(`/projects/${p}/pipeline-runs?pipeline=${enc(x.name)}&limit=${HISTORY}`).then((r) => r.items)));
    return items.map((pipeline, i) => ({ pipeline, runs: runs[i] ?? [] }));
  }, (rows) => rows.some((r) => r.runs.some((x) => ACTIVE.has(x.status))));

  return (
    <QueryView query={query}>
      {(rows) => (
        <>
          <div className="page-head"><h1>Pipelines</h1></div>
          <p className="sub">Multi-step workflows. Health is measured over each pipeline's last {HISTORY} runs.</p>
          {rows.length === 0 ? (
            <>
              <Empty>No pipelines yet. Pipelines are defined in code and registered through the API, usually from CI.</Empty>
              <Snippet label="Register a pipeline from CI" code={pipelineCurl(project, 'training', [
                { name: 'prepare', job: 'prepare-data', depends_on: [] }, { name: 'train', job: 'train-model', depends_on: ['prepare'] }])} />
            </>
          ) : (
            <Table testid="pipelines" head={['Pipeline', 'Version', 'Steps', 'Last run', 'Success rate', 'Typical duration', '']}>
              {rows.map(({ pipeline: x, runs }) => {
                const st = runStats(runs);
                return (
                  <tr key={x.id} className="click" data-testid="pipeline-row" onClick={() => go(`${routes.project(project)}/pipelines/${enc(x.name)}`)}>
                    <td><a href={`${routes.project(project)}/pipelines/${enc(x.name)}`}>{x.name}</a></td>
                    <td className="mono">{`v${x.version}`}</td><td className="num">{x.steps.length}</td>
                    <td>{st.last ? <><Badge status={st.last.status} /> <Time iso={st.last.created_at} /></> : <span className="muted">never run</span>}</td>
                    <td className="num" data-testid="success-rate">{st.successRate == null ? '—' : <Rate value={st.successRate} n={st.finished} />}</td>
                    <td className="num">{fmtDuration(st.medianSeconds)}</td>
                    <td className="num"><button className="btn small" type="button" disabled={!access.may('operator')} title={access.why('operator')} onClick={(e) => { e.stopPropagation(); runPipeline(rows.map((r) => r.pipeline), x.name); }}>Run</button></td>
                  </tr>);
              })}
            </Table>
          )}
        </>
      )}
    </QueryView>
  );
}

function Rate({ value, n }: { value: number; n: number }) {
  const tone = value >= 0.9 ? 'ok' : value >= 0.6 ? 'warn' : 'bad';
  return <span className={`rate ${tone}`} title={`over the last ${n} finished runs`}>{pct(value, 0)}</span>;
}

export function PipelinePage({ project, name }: { project: string; name: string }) {
  const access = useAccess(project);
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) },
    { label: 'Pipelines', href: `${routes.project(project)}/pipelines` }, { label: name }]);
  const p = enc(project);
  const runPipeline = useRunPipeline(project);
  const [{ version }, update] = useSearchState({ version: '' });
  const [selected, setSelected] = useState<string | null>(null);

  const query = useLiveQuery(['pipeline', project, name, version], async () => {
    const [latest, def, runs] = await Promise.all([
      api.get<S['PipelineOut']>(`/projects/${p}/pipelines/${enc(name)}`),
      api.get<S['PipelineOut']>(`/projects/${p}/pipelines/${enc(name)}` + (version ? `?version=${version}` : '')),
      api.get<S['PipelineRunList']>(`/projects/${p}/pipeline-runs?pipeline=${enc(name)}&limit=${HISTORY}`),
    ]);
    return { latest, def, runs: runs.items };
  }, (d) => d.runs.some((r) => ACTIVE.has(r.status)));

  return (
    <QueryView query={query}>
      {({ latest, def, runs }) => {
        const st = runStats(runs);
        const steps = def.steps.map((s) => ({ step: s.name, status: 'DEFINED', depends_on: s.depends_on, detail: `job: ${s.job}` }));
        return (
          <>
            <div className="page-head">
              <h1>{name}</h1>
              <label className="inline">Version
                <select value={String(def.version)} data-testid="pipeline-version" onChange={(e) => update({ version: e.target.value === String(latest.version) ? '' : e.target.value })}>
                  {Array.from({ length: latest.version }, (_, i) => latest.version - i).map((v) => (
                    <option key={v} value={v}>{`v${v}${v === latest.version ? ' (latest)' : ''}`}</option>))}
                </select>
              </label>
              <div className="actions">
                <button className="btn primary" type="button" data-testid="run-this-pipeline" disabled={!access.may('operator')} title={access.why('operator')}
                  onClick={() => runPipeline([{ name, version: def.version }], name, def.version === latest.version ? undefined : def.version)}>
                  {def.version === latest.version ? 'Run pipeline' : `Run v${def.version}`}
                </button>
              </div>
            </div>
            <p className="sub">{`${def.steps.length} steps · registered `}<Time iso={def.created_at} /></p>
            <div className="tiles">
              <div className="tile"><div className="n">{st.successRate == null ? '—' : pct(st.successRate, 0)}</div><div className="l">{`Success, last ${st.finished} finished`}</div></div>
              <div className="tile"><div className="n">{fmtDuration(st.medianSeconds)}</div><div className="l">Typical duration</div></div>
              <div className="tile"><div className="n">{st.lastSuccess ? <Time iso={st.lastSuccess.created_at} /> : '—'}</div><div className="l">Last success</div></div>
              <div className="tile"><div className="n">{st.lastFailure ? <Time iso={st.lastFailure.created_at} /> : '—'}</div><div className="l">Last failure</div></div>
            </div>
            <div className="card" data-testid="definition">
              <h2>Definition</h2>
              <Dag steps={steps} selected={selected} onSelect={setSelected} label={`Steps of ${name} v${def.version}`} />
              <Table head={['Step', 'Runs job', 'Depends on']}>
                {def.steps.map((s) => (
                  <tr key={s.name} className={s.name === selected ? 'sel' : ''}>
                    <td>{s.name}</td><td><a href={`${routes.project(project)}/jobs/${enc(s.job)}`}>{s.job}</a></td>
                    <td className="muted">{s.depends_on.join(', ') || '—'}</td>
                  </tr>))}
              </Table>
              <Snippet label="This definition as an API call (for CI)" code={pipelineCurl(project, name, def.steps)} />
            </div>
            {runs.length >= 2 && (
              <div className="section card">
                <h2>{`Last ${runs.length} runs`}</h2>
                <RunHistory runs={runs.map((r) => ({ id: r.id, status: r.status, created_at: r.created_at, duration_seconds: r.duration_seconds,
                  href: routes.pipelineRun(project, r.id), label: `${name} ${shortId(r.id)}` }))} />
              </div>)}
            <Section title="Recent runs" testid="pipeline-runs">
              {runs.length ? <PipelineRunTable project={project} rows={runs} /> : <Empty>Never run.</Empty>}
              {runs.length >= HISTORY && <p className="more"><a href={`${routes.project(project)}/runs?name=${enc(name)}`}>All runs of {name}</a></p>}
            </Section>
          </>
        );
      }}
    </QueryView>
  );
}

function pipelineCurl(project: string, name: string, steps: { name: string; job: string; depends_on: string[] }[]) {
  const body = JSON.stringify({ name, steps: steps.map((s) => ({ name: s.name, job: s.job, depends_on: s.depends_on })) }, null, 2);
  return `curl -X POST ${location.origin}/projects/${project}/pipelines \\\n  -H 'content-type: application/json' \\\n  -d '${body}'`;
}

