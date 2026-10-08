import { useQuery } from '@tanstack/react-query';
import { useRef } from 'react';
import { useSearchState } from '../lib/search';
import { api, ApiError, enc, type S } from '../api/client';
import { Alert, Badge, Empty, Table, Time } from '../components/bits';
import { useDeploy } from '../components/Deploy';
import { MetricLegend, MetricStrip } from '../components/charts/MetricStrip';
import { useOverlays } from '../components/overlays';
import { useAccess } from '../lib/me';
import { useCrumbs } from '../lib/chrome';
import { formatThresholds, lowerIsBetter, num, parsePairs, parseThresholds, routes, shortId, type Thresholds } from '../lib/format';
import { QueryView, useAct, useLiveQuery } from '../lib/query';

const EVALUATABLE = new Set(['REGISTERED', 'EVALUATING']);
type Version = S['ModelVersionOut'];

const metricNames = (versions: Version[]) =>
  [...new Set(versions.flatMap((v) => v.evaluations.flatMap((e) => Object.keys(e.metrics))))].sort();
const latest = (v: Version) => v.evaluations[v.evaluations.length - 1];

export function ModelPage({ project, name, functions = false }: { project: string; name: string; functions?: boolean }) {
  const access = useAccess(project);
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) },
    { label: functions ? 'Functions' : 'Models', href: `${routes.project(project)}/${functions ? 'functions' : 'models'}` }, { label: name }]);
  const base = `/projects/${enc(project)}/models/${enc(name)}`;
  const { confirm, toast, form } = useOverlays();
  const deploy = useDeploy(project);
  const act = useAct();
  const [selection, setSelection] = useSearchState({ version: '' });
  const picked = selection.version || null;
  const setPicked = (id: string | null) => setSelection({ version: id ?? '' });
  const versionsNow = useRef<Version[]>([]);

  const query = useLiveQuery(['model', project, name], async () => {
    const [model, list] = await Promise.all([api.get<S['ModelOut']>(base), api.get<{ items: { id: string }[] }>(`${base}/versions`)]);
    const versions = await Promise.all(list.items.map((v) => api.get<Version>(`/model-versions/${v.id}`)));
    return { model, versions: versions.sort((a, b) => b.version - a.version) };
  }, (d) => d.versions.some((v) => v.status === 'EVALUATING'));

  async function editThresholds(current: Thresholds) {
    const done = await form<S['ModelOut']>({
      title: 'Acceptance thresholds', submitLabel: 'Save',
      intro: 'Applies to versions evaluated from now on. Versions already accepted or rejected keep their result.',
      fields: [{ name: 'thresholds', label: 'Thresholds', type: 'textarea', value: formatThresholds(current), placeholder: 'auc >= 0.9\nrmse <= 0.3',
        hint: 'One per line: metric >= number or metric <= number.' }],
      submit: (v) => {
        let thresholds;
        try { thresholds = parseThresholds(v.thresholds ?? ''); } catch (e) { throw new ApiError(422, 'invalid_argument', e instanceof Error ? e.message : 'invalid'); }
        return api.put<S['ModelOut']>(`${base}/thresholds`, { thresholds });
      },
      preview: (v) => <ThresholdPreview text={v.thresholds ?? ''} versions={versionsNow.current} />,
    });
    if (done) { toast('Thresholds saved'); await query.refetch(); }
  }

  async function registerImage() {
    const done = await form<S['ModelVersionSummary']>({
      title: 'Register an image', submitLabel: 'Register',
      intro: 'A function version is a container image. It listens on its port and answers POST / with JSON. There is nothing to evaluate, so it can be deployed at once; a canary of it still has to pass its gates.',
      fields: [{ name: 'image', label: 'Image', required: true, placeholder: 'ghcr.io/acme/ticket-router@sha256:…',
        hint: 'Use a registry image with @sha256: and its 64-character digest. Tags can move; the digest keeps deploys and rollbacks on the same code.' }],
      submit: (v) => api.post<S['ModelVersionSummary']>(`${base}/images`, { image: v.image }),
    });
    if (done) { toast(`Registered v${done.version}; deploy it from its row`); await query.refetch(); }
  }

  async function registerFromHub(model: S['ModelOut']) {
    const thresholds = model.thresholds as Thresholds;
    const done = await form<S['ModelVersionSummary']>({
      title: 'Register a version from the hub', submitLabel: 'Register',
      intro: 'A version is a model on the Hugging Face Hub, pinned to a revision so it always means the same weights, with the results of your own offline evaluation. It is judged on those results like any other version.',
      fields: [
        { name: 'source', label: 'Source', required: true, pattern: '^hf://[A-Za-z0-9][\\w.-]*/[\\w.-]+@[0-9a-f]{40}$', placeholder: 'hf://org/model@<full-commit-sha>',
          hint: 'A full 40-character lowercase commit SHA is required. Gated models need the project’s Hugging Face token.' },
        { name: 'metrics', label: 'Evaluation results', type: 'textarea', placeholder: Object.keys(thresholds).map((m) => `${m} = 0.8`).join('\n') || 'helpfulness = 0.82',
          hint: 'One per line: metric = number. Use the metrics the thresholds name.' },
      ],
      preview: (v) => <HubPreview text={v.metrics ?? ''} thresholds={thresholds} />,
      submit: (v) => {
        let metrics: Record<string, number>;
        try {
          metrics = Object.fromEntries(Object.entries(parsePairs(v.metrics ?? '')).map(([k, x]) => {
            const n = Number(x);
            if (!Number.isFinite(n)) throw new Error(`${k}: "${x}" is not a number`);
            return [k, n];
          }));
        } catch (e) { throw new ApiError(422, 'invalid_argument', e instanceof Error ? e.message : 'invalid'); }
        return api.post<S['ModelVersionSummary']>(`${base}/versions`, { source: v.source, metrics });
      },
    });
    if (done) { toast(`Registered v${done.version}; evaluate it to see if it qualifies`); await query.refetch(); }
  }

  return (
    <QueryView query={query}>
      {({ model, versions }) => {
        versionsNow.current = versions;
        const selected = picked ?? (versions.find((v) => v.status === 'CHAMPION') ?? versions[0])?.id ?? null;
        const champion = versions.find((v) => v.status === 'CHAMPION');
        const detail = versions.find((v) => v.id === selected);
        const metrics = metricNames(versions);
        const championEval = champion ? latest(champion) : undefined;
        return (
          <>
            <div className="page-head">
              <h1>{model.name}</h1>{model.champion && <Badge status="CHAMPION" />}
              <div className="actions">
                {model.kind === 'function' && (
                  <button className="btn" data-testid="register-image" disabled={!access.may('operator')} title={access.why('operator') ?? 'Register a version: a container image'}
                    onClick={() => registerImage()}>Register image</button>)}
                {model.kind === 'llm' && (
                  <button className="btn" data-testid="register-hub" disabled={!access.may('operator')} title={access.why('operator') ?? 'Register a version from the Hugging Face Hub'}
                    onClick={() => registerFromHub(model)}>Register from hub</button>)}
                {model.kind !== 'function' && (<>
                <button className="btn" data-testid="discover" disabled={!access.may('operator')} title={access.why('operator')} onClick={() => act(async () => {
                  const r = await api.post<{ created: unknown[] }>(`${base}/discover`);
                  toast(r.created.length ? `Registered ${r.created.length} new version(s)` : 'No new versions found');
                }, 'Checked the registry')}>Discover versions</button>
                <button className="btn" data-testid="edit-thresholds" disabled={!access.may('admin')} title={access.why('admin')} onClick={() => editThresholds(model.thresholds as Thresholds)}>Edit thresholds</button>
                </>)}
              </div>
            </div>
            <p className="sub">
              {model.kind === 'function' && model.function && (
                <span className="chip" data-testid="function-serving">{`Function · ${model.function.min_scale}–${model.function.max_scale} replicas · ${model.function.concurrency} at once · port ${model.function.port} · ${model.function.requests?.cpu ?? '100m'}/${model.function.requests?.memory ?? '128Mi'} requested · ${model.function.limits?.cpu ?? '1'}/${model.function.limits?.memory ?? '512Mi'} limit`}</span>)}
              {model.kind === 'llm' && model.llm && (
                <span className="chip" data-testid="llm-serving">{`LLM · ${model.llm.gpus} GPU${model.llm.gpus === 1 ? '' : 's'} per replica · context ${model.llm.context_length ?? 'model default'}`}</span>)}
              {model.kind !== 'function' && <>
              Registry name <span className="mono">{model.registry_name}</span>{' · acceptance thresholds '}
              {Object.keys(model.thresholds).length
                ? Object.entries(model.thresholds).map(([m, t]) => (
                  <span className="chip mono" key={m}>{`${m} ${t.min != null ? '≥ ' + t.min : ''}${t.min != null && t.max != null ? ', ' : ''}${t.max != null ? '≤ ' + t.max : ''}`}</span>))
                : <span className="muted">none set</span>}
              </>}
            </p>
            {model.alias_drift && <Alert>{`Registry alias drift: ${model.alias_drift}`}</Alert>}
            {versions.length ? (
              <Table testid="versions" head={['Version', 'Status', ...metrics, 'Source', '']}>
                {versions.map((v) => {
                  const ev = latest(v);
                  return (
                    <tr key={v.id} className={`click${v.id === selected ? ' sel' : ''}`} data-testid="version-row" data-version={v.version} onClick={() => setPicked(v.id)}>
                      <td className="mono">{`v${v.version}`}</td><td><Badge status={v.status} /></td>
                      {metrics.map((m) => <td key={m} className="num mono"><Metric value={ev?.metrics[m]} base={v.id === champion?.id ? undefined : championEval?.metrics[m]}
                        lower={lowerIsBetter(m, model.thresholds as Thresholds)} /></td>)}
                      <td>{v.source_uri
                        ? <span className="mono small clip" title={v.source_uri} data-testid="version-source">{v.source_uri.replace(/^hf:\/\//, '')}</span>
                        : v.source_pipeline_run_id
                          ? <a href={routes.pipelineRun(project, v.source_pipeline_run_id)} onClick={(e) => e.stopPropagation()}>{shortId(v.source_pipeline_run_id)}</a> : '—'}</td>
                      <td className="num">
                        {EVALUATABLE.has(v.status) && (
                          <button className="btn small" data-testid="evaluate" disabled={!access.may('operator')} title={access.why('operator')} onClick={(e) => { e.stopPropagation(); act(() => api.post(`/model-versions/${v.id}/evaluate`), `Evaluated v${v.version}`); }}>Evaluate</button>)}
                        {v.status === 'CANDIDATE' && (
                          <button className="btn small primary" data-testid="promote" disabled={!access.may('operator')} title={access.why('operator')} onClick={async (e) => {
                            e.stopPropagation();
                            const body = champion ? `v${champion.version} (current champion) will be archived and v${v.version} becomes the champion.` : `v${v.version} becomes the first champion.`;
                            if (await confirm({ title: `Promote v${v.version}?`, body: `${body} The registry alias follows shortly.`, confirmLabel: 'Promote' }))
                              await act(() => api.post(`/model-versions/${v.id}/promote`), `v${v.version} is now the champion`);
                          }}>Promote</button>)}
                        {(v.status === 'CANDIDATE' || v.status === 'CHAMPION') && (
                          <button className="btn small" data-testid="deploy-version" disabled={!access.may('operator')}
                            title={access.why('operator') ?? 'Deploy this version, as a canary or directly'}
                            onClick={(e) => { e.stopPropagation(); deploy({ model: name, version: v.version }); }}>Deploy</button>)}
                      </td>
                    </tr>);
                })}
              </Table>
            ) : <Empty>No versions yet. Train and register one, then press “Discover versions”.</Empty>}
            {metrics.length > 0 && versions.some((v) => latest(v)) && (
              <div className="section card" data-testid="compare">
                <h2>How the versions compare</h2>
                <p className="muted small">Each version's latest evaluation, against the acceptance threshold. Click a version for its evaluations.</p>
                {metrics.map((m) => (
                  <MetricStrip key={m} metric={m} selected={selected} onSelect={setPicked}
                    bound={(model.thresholds as Thresholds)[m]} lowerIsBetter={lowerIsBetter(m, model.thresholds as Thresholds)}
                    points={versions.flatMap((v) => {
                      const value = latest(v)?.metrics[m];
                      return value == null ? [] : [{ id: v.id, version: v.version, status: v.status, value }];
                    })} />))}
                <MetricLegend />
              </div>)}
            {detail && <Detail v={detail} project={project} />}
          </>
        );
      }}
    </QueryView>
  );
}

function Detail({ v, project }: { v: Version; project: string }) {
  if (v.source_uri && !v.source_uri.startsWith('hf://')) {
    return (
      <div className="section card" data-testid="version-detail">
        <h2>{`v${v.version}`}</h2>
        <p>Container image <span className="mono">{v.source_uri}</span>.</p>
        <p className="muted small">A function is not evaluated against thresholds: it is a candidate as soon as it is registered. Deploy it as a canary to have its error rate and latency judged in real traffic before it takes over.</p>
      </div>
    );
  }
  return (
    <div className="cols section" data-testid="version-detail">
      <div className="card">
        <h2>{`v${v.version} evaluations`}</h2>
        <Lineage v={v} project={project} />
        {v.evaluations.length ? v.evaluations.map((e) => (
          <div key={e.id}>
            <div><Badge status={e.status} />{' '}<Time iso={e.created_at} /></div>
            <table className="t" style={{ margin: '8px 0' }}>
              <thead><tr>{['Metric', 'Value', 'Required', ''].map((c, i) => <th key={i}>{c}</th>)}</tr></thead>
              <tbody>
                {e.checks.map((c) => (
                  <tr key={c.metric}>
                    <td>{c.metric}</td><td className="mono">{c.value == null ? 'not reported' : num(c.value, 3)}</td>
                    <td className="mono">{[c.min != null ? `≥ ${c.min}` : null, c.max != null ? `≤ ${c.max}` : null].filter(Boolean).join(', ')}</td>
                    <td><Badge status={c.passed ? 'PASSED' : 'FAILED'} /></td>
                  </tr>))}
              </tbody>
            </table>
          </div>)) : <p className="muted">Not evaluated yet.</p>}
      </div>
      <div className="card">
        <h2>Promotion history</h2>
        {v.promotions.length ? (
          <ul className="timeline">
            {v.promotions.map((p) => (
              <li key={p.id}><Time iso={p.created_at} />
                <span>Promoted to champion{p.previous_champion_id ? ' (replaced the previous champion)' : ' (first champion)'}</span><Badge status={p.status} /></li>))}
          </ul>) : <p className="muted">Never promoted.</p>}
      </div>
    </div>
  );
}

/** A metric value, with how it compares to the champion's (green when better). */
function Metric({ value, base, lower }: { value: number | undefined; base: number | undefined; lower: boolean }) {
  if (value == null) return <>—</>;
  if (base == null || value === base) return <>{num(value, 3)}</>;
  const diff = value - base;
  const better = lower ? diff < 0 : diff > 0;
  return (
    <>{num(value, 3)} <span className={`delta ${better ? 'up' : 'down'}`} title="Compared with the champion">{`${diff > 0 ? '+' : ''}${num(diff, 3)}`}</span></>
  );
}

/** Which versions the edited thresholds would accept, judged on their latest evaluation. */
function ThresholdPreview({ text, versions }: { text: string; versions: Version[] }) {
  let parsed: Thresholds;
  try { parsed = parseThresholds(text); } catch (e) { return <p className="fail">{e instanceof Error ? e.message : 'Invalid thresholds'}</p>; }
  const rules = Object.entries(parsed);
  const judged = versions.filter((v) => latest(v)).map((v) => {
    const metrics = latest(v)!.metrics;
    const misses = rules.flatMap(([m, b]) => {
      const value = metrics[m];
      if (value == null) return [`${m} not reported`];
      if (b.min != null && value < b.min) return [`${m} ${num(value, 3)} < ${b.min}`];
      if (b.max != null && value > b.max) return [`${m} ${num(value, 3)} > ${b.max}`];
      return [];
    });
    return { v, misses };
  });
  return (
    <div>
      <p>{rules.length ? 'Judged on each version\'s latest evaluation, with these thresholds:' : 'With no thresholds, every version that is evaluated passes.'}</p>
      {rules.length > 0 && (
        <ul data-testid="threshold-preview">
          {judged.map(({ v, misses }) => (
            <li key={v.id} className={misses.length ? 'fail' : 'pass'}>
              {`v${v.version} (${v.status.toLowerCase()}): ${misses.length ? `would fail, ${misses.join(', ')}` : 'would pass'}`}
            </li>))}
        </ul>)}
      <p className="muted">Only versions evaluated from now on use the new thresholds. Results already recorded do not change.</p>
    </div>
  );
}

/** Where this version came from: the pipeline run that trained it, and the commit it ran. */
function Lineage({ v, project }: { v: Version; project: string }) {
  const run = useQuery({
    queryKey: ['pipeline-run', v.source_pipeline_run_id], enabled: Boolean(v.source_pipeline_run_id),
    queryFn: () => api.get<S['PipelineRunOut']>(`/pipeline-runs/${v.source_pipeline_run_id}`),
  });
  if (!v.source_pipeline_run_id) return <p className="lineage muted" data-testid="lineage">Registered outside a platform pipeline run, so its training run is unknown.</p>;
  const r = run.data;
  return (
    <p className="lineage" data-testid="lineage">
      {'Trained by '}<a href={routes.pipelineRun(project, v.source_pipeline_run_id)}>{r ? `${r.pipeline} v${r.pipeline_version} ${shortId(r.id)}` : shortId(v.source_pipeline_run_id)}</a>
      {r?.commit_sha && <>{' at commit '}<span className="mono">{r.commit_sha}</span></>}
      {r && <>{', '}<Time iso={r.finished_at ?? r.created_at} /></>}
    </p>
  );
}

/** Which thresholds the entered results would pass, before the version is registered. */
function HubPreview({ text, thresholds }: { text: string; thresholds: Thresholds }) {
  let given: Record<string, string>;
  try { given = parsePairs(text); } catch (e) { return <p className="fail">{e instanceof Error ? e.message : 'Invalid results'}</p>; }
  const rows = Object.entries(thresholds).map(([metric, t]) => {
    const value = given[metric] == null ? undefined : Number(given[metric]);
    const ok = value != null && Number.isFinite(value) && (t.min == null || value >= t.min) && (t.max == null || value <= t.max);
    return { metric, value, ok, need: `${t.min != null ? `≥ ${t.min}` : ''}${t.min != null && t.max != null ? ', ' : ''}${t.max != null ? `≤ ${t.max}` : ''}` };
  });
  if (!rows.length) return <p>This model has no thresholds, so evaluation will have nothing to judge against.</p>;
  const passes = rows.every((r) => r.ok);
  return (
    <>
      <p className={passes ? 'pass' : 'fail'}>{passes ? 'Evaluated, this version would become a candidate.' : 'Evaluated, this version would be rejected.'}</p>
      <ul>{rows.map((r) => <li key={r.metric} className={r.ok ? 'pass' : 'fail'}>{`${r.metric}: ${r.value ?? 'missing'} (needs ${r.need})`}</li>)}</ul>
    </>
  );
}
