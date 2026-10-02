import { api, ApiError, enc, type S } from '../api/client';
import { Badge, Empty, Kv, Section, Snippet, Table, Time } from '../components/bits';
import { useOverlays } from '../components/overlays';
import { useCrumbs } from '../lib/chrome';
import { fmtDuration, go, parsePairs, pct, routes, shellQuote, splitCommand } from '../lib/format';
import { QueryView, useLiveQuery } from '../lib/query';
import { runStats } from '../lib/stats';
import { JobRunTable } from './Runs';

const ACTIVE = new Set(['PENDING', 'SUBMITTED', 'RUNNING']);
const HISTORY = 20;

function useStartJob(project: string) {
  const { toast } = useOverlays();
  return async (job: string) => {
    try {
      const run = await api.post<S['RunOut']>(`/projects/${enc(project)}/jobs/${enc(job)}/runs`, {});
      toast(`${job} started`);
      go(routes.jobRun(project, run.id));
    } catch (error) { toast(error instanceof Error ? error.message : 'Could not start the job', 'bad'); }
  };
}

export function JobsPage({ project }: { project: string }) {
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) }, { label: 'Jobs' }]);
  const p = enc(project);
  const { form, toast } = useOverlays();
  const start = useStartJob(project);
  const query = useLiveQuery(['jobs', project], async () => {
    const [jobs, projects] = await Promise.all([api.get<S['JobList']>(`/projects/${p}/jobs`), api.get<S['ProjectList']>('/projects?limit=200')]);
    const runs = await Promise.all(jobs.items.map((j) => api.get<S['RunList']>(`/projects/${p}/runs?job=${enc(j.name)}&limit=${HISTORY}`).then((r) => r.items)));
    return { ready: projects.items.find((x) => x.name === project)?.status === 'READY', rows: jobs.items.map((job, i) => ({ job, runs: runs[i] ?? [] })) };
  }, (d) => d.rows.some((r) => r.runs.some((x) => ACTIVE.has(x.status))));

  async function newJob() {
    const job = await form<S['JobOut']>({
      title: 'New job', submitLabel: 'Create job',
      intro: 'A job is a container image and a command. Run it on its own or as a step of a pipeline.',
      fields: [
        { name: 'name', label: 'Name', required: true, pattern: '^[a-z][a-z0-9]*(-[a-z0-9]+)*$', placeholder: 'train-model',
          hint: 'Lowercase letters, digits and dashes; pipelines refer to the job by this name.' },
        { name: 'image', label: 'Image', required: true, placeholder: 'registry.example.com/team/train:sha-a83d2c1', hint: 'Pin a tag or digest, so a rerun runs the same code.' },
        { name: 'command', label: 'Command', placeholder: 'python -m train --epochs 3', hint: 'Optional; the image entrypoint is used when empty. Quotes group words.' },
        { name: 'cpu', label: 'CPU', placeholder: '2', hint: 'Cores (2, 500m). Optional.' },
        { name: 'memory', label: 'Memory', placeholder: '4Gi', hint: 'Optional.' },
        { name: 'env', label: 'Environment', type: 'textarea', placeholder: 'MODEL_NAME=scorer', hint: 'One KEY=value per line. Not for secrets.' },
      ],
      submit: (v) => {
        let command: string[]; let env: Record<string, string>;
        try { command = splitCommand(v.command ?? ''); env = parsePairs(v.env ?? ''); }
        catch (e) { throw new ApiError(422, 'invalid_argument', e instanceof Error ? e.message : 'invalid input'); }
        const resources: Record<string, string> = {};
        if (v.cpu) resources.cpu = v.cpu;
        if (v.memory) resources.memory = v.memory;
        return api.post<S['JobOut']>(`/projects/${p}/jobs`, { name: v.name, image: v.image, command, resources, env });
      },
    });
    if (job) { toast(`Job ${job.name} created`); go(`${routes.project(project)}/jobs/${enc(job.name)}`); }
  }

  return (
    <QueryView query={query}>
      {({ ready, rows }) => (
        <>
          <div className="page-head">
            <h1>Jobs</h1>
            <div className="actions"><button className="btn primary" type="button" data-testid="new-job" onClick={newJob}>New job</button></div>
          </div>
          <p className="sub">Containers the platform can run, alone or as pipeline steps.</p>
          {rows.length === 0 ? <Empty>No jobs yet. Create one with “New job”.</Empty> : (
            <Table testid="jobs" head={['Job', 'Image', 'Resources', 'Last run', 'Success rate', 'Typical duration', '']}>
              {rows.map(({ job, runs }) => {
                const st = runStats(runs);
                const href = `${routes.project(project)}/jobs/${enc(job.name)}`;
                return (
                  <tr key={job.id} className="click" data-testid="job-row" onClick={() => go(href)}>
                    <td><a href={href}>{job.name}</a></td>
                    <td className="mono small clip" title={job.image}>{job.image}</td>
                    <td className="mono small">{Object.entries(job.resources).map(([k, v]) => `${k} ${v}`).join(' · ') || '—'}</td>
                    <td>{st.last ? <><Badge status={st.last.status} /> <Time iso={st.last.created_at} /></> : <span className="muted">never run</span>}</td>
                    <td className="num">{st.successRate == null ? '—' : pct(st.successRate, 0)}</td>
                    <td className="num">{fmtDuration(st.medianSeconds)}</td>
                    <td className="num"><button className="btn small" type="button" disabled={!ready} title={ready ? '' : 'The project is not ready yet'}
                      onClick={(e) => { e.stopPropagation(); start(job.name); }}>Start</button></td>
                  </tr>);
              })}
            </Table>
          )}
        </>
      )}
    </QueryView>
  );
}

export function JobPage({ project, name }: { project: string; name: string }) {
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) },
    { label: 'Jobs', href: `${routes.project(project)}/jobs` }, { label: name }]);
  const p = enc(project);
  const start = useStartJob(project);
  const query = useLiveQuery(['job', project, name], async () => {
    const [job, runs] = await Promise.all([
      api.get<S['JobOut']>(`/projects/${p}/jobs/${enc(name)}`),
      api.get<S['RunList']>(`/projects/${p}/runs?job=${enc(name)}&limit=${HISTORY}`),
    ]);
    return { job, runs: runs.items };
  }, (d) => d.runs.some((r) => ACTIVE.has(r.status)));

  return (
    <QueryView query={query}>
      {({ job, runs }) => {
        const st = runStats(runs);
        return (
          <>
            <div className="page-head">
              <h1>{job.name}</h1>
              <div className="actions"><button className="btn primary" type="button" data-testid="start-this-job" onClick={() => start(job.name)}>Start job</button></div>
            </div>
            <p className="sub">{'Defined '}<Time iso={job.created_at} />
              {st.successRate != null && ` · ${pct(st.successRate, 0)} of the last ${st.finished} finished runs succeeded · typically ${fmtDuration(st.medianSeconds)}`}</p>
            <div className="card">
              <h2>Definition</h2>
              <Kv entries={[
                ['Image', <span className="mono">{job.image}</span>],
                ['Command', job.command.length ? <span className="mono">{job.command.map(shellQuote).join(' ')}</span> : <span className="muted">image entrypoint</span>],
                ['Resources', Object.keys(job.resources).length ? Object.entries(job.resources).map(([k, v]) => <span className="chip mono" key={k}>{`${k} ${v}`}</span>) : <span className="muted">platform defaults</span>],
                ['Environment', Object.keys(job.env).length ? Object.entries(job.env).map(([k, v]) => <span className="chip mono" key={k}>{`${k}=${v}`}</span>) : <span className="muted">none</span>],
              ]} />
              <Snippet label="Start this job from a script" code={`curl -X POST ${location.origin}/projects/${project}/jobs/${job.name}/runs \\\n  -H 'Idempotency-Key: <unique-per-attempt>'`} />
            </div>
            <Section title="Recent runs" testid="job-runs">
              {runs.length ? <JobRunTable project={project} rows={runs} /> : <Empty>Never run.</Empty>}
              {runs.length >= HISTORY && <p className="more"><a href={`${routes.project(project)}/runs?kind=job&name=${enc(name)}`}>All runs of {name} →</a></p>}
            </Section>
          </>
        );
      }}
    </QueryView>
  );
}
